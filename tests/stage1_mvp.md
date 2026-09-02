# 阶段一 MVP 测试用例

> **测试范围**: `devflow` 包 — 需求澄清循环 + 可机读逻辑图生成 + SQLite Checkpoint 持久化
> **对应 SPEC**: 第四章节「阶段一 MVP」
> **注意**: 涉及 LLM 的节点（`clarify_extract`、`clarify_build_question`、`graph_generate`）
> 需要真实 API Key 才能跑通；本机若无 Key，则仅验证 **Schema 校验 + 状态流转 + Checkpoint 读写 + CLI help / list / new 初始化** 逻辑。

---

## TC101: Schema 校验 — 空需求错误

| 项 | 内容 |
|---|---|
| **目的** | 验证空的 `RequirementSchema` 会被 `validate_requirement()` 捕获 7 项错误 |
| **前置** | `pip install -r requirements.txt` 完成 |
| **命令** | |

```bash
cd /workspace && python3 <<'PY'
from devflow import schemas
errs = schemas.validate_requirement(schemas.empty_requirement())
print("错误数:", len(errs))
for e in errs:
    print(" -", e)
assert len(errs) >= 7
print("PASS")
PY
```

| 期望响应 |
|---|
| 错误数 ≥ 7，包含：`project_root`、`target_modules`、`edge_cases`、`acceptance_criteria`、`project_context`、`io_constraints.input`、`io_constraints.output` |
| 最后打印 `PASS` |

---

## TC102: Schema 校验 — 完整需求零错误

| 项 | 内容 |
|---|---|
| **目的** | 验证填全所有必填字段后 `validate_requirement()` 返回空列表 |
| **命令** | |

```bash
cd /workspace && python3 <<'PY'
from devflow import schemas
complete = {
    "req_type": "component_iteration",
    "project_root": "/workspace/demo-app",
    "project_context": "Flask + SQLAlchemy 电商后端，已实现基本登录。",
    "target_modules": ["auth", "api/routes.login"],
    "existing_code_accessible": True,
    "reference_files": ["docs/login.md"],
    "io_constraints": {
        "input": "POST /api/login { username:str, password:str, sms_code?:str }",
        "output": "200 { token:jwt, user_id:int } / 401 { code:str, msg:str }"
    },
    "edge_cases": ["密码连续错误5次锁定30分钟", "sms_code 不匹配返回 401", "空字段拒绝"],
    "acceptance_criteria": ["2FA 开关由 user.twofa_enabled 控制", "bcrypt ≥ 12 轮", "错误日志脱敏"],
}
errs = schemas.validate_requirement(complete)
print("错误数:", len(errs))
for e in errs:
    print(" -", e)
assert len(errs) == 0
print("PASS")
PY
```

| 期望响应 |
|---|
| `错误数: 0` + `PASS` |

---

## TC103: 需求信息合并（_merge_requirement）—— 只覆盖非空字段

| 项 | 内容 |
|---|---|
| **目的** | 验证 LLM 抽取到的 patch 不会把 None 覆盖已有填写值 |
| **命令** | |

```bash
cd /workspace && python3 <<'PY'
from devflow.nodes.clarify import _merge_requirement
from devflow import schemas
old = schemas.empty_requirement()
old["project_context"] = "用户已填项目背景"
old["target_modules"] = ["auth"]
patch = {
    "project_context": None,          # LLM 本轮没读到，不能覆盖！
    "target_modules": [],             # 空数组 = 没新内容，不应覆盖
    "io_constraints": {"input": "", "output": "新填 output"},
    "acceptance_criteria": ["token 24h"],
}
merged = _merge_requirement(old, patch)
assert merged["project_context"] == "用户已填项目背景", "None 不能覆盖旧值"
assert merged["target_modules"] == ["auth"], "空数组不能覆盖旧值"
assert merged["io_constraints"]["output"] == "新填 output", "实质字段写入"
assert merged["io_constraints"]["input"] == "", "空字符串值保留（但后续校验会报错）"
assert merged["acceptance_criteria"] == ["token 24h"], "新数组覆盖空数组"
print("PASS")
PY
```

| 期望响应 |
|---|
| 所有 `assert` 通过，打印 `PASS` |

---

## TC104: 压缩节点（compress_messages）—— 只截断对话，不影响结构化字段

