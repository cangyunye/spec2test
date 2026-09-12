"""devflow.providers 包入口：暴露工厂 + 各实现。

使用方式（LangGraph 节点只依赖抽象）：

    from devflow.providers import Providers, get_providers
    providers: Providers = get_providers()  # 按环境变量装配
    res = await providers.code_search.search(...)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .archify import ArchifyProvider
from .base import (
    CodeEditProvider,
    CodeGraphRenderProvider,
    CodeSearchProvider,
    TestGenProvider,
)
from .codegraph import CodeGraphProvider
from .llm_testgen import LlmTestGenProvider
from .mock import MockCodeEdit, MockCodeGraphRender, MockCodeSearch, MockTestGen
from .opencode import (
    OpenCodeEditProvider,
    OpenCodeSearchProvider,
    OpenCodeTestProvider,
)
from .pi import PiEditProvider, PiSearchProvider, PiTestProvider


@dataclass
class Providers:
    """LangGraph 节点消费的 Provider 集合。"""

    code_search: CodeSearchProvider
    graph_render: CodeGraphRenderProvider
    code_edit: CodeEditProvider
    test_gen: TestGenProvider


# ═══════════════════════════════════════════════════════════════════
# 单个能力的工厂
# ═══════════════════════════════════════════════════════════════════
def build_code_search(name: str, **_: Any) -> CodeSearchProvider:
    if name in ("codegraph", "code_graph"):
        # CodeGraph 失败时自动 fallback 到 mock，保证 CI 离线能跑
        return CodeGraphProvider(fallback=MockCodeSearch())
    if name == "opencode":
        return OpenCodeSearchProvider()
    if name == "pi":
        # pi 无代码索引，agent 翻文件式检索（慢）；优先 codegraph
        return PiSearchProvider()
    if name == "mock":
        return MockCodeSearch()
    raise ValueError(f"未知 CODE_SEARCH_PROVIDER={name!r}，可选: codegraph/opencode/pi/mock")


def build_graph_render(name: str, **_: Any) -> CodeGraphRenderProvider:
    if name == "archify":
        return ArchifyProvider()
    if name in ("mermaid", "mock"):
        return MockCodeGraphRender()
    raise ValueError(
        f"未知 CODE_GRAPH_RENDER_PROVIDER={name!r}，可选: archify/mermaid/mock"
    )


def build_code_edit(name: str, **_: Any) -> CodeEditProvider:
    if name == "opencode":
        return OpenCodeEditProvider()
    if name == "pi":
        return PiEditProvider()
    if name == "mock":
        return MockCodeEdit()
    raise ValueError(f"未知 CODE_EDIT_PROVIDER={name!r}，可选: opencode/pi/mock")


def build_test_gen(name: str, **_: Any) -> TestGenProvider:
    if name == "opencode":
        return OpenCodeTestProvider()
    if name == "llm":
        from .llm_testgen import LlmTestGenProvider

        return LlmTestGenProvider()
    if name == "pi":
        return PiTestProvider()
    if name == "mock":
        return MockTestGen()
    raise ValueError(f"未知 TEST_GEN_PROVIDER={name!r}，可选: llm/opencode/pi/mock")


# ═══════════════════════════════════════════════════════════════════
# 总入口
# ═══════════════════════════════════════════════════════════════════
def get_providers(
    *,
    code_search: str | None = None,
    graph_render: str | None = None,
    code_edit: str | None = None,
    test_gen: str | None = None,
) -> Providers:
    """按 .env / 显式参数装配。显式参数优先于环境变量。

    test_gen 默认 llm：测试设计是纯 LLM 工作，配置了 Key 即产出真实用例；
    未配置 Key 时 LlmTestGenProvider 内部经 invoke_json 自动降级 Mock 演示模板，
    离线/无 Key 场景不需要专门的 mock 配置。
    """
    cs = code_search or os.getenv("CODE_SEARCH_PROVIDER", "mock")
    gr = graph_render or os.getenv("CODE_GRAPH_RENDER_PROVIDER", "mermaid")
    ce = code_edit or os.getenv("CODE_EDIT_PROVIDER", "mock")
    tg = test_gen or os.getenv("TEST_GEN_PROVIDER", "llm")
    return Providers(
        code_search=build_code_search(cs),
        graph_render=build_graph_render(gr),
        code_edit=build_code_edit(ce),
        test_gen=build_test_gen(tg),
    )


__all__ = [
    "Providers",
    "get_providers",
    "build_code_search",
    "build_graph_render",
    "build_code_edit",
    "build_test_gen",
    "CodeSearchProvider",
    "CodeGraphRenderProvider",
    "CodeEditProvider",
    "TestGenProvider",
    "ArchifyProvider",
    "CodeGraphProvider",
    "LlmTestGenProvider",
    "MockCodeSearch",
    "MockCodeGraphRender",
    "MockCodeEdit",
    "MockTestGen",
    "OpenCodeSearchProvider",
    "OpenCodeEditProvider",
    "OpenCodeTestProvider",
    "PiSearchProvider",
    "PiEditProvider",
    "PiTestProvider",
]
