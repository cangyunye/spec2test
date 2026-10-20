"""doc manifest 流式契约：分章 clarify 抽取 → 拆分 doc_refs → handoff 切片注入。

LLM 调用全部 monkeypatch（遵循 conftest 的 hermetic 约定），验证的是契约：
批次调用形状、确定性合并、对齐引用、一次性守卫、失败重试语义、切片注入与覆盖校验。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from devflow.config import settings
from devflow.doc_index import build_doc_index
from devflow.errors import DevFlowError, LLM_RATE_LIMIT
from devflow.nodes.clarify import (
    clarify_extract_async,
    merge_doc_fragments,
)
from devflow.schemas import empty_requirement

REQ_MD = """# 订单系统需求

## 1. 用户管理
用户可注册、登录；密码长度 8~20 位。

## 2. 订单管理
下单生成订单；金额必须大于 0。
"""

OPENAPI = {
    "openapi": "3.0.0", "info": {"title": "订单接口"},
    "paths": {
        "/api/login": {"post": {"summary": "登录", "responses": {"200": {"description": "ok"}}}},
    },
}


@pytest.fixture()
def manifest(tmp_path: Path) -> dict:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "需求文档.md").write_text(REQ_MD, encoding="utf-8")
    (d / "api.json").write_text(json.dumps(OPENAPI, ensure_ascii=False), encoding="utf-8")
    return build_doc_index(
        [d / "需求文档.md"], [d / "api.json"], [], root=tmp_path / "idx"
    )


def _state(manifest: dict | None, **extra) -> dict:
    s = {
        "messages": [HumanMessage(content="（已载入需求文档集，请开始抽取）")],
        "requirement": empty_requirement(),
        "clarify_mode": "normal",
        "session_title": "已命名",  # 预置会话名，跳过起名 LLM 调用
    }
    if manifest is not None:
        s["doc_manifest"] = manifest
    s.update(extra)
    return s


# ═══════════════════════════════════════════════════════════════════
# merge_doc_fragments：确定性合并
# ═══════════════════════════════════════════════════════════════════


class TestMergeDocFragments:
    def test_dedupe_and_merge(self):
        f1 = {
            "acceptance_criteria": ["密码长度 8~20 位"],
            "edge_cases": ["密码过短拒绝注册"],
            "target_modules": ["用户模块"],
            "project_context": "订单系统",
            "io_constraints": {"input": "账号密码", "output": ""},
        }
        f2 = {
            "acceptance_criteria": ["密码长度 8~20 位", "金额必须大于 0"],  # 第一条跨批重复
            "edge_cases": [],
            "target_modules": ["用户模块", "订单模块"],
            "project_context": "订单系统（第 2 章）",
            "io_constraints": {"input": "", "output": "订单 JSON"},
        }
        patch, _ = merge_doc_fragments([(f1, ["CH-002"]), (f2, ["CH-003"])])
        assert patch["acceptance_criteria"] == ["密码长度 8~20 位", "金额必须大于 0"]
        assert patch["edge_cases"] == ["密码过短拒绝注册"]
        assert patch["target_modules"] == ["用户模块", "订单模块"]
        assert patch["io_constraints"] == {"input": "账号密码", "output": "订单 JSON"}
        assert "订单系统" in patch["project_context"] and "第 2 章" in patch["project_context"]

    def test_alignment_prefers_chapter_refs_falls_back_to_batch(self):
        f1 = {
            "acceptance_criteria": ["密码长度 8~20 位", "金额必须大于 0"],
            "chapter_refs": [{"text": "密码长度 8~20 位", "chapter_ids": ["CH-002"]}],
        }
        _, alignment = merge_doc_fragments([(f1, ["CH-002", "CH-003"])])
        # 有 chapter_refs 的用映射；没有的退回所在批的全部章节
        assert alignment["acceptance_criteria"]["密码长度 8~20 位"] == ["CH-002"]
        assert alignment["acceptance_criteria"]["金额必须大于 0"] == ["CH-002", "CH-003"]

    def test_empty_fragments_produce_empty_patch(self):
        patch, alignment = merge_doc_fragments([])
        assert patch == {}
        assert alignment == {"acceptance_criteria": {}, "edge_cases": {}}


# ═══════════════════════════════════════════════════════════════════
# clarify_extract × doc_manifest：分章抽取契约
# ═══════════════════════════════════════════════════════════════════


class TestClarifyDocIndex:
    @pytest.mark.asyncio
    async def test_chapter_extraction_merges_and_records_alignment(
        self, monkeypatch, manifest
    ):
        """manifest 模式首轮：分批抽取 → 合并 requirement + 对齐引用落 manifest。"""
        calls: list[dict] = []

        async def fake_json(**kwargs):
            calls.append(kwargs)
            chs = [ln.lstrip("#").strip()[1:].split("]")[0].strip() for ln in
                   kwargs["user_prompt"].splitlines()
                   if ln.lstrip("#").strip().startswith("[CH-")]
            frag: dict = {"project_context": "订单系统"}
            ac: list[str] = []
            if "CH-002" in chs:
                ac.append("密码长度 8~20 位")
                frag.update({
                    "edge_cases": ["密码过短拒绝注册"],
                    "chapter_refs": [{"text": "密码长度 8~20 位", "chapter_ids": ["CH-002"]}],
                })
            if "CH-003" in chs:
                ac.append("金额必须大于 0")
            if ac:
                frag["acceptance_criteria"] = ac
            return frag

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        out = await clarify_extract_async(_state(manifest))  # type: ignore[arg-type]

        req = out["requirement"]
        assert req["acceptance_criteria"] == ["密码长度 8~20 位", "金额必须大于 0"]
        assert req["edge_cases"] == ["密码过短拒绝注册"]
        assert req["project_context"] == "订单系统"
        assert out["doc_index_extracted"] is True
        assert out["requirement_confirmed"] is False
        # 来源标 user（文档是用户提供的材料）；对齐引用回填 manifest
        assert out["requirement_sources"]["acceptance_criteria"] == "user"
        align = out["doc_manifest"]["alignment"]
        assert align["acceptance_criteria"]["密码长度 8~20 位"] == ["CH-002"]
        # 无 chapter_refs 的条目回退到所在批的全部章节（默认批量预算下单批装下 3 章）
        assert align["acceptance_criteria"]["金额必须大于 0"] == ["CH-001", "CH-002", "CH-003"]
        # 每次调用都是分章抽取形态：带章节切片块与文档抽取 schema
        assert all(c.get("response_type") == "doc_chapter_extract" for c in calls)
        assert all("[CH-" in c["user_prompt"] for c in calls)
        assert all("chapter_ids" in json.dumps(c.get("json_schema") or {}) for c in calls)

    @pytest.mark.asyncio
    async def test_extract_runs_only_once(self, monkeypatch, manifest):
        """doc_index_extracted 已置位 → 走常规单轮抽取，不再分章调用。"""
        extract_calls: list[dict] = []

        async def fake_json(**kwargs):
            extract_calls.append(kwargs)
            return {"project_context": "用户补充的背景"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        out = await clarify_extract_async(
            _state(manifest, doc_index_extracted=True)
        )  # type: ignore[arg-type]
        assert out["requirement"]["project_context"] == "用户补充的背景"
        assert "doc_chapter_extract" not in "".join(
            str(c.get("response_type")) for c in extract_calls
        )

    @pytest.mark.asyncio
    async def test_existing_requirement_not_overwritten(self, monkeypatch, manifest):
        """requirement 已有实质内容（用户先手输了需求）→ 不进分章抽取分支。"""
        calls: list[dict] = []

        async def fake_json(**kwargs):
            calls.append(kwargs)
            return {}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        req = empty_requirement()
        req["project_context"] = "用户已手输"
        out = await clarify_extract_async(_state(manifest, requirement=req))  # type: ignore[arg-type]
        assert "chapter_extract" not in "".join(
            str(c.get("response_type")) for c in calls
        )
        assert out["requirement"]["project_context"] == "用户已手输"

    @pytest.mark.asyncio
    async def test_llm_failure_is_retryable_and_flag_untouched(self, monkeypatch, manifest):
        """任一批失败 → 可重试错误（图级 retry 重跑），doc_index_extracted 不置位。"""
        async def fake_json(**kwargs):
            raise DevFlowError(LLM_RATE_LIMIT, "限流", retryable=True)

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        out = await clarify_extract_async(_state(manifest))  # type: ignore[arg-type]
        assert out["last_error_code"] == LLM_RATE_LIMIT
        assert out["last_error_retryable"] is True
        assert out["retry_count"]["clarify_extract"] == 1
        assert "doc_index_extracted" not in out

    @pytest.mark.asyncio
    async def test_mock_fragments_not_merged(self, monkeypatch, manifest):
        """mock 兜底的编造分片不并入（诚实原则）；守卫仍置位避免死循环。"""
        async def fake_json(**kwargs):
            # 模拟 mock 兜底：返回编造需求 + meta 标记
            return {"project_context": "mock 编造的需求"}, {"mock": True}

        # invoke_json 的 meta 是出参关键字——fake 需要按真实签名写 meta
        async def fake_json_real(system_prompt, user_prompt, json_schema=None,
                                 response_type="general", meta=None, **kw):
            meta["mock"] = True
            return {"project_context": "mock 编造的需求"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json_real)
        out = await clarify_extract_async(_state(manifest))  # type: ignore[arg-type]
        assert out["requirement"] == empty_requirement()
        assert out["doc_index_extracted"] is True
        assert out["clarify_round_no_progress"] is True

    @pytest.mark.asyncio
    async def test_manifest_without_requirement_chapters_passthrough(
        self, monkeypatch, manifest
    ):
        """只挂接口文档（无需求章节）→ 标记已抽取，走正常追问。"""
        api_only = {k: v for k, v in manifest.items()}
        api_only["docs"] = [d for d in manifest["docs"] if d["kind"] == "api"]
        api_only["chapters"] = [
            c for c in manifest["chapters"] if c["doc_id"] == "D2"
        ]

        async def fake_json(**kwargs):
            return {}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        out = await clarify_extract_async(_state(api_only))  # type: ignore[arg-type]
        assert out["doc_index_extracted"] is True
        assert out["requirement"] == empty_requirement()


# ═══════════════════════════════════════════════════════════════════
# feature 拆分 × manifest：doc_refs 产出
# ═══════════════════════════════════════════════════════════════════


class TestSplitWithManifest:
    @pytest.mark.asyncio
    async def test_split_prompt_carries_manifest_and_features_get_refs(
        self, monkeypatch, manifest
    ):
        captured: dict = {}

        async def fake_json(system_prompt, user_prompt, response_model=None,
                            response_type="general", meta=None, **kw):
            captured["user_prompt"] = user_prompt
            meta["mock"] = False
            return {
                "features": [{
                    "name": "登录", "description": "用户登录",
                    "acceptance_criteria": ["密码长度 8~20 位"],
                    "doc_refs": {
                        "chapter_ids": ["CH-002"],
                        "api_ids": ["API-001", "API-001"],  # 重复 id 应去重
                        "model_ids": [],
                    },
                }],
                "open_questions": [], "assumptions": [],
            }

        monkeypatch.setattr("devflow.feature_split.invoke_json", fake_json)
        from devflow.feature_split import llm_split_features

        out = await llm_split_features(empty_requirement(), None, manifest)
        assert out is not None
        features, _, _ = out
        refs = features[0]["doc_refs"]
        assert refs == {"chapter_ids": ["CH-002"], "api_ids": ["API-001"],
                        "model_ids": []}
        # 目录视图进了拆分上下文
        assert "API-001" in captured["user_prompt"]
        assert "CH-002" in captured["user_prompt"]

    def test_fallback_single_covers_all_requirement_chapters(self, manifest):
        from devflow.nodes.feature_split import _fallback_single

        req = empty_requirement()
        req["acceptance_criteria"] = ["密码长度 8~20 位"]
        features = _fallback_single(req, manifest)
        # 整单兜底覆盖全部需求章节（含文档标题章，内容不丢）
        assert features[0]["doc_refs"]["chapter_ids"] == ["CH-001", "CH-002", "CH-003"]

    def test_fallback_single_without_manifest_has_no_refs(self):
        from devflow.nodes.feature_split import _fallback_single

        req = empty_requirement()
        req["acceptance_criteria"] = ["某准则"]
        features = _fallback_single(req, None)
        assert "doc_refs" not in features[0]


# ═══════════════════════════════════════════════════════════════════
# handoff prompt：切片注入 + 覆盖校验
# ═══════════════════════════════════════════════════════════════════


def _feature(**kw) -> dict:
    from devflow.feature_split import make_feature

    return make_feature(
        "F1", "登录", "用户登录",
        acceptance_criteria=["密码长度 8~20 位"],
        doc_refs=kw.pop("doc_refs", None),
    )


class TestHandoffPrompt:
    def test_prompt_injects_slices_and_manifest_path(self, manifest):
        from devflow.feature_split import build_feature_design_prompt

        f = _feature(doc_refs={"chapter_ids": ["CH-002"], "api_ids": ["API-001"]})
        prompt = build_feature_design_prompt(f, doc_manifest=manifest)
        assert "【关联文档切片" in prompt
        assert "用户可注册、登录" in prompt          # 章节原文
        assert '"summary": "登录"' in prompt          # 接口契约逐字切片
        assert "manifest.json" in prompt              # 回查路径
        assert "「接口:<API-xxx>」" in prompt          # 引用标注约定
        assert "文档对齐引用：章节 CH-002；接口 API-001" in prompt

    def test_prompt_without_refs_no_slices(self, manifest):
        from devflow.feature_split import build_feature_design_prompt

        prompt = build_feature_design_prompt(_feature(doc_refs=None), doc_manifest=manifest)
        assert "【关联文档切片" not in prompt

    def test_prompt_slice_budget_respected(self, manifest):
        from devflow.feature_split import build_feature_design_prompt

        f = _feature(doc_refs={"chapter_ids": ["CH-002", "CH-003"]})
        prompt = build_feature_design_prompt(
            f, doc_manifest=manifest, doc_slice_max_chars=30, doc_slice_max_count=1
        )
        assert "已截断" in prompt
        # 只注入 1 条切片：CH-003 的内容不出现
        assert "下单生成订单" not in prompt

    def test_validate_report_checks_doc_ref_citation(self, manifest):
        from devflow.feature_split import validate_feature_report

        f = _feature(doc_refs={"api_ids": ["API-001"]})
        case_no_ref = {"title": "登录成功", "case_type": "正向", "steps": "输入",
                       "expected": "成功", "rationale": "验收:密码长度 8~20 位"}
        issues = validate_feature_report(f, {"test_cases": [case_no_ref]})
        assert any("接口/模型未在用例中标注引用" in i for i in issues)

        case_ref = {**case_no_ref, "rationale": "验收:密码长度；接口:API-001"}
        issues = validate_feature_report(f, {"test_cases": [case_ref]})
        assert not any("接口/模型" in i for i in issues)


class TestLlmTestGenDocSection:
    def test_feature_mode_injects_slices(self, manifest):
        from devflow.providers.llm_testgen import _build_user_prompt

        f = _feature(doc_refs={"api_ids": ["API-001"]})
        prompt = _build_user_prompt("", ["登录"], {}, requirement=empty_requirement(),
                                    feature=f, doc_manifest=manifest)
        assert "【关联文档切片" in prompt and '"summary": "登录"' in prompt

    def test_whole_mode_injects_manifest_view(self, manifest):
        from devflow.providers.llm_testgen import _build_user_prompt

        prompt = _build_user_prompt("", ["端到端"], {}, requirement=empty_requirement(),
                                    feature=None, doc_manifest=manifest)
        assert "【文档目录清单" in prompt and "API-001" in prompt

    def test_no_manifest_no_section(self):
        from devflow.providers.llm_testgen import _build_user_prompt

        prompt = _build_user_prompt("", ["端到端"], {}, requirement=empty_requirement())
        assert "文档目录清单" not in prompt


class TestProviderContract:
    @pytest.mark.asyncio
    async def test_mock_testgen_accepts_doc_manifest(self, manifest):
        """全 provider 链签名贯通：mock 也接受 doc_manifest kwarg。"""
        from devflow.providers.mock import MockTestGen

        report = await MockTestGen().generate("", ["t"], doc_manifest=manifest)
        assert "test_cases" in report

    def test_opencode_feature_prompt_passes_manifest(self, manifest):
        from devflow.providers.opencode import OpenCodeTestProvider

        p = OpenCodeTestProvider()
        prompt = p._build_prompt(
            ["t"], logic_graph=None, requirement=None, feedback=None,
            checklists=None,
            feature=_feature(doc_refs={"api_ids": ["API-001"]}),
            skill_paths=None, doc_manifest=manifest,
        )
        assert '"summary": "登录"' in prompt


# ═══════════════════════════════════════════════════════════════════
# graph 级端到端（mock LLM）：manifest 会话一轮推进的守卫与追问
# ═══════════════════════════════════════════════════════════════════


class TestGraphRoundTrip:
    def test_manifest_session_first_round(self, manifest):
        """喂 notice 文案 → 分章抽取（mock 全批 → 不并入）→ 停在追问。

        验证三件事：doc_manifest/doc_index_extracted 键在 LangGraph 状态里存活
        （TypedDict 未声明会被静默丢弃）、抽取守卫置位、mock 兜底下需求为空走
        正常追问而不是拿编造数据往下跑。
        """
        from devflow.doc_index import doc_feed_notice
        from devflow.orchestrator import build_graph_with_providers, initial_state

        graph = build_graph_with_providers()
        tid = "docidx-e2e"
        config = {"configurable": {"thread_id": tid}}
        state = initial_state()
        state["doc_manifest"] = manifest
        next(graph.stream(state, config))

        msg = {"messages": [HumanMessage(content=doc_feed_notice(manifest))]}
        for _ in graph.stream(msg, config, stream_mode="updates"):
            pass

        vals = graph.get_state(config).values
        assert vals.get("doc_index_extracted") is True
        assert vals.get("requirement") == empty_requirement()
        assert vals.get("missing_fields")          # 走正常追问
        assert not vals.get("last_error_code")     # mock 不并入 ≠ 抽取失败
        # 章节正文不进对话（首条消息是短文案，不是全文）
        texts = [str(getattr(m, "content", "")) for m in vals.get("messages") or []]
        assert all("用户可注册、登录" not in t for t in texts)


# ═══════════════════════════════════════════════════════════════════
# Web 端点：/api/doc/index + CreateSession.doc_manifest
# ═══════════════════════════════════════════════════════════════════


class TestWebApi:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import web.server as ws
        from devflow.config import settings

        monkeypatch.setattr(settings, "DOC_INDEX_ROOT", tmp_path / "docidx")
        ws._buses.clear()
        ws._tid_locks.clear()
        with TestClient(ws.app) as c:
            yield c
        ws._buses.clear()
        ws._tid_locks.clear()

    def test_index_and_session_roundtrip(self, client, tmp_path):
        """上传双文档建目录 → manifest 建会话 → state 携带目录。"""
        files = [
            ("files", ("需求.md", "## 登录\n密码 8~20 位。\n".encode("utf-8"))),
            ("files", ("api.json", json.dumps(OPENAPI, ensure_ascii=False).encode("utf-8"))),
        ]
        r = client.post(
            "/api/doc/index",
            data={"kinds": ["requirement", "api"]},
            files=files,
        )
        assert r.status_code == 200
        m = r.json()
        assert len(m["chapters"]) >= 2
        assert any(a["path"] == "/api/login" for a in m["apis"])
        assert (tmp_path / "docidx").exists()  # 落在配置根下

        tid = f"w-{uuid.uuid4().hex[:8]}"
        r2 = client.post("/api/sessions", json={"thread_id": tid, "doc_manifest": m})
        assert r2.status_code == 200
        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        assert vals.get("doc_manifest", {}).get("root") == m["root"]

    def test_index_rejects_bad_kind(self, client):
        r = client.post(
            "/api/doc/index",
            data={"kinds": ["bogus"]},
            files=[("files", ("a.md", "x".encode("utf-8")))],
        )
        assert r.status_code == 400

    def test_index_rejects_kind_count_mismatch(self, client):
        r = client.post(
            "/api/doc/index",
            data={"kinds": ["requirement", "api"]},
            files=[("files", ("a.md", "x".encode("utf-8")))],
        )
        assert r.status_code == 400

    def test_index_auto_classifies_without_kinds(self, client):
        """kinds 缺省 → 内容嗅探自动分拣（JSON 接口 / 字段表模型 / 叙述需求）。"""
        files = [
            ("files", ("需求.md", "## 登录\n密码 8~20 位。\n".encode("utf-8"))),
            ("files", ("api.json", json.dumps(OPENAPI, ensure_ascii=False).encode("utf-8"))),
            ("files", ("models.md", "# 模型\n\n## Order\n字段：id, amount, status\n".encode("utf-8"))),
        ]
        r = client.post("/api/doc/index", files=files)  # 不传 kinds
        assert r.status_code == 200
        docs = sorted(r.json()["docs"], key=lambda d: d["name"])  # 文件名前缀 = 上传序
        assert [d["kind"] for d in docs] == ["requirement", "api", "model"]
