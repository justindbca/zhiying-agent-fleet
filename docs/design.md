# cmux-fleet 设计基线

> 用户确认日期：2026-10-10。本文固定设计约束，不是某次演示的临时配置。
> 历史实现基础：`0c43b9c`；Pi/profile 增量来自 `feat/pi-agents`，Redis v2 基础模块已单独进入本地 `main@5f973d5`。
> **开发验证通过不等于已复核、已合并或已生产部署。** 旧实现验证状态见 §10；当前本地集成范围与复核见 [2026-10-12 报告](../reports/full-local-merge-20261012.md)。
> **2026-10-11 新要求：Redis 阻塞通信 + control 详情显示。** 目标设计见 §11 与[完整 v2 方案](../../us-stock-option/reports/cmux-fleet-redis-control-design-v2-20261011.md)。§11 明确覆盖下文旧文件投递/机械结果转述要求；**v2 基础模块与 all-Pi daemon/bridge/control 已有集成候选，真实 Redis/SDK/TUI 的局部闭环已验证；完整验收仍未完成，见 §14**，不能把模块合并说成运行代码已升级。

## 1. 延续原设计，不另造框架

保留：**一个 supervisor + N 个 worker + 一个 daemon + 一套私有 Redis**。

- supervisor 负责判断、分派、查看结果，不代替 worker 执行任务。
- worker 执行被分派的工作；不同职责写在各自角色 prompt，不擅自改变 agent 身份。
- daemon 负责投递、状态、校验、超时、换代与历史记录；不依赖模型声称“成功”，不读屏幕判断回合结束。
- pi 是另一种 agent CLI 适配，不取代 daemon，不增加第二套派活或记忆/交接系统。
- 原 task/service 模式、命令行入口、请求/结果协议、worker 别名路由继续保留。

设计来源：原 cmux-fleet README/在役代码；业务侧原始设计 `us-stock-option/tasks/plans/tgog-d-order-fable-v1-20261010.md` §17/17.1；用户后续关于同页布局、代数标识、官方 Claude CLI、control/history 同栏和手工派活的明确要求。

**本轮不是重设计。不得借换 CLI 擅改上述分工，也不得把临时验收角色或参数变成默认设计。**

## 2. 唯一主页面与固定布局

```text
Fleet agents（一个工作区）
┌──────────────────────────────┬──────────────────────────────┐
│ supervisor-1.N               │ [worker-1.N] [worker-2.N] ... │
│ supervisor 实时交互窗口       │ worker 标签页，共用右侧一栏   │
├──────────────────────────────┤                              │
│ [history] [control]          │ 当前选中 worker 的实时窗口    │
│ 默认显示 history            │                              │
└──────────────────────────────┴──────────────────────────────┘
```

| 部分 | 约束 |
|---|---|
| supervisor | 独占左上；不与 control 混成同一栏 |
| workers | 右侧同一 pane 的标签页；不是每个 worker 再开一个工作区 |
| history | 左下内置浏览器；默认显示；可按 agent/代数查历史屏幕与交接单、全文搜索 |
| control | 与 history **同一 pane 的后台标签**；仅在排错时点开，不默认占画面 |
| daemon | control 终端里的调度程序，不是另一个 AI，也不是第二个 supervisor |

启动时可暂建 control 工作区；主布局就绪后将其终端移入 history 栏。只关闭已空的旧工作区，不强制关闭其他窗口。移动失败必须保留控制台，不能为隐藏界面而杀 daemon。

移动 control 只改变布局：不得重启 Redis/daemon/agents、改变请求状态或打断任务。

## 3. 身份与代数：所有显示使用同一个 label

| 逻辑槽位 | 首代 generation=0 | 第一次交接 generation=1 | 后续 |
|---|---|---|---|
| `supervisor` | `supervisor-1` | `supervisor-1.1` | `supervisor-1.2`、… |
| `worker-1` | `worker-1` | `worker-1.1` | `worker-1.2`、… |
| `worker-2` | `worker-2` | `worker-2.1` | `worker-2.2`、… |

- 逻辑槽位稳定，交接只增加代数。旧代名在**同一次 run 内**继续路由到该槽位。
- Redis agent 状态里的 `label` 是显示标识来源；cmux 标签、pi `--name`、终端标题、结果、历史页必须一致。
- 不用工作目录名、模型名覆盖标识；不手工另造一套编号；不靠事后 rename 补救。
- 原 cmux 标签可能保留 `fx-` 装饰前缀；它不是新的逻辑槽位或代数。
- 默认使用 `worker-1..N`。原 `--worker-names` 功能保留给**明确指定角色槽位的调用方**；演示或通用会话不得擅自换成 `checker` 等名字。
- 角色 prompt、CLI 类型、模型与 agent 身份是不同概念。改变模型不意味着改名。
- 重新创建整个 fleet 是新 run，不假装继承旧 run 的代数；旧 run 文件保留，不冒称仍属于新历史页。

## 4. 交接与历史沿用原实现

1. daemon 在空闲投递边界读取用量。
2. 达到 `handoff_ratio` 后，请旧代填写三节交接草稿。
3. 把模型内容并入机器骨架；模型未写或失败时用骨架，不因缺交接文本阻塞换代。
4. 保存旧屏幕和交接单；在原 pane/tab 位置启动新一代。
5. 新一代只读交接并回 `READY`，然后才接收等待中的任务。
6. 退役会话关闭，历史页刷新，迟到回调按 generation 丢弃。

- 正常默认阈值 **0.6（60%）**。1%/5% 仅作显式换代测试，结束后恢复正常值。
- 新会话仅 READY 就已超阈值时，不反复重开；保留原 `rotate.impossible` 防循环机制。
- pi 自动压缩只作长回合保险，不替换 fleet 的跨会话交接和历史。
- 保留原关闭失败/投递失败处理；不能把“机器骨架可用”夸大成任何窗口/进程故障下都保证换代成功。

