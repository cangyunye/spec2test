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


# 内置通用领域清单：devflow checklist init 默认携带（--no-general 跳过）。
# 与 payment 示例的定位不同：示例演示「怎么写」，内置清单是开箱可路由的通用测试检查点
# （HTTP 接口 / 前端鉴权·布局·易用 / SQL / Shell）。已存在的业务一律跳过，绝不覆盖用户修改。
BUILTIN_GENERAL: list[dict[str, str]] = [
    {
        "rel_dir": "api",
        "scenario": """---
name: HTTP 接口
description: HTTP/HTTPS 接口设计与测试：方法语义、状态码、参数校验、幂等、错误契约、接口安全。需求涉及接口/API/REST/请求响应时路由到此。
keywords: [接口, api, http, https, rest, 端点, 状态码, 请求, 响应]
references: []
---

## 使用场景

凡涉及后端 HTTP/REST 接口的新增或修改都适用：新增端点、改请求/响应结构、调状态码与错误处理、加鉴权或限流、接口幂等与版本兼容。
""",
        "checklist": """---
name: HTTP 接口
business: api
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 合法请求返回 2xx，响应体字段名/类型/结构与接口契约一致
- [P0] 各 HTTP 方法语义正确：GET 只读、POST 创建、PUT 全量替换、PATCH 局部更新、DELETE 删除
- [P1] Content-Type 与请求体格式一致（JSON/表单/文件上传），服务端解析正确

## 反向

- [P0] 非法参数返回 4xx 并定位到字段（字段名 + 原因），不泄露堆栈与内部信息
- [P0] 未登录/凭证过期访问受保护接口返回 401，权限不足返回 403，而非 500 或放行
- [P1] 不存在的资源返回 404，不支持的请求方法返回 405 并带 Allow 头
- [P2] 请求体超限（超大 JSON/文件）返回 413 且有明确提示

## 边界值

- [P1] 字符串字段在最大长度/最小长度/空串/纯空格下的行为符合契约
- [P1] 数值字段在 0、负数、超精度小数、超大整数下的行为符合契约
- [P2] 分页参数边界（page=0/size=0/超大 page/size 上限）返回合理结果

## 等价类

- [P1] 必填字段缺失被拒；选填字段缺失走默认值，两类行为均符合文档
- [P2] 枚举字段的每个合法取值均可用，非法取值被明确拒绝

## 状态流转

- [P0] 幂等性：同一请求重复提交（重复创建/重复回调）不产生重复副作用
- [P1] 并发更新同一资源有版本控制，冲突返回 409 或按规则合并
- [P2] 异步任务接口提交→查询→取消全程状态一致，重复取消幂等

## 场景法

- [P1] 创建→查询→更新→删除全链路数据一致，中间态查询可见性正确
- [P2] 接口版本共存：v1 行为不因 v2 新增字段而破坏旧调用方

## 安全

- [P0] SQL 注入/命令注入探测串被参数化处理，不改变查询语义
- [P0] 敏感数据（密码/令牌/证件号）不出现在 URL、响应与日志中
- [P1] 横向越权（改 ID 访问他人数据）与纵向越权（低权调高权接口）均被拦截
- [P1] HTTP 访问强制跳转 HTTPS，响应携带 HSTS
- [P2] CORS 仅允许可信来源，不存在通配符+凭证组合；限流超限返回 429

## 性能

- [P1] 目标负载下 P95 响应时间达标，无慢查询拖垮连接
- [P2] 万级数据列表分页响应可接受，深度分页有保护策略
""",
    },
    {
        "rel_dir": "frontend",
        "scenario": """---
name: 前端界面
description: 前端页面与组件的通用检查：设计还原、响应式兼容、交互反馈与防错。涉及界面/页面/组件/表单/弹窗时路由到此，再按子业务细分。
keywords: [前端, 界面, 页面, ui, 组件, 表单, 弹窗]
references:
  - path: auth
    desc: 安全鉴权（登录/会话/权限）
  - path: layout
    desc: UI 布局（设计还原/响应式/兼容）
  - path: usability
    desc: 用户易用（反馈/防错/引导）
---

## 使用场景

凡是前端页面/组件的改动都适用；确认后按子业务加载：auth（登录/会话/权限）、layout（布局/响应式/兼容）、usability（易用性/防错）。
""",
        "checklist": "",
    },
    {
        "rel_dir": "frontend/auth",
        "scenario": """---
name: 安全鉴权
description: 登录注册、会话管理、权限控制的界面检查。涉及登录/鉴权/权限/会话/token 时路由到此。
keywords: [登录, 登出, 鉴权, 认证, 授权, 权限, 会话, token, 密码, 注册]
references: []
---

## 使用场景

凡涉及登录/注册/登出、会话与 Token 有效期、角色权限、受保护页面访问控制的改动适用。
""",
        "checklist": """---
name: 安全鉴权
business: frontend/auth
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 正确凭证登录成功并建立会话；登出后所有凭证失效，受保护页面不可再入
- [P1] 记住登录在有效期内免登，且可主动退出登录态
- [P2] 第三方（OAuth）登录授权回跳后正常建立会话并绑定账号

## 反向

- [P0] 登录失败提示统一模糊（不区分用户不存在与密码错误），不泄露账号是否已注册
- [P0] 连续失败达到阈值触发锁定/验证码/限速，防暴力破解
- [P1] 禁用/过期账号登录被拒并给出明确提示
- [P2] 弱密码注册被拒并展示强度规则

## 边界值

- [P1] 凭证有效期边界（临期与刚过期）行为正确
- [P1] 失败次数阈值边界（第 N-1/N/N+1 次）触发行为符合策略

## 等价类

- [P1] 角色矩阵逐角色验证：可见菜单、可操作按钮、接口权限三者一致
- [P2] 多种凭证方式（密码/短信/SSO）各自的登录登出流程完整

## 状态流转

- [P0] 会话过期后操作被拦截并引导重登，重登后回到原操作位置
- [P1] 改密/踢出会话后旧 Token 全部失效，多标签页登录状态同步
- [P2] 权限调整后无需重登即生效（刷新可见）

## 场景法

- [P1] 未登录访问深链，登录成功后回跳原地址且参数不丢
- [P2] 敏感操作（改密/换绑手机号）有二次验证

## 安全

- [P0] 密码仅经 HTTPS 传输，请求体与日志不出现明文
- [P0] 会话凭证 HttpOnly + Secure，前端 JS 不可读取
- [P0] 登录/登出/改密接口防 CSRF（Token 或 SameSite）
- [P1] 密码存储为强哈希（bcrypt/argon2），不可逆

## 性能

- [P2] 登录接口响应时间可接受，登录峰值不误伤正常用户
""",
    },
    {
        "rel_dir": "frontend/layout",
        "scenario": """---
name: UI 布局
description: 界面布局与视觉还原：分辨率适配、响应式断点、浏览器兼容、空态错误态。涉及布局/样式/响应式/兼容时路由到此。
keywords: [布局, 样式, 还原, 响应式, 分辨率, 兼容, 主题, 错位]
references: []
---

## 使用场景

凡涉及页面布局、样式还原、响应式适配、浏览器兼容、主题配色的改动适用。
""",
        "checklist": """---
name: UI 布局
business: frontend/layout
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 页面还原符合设计稿：文案、间距、对齐、层级一致
- [P0] 主流程页面在目标分辨率（桌面 1920/1366、移动 375）下无错位、无意外横向滚动

## 反向

- [P0] 接口报错/数据为空不白屏，有空态与错误态占位
- [P1] 图片加载失败有兜底占位；弹窗/抽屉叠层 z-index 正确、遮罩不穿透

## 边界值

- [P1] 列表数据 0 条/1 条/满屏/超长条数下渲染与滚动正常
- [P1] 超长标题/数字/无空格英文正确截断或换行，不撑破容器
- [P2] 极窄视口（320px）与超宽屏布局可接受

## 等价类

- [P1] 分辨率档位逐档走查（大屏/笔记本/平板/手机）布局均正常
- [P2] 浏览器缩放 80%–150% 不破版

## 状态流转

- [P1] 屏幕旋转/窗口拖拽跨断点后布局正确重排，已填表单内容不丢
- [P2] 弹窗打开→关闭→再打开状态正确，滚动位置合理

## 场景法

- [P1] 目标浏览器（Chrome/Edge/Safari/Firefox）渲染一致
- [P1] 明暗主题切换后所有页面配色与对比度正常

## 性能

- [P2] 长列表滚动与动画不掉帧，无明显渲染卡顿
""",
    },
    {
        "rel_dir": "frontend/usability",
        "scenario": """---
name: 用户易用
description: 交互易用性与防错：操作反馈、危险操作确认、表单纠错、引导与可访问性。涉及体验/交互/提示/引导时路由到此。
keywords: [易用, 交互, 体验, 反馈, 提示, 文案, 引导, 可用性, 无障碍]
references: []
---

## 使用场景

凡涉及交互流程、操作反馈、提示文案、新手引导、可访问性的改动适用。
""",
        "checklist": """---
name: 用户易用
business: frontend/usability
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 核心任务路径短且主操作按钮显眼可发现，操作成功/失败有即时反馈
- [P0] 提交/删除等危险操作有二次确认，并说明影响范围
- [P1] 加载中有 loading 指示，提交按钮防重复点击（禁用/防抖）

## 反向

- [P1] 误操作可撤销（回收站/软删除）或明确警告不可恢复
- [P1] 表单校验错误定位到具体字段并给出修改建议，而非只弹提示
- [P2] 意外刷新/返回不丢已填内容（草稿暂存）

## 边界值

- [P2] 首次使用（无任何数据）有引导空态而非白板
- [P2] 权限受限用户的界面隐藏无权操作，而非点击后报错

## 等价类

- [P2] 全站提示文案、标点、日期与数字格式风格统一

## 状态流转

- [P1] 多步向导可上一步、中断后续做，已填内容保留
- [P2] 操作完成后焦点/视图落到合理位置（新项可见）

## 场景法

- [P1] 新用户不看文档能独立完成一次核心任务（可用性走查）
- [P2] 键盘可完成核心操作，Tab 焦点顺序合理

## 性能

- [P2] 常规操作即时响应（1 秒内有反馈），慢操作有进度提示
""",
    },
    {
        "rel_dir": "sql",
        "scenario": """---
name: 数据库 SQL
description: 数据库设计与 SQL 编写：表结构、约束、迁移脚本、索引与查询性能、数据安全。涉及建表/SQL/迁移/索引时路由到此。
keywords: [数据库, sql, 表结构, ddl, 迁移, 索引, 查询, 建表, 字段]
references: []
---

## 使用场景

凡涉及建表改表、迁移脚本、复杂查询、索引设计、数据订正脚本的改动适用。
""",
        "checklist": """---
name: 数据库 SQL
business: sql
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] DDL 与设计一致：表/字段/类型/默认值/注释齐全，命名风格统一
- [P0] 主键与业务唯一键约束正确，重复数据被拒绝
- [P1] 关联字段类型/长度/字符集与被引用表一致

## 反向

- [P0] UPDATE/DELETE 必带 WHERE 并先验证影响范围，不允许无条件全表变更
- [P0] 关键查询 EXPLAIN 无全表扫描，无隐式类型转换导致索引失效
- [P1] NOT NULL 字段有默认值或迁移中回填，不产生脏数据

## 边界值

- [P1] 金额用 DECIMAL 定点类型；手机号/编码等字段长度预留充分不留截断隐患
- [P1] 时间字段时区策略统一（UTC 或本地），跨时区读写不偏移

## 等价类

- [P1] 类型选型恰当：不用 FLOAT 存金额、不用字符串存时间
- [P2] 枚举/状态字段取值在 COMMENT 中逐一说明

## 状态流转

- [P0] 迁移脚本幂等可重跑（IF NOT EXISTS/先判断），失败可回滚不留半成品结构
- [P0] 改列名/改类型有兼容迁移步骤，迁移前后数据不丢失
- [P1] 大表变更评估锁影响，采用在线 DDL 或分批执行

## 场景法

- [P1] 迁移在预发演练并核对行数/抽样校验和后再上生产
- [P2] 结构变更与代码发布顺序兼容（先加后删、双写过渡）

## 安全

- [P0] SQL 全部参数化，应用账号最小权限（无 DDL/超管权限）
- [P2] 敏感列（手机号/证件号）加密或脱敏存储

## 性能

- [P1] 无 SELECT *，列表查询带 LIMIT；无 N+1 查询，批量写入用批量语句
- [P2] 索引适度：读多表覆盖常用查询，写多表不过度建索引
""",
    },
    {
        "rel_dir": "shell",
        "scenario": """---
name: Shell 脚本
description: Shell/Bash 脚本设计通用清单：错误处理、幂等、退出码、参数与路径、脚本安全。涉及脚本/bash/自动化/CI 命令时路由到此。
keywords: [脚本, shell, bash, 命令行, 部署, 自动化, ci]
references: []
---

## 使用场景

凡涉及编写或修改 Shell/Bash 脚本（部署、初始化、CI 钩子、批处理）的改动适用。
""",
        "checklist": """---
name: Shell 脚本
business: shell
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 脚本头 set -euo pipefail（或等效错误检查），出错即停不静默继续
- [P0] shebang 明确（#!/usr/bin/env bash），变量引用一律加引号，含空格路径不出错
- [P1] 关键步骤输出带时间戳的日志，失败信息可定位到步骤

## 反向

- [P0] 依赖命令缺失时给出明确提示（command -v 检查），不半途莫名报错
- [P1] 参数缺失/非法时打印 usage 并以非零码退出

## 边界值

- [P1] 幂等：重复执行不产生重复副作用（已存在则跳过或原样成功）
- [P1] 临时文件用 mktemp 创建并 trap 清理，异常中断不残留
- [P2] 特殊文件名（空格/换行/中划线开头）处理正确

## 等价类

- [P1] 目标环境等价类已验证：GNU/Linux 与 macOS（BSD 工具链）差异已处理或声明仅限其一

## 状态流转

- [P0] 退出码语义明确（0 成功/非 0 分类失败），上游 CI 可据此判断
- [P1] 可安全中断（Ctrl+C 后不损坏目标状态），重跑可恢复

## 场景法

- [P1] 长任务有进度输出，支持 --dry-run 干跑预演
- [P2] 可从任意工作目录执行（路径基于脚本位置解析）

## 安全

- [P0] 不硬编码密码/Token，敏感信息走环境变量或密钥管理
- [P0] 变量拼入命令前已校验，不使用未加引号的 eval/展开
- [P1] 远程脚本下载执行前校验 checksum/签名

## 性能

- [P2] 大文件/大目录处理不整读进内存，必要时分批流式处理
""",
    },
    {
        "rel_dir": "skill",
        "scenario": """---
name: Skill 设计
description: Agent 技能（SKILL.md）的设计与评审：触发描述、单一职责、指令可执行性、上下文经济与组合。涉及 skill/技能/能力封装时路由到此。
keywords: [skill, 技能, 能力包, 提示词模板, skill设计]
references: []
---

## 使用场景

凡涉及编写或修改 Agent 技能（SKILL.md 及其辅助文件）、划分 skill 边界、评审触发准确性的改动适用。
""",
        "checklist": """---
name: Skill 设计
business: skill
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] frontmatter 的 name/description 讲清「做什么 + 何时用」，目标场景的需求能稳定触发
- [P0] 单一职责：一个 skill 只解决一类任务，与其它 skill 边界清晰不重叠
- [P1] 指令可执行：步骤具体、每步有明确产出（输出格式/落盘路径），不含含糊动作
- [P1] 渐进披露：主体精炼、细节放辅助文件按需加载，不把长文档全塞进常驻上下文

## 反向

- [P0] 触发关键词不过宽，无关需求不会被误触发
- [P1] 无循环依赖与重复职责：同一任务两个 skill 都适用时，描述足以分辨

## 边界值

- [P1] 必输入缺失时行为有定义（追问清单或默认值），不自由发挥
- [P2] 超长输入（大文件/长文本）有分段或截断策略

## 等价类

- [P1] 声明的输入形态（文本/文件路径/URL）逐类实测可用

## 状态流转

- [P1] 中断后重跑幂等，不产生重复副作用或重复产出

## 场景法

- [P1] 端到端走查：真实任务从触发到产出全程顺畅，无需人工中途补指令
- [P2] 与其它 skill 组合（前者输出作后者输入）衔接顺畅

## 安全

- [P1] 不硬编码密钥/内网地址；外部输入拼命令前经校验
- [P2] 示例与默认值不含真实客户数据

## 性能

- [P2] 常驻描述与常载内容的上下文占用在预算内
""",
    },
    {
        "rel_dir": "agent",
        "scenario": """---
name: Agent 设计
description: LLM Agent/智能体应用设计：提示词契约、工具选择、幻觉校验、状态续跑、评测回归与安全护栏。涉及 agent/智能体/工具调用时路由到此。
keywords: [agent, 智能体, 助手, 工具调用, llm, 提示词, 多轮对话]
references: []
---

## 使用场景

凡涉及 Agent 的系统提示词、工具集、编排流程、评测与护栏设计的改动适用。
""",
        "checklist": """---
name: Agent 设计
business: agent
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 系统提示词定义角色/能力边界/输出契约，同样输入行为可复现
- [P0] 工具集最小化且描述精确：每个工具写清「何时用/何时不用」，模型能选对
- [P1] 输出有结构化契约（schema/格式约定），下游可解析、失败可识别
- [P1] 工具报错/解析失败有重试与降级路径，不死循环不静默吞错

## 反向

- [P0] 关键产出（文件路径/接口名/数据值）有落地校验，幻觉内容被拦截而非直接执行
- [P1] 工具调用有步数/预算上限，不无限循环
- [P1] 高危操作（删除/外发/支付）有确认门或权限白名单

## 边界值

- [P1] 长对话有压缩/截断策略，关键约束不因截断丢失
- [P1] 工具超时与部分失败的行为有定义

## 等价类

- [P1] 短指令/长文档/多任务混合等输入形态都能走对处理路径

## 状态流转

- [P0] 多轮任务状态可续跑：中断恢复不从头重来，状态存取一致
- [P1] 需要人工确认的决策点不静默自动执行

## 场景法

- [P1] 有代表性任务的金标准评测集，改提示词/换模型后跑回归对比
- [P2] 多 agent 协作时职责与交接协议清晰，无重复劳动

## 安全

- [P0] 提示词注入防护：外部内容（网页/文件/用户输入）不会被当作指令执行
- [P1] 敏感数据不进日志、不发送给无必需的数据处理方
- [P2] token 成本有预算与告警

## 性能

- [P2] 延迟与成本在目标内（缓存命中、模型分级调度）
""",
    },
    {
        "rel_dir": "ci",
        "scenario": """---
name: 自动化流水线
description: CI/CD 与自动化工作流设计：触发策略、幂等重跑、密钥管理、缓存与矩阵、部署回滚。涉及流水线/CI/GitHub Actions/自动发布时路由到此。
keywords: [ci, cd, 流水线, pipeline, actions, 工作流, 自动化, 发布]
references: []
---

## 使用场景

凡涉及 CI/CD 工作流、自动构建/测试/发布流水线、定时任务的编排设计适用（单脚本质量见 Shell 脚本清单）。
""",
        "checklist": """---
name: 自动化流水线
business: ci
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 触发条件明确（分支/标签/路径过滤），该跑的必跑、不该跑的不空跑
- [P0] 关键步骤失败即停（fail-fast），失败可定位到阶段与原因
- [P1] 构建产物与报告有版本化留存，可下载追查历史

## 反向

- [P0] 密钥全部经平台 secret 注入，不出现在配置文件、命令行与日志中
- [P1] 第三方 action/容器镜像锁定版本或摘要，不使用 latest 漂移

## 边界值

- [P1] 并发/重复触发有去重或排队策略，不互相踩踏产物
- [P1] 无匹配变更时不空跑全量流水线

## 等价类

- [P1] 声明支持的矩阵（多 OS/多运行时版本）真实覆盖并测试

## 状态流转

- [P0] 流水线幂等可重跑：重跑不产生重复发布、重复通知
- [P1] 部署有回滚手段且回滚路径演练过

## 场景法

- [P1] 任何步骤都能在本地用相同命令复现，不只活在 CI 里
- [P2] 失败通知送达责任人并附定位信息（日志/产物链接）

## 安全

- [P0] 凭证最小权限（scope 收敛）；PR 标题/评论等外部输入不拼进 shell 执行
- [P1] 发布凭证不落入缓存与产物附件

## 性能

- [P2] 依赖与构建缓存生效，流水线时长在团队可接受范围
""",
    },
    {
        "rel_dir": "unittest",
        "scenario": """---
name: 单元测试
description: 单元测试设计：行为断言、边界与等价类覆盖、mock 纪律、确定性（反 flaky）与速度。涉及单测/pytest/测试覆盖时路由到此。
keywords: [单元测试, 单测, pytest, unittest, mock, 测试覆盖, 测试设计]
references: []
---

## 使用场景

凡涉及编写或评审单元测试、补覆盖、治理 flaky 的改动适用（端到端用例设计见各业务清单）。
""",
        "checklist": """---
name: 单元测试
business: unittest
updated: {today}
sources: [builtin:general]
---

## 正向

- [P0] 一个测试只验证一个行为，命名自述「被测行为_期望结果」
- [P0] 断言验证行为结果而非实现细节，失败信息可定位问题
- [P1] AAA 结构清晰（准备/执行/断言），用例间无共享可变状态
- [P1] 分支与异常路径均有对应用例，不只测 happy path

## 反向

- [P0] 异常路径断言异常类型与语义，而非仅不抛错
- [P1] 非法输入（None/空/超界/错误类型）有对应用例且行为符合契约

## 边界值

- [P1] 空集合/单元素/满容量/临界数值等边界全覆盖
- [P2] 浮点用近似断言；时间与随机通过注入受控，不用真实时钟

## 等价类

- [P1] 输入域按等价类划分，每类至少一个代表用例，无冗余重复

## 状态流转

- [P1] 状态机逐迁移覆盖，非法迁移被拒同样有用例
- [P2] 异步/并发用确定性手段测试（受控调度/事件驱动），不依赖 sleep

## 场景法

- [P1] mock/stub 只用于边界依赖（外部服务/时钟/IO），不 mock 被测对象内部
- [P1] 测试不因纯重构而失败：无耦合实现细节的脆弱断言

## 安全

- [P2] 测试数据不含真实用户敏感信息

## 性能

- [P2] 套件秒级完成，慢测试打标隔离，不拖慢日常反馈
""",
    },
]


def init_library(
    root: Path | None = None, *, with_example: bool = True, with_general: bool = True
) -> list[Path]:
    """初始化库：README + _template 模板；with_example 附 payment 示例（含退款子业务）；
    with_general 附内置通用领域清单（api / frontend 三子业务 / sql / shell）。

    README / _template / 内置通用业务已存在时跳过，不覆盖用户修改；
    payment 示例为演示内容，重跑会重写。
    """
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

    if with_general:
        for item in BUILTIN_GENERAL:
            biz_dir = root / item["rel_dir"]
            if (biz_dir / "scenario.md").exists():
                continue  # 业务已存在（用户可能已修改）：跳过，不覆盖
            if item["checklist"].strip():
                write_checklist(
                    root, item["rel_dir"], _fmt(item["scenario"]), _fmt(item["checklist"])
                )
            else:  # 仅路由标签、无 checklist 的业务壳（如 frontend）
                biz_dir.mkdir(parents=True, exist_ok=True)
                (biz_dir / "scenario.md").write_text(_fmt(item["scenario"]), encoding="utf-8")
            written.append(biz_dir)
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
