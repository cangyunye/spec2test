# AI 全流程开发工具 - 需求与架构设计文档 (SPEC)

> 版本: v0.1 (评审草案)  
> 日期: 2026-08-08  
> 状态: **待用户评审**

---

## 一、头脑风暴：各阶段可行性深度分析

### 1.1 阶段一：交互式需求澄清 Agent

| 维度 | 分析 | 结论 |
|------|------|------|
| **核心难点** | 多轮循环追问不能跑偏、信息不能遗漏、结构化字段必须强制对齐 | ⚠️ 中高风险 |
| **技术可行性** | LangGraph 原生状态机 + 条件循环路由 = 完美匹配。JSON Schema 强制输出可保证不跑偏。 | ✅ 高 |
| **工程复杂度** | 需要预定义《需求信息完备性清单》Schema，字段设计需要覆盖全新功能/组件迭代/缺陷修复三种场景 | 🟡 中等 |
| **失败回退** | 若 LLM 追问发散，可通过「Schema 校验节点」拦截，只接受缺失字段，拒绝冗余回答 | ✅ 有兜底 |
| **MVP 可裁剪** | 第一版可只做 5 个核心必填字段：需求类型、项目路径、输入输出约束、参考文件、边界场景 | ✅ 可裁剪 |

> **关键决策点**: 必须用 JSON Schema 强制约束 LLM 输出，禁止自由追问。否则纯 Skill 方案必跑偏。

---

### 1.2 阶段二：代码上下文自动检索模块

| 维度 | 分析 | 结论 |
|------|------|------|
| **核心难点** | 如何精准定位用户需求对应的代码片段、调用链路、依赖关系 | ⚠️ 高风险 |
| **自研可行性** | tree-sitter 做 AST 解析 + Chroma 向量检索，能跑但效果上限取决于 embedding 质量，调优周期长 | 🟡 可行但成本高 |
| **OpenCode 复用可行性** | OpenCode 已内置 LSP 符号搜索、文件 AST 解析、跨文件调用链追踪，直接调用其 API 即可 | ✅ 强烈推荐 |
| **失败回退** | 检索不到结果时，自动路由回「需求澄清」节点，要求用户提供具体文件路径或函数名 | ✅ 有兜底 |
| **MVP 可裁剪** | 第一版可不做自动检索，直接让用户提供文件路径，跳过此节点 | ✅ 可裁剪 |

> **关键决策点**: **不自研代码检索**，直接通过 API 对接 OpenCode。代码检索的 LSP/AST 能力从零构建至少 2~3 周工作量，OpenCode 现成且成熟。

---

### 1.3 阶段三：可机读逻辑图生成

| 维度 | 分析 | 结论 |
|------|------|------|
| **核心难点** | 图结构必须同时满足：人能看懂（Mermaid 可视化）+ 机可读（JSON 结构化可遍历）+ 绑定代码位置 | ⚠️ 高风险 |
| **技术可行性** | LangGraph 的 StateGraph 本身就是图结构，生成的逻辑图格式可直接作为后续节点输入，无需二次解析 | ✅ 高 |
| **工程复杂度** | 需要设计统一的图 Schema（Node/Edge/Metadata），并绑定 code_ref、is_modified、branch_condition 等字段 | 🟡 中等 |
| **失败回退** | 图生成后必须经过「校验节点」检查：是否所有节点都有 code_ref？修改标记是否完整？缺失则回退重新生成 | ✅ 有兜底 |
| **MVP 可裁剪** | 第一版可只生成 3~5 个核心节点的简化图，不强求全量分支 | ✅ 可裁剪 |

> **关键决策点**: 图 = LangGraph State 中的结构化字段，不是 Mermaid 文本。Mermaid 只是从结构化字段渲染给用户看的「视图层」。

---

### 1.4 阶段四：代码生成 / 修改

| 维度 | 分析 | 结论 |
|------|------|------|
| **核心难点** | 代码修改不能破坏现有功能、需遵循项目风格、依赖现有上下文 | ⚠️ 极高风险 |
| **自研可行性** | 纯 LLM 生成 + 文件写入，容易出现引用错误、风格不一致、破坏现有代码 | ❌ 不推荐 |
| **OpenCode 复用可行性** | OpenCode 是编程专用 Agent，内置代码风格学习、LSP 诊断、多轮修复循环、会话压缩，效果远优于通用 LLM 直接生成 | ✅ 强烈推荐 |
| **失败回退** | OpenCode 返回结果后，LangGraph 校验「是否通过 Lint/TypeCheck」，失败则把错误信息传回 OpenCode 子会话继续修复 | ✅ 有兜底 |
| **MVP 可裁剪** | 第一版可只生成代码 diff（不直接写文件），由人工确认后再应用 | ✅ 可裁剪 |

> **关键决策点**: 代码生成/修改 **100% 外包给 OpenCode**。LangGraph 只负责任务派发和结果验收，不自研代码生成。

---

### 1.5 阶段五：单测 / 接口测试生成

| 维度 | 分析 | 结论 |
|------|------|------|
| **核心难点** | 测试用例需覆盖逻辑图所有分支，修改部分必须有增量测试 | ⚠️ 中高风险 |
| **技术可行性** | 逻辑图已标记 is_modified 边，可精准定位需要新增测试的分支，定向生成 | ✅ 可行 |
| **工程复杂度** | 需要把逻辑图的「分支条件」映射为测试用例的输入输出断言 | 🟡 中等 |
| **OpenCode 复用可行性** | OpenCode 已有测试生成能力，可直接调用 | ✅ 推荐 |
| **MVP 可裁剪** | 第一版可不执行测试，只生成测试用例文件交给人工确认 | ✅ 可裁剪 |

> **关键决策点**: 测试生成的「分支覆盖率」指标由逻辑图驱动，不是 LLM 自由发挥。

---

### 1.6 跨阶段共性问题：会话记忆与压缩

| 策略 | 实现方式 | Token 占用 | 信息损失 | 适用阶段 |
|------|----------|------------|----------|----------|
| 尾部截断 | trim_messages 保留最后 N 轮 | 低 | 丢失旧对话 | 闲聊式交互 |
| 摘要压缩 | LLM 总结旧对话 | 中 | 细节丢失 | 通用长对话 |
| **结构化提取（首选）** | 只保留各环节结构化产物（需求 Schema、逻辑图等），原始对话归档 | **极低** | **零损失** | **本系统所有阶段** |

> **关键决策点**: 所有关键结论必须沉淀为 State 中的结构化字段，原始对话只保留最近 3 轮用于理解用户意图，归档部分不参与 LLM 推理。

---

## 二、OpenCode 与 LangGraph 调用协议设计

### 2.1 架构定位

