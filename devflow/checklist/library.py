"""Checklist 库的文件层操作：定位、扫描、加载、写盘。

路径约定：一切对外接口用「相对库根的目录路径」（rel_dir，如 "payment" /
"payment/refund"），段名限定 [A-Za-z0-9][A-Za-z0-9_.-]*，杜绝路径穿越。
以 "_" 开头的目录（如 _template）是脚手架模板，扫描一律跳过。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config import settings
from .models import RouteCandidate, RouteMatch, ScenarioDoc, ScenarioRef

logger = logging.getLogger(__name__)

CHECKLIST_DIRNAME = ".checklist"
SCENARIO_FILE = "scenario.md"
CHECKLIST_FILE = "checklist.md"

_SEG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


# ═══════════════════════════════════════════════════════════════════
# 路径解析与安全
# ═══════════════════════════════════════════════════════════════════


def resolve_root(project_root: str = "") -> Path:
    """库根解析：DEVFLOW_CHECKLIST_ROOT > <project_root>/.checklist（存在时）> 全局。

    全局回退 = checkpoint 同级 data/checklist，保证仅需求模式（无 project_root）
    也能用库。返回的路径不保证已创建（空库是合法状态）。
    """
    env_root = os.getenv("DEVFLOW_CHECKLIST_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser()
    if project_root:
        candidate = Path(project_root).expanduser() / CHECKLIST_DIRNAME
        if candidate.is_dir():
            return candidate
    return settings.CHECKPOINT_SQLITE_PATH.parent / "checklist"


def validate_rel_dir(rel_dir: str) -> Optional[str]:
    """校验并归一化 rel_dir（去首尾斜杠）；非法返回 None。"""
    rel = str(rel_dir or "").strip().strip("/")
    if not rel:
        return None
    segments = rel.split("/")
    for seg in segments:
        if not _SEG_RE.match(seg) or seg.startswith("_"):
            return None
    return rel


def _dir_for(root: Path, rel_dir: str) -> Optional[Path]:
    rel = validate_rel_dir(rel_dir)
    if rel is None:
        return None
    return root / rel


# ═══════════════════════════════════════════════════════════════════
# frontmatter 解析与 scenario 加载
# ═══════════════════════════════════════════════════════════════════


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """剥离 markdown 头部 YAML frontmatter，返回 (meta, body)。

    无 frontmatter / 未闭合 / YAML 非法 → ({}, 原文)。解析失败绝不抛异常
    （库文件是用户手写的，容错优先）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() != "---":
            continue
        raw_yaml = "\n".join(lines[1:i])
        body = "\n".join(lines[i + 1:])
        try:
            meta = yaml.safe_load(raw_yaml) or {}
        except yaml.YAMLError as e:
            logger.warning("scenario frontmatter YAML 解析失败（按无标签处理）: %s", e)
            return {}, text
        return (meta if isinstance(meta, dict) else {}), body.strip()
    return {}, text


def load_scenario(root: Path, rel_dir: str) -> Optional[ScenarioDoc]:
    """加载 <root>/<rel_dir>/scenario.md；文件缺失或 rel 非法返回 None。"""
    d = _dir_for(root, rel_dir)
    if d is None:
        return None
    scenario_path = d / SCENARIO_FILE
    if not scenario_path.is_file():
        return None
    try:
        text = scenario_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("scenario.md 读取失败 %s: %s", scenario_path, e)
        return None
    meta, body = parse_frontmatter(text)
    refs_raw = meta.get("references") or []
    references: list[ScenarioRef] = []
    if isinstance(refs_raw, list):
        for r in refs_raw:
            if isinstance(r, dict) and str(r.get("path") or "").strip():
                references.append(
                    ScenarioRef(path=str(r["path"]).strip(), desc=str(r.get("desc") or ""))
                )
            elif isinstance(r, str) and r.strip():
                references.append(ScenarioRef(path=r.strip()))
    keywords = meta.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    dir_name = rel_dir.split("/")[-1]
    return ScenarioDoc(
        rel_dir=rel_dir,
        name=str(meta.get("name") or "").strip() or dir_name,
        description=str(meta.get("description") or "").strip(),
        keywords=[str(k) for k in keywords if str(k).strip()],
        references=references,
        usage=body.strip(),
    )