## 5. supervisor 手工派活必须形成闭环

用户可以在 **pi supervisor 的输入框**说：

```text
让 worker-1 回复 OK，请把它的回复转告我。
```

### service 模式

```text
用户在 supervisor 输入普通文字
  → pi input 适配
  → 原 daemon 队列（校验 supervisor 当前 session/generation）
  → Python 分配真实 ui-<uuid> 请求 ID
  → 原 request / inbox / supervisor 路由
  → worker 执行并返回 {"reply":"OK"}
  → 原 JSON 验收和 publish
  → 结果排回 supervisor，由它转告用户
```

- 不能只让模型口头说“已通知”，也不能让模型编造 `request_id`。
- 人在终端输入的文本不得因缺少 daemon 投递 ID 而静默消失。
- 回调关联检查仍保留；不是放行任意 idle 回调，而是先把人类输入登记成正式请求。
- supervisor 的结果转告属于已完成请求的汇报，不得再次派发同一请求。
- 原机器接口 `request` 不变：`id/payload/reply.required`，可带 `to/instruction/reply.allowed/timeout_s`。
- `accepted` 只表示入队；必须得到最终 result 才表示处理完成。
- wire format 仍为 JSON；“回复 OK”对应 `{"reply":"OK"}`，不是修改成无校验的字符串协议。

### task 模式

没有正在进行的任务时，普通文字经原 `submit()` 建任务；已有任务时，经原队列补充指令。继续使用原 dispatch、产物校验、COMPLETED/BLOCKED 和预算生命周期，不另建 task 引擎。

### 输入边界

- 当前自然语言入队适配针对 **pi supervisor**，不宣称其他 CLI 已实现相同入口。
- 初始启动提示、daemon 的 `.inbox` 指针、slash 命令不再次入队。
- worker 的手工输入仍是该 worker 自己的交互，不冒充 supervisor 派活，也不冒充某个已有请求的结果。
- 不支持直接转送图像附件，明确提示改用文件路径；不得静默丢掉图片后当作正常请求。
- 未 READY、旧 generation/thread、空文本等输入应明确拒绝；队列确认失败显示错误/未确认，不伪造成功。

## 6. CLI 与认证边界

| 角色 | 可选 CLI | 完成通知 |
|---|---|---|
| supervisor | 原 Codex / pi | Codex notify / pi agent_settled |
| worker | 原 Codex / 官方 Claude Code CLI / pi | notify / Claude Stop / agent_settled |

用户明确要求：**Claude 必须由 fleet 直接启动官方 `claude` CLI。**

- 不用 pi 代理 Claude 订阅接口；不把 Claude Code 的订阅/OAuth 凭证交给 pi；不伪装官方客户端。
- 本地 Flash Next 可经 pi 的 OpenAI-compatible provider 使用；不得因模型不可用而暗中换云模型。
- `--pi-model` 必填；CLI/模型/角色可配置，但没有理由擅自替换用户已选的执行链。
- 官方 CLI 也不等于零封号或零限流风险，仍须遵守服务条款。额度不足不授权换账号、挪凭证或绕限额。

隔离保持原意：Codex 使用原隔离 home；pi 每个 run 新建 home，只链接选定 pi 配置来源的 `auth.json/models.json`，禁发现个人扩展、MCP、skills 等，仅显式加载 fleet bridge 和角色指令。

**pi 没有 OS 沙盒**，`--allow-unsandboxed-pi` 仅是确认，不创建隔离。原 Claude worker 同样使用官方 CLI 的现有权限参数；不能宣传成 Codex workspace-write 沙盒。角色禁令不等于权限边界。

## 7. 回调、投递与故障处理

### pi 适配必须满足

- 只在 `agent_settled` 判定最终完成；`agent_end/turn_end` 后仍可能有工具、重试、压缩或续跑。
- generation 固定在该进程回调参数中；每次 daemon 投递对应唯一 inbox msg_id。
- **仅旧 v1 兼容路径**：只提取末尾固定信封中、属于当前 agent cwd 的 `.inbox/msg-*.md` ID，不能取正文引用的其他 agent 路径。**v2 以 §11 为准，删除文件信封依赖，身份直接来自 Redis delivery 元数据**；不要把旧回归当成新设计要求。
- 错误/中止的部分 JSON 不得通过验收；启动失败或非 READY 不得伪报就绪。
- 不放宽过期、重复、跨 agent 回调检查来“修复不回复”；应修正输入登记或关联源。
- context 用量取 pi API，会话文件用于活动/卡死检查；不拿 Codex rollout 格式解析 pi。

### 原故障语义保留

- service：暂停停止投递/换代，但请求继续排队且仍有超时；BLOCKED/故障交人处理。
- task：按原 completed/blocked/budget_exhausted/cancelled 生命周期结束。
- 保留超时、invalid、skipped、stalled 等结果，不把错误伪装成模型成功。
- 私有 Redis 新 run 使用项目 `.runtime/redis/<token>/redis.sock`（用户新要求见 §13）；旧 `/tmp/cmux-fleet-<token>.sock` 配置仅作兼容。不能回退 localhost:6379 或写生产 Redis。
- `max-busy` 原语义只统计 worker，supervisor 不算，也不控制启动瞬间；不能把它宣传成完整的模型并发闸。

## 8. 配置与运行边界

