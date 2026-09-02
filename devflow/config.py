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
- model:       str    模型名，如 deepseek-chat / gpt-4o-mini / custom-model
- temperature: float  采样温度，默认 0.1
"""


def _parse_llm_providers_from_env() -> list[LlmProviderSpec]:
    """解析 LLM_PROVIDERS_JSON 环境变量，作为结构化 fallback 链。

    支持两种方式，优先 JSON：
      1) LLM_PROVIDERS_JSON: 完整列表，如
           [{"name":"primary","base_url":"https://api.deepseek.com/v1","model":"deepseek-chat"},...]
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
                    if "base_url" not in item or "model" not in item:
                        continue
                    result.append({
                        "name": item.get("name") or f"llm-{len(result)}",
                        "base_url": item["base_url"],
                        "api_key": item.get("api_key") or os.getenv("LLM_API_KEY", "sk-dummy-key"),
                        "model": item["model"],
                        "temperature": float(item.get("temperature", 0.1)),
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
        "model": os.getenv("LLM_MODEL", "deepseek-chat"),
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
        self.LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")
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

        # ── Checkpoint ────────────────────────────────────
        self.CHECKPOINT_SQLITE_PATH: Path = Path(
            os.getenv("CHECKPOINT_SQLITE_PATH", "./data/checkpoints.db")
        )

        # ── OpenCode (阶段一占位) ──────────────────────────
        self.OPENCODE_BASE_URL: str = os.getenv("OPENCODE_BASE_URL", "http://localhost:8080")
        self.OPENCODE_API_TOKEN: str = os.getenv("OPENCODE_API_TOKEN", "")

        # ── 记忆策略 ──────────────────────────────────────
        # 保留最近 N 轮对话
        self.HOT_MEMORY_LAST_N: int = int(os.getenv("HOT_MEMORY_LAST_N", "5"))


settings = Settings()

# 确保 SQLite 目录存在
settings.CHECKPOINT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
