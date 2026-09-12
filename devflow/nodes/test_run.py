"""执行闭环节点：apply_code（diff 落盘）+ test_run（真实测试执行）。

对应 SPEC 阶段三的「代码变更落盘」与「测试报告回填」缺口：
  - apply_code : 把 code_gen 产出的 unified diff 应用到目标项目（all-or-nothing + 备份）。
                 应用失败不 abort——重试一次后降级为「仅设计」模式继续（code_apply.applied=False）。
  - test_run   : 在落盘后的目标项目里跑真实 pytest，把 TestReport.run 从「场景设计数」
                 换成真实执行数（passed/failed/skipped/时长/失败明细）。
                 失败 → 回 code_gen 修复（带失败摘要），连续失败超限 → 带失败报告进人工验收。

错误通道约定：业务性失败（diff 应用不了 / 测试没过）不占用 last_error_code（那是基础设施
错误的通道），走业务路由；只有 NODE.CONTEXT 级 bug 才进错误分流。
"""
from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any

from .. import code_apply
from ..config import settings
from ..errors import DevFlowError, ExecApplyFailedError, wrap_exception
from ..state import GlobalState
from ..test_runner import run_pytest

logger = logging.getLogger(__name__)

# 测试失败摘要里最多带的失败条目数
_MAX_FAILURE_LINES = 5


def _build_patches(changes: list[dict[str, Any]]) -> tuple[list[code_apply.FilePatch], list[str]]:
    """把 code_changes 转成 FilePatch 列表。

    安全规则：content_after 整文件直写仅允许两类（防止 mock 内容覆盖真实源码）——
    1. 新建文件；2. 带 in_place 标志的变更（pi 等就地写入型 Provider，落盘=幂等回写现状）。
    其余修改/删除必须走 diff。返回 (patches, problems)，problems 非空表示有变更无法构造补丁。
    """
    patches: list[code_apply.FilePatch] = []
    problems: list[str] = []
    for ch in changes:
        fp = ch.get("file_path") or ""
        if not fp:
            problems.append("存在缺 file_path 的变更")
            continue
        action = str(ch.get("action") or "update")
        content_after = ch.get("content_after")
        diff_text = ch.get("diff") or ch.get("diff_unified") or ""
        in_place = bool(ch.get("in_place"))
        if action in ("create", "created") and content_after:
            patches.append(code_apply.patch_from_content(fp, str(content_after)))
            continue
        if action == "update" and in_place and content_after:
            # 就地写入型 Provider（pi）：文件已是目标状态，diff 基线是 HEAD 而非当前磁盘，
            # 重放必然上下文失配 → 用 content_after 直写（幂等），diff 仅留作展示
            patches.append(code_apply.patch_from_content(fp, str(content_after)))
            continue
        if not diff_text.strip():
            problems.append(f"{fp}: 没有 diff 无法落盘")
            continue
        try:
            patches.extend(code_apply.parse_unified_diff(diff_text))
        except code_apply.DiffApplyError as e:
            problems.append(f"{fp}: {e}")
    return patches, problems


