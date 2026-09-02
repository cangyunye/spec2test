"""Mock Provider：离线、无外部依赖，用于阶段二/三联调和单元测试。

所有实现只做「最短路径返回」——构造符合 TypedDict 结构的合法结果。
真实后端行为由具体实现承担。
"""
from __future__ import annotations

import uuid
from typing import Any

from .base import (
    CodeChange,
    CodeEditProvider,
    CodeEditResult,
    CodeGraphRenderProvider,
    CodeSearchHit,
    CodeSearchProvider,
    CodeSearchResult,
    LintIssue,
    QueryType,
    RenderOutput,
    TestCase,
    TestGenProvider,
    TestReport,
    TestRun,
)


def _new_sess(prefix: str, old: str | None) -> str:
    return old or f"{prefix}-{uuid.uuid4().hex[:8]}"


class MockCodeSearch(CodeSearchProvider):
    name = "mock_code_search"

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
        results: list[CodeSearchHit] = []
        syms = target_symbols or [f"symbol_from_{i}" for i in range(2)]
        for i, sym in enumerate(syms[: max(1, max_results)]):
            results.append(
                {
                    "file_path": f"src/mock/{sym.split('.')[-1]}.py",
                    "symbol_name": sym,
                    "line_start": 10 + i * 10,
                    "line_end": 20 + i * 10,
                    "code_snippet": f"def {sym.split('.')[-1]}(self, x):\n    return x + {i}",
                    "relevance_score": round(1.0 - i * 0.1, 2),
                    "callers": [f"src/mock/caller_{i}.py:call_{i}"],
                    "callees": [f"src/mock/callee_{i}.py:helper_{i}"],
                }
            )
        return {
            "session_id": _new_sess("search", session_id),
            "total": len(results),
            "results": results,
            "raw": {
                "query": query_text,
                "query_type": query_type,
                "scope_files": scope_files or [],
                "project_root": project_root,
            },
        }


class MockCodeGraphRender(CodeGraphRenderProvider):
    name = "mock_graph_render"

    async def render(
        self,
        logic_graph: dict[str, Any],
        *,
        preferred_format: str = "mermaid",
    ) -> RenderOutput:
        mmd = logic_graph.get("mermaid_source") or "graph TD\n  A[Mock] --> B[Done]"
        if preferred_format == "html":
            return {
                "format": "html",
                "html_bytes": (
                    f"<!DOCTYPE html><html><body><pre>{mmd}</pre></body></html>"
                ).encode("utf-8"),
                "svg_bytes": None,
                "png_bytes": None,
                "mermaid_text": mmd,
                "render_backend": self.name,
            }
        return {
            "format": "mermaid",
            "html_bytes": None,
            "svg_bytes": None,
            "png_bytes": None,
            "mermaid_text": mmd,
            "render_backend": self.name,
        }


class MockCodeEdit(CodeEditProvider):
    name = "mock_code_edit"

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
        files = [rf.get("file_path", "src/mock/generated.py") for rf in (related_files or [])]
        if not files:
            files = ["src/mock/generated.py"]
        changes: list[CodeChange] = [
            {
                "file_path": fp,
                "action": "update" if fp.endswith(".py") and "/" in fp else "create",
                "diff_unified": f"--- a/{fp}\n+++ b/{fp}\n@@ -1,1 +1,2 @@\n # mock\n+# {instruction[:40]}",
                "content_after": f"# mock\n# {instruction[:80]}\n",
            }
            for fp in files
        ]
        lint: list[LintIssue] = []
        if run_lint and acceptance and any("lint" in a.lower() for a in acceptance):
            lint.append(
                {
                    "file_path": files[0],
                    "line": 2,
                    "level": "warning",
                    "message": "mock-lint: long comment",
                    "linter": "mock",
                }
            )
        return {
            "session_id": _new_sess("code", session_id),
            "changes": changes,
            "lint": lint,
            "lint_passed": not lint,
            "_meta": {  # type: ignore[typeddict-unknown-key]
                "logic_graph_node_id": logic_graph_node_id,
                "acceptance": acceptance or [],
            },
        }  # type: ignore[typeddict-unknown-key]


class MockTestGen(TestGenProvider):
    name = "mock_test_gen"

    async def generate(
        self,
        project_root: str,
        target_symbols: list[str],
        *,
        coverage_target: int = 80,
        modified_branches_only: bool = True,
        logic_graph: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> TestReport:
        edge_ids: list[str] = []
        if logic_graph:
            edge_ids = [e["edge_id"] for e in logic_graph.get("edges", [])]
        cases: list[TestCase] = []
        for i, sym in enumerate(target_symbols):
            safe = sym.replace(".", "_")
            cases.append(
                {
                    "test_file": f"tests/test_{safe}.py",
                    "test_symbol": f"test_{safe}_ok",
                    "line_start": 10 + i,
                    "line_end": 18 + i,
                    "code_snippet": (
                        f"def test_{safe}_ok():\n"
                        f"    assert {sym.split('.')[-1]}({i}) == {i + 1}\n"
                    ),
                    "covered_edges": edge_ids[i : i + 1] if edge_ids else [],
                }
            )
        total = len(cases) or 1
        passed = max(total - 1, 0)
        failed = 1 if total and passed == 0 else 0
        run: TestRun = {
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "coverage_pct": float(coverage_target),
            "logs": f"mock pytest finished: {passed} passed, {failed} failed",
        }
        return {
            "session_id": _new_sess("test", session_id),
            "test_cases": cases,
            "run": run,
            "target_symbols": target_symbols,
        }