- 常规用法见 [README](../README.md)；本机/Studio/macmini 路径和在役版本以外部项目 `references/cmux-fleet.md` 为准，不在本设计写死机器路径。
- 每台机器当前实现只维护一套 fleet registry；本地验证不要与已有 fleet 抢窗口/登记。
- 本机验证只能处理测试或授权的普通开发工作，不把“本机跑通”理解为授权跨机器运行交易职责。
- 不重启/替换其他机器在役服务，不自动部署。文档修改不授权生产操作。
- 模型窗口大小来自真实后端/模型配置。修改客户端数字不等于扩容服务器。

## 9. 实现落点与回归要求

| 文件/区域 | 责任 |
|---|---|
| `cmux_fleet/fleet.py` | 原编排、CLI 选择、正式请求登记、回调校验、结果转告 |
| `cmux_fleet/pi_adapter.py` | pi home、argv（复用原 label）、回调结构验证 |
| `cmux_fleet/pi_extension.ts` | pi 生命周期桥、标题、supervisor 输入桥、本 agent 投递 ID |
| `cmux_fleet/cmux_manager.py` | 原终端/标签/布局；control dock 到 history 栏 |
| `cmux_fleet/handoff.py` / `history.py` | 原交接骨架、历史页；不是本轮重写对象 |
| `cmux_fleet/agents/*.md` | 原角色边界、命令表、回复格式 |

至少保留这些回归：

- [ ] 初代、继任代的 UI/会话名/路由/历史标识一致。
- [ ] worker 交接仍在右侧原 pane，supervisor 仍在左上。
- [ ] history/control 同栏，history 默认选中；control 移动不杀进程。
- [ ] move 失败保留控制台；原工作区有无关 surface 时不关闭。
- [ ] supervisor 真输入框派活 → worker 回复 → supervisor 转告，而非只测 request CLI。
- [ ] 正文引用其他 agent inbox 时仍关联自己的投递 ID。
- [ ] 旧代、重复、错误、中止回调不污染新请求。
- [ ] Codex/Claude 旧路径回归通过，Claude 仍直接 exec 官方 CLI。
- [ ] 演示阈值不遗留成正常配置。

## 10. 验证与交付状态（截至本次固化）

| 项目 | 状态 |
|---|---|
| 本机完整测试 | 91 passed，含实际 TS 扩展合同测试 |
| Studio 隔离目录测试 | 90 passed、1 缺 Node 跳过；该项本机已过，未替换在役安装 |
| 全 pi + Flash Next 真实 cmux | READY、请求结果、双角色交接、历史浏览器已实测 |
| 原标识与 pi session name | 已做单测和真实会话核验；一次实测中发现的跨 inbox ID bug 已修并加回归 |
| supervisor 手工要求 worker 回复 OK | 真实 TUI 输入闭环通过，worker `reply=OK`，supervisor 已转告 |
| control 与 history 同栏 | 真实当前实例已移动、心跳继续，启动代码与测试已更新 |
| 正常换代阈值 | 当前本机实例已恢复 0.6 |
| Claude 真实混合任务 | 尚未实测；只有路由/官方 CLI 调用链单测，不能宣称端到端通过 |
| 独立复核 | 此前 Fable 额度不足、Opus 超时，无完整复核结论 |
| 合并/生产部署 | 未完成，不能由开发演示状态推定已上线 |

外部项目 `us-stock-option` 的证据文件：
- `reports/cmux-fleet-pi-validation.md`：早期完整窗口/交接测试（历史 checker/1% 实例，不是当前配置）。
- `reports/cmux-fleet-pi-ui-fixes-20261010.md`：命名、输入闭环及 ID 修正；control 最初同 supervisor 栏，后被下一条确认覆盖。
- `reports/cmux-fleet-control-history-20261010.md`：最新 control/history 同栏确认。
- `reports/cmux-fleet-pi-naming/operator-ok.json`：真实手工输入闭环结果。
- `references/cmux-fleet.md`：本地运行手册与当前配置入口。

以后修改先对照本文；用户新要求须明确记录替代了哪条约束，再修改实现与回归。不能只改演示、只改截图，或把旧报告的临时状态当作新设计。

## 11. Redis 通信与分层显示 v2（2026-10-11 新要求，开发接线待验收）

完整实施方案：[Redis 阻塞通信与分层显示](../../us-stock-option/reports/cmux-fleet-redis-control-design-v2-20261011.md)。本节为目标约束，不是上线声明。

### 替代关系

- 覆盖 §7 的文件路径关联方式：正文、逻辑消息 ID、delivery ID、代数/会话/执行授权均在 Redis；agent 适配层阻塞收件并直接注入业务上下文，不让模型读 msg Markdown 或轮询 Redis。
- 覆盖 §5/§9 中“每个 operator result 都交 supervisor 模型转述”的旧行为：简单结果、超时、失败由代码生成 UI-only 卡，零机械转述回合；只有继续判断/计划/总结才创建业务 follow-up。
- 覆盖 pi“必须最终打印协议 JSON”的旧显示方式：拟用结构化结束工具收集候选，保持原 JSON schema/外部协议；agent_settled 后再由 daemon 验收。UI renderer 显示摘要，control 展示完整参数/结果。
- 保留 generation/投递验证、错误拒绝、外部 request/result、原 task/service 与换代规则；增强 PEL/租约/fence/幂等恢复，不宣称副作用 exactly-once。

### 显示边界

- supervisor：用户指令、派工摘要、业务判断、结论与明确故障提示。
- worker：当前任务、实际工作过程、业务结果与明确故障提示。
- control：完整通信信封/JSON、ID、时间戳、ACK、路由、重试、心跳、校验与错误链；支持按任务/槽位查详情。
- 运输信息不进入模型上下文；必要业务正文可入上下文而不在窗口重复铺全文。UI-only 用非模型 session entry，不能误用隐藏 custom message 让通知进入上下文。
- 用户给出的 `[Operator result ...] ... Read that whole file ...` 不再出现；显示“worker-1 处理超时（约 10 分钟），未取得有效结果”。超时不等于执行已停止，未确认停止必须明示。
- 不全局过滤 JSON/路径/assistant 文本，不隐藏业务内容、真实失败或待授权事项。
- control/history 仍同栏，history 仍默认选中；详情集中 control 不意味着擅改布局或抢焦点。

