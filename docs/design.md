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

- 当前自然语言入队适配针对 **pi supervisor** 与 v2 **Claude supervisor**（UserPromptSubmit 钩子，见 §11 的 2026-10-13 小节），不宣称 Codex 监工已实现相同入口。
- 初始启动提示、daemon 的 `.inbox` 指针、slash 命令不再次入队。
- worker 的手工输入仍是该 worker 自己的交互，不冒充 supervisor 派活，也不冒充某个已有请求的结果。
- 不支持直接转送图像附件，明确提示改用文件路径；不得静默丢掉图片后当作正常请求。
- 未 READY、旧 generation/thread、空文本等输入应明确拒绝；队列确认失败显示错误/未确认，不伪造成功。

## 6. CLI 与认证边界

| 角色 | 可选 CLI | 完成通知 |
|---|---|---|
| supervisor | 原 Codex / pi；官方 Claude（仅 v2，见 §11 2026-10-13 小节） | Codex notify / pi agent_settled / Claude Stop |
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

**限制**：自 A2 起 UI-only 卡片由 daemon 用 `tui_cards` 渲染到 `<run>/cards/<slot>.log` 与历史页 Cards 视图，不进模型上下文；`codex+claude/service` 因此解除阻塞（`adapter_missing: []`），但在真实 smoke 通过前仍为 `unverified`（只能 `--lifecycle smoke` 起）；Codex 监工的人工输入不入队（§5 输入边界不变）。`max-busy` 缺省 0 = 不限，6 个 worker 可同轮投递；它不管启动瞬间。

**验证（2026-10-12）**：本机 `python3 -m pytest -q tests` 592 passed（基线 554），新增 `tests/test_v2_codex_claude.py`（真私有 Redis + 真钩子入口 + 假 cmux/CLI：矩阵、派活入总线、正文投递无消息文件、Stop 回复进 v2、旧代/重复/人工回调被拒、6 worker 同轮投递、max-busy、打字失败 hold、READY 闸、续租、Claude bracketed、转录工具输出超 1MB 仍能结算、被忽略回合留审计）。真实 cmux 冒烟：隔离 `CMUX_FLEET_HOME/CMUX_FLEET_STATE`、私有 Redis `/tmp/cfv2x/redis`，1 个 Codex（gpt-6-astra）监工 + 1 个 Claude（sonnet）worker，task “写 worker-1/ok.txt=OK” 走完 READY→派活→worker 回报→监工 COMPLETED→产物验收，无 `.inbox`/msg 文件、审计无 error/warn。前两次冒烟暴露并修复两处（`/tmp` 软链下 Codex rollout cwd 比对失败致 READY 被丢；Claude 非 bracketed 多行粘贴不可靠，第 2 次靠人工补一次 Enter 才提交），修复后第 3 次全程无人工干预通过。冒烟会在 `~/.claude.json` 为临时 run 目录写信任项（v1 既有行为）。

**未验收**：6 worker 真并发（只测 1+1，7 个 tab 冷启动是否在 180 秒 READY 闸内未实测）；`shutil.which('claude')` 在 cmux 内解析到的 cmux claude shim（冒烟用 `--claude` 指真实二进制）；带交接的换代（generation+1）在真实 TUI 上；profile live 切换到官方 CLI；长任务中的续租/停滞；Claude 版本升级后粘贴行为；人工在 worker tab 输入与投递撞车（Claude 会把粘贴排进队列，未测）；独立复核、合并、部署均未做。

### 2026-10-13 Claude 监工适配层（路线 2.0b，组合表 `claude+pi` task/service、`claude+claude` task）

本小节补上 §6 表里没有的「Claude 监工」；三条禁令、唯一 daemon、task/service 外形、slot/generation 标识不变。组合表这三行 `adapter_missing` 清空、仍为 `unverified`：只能 `--lifecycle smoke` 起，smoke 真跑通过后由人改 `verified` 并填 `verified_by`。

| 项 | 约束 |
|---|---|
| 启动 | `start --supervisor-cli claude [--supervisor-model M]`（缺省 `opus`）；fleet 直接起官方 `claude`，监工规则照原 `supervisor.md` / `service_supervisor.md` 写成 AGENTS.md + CLAUDE.md，不带 `fleet_decision` 改写。 |
| 投递 / 决策 JSON / READY / 纠错 | 全用上文 Codex+Claude 小节的 TUI 通路：daemon 以 `tui-supervisor-g<N>` 收件、bracketed 粘贴进 Claude；Stop 钩子 → `tui_turn` → `fleet_decision` 候选 → 原 `on_supervisor` / `on_service_supervisor`。READY 闸、三次纠错（task 判 blocked，service 暂停）均为原逻辑。 |
| 人工输入入队（§5） | 只给非 adopt 的 v2 Claude 监工加 `UserPromptSubmit` 钩子（`claude-prompt`）。放行：daemon 打进去的本代投递正文、本代起始提示（仅 READY 前）、`/` 命令、空文本；拒绝并说明：未 READY、旧代、图片占位（请改用文件路径）。其余人工文字一律发 `operator_input` 事件（原 submit / `[Operator instruction]` / `ui-<uuid>` 请求语义，生成 ID 的仍是 Python），然后**挡住**这条提示不进模型，原因里显示「已排队（ID）」/「被拒绝：原因」/「未确认，请先看 control」。被接受的输入随后作为正式投递打回监工。钩子通过时 stdout 为空（其输出会进模型上下文）。 |
| 已知降级 | 钩子 30 秒超时按非阻塞错误处理、该提示会被放行（按 Claude Code 钩子语义推断，未实测）：此时人工文字进模型、Stop 只记 `turn.ignored`，不会结算；daemon 等待上限 20 秒留在钩子超时内。被挡的人工文字不进入 Claude 转录，留存在钩子提示、Redis 审计和随后的投递里。官方 TUI 监工（Codex / Claude）没有 UI-only 会话条目：daemon 用 `tui_cards` 把卡片渲染到 `<run>/cards/supervisor.log` 与历史页 Cards 视图（A2），不进模型上下文。 |

**验证**：`tests/test_claude_supervisor.py`（真私有 Redis + 真 Stop / UserPromptSubmit 钩子与 `claude-prompt` 入口 + 假 cmux；worker 分别为官方 Claude TUI 与 Pi bridge）；`scripts/smoke_claude_supervisor.sh` 离线档跑组合表与回归，`--live pi|claude` 档需 cmux、真模型和 `start --lifecycle`（D 块）后才能跑。**未验收**：真实 Claude TUI 上的 UserPromptSubmit 拦截与粘贴排队、真实 smoke、Fable 监工念稿预演。

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

