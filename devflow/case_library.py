"""采纳用例库（case library）：被采纳测试用例的跨会话持久登记。

之前采纳只落在会话 checkpoint（state.adopted_cases + test_report.test_cases），
会话删除即消失，且无法跨会话管理与复用。本模块把「被用户采纳的用例」登记为
独立资产，使两件事成为可能：

1. 管理与删除：devflow cases list/rm、Web 用例库页签，跨会话可见可删；
2. 独立沉淀：勾选任意会话的任意用例子集，经 distill 归纳为 checklist 库条目，
   不必再跑「需求澄清 → 用例生成」全流程。

设计约定：
- 登记时机在客户端采纳入口（Web /review/adopt、CLI approve <ids>），不在 review
  节点内——spec 自动全流程无人工过滤，天然不入库（与「不写 checklist 库」同语义）。
- 存储为每来源一份 JSONL（<root>/<thread_slug>.jsonl），一行一条完整结构化用例，
  附 thread_id / adopted_at 溯源；(thread_id, case_id) 幂等去重，重登记覆盖旧记录。
- 用例库是全局资产（跨项目累积），business 归属在沉淀为清单时才标记，
  因此库根不像 checklist 那样按项目解析：env 覆盖 > checkpoint 同级全局目录。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import settings

logger = logging.getLogger(__name__)

CASE_LIBRARY_DIRNAME = "case_library"
FILE_SUFFIX = ".jsonl"

# 与 auto_run.CSV_HEADERS 导出格式对齐（12 列中文表头）；反向映射用于回灌
_CSV_HEADER_MAP = {
    "标识": "case_id",
    "层级": "tier",
    "优先级": "priority",
    "类型": "case_type",
    "标题": "title",
    "所属模块": "target",
    "前置条件": "precondition",
    "步骤": "steps",
    "预期结果": "expected",
    "数据要求": "data_requirement",
    "设计依据": "rationale",
    "来源": "origin",
}
_TIER_FROM_NAME = {"功能": "functional", "性能": "performance", "安全": "security"}

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


# ═══════════════════════════════════════════════════════════════════
# 路径解析
# ═══════════════════════════════════════════════════════════════════


def resolve_root() -> Path:
    """用例库根：DEVFLOW_CASE_LIBRARY_ROOT > 全局（checkpoint 同级 data/case_library）。

    返回路径不保证已创建（空库是合法状态）。
    """
    env_root = os.getenv("DEVFLOW_CASE_LIBRARY_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser()
    return settings.CHECKPOINT_SQLITE_PATH.parent / CASE_LIBRARY_DIRNAME


def _thread_slug(thread_id: str) -> str:
    """thread_id → 安全文件名（会话 id 实际为 uuid 形态，净化只为防御性兜底）。"""
    slug = _UNSAFE_RE.sub("_", str(thread_id).strip())[:80].strip("._") or "thread"
    return slug


def _thread_file(root: Path, thread_id: str) -> Path:
    return root / (_thread_slug(thread_id) + FILE_SUFFIX)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ═══════════════════════════════════════════════════════════════════
# 登记 / 读取
# ═══════════════════════════════════════════════════════════════════


def register_cases(
    thread_id: str,
    cases: list[dict[str, Any]],
    adopted_ids: list[str] | None,
    *,
    adopted_at: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """把采纳子集登记入库（幂等）：按 case_id 过滤用例，重登记覆盖旧记录。

    返回 {"registered": 本次写入条数, "total": 该来源现有条数, "missing": 未匹配到的编号}。
    没有一条匹配时不落盘（防误调用制造空文件）。
    """
    root = root or resolve_root()
    wanted = {str(c).strip() for c in (adopted_ids or []) if str(c).strip()}
    picked: dict[str, dict[str, Any]] = {}
    for case in cases or []:
        if not isinstance(case, dict):
            continue
        cid = str(case.get("case_id") or "").strip()
        if cid and cid in wanted and cid not in picked:
            picked[cid] = case
    if not picked:
        return {"registered": 0, "total": 0, "missing": sorted(wanted)}

    path = _thread_file(root, thread_id)
    existing = _read_thread_file(path)
    stamp = adopted_at or _now_iso()
    for cid, case in picked.items():
        rec = dict(case)
        rec["thread_id"] = str(thread_id)
        rec["adopted_at"] = stamp
        existing[cid] = rec

    root.mkdir(parents=True, exist_ok=True)
    ordered = sorted(existing.values(), key=lambda r: str(r.get("case_id") or ""))
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered), encoding="utf-8"
    )
    return {
        "registered": len(picked),
        "total": len(existing),
        "missing": sorted(wanted - set(picked)),
    }


def _read_thread_file(path: Path) -> dict[str, dict[str, Any]]:
    """读单份 JSONL → {case_id: record}；坏行跳过（手工编辑容错）。"""
    out: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            logger.warning("case library: 跳过坏行 %s", path.name)
            continue
        if isinstance(rec, dict):
            cid = str(rec.get("case_id") or "").strip()
            if cid:
                out[cid] = rec
    return out


def load_thread(thread_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    """读某来源全部登记用例（按 case_id 排序）。"""
    root = root or resolve_root()
    return list(_read_thread_file(_thread_file(root, thread_id)).values())


def iter_threads(root: Path | None = None) -> list[dict[str, Any]]:
    """列举库中全部来源：[{thread_id, count, last_adopted_at}]，按最近采纳倒序。"""
    root = root or resolve_root()
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*" + FILE_SUFFIX)):
        recs = list(_read_thread_file(path).values())
        if not recs:
            continue
        stamps = [str(r.get("adopted_at") or "") for r in recs if r.get("adopted_at")]
        threads = {str(r.get("thread_id") or "") for r in recs} - {""}
        out.append({
            "thread_id": (sorted(threads)[0] if len(threads) == 1 else path.stem),
            "count": len(recs),
            "last_adopted_at": max(stamps) if stamps else "",
        })
    out.sort(key=lambda t: t["last_adopted_at"], reverse=True)
    return out


def list_cases(
    root: Path | None = None,
    *,
    thread: str | None = None,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    """跨来源列用例（附 thread 溯源）；thread/keyword 可选过滤，keyword 匹配编号/标题/模块/预期。"""
    root = root or resolve_root()
    if thread:
        files = [_thread_file(root, thread)] if _thread_file(root, thread).is_file() else []
    else:
        files = sorted(root.glob("*" + FILE_SUFFIX))
    kw = str(keyword or "").strip().lower()
    out: list[dict[str, Any]] = []
    for path in files:
        for rec in _read_thread_file(path).values():
            rec = dict(rec)
            rec.setdefault("thread_id", path.stem)
            if kw:
                blob = " ".join(
                    str(rec.get(k) or "") for k in ("case_id", "title", "target", "expected")
                ).lower()
                if kw not in blob:
                    continue
            out.append(rec)
    return out


# ═══════════════════════════════════════════════════════════════════
# 删除 / 导入 / 统计
# ═══════════════════════════════════════════════════════════════════


def remove_cases(
    *,
    thread: str | None = None,
    case_ids: list[str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """删除登记：只给 thread = 注销整个来源；给 case_ids（可配 thread 收窄）= 删指定条目。

    删空的来源文件一并移除。返回 {"removed": 条数, "threads_removed": [slug...]}。
    """
    root = root or resolve_root()
    wanted = {str(c).strip() for c in (case_ids or []) if str(c).strip()}
    if not thread and not wanted:
        raise ValueError("remove_cases 需要 thread 或 case_ids 至少其一")
    files = [_thread_file(root, thread)] if thread else sorted(root.glob("*" + FILE_SUFFIX))
    removed = 0
    threads_removed: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        recs = _read_thread_file(path)
        if thread and not wanted:  # 整线程注销
            removed += len(recs)
            path.unlink()
            threads_removed.append(path.stem)
            continue
        keep = {cid: rec for cid, rec in recs.items() if cid not in wanted}
        dropped = len(recs) - len(keep)
        if not dropped:
            continue
        removed += dropped
        if keep:
            ordered = sorted(keep.values(), key=lambda r: str(r.get("case_id") or ""))
            path.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ordered),
                encoding="utf-8",
            )
        else:
            path.unlink()
            threads_removed.append(path.stem)
    return {"removed": removed, "threads_removed": threads_removed}


def import_cases_file(
    path: str | Path,
    *,
    thread_label: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """外部用例文件整体登记为新来源：.csv = CaseCraft 导出格式，.json = 用例数组或 test_report。

    缺编号的用例自动补 IMP-{序号}；返回 {"thread_id", "registered", "total_in_file"}。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        cases = _cases_from_csv(p.read_text(encoding="utf-8-sig"))
    elif suffix == ".json":
        cases = _cases_from_json(json.loads(p.read_text(encoding="utf-8")))
    else:
        raise ValueError(f"不支持的格式 {suffix}（仅 .csv / .json）")

    label = (thread_label or "").strip() or f"imported-{p.stem}"
    ids = [str(c.get("case_id") or "") for c in cases if c.get("case_id")]
    reg = register_cases(label, cases, ids, root=root)
    return {
        "thread_id": label,
        "registered": reg["registered"],
        "total_in_file": len(cases),
    }


