"""Unified diff 解析与落盘应用（执行闭环第一步：把 code_gen 产出的变更真正写进目标项目）。

职责边界：
  1. parse_unified_diff  — 把 Provider 产出的 unified diff 文本解析为 FilePatch 列表
  2. patch_from_content  — 用「整文件内容」直接构造 FilePatch（content_after 快捷路径）
  3. apply_patches       — all-or-nothing 落盘：先全量预检渲染，再备份原文件、写入新内容；
                           任一写入失败自动回滚本次已写的全部文件
  4. rollback            — 按备份目录里的 manifest.json 恢复到应用前状态

设计约束：
  - 纯标准库，不依赖 git / 第三方 diff 库（离线可测）
  - 只碰 project_root 内的相对路径；拒绝绝对路径与 `..` 越界（防 LLM 幻觉路径写穿）
  - 行内容统一按不带行尾的 \n 文本处理；「\ No newline at end of file」标记保留到产物
  - 备份目录固定为 project_root/.devflow_backup/<时间戳>，pytest 默认不收集点目录

Hunk 匹配策略：优先按 @@ 头的行号精确落位；上下文对不上时在整文件内找与
「上下文+删除行」序列完全一致的最近位置（容 LLM 行号漂移）；找不到 → 该文件预检失败。
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


class DiffApplyError(Exception):
    """diff 解析或落盘失败。files 里带每个文件的成功/失败明细。"""

    def __init__(self, message: str, files: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.files = files or []


# ═══════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════

@dataclass
class Hunk:
    """一个 @@ 块。lines 保留原始 tag（" "=上下文 / "-"=删除 / "+"=新增）。"""

    old_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    no_eof_newline: bool = False   # 块内出现 \ No newline at end of file

    @property
    def old_lines(self) -> list[str]:
        """上下文 + 删除行：用于在旧文件中定位。"""
        return [text for tag, text in self.lines if tag in (" ", "-")]

    @property
    def new_lines(self) -> list[str]:
        """上下文 + 新增行：hunk 应用后的样子。"""
        return [text for tag, text in self.lines if tag in (" ", "+")]


@dataclass
class FilePatch:
    path: str
    is_new: bool = False        # --- 侧为 /dev/null
    is_delete: bool = False     # +++ 侧为 /dev/null
    is_full: bool = False       # 整文件内容直写（content_after 路径，免 hunk 匹配）
    full_content: str = ""
    hunks: list[Hunk] = field(default_factory=list)
    no_eof_newline: bool = False


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _clean_path(raw: str) -> str | None:
    """清洗 diff 头里的路径；/dev/null 返回 None；拒绝绝对路径与 .. 越界。"""
    p = raw.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    p = p.split("\t")[0]  # 去掉 git 风格时间后缀
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    if p.startswith("/") or ".." in Path(p).parts:
        raise DiffApplyError(f"diff 中出现不安全的路径: {raw!r}")
    return p


# ═══════════════════════════════════════════════════════════════════
# 解析
# ═══════════════════════════════════════════════════════════════════

def parse_unified_diff(diff_text: str) -> list[FilePatch]:
    """解析 unified diff（单/多文件，容 git 头杂项）。解析不出任何 patch → DiffApplyError。"""
    if not diff_text or not diff_text.strip():
        raise DiffApplyError("diff 内容为空")

    patches: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    saw_old_path = False

    def close_patch() -> None:
        nonlocal cur, hunk
        if hunk is not None and cur is not None:
            cur.hunks.append(hunk)
            if hunk.no_eof_newline:
                cur.no_eof_newline = True
        hunk = None
        if cur is not None and cur.path:
            patches.append(cur)
        cur = None

    for raw_line in diff_text.splitlines():
        line = raw_line.rstrip("\r")

        if line.startswith("--- "):
            close_patch()
            path = _clean_path(line[4:])
            cur = FilePatch(path=path or "")
            saw_old_path = path is not None
            if path:
                cur.path = path
            continue
        if line.startswith("+++ "):
            path = _clean_path(line[4:])
            if cur is None:
                cur = FilePatch(path=path or "")
            if path is None:
                cur.is_delete = True
            elif not saw_old_path:
                cur.is_new = True
                cur.path = path
            elif cur.path:
                cur.path = path
            continue
        if line.startswith("@@ "):
            m = _HUNK_RE.match(line)
            if m is None:
                continue
            if hunk is not None and cur is not None:
                cur.hunks.append(hunk)
                if hunk.no_eof_newline:
                    cur.no_eof_newline = True
            hunk = Hunk(old_start=max(int(m.group(1)), 1))
            continue
        if hunk is not None and cur is not None:
            if line.startswith("\\"):
                hunk.no_eof_newline = True
                continue
            if line.startswith("+"):
                hunk.lines.append(("+", line[1:]))
            elif line.startswith("-"):
                hunk.lines.append(("-", line[1:]))
            elif line.startswith(" ") or line == "":
                hunk.lines.append((" ", line[1:] if line.startswith(" ") else ""))
            continue
        # 其余（diff --git / index / new file mode ...）忽略

    close_patch()
    if not patches:
        raise DiffApplyError("diff 中没有可识别的文件补丁")
    for p in patches:
        if not p.path:
            raise DiffApplyError("diff 缺少目标文件路径")
        if not p.is_full and not p.hunks:
            raise DiffApplyError(f"文件 {p.path} 的补丁没有 hunk")
    return patches


def patch_from_content(path: str, content: str) -> FilePatch:
    """用整文件内容构造直写补丁（Provider 返回 content_after 时免 diff 匹配）。"""
    if path.startswith("/") or ".." in Path(path).parts:
        raise DiffApplyError(f"不安全的文件路径: {path!r}")
    return FilePatch(path=path, is_full=True, full_content=content)


# ═══════════════════════════════════════════════════════════════════
# 定位 + 渲染
# ═══════════════════════════════════════════════════════════════════

def _match_at(old_lines: list[str], start: int, expected: list[str]) -> bool:
    if start < 0 or start + len(expected) > len(old_lines):
        return False
    return all(
        old_lines[start + i].rstrip("\r\n") == exp
        for i, exp in enumerate(expected)
    )


def _locate(old_lines: list[str], hunk: Hunk) -> int:
    """返回 hunk.old_lines 在 old_lines 中的 0 基下标；找不到抛 DiffApplyError。"""
    if not hunk.old_lines:  # 纯新增 hunk：锚定行号（越界则贴到文件尾）
        return min(max(hunk.old_start - 1, 0), len(old_lines))
    target = hunk.old_start - 1
    if _match_at(old_lines, target, hunk.old_lines):
        return target
    best: int | None = None
    best_dist: int | None = None
    for i in range(len(old_lines) - len(hunk.old_lines) + 1):
        if _match_at(old_lines, i, hunk.old_lines):
            d = abs(i - target)
            if best_dist is None or d < best_dist:
                best, best_dist = i, d
    if best is None:
        raise DiffApplyError(
            f"@@ -{hunk.old_start} 的上下文在文件中匹配不上（文件内容与 diff 不一致）"
        )
    return best


def _render(old_lines: list[str], patch: FilePatch) -> str:
    """把 patch 的 hunks 重放到 old_lines 上，返回新文件全文（\n 连接）。"""
    out: list[str] = []
    pos = 0
    for hunk in patch.hunks:
        at = _locate(old_lines, hunk)
        out.extend(l.rstrip("\r\n") for l in old_lines[pos:at])
        pos = at
        oi = 0
        for tag, text in hunk.lines:
            if tag == " ":
                out.append(old_lines[at + oi].rstrip("\r\n"))
                oi += 1
            elif tag == "-":
                oi += 1
            else:
                out.append(text)
        pos = at + oi
    out.extend(l.rstrip("\r\n") for l in old_lines[pos:])
    if not out:
        return ""
    text = "\n".join(out)
    return text if patch.no_eof_newline else text + "\n"


def _render_new_content(root: Path, patch: FilePatch) -> str:
    """渲染单个文件补丁的目标内容；目标文件读不到 / 上下文不匹配 → DiffApplyError。"""
    if patch.is_full:
        return patch.full_content
    if patch.is_new:
        return _render([], patch)
    dest = root / patch.path
    if not dest.is_file():
        raise DiffApplyError(
            f"目标文件不存在: {patch.path}（新建文件的 diff 头应为 --- /dev/null）"
        )
    try:
        old_text = dest.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise DiffApplyError(f"文件 {patch.path} 不是 UTF-8 文本: {e}") from e
    return _render(old_text.splitlines(), patch)


def _action_of(patch: FilePatch) -> str:
    if patch.is_new:
        return "create"
    if patch.is_delete:
        return "delete"
    return "update"


# ═══════════════════════════════════════════════════════════════════
# 应用 + 回滚
# ═══════════════════════════════════════════════════════════════════

def apply_patches(
    project_root: str | Path,
    patches: list[FilePatch],
    *,
    backup: bool = True,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """all-or-nothing 落盘。返回 {applied, files: [{path, action, ok, error}], backup_dir}。

    流程：预检（全量渲染）→ 逐文件备份 + 写入 → 任一步失败回滚本次全部写入。
    """
    root = Path(project_root)
    if not patches:
        raise DiffApplyError("没有可应用的补丁")

    # ── 1. 预检：所有文件都能渲染出目标内容才动盘 ──
    rendered: list[tuple[FilePatch, str]] = []
    failures: list[dict[str, Any]] = []
    for p in patches:
        try:
            rendered.append((p, _render_new_content(root, p)))
        except DiffApplyError as e:
            failures.append(
                {"path": p.path, "action": _action_of(p), "ok": False, "error": str(e)}
            )
    if failures:
        raise DiffApplyError(
            "; ".join(f"{f['path']}: {f['error']}" for f in failures), files=failures
        )

    # ── 2. 备份 + 写入 ──
    bdir: Path | None = None
    if backup:
        bdir = (
            Path(backup_dir)
            if backup_dir is not None
            else root / ".devflow_backup" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
    manifest: list[dict[str, Any]] = []
    try:
        for p, content in rendered:
            dest = root / p.path
            existed = dest.is_file()
            if bdir is not None and existed:
                bdest = bdir / p.path
                bdest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, bdest)
            if p.is_delete:
                dest.unlink()
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
            manifest.append({"path": p.path, "existed": existed})
    except Exception as e:
        _restore(root, manifest, bdir)
        raise DiffApplyError(f"落盘失败（已回滚）: {e}") from e

    if bdir is not None:
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return {
        "applied": True,
        "files": [
            {"path": p.path, "action": _action_of(p), "ok": True, "error": None}
            for p, _ in rendered
        ],
        "backup_dir": str(bdir) if bdir is not None else None,
    }


def _restore(root: Path, manifest: list[dict[str, Any]], bdir: Path | None) -> None:
    """按 manifest 恢复：existed 的从备份拷回，新建的删除。"""
    for item in manifest:
        f = root / item["path"]
        try:
            if item["existed"]:
                b = (bdir / item["path"]) if bdir is not None else None
                if b is not None and b.is_file():
                    f.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(b, f)
            else:
                f.unlink(missing_ok=True)
        except OSError:
            pass  # 回滚尽力而为；失败细节已在抛出的异常链里


def rollback(project_root: str | Path, backup_dir: str | Path) -> dict[str, Any]:
    """把整个备份目录回滚到目标项目。返回 {restored, missing}。"""
    root = Path(project_root)
    bdir = Path(backup_dir)
    mpath = bdir / "manifest.json"
    if not mpath.is_file():
        raise DiffApplyError(f"备份目录缺少 manifest.json: {bdir}")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    _restore(root, manifest, bdir)
    return {"restored": len(manifest), "backup_dir": str(bdir)}


def apply_diff(
    project_root: str | Path,
    diff_text: str,
    *,
    backup: bool = True,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """解析 + 应用一条 unified diff 文本。解析/预检/写入失败统一抛 DiffApplyError。"""
    patches = parse_unified_diff(diff_text)
    return apply_patches(project_root, patches, backup=backup, backup_dir=backup_dir)
