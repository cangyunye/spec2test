# 澄清节点 CLI 文本用例（阶段一）

> 覆盖：`devflow new --from-doc <file>` 与 `devflow new --set k=v`（可多次）
> 执行环境：配置 `LLM_API_KEY` 接入 DeepSeek / OpenAI 兼容服务；未配置时走 Mock 兜底。

---

## 1. 场景 A：从需求文档（.docx）初始化

### 前置条件
- 存在 `docs/requirements.docx`，内容含：需求类型、项目根目录、功能描述、边界场景、验收标准。

### 发送的命令

```bash
devflow new --id demo-docx --from-doc docs/requirements.docx
```

### 预期交互

| 步骤 | 输入/输出 | 预期响应 |
|---|---|---|
| 1 | 命令执行 | `✓ 已创建新会话 thread_id=demo-docx（阶段一）` |
| 2 | 文档读取 | `✓ 已读取需求文档: docs/requirements.docx (N 字符)` |
| 3 | 首轮 AI 处理 | 若 docx 内容可抽取完整需求 → `Missing 0 项 ✓` + 进入逻辑图生成 |
| 4 | 若 docx 缺字段 | AI 输出追问 AIMessage（如「请补充项目根目录绝对路径」），状态 `Missing 1 项` |
| 5 | 用户补充 | `你 (clarify) > 项目根目录是 /app/backend` → 校验通过进入制图 |
| 6 | 收尾 | 生成 `logic_graph`（`graph_id=graph-xxxx`） |

### 成功判据
- `devflow export demo-docx` 能导出 `requirement.json` 与 `logic_graph.json/.mmd`。

---

## 2. 场景 B：--set 手动强制赋值（跳过澄清追问）

### 发送的命令

```bash
devflow new --id demo-set \
  --set req_type=bug_fix \
  --set project_root=/app/backend \
  --set 'target_modules=["src/api/login.py","src/services/auth.py"]' \
  --set 'io_constraints={"input":"POST /login","output":"200 {token} / 401 {code}"}' \
  --set 'edge_cases=["密码错误5次锁定","token 过期"]' \
  --set 'acceptance_criteria=["登录成功返回 token","错误密码返回 401"]'
```

### 预期交互

| 步骤 | 输入/输出 | 预期响应 |
|---|---|---|
| 1 | 命令执行 | `✓ 已创建新会话 thread_id=demo-set（阶段一）` |
| 2 | 首次校验 | 以上 6 项已填 → 只差 `project_context`（或已由其他 --set 补充）→ 输出 1 条追问或不追问 |
| 3 | 若仍有缺失 | 追问中**不得**包含已通过 --set 填写的字段 |

### 成功判据
- 追问列表不包含 `req_type` / `project_root` / `target_modules` 等已填项。
- 全部补全后 `devflow export demo-set` 的 `requirement.json` 中 `req_type=="bug_fix"`、`target_modules` 为数组。

---

## 3. 场景 C：--set 清空字段

### 发送的命令

```bash
devflow new --id demo-clear \
  --set 'target_modules=[]' \
  --set project_root=/app
```

### 预期交互

| 步骤 | 输入/输出 | 预期响应 |
|---|---|---|
| 1 | 命令执行 | 会话创建成功 |
| 2 | 首次校验 | `target_modules` 为空数组 → `Missing 1 项`（target_modules: 至少指定 1 个涉及模块）→ AI 追问补模块路径 |

### 成功判据
- `--set target_modules=[]` 确实**清空**（而不是保留旧值）。

---

## 4. 场景 D：--set 参数格式错误

### 发送的命令

```bash
devflow new --id demo-bad --set target_modules
```

### 预期响应

```text
× --set 参数解析失败: --set 格式应为 key=value，got: 'target_modules'
```

### 成功判据
- 命令以非 0 退出码结束，未创建会话。

---

## 5. 场景 E：澄清轮次上限（CLARIFY.LOOP_EXHAUSTED）

### 前置条件
- 通过环境变量把上限调小便于演示：`export CLARIFY_MAX_ROUNDS=2`

### 发送的命令（交互）

```bash
devflow new --id demo-loop
# 连续 3 轮回答都不提供 project_root（或总缺同一字段）
你 (clarify) > 帮我做登录功能          # 第 1 轮追问
你 (clarify) > 用 Flask 实现           # 第 2 轮追问
你 (clarify) > 接口返回 JSON           # 第 3 轮
```

### 预期交互

| 轮次 | 预期响应 |
|---|---|
| 1 | AI 追问缺项（含 project_root） |
| 2 | 继续追问缺项 |
| 3 | 不再追问 → 状态显示 `Last Error: [clarify_validate:CLARIFY.LOOP_EXHAUSTED] ...`，流程终止（走 dead_letter 落盘） |

### 成功判据
- 第 3 轮后不会出现新的追问；`data/dead_letter/` 下有当日 JSONL 死信记录，`error_code=="CLARIFY.LOOP_EXHAUSTED"`。