```
┌─────────────────────────────────────────────────────────────────┐
│                        LangGraph (主流程)                        │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐  │
│  │ 需求澄清   │→│  代码检索  │→│ 逻辑图生成  │→│ 代码/测试   │  │
│  │   Agent    │  │   Node     │  │   Agent    │  │  生成 Node │  │
│  └────────────┘  └──────┬─────┘  └────────────┘  └──────┬─────┘  │
│                         │                                │        │
│         ┌───────────────┴────────────────────────────────┘        │
│         │    调用协议 (统一 API 契约)                              │
│         ▼                                                         │
│  ┌───────────────────────────────────────────────────┐            │
│  │              OpenCode (代码子流程中间层)           │            │
│  │  ┌──────────┐  ┌──────────┐  ┌────────────────┐  │            │
│  │  │ 代码检索  │  │ 代码生成 │  │ 测试生成 + Lint│  │            │
│  │  │ Session A│  │ Session B│  │   Session C    │  │            │
│  │  └──────────┘  └──────────┘  └────────────────┘  │            │
│  └───────────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 调用方式选型对比

| 方式 | 优点 | 缺点 | 推荐度 |
|------|------|------|--------|
| **HTTP REST API** | 简单通用、跨语言、易调试、OpenCode 部署独立 | 需自己做超时/重试、序列化开销 | ⭐⭐⭐⭐⭐ **首选** |
| MCP (Model Context Protocol) | 工具发现自动、参数校验标准化；**codegraph 原生支持 MCP server** | LangGraph 调 MCP 需桥接层 | ⭐⭐⭐⭐ **可选（codegraph 专用）** |
| Agent Skill（`npx skills add`） | Archify / OpenCode 原生 skill 生态，安装即用 | LangGraph 无法直接触发 skill，得绕到 agent 进程里执行 | ⭐⭐ 仅展示层对接 |
| Python 直接 import | 无网络开销、调用最快 | 强耦合、版本锁定、无法独立部署扩展 | ⭐⭐ 不推荐 |

### 2.3 代码检索/制图后端选型：OpenCode vs CodeGraph(colbymchenry) vs Archify(tt-a1i)

SPEC 原文把「代码检索 + 逻辑图生成 + 代码生成 + 测试生成」全部外包给 OpenCode。经过调研发现：

- **CodeGraph** 是 **Rust 内核的预索引语义知识图谱**，专注 *代码检索 / 调用链追踪 / 影响面分析*，是 SPEC 阶段二需求的最佳实现，比 OpenCode 自带检索更专业、更省 token；
- **Archify** 是 *Agent Skill* 形态的 **架构/流程图渲染器**，专注「把代码库或自然语言描述 → 带验证器的高保真系统架构图（HTML/SVG/PNG）」，刚好替代 SPEC 中「逻辑图展示层（Mermaid）」的纯文本输出；
- **OpenCode** 仍是 *代码生成 / 测试生成 / lint 修复* 这种「写代码」场景的唯一候选，CodeGraph 和 Archify 都不生成代码。

因此三者是**互补关系，不是竞争关系**。下表给出每个 SPEC 阶段的最佳实现：

| 能力需求（对应 SPEC 章节） | OpenCode | **CodeGraph** (colbymchenry) | **Archify** (tt-a1i) | 推荐实现 |
|---|---|---|---|---|
| **阶段二：语义代码检索** / LSP/AST / callers+callees / 影响面分析 / 增量同步 | ⚠️ 有但非核心 | ✅ **原生就是干这个的**（Rust 内核，benchmark 29% fewer tokens，支持多语言，**MCP server + CLI 双模式**） | ❌ 不做 | **CodeGraph** |
| **阶段三：逻辑图展示层**（HTML/SVG/PNG，可交互，架构/流程/时序/数据流/生命周期 5 种图，带 JSON Schema 校验） | ❌ 无 | ❌ 仅 code-level 图谱，不是系统级 | ✅ **原生就是干这个的**（Typed JSON IR + 验证器，单文件 HTML 输出） | **Archify**（替代 Mermaid 作为高端展示） |
| **阶段三：代码生成/修改 + LSP lint 修复 + 子会话** | ✅ 编程 Agent，核心能力 | ❌ 只读 | ❌ 不做 | **OpenCode** |
| **阶段三：测试生成** | ✅ 内置 | ❌ 只读 | ❌ 不做 | **OpenCode** |
| **阶段三：PR 变更影响面预判（CodeGraph Roadmap 预告）** | ❌ | 🔮 官方预告「The CodeGraph platform」将支持 PR 级测试影响分析 | ❌ | 未来接入 CodeGraph |

**结论：三者合体 = 最优解**。原先「垂直增强层 = OpenCode 全部包办」拆成三块：

```
┌──────────────────────────────────────────────────────────────────┐
│                    垂直增强层（多实现 = 各司其职）                  │
│                                                                  │
│  ┌─────────────────────────────────────┐                         │
│  │  代码知识图谱：CodeGraph (Rust)      │ ← 阶段二 代码检索        │
│  │  MCP Server: search/callers/impact  │                         │
│  └─────────────────────┬───────────────┘                         │
│                        ↓ code_context / code_ref                 │
│  ┌─────────────────────────────────────┐                         │
│  │  系统图渲染：Archify (Agent Skill)   │ ← 阶段二/三 展示层      │
│  │  Typed JSON IR → HTML/SVG/PNG 输出   │   替代 Mermaid          │
│  └─────────────────────┬───────────────┘                         │
│                        ↑ logic_graph JSON                        │
│  ┌─────────────────────────────────────┐                         │
│  │  代码生成与测试：OpenCode            │ ← 阶段三 写代码/测代码  │
│  │  /api/v1/code/generate + /tests/     │                         │
│  └─────────────────────────────────────┘                         │
└──────────────────────────────────────────────────────────────────┘
```

#### 2.3.1 配套改造：在 LangGraph 与后端之间增加 `CodeProvider` 适配层（多实现）

原先 LangGraph 节点直接调 OpenCode HTTP，会导致后续接入 CodeGraph/Archify 时每个节点改一遍。
改造方案：定义抽象基类 `CodeProvider`，每个后端实现 1~N 个能力接口；LangGraph 节点只依赖基类，运行时根据 `.env` 配置拼出实际实现。

```python
# devflow/providers/base.py（抽象协议）
class CodeProvider(ABC):
    name: ClassVar[str]

class CodeSearchProvider(CodeProvider, ABC):
    """阶段二：代码检索。对应 SPEC /api/v1/code/search。"""
    @abstractmethod
    async def search(
        self, project_root: str, query_text: str, *,
        query_type: Literal["semantic","symbol","call_chain"]="semantic",
        target_symbols: list[str] | None=None, scope_files: list[str] | None=None,
        max_results: int=20,
    ) -> CodeSearchResult: ...

class CodeGraphRenderProvider(CodeProvider, ABC):
    """阶段二/三：把 logic_graph JSON 渲染为可交付产物。"""
    @abstractmethod
    async def render(self, logic_graph: dict) -> RenderOutput:
        """RenderOutput = {html_bytes?, svg_bytes?, png_bytes?, mermaid_text?}"""

class CodeEditProvider(CodeProvider, ABC):
    """阶段三：代码生成/修改 + lint 自动回修。"""
    @abstractmethod
    async def generate(self, project_root: str, instruction: str, *,
                       logic_graph_node_id: str | None=None,
                       related_files: list[dict], acceptance: list[str],
                       run_lint: bool=True) -> CodeEditResult: ...

