"""LangGraph 主流程编排。

阶段一 MVP 节点与路由：

    [START]
       │
       ▼
 clarify_extract   ← 合并用户输入 → 更新 requirement
       │
       ▼
 clarify_validate  ← 纯程序校验 Schema
       │
    ┌──┴──┐ 有缺失？
    │     │
    ▼     ▼ 无缺失
build_  compress
question │(可选)
    │    │
    │    ▼
    │ graph_type_select ← 制图前 interrupt 门禁：选逻辑图种类
    │    │   （flowchart / sequence / state / er，选定后重制图沿用不再询问）
    │    ▼
    │ graph_generate ──▶ [DONE: logic_graph 生成结束]
    └──── (循环回到 clarify_extract，等待用户输入后由 stream 驱动继续)

注意：多轮循环交互（需求澄清）靠外部 CLI 反复 stream(input=HumanMessage(...)) 来推进；
LangGraph 内部不阻塞等待 I/O。

──────────────────────────────────────────────────────────────────────
阶段二/三扩展（build_graph_with_providers）：

  ... graph_generate
        │
        ▼
  graph_review(门禁1) ── approve ──┬─ 有代码 ──▶ code_search ──▶ graph_render ──▶ code_gen
        │                          └─ 无代码 ──▶ test_gen（仅需求模式：直接设计端到端用例）
        └─ reject → graph_generate 重制图

  code_gen ──▶ apply_code ──▶ test_gen ──▶ test_run ──▶ review(门禁2)
  review reject ──┬─ 有代码 ──▶ code_gen 回修
                 └─ 无代码 ──▶ test_gen 重新设计用例

  用 make_*_node(providers) 工厂注入 Provider 实例；
  节点函数是 async def，LangGraph 原生支持。

──────────────────────────────────────────────────────────────────────
SPEC 5.6 异常路由映射：
  route_* 返回的标签 → 下一跳：
    "retry"        → 回到前驱节点（graph 层再保险，防 @retry_with_backoff 超限）
    "abort"        → 先经 dead_letter_drain_node 落盘死信 JSONL，再 END
    "no_results"   → 代码检索空集/不可重试错：回澄清（build_graph_with_providers）或 END（build_graph）
    "__end__"      → 直接 END
"""
from __future__ import annotations

import sqlite3
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .config import settings
from .errors import CLARIFY_LOOP_EXHAUSTED, DevFlowError
from .nodes import (
    clarify_build_question,
    clarify_extract,
    clarify_validate,
    compress_messages,
    graph_generate,
    graph_review_node,
    graph_type_select,
    make_apply_code_node,
    make_code_gen_node,
    make_code_search_node,
    make_graph_render_node,
    make_test_gen_node,
    make_test_run_node,
    requirement_review_node,
    review_node,
    route_after_code_apply,
    route_after_code_gen,
    route_after_code_search,
    route_after_graph_review,
    route_after_requirement_review,
    route_after_review,
    route_after_test_gen,
    route_after_test_run,
)
from .nodes.checklist_route import checklist_route_gate, checklist_route_match
from .providers import Providers, get_providers
from .resilience import dead_letter_record
from .schemas import empty_requirement
from .state import GlobalState

# graph 层每节点的 max 重试上限（兜底防循环；细粒度由 resilience.RetryPolicy 控制）
_GRAPH_RETRY_CAP_PER_NODE: int = 3

# SQLite 连接单例：长时间保持打开，checkpointer.setup() 负责建表
_conn: sqlite3.Connection | None = None


def _get_sqlite_conn() -> sqlite3.Connection:
    """惰性打开 SQLite 连接（多线程安全 + 全局复用）。"""
    global _conn
    if _conn is None:
        db_path = settings.CHECKPOINT_SQLITE_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return _conn


# ═══════════════════════════════════════════════════════════════════
# 通用路由辅助
# ═══════════════════════════════════════════════════════════════════


