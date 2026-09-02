# CodeProvider 适配层 CLI 文本用例（阶段二/三）

> 覆盖：`devflow check-providers` 后端自检、`devflow new --full` 各后端组合。
> 相关实现：devflow/providers/{codegraph,archify,opencode,mock}.py + check.py

---

## 1. 场景 A：check-providers 自检（未装任何真实后端）

### 前置条件
- 未安装 codegraph，未启动 OpenCode 服务，node/npx 可用或不可用均可。

### 发送的命令

```bash
devflow check-providers --project-root /workspace
```

### 预期响应（诊断表）

```text
                CodeProvider 后端自检（project_root=/workspace）
┏━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ 后端      ┃ 状态   ┃ 详情                                        ┃
┡━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ codegraph │ ✗ 降级 │ codegraph 二进制未找到，请执行官方安装脚本  │
│ archify   │ ✓ 可用 │ node: <path>; npx: <path>；可渲染 HTML/SVG │
│ opencode  │ ✗ 降级 │ http://localhost:8080 连接失败（将走 Mock） │
└───────────┴────────┴─────────────────────────────────────────────┘
1/3 个后端可用。未装的后端会自动回退 Mock/Mermaid，不影响流程。
```

### 成功判据
- 三个后端行都出现，字段 `状态 / 详情` 齐全；
- 可用数 + 降级数 = 3。

---

## 2. 场景 B：Archify 真实渲染失败 → Mermaid 自动回退

### 前置条件
- node/npx 可用但 `npx skills use tt-a1i/archify` 不可用（网络受限 / skill 未装）。

### 发送的命令（Python 直调等价）

```bash
python -c "
import asyncio
from devflow.providers.archify import ArchifyProvider
graph = {'graph_id':'g','nodes':[{'node_id':'n-1','label':'In','node_type':'io','is_modified':False}],'edges':[],'mermaid_source':'graph TD\n n-1 --> n-2'}
print(asyncio.run(ArchifyProvider().render(graph, preferred_format='html')))
"
```

### 预期响应

```json
{"format": "mermaid", "render_backend": "archify+mermaid-fallback", "mermaid_text": "graph TD\n n-1 --> n-2", ...}
```

### 成功判据
- `render_backend` 含 `+mermaid-fallback`；流程不抛异常。

---

## 3. 场景 C：CODEGRAPH_REQUIRE_INDEX=1 且索引缺失

### 前置条件
- 项目目录无 `.codegraph/`。

### 发送的命令

```bash
CODEGRAPH_REQUIRE_INDEX=1 python -c "
from devflow.providers.codegraph import CodeGraphProvider
p = CodeGraphProvider(bin_path='/bin/sh', require_index=True)
try:
    p._ensure_env('/tmp/no-codegraph-project')
    print('NO ERROR (unexpected)')
except Exception as e:
    print(type(e).__name__, ':', e)
"
```

### 预期响应

```text
CliIndexMissingError : 项目 '/tmp/no-codegraph-project' 缺少 .codegraph/ 索引，请先执行 `codegraph init`
```

### 成功判据
- 抛出 `CliIndexMissingError`（code=`CLI.INDEX_MISSING`，retryable=True）；
- 设置 `CODEGRAPH_REQUIRE_INDEX=0`（默认）时同一目录仅 warning 不抛错。

---

## 4. 场景 D：--full 全链路（全部后端走 Mock 降级）

### 前置条件
- 已配置真实 LLM Key（LLM_API_KEY / LLM_PROVIDERS_JSON）；
- 未装 codegraph / 未起 OpenCode（自动降级 Mock）。

### 发送的命令

```bash
devflow new --full --id demo-full --set project_root=/workspace \
  --set 'target_modules=["devflow"]' --set 'edge_cases=["空输入"]' \
  --set 'acceptance_criteria=["流程跑通"]' --set 'project_context=DevFlow 自身' \
  --set 'io_constraints={"input":"x","output":"y"}'
```

### 预期交互

| 步骤 | 预期响应 |
|---|---|
| 1 澄清 | `Missing 0 项 ✓` 或按实际 --set 补齐 |
| 2 制图 | 真实 LLM 产出逻辑图（`graph_id=graph-xxx`） |
| 3 code_search | mock 返回 `代码检索到 N 条结果` |
| 4 code_gen | mock 返回 `代码变更 N 个文件` |
| 5 test_gen | mock 返回 `测试: passed=.. failed=..` |
| 6 review | 提示人工验收 approve/reject |

### 成功判据
- 全链路不因后端缺失而中断；每阶段产物（code_context / code_changes / test_report）非空。

---

## 5. 场景 E：code_gen 上下文带真实代码片段

### 前置条件
- code_context 含 `code_snippet` 字段。

### 发送的命令（等价断言）

```bash
python -m pytest tests/test_code_gen_context.py -v
```

### 预期响应

```text
4 passed
```

### 成功判据
- related_files 携带 `code_snippet`（超 1600 字符截断）；无 snippet 时降级为只传路径。