- 入口：`start --roster <yaml>`，与 `--workers/--worker-names` 互斥；schema 为 `workers: [{name, role?, window?, handoff_ratio?}]`（后两项与顶层 `handoff:` 见 §22），`role` 相对 yaml 目录、优先于 `--roles DIR/<name>.md`。启动时文件不合法直接拒绝启动。不带 `--roster` 的 run 行为不变。
- 检测：daemon 每次 tick 开头比较文件 mtime/大小，再比内容哈希；名单只在这一处改动，不在逐 slot 循环里改。
- 整份校验：YAML 错、未知字段、名字不合法/重复、零个 worker、角色文件不存在、本 run 已用过的名字（Redis agent 状态或 workspace 目录已存在）→ 整份拒收，保留当前名单，`roster.rejected` 写入审计（control 可见）并列出每条原因。
- 加：先写 `config.json`（`latest.json` 指向本 run 时同步）再启动——钩子/桥按配置文件校验成员；Redis 字段与 `start_all` 相同，READY 提示相同，标签放进右侧 worker 栏末尾（无 worker 时放 supervisor 右侧），刷新历史页。
- 卸：标记退役中——不再是派活目标（决策/请求/路由校验、profile 分配都排除），但仍计入 `max_busy` 与“有 worker 在忙”；空闲且队列空、v2 无在途投递时，按换代同样的方式存屏、关闭旧 tab（v2 `retire`），从名单删除并写回配置。工作目录保留，历史页继续可查。退役完成前写回文件即取消。
- 只改 `role`：下一代生效，不重启。
- 通知监工：不单独发一条消息（task 模式下单独一条会让监工回一个无派活的 RUNNING，按原规则判错三次即 blocked）；改为在下一条发给监工的业务消息末尾追加当前可派 worker 名单。
- 已知边界：新加的 worker 若启动未 READY，按原启动超时规则使在途任务 blocked；退役中 worker 处于 held 时不会退役，需人工处理；暂停中不投递，退役中 worker 的队列不会排空。

## 17. 断点续跑（2026-10-12 用户新要求）

设计稿：外部项目 `tasks/plans/cmux-fleet-resume-design-20261012.md`（照期货研报生产线）。本节**改动 §11「daemon 同 run 重启不自动重做」这一条**，其余（唯一 daemon、slot/generation 标识、一页布局、task/service 协议、§16 名单语义）不变。

- **入口**：`start --resume <run 目录|auto>`。`auto` 取 `latest.json` 的 run；它已有 `result.json`、读不到或非 v2 → 照常新建。显式目录遇到这些情况拒绝并写明原因。run 的 `daemon.lock` 被占（daemon 还活着）→ 不做任何事。只认 v2 run。
- **Redis**：先连原 socket，连得上就用（只死了 daemon）；连不上就用 config 里原 `redis_runtime_dir`、原 socket 重启，AOF 原目录重载（`fleet.start_redis`，与新建共用）。不调 `redis_runtime.prepare`（它拒绝复用目录）。AOF `everysec` 的约 1 秒窗口不变。
- **完成以磁盘/Redis 为准**：task 产出沿用 `check_artifacts`；worker 回报本来就存在消息哈希 `result` 字段（不 TTL、不裁剪），不另写 checkpoint；service 以 `fx:result:<id>` 是否存在为准。
- **新原则（替代 §11 该条）**：只有显式 `--resume` 才重做不确定的在途消息，且计入次数；不带 `--resume` 的同 run 重启仍走 `recover()`：标不确定、整套暂停、不重做。`--resume` 本身就是操作员对重做的授权：在途投递按原规则先标不确定，再以「操作员已授权重做」解除（`resolve_uncertain`）；接回的活窗口先发 Escape 中止旧回合。**这是 Escape 唯一被当作「接受停止」而非停止证据的地方。**
- **窗口**：只有官方 CLI（Codex/Claude）worker 在窗口还在、CLI 进程还在、上次已 READY 时接回；task 监工一律起下一代（用交底重新开始）；Pi 一律起下一代（Pi bridge 遇 Redis 断线永久 fault、不重连）。下一代在原位（`tab_into` 旧 tab 后关旧 tab，同交接）；旧 tab 没了的 worker 放进 worker 栏末尾；监工窗口也没了 → 关掉残留、全部下一代、按 `arrange` 重排整页。新 control 标签重新并入 history 栏。
- **对账**：先重放 prepared intent（有 applying 不明 intent → 退回 `recover()`）；各槽在途解除、排队取消；解除暂停（原暂停原因写进审计 `daemon.resumed`）。task：本任务排队/在途的 worker 派发由 **daemon 自己**按原文重新排队（消息字段 `replay`=第几次），再给新监工一条交底：目标、产出已验/缺、已完成派发（附回报摘要）、已重排派发（明说不要再派）、只补派缺的。设计稿写「它只补派缺的」；由 daemon 而不是监工重派丢失的那条，是为了次数上限能落到确定的那一条上。service：`fx:reqq` 里没有结果的请求重置超时窗口、重新路由；`"replay": false` 的发布 `timeout`（原因「not replayed on resume」）。
- **时间预算**：停机时间照算进 task 的 `hours`；续跑时已过 deadline 的 task 在第一拍判 `budget_exhausted` 并写 `result.json`。
- **次数上限**：`--max-attempts`（缺省 2，写进 config，续跑时可覆盖）。超限 task 判 `blocked`（写明哪个 worker、哪条指令），service 记 `timeout` 带原因。
- **结束标志**：task 进入任一终态时 daemon 写 `<run>/result.json`（状态、原因、task id、产出逐条复验结果，原子写）；同一 run 接受新 task 时删除它。显式 `stop` 成功退役所有 runtime 后也写（service 记 `stopped`），所以 `stop` 等于结束这个 run。续跑时发现 Redis 里 task 已终态但没有 `result.json` → 补写后按已结束处理。
- **名单**：沿用 config.json 已落盘的名单；给了 `--roster` 就写进 config，daemon 第一拍按 §16 热加载对账。
- **已知边界（P2）**：丢失的「报告格式纠错」消息会原样重排给新一代（新一代没有上下文）；只死 daemon、Redis 未死时，worker 在停机期间做完的回合会被拒、再做一次；Pi 不接回；真实 cmux 下只剩 history 栏的整页重建未实测；`has_cli_process` 按进程名（codex/claude/pi/node）判断。


## 18. 接口契约：单 fleet 内的任务编号 + 开工闸（2026-10-13 用户新要求）

规范：外部项目 `tasks/plans/cmux-fleet-interface-contract-spec-20261013.md`（用户已批，含四条判断）与 `cmux-fleet-nested-decompose-design-20261013.md` §1–2。本节只落**单个 fleet**（顶级 supervisor = 本 fleet 的 supervisor）；同伴通知（SendMessage / `{{FLEET_PEER}}`）、子 fleet、escalate / veto 是下一块。唯一 daemon、slot/generation 标识、一页布局、task/service 协议、§16 名单、§17 续跑都不变。

