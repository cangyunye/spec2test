"""conftest checkpoint 隔离回归：settings 属性与 orchestrator 连接缓存必须真正重置。

历史缺陷：conftest 只 monkeypatch.setenv("CHECKPOINT_SQLITE_PATH")，而 settings 在
import 时已读取该变量、orchestrator 的 sqlite 连接又全局缓存——测试全部写穿到
data/checkpoints.db，把 tc3xx / rr-e2e 等测试线程混进真实会话列表。
"""
from __future__ import annotations

from pathlib import Path

from devflow.config import settings


def test_checkpoint_path_points_to_tmp(tmp_path):
    assert Path(settings.CHECKPOINT_SQLITE_PATH).parent == tmp_path
    assert str(settings.CHECKPOINT_SQLITE_PATH).endswith("checkpoints.db")


def test_orchestrator_conn_reset_between_tests():
    import devflow.orchestrator as orch

    assert orch._conn is None  # autouse fixture 每个用例前后都重置连接缓存