class TestGenProvider(CodeProvider, ABC):
    """阶段三：测试生成。"""
    @abstractmethod
    async def generate(self, project_root: str, target_symbols: list[str], *,
                       coverage_target: int=80,
                       modified_branches_only: bool=True,
                       logic_graph: dict | None=None) -> TestReport: ...
```

每种能力可独立选实现：

```ini
# .env 中的 CodeProvider 装配
CODE_SEARCH_PROVIDER=codegraph        # 可选项: codegraph / opencode / mock
CODE_GRAPH_RENDER_PROVIDER=archify    # 可选项: archify / mermaid  / mock
CODE_EDIT_PROVIDER=opencode           # 可选项: opencode / mock
TEST_GEN_PROVIDER=opencode            # 可选项: opencode / mock
```

#### 2.3.2 CodeGraph（colbymchenry）集成方式细节

CodeGraph 官方有**三种**可被我们消费的接口，优先级从高到低：

| 集成方式 | CLI / 协议 | LangGraph 调用点 | 优势 |
|---|---|---|---|
| **A. `codegraph` CLI 子命令（JSON 输出）** | `codegraph search --json <q>`、`codegraph callers --json <sym>`、`codegraph explore --json <task>`、`codegraph node --json <file:sym>`、`codegraph impact --json <sym>`、`codegraph files --json` | `CodeSearchProvider` 通过 `subprocess.run(..., capture_output=True)` → 解析 stdout JSON | 不需要起常驻服务；数据格式最稳定；CodeGraph 自动增量 `.codegraph/` 目录 |
| **B. MCP Server（stdio）** | `codegraph serve --mcp --path <root>` 暴露：`codegraph_search` / `codegraph_callers` / `codegraph_callees` / `codegraph_impact` / `codegraph_context` / `codegraph_explore` / `codegraph_node` / `codegraph_status` | LangChain 的 MCP 桥接 → 转 LangChain Tool | 未来想让 LLM 动态挑工具时最灵活 |
| **C. HTTP MCP Server** | `codemap` (CodeGraph Rust 实现 fork) 原生支持 `codemap serve --port 8080` | HTTP 客户端 | 当前 colbymchenry/codegraph 还没正式有 HTTP，需要等或用 codemap fork |

**阶段二 MVP 采用方案 A（CLI JSON）**：前置条件最少、可离线、可测试。安装步骤：

```bash
# 1. 安装（Linux/Mac，无需 Node.js 也可）
curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh

# 2. 进入项目根目录，初始化索引（一次即可，后续会自动增量同步）
cd /path/to/project-root
codegraph init        # 生成 .codegraph/ 子目录（SQLite Graph DB）

# 3. CLI 子命令用 --json 输出即可被 Python 解析
codegraph node --json src/auth/service.py:AuthService.login
codegraph callers --json AuthService.login
codegraph impact --json AuthService.login
codegraph explore --json "实现 JWT 登录校验的入口函数"
```

#### 2.3.3 Archify（tt-a1i）集成方式细节

Archify 的定位是 **SPEC 逻辑图 = LangGraph 内部结构化 JSON（机读）+ Archify 渲染（人读/分享）**，它**不替代**我们的 `logic_graph` State，只做 `logic_graph → 渲染`。

Archify 输入是自然语言或代码库路径，输出是 **Typed JSON IR（它自己的 Schema）** 然后渲染成单文件 HTML。我们桥接思路：

1. 把 `devflow` 里的 `LogicGraph` TypedDict 转换成 *Archify JSON IR*（字段映射表如下）；
2. 调用 `npx skills use tt-a1i/archify@latest -- agent archify-ir.json`（或直接用它的 Node render CLI）；
3. 拿到输出 HTML / SVG / PNG，与 `logic_graph.mmd` 一起导出。

**字段映射表（devflow LogicGraph → Archify JSON IR 核心节点）**：

| devflow LogicNode/Edge | Archify JSON IR 字段 | 说明 |
|---|---|---|
| `LogicGraph.nodes[].label` | `nodes[].label` | 一致 |
| `LogicNode.node_type: "io"` / `"external"` / `"module"` | `nodes[].role = "frontend"` / `"backend"` / `"database"` / `"external"` / `"security"` | 分类对应到 Archify 预定义 color set |
| `LogicNode.code_ref.file_path` + `:symbol` | `nodes[].source.ref` | Archify 支持 revision-verified source 回链 |
| `LogicEdge.edge_type: "condition"` | `edges[].label = condition 字段` | 条件分支展示在箭头 label 上 |
| `LogicEdge.is_modified=true` | `edges[].diff = "added"` 或 `"modified"` | Archify 原生支持 Before/Delta/After 三色差分图 |
| `LogicGraph.mermaid_source` | 不处理，作为保底 fallback | 小场景用户直接看 Mermaid 也行 |

安装方式：

```bash
# 安装 skills CLI（Node 环境即可）
npm i -g @skills/cli

# 全局安装 Archify skill
npx skills add tt-a1i/archify -g