- **开关**：task spec `"contracts": true`（严格 boolean）才启用；不带时 Redis、提示词、报告转述、产物校验全部与原来一致。`skeleton_worker` 可选，缺省第一个 worker。
- **状态**：一份 JSON 存 Redis `fx:contracts`（阶段 / 拆解表 / 登记表 / 待批提案 / 任务表 / 每个 worker 的回报归属），随 AOF 进续跑；程序每次变更写 `<run>/contracts.json` 并拷到 `<workspace>/contracts.json`，tick 再按 Redis 补同步；`<run>/contracts.log.jsonl` 追加日志。模型只读。
- **提示词**：`agents/contracts_supervisor.md`（附录 A）/ `contracts_worker.md`（附录 B §4），只在契约任务里注入：submit 时写进每个 agent 目录的 `CONTRACTS.md` 并在任务消息里指向它；任务期间新拉起的代（交接 / 名单热加）把同一节附进 AGENTS.md。基础 `supervisor*.md` / `worker*.md` 不改。
- **P0**：首个回复必须 `phase: "interfaces"` + `plan` + `interfaces` + 空 dispatch；五项检查（无生产方 / 无消费方且非 final / 成环 / 样例自检 / 字段写入方恰一个）逐项拒；**P0 拒绝不计入三次纠错、不设上限**（用户 10/13「P0 放宽到全部解决」），只受 task `hours` 预算约束。interfaces / skeleton 段「无 worker 忙时 RUNNING 必须派单」豁免（`others_active`，v1 与 v2 校验同一入口）。`path` 在 P0 只查相对路径，派单时才查是否在 `to` 那个 worker 的目录下（P0 没有 `to`）。
- **P1**：P0 过后 daemon 自己给 skeleton worker 派 `[skeleton]`；其回报到达时读 `skeleton.md`：每个接口一行 PASS、无 FAIL → 全部 `declared`、冻结 v1、`[contract] interfaces frozen at v1` 随回报转监工；否则回 P0，**不设次数上限**（同上，只受 `hours` 预算约束）；`skeleton.md` 里照抄的字面 `PASS/FAIL`（表头 / 图例）不算失败。
- **任务表与派单**：dispatch 项须带 `id`（M 编号），可带 `provides` / `consumes`（`id@version`）/ `edits` / `depends_on` + `serial_reason`；规范 §3.1 / §3.3 / §3.0.1④ 的拒绝文本原样。`depends_on` 未完成的项由 daemon 挂起，前置全部 `done` 后自动投递。回报归属按「排队时记下每条消息期待谁的回报」FIFO 弹出：派单记 M 编号，`[contract]` 通知与提案拒绝记空，所以通知回合不会结掉别的任务。任务 `done` = 它 provides 的接口全部 `ready`；回报了但未 ready 记 `failed`，监工重派同一编号（attempts+1）。
- **就绪与变更**：worker 报告 `Interfaces: IF-x@n provided: ready` → 程序字面校验（存在 / 字数 / contains / json_keys）过才 `ready`，并向在跑的消费方投 `[contract] ready`；不过则向生产方与消费方投 `[contract] not ready`。worker 报告末尾最后一个 JSON 对象（`raw_decode` 从后往前，不取第一个 `{`）里的 `interface_change` 按 §4.3 校验，过了进待批并转监工，监工用 `interfaces[].decision` 批 / 拒；监工自己声明的 compatible / breaking 即批准。程序按字段差判级（改 path、减 contains / json_keys = breaking）。breaking 批准后旧版 `deprecated`、消费方 `stale`，其下一条派单必须 consume 新版。通知只投给任务在跑的 worker，其余靠读 `contracts.json`。
- **COMPLETED**：另需全部非 deprecated 接口 `ready`，否则照原 bounce 回 `Independent verification failed ... IF-x@n not ready (...)`。契约任务的 artifacts 可加 `not_contains`；`cmd_exit`（argv 不经 shell，在共享输出目录运行，`timeout_s` 缺省 120、上限 600，输出写进 `path` 再做字面检查）**只认调用方在 task spec 里声明的**（submit 时另存 `caller_artifacts`，按 path + argv + timeout 比对）：监工 decision 新增或替换 cmd_exit 按无效 decision 纠错，存表里混进的未声明命令只报失败不执行（2026-10-12 E 修）；命令只在 COMPLETED 时跑，result.json / 续跑交底只复查文件。非契约任务这两个键仍被忽略。`pending_user` 的 decisions 汇总进 result.json 的 `contracts`。
- **v2**：官方 CLI worker 的自由文本报告，只有出现 `Interfaces:` / `Peer notices:` / `Question for supervisor:` 行或末尾带 `interface_change` 等键的 JSON 时才多出对应键，四段原样；续跑交底附任务表一行。
- **已知边界（P2）**：`cmd_exit` 只跑调用方声明的命令（仅契约 / 冻结任务、无 shell、限时），运行期间 daemon 阻塞；Pi worker 只能把契约字段作为 fleet_result 的额外键交（TypeBox 是否放行额外键未实测）；`consumers_runtime.session` 由 §21 填写；契约 notices 会让在跑的 worker 多一个回合；`Interfaces:` 等标题行若出现在非契约 v2 报告里也会被单独取出。P0 / 派单的拒绝文本包在原「not a valid decision JSON」纠错里；P0 拒绝不计数，其余（JSON 坏、派单校验）仍按原三次连拒 blocked；官方 CLI worker 对 `[contract]` 通知若不按 Done/Evidence 回报，走原 2 次纠错后同样 blocked；挂起项的 profile 推荐按决策时刻算，不在放行时重算；`writers` 只能对 `json_keys` 强制。

## 19. 多 fleet 并存 + 子 fleet 起停与窗口移交（2026-10-13 用户新要求，开发中）

设计稿：外部项目 `tasks/plans/cmux-fleet-nested-decompose-design-20261013.md`（§4–§8）。本节**替代 §8「每台机器只维护一套 fleet registry」**，并**在 §11 v2 支持矩阵里新增一格**；唯一 daemon（每套 fleet 一个）、slot/generation 标识、一页布局、task/service 协议不变。任务表 / 编号依赖 / 申请拆解的审批路径在另一块（feat/fleet-contracts），本节只有起停与移交的底层机制。

