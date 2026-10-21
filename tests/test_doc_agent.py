"""doc_agent：opencode/pi 读取语料 → 关键要素 JSON 契约。

真实 CLI 不启动（hermetic）：provider 的 _run_* 全部 monkeypatch。
"""
from __future__ import annotations

import json

import pytest

from devflow.config import settings
from devflow.doc_agent import (
    agent_feed_notice,
    analyze_docs,
    build_analyze_prompt,
    catalog_from_agent,
    error_note,
    patch_from_agent,
    resolve_analyze_provider,
)

PAYLOAD = {
    "req_type": "new_feature",
    "project_context": "V网通话使用信息查询接口改造",
    "target_modules": ["queryServNumberVnetVoiceUsage"],
    "io_constraints": {
        "input": "请求报文：servNumber 必填，月份 yyyyMM",
        "output": "响应：usageList 数组，含 totalDuration/totalFee",
    },
    "edge_cases": ["号码不存在返回空列表", "跨月查询"],
    "acceptance_criteria": ["查询结果与计费话单一致"],
    "apis": [{"method": "post", "path": "/queryServNumberVnetVoiceUsage",
              "summary": "号码V网通话使用信息查询"}],
    "models": [{"name": "VnetUsage", "fields": ["servNumber", "totalDuration"]}],
    "files": [{"path": "/docs/接口.md", "summary": "接口协议"},
              {"path": "/docs/V网数据配置.md", "summary": "配置说明"}],
    "open_questions": [{"question": "是否需要分页？", "recommended": "否"}],
    "assumptions": ["按单号码查询"],
}


class TestResolveProvider:
    def test_explicit_modes(self, monkeypatch):
        for mode in ("local", "opencode", "pi"):
            monkeypatch.setattr(settings, "DOC_ANALYZE_PROVIDER", mode)
            assert resolve_analyze_provider() == mode

    def test_auto_prefers_opencode_then_pi_then_local(self, monkeypatch):
        monkeypatch.setattr(settings, "DOC_ANALYZE_PROVIDER", "auto")
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/opencode" if "opencode" in b else None)
        assert resolve_analyze_provider() == "opencode"
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/pi" if b == settings.PI_BIN else None)
        assert resolve_analyze_provider() == "pi"
        monkeypatch.setattr("shutil.which", lambda b: None)
        monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
        assert resolve_analyze_provider() == "local"


class TestPromptAndParse:
    def test_prompt_lists_paths_and_schema(self):
        prompt = build_analyze_prompt(["/data/docs", "/data/api.json"])
        assert "/data/docs" in prompt and "/data/api.json" in prompt
        assert "遍历读取" in prompt
        assert '"io_constraints"' in prompt and '"apis"' in prompt and '"models"' in prompt
        assert "不要代码围栏" in prompt

    def test_patch_filters_and_normalizes(self):
        patch = patch_from_agent({
            **PAYLOAD,
            "bogus": "x",
            "edge_cases": ["  ", "跨月查询"],
        })
        assert patch["req_type"] == "new_feature"
        assert patch["edge_cases"] == ["跨月查询"]
        assert patch["io_constraints"]["input"].startswith("请求报文")
        assert "bogus" not in patch

    def test_patch_drops_invalid_req_type(self):
        assert "req_type" not in patch_from_agent({"req_type": "not_a_type"})

    def test_patch_keeps_known_empty_fields_absent(self):
        patch = patch_from_agent({"project_context": "", "acceptance_criteria": []})
        assert patch == {}

    def test_catalog_shape(self):
        cat = catalog_from_agent(PAYLOAD, ["/docs/api.md"], "opencode")
        assert cat["mode"] == "agent" and cat["provider"] == "opencode"
        assert cat["source_paths"] == ["/docs/api.md"]
        assert [d["name"] for d in cat["docs"]] == ["接口.md", "V网数据配置.md"]
        assert [(a["api_id"], a["method"], a["path"]) for a in cat["apis"]] == [
            ("API-001", "POST", "/queryServNumberVnetVoiceUsage")
        ]
        assert cat["models"][0]["model_id"] == "MDL-001"
        # 无行号坐标 → 章节为空（切片不可用，靠 source_paths 回查）
        assert cat["chapters"] == []

    def test_catalog_without_files_falls_back_to_corpus_doc(self):
        cat = catalog_from_agent({"apis": []}, ["/docs/a.md", "/docs/b.md"], "pi")
        assert len(cat["docs"]) == 1
        assert ";".join(["/docs/a.md", "/docs/b.md"]) in cat["docs"][0]["source_path"]

    def test_feed_notice_and_error_note(self):
        cat = catalog_from_agent(PAYLOAD, ["/docs/api.md"], "opencode")
        notice = agent_feed_notice(cat, "opencode")
        assert "opencode" in notice and "2 份文档" in notice and "1 个接口" in notice
        assert "已回退本地解析" in error_note(RuntimeError("boom"))


