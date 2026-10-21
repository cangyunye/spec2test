"""文档语料 agent 分析（opencode / pi）：给路径 → agent 读取 → 结构化关键要素。

与 doc_index 确定性摄入的分工：
  - local（默认回退）：服务端自读文件 → 确定性切章 → 分章 LLM 抽取（可切片、
    可给 doc_refs 精确原文；适合结构化 Markdown/OpenAPI）
  - opencode / pi：把语料路径交给本机 agent CLI，由它读取文档（目录则由它自己
    遍历）、梳理关键要素，返回需求字段 + 语料目录摘要（files/apis/models）；
    服务端不再自读原文，handoff 时把原始路径交给下游执行器按需回查

重要约束：agent 与服务进程跑在**同一台机器**、共用同一文件系统。跨机器路径
（如 Windows 盘符路径 + macOS 服务）agent 同样读不到——需上传或换本机路径。

agent 输出契约（与提示词、解析器同一份）：

    {
      "req_type": "new_feature | component_iteration | bug_fix",
      "project_context": "2~4 句项目背景/业务场景",
      "target_modules": ["…"],
      "io_constraints": {"input": "…", "output": "…"},
      "edge_cases": ["…（尽量原文）"],
      "acceptance_criteria": ["…（尽量原文）"],
      "apis": [{"method": "POST", "path": "/x", "summary": "…"}],
      "models": [{"name": "Order", "fields": ["id", "…"]}],
      "files": [{"path": "…", "summary": "…"}],
      "open_questions": [{"question": "…", "recommended": "…"}],
      "assumptions": ["…"]
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .config import settings
from .providers.pi import PiProviderBase, _extract_json_payload

logger = logging.getLogger(__name__)

ANALYZE_JSON_KEYS = (
    "req_type", "project_context", "target_modules", "io_constraints",
    "edge_cases", "acceptance_criteria", "apis", "models", "files",
    "open_questions", "assumptions",
)

_PROMPT = """你是资深需求分析师。请阅读下面这些文档语料，梳理需求关键要素，供后续测试设计使用。

【语料路径】
{paths}

【阅读要求】
1. 列出的每一项若是目录，请自行遍历读取其中所有 .md / .markdown / .txt / .docx / .json 文档；
   若是文件则直接读取。不要跳过任何文档。
2. 接口专用文档重点提取：每个接口的方法、路径、请求入参字段与格式、响应结构；
   模型专用文档重点提取：模型名与字段清单。
3. acceptance_criteria / edge_cases 尽量保留文档原文措辞（逐字优先，可轻度裁剪成完整短句）；
   叙述式写出的功能规则、限制、异常处理也要提炼成条目。
