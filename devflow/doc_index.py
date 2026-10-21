"""多文档摄入与目录层（doc manifest）：大需求文档集的结构化底座。

解决什么问题：需求文档非常大、拆成多个章节，另有接口专用文档、模型专用文档时，
把全文塞进上下文会撑爆澄清抽取与 checkpoint。本模块提供确定性的「开料」工段：

  1. 摄入：多文档（需求 / 接口 / 模型）→ 归一化缓存文本（.docx 提取、OpenAPI JSON
     转 Markdown）→ 按标题层级切章节 → 产出 doc manifest（稳定 ID + 行号范围 + 摘要）
  2. 检索：manifest 只进 state（小）；章节正文留磁盘，slice_text 按需读取切片
  3. 视图：render_manifest_view 渲染紧凑目录清单（喂给拆分/设计 LLM 做选择依据）
  4. 对齐：doc_refs（chapter_ids/api_ids/model_ids）→ resolve_doc_slices 还原
     「引用 + 原文切片」的 handoff 载荷

确定性优先：同输入同输出，不依赖模型；接口/模型是枚举型资产，靠目录精确挑选，
不靠模糊检索。LLM 只在下游（分章抽取 / 拆分 / 设计）的明确点位介入。

manifest 形状（JSON 可序列化，随 state 走 checkpoint）：

    {
      "version": 1,
      "root": "<manifest.json 与 cache/ 所在目录（绝对路径）>",
      "docs":   [{"doc_id", "kind": "requirement|api|model", "name",
                  "source_path", "cache_path", "chars"}],
      "chapters": [{"chapter_id": "CH-001", "doc_id", "title", "level",
                    "line_start", "line_end", "chars", "summary"}],
      "apis":   [{"api_id": "API-001", "doc_id", "chapter_id",
                  "method", "path", "title", "chars"}],
      "models": [{"model_id": "MDL-001", "doc_id", "chapter_id",
                  "name", "title", "chars"}],
      "alignment": {"acceptance_criteria": {"<原文>": ["CH-001"]}, ...}  # 抽取后回填
    }
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .doc_reader import read_doc

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

# 章节切分默认阈值：#/## 开新章，### 及更深归属当前章；文档里没有 ≤2 级标题时
# 退化用文档中出现的最浅标题层级（否则整份文档糊成一章，目录失去意义）
CHAPTER_HEAD_LEVEL = 2

# OpenAPI 操作对象里保留的关键字（其余丢弃，控制缓存体积）
_OPENAPI_OP_KEYS = (
    "summary", "description", "operationId", "tags", "parameters",
    "requestBody", "responses", "deprecated",
)

_HTTP_METHOD_RE = re.compile(
    r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/[\w./:{}\-\[\]]+)",
    re.IGNORECASE,
)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")

# ── 类型自动识别（内容嗅探）────────────────────────────────────────
# 设计原则：保守判、模糊回退需求。判成需求的代价只是多喂一章正文给抽取
# （噪音）；判成接口/模型的代价是该章被排除出需求抽取（丢准则），所以
# 接口/模型只在强信号下才判，需求永远是兜底。
_MODEL_TITLE_RE = re.compile(r"数据?模型|实体定义|表结构|数据结构")
_MODEL_FIELD_LINE_RE = re.compile(
    r"^\s*[-•*]?\s*(字段|fields?|属性|attributes?)\s*[：:]", re.IGNORECASE
)


# ═══════════════════════════════════════════════════════════════════
# 章节切分（Markdown 标题层级 → 行号范围）
# ═══════════════════════════════════════════════════════════════════


def split_into_chapters(text: str) -> list[dict[str, Any]]:
    """把归一化文本按标题层级切章节（确定性）。

    返回章节列表（无 chapter_id，由 build_doc_index 统一编号），每项：
      {title, level, line_start, line_end(1-based 闭区间), chars, summary}
    无任何标题 → 整份文档单章（title「（全文）」）；首章前的散落内容归「（开篇）」。
    """
    lines = text.splitlines()
    heads = [
        (i, len(m.group(1)), m.group(2).strip())
        for i, m in ((_i, _MD_HEADING_RE.match(ln)) for _i, ln in enumerate(lines))
        if m
    ]
    if not heads:
        return [_make_chapter("（全文）", 1, 1, len(lines), lines)]

    # 阈值：#/## 都开章；整份文档没有 ≤2 级标题（全部更深）时才退化用最浅层级
    shallow = [lv for _, lv, _ in heads if lv <= CHAPTER_HEAD_LEVEL]
    threshold = CHAPTER_HEAD_LEVEL if shallow else min(lv for _, lv, _ in heads)

    # 首个章节标题之前的内容（>2 行非空）算开篇
    first = heads[0][0]
    chapters: list[dict[str, Any]] = []
    if first > 0 and any(ln.strip() for ln in lines[:first]):
        chapters.append(_make_chapter("（开篇）", threshold, 1, first, lines))

    chapter_heads = [(i, lv, title) for i, lv, title in heads if lv <= threshold]
    for si, (line_no, level, title) in enumerate(chapter_heads):
        end = chapter_heads[si + 1][0] - 1 if si + 1 < len(chapter_heads) else len(lines)
        chapters.append(_make_chapter(title, level, line_no + 1, end, lines))
    return chapters


def _make_chapter(
    title: str, level: int, line_start: int, line_end: int, lines: list[str]
) -> dict[str, Any]:
    body = lines[line_start - 1:line_end]
    summary = next(
        (ln.strip() for ln in body
         if ln.strip() and not _MD_HEADING_RE.match(ln)),
        "",
    )[:80]
    return {
        "title": title[:120],
        "level": level,
        "line_start": line_start,
        "line_end": max(line_end, line_start),
        "chars": len("\n".join(body)),
        "summary": summary,
    }


# ═══════════════════════════════════════════════════════════════════
# 文档归一化：OpenAPI JSON / 模型 Schema → Markdown（章节与条目共用一套行号切片）
# ═══════════════════════════════════════════════════════════════════


def normalize_api_doc(text: str, path: Path) -> str:
    """接口文档归一化：OpenAPI/Swagger JSON → Markdown（每端点一章，原文内联）。

    Markdown/普通文本原样返回（标题里带 METHOD /path 的章节会被识别为接口条目，
    其余章节保留供对齐引用）。解析失败 / 非 OpenAPI 结构 → 原样返回，不硬解析。
    """
    if path.suffix.lower() not in (".json",):
        return text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(data, dict) or "paths" not in data or not isinstance(
        data.get("paths"), dict
    ):
        return text
    if "openapi" not in data and "swagger" not in data:
        return text

    out: list[str] = [f"# {data.get('info', {}).get('title') or 'OpenAPI 接口文档'}"]
    desc = str((data.get("info") or {}).get("description") or "").strip()
    if desc:
        out += [desc[:2000], ""]
    for p_path, item in data["paths"].items():
        if not isinstance(item, dict):
            continue
        shared = {k: item[k] for k in ("parameters",) if k in item}
        for method in ("get", "post", "put", "delete", "patch", "head", "options"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            merged: dict[str, Any] = {**shared, **{
                k: op[k] for k in _OPENAPI_OP_KEYS if k in op
            }}
            title = str(op.get("summary") or op.get("operationId") or "").strip()
            out += [
                f"## {method.upper()} {p_path}" + (f"  {title}" if title else ""),
                "```json",
                json.dumps(merged, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
    return "\n".join(out)


def normalize_model_doc(text: str, path: Path) -> str:
    """模型文档归一化：JSON Schema（单模型或多模型映射）→ Markdown（每模型一章）。

    根对象是 {模型名: {properties...}} 的映射、或单个 object schema（带 properties）
    时转 Markdown；否则原样返回（Markdown 模型文档天然按章节切）。
    """
    if path.suffix.lower() != ".json":
        return text
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(data, dict):
        return text

    def _is_model(v: Any) -> bool:
        return isinstance(v, dict) and "properties" in v

    if _is_model(data):  # 单模型 schema
        name = str(data.get("title") or path.stem)
        return "\n".join([
            f"## {name}",
            "```json",
            json.dumps(data, ensure_ascii=False, indent=2),
            "```",
        ])
    models = {k: v for k, v in data.items() if _is_model(v)}
    if len(models) >= 2:  # 多模型映射
        out = ["# 模型定义"]
        for name, schema in models.items():
            out += [
                f"## {name}",
                "```json",
                json.dumps(schema, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        return "\n".join(out)
    return text


# ═══════════════════════════════════════════════════════════════════
# 类型自动识别（文档级嗅探 + 章节级改判）
# ═══════════════════════════════════════════════════════════════════


def _chapter_body(lines: list[str], ch: dict[str, Any]) -> str:
    return "\n".join(lines[int(ch.get("line_start", 1)) - 1:int(ch.get("line_end", 0))])


def _model_chapter_signals(title: str, body: str) -> tuple[int, bool, bool]:
    """模型章节信号：(字段行数, 是否有单行多字段枚举, 标题带模型类标记)。"""
    field_lines = [ln for ln in body.splitlines() if _MODEL_FIELD_LINE_RE.match(ln)]
    # 「字段：id, amount, status」单行枚举多个字段 = 充分证据（模型文档的常见形态）
    rich = any(len(re.split(r"[,，、]", ln.split("：", 1)[-1].split(":", 1)[-1])) >= 2
               for ln in field_lines)
    return len(field_lines), rich, bool(_MODEL_TITLE_RE.search(title))


def _looks_like_model_chapter(title: str, body: str) -> bool:
    """章节级模型判据（保守）：≥2 字段行，或单行枚举多字段，或标题带标记且有佐证。"""
    n_fields, rich, marked = _model_chapter_signals(title, body)
    if n_fields >= 2 or rich:
        return True
    return marked and (n_fields >= 1 or '"properties"' in body)


def sniff_chapter_kind(title: str, body: str) -> str | None:
    """单一混合文档里的章节改判：api / model；证据不足保持需求（返回 None）。

    api：标题含 METHOD /path，或正文 ≥2 行各含端点样式（接口清单章）；
    model：字段列表/模型标记判据（_looks_like_model_chapter）。
    """
    if _HTTP_METHOD_RE.search(title or ""):
        return "api"
    body_hits = sum(1 for ln in (body or "").splitlines() if _HTTP_METHOD_RE.search(ln))
    if body_hits >= 2:
        return "api"
    if _looks_like_model_chapter(title or "", body or ""):
        return "model"
    return None


def sniff_doc_kind(text: str, path: str | Path) -> str:
    """文档级类型嗅探：api / model / requirement（兜底，永不失败）。

    - .json 精确判：OpenAPI/Swagger（openapi|swagger + paths）→ api；
      模型 Schema（单 object properties 或多模型映射）→ model；其余 → requirement
    - Markdown/文本启发判：标题端点密度高 → api；过半章节是字段列表形态 → model；
      其余 → requirement（叙述式接口/模型文档与需求文档无法可靠区分，宁可当需求
      处理——多喂点正文给抽取，好过把准则当接口排除掉）
    """
    p = Path(path)
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return "requirement"
        if isinstance(data, dict) and isinstance(data.get("paths"), dict) \
                and ("openapi" in data or "swagger" in data):
            return "api"
        if isinstance(data, dict):
            if "properties" in data:
                return "model"
            # 模型映射（{"Order": {...properties}, ...}，单个也算）
            if any(isinstance(v, dict) and "properties" in v for v in data.values()):
                return "model"
        return "requirement"

    heads = [
        m.group(2) for m in (_MD_HEADING_RE.match(ln) for ln in text.splitlines())
        if m
    ]
    # 全局端点标题占比 ≥0.4 才判整份接口文档；占比不高（如需求文档里夹一个接口
    # 附录）不硬判——章节级改判（sniff_chapter_kind）会把端点章挑出来，整份误判
    # 反而会让端点条目丢失
    if heads and sum(1 for t in heads if _HTTP_METHOD_RE.search(t)) / len(heads) >= 0.4:
        return "api"
    chapters = split_into_chapters(text)
    lines = text.splitlines()
    modelish = sum(
        1 for ch in chapters
        if ch["title"] not in ("（全文）", "（开篇）")
        and _looks_like_model_chapter(ch["title"], _chapter_body(lines, ch))
    )
    if modelish >= max(1, (len(chapters) + 1) // 2):
        return "model"
    return "requirement"


def _has_special_chapters(text: str) -> bool:
    """需求文档里是否存在会被章节级改判的接口/模型附录（混合文档信号）。"""
    chapters = split_into_chapters(text)
    lines = text.splitlines()
    return any(
        ch["level"] <= CHAPTER_HEAD_LEVEL
        and sniff_chapter_kind(ch["title"], _chapter_body(lines, ch)) is not None
        for ch in chapters
    )


def classify_docs(
    paths: list[str | Path],
) -> tuple[list[Path], list[Path], list[Path]]:
    """把用户随手提交的一组文档按内容自动分拣为 (需求, 接口, 模型) 三桶。"""
    buckets: dict[str, list[Path]] = {"requirement": [], "api": [], "model": []}
    for p in paths:
        p = Path(p)
        buckets[sniff_doc_kind(_read_any(p), p)].append(p)
    return buckets["requirement"], buckets["api"], buckets["model"]


# 聊天路径识别：认「文件路径」（白名单扩展名）与「绝对目录」（可一层展开）；
# 裸文件名（需求.md）歧义太大——相对谁？——不认；带空格的路径认不了，用上传/CLI。
# 兼容 POSIX（/ 或 ~/ 或 ./）与 Windows（盘符 X:\ 或 UNC \\server\share）两种形态。
_DOC_EXT_RE = re.compile(r"\.(md|markdown|txt|docx|json)$", re.IGNORECASE)
# 注意不含 `.`——会把 ./x.md 的相对前缀剥掉；不含右括号——目录名可能自带
# 尾括号（…\备份（2024）），只有「文件名 + 尾标点」形态才截到扩展名
_PATH_TOKEN_STRIP = "，,;；！!？?'\""
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\")
# 文件名前缀 + 尾随标点/括号（如 /x/y.md））→ 截到扩展名
_FILE_WITH_TAIL_RE = re.compile(
    r"^(.*?\.(?:md|markdown|txt|docx|json))[）)\]】」'\"，,;；！!？?\s]*$",
    re.IGNORECASE,
)


def is_doc_filename(name: str) -> bool:
    """文件名是否属于摄入白名单（目录展开时过滤用）。"""
    return bool(_DOC_EXT_RE.search(str(name or "")))


def _iter_path_tokens(text: str) -> list[str]:
    """消息 → 路径 token（粗切分 + 引号/注解清理）。"""
    out: list[str] = []
    for tok in re.split(r"[\s，。；、|]+", text or ""):
        # 描述句里被引号夹住的路径（需求文档位于"D:\…\通知"目录下）：
        # 引号内容含分隔符才算路径，否则保留原 token 继续别的清理
        m = re.search(r"[\"'「『]([^\"'」』]+)[\"'」』]", tok)
        if m and ("/" in m.group(1) or "\\" in m.group(1)):
            tok = m.group(1)
        else:
            # 紧贴注释的括号组只在前面是文档文件名时剥（…models.md（按这些
            # 文档来））；目录名自带尾括号（…\备份（2024））不误伤
            m2 = re.match(r"^(.*)(（[^）]*）)$", tok)
            if m2 and _DOC_EXT_RE.search(m2.group(1)):
                tok = m2.group(1)
            else:
                # 文件名 + 尾随标点（含右括号）：截到扩展名，目录路径原样保留
                m3 = _FILE_WITH_TAIL_RE.match(tok)
                tok = m3.group(1) if m3 else tok.strip(_PATH_TOKEN_STRIP)
        tok = tok.strip().strip("'\"")
        if tok:
            out.append(tok)
    return out


def _is_abs_path(tok: str) -> bool:
    return bool(
        tok.startswith(("/", "~/", "./", "../"))
        or _WIN_ABS_RE.match(tok)
        or _UNC_RE.match(tok)
    )


def extract_doc_candidates(text: str) -> list[str]:
    """消息 → 文档摄入候选（文件或绝对目录，去重保序）。

    - 文件：含路径分隔符（/ 或 \\）+ 白名单扩展名（允许相对子路径 a/b.md）
    - 目录：绝对路径形态且无白名单扩展名（调用方负责 is_dir 判断并一层展开）
    只提取候选、不做存在性判断。
    """
    out: list[str] = []
    seen: set[str] = set()
    for tok in _iter_path_tokens(text):
        has_sep = ("/" in tok) or ("\\" in tok)
        if not has_sep:
            continue
        if _DOC_EXT_RE.search(tok):
            pass  # 文件形态
        elif _is_abs_path(tok):
            pass  # 目录形态候选
        else:
            continue
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def extract_doc_paths(text: str) -> list[str]:
    """从聊天消息里提取文档**文件**路径候选（去重保序，目录候选已滤掉）。

    判据：含路径分隔符（/ 或 \\）+ 白名单扩展名结尾；POSIX 与 Windows 盘符路径
    （D:\\…\\x.md）都认。只提取候选、不做存在性判断（调用方负责 is_file 过滤）。
    """
    return [p for p in extract_doc_candidates(text) if _DOC_EXT_RE.search(p)]


def expand_doc_inputs(
    paths: list[str], *, max_files: int = 20
) -> tuple[list[str], list[str], list[str]]:
    """路径列表 → (可摄入的文档文件, 展开的目录描述, 跳过项)，文件列表去重保序。

    文件直取；目录按白名单扩展名一层展开（上限 max_files，防整盘目录灌爆）；
    其余（不存在/非白名单/空目录）进跳过项。web 聊天与 CLI 共用同一口径。
    """
    files: list[str] = []
    expanded: list[str] = []
    skipped: list[str] = []
    for cand in paths:
        p = Path(cand)
        if p.is_file() and is_doc_filename(p.name):
            files.append(cand)
        elif p.is_dir():
            kids = [
                str(k) for k in sorted(p.iterdir())
                if k.is_file() and is_doc_filename(k.name)
            ][:max_files]
            if kids:
                files.extend(kids)
                expanded.append(f"{cand}（{len(kids)} 份）")
            else:
                skipped.append(cand)
        else:
            skipped.append(cand)
    return list(dict.fromkeys(files)), expanded, skipped


# ── 自由文本里的路径扫描（带空格路径的回退方案）──────────────────
# 聊天输入按空白切 token，含空格的路径会被拆碎；资源管理器粘贴的路径还常带
# 中文/引号/尾随说明。回退策略：找到路径起始符后，按字符尝试渐长前缀，
# 取「磁盘上存在的最长前缀」——空格、尾随「目录下」等噪音自然被裁掉。
_PATH_START_RE = re.compile(
    r"(?:/|~/|\./|\.\./|[A-Za-z]:[\\/]|\\\\)"
)
_PATH_START_OK_PREFIX = set(" \t\n\"'「『（(")
# 注意不含 ':'：URL（https://…）的首个 / 前是冒号，不应被当路径起点；
# Windows 盘符（D:/…）由 _PATH_START_RE 的 drive 分支在字母处命中


def looks_like_path_mention(text: str) -> bool:
    """文本里是否出现路径样子的内容（用于「没识别到但确实给了路径」的提示）。"""
    t = text or ""
    if re.search(r"\.(md|markdown|txt|docx|json)(?![\w])", t, re.IGNORECASE):
        return True  # 文档扩展名出现在句中即可（不要求结尾）
    return bool(
        re.search(r"(?:^|[\s\"'「『（(])(?:/|~/|\./|\.\./|[A-Za-z]:[\\/]|\\\\)", t)
    )


def scan_existing_paths(text: str, *, max_path_len: int = 400) -> list[str]:
    """从自由文本扫描磁盘上真实存在的文档路径（文件或含文档的目录）。

    作为 token 提取无结果时的回退：支持含空格/中文的路径，并用「最长存在前缀」
    自动裁掉尾随说明文字（如「…/需求 文档」目录下）。文件要求白名单扩展名；
    目录要求一层内至少含一份白名单文档（避免误吞 /tmp 这类无关目录）。
    只返回路径候选，不展开。
    """
    out: list[str] = []
    seen: set[str] = set()
    s = text or ""
    for m in _PATH_START_RE.finditer(s):
        i = m.start()
        if i > 0 and s[i - 1] not in _PATH_START_OK_PREFIX:
            continue
        # 路径候选区间：到行尾/引号/中文括号/句读为止
        end = len(s)
        for stop in ("\n", "\"", "'", "”", "」", "』", "）", ")", "。", "，"):
            j = s.find(stop, i)
            if j != -1:
                end = min(end, j)
        line = s[i:min(end, i + max_path_len)]
        best = ""
        # 从长到短，取存在的最长前缀（尾随噪音自动裁掉）
        for cut in range(len(line), 1, -1):
            cand = line[:cut].rstrip(" \t,;，；")
            if len(cand) < 2:
                continue
            p = Path(cand)
            try:
                if p.is_file():
                    if is_doc_filename(p.name):
                        best = cand
                        break
                elif p.is_dir():
                    if any(
                        c.is_file() and is_doc_filename(c.name)
                        for c in p.iterdir()
                    ):
                        best = cand
                        break
            except OSError:
                continue
        if best and best not in seen:
            seen.add(best)
            out.append(best)
    return out


# ═══════════════════════════════════════════════════════════════════
# 目录构建：多文档 → manifest（稳定 ID + 缓存 + manifest.json 落盘）
# ═══════════════════════════════════════════════════════════════════


def _read_any(path: Path) -> str:
    """读取文档文本：.json 直接读（OpenAPI / 模型 Schema），其余走 doc_reader。"""
    if path.suffix.lower() == ".json":
        if not path.exists():
            raise FileNotFoundError(f"需求文档不存在: {path}")
        return path.read_text(encoding="utf-8")
    return read_doc(path)


def build_doc_index(
    req_docs: list[str | Path],
    api_docs: list[str | Path] | None = None,
    model_docs: list[str | Path] | None = None,
    *,
    root: str | Path,
) -> dict[str, Any]:
    """摄入多文档，写 cache/<doc_id>.md 与 manifest.json，返回 manifest dict。

    章节正文不进 manifest（state 只放目录）；slice_text 按 root 下的缓存按需读取。
    接口/模型目录条目同时归属所在章节（chapter_id），对齐引用与切片复用同一套坐标。
    """
    root = Path(root).resolve()
    cache_dir = root / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "root": str(root),
        "docs": [],
        "chapters": [],
        "apis": [],
        "models": [],
    }

    groups: list[tuple[str, list[str | Path]]] = [
        ("requirement", list(req_docs or [])),
        ("api", list(api_docs or [])),
        ("model", list(model_docs or [])),
    ]
    ch_seq = api_seq = mdl_seq = 0
    for kind, paths in groups:
        for src in paths:
            p = Path(src)
            text = _read_any(p)  # FileNotFoundError / ValueError / ImportError 上抛
            if kind == "api":
                text = normalize_api_doc(text, p)
            elif kind == "model":
                text = normalize_model_doc(text, p)

            doc_id = f"D{len(manifest['docs']) + 1}"
            cache_path = f"cache/{doc_id}.md"
            (root / cache_path).write_text(text, encoding="utf-8")
            manifest["docs"].append({
                "doc_id": doc_id,
                "kind": kind,
                "name": p.name,
                "source_path": str(p.resolve()),
                "cache_path": cache_path,
                "chars": len(text),
            })

            chapters = split_into_chapters(text)
            doc_lines = text.splitlines()
            for ch in chapters:
                ch_seq += 1
                entry = {
                    "chapter_id": f"CH-{ch_seq:03d}",
                    "doc_id": doc_id,
                    **ch,
                }
                if kind == "requirement" and ch["level"] <= CHAPTER_HEAD_LEVEL:
                    # 章节级改判：单一混合文档（需求正文 + 接口/模型附录）——证据
                    # 充分的章节同时产出目录条目并标 entry_kind（排除出需求抽取，
                    # 但仍可按 chapter_id 切片引用）。改判保守：证据不足保持需求。
                    body = _chapter_body(doc_lines, ch)
                    ckind = sniff_chapter_kind(ch["title"], body)
                    if ckind == "api":
                        # 一章多端点（接口清单/附录）：逐端点出条目，共用 chapter_id
                        seen_eps: set[tuple[str, str]] = set()
                        matches: list[re.Match] = []
                        for mm in [
                            _HTTP_METHOD_RE.search(ch["title"]),
                            *map(_HTTP_METHOD_RE.search, body.splitlines()),
                        ]:
                            if not mm:
                                continue
                            key = (mm.group(1).upper(), mm.group(2))
                            if key in seen_eps:
                                continue
                            seen_eps.add(key)
                            matches.append(mm)
                            if len(matches) >= 20:
                                break
                        if matches:
                            entry["entry_kind"] = "api"
                            for mm in matches:
                                api_seq += 1
                                manifest["apis"].append({
                                    "api_id": f"API-{api_seq:03d}",
                                    "doc_id": doc_id,
                                    "chapter_id": entry["chapter_id"],
                                    "method": mm.group(1).upper(),
                                    "path": mm.group(2),
                                    "title": f"{mm.group(1).upper()} {mm.group(2)}",
                                    "chars": ch["chars"],
                                })
                    elif ckind == "model":
                        entry["entry_kind"] = "model"
                        mdl_seq += 1
                        manifest["models"].append({
                            "model_id": f"MDL-{mdl_seq:03d}",
                            "doc_id": doc_id,
                            "chapter_id": entry["chapter_id"],
                            "name": re.sub(r"^附录[A-Za-z0-9]?\s*", "", ch["title"]).split()[0][:60],
                            "title": ch["title"][:120],
                            "chars": ch["chars"],
                        })
                manifest["chapters"].append(entry)
                if kind == "api" and ch["level"] <= CHAPTER_HEAD_LEVEL:
                    m = _HTTP_METHOD_RE.search(ch["title"])
                    if m:
                        api_seq += 1
                        manifest["apis"].append({
                            "api_id": f"API-{api_seq:03d}",
                            "doc_id": doc_id,
                            "chapter_id": entry["chapter_id"],
                            "method": m.group(1).upper(),
                            "path": m.group(2),
                            "title": re.sub(r"\s+", " ", ch["title"]).strip()[:120],
                            "chars": ch["chars"],
                        })
                elif (
                    kind == "model"
                    and ch["level"] >= 2
                    and ch["title"] not in ("（全文）", "（开篇）")
                ):
                    mdl_seq += 1
                    manifest["models"].append({
                        "model_id": f"MDL-{mdl_seq:03d}",
                        "doc_id": doc_id,
                        "chapter_id": entry["chapter_id"],
                        "name": ch["title"].split()[0][:60],
                        "title": ch["title"][:120],
                        "chars": ch["chars"],
                    })

    (root / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "doc_index: %d 文档 → %d 章 / %d 接口 / %d 模型（%s）",
        len(manifest["docs"]), len(manifest["chapters"]),
        len(manifest["apis"]), len(manifest["models"]), root,
    )
    return manifest


def load_manifest(path_or_root: str | Path) -> dict[str, Any]:
    """从 manifest.json 路径或其所在目录加载 manifest。"""
    p = Path(path_or_root)
    if p.is_dir():
        p = p / MANIFEST_FILENAME
    return dict(json.loads(p.read_text(encoding="utf-8")))


def manifest_dir(manifest: dict[str, Any] | None) -> str | None:
    """manifest 所在目录（prompt 里提示执行器回查用）；无 manifest 返回 None。"""
    if not manifest or not manifest.get("root"):
        return None
    return str(manifest["root"])


# ═══════════════════════════════════════════════════════════════════
# 切片读取：按需从缓存提取章节 / 条目原文
# ═══════════════════════════════════════════════════════════════════


def _entry_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    idx: dict[str, dict[str, Any]] = {}
    for key in ("chapters", "apis", "models"):
        for e in manifest.get(key) or []:
            id_key = {
                "chapters": "chapter_id", "apis": "api_id", "models": "model_id",
            }[key]
            if e.get(id_key):
                idx[str(e[id_key])] = {**e, "_kind": key}
    return idx


def find_entry(manifest: dict[str, Any] | None, ref_id: str) -> dict[str, Any] | None:
    """按 ID（CH-xxx / API-xxx / MDL-xxx / D<n>）查目录条目。"""
    if not manifest:
        return None
    hit = _entry_index(manifest).get(str(ref_id))
    if hit is None:
        for d in manifest.get("docs") or []:
            if d.get("doc_id") == str(ref_id):
                return {**d, "_kind": "docs"}
    return hit


def _cache_lines(manifest: dict[str, Any], cache_path: str) -> list[str]:
    """读缓存正文；空 cache_path（agent 模式目录无缓存）直接返回空——不能
    Path("") 变成 Path(".") 去 read_text（那是目录，必抛 IsADirectoryError）。"""
    if not str(cache_path or "").strip():
        return []
    root = manifest.get("root") or "."
    return (Path(root) / cache_path).read_text(encoding="utf-8").splitlines()


def slice_text(
    manifest: dict[str, Any] | None,
    entry: dict[str, Any],
    *,
    max_chars: int,
) -> str:
    """读取条目对应章节的原文切片（chapter 直接取；api/model 经 chapter_id）。

    超过 max_chars 从尾部截断并标注（契约类条目尽量给全，截断显式可见）。
    """
    if not manifest or not entry:
        return ""
    ch = entry
    if entry.get("_kind") in ("apis", "models"):
        ch = find_entry(manifest, str(entry.get("chapter_id") or "")) or {}
    doc = next(
        (d for d in manifest.get("docs") or [] if d.get("doc_id") == ch.get("doc_id")),
        {},
    )
    if not doc or not ch:
        return ""
    lines = _cache_lines(manifest, str(doc.get("cache_path") or ""))
    text = "\n".join(lines[int(ch.get("line_start", 1)) - 1:int(ch.get("line_end", 0))])
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…（切片超长已截断，完整原文见缓存文档）"
    return text


def resolve_doc_slices(
    manifest: dict[str, Any] | None,
    doc_refs: dict[str, Any] | None,
    *,
    max_chars_per: int,
    max_slices: int,
) -> list[dict[str, str]]:
    """feature 的 doc_refs → [{ref, kind, title, text}] 原文切片（handoff 载荷）。

    api/model 条目解析到其归属章节切片；同一章节只出一次；数量按 refs 顺序
    截到 max_slices，单条截到 max_chars_per。
    """
    if not manifest or not doc_refs:
        return []
    idx = _entry_index(manifest)
    ordered: list[tuple[str, str]] = []  # (ref_id, kind)
    for key, kind in (("chapter_ids", "chapter"), ("api_ids", "api"), ("model_ids", "model")):
        for rid in doc_refs.get(key) or []:
            rid = str(rid)
            if rid in idx or find_entry(manifest, rid):
                ordered.append((rid, kind))
    out: list[dict[str, str]] = []
    seen_chapters: set[str] = set()
    for rid, kind in ordered:
        entry = find_entry(manifest, rid)
        if not entry:
            continue
        ch_id = str(entry.get("chapter_id") or rid)
        if ch_id in seen_chapters:
            continue
        seen_chapters.add(ch_id)
        text = slice_text(manifest, entry, max_chars=max_chars_per)
        if not text:
            continue
        out.append({
            "ref": str(entry.get("chapter_id") or rid),
            "kind": kind,
            "title": str(entry.get("title") or entry.get("name") or rid),
            "text": text,
        })
        if len(out) >= max_slices:
            break
    return out


# ═══════════════════════════════════════════════════════════════════
# 目录视图与批量分组（喂给 LLM 的紧凑形态）
# ═══════════════════════════════════════════════════════════════════


def render_manifest_view(
    manifest: dict[str, Any] | None,
    *,
    summary_chars: int = 60,
    max_chars: int = 16000,
) -> str:
    """manifest → 紧凑目录清单（拆分/设计 LLM 挑选 doc_refs 的依据）。"""
    if not manifest:
        return ""
    docs = [d for d in manifest.get("docs") or []]
    kind_label = {"requirement": "需求", "api": "接口", "model": "模型"}

    def _doc_head(d: dict[str, Any]) -> str:
        return (
            f"文档 {d.get('doc_id')} {d.get('name')}（{kind_label.get(d.get('kind'), d.get('kind'))}"
            f"，{d.get('chars', 0)} 字）"
        )

    parts: list[str] = []

    req_docs = [d for d in docs if d.get("kind") == "requirement"]
    if req_docs:
        parts.append("【需求文档章节目录】")
        for d in req_docs:
            parts.append(_doc_head(d))
            parts += [
                f"- {e['chapter_id']} {e['title']}（{e['chars']}字）"
                + (f"：{e['summary'][:summary_chars]}" if e.get("summary") else "")
                for e in manifest.get("chapters") or []
                if e.get("doc_id") == d.get("doc_id") and not e.get("entry_kind")
            ]

    api_kind_docs = [d for d in docs if d.get("kind") == "api"]
    apis = manifest.get("apis") or []
    if apis or api_kind_docs:
        parts.append("【接口目录】")
        # 条目驱动：显式接口文档与需求文档里章节改判出的接口条目都列在这里
        for d in docs:
            entries = [e for e in apis if e.get("doc_id") == d.get("doc_id")]
            if entries:
                parts.append(_doc_head(d))
                parts += [
                    f"- {e['api_id']} {e['method']} {e['path']}（{e['title']}，{e['chars']}字）"
                    for e in entries
                ]
        _append_orphan_chapters(parts, manifest, api_kind_docs, {a.get("chapter_id") for a in apis})

    model_kind_docs = [d for d in docs if d.get("kind") == "model"]
    models = manifest.get("models") or []
    if models or model_kind_docs:
        parts.append("【模型目录】")
        for d in docs:
            entries = [e for e in models if e.get("doc_id") == d.get("doc_id")]
            if entries:
                parts.append(_doc_head(d))
                parts += [
                    f"- {e['model_id']} {e['name']}（{e['title']}，{e['chars']}字）"
                    for e in entries
                ]
        _append_orphan_chapters(parts, manifest, model_kind_docs, {m.get("chapter_id") for m in models})

    view = "\n".join(parts)
    if len(view) > max_chars:
        view = view[:max_chars].rstrip() + "\n…（目录超长已截断，完整清单见 manifest.json）"
    return view


def _append_orphan_chapters(
    parts: list[str],
    manifest: dict[str, Any],
    docs: list[dict[str, Any]],
    claimed: set[Any],
) -> None:
    """接口/模型文档里没被目录条目认领的章节（对齐时可按 chapter_id 引用）。"""
    doc_ids = {d.get("doc_id") for d in docs}
    orphan = [
        e for e in manifest.get("chapters") or []
        if e.get("doc_id") in doc_ids and e.get("chapter_id") not in claimed
    ]
    if orphan:
        parts.append("（其余章节，可按 chapter_id 引用）")
        parts += [f"- {e['chapter_id']} {e['title']}（{e['chars']}字）" for e in orphan]


def requirement_chapters(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    """kind=requirement 文档的纯需求章节（分章 clarify 的抽取对象）。

    章节级改判为 api/model 的章节（entry_kind 非空）被排除——端点定义与字段表
    不进需求抽取；它们仍以 API/MDL 条目和 chapter_id 切片参与下游对齐。
    """
    if not manifest:
        return []
    return [
        e for e in manifest.get("chapters") or []
        if not e.get("entry_kind")
        and (docs_doc_kind(manifest, e.get("doc_id")) == "requirement")
    ]


def context_chapters(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    """接口/模型内容的章节（分章 clarify 的受限上下文：只用于抽取输入输出约束，
    不作为验收标准/边界场景的来源）。

    覆盖两类：接口/模型文档的章节，以及需求文档里被章节级改判为 api/model 的
    附录章（entry_kind）——它们同样承载入参/响应/字段信息。
    """
    if not manifest:
        return []
    return [
        e for e in manifest.get("chapters") or []
        if docs_doc_kind(manifest, e.get("doc_id")) in ("api", "model")
        or e.get("entry_kind") in ("api", "model")
    ]


def docs_doc_kind(manifest: dict[str, Any], doc_id: Any) -> str:
    return next(
        (str(d.get("kind")) for d in manifest.get("docs") or [] if d.get("doc_id") == doc_id),
        "",
    )


def select_context_chapters(
    manifest: dict[str, Any] | None,
    *,
    max_chapters: int,
    probe_chars: int = 4000,
) -> list[dict[str, Any]]:
    """按与需求内容的关联度挑选接口/模型上下文章节（不足按文档顺序补齐）。

    多文档、接口章节很多时，顺序截前 N 章会漏掉需求真正提到的端点/模型——
    这里按确定性关联度排序：需求正文中出现接口路径(+5)/路径末段(+2)/模型名(+3)/
    章节标题(+3) 的章节优先，无信号时保持文档顺序。返回顺序即批次顺序。
    """
    ctx = context_chapters(manifest)
    if not ctx or max_chapters <= 0:
        return []
    req_text = "\n".join(
        slice_text(manifest, ch, max_chars=probe_chars)
        for ch in requirement_chapters(manifest)
    )
    api_by_ch = {a.get("chapter_id"): a for a in manifest.get("apis") or []}
    model_by_ch = {m.get("chapter_id"): m for m in manifest.get("models") or []}
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for i, ch in enumerate(ctx):
        score = 0
        title = str(ch.get("title") or "").strip()
        if req_text and title and title in req_text:
            score += 3
        api = api_by_ch.get(ch.get("chapter_id")) or {}
        path = str(api.get("path") or "")
        if req_text and path:
            if path in req_text:
                score += 5
            elif path.rstrip("/").rsplit("/", 1)[-1] in req_text:
                score += 2
        mdl = model_by_ch.get(ch.get("chapter_id")) or {}
        name = str(mdl.get("name") or "")
        if req_text and name and name in req_text:
            score += 3
        scored.append((score, i, ch))
    ordered = [ch for _, _, ch in sorted(scored, key=lambda x: (-x[0], x[1]))]
    return ordered[:max_chapters]


def batch_chapters(
    chapters: list[dict[str, Any]], max_chars: int
) -> list[list[dict[str, Any]]]:
    """把章节按顺序聚批（每批正文 ≤ max_chars，单章超限独立成批）——
    分章抽取的 LLM 调用次数 = 批数，而不是章数（30 章文档不产生 30 次调用）。"""
    batches: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_chars = 0
    for ch in chapters:
        n = int(ch.get("chars") or 0)
        if cur and cur_chars + n > max_chars:
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(ch)
        cur_chars += min(n, max_chars)
    if cur:
        batches.append(cur)
    return batches


def concat_chapter_slices(
    manifest: dict[str, Any] | None,
    *,
    per_chapter_chars: int,
    total_chars: int,
) -> str:
    """全部需求章节切片拼接（auto spec 脑补兜底的文档上下文），总量截断。"""
    parts: list[str] = []
    used = 0
    for ch in requirement_chapters(manifest):
        if used >= total_chars:
            break
        text = slice_text(manifest, ch, max_chars=min(per_chapter_chars, total_chars - used))
        if text:
            parts.append(f"[{ch.get('chapter_id')}] {ch.get('title')}\n{text}")
            used += len(text)
    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════
# doc_refs 规整与模式决策
# ═══════════════════════════════════════════════════════════════════

DOC_REF_KEYS = ("chapter_ids", "api_ids", "model_ids")


def normalize_doc_refs(raw: Any) -> dict[str, list[str]]:
    """LLM 产出的 doc_refs → 只含合法键、字符串化去重的规整形态。"""
    out: dict[str, list[str]] = {k: [] for k in DOC_REF_KEYS}
    if not isinstance(raw, dict):
        return out
    for key in DOC_REF_KEYS:
        vals = raw.get(key)
        if not isinstance(vals, list):
            continue
        seen: set[str] = set()
        for v in vals:
            s = str(v or "").strip()
            if s and s not in seen:
                seen.add(s)
                out[key].append(s)
    return out


def format_doc_refs(doc_refs: dict[str, Any] | None) -> str:
    """doc_refs → 一行短描述（门禁卡 / 日志展示用）。"""
    refs = normalize_doc_refs(doc_refs)
    labels = {"chapter_ids": "章节", "api_ids": "接口", "model_ids": "模型"}
    segs = [
        f"{labels[k]} {'、'.join(refs[k])}" for k in DOC_REF_KEYS if refs[k]
    ]
    return "；".join(segs)


def should_use_index(
    total_req_chars: int, n_req: int, n_api: int, n_model: int, threshold: int
) -> bool:
    """是否启用 manifest 模式：多需求文档 / 提供接口或模型文档 / 需求正文超阈值。"""
    return bool(n_api or n_model) or n_req > 1 or total_req_chars >= max(1, threshold)


def ingest_docs(
    req_docs: list[str | Path],
    api_docs: list[str | Path] | None = None,
    model_docs: list[str | Path] | None = None,
    *,
    root: str | Path,
    threshold: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    """入口决策 + 摄入：返回 (manifest 或 None, 需求正文列表)。

    **自动识别**：req_docs 里的每份文档按内容嗅探分拣（sniff_doc_kind——JSON
    精确判、Markdown 启发判、模糊回退需求），用户不需要按类型分开提交；
    api_docs / model_docs 是显式强制指定（类型不再重判），用于嗅探失手时人工纠偏。
    - manifest 模式（分拣出接口/模型文档、多需求文档、或需求正文超阈值）→
      manifest 非 None，调用方放进 state["doc_manifest"]，首条消息只喂
      doc_feed_notice 短文案
    - 单一小需求文档 → (None, [全文])，调用方走原文直喂老路径（零额外开销）
    读取失败（FileNotFoundError/ValueError/ImportError）原样上抛。
    """
    req_paths = [Path(p) for p in (req_docs or [])]
    texts = [_read_any(p) for p in req_paths]
    sniffed: dict[str, list[Path]] = {"requirement": [], "api": [], "model": []}
    mixed_signals = False
    for p, t in zip(req_paths, texts):
        kind = sniff_doc_kind(t, p)
        sniffed[kind].append(p)
        if kind == "requirement" and _has_special_chapters(t):
            mixed_signals = True  # 单一混合文档（需求正文 + 接口/模型附录）

    api_paths = [Path(p) for p in (api_docs or [])] + sniffed["api"]
    model_paths = [Path(p) for p in (model_docs or [])] + sniffed["model"]
    final_reqs = sniffed["requirement"]

    if not should_use_index(
        sum(len(_read_any(p)) for p in final_reqs),
        len(final_reqs), len(api_paths), len(model_paths),
        threshold,
    ) and not mixed_signals:
        return None, texts
    manifest = build_doc_index(final_reqs, api_paths, model_paths, root=root)
    return manifest, texts


def doc_feed_notice(manifest: dict[str, Any] | None) -> str:
    """manifest 模式的首条用户消息：短文案占位（全文不进对话上下文）。

    clarify_extract 看到未抽取的 doc_manifest 会走分章抽取，不依赖本消息内容；
    文案保留在会话里是给用户看的载入回执。
    """
    if not manifest:
        return ""
    n_ch = len(requirement_chapters(manifest))
    return (
        f"（已载入需求文档集：{n_ch} 个需求章节 / "
        f"{len(manifest.get('apis') or [])} 个接口 / "
        f"{len(manifest.get('models') or [])} 个模型，目录清单见 "
        f"{Path(str(manifest.get('root'))) / MANIFEST_FILENAME}。"
        "请直接从文档章节抽取需求，不要复述原文。）"
    )
