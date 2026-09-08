# QUICKSTART · 5 分钟上手

> 前置：Python ≥ 3.11。本指南对应当前版本的 Web Shell 与 CLI；架构细节见 [SPEC.md](SPEC.md)。

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
4. **制图评审（门禁 1/2）**：逻辑图生成后弹出评审窗，左侧是需求对照、右侧是图预览。
   - 点「查看完整产物」可收起弹窗，到主视图放大检查（滚轮缩放、拖拽平移、点节点看详情）；
   - 有问题点「驳回」并**填写修改意见**——意见会回传给制图节点自动重制图；
   - 没问题点「通过」进入下一阶段。
5. **代码检索 → 代码生成 → 测试设计**：自动推进，每个节点完成都会显示耗时；代码变更以 diff 视图展示。
   **仅需求模式**：如果不提供项目代码（⚙ 配置里不填 project_root，或澄清时说明没有现有代码），
   制图评审通过后会**跳过代码检索与生成**，直接基于需求 + 逻辑图设计**端到端测试用例**，
   步骤条上这两个阶段显示为划掉；人工验收驳回时会回到测试用例设计（而不是代码生成）。
6. **人工验收（门禁 2/2）**：展示变更文件数、测试通过 / 失败、覆盖率等统计；同样可驳回带意见回修。
7. **导出交付物**：验收通过后，测试场景表支持按层级 / 优先级 / 关键词筛选，导出 **CSV（Excel 可直接打开）/ Markdown**、一键复制；底部「导出交付物」条可打包整会话的 **Markdown 汇总 / JSON**。

其他常用操作：

- **会话管理**：左侧「历史会话」点任一会话即可恢复完整上下文（对话、产物、阶段，挂起中的门禁也会弹回）；悬停点 ✕ 删除（二次确认）；
- **＋ 新会话**：回到空态，随时开新一轮；
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

## 5. CLI 用法（与 Web 共享会话数据）

```bash
python3 -m devflow.cli new                    # 新会话（澄清 → 制图）
python3 -m devflow.cli new --full             # 新会话（全链路到验收）
python3 -m devflow.cli new --full --from-doc requirements.md   # 从文档读取需求
python3 -m devflow.cli resume <thread_id>     # 断点续跑
python3 -m devflow.cli list                   # 列出所有会话
python3 -m devflow.cli export <thread_id>     # 导出产物到 ./artifacts/<thread_id>/
```

会话内命令：`:export` 导出产物、`:reset` 重开、`:quit` 退出。Web 与 CLI 共用 `data/checkpoints.db`，一边创建的会话另一边可以继续。

## 6. 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| 页面右上角显示「Mock 模式」 | 未配置 Key，LLM 输出为演示数据；按第 4 节配置后重启 |
| 报 LLM 调用失败 / 401 / 余额 | Key 无效或欠费；检查 `.env`，运行 `check-providers` 定位 |
| 8000 端口被占 | 换端口启动：`--port 8100`（任意空闲端口均可） |
| 代码检索始终 0 条 / mock | 真实检索需部署 CodeGraph 并在目标项目 `codegraph init` 建索引（见 `.env.example`） |
| 想彻底重跑某会话 | 左侧会话悬停 ✕ 删除（同时清理 checkpoint），再新建 |
| 依赖报错 No module named fastapi/uvicorn | `pip install -r requirements.txt`（Web 依赖已含在内） |
