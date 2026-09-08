"""OpenCode HTTP Provider：调用 SPEC 2.4 定义的 REST API。

覆盖四个能力里的三个（search / code generate / test generate）：
  CodeSearchProvider  ↔ POST /api/v1/code/search
  CodeEditProvider    ↔ POST /api/v1/code/generate
  TestGenProvider     ↔ POST /api/v1/tests/generate

所有对外方法都接入：
  1. 熔断器 opencode_http（连续失败快速失败，避免打爆供应商）
  2. DevFlowError.wrap_exception：把 httpx/aiohttp/urllib 异常统一成 SPEC 5 错误码
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..config import settings
from ..errors import (
    DevFlowError,
    HttpAuthError,
    HttpLintFailedError,
    HttpReqInvalidError,
    HttpSessionTimeoutError,
    HttpUpstreamError,
    LlmContextOverflowError,
    LlmRateLimitError,
    LlmRefusedError,
    wrap_exception,
)
from ..resilience import CircuitOpenError, default_breaker, retry_with_backoff
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


class OpenCodeHTTPError(RuntimeError):
    """OpenCode HTTP 请求失败，含状态码 + 响应体前 500 字符。

    注意：外部用 wrap_exception 统一转成 DevFlowError；保留该类仅用于向后兼容 tests 的断言。
    """


class OpenCodeUnavailableError(RuntimeError):
    """OPENCODE_BASE_URL 未配置或连不上。保留仅用于向后兼容。"""


_RETRY_AFTER_RE = re.compile(r"retry-after[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE)


def _http_status_to_error(status_code: int, text: str, context: str) -> DevFlowError:
    """按 SPEC 5 的 HTTP 状态码 → DevFlowError 映射。"""
    combined = f"{status_code} {text}"
    prefixed = f"{context}: HTTP {status_code}: {text[:200]}"
    if status_code in (401, 403):
        return HttpAuthError(prefixed)
    if status_code == 408:
        return HttpSessionTimeoutError(prefixed)
    if status_code == 422:
        return HttpLintFailedError(prefixed)
    if status_code == 429:
        if _hint_match(text, _REFUSED_HINTS):
            return LlmRefusedError(prefixed)
        return LlmRateLimitError(
            prefixed,
            retry_after_sec=_parse_retry_after(text),
        )
    if status_code == 400:
        if _hint_match(text, _CTX_OVERFLOW_HINTS):
            return LlmContextOverflowError(prefixed)
        if _hint_match(text, _REFUSED_HINTS):
            return LlmRefusedError(prefixed)
        if _hint_match(text, _RATE_LIMIT_HINTS):
            return LlmRateLimitError(prefixed)
        return HttpReqInvalidError(prefixed)
    if 500 <= status_code < 600:
        return HttpUpstreamError(prefixed)
    return HttpUpstreamError(prefixed)  # 418 / 499 等异常状态码，按上游错处理


_CTX_OVERFLOW_HINTS = (
    "context length", "context_length", "max_tokens", "maximum context length",
    "prompt is too long", "token limit", "too many tokens", "context window",
    "context exceeded", "上下文长度", "超出上下文",
)
_REFUSED_HINTS = (
    "refused", "refuse", "policy violation", "content policy", "safety policy",
    "moderation", "blocked", "rejected", "拒绝服务", "内容审核", "quota exceeded",
    "out of quota", "credit exhausted",
)
_RATE_LIMIT_HINTS = (
    "rate limit", "rate_limit", "too many requests", "throttl", "429",
    "限流", "频率限制",
)


def _hint_match(text: str, hints: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(h.lower() in low for h in hints)


def _parse_retry_after(text: str) -> float | None:
    m = _RETRY_AFTER_RE.search(text or "")
    if m:
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


@retry_with_backoff(
    on_error_wrap=True,
    wrap_context="opencode_http",
)
async def _post(
    base_url: str,
    token: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout_sec: int = 120,
) -> dict[str, Any]:
    """最小实现的 async HTTP POST；优先使用 httpx（如果已装），其次 aiohttp，
    再次退回到 urllib（同步 + asyncio.to_thread），这样 tests 不需要安装
    任何新依赖也能跑。

    所有抛出的错误都会被 @retry_with_backoff(wrap_context=...) 包成 DevFlowError。
    """
    breaker = default_breaker("opencode_http")
    async with breaker.guard():  # 熔断器：Open 状态直接抛 CircuitOpenError（SPEC 5.2.4）
        url = base_url.rstrip("/") + path
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        try:  # httpx 优先（requirements.txt 没写，tests 可以 mock 掉）
            import httpx  # type: ignore

            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.post(url, content=data, headers=headers)
                if resp.status_code >= 400:
                    raise _http_status_to_error(
                        resp.status_code,
                        getattr(resp, "text", "") or "",
                        context=f"opencode:{path}",
                    )
                return resp.json()
        except DevFlowError:
            raise
        except ImportError:
            pass

        try:  # aiohttp 兜底
            import aiohttp  # type: ignore

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                ) as resp:
                    body = await resp.read()
                    text = body.decode("utf-8", errors="replace")
                    if resp.status >= 400:
                        raise _http_status_to_error(
                            resp.status, text, context=f"opencode:{path}"
                        )
                    return json.loads(text)
        except DevFlowError:
            raise
        except ImportError:
            pass

        # 最后：urllib 同步请求，包到 to_thread 里，允许 async 跑
        import asyncio
        import urllib.error
        import urllib.request

        def _sync() -> dict[str, Any]:
            req = urllib.request.Request(
                url, data=data, headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
                    body = resp.read()
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", errors="replace")
                raise _http_status_to_error(
                    e.code, text, context=f"opencode:{path}"
                ) from e
            return json.loads(body.decode("utf-8", errors="replace"))

        return await asyncio.to_thread(_sync)
    # 注意：guard 上下文会直接抛错，这里实际 unreachable
    raise RuntimeError("unreachable")


# ═══════════════════════════════════════════════════════════════════
# 实现：CodeSearchProvider
# ═══════════════════════════════════════════════════════════════════
class OpenCodeSearchProvider(CodeSearchProvider):
    name = "opencode_search"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        request_id_prefix: str = "devflow",
    ) -> None:
        self.base_url = base_url or settings.OPENCODE_BASE_URL
        self.api_token = api_token if api_token is not None else settings.OPENCODE_API_TOKEN
        self.prefix = request_id_prefix

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
        if not self.base_url:
            # 供应商未启用 —— 不可重试；抛统一 DevFlowError，让 provider_nodes 可以走 fallback
            from ..errors import CliNotFoundError
            raise CliNotFoundError("opencode: OPENCODE_BASE_URL 未配置")
        body = {
            "request_id": f"{self.prefix}-{uuid.uuid4().hex}",
            "thread_id": "",  # 调用方不传也能跑；LangGraph 包装层会传
            "session_id": session_id,
            "project_root": project_root,
            "query": {
                "type": query_type,
                "text": query_text,
                "target_symbols": target_symbols or [],
                "scope_files": scope_files or [],
            },
            "max_results": max_results,
            "include_context": True,
        }
        # 让 tests 可以直接抓 last_request 断言
        self.last_request = body  # type: ignore[attr-defined]
        resp = await _post(self.base_url, self.api_token, "/api/v1/code/search", body)
        results: list[CodeSearchHit] = []
        for r in resp.get("results", []) or []:
            results.append(
                {
                    "file_path": r.get("file_path", ""),
                    "symbol_name": r.get("symbol_name"),
                    "line_start": int(r.get("line_start", 1)),
                    "line_end": int(r.get("line_end", 1)),
                    "code_snippet": r.get("code_snippet", ""),
                    "relevance_score": float(r.get("relevance_score", 0.0) or 0),
                    "callers": list(r.get("callers", []) or []),
                    "callees": list(r.get("callees", []) or []),
                }
            )
        return {
            "session_id": resp.get("session_id") or session_id,
            "total": resp.get("total", len(results)),
            "results": results,
            "raw": resp,
        }


# ═══════════════════════════════════════════════════════════════════
# 实现：CodeEditProvider
# ═══════════════════════════════════════════════════════════════════
class OpenCodeEditProvider(CodeEditProvider):
    name = "opencode_edit"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        request_id_prefix: str = "devflow",
    ) -> None:
        self.base_url = base_url or settings.OPENCODE_BASE_URL
        self.api_token = api_token if api_token is not None else settings.OPENCODE_API_TOKEN
        self.prefix = request_id_prefix

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
        if not self.base_url:
            # 供应商未启用 —— 不可重试；抛统一 DevFlowError，让 provider_nodes 可以走 fallback
            from ..errors import CliNotFoundError
            raise CliNotFoundError("opencode: OPENCODE_BASE_URL 未配置")
        body = {
            "request_id": f"{self.prefix}-{uuid.uuid4().hex}",
            "thread_id": "",
            "session_id": session_id,
            "project_root": project_root,
            "instruction": instruction,
            "context": {
                "logic_graph_node_id": logic_graph_node_id,
                "related_files": related_files or [],
                "acceptance_criteria": acceptance or [],
            },
            "run_lint": run_lint,
            "max_retry_fix": 1,
        }
        self.last_request = body  # type: ignore[attr-defined]
        resp = await _post(self.base_url, self.api_token, "/api/v1/code/generate", body)
        changes: list[CodeChange] = []
        for c in resp.get("changes", []) or []:
            changes.append(
                {
                    "file_path": c.get("file_path", ""),
                    "action": c.get("action", "update"),
                    "diff_unified": c.get("diff_unified", ""),
                    "content_after": c.get("content_after"),
                }
            )
        lint: list[LintIssue] = []
        for l in resp.get("lint", {}).get("issues", []) or []:
            lint.append(
                {
                    "file_path": l.get("file_path", ""),
                    "line": int(l.get("line", 0)),
                    "level": l.get("level", "info"),
                    "message": l.get("message", ""),
                    "linter": l.get("linter", ""),
                }
            )
        return {
            "session_id": resp.get("session_id") or session_id,
            "changes": changes,
            "lint": lint,
            "lint_passed": bool(resp.get("lint", {}).get("passed", True)),
        }


# ═══════════════════════════════════════════════════════════════════
# 实现：TestGenProvider
# ═══════════════════════════════════════════════════════════════════
class OpenCodeTestProvider(TestGenProvider):
    name = "opencode_test"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_token: str | None = None,
        request_id_prefix: str = "devflow",
    ) -> None:
        self.base_url = base_url or settings.OPENCODE_BASE_URL
        self.api_token = api_token if api_token is not None else settings.OPENCODE_API_TOKEN
        self.prefix = request_id_prefix

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
    ) -> TestReport:
        if not project_root:
            # 仅需求模式（未提供项目代码）：OpenCode 没有可操作的项目，委托 LLM
            # 基于需求 + 逻辑图设计端到端测试场景。
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
            )
        if not self.base_url:
            # 供应商未启用 —— 不可重试；抛统一 DevFlowError，让 provider_nodes 可以走 fallback
            from ..errors import CliNotFoundError
            raise CliNotFoundError("opencode: OPENCODE_BASE_URL 未配置")
        body = {
            "request_id": f"{self.prefix}-{uuid.uuid4().hex}",
            "thread_id": "",
            "session_id": session_id,
            "project_root": project_root,
            "target": {
                "files_or_symbols": target_symbols,
                "modified_branches_only": modified_branches_only,
                "logic_graph_ref": (logic_graph or {}).get("graph_id"),
            },
            "framework": "pytest",
            "coverage_target": coverage_target,
        }
        self.last_request = body  # type: ignore[attr-defined]
        resp = await _post(self.base_url, self.api_token, "/api/v1/tests/generate", body)
        cases: list[TestCase] = []
        for t in resp.get("test_cases", []) or []:
            cases.append(
                {
                    "test_file": t.get("test_file", ""),
                    "test_symbol": t.get("test_symbol", ""),
                    "line_start": int(t.get("line_start", 1)),
                    "line_end": int(t.get("line_end", 1)),
                    "code_snippet": t.get("code_snippet", ""),
                    "covered_edges": list(t.get("covered_edges", []) or []),
                }
            )
        run_raw = resp.get("run", {}) or {}
        run: TestRun = {
            "passed": int(run_raw.get("passed", 0)),
            "failed": int(run_raw.get("failed", 0)),
            "skipped": int(run_raw.get("skipped", 0)),
            "coverage_pct": (
                float(run_raw["coverage_pct"]) if run_raw.get("coverage_pct") is not None else None
            ),
            "logs": run_raw.get("logs", ""),
        }
        return {
            "session_id": resp.get("session_id") or session_id,
            "test_cases": cases,
            "run": run,
            "target_symbols": target_symbols,
        }
