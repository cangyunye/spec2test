# DevFlow · 需求到测试全链路 AI 工具

输入一段需求，DevFlow 通过 LLM 完成需求澄清、可机读逻辑图生成、代码检索 / 生成、测试场景设计；在制图评审与人工验收两个门禁由人把关，最终交付可导出的测试场景与产物汇总。

> 设计文档（架构 / 阶段可行性 / 协议）：见 [SPEC.md](SPEC.md) · 上手教程：见 [QUICKSTART.md](QUICKSTART.md)

## 核心特性

- **全链路编排**：需求澄清 → 逻辑制图 → 制图评审 → 代码检索 → 代码生成 → 测试设计 → 人工验收，LangGraph 状态机驱动，SQLite checkpoint 断点续跑
- **双门禁人工把关**：逻辑图评审、最终验收两处中断等待决策；**驳回必须带修改意见**，意见回传给制图 / 代码生成节点做针对性修正
- **Web Shell（推荐）**：黑白双主题界面，聊天式单入口、LLM 逐字流式输出、节点级耗时进度、逻辑图缩放 / 平移 / 节点检查器、测试场景表筛选与 CSV / Markdown 导出
- **CLI 孪生客户端**：同一套图与事件协议，`devflow new / resume / list / export / check-providers`
- **永不卡死的演示模式**：未配置 API Key 时自动 Mock 兜底，全流程可跑通（输出为演示数据）
- **可插拔 Provider**：检索 / 制图 / 代码生成 / 测试生成各能力独立选择 codegraph / opencode / llm / mock 后端

## 快速开始

```bash
pip install -r requirements.txt
python3 -m uvicorn web.server:app --port 8100
# 打开 http://127.0.0.1:8100，输入框直接描述需求回车即可
```

配置真实 LLM：`cp .env.example .env` 后填入 DeepSeek / SiliconFlow 等 OpenAI 兼容服务的 Key（不配置则 Mock 演示模式）。完整步骤与 CLI 用法见 **[QUICKSTART.md](QUICKSTART.md)**。

## 架构一览

```
┌────────────────────────────────────────────────────────────┐
│            客户端：Web Shell (SSE)  ·  CLI (rich)           │
├────────────────────────────────────────────────────────────┤
│  事件层 devflow/events.py（唯一事件源：stage/artifact/gate/  │
│  node_done/token/question/error，两个客户端共用）             │
├────────────────────────────────────────────────────────────┤
│  LangGraph 编排 devflow/orchestrator.py                     │
│  compress → clarify_extract → clarify_validate ─┬→ 追问     │
│      └→ graph_generate → graph_review(门禁1) ──┘            │
│         → code_search → graph_render → code_gen             │
│         → test_gen → review(门禁2) → END                    │
├────────────────────────────────────────────────────────────┤
│  Providers：LLM(OpenAI 兼容/Mock) · CodeGraph · OpenCode    │
│  Checkpoint：SQLite（data/checkpoints.db，断点恢复/会话管理） │
└────────────────────────────────────────────────────────────┘
```

## 目录结构

```
devflow/            核心包：编排 / 节点 / 事件 / LLM 客户端 / Provider / Schema
  nodes/            LangGraph 节点（澄清、制图、双门禁、检索、生成、测试）
  providers/        可插拔后端（mock / opencode / codegraph / archify）
web/                Web Shell：FastAPI 服务 + 静态前端（原生 JS，无框架）
tests/              pytest 单测（live 用例需 LLM_LIVE_TESTS=1 才执行，默认跳过）
SPEC.md             需求与架构设计文档（评审稿）
QUICKSTART.md       5 分钟上手指南（推荐从这里开始）
data/               SQLite checkpoint（会话数据）
```

## 开发

```bash
python3 -m pytest tests/ -q          # 全量单测
python3 -m devflow.cli check-providers   # 各后端可用性自检
```

## 已知限制

- 仓库历史中出现的测试 Key 已吊销，不可使用；请配置自己的 Key
- 代码检索 / 代码生成的真实执行依赖 OpenCode 或 CodeGraph 本地部署，默认 mock
- 会话列表不含墙钟时间（checkpoint 未存时间戳），按最近活动排序
