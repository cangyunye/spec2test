"""`devflow spec` 一次性自动全流程：无人评审、需求缺口 AI 脑补、产物本地落盘。

与交互模式（new/resume + _interactive_loop）共用同一张全链路图
（build_graph_with_providers），区别只在驱动方式：
  - 交互模式：每个 interrupt 门禁停下等用户输入；
  - spec 模式：检测到门禁挂起即按「推荐」自动 resume（decide_gate），
    澄清仍有缺失时由 LLM 直接给出最佳推断值（fill_missing，标 inferred），
    全程无人工介入，结束后把 CSV 用例与全过程 MD 报告写到本地。

边界约定：
  - 全程不写 checklist 库——没有人工用例过滤步骤，管线内本就无写库调用
    （write_checklist 仅 Web 端人工确认后的 commit 端点会调）。
  - 脑补字段直写 state（requirement_sources 标 SOURCE_INFERRED），不走消息
    抽取——抽取无法区分「文档原话」与「AI 脑补」，直写才能保证标注准确。
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings
from .doc_reader import read_doc
from .errors import CLARIFY_LOOP_EXHAUSTED
from .events import events_from_stream
from .graph_types import suggest_graph_types
from .state import SOURCE_INFERRED

# 自动驱动循环轮数上限（6 门禁 × 重试回炉 + 脑补，实际全流程 ~15 轮内，留足余量防死循环）
MAX_LOOP_ROUNDS = 120
# 需求缺口脑补轮数上限：连续两次补完仍缺 → 视为文档信息量不足，报错退出
MAX_FILL_ROUNDS = 2
# 脑补 prompt 里需求文档的最大携带长度（字符）
_FILL_DOC_MAX_CHARS = 8000
# 脑补 LLM 调用的外层重试：前序节点（澄清抽取）遇网关抖动会把共享熔断器打开，
# 脑补作为「全自动模式的最后一道兜底」值得等熔断冷却自愈，而不是一次失败就放弃
_FILL_ATTEMPTS = 3
_FILL_RETRY_WAIT_SEC = 20.0

_sleep = asyncio.sleep  # 测试可替换

# 全链路 6 个 interrupt 门禁 → 自动应答映射（与 web/server.py _GATE_NODES 一致）
_GATE_NODES = (
    "requirement_review",
    "graph_type_select",
    "graph_review",
    "checklist_route_gate",
    "feature_gate",
    "review",
)


# ═══════════════════════════════════════════════════════════════════
# 过程日志：记录每一步自动决策，最终渲染进 MD 报告
# ═══════════════════════════════════════════════════════════════════

class SpecJournal:
    """spec 运行过程日志：门禁自动决策、AI 脑补明细、错误，供 render_spec_md 使用。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        # 点路径 → AI 脑补值（含 mock 兜底标记，均要在报告中标注）
        self.filled_fields: dict[str, Any] = {}
        self.fill_mock: bool = False
        self.fill_rounds: int = 0

    def record(self, kind: str, **detail: Any) -> None:
        self.entries.append({"kind": kind, "t": time.strftime("%H:%M:%S"), **detail})


@dataclass
class SpecResult:
    """run_spec 的返回：终态 + 产物路径。"""
    tid: str
    ok: bool
    error: str | None = None
    state: dict[str, Any] = field(default_factory=dict)
    journal: SpecJournal = field(default_factory=SpecJournal)
    files: list[Path] = field(default_factory=list)


def _say(console: Any, msg: str) -> None:
    if console is not None:
        console.print(msg)


# ═══════════════════════════════════════════════════════════════════
# 门禁自动决策（纯函数，按各节点 resume 契约应答，全按推荐）
# ═══════════════════════════════════════════════════════════════════

