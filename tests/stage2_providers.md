# Stage 2 · CodeProvider 适配层 · CLI / HTTP 接口用例集

本文件覆盖 SPEC 2.3.1/2.3.2/2.3.3/2.4.1/2.4.2/2.4.3 的**实际可执行**请求-响应对，
便于调试后端对接、手工 curl / bash 测试。

环境变量样例：

```bash
CODE_SEARCH_PROVIDER=codegraph        # 可选 codegraph / opencode / mock
CODE_GRAPH_RENDER_PROVIDER=archify    # 可选 archify / mermaid / mock
CODE_EDIT_PROVIDER=opencode           # 可选 opencode / mock
TEST_GEN_PROVIDER=opencode            # 可选 opencode / mock

OPENCODE_BASE_URL=http://localhost:8080
OPENCODE_API_TOKEN=xxxxx
```

---

## 1. Mock Provider（不依赖任何外部服务，CI 默认）

Python snippet：

```python
# test_mock_flow.py
import asyncio
from devflow.providers import get_providers

async def main():
    # 强制使用 mock
    p = get_providers(
        code_search="mock", graph_render="mock",
        code_edit="mock", test_gen="mock",
    )

    # 1) 代码检索
    s = await p.code_search.search(
        "/tmp/prj", "JWT 登录入口",
        query_type="semantic",
        target_symbols=["AuthService.login"],
        max_results=3,
    )
    print("search session_id:", s["session_id"], "total:", s["total"])

    # 2) 逻辑图渲染
    logic_graph = {
        "graph_id": "graph-demo",
        "nodes": [
            {"node_id": "n-in", "label": "HTTP /login", "node_type": "io",
             "code_ref": None, "is_modified": False},
            {"node_id": "n-auth-new", "label": "AuthService.login",
             "node_type": "function",
             "code_ref": {"file_path": "src/auth/svc.py", "symbol": "AuthService.login"},
             "is_modified": True},
        ],
        "edges": [
            {"edge_id": "e-1", "from_node": "n-in", "to_node": "n-auth-new",
             "edge_type": "call", "condition": None, "is_modified": True},
        ],
        "mermaid_source": "graph TD\\n  n-in([HTTP /login])-->n-auth-new(AuthService.login)",
    }
    r = await p.graph_render.render(logic_graph, preferred_format="html")
    print("render backend:", r["render_backend"], "fmt:", r["format"])
    print("html bytes len:", len(r["html_bytes"] or b""))

    # 3) 代码生成
    e = await p.code_edit.generate(
        "/tmp/prj", "给 AuthService.login 加 2FA 校验分支",
        logic_graph_node_id="n-auth-new",
        related_files=[{"file_path": "src/auth/svc.py"}],
        acceptance=["登录成功返回 token", "2FA 开启时返回 SMS_CODE_MISSING 错误"],
    )
    print("edit session_id:", e["session_id"],
          "lint_passed:", e["lint_passed"],
          "changed_files:", [c["file_path"] for c in e["changes"]])

    # 4) 测试生成
    t = await p.test_gen.generate(
        "/tmp/prj",
        ["AuthService.login", "AuthService._send_sms"],
        coverage_target=80,
        logic_graph=logic_graph,
    )
    print("tests count:", len(t["test_cases"]),
          "p/f/s:", t["run"]["passed"], t["run"]["failed"], t["run"]["skipped"],
          "coverage:", t["run"]["coverage_pct"])

asyncio.run(main())
```

**预期输出（形状）**：

```
search session_id: search-xxxxxxxx total: 2
render backend: mock_graph_render fmt: html
html bytes len: ~100
edit session_id: code-xxxxxxxx lint_passed: True changed_files: ['src/auth/svc.py']
tests count: 2 p/f/s: 1 1 0 coverage: 80.0
```

---

## 2. CodeGraph CLI Provider · 完整 shell 用例

对应实现：`devflow.providers.codegraph.CodeGraphProvider`

