# QUICKSTART · 5 分钟上手

> 前置：Python ≥ 3.11。本指南对应当前版本的 Web Shell 与 CLI；架构细节见 [SPEC.md](SPEC.md)。

## 0. 首次启动引导（推荐）

```bash
python3 -m devflow.cli setup
```

向导自动完成首启检测与配置，全程可随时回车跳过、绝不阻塞启动：

1. **检测** `.env` 是否存在、LLM Key 是否仍是占位符；
2. **探测** mock 以外的服务后端（OpenCode / Pi / CodeGraph / Archify）；
3. **多选安装**缺失服务——CodeGraph 从 GitHub Releases 检测最新版本并下载安装（SHA256
   校验）；Pi 走 npm 全局安装（`npm install -g @mariozechner/pi-coding-agent`，装完需先
   运行一次 `pi` 完成模型登录）；网络超时 / 失败 / npm 缺失时打印官方安装指引并跳过该步骤，
   之后可自行安装；
4. **配置 `.env`**：按已安装的服务写入各能力后端选择（只改示例默认值，不覆盖你的自定义配置）；
5. **收尾指引**：按要求自行填入 LLM 供应商 Key 后，运行测试指令
   （`devflow check-providers` / `devflow check-llm`），再启动服务（见下文）。

只想看检测报告不交互：`python3 -m devflow.cli setup --check`。
Web 服务启动时若未检测到 `.env`，也会打印一次性 Mock 模式提示（不影响启动）。

## 1. 安装

```bash
cd spec2autotest
pip install -r requirements.txt
```

## 2. 启动 Web UI

```bash
python3 -m uvicorn web.server:app --port 8100
```

打开 **http://127.0.0.1:8100**。若 8000 等端口被其他项目占用，换任意空闲端口即可（下文以 8100 为例）。

启动后右上角有一个配置状态徽章：

- **Mock 模式**（未配置 Key）：全流程可跑通，LLM 输出为演示数据，适合先熟悉交互；
- **Provider 名称**：已配置真实 Key，输出为真实 LLM 结果。

## 3. 走通第一个全流程

Web 界面只有**一个输入入口**（底部输入框）：

1. **输入需求**：直接描述需求后回车；或点中间空态的「✦ 用示例需求试试」填充计算器示例；
   也可以点输入框左侧 **⇪** 导入 `.md` / `.txt` / `.docx` 需求文档，或直接把文件拖到输入框上。
2. **⚙ 运行配置**（可选）：输入框右侧 ⚙ 可设置 project_root、目标模块、边界场景、验收标准等；
   这些字段**仅在新会话创建时生效**，已设置项会以 chips 显示在输入框上方，可逐个删除。
3. **需求澄清**：AI 会针对缺失信息追问，在输入框里回答即可；顶部步骤条实时显示当前阶段。
   - 需求**有缺口**时：首轮追问下方会弹**澄清方式选择卡**——「🧠 头脑风暴」逐条探讨（想法还模糊时用，
     每问附候选方向与推荐，可回「你来定」让 AI 拍板）或「🔥 拷问」逐题深挖（需求已成型时用，
     每问附推荐答案，回「同意」直接采纳）；不选也行，直接在输入框补充内容即可；
     之后随时可用指令切换，「退出头脑风暴 / 退出拷问」回到普通列表模式；
     这两种模式下轮次上限更宽（`CLARIFY_DIALOG_MAX_ROUNDS`，默认 12）。
   - 需求**一次说清**时：会收到「需求已足够清晰，无需再发散」的确认，直接进入制图；
     缺口补齐的那一刻也会收到「缺口已补齐」确认。
4. **需求确认（门禁 1/3）**：澄清完备后、制图前弹出确认窗，把需求字段逐项摊开。
   - 每个字段标注来源：**「AI 推断」= 模型从描述里提炼/脑补的内容**（如你只说了大概流程、AI 补出的验收标准），
     需重点核对；没有「AI 推断」标记说明该字段来自你的原话；
   - **只有存在 AI 推断字段时才会弹这个窗**——全字段都出自你的原话时直接放行，不打扰；
   - 可就地修改任意字段（多行字段每行一项），点「确认需求」后**只有被改动的字段**会写回并把来源记为你的确认；
   - 也可点「驳回，先补充需求」——本轮结束，补充描述后重新澄清，再走一次确认。