def decide_gate(gate: str, vals: dict[str, Any]) -> tuple[Any, str]:
    """门禁名 + 当前 state → (resume 值, 决策理由)。

    各门禁 resume 契约见对应节点 docstring：
    requirement_review / graph_type_select / graph_review / checklist_route_gate /
    feature_gate / review。
    """
    if gate == "requirement_review":
        inferred = [
            k for k, v in (vals.get("requirement_sources") or {}).items()
            if v in (SOURCE_INFERRED, "mock")
        ]
        return "confirm", (
            "需求确认通过" + (f"（{len(inferred)} 个 AI 脑补字段已标注：{('、'.join(inferred))}）" if inferred else "")
        )

    if gate == "graph_type_select":
        cands = suggest_graph_types(vals.get("requirement"))
        # flowchart 恒在首位作默认；非默认种类按命中数降序，取第一个达推荐阈值的
        pick, reason = cands[0], str(cands[0].get("reason") or "默认 · 通用处理流程")
        for c in cands[1:]:
            if c.get("recommended"):
                pick, reason = c, str(c.get("reason") or "")
                break
        return pick["id"], f"按规则推荐选择「{pick['label']}」：{reason}"

    if gate == "graph_review":
        return "approve", "制图与需求对齐检查通过（自动放行）"

    if gate == "checklist_route_gate":
        route = vals.get("checklist_route") or {}
        cands = route.get("candidates") or []
        if not cands:
            status = route.get("status") or "empty_library"
            return {"decision": "skip"}, (
                f"清单库{'为空' if status == 'empty_library' else '无匹配业务'}，跳过清单注入"
            )
        # 按推荐 = AI 路由预选（suggested）全采纳；缺标记时退化为全选
        selected: list[str] = []
        for biz in cands:
            if biz.get("suggested", True):
                selected.append(str(biz["rel_dir"]))
            for sub in biz.get("children") or []:
                if sub.get("suggested", True):
                    selected.append(str(sub["rel_dir"]))
        return {"decision": "confirm", "selected": selected}, (
            f"加载 AI 路由推荐的 {len(selected)} 个业务清单：{('、'.join(selected))}"
        )

    if gate == "feature_gate":
        # 节点语义：skip = 未回答的问题全部按 recommended 继续
        return {"decision": "skip"}, "拆分问题全部按推荐项执行"

    if gate == "review":
        cases = (vals.get("test_report") or {}).get("test_cases") or []
        # 兼容两种用例形态：设计用例（case_id）/ pi 执行条目（test_symbol）
        adopted = [
            str(c.get("case_id") or c.get("test_symbol"))
            for c in cases
            if c.get("case_id") or c.get("test_symbol")
        ]
        return {"decision": "approve", "adopted": adopted}, (
            f"验收通过，采纳全部 {len(adopted)} 条用例"
        )

    raise ValueError(f"未知门禁: {gate}")


def _pick_gate(next_nodes: tuple | list) -> str | None:
    for g in _GATE_NODES:
        if g in next_nodes:
            return g
    return None


# ═══════════════════════════════════════════════════════════════════
# 需求脑补：缺失字段由 LLM 给出「AI 脑补的最佳选择」，直写 state 标 inferred
# ═══════════════════════════════════════════════════════════════════

def _parse_missing(missing: list[str]) -> dict[str, str]:
    """missing_fields 元素（"字段点路径: 说明"）→ {点路径: 说明}。"""
    out: dict[str, str] = {}
    for item in missing or []:
        key, _, desc = str(item).partition(":")
        key = key.strip()
        if key:
            out[key] = desc.strip()
    return out


_FILL_SYSTEM_PROMPT = (
    "你是资深需求分析师，运行在全自动模式：需求文档信息不足时，"
    "由你直接给出每个缺失字段最合理的推断值（AI 脑补的最佳选择）。"
    "要求：值必须具体、可执行、无占位符；贴合文档与项目上下文；"
    "无法精确推断时给出该类系统最通用的合理默认。"
)


def _fill_user_prompt(
    req: dict[str, Any], missing: dict[str, str], doc_text: str
) -> str:
    lines = [
        "【需求文档（节选）】",
        doc_text[:_FILL_DOC_MAX_CHARS],
        "",
        "【当前需求（JSON）】",
        json.dumps(req, ensure_ascii=False, indent=2),
        "",
        "【缺失字段】",
    ]
    for dotted, desc in missing.items():
        lines.append(f"- {dotted}: {desc}")
    lines += [
        "",
        "为以上每个缺失字段给出你的最佳推断值：",
        "- str 字段给一句话；list 字段给 2~5 条具体条目；bool 给 true/false；",
        "- io_constraints.input / io_constraints.output 分别描述系统输入与输出；",
        "- acceptance_criteria 用可验证的验收语句；edge_cases 覆盖关键边界与异常。",
        '只输出 JSON：{"fields": {"<字段点路径>": <值>, ...}}',
    ]
    return "\n".join(lines)


