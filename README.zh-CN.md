# zhiying-agent-fleet

[![tests](https://github.com/justindbca/zhiying-agent-fleet/actions/workflows/tests.yml/badge.svg)](https://github.com/justindbca/zhiying-agent-fleet/actions/workflows/tests.yml)

命令行与 Python 包仍叫 `cmux-fleet` / `cmux_fleet`（fleet 跑在 [cmux](https://cmux.dev) 上）。

一个 daemon 把**一个监工（supervisor）加 N 个写代码的 worker** 作为看得见的终端会话跑起来，各占一个
[cmux](https://cmux.dev) 标签。监工只判断、只派活；worker 只执行；结果由 daemon 自己核验，不信模型的自述。
上下文快满的会话会自动写交接单并换新会话接力，一个任务可以跨很多个模型会话跑几个小时。

同一个 fleet 里可以混用官方 **Claude Code**、官方 **Codex CLI**、**Pi**（Codex 模型与本地模型）和 **opencode**；
哪些「监工 × worker」组合可用，以真跑冒烟验过的为准（`cmux_fleet/config/fleet.yaml#cli_pairs`）。

[English](README.md) · **[设计基线](docs/design.md)**

要点：字面验收（产物不在 / 内容不对就不算完成）；上下文换代接力；每个 run 一个私有 Redis（只走 Unix socket）；
卡死看门与预算刹车（账号写在私有配置文件的 `accounts` 块里，不进代码）；**只有一个配置文件** `cmux_fleet/config/fleet.yaml`，一张表一个块，私有的 `~/.cmux-fleet/fleet.yaml` 同格式覆盖允许改的块；
子 fleet；**2,000+ 个测试**。

## 为什么这样做

用交互式 TUI 而不是无头的 `codex exec`，是为了**看得见**：每个标签都能盯，必要时能亲自敲进去，不想要了点 ✕ 就关。
daemon 从不读屏幕：Codex 的 `notify` 钩子在每一轮结束时回调 daemon，daemon 只在会话空闲时把下一条消息粘贴进去。

daemon 独立于模型的自述做这几件事：

- **验收是字面的。** 任务只有在声明的每个产物都存在、够长、含必需文字时才算完成。监工三次误报 `COMPLETED` 直接判阻塞。
- **模型不能提交宿主命令。** 监工只能声明要检查的文件，不能提交要跑的命令。Codex agent 跑在沙盒里（可写工作区、开网络）；pi 和可选的 Claude worker 没有这一层沙盒。
- **上下文满之前先轮换。** 每次投递前 daemon 从 Codex 自己的 rollout 文件读 token 用量。到 `--handoff-ratio`（默认 60%）就让会话写三节交接单，
  并进机器骨架，旧屏幕存进历史页，在原位置起接班者（`worker-1` → `worker-1.1` → `worker-1.2`）读交接单继续。模型不写就用骨架，不会单因缺交接文本阻塞；窗口/进程错误仍按原故障语义处理。
  窗口和比例按每个 agent 当前的 CLI / 模型分别取（docs/design.md §22）：roster 的 worker 条目 `window` / `handoff_ratio` ＞ roster `handoff:` 表（`<cli>:<model>` 再 `<cli>`）＞ config 同名表 ＞ 内置（Claude 模型名含 `[1m]`/`opus`/`fable`/`sonnet` → 1,000,000，其余 → `--claude-window`；Codex / Pi 用自报窗口）＞ `--handoff-ratio`。roster 改了下一次投递检查即生效，不重启；`status` 显示每个 agent 的生效值和来源。
- **卡死看门。** 忙碌中的 agent 若 rollout 文件 `--stall-timeout` 秒（默认 20 分钟；transport v2 另有 5 分钟多信号窗口 `--stall-window`，见 docs/design.md §24）没动，就中止该轮并报给监工；监工自己卡死给一次重试，再卡就判阻塞交人。
- **过期回调一律丢弃。** 每次拉起会话都把代数烘进 notify 钩子参数，退役会话的迟到回调会被忽略。

## 依赖

- macOS + [cmux](https://cmux.dev) ≥ 0.64.25（用到 `new-surface --command`）。`start` 要**在 cmux 终端里跑**，或把 cmux 设置里 automation → socket control mode 从 `cmuxOnly` 改掉；默认 cmux 只接受它自己窗口里起的进程。
- [Codex CLI](https://github.com/openai/codex) ≥ 0.156（独立文件的配置档、`check_for_update_on_startup`）。
  daemon 解析 Codex 的 rollout `.jsonl`（`token_count`、`task_complete`、子 agent 元数据），Codex 若改格式，请钉住能用的版本。
- PATH 里有 `redis-server`（`brew install redis`）。每次运行自起私有实例，socket、PID、日志和数据集中在项目 `.runtime/redis/<token>/`，关闭 TCP，绝不碰 `localhost:6379`。Redis 二进制仍是依赖，不代表已打包进软件。
- Python ≥ 3.9 和 `redis` 包。
- 任何 Codex 能连的模型：OpenAI API、ChatGPT 登录、本地 OpenAI 兼容服务。上下文窗口从 rollout 读，不用配。
- agent 跑在**隔离的 Codex home**（`--codex-home`，默认 `~/.cmux-fleet/codex-home`）：`auth.json` 和配置档文件从你的 `~/.codex` 软链过来，登录共享，但你的钩子、MCP、插件不会在 agent 里跑；每个 agent 目录的信任条目自动写入。

## 安装

```bash
git clone https://github.com/justindbca/zhiying-agent-fleet && cd zhiying-agent-fleet
pip install -e .
```

给 agent 加一个 Codex 配置档。Codex ≥ 0.156 每个配置档是独立文件（见 [`codex.config.example.toml`](codex.config.example.toml)）：

```toml
# ~/.codex/fleet.config.toml   （用 `codex --profile fleet` 选中）
model = "gpt-5.5"
```

别的不用配：fleet 会给 agent 建一个独立的 Codex home，并自动替每个 agent 目录回答 Codex 的「信任此目录？」。

## 用法

在 cmux 终端里先配置好 Pi provider/model，并替换下面的 `local/my-model`。新启动只允许 Redis v2，需要显式开发许可。不带 `--supervisor-cli` / `--worker-cli` 时，`start` 取 `cmux_fleet/config/fleet.yaml#cli_pairs` 的缺省组合：**pi 监工（`openai-codex/gpt-6-astra`，thinking high）+ 官方 Claude Code worker**（用户 2026-10-13 定）。支持的组合及状态以该表为准（见下文「Redis v2 开发接线」），表里没有的组合直接拒绝。`start` 还必须带 `--lifecycle <模式>`（`service`、`dev_task`、`program`、`scheduled`、`review_loop`、`smoke`，见 `cmux_fleet/config/fleet.yaml#lifecycle_modes`）；`unverified` 组合只能 `--lifecycle smoke` 启动。

```bash
cmux-fleet start --workers 2 --supervisor-cli pi --worker-cli pi --lifecycle <模式> \
  --pi-model local/my-model --allow-unsandboxed-pi --allow-development-v2
echo '{"goal": "……", "hours": 4, "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["结论"]}]}' \
  | cmux-fleet task -               # 把目标交给监工
cmux-fleet status                   # 任务 + 各 agent 状态 / 上下文占用 / 待投递数
cmux-fleet stop                     # 取消任务，确认 runtime 退役后才关闭 Redis
```

`start` 参数：`--profile`（Codex 配置档，默认 `fleet`）、`--codex` / `--redis-server`（二进制路径，默认 PATH 里第一个）、
`--redis-runtime-dir`（默认项目 `.runtime/redis`）、`--runs-dir`（默认 `~/.cmux-fleet/runs`）、`--codex-home`（agent 的隔离 Codex home，默认 `~/.cmux-fleet/codex-home`）、`--handoff-ratio`、`--handoff-timeout`、`--stall-timeout`、
`--window`（rollout 还没报窗口时的兜底值）、`--compact-limit`（Codex 自动压缩阈值，默认交给 Codex 决定）、`--chrome-script`（见下）。

每次运行写在 `~/.cmux-fleet/runs/fleet-<时间>-<token>/`：`config.json`、`workspace/<worker>/`（worker 的 cwd，产物都在这）、
`agents/supervisor/`、`handoffs/`、`prompts/`。`~/.cmux-fleet/latest.json` 指向最近一次，并固定该次 Redis 的目录、实例 ID 和 socket 路径，回调与 control 共用。

私有 Redis 文件独立集中在项目 `.runtime/redis/<token>/`：`redis.sock`、`redis.pid`、`redis.log`、`fleet.rdb`，v2 的 AOF 在 `appendonlydir/`。目录权限 700；短 token 避免 Unix socket 路径过长，超长在启动 Redis 前明确失败，不回退 `/tmp` 或 TCP。非 editable/只读安装可显式传短且可写的 `--redis-runtime-dir`。旧 `/tmp/cmux-fleet-*.sock` 配置继续兼容，不迁移或重启在途实例；stop 后保留日志和数据。`.runtime/` 不纳入 Git，可用本地 `.git/info/exclude` 排除；详见[设计基线 §13](docs/design.md#13-redis-项目内运行目录用户新要求)。

## 一页布局与历史页

所有窗口在同一个 cmux 页面：左上 supervisor，左下「history」浏览器格，右边一个区域放所有 worker 的标签页（worker-1、worker-2 …，点标签切换）。启动后 daemon 的 control 终端收进左下 history 同一栏的后台标签，该栏默认显示 history，supervisor 独占左上；关闭已空的启动工作区不杀 daemon。control 只是日志控制台，不是另一个 AI。换代时接班者在原位置打开；
退下来的那一代的整屏（含回滚）和交接单写进 `<run>/history.html`，历史格自动刷新。页面上可选 agent（supervisor / worker-1 …）、
选某一代（supervisor-1、supervisor-1.1 …，worker-1、worker-1.1 …），切换看屏幕或交接单，或一次搜索所有 agent 的所有代（回车跳下一处）。
supervisor 和 worker 一样按「槽位.代数」命名：supervisor-1 → supervisor-1.1 → supervisor-1.2，worker-1 → worker-1.1 → worker-1.2。

## 服务模式（常驻 fleet）

`start --mode service`：没有目标、没有结束，持续处理一串请求。supervisor 只分派；每个 worker 可以有自己的 prompt（`--roles DIR`，
`DIR/<名字>.md` 追加进它的 AGENTS.md）；回答必须是一个 JSON 对象，由 daemon 按 `reply.required` / `reply.allowed` 字面校验，
答错一次给一次更正机会，再错发布为 `invalid`。`--worker-names` 按角色给 worker 命名；`--supervisor-profile` / `--worker-profile`
两边可用不同模型（如本地模型）；`--max-busy N` 限同时干活的 worker 数；`pause` / `resume` 由外部（交易日历等）控制，
暂停时不投递、不换代，请求照常排队并会超时。命令与字段见英文 README「Service mode」。

## worker 名单 YAML（运行中加 / 卸）

`start --roster roster.yaml`（与 `--workers` / `--worker-names` 二选一）从文件取 worker，fleet 运行中 daemon 会重读这个文件：

```yaml
workers:
  - name: exit              # 命名规则同 --worker-names
    role: roles/exit.md     # 可选，相对 yaml 所在目录；优先于 --roles DIR/exit.md
  - name: reentry
```

- **加**：新名字先写进 `config.json`（`latest.json` 指向本 run 时一起写），再用 READY 提示启动，作为标签放到右侧 worker 栏末尾；监工的下一条消息末尾会附上新名单。
- **卸**：该 worker 不再接新活（派给它会被拒），手上的活做完、队列空了才退役：屏幕存进历史页、关掉标签；工作目录保留。退役前把它写回文件即取消卸载。
- **只改角色**：在它下一代生效，不重启。
- **整份拒收**（名单不变，原因记在 control 日志 `roster.rejected`）：YAML 错、未知字段、名字不合法或重复、零个 worker、角色文件不存在、本 run 用过的名字（卸掉的名字不能再加回）。变更记为 `roster.changed`。
- **附带标签**（docs/design.md §31）：可选顶层 `tabs: [{name, command, cwd?, env?}]`（name `^[a-z][a-z0-9-]*$` 且唯一；cwd 相对名单目录、支持 `~`；env 值为字符串），终端程序（不是 agent），开在本 fleet 工作区 control 标签后面，不抢焦点。每次起 fleet（`start`、`--resume`、子 fleet `daemon --resume`）开之前先把命令行与该程序精确相等的进程 SIGTERM（约 5 秒后 SIGKILL）再开，匹配规则见 §31；`stop` / `close_all` 一并关；daemon 活着时标签被关不会自动重开，等下次起 fleet。程序以 fleet 用户身份运行，名单即授权。
- 跑 daemon 的 Python 需装 PyYAML。

## 断点续跑（`start --resume`）

`cmux-fleet start --resume <run 目录>` 接上一个没结束的 Redis v2 run，不新建；`--resume auto` 取 `~/.cmux-fleet/latest.json` 指向的 run，它已结束（有 `<run>/result.json`）或不存在就照常新建。daemon 还活着则什么都不做，可由定时器反复调用。

- 私有 Redis 用原目录、原 socket 重启，AOF 恢复到最后一次落盘（约 1 秒内）；Redis 还活着就直接用。
- 窗口还在且 CLI 进程还在的官方 Claude worker 接回原窗口；其余（task 监工、Pi、窗口没了的）在原位起下一代（`worker-1` → `worker-1.1`）。
- task：已验过的产出与已接受的回报不重做；排队中或在途的派发由 daemon 重新排队，新监工收到一份交底（目标、产出已验/缺、已完成派发及回报摘要、已重排的派发）。service：已受理但没有结果的请求重新分派；请求里写 `"replay": false` 的记 `timeout`。
- `--max-attempts N`（缺省 2，存进 config，续跑时可改）：同一条派发 / 请求最多跑 N 次，超限 task 判 `blocked`、请求记 `timeout`，写明是哪条。
- task 终结（任一终态）或显式 `stop` 会写 `<run>/result.json`，这样的 run 不再续跑。不带 `--resume` 的 daemon 重启仍按原规则：不确定的在途消息挂起、整套暂停，不重做。详见 design.md §17。

## pi 监工 + 官方 Claude Code worker（v1 说明；v2 缺省组合见「Redis v2 开发接线」）

pi 可运行 supervisor 或 worker；Claude 侧始终由 fleet **直接启动官方 `claude` CLI**，不是 pi 接 Claude 订阅接口，
也不读取/复制 Claude 的登录凭证给 pi。仍需遵守服务条款，不能据此保证不会限流或封号。

```bash
cmux-fleet start --lifecycle service --workers 2 \
  --supervisor-cli pi --pi-model local/my-model --pi-thinking off \
  --worker-cli claude --worker-model sonnet \
  --allow-unsandboxed-pi --allow-development-v2
```

显式 `--supervisor-cli pi` 的模型取 `--pi-model`（不读组合表缺省）。

- pi ≥ 1.1.0；`--pi` 指定真实 CLI 路径。先在 pi 的 `models.json` 配好 `local/my-model`，替换示例模型名。
- `--pi-model` 是所有没有自己模型的 pi agent 的兜底，缺了就拒绝，不隐式回退到云模型。若 worker 也用本地 pi，改 `--worker-cli pi`；全 pi 无需 Codex binary/profile。
- 分 agent 指定 pi 模型，优先级从高到低：
  - 监工：`--supervisor-pi-model`（或未给 `--supervisor-cli` 时旧配置档映射，`--supervisor-profile fleet-astra` → `openai-codex/gpt-6-astra`）> 显式 `--pi-model` >（**仅未给 `--supervisor-cli`**，pi 取自 `fleet.yaml#cli_pairs` 缺省）`default.supervisor_pi_model`。手敲的旗标永不被组合表缺省盖过。显式 `--supervisor-cli pi` **不读**组合表缺省，须给 `--supervisor-pi-model` 或 `--pi-model`。所以只给 `start --pi-model X` 时缺省 pi 监工跑 X；想监工保持 Astra、worker 用 X，须另给 `--supervisor-pi-model`。
  - worker：live 档案卡的 `model` > 名单行 `pi_model:`（仅 pi worker，不能与 `profile:` 同行）> `--pi-model`。
  - thinking 与模型分开解析、同一规则：监工 = `--supervisor-pi-thinking`（或映射档位）> 显式 `--pi-thinking` > `default.supervisor_pi_thinking`（仅缺省监工）> `off`；worker = 档案卡 > 名单 `pi_thinking:` > `--pi-thinking` > `off`。例：只给 `--pi-model X` → 缺省监工 X / high；再加 `--pi-thinking low` → X / low。
  - 任一 pi agent 解析不出模型 → 拒绝启动：`--pi-model provider/model is required for pi agents (or --supervisor-pi-model / roster pi_model); no model for: ...`。
  - 非 pi 监工（`--supervisor-cli codex|claude`）带 `--supervisor-pi-model` / `--supervisor-pi-thinking` → 报错，不是忽略。显式 `--supervisor-cli pi` 再带 `--supervisor-profile X` → 报错（改用 `--supervisor-pi-model`）；旧配置档映射只对缺省监工生效。
  - 名单热载改了某行的 `pi_model` / `pi_thinking`，该 worker 空闲后在原槽位用新模型起下一代。
- `--pi-agent-dir` 指定已有 pi 配置目录（默认环境变量 `PI_CODING_AGENT_DIR` 或 `~/.pi/agent`），只软链其中的
  `auth.json`、`models.json` 到本次 `<run>/pi-home`，不带用户 hooks/skills/MCP/扩展，也不改用户默认模型。
- 仅加载 fleet 自带 pi 桥接扩展，生成的 AGENTS.md 显式注入。会话文件按 `<run>/pi-sessions/<槽位>/g<代数>/` 隔离。
- **pi 无 OS 沙盒**，所以必须显式给 `--allow-unsandboxed-pi`。该参数是风险确认，不会创建沙盒；提示词禁令不等于权限隔离。
- pi 会话名和终端标题直接使用 fleet 原有的 `supervisor-1.1`、`worker-1.1` 等标识，不另造命名规则，原槽位/换代/历史机制不变。
- 可以直接在 supervisor 输入框用自然语言派活：文字接入原 daemon 队列。service 模式由 Python 建真实请求 ID，worker 将文字答案放进 `reply` 字段，结果再交回 supervisor 转告；task 模式首次文字建任务、后续文字补充当前任务。不会把 daemon 的消息指针、`/model` 等命令再次入队。图像附件需改成文件路径。
- 回合结束用 `agent_settled`（不是可能仍会重试的 `agent_end`），回调绑定代数和投递 ID。只取消息末尾指向本 agent `.inbox` 的投递 ID，不取正文引用的 supervisor/其他 agent 路径。错误/中止的半截 JSON 不会当成成功。
- 用量由 pi API 提供，fleet 仍负责轮换与历史页；pi 自动压缩只作长回合保险。`--compact-limit` 只作用于 Codex。
- 回调只写本套 fleet 的私有 Redis socket；回调失败交给看门处理，不会回退生产 Redis。

可选真实 pi 冒烟：`python tests/pi_smoke.py --model local/my-model`。仅启动临时私有 Redis，用假 cmux 验证 READY、
请求/JSON 结果、换代/接力；不调用 Claude，不代表真实 cmux 窗口验收通过。

## Redis v2 开发接线

新 `start` 默认且只允许 Redis v2，须带 `--allow-development-v2`；`--transport-version 1` 明确拒绝。旧配置仍可 status/收束/stop，但不可原地修改版本冒充迁移。上文 Codex/Claude 启动示例属于旧版；未支持的 v2 CLI 组合直接失败，不回退文件信箱或暗换 CLI。
v2 已接到原 Fleet 的队列/分派/验收链，常驻 Python bridge 阻塞读 Redis，把业务正文直接送给 Pi；不写消息 Markdown、不粘贴收件。
结果工具只暂存候选，settled 后由 daemon 验证；简单 reply/失败/超时为 appendEntry UI-only 卡，不增加监工机械转述回合。
task 报告仍另送监工判断，产物仍独立验收。

2026-10-12 用户新增：**Codex 监工 + 官方 Claude Code worker（仅 task 模式）**也走 v2。daemon 从 Redis 收件后只把业务正文打进官方 TUI（Claude 窗格用 bracketed 粘贴），不写 `.inbox`、不带运输 ID；Codex notify / Claude Stop 钩子在 READY 时绑定会话，之后只结算本代已收的投递且须由打进去的正文发起该回合，再由 daemon 按原规则校验。启动示例见英文 README “Redis v2 development runtime”。官方 Claude v1 保留。

2026-10-13 用户定：**除 Claude Code 外，Codex 与 FlashNext 一律走 pi；Claude 只走官方 `claude` CLI**，pi 不接 Claude 订阅、不拿 Claude 凭证。

- **缺省组合**：pi 监工 `openai-codex/gpt-6-astra`、thinking high（ChatGPT 订阅经 pi 的 `openai-codex` provider 登录）+ 官方 Claude Code worker。来自 `fleet.yaml#cli_pairs` 的 `default.supervisor_pi_model` / `supervisor_pi_thinking`，可用 `--supervisor-pi-model` / `--supervisor-pi-thinking` 或显式 `--pi-model` / `--pi-thinking` 覆盖（显式 `--supervisor-cli pi` 不读这两个缺省键，须自给 `--supervisor-pi-model` 或 `--pi-model`；完整优先级见上文 pi 段）；worker 是 Claude 时不需要 `--pi-model`；仍须 `--allow-unsandboxed-pi`。`pi+claude` task/service 两行已 `verified`（真实 `scripts/smoke_pair.sh pi claude task|service` 4/4 通过，run `fleet-20261013-115145-828661b9` / `fleet-20261013-115347-7e61f9af`，2026-10-13，未提交分支树）：

  ```bash
  cmux-fleet start --worker-model sonnet --workers 2 --lifecycle <模式> \
    --allow-unsandboxed-pi --allow-development-v2
  ```
- **夜间组合**：pi Astra 监工 + pi FlashNext worker（已 verified 的 `pi+pi` 行，分 agent 模型；此处必须给 `--supervisor-pi-model`，否则 `--pi-model` 会让监工也跑 FlashNext）：

  ```bash
  cmux-fleet start --supervisor-cli pi --worker-cli pi \
    --supervisor-pi-model openai-codex/gpt-6-astra --supervisor-pi-thinking high \
    --pi-model flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP --workers 2 --lifecycle <模式> \
    --allow-unsandboxed-pi --allow-development-v2
  ```
- **官方 Codex 保留为可选**：`--supervisor-cli codex [--supervisor-profile fleet-astra]` 照旧起官方 Codex TUI 监工。老写法不带 `--supervisor-cli`、给 `--supervisor-profile fleet-astra`（或 worker 非 Codex 时的 `--profile fleet-astra`）时，改为起 pi 监工（`openai-codex/gpt-6-astra`，thinking high）并向 stderr 打印提示；同时给了不同的 `--supervisor-pi-model` 则报错。其它配置档名在此情形下报错并给出两种新用法（`--supervisor-cli codex --supervisor-profile X` 或 `--supervisor-pi-model provider/model`），绝不静默起官方 Codex TUI。官方 Codex 例：

  ```bash
  cmux-fleet start --supervisor-cli codex --supervisor-profile fleet-astra \
    --worker-cli claude --worker-model sonnet --workers 6 --lifecycle <模式> --allow-development-v2
  ```
- 各组合状态（verified / unverified = 只能 `--lifecycle smoke` / 拒绝）以 `cmux_fleet/config/fleet.yaml#cli_pairs` 为准；Codex worker（`codex+codex`）仍为 unverified，表里没有的组合在 provisioning/退役前带原因拒绝。

timeout/中止/通信失联保留槽位和 PEL，未确认停止不重新派活。daemon 同 run 重启先 held/paused，不重起 agents、不因异常关闭 Redis。
显式 v2 stop 确认各 runtime 退役后才关 Redis。AOF everysec 仍有约 1 秒主机崩溃丢失窗口。`logs --config ... --request ... --json --follow` 为只读详情导出。

**这不是生产验收。** 本地集成候选已通过完整回归、真实 Redis 竞态探针与已安装 Pi 的 SDK/TUI 合同。
早一版冻结候选已通过真实 Redis + Fleet + Pi/FlashNext TUI 输入闭环（独立 PTY，cmux 布局模拟）；追加错误审计后的复测被模型后端内存保护拒绝启动。
完整 crash/慢 UI/1000 条整链对账、性能、AOF/容量和混合 CLI 仍待验收，development gate 保留。
可运行的隔离测试命令见英文 README 的 Redis v2 章节；v2 browser broker 尚未接入并明确拒绝。

## 任务怎么流转

1. `task` 把目标放进监工的 inbox，daemon 粘贴进监工标签。
2. 监工回一个 JSON：`{"status": "RUNNING", "completion": 30, "reason": "…", "dispatch": [{"to": "worker-1", "instruction": "…"}], "artifacts": [...]}`。连续三次坏 JSON 判阻塞。
3. 每条 `dispatch` 进对应 worker 的 inbox，worker 空闲时投递。
4. worker 的回复带 `[worker-1 report]` 标签回到监工。
5. `COMPLETED` 对照声明的产物校验（用户预声明的也算）。不过就以「Independent verification failed」打回，三次打回判阻塞。
6. `BLOCKED` 停任务，原因留在 `status` 里交给人。

## agent 范围文件

`cmux_fleet/agents/supervisor.md`、`worker.md` 在拉起时复制到各 agent 的 cwd 做 `AGENTS.md`（`{{WORKSPACE}}`、`{{FLEET_CHROME}}` 会被填入实际值）。
里面只有禁令、命令表、回复格式，不写方法论——模型对禁令和表格的遵从度远高于「怎么思考」的散文。

## 可选：浏览器代办

沙盒里的 agent 操作不了用户的浏览器（也绝不该能杀掉它）。如果你有一个实现了 `check` / `tabs` / `newtab <url>` / `go <tab> <url>` / `url <tab>` / `js <tab> <file>`
这几个动词的脚本，用 `start --chrome-script /path/to/broker` 传入。worker 调 `fleet.py chrome <动词> …`（确切命令已写进它的 `AGENTS.md`），
daemon 校验动词、把 `js` 文件限制在本次运行目录、给页面动作限速（`--chrome-min-interval`，默认 20 秒），然后在宿主上跑脚本。不传 `--chrome-script` 这些动词一律拒绝。

## 测试状态

- **在另一台干净的 Mac 上端到端跑通**（macOS 26.2、cmux 0.64.25、codex-cli 0.156.1、ChatGPT 登录的 GPT-6-Sol）：`start` → 两个 agent 7 秒 READY →
  单步任务 40 秒完成并过产物校验 → 两步任务中监工触到轮换阈值、自己写了交接单、旧标签收成只读记录格、接班者继续把任务做完。
  轮换、产物验收、更新弹窗压制、隔离的 Codex home 都在真盘跑过。
- 当前版本：macOS + Python 3.14 全套 **2,000+ 个测试**（约 13 分钟）；缺 redis-server / node / tmux / pi 的测试自动跳过。
- 真实 pi + Flash Next + cmux0.65 已验证启动、请求、双角色换代、历史；直接在 supervisor 里要求 worker 回复OK，worker回复及supervisor转告闭环通过；control/history同栏且移动不中断daemon。
- Claude真实混合任务仍未实测，只验证了官方CLI路由单测；独立复核及生产合并/部署仍未完成。详见[设计基线](docs/design.md)。
- 还没真盘跑过：卡死看门（只有单测）、浏览器代办。

## 真盘跑出来的坑（都已修）

- 往刚起的登录 shell 里敲 1.5 KB 的长命令会被截断（行编辑器就位前 macOS 终端每行最多 1024 字节）。现在每次拉起先写一个短启动脚本，由 cmux 的 `--command` 执行。
- Codex 的「Trust this folder?」按精确目录问，且不接受用 `-c` 注入信任表。fleet 现在给 agent 一个自己的 `CODEX_HOME`，逐 agent 写信任条目。
- agent 跑在你个人的 Codex home 里会把你的钩子、MCP、插件一起带起来，一个失败的 Stop 钩子就卡住了 READY 回合。隔离 home 同时解决了这个，绕过钩子信任的旗标也去掉了。
- 新会话底噪已超 `--handoff-ratio` 时会无限轮换（1% 测试阈值下十分钟换了 21 代）。现在新会话永不轮换，daemon 记 `rotate.impossible` 照常投递。

## 已知限制

- 只支持 macOS（cmux）。有 `caffeinate` 时 daemon 用它防止机器休眠。
- 新建 cmux 工作区时会短暂抢焦点（`new-workspace` 不认 `--focus false`），daemon 会切回来。
- Codex 没有标签的流式日志；标签退役时把最后一屏存到交接单旁边。完整记录是 `~/.codex/sessions` 下的 rollout 文件。
- 模型偶尔会一轮把派的两步都做完。产物校验管结果，不管越权。

## 开发

```bash
pip install -e '.[dev]'
pytest
```

多数测试用内存 Redis、假 cmux 与合成 transcript；v2 集成只起自己创建的临时 Redis，TS 合同测试只起 JSONL fixture 子进程，不起模型或真实 cmux fleet。

## 许可

MIT