# ═══════════════════════════════════════════════════════════════════
# 扫描（路由数据源：只读 frontmatter，渐进披露）
# ═══════════════════════════════════════════════════════════════════


def scan_business_types(root: Path) -> list[ScenarioDoc]:
    """顶层业务类型（一级子目录中带 scenario.md 的；跳过 "_" 前缀模板目录）。"""
    if not root.is_dir():
        return []
    docs: list[ScenarioDoc] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        doc = load_scenario(root, child.name)
        if doc is not None:
            docs.append(doc)
    return docs


def scan_sub_businesses(root: Path, business_rel: str) -> list[ScenarioDoc]:
    """某业务类型下的全部子业务（任意深度、平铺返回；rel_dir 含业务前缀）。"""
    base = _dir_for(root, business_rel)
    if base is None or not base.is_dir():
        return []
    docs: list[ScenarioDoc] = []
    for scenario_path in sorted(base.rglob(SCENARIO_FILE)):
        rel = scenario_path.parent.relative_to(root).as_posix()
        if rel == business_rel:
            continue
        doc = load_scenario(root, rel)
        if doc is not None:
            docs.append(doc)
    return docs


def catalog_for_routing(root: Path) -> list[dict[str, Any]]:
    """给路由 LLM 的目录摘要：业务 + 子业务（仅 frontmatter，正文不进上下文）。"""
    catalog: list[dict[str, Any]] = []
    for biz in scan_business_types(root):
        subs = scan_sub_businesses(root, biz.rel_dir)
        catalog.append(
            {
                "name": biz.rel_dir,
                "display_name": biz.name,
                "description": biz.description,
                "keywords": biz.keywords,
                "references": [r.model_dump() for r in biz.references],
                "sub_businesses": [
                    {
                        "name": sub.rel_dir,
                        "display_name": sub.name,
                        "description": sub.description
                        or next(
                            (r.desc for r in biz.references if r.path in sub.rel_dir),
                            "",
                        ),
                        "keywords": sub.keywords,
                    }
                    for sub in subs
                ],
            }
        )
    return catalog


def build_candidates(root: Path, match: RouteMatch) -> list[RouteCandidate]:
    """LLM 匹配结果 → 候选树（suggested 标记 AI 预选）。

    目录扫描是事实源：LLM 返回的名字必须能对应到实际存在的 scenario.md，
    幻觉条目直接丢弃。子业务匹配为空时，回落到该业务 frontmatter references
    指向的目录（用户手写的 reference 是第一优先提示）。
    """
    candidates: list[RouteCandidate] = []
    matched_names = {b.name for b in match.businesses}
    sub_by_biz: dict[str, list[str]] = {}
    for s in match.sub_businesses:
        sub_by_biz.setdefault(s.business, []).append(s.name)

    for biz in scan_business_types(root):
        if biz.rel_dir not in matched_names:
            continue
        reason = next((b.reason for b in match.businesses if b.name == biz.rel_dir), "")
        wanted = set(sub_by_biz.get(biz.rel_dir, []))
        if not wanted:
            wanted = {r.path for r in biz.references}
        subs: list[RouteCandidate] = []
        for sub in scan_sub_businesses(root, biz.rel_dir):
            # LLM 可能返回 "refund" 或全路径 "payment/refund"，两种都认
            if sub.rel_dir in wanted or any(
                sub.rel_dir.endswith("/" + w) for w in wanted
            ):
                subs.append(_candidate_from_doc(root, sub, suggested=True))
        candidates.append(
            RouteCandidate(
                rel_dir=biz.rel_dir,
                name=biz.name,
                description=biz.description,
                keywords=biz.keywords,
                has_checklist=(root / biz.rel_dir / CHECKLIST_FILE).is_file(),
                suggested=True,
                reason=reason,
                children=sorted(subs, key=lambda c: c.rel_dir),
            )
        )
    return candidates


