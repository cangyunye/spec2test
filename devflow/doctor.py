"""首次启动自检与引导（setup wizard）。

职责：
  1) 检测 .env 是否存在、LLM Key 是否仍是占位符
  2) 探测 mock 以外的服务后端（OpenCode / Pi / CodeGraph / Archify），复用 providers.check
  3) 多选询问缺失项是否安装：codegraph 走 GitHub Releases 自动下载；pi 走
     npm 全局安装；其余给自行安装指引。网络超时/失败 → 打印指引并跳过，绝不阻塞
  4) 根据已安装服务写好 .env（只填默认/占位值，不覆盖用户自定义配置）
  5) 输出收尾指引：用户自行填 LLM Key → check-providers / check-llm 测试 → 启动服务

用法（CLI）：
  devflow setup            # 交互式向导
  devflow setup --check    # 只打印检测报告与建议，不交互

Web 侧：uvicorn 导入 web.server 时调用 first_run_notice() 做非阻塞首启提示。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import httpx
from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .providers.check import (
    check_providers_all,
    probe_archify,
    probe_codegraph,
    probe_opencode,
    probe_pi,
)
from .providers.pi import PI_NPM_PACKAGE

# ── GitHub 官方仓库与网络参数 ──────────────────────────────────────
CODEGRAPH_REPO = "colbymchenry/codegraph"
OPENCODE_REPO = "sst/opencode"
NODE_REPO = "nodejs/node"
PI_REPO = "badlogic/pi-mono"

GITHUB_API_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
DOWNLOAD_DEADLINE_SEC = 300          # 单文件下载总时长上限，超时转「自行安装」
PLACEHOLDER_KEY_PREFIXES = ("sk-your", "sk-dummy")

# .env.example 的默认值：向导只允许改写「缺省 / 等于示例默认值」的键
EXAMPLE_DEFAULTS = {
    "CODE_SEARCH_PROVIDER": "mock",
    "CODE_GRAPH_RENDER_PROVIDER": "mermaid",
    "CODE_EDIT_PROVIDER": "mock",
    "TEST_GEN_PROVIDER": "mock",
}


# ═══════════════════════════════════════════════════════════════════
# GitHub：最新版本检测与下载（超时/失败一律降级为手动指引，不抛异常）
# ═══════════════════════════════════════════════════════════════════

def github_latest_release(
    repo: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: httpx.Timeout = GITHUB_API_TIMEOUT,
) -> dict[str, str] | None:
    """查 GitHub 最新 Release。

    返回 {"tag", "releases_url"}；超时 / 不可达 / 限流 / 字段缺失 一律返回 None，
    由调用方打印自行安装指引（超时处理方案的第一环）。
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return None
        tag = str(resp.json().get("tag_name") or "").strip()
        if not tag:
            return None
        return {"tag": tag, "releases_url": f"https://github.com/{repo}/releases"}
    except Exception:  # noqa: BLE001 - 网络自检需吞掉全部错误并降级
        return None


def _download_file(
    url: str,
    dest: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    deadline_sec: float = DOWNLOAD_DEADLINE_SEC,
) -> bool:
    """流式下载到 dest；总时长超过 deadline_sec 或任何网络错误 → 清理半成品返回 False。"""
    deadline = time.monotonic() + deadline_sec
    try:
        with httpx.Client(
            transport=transport, timeout=httpx.Timeout(30.0, connect=5.0), follow_redirects=True
        ) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"下载超过 {deadline_sec}s 上限")
                        f.write(chunk)
        return True
    except Exception:  # noqa: BLE001 - 下载失败统一走手动指引
        dest.unlink(missing_ok=True)
        return False


def codegraph_asset_name() -> str | None:
    """按当前平台映射 Release 资产名；不支持自动安装的平台返回 None。"""
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sysname == "darwin":
        return f"codegraph-darwin-{arch}.tar.gz"
    if sysname == "linux":
        return f"codegraph-linux-{arch}.tar.gz"
    return None  # windows 等平台走手动指引


