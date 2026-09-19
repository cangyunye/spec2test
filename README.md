# TC-CHECKLIST · 通用测试检查清单库

CaseCraft（spec2test）的业务检查清单库快照：与 main 零共同历史的孤儿分支，
只包含清单库本身、独立演进，永不合并主干。

## 接入 DevFlow / CaseCraft

```bash
git clone -b tc-checklist --depth 1 https://github.com/cangyunye/spec2test my-checklist
# 之后任选其一：
#   设 DEVFLOW_CHECKLIST_ROOT=<克隆目录>
#   或拷贝到目标项目 <project_root>/.checklist/
```

## 库内容一览

| 业务（rel_dir） | 名称 | 条目 | 子业务 |
|---|---|---|---|
| `agent` | agent | 18 | Agent 设计（agent）18条 |
| `api` | api | 24 | HTTP 接口（api）24条 |
| `ci` | ci | 15 | 自动化流水线（ci）15条 |
| `frontend` | frontend | 49 | 安全鉴权（frontend/auth）21条、UI 布局（frontend/layout）14条、前端界面（frontend）0条、用户易用（frontend/usability）14条 |
| `shell` | shell | 17 | Shell 脚本（shell）17条 |
| `skill` | skill | 15 | Skill 设计（skill）15条 |
| `sql` | sql | 19 | 数据库 SQL（sql）19条 |
| `unittest` | unittest | 15 | 单元测试（unittest）15条 |

## 结构与更新

- 每个业务目录 = `scenario.md`（路由标签）+ `checklist.md`（分节检查点，`- [P0] 可验证一句话`）；
- `_` 开头目录不参与路由；条目按「正向/反向/边界值/等价类/状态流转/场景法/安全/性能」八分节；
- 本分支由主仓 `scripts/publish_checklist.py` 从运行库发布（内容以运行库为准），
  主仓内改清单请同步运行库后重新发布；直接改本分支的提交也会被保留（下次发布会挂在其后）。

_2026-09-19 由 publish_checklist.py 生成_