def _merge_fill(req: dict[str, Any], fill: dict[str, Any]) -> dict[str, Any]:
    """脑补值按点路径合并进 requirement（深拷贝，不改原 dict）。"""
    out = copy.deepcopy(req)
    for dotted, val in fill.items():
        parts = [p for p in str(dotted).split(".") if p]
        if not parts:
            continue
        cur = out
        for p in parts[:-1]:
            nxt = cur.get(p)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[p] = nxt
            cur = nxt
        cur[parts[-1]] = val
    return out


def fill_missing(
    graph: Any,
    config: dict[str, Any],
    vals: dict[str, Any],
    doc_text: str,
    journal: SpecJournal,
    console: Any = None,
) -> bool:
    """脑补缺失字段并直写 state，让图从 clarify_validate 完备分支继续。

    直写（update_state as_node="clarify_validate"）而非走消息抽取：抽取只能把
    脑补内容当用户原话（标 user），直写才能把新字段准确标为 SOURCE_INFERRED，
    需求确认单与最终报告中的「AI 脑补」标注才真实。返回 False = 无法继续。
    """
    missing = _parse_missing(vals.get("missing_fields") or [])
    if not missing:
        return False
    journal.fill_rounds += 1
    _say(console, f"[cyan]…[/] 需求仍有 {len(missing)} 项缺口，AI 脑补第 {journal.fill_rounds} 轮："
                  f"{('、'.join(missing))}")

    from .llm_client import invoke_json

    meta: dict[str, Any] = {}
    result: dict[str, Any] | None = None
    last_err: Exception | None = None

    async def _call() -> dict[str, Any]:
        return await invoke_json(
            _FILL_SYSTEM_PROMPT,
            _fill_user_prompt(vals.get("requirement") or {}, missing, doc_text),
            response_type="spec_fill_missing",
            meta=meta,
        )

    async def _call_with_retry() -> None:
        nonlocal result, last_err
        for attempt in range(_FILL_ATTEMPTS):
            try:
                result = await _call()
                return
            except Exception as e:
                last_err = e
                if attempt < _FILL_ATTEMPTS - 1:
                    wait = _FILL_RETRY_WAIT_SEC * (attempt + 1)
                    _say(console, f"[yellow]…[/] 脑补调用失败（{e}），{wait:.0f}s 后重试"
                                  f"（{attempt + 2}/{_FILL_ATTEMPTS}）")
                    await _sleep(wait)

    asyncio.run(_call_with_retry())
    if result is None:
        journal.record("error", step="fill_missing", detail=str(last_err))
        _say(console, f"[red]×[/] 脑补调用失败: {last_err}")
        return False

    fill = result.get("fields") if isinstance(result, dict) else None
    if not isinstance(fill, dict) or not fill:
        # mock 兜底返回的是需求形状的演示数据（无 "fields" 包装）：取与缺失字段
        # 相交的键（含 io_constraints.input 这类点路径 → 顶层 dict 子键）继续流程，
        # fill_mock 标记让报告明确警示「非真实推断」
        if meta.get("mock") and isinstance(result, dict):
            fill = {}
            for dotted in missing:
                if dotted in result:
                    fill[dotted] = result[dotted]
                elif "." in dotted:
                    top, sub = dotted.split(".", 1)
                    sub_val = result.get(top)
                    if isinstance(sub_val, dict) and sub in sub_val:
                        fill[dotted] = sub_val[sub]
    if not isinstance(fill, dict) or not fill:
        journal.record("error", step="fill_missing", detail="脑补结果为空")
        _say(console, "[red]×[/] 脑补结果为空，无法继续")
        return False
    if meta.get("mock"):
        journal.fill_mock = True

    # 只收缺失字段的值，防止模型顺手改写已有字段
    clean = {k: v for k, v in fill.items() if k in missing}
    req = _merge_fill(vals.get("requirement") or {}, clean)
    sources = dict(vals.get("requirement_sources") or {})
    for dotted in clean:
        sources[dotted] = SOURCE_INFERRED
    journal.filled_fields.update(clean)
    journal.record(
        "fill",
        round=journal.fill_rounds,
        fields={k: v for k, v in clean.items()},
        mock=bool(meta.get("mock")),
    )
    _say(console, f"[green]✓[/] 已脑补 {len(clean)} 项（均标注为 AI 脑补的最佳选择）")

    graph.update_state(config, {
        "requirement": req,
        "requirement_sources": sources,
        "missing_fields": [],
        "clarify_round_no_progress": False,
    }, as_node="clarify_validate")
    return True


