"""需求澄清节点组：
  1. clarify_validate  —— 程序校验：检查当前 requirement 是否完备
  2. clarify_extract   —— LLM 从用户对话中抽取信息，更新 requirement（合并已有值）
  3. clarify_build_question —— 根据缺失字段生成定向追问（追加到 messages 末尾作为 AIMessage）

循环路径（由主 graph 的路由函数控制）：
  validate → 有缺失 → build_question → 等待用户输入 → extract → validate → ...
  validate → 无缺失 → 进入下一阶段

追问有三种模式（state.clarify_mode）：
  normal    列表模式（默认）：一次把所有缺失字段列成问题清单
  brainstorm 头脑风暴：用户消息以「头脑风暴」开头进入；一次一问、探索式，
            每问附 2-3 个候选方向及推荐，帮用户把模糊想法聊成可填入需求清单的具体内容
  grill     拷问模式：用户消息以「拷问」开头进入；一次只问优先级最高的一个缺失项，
            并附「推荐答案」（可直接回复「同意」采纳），逐题施压验证需求完备性
  两种对话模式均以「退出头脑风暴 / 退出拷问」返回 normal；
  轮次上限单独放宽（CLARIFY_DIALOG_MAX_ROUNDS，默认 12；normal 仍为 CLARIFY_MAX_ROUNDS）

注意：对外同时暴露 sync/async 两份入口：
- sync 版 `clarify_extract` / `clarify_build_question` / `graph_generate` 用于 LangGraph .invoke() 同步流程
- async 版 `clarify_extract_async` / `clarify_build_question_async` / `graph_generate_async` 用于 .ainvoke() 和内部链
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage

from pydantic import BaseModel, Field, ValidationError

from ..config import settings
from ..errors import (
    CLARIFY_LOOP_EXHAUSTED,
    ClarifyLoopExhaustedError,
    DevFlowError,
    LlmOutputFormatError,
)
from ..llm_client import invoke_json, invoke_text
from ..schemas import (
    REQUIREMENT_SCHEMA,
    empty_requirement,
    validate_requirement,
)
from ..state import SOURCE_INFERRED, SOURCE_MOCK, SOURCE_USER, GlobalState

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# 澄清模式（brainstorm / grill）：入口指令识别
# ═══════════════════════════════════════════════════════════════════

# 模式关键词 → clarify_mode 值；检测顺序即优先级（先查退出，再查进入）
_MODE_KEYWORDS: dict[str, str] = {
    "头脑风暴": "brainstorm",
    "风暴": "brainstorm",
    "拷问": "grill",
}

_EXIT_PREFIXES = ("退出", "结束", "关闭", "停止")


def detect_mode_switch(text: str) -> tuple[str | None, str]:
    """识别用户消息里的澄清模式切换指令。

    返回 (新模式或 None, 剩余文本)：
      - 「拷问」/「头脑风暴」→ 进入对应模式，剩余文本为空
      - 「拷问：顺便重点关注边界」→ 进入 grill，剩余文本为指令后的补充内容（仍走抽取）
      - 「退出拷问」/「结束头脑风暴」→ 退回 normal
      - 普通消息 → (None, 原文)
    """
    t = (text or "").strip()
    if not t:
        return None, t

    # 退出：前缀 + 含任一模式关键词（如「退出拷问」「结束头脑风暴模式」）
    if t.startswith(_EXIT_PREFIXES) and any(k in t for k in _MODE_KEYWORDS):
        return "normal", ""

    for kw, mode in _MODE_KEYWORDS.items():
        if t == kw or t.startswith(kw):
            # 「拷问」/「头脑风暴」或其开头（容忍「拷问模式」「头脑风暴一下」）；
            # 跳过关键词与其后紧邻的分隔符，剩下的是用户附加语境（可为空）
            rest = t[len(kw):]
            rest = re.sub(r"^[\s：:，,。.]+", "", rest)
            return mode, rest.strip()
    return None, t


def _mode_announce(mode: str) -> str:
    """模式切换的确认话术（作为 AIMessage 追加，告知用户如何退出）。"""
    if mode == "brainstorm":
        return (
            "已进入【头脑风暴模式】。\n"
            "接下来一次只聊一个问题：我会先了解目的、约束和成功标准，"
            "每个问题给出几个候选方向和我的推荐，帮你把想法聊成完整需求。\n"
            "随时回复「退出头脑风暴」回到普通模式。"
        )
    if mode == "grill":
        return (
            "已进入【拷问模式】。\n"
            "接下来一次只问一个最关键的缺口：每个问题都附上我的推荐答案，"
            "回复「同意」直接采纳，或给出你自己的答案。\n"
            "随时回复「退出拷问」回到普通模式。"
        )
    return "已退出对话式澄清，回到普通模式。后续会一次性列出待补充清单。"


# ═══════════════════════════════════════════════════════════════════
# 承接上文：LLM 故障恢复后，用户用「继续」要求接着处理上一条没写入的消息
# ═══════════════════════════════════════════════════════════════════

# 承接指令识别：刻意收窄——只匹配极短、不含实质需求内容的指令，
# 避免把用户正常回复（如「继续说一下边界场景」）误判成承接。
_CONTINUATION_RE = re.compile(
    r"^(?:继续|继续吧|接着|接着说|接着来|重试|再试|再试一次|go\s*on|continue|retry)"
    r"[！!。.，,～~？?\s]*$",
    re.IGNORECASE,
)


def _is_continuation(text: str) -> bool:
    return bool(_CONTINUATION_RE.match((text or "").strip()))


def _collect_pending_user_context(
    messages: list, max_messages: int = 3, max_chars: int = 4000
) -> list[str]:
    """收集最后一条用户消息之前的近期用户消息原文（承接上文用）。

    场景：上一轮因 LLM 不可用抽取失败，恢复后用户只发「继续」——那条详细描述
    还在 checkpoint 的 messages 里，把它重放进抽取 prompt 才能承接上文。
    跳过本身是承接指令的消息（连续多次「继续」只取有内容的那条）。
    """
    collected: list[str] = []
    for m in reversed(messages):
        if getattr(m, "type", None) != "human":
            continue
        text = str(getattr(m, "content", "") or "").strip()
        if not text or _is_continuation(text):
            continue
        collected.append(text[:max_chars])
        if len(collected) >= max_messages:
            break
    collected.reverse()
    return collected


def _format_earlier_context(earlier: list[str]) -> str:
    """把待承接的用户原文渲染进抽取 prompt；空列表返回空串（不出现孤儿标题）。"""
    if not earlier:
        return ""
    return (
        "# 更早的未写入对话（此前因 LLM 故障没能写入需求清单）\n"
        + "\n".join(f"[用户] {t}" for t in earlier)
        + "\n"
    )


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
    inferred_fields: list[str] | None = Field(
        default=None,
        description=(
            "哪些字段是模型按提示词第 4 条从上下文【提炼/推断】出来的（非用户原话）。"
            "填字段名，io_constraints 的子字段写 io_constraints.input / io_constraints.output；"
            "用户原话里明确给出的字段不要列；没有推断就填 null。"
            "这些字段会在制图前被要求用户确认。"
        ),
    )


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
7. 如果「上一轮 AI 追问」存在：本轮用户消息若是对该追问的简短确认（如「同意」「可以」「就按你说的」），
   应结合追问里的推荐答案提取出对应字段值；无法对应时仍填 null。
8. inferred_fields：按第 4 条从上下文提炼/推断出来的字段要如实列出来（字段名；
   io_constraints 的子字段写 io_constraints.input / io_constraints.output）。
   用户原话里明确给出的字段不要列进来；没有任何推断就填 null。
   这些字段会在制图前被要求用户确认，如实标注能减少返工。
9. 如果存在「更早的未写入对话」且【本轮用户新输入】只是「继续/重试」等承接指令（没有新信息），
   说明之前有一条因 LLM 故障没被写入的需求描述——改从「更早的未写入对话」中抽取；
   本轮输入有真实内容时，以本轮输入为准，更早内容仅作理解参考。
"""