### 2.1 安装 + 初始化

```bash
curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh

cd /path/to/your/repo
codegraph init
# 成功后会产生：
#   .codegraph/graph.sqlite
#   .codegraph/... (内部索引)
```

### 2.2 语义检索（对应 search 调用路径）

```bash
# Request:
codegraph search --json "用户登录流程 JWT 校验逻辑"

# Response（样例；字段别名测试：用的是 file/symbol/start_line 这种老名）:
# [
#   {
#     "file": "src/auth/service.py",
#     "symbol": "AuthService.login",
#     "start_line": 45,
#     "end_line": 78,
#     "snippet": "def login(self, user, password):\n    ...",
#     "score": 0.95,
#     "called_by": ["src/api/routes.py:login_handler"],
#     "calls": ["src/db/repo.py:find_user", "src/auth.py:sign_jwt"]
#   }
# ]
```

### 2.3 符号查询 + 调用链（对应 symbol 调用路径）

```bash
# Request 1 - 取符号详情：
codegraph node --json src/auth/service.py:AuthService.login
# Response：单条 JSON 对象，字段同 2.2

# Request 2 - 列出调用者：
codegraph callers --json AuthService.login
# Response 数组，每项含 file/symbol/...

# Request 3 - 列出被调用：
codegraph callees --json AuthService.login
# Response 数组，每项含 file/symbol/...

# Request 4 - 影响面分析（CodeGraph 独有能力）：
codegraph impact --json AuthService.login
# Response：列出会被 AuthService.login 变更波及的符号/文件
```

### 2.4 Python Provider 调用（断言 scope_files 过滤生效）

```python
import asyncio
from devflow.providers.codegraph import CodeGraphProvider

async def main():
    p = CodeGraphProvider()  # 假设 PATH 里有 codegraph
    out = await p.search(
        "/path/to/your/repo",
        "JWT",
        query_type="semantic",
        scope_files=["src/auth/*", "src/api/*"],  # 只保留这两目录下的
        max_results=5,
    )
    for r in out["results"]:
        assert r["file_path"].startswith("src/auth/") or r["file_path"].startswith("src/api/")
        print(r["file_path"], r["symbol_name"], r["relevance_score"])

asyncio.run(main())
```

---

## 3. Archify Provider · 渲染用例（Node 环境）

对应实现：`devflow.providers.archify.ArchifyProvider`

### 3.1 安装 skills CLI + Archify skill

```bash
npm i -g @skills/cli
npx skills add tt-a1i/archify -g
# 验证（交互方式）：
#   npx skills use tt-a1i/archify
#   在 prompt 中输入：Use archify to map /path/to/repo
#   产生 archify-map.html
```

### 3.2 Python 侧字段映射（无需 Node，可用纯 Python 断言）

```python
from devflow.providers.archify import logic_graph_to_archify_ir

lg = {
    "graph_id": "graph-2fa",
    "nodes": [
        {"node_id": "n-in", "label": "/login", "node_type": "io",
         "code_ref": None, "is_modified": False},
        {"node_id": "n-auth", "label": "Auth.login", "node_type": "function",
         "code_ref": {"file_path": "src/auth/svc.py", "symbol": "Auth.login"},
         "is_modified": True},
        {"node_id": "n-sms-new", "label": "SMS Gateway", "node_type": "external",
         "code_ref": None, "is_modified": True},
    ],
    "edges": [
        {"edge_id": "e-1", "from_node": "n-in", "to_node": "n-auth",
         "edge_type": "call", "condition": None, "is_modified": False},
        {"edge_id": "e-2-new", "from_node": "n-auth", "to_node": "n-sms-new",
         "edge_type": "condition", "condition": "2FA_ENABLED==True",
         "is_modified": True},
    ],
    "mermaid_source": "graph TD\\n  n-in-->n-auth-->|2FA|n-sms-new",
}
ir = logic_graph_to_archify_ir(lg)

# 断言字段映射表（对应 SPEC 2.3.3）
assert ir["title"] == "graph-2fa"
node_by_id = {n["id"]: n for n in ir["diagram"]["nodes"]}
edge_by_id = {e["id"]: e for e in ir["diagram"]["edges"]}

# node_type → role
assert node_by_id["n-in"]["role"] == "frontend"
assert node_by_id["n-auth"]["role"] == "backend"
assert node_by_id["n-sms-new"]["role"] == "external"

# code_ref → source.ref
assert node_by_id["n-auth"]["source"]["ref"] == "src/auth/svc.py:Auth.login"

# is_modified + id 含 "new" → diff=added
assert node_by_id["n-sms-new"]["diff"] == "added"
assert node_by_id["n-auth"]["diff"] == "modified"          # id 不含 new → modified
assert edge_by_id["e-2-new"]["diff"] == "added"            # edge 含 new

# condition 边 → label = condition 内容
assert edge_by_id["e-2-new"]["label"] == "2FA_ENABLED==True"
```

