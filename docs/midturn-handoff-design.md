# 回合中途换代（mid-turn handoff）设计稿 r3：daemon 探针 + 强制打 prompt

> 2026-10-27，分支 `feat/midturn-handoff`（基于在役 `deploy/cmux-fleet@41632170`）。只出设计，未改代码。
> 落地后并入 `docs/design.md` 为 **§33**。沿用 §4 换代流程、§3 代数命名 slot.N、交接单三节 + 机器骨架、历史页；不另造第二套换代。
> r1 / r2（A/B 两层、CLI 钩子、用量帧、P1–P8 探针脚本）已被用户叫停，压在 §9「已否决」。

## 用户原话（2026-10-27，本稿的依据）

1. 「修吧，开分支做回合中途换代，这个问题纠缠了N次了，不论哪种模式都要能auto handoff啊，这是最基本的功能啊，怎么基本功能都丢失啊」
2. 「你不用搞那么复杂，就让daemon自动做一个探针就行吧，所有的会话的窗口都由daemon去控制。针对不同worker，像claude code、codex、pi之类的都强制它执行auto handoff的prompt」
3. 中途阈值 80%（`midturn_ratio` 缺省 0.8）。
4. 「这个探针max（1秒，查完所有窗口时间）去执行循环这样可以吗？」（已答可以）

原话 1、2 是推翻 `pi_extension_v2.ts` 首行「agent_settled only reports; it never requests continuation」与 design.md §32「长回合中途仍不换代」的依据，§33 原样引用。

---

## 0. 结论

1. **根因**：换代只在两处判——`Fleet.deliver()`（`fleet.py:3958-3976`）与 §32 `Fleet.idle_rotate()`（`fleet.py:3789-3809`），都要求槽位 `idle`；回合进行中没人看用量。pi 用量还只在 startup / settled 落 Redis（`Fleet.pi_metadata`，`fleet.py:3151`），所以 analyst-1 一回合涨到 186,673（pi 会话文件实测）时 status 一直是 4,323。
2. **探针 = daemon 自己**：独立线程循环，间隔 = max(1 秒, 本轮查完所有忙槽的耗时)；每轮只看忙槽；先 `stat`（mtime, size），没变跳过，变了只读尾部 ≤ 64KB；pi 读 `pi-sessions/<slot>/g<N>/*.jsonl`、Claude 读 transcript、Codex 读 rollout。不加用量帧、不加 CLI 钩子、不写探针脚本。
3. **强制打 prompt**：用量 ≥ 0.8 × window → daemon 绕过投递闸，直接往该槽窗口粘贴一行固定的 auto-handoff prompt 并回车。每代只打一次。
4. **回合结算**：该回合结束时，消息带中途标记、没有合格正式回报 → 记 `handoff_split`：不算完成、不判 BLOCKED、不计 attempt、不触发监工验收；续做消息排 FIFO 头；照原 §4 `finish_rotation` 换代；接班代 READY 后续做同一条派工。
5. **兜底只一条**：打 prompt 后 300 秒回合没结束 → 再打一次；再 300 秒 → 走看门现有 `Watchdog.rotate` 杀窗 + 机器骨架交接 + 续做。
6. **上限与时钟**：同一条派工最多拆 3 次，再过线转 BLOCKED 给监工「请拆小」；service 请求时钟在中途换代期间暂停。
7. **三个关键事实**（§4）：三种 CLI 运行中收到粘贴 + 回车都是**当回合内读到**（工具批之后），不需要 Escape；但要改三处小闸——pi 扩展的 `input` 钩子会把打进去的字吞掉（worker）或转成 operator_input（监工），Codex 的回合绑定要求「恰好一条用户消息」，Claude 监工的 UserPromptSubmit 钩子会拦。
8. **阈值风险要报用户**（§5）：0.8 在 1M（Claude）与 400k（Astra）下余量充足；**在 200k（pi DeepSeek）下只剩 23,616 token，一次两个大读文件就会让 pi 先自压缩、交接落空**。按用户定的 0.8 写，不偷偷调低，交用户决定。

---

## 1. 现状复核（证据）