- **并存**：cmux 登记名 = `cmux-fleet` + 任务编号（主 `cmux-fleet`、子 `cmux-fleet:M0`、孙 `cmux-fleet:M0.1`），每套一份窗口登记（`fleet.manager_for(cfg)`）。`latest.json` 只记主 fleet；子 fleet run 目录在 `<父 run>/sub/<编号>`（重试为 `<编号>-attempt-<n>`，旧目录留档），状态记在其 `subfleet.json`。`status` 带出整棵树；主 fleet `stop` 先停子孙（每个子 daemon 再先停自己的子 fleet）、收回窗格，子 daemon 显式 stop 关自己的 Redis，崩掉的子 fleet 由父关 Redis，不留孤儿。`start --resume` 主 fleet：子 daemon 已不在的子 fleet 按原 socket/目录重启 Redis 后以 `daemon --resume` 拉起。
- **名字与标题**：`start --name` 设人能读的 fleet 名（缺省：任务目标前 24 字，再缺省 run token）；只给了 `--name` 时主工作区标题才变为 `Fleet <名>`，不给保持 `Fleet agents`。子工作区标题 `<主 fleet 名> › worker-1 › 子 fleet M0`，孙 `<主> › worker-1 › M0 › worker-2 › 子 fleet M0.1`，超 60 字时中间层缩成 `…`、首段和末两段保留。子工作区用 `reorder-workspace --after` 排在父工作区（及更早的兄弟）之后；控制标签所在工作区标题 `Fleet control · M0` 各不相同。父工作区原 worker 位置留占位标签 `worker-1 → 已委派：子 fleet M0（看侧栏「…」）`。子 fleet 的 worker 标签带前缀 `fx-M0/worker-1`（装饰，同 `fx-`），Redis label 仍是 `worker-1`。**不嵌套**：子 fleet 从不在父工作区里加分屏/标签页（占位标签除外）。
- **上限（程序硬挡，带原因）**：`max_depth` 2（主 fleet 是第 1 层，M0 第 2 层，M0.1 第 3 层）、`max_children` 3（每套 fleet 同时活着的子 fleet）、`max_agents_total` 12（全树；子 fleet 的 supervisor 是被委派的 worker，不重复计）、`child_workers_max` 3。来源：缺省 ← 主 config `limits` ← 主 roster YAML 的 `limits:`（每次检查现读，即热载；roster 其余键仍整份校验）。编号必须属于本 fleet（主层 `M<n>`，M0 下 `M0.<n>`）。
- **移交**（`subfleet.spawn_child`，只在 daemon tick 里、v2 事务之外执行；事务内调用方用 `subfleet.request` 只写请求文件、`subfleet.check` 同步拿裁决）：worker 必须 idle、READY、无在途投递、标签在。父：开占位标签放到 worker 前 → 备份 worker 的 AGENTS.md/CLAUDE.md → 建子 run/config/私有 Redis → 往子 bus 投子任务（目标 = 原指令 + 批准的拆法 + 父要的产出绝对路径；时限 = 父任务剩余时间）→ slot 标 `delegated`（`pane` 指占位标签，原窗格记 `delegated_pane`，`delegate_cfg` 指子 config）→ 从父登记 release 原窗格、写入子登记 → 拉起子 daemon。任一步失败回滚并带原因拒绝。子 daemon 首拍：起子 worker → `adopt`（`move-surface` 跨工作区）原窗格到子工作区左上 → 在原 worker 目录写监工规则 → 打字告知角色已变、要 `READY`。
- **钩子改投**：窗格里的 CLI 进程钩子是启动时钉死的（父 config、`worker-1`、代数），进程无法改。`claude_stop` / `notify` 进 v2 分支后先调 `subfleet.delegated_route`：父 Redis 里该 slot 是 `delegated` 且代数一致 → 改投子 config 的 `supervisor` 槽（第 0 代）。子 fleet 第一拍按原流程 `bridge_session → register_slot → startup(READY)` 绑定同一个 Claude 会话。代数不一致的迟到回调不改投。
- **收回**：子 fleet 写 `result.json` → 父 tick 记下结局后发 stop → 子 daemon 显式 stop 时对 adopt 的 supervisor **只 release 不 kill**（`handback.json`）、照常退役子 worker、关 Redis → 父见子 daemon 已退出：把原窗格 `adopt` 回占位标签前、关占位、恢复 worker 规则文件、关子工作区全部标签（历史页文件留在子 run 目录）、删子登记 → slot `starting`，打字告知已变回 worker、要 `READY`（同代、同会话，`register_slot` 幂等）。原会话没了（cmux 重启等）→ 父按 §17 的 `respawn` 在占位处起下一代。结局 `completed` 且父要的产出文件存在非空 → `returned`，否则 `failed`（重试另起 `-attempt-<n>`）；父任务仍在跑时给父监工一条 `[Sub-fleet M0 completed|failed]` 业务消息。父任务进入终态时，在跑的子 fleet 一并停掉收回。
- **续跑与失败**：主 fleet `--resume` 时 `delegated` 的 slot 不接回也不起下一代（窗格归子 fleet，子 fleet 随后续上）；整页丢失（`page=False` 重建）时带 `delegated` slot 的情形未处理。`spawn_child` 任一步失败（含 `start_redis` 的 `SystemExit`）都回滚：子 Redis 关掉、窗格还给父、slot 回 idle、记录 `failed`，父 daemon 不受影响。收回前先给窗格发一次 Escape，中止 adopt 会话可能还在跑的回合，免得它迟到的 Stop 被当成「变回 worker」的 READY——这是 §17「Escape 唯一被当作接受停止」之外的第二处，只限子 fleet 收回。`stop` CLI 立刻返回 accepted，daemon 随后每个子 fleet 最多等 180 秒。
- **v2 矩阵新增**：`claude` 监工 + `claude` worker 仅当 config 带 `adopt`（即子 fleet、task 模式）放行；普通 `claude` 监工照旧拒绝。adopt 的 supervisor 不换代（换代会把要还给父 fleet 的窗格换掉），上下文增长靠 Claude 自身压缩。子 fleet `--resume` 时 adopt 窗格像 worker 一样接回、不起下一代。
- **只支持官方 Claude worker 窗格**：Pi bridge 绑父 Redis socket 且断线永不重连（§17），拒绝并写明原因；Codex worker 不在 v2 矩阵内。
- **Claude worker 会话名**：`--name f<主 run token>[-<子 fleet 编号>]-<slot>`（如 `fa1b2c3d4-M0-worker-2`），每槽固定、换代沿用（契约规范附录 C）。cmux 标签仍由 fleet `rename-tab` 设为 slot.generation；**`rename-tab` 是否压过 Claude 用 `--name` 写的终端标题尚未在真实 cmux 上验证**——若被覆盖，按规范改用 `…-<slot>.<代数>` 并在接口契约 5-A 写「前缀匹配取最高代」。
- **顺带修复**：v2 daemon 主循环此前直接调 `RuntimeV2.tick`，`Fleet.tick`（§16 名单热加载、§17 任务终态写 `result.json`、本节子 fleet 收回）在真实 v2 daemon 里从未执行；改为 `self.f.tick()`（内部仍调 `RuntimeV2.tick`），加回归测试。

**验证**：`python3 -m pytest -q tests` 全套通过（新增 `tests/test_subfleet.py`：两套真私有 Redis + 两个进程内 daemon + 每套登记分开记归属的假 cmux + 真 `claude_stop`/`notify` 入口：移交→子 fleet READY 走父钩子→子监工决策派给子 worker→交卷→release→父收回同会话 READY→父监工收到结果；委派中不投递不判死；产出缺失判 failed 与重试编号；起子 Redis 失败回滚；收回前 Escape；会话丢失原位起下一代；主 resume 保留 delegated slot 并拉起子 fleet；上限四项与编号范围；Pi/忙槽拒绝；请求文件经 tick 执行；status 树；迟到代数不改投；树停无孤儿 Redis；主 resume 拉起子 fleet；子 resume 接回 adopt 窗格；父任务终态停子 fleet；v2 主循环跑 Fleet.tick 回归；cmux_manager 的 release/adopt/place_after/close_all 用假 `_run`）。