### 3.3 Python Provider 渲染调用

```python
import asyncio
from devflow.providers.archify import ArchifyProvider

async def main():
    # 有 Node 环境时：
    # p = ArchifyProvider()
    # out = await p.render(logic_graph, preferred_format="html")
    # with open("archify-output.html", "wb") as f:
    #     f.write(out["html_bytes"])

    # 没有 Node 也能用，自动 fallback 到 Mermaid：
    p = ArchifyProvider(force_mermaid_fallback=True)
    out = await p.render(lg, preferred_format="html")
    print("render_backend:", out["render_backend"])  # archify+mermaid
    print("mermaid source:\n", out["mermaid_text"])

asyncio.run(main())
```

---

## 4. OpenCode Provider · HTTP 请求-响应 cURL

对应实现：
- Search → `devflow.providers.opencode.OpenCodeSearchProvider`
- Edit   → `devflow.providers.opencode.OpenCodeEditProvider`
- Test   → `devflow.providers.opencode.OpenCodeTestProvider`

公共：

```bash
OC=http://localhost:8080
TOKEN=dev-demo-token
REQ_ID=devflow-demo-001
THREAD_ID=proj-2fa-demo
```

### 4.1 代码检索 SPEC 2.4.1 · POST /api/v1/code/search

**请求（curl）**：

```bash
curl -sS -X POST "$OC/api/v1/code/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "request_id": "'"$REQ_ID"'",
    "thread_id": "'"$THREAD_ID"'",
    "session_id": null,
    "project_root": "/workspace/my-app",
    "query": {
      "type": "call_chain",
      "text": "登录接口 JWT 校验",
      "target_symbols": ["AuthService.login"],
      "scope_files": ["src/auth/*.py", "src/api/*.py"]
    },
    "max_results": 20,
    "include_context": true
  }' | python -m json.tool
```

**预期响应**：

```json
{
  "request_id": "devflow-demo-001",
  "session_id": "search-sess-001",
  "total": 1,
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
  ]
}
```

### 4.2 代码生成 SPEC 2.4.2 · POST /api/v1/code/generate

**请求**：

```bash
curl -sS -X POST "$OC/api/v1/code/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "request_id": "devflow-demo-002",
    "thread_id": "'"$THREAD_ID"'",
    "session_id": null,
    "project_root": "/workspace/my-app",
    "instruction": "给 AuthService.login 增加 2FA 分支：当 user.twofa=true 时，发短信并返回错误码 SMS_CODE_MISSING",
    "context": {
      "logic_graph_node_id": "n-login-new",
      "related_files": [
        {"file_path": "src/auth/service.py", "symbol": "AuthService.login"},
        {"file_path": "src/errors.py", "symbol": "ErrSMSMissing"}
      ],
      "acceptance_criteria": [
        "AC1: 2FA 用户首次调用返回 SMS_CODE_MISSING 错误",
        "AC2: 非 2FA 用户行为不变",
        "AC3: Lint 通过"
      ]
    },
    "run_lint": true,
    "max_retry_fix": 1
  }' | python -m json.tool
```