| 声称 | 复核 | 证据 |
|---|---|---|
| 换代只在 deliver 与 idle_rotate 判 | 属实 | `fleet.py:3958-3976`；`fleet.py:3789-3809`（要求 `state == 'idle'` 且信箱空） |
| v2 交接是单独一条 handoff 投递，要求租约 idle | 属实 | `runtime_v2.offer_handoff`（`runtime_v2.py:2035`）；`settle` 的 `purpose == 'handoff'` 分支（`runtime_v2.py:972-976`） |
| §32 写明不覆盖长回合 | 属实 | design.md §32「已知边界」 |
| 今晚 analyst-1 一回合 186,673 并被 pi 压缩 | 属实 | `~/.cmux-fleet-feishu/runs/fleet-20261027-213921-248bd82f/pi-sessions/analyst-1/g0/*.jsonl`：assistant usage 峰值 186,673，`type=compaction` 2 行；run config `handoff_ratio 0.6` |
| analyst-2 / reviewer 超阈值未换代 | 属实 | analyst-2 g0 峰值 140,911；reviewer g0 峰值 135,283 后才有 g1 |
| reviewer 因没交 `fleet_result` 卡死整个 task | 机制属实 | pi task worker 无结构化候选 → `settle` 判 `exactly one structured candidate is required` → pi worker 直接 `finish('blocked')`（`runtime_v2.py:1059-1081`，pi 不走 TUI 两次纠错） |

---

## 2. 方案

### 2.1 探针（daemon 内独立线程）

```text
loop:
  t0 = now
  for slot in 忙槽（state busy，且租约 received、消息不是 handoff 投递）:
      path = 该槽本代会话文件
      st = stat(path)；(mtime, size) 与上次相同 → 跳过
      读尾部 ≤ 64KB，取最后一条主线程 assistant usage → used
      results[slot] = (generation, message_id, used, window, path, st)
  sleep(max(0, 1 − (now − t0)))      # 间隔 = max(1 秒, 本轮耗时)，单线程顺序执行，不重叠不堆积
```

- **只读文件、只写一个内存结果表**（加锁）；Redis 事务、打字、换代都在主循环里做（`RuntimeV2.run` 的 `step → pump → tick`，新增一步 `midturn_apply()` 读结果表）。读文件慢只会让探针本轮变长，不会卡投递。
- 空闲槽不看：归 §32 `idle_rotate`。
- 结果带 `generation` 与 `message_id`，主循环应用前核对仍是同一代、同一在途消息，过期结果丢弃。
- 顺带把 `used` 写进 `fx:agent:<slot>` 的 `ctx_used`，status 回合内实时（关掉 4,323 显示问题）。

| CLI | 文件 | 用量取法 | 证据 |
|---|---|---|---|
| pi | `<run>/pi-sessions/<slot>/g<N>/*.jsonl`（取本代目录下最新的一个） | 最后一条 `message.role == assistant` 的 `usage`：`input + cacheRead + cacheWrite + output`（cacheWrite 今晚为 0 时与编排者口径 `input+cacheRead+output` 相同）；window 用 Redis `ctx_window`（pi startup 自报） | 今晚 run 四个槽的会话文件实测：analyst-1 186,673、analyst-2 140,911、reviewer 135,283 |
| Claude Code | 槽字段 `transcript`（bind 时登记） | 现有 `claude_transcript_usage`（`fleet.py:362-378`）：最后一条非 sidechain assistant 行 `input + cache_read + cache_creation + output`；window 按 §22 | 本机实测：在跑会话的转录每次模型响应都写 usage 行，最后一行时间戳与当时差 < 1 秒 |
| Codex | 槽字段 `thread` → rollout | 现有 `handoff.read_usage`（`handoff.py:59-87`）：最后一条 `token_count.info.last_token_usage.total_tokens`，window 取 rollout 自报 `model_context_window` | 本机 rollout 实测：一个回合内每次工具调用后一条 token_count，20,650 → 108,394 逐条上涨 |
| opencode | 该代数据目录（`CMUX_FLEET_OC_SESSION_DIR`）里的会话存储 | **未核实**（路径与格式），端到端前查清；查不到 → 每代一次 `midturn.no_usage`（error 审计 + control 卡），明着报，不静默 | 插件里已有 `ctxUsed`（`opencode_plugin/index.js:333-337`），备用：随下一帧 metadata 落 Redis |

现有 `claude_transcript_usage` 读 512KB 尾、`read_usage` 读 128KB 尾；探针统一改 64KB 尾（usage 行都在最后一次响应附近；读不到 usage 行就沿用上次值，不当 0）。

### 2.2 触发与强制打字

条件（主循环 `midturn_apply` 判）：`used ≥ midturn_ratio × window`，且本代还没打过（槽字段 `midturn_typed_g == generation`），且守卫全过（§6）。