**未验收 / 已知缺口**：真实 cmux 上的跨工作区 `move-surface` 移交、`reorder-workspace`、标签标题与 `--name` 的先后，均未实测；一个正在跑的 Claude 会话在中途换掉 CLAUDE.md 后是否真按监工格式只回决策 JSON（提示词层面），未实测——这是本块最大的剩余风险；主 fleet 控制页「子 fleet 一览/跳转」与子 fleet「回到父 fleet」链接未做；委派中的 slot 仍可被父监工派活，消息排队到收回后再投；申请拆解→审批→`spawn_child` 的接线留给 contracts 合并后的下一块；子 fleet 任务的产出只做「文件存在非空」检查，正式验收交 contracts 块；独立复核、合并、部署均未做。

## 20. 申请拆解接线 + 子 fleet 接口通知 / 否决（2026-10-13 用户新要求，开发中）

设计稿：外部项目 `tasks/plans/cmux-fleet-nested-decompose-design-20261013.md` §3–§4、`cmux-fleet-interface-contract-spec-20261013.md` §3.0 / §4.2（子 fleet 内部接口「立即生效 + 通知顶级、顶级可 veto」，用户已认可）。本节把 §19 留下的「申请拆解 → 审批 → `spawn_child`」接上；唯一 daemon、slot/generation 标识、一页布局、task/service 协议、§16–§19 不变。

- **worker 申请**：报告照常四段（Done 写已做的部分），末尾恰一个 JSON 对象 `{"escalate": 原因, "proposal": [{id, instruction, output}], "workers": n}`（契约任务与 `interface_change` 同一个对象；`workers` 缺省 = 方案块数）。程序校验：方案编号互不相同且全是 `<任务编号>.<n>`，该任务是这个 worker 正在做的（契约：任务表 `to` = 它、状态 running/escalated；普通任务：本任务里带 `id` 派给它的项），再跑 `subfleet.check(..., ready=False)`（编号归属、层数、子 fleet 数、全树 agent 数、`child_workers_max`、槽位类型；不查 idle / 在途投递——worker 此刻正在结算这条报告，真正起子 fleet 时再查）。不过 → 原因同时发给 worker（「自己继续做」）和 supervisor；过 → 存 Redis `fx:escalations`，一条转给 supervisor（方案逐块列出 + 怎么答）。
- **普通任务的编号**：dispatch 项可带 `id`（`M<n>`，子 fleet 内 `M0.<n>`）；带了才写进 `fx:items` 并在派单首行加 `[M0]`，才能申请拆解。没有编号的申请被拒，原因写明「派单要带 id，或用契约任务」。`fx:escalations` / `fx:items` 在新 task 提交时清掉。
- **契约任务表**：申请那条报告不是交付，任务行记 `escalated`（不判 failed）；被拒时仍 `running` 并把拒绝消息记在该任务名下（下一条报告照常结掉它）。
- **supervisor 决定**：decision 新键 `escalations: [{task, decision: approve|revise|reject, workers?, note?}]`。格式错、任务没有待批申请、同一回复里又派了这个任务、approve 又给同一 worker 派单 → 走原纠错（计三次连拒）。带 `escalations` 的 RUNNING 不受「无人忙必须派单」限制。
  - `approve`：再查一次上限；**上限不过不算格式错**——申请保持待批，给 supervisor 一条原因（可减 worker 数、revise 或 reject）。过了 → `subfleet.request`（目标 = 该任务原指令 + 父 fleet 要核的接口路径 + note；拆法 = 批准的方案；契约任务带 `contracts: true`，子 fleet 先走自己的 P0 / P1；产出 = 首个 provides 接口的绝对路径）→ 任务行 `delegated`、attempts+1。下一拍 tick 执行请求：答复 `accepted` → 记 `delegated`；`rejected`（如 worker 恰好不空闲）→ 申请回到待批、行回 `escalated`、告诉 supervisor 原因。
  - `revise` / `reject`：给 worker 发一条带 `[M0]` 的消息（附 note），契约任务行回 `running` 并把这条记在 M0 名下；worker 改方案重报或自己做。supervisor 直接重派该编号也视为回答，待批申请作废。
- **子 fleet 交回**：`subfleet.collect` 在给 supervisor 的 `[Sub-fleet M0 completed|failed]` 后附任务表结果。契约任务：completed 时逐个按接口字面检查 M0 的 provides 文件 → 全过则接口 `ready`、通知消费方、M0 `done`、放行依赖它的挂起项；否则 / failed → M0 `failed`（原因带第几次）。普通任务只清申请记录。
- **子 fleet 内部接口**：子 fleet 骨架通过（全部 v1 生效）以及子 supervisor 批准的新增 / 变更，**立即生效**，并写 `<父 run>/subfleet-notices/`；中间层原样上交，顶级 tick 记下来源、给 supervisor 一条 `[sub-fleet interfaces] …`。顶级在 `interfaces[]` 里用 `decision: "veto"`（只对子 fleet 报来的接口有效，否则纠错）→ 写 `<来源 run>/subfleet-vetoes/`，子 fleet 下一拍把该接口标 `deprecated`（`deprecated_by: veto by the top supervisor`）、通知生产方 / 消费方和子 supervisor。**偏离规范 §4.1 第 4 行**：子 fleet 内部接口的兼容 / 破坏变更也按「立即生效 + 可否决」处理，不等顶级批准（跨 fleet 的审批通道没做）。
- **护栏（Fable r1 P2-01/02/03 + v2 首次真跑 `Fleet.tick`）**：`limits()` 接住 `yaml.YAMLError`；`process_requests` 单个请求任何异常都写 `rejected` 答复并删请求文件；没有 `config.json` 的子 fleet 记录（起到一半被杀）在 tick / stop / resume 里标 `failed` 跳过；子 daemon 不在、没有 `result.json`、父任务还在跑、且已过启动宽限（120 秒，按 `launched`/`created`，续跑重拉时更新 `launched`）→ 按 `cancelled` 收回，结局 `failed`。`Fleet.tick` 的名单热载 / 写 result.json / 同步 contracts.json / 子 fleet / 申请答复 / 接口通知 / 否决各段单独兜异常、写 `tick.error` 日志后继续；`BusError` 仍上抛（v2 事务不明须停）。`contracts.json` 同步在 v2 下每拍按 Redis 比对重写，事务里提前写出的未提交版本会在下一拍被纠正。`CONTRACTS.md` 与 AGENTS.md / CLAUDE.md 一起备份、收回时恢复。

