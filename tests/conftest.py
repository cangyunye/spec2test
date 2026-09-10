"""全局 pytest fixture：测试隔离（清理进程内全局单例）。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="session", autouse=True)
def _force_mock_llm_unless_live():
    """测试会话默认强制 Mock LLM：隔离网络与真实 Key（ Hermetic tests）。

    开发者 .env 里的真实 Key 不应让单测打真实 API（变慢、耗 token、依赖网络）。
    需要打真实 LLM 的用例：LLM_LIVE_TESTS=1 pytest（对应 tests/test_live_llm.py）。
    """
    if os.getenv("LLM_LIVE_TESTS"):
        yield
        return
    from devflow.config import settings

    saved = (list(settings.LLM_PROVIDERS), settings.LLM_USE_MOCK_FALLBACK)
    settings.LLM_PROVIDERS = []
    settings.LLM_USE_MOCK_FALLBACK = True
    yield
    settings.LLM_PROVIDERS, settings.LLM_USE_MOCK_FALLBACK = saved


@pytest.fixture(autouse=True)
def _isolate_global_state(tmp_path, monkeypatch):
    """每个测试执行前后：重置全局单例，杜绝交叉污染。

    覆盖：
      - 熔断器（CircuitBreaker 默认池）
      - Token 预算
      - LLM 模型缓存（_model_cache）
      - SQLite checkpoint 路径（指到 tmp_path 下临时文件）
      - data/ 下的 dead_letter 目录（执行前清空）
    """
    # ── pre: 重置 ────────────────────────────────────────
    from devflow.resilience import (
        reset_all_default_breakers,
        reset_default_token_budget,
    )
    from devflow.llm_client import reset_model_cache

    reset_all_default_breakers()
    reset_default_token_budget()
    reset_model_cache()

    # SQLite checkpoint → 每个测试独立临时文件。
    # 注意：settings 在 import 时就读过环境变量，仅 setenv 不生效；必须同时改
    # settings 对象本身，并清掉 orchestrator 的全局连接缓存（首用后即定型），
    # 否则测试会写穿到 data/checkpoints.db，把测试线程混进真实会话列表。
    ckpt = tmp_path / "checkpoints.db"
    monkeypatch.setenv("CHECKPOINT_SQLITE_PATH", str(ckpt))
    from devflow.config import settings as _settings
    import devflow.orchestrator as _orch

    monkeypatch.setattr(_settings, "CHECKPOINT_SQLITE_PATH", ckpt)
    monkeypatch.setattr(_orch, "_conn", None)

    # 清理已有的 dead_letter（对之前的测试残留）
    dead = DATA_DIR / "dead_letter"
    if dead.exists():
        shutil.rmtree(dead, ignore_errors=True)

    yield  # 执行测试

    # ── post: 再重置一次（防止测试留脏数据给下一个用例）──────────
    reset_all_default_breakers()
    reset_default_token_budget()
    reset_model_cache()