动作：
1. **[事务]** 在途消息 hash 写 `midturn_id`、`midturn_at`、`midturn_used`；槽字段写 `midturn_id`、`midturn_draft`（预建草稿，`handoff.partial_template()`，与 `start_rotation` 同法）、`midturn_mtime`、`midturn_typed_g`、`midturn_deadline = now + 300`、`midturn_tries = 1`；service 请求时钟暂停（§2.5）；在途消息 `deadline_at` 推后 `2 × 300 + profile_ready_timeout` 秒，免得交接回合里被 tick 判过期 held；审计 `midturn.ordered`；control 卡「worker-1 上下文 160k/200k，已发中途交接指令」。
2. **[事务外]** `self.mgr.send_prompt_to_idle(pane, prompt, bracketed=<Claude 为 True，其余 False>)`。这个函数名叫 idle，实际不查空闲，只粘贴 + 回车（`cmux_manager.py:667-669`）。失败 → 审计 `midturn.type_failed`，下一轮再试一次，仍失败按兜底的计时走。

prompt 固定**单行**（避免多行粘贴在非 bracketed 模式被拆成两条，§11 有记录），沿用 §4 三节格式：

> `[FLEET MID-TURN HANDOFF fx-<16hex>] Context is {used}/{window}. Stop now; start no new step. Edit only the handoff note {path}: keep its three ## headings (Done (facts) / In progress / Next steps (in order)), at most 10 lines each; under In progress write exactly where you stopped inside the current instruction, under Next steps what remains of it. Do not send your final report or call fleet_result/fleet_decision. Then end your turn with the single word HANDOFF.`

`fx-<16hex>` = `midturn_id`，供 Codex 绑定与 Claude 监工钩子放行认这一条（§4）。

### 2.3 回合结算：`handoff_split`

位置：`runtime_v2.settle`，`wall_settle` 之后、`aborted / failed` 判断之前（墙优先，§23 不变）。消息带 `midturn_id` 时：

| 结算内容 | 处理 |
|---|---|
| 合格正式回报（worker Done+Evidence / 监工决策 JSON / service 业务 JSON） | 模型没理会、已把活干完：照常结算、照常 report；清槽上 `midturn_*`；回合后 §32 `idle_rotate` 正常换代 |
| 没有合格回报（含只回 HANDOFF、只写了草稿） | **`handoff_split`**，见下 |
| `aborted` / `failed` / `unfinished`（不是我们造成的） | 原 hold 路径不变 |

`handoff_split` 动作（一个事务，照 `wall_settle` 写法，`runtime_v2.py:840-869`）：
1. `release(env, {'midturn': 'split'}, outcome='handoff_split')`：租约回 idle、XACK、PEL 清。**不调** `report` / `on_supervisor(invalid)` / `remember`，**不动** `report_retries`、`bad_replies`、契约回报归属；**不走** pi worker「无候选直接 blocked」那条（今晚 reviewer 卡死的那条）。
2. 造续做消息：`request_id`、`kind`、回复规格同原消息；`continues=<原 message_id>`；`midturn_splits = 原值 + 1`；`replay` 原样继承（不加）；`deadline_at = 投出时刻 + 原消息下单时剩余时长`（§2.5）；正文 = `[Continuation] You are {label}; {prev} stopped this same instruction midway (context full). Handoff note: {note}. Continue from it: do not redo finished steps, re-check the step marked in progress. The original instruction follows unchanged:` + 原正文。用 `offer_handoff` 里那段 LREM/LPUSH Lua 放 **FIFO 头**。
3. 槽：`handoff_id = midturn_id`、`handoff_draft / handoff_mtime` 取 `midturn_*`、`state = handoff`，`rotation_pending.add(slot)` → 现有 `finish_rotation(timed_out=False)`：草稿被改过且三节校验过 → 模型版，否则骨架（§4 原规则）。
4. 审计 `midturn.split`；UI 卡「worker-1 中途交接 → worker-1.1 续做（第 1 次）」（不进模型上下文，§11）。

**为什么下单时不直接写 `handoff_id`**：`RuntimeV2.tick` 对 `handoff_id` 非空且 `state in (handoff, held)` 的槽到期会 `finish_rotation(timed_out=True)`（`runtime_v2.py:2271`），提前写会被那条路抢先换代、丢掉续做。

续做过 `deliver()` 的闸（`fleet.py:3894`）：`kind='continuation'` 保留 task id 校验，跳过 live 换卡判定与顾问会诊（同一条派工，正常换代沿用已选 runtime，§12 / §27）；service「请求已终态即丢」保护（`runtime_v2.py:1906-1910`）照常生效。

### 2.4 兜底（只一条）

