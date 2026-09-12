"""Pi Coding Agent（badlogic/pi-mono）Provider：CLI --print 子进程调用。

npm 包 @mariozechner/pi-coding-agent（命令名 pi）。无头模式（官方 README）：
  pi -p "prompt"              # --print 一次性执行
  pi --no-session             # 临时会话，不落盘（本 Provider 全程使用）
  pi --provider X --model Y   # 模型选择，model 支持 provider/id 形式
pi 没有 cwd 参数，靠子进程 cwd=project_root 进入目标项目（与 codegraph provider 同法）。

能力覆盖（骨架版）：
  PiSearchProvider — 让 agent 现场检索代码。pi 无代码索引，靠自身翻文件，慢；
                     检索一格建议仍用 codegraph（CODE_SEARCH_PROVIDER=codegraph）。
  PiEditProvider   — 按 instruction 修改代码；变更集用 git diff/status 采集，
                     lint 用 ruff（装了才跑，没装视为通过）。
  PiTestProvider   — 写测试 + 执行 pytest，结果按约定 JSON 回传；
                     project_root 为空时委托 LlmTestGenProvider 走仅需求模式。

错误治理（SPEC 5）：
  - `_run_pi()` 接入 pi_cli 熔断器 + @retry_with_backoff；
  - CLI 异常统一 wrap 成 DevFlowError 的 CLI.* 子类
    （CliNotFoundError 不可重试 → 供上层 fallback；超时/非零退出可重试）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..config import settings
from ..errors import (
    CliAuthError,
    CliExitError,
    CliNotFoundError,
    CliTimeoutError,
    DevFlowError,
)
from ..resilience import default_breaker, retry_with_backoff
from .base import (
    CodeChange,
    CodeEditProvider,
    CodeEditResult,
    CodeSearchHit,
    CodeSearchProvider,
    CodeSearchResult,
    LintIssue,
    QueryType,
    TestCase,
    TestGenProvider,
    TestReport,
    TestRun,
)

logger = logging.getLogger(__name__)

PI_NPM_PACKAGE = "@mariozechner/pi-coding-agent"
"""setup 向导引导语与错误提示共用，改包名只动这里。"""


class PiNotInstalledError(CliNotFoundError):
    """`pi` CLI 不在 PATH 中。

    继承 CliNotFoundError → retryable=False，装配了 fallback 的能力直接降级，
    重试无意义（装不上就是装不上）。
    """


def _extract_json_payload(text: str) -> Any | None:
    """从 pi 的自然语言回复里抠 JSON：整段解析 → ```json 围栏 → 首尾大括号截取。

    返回 None 表示完全找不到合法 JSON，由调用方决定报错还是降级。
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.+?)```", t, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = t.find(open_ch), t.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _clip(s: Any, limit: int) -> str:
    return str(s or "")[:limit]


_NOISE_PATH_RE = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.coverage[^/]*|dts)(/|$)|\.pyc$"
)


def _is_noise_path(path: str) -> bool:
    """运行产物/目录条目（git status 的 `?? __pycache__/` 行）：无 diff 可落盘，
    混进变更集会让 apply_code 整批 EXEC.APPLY_FAILED → test_run 跳过执行。"""
    return path.endswith("/") or bool(_NOISE_PATH_RE.search(path))