### 实现与交付边界

- Redis Streams 阻塞消费、确认与恢复，沿用唯一 daemon；私有 Unix socket 传输不变，路径按 §13 改为项目内运行目录，不能静默回退文件信箱/生产 Redis。
- pi 原生扩展路径优先；Claude/Codex 官方 CLI 的输入与显示能力单独验证。未经验证不宣称混合链已支持，不擅换 CLI/认证路径。
- v1/v2 按 run 显式区分，不热改在途实例。旧 §10 的 91 测试/演示不能证明 v2 通过。
- v2 必测：无消息文件/模型 read；ACK/心跳/超时卡零模型调用；control 详情可追溯；旧代/重复/崩溃恢复；慢 UI 不阻塞调度；人工输入、命名、布局、交接与旧接口回归。

### 2026-10-12 开发接线原始交付记录（后续集成验证见 §14）

- 2026-10-12 用户进一步要求“所有实例通讯全部改成 Redis”：新 `start` 默认且只允许 `transport_version=2`，仍须 `--allow-development-v2`；拒绝新建 v1，不能再默认生成消息文件。旧/无版本配置仍按 v1 解释，仅供现存 run 正常收束、status/stop/callback，不热改在途实例。当时 v2 只支持 **Pi supervisor + Pi workers**，在 provisioning 和 profile retirement 前拒绝 Claude/Codex v2；不回退文件信箱、代理 Claude 或改用 headless CLI。（“只支持 all-Pi”已被下文 2026-10-12 Codex 监工 + 官方 Claude worker 小节替代；后半句三条禁令继续有效。）
- `Fleet` 保留唯一的 `submit/request/assign/on_supervisor/on_service_supervisor` 判断和产物验收、worker FIFO、pause/max-busy、label/代数及交接骨架。`runtime_v2.py` 只接 transport 与事件效果；`agent_bridge.py` 常驻 JSONL 子进程独立阻塞读取 inbox/UI/reply，并用生产连接登记/续租。v2 正文来自 Redis，投递不用 `.inbox` 文件或 cmux paste。
- `pi_extension_v2.ts` 在 session_start 启桥、session_shutdown 幂等停桥；使用已安装 Pi 1.1.0 API。自定义 sendMessage 启动回合从 agent_start 关联（实际 SDK 不触发 before_agent_start）；人工回合隔离。fleet_decision/fleet_result 仅收候选，最后 settled 通知后由 daemon 验证。terminate 只按 Pi 的整批工具语义跳过冗余续问。UI Stream → appendEntry/registerEntryRenderer，transport IDs 只存非模型元数据。
- 简单 service 结果、invalid/skipped/timeout/stalled 用 presentation 卡；不自动增加 supervisor 机械转述。task worker 报告仍形成另一个绑定的 supervisor 业务 delivery，COMPLETED 仍需字面产物验收。
- 事件 XREADGROUP 与 legacy `fx:events` → processing List → 确定性登记 → 确认；保留旧 `fx:reply:* / fx:result:* / fx:results`。效果先在无副作用 overlay 上准备，再持久化 intent，preflight Lua 一起应用公开结果/UI、delivery 完成和事件 XACK。正常 Lua 执行隔离不等于异常回滚；prepared intent 可重放，applied 不重做效果，任何留下 applying 的不明写入失败暂停待查，不能盲重 RPUSH 或重跑工具。
- timeout/cancel/disconnect/aborted/unfinished 保留 active lease/PEL，状态为 held，不把 Escape 当停机证据。交接继任进程 launch gate 等待旧 tab/process-tree 退役确认；关闭失败禁止继任执行。daemon 同 run 重启不 start_all，未证明安全时 held/paused；异常或 daemon 信号退出保留 Redis。只有显式 fleet stop 且各 runtime 退役确认后关闭 Redis。
- v2 私有 Redis 使用 AOF `everysec`：主机/Redis 崩溃仍有约 **1 秒**未 fsync 窗口。未终态消息与去重证据不 TTL/裁剪；当前尚无容量归档清理的完整验收。browser broker 的 v2 可重放副作用事务尚未实现，命令明确拒绝。
- 新 TS 扩展合同测试和效果规划单测通过，类型检查通过；这些不是模型或真实 TUI 测试。沙箱内实际 Redis 因 `Failed opening Unix socket: bind: Operation not permitted` 未能启动，真实 bridge/daemon 集成套件未通过。**development gate 不可移除**，直到独立复核、真实 Redis 全套、实际 SDK/监督者 TUI 输入、所有崩溃边界/慢 UI、1000 条对账、AOF 窗口/日志容量与性能分位测量完成。完整命令和进展保存在外部项目 `reports/cmux-fleet-runtime-v2/core-*.md`，不把旧 v1 演示当作 v2 验收。

### 2026-10-12 用户新要求：Codex 监工 + 官方 Claude Code worker（替代“v2 只许 all-Pi”）

用户 2026-10-12 拍板：先让 v2 支持 **Codex supervisor + 官方 Claude Code workers**，之后用它开 6 个 Claude worker 并行开发，监工 Codex `gpt-6-astra`。本小节替代上文与 §14 “v2 只允许 all-Pi” 的约束；三条禁令（不回退文件信箱、不代理 Claude、不改 headless CLI）、唯一 daemon、task/service 外形、slot/generation 标识全部不变。

