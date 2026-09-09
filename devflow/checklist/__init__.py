"""业务 Checklist 库（.checklist）。

本地文件库 + skill 式渐进披露路由：
  - 路由只读各 scenario.md 的 frontmatter 标签（name/description/keywords），不碰正文；
  - 用户确认匹配后，才加载对应目录下的 checklist.md 注入测试设计 prompt；
  - 用例评审后可「沉淀」回库（distill），形成 越用越厚 的闭环。

文件规范：
  <root>/<business>/scenario.md    必须；frontmatter 路由标签 + 使用场景 + references
  <root>/<business>/checklist.md   可选；确认匹配后真正加载的检查清单
  子业务目录平铺在业务目录下（层级不限，以目录扫描为准）。

库根解析优先级（resolve_root）：
  DEVFLOW_CHECKLIST_ROOT 环境变量 > <project_root>/.checklist（存在时）> data/checklist（全局）
"""
from .library import (
    CHECKLIST_DIRNAME,
    CHECKLIST_FILE,
    SCENARIO_FILE,
    checklist_tree,
    load_checklists,
    load_scenario,
    resolve_root,
    scan_business_types,
    write_checklist,
)
from .models import (
    DistillOutput,
    DistillSection,
    RouteCandidate,
    RouteMatch,
    ScenarioDoc,
)

__all__ = [
    "CHECKLIST_DIRNAME",
    "CHECKLIST_FILE",
    "SCENARIO_FILE",
    "DistillOutput",
    "DistillSection",
    "RouteCandidate",
    "RouteMatch",
    "ScenarioDoc",
    "checklist_tree",
    "load_checklists",
    "load_scenario",
    "resolve_root",
    "scan_business_types",
    "write_checklist",
]
