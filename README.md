# cmux-fleet

One **supervisor** and N **worker** [Codex CLI](https://github.com/openai/codex) sessions, each in its
own [cmux](https://cmux.dev) tab, coordinated by a small daemon. The supervisor only judges and
dispatches; workers only execute. Sessions whose context fills up are rotated automatically with a
written handoff note, so a task can run for hours across many model sessions.

[中文说明](README.zh-CN.md)

```
 cmux workspace "Fleet agents"                 cmux workspace "Fleet control"
 ┌──────────────┬──────────────┬─────────────┐  ┌──────────────────────────────┐
 │ supervisor   │ worker-1     │ worker-2    │  │ daemon (fleet.py daemon)     │
 │ codex TUI    │ codex TUI    │ codex TUI   │  │  ├─ private Redis (unix sock)│
 │              │              │             │  │  ├─ routes replies           │
 │ ↑ notify ────┼──────────────┼─────────────┼─▶│  ├─ verifies artifacts       │
 │   hook       │              │             │  │  ├─ rotates full sessions    │
 │ ◀────────────┴── pastes next message ─────┼──│  └─ stall watchdog           │
 └──────────────┴──────────────┴─────────────┘  └──────────────────────────────┘
```

## Why

Running agents as interactive TUIs instead of headless `codex exec` calls keeps them visible: you can
watch every tab, type into one if you must, and close any of them with the ✕. The daemon never scrapes
screens. Codex's `notify` hook calls back into the daemon at the end of every turn, and the daemon pastes
the next message only when a session is idle.

What the daemon enforces, independently of what the models claim:

- **Acceptance is literal.** A task ends only when every declared artifact exists, is long enough and
  contains the required text. Three false `COMPLETED` claims block the task.
- **No host commands from models.** The supervisor can only declare files to check, never commands to
  run. Workers are sandboxed (`workspace-write`, network on).
- **Context rotation before overflow.** Before each delivery the daemon reads the session's token usage
  from Codex's own rollout file. At `--handoff-ratio` (default 60 %) it asks the session to write a
  three-section handoff note, merges it into a machine skeleton, retires the tab into a read-only record,
  and starts a successor (`worker-1` → `worker-1.1` → `worker-1.2`) that reads the note. If the model
  does not write, the skeleton is used; a rotation never fails.
- **Stall watchdog.** A busy agent whose rollout file has not changed for `--stall-timeout` seconds
  (default 30 min) is aborted and reported to the supervisor; a stalled supervisor gets one retry, then
  the task is blocked for a human.
- **Stale callbacks are dropped.** Every spawn bakes a generation number into the notify hook; a late
  callback from a retired session is ignored.

## Requirements

- macOS with [cmux](https://cmux.dev) ≥ 0.64.25 (`new-surface --command`). Run `start` **inside a cmux
  terminal**, or set cmux's *automation → socket control mode* away from `cmuxOnly`; by default cmux
  only accepts commands from processes it started.
- [Codex CLI](https://github.com/openai/codex) ≥ 0.156 (per-file profiles, `check_for_update_on_startup`). The daemon parses Codex's rollout `.jsonl` files (`token_count`,
  `task_complete`, subagent metadata); if a Codex release changes that format, pin the version that
  works for you.
- `redis-server` on PATH (`brew install redis`). Each run starts its own instance on a Unix socket under
  `/tmp`; it never touches `localhost:6379`.
- Python ≥ 3.9 and the `redis` package.
- Any model Codex can talk to: OpenAI API, ChatGPT login, or a local OpenAI-compatible server. The
  daemon reads the context window from the rollout, so no window size has to be configured.
- Agents run in an **isolated Codex home** (`--codex-home`, default `~/.cmux-fleet/codex-home`):
  `auth.json` and the profile file are symlinked from your `~/.codex`, so the login is shared, but
  your hooks, MCP servers and plugins do not run inside agents. Per-agent "trust this folder" entries
  are written there automatically.

## Install

```bash
git clone https://github.com/<you>/cmux-fleet && cd cmux-fleet
pip install -e .
```

Add a Codex profile for the agents. Codex ≥ 0.156 keeps each profile in its own file
(see [`codex.config.example.toml`](codex.config.example.toml)):

```toml
# ~/.codex/fleet.config.toml   (selected with `codex --profile fleet`)
model = "gpt-5.5"
```

Nothing else: the fleet creates its own Codex home for the agents and answers Codex's
"trust this folder?" prompt for each agent directory itself.

## Use

Open a cmux terminal, then:

```bash
cmux-fleet start --workers 2                   # private redis + control tab; the daemon spawns 3 codex tabs
echo '{"goal": "Survey the three most-cited papers on X and write a comparison",
       "hours": 4,
       "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["Conclusion"]}]}' \
  | cmux-fleet task -                           # hand the goal to the supervisor
cmux-fleet status                               # task + per-agent state, context usage, inbox depth
cmux-fleet stop                                 # cancel, shut the daemon and its redis down; tabs stay, close with ✕
```

`start` options: `--profile` (Codex profile, default `fleet`), `--codex` / `--redis-server` (binaries,
default: first on PATH), `--runs-dir` (default `~/.cmux-fleet/runs`), `--codex-home` (the agents'
isolated Codex home, default `~/.cmux-fleet/codex-home`), `--handoff-ratio`,
`--handoff-timeout`, `--stall-timeout`, `--window` (fallback context size until the rollout reports one),
`--compact-limit` (Codex auto-compaction token limit; by default Codex decides), `--chrome-script`
(see below).

Each run writes to `~/.cmux-fleet/runs/fleet-<time>-<token>/`: `config.json`, `workspace/<worker>/`
(worker cwd, all deliverables land here), `agents/supervisor/`, `handoffs/`, `prompts/`, the private
Redis dump and log. `~/.cmux-fleet/latest.json` points at the newest run.

## Claude Code workers (optional)

The supervisor is always Codex. Workers can be [Claude Code](https://claude.com/claude-code) sessions instead:

```bash
cmux-fleet start --workers 2 --worker-cli claude --worker-model sonnet --worker-kit ~/my-kit
```

- Each worker runs `claude --dangerously-skip-permissions --model <model> --settings <hook> --add-dir <workspace> [--mcp-config mcp.json] -- <prompt>`.
- Turn ends come from a Claude Code **Stop hook** (`fleet.py claude-stop`) instead of Codex `notify`. A Stop that still lists
  `background_tasks` (subagents running in the background) is ignored; the session stops again when they finish.
- Context usage and the stall watchdog read the session transcript (plus its `subagents/` transcripts).
- Multi-line messages are written to `<worker>/.inbox/msg-*.md` and the tab gets a one-line pointer: Claude Code would
  submit a pasted newline as Enter.
- The "trust this folder?" prompt is pre-answered in `~/.claude.json`; the scope file is also written as `CLAUDE.md`.
- `--worker-kit DIR` is copied into every worker cwd: `DIR/.claude/{agents,skills}` and `DIR/mcp.json`.

## How a task flows

1. `task` puts the goal in the supervisor's inbox. The daemon pastes it into the supervisor tab.
2. The supervisor replies with one JSON object:
   `{"status": "RUNNING", "completion": 30, "reason": "...", "dispatch": [{"to": "worker-1", "instruction": "..."}], "artifacts": [...]}`.
   Bad JSON three times in a row blocks the task.
3. Each `dispatch` item goes to that worker's inbox and is delivered when the worker is idle.
4. A worker's reply comes back to the supervisor tagged `[worker-1 report]`.
5. `COMPLETED` is checked against the declared artifacts (user-declared ones count). Failures bounce
   back as "Independent verification failed"; three bounces block the task.
6. `BLOCKED` stops the task and leaves the reason in `status` for a human.

## Agent scope files

`cmux_fleet/agents/supervisor.md` and `worker.md` are copied into each agent's cwd as `AGENTS.md` at
spawn (`{{WORKSPACE}}` and `{{FLEET_CHROME}}` are filled in). They contain only prohibitions, a command
table and the reply format; no methodology. Models follow prohibitions and tables far more reliably than
prose about "how to think".

## Optional: browser broker

Sandboxed agents cannot drive the user's browser (and should never be able to kill it). If you have a
script that implements the verbs `check`, `tabs`, `newtab <url>`, `go <tab> <url>`, `url <tab>` and
`js <tab> <file>`, pass it with `start --chrome-script /path/to/broker`. Workers then call
`fleet.py chrome <verb> ...` (the exact command is written into their `AGENTS.md`), the daemon validates
the verb, confines `js` files to the run directory, paces page actions (`--chrome-min-interval`, default
20 s) and runs the script on the host. Without `--chrome-script` the verbs are refused.

## Redis keys (private instance only)

| Key | Type | Content |
|---|---|---|
| `fx:events` | list | daemon BLPOP: `turn` (notify hook), `task` / `stop` / `chrome` (requests with `request_id`) |
| `fx:reply:<request_id>` | string | request reply (24 h) |
| `fx:agent:<name>` | hash | role, pane, thread, generation, state (starting/idle/busy/handoff/dead), ctx_used, ctx_window, busy_since, stalls, handoff_* |
| `fx:inbox:<name>` | list | pending messages; delivered only when the agent is idle |
| `fx:replies:<name>` | list | last 5 replies (used for the handoff skeleton) |
| `fx:task` | hash | id, goal, status, completion, reason, deadline, artifacts, bad_replies, bounces |
| `fx:log` | list | event log, last 2000 |
| `fx:seen:<sha256>` | string | notify de-duplication (SET NX, 24 h) |

## Test status

- **End-to-end on a clean second Mac (macOS 26.2, cmux 0.64.25, codex-cli 0.156.1, GPT-6-Sol via a
  ChatGPT login):** `start` → both agents READY in 7 s → a one-step task completed with the artifact
  check in 40 s → a two-step task during which the supervisor hit the handoff ratio, wrote its own
  handoff note, was retired into a read-only record pane and replaced by a successor that finished
  the task. Rotation, artifact verification, the update-dialog suppression and the isolated Codex home
  were all exercised live.
- The unit suite (27 tests) runs the daemon against an in-memory Redis, a fake cmux manager and
  synthetic rollout files.
- Not exercised live yet: the stall watchdog (unit-tested only) and the browser broker.

## What the live run taught us (already fixed)

- Typing a ~1.5 kB command into a fresh login shell truncates it (macOS tty line limit before the
  line editor is up). Spawns now go through a short launcher script and cmux's `--command`.
- Codex asks "Trust this folder?" per exact directory and does not accept the trust table as a `-c`
  override. The fleet runs agents in its own `CODEX_HOME` and writes the trust entry per agent.
- Running agents in your personal Codex home also runs your hooks, MCP servers and plugins inside
  them; a failing Stop hook blocked the READY turn. The isolated home fixes this too, and the
  hook-trust bypass flag is gone.
- A fresh session whose baseline usage already exceeds `--handoff-ratio` would be rotated forever
  (observed: 21 generations in ten minutes at a 1 % test ratio). Fresh sessions are never rotated;
  the daemon logs `rotate.impossible` and delivers anyway.

## Known limitations

- macOS only (cmux). The daemon uses `caffeinate` when available so the machine does not sleep.
- A new cmux workspace briefly steals focus when it is created (cmux ignores `--focus false` for
  `new-workspace`); the daemon switches you back.
- Codex keeps no streaming log of a tab; when a tab is retired its final screen is saved next to the
  handoff note. The full transcript is Codex's rollout file under `~/.codex/sessions`.
- Models occasionally do more than one dispatched step in a turn. The artifact check catches the
  outcome, not the overreach.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests use an in-memory Redis, a fake cmux manager and synthetic rollout files; they never start a
model, cmux or redis-server.

## License

MIT