**验证**：`python3 -m pytest -q tests` 全套通过（新增 `tests/test_escalate.py`：契约任务表 escalated / delegated / 答复拒绝回待批；revise / reject 回 worker 并结掉下一条报告；上限原因同时到 worker 与 supervisor；approve 上限不计次、未知任务与同回复派单计次；子 fleet 交回 → done/ready/放行依赖与 failed 带次数；普通任务要求 dispatch id；重派作废申请；tick 各段异常不杀 daemon、BusError 上抛；坏 roster 保留上限；真两套 v2 fleet：派 M0 → worker 报告带 escalate → supervisor approve → tick 起子 fleet → 子 fleet 目标含原指令与方案 → 完成收回 → 申请清除、父 supervisor 收到 `[Sub-fleet M0 completed]`；坏请求只答复不复崩；无 config 记录标 failed；子 daemon 消失按失败收回；子接口通知上交与 veto 落到子 fleet；中间层上交）。

**已知缺口（P2）**：approve 后到 tick 之间若有契约通知排给该 worker，起子 fleet 会因「不空闲」被拒，回到待批（不自动重试）；`escalate` 的 Pi worker 路径未接（Pi 窗格本就不能移交，`check` 拒）；veto 不级联到依赖该接口的任务（不标 stale，靠子 supervisor 收到通知后重新规划）；通知 / 否决文件在 v2 事务内写出，事务若重放可能重复通知；子 fleet 内部接口的兼容 / 破坏变更不等顶级批准（见上）；父 supervisor 在 `delegated` 期间可把同一编号重派给别的 worker（不拦）；契约父任务 → 契约子 fleet 没有进程内端到端测试（只验了请求带 `contracts: true`；子 fleet 提交契约任务、把 supervisor 版 CONTRACTS.md 写进被移交 worker 的目录、收回时恢复 worker 版，均未跑过）；中间层 fleet 把孙 fleet 的接口通知原样上交，**不给自己的 supervisor 抄送**（偏离规范 §4.2「中间 supervisor 只收抄送」）；`tick.error` 对持续故障每拍记一条、不限频；worker.md 写死「workers 1–3」，实际上限是可热载的 `child_workers_max`；真实 cmux / Claude 会话上的整条链未实测；独立复核、合并、部署均未做。

## 21. 同伴通知：worker 之间直发接口变更（2026-10-13 用户新要求，开发中）

规范：外部项目 `tasks/plans/cmux-fleet-interface-contract-spec-20261013.md` §4.3–§4.4、附录 B §5-A / §5-B、附录 C「同伴通知」两节。用户口径：Claude Code worker 之间走 Claude Code 自带的会话间消息；Pi / Codex 走 fleet Redis 信箱；任一端不是 Claude 走 broker；同伴之间只传事实，拿不准的一律在报告里问监工（`Question for supervisor:`）。本节只加通道，不改 §18 的审批权威（批准仍只走监工 decision JSON）；唯一 daemon、slot/generation 标识、一页布局、task/service 协议不变。代码在 `cmux_fleet/peer.py`，fleet.py / runtime_v2.py 只挂小钩子。

- **开关**：不另设 `peer_notices` 配置——同伴通知随契约任务（task spec `contracts: true`）开启，非契约任务的提示词、Redis、回合处理都不变；broker 在非契约任务里拒绝（`peer: interface contracts are off for this task`）。规范附录 C「`peer_notices: true` 时 CLI 既非 claude 也无 broker → 启动拒绝」因此不需要：broker 对所有 CLI 都可用。
- **提示词**：`CONTRACTS.md`（及任务期间新拉起一代的 AGENTS.md）在 §18 那一节后追加第 5 节，按该 worker 当前 CLI（`agent_cli(runtime_cfg)`，含 profile 覆盖）二选一：Claude 用 `agents/contracts_peer_claude.md`（5-A：`ToolSearch select:SendMessage,ListAgents` → `ListAgents` 按 `{{FLEET_PEER_PREFIX}}` 过滤 → `SendMessage`；对非 Claude 消费方改用 broker），Pi / Codex 用 `contracts_peer_broker.md`（5-B），**Pi / Codex 的提示词里不出现 `SendMessage` / `ListAgents` / 前缀**。监工追加 `contracts_peer_supervisor.md`（说明 `[peer CC]` / unsolicited / `[peer violation]` 行不需要它批）。`{{FLEET_PEER}}` = `python fleet.py peer --config <cfg> --from <slot>`，发送方钉在命令里（与 `{{FLEET_CHROME}}` 同等信任级别：沙盒里的 agent 能伪造 `--from`，daemon 只验它是本 fleet 在役 worker）。前缀 `{{FLEET_PEER_PREFIX}}` = `f<根 run token>-`。
- **`consumers_runtime`**：`contracts.json` 每个消费方填 `cli`（该 worker 当前 CLI）与 `session`（Claude 为 `claude_session_name`，即 §19 的 `--name`，每槽固定、换代沿用；其余为 `null`）。tick 每拍按文本差重写，所以换代换 CLI、名单热加载后下一拍即刷新。
- **broker `peer`**：CLI `fleet.py peer --from --to <worker|consumers:IF-x> --iface IF-x --text-file <自己目录内文件>`，走 `request()` 同一条事件路径（v1 `Fleet.handle`、v2 `runtime_v2.handle` 新 kind `peer`；与 chrome 不同，它的副作用只有 Redis 写，可进 v2 事务）。校验按附录 C，报错原文：`--to` 不是本 fleet 在役 worker（槽名 / label / Claude 会话名均可，不能是自己或监工）→ `peer: --to must be a worker in this fleet tree or consumers:<IF-id>`；接口未登记且首行不是 `0 -> 1 | new | UNAPPROVED` → `peer: IF-x is not registered`；超 600 字、首行不合固定格式、首行接口不等于 `--iface`、正文含 `[Supervisor instruction …]` 头 → `peer: notice must be <= 600 chars and start with the fixed first line`；`READY` 非生产方 → `peer: only the producer may announce READY`；每个发送方每小时（滚动 3600 秒）第 21 条 → `peer: rate limit 20/hour reached`。新增三句：`peer: --from must be a live worker of this fleet`、`peer: --text-file must be a file in your own directory`、`peer: no consumer of IF-x has a worker yet`（`consumers:` 展开后没有已派的消费方）。合格件以 `[peer] <原文>` + 一句「按 CONTRACTS.md 处理、按 AGENTS.md 格式回报」进目标 v2 信箱（`contract_queue`，回报归属记空，所以通知回合不结任何任务），目标下一个空闲回合取。
- **抄送与转给监工的方式**：`[peer CC] <发送方> -> <目标>:` + 原文、unsolicited 回合的 `Peer notices:` / `[worker question]` 行、`[peer violation]`，都先进 Redis 列表 `fx:peer:notes`，**追加在监工下一条业务消息末尾**（同 §16 `[Roster change]`），不单独发一条：单独一条会逼监工回一个无派活的 RUNNING，在没有 worker 忙时按原规则算错。代价：抄送要等到下一条业务消息（契约任务里通常就是目标 worker 对 `[peer]` 的那份回报）。
- **日志**：`contracts.log.jsonl` 追加 `peer`（发送方、目标、接口、级别、状态、原文）、`peer.rejected`（含报错与原文）、`peer_notices`（报告里的行，SendMessage 线唯一留痕）、`unsolicited`、`peer.violation`。
- **unsolicited 回合**：Claude worker 被同伴 `SendMessage` 唤醒（或操作员手打）的回合不对应任何在途投递。原有处理已是「不结算、只审计 `turn.ignored`」；本节在 v2 `tui_turn` 的两条 ignored 分支（无在途投递 / 回合不是由打进去的投递启动）给 daemon 发 `unsolicited` 事件（不等回复），daemon 核对 run / 代数 / 会话后记 `turn.unsolicited`，契约任务里写日志并把 `Peer notices:` / `Question for supervisor:` 行转监工；不动 lease、不弹回报归属、不计纠错、不续租、不排任何消息，所以不触发「空闲必派单」。v1 Claude 在 `on_turn` 的 duplicate 分支做同样处理。同一 Stop 重复触发：与 `fx:replies:<slot>` 最后一条（v1 原文 / v2 结构化值）相同 → 记 `turn.duplicate`，不再转一次。
- **违规**：报告或 unsolicited 回合的 `Peer notices:` 行里 `from <x>` 的 x 既不以本树前缀开头、也不是本 fleet 的 worker 名 / label → 写 `peer.violation` 并给监工 `[peer violation]`。SendMessage 本身程序拦不住，靠两头提示词。

