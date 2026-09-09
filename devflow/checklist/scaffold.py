"""库脚手架：devflow checklist init —— 生成目录规范模板 + 可路由的示例业务。"""
from __future__ import annotations

from pathlib import Path

from .library import CHECKLIST_DIRNAME, resolve_root, write_checklist

README_TEMPLATE = """# .checklist 业务清单库

测试设计前由路由器按需加载的业务检查清单，规范如下：

```
<root>/
  <business>/            # 英文目录名 = 业务类型
    scenario.md          # 必须。frontmatter 路由标签 + 使用场景 + references
    checklist.md         # 确认匹配后加载的检查清单（分节 + [P0-P2] 条目）
    <sub-business>/      # 子业务，平铺在业务目录下（层级不限）
      scenario.md
      checklist.md
```

- 路由只读 scenario.md 的 frontmatter（name/description/keywords），确认后才加载 checklist.md；
- checklist.md 按 正向/反向/边界值/等价类/状态流转/场景法/安全/性能 分节，
  条目格式 `- [P0] 可验证的一句话检查点`；
- `_template/` 是模板，不参与路由。
"""

SCENARIO_TEMPLATE = """---
name: （业务中文名）
description: （一句话路由描述：什么需求应路由到此清单，含业务关键词）
keywords: []
references: []            # 可选：[{path: 子业务目录名, desc: 一句话说明}]
---

## 使用场景

（何时使用这份清单、典型需求样例、适用边界）
"""

CHECKLIST_TEMPLATE = """---
name: （业务中文名）
business: （业务/子业务路径，如 payment/refund）
updated: {today}
sources: []
---

## 正向

- [P0] （可验证的一句话检查点）

## 反向

- [P1] （应被拒绝/提示的非法输入与操作）

## 边界值

- [P1] （额度、数量、状态的临界条件）
"""

EXAMPLE_PAYMENT_SCENARIO = """---
name: 支付业务
description: 覆盖订单支付、退款、对账等资金流程。需求涉及支付/收银/退款/账单时路由到此。
keywords: [支付, payment, 收银, 退款, 账单, 结算]
references:
  - path: refund
    desc: 退款子业务（全额/部分退款、退款失败处理）
---

## 使用场景

需求涉及订单资金流：收银台支付、支付渠道对接、退款、账单对账等，
路由到本业务清单。示例：「订单支持微信支付」「退款审核流程重构」。
"""

EXAMPLE_PAYMENT_CHECKLIST = """---
name: 支付业务
business: payment
updated: {today}
sources: []
---

## 正向

- [P0] 正常下单并用可用余额完成支付，订单状态流转为已支付
- [P0] 支付成功回调幂等：重复回调不重复记账

## 反向

- [P0] 余额不足时支付被拒绝并给出明确提示，不产生脏订单
- [P1] 已取消订单发起支付被拒绝

## 边界值

- [P1] 恰好等于订单金额的余额可支付成功（分单位对齐）
- [P2] 超长优惠码/特殊字符在支付备注中的处理

## 状态流转

- [P0] 支付中超时未回调，订单按超时策略关闭且可重新支付
"""

EXAMPLE_REFUND_SCENARIO = """---
name: 退款子业务
description: 处理已支付订单的全额/部分退款、退款审核与失败重试。需求涉及退款时路由到此。
keywords: [退款, refund, 售后, 退单]
references: []
---

## 使用场景

需求触及退款流程：发起退款、退款审核、渠道退款回调、退款失败处理。
示例：「订单支持部分退款」「退款原路退回」。
"""

EXAMPLE_REFUND_CHECKLIST = """---
name: 退款子业务
business: payment/refund
updated: {today}
sources: []
---

## 正向

- [P0] 已支付订单全额退款成功，订单状态流转为已退款，资金原路退回

## 反向

- [P0] 退款金额超过实付金额（含已退部分）时被拒绝并提示
- [P1] 未支付订单发起退款被拒绝

## 边界值

- [P1] 最小退款金额（0.01 元）可发起并成功
- [P1] 多次部分退款累计恰好等于实付金额后再退被拒

## 状态流转

- [P0] 渠道退款失败进入重试队列，最终失败可人工介入，状态与资金一致
"""


def init_library(root: Path | None = None, *, with_example: bool = True) -> list[Path]:
    """初始化库：README + _template 模板；with_example 时附带 payment 示例（含退款子业务）。"""
    root = root or resolve_root()
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(README_TEMPLATE, encoding="utf-8")
        written.append(readme)

    template_dir = root / "_template"
    template_dir.mkdir(exist_ok=True)
    scenario_tpl = template_dir / "scenario.md"
    checklist_tpl = template_dir / "checklist.md"
    if not scenario_tpl.exists():
        scenario_tpl.write_text(SCENARIO_TEMPLATE, encoding="utf-8")
        written.append(scenario_tpl)
    if not checklist_tpl.exists():
        checklist_tpl.write_text(
            CHECKLIST_TEMPLATE.format(today=_today()), encoding="utf-8"
        )
        written.append(checklist_tpl)

    if with_example:
        write_checklist(
            root, "payment", _fmt(EXAMPLE_PAYMENT_SCENARIO), _fmt(EXAMPLE_PAYMENT_CHECKLIST)
        )
        write_checklist(
            root,
            "payment/refund",
            _fmt(EXAMPLE_REFUND_SCENARIO),
            _fmt(EXAMPLE_REFUND_CHECKLIST),
        )
        written.extend([root / "payment", root / "payment" / "refund"])
    return written


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _fmt(text: str) -> str:
    return text.format(today=_today())


def library_root_label(project_root: str = "") -> str:
    """展示用：当前生效的库根路径。"""
    return str(resolve_root(project_root))


__all__ = ["CHECKLIST_DIRNAME", "init_library", "library_root_label"]