# ═══════════════════════════════════════════════════════════════════
# 事件消费（终端进度显示；journal 只记门禁决策，节点产物最终从 state 读）
# ═══════════════════════════════════════════════════════════════════

def _render_progress(event: dict, console: Any) -> None:
    etype = event.get("type")
    if console is None or etype == "gate":
        return
    if etype == "stage":
        _say(console, f"[cyan]▶[/] 阶段推进 → [bold]{event.get('stage')}[/]")
    elif etype == "provider":
        if event.get("status") == "skip":
            _say(console, f"[yellow]↯[/] {event.get('provider')} 失败（{event.get('code')}）→ 切换下一个")
        else:
            _say(console, f"[green]✓[/] {event.get('provider')} · {event.get('model')} 完成")
    elif etype == "artifact" and event.get("kind") == "logic_graph":
        payload = event.get("payload") or {}
        _say(console, f"[green]✓[/] 逻辑图已生成 graph_id={payload.get('graph_id')}")
    elif etype == "error":
        _say(console, f"[red]×[/] 节点错误: {event.get('error')}")


def _drive(graph: Any, payload: Any, config: dict[str, Any], console: Any = None) -> None:
    """推进一步并消费事件到下一个暂停点。payload = 输入 dict / Command / None。"""
    for event in events_from_stream(
        graph.stream(payload, config, stream_mode="updates")
    ):
        _render_progress(event, console)


# ═══════════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════════

