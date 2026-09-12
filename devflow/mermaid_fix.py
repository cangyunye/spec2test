"""Mermaid 源码自动修复与轻量校验（按图种类分派）。

LLM 生成的 mermaid 常见渲染失败原因（按出现频率）：
  1. 用 ```mermaid ... ``` 代码块包裹了整个源码
  2. 字符串里是字面量 \\n 而不是真实换行（整段挤成一行，必然解析失败）
  3. 节点/菱形标签里有未加引号的特殊字符：( ) [ ] { } " ' | ;
  4. 用了 :::class 或 class 语句但没定义 classDef
  5. 缺少首行声明（flowchart / sequenceDiagram / stateDiagram-v2 / erDiagram / journey）

sanitize_mermaid 按 1→5 逐项修复。标签加引号与 classDef 补齐是 flowchart 专属
语法（sequence/state/er/journey 的标签语法不同，乱加引号反而会破坏源码），非 flowchart
种类只做 1/2/5 三项通用修复。mermaid_problems 做修复后的轻量自检。
修复不了的问题不硬修——前端已有源码视图兜底。
"""
from __future__ import annotations

import re
from typing import Any

from .graph_types import DEFAULT_GRAPH_TYPE, graph_type_meta

# 标签里出现即需加引号的字符（分号是语句分隔符，混进标签会截断语句）
_RISKY_CHARS = set('()[]{}"\'|;')

# flowchart/graph 声明行（方向任选）
_HEADER_RE = re.compile(r"^\s*(flowchart|graph)\s+(TD|TB|BT|RL|LR)\b", re.IGNORECASE)
# 各种类声明行：sequenceDiagram / stateDiagram-v2 / erDiagram / journey（允许尾随配置）
_TYPED_HEADER_RES: dict[str, re.Pattern[str]] = {
    "flowchart": _HEADER_RE,
    "sequence": re.compile(r"^\s*sequenceDiagram\b"),
    "state": re.compile(r"^\s*stateDiagram-v2\b"),
    "er": re.compile(r"^\s*erDiagram\b"),
    "journey": re.compile(r"^\s*journey\b"),
}
_SKIP_LINE_RE = re.compile(r"^\s*(%%|classDef\s|class\s|style\s|subgraph\s|end\s*$)")
_CLASSDEF_NAME_RE = re.compile(r"^\s*classDef\s+(\w+)", re.MULTILINE)
# class 语句：class <逗号分隔的节点id列表> <类名>;  —— 最后一个 token 是类名
_CLASS_STMT_RE = re.compile(r"^\s*class\s+[\w\-, ]+?\s+(\w+);?\s*$", re.MULTILINE)
_PSEUDO_CLASS_USE_RE = re.compile(r":::(\w+)")

# 非 flowchart 种类的连线语法特征（轻量自检「有无内容」用）
_TYPED_EDGE_HINTS: dict[str, tuple[str, ...]] = {
    "sequence": ("->>", "-->>", "-)", "--)", "->", "--"),
    "state": ("-->", "-->", ":"),
    "er": ("|--", "|o", "||", "}o", "{"),
    # journey 任务行 `任务名: 分数: 角色` 以冒号分隔；section 分组行也算实质内容
    "journey": (":", "section"),
}


def _used_classes(text: str) -> set[str]:
    """收集源码中引用到的 class 名（:::name 与 class ... name 两种形态）。"""
    used = set(_PSEUDO_CLASS_USE_RE.findall(text))
    used.update(_CLASS_STMT_RE.findall(text))
    return used


def _escape_label(text: str) -> str:
    return '"' + text.replace('"', "#quot;") + '"'


def _needs_quoting(text: str) -> bool:
    return any(c in _RISKY_CHARS for c in text)