5. **制图评审（门禁 2/3）**：逻辑图生成后弹出评审窗，左侧是需求对照、右侧是图预览。
   - 点「查看完整产物」可收起弹窗，到主视图放大检查（滚轮缩放、拖拽平移、点节点看详情）；
   - 有问题点「驳回」并**填写修改意见**——意见会回传给制图节点自动重制图；
   - 没问题点「通过」进入下一阶段。
6. **代码检索 → 代码生成 → 测试设计**：自动推进，每个节点完成都会显示耗时；代码变更以 diff 视图展示。
   **仅需求模式**：如果不提供项目代码（⚙ 配置里不填 project_root，或澄清时说明没有现有代码），
   制图评审通过后会**跳过代码检索与生成**，直接基于需求 + 逻辑图设计**端到端测试用例**，
   步骤条上这两个阶段显示为划掉；人工验收驳回时会回到测试用例设计（而不是代码生成）。
7. **人工验收（门禁 3/3）**：展示变更文件数、测试通过 / 失败、覆盖率等统计；同样可驳回带意见回修。
8. **导出交付物**：验收通过后，测试场景表支持按层级 / 优先级 / 关键词筛选，导出 **CSV（Excel 可直接打开）/ Markdown**、一键复制；底部「导出交付物」条可打包整会话的 **Markdown 汇总 / JSON**。

其他常用操作：

- **会话管理**：左侧「历史会话」点任一会话即可恢复完整上下文（对话、产物、阶段，挂起中的门禁也会弹回）；悬停点 ✕ 删除（二次确认）；
- **＋ 新会话**：回到空态，随时开新一轮；
- **终止 / 继续**：流程推进中输入框右侧的发送键（↑）会变成 **■ 终止**，点击即请求终止——LLM 生成阶段几乎立即停下，真实测试执行这类长节点会等当前节点跑完；终止后进度不丢，发送键变绿色 **⏵ 继续**，点击从断点续跑，刷新页面也能恢复该状态；想换方向可用对话流里的「⟲ 从此重来」回退；
- **◐ 主题**：右上角切换暗 / 亮双主题，自动记住偏好。

## 4. 配置真实 LLM

```bash
cp .env.example .env
```

编辑 `.env`，任选一种方式：

**方式 A：多 Provider fallback 链（推荐）**——编辑 `LLM_PROVIDERS_JSON`，填入 DeepSeek / SiliconFlow / 自建 vLLM 等 OpenAI 兼容服务的 `base_url` + `api_key` + `model`；

**方式 B：单 Provider**——直接填 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。

相关可选项：`LLM_USE_MOCK_FALLBACK=1`（真实服务全部失败时兜底为 Mock，开发期建议开启）、`LLM_TOKEN_BUDGET_DAILY`（日 token 预算，0 不限）。

改完重启服务生效。自检：

```bash
python3 -m devflow.cli check-providers   # 各后端可用性一键自检
python3 -m devflow.cli check-llm         # LLM 连通性检查
```

> ⚠️ 仓库历史提交中出现过的测试 Key 已吊销，不可使用，请配置自己的 Key。

### 可选：接入外部编码 Agent（OpenCode / Pi）

DevFlow 只做**编排**（需求澄清、逻辑图、门禁、状态权威、产物交付）；「读代码 / 改代码 /
跑测试」这类专业活交给外部编码 Agent——它们是专业 skill 与「代码检索 → 修改 → lint / 测试
修复」轮询的载体，在目标项目里循环到产出可接受的产物，再以结构化结果交回 DevFlow。

- **可替换**：换 Agent（如 OpenCode → Pi）、换版本、换模型只改 `.env` 的 `*_PROVIDER`
  与 `PI_*` 配置，编排、门禁、checkpoint 与前端都不动；
- **可对比**：同一份需求换后端各跑一遍，用相同的门禁流程与产物结构比较不同 Agent 的实际
  效果（用例质量、改动幅度、lint / 测试通过率）；
- **不接也能跑**：默认主路径是 LLM 直连 + 本地 pytest 真实执行，未配置时 Mock 降级。

```bash
# 例：检索交给 codegraph，写代码 / 写测试交给 pi
CODE_SEARCH_PROVIDER=codegraph
CODE_EDIT_PROVIDER=pi
TEST_GEN_PROVIDER=pi
```

各能力可选后端：代码检索 `codegraph | opencode | pi | mock`，代码生成 `opencode | pi | mock`，
测试生成 `opencode | pi | llm | mock`，制图渲染 `archify | mermaid | mock`。
未配置后端时全流程仍可跑通（Mock 降级 + LLM 直连）。