**验证**：`python3 -m pytest -q tests` 全套通过（新增 `tests/test_peer.py`：按 CLI 注入两套第 5 节且 Pi / Codex 文本无 `SendMessage`；`consumers_runtime.session` 随 CLI / 换代刷新；broker 每条拒绝原文 + 日志；投递进信箱 + 回报归属记空 + 抄送随下一条业务消息 + 只抽一次；READY / 新接口 / 会话名目标；限速按发送方、滚动一小时；违规行；v2 真私有 Redis + 真 `peer` CLI + 真 Claude Stop 钩子：`[peer]` 打进 Claude worker、被同伴唤醒的回合记 unsolicited 且不结算在途投递、之后在途投递正常结算、重复 Stop 不重转；v1 Claude 同样）。

**已知缺口（P2）**：跨 fleet 投递（目标在同树另一套 fleet）直接拒绝并请在报告里说明，抄送也只到本 fleet 监工、不沿父链到顶级——规范要根 daemon 持有全树路由，§18 契约本身还只到单 fleet，这块随子 fleet 契约一起做；`[peer]` 投给官方 CLI worker 后若它不按 Done/Evidence 回报，走原 2 次纠错后 blocked（同 §18 `[contract]` 通知）；daemon 在 Claude worker 被同伴唤醒、Redis 里仍是 idle 时可能把下一条投递打进它的输入框，Claude 把打进去的文字排到当前回合之后，该回合按「不是由投递启动」记 unsolicited，若 Claude 把两者并成一个回合则会被当成那条投递的回报（未实测）；Claude 收到 `SendMessage` 时在 transcript 里的具体形态、`--name` 与 `ListAgents` 在真实 2.1.x 上的可见性，均未在本块实测；`--from` 可被沙盒内 agent 伪造（同 chrome）。违规检测按 `Peer notices:` 行里的 `from <x>` 取词，worker 若把 daemon 的 `[contract]` 通知写成「from daemon / from the supervisor」会误报 `[peer violation]`；`fx:peer:notes` / `fx:peer:sent:*` 随 AOF 进续跑，但带未抄送的 notes 或半用的限速窗口续跑未测。独立复核、合并、部署均未做。

## 22. 换代阈值按模型 / CLI 分别配置（2026-10-13 用户新要求）

用户原话：「handoff 对于 worker 窗口要根据模型不同去设置不同配置，像 claude code 是 1M 上下文就不该频繁的 handoff」。此前 Claude 的窗口固定取 `--claude-window`（200,000），换代比例全局一个 `--handoff-ratio`，1M 的 opus worker 在 120k 就换代。本节只改阈值来源；换代流程（§4）、交接单、代数标识不变。

- **判定**：投递前 `used >= ratio × window` 才换代（原规则）。`window`、`ratio` 各自独立解析，先命中者生效：
  1. roster 该 worker 条目的 `window` / `handoff_ratio`；
  2. roster 顶层 `handoff:` 表，键先 `<cli>:<model>`（模型串精确匹配）再 `<cli>`，值 `{window?, ratio?}`；
  3. `config.json` 的同名 `handoff:` 表（静态，启动时校验，不热载）；
  4. 内置：Claude 模型名含 `[1m]` / `opus` / `fable` / `sonnet`（小写子串）→ 1,000,000，其余 Claude 模型 → `--claude-window`；Codex 取 rollout 自报 `model_context_window`、Pi 取自报 ctx_window（"measured"），只有显式 `window` 才覆盖；
  5. `--handoff-ratio`。两个旗标保留为最低层缺省，向后兼容。
- **模型取当前运行值**：`runtime_cfg(name)`（含 profile 切换写入的 `runtime_launch.overrides`）里的 `worker_model`（Claude，监工同）/ `codex_model` / `pi_model`；监工走同一解析，只是没有 worker 条目。
- **热载**：随 §16 名单检测，下一次投递检查即生效，不重启；`roster.changed` 带 `handoff` 字段记新旧值（表 `table`、各 worker）。校验：ratio 是 (0,1) 内的数、window 是正整数（布尔不算）、表键 CLI 只能 claude/codex/pi、条目只许 window/ratio；任一不合法按 §16 整份拒收，原阈值保留。退役中 worker 保留原阈值直到退役。
- **显示**：每次解析写入 `fx:agent:<slot>` 的 `handoff_window`（或 `measured`）/ `handoff_ratio` / `handoff_source`（如 `window: built-in opus; ratio: --handoff-ratio`）；名单变更后立即刷新全体。`status` 的 `context` 带这三项，control 行显示 `handoff=<ratio>@<window>`。
- **已知边界**：内置表按子串判断，`opus` 一律当 1M 依据用户陈述（未在 API 层核实具体模型的真实窗口，若某个 opus 变体实为 200k 用 `claude:<model>` 覆盖）；模型串精确匹配，别名（`opus`）与全名（`claude-opus-…`）是不同键；adopt 的子 fleet 监工仍不换代（§19）。

**验证**：`python3 -m pytest -q tests` 全套通过（新增 `tests/test_handoff_limits.py`：内置表与旗标、五层顺序、opus 300k 不换代 / haiku 120k 换代、profile 运行模型、worker 覆盖 + 不重启热载改变阈值、坏值整份拒收保留原阈值、config 表校验、Codex 实测窗口不动除非显式覆盖、状态字段；`tests/test_roster.py` 新增坏值参数与 v2 热载进 `status`）。独立复核、合并、部署均未做。

