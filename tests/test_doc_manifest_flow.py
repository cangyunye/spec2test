"""doc manifest 流式契约：分章 clarify 抽取 → 拆分 doc_refs → handoff 切片注入。

LLM 调用全部 monkeypatch（遵循 conftest 的 hermetic 约定），验证的是契约：
批次调用形状、确定性合并、对齐引用、一次性守卫、失败重试语义、切片注入与覆盖校验。
"""
from __future__ import annotations

import asyncio
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
        # 来源标 doc（LLM 对文档的提炼，非用户原话 → 需求确认门禁必须核对）；对齐引用回填 manifest
        assert out["requirement_sources"]["acceptance_criteria"] == "doc"
        align = out["doc_manifest"]["alignment"]
        assert align["acceptance_criteria"]["密码长度 8~20 位"] == ["CH-002"]
        # 无 chapter_refs 的条目回退到所在批的全部章节——需求批与接口上下文批
        # 分池，因此只含需求 3 章（接口章节不会混进验收标准的出处）
        assert align["acceptance_criteria"]["金额必须大于 0"] == [
            "CH-001", "CH-002", "CH-003",
        ]
        # 每次调用都是分章抽取形态：带章节切片块与文档抽取 schema
        assert all(c.get("response_type") == "doc_chapter_extract" for c in calls)
        assert all("[CH-" in c["user_prompt"] for c in calls)
        assert all("chapter_ids" in json.dumps(c.get("json_schema") or {}) for c in calls)

    @pytest.mark.asyncio
    async def test_extract_runs_only_once(self, monkeypatch, manifest):
        """doc_index_extracted 已置位 → 用户新消息走常规单轮抽取，不再分章调用。"""
        extract_calls: list[dict] = []

        async def fake_json(**kwargs):
            extract_calls.append(kwargs)
            return {"project_context": "用户补充的背景"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        out = await clarify_extract_async(
            _state(manifest, doc_index_extracted=True,
                   messages=[HumanMessage(content="补充一下：用户背景是订单系统")])
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
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


class TestWebApi:

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

    # ── 聊天路径识别（POST /messages 首条消息贴路径 → 自动建目录挂会话）──

    def _wait_idle(self, client, tid: str, timeout: float = 30.0) -> None:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not client.get(f"/api/sessions/{tid}").json()["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("会话运行超过 30s 仍未空闲")

    def test_message_with_doc_paths_ingests_manifest(self, client, tmp_path):
        """消息里贴 3 个真实文档路径 → 自动建目录挂会话 + 消息替换为载入回执。"""
        d = tmp_path / "userdocs"
        d.mkdir()
        (d / "需求.md").write_text(
            "# 审核系统需求\n\n## 文本审核\n违规文本给出类别；长度上限 5000 字。\n",
            encoding="utf-8")
        (d / "api.json").write_text(json.dumps(OPENAPI, ensure_ascii=False), encoding="utf-8")
        (d / "models.md").write_text(
            "# 模型\n\n## ModerationRecord\n字段：id, category, created_at\n",
            encoding="utf-8")
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200

        text = f"{d / '需求.md'} {d / 'api.json'} {d / 'models.md'}"
        r = client.post(f"/api/sessions/{tid}/messages", json={"text": text})
        assert r.status_code == 200
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        m = vals.get("doc_manifest")
        assert m and len(m["docs"]) == 3
        kinds = {doc["name"]: doc["kind"] for doc in m["docs"]}
        assert set(kinds.values()) == {"requirement", "api", "model"}
        # 首条消息被替换为载入回执（纯路径消息剥完为空 → 短文案占位）
        human = [str(getattr(msg, "content", msg)) for msg in vals.get("messages") or []]
        assert any("已载入需求文档集" in t for t in human)

    def test_message_with_bogus_paths_stays_plain_text(self, client):
        """路径都不存在 → 原样按普通文本澄清，不建目录。"""
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        r = client.post(
            f"/api/sessions/{tid}/messages",
            json={"text": "/no/such/需求.md /no/such/api.json 看看这些文档"},
        )
        assert r.status_code == 200
        self._wait_idle(client, tid)
        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        assert not vals.get("doc_manifest")

    def test_second_message_after_manifest_not_reingested(self, client, tmp_path):
        """已挂 manifest 的会话：后续消息里的路径不再触发识别（不误吞文本）。"""
        d = tmp_path / "docs2"
        d.mkdir()
        (d / "需求.md").write_text("# 需求\n\n## 登录\n密码 8~20 位。\n", encoding="utf-8")
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages",
                    json={"text": f"{d / '需求.md'} 请开始"})
        self._wait_idle(client, tid)
        m1 = client.get(f"/api/sessions/{tid}").json()["values"].get("doc_manifest")
        assert m1

        client.post(f"/api/sessions/{tid}/messages",
                    json={"text": f"补充一下 {d / '需求.md'} 的边界场景"})
        self._wait_idle(client, tid)
        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        assert vals.get("doc_manifest") == m1  # 没有重建目录
        # 消息原样进对话（路径没被剥）
        human = [str(getattr(msg, "content", msg)) for msg in vals.get("messages") or []]
        assert any("补充一下" in t and "需求.md" in t for t in human)


class TestStartRunPrepare:
    """start_run 的 prepare 回调：锁内执行、产出替换 input、busy 时不执行。"""

    @pytest.mark.asyncio
    async def test_prepare_runs_inside_lock_and_replaces_input(self):
        import web.server as ws

        tid = f"w-prepare-{uuid.uuid4().hex[:6]}"
        graph = ws.build_graph_with_providers()
        graph.update_state(ws._config(thread_id=tid), ws.initial_state())
        called = {}

        async def prepare():
            called["ran"] = True
            return {"messages": [HumanMessage(content="prepare 产出的消息")]}

        r = await ws.start_run(tid, None, prepare=prepare)
        assert r["accepted"] is True
        # 等 run 跑完（mock LLM 很快），轮询 running
        import time
        for _ in range(200):
            if not ws._session_running(tid):
                break
            await asyncio.sleep(0.05)
        assert called.get("ran") is True
        vals = graph.get_state(ws._config(thread_id=tid)).values
        texts = [str(getattr(m, "content", "")) for m in vals.get("messages") or []]
        assert any("prepare 产出的消息" in t for t in texts)

    @pytest.mark.asyncio
    async def test_busy_session_skips_prepare(self):
        import web.server as ws

        tid = f"w-busy-{uuid.uuid4().hex[:6]}"
        lock = ws._tid_lock(tid)
        await lock.acquire()  # 模拟已有 run 占锁
        called = {}

        async def prepare():
            called["ran"] = True
            return {"messages": []}

        try:
            r = await ws.start_run(tid, None, prepare=prepare)
            assert r["accepted"] is False
            assert called == {}  # 锁外绝不执行 prepare（不写 checkpoint）
        finally:
            lock.release()


class TestChatIngestFeedback:
    """P1-3/4/5：贴路径的分支反馈——混写合并、阻塞引导、部分失败清单。"""

    def _wait_idle(self, client, tid: str, timeout: float = 30.0) -> None:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not client.get(f"/api/sessions/{tid}").json()["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("会话运行超过 30s 仍未空闲")

    def test_paths_with_prose_merges_both(self, client, tmp_path, monkeypatch):
        """路径 + 正文混写：分章抽取与残留正文各抽各的、合并进同一份需求。"""
        from devflow.nodes import clarify as clarify_mod

        d = tmp_path / "mixdocs"
        d.mkdir()
        (d / "需求.md").write_text(
            "# 审核需求\n\n## 文本审核\n章节准则：文本长度上限 5000 字。\n",
            encoding="utf-8")

        async def dispatch_json(**kwargs):
            sp = str(kwargs.get("system_prompt") or "")
            meta = kwargs.get("meta") or {}
            meta["mock"] = False
            if "需求文档的章节原文" in sp:
                return {"acceptance_criteria": ["章节准则：文本长度上限 5000 字"]}
            if "严谨的需求分析师" in sp:
                return {"acceptance_criteria": ["残留正文验收：支持批量上传"]}
            return {}

        monkeypatch.setattr(clarify_mod, "invoke_json", dispatch_json)

        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        text = f"{d / '需求.md'} 系统要支持批量上传审核"
        assert client.post(f"/api/sessions/{tid}/messages",
                           json={"text": text}).status_code == 200
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        assert vals.get("doc_manifest")
        criteria = " ".join(vals["requirement"].get("acceptance_criteria") or [])
        assert "章节准则" in criteria and "残留正文验收" in criteria
        # 来源分账：章节条目 doc、正文条目 user
        src = vals.get("requirement_sources") or {}
        assert src.get("acceptance_criteria") in ("user", "doc")

    def test_blocked_when_requirement_has_content(self, client, tmp_path):
        """会话已有需求内容再贴路径 → 不摄入 + 引导新开会话 + 路径被剥。"""
        d = tmp_path / "late"
        d.mkdir()
        (d / "需求.md").write_text("# 需求\n\n## 登录\n密码 8~20 位。\n", encoding="utf-8")
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        # 直接写一个"已有实质内容"的 requirement（不走第一轮——mock 会填满需求
        # 并停在需求确认门禁，门禁期的 /messages 是 409，到不了识别分支）
        import web.server as ws

        req = {
            "project_context": "订单导出工具",
            "io_constraints": {"input": "订单号列表", "output": "Excel"},
            "acceptance_criteria": ["导出文件可打开"],
            "edge_cases": ["订单号不存在时提示"],
        }
        ws.build_graph_with_providers().update_state(
            ws._config(thread_id=tid), {"requirement": req}
        )
        client.post(f"/api/sessions/{tid}/messages",
                    json={"text": f"{d / '需求.md'} 用这个文档"})
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        assert not vals.get("doc_manifest")  # 未摄入
        human_ai = [str(getattr(m, "content", m)) for m in vals.get("messages") or []]
        assert any("新开会话" in t for t in human_ai)  # 引导提示
        assert all("需求.md" not in t or "新开会话" in t for t in human_ai if "用这个文档" in t)

    def test_partial_missing_paths_reports_skipped(self, client, tmp_path):
        """一真一假路径：真的摄入、假的进跳过清单提示。"""
        d = tmp_path / "part"
        d.mkdir()
        (d / "需求.md").write_text("# 需求\n\n## 登录\n密码 8~20 位。\n", encoding="utf-8")
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages",
                    json={"text": f"{d / '需求.md'} /no/such/api.json"})
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        m = vals.get("doc_manifest")
        assert m and len(m["docs"]) == 1
        texts = [str(getattr(msg, "content", msg)) for msg in vals.get("messages") or []]
        assert any("未找到已忽略" in t and "/no/such/api.json" in t for t in texts)

    def test_delete_session_removes_doc_dir(self, client, tmp_path):
        """删除会话回收文档目录（聊天识别建的 DOC_INDEX_ROOT/<tid>）。"""
        from devflow.config import settings

        d = tmp_path / "gc"
        d.mkdir()
        (d / "需求.md").write_text("# 需求\n\n## 登录\n密码 8~20 位。\n", encoding="utf-8")
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages",
                    json={"text": str(d / "需求.md")})
        self._wait_idle(client, tid)
        doc_dir = Path(settings.DOC_INDEX_ROOT) / tid
        assert doc_dir.is_dir()

        assert client.delete(f"/api/sessions/{tid}").status_code == 200
        assert not doc_dir.exists()


class TestApiContextExtraction:
    """接口/模型章节作为受限上下文参与抽取：io 约束从接口文档来，条目禁令写进提示词。"""

    @pytest.mark.asyncio
    async def test_io_constraints_extracted_from_api_chapters(self, monkeypatch, manifest):
        calls: list[dict] = []

        async def fake_json(**kwargs):
            calls.append(kwargs)
            meta = kwargs.get("meta") or {}
            meta["mock"] = False
            up = kwargs.get("user_prompt", "")
            frag: dict = {}
            # 默认预算下需求章与接口上下文可能在同一批：按标记合并产出
            if "（接口文档）" in up:
                frag["io_constraints"] = {
                    "input": "request.text 字符串，最长 5000",
                    "output": "审核结论 JSON",
                }
            if "[CH-002]" in up:
                frag["acceptance_criteria"] = ["密码长度 8~20 位"]
            return frag

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        out = await clarify_extract_async(_state(manifest))  # type: ignore[arg-type]

        req = out["requirement"]
        assert req["io_constraints"]["input"] == "request.text 字符串，最长 5000"
        assert req["io_constraints"]["output"] == "审核结论 JSON"
        assert req["acceptance_criteria"] == ["密码长度 8~20 位"]
        assert out["requirement_sources"]["io_constraints.input"] == "doc"
        # 上下文批次确实带上了接口原文（端点契约逐字进 prompt）
        assert any("（接口文档）" in c["user_prompt"] for c in calls)
        assert any('"summary": "登录"' in c["user_prompt"] for c in calls)

    def test_doc_extract_prompt_contract(self):
        from devflow.nodes.clarify import SYSTEM_PROMPT_DOC_EXTRACT

        # 接口/模型条目禁止进验收标准；叙述式规则要能提炼成条目（不再"没列表就 null"）
        assert "禁止摘进" in SYSTEM_PROMPT_DOC_EXTRACT
        assert "不要因为「文档没有列表」就返回 null" in SYSTEM_PROMPT_DOC_EXTRACT
        assert "输入约束 = 请求入参" in SYSTEM_PROMPT_DOC_EXTRACT


class TestDocModeQuestionHint:
    @pytest.mark.asyncio
    async def test_no_progress_question_explains_doc_extraction(self, monkeypatch, manifest):
        async def fake_text(**kwargs):
            return "请补充输入约束：io_constraints.input"

        monkeypatch.setattr("devflow.nodes.clarify.invoke_text", fake_text)
        from devflow.nodes.clarify import clarify_build_question_async

        state = {
            "messages": [],
            "requirement": empty_requirement(),
            "missing_fields": ["io_constraints.input: 必须填写输入约束"],
            "clarify_mode": "normal",
            "doc_manifest": manifest,
            "doc_index_extracted": True,
            "clarify_round_no_progress": True,
        }
        out = await clarify_build_question_async(state)  # type: ignore[arg-type]
        text = str(out["messages"][0].content)
        assert "文档分章抽取本轮没有产出可用字段" in text

    @pytest.mark.asyncio
    async def test_partial_progress_question_adds_doc_hint(self, monkeypatch, manifest):
        async def fake_text(**kwargs):
            return "请补充输入约束"

        monkeypatch.setattr("devflow.nodes.clarify.invoke_text", fake_text)
        from devflow.nodes.clarify import clarify_build_question_async

        state = {
            "messages": [],
            "requirement": empty_requirement(),
            "missing_fields": ["io_constraints.input: 必须填写输入约束"],
            "clarify_mode": "normal",
            "doc_manifest": manifest,
            "doc_index_extracted": True,
            "clarify_round_no_progress": False,
        }
        out = await clarify_build_question_async(state)  # type: ignore[arg-type]
        text = str(out["messages"][0].content)
        assert "文档目录模式" in text and "接口/模型章节" in text


class TestProvenanceMerge:
    """批次出处规则：契约 io 压过叙述 io；ctx-only 批次不贡献验收标准。"""

    def test_contract_io_beats_prose_io(self):
        from devflow.nodes.clarify import merge_doc_fragments

        req_frag = {"io_constraints": {"input": "用户提交的内容", "output": "结果"},
                    "acceptance_criteria": ["金额必须大于 0"]}
        ctx_frag = {"io_constraints": {"input": "request.text 字符串，maxLength=5000",
                                       "output": "{\"category\": string}"}}
        # ctx 在前后顺序都不影响：契约优先
        patch1, _ = merge_doc_fragments([(req_frag, ["CH-002"], "req"),
                                         (ctx_frag, ["CH-005"], "ctx")])
        patch2, _ = merge_doc_fragments([(ctx_frag, ["CH-005"], "ctx"),
                                         (req_frag, ["CH-002"], "req")])
        for p in (patch1, patch2):
            assert p["io_constraints"]["input"] == "request.text 字符串，maxLength=5000"
            assert p["io_constraints"]["output"] == '{"category": string}'

    def test_prose_io_used_when_contract_absent(self):
        from devflow.nodes.clarify import merge_doc_fragments

        patch, _ = merge_doc_fragments([
            ({"io_constraints": {"input": "上传的图片"}}, ["CH-002"], "req"),
            ({"io_constraints": {"input": None, "output": None}}, ["CH-005"], "ctx"),
        ])
        assert patch["io_constraints"]["input"] == "上传的图片"

    def test_ctx_only_criteria_dropped(self):
        from devflow.nodes.clarify import merge_doc_fragments

        patch, alignment = merge_doc_fragments([
            ({"acceptance_criteria": ["金额必须大于 0"]}, ["CH-002"], "req"),
            ({"acceptance_criteria": ["POST /api/orders 创建订单"],
              "edge_cases": ["请求体缺少 amount"]}, ["CH-005"], "ctx"),
        ])
        assert patch["acceptance_criteria"] == ["金额必须大于 0"]  # 接口条目被丢弃
        assert "edge_cases" not in patch
        assert all("API" not in k for k in alignment["edge_cases"])

    def test_mixed_batch_keeps_criteria(self):
        from devflow.nodes.clarify import merge_doc_fragments

        patch, _ = merge_doc_fragments([
            ({"acceptance_criteria": ["来自混合批的规则"]}, ["CH-002", "CH-005"], "mixed"),
        ])
        assert patch["acceptance_criteria"] == ["来自混合批的规则"]

    def test_two_tuple_backward_compatible(self):
        from devflow.nodes.clarify import merge_doc_fragments

        patch, _ = merge_doc_fragments([({"acceptance_criteria": ["老调用形态"]}, ["CH-001"])])
        assert patch["acceptance_criteria"] == ["老调用形态"]


class TestMultiBatchContractPrecedence:
    @pytest.mark.asyncio
    async def test_split_batches_contract_io_wins(self, monkeypatch, manifest):
        """强制每章独立成批：需求批次给模糊 io、接口批次给精确 io → 后者胜。"""
        from devflow.config import settings

        monkeypatch.setattr(settings, "DOC_EXTRACT_BATCH_CHARS", 10)
        calls: list[str] = []

        async def fake_json(**kwargs):
            meta = kwargs.get("meta") or {}
            meta["mock"] = False
            up = kwargs.get("user_prompt", "")
            calls.append(up)
            if "（接口文档）" in up:
                return {
                    "io_constraints": {"input": "request.text 字符串，maxLength=5000",
                                       "output": "审核结论 JSON"},
                    "acceptance_criteria": ["接口条目不该成为验收标准"],
                }
            if "[CH-002]" in up:
                return {"io_constraints": {"input": "用户提交的文本", "output": "结果"},
                        "acceptance_criteria": ["密码长度 8~20 位"]}
            return {}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        out = await clarify_extract_async(_state(manifest))  # type: ignore[arg-type]

        req = out["requirement"]
        assert req["io_constraints"]["input"] == "request.text 字符串，maxLength=5000"
        assert req["io_constraints"]["output"] == "审核结论 JSON"
        assert req["acceptance_criteria"] == ["密码长度 8~20 位"]  # 接口条目未混入
        assert len(calls) >= 3  # 每章独立批


class TestDirectoryWindowsPaths:
    def _wait_idle(self, client, tid: str, timeout: float = 30.0) -> None:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not client.get(f"/api/sessions/{tid}").json()["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("会话运行超过 30s 仍未空闲")

    def test_directory_path_expands_one_level(self, client, tmp_path):
        """贴目录路径 → 一层展开白名单文档（png 忽略）+ 展开回执。"""
        d = tmp_path / "docdir"
        d.mkdir()
        (d / "需求.md").write_text("# 需求\n\n## 登录\n密码 8~20 位。\n", encoding="utf-8")
        (d / "api.md").write_text(
            "## POST /api/login 登录\n请求体：账号密码\n", encoding="utf-8")
        (d / "截图.png").write_bytes(b"\x89PNG")
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages", json={"text": str(d)})
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        m = vals.get("doc_manifest")
        assert m and len(m["docs"]) == 2  # png 被白名单过滤
        texts = [str(getattr(msg, "content", msg)) for msg in vals.get("messages") or []]
        assert any("已展开目录" in t for t in texts)

    def test_windows_path_on_posix_gets_explicit_hint(self, client):
        """Windows 盘符路径出现在非 Windows 服务 → 明确提示而不是静默当文本。"""
        import os

        if os.name == "nt":
            pytest.skip("仅验证非 Windows 服务上的跨系统提示")
        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(
            f"/api/sessions/{tid}/messages",
            json={"text": r"需求文档位于 D:\test_cases\BR20261009012\需求 目录下"},
        )
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        assert not vals.get("doc_manifest")
        texts = [
            str(m.get("content") if isinstance(m, dict) else getattr(m, "content", m))
            for m in vals.get("messages") or []
        ]
        assert any("非 Windows 系统" in t and "D:\\test_cases" in t for t in texts)


class TestAgentAnalyzeMode:
    """DOC_ANALYZE_PROVIDER=opencode/pi：agent 读语料 → 预填需求；失败回退本地。"""

    def _wait_idle(self, client, tid: str, timeout: float = 30.0) -> None:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not client.get(f"/api/sessions/{tid}").json()["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("会话运行超过 30s 仍未空闲")

    def _payload(self) -> dict:
        return {
            "req_type": "new_feature",
            "project_context": "V网通话查询接口改造",
            "target_modules": ["queryServNumberVnetVoiceUsage"],
            "io_constraints": {"input": "servNumber 必填", "output": "usageList 数组"},
            "edge_cases": ["号码不存在返回空列表"],
            "acceptance_criteria": ["与计费话单一致"],
            "apis": [{"method": "post", "path": "/queryServNumberVnetVoiceUsage",
                      "summary": "查询"}],
            "models": [{"name": "VnetUsage", "fields": ["servNumber"]}],
            "files": [{"path": "/docs/接口.md", "summary": "接口协议"}],
        }

    def test_agent_fills_requirement_and_catalog(self, client, tmp_path, monkeypatch):
        from devflow.config import settings as _s

        d = tmp_path / "agentdocs"
        d.mkdir()
        (d / "接口.md").write_text("## POST /x\n入参 servNumber\n", encoding="utf-8")
        monkeypatch.setattr(_s, "DOC_ANALYZE_PROVIDER", "opencode")
        calls = {}

        async def fake_analyze(paths, provider=None):
            calls["paths"] = paths
            calls["provider"] = provider
            return self._payload()

        monkeypatch.setattr("devflow.doc_agent.analyze_docs", fake_analyze)

        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages", json={"text": str(d / "接口.md")})
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        req = vals.get("requirement") or {}
        assert req["io_constraints"]["input"] == "servNumber 必填"
        assert req["acceptance_criteria"] == ["与计费话单一致"]
        assert vals["requirement_sources"]["io_constraints.input"] == "doc"
        cat = vals.get("doc_manifest")
        assert cat and cat.get("mode") == "agent" and cat.get("provider") == "opencode"
        assert cat["source_paths"] == [str(d / "接口.md")]
        assert vals.get("doc_index_extracted") is True
        texts = [
            str(m.get("content") if isinstance(m, dict) else getattr(m, "content", m))
            for m in vals.get("messages") or []
        ]
        assert any("opencode" in t and "读取并分析" in t for t in texts)
        assert calls["provider"] == "opencode"

    def test_agent_failure_falls_back_to_local(self, client, tmp_path, monkeypatch):
        from devflow.config import settings as _s

        d = tmp_path / "agentfail"
        d.mkdir()
        (d / "需求.md").write_text("# 需求\n\n## 登录\n密码 8~20 位。\n", encoding="utf-8")
        monkeypatch.setattr(_s, "DOC_ANALYZE_PROVIDER", "pi")

        async def fake_analyze(paths, provider=None):
            raise RuntimeError("pi 未登录")

        monkeypatch.setattr("devflow.doc_agent.analyze_docs", fake_analyze)

        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages", json={"text": str(d / "需求.md")})
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        cat = vals.get("doc_manifest")
        assert cat and cat.get("mode") != "agent"  # 回退本地 manifest 形态
        assert cat.get("chapters")  # 本地切章成功
        texts = [
            str(m.get("content") if isinstance(m, dict) else getattr(m, "content", m))
            for m in vals.get("messages") or []
        ]
        assert any("已回退本地解析" in t for t in texts)

    def test_agent_prompt_reaches_handoff_with_source_paths(self, tmp_path):
        """agent 目录（无切片）→ handoff prompt 带原始语料路径供执行器回查。"""
        from devflow.doc_agent import catalog_from_agent
        from devflow.feature_split import build_feature_design_prompt, make_feature

        cat = catalog_from_agent(self._payload(), ["/docs/接口.md", "/docs/模型.md"], "opencode")
        f = make_feature("F1", "查询", doc_refs={"api_ids": ["API-001"]})
        prompt = build_feature_design_prompt(f, doc_manifest=cat)
        assert "原始文档语料路径" in prompt
        assert "/docs/接口.md" in prompt and "/docs/模型.md" in prompt


class TestFeedNoticeGuard:
    @pytest.mark.asyncio
    async def test_agent_feed_notice_not_extracted(self, monkeypatch, manifest):
        """agent 分析回执不是需求内容：已分析过时直接跳过抽取（防 mock/弱模型覆盖）。"""
        calls: list[dict] = []

        async def fake_json(**kwargs):
            calls.append(kwargs)
            return {"project_context": "不该出现的覆盖"}

        monkeypatch.setattr("devflow.nodes.clarify.invoke_json", fake_json)
        st = _state(manifest, doc_index_extracted=True, messages=[HumanMessage(
            content="（已由 opencode 读取并分析文档集：2 份文档 / 1 个接口 / 1 个模型，"
                    "需求要素已写入清单。全部文档正文不进对话上下文。）"
        )])
        out = await clarify_extract_async(st)  # type: ignore[arg-type]
        assert calls == []
        assert out["requirement"] == empty_requirement()


class TestAgentCorpusBudget:
    """agent 整语料模式的体积预算：超限回落 local 并注明。"""

    def _wait_idle(self, client, tid: str, timeout: float = 30.0) -> None:
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not client.get(f"/api/sessions/{tid}").json()["running"]:
                return
            time.sleep(0.05)
        raise AssertionError("会话运行超过 30s 仍未空闲")

    def test_over_budget_falls_back_to_local_with_note(self, client, tmp_path, monkeypatch):
        import web.server as ws
        from devflow.config import settings as _s

        d = tmp_path / "bigdocs"
        d.mkdir()
        big = d / "需求.md"
        big.write_text("# 需求\n\n## 登录\n" + "验收标准。" * 200, encoding="utf-8")
        monkeypatch.setattr(_s, "DOC_ANALYZE_PROVIDER", "opencode")
        monkeypatch.setattr(_s, "DOC_ANALYZE_MAX_CORPUS_CHARS", 500)
        called = {}

        async def fake_analyze(paths, provider=None):
            called["ran"] = True
            return {"files": []}

        monkeypatch.setattr("devflow.doc_agent.analyze_docs", fake_analyze)

        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages", json={"text": str(big)})
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        cat = vals.get("doc_manifest")
        assert cat and cat.get("mode") != "agent"  # 回落本地 manifest
        assert cat.get("chapters")
        assert called == {}  # agent 未被调用
        texts = [
            str(m.get("content") if isinstance(m, dict) else getattr(m, "content", m))
            for m in vals.get("messages") or []
        ]
        assert any("超出 agent 分析预算" in t and "分章抽取" in t for t in texts)

    def test_within_budget_notice_shows_corpus_size(self, client, tmp_path, monkeypatch):
        from devflow.config import settings as _s

        d = tmp_path / "smalldocs"
        d.mkdir()
        f = d / "需求.md"
        f.write_text("# 需求\n\n## 登录\n密码 8~20 位。\n", encoding="utf-8")
        monkeypatch.setattr(_s, "DOC_ANALYZE_PROVIDER", "opencode")

        async def fake_analyze(paths, provider=None):
            return {"files": [{"path": str(f), "summary": "s"}], "apis": [], "models": []}

        monkeypatch.setattr("devflow.doc_agent.analyze_docs", fake_analyze)

        tid = f"w-{uuid.uuid4().hex[:8]}"
        assert client.post("/api/sessions", json={"thread_id": tid}).status_code == 200
        client.post(f"/api/sessions/{tid}/messages", json={"text": str(f)})
        self._wait_idle(client, tid)

        vals = client.get(f"/api/sessions/{tid}").json()["values"]
        assert (vals.get("doc_manifest") or {}).get("mode") == "agent"
        texts = [
            str(m.get("content") if isinstance(m, dict) else getattr(m, "content", m))
            for m in vals.get("messages") or []
        ]
        assert any("语料约" in t and "opencode" in t for t in texts)
