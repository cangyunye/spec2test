"""全局 pytest fixture：测试隔离（清理进程内全局单例）。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


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

    # SQLite checkpoint → 每个测试独立临时文件
    ckpt = tmp_path / "checkpoints.db"
    monkeypatch.setenv("CHECKPOINT_SQLITE_PATH", str(ckpt))

    # 清理已有的 dead_letter（对之前的测试残留）
    dead = DATA_DIR / "dead_letter"
    if dead.exists():
        shutil.rmtree(dead, ignore_errors=True)

    yield  # 执行测试

    # ── post: 再重置一次（防止测试留脏数据给下一个用例）──────────
    reset_all_default_breakers()
    reset_default_token_budget()
    reset_model_cache()