def _fix_shaped_labels(line: str, open_ch: str, close_ch: str) -> str:
    """给未加引号、且含特殊字符的 [...] / {...} 标签补双引号。

    已是 ["..."] / {"..."} 形式（首字符为引号）的不动；[[...]] 双层形状
    因 [^[] 排除集自然跳过外层，只处理内层。
    """
    pattern = re.compile(
        re.escape(open_ch) + r"([^" + re.escape(open_ch) + re.escape(close_ch) + r"]*)" + re.escape(close_ch)
    )

    def _repl(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith('"'):
            return m.group(0)
        if _needs_quoting(inner):
            return open_ch + _escape_label(inner) + close_ch
        return m.group(0)

    return pattern.sub(_repl, line)


def _fix_edge_labels(line: str) -> str:
    """给含特殊字符的 |边标签| 补双引号：-->|"已支付(含税费)"| 。"""
    def _repl(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith('"'):
            return m.group(0)
        if _needs_quoting(inner):
            return "|" + _escape_label(inner) + "|"
        return m.group(0)

    return re.sub(r"\|([^|]*)\|", _repl, line)


def sanitize_mermaid(src: str, graph_type: str = DEFAULT_GRAPH_TYPE) -> str:
    """把 LLM 产出的 mermaid 源码修到可渲染；修不了的保持原样交前端兜底。

    flowchart：全套修复（剥围栏/字面量换行/标签补引号/classDef 补齐/补声明行）。
    sequence / state / er：只做通用修复（剥围栏/字面量换行/补对应声明行）——
    标签补引号与 classDef 是 flowchart 专属语法，对其他种类会破坏源码。
    """
    if not src or not src.strip():
        return src or ""

    meta = graph_type_meta(graph_type)
    default_header = meta["header"]
    header_re = _TYPED_HEADER_RES.get(meta["id"], _HEADER_RE)
    is_flowchart = meta["id"] == "flowchart"

    text = src.strip()

    # 1. 剥代码块围栏（支持 ```mermaid / ```  开头，``` 结尾）
    fence = re.match(r"^```[a-zA-Z]*\s*", text)
    if fence:
        text = text[fence.end():]
        text = re.sub(r"```\s*$", "", text).strip()

    # 2. 字面量 \n（模型把换行写成了两个字符）→ 真实换行；统一 \r\n
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")

    lines = [ln.rstrip() for ln in text.split("\n")]
    body: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        # 跳过围栏残留与空行
        if stripped.startswith("```"):
            continue
        # 3. 逐行修标签引号；classDef/class/style/注释/subgraph 行不动
        #    （仅 flowchart：其他种类的标签语法不同，不适用引号规则）
        if is_flowchart and stripped and not _SKIP_LINE_RE.match(stripped):
            fixed = _fix_shaped_labels(ln, "[", "]")
            fixed = _fix_shaped_labels(fixed, "{", "}")
            fixed = _fix_edge_labels(fixed)
            body.append(fixed)
        else:
            body.append(ln)

    # 4. 缺声明行则补对应种类的声明（flowchart 补 flowchart TD）
    first_content = next((ln for ln in body if ln.strip()), "")
    if not header_re.match(first_content):
        body.insert(0, default_header)

    if not is_flowchart:
        return "\n".join(body).strip() + "\n"

    # 5. 用了 class 但没定义 classDef → 补默认定义（flowchart 专属）
    text = "\n".join(body)
    used = _used_classes(text)
    defined = set(_CLASSDEF_NAME_RE.findall(text))
    for name in sorted(used - defined):
        text += f"\nclassDef {name} fill:#f5a623,stroke:#333,stroke-width:1px;"

    return text.strip() + "\n"


def mermaid_problems(src: str, graph_type: str = DEFAULT_GRAPH_TYPE) -> list[str]:
    """轻量自检：返回仍存在的问题列表（空列表 = 基本可渲染）。"""
    problems: list[str] = []
    if not src or not src.strip():
        return ["mermaid 源码为空"]
    meta = graph_type_meta(graph_type)
    header_re = _TYPED_HEADER_RES.get(meta["id"], _HEADER_RE)
    edge_hints = _TYPED_EDGE_HINTS.get(meta["id"], ("-->", "---", "-.-"))
    text = src.strip()
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not header_re.match(lines[0]):
        problems.append(f"缺少 {meta['header']} 声明行")
    body = lines[1:]
    if not any(hint in ln for ln in body for hint in edge_hints):
        if not any("[" in ln or "(" in ln or "{" in ln for ln in body):
            problems.append("没有任何节点或连线")
    for i, ln in enumerate(lines, 1):
        if ln.count('"') % 2:
            problems.append(f"第 {i} 行引号不配对")
            break
    if meta["id"] == "flowchart":
        missing = _used_classes(text) - set(_CLASSDEF_NAME_RE.findall(text))
        if missing:
            problems.append(f"使用了未定义的 class: {', '.join(sorted(missing))}")
    return problems


# ═══════════════════════════════════════════════════════════════════
# 空壳检测 + 结构化数据确定性重建
# ═══════════════════════════════════════════════════════════════════

def is_stub_mermaid(src: str, graph_type: str = DEFAULT_GRAPH_TYPE) -> bool:
    """判断 Mermaid 源码是否只是声明头的空壳（无任何节点/连线）。

    网关在 function calling 下偶发把 mermaid_source 截断成只剩声明行
    （如「flowchart TD」），而同响应里的结构化数据（nodes/edges/…）完整独立。
    """
    problems = mermaid_problems(src, graph_type)
    return "mermaid 源码为空" in problems or "没有任何节点或连线" in problems


def _esc_label(text: Any) -> str:
    """转义标签里可能破坏 mermaid 语法的引号（mermaid 支持 #quot; 实体）。"""
    return str(text).replace('"', "#quot;")


_ER_CARDINALITY_TOKENS: dict[str, str] = {
    "one_to_one": "||--||",
    "one_to_many": "||--o{",
    "many_to_one": "}o--||",
    "many_to_many": "}o--o{",
}


def rebuild_mermaid_source(graph: dict[str, Any], graph_type: str) -> str | None:
    """从结构化数据确定性重建 Mermaid 源码；结构化数据不全时返回 None（保持原样）。

    mermaid_source 是模型独立产出、且最容易被网关截断的字段；而 nodes/edges、
    participants/messages、states/transitions、entities/relations、sections/tasks
    与它同源但各自完整。空壳时用它重建，保证渲染图与结构一致且必定可渲染。
    """
    if graph_type == "er":
        entities = graph.get("entities") or []
        relations = graph.get("relations") or []
        if len(entities) < 2:
            return None
        name_of = {e.get("e_id"): e.get("table") for e in entities}
        lines = ["erDiagram"]
        for e in entities:
            lines.append(f"{e.get('table')} {{")
            for a in e.get("attributes") or []:
                pk = " PK" if a.get("is_pk") else ""
                lines.append(f"  {a.get('type') or 'string'} {a.get('name')}{pk}")
            lines.append("  }")
        for r in relations:
            frm = name_of.get(r.get("from_entity"))
            to = name_of.get(r.get("to_entity"))
            if not frm or not to:
                continue
            token = _ER_CARDINALITY_TOKENS.get(r.get("cardinality"), "||--o{")
            lines.append(f"  {frm} {token} {to} : {r.get('label') or 'relates'}")
        return "\n".join(lines) + "\n"

    if graph_type == "flowchart":
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        if not nodes or not edges:
            return None
        node_ids = {n["node_id"] for n in nodes}
        lines = ["flowchart TD"]
        for n in nodes:
            label = _esc_label(n.get("label") or n["node_id"])
            lines.append(f'{n["node_id"]}["{label}"]')
        for e in edges:
            if e.get("from_node") in node_ids and e.get("to_node") in node_ids:
                cond = f'|"{_esc_label(e["condition"])}"|' if e.get("condition") else ""
                lines.append(f'{e["from_node"]} -->{cond} {e["to_node"]}')
        return "\n".join(lines) + "\n"

    if graph_type == "sequence":
        participants = graph.get("participants") or []
        messages = graph.get("messages") or []
        if not participants:
            return None
        aliases = {p["alias"] for p in participants}
        lines = ["sequenceDiagram"]
        for p in participants:
            kind = "actor" if p.get("kind") == "actor" else "participant"
            lines.append(f"{kind} {p['alias']} as {_esc_label(p.get('label') or p['alias'])}")
        arrows = {"sync": "->>", "async": "-)", "return": "-->>"}
        for m in messages:
            if m.get("from_participant") in aliases and m.get("to_participant") in aliases:
                arrow = arrows.get(m.get("kind"), "->>")
                lines.append(
                    f"  {m['from_participant']} {arrow} {m['to_participant']}: "
                    f"{m.get('label') or ''}"
                )
        return "\n".join(lines) + "\n"

    if graph_type == "state":
        states = graph.get("states") or []
        transitions = graph.get("transitions") or []
        if not states or not transitions:
            return None
        state_ids = {s["state_id"] for s in states}
        lines = ["stateDiagram-v2"]
        for s in states:
            lines.append(f"  {s['state_id']} : {_esc_label(s.get('label') or s['state_id'])}")
            if s.get("kind") == "initial":
                lines.append(f"  [*] --> {s['state_id']}")
            elif s.get("kind") == "final":
                lines.append(f"  {s['state_id']} --> [*]")
        for t in transitions:
            if t.get("from_state") in state_ids and t.get("to_state") in state_ids:
                event = f": {t['event']}" if t.get("event") else ""
                lines.append(f"  {t['from_state']} --> {t['to_state']}{event}")
        return "\n".join(lines) + "\n"

    if graph_type == "journey":
        tasks = graph.get("tasks") or []
        if not tasks:
            return None
        sections = graph.get("sections") or []
        section_label = {s["section_id"]: s.get("label") or s["section_id"] for s in sections}

        def _task_line(t: dict[str, Any]) -> str:
            actors = ", ".join(str(a) for a in (t.get("actors") or ["用户"]))
            return f"{_esc_label(t.get('label') or t['task_id'])}: {t.get('score', 3)}: {_esc_label(actors)}"

        lines = ["journey"]
        # journey 语法一旦进入 section 就无法回退到无分组区：
        # 先输出未归组（或归到了未知分组）的任务，再按注册顺序展开各分组
        known = set(section_label)
        for t in tasks:
            if t.get("section_id") not in known:
                lines.append(f"  {_task_line(t)}")
        for s in sections:
            lines.append(f"  section {_esc_label(section_label[s['section_id']])}")
            for t in tasks:
                if t.get("section_id") == s["section_id"]:
                    lines.append(f"  {_task_line(t)}")
        return "\n".join(lines) + "\n"

    return None
