"""doc_index 确定性链路：多文档摄入 → manifest 目录 → 切片/视图/doc_refs。

全部纯函数、零 LLM：同输入同输出。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from devflow.doc_index import (
    batch_chapters,
    build_doc_index,
    classify_docs,
    concat_chapter_slices,
    doc_feed_notice,
    extract_doc_paths,
    find_entry,
    format_doc_refs,
    ingest_docs,
    load_manifest,
    normalize_api_doc,
    normalize_doc_refs,
    normalize_model_doc,
    render_manifest_view,
    requirement_chapters,
    resolve_doc_slices,
    should_use_index,
    slice_text,
    sniff_chapter_kind,
    sniff_doc_kind,
    split_into_chapters,
)

REQ_MD = """# 订单管理系统需求

## 1. 用户管理
用户可注册、登录。见《接口文档》的登录接口。

### 1.1 权限
管理员可审核商户。

## 2. 订单管理
下单走 POST /orders。
"""

OPENAPI = {
    "openapi": "3.0.0",
    "info": {"title": "订单接口", "description": "订单相关接口"},
    "paths": {
        "/api/login": {"post": {"summary": "登录", "responses": {"200": {"description": "ok"}}}},
        "/api/orders": {"post": {"summary": "创建订单", "requestBody": {"content": {"application/json": {}}}}},
    },
}

MODELS_MD = """# 数据模型

## Order 订单
字段：id, user_id, amount, status

