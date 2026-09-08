"""Provider 接入节点示例：展示如何在 LangGraph 节点中调用 CodeProvider 适配层。

四个 async 节点对应 SPEC 3.2 工作流图中的阶段二/三节点：
  1. code_search_node   — 调 providers.code_search.search()
  2. graph_render_node  — 调 providers.graph_render.render()
  3. code_gen_node      — 调 providers.code_edit.generate()
  4. test_gen_node      — 调 providers.test_gen.generate()

设计要点：
  - 节点只依赖抽象基类（CodeSearchProvider 等），不 import 具体实现
  - Provider 实例通过 get_providers() 在 graph 构建时注入，节点函数用闭包捕获
  - 每个节点是 async def，LangGraph 原生支持 async 节点；同步入口用 sync wrapper
  - 节点返回值是 dict[str, Any]，只写回 GlobalState 需要更新的字段
  - 所有 Provider 异常按 SPEC 5 统一写入: last_error / last_error_code / last_error_retryable / retry_count / dead_letters
"""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timezone
from typing import Any

from ..errors import DevFlowError, wrap_exception
from ..providers import Providers, get_providers
from ..resilience import DeadLetter, push_dead_letter
from ..state import GlobalState

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 节点工厂：接收 Providers 实例，返回闭包节点函数
# 这样做的好处：
#   1. 测试时可以注入 mock providers
#   2. 生产时用 get_providers() 按 .env 装配
#   3. 节点函数签名仍然是 (state: GlobalState) -> dict，兼容 LangGraph
# ═══════════════════════════════════════════════════════════════════