class TestAnalyzeDispatch:
    @pytest.mark.asyncio
    async def test_opencode_returns_parsed_payload(self, monkeypatch):
        from devflow.providers.opencode import OpenCodeTestProvider

        captured = {}

        async def fake_run(self, project_root, prompt, **kw):
            captured["root"] = project_root
            captured["prompt"] = prompt
            # 真实 `--format json` 是 JSONL 事件流：assistant 文本挂在 part/text
            return "\n".join([
                json.dumps({"type": "session", "sessionID": "s-1"}, ensure_ascii=False),
                json.dumps({
                    "part": {"type": "text", "text": json.dumps(PAYLOAD, ensure_ascii=False)}
                }, ensure_ascii=False),
            ])

        monkeypatch.setattr(OpenCodeTestProvider, "_run_opencode", fake_run)
        payload = await analyze_docs(["/docs/api.md"], provider="opencode")
        assert payload["project_context"] == PAYLOAD["project_context"]
        assert "遍历读取" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_pi_parses_fenced_json(self, monkeypatch):
        from devflow.providers.pi import PiProviderBase

        async def fake_run(self, project_root, prompt, *, timeout_sec=None):
            return "分析完成：\n```json\n" + json.dumps(PAYLOAD, ensure_ascii=False) + "\n```"

        monkeypatch.setattr(PiProviderBase, "_run_pi", fake_run)
        payload = await analyze_docs(["/docs/api.md"], provider="pi")
        assert payload["io_constraints"]["input"]

    @pytest.mark.asyncio
    async def test_invalid_output_raises(self, monkeypatch):
        from devflow.providers.pi import PiProviderBase

        async def fake_run(self, project_root, prompt, *, timeout_sec=None):
            return "这不是 JSON"

        monkeypatch.setattr(PiProviderBase, "_run_pi", fake_run)
        with pytest.raises(ValueError):
            await analyze_docs(["/docs/api.md"], provider="pi")

    @pytest.mark.asyncio
    async def test_local_mode_rejected(self):
        with pytest.raises(ValueError):
            await analyze_docs(["/docs/api.md"], provider="local")


class TestCorpusBudget:
    def test_estimate_text_and_binary(self, tmp_path):
        from devflow.doc_agent import estimate_corpus_chars

        a = tmp_path / "a.md"
        a.write_text("字" * 1000, encoding="utf-8")
        b = tmp_path / "b.docx"
        b.write_bytes(b"x" * 3000)  # 二进制按字节/3 折算 ≈ 1000
        assert estimate_corpus_chars([str(a), str(b), str(tmp_path / "missing.md")]) == 2000

    def test_exceeds_budget(self, tmp_path):
        from devflow.doc_agent import exceeds_agent_budget

        a = tmp_path / "a.md"
        a.write_text("x" * 500, encoding="utf-8")
        over, est = exceeds_agent_budget([str(a)], 100)
        assert over is True and est == 500
        over, est = exceeds_agent_budget([str(a)], 1000)
        assert over is False and est == 500

    def test_feed_notice_includes_corpus_size(self):
        cat = catalog_from_agent(PAYLOAD, ["/docs/api.md"], "opencode")
        assert "语料约 12345 字符" in agent_feed_notice(cat, "opencode", corpus_chars=12345)
        assert "语料约" not in agent_feed_notice(cat, "opencode")
