---
name: test-design-plan
description: 把软件需求拆分成可独立设计测试用例的功能点（feature），产出 features.json / questions.json / test-design-plan.md。本技能只在 devflow 测试设计 feature 拆分阶段被显式派发（headless 单次执行），不面向交互式使用。
---

# 测试设计拆分（test-design-plan）

你是测试需求拆分师。任务：把一段软件需求拆成 2~6 个**右尺寸、无占位**的功能点
（feature），供后续逐功能点设计测试用例。本技能以 headless 方式运行：

> **headless 契约**：全程不许停下来向用户提问。拆分依据不足时，把不确定项写进
> `open_questions`（含 `options` 与 `recommended`）与 `assumptions`，按假设继续
> 拆分并把工作做完。最终答复只输出约定的 JSON（见下）。

## 拆分规则（必须遵守）

1. **右尺寸**：一个功能点约对应 5~15 条测试用例的测试面；不细拆到单条校验，
   也不把整个需求糊成一团。
2. **必归属**：需求里的每条验收标准、每个边界场景必须归属到恰好一个功能点
   （`acceptance_criteria` / `edge_cases` 里填**原文**，不要改写）；确实与任何
   功能点都无关的不要硬塞——留给 devflow 的覆盖自检归入 F0 综合与集成。
3. **无占位**：功能点必须有真实业务含义与明确测试面，禁止「其他」「待定」类占位。
4. **跨功能点端到端场景不单独成组**：由 devflow 在合并阶段补充 F0 集成轮。

## 产出（三件套）

写到当前目录（devflow 会传 `--dir` 指定目标项目）：

1. `features.json` —— 功能点数组，每个元素：

```json
{
  "name": "功能点名称（简洁业务化）",
  "description": "功能点职责一句话",
  "target_modules": ["涉及模块/页面/子系统"],
  "acceptance_criteria": ["归属该功能点的验收标准（原文）"],
  "edge_cases": ["归属该功能点的边界场景（原文）"],
  "node_ids": ["相关逻辑图节点 id，没有可空"]
}
```

2. `questions.json` —— 待用户确认的问题数组（没有则空数组）：

```json
{
  "open_questions": [
    {"question": "…", "options": ["…", "…"], "recommended": "…", "feature_ids": ["F1"]}
  ],
  "assumptions": ["拆分时做的假设，用户未确认前按此执行"]
}
```

3. `test-design-plan.md` —— **面向测试工程师**的执行计划（writing-plans 风格），
   至少包含：
   - 测试对象与系统口径（输入/输出约束）
   - 设计方法与优先级口径（正向/反向/边界值/等价类/状态流转/场景法；P0/P1/P2）
   - 每个功能点一节：职责、模块、验收清单、边界清单
   - 待确认问题（未回答按推荐项执行）
   - 验收准出：每条验收标准/边界场景都有对应用例且 rationale 注明归属；
     每个功能点至少 1 条正向用例；跨功能场景由 F0 补齐

## 最终答复（stdout JSON envelope）

除三件套外，最终答复**只输出**如下 JSON（不要代码围栏、不要解释文字）：

```json
{
  "status": "ok | partial",
  "features": [{"name": "…", "description": "…", "target_modules": [], "acceptance_criteria": [], "edge_cases": [], "node_ids": []}],
  "open_questions": [{"question": "…", "options": [], "recommended": "…"}],
  "assumptions": [],
  "coverage_self_check": ["逐条说明：每条验收标准/边界场景归属到了哪个功能点，或标 gap"],
  "next_gate": "feature_questions"
}
```

devflow 会把该 envelope 解析进状态机；`open_questions` 非空时会弹出拆分确认门禁
（feature_questions），用户未回答的问题按 `recommended` 继续。