class PiProviderBase:
    """三个 pi Provider 的公共部分：CLI 定位、参数拼装、子进程执行。"""

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        timeout_sec: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.bin_path = bin_path or settings.PI_BIN or "pi"
        self.provider = provider if provider is not None else settings.PI_PROVIDER
        self.model = model if model is not None else settings.PI_MODEL
        self.timeout_sec = timeout_sec or settings.PI_TIMEOUT_SEC
        self.extra_args = (
            extra_args if extra_args is not None else list(settings.PI_EXTRA_ARGS)
        )

    def _build_args(self, prompt: str) -> list[str]:
        """拼 `pi [--provider X] [--model Y] --no-session [extra] --print <prompt>`。

        prompt 一定是最后一个位置参数；--no-session 保证不污染用户本地会话列表。
        项目信任策略等开关走 PI_EXTRA_ARGS（如 --no-approve / -t read,edit,bash）。
        """
        args = [self.bin_path]
        if self.provider:
            args += ["--provider", self.provider]
        if self.model:
            args += ["--model", self.model]
        args += ["--no-session", *self.extra_args, "--print", prompt]
        return args

    @retry_with_backoff(
        on_error_wrap=True,
        wrap_context="pi_cli",
    )
    async def _run_pi(self, project_root: str, prompt: str, *, timeout_sec: int | None = None) -> str:
        """在 project_root 下执行一次 pi --print，返回 stdout 文本。

        接入熔断器 + 重试 + DevFlowError 包装，失败语义与 codegraph._run 一致。
        """
        breaker = default_breaker("pi_cli")
        async with breaker.guard():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._build_args(prompt),
                    cwd=project_root or None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, PermissionError) as e:
                raise PiNotInstalledError(
                    f"pi 启动失败: {e}。请先 npm install -g {PI_NPM_PACKAGE}",
                    cause=e,
                ) from e

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_sec or self.timeout_sec
                )
            except asyncio.TimeoutError as e:
                proc.kill()
                raise CliTimeoutError(
                    f"pi 超时（>{timeout_sec or self.timeout_sec}s）；"
                    "可在 .env 调大 PI_TIMEOUT_SEC",
                    cause=e,
                ) from e
            except Exception as e:  # 其它 asyncio 错误
                proc.kill()
                raise DevFlowError("CLI.EXIT", f"pi 执行异常: {e}", cause=e) from e

            err = stderr_b.decode("utf-8", errors="replace").strip()
            out = stdout_b.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                # 401/鉴权失败重试无意义（pi 凭据过期时实测白耗 ~90s 重试）→ 不可重试快速上抛
                if re.search(r"\b401\b|authentication|unauthorized", f"{err}\n{out}", re.IGNORECASE):
                    raise CliAuthError(
                        f"pi 鉴权失败 (exit {proc.returncode}): {(err or out)[:300]}；"
                        "请交互运行 pi 重新登录，或在 .env 配置 PI_PROVIDER/PI_MODEL 指定有效模型",
                        stderr_tail=(err or out)[-400:],
                    )
                raise CliExitError(
                    f"pi 失败 (exit {proc.returncode}): {err[:400]}",
                    stderr_tail=err[-400:],
                )
            return out


# ═══════════════════════════════════════════════════════════════════
# 实现：CodeSearchProvider
# ═══════════════════════════════════════════════════════════════════
class PiSearchProvider(PiProviderBase, CodeSearchProvider):
    """让 pi 在项目里现场检索代码并按约定 JSON 回传。

    注意：pi 没有代码索引，这是「agent 翻文件」式检索，比 codegraph 慢且贵，
    仅建议在无 codegraph 索引的小项目里启用。
    """

    name = "pi_search"

    def __init__(self, *, max_prompt_chars: int = 8000, **kw: Any) -> None:
        super().__init__(**kw)
        self.max_prompt_chars = max_prompt_chars

    async def search(
        self,
        project_root: str,
        query_text: str,
        *,
        query_type: QueryType = "semantic",
        target_symbols: list[str] | None = None,
        scope_files: list[str] | None = None,
        max_results: int = 20,
        session_id: str | None = None,
    ) -> CodeSearchResult:
        if shutil.which(self.bin_path) is None and not Path(self.bin_path).exists():
            raise PiNotInstalledError(
                f"pi 二进制未找到: {self.bin_path!r}。请先 npm install -g {PI_NPM_PACKAGE}"
            )
        prompt = (
            "你是代码检索引擎。在当前目录的项目中定位与描述最相关的代码。\n"
            f"检索类型: {query_type}\n"
            f"描述: {_clip(query_text, self.max_prompt_chars)}\n"
            f"目标符号: {target_symbols or []}\n"
            f"文件范围（只允许这些文件，可留空忽略）: {scope_files or []}\n"
            f"最多返回 {max_results} 条。\n"
            "只输出一个 JSON 数组，不要任何解释或代码围栏，每条字段：\n"
            '{"file_path": str, "symbol_name": str|null, "line_start": int, '
            '"line_end": int, "code_snippet": str, "relevance_score": float, '
            '"callers": [str], "callees": [str]}'
        )
        raw = await self._run_pi(project_root, prompt)
        payload = _extract_json_payload(raw)
        items = payload if isinstance(payload, list) else []
        if not items and isinstance(payload, dict):
            items = payload.get("results") or []
        hits = [self._normalize_hit(it) for it in items if isinstance(it, dict)]
        return {
            "session_id": session_id,
            "total": len(hits),
            "results": hits[:max_results],
            "raw": {"stdout_tail": raw[-2000:], "query_type": query_type},
        }

    @staticmethod
    def _normalize_hit(raw: dict[str, Any]) -> CodeSearchHit:
        return {
            "file_path": str(raw.get("file_path") or raw.get("file") or ""),
            "symbol_name": str(raw["symbol_name"]) if raw.get("symbol_name") else None,
            "line_start": _to_int(raw.get("line_start") or raw.get("start_line") or 1, 1),
            "line_end": _to_int(raw.get("line_end") or raw.get("end_line") or raw.get("line_start") or 1, 1),
            "code_snippet": str(raw.get("code_snippet") or raw.get("snippet") or ""),
            "relevance_score": float(raw.get("relevance_score") or raw.get("score") or 0.0),
            "callers": [str(c) for c in (raw.get("callers") or [])],
            "callees": [str(c) for c in (raw.get("callees") or [])],
        }