| 项 | 约束 |
|---|---|
| 支持矩阵 | v2 只放行 `pi+pi`（task/service）与 `codex+claude`（**仅 task**）。Claude 监工、Codex worker、Pi 与官方 CLI 混配、`codex+claude` service 模式在 provisioning/profile 退役前带原因拒绝（`runtime_v2.v2_cli_problem`，inventory 同口径）；仍须 `--allow-development-v2`。 |
| 派活 | 原 `task` / `request` 命令入 v2 events Stream，原 `submit/on_supervisor` 判断与产物验收不变；监工最终回复仍是 AGENTS.md 决策 JSON。Pi 专用的 `fleet_decision/fleet_result` 改写与 `_v2` 角色文件只用于 Pi 槽，官方 CLI 槽保留原文。 |
| 投递 | 唯一 daemon 在原 idle 投递 / 交接边界 `offer` 后，以 `tui-<slot>-g<N>` 身份 `receive`，把 `body.text`（业务正文）用 cmux 打进官方 TUI 并回车；Claude 窗格用 bracketed 粘贴（实测非 bracketed 多行会停在输入框或被拆成两条提示），Codex 保持原非 bracketed。不写 `.inbox`/消息 Markdown，不带 message/delivery/fence 等运输 ID。打字失败即 hold（fail closed）。daemon 作为接收方在 tab 存在时续租，停滞/超时/tab 消失仍按原规则 hold。 |
| 回收 | Claude Stop / Codex notify 的 v2 分支（`fleet.tui_turn`）：READY 回合发 `bridge_session` → `register_slot` → `startup`（READY 闸）；之后只在本代租约为 `received`、且本回合由打进去的正文发起时（Claude 查转录里的用户提示，Codex 查 notify 的 `input-messages`），把最终文本映射为候选（监工决策 JSON；worker Done/Evidence/Not done/Blocked 文本或同键 JSON）发 `settled`。人工回合、重复回调、旧代回调不结算；daemon 仍按原 settle 校验身份/租约/fence/schema。解析不出候选不伪造成功：监工走原三次纠错；官方 CLI 的 task worker 该次投递记 failed，经原 FIFO 投递路径（新 delivery/fence、同 generation）排一条“只按 Done/Evidence/Not done/Blocked 格式重发报告”的纠错，最多 2 次（`TUI_REPORT_RETRIES`），仍不合格才判 task blocked；合格后计数清零。Pi worker 仍按原规则直接 blocked。 |
| 钩子超时 | v2 Claude Stop hook 超时 = `profile_ready_timeout`+30 秒，READY 回合等 daemon 回复最长 `profile_ready_timeout`（daemon 仍在逐个开 tab 时也不丢 READY）；普通回合结算最多等 30 秒，事件已持久不因等待超时丢失。v1 仍 30 秒。被忽略/过期回合写 `turn.ignored` / `turn.stale` 审计。`profile_ready_timeout` 目前无启动旗标。 |

**性质说明**：把正文打进官方交互 TUI 不是三条禁令中的任何一条（不是文件信箱、不经 pi、不是 headless）。§11 交付记录“投递不用 cmux paste”写在 Pi 实现段、v2 方案验收清单写作“pi 不再靠 cmux paste 收件”，本小节按 Pi 作用域理解；v2 方案 §8 给官方 Claude 列的候选是“prompt-submit hook 注入正文、终端只留短标题”，本次未采用、未实测。若用户要把“不用 cmux paste”扩到全体 v2，这条链须改走该候选或退回阻塞项。

**限制**：Codex 监工没有 UI-only 卡渲染器，service 模式简单结果只能在 control 看，故本组合只开 task 模式；Codex 监工的人工输入不入队（§5 输入边界不变）。`max-busy` 缺省 0 = 不限，6 个 worker 可同轮投递；它不管启动瞬间。

**验证（2026-10-12）**：本机 `python3 -m pytest -q tests` 592 passed（基线 554），新增 `tests/test_v2_codex_claude.py`（真私有 Redis + 真钩子入口 + 假 cmux/CLI：矩阵、派活入总线、正文投递无消息文件、Stop 回复进 v2、旧代/重复/人工回调被拒、6 worker 同轮投递、max-busy、打字失败 hold、READY 闸、续租、Claude bracketed、转录工具输出超 1MB 仍能结算、被忽略回合留审计）。真实 cmux 冒烟：隔离 `CMUX_FLEET_HOME/CMUX_FLEET_STATE`、私有 Redis `/tmp/cfv2x/redis`，1 个 Codex（gpt-6-astra）监工 + 1 个 Claude（sonnet）worker，task “写 worker-1/ok.txt=OK” 走完 READY→派活→worker 回报→监工 COMPLETED→产物验收，无 `.inbox`/msg 文件、审计无 error/warn。前两次冒烟暴露并修复两处（`/tmp` 软链下 Codex rollout cwd 比对失败致 READY 被丢；Claude 非 bracketed 多行粘贴不可靠，第 2 次靠人工补一次 Enter 才提交），修复后第 3 次全程无人工干预通过。冒烟会在 `~/.claude.json` 为临时 run 目录写信任项（v1 既有行为）。

**未验收**：6 worker 真并发（只测 1+1，7 个 tab 冷启动是否在 180 秒 READY 闸内未实测）；`shutil.which('claude')` 在 cmux 内解析到的 cmux claude shim（冒烟用 `--claude` 指真实二进制）；带交接的换代（generation+1）在真实 TUI 上；profile live 切换到官方 CLI；长任务中的续租/停滞；Claude 版本升级后粘贴行为；人工在 worker tab 输入与投递撞车（Claude 会把粘贴排进队列，未测）；独立复核、合并、部署均未做。

## 12. 动态 agent profile 路由（2026-10-11，Phase 5 显式 live opt-in）