| 时点 | 动作 |
|---|---|
| 打 prompt 后 300 秒（`midturn_retry`）回合未结束 | 再打一次同一行（`midturn_tries = 2`） |
| 再 300 秒仍未结束 | 调看门现有 `Watchdog.rotate(slot, 'midturn timeout', screen)`（`watchdog.py:689`）：存屏 → 骨架交接单（模型若已改草稿则合并）→ 原位新一代 → `kill_pane` → `confirm_retired`。新增 `reason='midturn'` 变体：不发 FROZEN 报告，原消息记 `handoff_split`（审计 `midturn.killed`），按 §2.3 第 2 步造续做 |

- 暂停中 / adopt 槽 / 不在本 fleet 注册表 → 照 §24 只 hold 不杀，时钟恢复计时。
- 槽带 §24 `prompt_wait`（弹框等人，含删除确认）→ 停表、不再打字、不杀，标记清掉后接着数。
- kill 只有 `Watchdog.rotate` 一条路；看门自己的 stalled → frozen 若先到、而消息带 `midturn_id`，同样走续做变体。Escape 不用、也不当停止证明（§11、§17 不动）。

### 2.5 上限与时钟（短）

- **`midturn_splits`**：存 Redis 消息 hash（续做 = 原值 + 1）；service 另写请求 hash `fx:req:<id>`。随 AOF 持久、不 TTL；换代保留（计数在消息上，不在槽上）；`start --resume` 保留：`requeue_lost` 重排时把 `midturn_splits` 与 `continues` 抄到新消息（今天只抄正文，`runtime_v2.py:1849-1852`，要改），service 读请求 hash。上限 `midturn_max_splits` 缺省 3。到上限再过线 → 不打 prompt、不拆：task worker 登记 §24 F′ 8.4 的 `blocked:<槽>` 交监工，原因「这条派工一个会话装不下（已续做 3 次），请拆小」；service worker 合成 `{"blocked": …}` 走 `service_blocked_intake`；监工槽 task `finish('blocked')`、service 暂停 + 卡片。接班代 READY 时用量已 ≥ 中途线（交接单 + 角色本身就装不下）→ 同样转 BLOCKED（`midturn.impossible`）。
- **service 请求时钟暂停**：下单时请求 hash 写 `clock_paused_at`；续做被 `offer` 给接班代时把这段累进 `clock_paused_ms`、清 `clock_paused_at`；`Fleet.expire_requests` 与 `RuntimeV2.tick` 的 expire 按 `now − created − clock_paused_ms −（暂停中则 now − clock_paused_at）` 判；拆分链以任何别的方式结束（kill 失败 hold、转 BLOCKED、请求取消）立即恢复。公布结果带 `clock_paused_ms`。task 的单条消息 `deadline_at` 同样推后（§2.2），task 整体 `hours` 预算不暂停（§17 / §23 硬预算）。

---

## 3. 覆盖面

| 槽 | 是否覆盖 |
|---|---|
| worker：pi / Claude / Codex | 是 |
| worker：opencode | 是，前提是 §2.1 的用量来源查清；查不到明着报 `midturn.no_usage` |
| 监工：pi（缺省）/ Claude / Codex | 是（续做 = 同一条消息重投给接班监工，`bad_replies` 不动） |
| task / service 模式 | 都覆盖 |
| 顾问槽（§27） | 不需要：一次一条会诊、180 秒票、`advisor_restart` 自管 |
| adopt 的子 fleet 监工（§19）、具名子 fleet 委派中的 supervisor_slot（§29） | 不做，照旧（同 `deliver` 的 `subfleet.adopted → window=0`） |

---

## 4. 关键事实 a / b / c

### a) 运行中收到粘贴 + 回车：插话还是排到回合后