# 验证安装：在 agent 中输入 "Use archify to map this repo"，产出单文件 HTML archify-map.html
```

> 注：阶段二 MVP 可以先不接 Archify，用我们现有 Mermaid 当展示层，确保 CodeGraph 代码检索先跑通。Archify 作为阶段二增强项在后期接入。

#### 2.3.4 三者的共同问题 & 选型兜底

| 风险 | 说明 | 兜底 |
|---|---|---|
| CodeGraph 的 `--json` 输出格式版本升级 | CLI 子命令 JSON schema 可能变 | `CodeProvider` 适配层内做「字段别名 + 软失败」，老字段找不到就 fallback 到 `opencode` 实现 |
| Archify 需要 Node 环境 + skills CLI | 纯 Python 机器可能没装 | 默认 `CODE_GRAPH_RENDER_PROVIDER=mermaid`，Archify 是可选项 |
| OpenCode 服务 / API 不可用 | 阶段三写代码环节阻塞 | `Mock` provider 只记录输入、输出模板 diff，让阶段二仍能跑通到「逻辑图 + Archify」 |


### 2.4 HTTP API 契约（详细）

#### 2.4.1 代码检索接口

**POST** `/api/v1/code/search`

**请求体** (LangGraph → OpenCode):
```json
{
  "request_id": "uuid-xxx",                // LangGraph 生成，用于追踪
  "thread_id": "project-123",              // 与 LangGraph Checkpoint thread_id 一致
  "session_id": "search-sess-001",         // OpenCode 子会话 ID，首次为空
  "project_root": "/workspace/my-app",     // 代码仓库根目录
  "query": {
    "type": "semantic|symbol|call_chain",  // 检索类型
    "text": "用户登录流程中 JWT 校验逻辑", // 自然语言描述
    "target_symbols": ["AuthService.login"], // 已知符号（可选）
    "scope_files": ["src/auth/*.py"]      // 文件范围（可选）
  },
  "max_results": 20,
  "include_context": true                  // 是否包含周边代码上下文
}
```

**响应体** (OpenCode → LangGraph):
```json
{
  "request_id": "uuid-xxx",
  "session_id": "search-sess-001",        // 保存下来，后续多轮检索复用
  "results": [
    {
      "file_path": "src/auth/service.py",
      "symbol_name": "AuthService.login",
      "line_start": 45,
      "line_end": 78,
      "code_snippet": "def login(self, user, password):\n    ...",
      "relevance_score": 0.95,
      "callers": ["src/api/routes.py:login_handler"],
      "callees": ["src/db/repo.py:find_user"]
    }
  ],
  "summary": "定位到 3 个核心文件：JWT 校验在 AuthService._verify_token，调用链见 results[0].callers"
}
```

---

#### 2.4.2 代码生成 / 修改接口

**POST** `/api/v1/code/generate`

**请求体** (LangGraph → OpenCode):
```json
{
  "request_id": "uuid-yyy",
  "thread_id": "project-123",
  "session_id": "gen-sess-001",
  "project_root": "/workspace/my-app",
  "task": {
    "instruction": "在登录流程中增加短信验证码校验，若用户开启了 2FA 则必须验证",
    "logic_graph_node_id": "node-login-2fa", // 关联逻辑图节点 ID
    "related_files": [
      { "path": "src/auth/service.py", "readonly": false },
      { "path": "src/sms/client.py", "readonly": true }
    ],
    "acceptance_criteria": [
      "不破坏原有账号密码登录逻辑",
      "新增 SMS_CODE_MISSING 错误码",
      "单元测试覆盖率 ≥ 80%"
    ],
    "run_lint_after": true,
    "run_tests_after": false
  }
}
```

**响应体** (OpenCode → LangGraph):
```json
{
  "request_id": "uuid-yyy",
  "session_id": "gen-sess-001",
  "status": "success|partial|failed",
  "changed_files": [
    {
      "path": "src/auth/service.py",
      "action": "modified",                 // created | modified | deleted
      "diff": "--- a/src/auth/service.py\n+++ b/src/auth/service.py\n...",
      "lint_result": { "passed": true, "errors": [] },
      "test_result": null
    }
  ],
  "open_issues": [
    "短信发送频率限制未实现，建议在后续节点补充"
  ],
  "summary": "修改了 AuthService.login 增加 2FA 分支，新增 SMS_CODE_MISSING 错误处理"
}
```

---

#### 2.4.3 测试生成接口

**POST** `/api/v1/tests/generate`

**请求体** (LangGraph → OpenCode):
```json
{
  "request_id": "uuid-zzz",
  "thread_id": "project-123",
  "session_id": "test-sess-001",
  "project_root": "/workspace/my-app",
  "target": {
    "files_or_symbols": ["src/auth/service.py:AuthService.login"],
    "modified_branches_only": true,         // 只测逻辑图中 is_modified=true 的分支
    "logic_graph_ref": "graph-uuid"        // 逻辑图 ID，用于读取分支条件
  },
  "framework": "pytest",
  "coverage_target": 80
}
```

---

#### 2.4.4 通用错误码

| HTTP 状态码 | 业务码 | 含义 | LangGraph 处理方式 |
|-------------|--------|------|--------------------|
| 400 | `INVALID_PARAMS` | 参数错误 | 回退到参数拼装节点重试 |
| 404 | `PROJECT_NOT_FOUND` | project_root 不存在 | 回退到需求澄清，要求用户确认路径 |
| 408 | `SESSION_TIMEOUT` | OpenCode 子会话过期 | 不传 session_id，开新子会话重试 |
| 422 | `LINT_FAILED` | 代码生成但 lint 不通过 | 把错误传回 OpenCode 子会话修复（session_id 不变） |
| 500 | `INTERNAL_ERROR` | OpenCode 内部错误 | 重试 2 次，失败则提示人工介入 |

### 2.5 状态同步协议

```
LangGraph State 字段               OpenCode 子会话
─────────────────────────          ──────────────────
┌────────────────────────┐        ┌────────────────────────┐
│ thread_id: "proj-123"  │───────▶│ 关联 thread_id 归档     │
├────────────────────────┤        ├────────────────────────┤
│ opencode_sessions: {   │        │  各子会话内部状态        │
│   search: "sess-001",  │        │  - 上下文压缩           │
│   code:   "sess-002",  │        │  - 对话历史             │
│   test:   "sess-003"   │◀───────│  - 中间文件             │
│ }                      │        │                        │
├────────────────────────┤        └────────────────────────┘
│ code_context: [...]    │               ▲
│ logic_graph: {...}     │               │ 结果回写
│ code_changes: [...]    │───────────────┘
└────────────────────────┘
```

> **关键原则**: 
> - OpenCode 子会话内部状态**不回传**给 LangGraph，只传结构化结果。
> - LangGraph 始终是**唯一的全局状态权威来源**（Single Source of Truth）。
> - 同一个 thread_id 下，OpenCode 三种子任务各自独立会话，避免上下文污染。

---

## 三、完整架构设计

### 3.1 技术栈分层总览

```
┌──────────────────────────────────────────────────────────────┐
│                        展示层 (可选)                           │
│   Mermaid / Graphviz 可视化  ·  人工审批 UI  ·  进度展示       │
├──────────────────────────────────────────────────────────────┤
│                     全局编排层 (LangGraph)                     │
│  StateGraph 编排  ·  循环/分支路由  ·  Checkpoint 持久化       │
│  人工中断节点 (Interrupt)  ·  压缩节点  ·  校验节点            │
├──────────────────────────────────────────────────────────────┤
│                     基础能力层 (LangChain)                     │
│  多模型封装  ·  消息格式  ·  Tool 桥接  ·  JSON Schema 输出    │
├──────────────────────────────────────────────────────────────┤
│                   垂直增强层 (OpenCode - 子流程)                │
│  代码检索  ·  代码生成  ·  测试生成  ·  LSP/AST  ·  会话压缩    │
├──────────────────────────────────────────────────────────────┤
│                         工具层                                 │
│  tree-sitter · Chroma · GitPython · SQLite/Redis · Pytest     │
├──────────────────────────────────────────────────────────────┤
│                         模型层                                 │
│  LLM (OpenAI / 豆包 / Claude)  ·  Embedding 模型               │
└──────────────────────────────────────────────────────────────┘
```

### 3.2 LangGraph 节点工作流图

```
                     ┌─────────────────┐
                     │  用户输入需求     │
                     └────────┬────────┘
                              ▼
                     ┌─────────────────┐
              ┌──────│  需求信息校验     │◀───────────────┐
              │      └────────┬────────┘                │
              │               │ 信息缺失                 │ 补充后
              │               ▼                          │ 再校验
              │      ┌─────────────────┐                │
              │      │  生成定向追问    │───────────────▶┘
              │      └─────────────────┘
              │
              │ 信息完备
              ▼
                     ┌─────────────────┐
                     │  压缩节点(可选)  │ 旧对话 → 摘要
                     └────────┬────────┘
                              ▼
                     ┌─────────────────┐
                     │ OpenCode 代码检索│ ────▶ API /api/v1/code/search
                     └────────┬────────┘
                              │
                  ┌───────────┼───────────┐
                  │ 检索成功             │ 检索失败/结果不足
                  ▼                       ▼
          ┌─────────────────┐     ┌─────────────────┐
          │ 生成可机读逻辑图 │     │ 回退需求澄清节点 │
          │  (结构化JSON)   │     │ 索要具体文件路径 │
          └────────┬────────┘     └─────────────────┘
                   ▼
          ┌─────────────────┐
          │  逻辑图校验节点  │ → code_ref 缺失? 分支条件缺失?
          └────────┬────────┘
                   ▼
          ┌─────────────────────────────────────────┐
          │              条件分流                    │
          │  修改现有代码  │  新增功能  │  纯文档    │
          └──────┬────────┴────┬───────┴──────┬─────┘
                 ▼             ▼              ▼
        ┌─────────────────────────┐  ┌────────────┐
        │  OpenCode 代码生成/修改 │  │ 输出设计文档│
        │  /api/v1/code/generate  │  └────────────┘
        └────────────┬────────────┘
                     ▼
              Lint / TypeCheck
              通过? │ ┌──┐
               ┌────┴─┘  │ 不通过 → 回 OpenCode 修复 (session_id 复用)
               ▼          └──────────────────────┐
        ┌─────────────────┐                       │
        │ OpenCode 测试生成│◀──────────────────────┘
        │ /api/v1/tests/  │
        └────────┬────────┘
                 ▼
        ┌─────────────────┐
        │  运行测试 + 报告 │
        └────────┬────────┘
                 ▼
        ┌─────────────────┐
        │  人工验收节点    │ ← 人工中断 (Interrupt)
        └────────┬────────┘
                 ▼
        ┌─────────────────┐
        │   流程结束 ✓    │
        └─────────────────┘
