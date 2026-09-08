"""需求澄清节点组：
  1. clarify_validate  —— 程序校验：检查当前 requirement 是否完备
  2. clarify_extract   —— LLM 从用户对话中抽取信息，更新 requirement（合并已有值）
  3. clarify_build_question —— 根据缺失字段生成定向追问（追加到 messages 末尾作为 AIMessage）

循环路径（由主 graph 的路由函数控制）：
  validate → 有缺失 → build_question → 等待用户输入 → extract → validate → ...
  validate → 无缺失 → 进入下一阶段

注意：对外同时暴露 sync/async 两份入口：
- sync 版 `clarify_extract` / `clarify_build_question` / `graph_generate` 用于 LangGraph .invoke() 同步流程
- async 版 `clarify_extract_async` / `clarify_build_question_async` / `graph_generate_async` 用于 .ainvoke() 和内部链
"""
from __future__ import annotations

import asyncio
import copy
from typing import Any

from pydantic import BaseModel, Field

from ..config import settings
from ..errors import CLARIFY_LOOP_EXHAUSTED, ClarifyLoopExhaustedError, DevFlowError
from ..llm_client import invoke_json
from ..schemas import (
    REQUIREMENT_SCHEMA,
    empty_requirement,
    validate_requirement,
)
from ..state import GlobalState


# ── Pydantic 响应模型：让 LLM 严格按需求 Schema 抽取 ────────
class _FieldIOConstraints(BaseModel):
    input: str = Field(description="输入约束，用户提供的输入是什么、格式如何")
    output: str = Field(description="输出约束，期望输出什么、格式如何")


class RequirementExtract(BaseModel):
    """LLM 从最新一轮用户消息中提取的需求信息。
    所有字段都是可选的，LLM 只能填写它从本轮用户对话中明确读到的内容；
    不确定的字段留空，后续合并逻辑不会覆盖已有值。
    """
    req_type: str | None = Field(
        default=None,
        description="需求类型：只能是 new_feature / component_iteration / bug_fix，不确定留 null",
    )
    project_root: str | None = Field(
        default=None,
        description="项目根目录绝对路径；用户未提供项目代码时留 null",
    )
    project_context: str | None = Field(default=None, description="项目背景/技术栈/业务场景简述")
    target_modules: list[str] | None = Field(default=None, description="本次需求涉及的模块/目录列表")
    existing_code_accessible: bool | None = Field(
        default=None,
        description=(
            "用户是否提供现有项目代码；用户没提供代码或只想按需求生成测试用例时填 false"
        ),
    )
    reference_files: list[str] | None = Field(default=None, description="参考文件路径列表，无则空数组")
    io_constraints: _FieldIOConstraints | None = None
    edge_cases: list[str] | None = Field(default=None, description="边界/异常场景清单")
    acceptance_criteria: list[str] | None = Field(default=None, description="可独立验证的验收标准")


SYSTEM_PROMPT_EXTRACT = """你是一个严谨的需求分析师，任务是从用户的对话中抽取结构化信息。
重要规则：
1. 只提取【本轮用户消息里明确提到】的信息，不要猜测，不要脑补。
2. 用户没提到或语义不明确的字段，必须填 null，禁止用默认值蒙混过关。
3. req_type 严格三选一：new_feature(全新功能) / component_iteration(组件迭代) / bug_fix(缺陷修复)。
4. edge_cases、acceptance_criteria、target_modules 只要用户提到就提取成数组；
   用户没提到但有相关语义可从上下文提炼时，可用数组列出；完全没提就填 null。
5. project_root 必须是绝对路径；用户没给绝对路径时就填 null，不要自己拼接。
6. existing_code_accessible：仅当用户明确表示提供了/可访问现有项目代码时填 true
   （通常会伴随项目路径）；用户说明没有现有代码、不提供代码、或只想基于需求直接生成
   测试用例时，填 false。不提供项目代码完全可以继续，后续仅基于需求生成端到端测试用例，
   所以不要为了凑字段而追问项目路径。
"""