| CLI | 结论 | 用不用 Escape | 证据 | 状态 |
|---|---|---|---|---|
| Claude Code | **当回合内读到**：在跑工具调用时回车的消息，工具调用一结束就在同一回合交给模型；只在纯文字输出时，排到回合结束后再发 | 不用 | 官方文档 code.claude.com interactive-mode「Queue messages while Claude works」：「if you queue a message while Claude is running tool calls, Claude Code passes it to Claude as soon as those tool calls finish, within the same turn」；how-claude-code-works「Interrupt and steer」。本机该用户真实转录里，回合中途打进去的话记为 `type: attachment` / `attachment.type: queued_command` / `commandMode: prompt` 行（2026-10-24 至 10-27 多个会话） | 文档 + 转录形态已核实；**fleet 交互 Claude 槽实测未做**（端到端覆盖） |
| Codex | **当回合内读到**：TUI 回车 = submit，回合进行中 submit 就是 steer，进当前回合的待输入队列，由回合循环在下一步取用；Tab（`composer.queue`）才是排到回合后 | 不用 | main 源码 `codex-rs/core/src/session/turn_input.rs`（steer 推进 active turn 的 pending input）、`codex-rs/tui/src/chatwidget/input_flow.rs`、`codex-rs/config/src/tui_keymap.rs`（`submit` / `queue` 两个键）、`tui/assets/tooltips.txt`；本机 `codex features list`：`steer  removed  true`（已转为常开） | 源码已核实；**0.162.1 / 0.164 实测未做** |
| pi | **当回合内读到**：回车 = submit，运行中 submit 走 steer，在当前 assistant 回合的工具调用跑完后、下次模型调用前进入；`alt+enter` 才是 follow-up（排到跑完后） | 不用 | `docs/keybindings.md:89`（`tui.input.submit = enter`）、`:166`（`app.message.followUp = alt+enter`）；`docs/how-pi-works.md:13`；`InputEvent.streamingBehavior?: "steer" \| "followUp"`（`types.d.ts:910-920`） | 文档已核实；**但被 fleet 扩展拦住，见 c)** |
| opencode | 未核实 | — | 二进制有 `delivery:"steer"` | **未核实** |

所以三种 CLI 都不需要先 Escape。唯一会「排到回合后」的情况是模型正在一口气输出长文字、不调工具——这种回合上下文涨得慢，等它自然结束即可（结束后无合格回报 → 仍走 split；该行 prompt 会在旧会话里作为下一条排着，旧 tab 随即被 `finish_rotation` 关掉，交接单按草稿有无取模型版或骨架）。

**目标机版本**：本机 pi 1.3.0、Claude Code 2.1.280、codex-cli 0.162.1、opencode v2.0.32。编排者点名的 pi 1.3/1.4、Claude 2.1.3xx、Codex 0.162/0.164 在 Studio / macmini 上的实际版本**本次读不到**（`ssh studio` banner 超时，`macmini` 主机名解析失败），上线前必须在目标机逐项对版本并跑端到端（2026-10-14 Codex 0.161 vs 0.162 事故）。Claude 文档注明 v2.1.281 起队列行为有一处变化（「Before v2.1.281, the key interrupted the turn in both cases」，针对的是立即发送键，不是普通回车），2.1.3xx 目标机要实测。

### b) 现有 v2 投递在 agent 忙时怎么拦、强制打字绕哪道闸

| 闸 | 位置 | 行为 |
|---|---|---|
| `Fleet.pump` | `fleet.py:3758-3787` | 只对 `state == 'idle'` 且有待投消息的槽调 `deliver` |
| `Fleet.deliver` | `fleet.py:3894-3897` | `state != 'idle'` 直接返回 |
| `RuntimeV2.deliver` | `runtime_v2.py:1892-1900` | 要求槽 idle、租约 `status == 'idle'`，否则返回；`bus.offer` 也要 idle 租约 |
| 打字本身 | `cmux_manager.send_prompt_to_idle`（`cmux_manager.py:667-669`） | **不查空闲**，只 `_paste` + 回车 |

强制打字**不走队列、不 offer、不建新投递**：主循环新步骤 `Fleet.midturn_apply()`（放 `fleet.py`，紧挨 `idle_rotate`），事务里写标记后直接调 `self.mgr.send_prompt_to_idle(pane, prompt, bracketed=...)`。这样绕开了上面三道闸，又不碰在途投递的租约 / fence——它只是在在途投递的同一回合里多一句话，回合结算仍挂在原投递上。

### c) 打进去的字会不会被 fleet 自己的钩子拦掉