def run_spec(
    doc_path: str | Path,
    *,
    out_dir: Path = Path("./artifacts"),
    set_fields: list[str] | None = None,
    thread_id: str | None = None,
    console: Any = None,
) -> SpecResult:
    """一次性自动全流程：读文档 → 喂图 → 自动应答全部门禁 → 导出 CSV/MD。

    返回 SpecResult；无论成败都尽量落盘过程报告（ok=False 时也带已产出文件）。
    """
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from .orchestrator import build_graph_with_providers, initial_state
    from .cli import apply_assignments

    doc_text = read_doc(doc_path)
    tid = thread_id or f"spec-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": tid}}
    graph = build_graph_with_providers()
    journal = SpecJournal()
    _say(console, f"[green]✓[/] 已读取需求文档: [bold]{doc_path}[/]（{len(doc_text)} 字符）")
    _say(console, f"[cyan]▶[/] spec 全自动模式启动 thread_id=[bold]{tid}[/]"
                  "（无评审 · 缺口 AI 脑补 · 全按推荐 · 不写 checklist 库）")
    journal.record("doc", path=str(doc_path), chars=len(doc_text))

    result = SpecResult(tid=tid, ok=False, journal=journal)
    state: dict[str, Any] = {}

    try:
        state = initial_state()
        if set_fields:
            state["requirement"] = apply_assignments(state["requirement"], set_fields)
            journal.record("set", fields=set_fields)
        next(graph.stream(state, config))  # 触发 initial checkpoint（与 cmd_new 同路径）

        # 需求文档作为首条消息喂入，走正常澄清抽取
        _drive(graph, {"messages": [HumanMessage(content=doc_text)]}, config, console)
        journal.record("gate", gate="(输入)", decision="需求文档已喂入", rationale="进入澄清抽取")

        error: str | None = None
        for _ in range(MAX_LOOP_ROUNDS):
            snap = graph.get_state(config)
            vals = snap.values or {}
            state = vals
            nxt = tuple(snap.next or ())
            stage = vals.get("current_stage")

            if vals.get("last_error_code") == CLARIFY_LOOP_EXHAUSTED:
                error = "澄清轮次耗尽（文档信息量不足，脑补前图已自行放弃）"
                break

            gate = _pick_gate(nxt)
            if gate:
                decision, rationale = decide_gate(gate, vals)
                journal.record("gate", gate=gate, decision=decision, rationale=rationale)
                shown = decision if isinstance(decision, str) else str(decision.get("decision"))
                _say(console, f"[bold yellow]⚙[/] [{gate}] 自动决策 → [bold]{shown}[/]"
                              f"  [dim]{rationale}[/]")
                _drive(graph, Command(resume=decision), config, console)
                continue

            if not nxt:
                # 图已到 END：done=正常完成；clarify 缺失=脑补；其余=异常终态
                if stage == "done":
                    result.ok = True
                    break
                if stage == "clarify" and (vals.get("missing_fields") or []):
                    if journal.fill_rounds >= MAX_FILL_ROUNDS:
                        error = (f"脑补 {journal.fill_rounds} 轮后需求仍有缺口: "
                                 f"{('、'.join(vals.get('missing_fields') or []))}")
                        break
                    if not fill_missing(graph, config, vals, doc_text, journal, console):
                        error = "需求缺口脑补失败"
                        break
                    _drive(graph, None, config, console)
                    continue
                error = (
                    f"流程在 stage={stage} 提前结束"
                    + (f"：{vals.get('last_error')}" if vals.get("last_error") else "")
                )
                break

            # 半途中断（节点间错误恢复点）：续跑到下一暂停点
            _drive(graph, None, config, console)
        else:
            error = f"超过最大循环轮数 {MAX_LOOP_ROUNDS}，强制中止"
    except KeyboardInterrupt:
        error = "用户中断（Ctrl+C），已导出中间产物"
    except Exception as e:  # 图运行异常：落盘已收集的过程再上抛语义
        error = f"运行出错: {e}"
        journal.record("error", step="run", detail=str(e))

    if not result.ok and error is None:
        error = "流程未完成"

    # ── 导出：CSV 用例 + 全过程 MD（+ 结构化 JSON 产物）───────────────
    try:
        result.files = export_all(state, journal, tid, out_dir, doc_path=str(doc_path))
    except Exception as e:
        journal.record("error", step="export", detail=str(e))
        _say(console, f"[red]×[/] 导出失败: {e}")

    result.state = state
    result.error = error
    journal.record("done", ok=result.ok, error=error)
    if result.ok:
        _say(console, f"[green]✓[/] 全流程完成！用例 "
                      f"{len(((state.get('test_report') or {}).get('test_cases') or []))} 条")
    else:
        _say(console, f"[red]×[/] {error}")
    for f in result.files:
        _say(console, f"[green]✓[/] 导出 → [bold]{f}[/]")
    return result


# ═══════════════════════════════════════════════════════════════════
# 导出：CSV 用例（12 列，与前端 testToCSV 对齐）+ 全过程 MD
# ═══════════════════════════════════════════════════════════════════

TIER_NAME = {"functional": "功能", "performance": "性能", "security": "安全"}

CSV_HEADERS = ["标识", "层级", "优先级", "类型", "标题", "所属模块",
               "前置条件", "步骤", "预期结果", "数据要求", "设计依据", "来源"]

_CASE_CSV_FIELDS = ("case_id", "tier", "priority", "case_type", "title", "target",
                    "precondition", "steps", "expected", "data_requirement", "rationale")


def _origin_label(case: dict[str, Any]) -> str:
    """用例来源标签：评审期人工补录（origin=manual）标「人工」，其余为 AI 设计。"""
    return "人工" if case.get("origin") == "manual" else "AI"


def _case_field(case: dict[str, Any], key: str) -> Any:
    """取用例字段,兼容两种形态:
    设计用例(case_id/title/steps...)与 pi 执行条目(test_symbol/test_file/code_snippet)。
    """
    val = case.get(key)
    if val not in (None, ""):
        return val
    fallback = {
        "case_id": "test_symbol",
        "title": "test_symbol",
        "target": "test_file",
        "steps": "code_snippet",
    }.get(key)
    return case.get(fallback) if fallback else val