```

### 3.3 全局 State 结构定义 (TypedDict)

```python
from typing import TypedDict, Annotated, Literal, Optional
from langgraph.graph.message import add_messages

class RequirementSchema(TypedDict):
    """需求信息完备性清单 - 所有字段为结构化产物，不靠原始对话解析"""
    req_type: Literal["new_feature", "component_iteration", "bug_fix"]  # 需求类型
    project_root: str                                                      # 项目根路径
    project_context: str                                                   # 项目背景简述
    target_modules: list[str]                                              # 涉及模块
    existing_code_accessible: bool                                         # 现有代码是否可访问
    reference_files: list[str]                                             # 参考文件路径
    io_constraints: dict[str, str]                                         # 输入输出约束 {input:..., output:...}
    edge_cases: list[str]                                                  # 边界场景
    acceptance_criteria: list[str]                                         # 验收标准

class CodeRef(TypedDict):
    """代码引用绑定"""
    file_path: str
    symbol: Optional[str]           # 如 "AuthService.login"
    line_start: int
    line_end: int

class LogicNode(TypedDict):
    """逻辑图节点"""
    node_id: str
    label: str
    node_type: Literal["function", "module", "condition", "io", "external"]
    code_ref: Optional[CodeRef]
    is_modified: bool               # 是否本次需求涉及修改
    input_spec: Optional[dict]
    output_spec: Optional[dict]

class LogicEdge(TypedDict):
    """逻辑图边"""
    edge_id: str
    from_node: str
    to_node: str
    edge_type: Literal["call", "data_flow", "condition"]
    condition: Optional[str]        # 条件分支时的判断描述
    is_modified: bool

class LogicGraph(TypedDict):
    """可机读逻辑图 - 核心产物"""
    graph_id: str
    nodes: list[LogicNode]
    edges: list[LogicEdge]
    mermaid_source: str             # 仅用于展示，不作为程序输入

class OpenCodeSessions(TypedDict):
    """OpenCode 子会话 ID 映射"""
    search: Optional[str]
    code_gen: Optional[str]
    test_gen: Optional[str]

class CodeChange(TypedDict):
    """代码修改记录"""
    file_path: str
    action: Literal["created", "modified", "deleted"]
    diff: str
    lint_passed: bool
    test_passed: Optional[bool]

class GlobalState(TypedDict):
    """LangGraph 全局状态黑板 = 唯一信息传递总线"""
    # 1. 对话层 (热记忆，仅保留最近 3~5 轮)
    messages: Annotated[list, add_messages]
    
    # 2. 结构化业务数据 (温记忆，全程参与推理)
    requirement: RequirementSchema
    code_context: list[dict]          # OpenCode 检索结果
    logic_graph: Optional[LogicGraph] # 核心逻辑图
    code_changes: list[CodeChange]    # 代码修改记录
    test_report: Optional[dict]       # 测试报告
    
    # 3. 子系统会话映射
    opencode_sessions: OpenCodeSessions
    
    # 4. 流程控制字段
    current_stage: Literal["clarify", "search", "graph", "code", "test", "review", "done"]
    missing_fields: list[str]         # 校验节点输出的缺失字段列表
    last_error: Optional[str]         # 最近错误，用于重试
    retry_count: dict[str, int]       # 各节点重试次数计数