| 槽 | 会不会被拦 | 证据 | 最小改动 |
|---|---|---|---|
| pi worker | **会，被吞掉**：扩展 `input` 钩子对 `active \|\| !ctx.isIdle()` 返回 `{action: "handled"}` 并提示「派工执行期间请等待；手工输入未加入该任务」，模型根本看不到 | `pi_extension_v2.ts:206` | 在 `input` 钩子最前面加一条：`active` 存在、`event.source === "interactive"`、文本以 `[FLEET MID-TURN HANDOFF fx-` 开头 → `return {action: "continue"}`（交给 pi 按 steer 处理）。约 3 行。人手打同样前缀也会放行——那是人在打字，无害；是否真是中途回合由 daemon 结算时按消息上的 `midturn_id` 判 |
| pi 监工 | **会，被转成 operator_input** 进 daemon 队列，不进模型 | `pi_extension_v2.ts:196-205` | 同一条放行判断放在 `supervisor` 分支之前 |
| Claude worker | **不会**：只有 v2 Claude **监工**装 UserPromptSubmit（`prompt_hook_slot`，`fleet.py:1612-1614`）；Stop 钩子信箱通道不看中途输入。回合结算时 `claude_user_prompts` 只认 `type == 'user'` 行（`fleet.py:1255-1257`），中途插话是 `type: attachment` 行，所以「本回合由该投递发起」的判断（`tui_turn.answered`，`fleet.py:1446-1455`）仍成立，结算照常落到原投递 | 代码 + 转录形态 | 无 |
| Claude 监工 | **可能会**：UserPromptSubmit（`claude-prompt`）对非本代投递正文的人工文字一律转 operator_input 并挡住（design.md §11 Claude 监工小节）；中途插话是否触发 UserPromptSubmit **未核实** | `fleet.py:1322-1360` | 放行规则加一条：文本等于本槽本代登记的中途 prompt（Redis `midturn_id` 对得上）→ 放行、stdout 空 |
| Codex 监工 / worker | **不拦输入，但结算会被拒**：`codex_queue.bound` 要求回合「恰好一条用户消息且全文等于排队文本」，中途 steer 会多一条 → `turn.ignored`，原投递永远不结算 | `codex_queue.py:228-231` | 放宽为：第一条是本投递排队文本，其后的用户消息全文都等于本代登记的中途 prompt（带 `fx-<midturn_id>`）才算 |

---

## 5. 阈值：0.8 下三种窗口能否在 CLI 自压缩前交接完

**局限（先说清）**：探针 1 秒一轮，但会话文件里的用量**只在每次模型响应后才更新**。一次响应之后跑的那批工具结果，要等下一次响应才看得见；而 CLI 自压缩恰好在「工具结果追加之后、下次模型调用之前」检查（pi `docs/compaction.md:37`）。所以一个大工具结果（或一批并行读）可能一步把用量从中途线以下直接顶过自压缩点，**1 秒轮询也接不住**。中途线到自压缩点之间的余量，必须容得下「一批工具结果 + 交接回合本身（prompt + 写 30 行交接单，约 3–5k token）」。

单个工具结果进模型的上限（官方文档）：pi bash / read 2000 行或 50KB（`docs/codemode.md:43`），按中文最坏约 17k token；Claude Code 2.1.280 Read 25,000 token、Bash 30,000 字符、MCP 25,000 token（code.claude.com tools-reference / env-vars / mcp）；Codex 模型 `truncation_policy` 当前为 10,000 token（main `models.json`，0.162.1 未核实）。

| 窗口 | 中途线 0.8W | CLI 自压缩点 C | 余量 C − 0.8W | 能否交接完 |
|---|---|---|---|---|
| **200k（今晚 pi DeepSeek）** | 160,000 | 200,000 − 16,384 = 183,616（pi `docs/compaction.md:32-35`） | **23,616** | **不能保证**。交接回合约 5k，剩约 18.6k：一次 50KB 读（≤17k）勉强容得下，**两个大读并行、或越线那一步本身就落在 170k 以上，pi 就会先自压缩**，交接指令落在压缩之后（压缩后模型仍能看到最近的 prompt，多半还能写交接单，但这时已经丢细节，正是要避免的）。0.75 时余量 33,616，0.7 时 43,616 |
| **400k（Astra，pi provider `openai-codex`）** | 320,000 | 400,000 − 16,384 = 383,616 | **63,616** | 能：容得下 3 个满额大读 + 交接回合 |
| **1M（Claude opus / sonnet）** | 800,000 | 约 967,000（design.md §22 引 CHANGELOG 2.1.247） | **约 167,000** | 能：Claude 单次工具上限 30k，余量够 5 批 |

**报用户**：0.8 在 200k 窗口下不成立（余量 23.6k，一批两个大读就会先自压缩）。按用户定的 0.8 写进缺省，**没有偷偷调低**；是否对 200k 及以下窗口单独设 0.7（名单 `handoff:` 表按 `pi:<model>` 配即可，§22 现成），由用户决定。另两点：
- Claude 窗口 200k 的模型（haiku 等）自压缩点**未核实**；若约 167k，0.8 线 160k 只剩约 7k，同样不成立。
- Claude Code 2.1.296 起 Read 支持 `allow_large`（按剩余上下文定大小），单个工具结果失去上界；目标机若 ≥ 2.1.296，200k 档更危险。

---

## 6. 守卫与边界