| 项 | 内容 |
|---|---|
| **目的** | 验证 `compress_messages` 只影响 messages（保留最近 N×2 条），不会操作任何结构化字段 |
| **命令** | |

```bash
cd /workspace && python3 <<'PY'
from langchain_core.messages import HumanMessage, AIMessage
from devflow.nodes.compress import compress_messages
from devflow.config import settings

# 构造 20 条消息 = 10 轮（远大于 HOT_MEMORY_LAST_N 默认 5）
msgs = []
for i in range(10):
    msgs.append(HumanMessage(content=f"u{i}"))
    msgs.append(AIMessage(content=f"a{i}"))
state = {"messages": list(msgs)}
out = compress_messages(state)
trimmed = out["messages"]
assert len(trimmed) == settings.HOT_MEMORY_LAST_N * 2, f"期望截断到 {settings.HOT_MEMORY_LAST_N*2}，实际 {len(trimmed)}"
# 被保留的内容应是最后几轮：u6/a6 ... u9/a9 (索引 12~19)
assert trimmed[0].content == "u6" or trimmed[0].content == "u5"
print(f"截断后剩余 {len(trimmed)} 条，首条内容: {trimmed[0].content}")
print("PASS")
PY
```

| 期望响应 |
|---|
| 截断后剩余 `HOT_MEMORY_LAST_N*2` 条消息，打印 `PASS` |

---

## TC105: 可机读逻辑图 Schema 校验（validate_logic_graph）

| 项 | 内容 |
|---|---|
| **目的** | 验证有效图通过、无效图（引用不存在节点）被拦截 |
| **命令** | |

```bash
cd /workspace && python3 <<'PY'
from devflow import schemas

# 合法图
valid_g = {
    "graph_id": schemas.new_graph_id(),
    "nodes": [
        {"node_id": "n-1", "label": "入口", "node_type": "io", "is_modified": False},
        {"node_id": "n-2", "label": "校验", "node_type": "condition", "is_modified": True},
        {"node_id": "n-3", "label": "成功", "node_type": "io", "is_modified": True},
    ],
    "edges": [
        {"edge_id": "e-1", "from_node": "n-1", "to_node": "n-2", "edge_type": "call", "is_modified": False},
        {"edge_id": "e-2", "from_node": "n-2", "to_node": "n-3", "edge_type": "condition", "condition": "OK", "is_modified": True},
    ],
    "mermaid_source": "flowchart TD\n  n-1-->n-2\n  n-2-->|OK|n-3\n",
}
errs = schemas.validate_logic_graph(valid_g)
print("valid errors:", errs)
assert len(errs) == 0

# 非法图：边引用不存在节点
invalid_g = {**valid_g}
invalid_g["edges"] = [
    {"edge_id": "e-bad", "from_node": "n-NOTEXIST", "to_node": "n-3", "edge_type": "call", "is_modified": False}
]
errs2 = schemas.validate_logic_graph(invalid_g)
print("invalid errors:", errs2)
assert len(errs2) == 1 and "n-NOTEXIST" in errs2[0]

print("PASS")
PY
```

| 期望响应 |
|---|
| `valid errors: []`、`invalid errors` 中包含 `n-NOTEXIST`、打印 `PASS` |

---

## TC106: LangGraph 构建 + SQLite Checkpoint 持久化

| 项 | 内容 |
|---|---|
| **目的** | 验证 `build_graph()` 可编译、可写 checkpoint、重建 graph 后仍能读取 |
| **前置** | 清空 `./data/checkpoints.db` |
| **命令** | |

```bash
cd /workspace && rm -rf ./data && python3 <<'PY'
from devflow import orchestrator
import os, sqlite3

g1 = orchestrator.build_graph()
tid = "tc106-mvp"
cfg = {"configurable": {"thread_id": tid}}

# 1. invoke 初始化
g1.invoke(orchestrator.initial_state(), cfg)
s1 = g1.get_state(cfg).values
assert s1["current_stage"] == "clarify"
print("✓ step 1 init")

# 2. update_state 写入测试值
g1.update_state(cfg, {"requirement": {"project_root": "/tmp/tc106"}})
s2 = g1.get_state(cfg).values
assert s2["requirement"]["project_root"] == "/tmp/tc106"
print("✓ step 2 update state")

# 3. 销毁 g1，建 g2，重新读取（模拟服务重启）
del g1
g2 = orchestrator.build_graph()
s3 = g2.get_state(cfg).values
assert s3["requirement"]["project_root"] == "/tmp/tc106", "Checkpoint 读取失败！"
print(f"✓ step 3 重启后仍读到 project_root = {s3['requirement']['project_root']}")

# 4. 确认 SQLite 文件存在 & 表存在
db = orchestrator.settings.CHECKPOINT_SQLITE_PATH
assert os.path.exists(db), f"{db} 不存在"
with sqlite3.connect(str(db)) as c:
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("DB tables:", [t[0] for t in tables])
assert any(t[0] == "checkpoints" for t in tables)

print("PASS")
PY
```