def _error_retry_or(
    state: GlobalState,
    node_name: str,
    *,
    fallback_label: str,
    cap: int = _GRAPH_RETRY_CAP_PER_NODE,
) -> str:
    """SPEC 5.6.3：先看 last_error_retryable 再走业务路由。

    若 node_name 对应 retry_count 达到 cap，即便是 retryable 也强制走 fallback
    （通常 fallback_label="abort" 或 "__end__"），避免 LangGraph 图层面无限循环。
    """
    err = state.get("last_error_code")
    if not err:
        return fallback_label
    retryable = bool(state.get("last_error_retryable"))
    count = (state.get("retry_count") or {}).get(node_name, 0)
    if retryable and count <= cap:
        return "retry"
    # 不可重试 / 超限 → 交给调用方的 fallback（通常是 abort 或业务标签 no_results）
    return fallback_label


def _state_to_err(state: GlobalState) -> DevFlowError:
    """把 State 里的错误字段"还原"为 DevFlowError，喂给 dead_letter_record。"""
    code = state.get("last_error_code") or "NODE.UNKNOWN"
    msg = state.get("last_error") or f"[abort] code={code}"
    # 死信队列里最后一条若匹配 code/msg 则复用它的 snapshot/extra
    snapshot: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for dl in reversed(state.get("dead_letters") or []):
        if isinstance(dl, dict) and dl.get("error_code") == code:
            snapshot = dl.get("snapshot") or {}
            extra = dl.get("extra") or {}
            if dl.get("cause_repr"):
                extra.setdefault("cause_repr", dl["cause_repr"])
            break
    # 对不可重试错误，显式写 retryable=False（避免 DEFAULT_RETRYABLE_CODES 误判）
    retryable = bool(state.get("last_error_retryable"))
    err = DevFlowError(code, msg, retryable=retryable, extra=extra)
    dead_letter_record(err, state_snapshot=snapshot)
    return err


# ═══════════════════════════════════════════════════════════════════
# SPEC 5.7：中止/不可重试 → 死信落盘节点
# ═══════════════════════════════════════════════════════════════════


def dead_letter_drain_node(state: GlobalState) -> dict[str, Any]:
    """把 dead_letters 队列中的未脱敏/大 payload 同步落盘 JSONL。

    节点幂等：同一 error_code 只会写一次，后续重复 abort 跳过。
    """
    written: set[str] = set()
    for dl in state.get("dead_letters") or []:
        if not isinstance(dl, dict):
            continue
        code = dl.get("error_code")
        if not code or code in written:
            continue
        msg = dl.get("error_message") or f"[dead_letter] code={code}"
        retryable = bool(dl.get("retryable"))
        extra = dict(dl.get("extra") or {})
        if dl.get("cause_repr"):
            extra.setdefault("cause_repr", dl["cause_repr"])
        stage = dl.get("stage")
        if stage:
            extra.setdefault("stage", stage)
        err = DevFlowError(code, msg, retryable=retryable, extra=extra)
        try:
            dead_letter_record(err, state_snapshot=dl.get("snapshot"))
        except OSError:
            # 落盘失败不影响 graph 结束（避免失败套失败）
            pass
        written.add(code)
    # 清掉已落盘的 dead_letters 头（保留未写的后 N 条；避免后续 checkpoint 越堆越大）
    keep = list(state.get("dead_letters") or [])
    if len(keep) > 200:
        keep = keep[-200:]
    return {
        "dead_letters": keep,
        "current_stage": state.get("current_stage") or "end",
    }


# ═══════════════════════════════════════════════════════════════════
# 路由函数
# ═══════════════════════════════════════════════════════════════════


def _route_after_extract(state: GlobalState) -> str:
    """clarify_extract 后：如果抛了可重试 LLM 错 → 回 retry；否则进 validate。

    对应 SPEC 5.6.2：LLM.UPSTREAM / RATE_LIMIT → retry；LLM.REFUSED / CONTEXT_OVERFLOW 不
    可重试 → 正常推进到 clarify_validate（让 validate 看到缺失字段，再走 build_question
    或 abort 分支）。
    """
    return _error_retry_or(state, "clarify_extract", fallback_label="ok")


def _route_after_validate(state: GlobalState) -> str:
    """clarify_validate 后：有缺失就追问，没缺失就推进到制图阶段。

    兜底优先级：
      1. CLARIFY.LOOP_EXHAUSTED（澄清轮次用尽）→ abort（记死信 → END），
         否则 missing 非空会永远走 need_more_info，形成死循环；
      2. last_error_code 存在且不可重试 + 真的无信息（missing_fields 但 build_question 也
         跑不起来）→ 直接 abort；否则保持原行为。
    """
    err = state.get("last_error_code")
    retryable = bool(state.get("last_error_retryable"))
    missing = state.get("missing_fields") or []
    if err == CLARIFY_LOOP_EXHAUSTED:
        return "abort"
    if missing:
        return "need_more_info"
    if err and not retryable and not missing:
        # 前一个 clarify_extract / build_question 失败且不可重试，也没有缺失字段可追问
        return "abort"
    return "info_complete"