| 情形 | 处理 |
|---|---|
| §28 clearing / `clear_required` | 不打 prompt（同 `start_rotation`） |
| 暂停中 | 不打（§7「暂停停止投递 / 换代」）；已打的照常结算 |
| §24 `prompt_wait` 弹框等人 | 不打；已打的兜底停表（删除确认永远不会被杀窗） |
| adopt 槽 / profile 换卡中 | 不打（照抄 `idle_rotate` 守卫） |
| 在途是 handoff 投递本身 | 不打（交接回合撑爆走原 §4 handoff_timeout） |
| `max_busy` | 打 prompt 到 split 期间槽是 busy，之后 handoff，都计忙（`pump` 现有统计）；接班代 starting 不计（§7 原语义） |
| 监工「无人忙必须派单」 | worker 在 handoff / starting 时 `others_active()` 为真（`fleet.py:3289-3293`，`state != 'idle'` 即忙），监工回 RUNNING 不会被判错 |
| 迟到旧代回调 | 不变，按 generation 丢弃 |
| 被杀的长工具留下的半成品 | 程序不担保，靠接班代按交接单复核 |

---

## 7. 测试

- **单测**（真私有 Redis + 假 cmux，沿用 `tests/test_v2_codex_claude.py` 的壳；新文件 `tests/test_midturn_v2.py`）：探针（stat 不变跳过、64KB 尾读、三种 CLI 文件取数、过期结果丢弃、间隔 = max(1 秒, 耗时)）；触发（过 0.8 打一次、同代不再打、各守卫不打、单行 prompt、Claude bracketed）；结算（无回报 → split、续做在队首、不 report、不 blocked、`replay` 不变、`midturn_splits` +1；合格回报 → 正常结算后 idle 换代；pi worker 不走直接 blocked）；兜底（300 秒再打、再 300 秒 `Watchdog.rotate` 续做变体、`prompt_wait` 停表）；上限 3 转 BLOCKED（task / service / 监工三种）、`requeue_lost` 保留计数；service 时钟暂停与恢复；Codex `bound` 放宽（第二条用户消息是登记的中途 prompt 才算）；Claude 监工 UserPromptSubmit 放行中途 prompt；pi 扩展合同测试（带前缀放行 / 不带前缀照旧吞或转 operator_input）。
- **端到端冒烟**（真 cmux、目标机同版本 CLI，新脚本 `scripts/smoke_midturn.sh`）：隔离 `CMUX_FLEET_HOME` / `CMUX_FLEET_STATE`、`--lifecycle smoke`；名单把 `midturn_ratio` 调到 0.10（显式测试阈值，跑完恢复，§4「演示阈值不遗留」）；任务让 worker 逐个读 20 个中等文件再写汇总。断言：审计 `midturn.ordered → midturn.split → rotate.done`；交接单生成；历史页出现 `worker-1.1`；原消息 `outcome=handoff_split`、续做 committed 且 `continues` 指向原消息；task COMPLETED、产物验收过；无 `execution.held`。至少跑 `pi pi task`（今晚组合）、`pi claude task`（缺省组合）、`pi pi service`（请求时限设得比「交接 + 续做」短，断言不超时）、一个 Codex 槽（组合表 unverified 行用 smoke 生命周期）、一次监工槽中途。opencode 在用量来源查清后补一条。

---

## 8. 实现拆分