本节根据用户本轮授权替代 Phase 4 的“任何 live=true 都 unsupported”约束。Phase 1 registry/router/planner、Phase 2 task 接入、Phase 3 稳定 slot assignment、Phase 4 授权与元数据应用继续保留；Phase 5 在唯一 daemon 的原投递边界增加受控 respawn。默认仍是 **advisory + metadata**，不是第二套 dispatcher。本轮只授权本仓 dev 验证，不涉及交易生产系统，不重启已有 fleet。Phase 5 当时使用 v1 队列/投递协议；§11 新增的 v2 开发接线单独 opt-in，不表示 Phase 5 真实混合 CLI 已验收。

### 身份、规划与授权

- `flashnext-coder`、`claude-reviewer`、`researcher` 是 profile id，绝不成为 worker id、Redis key、CLI session name、cmux label 或历史标识。`worker-1` 切换后为同一 slot 的 `worker-1.1`；旧 label 在同一 run 仍路由到该 slot。
- card 必填 `id/display_name/cli/capabilities`；可选 `model/profile/role_file`。JSON 用标准库，YAML 仅在已有 PyYAML 时可用。rank 越低越优。
- router 依能力重合选择：复核/安全优先低 risk，代码优先低 latency/cost，最后按 id 稳定排序。code-change plan 包含 implementation 和依赖它的独立 review。
- task 的 `auto_agents` 为严格 boolean，默认 false；`agent_registry` 为已有目录，仅 expanduser，不做 shell/env/glob 展开。唯一 daemon 在 submit 存储 `dispatch_plan` 和 `profile_assignments`，operator artifacts 不变。
- assignment 按计划顺序使用已有 slot；容量不足则复用，同 profile 再次出现沿用原 slot。`depends_on` 仍由 supervisor 遵守，不增加 scheduler。
- supervisor 使用 assignment 的 `worker` 作为 `dispatch.to`，指令可含 `Recommended profile: <id>` / `Subtask: <id>`。推荐必须获当前 task/slot/subtask assignment 授权；无 marker 时必须唯一确定 profile。正文提及 id 不授权，能力只来自有效 registry card。
- 整条 decision 入队前校验；错误/未知/冲突推荐使用原纠错与三次失败机制。队首在实际投递前及新代 READY 后再次校验 task id、assignment、registry、live 开关；失败阻塞，不回退其他 CLI/model。

### 双重开关与 runtime 映射

- 只有 **task spec `apply_profiles_live: true` 且 fleet config `allow_profile_live_switch: true`** 才切换。两者严格 boolean，默认 false。配置由 `start --allow-profile-live-switch` 写入；配置许可不自动 opt-in 任何 task，也不从旧 config `apply_profiles_live` 继承 task 意图。
- task 请求 live 而 config 不允许，submit 明确拒绝；live task 还须 `auto_agents: true` 和有效 assignment。投递前再校验，没有 profile 不能悄悄退成 metadata 模式。service 协议不增加 live profile 意图。
- `pi`：card model 覆盖 `pi_model`，未填写沿用现有值；默认 coder 为 `flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP`。仍需 `allow_unsandboxed_pi`、Pi >= 1.1.0、独立 run home；只链接 Pi auth/models，不使用 Claude 凭证。
- `claude`：直接 exec 官方 Claude Code CLI，card model 覆盖 `worker_model`，默认 `sonnet`；不经 Pi。Claude Stop settings 按 generation 保存，避免旧进程读到新代 hook。
- `codex`：researcher 保持 Codex；card `profile` 覆盖 worker/shared profile，card `model` 可覆盖模型。未填沿用原 defaults。所需 profile 文件必须存在，保留隔离 CODEX_HOME。
- `profile` 指 Codex 配置档名；Pi/Claude 上填写该字段明确报不支持，不猜测 CLI 参数。`role_file` 按 registry 解析且必须存在，拒绝路径/符号链接逃逸；角色作为附加指令保留基础 fleet 协议。缺省不附加。
- 独立复核 worker（2026-10-13 用户要求“设置一个验收的 worker 单独去验收”）：`claude-reviewer` card 用 `role_file: reviewer.md`、`model: opus`。`reviewer.md` 是外部项目 `references/multi-round-review.md` 与 `.claude/rules-lib/R-WKF-033.md` 的 fleet 副本（最高原则、只读、只审给定范围、P0/P1/P2、报告模板、PASS=零 P0 零 P1），规范改了两处同步；它按上条附加在 worker 基础协议之后，回报仍是 Done/Evidence/Not done/Blocked，监工的产物验收不变。启用：`start --allow-profile-live-switch` + task `auto_agents: true` 与 `apply_profiles_live: true`，code-change plan 的 review 子任务即排到 `claude-reviewer`；不开 live 时只是 metadata 推荐，worker 仍跑原 CLI/角色。**v2 Codex 监工 + 官方 Claude worker 组合下的 profile 热切换尚未实测**（§11 对应小节“未验收”已列），只有 fake 回归；fleet `worker_cli=claude` 时 submit 以 `preferred_cli=claude` 规划（同能力卡里优先 claude 卡），code-change plan = `claude-coder`（claude/opus/worker.md）实现 + 依赖它的 `claude-reviewer` 复核，两张卡都过 v2 矩阵的 `preflight_profile`；pi/codex fleet 不传偏好，计划不变。research 仍只有 Codex 的 `researcher` 卡，该组合下 live 切过去仍会被拒。

### 元数据的唯一含义