```

### 3.4 节点输入输出契约表

| 节点 | 读取字段 | 写入字段 | 失败路由 |
|------|----------|----------|----------|
| 需求信息校验 | `messages`, `requirement` | `requirement`, `missing_fields`, `current_stage` | `missing_fields≠[]` → 生成定向追问 |
| 生成定向追问 | `missing_fields`, `requirement` | `messages` | (LLM 调用失败重试 2 次) |
| 压缩节点 | `messages` | `messages` (被替换为摘要) | 无（跳过即可） |
| OpenCode 代码检索 | `requirement`, `opencode_sessions.search` | `code_context`, `opencode_sessions.search` | 检索为空 → 需求澄清 (索要文件路径) |
| 生成可机读逻辑图 | `requirement`, `code_context` | `logic_graph` | 格式校验失败 → 重新生成 |
| 逻辑图校验节点 | `logic_graph` | `last_error` | code_ref/修改标记缺失 → 回退制图 |
| OpenCode 代码生成 | `logic_graph`, `requirement`, `opencode_sessions.code_gen` | `code_changes`, `opencode_sessions.code_gen` | lint 失败 → 传错给 OpenCode 同 session 修复 |
| OpenCode 测试生成 | `logic_graph`, `code_changes`, `opencode_sessions.test_gen` | `test_report`, `opencode_sessions.test_gen` | 覆盖率不达标 → 回 OpenCode 补测试 |
| 人工验收 | `code_changes`, `test_report`, `logic_graph` | (等待人工输入 approve/reject) | reject → 回代码生成阶段 |
| 流程结束 | 全部 | (产出归档) | — |

---

## 四、分阶段实施规划

### 阶段一：MVP 核心骨架（工作量预估：3~5 天）

**目标**: 验证 LangGraph 循环追问 + 状态持久化 + 逻辑图生成的基本可行性

| 任务 | 交付物 | 是否对接 OpenCode |
|------|--------|-------------------|
| State 结构定义 + Checkpoint (SQLite) | `src/state.py` | ❌ |
| 需求澄清循环（校验 + 追问节点） | `src/nodes/clarify.py` + Schema 定义 | ❌ |
| 可机读逻辑图生成节点 + 校验节点 | `src/nodes/graph.py` + `graph_schema.py` | ❌ |
| 压缩节点（结构化提取策略） | `src/nodes/compress.py` | ❌ |
| 主 Graph 编排 + CLI 入口 | `src/main.py` | ❌ |
| **测试用例** | `tests/stage1_mvp.md` | — |

**通过标准**: 
- 能模拟 5 轮以上追问，信息补全后自动进入下一环节
- 服务重启后通过 thread_id 能断点续跑
- 生成的逻辑图 JSON 通过 Schema 校验，且能渲染 Mermaid

### 阶段二：OpenCode 对接 + 代码检索（工作量预估：2~3 天）

**目标**: 验证 LangGraph ↔ OpenCode 调用协议

| 任务 | 交付物 |
|------|--------|
| OpenCode HTTP 客户端封装（超时/重试/错误码处理） | `src/clients/opencode.py` |
| 代码检索节点对接 OpenCode `/api/v1/code/search` | `src/nodes/code_search.py` |
| 检索失败 → 回退需求澄清的路由 | 主 Graph 路由更新 |
| 检索结果自动绑定逻辑图节点的 code_ref | 制图节点逻辑更新 |
| **测试用例** | `tests/stage2_opencode.md` |

**通过标准**:
- 给定一个带代码的测试项目，能正确检索到目标函数并绑定 code_ref
- session_id 正确复用，多轮检索上下文不丢失

### 阶段三：代码生成 + 测试全链路打通（工作量预估：4~6 天）

**目标**: 端到端跑通需求→制图→代码→测试

| 任务 | 交付物 |
|------|--------|
| 代码生成/修改节点对接 OpenCode `/api/v1/code/generate` | `src/nodes/code_gen.py` |
| Lint 失败 → 自动回 OpenCode 同 session 修复循环 | 路由 + 状态维护 |
| 测试生成节点对接 `/api/v1/tests/generate` | `src/nodes/test_gen.py` |
| 人工验收节点 (LangGraph Interrupt) | `src/nodes/review.py` |
| Mermaid 可视化 + 状态展示 (可选简单 Web UI) | `src/ui/` |
| **测试用例** | `tests/stage3_e2e.md` |

**通过标准**:
- 对一个测试项目（比如「给 Flask 登录接口加 2FA」）能端到端跑通
- 产出物：需求清单 JSON + 逻辑图 JSON/Mermaid + 代码 diff + 测试文件

---

## 五、弹性与容错：大模型调用 / 外部依赖异常处理

所有会调用外部服务的「两层」——**LangChain LLM 调用层**（clarify_extract / clarify_build_question / graph_generate 等节点）和 **CodeProvider 适配层**（OpenCode HTTP / CodeGraph CLI / Archify CLI）——都可能出现异常。本章节定义**统一异常分类 → 统一处理管线（重试/降级/熔断/死信）→ 异常到路由映射**，确保每一次失败都被收敛到可预测的 State 字段，而不是让 LangGraph graph 直接抛未捕获异常。

### 5.1 异常分类与是否可重试（核心矩阵）

所有错误统一用 `devflow.errors.DevFlowError`（含字段：`code`、`retryable`、`category`、`message`、`retry_after_sec`、`cause`）携带，不再让 `HTTPError/JSONDecodeError/TimeoutError/KeyError` 之类裸异常浮出节点层。

| Category（category 字段） | 典型触发（举例） | HTTP/错误码特征 | retryable？ | 兜底策略 |
|---|---|---|---|---|
| **LLM.CONTEXT_OVERFLOW** | LangChain 抛 `context_length_exceeded` / OpenAI 400 `max_tokens is too large` / 供应商返回 `PromptTooLong` | 4xx，响应体含 `context_length_exceeded` / `model_max_length` 字样 | ⚠️ **降级重试**：不是重打一次，而是主动做「系统 prompt 不变，历史 messages 截断 + code_context 采样 + logic_graph 只留摘要」后再打 | 压缩到 < `LLM_CONTEXT_WINDOW * 0.85`，最多压缩 3 次仍失败就走 `LlmDegradeError` → 路由到人工节点 |
| **LLM.REFUSED** | 供应商拒绝服务：401/403 鉴权失败、`Invalid API Key`、`Provider quota exceeded`、`You exceeded your current quota`、`Your account is not approved`、`content filter`（安全策略拒绝） | 401 / 403 / 429 `insufficient_quota` / 400 `content_filter` | ❌ **不可重试**（同一模型重试只会继续被拒） | 立刻切 `LLM_FALLBACK_MODEL`（豆包→ Claude→ 本地 vLLM→ 最终 MockProvider 返回模板兜底）；所有 fallback 都失败时路由到人工介入节点 |
| **LLM.RATE_LIMIT** | 429 Rate limit exceeded / 并发数超限 / `retry_after` 有值 | 429，Header 可能有 `Retry-After` | ✅ **可重试（指数退避 + jitter，服从 Retry-After）** | `RetryPolicy(max_attempts=5, base_backoff=1.0, use_jitter=True, respect_retry_after_header=True)`；5 次仍超限就降级模型 |
| **LLM.UPSTREAM** | 5xx（网关超时、上游维护、Unprocessable、暂时不可用）、TLS 握手失败、DNS 解析失败 | 500 / 502 / 503 / 504 或 `aiohttp.ClientError`/`httpx.RemoteProtocolError` | ✅ **可重试（3 次 + 快速失败 1s/3s/10s）** | 3 次仍失败 → 降级到下一个 fallback 模型 |
| **LLM.OUTPUT_FORMAT** | LLM 输出不是合法 JSON / JSON 不符合 Schema / Pydantic 解析失败 | — | ✅ **可重试（最多 2 次，带「错误说明 + 请按格式重输出」的额外 user message 引导）** | 2 次不行走 Mock（返回 empty_requirement / 空 logic_graph）并提示人工 |
| **HTTP.NETWORK** | OpenCode HTTP 连接失败、连接超时、EOF、SSL、CERTIFICATE_VERIFY_FAILED | `httpx.ConnectError` / `ConnectTimeout` / `ReadTimeout` | ✅ **可重试（3 次，指数退避）** | 3 次失败 → 有 fallback（OpenCode→Mock）就走 fallback，否则路由节点记录 `last_error` |
| **HTTP.AUTH** | OpenCode 401 / 403 | 401 / 403 | ❌ **不可重试** | 直接抛错给人工检查 `OPENCODE_API_TOKEN` / baseUrl |
| **HTTP.REQ_INVALID** | 400 Bad Request（参数非法、字段缺失） | 400，`INVALID_PARAMS` | ❌ **不可重试**（再打也错） | 记录 payload，抛错让节点修正参数拼装 |
| **HTTP.LINT_FAILED** | SPEC 2.4.4 422 LINT_FAILED（业务级错误） | 422 `LINT_FAILED` | ⚠️ **可重试（限 1 次，把 lint 错误原文作为上下文回传给 OpenCode 子会话修复）** | 2 次 lint 仍不过 → 用户节点展示 lint 报告 |
| **HTTP.SESSION_TIMEOUT** | SPEC 2.4.4 408 SESSION_TIMEOUT（子会话过期） | 408 | ✅ **可重试（自动传空 session_id，开新子会话）** | 必能恢复；不计入全局重试次数，因为这是幂等开新会话 |
| **CLI.NOT_FOUND** | CodeGraph / Archify 二进制不在 PATH（`FileNotFoundError`）| `errno.ENOENT` | ❌ **不可重试** | 走已配置 fallback：CodeGraph→ MockCodeSearch，Archify→ Mermaid 保底 |
| **CLI.TIMEOUT** | `codegraph explore --json` 因为要本地 LLM 所以超 30s | asyncio.TimeoutError / returncode=-9 | ✅ **可重试（最多 2 次，timeout 从 30s→60s）** | 2 次都超时 → fallback 到 `codegraph search`（无 LLM，纯向量）|
| **CLI.INDEX_MISSING** | CodeGraph 报错 `.codegraph` 不存在（首次使用） | stderr 含 `.codegraph` | ✅ **可重试（最多 1 次：自动执行 `codegraph init` 子命令建索引后再跑原命令）** | 还失败就 fallback Mock |
| **NODE.CONTEXT** | 节点内部断言失败 / 关键 State 字段缺失 | `KeyError/AssertionError` | ❌ **不可重试**（程序 bug，打多少遍都错） | 记录 last_error + retry_count，路由人工节点 |

### 5.2 统一重试策略：`RetryPolicy` + 指数退避 + Jitter

所有可重试错误一律通过 `devflow.resilience.retry_with_backoff(policy=RetryPolicy(...))` 装饰器执行，避免每个节点手写 for 循环。默认参数：

```python
RetryPolicy(
    max_attempts=3,               # 最多 3 次（第 1 次是初次，失败后再重试 2 次，所以总请求 = 3）
    base_backoff=1.0,             # 第 1 次失败等 1s
    max_backoff=60.0,             # 最长不超 60s
    multiplier=2.0,               # 指数乘数：1 → 2 → 4 → 8 ...
    use_jitter=True,              # ±25% 抖动，避免羊群效应
    respect_retry_after_header=True,  # LLM.RATE_LIMIT 时优先用 HTTP Retry-After
    timeout_per_attempt=None,     # 单次超时；None 表示调用方控制
    deadline_total=None,          # 从首次调用算起的总 deadline，避免无限累积
    retryable_codes=default_retryable,  # 只对 category ∈ 上表 ✅/⚠️ 的码重试
)
```

**死锁超时（Deadline）**：每个大的子流程（需求澄清、制图、代码生成、测试生成）都设独立 deadline；`deadline_total` 一到就不重试直接降级，避免「重试了 20 分钟仍在转圈」。默认值：澄清节点 3min，制图 5min，代码生成/测试各 8min。

### 5.3 LLM 侧的多模型 Fallback 链

在 `llm_client` 中维护一个「主模型 + 按错误类型切的 fallback 队列」：

```ini
# .env
LLM_PRIMARY=openai/gpt-4o-mini          # 主
LLM_FALLBACKS=zhipu/glm-4,anthropic/claude-sonnet,local/qwen2.5:7b,mock
# mock 是最后兜底，永远不失败（返回 empty_requirement / 模板 logic_graph）
```

切换规则：
- 遇 **LLM.REFUSED / LLM.UPSTREAM 3 次仍失败**：从队头取下一个模型，在该节点的 `retry_count` 中记录 `llm_switch`
- 切一次模型就**清空累积的退避时间**，从 1s 重新开始算
- 所有真实模型耗尽才落到 `mock`；走 mock 时 `logic_graph._render_backend = "mock-llm"` 这种 tag，方便后期排查

### 5.4 熔断（Circuit Breaker）：避免把坏掉的服务一直打到死

对 OpenCode HTTP / CodeGraph CLI / Archify CLI 三个外部依赖各独立维护一个熔断器（`devflow.resilience.CircuitBreaker`）。

状态机：
- **Closed**：正常；统计窗口内失败率 ≥ 50% → 切 Open
- **Open**：持续 `open_window=30s`，所有调用**直接返回错误**不打外部；期间节点走 fallback
- **Half-Open**：窗口到期后放 1 个探测请求；成功切 Closed，失败回 Open

目的：
- 避免 OpenCode 宕机 1 分钟，我们每分钟打 60 次；
- 避免 CodeGraph CLI 超时占住进程内 async 线程；
- 给外部依赖自愈的时间。

### 5.5 Token 预算（TokenBudget）：防止「一轮对话把一天配额烧完」

全局 `LLM_TOKEN_BUDGET_DAILY`（默认不启用，配了就生效）：

- 每次 `invoke_json` 结束后从响应中扣 `usage.prompt_tokens + usage.completion_tokens`
- 当 24h 内消耗超过预算的 **70%**：所有新请求自动降级到最便宜的 fallback 模型（或小一号 context 窗口）
- 超过 **95%**：除了 `clarify_validate` / `graph_validate` 这种不打 LLM 的节点，其他节点一律走 `mock`，在 `last_error` 中写入 **TOKEN_BUDGET_EXHAUSTED**，等次日 0 点自动恢复或人工解锁

### 5.6 错误写入 State 的格式 & 路由分支扩展

节点统一的错误捕获模式：

```python
try:
    await llm.invoke_json(...)