def _writable_bin_dirs() -> list[Path]:
    """候选安装目录：用户级 ~/.local/bin 优先，其余取 PATH 中可写目录（去重）。"""
    candidates = [Path.home() / ".local" / "bin"]
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and Path(p).is_dir() and os.access(p, os.W_OK):
            candidates.append(Path(p))
    seen: set[Path] = set()
    return [d for d in candidates if not (d in seen or seen.add(d))]


def _extract_codegraph(tgz_path: Path, install_dir: Path) -> Path:
    """从 tar.gz 提取 codegraph 可执行文件并安装到 install_dir，返回目标路径。"""
    install_dir.mkdir(parents=True, exist_ok=True)
    dest = install_dir / "codegraph"
    with tarfile.open(tgz_path, "r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile() and Path(member.name).name == "codegraph":
                src = tf.extractfile(member)
                if src is None:
                    continue
                dest.write_bytes(src.read())
                dest.chmod(0o755)
                return dest
    raise FileNotFoundError("压缩包内未找到 codegraph 可执行文件")


def install_codegraph(
    console: Console,
    *,
    transport: httpx.BaseTransport | None = None,
    api_transport: httpx.BaseTransport | None = None,
    deadline_sec: float = DOWNLOAD_DEADLINE_SEC,
    install_dirs: list[Path] | None = None,
) -> str:
    """自动安装 codegraph 二进制（GitHub 最新 Release + SHA256 校验）。

    返回 "installed" 或 "manual"。任何一环失败（API 超时 / 下载超时 / 校验不符 /
    无可写目录）都打印自行安装指引后返回 "manual"，不抛异常、不中断向导。
    """
    releases_url = f"https://github.com/{CODEGRAPH_REPO}/releases"
    asset = codegraph_asset_name()
    if asset is None:
        console.print(f"[yellow]![/] 当前平台不支持自动安装，请自行下载：{releases_url}")
        return "manual"

    latest = github_latest_release(CODEGRAPH_REPO, transport=api_transport)
    if latest is None:
        console.print(
            f"[yellow]![/] GitHub 连接超时或不可达，无法检测最新版本。\n"
            f"    请自行安装 CodeGraph：{releases_url}\n"
            f"    已跳过该步骤，不影响启动（缺失自动回退 Mock）。"
        )
        return "manual"

    console.print(f"[cyan]ℹ[/] 最新版本 [bold]{latest['tag']}[/]，开始下载 {asset} …")
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / asset
        url = f"{releases_url}/download/{latest['tag']}/{asset}"
        if not _download_file(url, tgz, transport=transport, deadline_sec=deadline_sec):
            console.print(
                f"[yellow]![/] 下载超时或失败（上限 {deadline_sec}s）。\n"
                f"    请自行安装 CodeGraph：{releases_url}\n"
                f"    已跳过该步骤，不影响启动（缺失自动回退 Mock）。"
            )
            return "manual"

        sums = Path(td) / "SHA256SUMS"
        if _download_file(
            f"{releases_url}/download/{latest['tag']}/SHA256SUMS", sums, transport=transport
        ):
            expected = ""
            for line in sums.read_text(encoding="utf-8").splitlines():
                if line.rstrip().endswith(asset):
                    expected = line.split()[0].strip().lower()
                    break
            actual = hashlib.sha256(tgz.read_bytes()).hexdigest()
            if expected and actual != expected:
                console.print(
                    f"[red]×[/] SHA256 校验不符（预期 {expected[:12]}…，实际 {actual[:12]}…），"
                    f"已丢弃下载。请自行安装：{releases_url}"
                )
                return "manual"

        try:
            dirs = install_dirs or _writable_bin_dirs()
            if not dirs:
                console.print(
                    f"[yellow]![/] 未找到可写的安装目录，请自行安装：{releases_url}"
                )
                return "manual"
            dest = _extract_codegraph(tgz, dirs[0])
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]![/] 安装失败（{e}），请自行安装：{releases_url}")
            return "manual"

    console.print(f"[green]✓[/] CodeGraph {latest['tag']} 已安装 → [bold]{dest}[/]")
    if dirs[0] not in [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]:
        console.print(
            f"[yellow]![/] {dirs[0]} 不在 PATH 中，请将其加入 PATH 后新开终端生效"
        )
    try:
        ver = subprocess.run(
            [str(dest), "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if ver:
            console.print(f"[green]✓[/] 验证通过：codegraph {ver.splitlines()[0]}")
    except Exception:  # noqa: BLE001 - 版本验证尽力而为，不作为安装成败依据
        pass
    console.print("[cyan]ℹ[/] 如需启用语义检索：在目标项目根目录执行 [bold]codegraph init[/] 建索引")
    return "installed"


# ═══════════════════════════════════════════════════════════════════
# 检测报告：.env 状态 + 服务后端探测
# ═══════════════════════════════════════════════════════════════════

@dataclass
class InstallItem:
    """一个缺失服务的安装项。auto=True 走 GitHub 自动下载；False 只给指引。"""
    key: str            # codegraph / opencode / node
    name: str
    missing: str
    releases_url: str
    install_hint: str
    auto: bool
    repo: str


def placeholder_providers(env_vars: dict[str, str | None]) -> list[str]:
    """返回仍是占位 Key 的 LLM provider 名（口径与 web/server._mask 一致）。"""
    raw = (env_vars.get("LLM_PROVIDERS_JSON") or "").strip()
    names: list[str] = []
    if raw:
        try:
            for item in json.loads(raw):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("api_key") or "")
                if not key or key.startswith(PLACEHOLDER_KEY_PREFIXES):
                    names.append(str(item.get("name") or "?"))
        except json.JSONDecodeError:
            names.append("LLM_PROVIDERS_JSON(JSON解析失败)")
        return names
    key = env_vars.get("LLM_API_KEY") or ""
    if not key or key.startswith(PLACEHOLDER_KEY_PREFIXES):
        return ["primary(LLM_API_KEY)"]
    return []


def collect_report(project_root: str = ".", *, env_path: Path | None = None) -> dict[str, Any]:
    """汇总首启检测报告：.env 存在性、占位 Key 的 provider、三个后端探测结果。"""
    env_path = env_path or Path(".env")
    env_vars: dict[str, str | None] = dotenv_values(env_path) if env_path.exists() else {}
    return {
        "env_path": env_path,
        "env_exists": env_path.exists(),
        "env_vars": env_vars,
        "placeholder_providers": placeholder_providers(env_vars),
        "providers": check_providers_all(project_root),
    }


def install_pi(console: Console, *, timeout_sec: int = 300) -> str:
    """npm 自动安装 pi CLI；npm 缺失 / 安装失败 → 打印手动指引返回 "manual"。

    不像 codegraph 走 GitHub Release 下载：pi 是 npm 全局包，装完后仍需用户
    交互运行一次 `pi` 完成模型登录（/login），向导只负责把二进制装好。
    """
    npm = shutil.which("npm")
    if npm is None:
        console.print(
            "[yellow]![/] 未找到 npm，无法自动安装 pi。请先安装 Node.js ≥ 18，再执行：\n"
            f"    npm install -g {PI_NPM_PACKAGE}"
        )
        return "manual"
    console.print(f"[cyan]ℹ[/] 正在执行 npm install -g {PI_NPM_PACKAGE}（最长 {timeout_sec}s）…")
    try:
        proc = subprocess.run(
            [npm, "install", "-g", PI_NPM_PACKAGE],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        console.print(
            f"[yellow]![/] npm 安装超时（>{timeout_sec}s），已跳过。请自行执行：\n"
            f"    npm install -g {PI_NPM_PACKAGE}"
        )
        return "manual"
    except OSError as e:
        console.print(f"[yellow]![/] npm 启动失败（{e}）。请自行执行：npm install -g {PI_NPM_PACKAGE}")
        return "manual"
    if proc.returncode != 0:
        tail = [l for l in (proc.stderr or proc.stdout or "").strip().splitlines() if l.strip()]
        console.print(
            f"[yellow]![/] npm 安装失败：{tail[-1] if tail else '未知错误'}\n"
            f"    请自行执行：npm install -g {PI_NPM_PACKAGE}"
        )
        return "manual"
    pi_bin = shutil.which("pi")
    if pi_bin is None:
        console.print(
            "[yellow]![/] 安装完成但 pi 不在 PATH 中（npm 全局 bin 目录可能未入 PATH），"
            "请检查 `npm bin -g` / `npm config get prefix`"
        )
        return "manual"
    try:
        ver = subprocess.run(
            [pi_bin, "--version"], capture_output=True, text=True, timeout=15
        ).stdout.strip()
    except Exception:  # noqa: BLE001 - 版本验证尽力而为，不作为安装成败依据
        ver = ""
    console.print(
        f"[green]✓[/] pi 已安装 → [bold]{pi_bin}[/]"
        + (f"（{ver.splitlines()[0]}）" if ver else "")
    )
    console.print(
        "[cyan]ℹ[/] 首次使用请先在任意目录运行 [bold]pi[/] 完成模型登录（/login 或配置文件）；\n"
        "    也可在 .env 里用 PI_PROVIDER / PI_MODEL 指定流程使用的模型"
    )
    return "installed"


def missing_install_items(reports: list[dict[str, Any]]) -> list[InstallItem]:
    """从探测报告生成缺失服务的安装项（二进制缺失才需要装；codegraph 缺索引属于 init 提示）。"""
    by = {r["name"]: r for r in reports}
    items: list[InstallItem] = []
    cg = by.get("codegraph", {})
    if not cg.get("bin_ok", False):
        items.append(InstallItem(
            key="codegraph", name="CodeGraph（语义代码检索）",
            missing="codegraph 二进制未安装",
            releases_url=f"https://github.com/{CODEGRAPH_REPO}/releases",
            install_hint="官方脚本：curl -fsSL https://raw.githubusercontent.com/"
                         f"{CODEGRAPH_REPO}/main/install.sh | sh",
            auto=True, repo=CODEGRAPH_REPO,
        ))
    oc = by.get("opencode", {})
    if not oc.get("ok", False):
        items.append(InstallItem(
            key="opencode", name="OpenCode（代码检索/生成/测试后端）",
            missing="OpenCode Server 未响应（检查 OPENCODE_BASE_URL）",
            releases_url=f"https://github.com/{OPENCODE_REPO}/releases",
            install_hint="curl -fsSL https://opencode.ai/install.sh | bash"
                         "  # 或 brew install sst/tap/opencode；装后 opencode serve 启动",
            auto=False, repo=OPENCODE_REPO,
        ))
    pi = by.get("pi", {})
    if not pi.get("bin_ok", False):
        items.append(InstallItem(
            key="pi", name="Pi Coding Agent（代码生成/测试后端，npm 包）",
            missing="pi CLI 未安装",
            releases_url=f"https://github.com/{PI_REPO}",
            install_hint=f"npm install -g {PI_NPM_PACKAGE}"
                         "  # 装后运行 pi 完成模型登录（/login）",
            auto=True, repo=PI_REPO,
        ))
    ar = by.get("archify", {})
    if not ar.get("ok", False):
        items.append(InstallItem(
            key="node", name="Node.js ≥ 18（Archify 架构图渲染依赖）",
            missing="node / npx 未安装（自动回退 Mermaid 文本）",
            releases_url=f"https://github.com/{NODE_REPO}/releases",
            install_hint="brew install node  # 或从 GitHub Releases 下载安装包",
            auto=False, repo=NODE_REPO,
        ))
    return items


def print_report(console: Console, report: dict[str, Any]) -> None:
    """打印检测报告（.env 状态 + 后端可用性表）。"""
    env_path: Path = report["env_path"]
    if report["env_exists"]:
        ph = report["placeholder_providers"]
        if ph:
            console.print(
                f"[green]✓[/] .env 已存在（{env_path}）；"
                f"[yellow]LLM Key 未配置：{', '.join(ph)}（当前 Mock 兜底）[/]"
            )
        else:
            console.print(f"[green]✓[/] .env 已存在（{env_path}），LLM Key 已配置")
    else:
        console.print(
            f"[red]×[/] 未检测到 .env（{env_path}）—— 将以 Mock 模式运行；"
            f"向导可自动创建（cp .env.example .env）"
        )

    table = Table(title="外部服务后端探测（mock 以外）")
    table.add_column("服务")
    table.add_column("状态")
    table.add_column("详情")
    for r in report["providers"]:
        status = "[green]✓ 可用[/]" if r["ok"] else "[yellow]✗ 缺失[/]"
        table.add_row(r["name"], status, r.get("detail", ""))
    console.print(table)


# ═══════════════════════════════════════════════════════════════════
# .env 配置：按已安装服务写后端选择（不覆盖用户自定义值）
# ═══════════════════════════════════════════════════════════════════

def env_updates_from_probes(
    reports: list[dict[str, Any]], current: dict[str, str | None]
) -> list[dict[str, str]]:
    """计算建议写入 .env 的键值对：[(key, new, cur, reason)]。

    只提议「键缺失 / 值为空 / 等于 .env.example 默认值」的键；
    用户已自定义的值保留不动（kept 由 apply 阶段给出）。
    """
    by = {r["name"]: r for r in reports}
    opencode_ok = bool(by.get("opencode", {}).get("ok"))
    cg = by.get("codegraph", {})
    codegraph_ready = bool(cg.get("bin_ok") and cg.get("index_ok"))
    archify_ok = bool(by.get("archify", {}).get("ok"))
    pi_ok = bool(by.get("pi", {}).get("bin_ok"))

    def mutable(key: str) -> bool:
        val = (current.get(key) or "").strip()
        return val == "" or val == EXAMPLE_DEFAULTS.get(key, "")

    def add(updates: list[dict[str, str]], key: str, new: str, reason: str) -> None:
        if mutable(key):
            updates.append({
                "key": key, "new": new,
                "cur": current.get(key) or "(缺省)", "reason": reason,
            })

    updates: list[dict[str, str]] = []
    if opencode_ok:
        add(updates, "CODE_EDIT_PROVIDER", "opencode", "OpenCode Server 已可用")
        add(updates, "TEST_GEN_PROVIDER", "opencode", "OpenCode Server 已可用")
        add(updates, "CODE_SEARCH_PROVIDER", "opencode", "OpenCode Server 已可用")
    elif pi_ok:
        # pi 与 OpenCode 二选一：OpenCode 没配好才建议 pi（避免两套更新打架）。
        # 检索一格不建议 pi——无代码索引，agent 翻文件式检索又慢又不稳
        add(updates, "CODE_EDIT_PROVIDER", "pi", "pi CLI 就绪（OpenCode 未配置）")
        add(updates, "TEST_GEN_PROVIDER", "pi", "pi CLI 就绪（OpenCode 未配置）")
    if codegraph_ready:
        add(updates, "CODE_SEARCH_PROVIDER", "codegraph", "CodeGraph 二进制 + 索引就绪")
    if archify_ok:
        add(updates, "CODE_GRAPH_RENDER_PROVIDER", "archify", "node/npx 就绪，启用 HTML/SVG 渲染")
    return updates


def apply_env_updates(
    updates: list[dict[str, str]],
    env_path: Path,
    example_path: Path = Path(".env.example"),
) -> tuple[list[str], list[str]]:
    """把建议写入 .env：不存在则先从 .env.example 复制。

    只改写「缺省 / 等于示例默认值」的键，用户自定义值保留。
    返回 (applied, kept) 的人读描述行。
    """
    created = False
    if not env_path.exists():
        if example_path.exists():
            shutil.copyfile(example_path, env_path)
        else:
            env_path.write_text("", encoding="utf-8")
        created = True

    lines = env_path.read_text(encoding="utf-8").splitlines()
    current = dotenv_values(env_path)
    applied: list[str] = []
    kept: list[str] = []
    for u in updates:
        key, new = u["key"], u["new"]
        cur = (current.get(key) or "").strip()
        if cur == new:
            continue  # 已是目标值
        if cur and cur != EXAMPLE_DEFAULTS.get(key, ""):
            kept.append(f"{key}={cur}（保留已有配置，未改写）")
            continue
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
                lines[i] = f"{key}={new}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={new}")
        applied.append(f"{key}: {cur or '(缺省)'} → {new}")

    if applied or created:
        env_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    if created:
        applied.insert(0, f"已创建 {env_path}（从 {example_path} 复制）")
    return applied, kept


# ═══════════════════════════════════════════════════════════════════
# 交互向导
# ═══════════════════════════════════════════════════════════════════

def parse_multi_selection(raw: str, total: int) -> list[int]:
    '''多选输入解析："1,3" → [0, 2]；空 / 全非法 → []；忽略越界与非数字，去重。'''
    out: list[int] = []
    for tok in raw.replace("，", ",").split(","):
        tok = tok.strip()
        if tok.isdigit():
            i = int(tok) - 1
            if 0 <= i < total and i not in out:
                out.append(i)
    return out


def _reprobe(item: InstallItem, project_root: str) -> dict[str, Any]:
    """安装动作后重探单个服务。"""
    import asyncio

    if item.key == "codegraph":
        return probe_codegraph(project_root)
    if item.key == "opencode":
        return asyncio.run(probe_opencode())
    if item.key == "pi":
        return probe_pi()
    return probe_archify()


def _guide_install(console: Console, item: InstallItem, input_fn: Callable, project_root: str) -> None:
    """无自动安装能力的服务：检测最新版本 + 打印自行安装指引，等用户装完回车复检。"""
    latest = github_latest_release(item.repo)
    if latest:
        console.print(f"\n[bold]{item.name}[/]（GitHub 最新版本 {latest['tag']}）")
    else:
        console.print(
            f"\n[bold]{item.name}[/][yellow]（GitHub 不可达，无法检测最新版本，"
            f"请直接到发布页下载）[/]"
        )
    console.print(f"  发布页: {item.releases_url}")
    console.print(f"  安装:   {item.install_hint}")
    try:
        ans = input_fn("  自行安装完成后回车复检；输入 s 跳过 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if ans == "s":
        console.print(f"[yellow]![/] 已跳过 {item.key}（运行时自动回退 Mock，不影响启动）")
        return
    r = _reprobe(item, project_root)
    if r.get("ok"):
        console.print(f"[green]✓[/] {item.key} 复检通过：{r.get('detail', '')}")
    else:
        console.print(f"[yellow]![/] {item.key} 仍未就绪：{r.get('detail', '')}（可稍后再装）")


def _ask_and_install(console: Console, items: list[InstallItem], input_fn: Callable, project_root: str) -> None:
    """多选询问要安装哪些缺失服务，逐项执行（auto=自动下载，否则指引）。"""
    console.print("\n[bold]缺失的外部服务[/] [dim]（不装也能跑，缺失后端自动回退 Mock/Mermaid）[/]")
    for i, it in enumerate(items, 1):
        console.print(f"  {i}. {it.name} — [yellow]{it.missing}[/]")
    try:
        raw = input_fn("要安装哪些？输入编号（逗号分隔，如 1,2）；直接回车 = 跳过安装 > ").strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]已跳过安装步骤[/]")
        return
    chosen = parse_multi_selection(raw, len(items))
    if not chosen:
        console.print("[dim]已跳过安装步骤[/]")
        return
    for i in chosen:
        item = items[i]
        if item.key == "codegraph":
            # install_codegraph 内部已含超时 → 自行安装指引 → 跳过 的降级路径
            install_codegraph(console)
        elif item.key == "pi":
            # install_pi 内部已含 npm 缺失 / 超时 / 失败 → 手动指引 的降级路径
            install_pi(console)
        else:
            _guide_install(console, item, input_fn, project_root)


def _run_env_config(console: Console, report: dict[str, Any], input_fn: Callable) -> None:
    """根据已安装服务把 .env 配置好（先展示变更，回车应用 / s 跳过）。"""
    env_path: Path = report["env_path"]
    updates = env_updates_from_probes(report["providers"], report["env_vars"])
    if not updates and report["env_exists"]:
        console.print("[cyan]ℹ[/] .env 已与已安装服务匹配，无需变更")
        return

    console.print(f"\n[bold]根据已安装的服务，将对 [bold]{env_path}[/] 做以下配置[/]：")
    if not report["env_exists"]:
        console.print("  • 创建 .env（从 .env.example 复制）")
    for u in updates:
        console.print(f"  • {u['key']}: {u['cur']} → [green]{u['new']}[/]  [dim]({u['reason']})[/]")
    try:
        ans = input_fn("回车应用；输入 s 跳过 > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]![/] 已跳过 .env 配置")
        return
    if ans == "s":
        console.print("[yellow]![/] 已跳过 .env 配置")
        return
    applied, kept = apply_env_updates(updates, env_path)
    for line in applied:
        console.print(f"[green]✓[/] {line}")
    for line in kept:
        console.print(f"[yellow]—[/] {line}")


def print_final_guidance(console: Console, report: dict[str, Any]) -> None:
    """收尾指引：用户自行设置供应商 → 测试指令 → 启动指令。"""
    ph = report["placeholder_providers"]
    env_path: Path = report["env_path"]
    lines: list[str] = []
    if ph:
        lines += [
            f"[bold yellow]1)[/] 请自行设置 LLM 供应商：编辑 [bold]{env_path}[/]，"
            f"为 {', '.join(ph)} 填入真实 api_key",
            "    （不用的 provider 可直接删掉；单供应商也可填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）",
        ]
    else:
        lines.append("[green]1)[/] LLM Key 已配置 ✓")
    lines += [
        "",
        "[bold]2)[/] [bold]供应商与 LLM 测试指令[/]",
        "    python3 -m devflow.cli check-providers   # 后端可用性自检（不发真实调用）",
        "    python3 -m devflow.cli check-llm         # LLM 连通性测试（每家约 50 token）",
        "",
        "[bold]3)[/] [bold]启动服务[/]",
        "    python3 -m uvicorn web.server:app --port 8100",
        "    打开 http://127.0.0.1:8100（Mock/Provider 状态见页面右上角徽章）",
    ]
    console.print(Panel("\n".join(lines), title="最后一步", border_style="green"))


def run_setup(
    console: Console,
    *,
    project_root: str = ".",
    check_only: bool = False,
    input_fn: Callable | None = None,
) -> None:
    """setup 向导主流程：检测 → 多选安装 → 配置 .env → 收尾指引。"""
    input_fn = input_fn or input
    console.print(Panel.fit("[bold cyan]CaseCraft 首次启动自检与引导[/]", border_style="cyan"))
    report = collect_report(project_root)
    print_report(console, report)

    items = missing_install_items(report["providers"])
    if not check_only:
        if items:
            _ask_and_install(console, items, input_fn, project_root)
        else:
            console.print("[green]✓[/] 外部服务全部就绪，无需安装")
        _run_env_config(console, report, input_fn)
    else:
        if items:
            console.print("\n[bold]建议安装[/]（devflow setup 可引导安装）：")
            for it in items:
                console.print(f"  • {it.name} — {it.releases_url}")

    print_final_guidance(console, report)


# ═══════════════════════════════════════════════════════════════════
# Web 首启提示（非阻塞）
# ═══════════════════════════════════════════════════════════════════

def first_run_notice(*, env_path: Path | None = None) -> str | None:
    """无 .env 时返回启动提示文案；已配置返回 None。uvicorn 启动时打印一次。"""
    env_path = env_path or Path(".env")
    if env_path.exists():
        return None
    bar = "═" * 62
    return (
        f"\n{bar}\n"
        "⚠  未检测到 .env —— 服务将以 Mock 模式运行（LLM 输出为演示数据）\n"
        "   首次配置引导：python3 -m devflow.cli setup\n"
        "   手动配置：cp .env.example .env 后填入 LLM API Key\n"
        f"{bar}"
    )