def _candidate_from_doc(
    root: Path, doc: ScenarioDoc, *, suggested: bool, reason: str = ""
) -> RouteCandidate:
    d = _dir_for(root, doc.rel_dir)
    return RouteCandidate(
        rel_dir=doc.rel_dir,
        name=doc.name,
        description=doc.description,
        keywords=doc.keywords,
        has_checklist=bool(d and (d / CHECKLIST_FILE).is_file()),
        suggested=suggested,
        reason=reason,
    )


# ═══════════════════════════════════════════════════════════════════
# 加载与写盘
# ═══════════════════════════════════════════════════════════════════


def load_checklists(root: Path, selected: list[str]) -> list[dict[str, str]]:
    """按用户确认的 rel_dir 列表加载 checklist.md 正文。

    返回 [{rel_dir, name, business, content}]；无 checklist.md 的目录跳过。
    content 原样注入测试设计 prompt（渐进披露的最后一跳）。
    """
    docs: list[dict[str, str]] = []
    for rel_dir in selected:
        d = _dir_for(root, rel_dir)
        if d is None:
            continue
        checklist_path = d / CHECKLIST_FILE
        if not checklist_path.is_file():
            continue
        try:
            content = checklist_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("checklist.md 读取失败 %s: %s", checklist_path, e)
            continue
        doc = load_scenario(root, rel_dir)
        docs.append(
            {
                "rel_dir": rel_dir,
                "name": doc.name if doc else rel_dir,
                "business": rel_dir,
                "content": content,
            }
        )
    return docs


def write_checklist(
    root: Path,
    rel_dir: str,
    scenario_md: str,
    checklist_md: str,
) -> Optional[Path]:
    """沉淀落盘：写入 scenario.md + checklist.md（覆盖，merge 已在上游预览完成）。"""
    d = _dir_for(root, rel_dir)
    if d is None:
        raise ValueError(f"非法的业务目录名: {rel_dir!r}")
    d.mkdir(parents=True, exist_ok=True)
    (d / SCENARIO_FILE).write_text(scenario_md, encoding="utf-8")
    (d / CHECKLIST_FILE).write_text(checklist_md, encoding="utf-8")
    return d


# ═══════════════════════════════════════════════════════════════════
# 树状视图（CLI list / GET /api/checklist/tree）
# ═══════════════════════════════════════════════════════════════════


def _count_items(checklist_path: Path) -> int:
    try:
        return sum(
            1
            for line in checklist_path.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("-", "*")) and "[" in line
        )
    except OSError:
        return 0


def checklist_tree(root: Path) -> list[dict[str, Any]]:
    """库全树：业务 → 子业务，含描述/关键词/条目数（管理界面与沉淀弹窗用）。"""
    tree: list[dict[str, Any]] = []
    for biz in scan_business_types(root):
        entry: dict[str, Any] = {
            "rel_dir": biz.rel_dir,
            "name": biz.name,
            "description": biz.description,
            "keywords": biz.keywords,
            "has_checklist": (root / biz.rel_dir / CHECKLIST_FILE).is_file(),
            "item_count": _count_items(root / biz.rel_dir / CHECKLIST_FILE)
            if (root / biz.rel_dir / CHECKLIST_FILE).is_file()
            else 0,
            "children": [],
        }
        for sub in scan_sub_businesses(root, biz.rel_dir):
            sub_path = root / sub.rel_dir
            entry["children"].append(
                {
                    "rel_dir": sub.rel_dir,
                    "name": sub.name,
                    "description": sub.description,
                    "keywords": sub.keywords,
                    "has_checklist": (sub_path / CHECKLIST_FILE).is_file(),
                    "item_count": _count_items(sub_path / CHECKLIST_FILE)
                    if (sub_path / CHECKLIST_FILE).is_file()
                    else 0,
                    "children": [],
                }
            )
        tree.append(entry)
    return tree


__all__ = [
    "CHECKLIST_DIRNAME",
    "CHECKLIST_FILE",
    "SCENARIO_FILE",
    "build_candidates",
    "catalog_for_routing",
    "checklist_tree",
    "load_checklists",
    "load_scenario",
    "parse_frontmatter",
    "resolve_root",
    "scan_business_types",
    "scan_sub_businesses",
    "validate_rel_dir",
    "write_checklist",
]