def _route_after_build_question(state: GlobalState) -> str:
    """追问节点写完 Human 侧提示词后：有可重试错就 retry 本节点，否则 END。"""
    return _error_retry_or(state, "clarify_build_question", fallback_label="__end__")


def _route_after_graph_generate(state: GlobalState) -> str:
    """制图后：成功→推进；可重试错→retry_graph；不可重试或超限→abort（落死信→END）。"""
    err = state.get("last_error")
    count = (state.get("retry_count") or {}).get("graph_generate", 0)
    if not err:
        # 业务成功：build_graph() → __end__；build_graph_with_providers() → code_search
        # 具体映射由两个 build_* 函数的 conditional_edges dict 分别定义，这里统一标签。
        return "ok"
    retryable = bool(state.get("last_error_retryable"))
    if retryable and count < 2:
        return "retry_graph"
    return "abort"


def _route_after_graph_review(state: GlobalState) -> str:
    """制图门禁后按业务路由 + 是否提供项目代码分流（仅需求模式分支）：
      rejected          → 重制图
      with_code（有代码）→ code_search 检索
      no_code（无代码）  → 跳过检索/生成，直接 test_gen 设计端到端测试用例
    """
    if route_after_graph_review(state) == "rejected":
        return "rejected"
    from .schemas import has_project_code

    return "with_code" if has_project_code(state.get("requirement")) else "no_code"


def _route_after_code_search(state: GlobalState) -> str:
    """provider_nodes.route_after_code_search 已先算业务标签；本函数再叠 error→retry/abort。"""
    business = route_after_code_search(state)
    if business == "has_results":
        # 成功，直接推进
        return "has_results"
    # no_results 或 retry（业务返回的 retry 也是 retry 标签）
    err = state.get("last_error_code")
    if err:
        retryable = bool(state.get("last_error_retryable"))
        count = (state.get("retry_count") or {}).get("code_search", 0)
        if retryable and count <= _GRAPH_RETRY_CAP_PER_NODE:
            return "retry"
        return "abort"
    # 无错误但没结果：业务上就是 no_results
    return "no_results"


def _route_after_code_gen(state: GlobalState) -> str:
    """业务路由（lint_ok / force_test / retry）+ 错误分流（abort）。"""
    business = route_after_code_gen(state)
    err = state.get("last_error_code")
    if not err:
        return business
    retryable = bool(state.get("last_error_retryable"))
    count = (state.get("retry_count") or {}).get("code_gen", 0)
    if business == "retry" and retryable and count <= _GRAPH_RETRY_CAP_PER_NODE:
        return "retry"
    if retryable and count <= _GRAPH_RETRY_CAP_PER_NODE:
        # 哪怕业务说 lint_ok/force_test，只要上轮有可重试错就先 retry（一般是 lint/语法坏了）
        return "retry"
    return "abort"


def _route_after_test_gen(state: GlobalState) -> str:
    """测试设计后路由：业务上无条件 → test_run 执行；仅剩错误分流（retry / abort）。"""
    business = route_after_test_gen(state)  # 恒为 "run"
    err = state.get("last_error_code")
    if not err:
        return business
    retryable = bool(state.get("last_error_retryable"))
    count = (state.get("retry_count") or {}).get("test_gen", 0)
    if retryable and count <= _GRAPH_RETRY_CAP_PER_NODE:
        return "retry"
    return "abort"


def _route_after_code_apply(state: GlobalState) -> str:
    """落盘后路由：可重试错（diff 应用不了）且回修未超限 → 回 code_gen 重新生成；
    其余（成功 / 降级 / 关闭）→ 继续测试设计。"""
    err = state.get("last_error_code")
    if not err:
        return "continue"
    business = route_after_code_apply(state)
    if business == "retry":
        count = (state.get("retry_count") or {}).get("apply_code", 0)
        if count <= 1:  # diff 坏了回炉一次就够，反复回炉没意义
            return "retry"
    return "continue"