### 执行后端与模型选择：serve / opencode 命令行 / pi 命令行

**决定权在 `.env` 的三个 `*_PROVIDER` 变量**。注意 `opencode` 这个值在不同能力下走的
具体通道不同：

| `.env` 变量 | 值 → 实际通道 | 说明 |
|---|---|---|
| `CODE_SEARCH_PROVIDER` | `opencode` → **serve HTTP**（`POST {OPENCODE_BASE_URL}/api/v1/code/search`） | 该端点是 DevFlow 自造协议，官方 `opencode serve` **没有**（请求会落回其网页前端）；有此端点的部署才可用 |
| `CODE_EDIT_PROVIDER` | `opencode` → **serve HTTP**（`/api/v1/code/generate`） | 同上 |
| `TEST_GEN_PROVIDER` | `opencode` → **opencode 命令行**（`opencode run --format json [-m $OPENCODE_MODEL] --agent $OPENCODE_AGENT`，不经 serve） | 官方无头入口，开箱可用 |
| 以上任意 | `pi` → pi CLI 子进程（`pi --provider $PI_PROVIDER --model $PI_MODEL --no-session --print <任务>`） | 模型每次显式指定 |

**供应商与模型是谁说了算**：DevFlow 调 pi / opencode CLI 时都**显式传参**
（`PI_PROVIDER` / `PI_MODEL` / `OPENCODE_MODEL`），会硬性覆盖你在 agent 交互界面里配置的
默认供应商/模型——界面配置只影响你手动裸跑 `pi` / `opencode` 时的默认值。两个防呆点：

- `.env` 里 `PI_PROVIDER` / `PI_MODEL` **必配**：pi 内置默认 provider 是 google，不传参
  会落到它而非你配置的任何 opencode 模型；
- `OPENCODE_MODEL` 留空 = 不传 `-m`，此时 opencode run 用其全局默认配置的模型。

**判断/自查当前实际走的谁**：

```bash
grep -E "^(CODE_|TEST_GEN|PI_|OPENCODE_)" .env   # 跑之前：直接看路由与模型配置
python3 -m devflow.cli check-providers            # 跑之前：探测各后端存活（opencode 一行探的是 serve）
# 手动复现 DevFlow 的调用并让 agent 回显实际模型：
pi --provider opencode-go --model "opencode-go/deepseek-v4-flash" \
   --no-session --no-tools --mode json --print "hi"        # 看 message.provider / message.model
opencode run --format json -m "opencode-go/deepseek-v4-flash" "hi"   # 事件流含 tokens/cost
```

运行中看事件流：每个阶段完成都有 provider 事件（`✓ 本阶段由 pi · <model> 完成` /
`↯ 供应商 xxx 失败 → 切换下一个`）；事后看产物（`test_report.json` 的 `session_id`、
`devflow spec` MD 报告的决策时间线）。

**opencode 不用 serve 的命令行用法**（与上表 `TEST_GEN_PROVIDER=opencode` 同源）：

```bash
opencode run -m "opencode-go/deepseek-v4-flash" --dir /path/to/project "任务"  # 指定项目
opencode run -m "..." --agent test-designer "任务"      # 指定技能 agent
opencode run --continue "继续"                          # 续跑上一会话
```

传不存在的模型名会直接报错而非静默回落，`-m` 是硬约束。

## 5. CLI 用法（与 Web 共享会话数据）

```bash
python3 -m devflow.cli setup                  # 首次启动引导（检测 → 多选安装 → 配 .env → 测试指引）
python3 -m devflow.cli new                    # 新会话（澄清 → 制图）
python3 -m devflow.cli new --full             # 新会话（全链路到验收）
python3 -m devflow.cli new --full --from-doc requirements.md   # 从文档读取需求
python3 -m devflow.cli spec requirements.md   # 一次性自动全流程（无评审，导出 CSV + 过程 MD）
python3 -m devflow.cli resume <thread_id>     # 断点续跑
python3 -m devflow.cli list                   # 列出所有会话
python3 -m devflow.cli export <thread_id>     # 导出产物到 ./artifacts/<thread_id>/
python3 -m devflow.cli check-providers        # 各后端可用性自检（不发真实调用）
python3 -m devflow.cli check-llm              # LLM 供应商连通性测试
```

