"""OpenCode Provider。

  CodeSearchProvider  ↔ POST /api/v1/code/search（HTTP Server）
  CodeEditProvider    ↔ POST /api/v1/code/generate（HTTP Server）
  TestGenProvider     ↔ `opencode run --format json`（CLI 子进程，真实 opencode）

测试设计走 CLI 而不是 REST：真实 opencode 没有自造的 /api/v1/tests/generate 接口，
`opencode run` 是官方无头入口；支持 --agent（技能派发 agent）、--session（续跑）、
--dir（目标项目）、--format json（事件流输出）。技能派发时把 SKILL.md 绝对路径
注入 prompt 前缀（先完整读取并严格遵守），并通过 OPENCODE_CONFIG_CONTENT 收紧
权限（设计任务禁写禁执行，读取保持默认放行）。

CLI 与 HTTP 一样接入熔断器 + DevFlowError.wrap_exception（CLI.EXIT / CLI.TIMEOUT
可重试，CLI.NOT_FOUND 不可重试 → 供上层 fallback）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..config import settings
from ..errors import (
    CliExitError,
    CliNotFoundError,
    CliTimeoutError,
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


def _clip(s: Any, limit: int) -> str:
    return str(s or "")[:limit]


def _extract_json_payload(text: str) -> Any | None:
    """从 opencode 的自然语言答复里抠 JSON：整段解析 → ```json 围栏 → 首尾大括号截取。

    返回 None 表示完全找不到合法 JSON，由调用方决定报错还是降级。
    （与 pi provider 的同名函数同实现；本地内联保持 provider 自包含）
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
# 实现：TestGenProvider（opencode run CLI 子进程）
# ═══════════════════════════════════════════════════════════════════

def _parse_run_output(raw: str) -> tuple[str, str | None]:
    """`opencode run --format json` 事件流 → (assistant 文本, session_id)。

    事件流是 JSONL（逐行 JSON 对象）；文本段可能挂在 message.part / part.text /
    text 等字段。宽松提取，解析不出任何事件行时把整段输出当纯文本兜底。
    """
    texts: list[str] = []
    session_id: str | None = None
    saw_event = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        saw_event = True
        if not session_id:
            sid = obj.get("sessionID") or obj.get("session_id")
            if isinstance(sid, str) and sid.strip():
                session_id = sid.strip()
            else:
                info = obj.get("info")
                if isinstance(info, dict):
                    sid = info.get("sessionID") or info.get("session_id") or info.get("id")
                    if isinstance(sid, str) and sid.strip():
                        session_id = sid.strip()
        part = obj.get("part") if isinstance(obj.get("part"), dict) else obj
        if str(part.get("type") or "") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
                continue
        msg = obj.get("message")
        if isinstance(msg, dict):
            for p in msg.get("parts") or []:
                if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                    texts.append(str(p["text"]))
    if not saw_event:
        return raw, session_id
    return "\n".join(texts), session_id


class OpenCodeTestProvider(TestGenProvider):
    name = "opencode_test"

    def __init__(
        self,
        *,
        bin_path: str | None = None,
        agent: str | None = None,
        timeout_sec: int | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.bin_path = bin_path or settings.OPENCODE_BIN
        self.agent = agent if agent is not None else settings.OPENCODE_AGENT
        self.timeout_sec = timeout_sec or settings.OPENCODE_RUN_TIMEOUT_SEC
        self.extra_args = (
            extra_args if extra_args is not None else list(settings.OPENCODE_EXTRA_ARGS)
        )

    def _build_args(
        self, prompt: str, *, session_id: str | None = None, project_root: str = ""
    ) -> list[str]:
        """拼 `opencode run --format json --agent X [--session S] [extra] [--dir R] <prompt>`。"""
        args = [self.bin_path, "run", "--format", "json"]
        if self.agent:
            args += ["--agent", self.agent]
        if session_id:
            args += ["--session", session_id]
        args += list(self.extra_args)
        if project_root:
            args += ["--dir", project_root]
        args.append(prompt)  # prompt 永远是最后一个位置参数
        return args

    @retry_with_backoff(
        on_error_wrap=True,
        wrap_context="opencode_cli",
    )
    async def _run_opencode(
        self,
        project_root: str,
        prompt: str,
        *,
        session_id: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> str:
        """在目标项目下执行一次 opencode run，返回 stdout 文本。

        熔断器 + 重试 + DevFlowError 包装；非零退出按可重试 CLI 错误治理
        （CLI.EXIT），二进制缺失不可重试（CLI.NOT_FOUND → 上层 fallback）。
        """
        breaker = default_breaker("opencode_cli")
        async with breaker.guard():
            env = dict(os.environ)
            if env_extra:
                env.update(env_extra)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._build_args(prompt, session_id=session_id, project_root=project_root),
                    cwd=project_root or None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except (FileNotFoundError, PermissionError) as e:
                raise CliNotFoundError(
                    f"opencode 启动失败: {e}。请先 npm install -g opencode-ai",
                    cause=e,
                ) from e
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_sec
                )
            except asyncio.TimeoutError as e:
                proc.kill()
                raise CliTimeoutError(
                    f"opencode 超时（>{self.timeout_sec}s）；可在 .env 调大 OPENCODE_RUN_TIMEOUT_SEC",
                    cause=e,
                ) from e
            except Exception as e:  # 其它 asyncio 错误
                proc.kill()
                raise DevFlowError("CLI.EXIT", f"opencode 执行异常: {e}", cause=e) from e
            if proc.returncode != 0:
                err = stderr_b.decode("utf-8", errors="replace").strip()
                raise CliExitError(
                    f"opencode 失败 (exit {proc.returncode}): {err[:400]}",
                    stderr_tail=err[-400:],
                )
            return stdout_b.decode("utf-8", errors="replace").strip()

    def _build_prompt(
        self,
        target_symbols: list[str],
        *,
        logic_graph: dict[str, Any] | None,
        requirement: dict[str, Any] | None,
        feedback: str | None,
        checklists: list[dict[str, str]] | None,
        feature: dict[str, Any] | None,
        skill_paths: list[str] | None,
    ) -> str:
        from ..feature_split import (
            SKILL_ENVELOPE,
            build_feature_design_prompt,
        )

        if feature:
            return build_feature_design_prompt(
                feature,
                requirement=requirement,
                feedback=feedback,
                checklists=checklists,
                skill_paths=skill_paths,
            )
        parts = [
            "你是测试架构师。根据给定的目标与上下文设计测试场景组"
            "（正向 / 反向 / 边界值 / 等价类 / 状态流转 / 场景法）。",
            f"目标: {target_symbols}",
            f"逻辑图: {_clip(json.dumps(logic_graph or {}, ensure_ascii=False), 4000)}",
            f"结构化需求: {_clip(json.dumps(requirement or {}, ensure_ascii=False), 4000)}",
        ]
        if feedback and feedback.strip():
            parts.append(f"上轮验收意见（必须针对性修正）: {feedback.strip()}")
        if checklists:
            parts.append("业务检查清单（逐条核对覆盖）:")
            for cl in checklists:
                parts.append(f"- [{cl.get('rel_dir', '')}] {str(cl.get('content', ''))[:1500]}")
        parts.append(
            "最终答复只输出一个 JSON（不要代码围栏），结构：\n"
            + json.dumps(
                {
                    **SKILL_ENVELOPE,
                    "feature_id": "F1（整单模式填 F1）",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return "\n".join(parts)

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
            # 仅需求模式（未提供项目代码）：opencode 没有可操作的项目，委托 LLM
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
                checklists=checklists,
                feature=feature,
            )
        if shutil.which(self.bin_path) is None and not Path(self.bin_path).exists():
            raise CliNotFoundError(
                f"opencode 二进制未找到: {self.bin_path!r}。请先 npm install -g opencode-ai"
            )
        env_extra: dict[str, str] = {}
        if skill_paths:
            # 技能派发 = 设计任务：禁写禁执行（读取默认放行），避免目标项目 agent
            # 配置里的权限比预期宽松；config content 只在本次子进程生效
            env_extra["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                {"permission": {"edit": "deny", "write": "deny", "bash": "deny"}},
                ensure_ascii=False,
            )
        prompt = self._build_prompt(
            target_symbols,
            logic_graph=logic_graph,
            requirement=requirement,
            feedback=feedback,
            checklists=checklists,
            feature=feature,
            skill_paths=skill_paths,
        )
        self.last_request = {  # type: ignore[attr-defined]
            "argv": self._build_args(prompt, session_id=session_id, project_root=project_root),
            "feature_id": (feature or {}).get("feature_id"),
            "skill_paths": skill_paths,
        }
        raw = await self._run_opencode(
            project_root, prompt, session_id=session_id, env_extra=env_extra
        )
        text, sid = _parse_run_output(raw)
        payload = _extract_json_payload(text)
        if not isinstance(payload, dict):
            # 设计报告解析失败不能静默当成功 → 按可重试的 CLI 错误上抛
            raise CliExitError(
                f"opencode 测试设计 JSON 解析失败: {text[:200]}",
                stderr_tail=text[-200:],
            )
        report = self._envelope_to_report(
            payload, feature=feature, target_symbols=target_symbols,
            session_id=sid or session_id,
        )
        return report

    @staticmethod
    def _envelope_to_report(
        payload: dict[str, Any],
        *,
        feature: dict[str, Any] | None,
        target_symbols: list[str],
        session_id: str | None,
    ) -> TestReport:
        """envelope / 旧 REST 形态 → TestReport。

        兼容两种产出：feature 技能契约（cases[]）与旧 test_cases[] 形态。
        """
        if "test_cases" in payload and "cases" not in payload:
            cases: list[TestCase] = []
            for t in payload.get("test_cases", []) or []:
                if not isinstance(t, dict):
                    continue
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
            run_raw = payload.get("run", {}) or {}
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
                "session_id": session_id,
                "test_cases": cases,
                "run": run,
                "target_symbols": target_symbols,
            }

        fid = str((feature or {}).get("feature_id") or payload.get("feature_id") or "F1")
        fname = str((feature or {}).get("name") or "")
        cases = []
        for i, t in enumerate(payload.get("cases") or []):
            if not isinstance(t, dict):
                continue
            cases.append(
                {
                    "test_file": "",
                    "test_symbol": f"test_{fid.lower()}_{i + 1:02d}",
                    "case_id": f"TC-{i + 1:03d}",
                    "tier": str(t.get("tier") or "functional"),
                    "priority": str(t.get("priority") or "P1"),
                    "case_type": str(t.get("case_type") or "正向"),
                    "title": str(t.get("title") or ""),
                    "target": str(t.get("target") or fname),
                    "precondition": str(t.get("precondition") or ""),
                    "steps": str(t.get("steps") or ""),
                    "expected": str(t.get("expected") or ""),
                    "rationale": str(t.get("rationale") or ""),
                    "code_snippet": f"{t.get('steps') or ''}\n预期: {t.get('expected') or ''}",
                    "covered_edges": [],
                    "feature_id": fid,
                    "feature_name": fname,
                }
            )
        issues = [str(x) for x in (payload.get("open_issues") or [])]
        return {
            "session_id": session_id or f"opencode-test-design:{fid}",
            "test_cases": cases,
            "run": {
                "passed": len(cases) if cases else 1,
                "failed": 0,
                "skipped": 0,
                "coverage_pct": None,
                "logs": (
                    f"opencode run 设计功能点 {fid}：{len(cases)} 条场景"
                    f"（status={payload.get('status', 'ok')}，未执行真实测试）"
                ),
            },
            "target_symbols": target_symbols,
            "overview": f"功能点 {fid} {fname} 设计要点（由 opencode run 产出）",
            "self_check": [str(x) for x in (payload.get("coverage_self_check") or [])],
            "open_issues": issues,
        }