**预期响应（形状）**：

```json
{
  "request_id": "devflow-demo-002",
  "session_id": "code-sess-001",
  "changes": [
    {
      "file_path": "src/auth/service.py",
      "action": "update",
      "diff_unified": "--- a/src/auth/service.py\n+++ b/src/auth/service.py\n@@ ...\n ...self._send_sms(phone)\n ...return ErrSMSMissing\n",
      "content_after": "..."
    }
  ],
  "lint": {"passed": true, "issues": []}
}
```

### 4.3 测试生成 SPEC 2.4.3 · POST /api/v1/tests/generate

**请求**：

```bash
curl -sS -X POST "$OC/api/v1/tests/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "request_id": "devflow-demo-003",
    "thread_id": "'"$THREAD_ID"'",
    "session_id": null,
    "project_root": "/workspace/my-app",
    "target": {
      "files_or_symbols": ["src/auth/service.py:AuthService.login"],
      "modified_branches_only": true,
      "logic_graph_ref": "graph-2fa"
    },
    "framework": "pytest",
    "coverage_target": 80
  }' | python -m json.tool
```

**预期响应（形状）**：

```json
{
  "request_id": "devflow-demo-003",
  "session_id": "test-sess-001",
  "test_cases": [
    {
      "test_file": "tests/auth/test_login_2fa.py",
      "test_symbol": "test_login_2fa_returns_sms_missing",
      "line_start": 12,
      "line_end": 22,
      "code_snippet": "def test_login_2fa_returns_sms_missing():...",
      "covered_edges": ["e-2-new"]
    }
  ],
  "run": {"passed": 1, "failed": 0, "skipped": 0,
          "coverage_pct": 82.5, "logs": "...pytest output..."}
}
```

---

## 5. 用例通过判定矩阵

| 编号 | 用例 | 判定标准 |
|---|---|---|
| M-1 | Mock 搜索 | 返回 `total>=1` 且每条 hit `file_path/symbol/line_start/line_end/code_snippet/score/callers/callees` 全齐全 |
| M-2 | Mock 渲染 | `preferred_format=html` 时返回非空 `html_bytes` |
| M-3 | Mock edit | 含 `acceptance="pass lint check"` 时返回 `lint_passed=False` + 1 条 mock lint issue |
| M-4 | Mock test | logic_graph 含 2 条边时第一个 case 的 `covered_edges` 对应第 1 条 edge_id |
| CG-1 | CodeGraph 字段别名 | 用 `source/identifier/start/score/called_by/calls` 的 JSON 能被正确归一 |
| CG-2 | CodeGraph symbol 查询 | 调用 `node/callers/callees --json <sym>` 三条命令，顺序正确 |
| CG-3 | CodeGraph 失败 fallback | binary 不存在且有 fallback 时，不抛错而是返回 fallback 的结果 |
| AR-1 | Archify 字段映射 5 条断言 | `role/source.ref/diff=added+modified/condition→label` 同时满足 |
| AR-2 | Archify Mermaid fallback | `force_mermaid_fallback=True` 或 Node 不存在时 `render_backend` 含 `mermaid` 且 `mermaid_text` 非空 |
| OC-1 | OpenCode search body | `query.type/text/target_symbols/scope_files`、`include_context=true`、`max_results` 与 SPEC 2.4.1 对齐 |
| OC-2 | OpenCode edit body | `context.logic_graph_node_id`、`context.acceptance_criteria`、`run_lint`、`max_retry_fix` 与 SPEC 2.4.2 对齐 |
| OC-3 | OpenCode test body | `target.modified_branches_only`、`target.logic_graph_ref`、`coverage_target` 与 SPEC 2.4.3 对齐 |