USER_PROMPT_TEMPLATE = """
# 已有的需求上下文（仅供参考，不要覆盖你不确定的字段）
已有需求：
{existing_requirement}

# 本轮用户新输入
{latest_user_message}

请只从【本轮用户新输入】中抽取明确提到的信息；如果已有需求中某个字段用户本轮没提，就填 null 让它保留旧值。
"""


def clarify_extract(state: GlobalState) -> dict[str, Any]:
    """同步入口（LangGraph .invoke 使用）：内部用 asyncio.run 跑 async 实现。"""
    return asyncio.run(clarify_extract_async(state))


async def clarify_extract_async(state: GlobalState) -> dict[str, Any]:
    """异步入口：从最新一轮用户对话中抽取信息，合并更新 requirement。

    输入（读 state）: messages, requirement（旧值）
    输出（写回 state）: requirement（新合并值）, retry_count, last_error_code, last_error_retryable
    """
    messages = state.get("messages", [])
    existing = copy.deepcopy(state.get("requirement") or empty_requirement())

    # 1. 找最后一条 HumanMessage
    latest_user_text = ""
    for m in reversed(messages):
        mtype = getattr(m, "type", None)
        if mtype == "human":
            latest_user_text = getattr(m, "content", "") or ""
            break

    # 若完全没用户输入，直接返回（不做事）
    if not latest_user_text.strip():
        return {"requirement": existing}

    # 2. LLM 结构化抽取
    try:
        extracted = await invoke_json(
            system_prompt=SYSTEM_PROMPT_EXTRACT,
            user_prompt=USER_PROMPT_TEMPLATE.format(
                existing_requirement=_format_requirement_for_llm(existing),
                latest_user_message=latest_user_text,
            ),
            response_model=RequirementExtract,
            response_type="requirement_extract",
        )
    except DevFlowError as e:
        retry = state.get("retry_count", {}) or {}
        retry["clarify_extract"] = retry.get("clarify_extract", 0) + 1
        return {
            "last_error": f"[clarify_extract:{e.code}] {e.message}",
            "last_error_code": e.code,
            "last_error_retryable": e.retryable,
            "retry_count": retry,
        }
    except Exception as e:
        from ..errors import wrap_exception
        err = wrap_exception(e, context="clarify_extract")
        retry = state.get("retry_count", {}) or {}
        retry["clarify_extract"] = retry.get("clarify_extract", 0) + 1
        return {
            "last_error": f"[clarify_extract:{err.code}] {err.message}",
            "last_error_code": err.code,
            "last_error_retryable": err.retryable,
            "retry_count": retry,
        }

    # 3. 合并：已有值优先，只覆盖本轮提取中明确非空的字段
    merged = _merge_requirement(existing, extracted)

    return {
        "requirement": merged,
        "last_error": None,
        "last_error_code": None,
        "last_error_retryable": None,
    }


def clarify_validate(state: GlobalState) -> dict[str, Any]:
    """节点：纯程序校验 requirement 是否完备，不调用 LLM。

    输出：missing_fields（错误/缺失列表）+ current_stage

    澄清轮次控制（评审稿 §2.3 D2）：
      - 每次进入把 retry_count["clarify_loop_cnt"] +1
      - 达到 settings.CLARIFY_MAX_ROUNDS 仍缺失 → 写 CLARIFY.LOOP_EXHAUSTED
        （不可重试），由 _route_after_validate 路由到 abort → dead_letter → END，
        避免无限追问循环。
    """
    req = state.get("requirement") or empty_requirement()
    errors = validate_requirement(req)

    retry = dict(state.get("retry_count") or {})
    retry["clarify_loop_cnt"] = retry.get("clarify_loop_cnt", 0) + 1

    if errors and retry["clarify_loop_cnt"] > settings.CLARIFY_MAX_ROUNDS:
        err = ClarifyLoopExhaustedError(
            f"澄清已达 {settings.CLARIFY_MAX_ROUNDS} 轮上限，仍有 {len(errors)} 项缺失: "
            + "; ".join(errors)
        )
        return {
            "missing_fields": errors,
            "current_stage": "clarify",
            "last_error": f"[clarify_validate:{err.code}] {err.message}",
            "last_error_code": CLARIFY_LOOP_EXHAUSTED,
            "last_error_retryable": False,
            "retry_count": retry,
        }

    return {
        "missing_fields": errors,
        "current_stage": "clarify" if errors else "graph",  # 阶段一跳过 search，直接去制图
        "retry_count": retry,
    }


