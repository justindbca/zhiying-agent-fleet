# cmux-fleet 设计基线

> 用户确认日期：2026-10-10。本文固定设计约束，不是某次演示的临时配置。
> 历史实现基础：`0c43b9c`；Pi/profile 增量来自 `feat/pi-agents`，Redis v2 基础模块已单独进入本地 `main@5f973d5`。
> **开发验证通过不等于已复核、已合并或已生产部署。** 旧实现验证状态见 §10。
> **2026-10-11 新要求：Redis 阻塞通信 + control 详情显示。** 目标设计见 §11。§11 明确覆盖下文旧文件投递/机械结果转述要求；**v2 基础模块与 all-Pi daemon/bridge/control 已有集成候选，真实 Redis/SDK/TUI 的局部闭环已验证；完整验收仍未完成，见 §14**，不能把模块合并说成运行代码已升级。

## 1. 延续原设计，不另造框架

保留：**一个 supervisor + N 个 worker + 一个 daemon + 一套私有 Redis**。

- supervisor 负责判断、分派、查看结果，不代替 worker 执行任务。
- worker 执行被分派的工作；不同职责写在各自角色 prompt，不擅自改变 agent 身份。
- daemon 负责投递、状态、校验、超时、换代与历史记录；不依赖模型声称“成功”，不读屏幕判断回合结束。
- pi 是另一种 agent CLI 适配，不取代 daemon，不增加第二套派活或记忆/交接系统。
- 原 task/service 模式、命令行入口、请求/结果协议、worker 别名路由继续保留。

设计来源：原 cmux-fleet README/在役代码；业务侧原始需求（未随仓库公开）；用户后续关于同页布局、代数标识、官方 Claude CLI、control/history 同栏和手工派活的明确要求。

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
- **worker 槽名以 `-<数字>` 结尾（2026-10-28 用户定「所有的 worker，以方便识别名字标识后，也应该加上 -1 的标志」）**：按角色起名也要带编号，如 `proto-1`、`term-iface-1`、`reviewer-1`，代数读作 `proto-1` → `proto-1.1` → `proto-1.2`，与上表 `worker-1` → `worker-1.1` 同形。`roster.SLOT_SUFFIX_RE`（`.*-[0-9]+`）只在**新建槽**的地方查，错一条整份拒收，消息点名条目并给改法（`workers[1] (proto): … rename it proto-1`）：`start --roster` / `--kit-fleet` 名单行、`start --worker-names`（均在 provisioning 前）、§16 热载新增的行、`start --resume --roster` 新增的行、具名子 fleet（§29）**新声明或换了 `roster` 文件**的子名单里的 worker（父 `start` 时查全部条目）。**本 run 已有的名字一律不复查**：续跑（§17）、续跑后首拍重读名单、`fleet advisor on|off` 改名单、本 run 早已声明的子 fleet 再次 up，老名字照常工作。不受此条约束：监工 `supervisor`、顾问 `advisor`（显示名本就是 `supervisor-1` / `advisor` → `advisor-1.1`）、子 fleet 名 `subfleets[].name`（是 fleet 名——会话名 `cmux-fleet:<名字>`、`fx:subfleet:<名字>`、`sub/<名字>/`——不是槽、没有代数；它的 `supervisor_slot` 是父 worker，已随父名单查过）。夜间名单与日间同名（`same_slots`），profile / live 切换不改名，§19 的 `M<n>` 子 fleet 用 `worker-1..N`，都不新增名字。已知缺口：子 fleet 处于 down 期间被改过的子名单，下次 up 时直接按新文件起、不再查名字（up 中改的由子 daemon 自己的 §16 热载查）。
- 角色 prompt、CLI 类型、模型与 agent 身份是不同概念。改变模型不意味着改名。
- 重新创建整个 fleet 是新 run，不假装继承旧 run 的代数；旧 run 文件保留，不冒称仍属于新历史页。

## 4. 交接与历史沿用原实现

1. daemon 在空闲投递边界读取用量（§32 起：回合结束、空闲且无待投消息时也读一次，不必等下一条消息）；回合中途（投递边界 / 空闲之外）由 §34 的探针另外判，不等空闲。
2. 达到 `handoff_ratio` 后，请旧代填写三节交接草稿。
3. 把模型内容并入机器骨架；模型未写或失败时用骨架，不因缺交接文本阻塞换代。
4. 保存旧屏幕和交接单；在原 pane/tab 位置启动新一代。
5. 新一代只读交接并回 `READY`，然后才接收等待中的任务。
6. 退役会话关闭，历史页刷新，迟到回调按 generation 丢弃。

- 正常默认阈值 **0.6（60%）**。1%/5% 仅作显式换代测试，结束后恢复正常值。
- 新会话仅 READY 就已超阈值时，不反复重开；保留原 `rotate.impossible` 防循环机制。
- pi 自动压缩只作长回合保险，不替换 fleet 的跨会话交接和历史；回合中途的强制换代见 §34。
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
| supervisor | pi（v2 缺省，`openai-codex/gpt-6-astra` high，见 §11「Codex/FlashNext 走 pi」小节）/ 原 Codex（可选）；官方 Claude（仅 v2，见 §11 2026-10-13 Claude 监工小节） | Codex notify / pi agent_settled / Claude Stop |
| worker | 原 Codex / 官方 Claude Code CLI / pi | notify / Claude Stop / agent_settled |

用户明确要求：**Claude 必须由 fleet 直接启动官方 `claude` CLI。**

- 不用 pi 代理 Claude 订阅接口；不把 Claude Code 的订阅/OAuth 凭证交给 pi；不伪装官方客户端。
- 本地 Flash Next 可经 pi 的 OpenAI-compatible provider 使用；不得因模型不可用而暗中换云模型。
- 每个 pi agent 必须解析出模型：自己的值（监工 `--supervisor-pi-model` / 组合表 `default.supervisor_pi_model`，worker 名单行 `pi_model`）或兜底 `--pi-model`，缺则拒绝；CLI/模型/角色可配置，但没有理由擅自替换用户已选的执行链。
- Codex 模型（ChatGPT 订阅，pi provider `openai-codex`）与 FlashNext 经 pi 使用（2026-10-13 用户定，§11 对应小节）；官方 Codex CLI 保留为可选。
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

- 常规用法见 [README](../README.md)；各机器路径和在役版本由部署方的运行手册维护，不在本设计写死机器路径。
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

上表的真机证据文件（窗口/交接测试、命名与输入闭环、control/history 同栏确认）保存在部署方项目中，未随仓库公开。

以后修改先对照本文；用户新要求须明确记录替代了哪条约束，再修改实现与回归。不能只改演示、只改截图，或把旧报告的临时状态当作新设计。

## 配置文件（2026-10-27 用户定：都合并到一个配置文件里）

- 只有一个配置文件 `cmux_fleet/config/fleet.yaml`，一张表一个块：`cli_pairs`（监工 × worker 组合与真跑冒烟记录）、`lifecycle_modes`（生命周期模式）、`walls`（撞墙文案 / 同壳降档 / 别名）、`watchdog_prompts`（看门可答的弹框 / 必须扣住的标记）、`capability_matrix`（顾问的能力实验表）、`models`（模型卡，按卡 id）、`profiles`（档案卡，按 id）、`accounts`（号池，仓库里为空）、`tools`（工具池，仓库里为空）。读取统一走 `cmux_fleet/fleet_config.py`。
- 部署方私有文件同格式：`$CMUX_FLEET_CONFIG`，否则 `~/.cmux-fleet/fleet.yaml`，不进仓库。私有文件里写了的块**整块替换**仓库里的同名块（不做深合并）；只许写 `accounts / tools / walls / lifecycle_modes`，写了别的块直接报错——`cli_pairs` 是真跑冒烟闸、`watchdog_prompts` 是弹框白名单，模型卡与能力表都只能在仓库里经复核改。
- 显式路径（`--prompt-table`、任务的 `agent_registry` 目录、测试里的临时表）照旧可用：文件顶层全是块名就按块读，否则整份当那一张表；档案卡目录仍按逐文件读取。热载语义不变：按两份文件的修改时间与大小判断是否重读。
- 角色提示词（`cmux_fleet/agents/*.md`）是提示词不是配置，仍是单独文件。

## 11. Redis 通信与分层显示 v2（2026-10-11 新要求，开发接线待验收）

本节为目标约束，不是上线声明。

### 替代关系

- 覆盖 §7 的文件路径关联方式：正文、逻辑消息 ID、delivery ID、代数/会话/执行授权均在 Redis；agent 适配层阻塞收件并直接注入业务上下文，不让模型读 msg Markdown 或轮询 Redis。
- 覆盖 §5/§9 中“每个 operator result 都交 supervisor 模型转述”的旧行为：简单结果、超时、失败由代码生成 UI-only 卡，零机械转述回合；只有继续判断/计划/总结才创建业务 follow-up。
- 覆盖 pi“必须最终打印协议 JSON”的旧显示方式：拟用结构化结束工具收集候选，保持原 JSON schema/外部协议；agent_settled 后再由 daemon 验收。UI renderer 显示摘要，control 展示完整参数/结果。
- 保留 generation/投递验证、错误拒绝、外部 request/result、原 task/service 与换代规则；增强 PEL/租约/fence/幂等恢复，不宣称副作用 exactly-once。

### 显示边界

- supervisor：用户指令、派工摘要、业务判断、结论与明确故障提示。
- worker：当前任务、实际工作过程、业务结果与明确故障提示。
- control：完整通信信封/JSON、ID、时间戳、ACK、路由、重试、心跳、校验与错误链；支持按任务/槽位查详情。
- 运输信息不进入模型上下文；必要业务正文可入上下文而不在窗口重复铺全文。**例外（2026-10-14 用户定）**：官方 Codex 经 `codex queue` 投递时，正文末尾保留一行不透明投递编号 `(fleet delivery fx-<16hex>)`，只用于「只结算本代已收投递」的对号（`codex_queue.bound`），不带业务含义。UI-only 用非模型 session entry，不能误用隐藏 custom message 让通知进入上下文。
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

用户 2026-10-12 拍板：先让 v2 支持 **Codex supervisor + 官方 Claude Code workers**，之后用它开 6 个 Claude worker 并行开发，监工 Codex `gpt-6-astra`。（2026-10-13 起缺省组合改为 pi 监工 + 官方 Claude worker，本组合保留为可选，见下文「Codex/FlashNext 走 pi」小节。）本小节替代上文与 §14 “v2 只允许 all-Pi” 的约束；三条禁令（不回退文件信箱、不代理 Claude、不改 headless CLI）、唯一 daemon、task/service 外形、slot/generation 标识全部不变。

| 项 | 约束 |
|---|---|
| 支持矩阵 | （已被 `cmux_fleet/config/fleet.yaml#cli_pairs` 与下文 2026-10-13「Codex/FlashNext 走 pi」小节的矩阵替代，本格为当日原始记录）v2 只放行 `pi+pi`（task/service）与 `codex+claude`（**仅 task**）；2026-10-13 起另放行 `pi+claude`（task/service，即缺省组合 pi Astra 监工 + 官方 Claude worker，见下文「Codex/FlashNext 走 pi」小节矩阵）。Claude 监工、Codex worker、Pi 与官方 CLI 混配、`codex+claude` service 模式在 provisioning/profile 退役前带原因拒绝（`runtime_v2.v2_cli_problem`，inventory 同口径）；仍须 `--allow-development-v2`。 |
| 派活 | 原 `task` / `request` 命令入 v2 events Stream，原 `submit/on_supervisor` 判断与产物验收不变；监工最终回复仍是 AGENTS.md 决策 JSON。Pi 专用的 `fleet_decision/fleet_result` 改写与 `_v2` 角色文件只用于 Pi 槽，官方 CLI 槽保留原文。 |
| 投递 | 唯一 daemon 在原 idle 投递 / 交接边界 `offer` 后，以 `tui-<slot>-g<N>` 身份 `receive`，按该槽本代的 CLI 分路交出 `body.text`（业务正文），都不写 `.inbox`/消息 Markdown（`RuntimeV2.type_into`，启动提醒同路由）：① **Claude worker**（钩子通道，`claude_hook.enabled`：Claude 且非监工，缺省开，run 配置 `claude_hook_delivery: false` 可关）：正文写入私有 Redis 钩子信箱 `<prefix>:hook:<slot>:g<N>:mailbox`，由该会话阻塞等待的 Stop 钩子以 `{"decision":"block","reason":正文}` 交付，不往输入框打字；没有钩子在等超过 10 秒（`GRACE`）时由 asyncRewake 兜底钩子以 exit 2 唤醒交付；另一个回合进行中兜底不取件（`TurnWatch`），操作员排队的输入先让路。首条启动提示仍走 CLI 参数；子 fleet 归还时钩子 worker 起下一代，不往旧会话打字。② **官方 Codex 监工 / agent**：`codex queue --thread <本代会话> --message=<正文>` 排进该 TUI 自己的队列，不打字；正文末尾附一行投递编号 `(fleet delivery fx-<16hex>)`（每次投递唯一，先写入 Redis `cq_*` 记录再调 CLI），这是唯一进入模型上下文的运输信息（见上文例外）。CLI 失败或结果不确定即 hold，从不回退打字；已排队未结算的条目不撤（Codex 无官方撤回），同槽有未结算条目时拒绝再排。③ **pi 槽**不变（扩展 `pi.sendMessage`）。打字（cmux 粘贴并回车；Claude bracketed，实测非 bracketed 多行会停在输入框或被拆成两条提示）只剩：非钩子 Claude 槽（Claude 监工、关了钩子通道的 Claude worker）和 subfleet 接管时打给被收编窗格的角色切换提示（R1-09）。除上述投递编号外不带 message/delivery/fence 等运输 ID。交付失败即 hold（fail closed）。daemon 作为接收方在 tab 存在时续租，停滞/超时/tab 消失仍按原规则 hold。 |
| 回收 | Claude Stop / Codex notify 的 v2 分支（`fleet.tui_turn`）：READY 回合发 `bridge_session` → `register_slot` → `startup`（READY 闸）；之后只在本代租约为 `received`、且本回合确由该投递发起时，把最终文本映射为候选（监工决策 JSON；worker Done/Evidence/Not done/Blocked 文本或同键 JSON，标题冒号前可带括号限定语如 `Evidence (with X):`）发 `settled`。「由该投递发起」按通道判：Claude 钩子 worker = 本会话钩子写下的 bind（message_id / delivery_id / session）与租约一致，且本回合最新起始输入是该投递的 `Stop hook feedback:` isMeta 行或 rewake 行（操作员原样重打正文不算）；非钩子 Claude 槽 = 转录里的用户提示；官方 Codex = rollout 证明：同会话、同代、`turn_trigger == "queue"`、恰好一条 UserMessage 且全文等于本次排队文本（含投递编号），并原子标记已结算（`codex_queue.bound` / `mark_bound`），不看 notify 的 `input-messages`。人工回合、重复回调、旧代回调不结算；daemon 仍按原 settle 校验身份/租约/fence/schema。解析不出候选不伪造成功：监工走原三次纠错；官方 CLI 的 task worker 该次投递记 failed，经原投递路径（新 delivery/fence、同 generation）排一条“只按 Done/Evidence/Not done/Blocked 格式重发报告”的纠错，纠错排在该槽待投队列**队首**（2026-10-28 改：此前排队尾，监工已排的后续消息先投，它们的回复把 2 次额度耗光、纠错从未送达就判 blocked），最多 2 次（`TUI_REPORT_RETRIES`），仍不合格才判 task blocked；合格后计数清零。Pi / opencode worker 同样走这 2 次纠错（2026-10-28 改，缺口 G1：10-27 飞书 run 里 reviewer 换代后只回纯文字「Done」、没调 fleet_result，原规则直接判 task blocked 并扣下两个在干活的分析员），纠错文案改为「只调用一次 fleet_result 重交本单回报」。 |
| 钩子超时 | Claude worker（钩子通道）：Stop 钩子超时 = `claude_hook_timeout`（缺省 86400 秒），到期前留 `MARGIN`（120 秒）交一条 keepalive 回合（不结算、不上报）；settings `env` 设 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0`（连续投递是连续的 Stop block）；同时挂 asyncRewake 兜底钩子和 StopFailure 钩子（API 错误结束回合时只有 StopFailure：只审计并写回合结束标记，在途投递走看门狗停滞路径）。非钩子 Claude 槽（监工）：v2 Stop hook 超时 = `profile_ready_timeout`+30 秒，READY 回合等 daemon 回复最长 `profile_ready_timeout`（daemon 仍在逐个开 tab 时也不丢 READY）；普通回合结算最多等 30 秒，事件已持久不因等待超时丢失。v1 仍 30 秒。被忽略/过期回合写 `turn.ignored` / `turn.stale` 审计。`profile_ready_timeout` 目前无启动旗标。 |

**性质说明**：把正文打进官方交互 TUI 不是三条禁令中的任何一条（不是文件信箱、不经 pi、不是 headless）。§11 交付记录“投递不用 cmux paste”写在 Pi 实现段、v2 方案验收清单写作“pi 不再靠 cmux paste 收件”，本小节按 Pi 作用域理解；v2 方案 §8 给官方 Claude 列的候选是“prompt-submit hook 注入正文、终端只留短标题”，本次未采用、未实测。若用户要把“不用 cmux paste”扩到全体 v2，这条链须改走该候选或退回阻塞项。（2026-10-14 更新：Claude worker 已改走 Stop 钩子信箱、官方 Codex 已改走 `codex queue`，见上表「投递」行；Claude 监工仍打字。）

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

### 2026-10-13 用户新要求：Codex / FlashNext 一律走 pi，Claude 只走官方 CLI（`feat/codex-via-pi`）

用户原话：「除了 claude code，其他的包括 qwen flash next 和 codex 都用 pi」「我们 codex 走 pi 是最开始就定了，后来被冲掉了」。本小节替代 2026-10-12 小节把组合表 `default` 设为官方 Codex 监工的做法（落实本节「pi 原生扩展路径优先」）；三条禁令、唯一 daemon、task/service 外形、slot/generation 标识、一页布局、交接流程全部不变。

| 项 | 约束 |
|---|---|
| CLI 边界 | Claude agent 只由 fleet 直接起官方 `claude` CLI；**禁止** pi 接 Claude 订阅或拿 Claude 凭证（§6）。Codex 模型经 pi（ChatGPT 订阅登录，provider `openai-codex`），FlashNext 经 pi 的 OpenAI-compatible provider。官方 Codex CLI 路径保留为可选，不删。 |
| 缺省组合 | `fleet.yaml#cli_pairs` `default = {supervisor_cli: pi, worker_cli: claude, mode: task, supervisor_pi_model: openai-codex/gpt-6-astra, supervisor_pi_thinking: high}`。`start` 未给 `--supervisor-cli` 时取默认 CLI；监工为 pi 时，`--supervisor-pi-model` / `--supervisor-pi-thinking` 与显式 `--pi-model` / `--pi-thinking` 都没给的那一项才取这两个键（手敲旗标永不被组合表缺省盖过）。**显式 `--supervisor-cli pi` 不读这两个键**（`fleet.resolve_supervisor`），模型须来自 `--supervisor-pi-model` 或 `--pi-model`。worker 为 Claude 时不需要 `--pi-model`。`--allow-unsandboxed-pi` 仍须显式给（确认项，不自动授予）。`default` 只认 `supervisor_cli / worker_cli / mode / supervisor_profile（仅 Codex 监工）/ supervisor_pi_model / supervisor_pi_thinking`，其它键加载即报错。 |
| 分 agent 模型 | 解析顺序（高 → 低）：监工 = `--supervisor-pi-model`（或仅缺省监工时的旧配置档映射）> 显式 `--pi-model` >（仅缺省监工）`default.supervisor_pi_model`；worker = live 档案卡 `model` > 名单 `pi_model` > `--pi-model`；thinking 分开按同一规则解析：监工 `--supervisor-pi-thinking`（或映射）> 显式 `--pi-thinking` > `default.supervisor_pi_thinking`（仅缺省监工）> `off`，worker 档案卡 > 名单 `pi_thinking` > `--pi-thinking` > `off`（`--pi-thinking` 旗标缺省为未设，以便区分显式值；config 未设时仍记 `off`）。夜间组合因此必须显式给 `--supervisor-pi-model`。任一 pi agent 无模型 → `start` 拒绝并列出槽名。`--supervisor-pi-model` / `--supervisor-pi-thinking` 设监工；名单 worker 行可选 `pi_model` / `pi_thinking`（仅 run 的 worker_cli 为 pi；不能与 `profile` 同行，违者整份拒收，§16 语义）。未设的沿用 run 级 `--pi-model` / `--pi-thinking`（兜底，原含义不变）。config 记 `pi_models` / `pi_thinkings`（按槽名）。名单热载改某行 `pi_model` / `pi_thinking` → 该槽空闲后在原槽名起下一代（同名单换卡路径）。§12 模型卡计票（`model_usage`：`Usage.from_agents` / `default_problem` / `check_slots`）按**每槽自己解析出的 pi 模型**计（`pi_adapter.for_agent`），名单 `pi_model` 行同样受该模型卡 `max_concurrent` 限制（start 拒绝、热载整份拒收）。档案卡的 `model` 仍优先于名单 / 旗标。 |
| 夜间组合 | pi Astra 监工 + pi FlashNext worker：用已 verified 的 `pi+pi` 行加分 agent 模型实现（`--supervisor-pi-model openai-codex/gpt-6-astra --supervisor-pi-thinking high --pi-model flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP`，或名单行 `pi_model`），组合表不另加 schema。§25 夜间切换不变。 |
| 老参数 | 显式 `--supervisor-cli codex [--supervisor-profile X]` 照旧起官方 Codex TUI。未给 `--supervisor-cli`、缺省监工为 pi 时：`--supervisor-profile fleet-astra`（或 worker 非 codex 时的 `--profile fleet-astra`）映射为 pi 监工 `openai-codex/gpt-6-astra` + high，并向 stderr 打印说明；其它配置档名报错并给出两种新用法；映射名与不同的 `--supervisor-pi-model` 同给 → 报错。显式 `--supervisor-cli pi` + `--supervisor-profile X` → 报错（不映射）。非 pi 监工带 `--supervisor-pi-model/-thinking` → 报错（不忽略）。**不许静默起官方 Codex TUI。** |

