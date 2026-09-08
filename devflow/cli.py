"""CLI 入口：交互式对话，演示全流程。

用法：
  devflow new                             # 新开会话（阶段一：澄清→制图）
  devflow new --full                      # 新开会话（全链路：澄清→制图→检索→生成→测试→验收）
  devflow new --from-doc requirements.docx  # 从 .docx/.txt/.md 文件读取初始需求
  devflow resume <thread_id>              # 从已有会话断点继续
  devflow list                            # 列出所有会话
  devflow export <thread_id>              # 导出结构化产物（需求 JSON + 逻辑图 JSON/Mermaid）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .config import settings
from .doc_reader import read_doc
from .events import events_from_stream
from .orchestrator import build_graph, build_graph_with_providers, initial_state

app = typer.Typer(help="AI-powered end-to-end dev workflow (Stage 1 MVP)")
console = Console()


# ═══════════════════════════════════════════════════════════════════
# 内部小工具
# ═══════════════════════════════════════════════════════════════════

def _config(*, thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _parse_assignment(kv: str) -> tuple[str, Any]:
    """解析 --set 'key=value'。

    value 优先尝试 JSON 字面量（数组 / 对象 / bool / 数字 / 双引号字符串），
    解析失败则按原始字符串处理。例如：
      --set req_type=bug_fix                     → ("req_type", "bug_fix")
      --set target_modules='["src/a.py"]'        → ("target_modules", ["src/a.py"])
      --set io_constraints='{"input":"x"}'       → ("io_constraints", {...})
      --set existing_code_accessible=true        → ("existing_code_accessible", True)
      --set target_modules=[]                    → ("target_modules", [])  # 清空
    """
    if "=" not in kv:
        raise ValueError(f"--set 格式应为 key=value，got: {kv!r}")
    key, _, raw = kv.partition("=")
    key = key.strip()
    if not key:
        raise ValueError(f"--set key 不能为空: {kv!r}")
    raw = raw.strip()
    try:
        return key, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return key, raw


def apply_assignments(requirement: dict[str, Any], assignments: list[str]) -> dict[str, Any]:
    """把 --set 列表应用到 requirement（深拷贝，不改原 dict）。

    支持点路径：io_constraints.input=xxx 只覆盖子字段；
    支持清空：target_modules=[]（JSON 空数组）。
    """
    import copy

    req = copy.deepcopy(requirement)
    for kv in assignments or []:
        key, val = _parse_assignment(kv)
        if key.startswith("io_constraints."):
            sub = key.split(".", 1)[1]
            req.setdefault("io_constraints", {})[sub] = val
        else:
            req[key] = val
    return req


def _banner(thread_id: str, stage: str) -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]Thread ID:[/] {thread_id}    [bold cyan]Stage:[/] {stage}\n"
            f"[dim]输入需求开始对话；输入 :quit 退出；:export 导出产物；:reset 清会话重开[/]",
            title="devflow · AI 全流程开发工具",
            border_style="cyan",
        )
    )


def _print_stage_report(state: dict[str, Any]) -> None:
    """每轮结束后打印当前进度摘要。"""
    stage = state.get("current_stage", "?")
    missing = state.get("missing_fields") or []
    err = state.get("last_error")

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold", width=14)
    table.add_column()
    table.add_row("Stage", f"[bold {'green' if stage=='done' else 'yellow'}]{stage}[/]")
    table.add_row("Missing", f"[red]{len(missing)}[/] 项" if missing else "[green]0 项 ✓[/]")
    if err:
        table.add_row("Last Error", f"[red]{err}[/]")

    req = state.get("requirement") or {}
    from .schemas import has_project_code

    filled = sum(
        1 for v in [
            req.get("project_root"),
            req.get("project_context"),
            req.get("target_modules") if req.get("target_modules") else None,
            req.get("edge_cases") if req.get("edge_cases") else None,
            req.get("acceptance_criteria") if req.get("acceptance_criteria") else None,
            (req.get("io_constraints") or {}).get("input"),
            (req.get("io_constraints") or {}).get("output"),
        ] if v
    )
    # 仅需求模式下 project_root / target_modules 不计入应填字段
    total = 7 if has_project_code(req) else 5
    mode_tag = "" if total == 7 else "（仅需求模式）"
    table.add_row("Requirement", f"{filled}/{total} 字段已填{mode_tag}")

    graph = state.get("logic_graph")
    if graph:
        table.add_row(
            "LogicGraph",
            f"graph_id={graph['graph_id']}  nodes={len(graph['nodes'])}  edges={len(graph['edges'])}",
        )

    apply_info = state.get("code_apply")
    if apply_info is not None:
        if apply_info.get("applied"):
            table.add_row(
                "CodeApply",
                f"[green]✓ {len(apply_info.get('files') or [])} 个文件已落盘[/]"
                f"（备份 {apply_info.get('backup_dir') or '-'}）",
            )
        else:
            table.add_row("CodeApply", f"[yellow]未落盘[/] {apply_info.get('reason') or ''}")

    report = state.get("test_report")
    if report:
        run = report.get("run") or {}
        if run.get("executed"):
            failures = int(run.get("failed", 0)) + int(run.get("errors", 0))
            color = "green" if failures == 0 else "red"
            table.add_row(
                "TestRun",
                f"[{color}]{'✓' if failures == 0 else '✗'} 真实执行[/] "
                f"passed={run.get('passed', 0)} failed={run.get('failed', 0)} "
                f"errors={run.get('errors', 0)} skipped={run.get('skipped', 0)} "
                f"（{run.get('duration_sec', 0)}s）",
            )
        else:
            n_cases = len(report.get("test_cases") or [])
            table.add_row(
                "TestRun",
                f"场景设计 {n_cases} 个（{run.get('skip_reason') or '未执行'}）",
            )

    console.print(Panel(table, title="状态摘要", border_style="blue"))


def _export_artifacts(state: dict[str, Any], out_dir: Path) -> list[Path]:
    """导出结构化产物：需求 JSON、逻辑图 JSON/Mermaid、代码变更、测试报告。"""
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


# ═══════════════════════════════════════════════════════════════════
# CLI commands
# ═══════════════════════════════════════════════════════════════════

@app.command("check-llm")
def cmd_check_llm() -> None:
    """连通性自检：对配置的每个 LLM provider 发一条最小请求（约 50 token）。"""
    import asyncio

    from .llm_client import check_llm_all

    with console.status("[cyan]正在自检 LLM provider…[/]"):
        reports = asyncio.run(check_llm_all())

    table = Table(title="LLM Provider 连通性自检")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("状态")
    table.add_column("耗时(ms)")
    table.add_column("回复 / 错误")
    for r in reports:
        status = "[green]✓ 连通[/]" if r["ok"] else f"[red]✗ {r['error_code']}[/]"
        detail = (r["reply"] or r["error_message"] or "").strip().replace("\n", " ")
        table.add_row(r["name"], r["model"], status, str(r["elapsed_ms"]), detail[:60])
    console.print(table)
    failed = [r for r in reports if not r["ok"]]
    if failed:
        console.print(f"[yellow]![/] {len(failed)}/{len(reports)} 个 provider 不可用，"
                      f"可检查 LLM_PROVIDERS_JSON / LLM_API_KEY 配置")


@app.command("check-providers")
def cmd_check_providers(
    project_root: str = typer.Option(".", "--project-root", "-p", help="检查哪个项目的 CodeGraph 索引"),
) -> None:
    """后端自检：探测 codegraph / archify / opencode 可用性（不发起真实调用）。"""
    from .providers.check import check_providers_all

    reports = check_providers_all(project_root)

    table = Table(title=f"CodeProvider 后端自检（project_root={project_root}）")
    table.add_column("后端")
    table.add_column("状态")
    table.add_column("详情")
    for r in reports:
        status = "[green]✓ 可用[/]" if r["ok"] else "[yellow]✗ 降级[/]"
        table.add_row(r["name"], status, r.get("detail", ""))
    console.print(table)

    ok_n = sum(1 for r in reports if r["ok"])
    console.print(
        f"{ok_n}/{len(reports)} 个后端可用。"
        + ("未装的后端会自动回退 Mock/Mermaid，不影响流程。" if ok_n < len(reports) else "全链路可真实执行。")
    )


@app.command("new")
def cmd_new(
    thread_id: str = typer.Option(None, "--id", help="自定义 thread_id，不填则自动生成"),
    full: bool = typer.Option(False, "--full", help="全链路模式（澄清→制图→检索→生成→测试→验收）"),
    from_doc: str = typer.Option(None, "--from-doc", help="从 .docx/.txt/.md 文件读取初始需求"),
    set_fields: list[str] = typer.Option(
        None, "--set",
        help="手动强制赋值（可多次）：--set req_type=bug_fix --set target_modules='[\"src/a.py\"]'",
    ),
) -> None:
    """新开一个会话：输入需求 → 自动追问 → 生成逻辑图（--full 启用全链路）。"""
    tid = thread_id or f"t-{uuid.uuid4().hex[:8]}"
    graph = build_graph_with_providers() if full else build_graph()
    # 首次 invoke 初始化状态（必须传 initial_state 仅首次有效）
    state = initial_state()
    # --set 在进入 graph 前强制覆盖 requirement（评审稿 §9：优先级最高，不再追问这些字段）
    if set_fields:
        try:
            state["requirement"] = apply_assignments(state["requirement"], set_fields)
        except ValueError as e:
            console.print(f"[red]×[/] --set 参数解析失败: {e}")
            raise typer.Exit(code=1)
    next(graph.stream(state, _config(thread_id=tid)))  # 触发 initial checkpoint

    mode_label = "全链路" if full else "阶段一"
    console.print(f"[green]✓[/] 已创建新会话 thread_id=[bold]{tid}[/]（{mode_label}）")

    # 如果有 --from-doc，读取文档并自动喂入
    if from_doc:
        try:
            doc_text = read_doc(from_doc)
            console.print(f"[green]✓[/] 已读取需求文档: [bold]{from_doc}[/] ({len(doc_text)} 字符)")
            _feed_input_and_stream(graph, tid, doc_text)
        except (FileNotFoundError, ValueError, ImportError) as e:
            console.print(f"[red]×[/] 读取文档失败: {e}")
            raise typer.Exit(code=1)

    _interactive_loop(tid, full=full)


@app.command("resume")
def cmd_resume(
    thread_id: str,
    full: bool = typer.Option(False, "--full", help="全链路模式恢复"),
) -> None:
    """从已有会话断点继续。"""
    # 检查是否存在
    if not _thread_exists(thread_id):
        console.print(f"[red]×[/] 找不到 thread_id=[bold]{thread_id}[/]，请先 devflow new 或 list 查看")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/] 恢复会话 thread_id=[bold]{thread_id}[/]（从 Checkpoint 断点继续）")
    _interactive_loop(thread_id, full=full)


@app.command("list")
def cmd_list() -> None:
    """列出所有已持久化的会话（thread_id + 最近阶段）。"""
    rows = _list_threads()
    if not rows:
        console.print("[yellow]—[/] 还没有历史会话，使用 [bold]devflow new[/] 创建")
        return

    table = Table(title="历史会话（Checkpoint SQLite）")
    table.add_column("Thread ID")
    table.add_column("Current Stage")
    table.add_column("Project Root")
    table.add_column("LogicGraph?")
    for r in rows:
        table.add_row(
            r["thread_id"],
            r.get("stage", "—"),
            r.get("project_root", "—"),
            "✓" if r.get("has_graph") else "—",
        )
    console.print(table)


@app.command("export")
def cmd_export(
    thread_id: str,
    out: Path = typer.Option(Path("./artifacts"), "--out", "-o", help="导出目录"),
) -> None:
    """导出需求 JSON、逻辑图 JSON、逻辑图 Mermaid 到目录。"""
    if not _thread_exists(thread_id):
        console.print(f"[red]×[/] 找不到 thread_id=[bold]{thread_id}[/]")
        raise typer.Exit(code=1)
    graph = build_graph()
    state = graph.get_state(_config(thread_id=thread_id)).values or {}
    files = _export_artifacts(state, out / thread_id)
    if not files:
        console.print("[yellow]—[/] 此会话暂无可导出的结构化产物")
        return
    for f in files:
        console.print(f"[green]✓[/] 导出 → [bold]{f}[/]")


# ═══════════════════════════════════════════════════════════════════
# 交互主循环
# ═══════════════════════════════════════════════════════════════════

def _interactive_loop(tid: str, *, full: bool = False) -> None:
    from langchain_core.messages import HumanMessage

    graph = build_graph_with_providers() if full else build_graph()
    first_round = True

    while True:
        # ── 先打印当前阶段报告 ─────────────────────────
        try:
            snap = graph.get_state(_config(thread_id=tid)).values or {}
        except Exception as e:
            snap = {}
            console.print(f"[yellow]![/] 读取 Checkpoint 失败: {e}")

        stage = snap.get("current_stage", "clarify")
        if first_round:
            _banner(tid, stage)
            first_round = False

        _print_stage_report(snap)

        # 已到 done + 有逻辑图 → 给用户展示 Mermaid 源码并提供导出
        if stage == "done" and snap.get("logic_graph"):
            console.print(
                Panel(
                    Syntax(snap["logic_graph"]["mermaid_source"], "mermaid", theme="monokai", line_numbers=True),
                    title="Mermaid 逻辑图（可复制到任何支持 Mermaid 的渲染器查看）",
                    border_style="green",
                )
            )

        # ── 检查是否在 review / graph_review 节点等待人工中断 ───────────
        try:
            state_obj = graph.get_state(_config(thread_id=tid))
            next_nodes = state_obj.next or []
        except Exception:
            next_nodes = []

        if "review" in next_nodes or "graph_review" in next_nodes:
            # 人工门禁中断：graph_review=确认图↔需求对齐；review=终审验收
            from .schemas import has_project_code

            no_code = not has_project_code(snap.get("requirement"))
            if "graph_review" in next_nodes:
                approve_hint = (
                    "直接进入端到端测试用例设计（未提供项目代码）" if no_code
                    else "进入代码检索"
                )
                gate_panel = Panel(
                    "[bold]请确认制图与需求对齐：[/]\n"
                    f"  输入 [green]approve[/] 制图通过，{approve_hint}\n"
                    "  输入 [red]reject[/] 回退重新制图",
                    title="制图评审",
                    border_style="cyan",
                )
                prompt_label = "制图确认"
            else:
                reject_hint = (
                    "回退到测试用例设计重新出用例" if no_code
                    else "回退到代码生成阶段"
                )
                gate_panel = Panel(
                    "[bold]请验收以下产物：[/]\n"
                    "  输入 [green]approve[/] 接受变更并结束流程\n"
                    f"  输入 [red]reject[/] 拒绝并{reject_hint}",
                    title="人工验收",
                    border_style="yellow",
                )
                prompt_label = "验收决定"
            console.print(gate_panel)
            _print_stage_report(snap)
            try:
                decision = console.input(f"[bold yellow]{prompt_label}[/] (approve/reject) > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]已退出（下次可用 devflow resume 继续）[/]")
                return
            if decision not in ("approve", "reject"):
                console.print("[yellow]![/] 请输入 approve 或 reject")
                continue
            gate = "graph_review" if "graph_review" in next_nodes else "review"
            _resume_from_interrupt(graph, tid, decision, gate=gate)
            continue

        # ── 读用户输入 ─────────────────────────────────
        try:
            user_input = console.input(f"\n[bold cyan]你[/] ({stage}) > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]已退出（下次可用 devflow resume {tid} 继续）[/]")
            return

        if not user_input:
            continue

        if user_input.lower() in (":q", ":quit", ":exit"):
            console.print(f"[dim]已退出（下次可用 devflow resume {tid} 继续）[/]")
            return

        if user_input.lower() in (":e", ":export"):
            out_dir = Path("./artifacts") / tid
            files = _export_artifacts(snap, out_dir)
            if not files:
                console.print("[yellow]—[/] 暂无可导出产物")
            else:
                for f in files:
                    console.print(f"[green]✓[/] → {f}")
            continue

        if user_input.lower() in (":reset",):
            # 不删除 checkpoint，但重置内部字段到初始（通过 invoke 覆盖）
            graph.update_state(_config(thread_id=tid), initial_state())
            console.print("[yellow]![/] 已重置当前会话内容（thread_id 不变）")
            continue

        if user_input.lower() in (":state",):
            # 调试：打印完整 state（脱敏 messages 内容只显示数量）
            debug = {**snap}
            if "messages" in debug:
                debug["messages"] = [
                    {"type": getattr(m, "type", str(type(m))), "len": len(str(getattr(m, "content", "")))}
                    for m in debug["messages"]
                ]
            console.print(JSON(json.dumps(debug, ensure_ascii=False, default=str, indent=2)))
            continue

        # ── 把输入喂给 Graph，stream 到下一个暂停点 ─────
        _feed_input_and_stream(graph, tid, user_input)


def _feed_input_and_stream(graph, tid: str, user_input: str) -> None:
    """把用户输入喂给 Graph，stream 到下一个暂停点并打印输出。

    与 Web（SSE）共用 events_from_stream 事件协议：这里消费事件打印到终端。
    """
    from langchain_core.messages import HumanMessage

    input_msg = {"messages": [HumanMessage(content=user_input)]}
    try:
        with console.status("[cyan]AI 处理中…[/]"):
            for event in events_from_stream(
                graph.stream(input_msg, _config(thread_id=tid), stream_mode="updates")
            ):
                _render_event(event)
    except Exception as e:
        console.print(f"[red]×[/] 运行出错: {e}")
        import traceback
        traceback.print_exc()


def _render_event(event: dict) -> None:
    """把一个事件渲染为终端输出（gate 事件由外层循环弹面板，这里静默跳过）。"""
    etype = event.get("type")
    if etype == "gate":
        return
    if etype == "messages":
        for m in event.get("messages", []):
            console.print(
                Panel(m.get("content", ""), title="[bold]AI[/]", border_style="magenta")
            )
    elif etype == "question":
        fields = event.get("missing") or event.get("questions")
        if fields:
            console.print("[yellow]?[/] 需要补充: " + "; ".join(fields))
    elif etype == "error":
        console.print(f"[red]×[/] 节点错误: {event.get('error')}")
    elif etype == "artifact":
        kind = event.get("kind")
        payload = event.get("payload") or {}
        if kind == "logic_graph":
            console.print(
                f"[green]✓[/] 已生成逻辑图 graph_id={payload.get('graph_id')} "
                f"(nodes={len(payload.get('nodes', []))}, edges={len(payload.get('edges', []))})"
            )
        elif kind == "code_context":
            console.print(f"[cyan]ℹ[/] 代码检索到 {len(payload)} 条结果")
        elif kind == "code_changes":
            console.print(f"[cyan]ℹ[/] 代码变更 {len(payload)} 个文件")
        elif kind == "test_report":
            run = payload.get("run") or {}
            console.print(
                f"[cyan]ℹ[/] 测试: passed={run.get('passed', 0)} "
                f"failed={run.get('failed', 0)} coverage={run.get('coverage_pct', 0)}%"
            )


def _resume_from_interrupt(graph, tid: str, decision: str, *, gate: str = "review") -> None:
    """从门禁中断恢复：传 approve/reject 给 graph。

    gate 区分是制图门（graph_review）还是终审（review），用于把恢复后的首个
    stage 事件翻译成模式感知的提示（代码模式 / 仅需求模式走不同分支）。
    首个 stage 事件 = 门节点返回值（search/graph/done/code/test），其余 stage 是
    下游节点推进标记，忽略；artifact/question/error 照常渲染。
    """
    from langgraph.types import Command

    try:
        with console.status(f"[cyan]恢复流程（{decision}）…[/]"):
            gate_seen = False
            for event in events_from_stream(
                graph.stream(Command(resume=decision), _config(thread_id=tid), stream_mode="updates")
            ):
                if event.get("type") == "stage":
                    if gate_seen:
                        continue
                    gate_seen = True
                    stage = event.get("stage")
                    if stage == "done":
                        console.print("[green]✓[/] 验收通过，流程完成！")
                    elif stage == "code":
                        console.print("[yellow]![/] 验收被拒绝，回退到代码生成阶段")
                    elif stage == "test" and gate == "review":
                        console.print("[yellow]![/] 验收被拒绝，回退到测试用例设计")
                    elif stage == "test" and gate == "graph_review":
                        console.print("[green]✓[/] 制图已确认，进入端到端测试用例设计（仅需求模式）")
                    elif stage == "search":
                        console.print("[green]✓[/] 制图已确认，进入代码检索阶段")
                    elif stage == "graph":
                        console.print("[yellow]![/] 制图被驳回，重新制图")
                else:
                    _render_event(event)
    except Exception as e:
        console.print(f"[red]×[/] 恢复出错: {e}")
        import traceback
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════
# SQLite 辅助：直接查 checkpoint 表做 list / exists
# ═══════════════════════════════════════════════════════════════════

def _connect_checkpoint():
    db = settings.CHECKPOINT_SQLITE_PATH
    if not db.exists():
        return None
    return sqlite3.connect(str(db))


def _thread_exists(tid: str) -> bool:
    conn = _connect_checkpoint()
    if not conn:
        return False
    try:
        cur = conn.execute(
            "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (tid,)
        )
        return cur.fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _list_threads() -> list[dict[str, Any]]:
    """列出所有会话及关键字段。

    langgraph 1.x checkpoint 列/序列化（msgpack）与旧 pickle 格式不同，
    不再直接解 blob，改用 graph.get_state 逐会话读取（依赖官方 API，稳）。
    """
    conn = _connect_checkpoint()
    if not conn:
        return []
    try:
        try:
            threads = [r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()]
        except sqlite3.OperationalError as e:
            console.print(f"[yellow]![/] checkpoints 表结构不对: {e}")
            return []
    finally:
        conn.close()

    graph = build_graph_with_providers()
    result: list[dict[str, Any]] = []
    for tid in threads:
        entry = {"thread_id": tid, "stage": "?", "project_root": "", "has_graph": False}
        try:
            vals = graph.get_state(_config(thread_id=tid)).values or {}
            req = vals.get("requirement") or {}
            entry["stage"] = vals.get("current_stage", "?")
            entry["project_root"] = req.get("project_root", "") or ""
            entry["has_graph"] = bool(vals.get("logic_graph"))
        except Exception:
            pass
        result.append(entry)
    return result


if __name__ == "__main__":
    app()