USER_PROMPT_TEMPLATE = """
# 已有的需求上下文（仅供参考，不要覆盖你不确定的字段）
已有需求：
{existing_requirement}

# 上一轮 AI 追问（可能为空；用户本轮若只是简短确认，从这里对应字段与推荐答案）
{latest_ai_message}

{earlier_context}
# 本轮用户新输入
{latest_user_message}

请只从【本轮用户新输入】中抽取明确提到的信息；如果已有需求中某个字段用户本轮没提，就填 null 让它保留旧值。
若【本轮用户新输入】只是「继续/重试」类承接指令且没有新信息，则改从上方「更早的未写入对话」中抽取
（那些内容此前因 LLM 故障没能写入需求清单）。
"""


def clarify_extract(state: GlobalState) -> dict[str, Any]:
    """同步入口（LangGraph .invoke 使用）：内部用 asyncio.run 跑 async 实现。"""
    return asyncio.run(clarify_extract_async(state))


async def clarify_extract_async(state: GlobalState) -> dict[str, Any]:
    """异步入口：从最新一轮用户对话中抽取信息，合并更新 requirement。

    输入（读 state）: messages, requirement（旧值）, clarify_mode
    输出（写回 state）: requirement（新合并值）, clarify_mode, retry_count,
                       last_error_code, last_error_retryable

    模式指令短路：最新用户消息若是「头脑风暴 / 拷问 / 退出…」切换指令，
    直接更新 clarify_mode 并追加确认消息，不做需求抽取（指令不是需求信息）；
    「拷问：补充语境」这类带后缀的指令切换模式后，用剩余文本继续正常抽取。
    """
    messages = state.get("messages", [])
    existing = copy.deepcopy(state.get("requirement") or empty_requirement())

    # 1. 找最后一条 HumanMessage / AIMessage
    latest_user_text = ""
    latest_ai_text = ""
    for m in reversed(messages):
        mtype = getattr(m, "type", None)
        if mtype == "human" and not latest_user_text:
            latest_user_text = getattr(m, "content", "") or ""
        elif mtype == "ai" and not latest_ai_text:
            latest_ai_text = getattr(m, "content", "") or ""
        if latest_user_text and latest_ai_text:
            break

    ok_ret = {"last_error": None, "last_error_code": None, "last_error_retryable": None}

    # 若完全没用户输入，直接返回（不做事）
    if not latest_user_text.strip():
        return {"requirement": existing, "clarify_round_user_chars": 0, **ok_ret}

    # 1.5 模式切换指令识别（短路）；mode_update 会透传到本轮所有返回路径
    mode_update: dict[str, Any] = {}
    new_mode, residual = detect_mode_switch(latest_user_text)
    if new_mode is not None:
        mode_update["clarify_mode"] = new_mode
        if new_mode != (state.get("clarify_mode") or "normal"):
            mode_update["messages"] = [AIMessage(content=_mode_announce(new_mode))]
        if residual:
            latest_user_text = residual  # 指令后带的补充内容仍做一次抽取
        else:
            return {"requirement": existing, "clarify_round_user_chars": 0, **ok_ret, **mode_update}

    # 1.6 承接上文：本轮输入只是「继续/重试」类指令 → 把之前没写进需求的用户原文
    # 一并放进 prompt（典型场景：LLM 故障期间回答没写入，恢复后用户发「继续」）。
    earlier_context: list[str] = []
    if _is_continuation(latest_user_text):
        earlier_context = _collect_pending_user_context(messages)
        if not earlier_context:
            # 没有可承接的上文：「继续」本身不含需求信息，不浪费这次 LLM 调用；
            # 键与正常返回路径对齐（短输入不算「零抽取静默失败」）
            return {
                "requirement": existing,
                "clarify_round_user_chars": len(latest_user_text.strip()),
                "clarify_round_no_progress": False,
                **ok_ret,
                **mode_update,
            }

    # 2. LLM 结构化抽取
    # 走 json_schema 档（裸补全 + 本地解析）而非 response_model 的 function_calling 档：
    # 部分 OpenAI 兼容网关的 tool_call 参数会被截断——实测同一网关 function_calling 只填
    # req_type + target_modules 两个字段、其余全 null，残缺需求被当成功、下游反复追问；
    # json_mode 则 9 个字段全填齐。字段类型安全由下方 model_validate 兜底（不合规 → 可重试）。
    mock_flag: dict[str, Any] = {}
    try:
        extracted = await invoke_json(
            system_prompt=SYSTEM_PROMPT_EXTRACT,
            user_prompt=USER_PROMPT_TEMPLATE.format(
                existing_requirement=_format_requirement_for_llm(existing),
                latest_ai_message=latest_ai_text or "（无）",
                earlier_context=_format_earlier_context(earlier_context),
                latest_user_message=latest_user_text,
            ),
            json_schema=RequirementExtract.model_json_schema(),
            response_type="requirement_extract",
            meta=mock_flag,
        )
        # json_schema 档只保证「是 JSON object」，字段级类型安全在这里补校验；
        # 不合规抛可重试的 OUTPUT_FORMAT，由既有的 except DevFlowError 路径提示重试。
        try:
            RequirementExtract.model_validate(extracted)
        except ValidationError as ve:
            raise LlmOutputFormatError(
                f"需求抽取结果不符合 RequirementExtract schema: {ve}",
                validation_errors=[err["msg"] for err in ve.errors()],
                extra={"raw_tail": str(extracted)[:200]},
            ) from ve
    except DevFlowError as e:
        retry = state.get("retry_count", {}) or {}
        retry["clarify_extract"] = retry.get("clarify_extract", 0) + 1
        return {
            "last_error": f"[clarify_extract:{e.code}] {e.message}",
            "last_error_code": e.code,
            "last_error_retryable": e.retryable,
            "retry_count": retry,
            **mode_update,
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
            **mode_update,
        }

    # 2.5 来源标注（B）：模型自报哪些字段是提炼/推断的 → 制图前的需求确认门禁据此
    # 只对「脑补字段」要求确认。未知来源（老会话 / 模型没报）一律按需确认（保守）。
    # mock 兜底（真实 provider 全挂）返回的是编造的演示需求：来源统一标 SOURCE_MOCK，
    # 不冒充用户原话/AI 推断——下轮真实抽取会据此覆盖这些字段。
    extracted = dict(extracted)
    declared = {str(x).strip() for x in (extracted.pop("inferred_fields", None) or [])}
    used_mock = bool(mock_flag.get("mock"))
    sources = dict(state.get("requirement_sources") or {})
    for key, val in extracted.items():
        if key == "io_constraints":
            if isinstance(val, dict):
                for sub in ("input", "output"):
                    if _is_present(val.get(sub)):
                        dotted = f"io_constraints.{sub}"
                        if used_mock:
                            sources[dotted] = SOURCE_MOCK
                        else:
                            sources[dotted] = (
                                SOURCE_INFERRED
                                if "io_constraints" in declared or dotted in declared
                                else SOURCE_USER
                            )
            continue
        if _is_present(val):
            if used_mock:
                sources[key] = SOURCE_MOCK
            else:
                sources[key] = SOURCE_INFERRED if key in declared else SOURCE_USER

    # 3. 合并：已有值优先，只覆盖本轮提取中明确非空的字段。
    # 真实抽取先剔除旧值里来源为 mock 的字段（编造数据让位给真实内容；
    # 未被本轮覆盖的 mock 字段也会被清掉 → validate 会重新追问）。
    if not used_mock:
        existing, removed_mock_keys = _strip_mock_sourced(existing, sources)
        for k in removed_mock_keys:
            sources.pop(k, None)
    merged = _merge_requirement(existing, extracted)

    # 无进展观测：抽取「成功」但什么都没抽到，而用户输入相当具体——多半是模型
    # 没按 schema 输出（成功返回了空壳），在日志里留个显眼线索
    if merged == existing and len(latest_user_text.strip()) >= 30:
        logger.warning(
            "[clarify] 需求抽取未产出任何新字段（用户输入 %d 字）——"
            "疑似模型未按 schema 输出，请检查 LLM 日志中的 LLM.OUTPUT_FORMAT",
            len(latest_user_text.strip()),
        )

    # 4. 会话命名：首次拿到主要功能时生成（LLM 起名，失败退 project_context 截断）。
    # mock 兜底的「需求」是编造的演示数据，不能拿它给真实会话起名。
    if used_mock:
        title_update: dict[str, Any] = {}
    else:
        title_update = await _ensure_session_title(state, merged)

    out_req: dict[str, Any] = {
        "requirement": merged,
        "requirement_sources": sources,
        # 本轮用户输入长度 + 是否「有输入但啥也没抽出」：build_question 据此识别
        # 结构化输出静默失败（长输入零抽取，首轮也会命中），明确告知用户回答没写入
        "clarify_round_user_chars": len(latest_user_text.strip()),
        "clarify_round_no_progress": merged == existing
        and len(latest_user_text.strip()) >= 30,
        **title_update,
        "last_error": None,
        "last_error_code": None,
        "last_error_retryable": None,
        **mode_update,
    }
    if merged != existing:
        # 抽取到新信息 → 之前的需求确认失效，制图前要重新过一遍确认门禁
        out_req["requirement_confirmed"] = False
    return out_req