**支持矩阵（v2，以 `cmux_fleet/config/fleet.yaml#cli_pairs` 为准；本表为 2026-10-13 快照）**：

| supervisor ↓ ／ worker → | pi | claude（官方 CLI） | codex（官方 CLI） |
|---|---|---|---|
| pi | task / service：verified（含夜间 Astra + FlashNext） | task / service：**verified**（缺省组合；真实 `scripts/smoke_pair.sh pi claude task|service` 4/4：`fleet-20261013-115145-828661b9` / `fleet-20261013-115347-7e61f9af`，2026-10-13，未提交分支树） | 不在表中 → 拒绝 |
| codex（官方，可选） | task / service：verified | task / service：verified | task：unverified；service 不在表中 → 拒绝 |
| claude（官方） | task / service：verified | task：verified（另 §19 adopt 子 fleet）；service 不在表中 → 拒绝 | 不在表中 → 拒绝 |

unverified 只能 `--lifecycle smoke` 起。`pi+claude` 两行已凭 worker-2 真冒烟（S1d / S2，脚本 exit 0、4/4 PASS）改为 `verified` 并填 `verified_by`，缺省组合可按任意 `--lifecycle` 启动；冒烟跑在未提交的分支树上，提交后的独立复核仍须确认。

**已实测（未提交分支树，2026-10-13）**：缺省 start（不带 CLI / 模型旗标，隔离 `CMUX_FLEET_HOME` / `CMUX_FLEET_STATE`，`--lifecycle smoke`）起「pi Astra 监工 + 2 Claude worker」，一个 task 内 6 次顺序派活、6 条 worker 回执全部 committed、一次监工换代 `supervisor-1` → `supervisor-1.1`（run `fleet-20261013-115851-c4278e1b`，worker-2 S4）；另以 `--lifecycle service`（既有生命周期，缺省 CLI / 模型不变）做 5 次独立提交（`fleet.py request`，各自 id、各自 committed 回执与 ok 结果），第 1、2 次之间一次真实监工换代，supervisor-1.1 路由第 2–5 次（run `fleet-20261013-131709-73c31fbf`，worker-1 S4-svc，换代后页脚 `(openai-codex) gpt-6-astra • high`，`fleet stop` 后工作区 closed、无残留进程）；监工模型由 pi 页脚 `… 1.0%/400k (auto)  gpt-6-astra •` 与 pi 会话文件 `model_change openai-codex/gpt-6-astra`、`thinking high` 证明。pi 改写自身进程标题，`ps` 只见 `pi`，进程参数不可得。**窗口标题不改**，保持 §3 的 `supervisor-1` label 基线（2026-10-13 23:50 用户裁定）。

夜间 Astra + FlashNext：`scripts/smoke_pair.sh` 改为在 smoke 生命周期关闭 run 后从其 Redis 持久化数据读最终 UI 卡 / 审计（原先的旧快照会漏掉与 COMPLETED 同刻写入的结果卡）后，独立复跑 `fleet-20261013-130107-a4f4e9a2` 脚本 exit 0、4/4 PASS、审计 error 0、无 `execution.held`，页脚实证监工 `gpt-6-astra • high`、worker `(flashnext) Qwen3.8-Flash-Next-…`（未提交分支树，2026-10-13）。

**未验收**：codex / claude 监工组合未用新版冒烟脚本重跑；提交后的独立复核尚未做。合并、测试、入队由 fleet 可信程序按 deliver 处理，worker 不自行合并；本任务不含部署。

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

### 模型特点卡（2026-10-13，总设计 v2 §3；feat/model-cards）