**Claude 窗口与自压缩（2026-10-13 补，Fable r2）**：官方文档 `code.claude.com/docs/en/model-config.md`：裸 `opus`（Opus 5.5）、裸 `sonnet` 都是原生 1M，内置表因此把 `sonnet` 也算 1M（haiku 仍走 `--claude-window`）。Claude Code 自压缩约在 967K（CHANGELOG 2.1.247）；`autoCompactWindow` 只能压低、不能抬高压缩点，所以 fleet **不**设它——0.6 × 1M = 600K 的换代总先于自压缩。roster 把 window 调到接近 1M 时要自己留出余量（ratio × window 低于约 967K）。

## 23. 刹车与验收合同（2026-10-13 开发批次 E，`feat/brake-contract`；集成分支 integration/batch-20261013 时由 §20 顺延为 §23）

依据：总设计 v2 §1.3 #2 / #3、v1 §1.3、R2、R14、批 2.1 / 2.3。

### 验收合同第一层：`artifacts_frozen`

- task spec 加 `"contract": {"artifacts_frozen": true}`：调用方写的 `artifacts` 即验收合同。冻结时 artifacts 必须非空；监工 decision 里追加的 artifacts **一律忽略**，审计事件 `contract.append_ignored`（带被丢弃的条目）；监工要改只能 BLOCKED 说明理由，由调用方重提任务（R14：层 1 不留通道）。
- 冻结任务与 `contracts: true` 任务一样走扩展校验：`not_contains`、`json_keys`（新）、`cmd_exit`。`json_keys`：整个文件是 JSON 对象，或文件以一个 JSON 对象结尾（复核报告末行判决），所列键都要在。`cmd_exit` 只执行调用方冻结的 argv（无 shell，有超时）。
- 未冻结、非契约任务零变化：监工仍可追加，扩展键仍被忽略。`result.json` 与 `start --resume` 交底复检用同一套规则（`extended_checks`）。

### 刹车：预算卡 + 墙表（`cmux_fleet/brake.py`、`cmux_fleet/config/walls.yaml`）

- 缺省现金封顶（总设计 v2 ★3）：`pool: pool-a` 的卡没写 `max_usd` 时按白天 40 / 夜间 10 USD 每场景评估（夜间看 B 的 `fx:night.brake_card`）；调用方写了就用调用方的。quota.json 缺失仍是 unknown、不触发。
- 预算卡：task spec `budget`，或 service 的 `start --budget JSON`（写进 config `budget`）。字段 `pool / max_usd / max_week_pct / max_calls / max_rounds / max_hours / max_busy`。按池分：`pool-a` 盯墙 + 等价额度 `max_usd`（池读数相对开卡时基线的增量）；`pool-b` 盯墙 + 周额度 `max_week_pct`（池的绝对百分比）。错池字段直接拒。
- 计数：`calls` = 每次投递（v1 / v2 同一处计）；`rounds` = 被接受的监工决策；`hours` = 开卡起算（软线，task `hours` 仍是硬的 `budget_exhausted`）；`max_busy` 只能收紧 `--max-busy`。计数在 Redis `fx:brake`，新任务换新卡。
- 池读数来自额度调度员的 `~/.cmux-fleet/quota.json`（config `quota_file` 可改）：`{"pools": {"pool-a": {"cash_used_usd", "at"}, "pool-b": {"week_used_pct", "at"}}}`。缺失 / 读不出 / 超过 1 小时 = unknown：显示在 `brake.json`，**不触发**。
- 到线：pause（不投递、不轮换，全部可续）+ 审计 `brake.tripped` + `<run>/brake.json` + 可选 `brake_notify` argv（`start --brake-notify`，无 shell）。v2 下判定在一个事务里做，通知在事务外。
- 续跑：`cmux-fleet resume` 在预算线仍越线时被拒（不论暂停记录最后是谁写的：后来的墙、F 的 hold 改写暂停原因都不解锁），须 `resume --raise LIMIT=VALUE`（不能改池）；墙引起的暂停可直接 resume。新任务开新卡时清掉上一张卡的刹车暂停（操作员手动 pause 不清）。
- 墙表一张：账号级墙（usage / weekly / session limit、429）→ 换边或停，fleet 内即停（pause）；分池墙（"You've reached your Fable limit"、`/usage-credits`）→ 同 CLI 降一档（`same_cli_downgrade`，吸收自 us-stock-option `config/cli_tier_map.yaml`），没有下一档也停。来源：卡住（超 `stall_timeout`）时的屏幕尾部（v1 在 `on_stall`，v2 在 `Fleet.tick` 的刹车事务里、早于 v2 hold）；v1 另看短回合文本（≤400 字，长报告不判墙）。另有三路不等 `stall_timeout`：① 启动回的不是 READY 而是墙 → 槽位 `walled`（不判死、不 blocked）+ 可续暂停，账号级墙在 resume 后按原 launch 重起下一代；② v2 官方 CLI 的短回复（≤400 字，`tui_turn` 带 `wall_text`）与 Pi 的模型错误事件（`candidate {tool: model, error}`）→ 投递以 `walled` 提交（结果不采用、不转监工），原指令带「[Resumed after a usage wall]」只重投一次（重放的 settled 命中 `settle.late`）；③ 看门墙闸（见下）。分池墙在 worker 槽**实际执行**同 CLI 降一档：空闲边界由 `wall_tick` 用 `wall_rotate` 起下一代（launch 覆盖模型键，交接骨架、存屏、tab 原位、杀旧 tab、v2 `confirm_retired`），新一代 READY 后只解除墙自己的暂停（`fx:brake.paused_by` 比对；F hold / 操作员 pause / 预算越线一律保持暂停）；槽位已被别的换卡（B 夜间 / live profile）改过 → 降档作废（`brake.downgrade_superseded`）。监工槽与账号级墙只停不降档。
- v1 task 的 `pause` 现在也会停投递（原来只对 service / v2 生效）。

### E 先于 F：看门的墙闸（`Fleet.wall_gate`）

- 接口：`Fleet.wall_gate(slot, screen, source='watch') -> record | None`。看门（F，`watchdog.Watchdog.check`）每次抓屏后、在弹框白名单 / 未知弹框 hold / stalled 阶梯 / frozen 之前先调它。只看屏幕最后 `brake.GATE_TAIL`=25 行（`brake.screen_wall`）。
- 返回 record：墙已触发刹车（pause、`agent.wall`、审计 `brake.tripped`、`brake.json` source=watch、可选通知；v2 下在一个事务里）。看门本 tick 对该槽**一个键都不按、不 hold、不杀不换代**——墙菜单（如 `❯ 1. Yes, switch…`）永远不当弹框答。同一 agent 的墙已经刹住时再调只返回原记录，不二次触发。返回 None：无墙，看门照自己的表走。
- hold 出口（F 的 `turn_ended` → `release_held`）只把槽位放回 idle，**不解除暂停**；F 的 hold 会改写暂停原因，但刹车记录在 `fx:brake` 里独立保存，`resume` 每次都按预算卡复核，越线就拒。