SYSTEM_PROMPT_QUESTION = """你是一个沟通能力很强的产品经理。根据【缺失字段列表】和【当前已填内容】，
生成一个【定向追问】的问题清单，一次性问清缺失项，不要闲聊。
要求：
1. 每个缺失字段问一条具体问题，问题要口语化、可被用户直接回答；
2. 对空数组类字段（target_modules/edge_cases/acceptance_criteria），举例告诉用户怎么填；
3. 不要问已经填好的字段；
4. 输出为纯文本，多问题用换行分隔，不要 JSON 格式。
"""


def clarify_build_question(state: GlobalState) -> dict[str, Any]:
    """同步入口（LangGraph .invoke 使用）：内部用 asyncio.run 跑 async 实现。"""
    return asyncio.run(clarify_build_question_async(state))


async def clarify_build_question_async(state: GlobalState) -> dict[str, Any]:
    """异步入口：根据 missing_fields 生成追问，作为 AIMessage 追加到 messages。"""
    from langchain_core.messages import AIMessage

    missing = state.get("missing_fields") or []
    req = state.get("requirement") or empty_requirement()

    if not missing:
        return {}

    try:
        question_text = await invoke_json(
            system_prompt=SYSTEM_PROMPT_QUESTION,
            user_prompt=(
                f"【缺失字段 / 错误】\n{chr(10).join('- ' + m for m in missing)}\n\n"
                f"【当前已填内容】\n{_format_requirement_for_llm(req)}\n\n"
                "请输出追问文本（纯文本多行）："
            ),
            json_schema={
                "type": "object",
                "required": ["questions"],
                "properties": {
                    "questions": {"type": "string", "minLength": 2}
                },
            },
            response_type="clarify_question",
        )
        text = question_text["questions"]
    except DevFlowError as e:
        text = (
            "需要您补充以下信息：\n" + "\n".join(f"- {m}" for m in missing)
            + f"\n(LLM 出错: [{e.code}] {e.message})"
        )
    except Exception as e:
        # 兜底：直接把缺失字段原样展示给用户
        text = "需要您补充以下信息：\n" + "\n".join(f"- {m}" for m in missing) + f"\n(LLM 出错: {e})"

    return {
        "messages": [AIMessage(content=text)],
    }


# ═══════════════════════════════════════════════════════════════════
# 内部工具函数
# ═══════════════════════════════════════════════════════════════════

def _merge_requirement(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把 patch 中明确非空 / 非 None 的字段合并进 old。"""
    result = copy.deepcopy(old)

    def _is_present(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str) and v.strip() == "":
            return False
        if isinstance(v, (list, dict)) and len(v) == 0:
            return False
        return True

    for key, val in patch.items():
        if key == "io_constraints":
            if isinstance(val, dict):
                for sub in ("input", "output"):
                    if _is_present(val.get(sub)):
                        result.setdefault("io_constraints", {})
                        result["io_constraints"][sub] = val[sub]
            continue
        if _is_present(val):
            result[key] = copy.deepcopy(val)

    # 兜底：确保 io_constraints 永远存在
    result.setdefault("io_constraints", {"input": "", "output": ""})
    result.setdefault("reference_files", [])
    result.setdefault("target_modules", [])
    result.setdefault("edge_cases", [])
    result.setdefault("acceptance_criteria", [])
    return result


def _format_requirement_for_llm(req: dict[str, Any]) -> str:
    import json
    return json.dumps(req, ensure_ascii=False, indent=2)