except DevFlowError as e:
    retry = state.get("retry_count", {}) or {}
    retry["clarify_extract"] = retry.get("clarify_extract", 0) + 1
    return {
        "last_error": f"[clarify_extract:{e.code}] {e.message}",  # 含 code 便于路由
        "last_error_code": e.code,
        "last_error_retryable": e.retryable,
        "retry_count": retry,
    }
```

路由新增 4 个分支（条件见 `_route_after_last_error`）：

| last_error_code ∈ | 路由 |
|---|---|
| `LLM.RATE_LIMIT`、`LLM.UPSTREAM`、`HTTP.NETWORK`、`CLI.TIMEOUT`、`CLI.INDEX_MISSING`（retryable=True 且 retry_count<阈值） | → 重新进入原节点重试 |
| `LLM.CONTEXT_OVERFLOW` | → 先进入 `compress_messages` 强制压缩，再回到原节点 |
| `LLM.REFUSED`、`HTTP.AUTH`、`CLI.NOT_FOUND`（retryable=False） | → 进入 `human_interrupt` 人工节点（Interrupt）展示具体报错 |
| `LLM.OUTPUT_FORMAT` 超 2 次 / `TOKEN_BUDGET_EXHAUSTED` / 熔断 OPEN | → 进入 `degrade_to_mock` 临时降级节点（把 mock 结果写回 State，打降级标签） |

### 5.7 死信 & 可观测

- 所有最终未被重试处理掉的 `DevFlowError`，连同当时 State 快照（不含 messages 原文，避免 PII），以 JSON Lines 追加写到 `./data/dead_letter/{YYYY-MM-DD}.jsonl`
- 每次 LLM / HTTP / CLI 调用，结构化日志：`category, code, retryable, duration_ms, attempt_index, final_outcome`，供 Prometheus/ELK 统计 TOP 失败原因

---

## 六、风险评估与兜底方案

| 风险 | 概率 | 影响 | 兜底方案 |
|------|------|------|----------|
| LLM 追问发散或格式错误 | 中 | 需求字段缺失 | JSON Schema 强制输出 + 校验节点二次拦截，格式错误直接抛错让 LLM 重输出 |
| OpenCode 服务不稳定 / 接口变更 | 中 | 代码环节断流 | 抽象 `CodeProvider` 接口，OpenCode 只是一个实现；紧急时可切到「人工确认 + 文件 diff 手动应用」模式 |
| 逻辑图生成不符合实际代码结构 | 中高 | 下游代码生成错误 | 逻辑图校验节点 + OpenCode 代码生成前做「图 vs 实际代码」一致性检查，不一致则重新检索 + 重画图 |
| 状态膨胀，token 超限 | 低 | LLM 调用失败 | 结构化提取压缩策略（首选）+ 每环节结束自动归档原始对话 |
| Checkpoint 存储损坏 | 低 | 会话丢失 | SQLite 做每日备份；另可导出 State JSON 做冷备份 |

---

## 七、待确认问题（已确认 ✅）

1. **技术栈确认**：Python (LangChain/LangGraph) + OpenCode HTTP 调用 ✅ 已确认
2. **OpenCode 部署方式**：暂用 Mock 接口开发，后续按需对接真实 OpenCode 服务
3. **模型选型**：**DeepSeek**（已配置到 config.py，通过 `LLM_BASE_URL` + `LLM_MODEL=deepseek-chat`）
4. **持久化存储**：**SQLite3**，MVP 及后续均使用，暂不上 Redis/Postgres
5. **UI 需求**：纯 CLI / API，暂不做 Web 可视化界面
6. **Scope 确认**：三个阶段按文档全部实现

### 7.1 需求文档读取：支持 .docx 格式（新增）

用户需求可能以 Word 文档（.docx）形式提供。集成 Anthropic skills 的 [docx skill](https://github.com/anthropics/skills/blob/main/skills/docx/SKILL.md) 能力，在 CLI 入口提供 `--from-doc <path>` 参数，自动提取 .docx 文本作为初始需求输入。

**实现方案**：
- `devflow/doc_reader.py`：使用 `python-docx` 库读取 .docx 文件，提取纯文本（段落 + 表格），输出结构化文本
- CLI `devflow new --from-doc requirements.docx`：读取文档内容，作为第一条 `HumanMessage` 喂给 LangGraph
- 支持 .docx / .txt / .md 三种格式，.docx 走 python-docx，其余直接读文本

**安装依赖**：
```bash
pip install python-docx>=1.1.0
```

---

## 八、实施补记（2026-09）：执行闭环与真实后端定位

### 8.1 执行闭环（已落地）

阶段三原缺口：code_gen 只产出 diff 不落盘，test_gen 只产出场景设计（`run.passed` 是场景数）。
现已补全为真实闭环：

```
code_gen ──lint_ok──▶ apply_code ──▶ test_gen ──▶ test_run ──test_ok──▶ 人工验收
   ▲                    │                          │
   │   diff 坏了（≤1 次）│            failed>0（≤TEST_RUN_MAX_FIX_ROUNDS）
   └────────────────────┴──────────────────────────┘