## User 用户
字段：id, name
"""


@pytest.fixture()
def doc_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs"
    d.mkdir()
    (d / "需求文档.md").write_text(REQ_MD, encoding="utf-8")
    (d / "api.json").write_text(json.dumps(OPENAPI, ensure_ascii=False), encoding="utf-8")
    (d / "models.md").write_text(MODELS_MD, encoding="utf-8")
    return d


# ═══════════════════════════════════════════════════════════════════
# 章节切分
# ═══════════════════════════════════════════════════════════════════


class TestSplitChapters:
    def test_mixed_levels(self):
        chs = split_into_chapters(REQ_MD)
        titles = [c["title"] for c in chs]
        # # 与 ## 开章；### 归属当前章；无首章前散落内容
        assert titles == ["订单管理系统需求", "1. 用户管理", "2. 订单管理"]
        # 「### 1.1 权限」落在「1. 用户管理」章内
        ch2 = chs[1]
        body = REQ_MD.splitlines()[ch2["line_start"] - 1:ch2["line_end"]]
        assert any("1.1 权限" in ln for ln in body)
        assert all(c["chars"] > 0 for c in chs)

    def test_no_headings_single_chapter(self):
        text = "第一段\n第二段"
        chs = split_into_chapters(text)
        assert len(chs) == 1
        assert chs[0]["title"] == "（全文）"
        assert chs[0]["line_start"] == 1

    def test_all_deep_headings_degrade(self):
        text = "### 甲\n内容甲\n### 乙\n内容乙"
        chs = split_into_chapters(text)
        assert [c["title"] for c in chs] == ["甲", "乙"]

    def test_leading_content_becomes_preface(self):
        text = "开篇说明。\n\n# 标题\n正文"
        chs = split_into_chapters(text)
        assert chs[0]["title"] == "（开篇）"
        assert chs[1]["title"] == "标题"


# ═══════════════════════════════════════════════════════════════════
# OpenAPI / 模型 Schema 归一化
# ═══════════════════════════════════════════════════════════════════


class TestNormalize:
    def test_openapi_to_markdown(self, tmp_path: Path):
        p = tmp_path / "api.json"
        p.write_text(json.dumps(OPENAPI, ensure_ascii=False), encoding="utf-8")
        text = normalize_api_doc(p.read_text(encoding="utf-8"), p)
        assert "## POST /api/login" in text
        assert "## POST /api/orders" in text
        assert '"summary": "登录"' in text  # 操作对象逐字内联

    def test_non_openapi_json_passthrough(self, tmp_path: Path):
        p = tmp_path / "other.json"
        raw = json.dumps({"foo": ["bar"]})
        assert normalize_api_doc(raw, p) == raw

    def test_broken_json_passthrough(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        assert normalize_api_doc("{not json", p) == "{not json"

    def test_model_single_schema(self, tmp_path: Path):
        p = tmp_path / "schema.json"
        schema = {
            "title": "Order", "type": "object",
            "properties": {"id": {"type": "string"}, "amount": {"type": "number"}},
        }
        text = normalize_model_doc(json.dumps(schema), p)
        assert "## Order" in text and '"properties"' in text

    def test_model_multi_mapping(self, tmp_path: Path):
        p = tmp_path / "schemas.json"
        data = {
            "Order": {"type": "object", "properties": {"id": {}}},
            "User": {"type": "object", "properties": {"name": {}}},
        }
        text = normalize_model_doc(json.dumps(data), p)
        assert "## Order" in text and "## User" in text

    def test_markdown_passthrough(self, tmp_path: Path):
        p = tmp_path / "m.md"
        assert normalize_model_doc(MODELS_MD, p) == MODELS_MD


# ═══════════════════════════════════════════════════════════════════
# manifest 构建（落盘 + 回读 + 条目识别）
# ═══════════════════════════════════════════════════════════════════


class TestBuildDocIndex:
    def test_end_to_end(self, doc_dir: Path, tmp_path: Path):
        manifest = build_doc_index(
            [doc_dir / "需求文档.md"], [doc_dir / "api.json"], [doc_dir / "models.md"],
            root=tmp_path / "idx",
        )
        # 文档：kind 与缓存
        assert [(d["doc_id"], d["kind"]) for d in manifest["docs"]] == [
            ("D1", "requirement"), ("D2", "api"), ("D3", "model"),
        ]
        for d in manifest["docs"]:
            assert (tmp_path / "idx" / d["cache_path"]).is_file()
        assert (tmp_path / "idx" / "manifest.json").is_file()

        # 章节：需求 3 章；OpenAPI 转 Markdown 后每端点一章
        req_chs = [c for c in manifest["chapters"] if c["doc_id"] == "D1"]
        assert [c["title"] for c in req_chs] == ["订单管理系统需求", "1. 用户管理", "2. 订单管理"]
        api_chs = [c for c in manifest["chapters"] if c["doc_id"] == "D2"]
        assert any("POST /api/login" in c["title"] for c in api_chs)
        assert any("POST /api/orders" in c["title"] for c in api_chs)

        # 接口条目归属端点章节；模型条目只从 ≥2 级章节出（# 文档标题不算模型）
        assert [(a["method"], a["path"]) for a in manifest["apis"]] == [
            ("POST", "/api/login"), ("POST", "/api/orders"),
        ]
        assert all(a["chapter_id"] for a in manifest["apis"])
        assert [m["name"] for m in manifest["models"]] == ["Order", "User"]

        # 回读一致
        assert load_manifest(tmp_path / "idx")["root"] == manifest["root"]

    def test_api_doc_without_endpoint_headings_falls_back_to_chapters(
        self, doc_dir: Path, tmp_path: Path
    ):
        p = doc_dir / "api-prose.md"
        p.write_text("## 登录接口\n用户提交账号密码换取 token。\n", encoding="utf-8")
        manifest = build_doc_index([], [p], [], root=tmp_path / "idx2")
        assert manifest["apis"] == []  # 识别不出端点 → 无接口条目
        # 章节保留，对齐时可按 chapter_id 引用
        assert any(c["title"] == "登录接口" for c in manifest["chapters"])

    def test_unsupported_format_raises(self, tmp_path: Path):
        p = tmp_path / "x.doc"
        p.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError):
            build_doc_index([p], [], [], root=tmp_path / "idx3")


# ═══════════════════════════════════════════════════════════════════
# 切片与目录视图
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture()
def manifest(doc_dir: Path, tmp_path: Path) -> dict:
    return build_doc_index(
        [doc_dir / "需求文档.md"], [doc_dir / "api.json"], [doc_dir / "models.md"],
        root=tmp_path / "idx",
    )


class TestSlices:
    def test_chapter_slice(self, manifest):
        ch = find_entry(manifest, "CH-002")
        text = slice_text(manifest, ch, max_chars=10_000)
        assert "用户可注册、登录" in text
        assert "1.1 权限" in text  # ### 归属本章
        assert "下单走" not in text  # 下一章内容不串

    def test_slice_truncation_marked(self, manifest):
        ch = find_entry(manifest, "CH-002")
        text = slice_text(manifest, ch, max_chars=10)
        assert "已截断" in text

    def test_api_resolves_to_chapter_slice(self, manifest):
        api = find_entry(manifest, "API-001")
        text = slice_text(manifest, api, max_chars=10_000)
        assert '"summary": "登录"' in text

    def test_resolve_doc_slices_dedup_and_order(self, manifest):
        refs = {
            "chapter_ids": ["CH-001"],
            "api_ids": ["API-001"],       # 与 CH-001 不同章 → 出现
            "model_ids": ["MDL-001"],     # Order 模型章
        }
        slices = resolve_doc_slices(manifest, refs, max_chars_per=2400, max_slices=8)
        refs_out = [s["ref"] for s in slices]
        assert len(refs_out) == len(set(refs_out))
        assert refs_out[0] == "CH-001"
        api_slice = next(s for s in slices if '"summary": "登录"' in s["text"])
        assert api_slice["kind"] == "api"

    def test_resolve_caps_count(self, manifest):
        all_ch = {"chapter_ids": [c["chapter_id"] for c in manifest["chapters"]]}
        slices = resolve_doc_slices(manifest, all_ch, max_chars_per=100, max_slices=3)
        assert len(slices) == 3
        assert all(len(s["text"]) <= 100 + 30 for s in slices)  # 截断标注的余量

    def test_resolve_empty_refs(self, manifest):
        assert resolve_doc_slices(manifest, None, max_chars_per=100, max_slices=3) == []
        assert resolve_doc_slices(manifest, {}, max_chars_per=100, max_slices=3) == []
        assert resolve_doc_slices(None, {"chapter_ids": ["CH-001"]},
                                   max_chars_per=100, max_slices=3) == []

    def test_render_view_contains_all_sections(self, manifest):
        view = render_manifest_view(manifest)
        assert "【需求文档章节目录】" in view
        assert "【接口目录】" in view and "API-001 POST /api/login" in view
        assert "【模型目录】" in view and "MDL-001 Order" in view
        assert "CH-001" in view

    def test_render_view_orphan_chapters(self, doc_dir: Path, tmp_path: Path):
        p = doc_dir / "api-prose.md"
        p.write_text("## 登录接口\n说明文字。\n", encoding="utf-8")
        m = build_doc_index([], [p], [], root=tmp_path / "idx4")
        view = render_manifest_view(m)
        assert "可按 chapter_id 引用" in view and "登录接口" in view

    def test_render_view_size_cap(self, doc_dir: Path, tmp_path: Path):
        m = build_doc_index([doc_dir / "需求文档.md"], [], [], root=tmp_path / "idx5")
        assert len(render_manifest_view(m, max_chars=50)) <= 90


# ═══════════════════════════════════════════════════════════════════
# 分批 / 需求章节 / 脑补拼接
# ═══════════════════════════════════════════════════════════════════


class TestBatching:
    def test_batch_by_chars(self, manifest):
        chs = requirement_chapters(manifest)
        assert [c["doc_id"] for c in chs] == ["D1"] * 3
        batches = batch_chapters(chs, max_chars=10)  # 每章都超限 → 单章成批
        assert len(batches) == 3

    def test_batch_keeps_order_and_groups(self, manifest):
        chs = requirement_chapters(manifest)
        batches = batch_chapters(chs, max_chars=10_000)
        assert len(batches) == 1 and len(batches[0]) == 3

    def test_concat_chapter_slices(self, manifest):
        text = concat_chapter_slices(manifest, per_chapter_chars=2000, total_chars=5000)
        assert "用户可注册、登录" in text and "[CH-002]" in text


# ═══════════════════════════════════════════════════════════════════
# doc_refs 规整 / 模式决策 / 首条消息文案
# ═══════════════════════════════════════════════════════════════════


class TestDocRefsAndDecision:
    def test_normalize_doc_refs(self):
        refs = normalize_doc_refs({
            "chapter_ids": ["CH-002", "CH-002", "", None],
            "api_ids": ["API-001"],
            "model_ids": "不是列表",
            "bogus": ["x"],
        })
        assert refs == {"chapter_ids": ["CH-002"], "api_ids": ["API-001"],
                        "model_ids": []}

    def test_format_doc_refs(self):
        assert format_doc_refs({"chapter_ids": ["CH-001", "CH-002"], "api_ids": ["API-001"]}) == \
            "章节 CH-001、CH-002；接口 API-001"
        assert format_doc_refs(None) == ""

    def test_should_use_index(self):
        assert not should_use_index(100, 1, 0, 0, threshold=8000)
        assert should_use_index(9000, 1, 0, 0, threshold=8000)   # 正文超阈值
        assert should_use_index(100, 2, 0, 0, threshold=8000)    # 多份需求文档
        assert should_use_index(100, 1, 1, 0, threshold=8000)    # 挂接口文档

    def test_ingest_docs_small_single_doc_is_plain(self, doc_dir: Path):
        small = doc_dir / "small.md"
        small.write_text("小需求", encoding="utf-8")
        manifest, texts = ingest_docs([str(small)], [], [], root="unused", threshold=8000)
        assert manifest is None and texts == ["小需求"]

    def test_ingest_docs_big_builds_manifest(self, doc_dir: Path, tmp_path: Path):
        big = doc_dir / "big.md"
        big.write_text("# 大文档\n" + "字" * 9000, encoding="utf-8")
        manifest, _ = ingest_docs([str(big)], [], [], root=tmp_path / "idx6", threshold=8000)
        assert manifest is not None and len(manifest["chapters"]) >= 1

    def test_doc_feed_notice(self, manifest):
        notice = doc_feed_notice(manifest)
        assert "3 个需求章节" in notice and "2 个接口" in notice and "2 个模型" in notice
        assert "manifest.json" in notice
        assert doc_feed_notice(None) == ""


# ═══════════════════════════════════════════════════════════════════
# 类型自动识别（文档级嗅探 + 章节级改判 + 混合提交自动分拣）
# ═══════════════════════════════════════════════════════════════════


MIXED_MD = """# 内容审核平台需求