| 字段 | 含义 |
|---|---|
| `desired_profile` | 队首 assignment 的目标意图；不是已运行证明 |
| `advisory_profile` | Phase 4 的首次 advisory profile；metadata 模式使用它，不再占用 current |
| `runtime_profile` | 本代 spawn 的 profile，初始默认进程为空；starting 阶段尚未 READY |
| `current_profile` | 本代通过 READY 的 applied profile；spawn/本代启动失败时为空，不再表示 advisory |
| `runtime_ready` | 本代是否通过 READY；profile 生效必须结合它与 state 判断 |
| `runtime_launch` | card id、实际 CLI/model/profile override、角色内容的冻结快照 |
| `profile_state` | `none/pending/switching/ready/applied-live/applied-metadata/blocked` |
| `profile_capabilities/reason/task` | 目标 card 能力 JSON、状态说明、相关 task id |

metadata-only 模式不改变 runtime/current/generation：安全边界接受首次 advisory 为 `applied-metadata`，不同 advisory 为 `pending`，仍使用已有 runtime。busy/handoff 只记录 pending；无意图为 none。`applied-metadata` 永远不是实际模型已切换的证明。

### Live 切换顺序与失败处理

1. 仅在 worker idle 且未因 abort 留下未确认空闲状态时进入；starting/busy/handoff/dead 均不能切换。Codex 有未结束 native children 时继续排队。
2. 校验二重开关、assignment、card、role、CLI binary、Pi 许可/版本/home、Codex profile 文件。缺依赖先阻塞，保留旧 runtime，不暗换 CLI/model。
3. 保存旧屏幕和切换历史；先增加 generation、保留稳定 slot/别名，对旧回调设 fence。调用现有 `spawn()` 创建继任 tab，actor 命令固定 generation，新 CLI 停在 `launch_ready=0` 闸门。
4. 用原 `tab_into` 保留右侧 worker pane；原 `kill_pane` 退役旧进程树/tab，并确认旧 tab 消失，才打开闸门执行新 CLI。60 秒仍未获闸门许可的 actor 自行失败，不执行目标 CLI。
5. 队列不出队，等待新代严格返回 `READY`，所有 CLI 都检查失败/中止/文本。然后标记 runtime ready/current 与 `profile_state=ready`。profile startup 默认 180 秒限时（`profile_ready_timeout`），即使 stall_timeout=0 仍执行；失败或超时阻塞并拒绝迟到 READY。
6. 下一次 idle 投递再次验证队首；冻结 launch 与当前 card/角色内容必须一致，漂移则阻塞。通过后 `applied-live` 才投递。相同且已 READY 的 profile 不重复 respawn；正常上下文 handoff 保留已选 runtime。

退役、布局、spawn 失败会撤销闸门、禁用新代并尝试清理继任 tab；不恢复旧代、不回退模型。关闭失败的旧 tab 记录 `retired_pane`，须人工处理。startup 失败的 tab 可保留用于排错；`dead/blocked` 代表禁止继续投递，不证明所有 OS 进程已消失。原任务终止会清空队列，必须修复后重新提交。模型 READY 是配置启动与协议就绪证明，不是后端模型权重或凭证有效期的独立认证。

### Phase 5 验证与部署状态

