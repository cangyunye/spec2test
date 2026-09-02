"""Archify (tt-a1i) Provider：把内部 LogicGraph 映射成 Archify JSON IR，再走
`npx skills use tt-a1i/archify` 渲染成 HTML/SVG/PNG。

MVP 默认行为：
  - 优先尝试 Node CLI 渲染；
  - 如果 Node / skills CLI / archify skill 任一项缺失，自动 fallback 到 Mermaid 文本。
    这保证纯 Python 机器（测试、CI）上 Provider 依然可用。

字段映射（见 SPEC 2.3.3 表格）：
  LogicNode.node_type                     ↔ nodes[].role
  LogicNode.code_ref.file_path + :symbol  ↔ nodes[].source.ref
  LogicEdge.edge_type=condition           ↔ edges[].label
  LogicEdge.is_modified=true              ↔ edges[].diff = added/modified

错误治理（SPEC 5）：
  - `_call_archify_cli` 接入 archify_cli 熔断器 + 重试；
  - CLI 错误统一 wrap 成 DevFlowError CLI.*；
  - 不可重试（CLI.NOT_FOUND）→ 直接 fallback mermaid。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..errors import (
    CliExitError,
    CliNotFoundError,
    CliTimeoutError,
    DevFlowError,
    wrap_exception,
)
from ..resilience import default_breaker, retry_with_backoff
from .base import CodeGraphRenderProvider, RenderOutput


class ArchifyRenderError(RuntimeError):
    """渲染失败；保留仅用于向后兼容，对外统一成 DevFlowError CLI.*"""


ROLE_MAP: dict[str, str] = {
    "io": "frontend",
    "external": "external",
    "module": "backend",
    "function": "backend",
    "condition": "security",  # 条件/校验节点在 Archify 配色里偏 security 蓝紫色
}
NODE_COLOR_DIFF: dict[str, str] = {"added": "#c8f7dc", "modified": "#fff2b2", "removed": "#ffd6d6"}



def _node_role(node: dict[str, Any]) -> str:
    return ROLE_MAP.get(node.get("node_type", ""), "backend")


def _diff_tag(obj: dict[str, Any]) -> str | None:
    if not obj.get("is_modified"):
        return None
    # 简单策略：新创建节点/边 id 形如 *-new-* 或 node_type=io 新建时认为 added
    oid = str(obj.get("node_id") or obj.get("edge_id") or "")
    return "added" if ("new" in oid or "add" in oid) else "modified"


def logic_graph_to_archify_ir(logic_graph: dict[str, Any]) -> dict[str, Any]:
    """把内部 LogicGraph（按 devflow/schemas.py LOGIC_GRAPH_SCHEMA）转 Archify JSON IR。

    返回值只保证结构可被 Archify CLI（或 mock）接受，不做 Archify 强 Schema 校验。
    """
    arch_nodes: list[dict[str, Any]] = []
    for node in logic_graph.get("nodes", []):
        nid = node["node_id"]
        cr = node.get("code_ref") or {}
        source_ref = None
        if cr.get("file_path"):
            sym = cr.get("symbol")
            source_ref = (
                f"{cr['file_path']}:{sym}" if sym else str(cr["file_path"])
            )
        payload: dict[str, Any] = {
            "id": nid,
            "label": node.get("label", nid),
            "role": _node_role(node),
        }
        if source_ref:
            payload["source"] = {"ref": source_ref}
        diff = _diff_tag(node)
        if diff:
            payload["diff"] = diff
            payload["background"] = NODE_COLOR_DIFF.get(diff)
        arch_nodes.append(payload)

    arch_edges: list[dict[str, Any]] = []
    for edge in logic_graph.get("edges", []):
        eid = edge["edge_id"]
        etype = edge.get("edge_type", "call")
        label = None
        if etype == "condition" and edge.get("condition"):
            label = str(edge["condition"])
        payload: dict[str, Any] = {
            "id": eid,
            "from": edge["from_node"],
            "to": edge["to_node"],
            "kind": etype,  # archify 常见 kind: call/data_flow/condition
        }
        if label:
            payload["label"] = label
        diff = _diff_tag(edge)
        if diff:
            payload["diff"] = diff
        arch_edges.append(payload)

    return {
        "version": "archify-1.0",
        "title": logic_graph.get("graph_id", "devflow-logic-graph"),
        "diagram": {
            "type": "architecture",
            "nodes": arch_nodes,
            "edges": arch_edges,
        },
    }


class ArchifyProvider(CodeGraphRenderProvider):
    name = "archify"

    def __init__(
        self,
        *,
        skills_bin: str | None = None,
        node_bin: str | None = None,
        force_mermaid_fallback: bool = False,
        timeout_sec: int = 60,
    ) -> None:
        self.skills_bin = skills_bin or shutil.which("npx") or "npx"
        self.node_bin = node_bin or shutil.which("node") or "node"
        self.force_mermaid_fallback = force_mermaid_fallback
        self.timeout_sec = timeout_sec

    # ── 对外 ─────────────────────────────────────────────
    async def render(
        self,
        logic_graph: dict[str, Any],
        *,
        preferred_format: str = "mermaid",
    ) -> RenderOutput:
        mmd = logic_graph.get("mermaid_source") or ""
        if self.force_mermaid_fallback or preferred_format == "mermaid":
            return {
                "format": "mermaid",
                "html_bytes": None,
                "svg_bytes": None,
                "png_bytes": None,
                "mermaid_text": mmd,
                "render_backend": self.name + "+mermaid",
            }

        ir = logic_graph_to_archify_ir(logic_graph)
        try:
            html_bytes = await self._call_archify_cli(ir)
        except (ArchifyRenderError, DevFlowError):
            # Node/skills 未装 / CLI 报错：自动 fallback 到 mermaid 保底
            return {
                "format": "mermaid",
                "html_bytes": None,
                "svg_bytes": None,
                "png_bytes": None,
                "mermaid_text": mmd,
                "render_backend": self.name + "+mermaid-fallback",
            }
        return {
            "format": "html",
            "html_bytes": html_bytes,
            "svg_bytes": None,
            "png_bytes": None,
            "mermaid_text": mmd,
            "render_backend": self.name,
        }

    # ── 内部：CLI 调用 ────────────────────────────────────
    @retry_with_backoff(
        on_error_wrap=True,
        wrap_context="archify_cli",
    )
    async def _call_archify_cli(self, ir: dict[str, Any]) -> bytes:
        breaker = default_breaker("archify_cli")
        async with breaker.guard():
            if shutil.which(self.node_bin) is None:
                raise CliNotFoundError(f"node 未找到: {self.node_bin}")
            with tempfile.TemporaryDirectory(prefix="archify_") as tmpdir:
                tmp = Path(tmpdir)
                ir_path = tmp / "archify-ir.json"
                ir_path.write_text(
                    json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                # 期望输出：tmpdir/archify-map.html （遵循 Archify README 习惯）
                out = tmp / "archify-map.html"
                cmd = [
                    self.skills_bin,
                    "skills",
                    "use",
                    "tt-a1i/archify@latest",
                    "--",
                    "--input",
                    str(ir_path),
                    "--output",
                    str(out),
                ]
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=tmpdir,
                    )
                except (FileNotFoundError, PermissionError) as e:
                    raise CliNotFoundError(
                        f"archify skills CLI 启动失败: {e}", cause=e
                    ) from e

                try:
                    _so, se = await asyncio.wait_for(
                        proc.communicate(), timeout=self.timeout_sec
                    )
                except asyncio.TimeoutError as e:
                    proc.kill()
                    raise CliTimeoutError(
                        f"archify CLI 超时（>{self.timeout_sec}s）", cause=e
                    ) from e
                except Exception as e:
                    proc.kill()
                    raise wrap_exception(e, context="archify_cli") from e

                stderr_text = se.decode("utf-8", errors="replace").strip()

                # Archify skill 用 `npx skills use` 很难保证稳定输出，
                # 只要目标 html 存在我们就当作成功（不管 exit code，因为 skills CLI 有时非 0）
                if out.exists():
                    return out.read_bytes()
                # 不存在 → 区分原因：如果 stderr 里提到 "command not found" / "not installed" → CLI.NOT_FOUND
                low = stderr_text.lower()
                if any(
                    kw in low
                    for kw in ("command not found", "not installed", "ENOENT", "could not find", "no such file")
                ):
                    raise CliNotFoundError(
                        "Archify skill 不可用: " + stderr_text[:400]
                    )
                if proc.returncode != 0:
                    raise CliExitError(
                        "Archify CLI 未产出 archify-map.html: " + stderr_text[:400],
                        extra={"exit_code": proc.returncode, "stderr": stderr_text[:400]},
                    )
                raise CliExitError(
                    "Archify 未产出 archify-map.html: " + stderr_text[:400],
                    extra={"stderr": stderr_text[:400]},
                )
        raise RuntimeError("unreachable")
