"""把运行清单库发布到 tc-checklist 孤儿分支（库根 = 分支根，与 main 零共同历史）。

git plumbing 实现：临时 GIT_INDEX_FILE + hash-object/write-tree/commit-tree + update-ref，
全程不切换分支、不碰当前工作区，只写 refs/heads/<branch> 一个引用。

用法:
  python scripts/publish_checklist.py                 # 发布（内容无变化则跳过）
  python scripts/publish_checklist.py --push          # 发布并推送 origin
  python scripts/publish_checklist.py --source <库根> --branch <名> --message <说明>

行为:
  - 默认源 = resolve_root()（DEVFLOW_CHECKLIST_ROOT > <project>/.checklist > data/checklist）
  - 过滤运行产物（__pycache__/.coverage/.devflow_backup/dts/.pyc 与目录条目）
  - 库根无 README.md 时自动生成（业务/条目数动态统计）
  - 分支不存在 → 根提交（孤儿）；已存在 → -p 挂上次 tip 保留分支内演进史
  - 幂等：新 tree 与分支现有 tip 相同 → 跳过
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRANCH_DEFAULT = "tc-checklist"

_NOISE_RE = re.compile(
    r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.devflow_backup|dts)(/|$)"
    r"|(^|/)\.coverage|\.pyc$"
)
_NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)
_ITEM_RE = re.compile(r"^- \[P[0-2]\]", re.MULTILINE)


def _git(*args: str, index: str | None = None, input_text: str | None = None,
         check: bool = True) -> str | None:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    r = subprocess.run(
        ["git", *args], cwd=str(REPO), env=env, capture_output=True,
        text=True, encoding="utf-8", input=input_text,
    )
    if r.returncode != 0:
        if check:
            raise SystemExit(f"git {' '.join(args)} 失败:\n{r.stderr.strip()}")
        return None
    return r.stdout.strip()


def _is_noise(rel: str) -> bool:
    return rel.endswith("/") or bool(_NOISE_RE.search(rel))


def _collect(source: Path) -> dict[str, Path]:
    """收集库根下全部非产物文件，rel 路径（POSIX 风格）→ 绝对路径。"""
    if not source.is_dir():
        raise SystemExit(f"清单库根不存在: {source}")
    files: dict[str, Path] = {}
    for p in sorted(source.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(source).as_posix()
        if _is_noise(rel):
            continue
        files[rel] = p
    if not files:
        raise SystemExit(f"清单库为空: {source}")
    return files


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _biz_stats(files: dict[str, Path]) -> list[dict[str, object]]:
    """按业务聚合：rel_dir / 名称 / 条目数 / 子业务列表（深度>1 归属一级目录）。"""
    stats: dict[str, dict[str, object]] = {}
    for rel in files:
        if rel == "README.md" or not rel.endswith("scenario.md"):
            continue
        biz = rel.split("/")[0]
        name = _NAME_RE.search(_read_text(files[rel]))
        entry = stats.setdefault(biz, {"name": biz, "items": 0, "subs": [], "has_checklist": False})
        if "/" not in rel:
            entry["name"] = (name.group(1) if name else biz)
            cl = Path(str(files[rel])).parent / "checklist.md"
            entry["has_checklist"] = cl.is_file()
            entry["items"] += len(_ITEM_RE.findall(_read_text(cl))) if cl.is_file() else 0
        else:
            sub = rel.rsplit("/", 1)[0]
            sub_cl = Path(str(files[rel])).parent / "checklist.md"
            n = len(_ITEM_RE.findall(_read_text(sub_cl))) if sub_cl.is_file() else 0
            entry["items"] += n
            entry["subs"].append((sub, (name.group(1) if name else sub), n))
    return [{**v, "rel": k} for k, v in sorted(stats.items())]  # type: ignore[list-item]


def _build_readme(files: dict[str, Path]) -> str:
    lines = [
        "# TC-CHECKLIST · 通用测试检查清单库",
        "",
        "CaseCraft（spec2test）的业务检查清单库快照：与 main 零共同历史的孤儿分支，",
        "只包含清单库本身、独立演进，永不合并主干。",
        "",
        "## 接入 DevFlow / CaseCraft",
        "",
        "```bash",
        "git clone -b tc-checklist --depth 1 https://github.com/cangyunye/spec2test my-checklist",
        "# 之后任选其一：",
        "#   设 DEVFLOW_CHECKLIST_ROOT=<克隆目录>",
        "#   或拷贝到目标项目 <project_root>/.checklist/",
        "```",
        "",
        "## 库内容一览",
        "",
        "| 业务（rel_dir） | 名称 | 条目 | 子业务 |",
        "|---|---|---|---|",
    ]
    for s in _biz_stats(files):
        subs = "、".join(f"{n}（{r}）{c}条" for r, n, c in s["subs"]) or "—"
        items = str(s["items"]) if s["has_checklist"] or s["subs"] else "—"
        lines.append(f"| `{s['rel']}` | {s['name']} | {items} | {subs} |")
    lines += [
        "",
        "## 结构与更新",
        "",
        "- 每个业务目录 = `scenario.md`（路由标签）+ `checklist.md`（分节检查点，`- [P0] 可验证一句话`）；",
        "- `_` 开头目录不参与路由；条目按「正向/反向/边界值/等价类/状态流转/场景法/安全/性能」八分节；",
        "- 本分支由主仓 `scripts/publish_checklist.py` 从运行库发布（内容以运行库为准），",
        "  主仓内改清单请同步运行库后重新发布；直接改本分支的提交也会被保留（下次发布会挂在其后）。",
        "",
        f"_{datetime.now().strftime('%Y-%m-%d')} 由 publish_checklist.py 生成_",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="发布运行清单库到 tc-checklist 孤儿分支")
    ap.add_argument("--source", default="", help="清单库根（默认 resolve_root()）")
    ap.add_argument("--branch", default=BRANCH_DEFAULT)
    ap.add_argument("--message", default="", help="提交说明（默认含时间戳）")
    ap.add_argument("--push", action="store_true", help="发布后推送 origin/<branch>")
    args = ap.parse_args()

    if args.source:
        source = Path(args.source).expanduser()
    else:
        sys.path.insert(0, str(REPO))
        try:
            from devflow.checklist.library import resolve_root
            source = resolve_root("")
        except Exception:
            source = REPO / "data" / "checklist"

    files = _collect(source)
    readme_src = files.pop("README.md", None)

    fd, idx = tempfile.mkstemp(prefix="publish-idx-")
    os.close(fd)  # 关闭句柄，否则 Windows 下 git 无法写入该文件
    try:
        _git("read-tree", "--empty", index=idx)  # 初始化为合法空索引
        for rel, path in sorted(files.items()):
            sha = _git("hash-object", "-w", str(path), index=idx)
            _git("update-index", "--add", "--cacheinfo", f"100644,{sha},{rel}", index=idx)
        readme_text = _read_text(readme_src) if readme_src else _build_readme(files)
        sha = _git("hash-object", "-w", "--stdin", index=idx, input_text=readme_text)
        _git("update-index", "--add", "--cacheinfo", f"100644,{sha},README.md", index=idx)
        tree = _git("write-tree", index=idx)
    finally:
        try:
            os.remove(idx)
        except OSError:
            pass

    branch = args.branch.lstrip("/")
    tip = _git("rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    if tip and tree == _git("rev-parse", f"refs/heads/{branch}^{{tree}}", check=False):
        print(f"无变化（{branch} 已是最新树 {tree[:12]}），跳过")
        return

    msg = args.message or (
        f"清单库快照：{len(files)} 清单文件 + README（{datetime.now().strftime('%Y-%m-%d %H:%M')}）"
    )
    commit_args = ["commit-tree", tree]
    if tip:
        commit_args += ["-p", tip]
    sha = _git(*commit_args, index=idx, input_text=msg)
    _git("update-ref", f"refs/heads/{branch}", sha)

    rel = "根提交（孤儿分支，无共同历史）" if not tip else f"续史提交（父 {tip[:12]}）"
    print(f"✓ 已发布 → {branch} @ {sha[:12]}（{rel}，{len(files)} 清单文件 + README）")
    if args.push:
        _git("push", "-u", "origin", branch)
        print(f"✓ 已推送 origin/{branch}")


if __name__ == "__main__":
    main()
