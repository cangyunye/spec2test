# CaseCraft · 需求到测试全链路 AI 工具

输入一段需求，CaseCraft 通过 LLM 完成需求澄清、可机读逻辑图生成、代码检索 / 生成、测试场景设计；在需求确认、制图评审与人工验收三处门禁由人把关——**模型从描述里推断（脑补）出的需求字段，必须经用户确认才能进入制图**，最终交付可导出的测试场景与产物汇总。

> 设计文档（架构 / 阶段可行性 / 协议）：见 [SPEC.md](SPEC.md) · 上手教程：见 [QUICKSTART.md](QUICKSTART.md)

## 核心特性

- **全链路编排**：需求澄清 → 逻辑制图 → 制图评审 → 代码检索 → 代码生成 → 测试设计 → 人工验收，LangGraph 状态机驱动，SQLite checkpoint 断点续跑
- **对话式澄清双模式**：需求有缺口时，首轮追问下方弹出选择卡——「头脑风暴」一轮一问探索目的 / 约束 / 成功标准（附候选方向与推荐），「拷问」逐题施压验证（附推荐答案，回「同意」直接采纳），也可直接输入补充；需求清晰时反馈确认、无需再发散
- **双模式分支**：提供项目代码走完整链路（检索 / 生成 / 真实测试执行）；**不提供代码则走「仅需求模式」**——评审逻辑图后直接基于需求 + 逻辑图生成端到端测试用例
- **系统化用例设计方法论**：融入 doc-based / functional testcase-generator 方法——正向 / 反向 / 边界值 / 等价类 / 状态流转 / 场景法六类设计策略，P0-P2 优先级，输出前质量自检；用例带标识与所属模块，导出为「概述 → 分模块用例 → 自检」的总-分结构文档
- **业务 Checklist 库（.checklist）**：测试设计前按需求路由本地业务清单库，**清单门禁每单必弹**——skill 式渐进披露，只读各 `scenario.md` 的路由标签匹配，确认后加载对应 `checklist.md` 注入用例设计并逐条核对覆盖；空库/无匹配时可直接上传 wiki/验收清单文档由 AI 按规范归纳入库；用例生成后逐条「采纳」即评审通过，采纳后 AI 归纳（或手写）沉淀新清单登记入库，库越用越厚，形成「导入/沉淀 → 路由 → 更准的生成」闭环
- **三门禁人工把关**：① **需求确认**（制图前）：把需求字段摊开给用户核对，每个字段标注来源（用户原话 / AI 推断），只有存在 AI 推断字段时才打断——可就地修改字段值后确认，或驳回继续补充需求；② **逻辑图评审**；③ **最终验收**。后两处**驳回必须带修改意见**，意见回传给制图 / 代码生成节点做针对性修正
- **Web Shell（推荐）**：黑白双主题界面，聊天式单入口、LLM 逐字流式输出、节点级耗时进度、逻辑图缩放 / 平移 / 节点检查器、测试场景表筛选与 CSV / Markdown 导出
- **CLI 孪生客户端**：同一套图与事件协议，`devflow setup / new / resume / list / export / checklist / check-providers / check-llm`
- **永不卡死的演示模式**：未配置 API Key 时自动 Mock 兜底，全流程可跑通（输出为演示数据）
- **可插拔 Provider（执行器可替换）**：检索 / 制图 / 代码生成 / 测试生成各能力独立选择
  codegraph / opencode / pi / llm / mock 后端；外部 Agent 只承担执行，可随时替换（见下节定位说明）

## 定位：DevFlow 只做编排，执行交给可替换的 Agent

DevFlow 自身只负责**编排**——需求澄清、逻辑图生成、三门禁评审、全局状态权威（Single Source
of Truth）、断点续跑与产物交付。真正「读代码、改代码、跑测试」这类专业活，交给外部编码
Agent（OpenCode / Pi 等）执行。这样设计有三个明确意图：

1. **接入专业能力，不自研重造**：外部 Agent 是专业 skill 与「代码检索 → 修改 → lint / 测试
   修复」轮询的载体——它们在目标项目里自主检索与多轮修复，**循环到产出一个可接受的产物**，
   再以结构化结果交回 DevFlow。DevFlow 不做代码生成，只负责派发任务与验收结果。
2. **随时可替换、可迭代**：Provider 抽象把「能力」和「实现」分开，四个能力各自独立选后端。
   换 Agent（如 OpenCode → Pi）、换版本、换模型都只改 `.env`，编排、门禁、checkpoint
   与前端全部不动；接入新 Agent 也只需实现同一套 Provider 接口。
3. **可横向对比不同 Agent 的效果**：同一份需求可以换后端各跑一遍，在相同的门禁流程与产物
   结构下对比实际产出（用例质量、改动幅度、lint / 测试通过率），为选型提供依据。

不接任何外部 Agent 也能跑：默认主路径是 LLM 直连 + 本地 pytest 真实执行，未配置时自动 Mock
降级，这是演示与 CI 的基线。

## 快速开始

```bash
pip install -r requirements.txt
python3 -m devflow.cli setup                          # 首次启动引导：检测 → 装服务 → 配 .env
python3 -m uvicorn web.server:app --port 8100
# 打开 http://127.0.0.1:8100，输入框直接描述需求回车即可
```

`setup` 向导会检测 `.env` 与外部服务（OpenCode / Pi / CodeGraph / Archify），多选引导安装缺失项
——CodeGraph 从 GitHub Releases 下载（SHA256 校验），Pi 走 `npm install -g
@mariozechner/pi-coding-agent`（装完先交互运行一次 `pi` 完成模型登录）——网络超时 / 失败
自动降级为手动指引并跳过；按已装服务写好 `.env`，最后输出供应商测试与启动指令。配置真实 LLM：`cp .env.example .env` 后填入 DeepSeek /
SiliconFlow 等 OpenAI 兼容服务的 Key（不配置则 Mock 演示模式）。完整步骤与 CLI 用法见
**[QUICKSTART.md](QUICKSTART.md)**。