def _cases_from_csv(text: str) -> list[dict[str, Any]]:
    """CaseCraft 导出 CSV（中文表头 + BOM）→ 结构化用例；表头不识别的列忽略。"""
    out: list[dict[str, Any]] = []
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=1):
        case: dict[str, Any] = {}
        for header, value in (row or {}).items():
            key = _CSV_HEADER_MAP.get(str(header or "").strip().lstrip("\ufeff"))
            if key and value not in (None, ""):
                case[key] = str(value).strip()
        if not case:
            continue
        case["tier"] = _TIER_FROM_NAME.get(str(case.get("tier") or ""), case.get("tier") or "functional")
        case["origin"] = "manual" if case.get("origin") == "人工" else "ai"
        if not case.get("case_id"):
            case["case_id"] = f"IMP-{i:03d}"
        out.append(case)
    return out


def _cases_from_json(data: Any) -> list[dict[str, Any]]:
    """JSON → 用例列表：容忍 test_report 包裹形态 {"test_cases": [...]}。"""
    if isinstance(data, dict):
        data = data.get("test_cases") or []
    if not isinstance(data, list):
        raise ValueError("JSON 需是用例数组，或含 test_cases 数组的对象")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        case = dict(item)
        case["case_id"] = str(case.get("case_id") or "").strip() or f"IMP-{i:03d}"
        out.append(case)
    return out


def stats(root: Path | None = None) -> dict[str, Any]:
    """库概况：{root, threads, cases}。"""
    root = root or resolve_root()
    threads = iter_threads(root)
    return {"root": str(root), "threads": len(threads), "cases": sum(t["count"] for t in threads)}