4. 只写文档里明确有的内容，不要编造；文档确实没有的信息填空数组/null。
5. 最终答复只输出一个 JSON（不要代码围栏、不要解释文字），结构：
{{
  "req_type": "new_feature | component_iteration | bug_fix",
  "project_context": "2~4 句项目背景与业务场景",
  "target_modules": ["涉及模块/页面/子系统"],
  "io_constraints": {{"input": "输入约束（入参字段/格式）", "output": "输出约束（响应结构）"}},
  "edge_cases": ["边界/异常场景"],
  "acceptance_criteria": ["可验证的验收标准"],
  "apis": [{{"method": "POST", "path": "/api/x", "summary": "接口用途"}}],
  "models": [{{"name": "ModelName", "fields": ["field1", "field2"]}}],
  "files": [{{"path": "读取到的文档路径", "summary": "该文档要点"}}],
  "open_questions": [{{"question": "待确认问题", "recommended": "推荐答案"}}],
  "assumptions": ["分析时做的假设"]
}}"""


def resolve_analyze_provider() -> str:
    """解析文档分析器：explicit local/opencode/pi 直接采用；auto 按二进制可用性回落。"""
    mode = (settings.DOC_ANALYZE_PROVIDER or "auto").strip().lower()
    if mode in ("local", "opencode", "pi"):
        return mode
    if shutil.which(settings.OPENCODE_BIN) or Path(settings.OPENCODE_BIN).exists():
        return "opencode"
    if shutil.which(settings.PI_BIN) or Path(settings.PI_BIN).exists():
        return "pi"
    return "local"


def build_analyze_prompt(paths: list[str]) -> str:
    listing = "\n".join(f"- {p}" for p in paths)
    return _PROMPT.format(paths=listing)


def _common_root(paths: list[str]) -> str:
    """agent 的工作目录：语料共同父目录（目录取自身），退化到当前目录。"""
    dirs: list[str] = []
    for p in paths:
        pp = Path(p)
        dirs.append(str(pp if pp.is_dir() else pp.parent))
    try:
        common = Path(dirs[0])
        for d in dirs[1:]:
            while not str(Path(d)).startswith(str(common)):
                common = common.parent
                if str(common) in ("/", "."):
                    return str(Path.cwd())
        return str(common) if common.exists() else str(Path.cwd())
    except Exception:
        return str(Path.cwd())


async def analyze_docs(paths: list[str], *, provider: str | None = None) -> dict[str, Any]:
    """交给 agent 读取并分析语料，返回解析后的 JSON 契约。

    provider 缺省走 resolve_analyze_provider()；local 不应调用本函数。
    失败（二进制缺失 / CLI 错误 / 输出不可解析）抛 DevFlowError 或 ValueError，
    由调用方决定回退 local。
    """
    prov = provider or resolve_analyze_provider()
    prompt = build_analyze_prompt(paths)
    root = _common_root(paths)

    if prov == "opencode":
        from .providers.opencode import OpenCodeTestProvider, _parse_run_output

        # 合并而非覆盖外部 OPENCODE_CONFIG_CONTENT（外部可能带 provider/apiKey 配置）
        env_cfg: dict[str, Any] = {}
        try:
            cur = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
            if isinstance(cur, dict):
                env_cfg = cur
        except (ValueError, TypeError):
            env_cfg = {}
        perm = dict(env_cfg.get("permission") or {})
        perm.update({"edit": "deny", "write": "deny", "bash": "deny"})
        env_cfg["permission"] = perm

        p = OpenCodeTestProvider(timeout_sec=settings.DOC_ANALYZE_TIMEOUT_SEC)
        raw = await p._run_opencode(
            root, prompt,
            env_extra={"OPENCODE_CONFIG_CONTENT": json.dumps(env_cfg, ensure_ascii=False)},
        )
        # --format json 输出是 JSONL 事件流：先提取 assistant 文本再找 JSON
        text, _sid = _parse_run_output(raw)
    elif prov == "pi":
        text = await PiProviderBase()._run_pi(
            root, prompt, timeout_sec=settings.DOC_ANALYZE_TIMEOUT_SEC
        )
    else:
        raise ValueError(f"analyze_docs 不支持 local 模式（provider={prov}）")

    payload = _extract_json_payload(text)
    if not isinstance(payload, dict):
        raise ValueError(f"agent 输出不是合法 JSON: {str(text or '')[:200]}")
    return payload


def patch_from_agent(payload: dict[str, Any]) -> dict[str, Any]:
    """agent 输出 → requirement patch（只认已知字段，类型规整）。"""
    req_type = payload.get("req_type")
    patch: dict[str, Any] = {}
    if req_type in ("new_feature", "component_iteration", "bug_fix"):
        patch["req_type"] = req_type
    ctx = str(payload.get("project_context") or "").strip()
    if ctx:
        patch["project_context"] = ctx
    for key in ("target_modules", "edge_cases", "acceptance_criteria"):
        vals = payload.get(key)
        if isinstance(vals, list):
            clean = [str(v).strip() for v in vals if str(v or "").strip()]
            if clean:
                patch[key] = clean
    io = payload.get("io_constraints")
    if isinstance(io, dict):
        io_clean = {
            sub: str(io.get(sub)).strip()
            for sub in ("input", "output")
            if str(io.get(sub) or "").strip()
        }
        if io_clean:
            patch["io_constraints"] = io_clean
    return patch


def catalog_from_agent(
    payload: dict[str, Any], paths: list[str], provider: str
) -> dict[str, Any]:
    """agent 输出 → manifest 形态的语料目录（mode=agent）。

    无行号坐标（agent 不给切片）；apis/models 条目照常供拆分标注 doc_refs 与
    目录视图展示；source_paths 留给 handoff 让执行器按路径回查原文。
    """
    files = payload.get("files")
    docs = []
    if isinstance(files, list) and files:
        for i, item in enumerate(files, start=1):
            path = str((item or {}).get("path") or "").strip() or f"document-{i}"
            docs.append({
                "doc_id": f"D{i}",
                "kind": "requirement",
                "name": Path(path).name,
                "source_path": path,
                "cache_path": "",
                "chars": 0,
            })
    if not docs:
        docs = [{
            "doc_id": "D1", "kind": "requirement",
            "name": "语料集", "source_path": ";".join(paths),
            "cache_path": "", "chars": 0,
        }]
    apis = []
    for i, item in enumerate(payload.get("apis") or [], start=1):
        if not isinstance(item, dict):
            continue
        apis.append({
            "api_id": f"API-{i:03d}",
            "doc_id": "D1",
            "chapter_id": None,
            "method": str(item.get("method") or "").upper(),
            "path": str(item.get("path") or ""),
            "title": str(item.get("summary") or item.get("path") or ""),
            "chars": 0,
        })
    models = []
    for i, item in enumerate(payload.get("models") or [], start=1):
        if not isinstance(item, dict):
            continue
        models.append({
            "model_id": f"MDL-{i:03d}",
            "doc_id": "D1",
            "chapter_id": None,
            "name": str(item.get("name") or ""),
            "title": str(item.get("name") or ""),
            "chars": 0,
        })
    return {
        "version": 1,
        "mode": "agent",
        "provider": provider,
        "root": None,
        "source_paths": list(paths),
        "docs": docs,
        "chapters": [],
        "apis": apis,
        "models": models,
    }


def agent_feed_notice(catalog: dict[str, Any], provider: str) -> str:
    """agent 模式的首条消息回执（短文案，正文不进对话）。"""
    n_files = len(catalog.get("docs") or [])
    return (
        f"（已由 {provider} 读取并分析文档集：{n_files} 份文档 / "
        f"{len(catalog.get('apis') or [])} 个接口 / "
        f"{len(catalog.get('models') or [])} 个模型，需求要素已写入清单。"
        "全部文档正文不进对话上下文。）"
    )


def error_note(exc: Exception) -> str:
    """agent 失败时的用户提示（含回退说明）。"""
    return f"agent 文档分析失败（{type(exc).__name__}: {str(exc)[:160]}），已回退本地解析。"