- 本机 `python3 -m pytest -q tests`：**243 passed**，包含双重 opt-in、busy/handoff 边界、READY gate、旧代/重复投递、失败清理、三种 CLI 参数/回调与角色映射、Pi supervisor operator-input 桥回归。使用 fake Redis/cmux/CLI，不称为真实 TUI 或混合模型验收。
- 两 worker 隔离 dev start 已尝试：当前沙箱不能连接 cmux socket，`Operation not permitted (errno 1)`，start 在 provisioning 前退出。
- 进一步运行 `tests/pi_smoke.py`：临时私有 Redis 因 `Failed opening Unix socket: bind: Operation not permitted` 退出；未到 Pi/model 或 live switch。测试子进程/临时目录已清理，没有替换已有 fleet。
- 完整命令、结果与待验收边界见 [dynamic-agent-routing.md](dynamic-agent-routing.md#dev-validation-2026-10-11)。**上述为 Phase 5 当时的历史状态：代码/模拟回归已完成；真实 dev 部署与 supervisor TUI smoke 受环境限制未通过；当时未独立复核、未合并、未生产部署。** 后续本地集成复核见 [2026-10-12 报告](../reports/full-local-merge-20261012.md)，不替代真实 TUI/生产验收。

## 13. Redis 项目内运行目录（用户新要求）

用户明确要求把 Redis 运行文件从 `/tmp` 改放本项目内。本节替代 §7/§11 的旧路径约束，不改变私有实例、唯一 daemon、task/service、v1/v2 协议或生产授权边界。

- 新 `start` 默认在源码项目 `.runtime/redis/<8位token>/` 保管 `redis.sock`、`redis.pid`、`redis.log`、`fleet.rdb`；v2 的 AOF 子目录也在此。Redis binary 仍来自 `--redis-server`/PATH，本轮不是打包 binary。
- Redis 专属目录与 `--runs-dir` 分开：保留原 workspace/history/agent home 路径和 latest 入口，避免带时间戳的长 run 路径超过 Unix socket 限制。默认 root 从代码位置确定，不随当前工作目录变化。
- `--redis-runtime-dir` 可显式选择其他短且可写的 root，用于只读安装或独立开发验证。配置固定 `redis_runtime_root/redis_runtime_dir/redis_instance_id/redis_socket`，新进程按配置连接，不依赖调用 shell 的环境或当前目录。
- `redis_runtime.py` 统一 startup、CLI/hooks/bridge 和 control 的 endpoint 校验。新配置的 socket 必须恰为 root/token/redis.sock，拒绝跨实例路径、相对路径、父目录穿越、符号链接逃逸或不完整元数据；不能把非法新配置降级成旧 `/tmp` 路径。
- root 和新实例目录为当前用户所有、owner-only（700）；已有共享权限 root 明确拒绝，不替用户 chmod 任意目录。已有实例目录不复用、不删除。此权限不是同账号进程之间的安全隔离，也不解除 OS sandbox。
- socket 按编码字节数检查：macOS 最长 103 字节（保留 NUL），Linux 107 字节。超长在创建/启动前失败，提示缩短路径；不建立 `/tmp` 软链、不自动退回 `/tmp` 或 TCP。仍 `--port 0` / `--unixsocketperm 700`。
- 旧无 managed 元数据的 `/tmp/cmux-fleet-<token>.sock` config 继续严格兼容，供在途实例正常 status/stop/callback；不迁移、重启旧 fleet。它不是新 start 的 fallback。
- stop 沿用唯一 daemon 的 Redis shutdown；socket/PID 由 Redis 清理，日志/RDB/AOF 保留供排错和恢复。不引入共享系统 Redis，不杀其他 Redis。
- `.runtime/` 不进入版本控制。本工作区用 Git 本地 `info/exclude` 排除，没有修改 `.gitignore`；其他 checkout 需同样排除。

## 14. Redis 信箱本地集成验证（2026-10-12）

用户要求合并 Redis 信箱功能。本轮冻结框架候选后重跑完整回归、真实 Redis 竞态探针、已安装 Pi SDK/TUI 合同，以及真实 Redis + Fleet + Pi/FlashNext 的 supervisor TUI 输入闭环；详情与文件哈希见[集成报告](../reports/redis-mailbox-merge-20261012/validation.md)。

这更新 §11 原始交付记录中的环境受限状态，不改变运行协议或撤销门槛。新启动默认且仅允许 Redis v2，仍需 `--allow-development-v2` 显式许可和 all-Pi（2026-10-12 起另放行 Codex 监工 + 官方 Claude worker 的 task 模式，见 §11 对应小节）；旧 v1 配置仅保留读取、收束与 stop 兼容，不热改。这落实用户将通信改入 Redis 的要求，不把新启动默认为文件信箱。真实模型验证使用独立 PTY，cmux 布局仍为模拟；单独的真实 cmux 布局测试不等于 v2 全链已获生产验收。完整崩溃/慢 UI/1000 条整链、性能/AOF/容量和混合 CLI 仍待验收。

合并不热切当前 v1 run，不停止其在途任务，也不代表其他机器或生产实例已迁移。

## 15. 全实例通信盘点与迁移（2026-10-12 用户新要求）

- 盘点配置、进程、registry 与运输方式，区分运行实例、旧历史和仅登记的残留窗口；不能把历史 `.inbox` 文件数量当作正在执行的证明。
- 用户当前本机 Pi 开发实例可在在途任务、队列收束后受控替换为新的 v2 run。保留旧 run/结果/交接记录，不篡改旧代数，也不删除历史文件伪装成无文件通信。
- 新 run 的 supervisor 和每个 worker 都必须实际验证 Redis 收件；验收包含无新消息 Markdown、无模型读取消息文件、无机械 supervisor 转述，而不只是检查 config=2。
- 2026-10-12 起 Codex 监工 + 官方 Claude worker（task）可新建 v2 run（§11 对应小节）；其余原生 Codex/Claude 组合和受保护的在役/生产实例仍明确列阻塞项；通信改造不授权替换 CLI、转移订阅凭证、停止交易服务或写生产 Redis。未验证的组合不宣称已迁移。
- 只读盘点工具 `python3 -m cmux_fleet.transport_inventory` 报告已扫描机器/目录、配置版本、关联进程和消息文件数量；不连接 Redis、不读取消息正文、不修改任何实例。

## 16. worker 名单热加载（2026-10-12 用户新要求）

用户要求“worker 可以热载也可以卸载，用 yaml 控制”。本节替代“worker 名单在 `start` 时冻结”的旧行为；slot/generation 标识、一页布局、唯一 daemon、task/service 协议不变。

- 入口：`start --roster <yaml>`，与 `--workers/--worker-names` 互斥；schema 为 `workers: [{name, role?}]`，`role` 相对 yaml 目录、优先于 `--roles DIR/<name>.md`。启动时文件不合法直接拒绝启动。不带 `--roster` 的 run 行为不变。
- 检测：daemon 每次 tick 开头比较文件 mtime/大小，再比内容哈希；名单只在这一处改动，不在逐 slot 循环里改。
- 整份校验：YAML 错、未知字段、名字不合法/重复、零个 worker、角色文件不存在、本 run 已用过的名字（Redis agent 状态或 workspace 目录已存在）→ 整份拒收，保留当前名单，`roster.rejected` 写入审计（control 可见）并列出每条原因。
- 加：先写 `config.json`（`latest.json` 指向本 run 时同步）再启动——钩子/桥按配置文件校验成员；Redis 字段与 `start_all` 相同，READY 提示相同，标签放进右侧 worker 栏末尾（无 worker 时放 supervisor 右侧），刷新历史页。
- 卸：标记退役中——不再是派活目标（决策/请求/路由校验、profile 分配都排除），但仍计入 `max_busy` 与“有 worker 在忙”；空闲且队列空、v2 无在途投递时，按换代同样的方式存屏、关闭旧 tab（v2 `retire`），从名单删除并写回配置。工作目录保留，历史页继续可查。退役完成前写回文件即取消。
- 只改 `role`：下一代生效，不重启。
- 通知监工：不单独发一条消息（task 模式下单独一条会让监工回一个无派活的 RUNNING，按原规则判错三次即 blocked）；改为在下一条发给监工的业务消息末尾追加当前可派 worker 名单。
- 已知边界：新加的 worker 若启动未 READY，按原启动超时规则使在途任务 blocked；退役中 worker 处于 held 时不会退役，需人工处理；暂停中不投递，退役中 worker 的队列不会排空。