## 业务 Checklist 库（.checklist）

测试设计前，CaseCraft 会按需求路由本地业务清单库（每单必弹确认门禁，空库/无匹配时支持
直接上传清单文档入库）；确认后把检查清单注入用例设计，用例逐条采纳即评审通过，
评审通过后可把用例（AI 归纳或手写）沉淀回库。

**库根解析优先级**：环境变量 `DEVFLOW_CHECKLIST_ROOT` > `<project_root>/.checklist`（存在时）
> `data/checklist/`（全局，仅需求模式也能用）。

**目录与文件规范**：

```
<root>/
  payment/                 # 英文目录名 = 业务类型
    scenario.md            # 必须。frontmatter = 路由标签；references 指向子业务
    checklist.md           # 可选。业务级总清单
    refund/                # 子业务，平铺在业务目录下（层级不限）
      scenario.md
      checklist.md         # 确认匹配后真正加载的检查清单
  _template/               # 模板目录（下划线开头不参与路由）
```

- `scenario.md` frontmatter：`name`（中文名）/ `description`（一句话路由描述）/ `keywords` /
  `references`（`[{path, desc}]`，可选）。路由阶段只读 frontmatter，正文不进上下文。
- `checklist.md` 按 正向/反向/边界值/等价类/状态流转/场景法/安全/性能 分节，
  条目格式 `- [P0] 可验证的一句话检查点`；frontmatter 带 `business/updated/sources`（沉淀溯源）。

**运行方式**：

- **路由（每单必弹）**：进入测试设计前，`checklist_route` 节点按需求匹配业务/子业务并弹出确认门禁——
  有候选展示 AI 预选勾选树（可取消勾选）；空库 / 无匹配同样必弹，提供
  「上传清单文档入库」「查看清单库」「跳过」入口，清单环节永远可见；
  已路由（用例回炉重生成）放行不重复弹。确认后清单内容注入用例设计 prompt，
  要求逐条核对覆盖并在用例卡片标注来源。
- **导入**：清单来源不限于本服务沉淀——在路由门禁上传 wiki 页面 / 验收清单 / 用例文档
  （.md / .txt / .docx ≤5MB），AI 按 8 分节规范归纳为 scenario.md + checklist.md，
  预览确认后入库（frontmatter `sources` 记 `import:<文件名>`），并可当场勾选注入本单。
- **采纳 = 评审通过**：测试场景卡逐条勾选「采纳」，终审门禁弹出后点「✓ 提交评审」
  一键通过（`state.adopted_cases` 记录采纳集）；不做采纳则终审门禁照常通过/驳回。
- **沉淀**：评审通过后服务端主动询问——AI 归纳（预勾选采纳的用例）/ 手写清单
  （AI 规范化）/ 暂不；也可随时在测试场景卡点「☰ 沉淀」。归纳时标记业务类型
  （选已有或新建），目标目录已有清单自动合并去重，`sources` 跨次累积，预览确认后入库。
- **CLI**：`devflow checklist init`（生成模板 + payment 示例 + 内置通用清单：HTTP 接口 / 前端
  鉴权·布局·易用 / 数据库 SQL / Shell 脚本 / Skill 设计 / Agent 设计 / 自动化流水线 / 单元测试，
  共 172 条，`--no-general` 跳过）· `list`（库树）· `show <业务路径>`。
- **库浏览**：顶栏「☰ TC-CHECKLIST」（testcases 检查清单）从右侧展开浏览抽屉——标题导航 +
  关键词检索（命中的检查点直列并高亮，点击直达原文位置），库根可切换（项目根模式读
  `<项目>/.checklist`，或直接指定库根），点标题用模态框阅读详情（scenario 路由标签 +
  checklist 分节条目，轻量 markdown 渲染），不打断当前会话；清单路由门禁可点「👁 预览」
  先看清单内容再确认注入，沉淀 / 导入 / 手写入库后的提示可一键「查看」定位新业务；
  也可直接访问 `/library` 独立文档页，展示库全树（只读），支持切换 `project_root` / 库根、
  关键词检索与 P0–P2 筛选。

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
│      └→ requirement_review(门禁1·需求确认) ──────┘            │
│         → graph_type_select → graph_generate                │
│         → graph_review(门禁2·制图评审)                       │
│         → code_search → graph_render → code_gen             │
│         → checklist_route(清单路由确认) → test_gen           │
│         → review(门禁3·人工验收) → END                       │
├────────────────────────────────────────────────────────────┤
│  Providers：LLM(OpenAI 兼容/Mock) · CodeGraph · OpenCode    │
│  Checkpoint：SQLite（data/checkpoints.db，断点恢复/会话管理） │
└────────────────────────────────────────────────────────────┘
```

## 目录结构

```
devflow/            核心包：编排 / 节点 / 事件 / LLM 客户端 / Provider / Schema
  nodes/            LangGraph 节点（澄清、需求确认、制图、三门禁、检索、生成、清单路由、测试）
  providers/        可插拔后端（mock / opencode / codegraph / archify）
  checklist/        业务清单库（.checklist）：扫描路由 / 沉淀归纳 / 脚手架
web/                Web Shell：FastAPI 服务 + 静态前端（原生 JS，无框架）
tests/              pytest 单测（live 用例需 LLM_LIVE_TESTS=1 才执行，默认跳过）
SPEC.md             需求与架构设计文档（评审稿）
QUICKSTART.md       5 分钟上手指南（推荐从这里开始）
data/               SQLite checkpoint（会话数据）+ checklist 全局库
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
