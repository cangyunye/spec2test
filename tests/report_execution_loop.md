# 执行闭环测试报告（apply_code + test_run）

> 对应 SPEC「八、实施补记 8.1」：code_gen 产出的 diff 真实落盘 + 目标项目真实执行 pytest。
> 环境：Python 3.14 · pytest 9.0.3（子进程复用当前解释器）· 全程离线（Mock Provider / Mock LLM）

---

## 1. 覆盖用例与结果

```
python3 -m pytest -q   → 249 passed, 2 skipped（skip=live 用例，需 LLM_LIVE_TESTS=1）
```

| 测试文件 | 覆盖点 | 结果 |
|---|---|---|
| `tests/test_code_apply.py` (16) | diff 解析（a/b 前缀、无前缀、多文件、new/delete、`/dev/null`、行号漂移模糊匹配、`\ No newline`）；预检 all-or-nothing；备份+manifest+回滚（含删除新建文件）；路径越界拒绝 | ✅ |
| `tests/test_test_runner.py` (7) | 真实子进程 pytest：全过 / 失败明细（junit 解析）/ 未收集用例 / project_root 不存在 / 解释器缺失 / 超时杀进程 / `paths` 收集范围 | ✅ |
| `tests/test_test_run_node.py` (13) | apply_code：落盘+备份、content_after 仅限新建、diff 坏→`EXEC.APPLY_FAILED`（可重试）、开关与 root 缺失优雅跳过；test_run：通过回填/失败摘要（test_failure）+retry 计数、未应用跳过、开关跳过、total=0 不声称通过；路由矩阵 | ✅ |
| `tests/test_stage3_e2e.py` 新增 (2) | TC311 全链路执行闭环：双门禁 approve 之间 diff 真实写入 tmp 项目、备份内容=应用前、`run.executed=True`；TC312 失败回修循环：mock 修不好真实失败，`TEST_RUN_MAX_FIX_ROUNDS+1` 次失败后有界收敛进人工验收，失败明细进报告 | ✅ |

## 2. 关键行为语义（实现时定下的约定）

1. **业务失败不占错误通道**：diff 应用不了 / 测试没过走业务路由（回 code_gen / 进人工验收）；
   `last_error_code` 只留给基础设施错误（NODE.CONTEXT 等）。
2. **落盘失败降级不中断**：`EXEC.APPLY_FAILED` 可重试（回炉 1 次），仍失败 → `code_apply.applied=False`
   带 reason 继续 → test_run 跳过执行 → 人工验收兜底。Mock 后端 + 真实项目的演示流程不受影响。
3. **content_after 只对新建文件整写**：update 必须走 diff 上下文匹配，防止 mock/幻觉内容覆盖真实源码。
4. **`total=0` 不等于通过**：没收集到用例时 `test_passed=None`，人工验收明确可见。
5. **修复循环有界**：`retry_count.test_run` 连续失败计数，超过 `TEST_RUN_MAX_FIX_ROUNDS`（默认 2）
   带失败报告进人工验收；通过后计数复位。

## 3. 顺带修复

`dead_letters` 之前未在 `GlobalState` 声明——LangGraph 会静默丢弃未声明键（已用最小复现验证），
死信从未真正到达 `dead_letter_drain` 落盘。声明后恢复生效。

## 4. 遗留 / 后续

- Web 前端仅在验收门禁统计条增加「测试执行」格（真实执行 / 未执行+原因）；测试场景表格渲染
  已兼容两种形态，无需改动。
- 回滚目前是库能力（`code_apply.rollback`）+ CLI 导出备份目录信息；Web 一键回滚按钮待做。
- 真实后端（OpenCode / CodeGraph / Archify）定位为可选增强，见 SPEC 8.2。