```

- **apply_code**（`devflow/code_apply.py` + `devflow/nodes/test_run.py`）：unified diff 解析
  （容行号漂移 / a-b 前缀 / new/delete / `\ No newline`）→ all-or-nothing 预检 → 备份到目标项目
  `.devflow_backup/<时间戳>/`（含 manifest.json）→ 落盘；失败自动回滚。`content_after` 整文件
  直写仅允许用于新建文件（防 mock 内容覆盖真实源码）。落盘失败不 abort：回炉一次后降级为
  「仅设计」模式继续，由人工验收兜底。
- **test_run**（`devflow/test_runner.py`）：目标项目内子进程跑 `pytest --junitxml`，解析回填
  真实 `run`（passed/failed/errors/skipped/时长/失败明细/日志尾部）。失败摘要（`test_failure`）
  回传 code_gen 的 instruction 针对性修复；连续失败超限后带失败报告进人工验收（循环有界收敛）。
  未收集到用例 ≠ 通过；执行被跳过（开关关闭 / 项目不可用 / 超时）时报告带 skip_reason，不误判。
- **开关**（.env）：`APPLY_CODE_ENABLED` / `TEST_RUN_ENABLED` / `TEST_RUN_TIMEOUT_SEC` /
  `TEST_RUN_MAX_FIX_ROUNDS` / `TEST_RUN_PATHS`。
- 顺带修复：`dead_letters` 此前未在 GlobalState 声明，LangGraph 静默丢弃该键，死信从未真正
  进入 drain 节点；现已声明，死信 JSONL 落盘恢复生效。

### 8.2 真实后端定位（OpenCode / Pi / CodeGraph / Archify = 可选增强）

外部后端的 Provider 适配层（含熔断、错误码映射、Mock 回退）均已实现，但默认主路径不依赖它们：

| 能力 | 默认主路径 | 可选增强后端 |
|---|---|---|
| 代码检索 | LLM 直连（graph_gen 生成目标锚点）+ 语义兜底 | `CODE_SEARCH_PROVIDER=codegraph`（需本地安装并 `codegraph init`）；`pi`（无索引，agent 翻文件式检索，慢） |
| 制图渲染 | Mermaid 文本（Web 端渲染） | `CODE_GRAPH_RENDER_PROVIDER=archify`（需 node ≥ 18） |
| 代码生成 | LLM 直连产出 diff + lint 回修 | `CODE_EDIT_PROVIDER=opencode`（需本地 OpenCode Server）或 `pi`（pi CLI 子进程） |
| 测试生成/执行 | llm_testgen 场景设计 + 本地 pytest 真实执行 | `TEST_GEN_PROVIDER=opencode`（远程执行）或 `pi`（CLI 内写用例并执行） |

> **定位说明（给用户的边界声明）**：DevFlow 只做**编排**——需求澄清、逻辑图、三门禁评审、
> 全局状态权威（Single Source of Truth）、断点续跑与产物交付；OpenCode / Pi 等外部 Agent 是
> **可替换的执行器**，用来接入专业 skill 与「代码检索 → 修改 → lint / 测试修复」轮询，直到
> 产出可接受的产物再以结构化结果回传（子会话内部状态不回传，见 2.5）。因此：
> ① 换 Agent / 换版本 / 换模型只影响 Provider 配置，编排、门禁与 checkpoint 不变，新 Agent
> 只需实现同一套 Provider 接口即可接入；② 同一份需求可换后端各跑一遍，在相同门禁与产物
> 结构下横向对比不同 Agent 的实际效果，为选型提供依据。

对接真实后端前先跑 `devflow check-providers` 自检；每次真实后端验证结论以 `tests/` 下报告文档为准。
未配置任何后端时全流程可用（Mock 降级 + LLM 直连），这是演示与 CI 的基线。

---

> **评审说明**：以上方案已通过用户评审，进入编码阶段。