def make_apply_code_node():
    """diff 落盘节点。

    读取 state: requirement.project_root, code_changes
    写入 state: code_apply, current_stage + SPEC 5 错误字段（仅基础设施级错误）
    """

    async def apply_code_node_async(state: GlobalState) -> dict[str, Any]:
        req = state.get("requirement") or {}
        project_root = str(req.get("project_root") or "")
        changes = state.get("code_changes") or []
        if not changes:
            return {
                "code_apply": {"applied": False, "reason": "没有代码变更"},
                "current_stage": "test",
                "last_error": None,
                "last_error_code": None,
                "last_error_retryable": None,
            }

        skip: dict[str, Any] = {
            "applied": False,
            "files": [],
            "backup_dir": None,
        }

        # 功能开关 / 目标项目不存在 → 优雅跳过（保持「仅设计」模式可用）
        if not settings.APPLY_CODE_ENABLED:
            skip["reason"] = "APPLY_CODE_ENABLED=0，落盘已关闭"
            return {"code_apply": skip, "current_stage": "test"}
        if not project_root or not Path(project_root).is_dir():
            skip["reason"] = f"project_root 不可用: {project_root or '(空)'}"
            return {"code_apply": skip, "current_stage": "test"}

        patches, problems = _build_patches(changes)
        if problems:
            err = ExecApplyFailedError(
                "diff 无法构造补丁: " + "; ".join(problems),
            )
            return _apply_err_out(state, err)

        try:
            report = code_apply.apply_patches(project_root, patches, backup=True)
        except code_apply.DiffApplyError as e:
            return _apply_err_out(
                state,
                ExecApplyFailedError(str(e), files=e.files or None),
            )
        except Exception as e:  # 文件系统级意外
            return _apply_err_out(state, wrap_exception(e, context="apply_code"))

        logger.info("apply_code: %d 个文件已落盘（备份 %s）",
                    len(report.get("files") or []), report.get("backup_dir"))
        return {
            "code_apply": report,
            "current_stage": "test",
            "last_error": None,
            "last_error_code": None,
            "last_error_retryable": None,
        }

    def apply_code_node(state: GlobalState) -> dict[str, Any]:
        return asyncio.run(apply_code_node_async(state))

    apply_code_node.__name__ = "apply_code_node"
    apply_code_node.async_version = apply_code_node_async  # type: ignore[attr-defined]
    return apply_code_node


def _apply_err_out(state: GlobalState, err: DevFlowError) -> dict[str, Any]:
    """apply_code 的错误出口：写错误字段 + code_apply 未应用标记（流程可降级继续）。"""
    retry_map = copy.deepcopy(state.get("retry_count") or {})
    retry_map["apply_code"] = retry_map.get("apply_code", 0) + 1
    return {
        "code_apply": {
            "applied": False,
            "files": [],
            "backup_dir": None,
            "reason": f"[{err.code}] {err.message}"[:500],
        },
        "last_error": f"[apply_code:{err.code}] {err.message}"[:500],
        "last_error_code": err.code,
        "last_error_retryable": err.retryable,
        "retry_count": retry_map,
        "current_stage": "test",
    }