def _route_after_test_run(state: GlobalState) -> str:
    """执行后路由（业务+错误分流）：
      test_ok / skip / review_failed → review；test_fail → code_gen 回修；
      基础设施可重试错 → test_run 本身；不可重试 → abort。
    """
    err = state.get("last_error_code")
    if err:
        retryable = bool(state.get("last_error_retryable"))
        count = (state.get("retry_count") or {}).get("test_run", 0)
        if retryable and count <= _GRAPH_RETRY_CAP_PER_NODE:
            return "retry"
        return "abort"
    return route_after_test_run(state)


# ═══════════════════════════════════════════════════════════════════
# 构建 Graph
# ═══════════════════════════════════════════════════════════════════


def build_graph():
    """返回一个已编译的 LangGraph（带 SQLite Checkpoint）。"""
    # 复用全局打开的 sqlite3 连接，让 compile 后的 graph 可以跨调用持久存活
    checkpointer = SqliteSaver(_get_sqlite_conn())
    try:
        checkpointer.setup()  # 建表（幂等）
    except Exception:
        pass  # 已建表则忽略

    workflow = StateGraph(GlobalState)

    # ── 注册节点 ──────────────────────────────────────
    workflow.add_node("clarify_extract", clarify_extract)
    workflow.add_node("clarify_validate", clarify_validate)
    workflow.add_node("clarify_build_question", clarify_build_question)
    workflow.add_node("compress_messages", compress_messages)
    workflow.add_node("requirement_review", requirement_review_node)
    workflow.add_node("graph_type_select", graph_type_select)
    workflow.add_node("graph_generate", graph_generate)
    workflow.add_node("dead_letter_drain", dead_letter_drain_node)

    # ── 连边 + 条件路由 ────────────────────────────────
    # 每轮先压缩热记忆（评审稿 §2.3 D3），再进入澄清抽取
    workflow.add_edge(START, "compress_messages")
    workflow.add_edge("compress_messages", "clarify_extract")

    # extract → ok→validate / retry→extract
    workflow.add_conditional_edges(
        "clarify_extract",
        _route_after_extract,
        {
            "ok": "clarify_validate",
            "retry": "clarify_extract",
        },
    )

    # validate → 三选一
    workflow.add_conditional_edges(
        "clarify_validate",
        _route_after_validate,
        {
            "need_more_info": "clarify_build_question",
            "info_complete": "requirement_review",  # 需求先经用户确认，再选图种类制图
            "abort": "dead_letter_drain",
        },
    )

    # 需求确认 → 选图种类 / 驳回则本轮 END（用户补充需求后重新澄清）
    workflow.add_conditional_edges(
        "requirement_review",
        route_after_requirement_review,
        {
            "confirmed": "graph_type_select",
            "rejected": END,
        },
    )

    # 问完问题 → 本轮结束（可重试错就回 retry 本节点）
    workflow.add_conditional_edges(
        "clarify_build_question",
        _route_after_build_question,
        {
            "__end__": END,
            "retry": "clarify_build_question",
        },
    )

    # abort 汇点：落死信 → END
    workflow.add_edge("dead_letter_drain", END)

    # 图种类已选定 → 制图（graph_type 未选时该节点不会走到这里：interrupt 已挂起）
    workflow.add_edge("graph_type_select", "graph_generate")

    # 制图结果 → 推进 / 重试 / abort
    workflow.add_conditional_edges(
        "graph_generate",
        _route_after_graph_generate,
        {
            "ok": END,
            "retry_graph": "graph_generate",
            "abort": "dead_letter_drain",
        },
    )

    graph = workflow.compile(checkpointer=checkpointer)
    return graph


# ═══════════════════════════════════════════════════════════════════
# 阶段二/三：带 Provider 的完整流程 Graph
# ═══════════════════════════════════════════════════════════════════