def cases_to_csv(cases: list[dict[str, Any]]) -> str:
    """用例 → CSV 文本。BOM（Excel 中文兼容）+ 全字段引号转义 + CRLF，同前端。"""
    def q(v: Any) -> str:
        return '"' + str(v if v is not None else "").replace('"', '""') + '"'

    rows = [",".join(q(h) for h in CSV_HEADERS)]
    for c in cases:
        vals = [_case_field(c, k) for k in _CASE_CSV_FIELDS]
        vals[1] = TIER_NAME.get(str(c.get("tier") or ""), c.get("tier"))
        vals.append(_origin_label(c))
        rows.append(",".join(q(v) for v in vals))
    return "﻿" + "\r\n".join(rows)  # BOM：Excel 打开中文不乱码


def _md_cell(v: Any) -> str:
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")


def _req_field_source_label(src: str) -> str:
    if src == SOURCE_INFERRED:
        return "**AI 脑补的最佳选择**"
    if src == "mock":
        return "**AI 兜底演示数据（LLM 全挂时的 mock）**"
    if src == "user":
        return "用户文档"
    return "—"


def _render_cases_tables(cases: list[dict[str, Any]], features: list[dict[str, Any]]) -> list[str]:
    """用例明细：feature 拆分模式按功能点分章节，否则按所属模块分组。"""
    lines: list[str] = []
    header = "| 标识 | 层级 | 优先级 | 类型 | 标题 | 前置 | 步骤 | 预期 | 依据 | 来源 |"
    sep = "|---|---|---|---|---|---|---|---|---|---|"

    def case_rows(group_cases: list[dict[str, Any]]) -> list[str]:
        out = [header, sep]
        for c in group_cases:
            cells = [_md_cell(_case_field(c, k)) for k in
                     ("case_id", "tier", "priority", "case_type", "title",
                      "precondition", "steps", "expected", "rationale")]
            cells[1] = _md_cell(TIER_NAME.get(str(c.get("tier") or ""), c.get("tier")))
            cells.append(_md_cell(_origin_label(c)))
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
        return out

    feats = [f for f in features if isinstance(f, dict)]
    if feats:
        for fid in [str(f.get("feature_id") or f"F{i}") for i, f in enumerate(feats, 1)]:
            feat = next(f for f in feats if str(f.get("feature_id") or "") == fid)
            feat_cases = [c for c in cases if str(c.get("feature_id") or "") == fid]
            lines.append(f"#### {fid} {feat.get('name') or fid}")
            if feat.get("description"):
                lines.append(str(feat["description"]))
            if not feat_cases:
                lines += ["（本功能点无用例——见质量自检遗留缺口）", ""]
                continue
            lines += [f"共 {len(feat_cases)} 条：", ""]
            lines += case_rows(feat_cases)
        orphans = [
            c for c in cases
            if str(c.get("feature_id") or "") not in {str(f.get("feature_id") or "") for f in feats}
        ]
        if orphans:
            lines += [f"#### 未归属用例（异常，应回炉）× {len(orphans)}", ""]
            lines += case_rows(orphans)
    else:
        groups: dict[str, list[dict[str, Any]]] = {}
        for c in cases:
            groups.setdefault(str(c.get("target") or "通用"), []).append(c)
        for gi, (mod, mod_cases) in enumerate(groups.items(), start=1):
            lines += [f"#### 2.{gi} {mod}（{len(mod_cases)} 条）", ""]
            lines += case_rows(mod_cases)
    return lines