def make_test_run_node():
    """真实测试执行节点。

    读取 state: requirement.project_root, code_apply, code_changes, test_report, retry_count
    写入 state: test_report（run 换成真实执行数）, code_changes.test_passed,
                test_failure（失败摘要，供 code_gen 回修）, current_stage, retry_count
    """

    async def test_run_node_async(state: GlobalState) -> dict[str, Any]:
        req = state.get("requirement") or {}
        project_root = str(req.get("project_root") or "")
        report = dict(state.get("test_report") or {})
        run = dict(report.get("run") or {})
        code_changes = state.get("code_changes") or []
        retry_map = copy.deepcopy(state.get("retry_count") or {})

        def _finish(*, stage: str, executed: bool | None = None,
                    skip_reason: str | None = None,
                    test_failure: str | None = None,
                    test_passed: bool | None = None,
                    inc_retry: bool = False) -> dict[str, Any]:
            if executed is not None:
                run["executed"] = executed
            if skip_reason is not None:
                run["skip_reason"] = skip_reason
            new_report = {**report, "run": run}
            # 显式写回（含 None）：覆盖上一轮残留的 test_passed，避免陈旧的 ✓/✗ 误导人工验收
            updated_changes = [{**ch, "test_passed": test_passed} for ch in code_changes]
            out: dict[str, Any] = {
                "test_report": new_report,
                "code_changes": updated_changes,
                "test_failure": test_failure,
                "current_stage": stage,
            }
            if inc_retry:
                retry_map["test_run"] = retry_map.get("test_run", 0) + 1
                out["retry_count"] = retry_map
            return out

        # ── 前置检查：任一不满足 → 跳过执行（不误判失败），带原因进人工验收 ──
        if not settings.TEST_RUN_ENABLED:
            return _finish(stage="review", executed=False,
                           skip_reason="TEST_RUN_ENABLED=0，执行已关闭")
        if not project_root or not Path(project_root).is_dir():
            from ..schemas import has_project_code

            if not has_project_code(req):
                # 仅需求模式：没有目标项目，测试场景本来就是设计交付物，不算异常
                return _finish(stage="review", executed=False,
                               skip_reason="仅需求模式（未提供项目代码），测试场景不执行，"
                                           "以用例设计为交付物")
            return _finish(stage="review", executed=False,
                           skip_reason=f"project_root 不可用: {project_root or '(空)'}")
        if not (state.get("code_apply") or {}).get("applied"):
            reason = (state.get("code_apply") or {}).get("reason") or "代码未落盘"
            return _finish(stage="review", executed=False, skip_reason=f"跳过执行（{reason}）")

        # ── 真实执行 ──
        try:
            res = await run_pytest(
                project_root,
                paths=settings.TEST_RUN_PATHS or None,
                timeout_sec=settings.TEST_RUN_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.exception("test_run 子进程异常")
            return _finish(stage="review", executed=False,
                           skip_reason=f"执行器异常: {e}")

        if not res.get("executed"):
            return _finish(stage="review", executed=False,
                           skip_reason=f"未执行: {res.get('error') or '未知原因'}")

        # ── 回填真实执行结果（覆盖 test_gen 的设计态数字）──
        run.update({
            "executed": True,
            "passed": res["passed"],
            "failed": res["failed"],
            "errors": res["errors"],
            "skipped": res["skipped"],
            "total": res["total"],
            "duration_sec": res["duration_sec"],
            "logs": res["logs"],
            "failures": res["failures"],
        })
        failures = res["failed"] + res["errors"]
        test_passed = res["total"] > 0 and failures == 0

        if failures > 0:
            summary = _summarize_failures(res["failures"], failures)
            return _finish(stage="test", test_failure=summary,
                           test_passed=False, inc_retry=True)

        # 通过（或没收集到用例）：清失败摘要 + 复位自动修复轮次。
        # total=0 时不声称 test_passed=True——「没有测试」不等于「测试通过」，交给人工判断。
        retry_map["test_run"] = 0
        out = _finish(
            stage="review",
            test_failure=None,
            test_passed=True if res["total"] > 0 else None,
        )
        out["retry_count"] = retry_map
        return out

    def test_run_node(state: GlobalState) -> dict[str, Any]:
        return asyncio.run(test_run_node_async(state))

    test_run_node.__name__ = "test_run_node"
    test_run_node.async_version = test_run_node_async  # type: ignore[attr-defined]
    return test_run_node


def _summarize_failures(failures: list[dict[str, Any]], total_failures: int) -> str:
    """把失败明细压成一段可拼进 code_gen instruction 的摘要。"""
    lines = [f"{total_failures} 个测试未通过，失败明细："]
    for f in (failures or [])[:_MAX_FAILURE_LINES]:
        msg = (f.get("message") or "").splitlines()
        first = msg[0][:200] if msg else ""
        lines.append(f"- {f.get('id', '?')}: {first}")
    if total_failures > _MAX_FAILURE_LINES:
        lines.append(f"-（其余 {total_failures - _MAX_FAILURE_LINES} 条见 test_report.run.failures）")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 路由函数（orchestrator conditional_edges 用）
# ═══════════════════════════════════════════════════════════════════

def route_after_code_apply(state: GlobalState) -> str:
    """落盘后路由：错误可重试且未超限 → retry（回 code_gen 重新生成 diff）；
    其余（成功 / 降级 / 功能关闭）→ continue（test_gen）。"""
    if state.get("last_error_code"):
        if state.get("last_error_retryable") is True:
            return "retry"
        return "continue"  # 不可重试也降级继续，靠人工验收兜底
    return "continue"


def route_after_test_run(state: GlobalState) -> str:
    """执行后路由：
      - executed 且有失败：未超自动修复轮数 → test_fail（回 code_gen）；超限 → review_failed
      - executed 且通过：test_ok
      - 未执行（关闭/跳过/环境问题）：skip
    """
    report = state.get("test_report") or {}
    run = report.get("run") or {}
    if not run.get("executed"):
        return "skip"
    failures = int(run.get("failed", 0)) + int(run.get("errors", 0))
    if failures == 0:
        return "test_ok"
    rounds = (state.get("retry_count") or {}).get("test_run", 0)
    if rounds <= settings.TEST_RUN_MAX_FIX_ROUNDS:
        return "test_fail"
    return "review_failed"
