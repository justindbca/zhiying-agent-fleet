# cmux-fleet 设计基线

> 用户确认日期：2026-10-10。本文固定设计约束，不是某次演示的临时配置。
> 历史实现基础：`0c43b9c`；Pi/profile 增量来自 `feat/pi-agents`，Redis v2 基础模块已单独进入本地 `main@5f973d5`。
> **开发验证通过不等于已复核、已合并或已生产部署。** 旧实现验证状态见 §10；当前本地集成范围与复核见 [2026-10-12 报告](../reports/full-local-merge-20261012.md)。
> **2026-10-11 新要求：Redis 阻塞通信 + control 详情显示。** 目标设计见 §11 与[完整 v2 方案](../../us-stock-option/reports/cmux-fleet-redis-control-design-v2-20261011.md)。§11 明确覆盖下文旧文件投递/机械结果转述要求；**v2 基础模块已有实现，但 daemon/bridge/control 运行链尚未接入或验收**，不能把模块合并说成运行代码已升级。

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
- 私有 Redis 仅使用 `/tmp/cmux-fleet-<token>.sock`；不能回退 localhost:6379 或写生产 Redis。
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

## 11. Redis 通信与分层显示 v2（2026-10-11 新要求，待实施）

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

- Redis Streams 阻塞消费、确认与恢复，沿用唯一 daemon；私有 socket 不变，不能静默回退文件信箱/生产 Redis。
- pi 原生扩展路径优先；Claude/Codex 官方 CLI 的输入与显示能力单独验证。未经验证不宣称混合链已支持，不擅换 CLI/认证路径。
- v1/v2 按 run 显式区分，不热改在途实例。旧 §10 的 91 测试/演示不能证明 v2 通过。
- v2 必测：无消息文件/模型 read；ACK/心跳/超时卡零模型调用；control 详情可追溯；旧代/重复/崩溃恢复；慢 UI 不阻塞调度；人工输入、命名、布局、交接与旧接口回归。

## 12. 动态 agent profile 路由（2026-10-11，Phase 5 显式 live opt-in）

本节根据用户本轮授权替代 Phase 4 的“任何 live=true 都 unsupported”约束。Phase 1 registry/router/planner、Phase 2 task 接入、Phase 3 稳定 slot assignment、Phase 4 授权与元数据应用继续保留；Phase 5 在唯一 daemon 的原投递边界增加受控 respawn。默认仍是 **advisory + metadata**，不是第二套 dispatcher。本轮只授权本仓 dev 验证，不涉及交易生产系统，不重启已有 fleet。§11 Redis v2 运行链仍未接入，本轮继续使用现有队列/投递协议。

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