## 1. 文本审核
用户提交文本，系统判定是否违规；违规文本给出违规类别。文本长度上限 5000 字。

## 附录A 审核接口
### POST /api/text/moderate 文本审核
请求体：{"text": "string"}

### GET /api/records 审核记录
返回记录列表。

## 附录B 数据模型
### ModerationRecord
字段：id, content_type, category
字段：created_at, expires_at
"""


class TestSniffDocKind:
    def test_openapi_json_is_api(self, tmp_path: Path):
        p = tmp_path / "a.json"
        p.write_text(json.dumps(OPENAPI), encoding="utf-8")
        assert sniff_doc_kind(p.read_text(encoding="utf-8"), p) == "api"

    def test_model_schema_json_is_model(self, tmp_path: Path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"Order": {"type": "object", "properties": {"id": {}}}}),
                     encoding="utf-8")
        assert sniff_doc_kind(p.read_text(encoding="utf-8"), p) == "model"

    def test_unknown_json_falls_back_requirement(self, tmp_path: Path):
        p = tmp_path / "x.json"
        p.write_text(json.dumps({"notes": ["随便什么"]}), encoding="utf-8")
        assert sniff_doc_kind(p.read_text(encoding="utf-8"), p) == "requirement"

    def test_endpoint_dense_markdown_is_api(self, tmp_path: Path):
        p = tmp_path / "api.md"
        text = "# 审核接口\n\n## POST /api/moderate 审核\n说明\n\n## GET /api/records 记录\n说明\n"
        assert sniff_doc_kind(text, p) == "api"

    def test_field_list_markdown_is_model(self, tmp_path: Path):
        p = tmp_path / "m.md"
        text = "# 数据模型\n\n## Order\n字段：id, amount\n字段：created_at\n\n## User\n字段：id, name\n"
        assert sniff_doc_kind(text, p) == "model"

    def test_prose_markdown_is_requirement(self, tmp_path: Path):
        # 叙述式接口文档与需求无法可靠区分 → 回退需求（多喂正文好过丢准则）
        assert sniff_doc_kind(REQ_MD, tmp_path / "r.md") == "requirement"

    def test_single_endpoint_mention_stays_requirement(self, tmp_path: Path):
        # 需求里顺带提了一句端点：1 个端点标题 / 少量章节 → 不改判
        text = "# 需求\n\n## 下单\n下单走 POST /orders。\n\n## 退款\n退款原路退回。\n"
        assert sniff_doc_kind(text, tmp_path / "r.md") == "requirement"


class TestSniffChapterKind:
    def test_title_endpoint_is_api(self):
        assert sniff_chapter_kind("附录A POST /api/moderate 审核接口", "请求体说明") == "api"

    def test_body_endpoint_list_is_api(self):
        body = "- POST /api/a 创建\n- GET /api/b 查询"
        assert sniff_chapter_kind("接口清单", body) == "api"

    def test_model_chapter_with_fields_and_marker(self):
        assert sniff_chapter_kind("附录B 数据模型", "字段：id, amount\n字段：created_at") == "model"

    def test_plain_requirement_chapter_is_none(self):
        assert sniff_chapter_kind("1. 文本审核", "用户提交文本，系统判定是否违规。") is None

    def test_fields_without_marker_stay_requirement(self):
        # 章节里出现一两行字段说明但标题无模型标记 → 保持需求（保守）
        assert sniff_chapter_kind("1. 表单校验", "字段：用户名必填。") is None


class TestAutoClassify:
    def test_classify_docs_routes_by_content(self, tmp_path: Path):
        req = tmp_path / "req.md"; req.write_text(REQ_MD, encoding="utf-8")
        api = tmp_path / "api.json"; api.write_text(json.dumps(OPENAPI), encoding="utf-8")
        mdl = tmp_path / "m.md"
        mdl.write_text("# 数据模型\n\n## Order\n字段：id, amount\n字段：created_at\n",
                       encoding="utf-8")
        reqs, apis, models = classify_docs([req, api, mdl])
        assert [p.name for p in reqs] == ["req.md"]
        assert [p.name for p in apis] == ["api.json"]
        assert [p.name for p in models] == ["m.md"]

    def test_ingest_docs_mixed_submission_no_declaration(self, tmp_path: Path):
        """混合提交不声明类型：各文档按嗅探结果进对应桶。"""
        req = tmp_path / "req.md"; req.write_text(REQ_MD, encoding="utf-8")
        api = tmp_path / "api.json"; api.write_text(json.dumps(OPENAPI), encoding="utf-8")
        manifest, _ = ingest_docs([str(req), str(api)], [], [],
                                  root=tmp_path / "idx", threshold=8000)
        assert manifest is not None  # 有接口文档 → 直接 manifest 模式
        kinds = {d["name"]: d["kind"] for d in manifest["docs"]}
        assert kinds == {"req.md": "requirement", "api.json": "api"}

    def test_single_small_mixed_doc_gets_manifest(self, tmp_path: Path):
        """单份小混合文档（需求正文 + 接口/模型附录）也进 manifest 模式。"""
        mixed = tmp_path / "mixed.md"
        mixed.write_text(MIXED_MD, encoding="utf-8")
        manifest, _ = ingest_docs([str(mixed)], [], [],
                                  root=tmp_path / "idx", threshold=8000)
        assert manifest is not None
        m = manifest
        # 附录A 两个端点章 → API 条目；附录B 模型章 → MDL 条目
        assert [(a["method"], a["path"]) for a in m["apis"]] == [
            ("POST", "/api/text/moderate"), ("GET", "/api/records"),
        ]
        assert len(m["models"]) == 1
        # 改判章被排除出需求抽取（附录不污染验收标准）
        req_titles = [c["title"] for c in requirement_chapters(m)]
        assert req_titles == ["内容审核平台需求", "1. 文本审核"]
        assert all("附录" not in t for t in req_titles)
        # 改判章保留 chapter_id，可切片引用
        api_ch = find_entry(m, m["apis"][0]["chapter_id"])
        assert "moderate" in slice_text(m, api_ch, max_chars=2000)

    def test_mixed_doc_view_groups_entries(self, tmp_path: Path):
        mixed = tmp_path / "mixed.md"
        mixed.write_text(MIXED_MD, encoding="utf-8")
        m, _ = ingest_docs([str(mixed)], [], [],
                           root=tmp_path / "idx", threshold=8000)
        view = render_manifest_view(m)
        assert "【需求文档章节目录】" in view
        assert "附录A 审核接口" not in view       # 改判章不再列在需求目录
        assert "API-001 POST /api/text/moderate" in view
        assert "MDL-001" in view

    def test_explicit_kind_overrides_sniff(self, tmp_path: Path):
        """--api-doc 显式指定的文档不再重判（人工纠偏优先）。"""
        req = tmp_path / "plain.md"; req.write_text(REQ_MD, encoding="utf-8")
        manifest, _ = ingest_docs([], [str(req)], [],
                                  root=tmp_path / "idx", threshold=8000)
        assert manifest["docs"][0]["kind"] == "api"


class TestExtractDocPaths:
    def test_absolute_and_relative_paths(self):
        text = "/a/b/需求.md ./docs/api.json ~/m/models.md"
        assert extract_doc_paths(text) == ["/a/b/需求.md", "./docs/api.json", "~/m/models.md"]

    def test_cjk_punctuation_trailing_is_stripped(self):
        assert extract_doc_paths("看下 /x/y.md。还有 /z/w.docx，对吧？") == ["/x/y.md", "/z/w.docx"]

    def test_duplicate_paths_deduped_in_order(self):
        assert extract_doc_paths("/a.md /b.md /a.md") == ["/a.md", "/b.md"]

    def test_bare_filename_ignored(self):
        # 裸文件名没有路径分隔符，相对谁的歧义太大 → 不认
        assert extract_doc_paths("需求.md 直接给") == []

    def test_non_whitelisted_extension_ignored(self):
        assert extract_doc_paths("/a/b.doc /c/d.py") == []

    def test_empty_and_none(self):
        assert extract_doc_paths("") == []
        assert extract_doc_paths("没有路径的一段话。") == []
