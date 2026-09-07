"""本地测试执行器（执行闭环第二步：对落盘后的目标项目跑真实 pytest）。

职责：
  1. 在 project_root 里起子进程跑 `python -m pytest --junitxml=...`（不依赖目标项目装 devflow）
  2. 解析 junit-xml 回填结构化结果（passed/failed/errors/skipped/时长/失败明细）
  3. 超时杀进程；pytest 缺失 / 用法错误 → executed=False 并带原因（节点据此走 skip 而不是误判失败）

为什么不解析 stdout：junit-xml 是机器可读契约，失败用例的 message/system-out 稳定可提取；
stdout 只截尾留作 logs 供人工排查。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# 日志尾部保留字符数（防大项目 stdout 撑爆 state / checkpoint）
_LOG_TAIL_CHARS = 4000
# 单条失败信息保留字符数
_FAILURE_MSG_CHARS = 800


async def run_pytest(
    project_root: str | Path,
    *,
    paths: list[str] | None = None,
    timeout_sec: int = 300,
    extra_args: list[str] | None = None,
    python_exe: str | None = None,
) -> dict[str, Any]:
    """在 project_root 执行 pytest，返回统一结构：

    {executed, passed, failed, errors, skipped, total, duration_sec,
     logs, failures: [{id, message}], error}

    executed=False 时 error 说明原因（pytest 缺失 / 超时 / 无法运行）。
    """
    root = Path(project_root)
    report: dict[str, Any] = {
        "executed": False,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "total": 0,
        "duration_sec": 0.0,
        "logs": "",
        "failures": [],
        "error": None,
    }
    if not root.is_dir():
        report["error"] = f"project_root 不存在: {root}"
        return report

    with tempfile.TemporaryDirectory(prefix="devflow-pytest-") as td:
        junit = Path(td) / "junit.xml"
        cmd = [
            python_exe or sys.executable,
            "-m", "pytest",
            "-p", "no:cacheprovider",
            "-q",
            f"--junitxml={junit}",
        ]
        if paths:
            cmd.extend(paths)
        if extra_args:
            cmd.extend(extra_args)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            report["error"] = f"无法启动 pytest 子进程: {e}"
            return report

        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            report["error"] = f"pytest 执行超时（>{timeout_sec}s），已终止"
            report["logs"] = f"[timeout >{timeout_sec}s]"
            return report

        logs = (out or b"").decode("utf-8", errors="replace")
        report["logs"] = logs[-_LOG_TAIL_CHARS:]

        # pytest 退出码：0 全过 / 1 有失败 / 5 没收集到用例；4=用法错误（如 pytest 未装）
        rc = proc.returncode
        if rc == 4:
            report["error"] = "pytest 不可用（用法错误，通常为目标环境未安装 pytest）"
            return report

        if not junit.is_file():
            report["error"] = f"pytest 未产出 junit 报告（退出码 {rc}）"
            return report

        try:
            _merge_junit(report, junit)
        except ET.ParseError as e:
            report["error"] = f"junit-xml 解析失败: {e}"
            return report

        report["executed"] = True
        report["total"] = (
            report["passed"] + report["failed"] + report["errors"] + report["skipped"]
        )
        if rc == 5 and report["total"] == 0:
            report["logs"] = (report["logs"] + "\n[devflow] 未收集到任何测试用例").strip()

    return report


def _merge_junit(report: dict[str, Any], junit: Path) -> None:
    """把 junit-xml 的统计与失败明细并进 report。兼容 <testsuite> 与 <testsuites> 根。"""
    tree = ET.parse(junit)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        raise ET.ParseError("no <testsuite> element")

    for suite in suites:
        report["passed"] += int(float(suite.get("tests", 0))) - int(
            float(suite.get("failures", 0))
        ) - int(float(suite.get("errors", 0))) - int(float(suite.get("skipped", 0)))
        report["failed"] += int(float(suite.get("failures", 0)))
        report["errors"] += int(float(suite.get("errors", 0)))
        report["skipped"] += int(float(suite.get("skipped", 0)))
        try:
            report["duration_sec"] += round(float(suite.get("time", 0)), 3)
        except ValueError:
            pass

        for case in suite.iter("testcase"):
            node = case.find("failure") if case.find("failure") is not None else case.find("error")
            if node is None:
                continue
            cid = _case_id(case)
            msg = (node.get("message") or (node.text or "")).strip()
            report["failures"].append(
                {"id": cid, "message": msg[:_FAILURE_MSG_CHARS]}
            )
    report["duration_sec"] = round(report["duration_sec"], 3)


def _case_id(case: ET.Element) -> str:
    file_attr = case.get("file")
    name = case.get("name", "?")
    if file_attr:
        return f"{file_attr}::{name}"
    cls = case.get("classname")
    return f"{cls}::{name}" if cls else name