| 块 | 文件 | 内容 | 成功标准 |
|---|---|---|---|
| **M1 daemon（一块做完）** | `cmux_fleet/midturn.py`（裁剪）、`cmux_fleet/fleet.py`、`cmux_fleet/runtime_v2.py`、`cmux_fleet/watchdog.py`、`cmux_fleet/handoff.py`、`cmux_fleet/codex_queue.py`、`cmux_fleet/roster.py`、`cmux_fleet/pi_extension_v2.ts`（仅 §4c 那条放行）、`tests/test_midturn_limits.py`、`tests/test_midturn_v2.py`（新）、pi 合同测试夹具 | **midturn.py 裁剪**：留用量读取（`claude_transcript_usage`、`codex_rollout_usage`，新增 pi 会话读取，统一 64KB 尾）、阈值（`resolve_settings` / `decide` 去掉 A/B 层参数，`DEFAULTS.midturn_ratio = 0.8`，新增 `midturn_retry = 300`、`midturn_max_splits = 3`）、时钟（`effective_age` / `pause_clock` / `resume_clock` / `pause_message` / `resume_message`）、续做与 BLOCKED 文本（`continuation`、`blocked_text`）、单行 prompt；删 `MIDTURN_INJECT`、`TOOL_LIMITS` / `TOOL_SOURCES`、`limits()` 的 A/B 层 / k / b、`parse_usage_frame`、`order_key` / `order_record` / `LUA_NOTICE` / `posttool_hook` / `posttool_main` / `HOOK_*`、`blocked_prompt`、`midturn_nudge` / `midturn_b_wait`。**fleet.py**：探针线程 + `midturn_apply()`（主循环一步）、强制打字、`deliver` 对 continuation 跳过换卡与顾问、`expire_requests` 按有效时长、Claude 监工 `claude-prompt` 放行、名单 `handoff:` 表接受 `midturn_ratio`（`roster.py:135-169` 一并改）。**runtime_v2.py**：`settle` 的 `handoff_split` 分支、续做入队首、tick expire 按有效时长、兜底计时、`requeue_lost` 抄 `midturn_splits` / `continues`（`runtime_v2.py:1836-1855`）。**watchdog.py**：`rotate(reason='midturn')` 续做变体。**handoff.py**：骨架中途变体（In progress 写原指令头与续做标记）。**codex_queue.py**：`bound` 放宽。**pi_extension_v2.ts**：带前缀放行 3 行 + 首行注释改写 | `tests/test_midturn_limits.py`（裁剪后）与 `tests/test_midturn_v2.py` 全过；全套 `pytest -q tests` 零回归（含 `test_v2_*`、`test_watchdog*`、`test_fleet.py`、`test_handoff_limits.py`、`test_roster.py`、`test_advisor*`、`test_codex_queue*`、pi 合同测试）；control 行显示 `ctx=<used>/<W> mid=<0.8W>` |
| **M2 端到端冒烟** | `scripts/smoke_midturn.sh`（新） | §7 端到端 | 目标机同版本 CLI 跑通 §7 列的组合；脚本开头打印 pi / claude / codex / opencode 版本并与在役机比对；无写死本机路径 |
| **M3 文档** | `docs/design.md` | 新增 §33（本稿压缩版，原样引用用户原话 1、2 作为推翻 `agent_settled only reports; it never requests continuation` 与 §32「长回合中途仍不换代」的依据）；§4 第 1 条补「§33 起回合内过 0.8 也换代」；§32 末尾「已知边界」改指向 §33；§7「pi 适配必须满足」补「带中途前缀的输入放行」；§9 落点表加 `midturn.py`；§5 / §11 写 Claude 监工钩子与 Codex 绑定的放宽 | 替代关系逐条写明；§5 阈值风险（200k 档）原样写进，端到端结果与未验收项如实列 |

---

## 9. 已否决（r1 / r2，2026-10-27 用户叫停：「你不用搞那么复杂」）

- **A/B 两层**（CLI 协作插话层 + daemon 杀窗底线层）、按 CLI 的注入能力表：否决，改为所有 CLI 一律由 daemon 强制打同一行 prompt，杀窗只作超时兜底。
- **pi 扩展用量帧 + bridge 控制流**（`usage` / `midturn` / `abort` 帧、`ctx.abort()`）：否决，改为 daemon 读 pi 会话文件；扩展只留 §4c 的 3 行放行。
- **Claude / Codex PostToolUse `additionalContext` 钩子**：否决，改为直接粘贴（Claude、Codex 运行中回车本就在回合内读到，§4a）。
- **P1–P8 探针脚本与版本比对流水线**：否决，由端到端冒烟在目标机同版本 CLI 上一次验证。
- **按工具截断上限推导的交接余量公式**（R = T·k·b + H）与自动夹取：否决，阈值按用户定的 0.8；余量不足的窗口在 §5 明着报用户，不自动调。
- **等「无工具子进程」空档再杀窗**：否决，兜底只保留「再打一次 → 杀窗续做」。

---

## 10. 未核实（上线前要在目标机核掉）

1. Studio / macmini 上 pi、Claude Code、Codex、opencode 的实际版本（本次 ssh 不通），以及这些版本下「运行中粘贴 + 回车」是否如 §4a 所述在回合内读到。
2. Claude 监工的 UserPromptSubmit 是否对回合中途插话触发（§4c，放行规则先加上，不依赖结论）。
3. opencode：运行中插话行为、会话存储里的用量路径与格式（§2.1）；查不清则 opencode 槽只能明着报 `midturn.no_usage`。
4. Claude 200k 模型（haiku 等）的自压缩点；Codex 不给 `--compact-limit` 时的自压缩点。
5. Codex 0.162.1 实际的单次工具输出上限（main 源码为 10,000 token）。