def clarify_validate(state: GlobalState) -> dict[str, Any]:
    """节点：纯程序校验 requirement 是否完备，不调用 LLM。

    输出：missing_fields（错误/缺失列表）+ current_stage

    澄清轮次控制（评审稿 §2.3 D2）：
      - 每次进入把 retry_count["clarify_loop_cnt"] +1
      - 达到轮次上限仍缺失 → 写 CLARIFY.LOOP_EXHAUSTED（不可重试），
        由 _route_after_validate 路由到 abort → dead_letter → END，避免无限追问循环。
      - 对话式澄清（头脑风暴 / 拷问）一轮只推进一个缺口，上限放宽为
        CLARIFY_DIALOG_MAX_ROUNDS（默认 12）；normal 仍为 CLARIFY_MAX_ROUNDS（默认 6）。
    """
    req = state.get("requirement") or empty_requirement()
    errors = validate_requirement(req)

    mode = state.get("clarify_mode") or "normal"
    max_rounds = (
        settings.CLARIFY_DIALOG_MAX_ROUNDS if mode in ("brainstorm", "grill")
        else settings.CLARIFY_MAX_ROUNDS
    )

    retry = dict(state.get("retry_count") or {})
    retry["clarify_loop_cnt"] = retry.get("clarify_loop_cnt", 0) + 1

    if errors and retry["clarify_loop_cnt"] > max_rounds:
        err = ClarifyLoopExhaustedError(
            f"澄清已达 {max_rounds} 轮上限（{mode} 模式），仍有 {len(errors)} 项缺失: "
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

    # 需求完备时的确定性确认（不花 LLM token）：
    #   一次说清 → 「需求已足够清晰，无需再发散」；补齐缺口 → 「缺口已补齐」。
    # 只在完备的那一刻发一次（上轮有缺口 / 首轮直达），不会每轮重复。
    confirm: str | None = None
    if not errors:
        old_missing = state.get("missing_fields") or []
        if old_missing:
            confirm = (
                f"✅ 缺口已补齐（{len(old_missing)} 项 → 0 项）。"
                "需求清单已完整，接下来请你确认字段清单，确认后进入制图。"
            )
        elif retry["clarify_loop_cnt"] <= 1:
            confirm = (
                "✅ 你的需求描述已足够清晰，关键信息齐备，无需再发散澄清。"
                "接下来请你逐项确认需求清单（可直接修改字段），确认后进入逻辑制图。"
            )

    out: dict[str, Any] = {
        "missing_fields": errors,
        "current_stage": "clarify" if errors else "graph",  # 阶段一跳过 search，直接去制图
        "retry_count": retry,
        "clarify_mode_prompt": False,  # 进入追问 / 完备确认后选择卡即收
    }
    if confirm is not None:
        # 键序：stage 事件先触发（分隔线），确认消息随后落在下方
        out["messages"] = [AIMessage(content=confirm)]
    return out


SYSTEM_PROMPT_QUESTION = """你是一个沟通能力很强的产品经理。根据【缺失字段列表】和【当前已填内容】，
生成一个【定向追问】的问题清单，一次性问清缺失项，不要闲聊。
要求：
1. 每个缺失字段问一条具体问题，问题要口语化、可被用户直接回答；
2. 对空数组类字段（target_modules/edge_cases/acceptance_criteria），举例告诉用户怎么填；
3. 不要问已经填好的字段；
4. 输出为纯文本，多问题用换行分隔，不要 JSON 格式。
"""


SYSTEM_PROMPT_SESSION_TITLE = """你是一个需求分析师。根据需求信息，给这个需求起一个简短、具体的名称，
用作会话列表里的展示名。
要求：
1. 只输出名称本身，不要引号、句号、前缀或任何解释，不要换行；
2. 长度 4-12 个字，体现主要功能或模块（如「桌面计算器科学计算」「订单导出Excel」）；
3. 不要用「需求」「功能」「模块」这类空泛词收尾。
"""


async def _ensure_session_title(state: GlobalState, requirement: dict[str, Any]) -> dict[str, Any]:
    """首次拿到 project_context 时生成会话名称（一次性，供会话列表展示）。

    LLM 起名失败（熔断 / 无 provider / 输出不可用）→ 退回 project_context 截断；
    project_context 一直缺失时澄清循环本来就会追问它（「项目背景/主要功能」即
    project_context），无需额外的提问环节。命名是锦上添花，任何异常都不得影响
    抽取主流程。
    """
    if str(state.get("session_title") or "").strip():
        return {}
    ctx = str(requirement.get("project_context") or "").strip()
    if not ctx:
        return {}
    name = ""
    try:
        raw = (await invoke_text(
            system_prompt=SYSTEM_PROMPT_SESSION_TITLE,
            user_prompt=(
                "需求信息：\n"
                f"{_format_requirement_for_llm(requirement)}\n\n"
                "请输出会话名称（只输出名称本身）："
            ),
            max_retries=0,  # 命名不值得重试拖慢主流程，失败直接截断兜底
        )).strip()
        if raw and "mock fallback" not in raw:
            name = raw.splitlines()[0].strip().strip("\"'「」『』` ").rstrip("。.，,")[:30]
    except Exception as e:  # noqa: BLE001 - 命名尽力而为
        logger.info("[clarify] LLM 会话起名失败，退回 project_context 截断: %s", e)
    return {"session_title": name or ctx[:20]}


# ═══════════════════════════════════════════════════════════════════
# 对话式澄清（brainstorm / grill）：一轮一问
# ═══════════════════════════════════════════════════════════════════

# 缺失字段的提问优先级：越靠前越影响下游阶段形态，先问
_FIELD_PRIORITY = [
    "req_type",
    "existing_code_accessible",
    "project_context",
    "io_constraints.input",
    "io_constraints.output",
    "edge_cases",
    "acceptance_criteria",
    "project_root",
    "target_modules",
]


def pick_dialog_field(missing: list[str]) -> tuple[str, str]:
    """从缺失列表中选出本轮该问的字段。

    返回 (字段 key, 完整缺失条目)。缺失条目形如 "edge_cases: 至少列出 1 个边界场景"。
    按提优先级表排序；不在表中的字段排在已知字段之后，保持原有相对顺序。
    """
    def _rank(entry: str) -> tuple[int, int]:
        key = entry.split(":", 1)[0].strip()
        try:
            return (_FIELD_PRIORITY.index(key), 0)
        except ValueError:
            return (len(_FIELD_PRIORITY), 1)

    ordered = sorted(list(missing), key=_rank)
    top = ordered[0]
    return top.split(":", 1)[0].strip(), top


SYSTEM_PROMPT_GRILL = """你是「拷问模式」下的资深需求评审，对需求做逐题施压验证。
本轮只允许问【一个】指定字段的缺口，绝对不要列出其他缺失项。
要求：
1. 针对该字段提出一个尖锐、具体的问题，点明它模糊或缺失会让下游（制图 / 用例设计）付出什么代价；
2. 必须给出「推荐答案」并用一句话说明理由（从已有需求上下文合理推断）；
3. 结尾明确告知用户：回复「同意」采纳推荐答案，或直接给出你自己的答案；
4. 输出纯文本，控制在 6 行以内，不要 JSON。
"""


SYSTEM_PROMPT_BRAINSTORM = """你是「头脑风暴模式」下的产品伙伴，帮用户把模糊想法聊成完整需求。
本轮只允许问【一个】问题，聚焦目的 / 约束 / 成功标准，探索式引导而不是质询。
要求：
1. 围绕指定缺口提问，问题开放、口语化；
2. 给出 2-3 个候选方向，每个一句话（可从已有需求上下文推断），并标出你的推荐；
3. 告诉用户可以选方向、自由描述，或说「你来定」；
4. 输出纯文本，控制在 7 行以内，不要 JSON，不要一次列出所有缺口。
"""


def clarify_build_question(state: GlobalState) -> dict[str, Any]:
    """同步入口（LangGraph .invoke 使用）：内部用 asyncio.run 跑 async 实现。"""
    return asyncio.run(clarify_build_question_async(state))


async def clarify_build_question_async(state: GlobalState) -> dict[str, Any]:
    """异步入口：根据 missing_fields 与 clarify_mode 生成追问，作为 AIMessage 追加到 messages。

    normal     —— 一次列出所有缺失字段的问题清单（原有行为）；
                  首轮置 clarify_mode_prompt=True，客户端据此弹「头脑风暴 / 拷问」选择卡
    brainstorm —— 一轮一问：围绕优先级最高的缺口，给 2-3 个候选方向及推荐
    grill      —— 一轮一问：只问优先级最高的一个缺口，附推荐答案，「同意」即可采纳
    """
    missing = state.get("missing_fields") or []
    req = state.get("requirement") or empty_requirement()
    mode = state.get("clarify_mode") or "normal"

    if not missing:
        return {}

    if mode in ("brainstorm", "grill"):
        field_key, field_entry = pick_dialog_field(missing)
        system_prompt = SYSTEM_PROMPT_GRILL if mode == "grill" else SYSTEM_PROMPT_BRAINSTORM
        mode_name = "拷问" if mode == "grill" else "头脑风暴"
        user_prompt = (
            f"【本轮只问这一个缺口】\n字段: {field_key}\n缺失原因: {field_entry}\n\n"
            f"【当前已填内容】\n{_format_requirement_for_llm(req)}\n\n"
            "请输出追问（纯文本）："
        )
        fallback = f"（{mode_name}模式）请先补充【{field_key}】：{field_entry}"
    else:
        system_prompt = SYSTEM_PROMPT_QUESTION
        user_prompt = (
            f"【缺失字段 / 错误】\n{chr(10).join('- ' + m for m in missing)}\n\n"
            f"【当前已填内容】\n{_format_requirement_for_llm(req)}\n\n"
            "请输出追问文本（纯文本多行）："
        )
        fallback = "需要您补充以下信息：\n" + "\n".join(f"- {m}" for m in missing)

    try:
        # 追问产物是纯文本（system prompt 明确「不要 JSON 格式」），必须走 invoke_text。
        # 若走 invoke_json，JsonOutputParser 会把「1. …」这类数字开头的纯文本宽容解析成
        # 裸 int，下标取值直接抛 'int' object is not subscriptable（弱模型必踩）。
        text = str(await invoke_text(system_prompt=system_prompt, user_prompt=user_prompt)).strip()
        if not text:
            text = fallback
    except DevFlowError as e:
        text = fallback + f"\n(LLM 出错: [{e.code}] {e.message})"
    except Exception as e:
        # 兜底：直接把缺失字段原样展示给用户
        text = fallback + f"\n(LLM 出错: {e})"

    # 失败可见化（两条触发路径）：
    #   a) last_error 非空 → 上轮 clarify_extract 抛错（成功时节点会写 None 清掉）；
    #   b) 上轮用户输入很长（≥30 字）却什么都没抽出来（requirement 未变）——
    #      多为结构化输出没按 schema 落地（静默空壳/截断），首轮也会命中。
    # 命中任一即明说「回答没被写入」，否则表现为反复重复同一份问题清单，
    # 用户无从知道是抽取在失败还是需求本身不完整。
    err_code = state.get("last_error_code")
    if err_code:
        err_msg = str(state.get("last_error") or "")[:160]
        text = (
            f"⚠️ 上一轮回答的需求抽取失败（[{err_code}] {err_msg}），"
            "你的回答这次没能写入需求清单，请重试一次或换种说法。\n\n" + text
        )
    elif state.get("clarify_round_no_progress"):
        text = (
            "⚠️ 你上一条消息内容不少，但本轮没能从中抽取到新的需求字段"
            "（疑似模型输出异常或被截断），你的回答这次没能写入需求清单。"
            "请重试一次或把关键信息拆开逐条说明。\n\n" + text
        )

    # 选择卡只在普通模式首轮出现一次（键序保证事件在 AI 追问之后触发，卡片落在问题下方）
    loop_cnt = (state.get("retry_count") or {}).get("clarify_loop_cnt", 0)
    mode_prompt = mode == "normal" and loop_cnt <= 1

    return {
        "messages": [AIMessage(content=text)],
        "clarify_mode_prompt": mode_prompt,
    }


# ═══════════════════════════════════════════════════════════════════
# 内部工具函数
# ═══════════════════════════════════════════════════════════════════

def _is_present(v: Any) -> bool:
    """字段是否算「有值」：None / 空串 / 空容器都不算。"""
    if v is None:
        return False
    if isinstance(v, str) and v.strip() == "":
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True


def _strip_mock_sourced(
    req: dict[str, Any], sources: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """把来源为 mock 兜底的字段从 requirement 里剔除（点路径支持 io_constraints.*）。

    mock 编造的数据不能在真实抽取后继续冒充有效值：剔除后 merge 只写真实抽到的
    字段，其余 mock 字段回归缺失 → validate 重新追问。返回 (剔除后的 requirement,
    被剔除的点路径列表)，调用方应同步清掉这些字段的来源标记。
    """
    out = copy.deepcopy(req)
    removed: list[str] = []
    for dotted, src in (sources or {}).items():
        if src != SOURCE_MOCK or not dotted:
            continue
        parts = dotted.split(".")
        cur: Any = out
        for p in parts[:-1]:
            nxt = cur.get(p) if isinstance(cur, dict) else None
            if not isinstance(nxt, dict):
                cur = None
                break
            cur = nxt
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
            removed.append(dotted)
    return out, removed


def _merge_requirement(old: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把 patch 中明确非空 / 非 None 的字段合并进 old。"""
    result = copy.deepcopy(old)

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
