# 阶段三 端到端测试用例

## TC301: Mock 全链路正常流程（clarify → 验收 approve）

### 前置条件
- 所有 Provider 使用 Mock 实现（MockCodeSearch / MockArchifyRender / MockCodeEdit / MockTestGen）
- LLM 使用 mock 模式（`LLM_FALLBACKS=mock`）
- SQLite checkpoint 使用临时文件

### 执行步骤

```python
from devflow.orchestrator import build_graph_with_providers, initial_state
from devflow.providers import Providers, MockCodeSearch, MockArchifyRender, MockCodeEdit, MockTestGen

providers = Providers(
    code_search=MockCodeSearch(),
    graph_render=MockArchifyRender(),
    code_edit=MockCodeEdit(),
    test_gen=MockTestGen(),
)
graph = build_graph_with_providers(providers)
state = initial_state()
config = {"configurable": {"thread_id": "tc301"}}

# 触发首轮（clarify_extract → validate → compress → graph_generate → code_search → ...）
list(graph.stream(state, config, stream_mode="updates"))

# 检查是否在 review 节点等待中断
snapshot = graph.get_state(config)
assert "review" in snapshot.next
```

### 预期响应

```json
{
  "current_stage": "code",
  "logic_graph": {"graph_id": "mock-graph-001", "nodes": [...], "edges": [...]},
  "code_context": [{"file_path": "mock/search.py", "code_snippet": "..."}],
  "code_changes": [{"file_path": "mock/edit.py", "action": "modify", "lint_passed": true}],
  "test_report": {"run": {"passed": 5, "failed": 0, "coverage_pct": 80.0}},
  "last_error_code": null,
  "next_nodes": ["review"]
}
```

### 恢复验收

```python
from langgraph.types import Command
list(graph.stream(Command(resume="approve"), config, stream_mode="updates"))
snapshot = graph.get_state(config)
assert snapshot.next == ()  # 流程结束
assert snapshot.values["current_stage"] == "done"
```

### 预期响应

```json
{
  "current_stage": "done",
  "next_nodes": []
}
```

---

## TC302: Mock 全链路验收 reject → 回退代码生成

### 前置条件
- 同 TC301

### 执行步骤

```python
# 同 TC301 前半段，到 review 中断后：
list(graph.stream(Command(resume="reject"), config, stream_mode="updates"))
snapshot = graph.get_state(config)
assert snapshot.values["current_stage"] == "code"
# code_gen 应该重新执行，最终再次到达 review 中断
assert "review" in snapshot.next
```

### 预期响应

```json
{
  "current_stage": "code",
  "last_error": "[review] 用户拒绝验收，回退到代码生成",
  "next_nodes": ["review"]
}
```

---

## TC303: doc_reader 读取 .docx 需求文档

### 前置条件
- 安装 python-docx>=1.1.0
- 创建测试 .docx 文件（含段落和表格）

### 执行步骤

```python
from devflow.doc_reader import read_doc

text = read_doc("tests/fixtures/sample_requirements.docx")
assert "项目名称" in text
assert "验收标准" in text  # 表格内容
```

### 预期响应

```json
{
  "text_length": 350,
  "contains_paragraphs": true,
  "contains_tables": true
}
```

---

## TC304: doc_reader 读取 .txt / .md 文件

### 执行步骤

```python
from devflow.doc_reader import read_doc

text = read_doc("tests/fixtures/sample.txt")
assert "需求" in text
```

### 预期响应

```json
{
  "text_length": 42,
  "format": "txt"
}
```

---

## TC305: doc_reader 不支持的格式

### 执行步骤

```python
from devflow.doc_reader import read_doc
read_doc("tests/fixtures/sample.pdf")
```

### 预期响应

```json
{
  "error": "ValueError",
  "message": "不支持的需求文档格式: .pdf（支持 .docx / .txt / .md）"
}
```

---

## TC306: CLI --from-doc 读取需求文档并启动会话

### 命令

```bash
devflow new --from-doc requirements.docx --id test-doc-001
```

### 预期响应

```
✓ 已读取需求文档: requirements.docx (350 字符)
✓ 已创建新会话 thread_id=test-doc-001（阶段一）
```

---

## TC307: CLI --full 启动全链路会话

### 命令

```bash
devflow new --full --id test-full-001
```

### 预期响应

```
✓ 已创建新会话 thread_id=test-full-001（全链路）
```

---

## TC308: review 节点 approve 路由

### 执行步骤

```python
from devflow.nodes.review import route_after_review

state = {"current_stage": "done"}
assert route_after_review(state) == "approved"
```

### 预期响应

```json
{
  "route": "approved"
}
```

---

## TC309: review 节点 reject 路由

### 执行步骤

```python
from devflow.nodes.review import route_after_review

state = {"current_stage": "code"}
assert route_after_review(state) == "rejected"
```

### 预期响应

```json
{
  "route": "rejected"
}
```

---

## TC310: build_graph_with_providers 图结构完整性

### 执行步骤

```python
from devflow.orchestrator import build_graph_with_providers
from devflow.providers import get_providers

graph = build_graph_with_providers(get_providers())
# 检查节点是否注册
node_names = set(graph.nodes.keys())
assert "clarify_extract" in node_names
assert "clarify_validate" in node_names
assert "graph_generate" in node_names
assert "code_search" in node_names
assert "graph_render" in node_names
assert "code_gen" in node_names
assert "test_gen" in node_names
assert "review" in node_names
assert "dead_letter_drain" in node_names
```

### 预期响应

```json
{
  "node_count": 10,
  "has_review_node": true,
  "has_dead_letter_drain": true
}
```