- **两层卡**：七张模型卡（`cmux_fleet/config/fleet.yaml` 的 `models` 块：fable / opus / sonnet / haiku / sol / astra / flashnext）（初评，取总设计 v2 §3），只是数据，**不参加路由**（档案卡在 `profiles` 块）；四张档案卡 id 不变（`claude-coder` / `claude-reviewer` / `flashnext-coder` / `researcher`），用 `model_card` 链到模型卡，自己没写的评估字段从模型卡继承（档案卡写了的以档案卡为准）。`claude-coder`、`claude-reviewer` → `opus`，`flashnext-coder` → `flashnext`，`researcher` 不链（Codex 缺省档未定是 Sol 还是 Astra）。档案卡与模型卡的 `cli` 必须一致。
- **评估字段**（全可选；缺省 / null = 不限制或未知）：`quota_pool`（额度池名）、`hosts`（能跑的机器，字符串列表）、`domains`（允许的业务领域）、`context_window`（预算侧窗口，正整数；不是能力上限）、`handoff_ratio`（(0, 1]）、`max_concurrent`（本 fleet 票数，≥1）、`roles_allowed` / `roles_denied`（同一角色不能两边都列）、`judgement`（`high` / `mid` / `low-mid` / `low`）、`night_ok`（严格 bool）、`measured`（对象或 null，由记账员回填）、`evidence`（依据，字符串）。校验失败整份 registry 拒绝加载，与原字段一致。
- **角色词表**（英文 slug ↔ 总设计角色）：`supervisor` 监工、`orchestrator` 编排者、`implementer` 实现者 / 机械实现、`fixer` 修复者、`reviewer` 复核 / 代码验收、`plan-reviewer` 方案审、`falsifier` 证伪者、`consultant` 会诊者、`researcher` 研究 worker / 网搜员、`reconciler` 对账员 / 断言抽取 / 取证员、`summarizer` 说人话员 / 格式类、`material` 材料加工 / 证据裁剪 / 事故夹具 / 生成端、`proposal-expert` proposal_only 专家、`final-writer` 写给用户的结论 / 对外定稿、`incident-analyst` 事故验尸假设员、`long-runner` 无硬超时长跑、`line-checker` 逐行核验、`protocol-json` 协议 JSON 产出。
- **router**（`route_task(..., host=, role=, occupancy=)`，先过滤再排序；每个参数 None = 不过滤）：`host` 与卡的 `hosts` 比（不分大小写，全名或第一段，`studio.local` 配 `studio`）；`role` 被 `roles_denied` 列出或不在 `roles_allowed` 里就排除；`occupancy`（{模型 id: 会占用的槽数}）到 `max_concurrent` 就排除。全被过滤时报错写明每张卡被哪条过滤掉。`build_dispatch_plan` 透传 `host` / `occupancy`，并按步骤传角色：实现 `implementer`、复核 `reviewer`、研究 `researcher`，规划不传（词表里没有规划角色）。主机只在 fleet config 带 `host`（`start --host studio`）时过滤；不给就不过滤（Studio 真实主机名未核实）。
- **并发（`cmux_fleet/model_usage.py`）**：计数单位是**模型**（档案卡按 `model_card`，未链的按自身 id；`claude-coder` + `claude-reviewer` 都记 `opus`）；**一个槽只记一次**，与代数无关（换代 / 同槽换卡不重复计）；槽跑的是本代 `runtime_launch` 冻结的卡（继任者 spawn 前就写入，所以同一拍里先决定的切换后面的判断已经看得到），没有就是 fleet 缺省，只按（cli, 模型串）精确匹配到模型卡，配不上就不计、不猜；**旧代占到 tab 真正关掉**：live 切换时槽同时占旧模型（`retired_pane` + `retired_launch`）和新模型，旧 tab 确认消失才放旧票，不提前释放；**新代起不来回滚预占**：切换失败、继任 tab 已关时 `runtime_launch` 写回旧值，失败的新代不占票；继任 tab 关不掉就继续占新模型；dead 槽不占，除非它的 tab 还活着（`Fleet.model_usage` 用 cmux 查 tab；查不到按活着算，不提前放票）；退役中的槽占到真正退掉；监工不计。真实派工路径：① submit 规划用 `Usage.for_planning(active)` 当 occupancy（某模型若已跑在可复用的在役槽上，该槽不再算占用），`assign_profiles(..., running=)` 让新档案优先落到已在跑同一模型的空槽；② 投递时的 live 切换先过 `Fleet.card_gate`：主机不符 → 阻塞 task（不暗换）；模型满 → 消息留在队列、`profile_state=pending`、审计 `profile.capacity_wait`（原因不变只记一次），每拍重试，有票即切；③ **缺省 runtime 同样受卡限**（`model_usage.default_problem`）：不带档案的 `start`（provisioning 之前，SystemExit）、名单热加新行（整份拒收，审计 `roster.rejected`）、子 fleet 委派（`subfleet.check` 拒绝）都按缺省模型的卡计票 + 主机；切回缺省用 `Fleet.card_gate(slot, None, registry)`；缺省配不上模型卡（含 Codex 档案名）= 不设限、不猜。续跑 / 旧 config 里已超票的只记 `capacity.over` 警告（已在跑的不杀）；④ 名单 / 夜间切换用 `model_usage.check_slots`（整份名单按模型计票 + 主机，缺省槽也按缺省模型自己的卡计），供 B 接（见 fleet 工作区 worker-3/C-interface.md）。`max_concurrent` 是本 fleet 票数，不跨 fleet。
- **未核实字段**：卡上 `unverified` 列出缺外部事实的字段（Sol / Astra 的 `context_window` 留空；Fable 的 `model`；Haiku 的 `judgement`；FlashNext 的 `hosts`、`roles_allowed`），档案卡继承。
- **`quota_pool`**：只写提供方额度族（`claude` / `codex` / `free-studio`）。实际账号（号）写在部署方私有配置文件的 `accounts` 块（`$CMUX_FLEET_CONFIG` 或 `~/.cmux-fleet/fleet.yaml`，格式见 `cmux_fleet/config/fleet.yaml#accounts`），每个号带 `provider`（= 本字段的额度族）、`meter`、`hosts`、`caps`，不写进卡、不进仓库。
- **handoff_ratio 按档案**：本槽 `runtime_profile` 对应档案卡（含继承）的 `handoff_ratio` 是 §22 换代阈值解析的一层（排在名单 / config `handoff:` 表之后、`--handoff-ratio` 之前；集成分支 integration/batch-20261013 统一为一套解析，不另设第二套判定）。只给比例，窗口不从卡取（§22 内置表 / 实测不变）。当前 FlashNext 0.5、Opus 0.6（与缺省同），其余未写。
- `judgement` / `night_ok` / `domains` / `quota_pool` / `measured` 在本项只是数据：夜间切换（B）、刹车（E）、记账员读取。

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
- 完整命令、结果与待验收边界见 [dynamic-agent-routing.md](dynamic-agent-routing.md#dev-validation-2026-10-11)。**上述为 Phase 5 当时的历史状态：代码/模拟回归已完成；真实 dev 部署与 supervisor TUI smoke 受环境限制未通过；当时未独立复核、未合并、未生产部署。**

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
- **孤儿 Redis 回收（2026-10-15）**：v2 daemon 非显式退出（崩溃、被杀、工作区被关）时故意不关 Redis，留给 `--resume`；但 run 被直接撇下（换成别的 fleet、从不 stop）时，它的 Redis 永远没人关（10/14 Studio 两套旧 evermem-haiku run 各留一个）。修法：`start_redis` 在实例目录写 `owner.json`（`{"run": ...}`）；`start` 真起新 run 前、`start --resume` 接回前各扫一次本用户的 `redis-server unixsocket:` 进程，对有 `owner.json` 的实例，三条同时满足才 SHUTDOWN：① owner run 没有 daemon 持锁；② 没有任何活进程命令行带该 run 路径（actor / 钩子还在写它时不关）；③ `owner.json` 已过 120 秒（挡 start_redis → daemon 拿锁之间的空窗）。子 fleet（路径含 `/sub/`）只要任一祖先 run 还活着就不碰，归父 `collect()` 管；`--resume` 的目标 run 不碰；没有 `owner.json` 的（本修复前起的、非 fleet 的）一律不碰。SHUTDOWN 走 save，AOF / RDB 留在原目录，之后 `--resume` 照常从同一目录重启 Redis，无损。扫描失败只打印、不阻断 start；测试经 conftest 关掉全机扫描，`tests/test_orphan_redis.py` 限定自己的 root。这是「不杀其他 Redis」的唯一例外，且只限 fleet 自己起的、owner run 已撇下的实例。
- `.runtime/` 不进入版本控制。本工作区用 Git 本地 `info/exclude` 排除，没有修改 `.gitignore`；其他 checkout 需同样排除。

## 14. Redis 信箱本地集成验证（2026-10-12）

用户要求合并 Redis 信箱功能。本轮冻结框架候选后重跑完整回归、真实 Redis 竞态探针、已安装 Pi SDK/TUI 合同，以及真实 Redis + Fleet + Pi/FlashNext 的 supervisor TUI 输入闭环。

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
- 整份校验：YAML 错、未知字段、名字不合法/重复、新增名字不以 `-<数字>` 结尾（§3，本 run 已有的不查）、零个 worker、角色文件不存在、本 run 已用过的名字（Redis agent 状态或 workspace 目录已存在）→ 整份拒收，保留当前名单，`roster.rejected` 写入审计（control 可见）并列出每条原因。
- 加：先写 `config.json`（`latest.json` 指向本 run 时同步）再启动——钩子/桥按配置文件校验成员；Redis 字段与 `start_all` 相同，READY 提示相同，标签放进右侧 worker 栏末尾（无 worker 时放 supervisor 右侧），刷新历史页。
- 卸：标记退役中——不再是派活目标（决策/请求/路由校验、profile 分配都排除），但仍计入 `max_busy` 与“有 worker 在忙”；空闲且队列空、v2 无在途投递时，按换代同样的方式存屏、关闭旧 tab（v2 `retire`），从名单删除并写回配置。工作目录保留，历史页继续可查。退役完成前写回文件即取消。
- 只改 `role`：下一代生效，不重启。
- 顾问开关（2026-10-14，advisor 设计稿 v0.7 §2.1）：顶层 `advisor: on|off`，缺省 off，随本节检测热载、不重启。改的入口只有 roster：用户改文件、`fleet advisor on|off`（只改 roster；不带 `--roster` 的 run 由它按当前 worker 生成 `<run>/roster.yaml`，daemon 回落读这个文件）、监工 decision 键 `advisor`（daemon 写 roster 后立即生效，同一条 decision 的派单已过顾问）；`start --advisor` 只是往 roster 写 `advisor: on`。config.json 不再存这个开关。坏值随整份拒收；开启时 kit 坏则拒开、不写 roster，审计 `advisor.rejected`。关闭时清掉待会诊登记，举着等首张配置单的槽按缺省放闸（不能永远举着）；已在跑的代与按配置单起的代不回退。审计 `advisor.on` / `advisor.off`（`who`=user|supervisor），control `STATE` 行显示 `adv=on|off`。
- 通知监工：不单独发一条消息（task 模式下单独一条会让监工回一个无派活的 RUNNING，按原规则判错三次即 blocked）；改为在下一条发给监工的业务消息末尾追加当前可派 worker 名单。
- 监工自己加 / 卸 worker（2026-10-28 用户定「Supervisor 能够动态地开启 Worker 和关闭 Worker、卸载 Worker……不用整个 fleet 全关掉再开下一区」）：五份监工 prompt（`supervisor.md` / `supervisor_v2.md` / `service_supervisor.md` / `service_supervisor_v2.md` / `service_supervisor_tui_v2.md`）第 2 部分末尾有「Adding and retiring workers (the roster)」小节：何时加（就绪的独立活多于空闲 worker 且任务说明允许）、何时卸（计划里没活了 / 并行宽度收窄）、任务说明的上限优先（最多几个、哪些不许卸、新 worker 先要的 worktree / 角色文件），加 = `workers:` 下加一行 `- {name: <role>-<n>, role: <path>}`、卸 = 删行，名字不复用、取下一个编号，只改 `workers:`；见到 `[Roster change]` 列表里有它才派活。占位 `{{ROSTER}}` 由 `roster_notice(cfg)` 渲染：run 有 `cfg['roster']` 时写名单路径（带夜间名单时同时写夜间路径并要求两份 `workers:` 同改，否则日夜切换被拒），没有（`--workers` / `--worker-names` 起的）写「本 run 不能加 / 卸」。禁令行同步为「只许改名单文件」，仍不许调 fleet CLI、不许往标签打字、不许动别的 agent 目录。Codex 监工的沙箱（`workspace-write`）在 run 有名单时额外把名单所在目录（及夜间名单目录）列入 `sandbox_workspace_write.writable_roots`，否则改不了文件（codex-cli 0.164 `debug prompt-input` 实测生效）。**拒收也告诉监工**：整份拒收时把错误原文存进 `fx:roster` 的 `rejected`，下一条业务消息末尾追加 `[Roster rejected] <错误，一行一条>; the roster in effect is unchanged: <当前可派名单>`，送出即清；同一份被拒内容不重复报（沿用内容哈希）；被拒后在送达前又改成合法名单的，清掉这条，只报 `[Roster change]`。已知边界：不带 `--roster` 的 run 被顾问开关写出 `<run>/roster.yaml` 后 daemon 也会热载它，但 prompt 仍写「不能加 / 卸」；`self.v2.queue('supervisor', …)` 直发的几条消息（service 的 `[blocked]` / `[dialog]` 等）与 `[Roster change]` 一样不带这两条附注，等下一条走 `Fleet.queue` 的消息。
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
- **结束标志**：task 进入任一终态时 daemon 写 `<run>/result.json`（状态、原因、task id、产出逐条复验结果，原子写）；同一 run 接受新 task 时删除它；但监工槽仍是 held（上一 task 终态时扣下的在途投递，pi 监工没有 hold 出口，见 §24 已知缺口 ⑦-4）时拒收新 task 并写明原因、提示另起新 run（2026-10-28：此前照收，task 永远投不出去）。显式 `stop` 成功退役所有 runtime 后也写（service 记 `stopped`），所以 `stop` 等于结束这个 run。续跑时发现 Redis 里 task 已终态但没有 `result.json` → 补写后按已结束处理。
- **名单**：沿用 config.json 已落盘的名单；给了 `--roster` 就写进 config，daemon 第一拍按 §16 热加载对账。
- **已知边界（P2）**：丢失的「报告格式纠错」消息会原样重排给新一代（新一代没有上下文）；只死 daemon、Redis 未死时，worker 在停机期间做完的回合会被拒、再做一次；Pi 不接回；真实 cmux 下只剩 history 栏的整页重建未实测；`has_cli_process` 按进程判断（§23：可执行文件名去掉 `.exe` 后是 codex / claude，或 `node` 且脚本路径含 CLI 名；裸 shell、无关 node 不算）。


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
- **移交**（`subfleet.spawn_child`，只在 daemon tick 里、v2 事务之外执行；事务内调用方用 `subfleet.request` 只写请求文件、`subfleet.check` 同步拿裁决）：worker 必须 idle、READY、无在途投递、标签在。父：开占位标签放到 worker 前 → 备份 worker 的 AGENTS.md/CLAUDE.md → 建子 run/config/私有 Redis → 往子 bus 投子任务（目标 = 原指令 + 批准的拆法 + 子 fleet 自己 workspace 里的交付路径（§23，不再给父路径）；时限 = 父任务剩余时间）→ slot 标 `delegated`（`pane` 指占位标签，原窗格记 `delegated_pane`，`delegate_cfg` 指子 config）→ 从父登记 release 原窗格、写入子登记 → 拉起子 daemon。任一步失败回滚并带原因拒绝。子 daemon 首拍：起子 worker → `adopt`（`move-surface` 跨工作区）原窗格到子工作区左上 → 在原 worker 目录写监工规则 → 打字告知角色已变、要 `READY`。
- **钩子改投**：窗格里的 CLI 进程钩子是启动时钉死的（父 config、`worker-1`、代数），进程无法改。`claude_stop` / `notify` 进 v2 分支后先调 `subfleet.delegated_route`：父 Redis 里该 slot 是 `delegated` 且代数一致 → 改投子 config 的 `supervisor` 槽（第 0 代）。子 fleet 第一拍按原流程 `bridge_session → register_slot → startup(READY)` 绑定同一个 Claude 会话。代数不一致的迟到回调不改投。
- **收回**：子 fleet 写 `result.json` → 父 tick 记下结局后发 stop → 子 daemon 显式 stop 时对 adopt 的 supervisor **只 release 不 kill**（`handback.json`）、照常退役子 worker、关 Redis → 父见子 daemon 已退出：把原窗格 `adopt` 回占位标签前、关占位、恢复 worker 规则文件、关子工作区全部标签（历史页文件留在子 run 目录）、删子登记 → slot `starting`，打字告知已变回 worker、要 `READY`（同代、同会话，`register_slot` 幂等）。原会话没了（cmux 重启等）→ 父按 §17 的 `respawn` 在占位处起下一代。结局 `completed` 时，父 daemon 先把子 fleet 在自己 workspace 里交的产出复制到父路径（§23），然后：父要的产出文件存在非空 → `returned`，否则 `failed`（重试另起 `-attempt-<n>`，同一任务的子 fleet 次数计入 `--max-attempts`，§23）；父任务仍在跑时给父监工一条 `[Sub-fleet M0 completed|failed]` 业务消息。父任务进入终态时，在跑的子 fleet 一并停掉收回。
- **续跑与失败**：主 fleet `--resume` 时 `delegated` 的 slot 不接回也不起下一代（窗格归子 fleet，子 fleet 随后续上）；整页丢失（`page=False` 重建）时带 `delegated` slot 的情形未处理。`spawn_child` 任一步失败（含 `start_redis` 的 `SystemExit`）都回滚：子 Redis 关掉、窗格还给父、slot 回 idle、记录 `failed`，父 daemon 不受影响。收回前先给窗格发一次 Escape，中止 adopt 会话可能还在跑的回合，免得它迟到的 Stop 被当成「变回 worker」的 READY——这是 §17「Escape 唯一被当作接受停止」之外的第二处，只限子 fleet 收回。`stop` CLI 立刻返回 accepted，daemon 随后每个子 fleet 最多等 180 秒。
- **控制页互链**（`subfleet.page_links` → `history.write_page(..., links)`）：每套 fleet 的历史页（控制标签旁的浏览器页）顶上一行：有子 fleet 时列「子 fleet n：M0 · 状态（结局）· worker-1」，子 fleet 的页给「← 回到父 fleet」。**点击 = 切到对方的 cmux 工作区**（子 fleet 禁嵌套、是侧栏里并列的独立工作区，总设计 v2 §4.9；cmux 只注册 http/https，网页自己切不了工作区）：点链接时页面留在本文件、只加显式标记 `?fleet-jump=<毫秒36进制>-<随机>&rel=parent|child&to=<对方页>`（真实 cmux 实测：网页里直接跳到页面所在目录之外的 file:// 页会变成 about:blank，`cmux browser goto` 则可以），本 fleet daemon 每拍（至多每秒一次）读自己历史窗格的 URL（`cmux browser url`），见到标记才处理（`subfleet.follow_jump`）：只认本 config 的 `parent_config` 父页（rel=parent）和本 run `sub/*/subfleet.json` 里的子页（rel=child），对方 cmux 登记里的工作区存在且不是本工作区 → `select-workspace`，再把本窗格 `browser goto` 回本 fleet 自己的页（上次见到的 hash 状态）；对方窗格不动。每个标记先记进 Redis（`fx:service` 哈希的 `history_jump`）再动作，重复 tick、reload 带标记的页、daemon 重启都只补做「放回本页」，不会第二次切；超过 120 秒的旧标记只放回本页（防 daemon 停机后抢焦点）。没有标记的 URL 变化（普通刷新、页内状态）一律不当点击。已收回 / 失败的子 fleet（工作区已关）、对方登记缺失、`select-workspace` 失败：不切，把对方历史页（去掉标记）留在本窗格可读；关系不符或无关页面：不切，放回本页。没有父也没有子的 fleet 不读窗格。父页在子 fleet 起动、转 stopping、收回时重写并 reload；数据只读磁盘（父 config、`subfleet.json`）。
- **v2 矩阵新增**：`claude` 监工 + `claude` worker 仅当 config 带 `adopt`（即子 fleet、task 模式）放行；普通 `claude` 监工照旧拒绝。adopt 的 supervisor 不换代（换代会把要还给父 fleet 的窗格换掉），上下文增长靠 Claude 自身压缩。子 fleet `--resume` 时 adopt 窗格像 worker 一样接回、不起下一代。
- **只支持官方 Claude worker 窗格**：Pi bridge 绑父 Redis socket 且断线永不重连（§17），拒绝并写明原因；Codex worker 不在 v2 矩阵内。
- **Claude worker 会话名**：`--name f<主 run token>[-<子 fleet 编号>]-<slot>`（如 `fa1b2c3d4-M0-worker-2`），每槽固定、换代沿用（契约规范附录 C）。cmux 标签仍由 fleet `rename-tab` 设为 slot.generation；**`rename-tab` 是否压过 Claude 用 `--name` 写的终端标题尚未在真实 cmux 上验证**——若被覆盖，按规范改用 `…-<slot>.<代数>` 并在接口契约 5-A 写「前缀匹配取最高代」。
- **顺带修复**：v2 daemon 主循环此前直接调 `RuntimeV2.tick`，`Fleet.tick`（§16 名单热加载、§17 任务终态写 `result.json`、本节子 fleet 收回）在真实 v2 daemon 里从未执行；改为 `self.f.tick()`（内部仍调 `RuntimeV2.tick`），加回归测试。

**验证**：`python3 -m pytest -q tests` 全套通过（新增 `tests/test_subfleet.py`：两套真私有 Redis + 两个进程内 daemon + 每套登记分开记归属的假 cmux + 真 `claude_stop`/`notify` 入口：移交→子 fleet READY 走父钩子→子监工决策派给子 worker→交卷→release→父收回同会话 READY→父监工收到结果；委派中不投递不判死；产出缺失判 failed 与重试编号；起子 Redis 失败回滚；收回前 Escape；会话丢失原位起下一代；主 resume 保留 delegated slot 并拉起子 fleet；上限四项与编号范围；Pi/忙槽拒绝；请求文件经 tick 执行；status 树；迟到代数不改投；树停无孤儿 Redis；主 resume 拉起子 fleet；子 resume 接回 adopt 窗格；父任务终态停子 fleet；v2 主循环跑 Fleet.tick 回归；cmux_manager 的 release/adopt/place_after/close_all 用假 `_run`）。

**未验收 / 已知缺口**：真实 cmux 上的跨工作区 `move-surface` 移交、`reorder-workspace`、标签标题与 `--name` 的先后，均未实测；一个正在跑的 Claude 会话在中途换掉 CLAUDE.md 后是否真按监工格式只回决策 JSON（提示词层面），未实测——这是本块最大的剩余风险；委派中的 slot 仍可被父监工派活，消息排队到收回后再投；申请拆解→审批→`spawn_child` 的接线留给 contracts 合并后的下一块；子 fleet 任务的产出只做「文件存在非空」检查，正式验收交 contracts 块；独立复核、合并、部署均未做。

## 20. 申请拆解接线 + 子 fleet 接口通知 / 否决（2026-10-13 用户新要求，开发中）

设计稿：外部项目 `tasks/plans/cmux-fleet-nested-decompose-design-20261013.md` §3–§4、`cmux-fleet-interface-contract-spec-20261013.md` §3.0 / §4.2（子 fleet 内部接口「立即生效 + 通知顶级、顶级可 veto」，用户已认可）。本节把 §19 留下的「申请拆解 → 审批 → `spawn_child`」接上；唯一 daemon、slot/generation 标识、一页布局、task/service 协议、§16–§19 不变。

- **worker 申请**：报告照常四段（Done 写已做的部分），末尾恰一个 JSON 对象 `{"escalate": 原因, "proposal": [{id, instruction, output}], "workers": n}`（契约任务与 `interface_change` 同一个对象；`workers` 缺省 = 方案块数）。程序校验：方案编号互不相同且全是 `<任务编号>.<n>`，该任务是这个 worker 正在做的（契约：任务表 `to` = 它、状态 running/escalated；普通任务：本任务里带 `id` 派给它的项），再跑 `subfleet.check(..., ready=False)`（编号归属、层数、子 fleet 数、全树 agent 数、`child_workers_max`、槽位类型；不查 idle / 在途投递——worker 此刻正在结算这条报告，真正起子 fleet 时再查）。不过 → 原因同时发给 worker（「自己继续做」）和 supervisor；过 → 存 Redis `fx:escalations`，一条转给 supervisor（方案逐块列出 + 怎么答）。
- **普通任务的编号**：dispatch 项可带 `id`（`M<n>`，子 fleet 内 `M0.<n>`）；带了才写进 `fx:items` 并在派单首行加 `[M0]`，才能申请拆解。没有编号的申请被拒，原因写明「派单要带 id，或用契约任务」。`fx:escalations` / `fx:items` 在新 task 提交时清掉。
- **契约任务表**：申请那条报告不是交付，任务行记 `escalated`（不判 failed）；被拒时仍 `running` 并把拒绝消息记在该任务名下（下一条报告照常结掉它）。
- **supervisor 决定**：decision 新键 `escalations: [{task, decision: approve|revise|reject, workers?, note?}]`。格式错、任务没有待批申请、同一回复里又派了这个任务、approve 又给同一 worker 派单 → 走原纠错（计三次连拒）。带 `escalations` 的 RUNNING 不受「无人忙必须派单」限制。
  - `approve`：再查一次上限；**上限不过不算格式错**——申请保持待批，给 supervisor 一条原因（可减 worker 数、revise 或 reject）。过了 → `subfleet.request`（目标 = 该任务原指令 + 父 fleet 要核的接口编号（形状在父 `contracts.json`，只读）+ note；拆法 = 批准的方案；契约任务带 `contracts: true`，子 fleet 先走自己的 P0 / P1；产出 = 全部 provides 接口的父路径，子 fleet 写进自己 workspace 的对应位置，收回时复制回来，§23）→ 任务行 `delegated`、attempts+1。下一拍 tick 执行请求：答复 `accepted` → 记 `delegated`；`rejected`（如 worker 恰好不空闲）→ 申请回到待批、行回 `escalated`、告诉 supervisor 原因。
  - `revise` / `reject`：给 worker 发一条带 `[M0]` 的消息（附 note），契约任务行回 `running` 并把这条记在 M0 名下；worker 改方案重报或自己做。supervisor 直接重派该编号也视为回答，待批申请作废。
- **另两种 escalation（2026-10-14 缺陷 F′）**：`fx:escalations` 里除了拆解申请（键 = 任务编号，无 `kind` 视为 split），还有 `dialog:<槽>:g<代>`（worker 停在非删除确认框，`approve | reject`）与 `blocked:<槽>`（worker 报 Blocked，`approve | revise | reject`），同一个 `escalations[]` 答；细则见 §24。答一条已不在的 `dialog:` / `blocked:` 记录（框已没了 / 已转用户）只记日志，不算格式错。
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

- **判定**：投递前 `used >= ratio × window` 才换代（原规则；§32 起空闲无消息时也判一次）。`window`、`ratio` 各自独立解析，先命中者生效：
  1. roster 该 worker 条目的 `window` / `handoff_ratio`；
  2. roster 顶层 `handoff:` 表，键先 `<cli>:<model>`（模型串精确匹配）再 `<cli>`，值 `{window?, ratio?}`；
  3. `config.json` 的同名 `handoff:` 表（静态，启动时校验，不热载）；
  3b. 仅 ratio：本槽运行中档案卡（§12 模型特点卡，`runtime_profile` 含继承）的 `handoff_ratio`，来源显示 `card <档案 id>`（集成 C 时并入本层级，原 C 分支的单独 `handoff_ratio()` 判定不再直接用于换代）；
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

### 刹车：预算卡 + 墙表（`cmux_fleet/brake.py`、`cmux_fleet/config/fleet.yaml#walls`）

- 缺省现金封顶（总设计 v2 ★3）：`meter: usd_equiv` 的号，卡没写 `max_usd` 时按该号 `caps.day_usd / caps.night_usd`（缺省白天 40 / 夜间 10 USD）每场景评估（夜间看 B 的 `fx:night.brake_card`）；调用方写了就用调用方的。quota.json 缺失仍是 unknown、不触发。
- 预算卡：task spec `budget`，或 service 的 `start --budget JSON`（写进 config `budget`）。字段 `pool / max_usd / max_week_pct / max_calls / max_rounds / max_hours / max_busy`。`pool` 写号池文件里的号 id 或别名（不在文件里直接拒）；按号的 `meter` 分：`usd_equiv` 盯墙 + 等价额度 `max_usd`（池读数相对开卡时基线的增量）；`week_pct` 盯墙 + 周额度 `max_week_pct`（池的绝对百分比）。字段与 meter 不符直接拒。代码里不写任何真实账号名。
- 计数：`calls` = 每次投递（v1 / v2 同一处计）；`rounds` = 被接受的监工决策；`hours` = 开卡起算（软线，task `hours` 仍是硬的 `budget_exhausted`）；`max_busy` 只能收紧 `--max-busy`。计数在 Redis `fx:brake`，新任务换新卡。
- 池读数来自额度调度员的 `~/.cmux-fleet/quota.json`（config `quota_file` 可改）：`{"pools": {"<号 id>": {"cash_used_usd" | "week_used_pct", "at"}}}`。缺失 / 读不出 / 超过 1 小时 = unknown：显示在 `brake.json`，**不触发**。
- 到线：pause（不投递、不轮换，全部可续）+ 审计 `brake.tripped` + `<run>/brake.json` + 可选 `brake_notify` argv（`start --brake-notify`，无 shell）。v2 下判定在一个事务里做，通知在事务外。
- 续跑：`cmux-fleet resume` 在预算线仍越线时被拒（不论暂停记录最后是谁写的：后来的墙、F 的 hold 改写暂停原因都不解锁），须 `resume --raise LIMIT=VALUE`（不能改池）；墙引起的暂停可直接 resume。新任务开新卡时清掉上一张卡的刹车暂停（操作员手动 pause 不清）。
- 墙表一张：账号级墙（只认 Claude / Codex CLI 自身的 usage / weekly / session limit 文案；通用的 `rate limited` / `429 Too Many Requests` / `quota exceeded` 不算墙——第三方工具 Tavily / reddit / WebFetch 会报这些字，worker 报告贴进监工屏也会带上，EF 复核 r1 P1-01）→ 换边或停，fleet 内即停（pause）；分池墙（"You've reached your Fable limit"、`/usage-credits`）→ 同 CLI 降一档（`same_cli_downgrade`，吸收自部署方的 `cli_tier_map.yaml`），没有下一档也停。来源：卡住（超 `stall_timeout`）时的屏幕尾部（v1 在 `on_stall`，v2 在 `Fleet.tick` 的刹车事务里、早于 v2 hold）；v1 另看短回合文本（≤400 字，长报告不判墙）。另有三路不等 `stall_timeout`：① 启动回的不是 READY 而是墙 → 槽位 `walled`（不判死、不 blocked）+ 可续暂停，账号级墙在 resume 后按原 launch 重起下一代；② v2 官方 CLI 的短回复（≤400 字，`tui_turn` 带 `wall_text`）与 Pi 的模型错误事件（`candidate {tool: model, error}`）→ 投递以 `walled` 提交（结果不采用、不转监工），原指令带「[Resumed after a usage wall]」只重投一次（重放的 settled 命中 `settle.late`）；③ 看门墙闸（见下）。分池墙在 worker 槽**实际执行**同 CLI 降一档：空闲边界由 `wall_tick` 用 `wall_rotate` 起下一代（launch 覆盖模型键，交接骨架、存屏、tab 原位、杀旧 tab、v2 `confirm_retired`），新一代 READY 后只解除墙自己的暂停（`fx:brake.paused_by` 比对；F hold / 操作员 pause / 预算越线一律保持暂停）；槽位已被别的换卡（B 夜间 / live profile）改过 → 降档作废（`brake.downgrade_superseded`）。监工槽与账号级墙只停不降档。
- v1 task 的 `pause` 现在也会停投递（原来只对 service / v2 生效）。

### E 先于 F：看门的墙闸（`Fleet.wall_gate`）

- 接口：`Fleet.wall_gate(slot, screen, source='watch') -> record | None`。看门（F，§24，`watchdog.Watchdog.check`）每次抓屏后、在弹框白名单 / 未知弹框 hold / stalled 阶梯 / frozen 之前先调它。只看屏幕最后 `brake.GATE_TAIL`=25 行（`brake.screen_wall`）。
- 返回 record：墙已触发刹车（pause、`agent.wall`、审计 `brake.tripped`、`brake.json` source=watch、可选通知；v2 下在一个事务里）。看门本 tick 对该槽**一个键都不按、不 hold、不杀不换代**——墙菜单（如 `❯ 1. Yes, switch…`）永远不当弹框答。同一 agent 的墙已经刹住时再调只返回原记录，不二次触发。返回 None：无墙，看门照自己的表走。
- hold 出口（F 的 `turn_ended` → `release_held`）只把槽位放回 idle，**不解除暂停**；F 的 hold 会改写暂停原因，但刹车记录在 `fx:brake` 里独立保存，`resume` 每次都按预算卡复核，越线就拒。


## 24. 看门五态（transport v2，2026-10-13 批次 F；集成分支 integration/batch-20261013 时由 §20 顺延为 §24）

只在 v2 运输层；v1 `check_stalls` / `on_stall` 不变。代码 `cmux_fleet/watchdog.py`，由 `RuntimeV2.tick` 在 Redis 意图之外调用（抓屏 / 按键 / 杀 / 起新代都是副作用）；结算（hold / 入队 / 公布结果）仍走 `transaction`。每个非空闲槽位每 `watch_interval`（缺省 5 s）判一次，状态变化写审计 `agent.state`（from / to / 证据）。

| 状态 | 判据 | 动作 |
|---|---|---|
| `idle` | 无在途投递且不在 starting | 什么都不做，再久也不判死（E1） |
| `working` | 在途，且下表三个信号在 `stall_window`（缺省 300 s）内**任一**动过（动一个，窗口基线全部重置），且转录 / rollout 在 `stall_timeout`（缺省 1200 s）内动过 | 无 |
| `prompting` | 屏幕最后 `tail_lines` 行命中弹框表白名单（starting 时也查，信任框在 READY 前） | 存屏到 `<run>/watchdog/`；**文案命中不等于授权**：再核①窗格属本 fleet 注册表且非子 fleet 收养，②该条 `verify` 规则证明对话框上的具体操作 / 路径落在 run 已有授权内，两项都过才发预定键（审计 `watch.prompt_answered` 带 `authorized`），否则同未知弹框：只存屏 + 暂停派工 + 槽标记 `prompt_wait`（租约不动，人答完后该轮正常结算），审计 `watch.prompt_unverified`。屏尾出现删除 / 移动类词（§24 删除分类）时即使白名单已授权也不按。同一投递同一弹框答满 `max_hits`（3）次仍在 → 按 `stalled` |
| `prompting`（未知） | 命中 `unknown_markers` 但不在白名单 | 只存屏 + 暂停派工 + 槽标记 `prompt_wait`（不是 hold），一个键都不按 |
| `stalled` | 在途，且①三个信号**全部**静止满 `stall_window`；或②转录 / rollout 静止满 `stall_timeout`（硬上限：屏幕在转圈、背后流已挂也能抓到） | Escape，最多 `stall_escapes`（3）次，均匀分布在 `stall_grace`（300 s）里；不 hold。期间**转录**再动 → `working`（屏幕不算：Escape 本身会改屏，算了梯子永远到不了 frozen） |
| `frozen` | stalled 满 `stall_grace`；或 3 次 Escape 后屏幕摘要不变；或 `ps` 连续两次看到该 tty 进程 `U`（Linux `D`） | 存屏 → skeleton 交接单 → 新一代（launch-held）`tab_into` 原位 → `kill_pane`（只认本 fleet 注册表里的 surface，杀其 tty 进程树）→ `confirm_retired`（投递 cancelled、`stop_confirmed`）→ 开启动闸 → worker：给监工发 `FROZEN` 报告，原指令**不重跑**；监工：原消息加注重新入队；service：请求公布 `stalled`。run 暂停中 / 子 fleet 收养 / 不在本 fleet 注册表的窗格 → 只 hold 不杀 |

- **卡住的三个信号（2026-10-14 用户定，`feat/stall-5min`）**：判卡住从「只看转录 30 分钟」改成「三信号 5 分钟 + 转录 20 分钟硬上限」。一个跑 10 分钟的 pytest / `sleep 420` 期间转录不动，单把阈值改 300 会 Esc 打断正在跑的工具，所以要三个都静止。

  | 信号 | 怎么读 | 什么算活着 |
  |---|---|---|
  | 会话记录 | `Fleet.last_activity`：Claude 主转录 + 子 agent 转录、Codex rollout、pi 会话文件的 mtime，及投递时刻 `busy_since` | 比窗口基线新 |
  | 屏幕 | 每拍 `capture_output` 的 sha256 前 16 位（与 frozen 判据的 `esc_digest` 是两个字段） | 与窗口基线不同；抓屏为空 = 读不到 |
  | 进程树 CPU | `ps -axo pid=,ppid=,tty=,time=` 一次：窗格 tty 上的进程（tmux 窗格取 `#{pane_tty}`）**加按 ppid 的全部后代**累计 CPU 秒。Claude Code / Codex 的工具子进程自开会话（tty 为 `??`），`ps -t` 看不到，所以按 ppid 找 | 窗口内逐拍正增量之和 > `stall_cpu_pct`（缺省 5）% × `stall_window`（缺省 15 s）；子进程退出导致总和下降的那一拍计 0；`ps` 失败 / tty 上无进程 = 读不到 |

  基线存槽字段 `quiet_since / quiet_act / quiet_digest / quiet_cpu / quiet_gain`（与 `esc_digest / esc_at` 同写法），`classify` 仍是纯函数，新输入 `quiet`（三信号同时静止的秒数）与 `stall_window`。**读不到的信号不能证明静止**：本拍窗口重来，只剩硬上限管（假 mgr 没有 CPU 探针，所以原有测试不受影响）。槽空闲 / 结算 / 同代换绑 / frozen 换代时基线清零。新信号只管「进 stalled」，出 stalled 仍只认转录。`frozen` 的「`ps` 连续两次 U/D」判据不变，仍只看 tty 直属进程。
  - **CPU 阈值不是 0（与「CPU 不增长」原话的差别）**：本机 2026-10-14 实测，空闲的 Claude TUI（粘了没回车）也以单核 0.6%–1.5% 爬（隔离会话 150 s 涨 0.88 s；用户终端里挂满 MCP 的 Claude 会话 90 s 涨 0.9–1.35 s），Codex 空闲 0.16%，pi 空闲 0；按字面「不增长」Claude 永远判不了卡住。5% 是空闲爬升的 3 倍余量，真干活的子进程（pytest 近 100%）轻松超过；Claude / pi 自己画转圈约 1.4%–4.5%，不靠 CPU，靠屏幕。
  - **三个 CLI 的屏幕前提（2026-10-14 本机隔离 tmux 实测，每 5 s 抓一次）**：Claude Code 2.1.280 前台 Bash 工具跑 100 s 的 python sleep → 屏上工具计时 `(1m 12s)` + 转圈计时，12/12 拍都变；Codex 0.162.1 跑 `sleep 240` → `Working (3m 43s • esc to interrupt)`，30 拍 29 变；pi 1.2.0（`Loader` 80 ms 一帧的转圈 + bash 行 `Elapsed 1m 28s`）12/12 变。三者「粘了没回车」空闲 30 拍 0 变。所以长工具调用不会被误判；粘贴不回车会在约 5 分钟判 stalled。API 重试倒计时屏幕在变 → 不判卡住（那是启动 READY 时限那条路，本次不动）。
  - **顾问槽（§27）不豁免、不另设阈值**：常驻顾问恒为官方 Claude，长思考期间屏幕带秒数一直在变，不会被窗口判卡住；三信号全静止 300 s 的会诊已超过 `ADVICE_WAIT`（180 s），答案本就按迟到丢弃（`advice.late`）；反过来豁免它，顾问真卡死时每条派工都要先排 180 s 再按缺省派出，直到硬上限。一次性 `claude -p` 顾问不是窗格，不归看门管。
  - **影响面**：`stall_timeout` 缺省 1800 → 1200 是 v1 / v2 共用的 `fleet.DEFAULTS`，v1 `check_stalls` 也从 30 分钟变 20 分钟（本节「v1 不变」的唯一例外）；`stall_window` / `stall_cpu_pct` 只在 v2 看门。v2 墙闸的 `stalled_screens` 仍按 `stall_timeout` 取屏（看门 `check` 自己先过墙闸，窗口判的 stalled 撞墙同样不按键）。

- **frozen 换代顺序与失败**：先起新代再杀旧代（与 `respawn` / 交接同序，保布局）。旧代确认退役前新代拿不到活：它是 `starting`（`pump` 只给 `idle` 投），旧租约仍占着投递（`offer` 要 idle 租约，`register_slot` 报 `busy`），`launch_ready=0` 时它的 READY 被拒。任何一步失败（新代起不来 / 进不了原 tab / 旧代杀不掉或杀后仍在）→ 先杀掉新代，槽位字段还原成旧代、hold（投递保持 uncertain，不 cancelled，不重跑），审计 `watch.rotate_failed`；只有新代也杀不掉时才可能两个会话并存：槽位 held + 启动闸关 + 任务 blocked，报告里点名两个 pane 交人工。
- 弹框表 `cmux_fleet/config/fleet.yaml#watchdog_prompts`（`--prompt-table` 可换），按 mtime 热载；坏改动审计 `watch.table_rejected` 并沿用旧表。每条必须写 `verify`：`own_cwd`（对话框上的路径都是本槽目录，spawn 时已信任）、`paths_in_run`（路径都在本槽目录或 `<run>/workspace`，即 `--add-dir` 已授权）；路径读不出来 → 不按。TUI 折行的长路径按「连续单 token 行」拼回。缺省表：`read_outside`（Enter）、`trust_folder`（Claude 2.1.280 实录：缺省选中「No, exit」，键 `Down, Enter`，且要求屏上是「❯ No, exit」）、`trust_folder_legacy`（Codex / 旧 Claude，Enter，文案未实录）。**删除一律交用户确认（2026-10-14 用户定）**：`dangerous rm` 弹框不进白名单，列在 `unknown_markers`（存屏 + hold 扣槽 + 不按键）；原 `rm_in_cwd` 校验已删；fleet 不装任何 PermissionRequest 钩子自动答删除确认。
- **弹框等人不是 hold（2026-10-14 缺陷 F，方案 B）**：`RuntimeV2.prompt_wait` 只写槽字段 `prompt_wait=<scope>`（在途投递 id；starting 槽 `startup-g<代>`）/ `prompt_wait_at` / `prompt_route=user`、审计 `execution.prompt_wait`、给该槽和监工各一张 notice 卡、暂停派工；**租约、消息、槽 state 都不动**。看门对带标记的槽：标记 ≠ 当前 scope → 清（`watch.prompt_cleared reason=scope_changed`）；转录 / rollout 没动 → 本拍直接跳过（不抓屏、不进 stalled / frozen 梯子、零按键，弹框滚出屏尾也一样）；转录动了（人已答、worker 继续）→ 清标记回 `working` 再正常判；槽空闲 / READY 后 → 清（`inactive`）；槽进 held / dead 等 → 清（`hands_off`）。`resume` 不清标记：人 resume 了没答框 → 槽继续等、fleet 不再被重新暂停；人答了没 resume → 该轮照常结算、报告入账，派工等 resume。starting 槽带本代 `startup-g` 标记时 `expire()` 不按 `startup_deadline` 判死（等人不是起不来），READY 照常注册。
- **删除分类（缺陷 F′ 8.1，2026-10-14 用户定）**：`watchdog.dialog_class` 对屏尾 `tail_lines` 行做大小写不敏感、词边界匹配，命中内置词表 `DELETE_MARKERS`（rm / rmdir / unlink / del / delete / remove / rimraf / shred / trash / purge / wipe / erase / truncate / drop table|database|schema / git clean / git reset --hard / git push --force|-f / git branch -D / git checkout -- / git restore / dd if= / > /dev/null / **mv / move / rename**（移动、改名算删除）/ 删除 / 清空 / 移除 / 清理 / 移动 / 改名 / 重命名，英文单词含屈折形）或 `fleet.yaml#watchdog_prompts` 的 `delete_markers:`（只能追加，内置词不可删，热载坏表沿用旧表）→ `delete`；屏读不出 → 不分类（交用户）。宁严勿宽：误判成删除只多问用户一次。删除类永远交用户，程序（含白名单）与监工都不按键。
- **非删除框交监工（缺陷 F′ 8.3，2026-10-14 用户定「除了删文件，worker 需要人工的事都交由 supervisor 处理」）**：核验不了的白名单框 / 未知框分类为 `other`，且 task 模式在跑、未暂停、槽属本 fleet 且非收养、槽不是监工、监工不在 held / dead / starting / handoff / clearing / walled 等态且自己不在等框 → `RuntimeV2.prompt_escalate`：槽标记 `prompt_route=supervisor`（同样不扣槽、不暂停），`fx:escalations['dialog:<槽>:g<代>'] = {kind: dialog, generation, session_id, scope, delivery_id, prompt, screen, digest(屏尾 sha256 前 16 位), keys_approve, keys_reject: [Escape], message_id, delivered_at}`，给监工排一条 `[dialog]`（屏尾原文 + 答法），审计 `watch.prompt_escalated`。`keys_approve`：白名单条目取表里的键；未知框只有屏尾光标可见在 Yes（`❯ 1. Yes` / `❯ Yes`）时给 `Enter`，否则无键（监工 approve 也转用户）。监工在决策 JSON `escalations` 里答 `approve | reject`（`revise` 是格式错）；`escalation_apply` 只把记录改成 `answered`，**不按键**。看门下一拍重抓屏，代数 / 会话 / scope / 窗格归属 / 屏尾 digest / 同一条框 / 删除分类重判**全部**成立才按（approve 按 `keys_approve`，reject 按 Escape，审计 `watch.prompt_answered authorized=supervisor:<决定>`），然后清标记、删记录；框没了 → `dialog.stale(dialog_gone)` 不按；换了框 → `dialog_changed` 不按、重新分类登记；身份变了 → `dialog.stale(identity)`；重判成删除 → `dialog.reclassified_delete` 转用户。等待期间人在窗格里答了（转录动）→ 删记录、按正常判。**回落用户**（= 删除类那条路：`prompt_wait`、暂停、notice）：监工不可用（登记时或等待中）、记录没了（如新 task 清了 `fx:escalations`）、监工收到 `[dialog]` 起 `dialog_escalation_timeout`（缺省 600 秒，config 键，`validate_config` 要求 ≥ 60）未答、监工回 RUNNING 没答（第一次退回纠正不计三振，第二次转用户）、监工自己撞框；审计 `dialog.to_human(reason=…)`。service 模式同样交监工（见下条）。trust 框 reject 按 Escape 等于让该 Claude 会话退出（之后走 liveness → hold），监工提示词已写明。
- **worker 报 Blocked 交监工（缺陷 F′ 8.4）**：task 模式 worker 报告的 `Blocked:` 段（v2 结算 JSON 的 `blocked`；`none / - / 无 / n/a` 不算）→ `fx:escalations['blocked:<槽>'] = {kind: blocked, slot, item(契约 owner / 运行中的行 / 最近的派单 id), reason, message_id}`，监工那条 `[worker report]` 末尾追加 `[blocked]` 答法。监工 `escalations` 答 `approve | revise(note 必填) | reject` → 给 worker 排 `[Supervisor ruling] [<item>] <决定>: <note>`（契约任务记在该行名下、行回 running，下一条报告照常结掉它），审计 `blocked.ruled`；同一回复直接给该槽派单 = 已答（同时答又派 → 格式错）；收到那条报告的 RUNNING 回复既不答也不派 → 第一次退回纠正不计三振，之后计三振。监工回 `BLOCKED` 照旧 `finish('blocked')`（人工逃生口不堵），提示词写明只有删除 / 移动类或确实非用户不能定的事才 BLOCKED。不暂停 fleet。
- 自检：连续 `capture_fail_alarm`（3）次抓屏失败写一条 `watch.capture_fail`（error，每段连续失败只报一次）。
- **hold 出口**：槽位 held（uncertain / expired）时，**同一会话、同一代的任一回合结束**（Claude Stop 经 `tui_turn`；2026-10-14 缺陷 E 起不再要求该回合由这单开启——Claude 会话回合串行，Stop 时已无回合在跑，后台任务未完的 Stop 本就不进 `tui_turn`，只记审计 `turn.deferred`），钩子发 `turn_ended`；**Codex** 没有官方撤回，别的回合结束不能证明队列里那条不会再跑，所以要 rollout 里带该单令牌的那一轮已 `task_complete`（`codex_queue.token_turn`），没起 / 没完照旧 `turn.ignored`；daemon 校验身份后 `resolve_uncertain(cancelled, execution_stopped=True)`，槽位回 idle，结果**不采用**，暂停状态不动（审计 `hold.released`），Codex 槽再 `codex_queue.clear`（否则下一次投递被「still unsettled」拒）。旧代 / 别的会话 / 未 held 的伪造事件一律拒绝。
- **service 模式（2026-10-14 用户定选项 B：service 也一样）**：`fx:escalations` 在 service 模式只放 `dialog:` / `blocked:` 两种记录；`parse_service_decision` 收 `escalations[]`（只许这两种键，形状同 task 模式），`on_service_supervisor` 校验 / 应用，第一次不答退回不计 `bad_replies`、之后与 task 模式同（dialog 转用户、blocked 计次）。非删除框：同 task 模式（同一套核验、超时 `dialog_escalation_timeout`、回落用户），`[dialog]` 消息绑定该 worker 正在做的请求 id、kind `followup`（请求中途结束也照投）。**blocked**：service worker 没有 Blocked 段，约定答案恰为 `{"blocked": "<原因 + 提议>"}`（请求自己的 reply 键里没有 `blocked` 时才算；`none` 等不算）→ 该投递结算但**不公布结果**，请求保持 `dispatched`，记 `blocked:<槽>`（带 request），给监工排 `[blocked] … (request <id>)`；监工裁定后以同一请求重新发给该 worker（`request_message` + `[Supervisor ruling]`），worker 的下一个答案照常公布。service 下给别的请求派单不算回答；请求先到终态（超时等）→ 记录作废（审计 `blocked.moot`），迟到的裁定只记日志。pi worker 的 `fleet_result` 对 `{"blocked"}` 放过本地 required 键检查。删除类照旧只交用户、零按键。
- **已知缺口（2026-10-14 缺陷 E / F / F′ 批次）**：监工答「approve」后若框在下一拍前因 TUI 重绘使屏尾 digest 变化，会按「换了框」重新问一次监工；`[dialog]` 的计时只从监工收到消息起算，监工一直忙时 worker 只受租约 `deadline_at` 约束；pi 槽的 hold 出口仍未覆盖（方案 ⑦-4）；FIFO 里 held / 等框期间堆的旧指令放槽后照序投出（方案 ⑦-3）。
- **标签没了（存活检查，2026-10-14 事故后用户定；`RuntimeV2.tab_missing`）**：每 30 秒发现某槽标签不在 → **只有 tmux 证明会话已结束才算「已停」**（`cmux_manager.tmux_session_alive`：服务器应答且无该会话 / `no server running` / socket 不在 → False；非 tmux 标签或 tmux 读不了 → None，无证据）。① 已停的 worker（已 READY、未暂停、不在交接 / 清空中）：照 §17 `start --resume` 对死窗的逐槽做法——`clear_slot` 释放租约与排队 → `respawn` 原位下一代 → task 用 `requeue_lost`、service 用 `replay_request` 重放丢失的派发 / 请求（计 `replay`，超 `max_attempts` task 判 blocked、service 发 timeout，`replay: false` 不重放）；**Claude 槽先接回原会话**（新一代 argv 带 `claude --resume <原 session>`，transcript 须在），接回的一代没 READY 就超时、或接回后再丢 → 全新一代 + 骨架交接单（工作目录有 PROGRESS.md 时写进下一步）；从没 READY 过的全新一代丢了只 hold（防重启循环）；暂停中只 hold，恢复后下一次检查补起。② 会话还在（只关了 client 标签）→ `reattach_client` 在旁边重开 client 标签、继承注册表条目，不 hold。③ 无证据 → 只 hold 该槽（`hold(pause=False)`，卡片照发），**不暂停 fleet**；监工（从不在 tmux、无证据）仍 hold + 暂停。④ 委派中的 supervisor_slot（§29）从不 hold（hold 会断掉钩子改投）：已停 → `subfleet.delegated_lost` 走现有收回（具名 `stop_child`，§19 记 failed）→ collect 用骨架交接单起下一代 → autostart / engine 重新 up；子 fleet 当时未答的请求照 §29 记进交接单，由调用方重投（子 daemon 自己死仍走 revive + `--resume` 在原子 Redis 里按 §17 重放）；会话还在 → 重挂父标签并同步 `subfleet.json` 的 `pane`；§19 占位标签没了 → 只审计。子 fleet 收养的监工已停 → 只 hold 不暂停，等父 fleet 收回；会话还在 → 重挂 view 标签。
- **墙闸先于看门（集成时接上）**：`Watchdog.check` 抓屏后先调 E 的 `Fleet.wall_gate`（§23「E 先于 F」）；命中墙 → 本 tick 对该槽不按键、不 hold、不杀不换代。F、E 各自分支上都没接这一行，集成分支 integration/batch-20261013 补接（测试 `test_wall_on_screen_trips_the_brake_and_the_watchdog_presses_nothing`）。
- 参数：`--stall-timeout`（原有；缺省 1800 → 1200，2026-10-14 与板块轮动驱动一致；v2 下是只看转录的硬上限）、`--stall-window`（缺省 300，0 = 关窗口规则只留硬上限）、`--stall-cpu-pct`（缺省 5）、`--stall-grace`、`--stall-escapes`、`--prompt-table`；配置非法（负数 / 非数字）在 `validate_config` 拒绝。具名子 fleet（§29）条目 `timeouts: {stall, stall_window, request}` 可配 `stall_window`（取条目值，没写取内置缺省 300，不取父值）；`stall_cpu_pct` 不是时限，不进 `timeouts`，子 fleet 照父 config。`walled`（墙文案）不在本项，归刹车（E）。

## 25. 夜间切换：名单 profile 列 + `night on|off`（2026-10-13 用户新要求，feat/night-switch，开发中；集成分支 integration/batch-20261013 时由 §20 顺延为 §25）

用户原话：「夜间模式切换的时候，把 Worker 的任务热载，当前的 Worker 卸载，然后根据从断点的地方，继续用 FlashNext 的免费版继续跑」。总设计 v2 § 4.13。本节在 §16 名单热加载、§17 断点续跑、§12 档案之上增加，不改 slot/generation 标识、一页布局、交接流程与 CLI 边界。

- **名单 `profile` 列**：`workers: [{name, role?, profile?}]`，`profile` 是档案卡 id。启动 / 热加载时 daemon 解析卡（`profile_launch.resolve_launch`），并对该槽合并后的配置调 `runtime_v2.validate_config`（组合表入口，A 项接管后读 `fleet.yaml#cli_pairs`）；未知卡或组合被拒 → 整份名单拒收（§16 语义），一条不放宽。
- **换模型 = 该槽下一代**：槽名不变（换名单名字会被「本 run 已用过」拒，切回日间就做不到）。`profile` 变了的槽：在途回合做完、空闲（无在途投递、空闲已确认）后，按交接同样的方式写机器骨架交接单、存屏、`launch_held` 起下一代（新卡的 CLI/模型）、`tab_into` 原位、关旧 tab、v2 `confirm_retired`；队列不出队，新代 READY 后照常投递。切换待办期间该槽不再接新投递。preflight 或关窗失败 → 按 §12 阻塞 task，不暗换。
- **断点交底**：本次切换的槽全部 READY 后，给监工一条 `[Night switch <task>]` / `[Day switch <task>]`：各槽新标签与档案、产出已验 / 缺、已完成派发（与 §17 交底同一个 `RuntimeV2.progress()`）、留在槽上的排队指令（明说不要再派），「只补派缺的」。无 running task 时只记审计。
- **触发**：`start --roster day.yaml --night-roster night.yaml`（两份名单槽名必须一致）；`cmux-fleet night on|off|status --config`：先校验两份名单，再原子写 `<run>/night.json`（即 /sleep 触发文件）。daemon 每拍读它决定跟哪份名单。监工不换（Fable 监工待 Claude 监工适配层）。
- **刹车占位**：Redis `fx:night {on, brake_card: night|day, since}` + 审计 `night.on/off`；夜卡封顶的判定归刹车项（总设计 v2 2.5）。
- **与 live profile 的关系**：task `apply_profiles_live` 生效时，名单换卡推迟到该 task 结束（审计 `roster.profile_deferred`），避免两套来源来回切。
- **v2 按槽 CLI**：v2 运输里判断「官方 CLI 窗口 / Pi 桥」的地方改读该槽本代的配置（`runtime_cfg`），无 profile 的槽与原来完全相同。
- **换 CLI 只放行夜间换卡（BC 复核 r1 P1-01）**：`preflight_profile(launch, cli_change_ok=False)` 在 v2 下仍拒「本槽 worker CLI 与 run 不同」；只有名单换卡 `swap_profile` 传 `cli_change_ok=True`——它的每个槽已在 `night.resolve_launches` 过了组合表（`pair_refusal` + `validate_config`）。task 级 live profile 切换等其它路径照旧拒换 CLI。**同 run 内 Claude TUI 槽 + Pi 桥槽混跑尚未真 cmux 验收，生产夜间使用前须先做 §4.13 验收。**
- **未验收**：只有 fake cmux / 真 Redis 回归；`codex+pi` 未被今天的矩阵放行（等 A 的组合表），真实日间 Opus → 夜间 FlashNext → 早上切回的整场验收（总设计 v2 § 4.13 验收句）未做。
- **与 §22 换代阈值、§12 模型卡、§23 刹车的统一（集成时定）**：只有一套「按模型」机制——夜间切换只改该槽的 `profile`（即 `runtime_launch` 覆盖），不另带阈值；换代阈值在每次投递前由 §22 `handoff_limits` 按该槽**本代运行模型**重新解析（名单 worker 行 > 名单 `handoff:` 表 > config 表 > 档案卡 `handoff_ratio`（§12，3b 层）> 内置 / 旗标），所以切到 FlashNext 的槽自动用 FlashNext 卡的 0.5、切回 Opus 自动回 1M 窗口。`profile` 列与 §22 的 `window` / `handoff_ratio` 列、`handoff:` 表在**同一份名单、同一个解析器**（`roster.parse_entries` + `roster.handoff`）里校验，任一不合法整份拒收；daemon 跟哪份名单（日 / 夜，`night.json` 决定），就用那份名单的 `handoff:` 表。名单热加的新行：带 `profile` 的由 `night.resolve_launches`（C 在时含 `model_usage.check_slots`）计票，不带的由 C 的 `default_capacity_problem` 按缺省模型计票，同一行不重复计。E 的分池墙降档遇到被本节换过卡的槽作废（§23 `brake.downgrade_superseded`）；E 的夜间现金封顶读本节的 `fx:night.brake_card`。

## 26. 验收修复（2026-10-13 真实 cmux 验收 373d22c 的 P0 / P1；main 上为 §23，集成分支 integration/batch-20261013 时顺延为 §26）

验收报告：外部项目 `tasks/plans/cmux-fleet-acceptance-20261013.md`。本节只修报告点名的缺陷；唯一 daemon、slot/generation 标识、一页布局、task/service 协议、§16–§22 其余不变。原则：有副作用的事（跨 workspace 写文件）由可信 Python 做，模型只在自己目录里写。

- **P0 子 fleet 交付路径**：子 fleet 的规则（CONTRACTS.md「path 在生产方自己目录」、派单尾巴「只写自己子目录」）与「写父路径」互相顶着，Claude worker 拒写，4 次子 fleet 全败。改为：父产出 `<父 ws>/<slot>/<rest>` 映射成 `<子 ws>/worker-1/<rest>`（不在该 slot 目录下的产出按文件名放 `worker-1/`），映射记进 `subfleet.json` 的 `deliver`；子目标只写子路径，并说明「谁都不写出本 workspace，结束 COMPLETED 后父 daemon 复制」；契约父任务的目标只列接口编号 + 父 `contracts.json`（读形状）。`collect` 在结局 `completed` 时、任何检查之前复制：`worker-1/<rest>` 优先，没有则取子 fleet 其他 worker 目录下同一相对路径（恰一个才算），跳过符号链接与空文件；非 completed 不复制；复制出错只记 `subfleet.deliver_failed`，窗格照常收回、由「产出缺失」判 failed。复制后照旧跑「产出缺失」检查与 `escalation_finished` 的接口字面检查——**父接口的 `path` 仍是父路径，语义不变**。`[sub-fleet interfaces]` 通知里的路径改为绝对路径（原来是子 workspace 相对路径，父监工会读成自己的）。
- **P1 续跑接不回 Claude**：原生安装的 Claude 进程名是 `claude.exe`。`RESUME_CLIS` 收成 `('codex', 'claude')`；`has_cli_process` 按 `ps -o comm=` 的可执行文件名去掉 `.exe` 比对；`node` 只在 `ps -o args=` 的脚本路径含 CLI 名时算（npm 安装），裸 shell、无关 node 进程不当活 CLI（续跑复核 P2-N1）。
- **P1 READY 判定**：startup 回复的最后一个非空行（去掉 markdown 强调符号 `*`、`_`、反引号与句末 `.`）等于 `READY` 即通过，`NOT READY` 不算。不通过（且不是 failed / aborted / launch 未就绪）→ 先在窗格里打一次「reply exactly READY」纠正、slot 仍 `starting`，第二次仍不对才按原规则 blocked。纠正次数以 `startup_nudged = startup_deadline` 记，每次要 READY（起代、adopt、收回、换代）各一次；打字在事务之后（同 `rotation_pending`）。
- **P1 子 fleet 次数上限**：同一任务编号的子 fleet 记录（`sub/M0`、`sub/M0-attempt-<n>`，失败与起到一半回滚的都算）达到 `--max-attempts`（缺省 2）→ `subfleet.check` 拒绝（`max-attempts N: sub-fleet M0 already started N time(s)…`），覆盖 escalate 受理、approve、`spawn_child`、操作员 `subfleet spawn`。契约任务里 escalate 因此被拒 → 任务行 `failed`（原因写明）、告诉监工可换法重派或结束；不回 worker「自己继续」。普通任务照原拒绝路径（worker 与监工都收到原因）。
- **P1 选择框卡死**：所有 Claude 窗格（worker，及由 worker 窗格变成的子 fleet 监工）启动参数加 `--disallowedTools AskUserQuestion`。只做预防；「回合停在交互框」的检测归 watchdog 那一块。
- **P2 `Interfaces:` 行**：接口文件字面检查已过、报告却没有合格的 `Interfaces: IF-x@n provided: ready` 行 → 任务行原因写明缺哪一行，并给 worker 一条挂在该任务名下的提示、给监工一行「已问 worker，不必重派」；它下一份报告重结这个任务。文件本身不过检时不提示（照原 not ready）。

**验证**：`python3 -m pytest -q tests` 全套通过（新增 `tests/test_acceptance_fixes.py`：契约父任务 → escalate → approve → 子 fleet 只写自己目录 → 父复制后检查过、接口 ready、M0 done、父监工收到 completed（验收 P0 原路径的回归）；另一子 worker 目录的产出被认、`worker-1` 优先；未 completed 不复制；READY 末行判定、一次纠正后通过、第二次 blocked；次数上限在 escalate 时判 failed、`check` 在任务仍在跑时先报「already running」；`Interfaces:` 行提示后重结为 done。`test_cmux_control` 增 `claude.exe` / npm node / 裸 shell / 无关 node 的进程判定；`test_fleet` 断言 argv 带 `--disallowedTools AskUserQuestion`；`test_v2_codex_claude` 的 startup 用例改为两次不对才 blocked）。

**已知缺口（P2）**：子 fleet 收回后旧子工作区仍留一个 history 标签（`close_all` 只关登记里的 surface，真实 cmux 下 history 为何没被关未查明，需真机复现）；新建子工作区抢焦点、子目标原样带父 worker 指令全文（含 escalate 说明）、`roster.changed` 续跑后重记 handoff、M1 用 stub 即判 done 后不再收到 `[contract] ready` 等报告 P2 未动；交付映射固定落 `worker-1/`，子监工若把终稿派给别的 worker 且两处都写了同名文件，以 `worker-1` 为准；真实 cmux / Claude 会话上未复验，独立复核、合并、部署均未做。

## 27. 顾问（advisor）：派工前选模型 / effort / 领域，中途裁接力或换人（2026-10-14 用户新要求；第一阶段 feat/advisor、第二阶段 feat/advisor-integ 均已合进 merge/advisor-into-deploy）

设计稿：外部项目 `tasks/plans/cmux-fleet-advisor-design-20261014.md`（v0.7，单一真相源；用户边界 §1 九条不可改、待拍板 §11 按缺省实现）。实现与复核：`cmux-fleet-advisor-impl-r1-20261014.md`、`cmux-fleet-advisor-review-fable-r{1,2,3}-20261014.md`（r3 PASS）、合并报告 `cmux-fleet-advisor-merge-r1-20261014.md`、合并缝复核 `cmux-fleet-advisor-merge-review-fable-r1-20261014.md`、第二阶段复核 `cmux-fleet-advisor-integ-review-fable-r1-20261014.md`。唯一 daemon、slot/generation 标识、一页布局、task/service 协议、§12 Phase 5 换代机制、§16–§26 均不变；顾问不是第二套 dispatcher。

- **是什么**：daemon 替监工做的一次性会诊——`claude -p --model fable --effort high`，精简会话（`--setting-sources ""`、`--strict-mcp-config`、`--disable-slash-commands`、`autoMemoryEnabled:false`），cwd 固定 `<run>/advisor/`，90 秒超时重试 1 次。不常驻、不占槽、不进布局。**（2026-10-14 起 v2 task 模式改常驻顾问标签，见下文「常驻顾问」条；本条一次性调用只留给 v1、子 fleet；service 主 fleet 2026-10-14 起也常驻）** **只听监工**：输入 = 任务书 + 这条 dispatch 指令 + 监工可选 `advise_note` + 槽现状 + 该 CLI 的模型卡与能力表 + 领域清单；不含 worker 对话。顾问不与 worker 直连。
- **开关 = roster `advisor: on|off`，缺省 off，热载**（§16，run 不重启）：入口只有 roster——用户改文件、`fleet advisor on|off`、监工 decision 键 `advisor`；`start --advisor` / `start --resume --advisor` 只是往 roster 写 `advisor: on` 的语法糖，config.json 不存开关。带夜间名单（§25）时开关日夜两份一起写，`night.check_rosters` 要求两份 `advisor` 相同（顾问按 run，夜间切换不开关顾问）。kit 校验跟着「上线点」走：`set_advisor(on)` 热载时 kit 坏拒开不写 roster（审计 `advisor.rejected`）；`start` 时 `--advisor` 或 roster 已写 `on` 都在开资源前校验；daemon 启动在线时 kit 坏整份拒绝。
- **必问**：顾问在线期间，每条 dispatch 投递前都过顾问（含契约任务被 `depends_on` 挂起、放行时再过）。通知 / 纠错不会诊。**关着则一切不变**（原有全套测试零改动是回归证据）。关闭只停新会诊：已在跑的代与按配置单起的代不回退，举着等首张单的槽按缺省放闸，等票中还没起的配置单丢弃（`advice.wait_dropped`，排队消息按槽在跑的 runtime 投）。
- **配置单**：`{model, effort, domains[], reason, capability?}`。`model` 只在该槽 CLI 的档案卡里选（Claude 槽 = `profiles` 块里 `claude-{fable,opus,sonnet,haiku}` 四张静态卡，链同名模型卡，参与 §12 计票）；`effort` 写进 `runtime_launch.extras`，漂移检查扩到 extras；`domains` ⊆ kit 总表 ∩ 档案卡 `kit.domains` 白名单；pi / codex 槽 `domains` 必须为空、effort 忽略；`reason` 必须引用能力表 `config/fleet.yaml#capability_matrix.yaml`（领域 × 任务类型 × 模型，每格带 evidence；`untested` 格禁选除非全 `not_enough`）。
- **生效**：launch 指纹 = (model, effort)。指纹变 → 走 §12 Phase 5 换代（增代 1.x），顾问在线时免双重 opt-in，但**先过 `card_gate`**（主机不符 → `block_profile`；票满 → 消息留队列、`profile_state=pending`、配置单存 `advice_wait`、每拍重过闸有票即切，不重复会诊）。codex 槽 native 子 agent 未完时 `switch_profile` 只延后不起新代，配置单同样留在 `advice_wait`，不投旧 runtime。只有领域新增 → **热载不换代**：该领域 `RULES.md` 前置进指令投递 + `kit.link_domains` 把 skills / agents 链进 worker `.claude/`，`installed_domains` 累加；领域减少不动作。首代 Claude 槽（run 启动时顾问在线才举着）`launch_held=True` 举着等首条配置单（不受 60 秒限、tick 不判超时、`others_active` 不算忙），放闸时写 launch、装包、拼 CLAUDE.md。
- **一槽一份启动记录**：`runtime_launch`（model / effort）+ `installed_domains`，只在真起一代时改——首代放闸、派工前换代、rescue respawn / reassign 的新代；等票或延后中的配置单只存在 `advice_wait` / `advice_preset`，不暂存进记录。交接、非顾问换代、v2 `--resume` 的下一代**沿用这份记录**（model、`--effort`、领域链入与 CLAUDE.md 里的 RULES），不看顾问此刻开没开、不再会诊；`release_default` 与 `init_worker` 清记录回槽缺省；关着起的 daemon 遇沿用记录的槽按需加载 kit（失败记 `advice.kit_unavailable`）。**（2026-10-14 用户定：配置文件一脉相承）启动记录无条件沿用，只有新的顾问配置单改它**：布局（`--setting-sources project` / `--strict-mcp-config` / `--effort` / 关 autoMemory、CLAUDE.md 拼 RULES、`.claude/` 只链所选领域）也属记录——`installed_domains` 为 `''` = 槽缺省布局（用户级 settings / skills / MCP、整包 kit 拷贝、CLAUDE.md 只有角色说明），为 JSON 列表（含 `[]`）= 顾问布局；`advice_domains` 里暂存的待起配置单也算顾问布局。`advised_layout` 只读记录，不再问 `advisor_online()`；每代快照里的 `advisor` 键按布局写或删，不从 daemon 内存开关漏进去（此前带 roster 卡的缺省槽顾问在线时也会被拼成顾问 argv）。顾问开关只决定**槽位首代**（`spawn_ready`）：在线 → v2 Claude 举着等首单 / v1 顾问布局零领域；关 → 缺省。举着的首代未放闸前不写记录（放闸由 `release_hold` 写配置单、`release_default` 写缺省）；`resume` 时仍举着从未放闸的槽按缺省起下一代并去掉 `advice_hold`。`advisor on/off` 不改任何记录（`release_default` / `init_worker` 只作用于未起的首代，不清在跑槽的记录）。旧 run 无记录字段 = 缺省（与缺省记录同字节，无需补写）。热载配置单也是已生效配置单：缺省布局槽被热载领域后，记录变顾问布局，下一代按配置单（只读项目设置）起。
- **kit**（`cmux_fleet/kit.py`，`--worker-kit DIR`，由外部项目 `scripts/context/build_worker_kit.py` 组装）：`core/CLAUDE.md`（项目核心版去领域索引节 + 无 EverMemOS 豁免行）、`context_domains.yaml`、`domains/<领域>/{RULES.md,skills/,agents/}`、全集 `mcp.json`（延迟加载，不按领域切；不含 evermemos / wechat）、`MANIFEST`。启动整份校验，不合法在开资源前拒绝（同 roster 坏值）。换代前先清 `.claude/{skills,agents}` 再链；Claude 槽 `CLAUDE.md` = role scope + core + 本次领域 RULES；spawn 提示不再要求读 AGENTS.md；`claude_argv` 加 `--effort --setting-sources project --strict-mcp-config`，settings 文件加 `autoMemoryEnabled:false`。与免打字批次并存（2026-10-14 合到在役线）：顾问起的每一代 Claude worker（首代放行、rescue respawn、交接换代）照样带钩子通道（Stop 阻塞钩子 + asyncRewake + StopFailure，settings `env` 含 `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0`）和 `DISABLE_AUTOUPDATER=1`，目录里照样装 `REVIEWER.md`；顾问自己的一次性 `claude -p` 也带 `DISABLE_AUTOUPDATER=1`。决策 JSON 里 `advisor` / `consult` 与 `withdraw` 并存。**fleet 全员零记忆层**：worker / 顾问不读不写 memory、不接 EverMemOS。
- **失败处理**：顾问起不来 / 超时 / 非 JSON / 配置单校验不过 / 卡片 JSON 坏 → 重试 1 次后用上一张同任务配置单，否则按槽当前 runtime + `domains: []` 派出，审计 `advice.unavailable`，监工收 `[advisor unavailable] 按缺省派出`，**不阻塞**。Phase 5 换代任何一步失败沿 §12 原规则。任务书自己开了 Phase 5 live（switching / applied-live）时顾问让路。service 模式不会诊。
- **审计与显示**：`fx:advice:<ticket>`（输入摘要、输出、耗时、顾问 token 与 `total_cost_usd`、生效方式 respawn / hotload / none）；`fx:agent:<slot>` 的 `advice_ticket / advice_reason / installed_domains / advice_last / advice_wait`；control 行 `adv=<model>/<effort>/<domains>`；监工收 `[advisor] <槽> → <model>/<effort>, domains: […]（reason；方式）`。pi 监工 `fleet_decision` schema 放行 `dispatch[].advise_note` 与 §20 的 `dispatch[].id`。
- **第二阶段（已实现，feat/advisor-integ）**：监工按 AGENTS.md 判定节（Blocked/Not done 属工具或能力缺口、连续两轮同一错误、Done 但 Evidence 不过验收且纠正一次仍不过）在 decision 加 `consult[{worker, task, finding, want}]`；顾问 `kind="rescue"` 裁 `respawn`（同槽 worker-N → worker-N.1，走 §4 交接 + 新配置单从零装，`handoff_hint` 进交接骨架下一步，原指令重投）/ `reassign`（任务收回，另一空闲槽按配置单起，监工重新 dispatch）/ `give_up`（BLOCKED 带原因）；同槽接力上限 2 次。中途一律换代不热载。respawn / reassign 换模型前同样过 `card_gate`：主机不符阻塞；respawn 票满不动作、不计接力次数、告诉监工未执行；reassign 票满或延后时留 `advice_preset`，监工重派时照单过闸起。顾问关着时 `consult` 只回一条 `[advisor off]`；会诊中途被关的 consult 同样回这条。pi 监工 schema 放行 `consult[]` 与 `advisor`。
- **投递通道（2026-10-14 合并 integ/no-typing-batch 后）**：顾问不另开投递口。发给监工的 `[advisor] …` / `[advisor off] …` / `[advisor unavailable] …` 仍只追加在下一条监工消息末尾（`ADVICE_NOTES`，consult-only 回复时单独排一条），随那条消息走监工的通道：Codex 监工 = `codex queue`，排队文本末行是不透明投递令牌 `(fleet delivery <token>)`，按令牌绑定回合；pi 监工（新缺省）= bridge；Claude 监工 = 原 typed 路径。发给 Claude worker 的内容（派工指令 + 领域 RULES 前置、接力说明、rescue 交接提示）进该槽该代的 Stop 钩子信箱（v1 `fx:hook:<slot>:g<n>:mailbox`，v2 按 run 前缀），不再打进输入框；信箱按代分键，换代（Phase 5 / rescue respawn / 交接）先增代再退旧 tab，旧代钩子 `alive()` 即为假、空手退出，旧代迟到 Stop 记 `turn.stale` 不结算。举着等首单的槽 CLI 尚未 exec，没有钩子在等，看门按「starting 无投递」只看不动。顾问布局（`--setting-sources project`）下 `--settings` 带的钩子与 env 是否照常生效未真机验（BLOCK_CAP 另由 actor 进程环境兜底）。启动记录仍是下一代的唯一来源：批次只读 `runtime_launch`，钩子槽从子 fleet 归来走 `v2.respawn` → `spawn` → `advised_layout`。§28 每条后清空与钩子通道互斥（开 §28 须 `--no-claude-hook-delivery`，走 typed）；§28 同代换绑只改 thread / transcript / ctx 与 bus session+fence，不碰 `runtime_launch` / `installed_domains` / 布局，举着的首代没有投递、不会进 clearing。
- **常驻顾问（2026-10-14 用户新要求「显式显示 advisor 窗口，并能正常的 handoff 换代」，`feat/advisor-resident`；设计增量 `cmux-fleet-advisor-resident-20261014.md` v0.3 §2c / §2c-8，复核 Opus r1 FAIL → r2 PASS）**：适用 transport v2 + 主 fleet（task 与 service 模式；service 2026-10-14 起，`feat/advisor-resident-service`）；v1、子 fleet（`adopt` / `parent_config`）仍走上面的一次性 `claude -p`。service 模式下会诊只发生在举着的首代 Claude worker 首单放闸时（派工与外部直派不会诊，supervisor_slot 不举着）；会诊消息按 ticket 绑定、不受「请求已终结即丢」规则约束；§28 只作用于 worker，顾问标签不在 workers 里，碰不上。
  - **槽身份**：槽名 `advisor`，进 `names`（总线、Stop 钩子、看门、§22 换代都用得到），**不进 `workers`**：不能被派工（不在 `dispatch_targets` / `NAMES` 路由表）、不算 `max_busy`、不算 `model_usage`（与监工同口径不计票，fable 卡 `max_concurrent: 1` 只数 worker）、不收 CONTRACTS.md、不会被 `others_active` 当忙。名单 / `--worker-names` 禁用 `advisor` 作 worker 名。代数标签 `advisor` → `advisor-1.1` → `advisor-1.2`（`slot_base`）。
  - **显式窗口**：`start_all` 排完页面后起它的标签，`tab_into` 进监工那一栏（监工标签之后）；cmux 元数据 `type=advisor`。换代 / 看门轮换 `tab_into(new, old)` 原位接替；`--resume` 原 tab 在则原位、不在则重新放进监工栏，整页重建时排完再放。
  - **固定 argv**（`advisor.resident_argv`，不经 `runtime_launch`，永不被配置单改写）：一次性 argv 去掉 `-p` / `--output-format` → `--model fable --effort high --setting-sources "" --strict-mcp-config --disable-slash-commands --system-prompt-file <run>/advisor.md --tools Read --add-dir <run>/handoffs`，`--settings` 换成该代 settings 文件（Stop 钩子 + 钩子通道 asyncRewake / StopFailure + `autoMemoryEnabled:false` + `DISABLE_AUTOUPDATER=1`），钩子通道开着时加 `--append-system-prompt <DECLARATION>`；cwd `<run>/advisor/`。`<run>/advisor.md` = `agents/advisor.md` + `RESIDENT_NOTE`（首轮回 READY、每条消息是一次会诊、更正消息怎么回）。**恒为官方 Claude CLI**：`agent_cli` / `tui_slot` / `runtime_cfg` 按槽名写死 claude / fable / high，不随 run 的 `--worker-cli`（codex / pi）走；actor 不对它做监工 × worker CLI 配对校验。
  - **开关与就绪拆开**：`advisor_enabled()`（= 原 `advisor_online()`，保留别名）管首代举着、consult 受理、off 提示、幂等；`advisor_ready()` = 开着且顾问标签 idle / busy。`advisor_tick`（daemon tick，事务外）：开着而无标签 → 起首代；关掉 → 当前会诊做完（租约不在 offered / received）即退役标签（排队会诊撤回）、`names` 去掉它；再开起下一代。
  - **会诊异步（票）**：派工前（`advise_before_delivery`、首代 `release_hold`）与 rescue 都改为「投进顾问总线队列 + 挂票」：`fx:advice:<ticket>` 即票的状态（`pending → answered → ok / unavailable`，含 `mid / attempt / phase / deadline / request`）；派工槽记 `advice_pending`，rescue 记 `fx:advice:rescue_pending`（一次一条；更正时附当前空闲槽，答复期间候选变忙不会拿旧候选再问），消息照留在原队列，daemon 照常转，之后每拍读票。时限固定 180 秒（`ADVICE_WAIT`）：等顾问 READY / 排队一段 180 秒，顾问拿到这条后（offered / received）答题一段再 180 秒；超时撤回仍排着的消息、按缺省。多条同时到按到达顺序串行（顾问一次一条）。第一次进入等待（顾问未就绪或前面有会诊）时发一条 `[advisor] waiting: …` 注记，随监工下一条业务消息走（`fx:advice:notes`，合并缝 M1）。
  - **首代举着**：run 启动时开着 → 举着的 worker 等顾问 READY 后会诊；顾问起不来（`spawn_failed`）或 180 秒内没有可用配置单 → `release_default`（缺省布局）并发 `[advisor unavailable] 按缺省派出`。中途关顾问 → 仍举着的当场 `release_default`，在途票当场按缺省结掉（`advisor_abandon`，排着的消息撤回）。
  - **结算分支**：顾问回合 `tui_candidates` 只交原文，`settle` 的顾问分支 `release` 后调 `advisor_settled` 把原文存进票；不走 Done / Evidence 校验、不 `report`、不 `remember`、不碰任务状态、不进刹车。读票时用该槽的配置单校验（`validate` / `validate_rescue`，从 Markdown 代码块里取 JSON 也行）；坏一次 → 给顾问排一条 `[Advisor correction <ticket>]`（附原输入与错误）；仍坏 → 本条按缺省，**不计 blocked**。
  - **故障不阻塞**：看门判死（看门照常 kill + 原位下一代，失败只 hold 不 `finish blocked`）、标签被关（存活检查 hold）、撞墙（回合原文或屏幕是墙：不进刹车、不暂停 fleet）、启动超时 / 启动没回 READY（置 dead，不 `finish blocked`）、租约过期 / 投递失败（hold）——`RuntimeV2.hold` 对顾问槽只标 `advisor_fault`、不暂停 fleet、不发卡给监工、不 hold 别的槽；在途那条会诊的消息变 uncertain / cancelled，等它的槽下一拍按缺省；`advisor_tick` 起下一代（骨架交接单，`source=advisor-restart`；两次重启至少隔 60 秒，fleet 暂停时不起）。**起下一代只有一条路 `advisor_restart`**（故障重启、§22 换代、看门轮换、`--resume` 都走它，复核 astra r1 P0-01）：先退旧 tab 再起新代并放回监工栏，从不两个顾问会话并存；旧 tab 关不掉（held）/ 新代起不来（dead + `spawn_failed`）/ 放不进监工栏（只记 `advisor.placement_failed`）都只算顾问故障——不抛进 daemon、不 blocked、不暂停、不 hold 别的槽、不取消派工；排着的会诊当场撤回，等它们的槽下一拍按缺省派出，60 秒后再试。任务结束时顾问的在途回合不 hold，答案迟到即丢（`advice.late`）。
  - **换代（§22）**：投递前按 fable 阈值（1M 窗口、`--handoff-ratio` / 名单表）判，过阈值即换代；顾问只有 Read 写不了草稿，所以不发交接回合，`start_rotation` 直接走 `advisor_restart(source=handoff)` 用 daemon 骨架交接单：近期配置单（`fx:advice:recent` 最近 10 张票的 kind / 槽 / 输出 sheet + reason），**不含任何 worker 对话**。换代只在空闲边界发起，在途那条先结算；排着的会诊由继任者接着做。
  - **测试替身**：注入 `advisor_runner` 的测试（含 v2）仍走一次性同步路径（不起顾问标签），原有断言不改；常驻路径由 `tests/test_advisor_resident.py` 用真 Stop 钩子入口驱动顾问标签。
  - **验证**：干净 PATH 全套 `pytest -q tests -p no:cacheprovider` 两遍 rc=0，各 1664 passed 5 skipped（基线 1027f4f8 为 1647 passed 5 skipped，+17 条 `test_advisor_resident.py`）；复核待做。
  - **未真机验**：`--system-prompt-file` 与 `--append-system-prompt` 同时生效、`--setting-sources ""` 下 `--settings` 带的 Stop 钩子照常触发、`--tools Read` 加 `--add-dir` 读交接单不弹权限框、cmux `tab_into` 进监工栏的实际位置、真 Fable 首轮回 READY 与按 `RESIDENT_NOTE` 回配置单、真换代；验证方法：dev fleet 开 `advisor: on` 起一次，看 `turn_ended` / `settled` 事件、`advice.queued → ok` 审计与标签位置。
- **已知边界（P2 名单，随上线挂账）**：一次性顾问调用（v1 / service / 子 fleet）同步阻塞 daemon 最坏 180 秒（常驻顾问已异步，不再阻塞）（租约 300 秒不过期，先看审计 `elapsed_s`）；fallback / `--resume` 后 runtime 无 effort 遇 medium 配置单多换一代（正确修法 = fallback sheet 写 `DEFAULT_EFFORT`，不改指纹比较——改指纹会撞漂移检查把 fail-open 变 fail-closed，已撤回一次）；Phase 5 live 与顾问同槽 extras 不等会 blocked；`take_pending_advice` 子串匹配；`--advisor` 不带 `--worker-kit` 零 skill 不告警；名单删掉从没派过活的举着 worker 不退役；四张静态卡参与 `route_task`（规划层无差异）；夜间切换 / 名单热载撞上举着或换代中的槽未覆盖；等票标识 v1 用消息哈希、等票无超时；rescue respawn 撞 codex native 子 agent 未单独处理；第二阶段复核 P2-1…P2-8（P2-7 已在合并时修）；设计稿 §12-4/7(agent 目录)/8 未真机验。**上线前提**：dev fleet 真跑一次「首派举着放闸 → 换代 → 热载」，顺带关掉 §12 存量「v2 Codex 监工 + Claude worker 真机热切换未实测」。

**验证**：`python3 -m pytest -q tests` 1295 passed 1 skipped（= 659084f4 的 1219 + 第二阶段 67 + 合并缝 9；含 cherry-pick 1a0579ed：夜间换卡测试自带 claude 桩、`fleet.yaml#walls` haiku 别名跟上 cli_tier_map 的 5.5；skip = `test_night_switch` 的「feature C 未合」占位；pi schema TypeBox 合同测试实跑通过）。干净 PATH（`PATH=/opt/homebrew/opt/redis/bin:/opt/homebrew/bin:/usr/bin:/bin`）1292 passed 4 skipped：多出的 3 个 skip 是该 PATH 下没有 node（fnm 装在别处），pi 合同测试三条跳过。复核：第一阶段 Fable r1–r3（r3 PASS）、合并缝 Fable r1 PASS、第二阶段 Fable r1 PASS；本次合并缝（feat/advisor-integ → merge/advisor-into-deploy）复核待做。独立部署未做。2026-10-14 fix/launch-record-inherit（启动记录无条件沿用，P2-1）：1304 passed 1 skipped（+9 个 `test_advisor_inherit.py`），复核待做。2026-10-14 integ/fleet-20261014（并入 docs/plan-change-via-supervisor + integ/no-typing-batch + cherry-pick cbaaccf5/0c08f7e4 + 本线修复）：`python3 -m pytest -q tests` 1567 passed 2 skipped（skip = `test_codex_via_pi.py:335`「pi+claude/task verified by a real smoke」、`test_night_switch.py:435`「feature C is merged」占位；pi 合同测试 node 实跑通过）；干净 PATH 1564 passed 5 skipped（多出 3 个 = 该 PATH 无 node，`test_pi.py:358` / `test_pi_decision_schema.py:15` / `test_pi_v2_contract.py:17`）。复核待做。

## 28. 每条后清空上下文（clear_after_result）

2026-10-14 用户选择：常驻 Claude Code service worker 每条结果后用 `/clear` 清上下文，优先节省 token。唯一 daemon、私有 Redis、slot/generation、布局、交接、task/service 请求与结果协议不变；本节仅增加显式开关下的会话边界，不改 §1–§27。

- **开关与入口**：`start --clear-after-result`，config 严格 boolean、缺省 false；只允许 `lifecycle=service`、`mode=service`、官方 Claude worker、Redis v2 typed delivery。**与 Claude Stop 钩子投递互斥**（2026-10-14 合进 integ/fleet-20261014）：钩子通道缺省开，Stop 钩子阻塞取下一条、worker 两条之间永不空闲，`/clear` 无从粘贴（钩子 PID 不退出 → 20 秒后 hold）；因此开关开着而 `claude_hook_delivery` 不为 false 时 `start` / `start --resume` / daemon 起 config 校验直接拒（`clear_after_result.validate`），提示加 `start --no-claude-hook-delivery`（写 config `claude_hook_delivery: false`）；`claude_hook.enabled()` 在开关开时也恒返回 false（兜底）。开关关时钩子通道行为不变。StopFailure 只有一个入口 `claude-stop-failure`：开关开 → 本节 `stop_failure` 事件；开关关 → 钩子通道的审计 `hook.stop_failure` + `turnend` 键。`--clear-timeout` / config `clear_timeout` 缺省 20 秒、须为有限正数。`start --resume` 沿用保存的开关与时限。关闭时不加新 hooks，StopFailure 事件只审计；supervisor 永远不清空。
- **钩子**：开关开启的 worker 保留 Stop，增加 `SessionStart`（matcher `clear`）→ `claude-clear`、`StopFailure` → `claude-stop-failure`。后两者只把 stdin JSON 转成持久事件，不等待 daemon。hook 命令以 `exec` 启动 Python，报告自身 PID；generation 仍固定在启动 argv。
- **结果后的闸门**：投递到 worker 的请求一旦公布终态（ok、invalid、failed、timeout 等），在同一 intent 中置 `clearing`、`clear_required=1` 与独立 deadline。排队但尚未投到该槽的请求超时不清空该槽。`pump` / `deliver` 不给 clearing 投递；正常 schema 错误和 StopFailure 不重跑请求。超时或无法证明停止的失败仍保留 current delivery / PEL，须等该投递自己的 Stop / StopFailure 证明回合结束，不能把结果发布当作停止证明。
- **安全粘贴点**：结果事务只落状态，不打字。后续 daemon tick 在无 current delivery、收到有效回合结束 PID、该 PID 已退出（只认 ESRCH）后，连续两次、至少相隔 250 ms 看到 Claude 底部空输入框与 idle footer，且无运行/取消/后台任务指示，才通过现有 `send_prompt_to_idle(pane, '/clear', bracketed=False)` 粘贴。屏幕仅是输入空闲闸门，绝不用于结算或判定回合完成。先持久化 `pasted` 再打字，失败或中断不盲目重发；捕获失败、无法识别的 TUI 或 PID 仍存活都会等到 deadline 后 hold。
- **同代换绑**：`cleared` 事件携带新 session / transcript、读到的旧 thread / clear token。校验 run、slot、generation、旧 session、旧 fence，要求 current delivery 为空、lease idle 且 PEL 已排空。共用的 Lua preflight 在真正 apply 时重验；bus session + fence 增 1、agent thread / transcript、ctx 清零及审计在同一 intent 中应用，可重放 prepared，applied 不重做。普通 `register_slot` 与 `bridge_session` 仍拒同代换会话。换绑后先保留 `clearing/confirmed`，待 SessionStart hook 也已退出、TUI 空闲再设 idle，下一条投递绑定新 session。
- **手工清空与失败**：idle 手工 `/clear` 走同样换绑；有在途投递、旧绑定不匹配、PEL 未排空或同代 clear 通知冲突 → 现有 hold，不结算、不猜当前会话。没有及时确认 clear（包括 hook 缺失、粘贴失败）→ hold、保留 clear_required；held 槽在无在途投递时仍可凭有效 clear 通知修复绑定，但现有 hold 的 run pause 语义保留，clear 完成不会替操作员或刹车解除暂停。
- **StopFailure**：仅同 run / generation / session 的在途 service worker 投递可终结。保留 delivery / fence / PEL consumer 校验，立即公布 failed 及 hook 错误文本、ACK 该投递，不排重试、不走墙的重投分支；然后走同一 clear 闸门。已有公开 timeout 时不覆盖结果，只收束执行并清空。旧代、旧 session、重复、无在途失败只有审计。
- **其他生命周期**：换代只在清空完成后的 idle 边界重新评估，clearing 期间拒绝 start_rotation；idle reclaim 不回收 clearing，service idle-stop 也把 clearing 算作活动。watchdog 对 clearing hands-off，由 clear deadline 接管，不按 Escape、不走 frozen 换代阶梯。§24 held-slot exit 保留完整身份校验，结束后先 clearing；手工 clear 导致绑定冲突的 held 不允许借旧回调放行。普通 daemon restart 保持 held/paused；显式 `--resume` 对清空未完成、换绑确认中或绑定冲突的槽走原下一代 respawn、确认旧窗格退役与 READY 闸，不接回可能已换 session 的旧窗格。

**验证**：新增 78 项（effect-plan / 输入闸 / 真实 Redis 集成）。Studio 真机（Claude Code + Haiku 5.5，3 worker，冻结目录）：非括号粘贴 `/clear` 执行、SessionStart(source=clear) 带新 session、清空后 Stop 照常；端到端 24 条真实提示词 23 ok（1 条模型输出格式 invalid，与清空无关），串内容 0，clear.pasted / slot_rebound / clear.ready 各 24、每 worker 8；修空闲判定（会话名印在上框线）之前的一轮实走了「确认超时 → hold → fleet 暂停」。复核 Opus r1 PASS（Fable 额度用尽改派）。2026-10-14 并入 integ/fleet-20261014（+ 钩子通道互斥）：`python3 -m pytest -q tests` 1650 passed 2 skipped；干净 PATH 1647 passed 5 skipped（多出 3 个 = 无 node 的 pi 合同测试）；互斥 + 启动记录用例 7 条在 `tests/test_clear_after_result.py`。复核待做。

## 29. 具名子 fleet：名单声明、外部起停、service 子 fleet（2026-10-14 用户新要求；feat/named-subfleet 合在 deploy/cmux-fleet 1027f4f8 之上，未上线）

设计稿：外部项目 `tasks/plans/fleet-ext-named-subfleet-20261014.md` v0.6（§1–§5；§3.1 双连接显示 = supervisor_slot 跑在 fleet 自带 tmux，由 `feat/named-subfleet-tmux` 合入）。节号：在役分支 §28 已被每条后清空占用，本节顺延为 §29。名单不写 `subfleets:` 段的 fleet，本节全部不生效；§19 由 escalate 起的 M<n> 子 fleet 行为不变。

- **声明**（`roster.py`）：名单 yaml 新增可选 `subfleets:` 段，每条 `{name, supervisor_slot, supervisor_role, roster, autostart?=false, mode?=service, claude?, worker_model?, timeouts?{stall, stall_window, request}, max_busy?, drain_s?=600, clear_after_result?, clear_timeout?, claude_hook_delivery?, worker_domains?}`（clear 三键：bool / 正数秒 / bool；`worker_domains`：领域名列表，可为 `[]`；类型不对整份拒收）；名字不与 worker 重名、一个 worker 只当一个子 fleet 的 supervisor、`supervisor_role` / `roster` 文件必须存在、子名单本身要能过校验。坏一条整份拒收（§16）。只追加新段校验，现有 workers / limits / handoff 解析不动。热载（`reload_roster` → `named_subfleet.declare`）：加行即声明（autostart 的下一拍委派）；删行 → 在跑的进 draining、无在途后收回；收回前加回 → 取消；改条目 → 下次 up 生效；§16「本 run 用过的名字整份拒收」只管 worker，子 fleet 名不受限。子 fleet 不进 `dispatch_targets()`。
- **`check()`**：传 `named=<条目>` 时跳过 `valid_child_id` / `task_level`（深度）/ `max_attempts`，「已在跑」按名字判；模型卡并发按条目 `worker_model`（未写取本体缺省）算；workers≥1、`child_workers_max`、`max_children`、`max_agents_total`、slot idle·READY·无在途、只限官方 Claude 照旧。
- **`child_config`**：具名子 fleet 的 `claude` / `worker_model` / `stall_timeout` / `stall_window` / `request_timeout` / `max_busy` / `clear_after_result` / `clear_timeout` / `claude_hook_delivery` 取条目值，没写取本体缺省（`DEFAULTS`：clear 关、`clear_timeout` 20 秒；`claude_hook_delivery` 不写 = 钩子投递开；`claude` 不写 = 走 PATH 上的 `claude`），**不取父值**；`worker_cli=claude`；workers / 角色 / profile 来自条目 `roster`（子 fleet 内 §16 照旧热载）；`mode` 按条目，service 子 fleet `lifecycle=None`、不投任务、不设时限、不写 `result.json`；会话名 `cmux-fleet:<名字>`；`supervisor_role` 委派时追加到子 supervisor 的 AGENTS.md / CLAUDE.md（规则文件照 §19 先备份、收回时恢复）。CLI 组合表对 adopt 的 claude+claude 放行到 service。
- **外部起停**（`named_subfleet.py`）：父 fleet（service）收 `{"id","op":"subfleet.up"|"subfleet.down","name","reason"}`，在 `Fleet.request` 里走独立分支——不过 `validate_request`、不进 `REQQ`、不受 `request_timeout`；同 id 重发按 `fx:subfleet:op:<id>` / 结果 / 墓碑判重拒收。程序闸先答能从状态答的：未声明 → `rejected(program)`；autostart 的拒外部 down；已 up / paused 再 up → `ok` 带同一地址；已 down 再 down → `ok`；up 时上限不够 → `rejected(program)`。其余问父 supervisor：service decision JSON 新增可选 `subfleet: {id, decision: approve|reject, note}`（v2 只许答本条绑定的 id），只批 / 拒；pi 监工（缺省监工）的 service `fleet_decision` TypeBox 同样放行这个可选字段（`id` 可省，由 runtime 绑到本条请求；`decision` 必填，`note` 文本），Python 侧 `parse_service_decision` 同口径。批准后由 daemon tick 执行：up 等 slot idle·READY（超 600 秒回 `timeout: slot busy`）→ 换代闸 → 委派 → 子 supervisor READY 后回 `ok {state, redis_socket, run_dir}`（超 `profile_ready_timeout + 120` 秒回 `timeout: child not READY`）；down 先把父 hash 置 `draining`（外部当不可用），等子 Redis 里 `REQQ` 空、各槽无在途 / 无排队，或超 `drain_s` 直接收回（审计 `subfleet.drain_timeout`，回 `timeout: drain exceeded`），收回后回 `ok {state: down}`。回执都写 `fx:result:<id>`；结果卡新增 `rejected` 状态。父 `start --resume`（service）的恢复对账取消所有未投出的 supervisor 消息，审批也在其中：`requeue_requests` 之后对 `fx:subfleet:opq` 里仍 `awaiting` 的 op 重发审批（审计 `subfleet.request_requeued`），`approved` 的照常由 tick 执行，保证每个已受理的 op 都有终态（复核 Astra r1 P0-01）。同一 id、同一 op / 名字在 op 未结时重发 → 回 `in_progress`（带 `op_status`），不另起审批；同 id 换 op、或已出结果 → 仍按重复拒收。
- **service 子 fleet 结局**：外部 down / 删行 / 父 stop / 父 resume 收回的结局记 `down`（`subfleet.json` state `down`），不记 failed、**不给父 supervisor 发「[Sub-fleet … failed]」**；子 daemon 无故退出（非 autostart）记 failed，同样不通知。
- **地址**：父 Redis `fx:subfleet:<名字>` hash = `state`（down / starting / up / paused / draining）/ `slot` / `redis_socket` / `run_dir` / `up_at` 等；`status` 树带 `name / mode / autostart`。子 fleet 自暂停（`fx:service.paused=1`）→ 父 hash `state=paused`，恢复后回 `up`。
- **autostart**：条目 `autostart: true` → slot 首次 idle·READY 那一拍委派（不经父 supervisor）。子 daemon 没了 → 在 tick 的收回分支之前先按原 socket 重启 Redis + `daemon --resume`；1 小时内第 3 次 → `subfleet.crashloop`、走原收回分支、父 hash `crashloop=1`，不再自动拉起（外部 up 批准后可再起，起来即清标记）。父 `--resume`：autostart 的照 §19 拉起；`autostart: false` 的收回并把父 hash 写 `down`。
- **换代闸**（N1）：每次委派前查 slot 上下文用量（§22 阈值，同一 Claude 会话的 transcript）；超阈值先 `start_rotation`，新一代 READY 后再委派；从未委派也从未派活的新会话超阈值只记 `rotate.impossible` 照常委派。autostart 子 fleet up 期间，父每 30 秒在子 supervisor 空闲时查一次，超阈值 → draining（`subfleet.rotate`）→ 收回 → 换代闸 → 再委派，同一个超阈值会话不会被原样再委派。（设计稿写「子 daemon 每拍查」，实现放在父 daemon：同一会话同一 transcript，结果相同，少一条跨进程通道。）
- **子 run 目录**：具名子 fleet 每次 up 复用 `sub/<名字>/`，上次的全部文件移到 `sub/<名字>/archive/<日期>/<时分秒>/`。
- **视图钩子**（合并 A2 双连接后）：父侧 `subfleet.parent_attach_view(f, slot, sid, task_id, ws_title)`（委派时：slot 跑在 fleet 自带 tmux → 不开占位、不 release，返回第二客户端信息，`rec['view']=1`；普通 cmux 窗格 → §19 占位 + release）与 `subfleet.parent_detach_view(f, rec, child_mgr, child_run)`（collect 时：view → `detach_child_views` 兜底断开子侧遗留客户端、父标签原地改名；否则 §19 handback / adopt 回占位处）；子侧沿用 A2 的 `attach_child_view(f, sid, peer, info)`（`start_adopted`）/ `detach_child_view(f, sid)`（`hand_back`）。**启用键**：`start --roster` / `start --resume --roster` 在首次 spawn 前把 `roster.load_subfleets()` 写进 `cfg['subfleets']`；`reload_roster` 接受新名单时同步改内存 `self.cfg` 并经 `persist_roster` 写回 config.json（`tmux_slot` 每次 spawn 读它）。子 fleet config 不继承 `subfleets`。**热载已有普通窗格的 slot**（选定最小行为）：本次委派走 §19 move-surface 并审计 `subfleet.view_fallback`，该 slot 下一代起在 fleet tmux 里、此后双连接；不为此强制换代。
- **审计**：`subfleet.declared / request_in / approved / rejected / autostart / autostart_wait / up / paused / draining / rotate / drain_timeout / stopping / down / crashloop / revive_failed`；`subfleet.up` 带 control 行 `<名字>: up via <slot> since HH:MM`。

- **收回 = 退役旧会话、父槽起下一代，带交接单（用户 2026-10-14 定，设计稿 v0.6 方案 A；已实现）**：免打字批次规定钩子通道上的 Claude worker 永不打字（§19 R1-01），所以具名子 fleet 收回时不再「同一会话换回 worker 规则原地回来」，而是对父槽走 §4 换代：
  - **旧会话写交接单**：`collect` 把槽置回 idle 并记 `subfleet_return`（子 fleet 名、up / down 时间、结局、子 fleet 答过的请求 id 与状态、down 时仍未答的请求 id——计划内 down 在向子 fleet 发 stop 前从子 Redis 读进 `subfleet.json`，崩溃 / 父 stop 时子 Redis 还在则当场读），然后 `start_rotation`，提示为 `handoff.SUBFLEET_RETURN_PROMPT`（`DUMP_PROMPT` 的同格式变体：说明子 fleet 已 down、监工角色结束）。投递照在役换代路径进该槽该代的钩子信箱，不打字。委派期间该会话作为子 supervisor 走 typed，收回时没有父侧 Stop 钩子在等；它最后一次 Stop 起的 asyncRewake 兜底（`claude_rewake`）在路由指向 typed 的子 supervisor、而本槽自己的通道是钩子时**不退出**，委派期间原地等，收回后按原兜底规则（无钩子在等 / 在结算、过 GRACE）把交接投递交给 Claude；该回合的 Stop 已路由回父通道，按 §21 绑定结算 → `finish_rotation` 合并模型三段。
  - **骨架兜底**：收回后 `RETURN_WAKE_S`（180 秒 = 末次 Stop 的 stopping 标记 + GRACE 余量）内没人取走交接投递（没有活着的 rewake）→ `subfleet.return_untaken`，直接用 daemon 骨架交接单换代。未被取走的交接投递没跑过，**不 hold、不暂停 fleet**；已被取走而超 `handoff_timeout` 的照 §4 原规则 hold。委派的会话已死（`collect` 收不回）→ 不等，骨架交接单 `handoffs/<slot>-g<n>-return.md` + 下一代。骨架的 Done 段写子 fleet 名、up / down 时间、答过的请求（最新 30 条 id + 状态），「Needs a human decision」写 down 时仍未答的请求 id；模型写了交接单时按 §4 以模型三段为准。
  - **下一代启动提示**：「你是 <label>，主 fleet 的 worker；上一代监管的子 fleet <名字> 已 down、那个角色结束；读交接单 <路径> 和 AGENTS.md，本回合只读、回 READY」，不再用「This run was resumed after an interruption」。
  - **迟到 Stop**：换代后旧会话的 Stop 按代数判 stale，不结算（沿用在役换代逻辑）。结局 / 回执 / 不通知父 supervisor 不变。
  - **委派期间同一会话双处显示不变**（父 tab 显示 worker、子 fleet 显示 supervisor，§3.1）。只有打字回退路径（`claude_hook_delivery: false`）仍是同会话回来、打字告知重为 worker。
- **每条后清空（§28）在子 fleet 里**：条目 `clear_after_result: true` 让子 fleet 的 Claude worker 照 §28 每条后 `/clear`；子 fleet 同样受 §28 与钩子投递的互斥：开 clear 而 `claude_hook_delivery` 不为 false → 该子 fleet **up 时** `rejected(program)`（autostart 记 `subfleet.autostart_wait`），原因写明「add claude_hook_delivery: false to its subfleets entry」；另需 `mode: service` 与 transport v2。service 具名子 fleet 的 `lifecycle` 是 None（父管它），§28 校验把「`named` + `parent_config` + lifecycle None」视同 service 生命周期。
- **子 fleet 的保底领域（用户 2026-10-14 定）**：条目 `worker_domains` 是子 fleet 自己的 `--worker-domains` 保底（§27 顾问只往上加），**写了就替换父值**（`[]` = 没有保底，如抽记忆的 memx 子 fleet 不装期货 skill），**没写沿用父值**（与 `claude` / `worker_model` 等「不取父值」的键不同：保底是父 fleet 给整棵树定的领域，缺省继承才不让 tgog 这类子 fleet 丢领域）。非空时 up 前由 `check()` 照 `parse_worker_domains` 口径核：父配置要有 `--worker-kit` 且是领域 kit、每个名字在其领域总表里，不符 → `rejected(program)`。
- **子 fleet 的顾问（用户 2026-10-14 定）**：子名单允许 `advisor: on|off`，缺省 off；父的开关从不继承（`child_config` 去掉 `advisor`），子 daemon 起时读自己名单的开关（§27），坏值随父名单整份拒收。开着时子 fleet 的 Claude worker 照 §27 举着等配置单；`worker_kit` 随父配置传入。
- **夜间名单**：`start --night-roster` / `night on|off` 的 `check_rosters` 要求夜间名单的 `subfleets:` 段与日间名单一致（按解析后的条目比较，相对路径各按自己文件目录解析），不一致整份拒收并列出不一致的名字——具名子 fleet 按 run 定，夜间切换不加、不删、不改。

**验证**：`tests/test_subfleet.py` 具名部分（名单校验、设计稿 §5 第 1–10 条、钩子通道收回换代、clear 三键 3 条、顾问 4 条）、`tests/test_subfleet_named_tmux.py` / `tests/test_subfleet_tmux.py`（双连接）、`tests/test_night_switch.py`（夜间 subfleets 一致）、`tests/test_pi_decision_schema.py`（pi service schema 合同，无 node 跳过）；全套结果见外部项目 `tasks/plans/named-subfleet-rebase-1027f4f8-20261014.md`。**未做**：父工作区标签标题后缀「（兼子 fleet <名字> 监工）」、真机 cmux / Claude 验证、独立复核、合进在役分支、部署。

## 30. 从 worker kit 起 fleet：`start --kit-fleet NAME`（2026-10-14 用户新要求）

用户定：各 fleet 的**业务**角色提示词与名单模板进 worker kit（外部项目 `scripts/context/build_worker_kit.py` 组装，§27），fleet 发版 = fleet 代码 + kit 两样。本节只加一个名单来源；§16 名单语义 / 热载、§17 续跑、§27 顾问开关、§29 具名子 fleet 全部不变。

- **协议提示词不进 kit**：`cmux_fleet/agents/*.md`（监工 / worker 协议、契约、同伴通知、顾问等）是 fleet 协议的一部分，留在 fleet 代码里随代码发版；kit 只带业务角色（`role` / `supervisor_role` 指的文件）和名单。
- **kit 布局**：`<worker_kit>/fleets/<NAME>/roster.yaml` 必有；同目录可有子名单（如 `memx_roster.yaml`）、`roles/*.md` 等，名单里的路径照 §16 / §29 相对名单所在目录。`<NAME>` 匹配 `^[a-z][a-z0-9-]*$`。kit 的 MANIFEST 含这些文件。`kit.py` 不读也不校验 `fleets/`（多出的目录照常接受）。
- **为什么复制**：kit 目录按哈希定版、只读；名单在运行中会被热改（§16 加减 worker、`fleet advisor on|off` 经 `roster.write_advisor` 写回），所以 kit 里的只是模板。
- **新起**：`start --kit-fleet NAME` 必须同给 `--worker-kit`，且 kit 里有 `fleets/NAME/roster.yaml`；与 `--roster` / `--workers` / `--worker-names` 互斥（同 `--roster` 口径）；名字非法、缺 kit、缺文件、同给 → provisioning 前 SystemExit 写明缺什么。校验直接对 kit 里的名单走 `--roster` 原路径（含 `--night-roster`、pi 模型、模型卡计票）；建出 run 目录后把 `<kit>/fleets/NAME/` 整棵树复制到 `<run>/fleet-config/`（补上属主写权限），按副本重解析名单（角色 / 子名单的绝对路径落在副本里，kit 删了也不断），之后完全等同 `--roster <run>/fleet-config/roster.yaml`（subfleets、advisor 开关、热载）。
- **config.json**：`kit_fleet: {"name": NAME, "kit": <kit 绝对路径>, "roster_md5": <kit 里 roster.yaml 的 md5>}`，只作来源记录，daemon 不读；`roster` 指副本。
- **续跑**：`start --resume` 沿用 `<run>/fleet-config/` 现有副本，**不重新复制**（热改不能丢），kit 目录不在了也不影响；续跑时带的 `--kit-fleet` 只做旗标互斥 / 名字检查，NAME 不与 config 比对、被忽略。
- **已知边界**：kit 升级不会进在跑或续跑的 run（要新版就新起 run）；副本与 kit 的差异只能拿 `roster_md5` 对 kit 版本的名单，子名单 / 角色文件不记哈希。

**验证**：`tests/test_kit_fleet.py`（只读 kit 新起复制 + 副本可写 + 与 `--roster <副本>` 的 config 一致；子名单相对路径在副本里解析；缺 kit / 缺 `fleets/NAME` / 名字非法 / 与 `--roster`、`--workers`、`--worker-names` 同给 → provisioning 前拒；改副本、删 kit 后 `--resume` 不重复制、热改保留；`Kit` 接受 `fleets/`）。**未做**：真机 cmux 起一次、独立复核、合进在役分支、部署。

## 31. 附带标签：名单 `tabs:`（2026-10-15 用户新要求，最小版）

用户定：Studio 上 EverMemOS 抽记忆的 HTTP 外壳（`evermem_fleet_shell.py`，:8895，转给 memx 子 fleet）原来手工跑在单独的 cmux 工作区；改成由 kit 名单声明、作为 memx 子 fleet 工作区里 control 后面的一个标签开出来，不在代码里写死。只加名单一个顶层键；不写 `tabs:` 本节不生效。

- **声明**（`roster.py`）：顶层 `tabs:` 列表，每项 `{name, command, cwd?, env?}`。`name` 匹配 `^[a-z][a-z0-9-]*$`、名单内唯一；`command` 非空字符串；`cwd` 相对名单目录（`~` 展开），缺省名单目录，须是已存在的目录；`env` 变量名 → 字符串（`8895` 要加引号）；其它键或任一项不合法 → 整份拒收（§16 `roster.rejected`）。谁加载这份名单（主 fleet 或具名子 fleet），标签就开在谁的工作区。
- **何时开**（`tabs.open_all`，`Fleet.tick` 的 `tabs` 段）：每次起该 fleet 的 daemon（`start`、`start --resume`、具名子 fleet 被父 fleet revive 的 `daemon --resume`）后，名单第一次被接受的那一拍开一次；之后不再管：**daemon 活着时标签被关 / 程序退出不会自动重开**，等下次起 fleet；热改 `tabs:` 也要等下次起 fleet 才生效。
- **开之前先清同一程序**：先关掉本登记里上一个 daemon 留下、cmux 还显示的标签；再对每个标签用 `ps -axww -o pid=,command=` 找命令行与它精确相等的进程，全部 SIGTERM，最多等 5 s，还在就 SIGKILL（再等 2 s），确认都没了才开；`ps` 失败或杀不掉 → 不开，记 `tab.start_failed`。**匹配规则**：标签 argv = `shlex.split(command)`；进程的 `ps` 命令行必须等于 argv 用空格连起来，唯一放宽是第一个词还可以是 argv[0] 经 `which` 后的路径或其 `realpath`，或（macOS 框架 Python）同一个 `Python.framework/Versions/X.Y` 下的 `Resources/Python.app/Contents/MacOS/Python`（框架 Python 会 re-exec 成它，`ps` 里 argv[0] 被改写）；其余每个词逐字相同。参数多一个、少一个、换解释器都不算。所以 `command` 要写成一个直接 exec 的程序（不用管道 / `&&`）；`env` 不参与匹配。
- **位置与归属**：`spawn_window` 跑 `cd <cwd> && exec [env K=V …] <command>`（程序就是 surface 里的进程，命令行与名单一致），开在本 fleet 工作区，再 `move-surface --after` 挪进 history / control 所在 pane：排在上一个标签后，否则 control 后，否则 history 后；`--focus false`，标题 = `name`。surface 登记为本 fleet 所有（`type: tab`、`tab: <name>`），所以 lifecycle close / 子 fleet 收回的 `close_all` 一并关；显式 `stop` 收尾也 `tabs.stop_all` 关掉、计入「全部停稳」。不是 agent：不进 worker 名单、不投递、不进提示词、不算配额。
- **审计**：`tab.cleared`（杀了哪些 pid）/ `tab.started` / `tab.start_failed` / `tab.dock_failed` / `tab.stopped`。
- **安全**：标签程序以 fleet 用户身份运行，**名单即授权**——能改名单（kit 或 run 副本）的人就能让 fleet 跑任意命令、杀掉同命令行的进程。

**验证**：`tests/test_roster_tabs.py`（名单解析与坏值拒收；匹配规则含框架 Python 存根；真进程：SIGTERM 清掉、忽略 SIGTERM 的被 SIGKILL、同程序不同参数不碰；只开一次、按序挂在 control 后、先关旧标签；清不掉不开；stop 关标签；`dock_tab` / `spawn_window` 登记）。**未做**：全套测试、真机 cmux、复核、合进在役分支、部署。

## 32. service 派工看状态 + 空闲即换代（2026-10-16 用户新要求）

用户 10-16 在本机全 FlashNext service fleet 上发现：监工派工不会自动挑空闲的 worker、复核找不到该找的 worker；某 worker 超过换代阈值却一直没换代。三处改动，协议、标识、换代流程（§4）不变。

- **路由消息带工人状态**（`Fleet.worker_board`，接在 `route()` 原 `Workers:` 行后，原行保留）：每个可派 worker 一行 `- <label>: <state>[, N queued][, has request <id>…][, role: <角色文件首个非空行，≤120 字>]`。在途请求按 `fx:req:*` 的 `status=dispatched` + `worker` 反查（v1 / v2 通用）。状态是入队时的快照；监工提示词补一句「前一回合派过、尚未回报的 worker 算忙」。
- **service 监工提示词**（`service_supervisor.md` / `_v2` / `_tui_v2` 同加两条禁令）：有空闲 worker 时不许把请求排到忙的 worker 后面，只有请求明确接续该 worker 自己未完成的请求才可以，并在 `reason` 写明；复核 / 验收请求有带 reviewer 角色的 worker 时不许派给别人，永不派给被复核工作的作者。角色由名单 `role` 文件给（§16），本节不加新名单字段。
- **空闲即换代**（`Fleet.idle_rotate`，`pump()` 里空闲且信箱空的监工 / worker）：每个（代数, delivered）只读一次用量，过阈值即 `start_rotation`；与 `deliver()` 同样的守卫（profile 换卡中、v2 非 TUI 槽未 `bridge_available`、子 fleet 收编槽、Codex 原生子 agent 在跑）；刚起的会话（delivered=0）不在这里判，`rotate.impossible` 仍归投递时。受 `max_busy` 与暂停约束（handoff 计入忙）。顾问槽不走这条（§27 自管）。
- **已知边界**：长回合中途换代已由 §34 覆盖（daemon 探针 + 强制 prompt）；请求超时被 `held` 的槽迟到结算后仍是 `held`（`settle.late` 只记审计），不会空闲即换代，要人工 / 看门处理——10-16 的 85k 事故正是这一情形，本节不覆盖。

**验证**：`tests/test_fleet.py` 新增空闲过阈值即换代（delivered 1 换、0 不换）、同（代数, delivered）只读一次、路由消息含状态 / 队列 / 在途请求 / 角色；`test_rotated_worker_gets_next_name_and_old_name_still_routes` 按新时序把 `handoff_timeout=0` 前移（换代提前到空闲那一拍）。

## 33. 终端后端：cmux 与 WezTerm 二选一（2026-10-27 用户新要求）

用户 10-27：「我写成两个选择，一个 cmux，另一个是仿照 cmux 写一个替代后端」。目标：macOS 照旧用 cmux；Linux（含 Windows 的 WSL2）用 WezTerm 跑同样的 supervisor + workers。Windows 原生、整体 Rust 重写、Zellij / tmux 无界面后端都**不做**（设计稿 §8）。本节不改任何标识 / 交接 / 版面基线（§3、§4、§2），cmux 后端行为零变化。

### 33.1 接口（只有一处碰终端）

fleet 只通过 `fleet.manager_for(cfg)` 造出的那一个对象碰终端（原 `fleet.py:210`）。该对象的公开方法就是后端接口，写在 `cmux_fleet/backends/base.py`：

- `METHODS`：39 个方法的清单（会话 `available` / `session_exists` / `get_or_create_session` / `claim_run` / `workspace_id` / `select_workspace` / `keep_windows_open` / `owns` / `close_all`；开窗 `spawn_window` / `spawn_interactive_cli`；读写 `send_prompt_to_idle` / `send_key` / `capture_output` / `capture_to`；状态 `get_pane_status` / `has_cli_process` / `process_states` / `process_cpu`；标签焦点 `rename` / `focus` / `kill_pane`；布局 `arrange` / `dock_tab` / `dock_control` / `place_right_of` / `place_after` / `tab_into` / `tab_panes`；浏览器格 `browser_goto` / `browser_url` / `reload_browser`；移交 `release` / `adopt`；tmux 视图 `tmux_view_info` / `attach_view` / `tmux_session_alive` / `reattach_client` / `detach_view`），签名与 `CmuxSessionManager` 完全一致。
- `TerminalBackend`（`typing.Protocol`）是同一清单的类型视图；`Capabilities(browser, nested_tabs)` 与类属性 `supports_browser` / `nested_tabs` 是 fleet 唯一看的能力位。
- 构造器统一 `(session_name, agents_title, control_title, state_dir=None)`；`backends.build(name, session, **extra)` 惰性 import 对应模块。
- 两个后端各自的可用性：`available()`（类方法/静态方法，cmux 就是 `cmux_available()`）、`unavailable_reason(name)`（cmux 由 `cmux_available()` 决定，WezTerm 报缺哪个程序/版本）。
- `tests/test_backend_interface.py` 逐方法比对两个实现的签名与构造参数，并要求 `METHODS` 不漏、不多；**加后端 = 实现清单 + 过这条测试**，改接口 = 同时改两个实现和 fleet。

### 33.2 两个后端

- **cmux**（`cmux_fleet/backends/cmux.py`）：原 `cmux_manager.py` 原样搬家（行为零变化），`cmux_fleet/cmux_manager.py` 只剩转发（`sys.modules[__name__] = backends.cmux`，两个模块名是同一个模块对象），所以既有 import 与测试对 `cmux_manager._run` / `cmux_available` 的 monkeypatch 全部照旧生效。`supports_browser = True`、`nested_tabs = True`。`/Applications/cmux.app` 这类 macOS 专有路径只出现在这个文件里。
- **WezTerm**（`cmux_fleet/backends/wezterm.py`）：每个 run 一个私有 `wezterm-mux-server`（短路径 unix socket，写在 run 的 `.runtime/wezterm/<token>/` 最小 `wezterm.lua` 里；所有 CLI 调用都带 `WEZTERM_UNIX_SOCKET` 与 `--no-auto-start`，**绝不**连用户自己的 WezTerm）；`supports_browser = False`、`nested_tabs = False`。方法映射、可用性判定（`wezterm` 与 `wezterm-mux-server` 在 PATH 且版本 ≥ `deps.yaml` 的 20240203）见设计稿 §4。

### 33.3 选择与继承

- `start --backend cmux|wezterm`；不给时：`sys.platform == 'darwin'` 且**装了** cmux（`CMUX_BIN` / cmux.app 路径或 PATH 上有 `cmux`，`backends.base.cmux_installed`）→ cmux，cmux 不应答（ssh / Terminal.app）就照旧拒开、报「run this inside a cmux terminal」，**绝不悄悄改用 wezterm**；只有非 macOS 或 Mac 上没装 cmux 才默认 wezterm（2026-10-28 核心复核收紧）。显式选的后端在这台机器上不可用 → 启前拒开并说明缺什么（`fleet.resolve_backend`，`start` 里在 provisioning 之前）。
- 选中值写进 run 的 `config.json` 的 `backend`；`--resume` 对没有 `backend` 键的老 run 直接按 cmux 判（`resolve_backend(cfg.get('backend') or 'cmux')`），不重新走缺省规则；`manager_for` 只读它，daemon / actor / `--resume` / 子 fleet（子 config 由父 config 复制）一律跟随，不再各自判断。**没有 `backend` 键的旧 config（本次之前起的 run）按 cmux 处理**，等价于原行为。
- 自动选择在 macOS 上不会把 run 推到 WezTerm：装了 cmux 一律 cmux；没装 cmux 而 WezTerm 后端也不可用（未装或未合入）时答案仍退回 cmux，报的仍是原来那条「run this inside a cmux terminal」类信息。`scripts/check_deps.py::pick_backend` 同一规则（darwin 上 `cmux --version` 跑得通即 cmux）。
- `fleet.yaml` 不加块：后端是一次运行的选择，不是表。

### 33.4 版面降级（WezTerm）

- cmux 版面（§2）：监工左上、右侧 worker 标签、左下 history/control 标签，依赖「分屏里再分标签」。WezTerm 没有这层：**一个窗口；第 1 个标签 = 监工（左）+ control（右下分屏）；每个 worker 一个标签**；`tab_into` / `dock_*` / `place_*` 降级为「放进对应标签 / 无操作并返回 True」，不报错；`arrange()` 在 `supports_browser = False` 时排好 worker、把 control 放进第 1 个标签（右下分屏，由 WezTerm 后端自己实现）后返回 `None`。
- 版面与历史是 fleet 仅有的两处读能力位的地方（`Fleet.announce_history_page()` / `subfleet.follow_jump()` 看 `supports_browser`）；其余调用路径一律走原接口，cmux 后端除 `available()` / `supports_browser` / `nested_tabs` 外与搬家前逐行一致。
- 历史页：`supports_browser = False` 时不建浏览器格；`Fleet.announce_history_page()` 在 control 里打印 `<run>/history.html` 路径，并在有图形环境时用一次系统打开（macOS `open` / Linux `xdg-open`，没有就只打印）；`refresh_history()` 只在有浏览器格时 `reload_browser`；`subfleet.follow_jump()` 在没有页面时直接返回（也不再每拍调 cmux）。cmux 这些分支全部保持原调用不变。
- **已知限制：WezTerm 不支持子 fleet**（2026-10-28 复核）。子 fleet 靠把父 fleet 的活 pane 交给子 fleet 的登记表（`release` / `adopt`、tmux 视图）；WezTerm 每个 run 一个私有 mux server，子 fleet 看不到也接不走父 fleet 的 pane。所以 `backend: wezterm` 下所有子 fleet 入口一律拒开、给同一句原因（`subfleet.backend_problem`）：`subfleet.check`（拆分子任务 / `subfleet spawn` / 具名子 fleet up / 升级审批）、子 daemon 的 `start_adopted`（直接 blocked）、`start` / `--resume` 带 `subfleets:` 的 roster、roster 热加载出现 `subfleets:`（整份拒收、旧 roster 照旧）。cmux 路径不变。要用子 fleet 就 `--backend cmux`；跨 server 交接的方案另开一条线定。

### 33.5 平台小改

`cmux_fleet/platform.py::keep_awake_prefix()`：macOS `caffeinate -i`、Linux 有 `systemd-inhibit` 且先拿 `true` 试跑成功（5 秒超时）时 `systemd-inhibit --what=idle:sleep --why=cmux-fleet`、否则空（从 ssh 会话或无头服务器起，polkit 拒绝抑制器「Failed to inhibit: Access denied」，包上它 daemon 一启动就死）——`fleet.py` 两处、`subfleet.py` 一处原 `caffeinate` 调用改调它。`deps.yaml` 按后端门控（`when: cmux` / `when: wezterm`，后者含 `wezterm-mux-server`，最低 20240203），`scripts/check_deps.py` 用同一条 §33.3 规则挑后端并跳过不属于它的项。

**验证**：`tests/test_backend_interface.py`（接口齐全）、`tests/test_backend_selection.py`（选择 / 继承 / 降级）、`tests/test_platform.py`、`tests/test_check_deps.py`、WezTerm 假 CLI 与真 mux server 测试（impl-wezterm 分支）、`.github/workflows/tests.yml` 的 `ubuntu-latest` 全量任务。真机冒烟（`start --backend wezterm` 起 pi 监工 + worker）留给编排会话，不在本 fleet 内做。

## 34. 回合中途换代（daemon 探针 + 强制 auto-handoff prompt）（2026-10-27 用户新要求，分支 feat/midturn-handoff）

用户原话：「修吧，开分支做回合中途换代，这个问题纠缠了N次了，不论哪种模式都要能auto handoff啊，这是最基本的功能啊，怎么基本功能都丢失啊」「你不用搞那么复杂，就让daemon自动做一个探针就行吧，所有的会话的窗口都由daemon去控制。针对不同worker，像claude code、codex、pi之类的都强制它执行auto handoff的prompt」。据此推翻 `pi_extension_v2.ts` 首行原则「agent_settled only reports; it never requests continuation」与 §32 末尾「已知边界：长回合中途仍不换代」——这两条此前都假定换代只能发生在回合之间，本节起回合进行中 daemon 也强制换代。

- **探针**：daemon 内一条独立线程，循环间隔 = max(1 秒, 本轮查完所有忙槽的耗时)，单线程顺序执行、不重叠不堆积；只看忙槽（busy、租约已 received、在途消息不是 handoff 投递本身），空闲槽仍归 §32 `idle_rotate`。每轮先 `stat` 该槽本代会话文件（mtime, size），未变跳过；变了只读尾部（≤64KB），不读全文件。
- **用量口径**：pi 槽按 pi 自己的算法取数——`calculateContextTokens` 加尾随未计入模型调用的消息按 `ceil(chars/4)`（按 UTF-16 长度计）估算，探针照抄这套口径、不另造一套；compaction 发生后、下一次模型响应产生前，用量视为未知（不当 0，沿用上一次读到的值）。Claude 槽用现有 `claude_transcript_usage`，Codex 槽用现有 `handoff.read_usage`。
- **中途线**：`midturn_ratio` 缺省 0.8（用户定）。用量 ≥ 0.8 × window、本代还没打过 → daemon 绕开投递闸，直接把固定单行 prompt 粘贴进该槽窗口并回车；prompt 带 `fx-<16hex>` 编号（即 `midturn_id`），每代只打一次。
- **回合结算**：该回合结束时消息带中途标记、没有合格正式回报 → 记 `handoff_split`：不算完成、不判 BLOCKED、不计 attempt；续做消息排队首（FIFO 头），照原 §4 流程换代（草稿被模型改过走模型版，否则走骨架），接班代续做同一条派工。
- **兜底**：打 prompt 后 300 秒回合未结束 → 再打一次；再 300 秒仍未结束 → 交现有 `Watchdog.rotate` 杀窗，走机器骨架交接 + 原位起新一代 + 续做。
- **上限**：同一条派工最多拆 3 次（`midturn_splits` 计数在消息上）；过线不再拆，转 BLOCKED「请拆小」交监工 / service 处理。
- **时钟**：service 的请求超时时钟在中途换代期间暂停，拆分链结束（结算、转 BLOCKED、取消）后把暂停时长累进恢复；task 整体 `hours` 预算不受影响，硬预算不暂停（§17 / §23）。
- **三处放行**：这套机制要绕开三处原本会拦掉中途输入的闸——pi 扩展 `input` 钩子对带 `fx-` 前缀的中途 prompt 放行（原本会吞掉或转成 operator_input）；Claude 监工的 `UserPromptSubmit` 钩子对登记过的中途 prompt 放行；Codex `codex_queue.bound` 放宽成「第二条用户消息等于登记的中途 prompt 也算本代」。

**已知边界**：探针能看到已追加、尚未发给模型的工具结果（pi 口径按 ceil(chars/4) 估算），但 CLI 在追加工具结果后几毫秒内就检查是否自压缩，快于 1 秒轮询——单批大工具结果可能一步把用量从中途线以下直接顶过 CLI 自压缩点，探针来不及打 prompt。200k 窗口（pi DeepSeek）下 0.8 的中途线到自压缩点只剩 23,616 token 余量，不保证能在压缩前交接完；用户 2026-10-27 知悉并定不调低 0.8（是否对 200k 及以下窗口单独调低留给用户另定）。pi 的 WorkBuddy DeepSeek 已改声明 400k 窗口，实测经网关可到 1M、单请求 612,564 token 通过；OpenCode 免费 DeepSeek 实测 185k 通过、260k 被拒，真实上限约 200k（其中 32k 留给输出），pi 扩展声明的 168k（= 200k − 32k）是对的，不改。

**验证**：设计稿 `docs/midturn-handoff-design.md`（r3）§7 列了完整单测与端到端冒烟计划（新文件 `tests/test_midturn_v2.py` + `scripts/smoke_midturn.sh`），尚未实现、未跑、未复核、未合进在役分支、未部署。

## 35. 断链即重启会话（2026-10-28 用户新要求，分支 feat/link-loss-restart）

用户原话：「通信中断，执行状态未确认；等待对账，这是一种断联情况，应该是发现断链就根据实际情况重启这个会话。」

- **先分清是不是断链**：bridge 的 fault 分两种。一种只是复述 daemon 已经做的扣槽（`daemon holds execution` / `settlement unconfirmed/held` / `recovered PEL may have executed` / 人工输入撞车 / opencode hold，`runtime_v2.BRIDGE_HELD_REASONS`），pane 显示「已被 fleet 扣下：<daemon 写在消息上的原因>」，续做上限显示「已达续做上限，等监工拆小」；daemon 收到这种 fault 时槽已是 held 就只记审计，不再二次扣槽、不暂停全 fleet。其余一律是断链（bridge 自己的 Redis 错、子进程退出、租约续不上），pane 显示「通信中断；fleet 将自动重启本会话」。10-27 飞书 fleet 两次「通信中断」实为 reviewer 坏回报整 task BLOCKED 的扣槽（缺口 G1），不是断链。
- **daemon 怎么发现断链**：① 忙槽：租约 `lease_until` 过了而派工 `deadline_at` 没过（bridge 不续租了）→ 断链；两者都过才是原来的超时。② 闲槽：bridge 续租线程每轮写心跳键 `<prefix>:hb:<slot>`（值＝代数，TTL 3 个周期）；本代心跳出现过、之后消失 → 断链。从没写过心跳的 bridge（旧代码）不判。③ bridge 自己报的断链类 fault。三者都走 `link_lost`：只扣本槽（`pause=False`），标 `link_lost=1`。只对 bridge 槽（pi / opencode），Claude / Codex 的租约由 daemon 代续，不在此列；顾问槽仍由 §27 原地重启。
- **重启**：看门狗每轮先处理 `link_lost=1` 的槽，复用冻结换代 `Watchdog.rotate`：存屏、骨架交接单 + 终端快照、起下一代、杀旧会话、`confirm_retired` 取消原派工。原派工**不自动重跑**（v2 原则不变）：task 模式监工收到「FROZEN … bridge link lost，已取消未重跑，先查工作区再派」，service 模式请求记 stalled；带中途换代标记的派工照 §34 续做。暂停中不重启，恢复后再做。
- **上限**：同一槽 30 分钟内最多重启 3 次（`watchdog.LINK_MAX_RESTARTS / LINK_WINDOW_S`），再断就扣槽并暂停派工，报「检查 Redis / bridge 后再恢复」；重启失败标 `link_lost=failed`，不每轮重试。
