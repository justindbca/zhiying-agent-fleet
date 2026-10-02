# cmux-fleet

一个**监工**（supervisor）加 N 个**执行**（worker），全是 [Codex CLI](https://github.com/openai/codex)
交互会话，各占一个 [cmux](https://cmux.dev) 标签，由一个小 daemon 协调。监工只判断、只派活；worker 只执行。
上下文快满的会话会自动写交接单并换新会话接力，一个任务可以跨很多个模型会话跑几个小时。

[English](README.md)

## 为什么这样做

用交互式 TUI 而不是无头的 `codex exec`，是为了**看得见**：每个标签都能盯，必要时能亲自敲进去，不想要了点 ✕ 就关。
daemon 从不读屏幕：Codex 的 `notify` 钩子在每一轮结束时回调 daemon，daemon 只在会话空闲时把下一条消息粘贴进去。

daemon 独立于模型的自述做这几件事：

- **验收是字面的。** 任务只有在声明的每个产物都存在、够长、含必需文字时才算完成。监工三次误报 `COMPLETED` 直接判阻塞。
- **模型不能提交宿主命令。** 监工只能声明要检查的文件，不能提交要跑的命令。worker 跑在沙盒里（可写工作区、开网络）。
- **上下文满之前先轮换。** 每次投递前 daemon 从 Codex 自己的 rollout 文件读 token 用量。到 `--handoff-ratio`（默认 60%）就让会话写三节交接单，
  并进机器骨架，把旧标签收成只读记录，起接班者（`worker-1` → `worker-1.1` → `worker-1.2`）读交接单继续。模型不写就用骨架，轮换永不失败。
- **卡死看门。** 忙碌中的 agent 若 rollout 文件 `--stall-timeout` 秒（默认 30 分钟）没动，就中止该轮并报给监工；监工自己卡死给一次重试，再卡就判阻塞交人。
- **过期回调一律丢弃。** 每次拉起会话都把代数烘进 notify 钩子参数，退役会话的迟到回调会被忽略。

## 依赖

- macOS + [cmux](https://cmux.dev) ≥ 0.64（daemon **必须在 cmux 终端里启动**，cmux 只接受它自己窗口里起的进程）。
- [Codex CLI](https://github.com/openai/codex) ≥ 0.156（用到 `--dangerously-bypass-hook-trust`、`check_for_update_on_startup`）。
  daemon 解析 Codex 的 rollout `.jsonl`（`token_count`、`task_complete`、子 agent 元数据），Codex 若改格式，请钉住能用的版本。
- PATH 里有 `redis-server`（`brew install redis`）。每次运行自起一个实例，只监听 `/tmp` 下的 Unix 套接字，绝不碰 `localhost:6379`。
- Python ≥ 3.9 和 `redis` 包。
- 任何 Codex 能连的模型：OpenAI API、ChatGPT 登录、本地 OpenAI 兼容服务。上下文窗口从 rollout 读，不用配。

## 安装

```bash
git clone https://github.com/<you>/cmux-fleet && cd cmux-fleet
pip install -e .
```

给 agent 加一个 Codex 配置档（见 [`codex.config.example.toml`](codex.config.example.toml)）：

```toml
# ~/.codex/config.toml
[profiles.fleet]
model = "gpt-5.5"
```

## 用法

在 cmux 终端里：

```bash
cmux-fleet start --workers 2        # 起私有 redis + 控制台标签；daemon 再拉起 3 个 codex 标签
echo '{"goal": "……", "hours": 4, "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["结论"]}]}' \
  | cmux-fleet task -               # 把目标交给监工
cmux-fleet status                   # 任务 + 各 agent 状态 / 上下文占用 / 待投递数
cmux-fleet stop                     # 取消任务，关 daemon 和它的 redis；标签保留，✕ 关
```

`start` 参数：`--profile`（Codex 配置档，默认 `fleet`）、`--codex` / `--redis-server`（二进制路径，默认 PATH 里第一个）、
`--runs-dir`（默认 `~/.cmux-fleet/runs`）、`--handoff-ratio`、`--handoff-timeout`、`--stall-timeout`、
`--window`（rollout 还没报窗口时的兜底值）、`--compact-limit`（Codex 自动压缩阈值，默认交给 Codex 决定）、`--chrome-script`（见下）。

每次运行写在 `~/.cmux-fleet/runs/fleet-<时间>-<token>/`：`config.json`、`workspace/<worker>/`（worker 的 cwd，产物都在这）、
`agents/supervisor/`、`handoffs/`、`prompts/`、私有 Redis 的 dump 和日志。`~/.cmux-fleet/latest.json` 指向最近一次。

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

- 本代码的前身（同一套 daemon 逻辑、中文提示词、固定一个本地模型）在 codex-cli 0.149–0.156 上跑过数小时任务，worker 自动交接连跑 5 代。
- codex-cli 0.156.1 上在 cmux 里实测过粘贴链路（多行粘贴进编辑框、不加括号粘贴标记）和更新弹窗的行为。
- 本仓库这个抽出来的、模型无关的版本**抽出后还没有端到端起过一次**（daemon + 三个真 Codex 标签）。25 个测试用内存 Redis、假 cmux 管理器和合成 rollout 文件跑 daemon 逻辑。第一次真跑若有提示词或 cmux 调用不对，请提 issue。

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

测试用内存版 Redis、假 cmux 管理器和合成的 rollout 文件，不起模型、不起 cmux、不起 redis-server。

## 许可

MIT
