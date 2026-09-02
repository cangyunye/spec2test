"""P2 CodeProvider 后端自检：探测 codegraph / archify / opencode 可用性。

用法（CLI）：
    devflow check-providers [--project-root <path>]

对外：
  - probe_codegraph(project_root)  → dict
  - probe_archify()                → dict
  - probe_opencode()               → dict
  - check_providers_all(project_root) → list[dict]（统一 {name, ok, detail, ...}）

统一报告字段：
  name / ok / detail；各 probe 可附带专有字段（bin_ok / index_ok / node_ok ...）
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..config import settings


def probe_codegraph(project_root: str) -> dict[str, Any]:
    """探测 codegraph 二进制 + .codegraph/ 索引。"""
    bin_path = shutil.which("codegraph")
    if bin_path is None:
        return {
            "name": "codegraph",
            "ok": False,
            "detail": "codegraph 二进制未找到，请执行官方安装脚本",
            "bin_ok": False,
            "index_ok": False,
        }
    index_ok = (Path(project_root) / ".codegraph").exists()
    return {
        "name": "codegraph",
        "ok": index_ok,
        "detail": (
            f"二进制: {bin_path}; 索引: "
            + ("✓ 已 init" if index_ok else "✗ 缺少 .codegraph/（请先 codegraph init）")
        ),
        "bin_ok": True,
        "index_ok": index_ok,
    }


def probe_archify() -> dict[str, Any]:
    """探测 node + npx（Archify 依赖）。"""
    node_bin = shutil.which("node")
    npx_bin = shutil.which("npx")
    ok = node_bin is not None and npx_bin is not None
    return {
        "name": "archify",
        "ok": ok,
        "detail": (
            f"node: {node_bin or '✗ 未找到'}; npx: {npx_bin or '✗ 未找到'}"
            + ("；可渲染 HTML/SVG/PNG" if ok else "；缺少依赖将自动回退 Mermaid")
        ),
        "node_ok": node_bin is not None,
        "npx_ok": npx_bin is not None,
    }


async def probe_opencode() -> dict[str, Any]:
    """探测 OpenCode HTTP 服务（只做轻量连通性检查，不抛异常）。"""
    base_url = (settings.OPENCODE_BASE_URL or "").rstrip("/")
    if not base_url:
        return {
            "name": "opencode",
            "ok": False,
            "detail": "OPENCODE_BASE_URL 未配置（将走 Mock）",
        }
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(base_url)
        ok = resp.status_code < 500
        return {
            "name": "opencode",
            "ok": ok,
            "detail": f"{base_url} → HTTP {resp.status_code}"
            + ("（已响应）" if ok else "（服务异常，将走 Mock）"),
        }
    except ImportError:
        return {
            "name": "opencode",
            "ok": False,
            "detail": "httpx 未安装，无法探测 OpenCode（将走 Mock）",
        }
    except Exception as e:  # noqa: BLE001 - 自检需吞掉全部连接错误
        return {
            "name": "opencode",
            "ok": False,
            "detail": f"{base_url} 连接失败: {type(e).__name__}: {str(e)[:120]}（将走 Mock）",
        }


def check_providers_all(project_root: str = ".") -> list[dict[str, Any]]:
    """汇总三个后端的探测报告（opencode 为 async，做同步包装）。"""
    import asyncio

    reports = [
        probe_codegraph(project_root),
        probe_archify(),
        asyncio.run(probe_opencode()),
    ]
    return reports