def render_spec_md(
    tid: str, journal: SpecJournal, vals: dict[str, Any], doc_path: str = ""
) -> str:
    """全过程 MD 报告：模式声明 → 需求清单（脑补标注）→ 决策时间线 → 脑补明细
    → 逻辑图 → 清单路由 → feature 拆分 → 用例明细 → 测试执行 → 产物清单。"""
    req = vals.get("requirement") or {}
    graph = vals.get("logic_graph") or {}
    report = vals.get("test_report") or {}
    cases = report.get("test_cases") or []
    run = report.get("run") or {}
    sources = vals.get("requirement_sources") or {}

    from .nodes.requirement_review import REQUIREMENT_FIELD_SPEC

    lines: list[str] = [f"# CaseCraft spec 全自动流程报告 · {tid}", ""]
    lines += [
        "> 本报告由 `devflow spec` 一次性自动模式生成：**全程无人工评审**，",
        "> 需求缺口由 AI 脑补（逐字段标注），图种类 / Checklist / 用例决策全按推荐自动通过。",
        "> 因无用例人工过滤步骤，**本会话产物不写入 checklist 库**。",
        "",
    ]
    from .schemas import has_project_code

    mode = "with_code（代码模式）" if has_project_code(req) else "no_code（仅需求模式）"
    lines += [
        f"- 输入文档：`{doc_path}`",
        f"- 运行模式：{mode}",
        f"- 最终阶段：{'✅ done（验收通过）' if vals.get('current_stage') == 'done' else vals.get('current_stage', '?')}",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    # ── 需求清单（含脑补标注）────────────────────────────────────
    lines += ["## 1. 需求清单", "", "| 字段 | 值 | 来源 |", "|---|---|---|"]
    for key, label, _kind in REQUIREMENT_FIELD_SPEC:
        cur = req
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur in (None, "", [], {}):
            continue
        shown = "；".join(str(x) for x in cur) if isinstance(cur, list) else str(cur)
        lines.append(f"| {label} | {_md_cell(shown)} | {_req_field_source_label(sources.get(key, ''))} |")
    lines.append("")

    # ── 自动决策时间线 ────────────────────────────────────────────
    lines += ["## 2. 自动决策时间线", "", "| 时间 | 环节 | 自动决策 | 理由 |", "|---|---|---|---|"]
    for e in journal.entries:
        if e["kind"] != "gate":
            continue
        decision = e.get("decision")
        if isinstance(decision, dict):
            decision = decision.get("decision") + (
                f"（{('、'.join(decision.get('selected') or decision.get('adopted') or []))}）"
                if decision.get("selected") or decision.get("adopted") else ""
            )
        lines.append(f"| {e.get('t', '')} | {e.get('gate', '')} | {_md_cell(decision)} | {_md_cell(e.get('rationale'))} |")
    lines.append("")

    # ── AI 脑补明细 ──────────────────────────────────────────────
    lines += ["## 3. AI 脑补字段明细", ""]
    if journal.filled_fields:
        if journal.fill_mock:
            lines += ["> ⚠️ LLM provider 全部不可用，以下为 mock 兜底演示数据，不代表真实推断。", ""]
        lines += [
            "以下字段在需求文档中缺失或不足，由 AI 脑补的最佳选择填充（需求来源已标 `inferred`）：",
            "",
            "| 字段 | 脑补值 |", "|---|---|",
        ]
        for dotted, val in journal.filled_fields.items():
            shown = "；".join(str(x) for x in val) if isinstance(val, list) else str(val)
            lines.append(f"| `{dotted}` | {_md_cell(shown)} |")
    else:
        lines.append("需求文档信息完备，无需脑补。")
    lines.append("")

    # ── 4 起章节编号动态递增（逻辑图 / Checklist / Feature 可能缺位,避免跳号）──
    sec = 3

    # ── 逻辑图 ───────────────────────────────────────────────────
    if graph:
        sec += 1
        lines += [
            f"## {sec}. 逻辑图", "",
            f"graph_id：`{graph.get('graph_id', '?')}` · 种类：{graph.get('graph_type', 'flowchart')}",
            "", "```mermaid", graph.get("mermaid_source", ""), "```", "",
        ]

    # ── Checklist 路由 ───────────────────────────────────────────
    route = vals.get("checklist_route") or {}
    if route:
        sec += 1
        selected = route.get("selected") or []
        decision = route.get("decision") or "—"
        detail = ("加载：" + "、".join(selected)) if selected and decision == "confirm" else "未注入清单"
        lines += [f"## {sec}. Checklist 路由", "",
                  f"- 决策：{decision}（{detail}）",
                  "- 说明：本次为自动模式，**未写入 checklist 库**。", ""]

    # ── Feature 拆分 ─────────────────────────────────────────────
    feats = report.get("features") or vals.get("features") or []
    if feats:
        sec += 1
        lines += [f"## {sec}. Feature 拆分（{len(feats)} 个功能点）", ""]
        for f in feats:
            if isinstance(f, dict):
                lines.append(f"- **{f.get('feature_id')} {f.get('name')}**：{_md_cell(f.get('description'))}")
        lines.append("")

    # ── 测试用例 ─────────────────────────────────────────────────
    if cases:
        sec += 1
        lines += [f"## {sec}. 测试用例（共 {len(cases)} 条）", ""]
        overview = report.get("overview")
        if overview:
            lines += [overview, ""]
        p0 = sum(1 for c in cases if str(c.get("priority") or "").upper() == "P0")
        lines += [
            f"- 优先级口径：P0 核心路径与关键校验 · P1 边界与重要异常 · P2 次要异常与体验（P0 共 {p0} 条）",
            f"- 完整表格版见同目录 `casecraft-tests-{tid}.csv`",
            "",
        ]
        lines += _render_cases_tables(cases, report.get("features") or [])
        checks = report.get("self_check") or []
        if checks:
            lines += [f"### {sec}.1 质量自检", ""] + [f"- {s}" for s in checks] + [""]

    # ── 测试执行 ─────────────────────────────────────────────────
    changes = vals.get("code_changes") or []
    if run or changes:
        sec += 1
        lines += [f"## {sec}. 测试执行与代码变更", ""]
        if run:
            if run.get("executed"):
                failures = int(run.get("failed", 0)) + int(run.get("errors", 0))
                lines.append(
                    f"- 真实 pytest：{'✅ 通过' if failures == 0 else '❌ 存在失败'} "
                    f"passed={run.get('passed', 0)} failed={run.get('failed', 0)} "
                    f"errors={run.get('errors', 0)} skipped={run.get('skipped', 0)} · "
                    f"覆盖率 {run.get('coverage_pct', 0)}% · {run.get('duration_sec', 0)}s"
                )
            else:
                lines.append(f"- 未真实执行（{run.get('skip_reason') or '仅场景设计'}）")
        for ch in changes:
            lint = "lint ✓" if ch.get("lint_passed") else "lint ✗"
            test = "test ✓" if ch.get("test_passed") else "test ?"
            lines.append(f"- `{ch.get('file_path')}` [{ch.get('action')}] {lint} {test}")
        lines.append("")

    # ── 尾注 ─────────────────────────────────────────────────────
    err_entries = [e for e in journal.entries if e["kind"] == "error"]
    if err_entries:
        lines += ["## 附：运行期错误", ""]
        lines += [f"- [{e.get('step', '')}] {e.get('detail', '')}" for e in err_entries]
        lines.append("")
    lines += ["---", "*由 CaseCraft `devflow spec` 自动生成 · 未经人工评审 · 未写入 checklist 库*", ""]
    return "\n".join(lines)


def export_artifacts(state: dict[str, Any], out_dir: Path) -> list[Path]:
    """导出结构化产物：需求 JSON、逻辑图 JSON/Mermaid、代码变更、测试报告。

    （原 cli._export_artifacts，spec 导出与 CLI :export 共用。）
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    req = state.get("requirement") or {}
    if any(bool(req.get(k)) for k in ("project_context", "target_modules", "acceptance_criteria")):
        p = out_dir / "requirement.json"
        p.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)

    graph = state.get("logic_graph")
    if graph:
        p = out_dir / "logic_graph.json"
        p.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)
        p = out_dir / "logic_graph.mmd"
        p.write_text(graph["mermaid_source"], encoding="utf-8")
        written.append(p)

    if state.get("code_changes"):
        p = out_dir / "code_changes.json"
        p.write_text(json.dumps(state["code_changes"], ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)

    apply_info = state.get("code_apply")
    if apply_info is not None:
        p = out_dir / "code_apply.json"
        p.write_text(json.dumps(apply_info, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)

    if state.get("test_report"):
        p = out_dir / "test_report.json"
        p.write_text(json.dumps(state["test_report"], ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)

    return written


def export_all(
    state: dict[str, Any],
    journal: SpecJournal,
    tid: str,
    out_dir: Path,
    doc_path: str = "",
) -> list[Path]:
    """spec 模式导出三件套：结构化 JSON 产物 + CSV 用例 + 全过程 MD。"""
    target = out_dir / tid
    written = export_artifacts(state, target)

    report = state.get("test_report") or {}
    cases = [c for c in (report.get("test_cases") or []) if isinstance(c, dict)]
    if cases:
        p = target / f"casecraft-tests-{tid}.csv"
        p.write_text(cases_to_csv(cases), encoding="utf-8")
        written.append(p)

    p = target / f"casecraft-spec-{tid}.md"
    p.write_text(render_spec_md(tid, journal, state, doc_path), encoding="utf-8")
    written.append(p)
    return written