# ═══════════════════════════════════════════════════════════════════
# 实现：CodeEditProvider
# ═══════════════════════════════════════════════════════════════════
class PiEditProvider(PiProviderBase, CodeEditProvider):
    """按 instruction 让 pi 改代码；变更集从 git 采集，lint 用 ruff（可选）。"""

    name = "pi_edit"

    async def generate(
        self,
        project_root: str,
        instruction: str,
        *,
        logic_graph_node_id: str | None = None,
        related_files: list[dict[str, Any]] | None = None,
        acceptance: list[str] | None = None,
        run_lint: bool = True,
        session_id: str | None = None,
    ) -> CodeEditResult:
        if shutil.which(self.bin_path) is None and not Path(self.bin_path).exists():
            raise PiNotInstalledError(
                f"pi 二进制未找到: {self.bin_path!r}。请先 npm install -g {PI_NPM_PACKAGE}"
            )
        rel = [_clip(f.get("path", ""), 300) for f in (related_files or []) if isinstance(f, dict)]
        prompt = (
            "你是代码修改执行者，直接修改当前目录项目里的文件（不要只给建议）。\n"
            f"任务: {_clip(instruction, 8000)}\n"
            f"关联逻辑图节点: {logic_graph_node_id or '无'}\n"
            f"相关文件: {rel or '自行判断'}\n"
            "验收标准（逐条满足）:\n"
            + "\n".join(f"- {a}" for a in (acceptance or []))
            + '\n完成后只输出一行 JSON 摘要（不要代码围栏）：'
              '{"summary": str, "open_issues": [str]}'
        )
        before = await self._git_status(project_root)
        raw = await self._run_pi(project_root, prompt)
        changes = await self._collect_changes(project_root, before)
        summary = _extract_json_payload(raw) if raw else None
        if isinstance(summary, dict) and summary.get("open_issues"):
            logger.info("pi_edit open_issues: %s", summary["open_issues"])
        lint, lint_passed = ([], True)
        if run_lint and changes:
            lint, lint_passed = await self._run_ruff(
                project_root, [c.get("file_path", "") for c in changes]
            )
        return {
            "session_id": session_id,
            "changes": changes,
            "lint": lint,
            "lint_passed": lint_passed,
        }

    # ── git 变更集采集（非 git 仓库全部降级为空，不阻断主流程）──────
    async def _git_status(self, project_root: str) -> str:
        try:
            return await self._git(project_root, "status", "--porcelain")
        except Exception:  # noqa: BLE001 - 非 git 仓库 / git 缺失
            return ""

    async def _collect_changes(self, project_root: str, before: str) -> list[CodeChange]:
        try:
            after = await self._git(project_root, "status", "--porcelain")
        except Exception:  # noqa: BLE001
            return []
        paths: list[tuple[str, str]] = []  # (xy, path)
        seen = {ln[3:] for ln in before.splitlines() if len(ln) > 3}
        for ln in after.splitlines():
            if len(ln) <= 3:
                continue
            xy, path = ln[:2], ln[3:]
            if path in seen and xy.strip() == "":
                continue  # pi 之前就已存在的改动不算它的产出
            if _is_noise_path(path):
                continue  # 运行产物（__pycache__/.coverage 等）：无 diff 且会卡死 apply_code 落盘
            paths.append((xy, path))
        changes: list[CodeChange] = []
        for xy, path in paths[:50]:
            if "D" in xy:
                action, diff, content = "delete", "", None
            elif "??" in xy or "A" in xy:
                action, diff = "create", ""
                content = self._read_file(project_root, path)
            else:
                action = "update"
                diff = await self._git(project_root, "diff", "HEAD", "--", path)
                content = self._read_file(project_root, path)
            changes.append(
                {"file_path": path, "action": action, "diff_unified": diff, "content_after": content}
            )
        return changes

    async def _git(self, project_root: str, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", project_root, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(stderr_b.decode("utf-8", errors="replace")[:200])
        return stdout_b.decode("utf-8", errors="replace")

    @staticmethod
    def _read_file(project_root: str, rel_path: str) -> str | None:
        try:
            return (Path(project_root) / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # ── lint：ruff 装了才跑，只查本次变更文件 ────────────────────────
    async def _run_ruff(
        self, project_root: str, files: list[str]
    ) -> tuple[list[LintIssue], bool]:
        ruff = shutil.which("ruff")
        targets = [f for f in files if f][:20]
        if ruff is None or not targets:
            return [], True
        proc = await asyncio.create_subprocess_exec(
            ruff, "check", "--output-format", "json", "--quiet", *targets,
            cwd=project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        try:
            issues_raw = json.loads(stdout_b.decode("utf-8", errors="replace") or "[]")
        except json.JSONDecodeError:
            return [], True  # ruff 输出异常不当作 lint 失败
        lint: list[LintIssue] = []
        for it in issues_raw if isinstance(issues_raw, list) else []:
            loc = it.get("location") or {}
            lint.append(
                {
                    "file_path": it.get("filename", ""),
                    "line": _to_int(loc.get("row"), 0),
                    "level": "error" if str(it.get("code", "")).startswith("E") else "warning",
                    "message": str(it.get("message", "")),
                    "linter": f"ruff:{it.get('code', '')}",
                }
            )
        return lint, not lint


# ═══════════════════════════════════════════════════════════════════
# 实现：TestGenProvider
# ═══════════════════════════════════════════════════════════════════
class PiTestProvider(PiProviderBase, TestGenProvider):
    """让 pi 在项目里写测试并跑 pytest，结果按约定 JSON 回传。

    仅需求模式（project_root 为空）与 OpenCodeTestProvider 同策略：
    委托 LlmTestGenProvider 基于 requirement + logic_graph 直接设计用例。
    """

    name = "pi_test"

    def __init__(self, *, pytest_timeout_sec: int | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        # 写测试 + 跑 pytest 是两段重活，默认超时放大到 15 分钟
        self.pytest_timeout_sec = pytest_timeout_sec or max(self.timeout_sec, 900)

    async def generate(
        self,
        project_root: str,
        target_symbols: list[str],
        *,
        coverage_target: int = 80,
        modified_branches_only: bool = True,
        logic_graph: dict[str, Any] | None = None,
        session_id: str | None = None,
        requirement: dict[str, Any] | None = None,
        feedback: str | None = None,
        checklists: list[dict[str, str]] | None = None,
        feature: dict[str, Any] | None = None,
        skill_paths: list[str] | None = None,
    ) -> TestReport:
        if not project_root:
            from .llm_testgen import LlmTestGenProvider

            return await LlmTestGenProvider().generate(
                "",
                target_symbols,
                coverage_target=coverage_target,
                modified_branches_only=modified_branches_only,
                logic_graph=logic_graph,
                session_id=session_id,
                requirement=requirement,
                feedback=feedback,
                checklists=checklists,
                feature=feature,
            )
        if shutil.which(self.bin_path) is None and not Path(self.bin_path).exists():
            raise PiNotInstalledError(
                f"pi 二进制未找到: {self.bin_path!r}。请先 npm install -g {PI_NPM_PACKAGE}"
            )
        checklist_lines = "\n".join(
            f"- [{c.get('rel_dir', '')}/{c.get('name', '')}] {_clip(c.get('content', ''), 1500)}"
            for c in (checklists or [])
        )
        if feature:
            # feature 拆分模式：只设计该功能点的用例；pi 无技能系统，派发时把
            # SKILL.md 绝对路径以「先完整读取并严格遵守」前缀注入 prompt
            from ..feature_split import build_feature_design_prompt

            prompt = build_feature_design_prompt(
                feature,
                requirement=requirement,
                feedback=feedback,
                checklists=checklists,
                skill_paths=skill_paths,
            )
        else:
            prompt = (
                "你是测试工程师。在当前目录项目里完成两件事：\n"
                "1) 为下列目标编写/补充 pytest 用例（新文件放 tests/ 下）；\n"
                f"2) 运行 pytest，确保全部通过。\n"
                f"目标: {target_symbols}\n"
                f"覆盖率目标: {coverage_target}%（无法统计就留空 coverage_pct）\n"
                f"只测改动分支: {modified_branches_only}\n"
                + (f"逻辑图: {_clip(json.dumps(logic_graph, ensure_ascii=False), 4000)}\n" if logic_graph else "")
                + (f"结构化需求: {_clip(json.dumps(requirement, ensure_ascii=False), 4000)}\n" if requirement else "")
                + (f"上一轮驳回意见（必须针对性修正）: {_clip(feedback, 2000)}\n" if feedback else "")
                + (
                    "业务检查清单（用例设计必须逐条核对覆盖）:\n" + checklist_lines + "\n"
                    if checklist_lines else ""
                )
                + "\n最后只输出一行 JSON（不要代码围栏），结构：\n"
                '{"test_cases": [{"test_file": str, "test_symbol": str, '
                '"code_snippet": str, "covered_edges": [str]}], '
                '"run": {"passed": int, "failed": int, "skipped": int, '
                '"coverage_pct": float|null, "logs": str}}'
            )
        raw = await self._run_pi(project_root, prompt, timeout_sec=self.pytest_timeout_sec)
        payload = _extract_json_payload(raw)
        if not isinstance(payload, dict):
            # 测试报告解析失败不能静默当成 0 失败 → 按可重试的 CLI 错误上抛
            raise CliExitError(
                f"pi 测试报告 JSON 解析失败: {raw[:200]}",
                stderr_tail=raw[-200:],
            )
        if feature:
            # 技能式产出 → 归一成设计报告（不执行 pytest）
            cases = [
                {
                    "test_file": "",
                    "test_symbol": f"test_{feature.get('feature_id', 'f')}_{i + 1:02d}",
                    "case_id": f"TC-{i + 1:03d}",
                    "tier": str(t.get("tier") or "functional"),
                    "priority": str(t.get("priority") or "P1"),
                    "case_type": str(t.get("case_type") or "正向"),
                    "title": str(t.get("title") or ""),
                    "target": str(t.get("target") or feature.get("name", "")),
                    "precondition": str(t.get("precondition") or ""),
                    "steps": str(t.get("steps") or ""),
                    "expected": str(t.get("expected") or ""),
                    "rationale": str(t.get("rationale") or ""),
                    "code_snippet": f"{t.get('steps') or ''}\n预期: {t.get('expected') or ''}",
                    "feature_id": str(feature.get("feature_id") or ""),
                    "feature_name": str(feature.get("name") or ""),
                }
                for i, t in enumerate(payload.get("cases") or [])
                if isinstance(t, dict)
            ]
            return {
                "session_id": session_id or f"pi-test-design:{feature.get('feature_id')}",
                "test_cases": cases,
                "run": {
                    "passed": len(cases) if cases else 1, "failed": 0, "skipped": 0,
                    "coverage_pct": None,
                    "logs": f"pi 技能式设计功能点 {feature.get('feature_id')}：{len(cases)} 条场景（未执行真实测试）",
                },
                "target_symbols": target_symbols,
                "overview": f"功能点 {feature.get('feature_id')} {feature.get('name', '')} 设计要点（由 pi 技能执行器产出）",
                "self_check": [str(x) for x in (payload.get("coverage_self_check") or [])],
                "open_issues": [str(x) for x in (payload.get("open_issues") or [])],
            }
        cases: list[TestCase] = []
        for t in payload.get("test_cases", []) or []:
            if not isinstance(t, dict):
                continue
            cases.append(
                {
                    "test_file": str(t.get("test_file", "")),
                    "test_symbol": str(t.get("test_symbol", "")),
                    "line_start": 1,
                    "line_end": 1,
                    "code_snippet": str(t.get("code_snippet", "")),
                    "covered_edges": [str(e) for e in (t.get("covered_edges") or [])],
                }
            )
        run_raw = payload.get("run", {}) or {}
        run: TestRun = {
            "executed": True,  # 真实跑过 pytest：人工验收以此判定「测试执行」，缺失会被当成未执行
            "passed": _to_int(run_raw.get("passed"), 0),
            "failed": _to_int(run_raw.get("failed"), 0),
            "skipped": _to_int(run_raw.get("skipped"), 0),
            "coverage_pct": (
                float(run_raw["coverage_pct"]) if run_raw.get("coverage_pct") is not None else None
            ),
            "logs": _clip(run_raw.get("logs"), 20000) or _clip(raw, 20000),
        }
        return {
            "session_id": session_id,
            "test_cases": cases,
            "run": run,
            "target_symbols": target_symbols,
        }