| 期望响应 |
|---|
| 4 个 step 都打印 ✓，DB 中有 checkpoints 表，最后 `PASS` |

---

## TC107: CLI 入口 — `--help`、`list`、`new`（无 LLM 交互）

| 项 | 内容 |
|---|---|
| **目的** | 验证 typer CLI 子命令可正常注册 & 解析参数（不触发 LLM） |
| **命令 1** | `cd /workspace && python3 -m devflow.cli --help` |
| **期望 1** | 显示 `devflow` 标题和 4 个子命令：`new`, `resume`, `list`, `export` |
| | |
| **命令 2** | `cd /workspace && python3 -m devflow.cli list`（数据库空或有数据） |
| **期望 2** | 无异常退出；要么显示「还没有历史会话」，要么列出表格 |
| | |
| **命令 3** | `cd /workspace && echo ':quit' | python3 -m devflow.cli new --id tc107-smoke 2>&1 | head -30` |
| **期望 3** | 显示 Banner 面板（Thread ID、Stage），输入 `:quit` 后优雅退出，不抛异常 |

---

## TC108（需真实 LLM API Key）: 端到端 5 轮追问 + 逻辑图生成

| 项 | 内容 |
|---|---|
| **目的** | 验证完整闭环：用户逐步填信息 → 自动追问 3+ 轮 → 信息齐备 → 生成逻辑图 + Mermaid 源码 |
| **前置** | 正确配置 `.env` 中 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`，能连通 |
| **命令** | |

```bash
cp .env.example .env
# 编辑 .env 填入真实 Key

cd /workspace && python3 -m devflow.cli new --id tc108-e2e
# 交互式按以下顺序逐行回答（每一行一次回车）：
#   (1) 我想给现有 Flask 登录接口加短信 2FA
#   (2) 项目路径 /workspace/demo-app，技术栈 Flask + SQLAlchemy + JWT
#   (3) 涉及模块 auth/service.py 中的 AuthService.login，现有代码可访问
#   (4) 输入 POST /login {username,password,sms_code?}，输出 200 {token,user_id} / 401 {code,msg}
#   (5) 边界：密码错5次锁30分，sms_code不对返回401，老用户没开2FA不走验证码
#   (6) 验收：user.twofa_enabled=true 才走短信；测试覆盖率 >= 80%；不破坏原有登录
#
# → 程序此时应不再追问（current_stage == graph / done），并输出 Mermaid 源码
# 接着在 CLI 里输入：
#   :export
#   :quit
ls -la ./artifacts/tc108-e2e/
# 期望看到 requirement.json、logic_graph.json、logic_graph.mmd 三个文件
```

| 期望响应 |
|---|
| 1. 追问 ≤ 6 轮后自动判断信息完备，进入制图阶段 |
| 2. 当前阶段变为 `graph` 或 `done`，Mermaid 面板正确渲染（语法合法，`:::modified` 样式类存在） |
| 3. `:export` 后导出的 `logic_graph.json` 通过 `jsonschema.validate(..., LOGIC_GRAPH_SCHEMA)` |
| 4. 退出后用 `resume tc108-e2e` 可从断点恢复，`current_stage` 仍为 `done` |

---

## 附录：CI 一键跑非 LLM 单元测试（TC101~TC106）

```bash
cd /workspace && pip install -r requirements.txt 2>&1 | tail -3 && \
python3 -m pytest tests/test_stage1_unit.py -v 2>&1 | tail -30
```

（若 `tests/test_stage1_unit.py` 不存在，则逐条用 `bash <<'PYEOF'` 方式执行 TC101~TC106 脚本）
