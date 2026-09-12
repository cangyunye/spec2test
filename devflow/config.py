"""全局配置：从 .env / 环境变量读取。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


# ═══════════════════════════════════════════════════════════════════
# 结构化 LLM Provider 配置
# ═══════════════════════════════════════════════════════════════════

LlmProviderSpec = dict[str, Any]
"""单个 LLM 提供商配置，字段如下：
- name:        str    标识名（用于熔断器命名、日志、去重缓存）
- base_url:    str    OpenAI 兼容接口地址，如 DeepSeek=https://api.deepseek.com/v1
- api_key:     str    API Key，不传则回退全局 LLM_API_KEY
- model:       str    激活模型名，如 deepseek-v4-flash / glm-5.3 / qwen3.8-max
- temperature: float  采样温度，默认 0.1
- headers:     dict   可选，附加请求头（透传 ChatOpenAI default_headers）。
                      OpenCode Go 网关要求 x-opencode-session 会话头

多模型池：LLM_PROVIDERS_JSON 条目可带可选 "models": [模型名列表]（同 base_url 的
多个模型）。解析时展开为顺序 fallback 链：激活模型沿用原 name 排最前，其余模型
依次跟随、命名 "name:model"，各自拥有独立熔断器。
LLM_ACTIVE_MODEL 环境变量可切换激活模型（不改 JSON）：裸模型名匹配任意 provider
的池，"provider名/模型名" 只匹配对应 provider。
"""


# ═══════════════════════════════════════════════════════════════════
# 已知模型官方上下文窗口（tokens）：按模型名前缀匹配，最长前缀优先。
# 数字来自官方文档（2026-09 查证）：
#   DeepSeek V4 官宣 1M，但官方 API 实测仍按 200K 限制（cherry-studio#14789）→ 保守取 200K；
#   deepseek-chat / deepseek-flash（V4.1 Flash）官方 API 档 128K
#   GLM-5.2 / GLM-5.3 = 1M；GLM-5 / GLM-4.6 = 200K        （docs.z.ai/guides/llm/glm-5.3）
#   Qwen3.8-Max ≈ 1M（983,616）；qwen3.8-flash 原生 262,144 （qwen.ai 官方博客）
#   Kimi K3 = 1M；K2.x 系列 = 256K                         （platform.kimi.ai）
_MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("deepseek-v4", 200_000),
    ("deepseek", 128_000),
    ("kimi-k3", 1_000_000),
    ("kimi", 256_000),
    ("glm-5.3", 1_000_000),
    ("glm-5.2", 1_000_000),
    ("glm-5", 200_000),
    ("glm-4", 200_000),
    ("qwen3.8-max", 1_000_000),
    ("qwen3.8", 262_144),
    ("qwen3", 262_144),
    ("qwen", 131_072),
)

# 自定义 / 未收录模型的上下文窗口下限：现代模型均在 128k 以上
MIN_CONTEXT_WINDOW_TOKENS = 128_000


def resolve_context_window(model: str, explicit: int | None = None) -> int:
    """解析某模型的上下文窗口（tokens）。

    优先级：provider 条目显式 context_window > 官方规格表（最长前缀命中）
    > 全局 LLM_CONTEXT_WINDOW_TOKENS（但不低于 128k 下限——自定义模型至少 128k）。
    """
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        return explicit
    name = (model or "").strip().lower()
    best_prefix, best_window = "", 0
    for prefix, window in _MODEL_CONTEXT_WINDOWS:
        if name.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix, best_window = prefix, window
    if best_window:
        return best_window
    try:
        fallback = int(os.getenv("LLM_CONTEXT_WINDOW_TOKENS", "128000"))
    except ValueError:
        fallback = MIN_CONTEXT_WINDOW_TOKENS
    return max(fallback, MIN_CONTEXT_WINDOW_TOKENS)


def _context_window_for(model: str, cw: Any) -> int | None:
    """provider 条目的 context_window 字段 → 该模型的显式值。

    int 作用于该供应商全部模型；dict 按模型名单独指定（如
    {"deepseek-v4-pro": 200000}）；bool/负数/0 等脏值一律忽略。
    """
    if isinstance(cw, bool):
        return None
    if isinstance(cw, int) and cw > 0:
        return cw
    if isinstance(cw, dict):
        v = cw.get(model)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return int(v)
    return None


def _resolve_active_model(pool: list[str], provider_name: str) -> str:
    """按 LLM_ACTIVE_MODEL 从模型池选激活模型；不匹配则用池里第一个。"""
    override = os.getenv("LLM_ACTIVE_MODEL", "").strip()
    if override and override in pool:
        return override
    if override and "/" in override:
        owner, _, model = override.partition("/")
        if owner == provider_name and model in pool:
            return model
    return pool[0]


def _parse_llm_providers_from_env() -> list[LlmProviderSpec]:
    """解析 LLM_PROVIDERS_JSON 环境变量，作为结构化 fallback 链。

    支持两种方式，优先 JSON：
      1) LLM_PROVIDERS_JSON: 完整列表，如
           [{"name":"primary","base_url":"https://api.deepseek.com/v1","model":"deepseek-v4-flash"},...]
         条目可选 "models": [模型池]，展开为同供应商多模型 fallback 链
         条目可选 "context_window"：int 作用于该供应商全部模型；dict 按模型名单独指定。
         未配置时按模型名查官方规格表（_MODEL_CONTEXT_WINDOWS），未收录模型不低于 128k。
      2) 旧版环境变量组合（JSON 未配时自动生成单元素列表）：
           LLM_BASE_URL + LLM_API_KEY + LLM_MODEL + LLM_FALLBACKS(追加 + mock)
    """
    raw = os.getenv("LLM_PROVIDERS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                result: list[LlmProviderSpec] = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    if "base_url" not in item:
                        continue
                    # 模型池：models 列表为准，model 单值前置（不在池里则并入池首）
                    pool = [
                        m for m in item.get("models", [])
                        if isinstance(m, str) and m.strip()
                    ]
                    model = item.get("model")
                    if isinstance(model, str) and model.strip():
                        if model not in pool:
                            pool.insert(0, model)
                    if not pool:
                        continue
                    seen: set[str] = set()
                    pool = [m for m in pool if not (m in seen or seen.add(m))]
                    base = {
                        "base_url": item["base_url"],
                        "api_key": item.get("api_key")
                        or os.getenv("LLM_API_KEY", "sk-dummy-key"),
                        "temperature": float(item.get("temperature", 0.1)),
                    }
                    # 可选请求头（如 OpenCode Go 要求 x-opencode-session），统一作用于该供应商展开的全部模型
                    headers = item.get("headers")
                    if isinstance(headers, dict) and headers:
                        base["headers"] = {
                            str(k): str(v) for k, v in headers.items() if str(v).strip()
                        }
                    name = item.get("name") or f"llm-{len(result)}"
                    active = _resolve_active_model(pool, name)
                    ordered = [active, *(m for m in pool if m != active)]
                    for i, m in enumerate(ordered):
                        spec: LlmProviderSpec = {
                            "name": name if i == 0 else f"{name}:{m}",
                            **base,
                            "model": m,
                        }
                        cw_val = _context_window_for(m, item.get("context_window"))
                        if cw_val is not None:
                            spec["context_window"] = cw_val
                        result.append(spec)
                if result:
                    return result
        except json.JSONDecodeError:
            pass  # 格式错则回退旧版

    # 旧版兼容路径：LLM_BASE_URL + LLM_MODEL + LLM_FALLBACKS 拼接
    primary = {
        "name": "primary",
        "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        "api_key": os.getenv("LLM_API_KEY", "sk-dummy-key"),
        "model": os.getenv("LLM_MODEL", "deepseek-flash"),
        "temperature": float(os.getenv("LLM_TEMPERATURE", "0.1")),
    }
    providers = [primary]

    for fb in os.getenv("LLM_FALLBACKS", "mock").split(","):
        fb = fb.strip()
        if not fb:
            continue
        if fb == "mock":
            # mock 用独立标志位处理，不进 providers 列表
            continue
        providers.append({
            "name": fb if "/" not in fb else fb.split("/", 1)[1],
            "base_url": primary["base_url"],
            "api_key": primary["api_key"],
            "model": fb,
            "temperature": primary["temperature"],
        })
    return providers


class Settings:
    def __init__(self) -> None:
        # ── LLM（默认 DeepSeek，OpenAI 兼容模式）─────────
        self.LLM_API_KEY: str = os.getenv("LLM_API_KEY", "sk-dummy-key")
        self.LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        self.LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-v4-flash")
        self.LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))

        # SPEC 5.3 结构化多提供商 fallback 链（OpenAI 兼容模式统一走 ChatOpenAI）
        self.LLM_PROVIDERS: list[LlmProviderSpec] = _parse_llm_providers_from_env()

        # SPEC 5.3 是否将 mock 作为最终兜底（推荐开启）
        # 条件：LLM_FALLBACKS 含 "mock"，或没配 LLM_PROVIDERS_JSON（默认模式）
        fallbacks_str = os.getenv("LLM_FALLBACKS", "mock")
        fb_list = [s.strip() for s in fallbacks_str.split(",")]
        self.LLM_USE_MOCK_FALLBACK: bool = (
            "mock" in fb_list or not os.getenv("LLM_PROVIDERS_JSON")
        )

        # SPEC 5.5 每日 token 预算，0 = 不限
        self.LLM_TOKEN_BUDGET_DAILY: int = int(os.getenv("LLM_TOKEN_BUDGET_DAILY", "0"))
        # 全局 context window 兜底（tokens，粗略按 1 token ≈ 3.5 chars 估算）：
        # 未知/自定义模型的下限（不低于 128k）；已知模型按 config._MODEL_CONTEXT_WINDOWS
        # 官方规格、或 provider 条目显式 context_window 优先（见 resolve_context_window）
        self.LLM_CONTEXT_WINDOW_TOKENS: int = int(
            os.getenv("LLM_CONTEXT_WINDOW_TOKENS", "128000")
        )

        # ── 需求澄清（SPEC 阶段一）────────────────────────
        # 澄清循环硬上限（达到后仍有缺失 → 终止并记死信）
        self.CLARIFY_MAX_ROUNDS: int = int(os.getenv("CLARIFY_MAX_ROUNDS", "6"))
        # 对话式澄清（头脑风暴 / 拷问）一轮只问一个问题，消耗轮数更快，上限单独放宽
        self.CLARIFY_DIALOG_MAX_ROUNDS: int = int(os.getenv("CLARIFY_DIALOG_MAX_ROUNDS", "12"))

        # ── Checkpoint ────────────────────────────────────
        self.CHECKPOINT_SQLITE_PATH: Path = Path(
            os.getenv("CHECKPOINT_SQLITE_PATH", "./data/checkpoints.db")
        )

        # ── OpenCode（可选增强后端；默认主路径为 LLM 直连，不配置即自动 Mock）──
        self.OPENCODE_BASE_URL: str = os.getenv("OPENCODE_BASE_URL", "http://localhost:8080")
        self.OPENCODE_API_TOKEN: str = os.getenv("OPENCODE_API_TOKEN", "")

        # ── OpenCode CLI（opencode run 子进程，测试设计技能执行器）──
        self.OPENCODE_BIN: str = os.getenv("OPENCODE_BIN", "opencode")
        # 技能派发使用的 agent 名（需在目标项目 opencode 配置里存在）
        self.OPENCODE_AGENT: str = os.getenv("OPENCODE_AGENT", "test-designer")
        # opencode run 单次调用超时；附加 CLI 参数（按空白切分，如 --auto）
        self.OPENCODE_RUN_TIMEOUT_SEC: int = int(os.getenv("OPENCODE_RUN_TIMEOUT_SEC", "900"))
        self.OPENCODE_EXTRA_ARGS: list[str] = os.getenv("OPENCODE_EXTRA_ARGS", "").split()

        # ── 测试设计 feature 化（可选流程，默认单次整单）──
        # feature | single：single 保持一次性整单设计（模型可靠时的默认）；
        # feature 走「拆分 → 逐 feature 生成 → 单元评审 → 合并」，供精细控制
        # 会话上下文 / 按功能点派发子代理时选用
        self.TEST_DESIGN_MODE: str = os.getenv("TEST_DESIGN_MODE", "single")
        self.TEST_DESIGN_MAX_FEATURES: int = int(os.getenv("TEST_DESIGN_MAX_FEATURES", "8"))
        self.TEST_DESIGN_CONCURRENCY: int = int(os.getenv("TEST_DESIGN_CONCURRENCY", "3"))
        # 每 feature 校验不过时的最大回炉轮数
        self.TEST_DESIGN_REVIEW_ROUNDS: int = int(os.getenv("TEST_DESIGN_REVIEW_ROUNDS", "2"))
        # 技能执行器（可选）：默认关闭；开启后逐 feature 派发时显式传 SKILL.md
        # 绝对路径并要求执行器先完整读取
        self.TEST_DESIGN_USE_SKILLS: bool = os.getenv(
            "TEST_DESIGN_USE_SKILLS", "0"
        ).strip().lower() not in ("0", "false", "no", "off")
        self.TEST_DESIGN_SKILL_DIR: Path = Path(
            os.getenv("TEST_DESIGN_SKILL_DIR", str(Path(__file__).resolve().parent.parent / ".agents" / "skills"))
        )

        # ── 执行闭环（diff 落盘 + 真实测试执行）────────────
        self.APPLY_CODE_ENABLED: bool = os.getenv(
            "APPLY_CODE_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        self.TEST_RUN_ENABLED: bool = os.getenv(
            "TEST_RUN_ENABLED", "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        # 单轮 pytest 超时（秒），超时按「未执行」处理而不是误判失败
        self.TEST_RUN_TIMEOUT_SEC: int = int(os.getenv("TEST_RUN_TIMEOUT_SEC", "300"))
        # 测试连续失败时自动回 code_gen 修复的最大轮数；超出后带失败报告进人工验收
        self.TEST_RUN_MAX_FIX_ROUNDS: int = int(os.getenv("TEST_RUN_MAX_FIX_ROUNDS", "2"))
        # 限定 pytest 收集范围（逗号分隔相对路径）；留空 = 整个项目
        self.TEST_RUN_PATHS: list[str] = [
            s.strip() for s in os.getenv("TEST_RUN_PATHS", "").split(",") if s.strip()
        ]

        # ── 记忆策略 ──────────────────────────────────────
        # 保留最近 N 轮对话
        self.HOT_MEMORY_LAST_N: int = int(os.getenv("HOT_MEMORY_LAST_N", "5"))


settings = Settings()

# 确保 SQLite 目录存在
settings.CHECKPOINT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