def build_graph_with_providers(providers: Providers | None = None):
    """构建包含代码检索/渲染/生成/落盘/测试全链路的 LangGraph。

    与 build_graph() 的区别：
      - graph_generate 后不再直接 END，而是进入制图门禁 graph_review
      - 门禁 approve 后按是否提供项目代码分流（schemas.has_project_code）：
          有代码 → code_search → graph_render → code_gen → apply_code → checklist_route → test_gen
          无代码 → checklist_route → test_gen（仅需求模式：基于需求+逻辑图设计端到端测试用例）
      - checklist_route：进入测试设计前先按需求路由 .checklist 业务清单库，
        有候选才弹确认门禁（勾选后注入用例设计）；空库/无匹配/已路由静默放行
      - 终审 review 的 reject 同样分流：代码模式回 code_gen；仅需求模式回 test_gen
      - Provider 驱动的阶段二/三节点 + 本地执行闭环节点（apply_code / test_run）
      - 路由：
          检索成功 → 渲染；检索可重试错 → retry；检索不可重试或真没结果 → 回澄清
          lint 成功 → apply_code 落盘；lint/生成可重试 → retry code_gen；超限/不可重试 → abort
          落盘成功/降级 → test_gen；diff 坏了可回炉一次 → code_gen
          测试设计 → test_run 真实执行；通过 → 人工验收；
          失败 → 回 code_gen（带失败摘要，上限 TEST_RUN_MAX_FIX_ROUNDS）；
          超限带失败报告 → 人工验收；未执行（跳过/仅需求模式）→ 人工验收
    """
    p = providers or get_providers()
    checkpointer = SqliteSaver(_get_sqlite_conn())
    try:
        checkpointer.setup()
    except Exception:
        pass

    workflow = StateGraph(GlobalState)

    # ── 阶段一节点（复用） ─────────────────────────────
    workflow.add_node("clarify_extract", clarify_extract)
    workflow.add_node("clarify_validate", clarify_validate)
    workflow.add_node("clarify_build_question", clarify_build_question)
    workflow.add_node("compress_messages", compress_messages)
    workflow.add_node("requirement_review", requirement_review_node)
    workflow.add_node("graph_type_select", graph_type_select)
    workflow.add_node("graph_generate", graph_generate)
    workflow.add_node("dead_letter_drain", dead_letter_drain_node)

    # ── 阶段二/三 Provider 节点（工厂注入） ────────────
    workflow.add_node("code_search", make_code_search_node(p))
    workflow.add_node("graph_render", make_graph_render_node(p))
    workflow.add_node("graph_review", graph_review_node)
    workflow.add_node("code_gen", make_code_gen_node(p))
    workflow.add_node("apply_code", make_apply_code_node())
    workflow.add_node("checklist_route_match", checklist_route_match)
    workflow.add_node("checklist_route_gate", checklist_route_gate)
    workflow.add_node("test_gen", make_test_gen_node(p))
    workflow.add_node("test_run", make_test_run_node())
    workflow.add_node("review", review_node)

    # ── 连边 ──────────────────────────────────────────
    # 每轮先压缩热记忆，再进入澄清抽取
    workflow.add_edge(START, "compress_messages")
    workflow.add_edge("compress_messages", "clarify_extract")

    # extract → ok / retry
    workflow.add_conditional_edges(
        "clarify_extract",
        _route_after_extract,
        {
            "ok": "clarify_validate",
            "retry": "clarify_extract",
        },
    )

    workflow.add_conditional_edges(
        "clarify_validate",
        _route_after_validate,
        {
            "need_more_info": "clarify_build_question",
            "info_complete": "requirement_review",  # 需求先经用户确认，再选图种类制图
            "abort": "dead_letter_drain",
        },
    )

    # 需求确认 → 选图种类 / 驳回则本轮 END（用户补充需求后重新澄清）
    workflow.add_conditional_edges(
        "requirement_review",
        route_after_requirement_review,
        {
            "confirmed": "graph_type_select",
            "rejected": END,
        },
    )

    # build_question：可重试错就回该节点；否则本轮 END（CLI 重新 START 进 clarify_extract）
    workflow.add_conditional_edges(
        "clarify_build_question",
        _route_after_build_question,
        {
            "__end__": END,
            "retry": "clarify_build_question",
        },
    )

    # 图种类已选定 → 制图
    workflow.add_edge("graph_type_select", "graph_generate")

    # 制图 → 制图门禁（人工确认图↔需求对齐）→ 推进到检索 / 重试制图 / abort
    workflow.add_conditional_edges(
        "graph_generate",
        _route_after_graph_generate,
        {
            "ok": "graph_review",
            "retry_graph": "graph_generate",
            "abort": "dead_letter_drain",
        },
    )

    # 制图门禁：approve → 按有无项目代码分流（有代码先检索；无代码先清单路由再测试设计）；reject → 重制图
    workflow.add_conditional_edges(
        "graph_review",
        _route_after_graph_review,
        {
            "with_code": "code_search",
            "no_code": "checklist_route_match",  # 仅需求模式：先清单路由，再出端到端测试用例
            "rejected": "graph_generate",
        },
    )

    # 代码检索：有结果→渲染；可重试→retry；真没结果→回澄清；不可重试→abort
    workflow.add_conditional_edges(
        "code_search",
        _route_after_code_search,
        {
            "has_results": "graph_render",
            "retry": "code_search",
            "no_results": "clarify_extract",  # SPEC 5.8：回退澄清，等用户补充上下文
            "abort": "dead_letter_drain",
        },
    )

    # 渲染 → 代码生成
    workflow.add_edge("graph_render", "code_gen")

    # 代码生成：lint_ok → 落盘；force_test（lint 超限）不落盘，先清单路由再设计；
    #           retry → code_gen；abort → drain
    workflow.add_conditional_edges(
        "code_gen",
        _route_after_code_gen,
        {
            "lint_ok": "apply_code",
            "force_test": "checklist_route_match",
            "retry": "code_gen",
            "abort": "dead_letter_drain",
        },
    )

    # diff 落盘：成功/降级 → 清单路由 → 测试设计；diff 坏了可回炉一次 → code_gen
    workflow.add_conditional_edges(
        "apply_code",
        _route_after_code_apply,
        {
            "continue": "checklist_route_match",
            "retry": "code_gen",
        },
    )

    # 清单路由：match（LLM 匹配，结果落 state）→ gate（有候选才 interrupt 确认）→ 测试设计
    workflow.add_edge("checklist_route_match", "checklist_route_gate")
    workflow.add_edge("checklist_route_gate", "test_gen")

    # 测试设计完成后一律交给 test_run 做真实执行判定
    workflow.add_conditional_edges(
        "test_gen",
        _route_after_test_gen,
        {
            "run": "test_run",
            "retry": "test_gen",
            "abort": "dead_letter_drain",
        },
    )

    # 真实执行：通过→人工验收；失败→code_gen 回修（带失败摘要）；
    #           超限带失败报告→人工验收；未执行（跳过）→人工验收；可重试错→自身；不可重试→abort
    workflow.add_conditional_edges(
        "test_run",
        _route_after_test_run,
        {
            "test_ok": "review",
            "test_fail": "code_gen",
            "review_failed": "review",
            "skip": "review",
            "retry": "test_run",
            "abort": "dead_letter_drain",
        },
    )

    # 人工验收：approve → END；reject → 代码模式回代码生成 / 仅需求模式回测试用例设计
    workflow.add_conditional_edges(
        "review",
        route_after_review,
        {
            "approved": END,
            "rejected": "code_gen",
            "rejected_test": "checklist_route_match",  # 仅需求模式打回：路由已做过，节点内直接放行
        },
    )

    # abort 汇点：落死信 → END
    workflow.add_edge("dead_letter_drain", END)

    graph = workflow.compile(checkpointer=checkpointer)
    return graph


# ═══════════════════════════════════════════════════════════════════
# 初始状态构造（新 thread 启动时用）
# ═══════════════════════════════════════════════════════════════════


def initial_state() -> dict[str, Any]:
    return {
        "messages": [],
        "requirement": empty_requirement(),
        "code_context": [],
        "logic_graph": None,
        "graph_type": None,
        "code_changes": [],
        "test_report": None,
        "opencode_sessions": {"search": None, "code_gen": None, "test_gen": None},
        "current_stage": "clarify",
        "session_title": "",
        "clarify_mode": "normal",
        "clarify_mode_prompt": False,
        "missing_fields": [],
        "requirement_confirmed": False,
        "requirement_sources": {},
        "review_feedback": None,
        "code_apply": None,
        "test_failure": None,
        "checklist_route": None,
        "checklist_context": None,
        "checklist_routed": False,
        "adopted_cases": None,
        "distill_dismissed": False,
        "last_error": None,
        "last_error_code": None,
        "last_error_retryable": False,
        "retry_count": {},
        "dead_letters": [],
    }