会话内命令：`:export` 导出产物、`:reset` 重开、`:quit` 退出。Web 与 CLI 共用 `data/checkpoints.db`，一边创建的会话另一边可以继续。

### 5.1 spec：一次性自动全流程（无人工评审）

一条命令从需求文档直接跑到验收，全程不打断：

```bash
python3 -m devflow.cli spec requirements.docx --out ./artifacts
# 可选：--set project_root=/path/to/repo --set existing_code_accessible=true  # 代码模式预填
#       --id my-thread                                                        # 自定义会话 ID
```

行为约定（与交互模式共用同一张图，只是驱动方式不同）：

- **全部门禁自动按推荐通过**：图种类取规则推荐、清单路由加载 AI 预选、feature 拆分问题按推荐项执行、验收自动采纳全部用例；
- **需求文档信息不足时由 AI 脑补**：缺口字段由 LLM 给出最佳推断值，来源标 `inferred`，报告中逐字段标注「AI 脑补的最佳选择」（LLM 全挂走 mock 兜底时另行警示）；
- **产物落盘 `./artifacts/<thread_id>/`**：`casecraft-tests-<tid>.csv`（11 列用例表，Excel 可直接打开）、`casecraft-spec-<tid>.md`（全过程报告：需求清单+脑补标注、门禁自动决策时间线、逻辑图、用例明细、测试执行结果）、以及 `requirement.json` / `logic_graph.*` 等结构化产物；
- **不写 checklist 库**：跳过了人工用例过滤步骤，本会话产物不会沉淀进 `.checklist`；
- 会话照常落 checkpoint，事后可 `devflow resume <thread_id> --full` 回看与继续。

## 6. 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 页面右上角显示「Mock 模式」 | 未配置 Key，LLM 输出为演示数据；运行 `devflow setup` 向导或按第 4 节配置后重启 |
| 报 LLM 调用失败 / 401 / 余额 | Key 无效或欠费；检查 `.env`，运行 `check-providers` 定位 |
| 8000 端口被占 | 换端口启动：`--port 8100`（任意空闲端口均可） |
| 代码检索始终 0 条 / mock | 真实检索需部署 CodeGraph 并在目标项目 `codegraph init` 建索引（见 `.env.example`） |
| 想彻底重跑某会话 | 左侧会话悬停 ✕ 删除（同时清理 checkpoint），再新建 |
| 想换 / 对比不同 Agent 的效果 | 改 `.env` 的 `CODE_EDIT_PROVIDER` / `TEST_GEN_PROVIDER`（如 `pi`）后重启，同一需求重跑即可；详见第 4 节末「可选：接入外部编码 Agent」 |
| 依赖报错 No module named fastapi/uvicorn | `pip install -r requirements.txt`（Web 依赖已含在内） |
| pi 提示未登录 / `pi auth check` 显示 not_ready，但我明明配置了 | pi 里 `opencode` 与 `opencode-go` 是**两个独立条目**：`pi auth check --provider opencode` 与 `--provider opencode-go` 分别检测。交互界面 `/login` 配的通常是 `opencode-go`（zen go 网关）。DevFlow 用 `PI_PROVIDER` 显式指定，只要所配条目 ready 即可，另一个条目未配置不影响 |
| opencode go 套餐的模型到底有没有在被使用？ | 在用，且分两层：LLM 层（澄清/制图/用例/脑补）直连 zen go 端点（`check-llm` 可见各模型连通）；执行层（检索/改码/测试）经 pi 或 opencode 命令行用同一套凭据。自查命令见第 4 节末「执行后端与模型选择」 |
| 在 agent 界面里配的自定义供应商/模型，会影响 DevFlow 的调用吗？ | 不会。DevFlow 调 pi / opencode CLI 时显式传 `--provider` / `--model`（取自 `.env`），硬性覆盖 agent 默认配置。唯一例外：`PI_PROVIDER` / `PI_MODEL` 清空时不传参，pi 会落到其内置默认（google）——这两行必配 |
| 不想开 `opencode serve`，命令行能指定供应商和模型吗？ | 能：`opencode run -m "provider/model" "任务"`（官方无头入口），可加 `--dir` 指定项目、`--agent` 指定技能、`--continue` 续跑；传错模型名会直接报错，不会静默回落。DevFlow 的 `TEST_GEN_PROVIDER=opencode` 走的就是这条通道（模型由 `OPENCODE_MODEL` 指定） |
