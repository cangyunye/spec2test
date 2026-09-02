"""CodeGraph (colbymchenry) Provider：通过 CLI --json 子进程调用。

阶段二 MVP 优先方案：https://github.com/colbymchenry/codegraph

前置：用户机器已执行 `codegraph init` 产生 `.codegraph/` 索引目录。
支持的子命令（对应 CodeGraph README）：
  - codegraph search   --json <query>
  - codegraph node     --json <file:symbol>
  - codegraph callers  --json <symbol>
  - codegraph callees  --json <symbol>
  - codegraph impact   --json <symbol>
  - codegraph explore  --json <task>    （LLM 辅助，本地若没装 LLM 会失败，自动回退 search）
  - codegraph files    --json

字段别名（CodeGraph 不同版本字段名可能漂移，这里集中处理）：
  source/file_path/file   统一成 file_path
  symbol/symbol_name/name 统一成 symbol_name
  start/line/line_start   统一成 line_start
  score/relevance         统一成 relevance_score

错误治理（按 SPEC 5）：
  - `_run()` 接入 codegraph_cli 熔断器 + @retry_with_backoff；
  - 所有 CLI 异常统一 wrap 成 DevFlowError 的 CLI.* 子类（SPEC 5.2.2）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from ..errors import (
    CLI_INDEX_MISSING,
    CliExitError,
    CliIndexMissingError,
    CliNotFoundError,
    CliTimeoutError,
    DevFlowError,
    wrap_exception,
)
from ..resilience import default_breaker, retry_with_backoff
from .base import CodeSearchHit, CodeSearchProvider, CodeSearchResult, QueryType

logger = logging.getLogger(__name__)


class CodeGraphNotInstalledError(CliNotFoundError):
    """`codegraph` 二进制不在 PATH 中或 `codegraph init` 未执行。

    向后兼容（tests 已断言该类）；同时继承 CliNotFoundError，
    对 SPEC 5 统一错误体系也可识别。
    """

    def __init__(self, message: str, *args: Any, **kw: Any) -> None:
        super().__init__(message, *args, **kw)


# 允许 CodeGraph 每个输出字段有多组合法名字，依次尝试
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "file_path": ("file_path", "file", "source", "path"),
    "symbol_name": ("symbol_name", "symbol", "name", "identifier"),
    "line_start": ("line_start", "start_line", "start", "line"),
    "line_end": ("line_end", "end_line", "end"),
    "code_snippet": ("code_snippet", "snippet", "code", "content"),
    "relevance_score": ("relevance_score", "score", "relevance", "similarity"),
    "callers": ("callers", "called_by", "from_list"),
    "callees": ("callees", "calls", "to_list"),
}


def _pick(obj: dict[str, Any], key: str, default: Any) -> Any:
    for alias in _FIELD_ALIASES.get(key, (key,)):
        if alias in obj and obj[alias] is not None:
            return obj[alias]
    return default


def _to_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _normalize_hit(raw: dict[str, Any]) -> CodeSearchHit:
    """把 CodeGraph CLI 一条 JSON 记录压到统一 CodeSearchHit 结构。"""
    return {
        "file_path": str(_pick(raw, "file_path", "")),
        "symbol_name": (lambda s: str(s) if s else None)(_pick(raw, "symbol_name", None)),
        "line_start": _to_int(_pick(raw, "line_start", 1), 1),
        "line_end": _to_int(_pick(raw, "line_end", _pick(raw, "line_start", 1) or 1), 1),
        "code_snippet": str(_pick(raw, "code_snippet", "")),
        "relevance_score": float(_pick(raw, "relevance_score", 0.0) or 0.0),
        "callers": list(_pick(raw, "callers", []) or []),
        "callees": list(_pick(raw, "callees", []) or []),
    }


class CodeGraphProvider(CodeSearchProvider):
    """CodeGraph CLI 封装。仅实现 CodeSearchProvider。"""

    name = "codegraph"

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        timeout_sec: int = 30,
        fallback: CodeSearchProvider | None = None,
        require_index: bool | None = None,
    ) -> None:
        self.bin_path = bin_path or shutil.which("codegraph") or "codegraph"
        self.timeout_sec = timeout_sec
        self.fallback = fallback  # CodeGraph 不可用时，降级到其它实现
        # P1：.codegraph 索引缺失是否视为硬错误（CliIndexMissingError，可重试→fallback）
        #     默认取环境变量 CODEGRAPH_REQUIRE_INDEX（"1"/"true" 视为 true）
        if require_index is None:
            env = os.getenv("CODEGRAPH_REQUIRE_INDEX", "0").strip().lower()
            require_index = env in ("1", "true", "yes", "on")
        self.require_index = require_index

    # ── 对外 ─────────────────────────────────────────────
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
        hits: list[CodeSearchHit] = []
        raw_parts: list[dict[str, Any]] = []

        try:
            self._ensure_env(project_root)
            # 策略 1：明确符号就查 node + callers/callees/impact
            if query_type in {"symbol", "call_chain"} and target_symbols:
                for sym in target_symbols:
                    node = await self._run(project_root, "node", sym)
                    callers = await self._run(project_root, "callers", sym)
                    callees = await self._run(project_root, "callees", sym)
                    for item in self._as_list(node):
                        item = dict(item)
                        if callers:
                            item["callers"] = self._refs(callers)
                        if callees:
                            item["callees"] = self._refs(callees)
                        hits.append(_normalize_hit(item))
                        raw_parts.append({"node": node, "callers": callers, "callees": callees})
            else:
                # 策略 2：search 语义检索；失败就回退 explore（如可用）
                search_raw = await self._run(project_root, "search", query_text)
                items = self._as_list(search_raw)
                if not items:
                    try:
                        explore_raw = await self._run(project_root, "explore", query_text)
                        items = self._as_list(explore_raw)
                        raw_parts.append({"explore": explore_raw})
                    except CodeGraphNotInstalledError:
                        raise
                    except Exception:
                        items = []
                else:
                    raw_parts.append({"search": search_raw})
                # scope_files 过滤
                if scope_files:
                    items = [
                        it
                        for it in items
                        if any(self._in_scope(str(_pick(it, "file_path", "")), sf) for sf in scope_files)
                    ]
                hits = [_normalize_hit(it) for it in items[:max_results]]
        except DevFlowError as e:
            # CLI.NOT_FOUND 这类不可重试错误 → 切 fallback；
            # CLI.INDEX_MISSING：索引缺失重试无意义，也直接 fallback（降级搜索语义）；
            # 其余可重试错误让重试装饰器在 _run 里管。
            if self.fallback is not None and (
                e.retryable is False or e.code == CLI_INDEX_MISSING
            ):
                return await self.fallback.search(
                    project_root,
                    query_text,
                    query_type=query_type,
                    target_symbols=target_symbols,
                    scope_files=scope_files,
                    max_results=max_results,
                    session_id=session_id,
                )
            raise e
        except CodeGraphNotInstalledError as e:
            # 向后兼容（理论上现在不会抛到这里；_ensure_env 已改成 CliNotFoundError）
            if self.fallback is not None:
                return await self.fallback.search(
                    project_root,
                    query_text,
                    query_type=query_type,
                    target_symbols=target_symbols,
                    scope_files=scope_files,
                    max_results=max_results,
                    session_id=session_id,
                )
            raise e

        return {
            "session_id": session_id,
            "total": len(hits),
            "results": hits,
            "raw": {"parts": raw_parts, "project_root": project_root},
        }

    # ── 内部工具 ─────────────────────────────────────────
    def _ensure_env(self, project_root: str) -> None:
        if shutil.which(self.bin_path) is None and not Path(self.bin_path).exists():
            raise CodeGraphNotInstalledError(
                f"codegraph 二进制未找到: {self.bin_path!r}。请执行 "
                "`curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh`"
            )
        # P1 索引检查：.codegraph/ 缺失时按 require_index 决定硬错误 or 仅警告。
        #   require_index=True  → 抛 CliIndexMissingError（可重试，搜索语义会走 fallback）
        #   require_index=False → 仅 warning（第一次查会较慢，CLI 可能自动建索引）
        if not (Path(project_root) / ".codegraph").exists():
            msg = f"项目 {project_root!r} 缺少 .codegraph/ 索引，请先执行 `codegraph init`"
            if self.require_index:
                raise CliIndexMissingError(msg)
            logger.warning("%s（已按 CODEGRAPH_REQUIRE_INDEX=0 降级为警告）", msg)

    @retry_with_backoff(
        on_error_wrap=True,
        wrap_context="codegraph_cli",
    )
    async def _run(self, project_root: str, subcmd: str, arg: str) -> Any:
        """执行 `codegraph <subcmd> --json <arg>`，返回解析后的 JSON。

        接入熔断器 + 重试 + wrap_exception，保证所有失败都是 DevFlowError。
        """
        breaker = default_breaker("codegraph_cli")
        async with breaker.guard():
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.bin_path,
                    subcmd,
                    "--json",
                    arg,
                    cwd=project_root,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, PermissionError) as e:
                raise CliNotFoundError(
                    f"codegraph 启动失败: {e}",
                    cause=e,
                ) from e

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_sec
                )
            except asyncio.TimeoutError as e:
                proc.kill()
                raise CliTimeoutError(
                    f"codegraph {subcmd} 超时（>{self.timeout_sec}s）",
                    cause=e,
                ) from e
            except Exception as e:  # 其它 asyncio 错误
                proc.kill()
                raise wrap_exception(e, context=f"codegraph:{subcmd}") from e

            if proc.returncode != 0:
                err = stderr_b.decode("utf-8", errors="replace").strip()
                # .codegraph/ 不存在时 explore 往往失败；返回空 JSON，不影响主流程
                low = err.lower()
                if "no such file or directory" in low or ".codegraph" in low:
                    return []
                raise CliExitError(
                    f"codegraph {subcmd} 失败 (exit {proc.returncode}): {err[:400]}",
                    extra={"exit_code": proc.returncode, "stderr": err[:400]},
                )
            text = stdout_b.decode("utf-8", errors="replace").strip()
            if not text:
                return []
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # 某些版本 CodeGraph 可能在 JSON 前后夹杂日志，做兜底剥离
                start, end = text.find("{"), text.rfind("}")
                start_arr, end_arr = text.find("["), text.rfind("]")
                if start != -1 and end != -1 and (start_arr == -1 or start < start_arr):
                    try:
                        return json.loads(text[start : end + 1])
                    except json.JSONDecodeError as e:
                        raise CliExitError(
                            f"codegraph {subcmd} JSON 解码失败: {e}",
                            extra={"raw_head": text[:200]},
                        ) from e
                if start_arr != -1 and end_arr != -1:
                    try:
                        return json.loads(text[start_arr : end_arr + 1])
                    except json.JSONDecodeError as e:
                        raise CliExitError(
                            f"codegraph {subcmd} JSON 数组解码失败: {e}",
                            extra={"raw_head": text[:200]},
                        ) from e
                # 最终失败：返回空（不中断主流程；语义 search 偶尔坏输出）
                return []
        # unreachable
        raise RuntimeError("unreachable")

    @staticmethod
    def _as_list(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            for k in ("results", "items", "nodes", "data", "matches"):
                if k in payload and isinstance(payload[k], list):
                    return [p for p in payload[k] if isinstance(p, dict)]
            if all(isinstance(v, dict) for v in payload.values()):
                return list(payload.values())
            return [payload]
        return []

    @staticmethod
    def _refs(objs: Any) -> list[str]:
        out: list[str] = []
        for o in CodeGraphProvider._as_list(objs):
            fp = _pick(o, "file_path", "")
            sym = _pick(o, "symbol_name", None)
            if fp and sym:
                out.append(f"{fp}:{sym}")
            elif fp:
                out.append(str(fp))
            elif sym:
                out.append(str(sym))
        return out

    @staticmethod
    def _in_scope(file_path: str, pattern: str) -> bool:
        """简易匹配：dir/* 或 *.py 或完整 path 匹配。"""
        if not file_path or not pattern:
            return False
        import fnmatch

        p = Path(file_path)
        if fnmatch.fnmatch(str(p), pattern):
            return True
        if pattern.endswith("/*"):
            return str(p).startswith(pattern[:-1])
        return file_path == pattern
