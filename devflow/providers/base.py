"""CodeProvider 抽象协议与返回值类型定义。

LangGraph 节点只依赖本模块暴露的抽象基类（ABC），具体后端（codegraph / archify /
opencode / mock）都通过工厂装配，节点无需感知。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal, TypedDict


# ═══════════════════════════════════════════════════════════════════
# 公共返回值 TypedDict（对应 SPEC 2.4 HTTP 契约结构，简化为 Python-native）
# ═══════════════════════════════════════════════════════════════════
class CodeRef(TypedDict, total=False):
    file_path: str
    symbol: str | None
    line_start: int
    line_end: int


class CodeSearchHit(TypedDict):
    file_path: str
    symbol_name: str | None
    line_start: int
    line_end: int
    code_snippet: str
    relevance_score: float
    callers: list[str]
    callees: list[str]


class CodeSearchResult(TypedDict):
    session_id: str | None
    total: int
    results: list[CodeSearchHit]
    raw: dict[str, Any] | None  # 留原后端输出，便于调试 & 字段别名 fallback


class RenderOutput(TypedDict, total=False):
    format: Literal["html", "svg", "png", "mermaid"]
    html_bytes: bytes | None
    svg_bytes: bytes | None
    png_bytes: bytes | None
    mermaid_text: str | None
    render_backend: str  # 实际跑的后端名（archify / mermaid / mock ...）


class CodeChange(TypedDict, total=False):
    file_path: str
    action: Literal["create", "update", "delete"]
    diff_unified: str  # unified diff 字符串
    content_after: str | None


class LintIssue(TypedDict):
    file_path: str
    line: int
    level: Literal["error", "warning", "info"]
    message: str
    linter: str


class CodeEditResult(TypedDict):
    session_id: str | None
    changes: list[CodeChange]
    lint: list[LintIssue]
    lint_passed: bool


class TestCase(TypedDict, total=False):
    test_file: str
    test_symbol: str  # 如 test_login_2fa_ok
    line_start: int
    line_end: int
    code_snippet: str
    covered_edges: list[str]  # 对应的 LogicGraph.edge_id 列表


class TestRun(TypedDict):
    passed: int
    failed: int
    skipped: int
    coverage_pct: float | None
    logs: str


class TestReport(TypedDict, total=False):
    session_id: str | None
    test_cases: list[TestCase]
    run: TestRun
    target_symbols: list[str]
    # 总-分结构（LLM 设计模式）
    overview: str | None
    self_check: list[str] | None
    # 注入的业务检查清单来源（checklist 库 rel_dir 列表，空 = 未加载）
    checklist_refs: list[str]


# ═══════════════════════════════════════════════════════════════════
# 抽象 Provider 基类
# ═══════════════════════════════════════════════════════════════════
class CodeProvider(ABC):
    """所有 Provider 的公共基类；name 用于日志和工厂拼注册表。"""

    name: ClassVar[str] = "base"


QueryType = Literal["semantic", "symbol", "call_chain"]


class CodeSearchProvider(CodeProvider, ABC):
    """阶段二：代码检索。对应该 SPEC 2.4.1 代码检索接口。"""

    name: ClassVar[str] = "code_search_base"

    @abstractmethod
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
        """执行一次代码检索，返回标准化 CodeSearchResult。"""


class CodeGraphRenderProvider(CodeProvider, ABC):
    """阶段二/三：把 logic_graph JSON 渲染为可交付产物。"""

    name: ClassVar[str] = "graph_render_base"

    @abstractmethod
    async def render(
        self,
        logic_graph: dict[str, Any],
        *,
        preferred_format: Literal["html", "svg", "png", "mermaid"] = "mermaid",
    ) -> RenderOutput:
        """渲染 logic_graph。返回结构里至少一项不是 None。"""


class CodeEditProvider(CodeProvider, ABC):
    """阶段三：代码生成/修改 + lint 自动回修。对应 SPEC 2.4.2。"""

    name: ClassVar[str] = "code_edit_base"

    @abstractmethod
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
        """按 instruction 修改/生成代码。返回变更集 + lint 结果。"""


class TestGenProvider(CodeProvider, ABC):
    """阶段三：测试生成。对应 SPEC 2.4.3。

    generate 除代码级测试外也支持「仅需求模式」：project_root 为空、
    通过 requirement + logic_graph 直接设计端到端测试场景。
    """

    name: ClassVar[str] = "test_gen_base"

    @abstractmethod
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
    ) -> TestReport:
        """生成针对 target_symbols 的测试并运行，返回完整测试报告。

        requirement: 结构化需求（仅需求模式下的主要设计依据，代码模式可忽略）
        feedback:    人工验收驳回的意见，重新设计用例时需针对性修正
        checklists:  路由确认后加载的业务检查清单 [{rel_dir, name, content}]，
                     非空时用例设计必须逐条核对覆盖（None = 库未命中）
        """