def _apply_error_out(
    node_name: str,
    state: GlobalState,
    err: DevFlowError,
    *,
    stage_when_fail: str,
    extra_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 DevFlowError 映射成 State 更新（SPEC 5.6：异常 → State 字段）。

    返回: last_error / last_error_code / last_error_retryable / retry_count / dead_letters / current_stage
    """
    retry_map = copy.deepcopy(state.get("retry_count") or {})
    retry_map[node_name] = retry_map.get(node_name, 0) + 1

    dl = DeadLetter(
        id=f"dl-{node_name}-{int(datetime.now(tz=timezone.utc).timestamp()*1000)}",
        node=node_name,
        error_code=err.code,
        error_message=err.message,
        retryable=err.retryable,
        retry_after_sec=err.retry_after_sec,
        stage=state.get("current_stage") or stage_when_fail,
        snapshot=copy.deepcopy(extra_snapshot or {}),
        cause_repr=None if err.cause is None else repr(err.cause),
        extra=copy.deepcopy(err.extra or {}),
    )
    dead_letters = list(state.get("dead_letters") or [])
    push_dead_letter(dead_letters, dl, max_items=200)

    return {
        "last_error": f"[{node_name}:{err.code}] {err.message}",
        "last_error_code": err.code,
        "last_error_retryable": err.retryable,
        "retry_count": retry_map,
        "dead_letters": dead_letters,
        "current_stage": stage_when_fail,
    }


def _inc_retry(node_name: str, state: GlobalState) -> dict[str, int]:
    retry_map = copy.deepcopy(state.get("retry_count") or {})
    retry_map[node_name] = retry_map.get(node_name, 0) + 1
    return retry_map


def make_code_search_node(providers: Providers | None = None):
    """阶段二：代码检索节点。

    读取 state: requirement.project_root, requirement.target_modules,
                 requirement.io_constraints, opencode_sessions.search
    写入 state: code_context, opencode_sessions.search, current_stage,
                last_error / last_error_code / last_error_retryable / retry_count / dead_letters
    """
    p = providers or get_providers()

    async def code_search_node_async(state: GlobalState) -> dict[str, Any]:
        req = state.get("requirement") or {}
        project_root = req.get("project_root", "")
        if not project_root:
            err = wrap_exception(
                ValueError("requirement.project_root 为空"), context="code_search"
            )
            return _apply_error_out("code_search", state, err, stage_when_fail="search")

        # 从需求中组装查询参数
        target_modules = req.get("target_modules") or []
        query_text = _build_search_query(req)
        sessions = state.get("opencode_sessions") or {}
        session_id = sessions.get("search")

        try:
            result = await p.code_search.search(
                project_root,
                query_text,
                query_type="semantic",
                target_symbols=None,  # 阶段二还没做符号解析，先语义检索
                scope_files=target_modules,
                max_results=20,
                session_id=session_id,
            )
        except DevFlowError as e:
            logger.exception("code_search 节点失败 (DevFlowError code=%s)", e.code)
            return _apply_error_out(
                "code_search",
                state,
                e,
                stage_when_fail="search",
                extra_snapshot={
                    "project_root": project_root,
                    "query_text": query_text,
                    "target_modules": list(target_modules),
                },
            )
        except Exception as e:
            logger.exception("code_search 节点失败 (未预期异常)")
            return _apply_error_out(
                "code_search",
                state,
                wrap_exception(e, context="code_search"),
                stage_when_fail="search",
                extra_snapshot={
                    "project_root": project_root,
                    "query_text": query_text,
                    "target_modules": list(target_modules),
                },
            )

        # 把搜索结果转成 code_context 条目（GlobalState.code_context 期望的格式）
        code_context: list[dict[str, Any]] = []
        for hit in result["results"]:
            code_context.append(
                {
                    "file_path": hit["file_path"],
                    "symbol_name": hit["symbol_name"],
                    "line_start": hit["line_start"],
                    "line_end": hit["line_end"],
                    "code_snippet": hit["code_snippet"],
                    "relevance_score": hit["relevance_score"],
                    "callers": hit["callers"],
                    "callees": hit["callees"],
                }
            )

        return {
            "code_context": code_context,
            "opencode_sessions": {**sessions, "search": result["session_id"]},
            "last_error": None,
            "last_error_code": None,
            "last_error_retryable": None,
            "current_stage": "graph" if code_context else "search",
        }

    def code_search_node(state: GlobalState) -> dict[str, Any]:
        """同步入口（LangGraph .invoke 兼容）。"""
        return asyncio.run(code_search_node_async(state))

    # 对外同时暴露 sync/async：sync 给 LangGraph .invoke，async 给 tests / ainvoke。
    code_search_node.__name__ = "code_search_node"
    code_search_node.async_version = code_search_node_async  # type: ignore[attr-defined]
    return code_search_node


def make_graph_render_node(providers: Providers | None = None):
    """阶段二/三：逻辑图渲染节点。

    读取 state: logic_graph
    写入 state: logic_graph（补渲染后端信息）, current_stage + SPEC 5 错误字段
    """
    p = providers or get_providers()

    async def graph_render_node_async(state: GlobalState) -> dict[str, Any]:
        logic_graph = state.get("logic_graph")
        if not logic_graph:
            err = wrap_exception(
                ValueError("logic_graph 为空，无法渲染"), context="graph_render"
            )
            return _apply_error_out("graph_render", state, err, stage_when_fail="graph")

        try:
            render_out = await p.graph_render.render(
                logic_graph,
                preferred_format="mermaid",  # MVP 默认 mermaid；配 Archify 后改 html
            )
        except DevFlowError as e:
            return _apply_error_out(
                "graph_render",
                state,
                e,
                stage_when_fail="graph",
                extra_snapshot={
                    "graph_id": logic_graph.get("graph_id"),
                    "preferred_format": "mermaid",
                },
            )
        except Exception as e:
            return _apply_error_out(
                "graph_render",
                state,
                wrap_exception(e, context="graph_render"),
                stage_when_fail="graph",
                extra_snapshot={"graph_id": logic_graph.get("graph_id")},
            )

        # 如果渲染器返回了 mermaid_text，更新 logic_graph 的 mermaid_source
        updated_graph = dict(logic_graph)
        if render_out.get("mermaid_text"):
            updated_graph["mermaid_source"] = render_out["mermaid_text"]
        updated_graph["_render_backend"] = render_out.get("render_backend", "unknown")

        return {
            "logic_graph": updated_graph,
            "last_error": None,
            "last_error_code": None,
            "last_error_retryable": None,
            "current_stage": "code",  # 渲染完进入代码生成阶段
        }

    def graph_render_node(state: GlobalState) -> dict[str, Any]:
        return asyncio.run(graph_render_node_async(state))

    graph_render_node.__name__ = "graph_render_node"
    graph_render_node.async_version = graph_render_node_async  # type: ignore[attr-defined]
    return graph_render_node


def make_code_gen_node(providers: Providers | None = None):
    """阶段三：代码生成/修改节点。

    读取 state: requirement, logic_graph, code_context, opencode_sessions.code_gen
    写入 state: code_changes, opencode_sessions.code_gen, current_stage + SPEC 5 错误字段
    """
    p = providers or get_providers()

    async def code_gen_node_async(state: GlobalState) -> dict[str, Any]:
        req = state.get("requirement") or {}
        project_root = req.get("project_root", "")
        logic_graph = state.get("logic_graph") or {}
        code_ctx = state.get("code_context") or []
        sessions = state.get("opencode_sessions") or {}
        session_id = sessions.get("code_gen")

        # 从 logic_graph 找出 is_modified=true 的节点，作为代码生成目标
        modified_nodes = [
            n for n in logic_graph.get("nodes", []) if n.get("is_modified")
        ]
        if not modified_nodes:
            err = wrap_exception(
                ValueError("逻辑图中没有 is_modified=true 的节点"), context="code_gen"
            )
            return _apply_error_out("code_gen", state, err, stage_when_fail="code")

        # 组装 related_files：从 code_context 里取文件路径 + 真实代码片段
        # （P4：带 snippet 让真实 OpenCode 拿到代码上下文；有长度上限保护 token）
        related_files = _build_related_files(code_ctx)

        # 取第一个修改节点的 label 作为 instruction 的锚点；
        # 门禁 reject 的意见（review_feedback）与上轮测试失败摘要（test_failure）拼进
        # instruction 针对性修正
        first_node = modified_nodes[0]
        instruction = _build_code_instruction(
            req,
            first_node,
            feedback=state.get("review_feedback"),
            test_failure=state.get("test_failure"),
        )

        try:
            result = await p.code_edit.generate(
                project_root,
                instruction,
                logic_graph_node_id=first_node.get("node_id"),
                related_files=related_files,
                acceptance=req.get("acceptance_criteria", []),
                run_lint=True,
                session_id=session_id,
            )
        except DevFlowError as e:
            logger.exception("code_gen 节点失败 (DevFlowError code=%s)", e.code)
            return _apply_error_out(
                "code_gen",
                state,
                e,
                stage_when_fail="code",
                extra_snapshot={
                    "project_root": project_root,
                    "target_node_id": first_node.get("node_id"),
                },
            )
        except Exception as e:
            logger.exception("code_gen 节点失败 (未预期异常)")
            return _apply_error_out(
                "code_gen",
                state,
                wrap_exception(e, context="code_gen"),
                stage_when_fail="code",
                extra_snapshot={
                    "project_root": project_root,
                    "target_node_id": first_node.get("node_id"),
                },
            )

        # 把 Provider 返回的 changes 映射到 GlobalState.code_changes 格式
        # content_after 一并携带（apply_code 节点仅对新建文件使用整文件直写）
        code_changes: list[dict[str, Any]] = []
        for ch in result["changes"]:
            code_changes.append(
                {
                    "file_path": ch["file_path"],
                    "action": ch.get("action", "update"),
                    "diff": ch.get("diff_unified", ""),
                    "content_after": ch.get("content_after"),
                    "lint_passed": result["lint_passed"],
                    "test_passed": None,  # test_run 节点回填
                }
            )

        lint_err: dict[str, Any] | None = None
        if not result["lint_passed"]:
            # lint 失败本身也是一种 HTTP.LINT_FAILED / SPEC 5.2 错误，按统一错误语义写 state
            from ..errors import HttpLintFailedError

            lint_err = _apply_error_out(
                "code_gen",
                state,
                HttpLintFailedError(
                    "lint 不通过，需修复",
                    extra={"lint_issues": [x.model_dump() if hasattr(x, "model_dump") else dict(x)
                                           for x in result.get("lint_issues", []) or []]},
                ),
                stage_when_fail="code",
                extra_snapshot={"target_node_id": first_node.get("node_id")},
            )

        base = {
            "code_changes": code_changes,
            "opencode_sessions": {**sessions, "code_gen": result["session_id"]},
            "test_failure": None,  # 已消费（拼进 instruction），避免残留到下一轮
        }
        if lint_err is not None:
            base.update(lint_err)
        else:
            base.update(
                {
                    "last_error": None,
                    "last_error_code": None,
                    "last_error_retryable": None,
                    "current_stage": "test",
                }
            )
        return base

    def code_gen_node(state: GlobalState) -> dict[str, Any]:
        return asyncio.run(code_gen_node_async(state))

    code_gen_node.__name__ = "code_gen_node"
    code_gen_node.async_version = code_gen_node_async  # type: ignore[attr-defined]
    return code_gen_node


def make_test_gen_node(providers: Providers | None = None):
    """阶段三：测试生成节点。

    读取 state: requirement, logic_graph, code_changes, review_feedback,
                opencode_sessions.test_gen
    写入 state: test_report, opencode_sessions.test_gen, current_stage + SPEC 5 错误字段

    两种模式：
      代码模式（has_project_code）：测试目标来自 code_changes；为空则报错回炉。
      仅需求模式（无项目代码）：跳过代码检索/生成，直接基于需求 + 逻辑图设计
      端到端测试场景；目标从 target_modules / 逻辑图改动节点推导。
    """
    p = providers or get_providers()

    async def test_gen_node_async(state: GlobalState) -> dict[str, Any]:
        from ..schemas import has_project_code

        req = state.get("requirement") or {}
        project_root = req.get("project_root", "")
        logic_graph = state.get("logic_graph") or {}
        code_changes = state.get("code_changes") or []
        sessions = state.get("opencode_sessions") or {}
        session_id = sessions.get("test_gen")
        requirement_only = not has_project_code(req)

        if requirement_only:
            # 仅需求模式：目标 = 需求里的模块列表，缺省则取逻辑图改动节点（兜底全部节点）
            target_symbols = [m for m in (req.get("target_modules") or []) if m]
            if not target_symbols:
                nodes = logic_graph.get("nodes") or []
                modified = [n.get("label") for n in nodes if n.get("is_modified") and n.get("label")]
                target_symbols = modified or [n.get("label") for n in nodes if n.get("label")]
                target_symbols = list(dict.fromkeys(target_symbols))
            if not target_symbols:
                target_symbols = ["端到端场景"]
        else:
            # 代码模式：从 code_changes 提取目标符号
            target_symbols = [
                f"{ch['file_path']}"
                for ch in code_changes
                if ch.get("file_path")
            ]
            if not target_symbols:
                err = wrap_exception(
                    ValueError("code_changes 为空，无测试目标"), context="test_gen"
                )
                return _apply_error_out("test_gen", state, err, stage_when_fail="test")

        try:
            report = await p.test_gen.generate(
                project_root,
                target_symbols,
                coverage_target=80,
                modified_branches_only=True,
                logic_graph=logic_graph,
                session_id=session_id,
                requirement=req if requirement_only else None,
                feedback=state.get("review_feedback"),
            )
        except DevFlowError as e:
            return _apply_error_out(
                "test_gen",
                state,
                e,
                stage_when_fail="test",
                extra_snapshot={
                    "project_root": project_root,
                    "target_symbols": list(target_symbols),
                },
            )
        except Exception as e:
            return _apply_error_out(
                "test_gen",
                state,
                wrap_exception(e, context="test_gen"),
                stage_when_fail="test",
                extra_snapshot={
                    "project_root": project_root,
                    "target_symbols": list(target_symbols),
                },
            )

        # 测试「执行」由下游 test_run 节点承担真实判定；本节点只产出设计报告。
        # code_changes.test_passed 保持 None，等 test_run 回填。
        return {
            "test_report": dict(report),
            "opencode_sessions": {**sessions, "test_gen": report["session_id"]},
            "last_error": None,
            "last_error_code": None,
            "last_error_retryable": None,
            "current_stage": "test",
        }

    def test_gen_node(state: GlobalState) -> dict[str, Any]:
        return asyncio.run(test_gen_node_async(state))

    test_gen_node.__name__ = "test_gen_node"
    test_gen_node.async_version = test_gen_node_async  # type: ignore[attr-defined]
    return test_gen_node


# ═══════════════════════════════════════════════════════════════════
# 路由函数（供 orchestrator 的 conditional_edges 使用）
#
# SPEC 5.6.3：把 last_error_retryable 纳入路由决策。
#   - retryable=True → 走 "retry"（节点重试、或回滚到上一阶段再回来）
#   - retryable=False → 走 "fallback" / "abort"（走 mock 兜底或结束）
# ═══════════════════════════════════════════════════════════════════

def route_after_code_search(state: GlobalState) -> str:
    """SPEC 5.6 错误路由版：
      1. 有 DevFlowError 且不可重试 → no_results（去 fallback）
      2. 有 DevFlowError 且可重试 → retry
      3. 有结果 → has_results
      4. 无结果 → no_results
    """
    if state.get("last_error_code"):
        if state.get("last_error_retryable") is True:
            return "retry"
        return "no_results"
    if state.get("code_context"):
        return "has_results"
    return "no_results"


def route_after_code_gen(state: GlobalState) -> str:
    """代码生成后路由：
      - DevFlowError 可重试 → retry；不可重试 → abort；
      - 否则按 lint 结果判断（retry / lint_ok）。
    """
    err_code = state.get("last_error_code")
    if err_code:
        if state.get("last_error_retryable") is True:
            retry = (state.get("retry_count") or {}).get("code_gen", 0)
            return "retry" if retry <= 2 else "abort"
        # 不可重试（供应商拒服 / 认证 / 上下文溢出但截断也救不回等）
        return "abort"
    err = state.get("last_error") or ""
    if "lint" in err.lower():
        retry = (state.get("retry_count") or {}).get("code_gen", 0)
        return "retry" if retry < 2 else "force_test"
    return "lint_ok"


def route_after_test_gen(state: GlobalState) -> str:
    """测试设计完成后的业务路由：一律 "run" → 交给 test_run 节点做真实执行判定。
    （错误分流由 orchestrator 的 _route_after_test_gen 包装处理。）"""
    return "run"


# ═══════════════════════════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════════════════════════

def _build_search_query(req: dict[str, Any]) -> str:
    """从需求字段组装自然语言检索 query。"""
    parts: list[str] = []
    ctx = req.get("project_context", "")
    if ctx:
        parts.append(ctx)
    io = req.get("io_constraints") or {}
    if io.get("input"):
        parts.append(f"输入: {io['input']}")
    if io.get("output"):
        parts.append(f"输出: {io['output']}")
    for ac in req.get("acceptance_criteria", []) or []:
        parts.append(ac)
    return "；".join(parts) if parts else "代码检索"


def _build_code_instruction(
    req: dict[str, Any],
    node: dict[str, Any],
    *,
    feedback: str | None = None,
    test_failure: str | None = None,
) -> str:
    """把需求 + 逻辑图节点（+ 评审意见 / 上轮测试失败摘要）拼成给 Provider 的 instruction。"""
    label = node.get("label", "未知节点")
    io = req.get("io_constraints") or {}
    lines = [
        f"修改目标：{label}",
        f"输入约束：{io.get('input', '无')}",
        f"输出约束：{io.get('output', '无')}",
    ]
    for ac in req.get("acceptance_criteria", []) or []:
        lines.append(f"验收标准：{ac}")
    for ec in req.get("edge_cases", []) or []:
        lines.append(f"边界场景：{ec}")
    if test_failure and test_failure.strip():
        lines.append(f"上轮测试执行未通过（必须针对性修复）：\n{test_failure.strip()}")
    if feedback and feedback.strip():
        lines.append(f"上轮验收意见（必须针对性修正）：{feedback.strip()}")
    return "\n".join(lines)


# 单文件 snippet 传给 Provider 的最大字符数（防 token 爆炸；约 400 tokens）
_SNIPPET_MAX_CHARS = 1600


def _build_related_files(
    code_context: list[dict[str, Any]],
    *,
    max_snippet_chars: int = _SNIPPET_MAX_CHARS,
) -> list[dict[str, Any]]:
    """把 code_context 转成 Provider 的 related_files 参数。

    P4：携带 code_snippet（有长度上限）让真实后端拿到代码上下文；
    无 snippet 字段时降级为只传路径。
    """
    files: list[dict[str, Any]] = []
    for c in code_context:
        fp = c.get("file_path")
        if not fp:
            continue
        entry: dict[str, Any] = {"file_path": fp, "symbol": c.get("symbol_name")}
        snippet = c.get("code_snippet")
        if snippet:
            text = str(snippet)
            if len(text) > max_snippet_chars:
                text = text[:max_snippet_chars] + "..."
            entry["code_snippet"] = text
        files.append(entry)
    return files
