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

多模型池：LLM_PROVIDERS_JSON 条目可带可选 "models": [模型名列表]（同 base_url 的
多个模型）。解析时展开为顺序 fallback 链：激活模型沿用原 name 排最前，其余模型
依次跟随、命名 "name:model"，各自拥有独立熔断器。
LLM_ACTIVE_MODEL 环境变量可切换激活模型（不改 JSON）：裸模型名匹配任意 provider
的池，"provider名/模型名" 只匹配对应 provider。
"""


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
                    name = item.get("name") or f"llm-{len(result)}"
                    active = _resolve_active_model(pool, name)
                    ordered = [active, *(m for m in pool if m != active)]
                    for i, m in enumerate(ordered):
                        result.append({
                            "name": name if i == 0 else f"{name}:{m}",
                            **base,
                            "model": m,
                        })
                if result:
                    return result
        except json.JSONDecodeError:
            pass  # 格式错则回退旧版

    # 旧版兼容路径：LLM_BASE_URL + LLM_MODEL + LLM_FALLBACKS 拼接
    primary = {
        "name": "primary",
        "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        "api_key": os.getenv("LLM_API_KEY", "sk-dummy-key"),
        "model": os.getenv("LLM_MODEL", "deepseek-v4-flash"),
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
        # LLM context window 上限（粗略字节估算，按 1 token ≈ 4 chars）
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
