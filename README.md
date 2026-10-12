# cmux-fleet

One **supervisor** and N **worker** sessions (new Redis-v2 runs: Pi; legacy v1: Codex/Pi/Claude Code), each in its
own [cmux](https://cmux.dev) tab, coordinated by a small daemon. The supervisor only judges and
dispatches; workers only execute. Sessions whose context fills up are rotated automatically with a
written handoff note, so a task can run for hours across many model sessions.

[中文说明](README.zh-CN.md) · [Design baseline / 已确认设计基线](docs/design.md)

The design baseline preserves the original supervisor/worker roles, naming, handoff and protocols.
See the [2026-10-12 local integration review](reports/full-local-merge-20261012.md) for the current scope and validation.
**Local integration is not production deployment or Redis-v2 runtime acceptance.**

```
 cmux workspace "Fleet agents" (one page)
 ┌───────────────────────────────┬────────────────────────┐
 │ supervisor-1.2                │ [worker-1.1] [worker-2] │
 │ supervisor TUI (selected)     │ worker tabs            │
 │                               │                        │
 ├───────────────────────────────┤ selected worker's TUI  │
 │ [history] [control]           │                        │
 │ retired screens + handoffs    │                        │
 └───────────────────────────────┴────────────────────────┘
 control: the same daemon, private Redis socket, routing, verification and watchdog.
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
  run. Codex agents are sandboxed (`workspace-write`, network on); Pi and the optional Claude workers are not.
- **Context rotation before overflow.** Before each delivery the daemon reads the session's token usage
  from the selected CLI's rollout/transcript or Pi context API. At `--handoff-ratio` (default 60 %) it asks the session to write a
  three-section handoff note, merges it into a machine skeleton, saves the old screen to the history
  page, and starts a successor in the same place (`worker-1` → `worker-1.1` → `worker-1.2`) that reads the note. If the model
  does not write, the skeleton is used; missing model-written notes alone do not block rotation.
  Window/process failures still follow the normal failure path.
- **Stall watchdog.** A busy agent whose rollout file has not changed for `--stall-timeout` seconds
  (default 30 min) is aborted and reported to the supervisor; a stalled supervisor gets one retry, then
  the task is blocked for a human.
- **Stale callbacks are dropped.** Every spawn bakes a generation number into the notify hook; a late
  callback from a retired session is ignored.

## Requirements

- macOS with [cmux](https://cmux.dev) ≥ 0.64.25 (`new-surface --command`). Run `start` **inside a cmux
  terminal**, or set cmux's *automation → socket control mode* away from `cmuxOnly`; by default cmux
  only accepts commands from processes it started.
- For Codex agents: [Codex CLI](https://github.com/openai/codex) ≥ 0.156 (per-file profiles, `check_for_update_on_startup`). The daemon parses Codex's rollout `.jsonl` files (`token_count`,
  `task_complete`, subagent metadata); if a Codex release changes that format, pin the version that
  works for you.
- `redis-server` on PATH (`brew install redis`). Each run starts its own private instance under the
  checkout's `.runtime/redis/<token>/` (socket, PID, log and data); TCP is disabled and it never
  touches `localhost:6379`. The binary is still an external dependency, not bundled.
- Python ≥ 3.9 and the `redis` package.
- Any model Codex can talk to: OpenAI API, ChatGPT login, or a local OpenAI-compatible server. The
  daemon reads the context window from the rollout, so no window size has to be configured.
- Agents run in an **isolated Codex home** (`--codex-home`, default `~/.cmux-fleet/codex-home`):
  `auth.json` and the profile file are symlinked from your `~/.codex`, so the login is shared, but
  your hooks, MCP servers, plugins and skills do not run inside agents (`skills.include_instructions=false`). Per-agent "trust this folder" entries
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

Open a cmux terminal, configure your Pi provider/model and replace `local/my-model` below.
New starts require Redis v2 and an explicit development opt-in. Supported v2 CLI pairs: all-Pi, or a Codex
supervisor with official Claude Code workers in task mode (see "Redis v2 development runtime"); every other pair is refused.
The Codex-specific setup and launch examples below are retained only as legacy v1 reference.

```bash
cmux-fleet start --workers 2 --supervisor-cli pi --worker-cli pi \
  --pi-model local/my-model --allow-unsandboxed-pi --allow-development-v2
echo '{"goal": "Survey the three most-cited papers on X and write a comparison",
       "hours": 4,
       "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["Conclusion"]}]}' \
  | cmux-fleet task -                           # hand the goal to the supervisor
cmux-fleet status                               # task + per-agent state, context usage, inbox depth
cmux-fleet stop                                 # cancel; confirm runtime retirement before Redis shutdown
```

`start` options: `--profile` (Codex profile, default `fleet`), `--codex` / `--redis-server` (binaries,
default: first on PATH), `--redis-runtime-dir` (default: checkout `.runtime/redis`),
`--runs-dir` (default `~/.cmux-fleet/runs`), `--codex-home` (the agents'
isolated Codex home, default `~/.cmux-fleet/codex-home`), `--handoff-ratio`,
`--handoff-timeout`, `--stall-timeout`, `--window` (fallback context size until the rollout reports one),
`--compact-limit` (Codex auto-compaction token limit; by default Codex decides), `--chrome-script`
(see below).

Each run writes to `~/.cmux-fleet/runs/fleet-<time>-<token>/`: `config.json`, `workspace/<worker>/`
(worker cwd, all deliverables land here), `agents/supervisor/`, `handoffs/`, and `prompts/`.
`~/.cmux-fleet/latest.json` points at the newest run and pins `redis_runtime_root`,
`redis_runtime_dir`, `redis_instance_id` and `redis_socket` for callbacks and control.

Private Redis files live separately in `.runtime/redis/<token>/`: `redis.sock`, `redis.pid`,
`redis.log`, `fleet.rdb`, and v2's `appendonlydir/`. The short token avoids Unix socket path-length
limits; directories are owner-only (700). Overlong paths fail before Redis starts, with no `/tmp`
or TCP fallback. Use an explicit short, writable `--redis-runtime-dir` for a read-only/non-editable
installation. Legacy `/tmp/cmux-fleet-*.sock` configs remain supported; existing fleets are not
migrated or restarted. Stop removes the socket through Redis shutdown, but retains logs/data.
Keep `.runtime/` out of Git (a local `.git/info/exclude` entry is sufficient); these are runtime
files, not source. See [the design baseline](docs/design.md#13-redis-项目内运行目录用户新要求).

## Layout and the history page

Everything lives on one cmux page: the supervisor top-left, the **history** browser pane under it, the
workers as tabs of one pane on the right (worker-1, worker-2, …). After startup the daemon's `control`
surface is docked as an inactive tab beside history in the lower-left pane; history stays selected.
Its now-empty startup workspace closes
without killing the daemon. Click `control` only when you need logs; it is not another AI agent. When a session rotates, its successor opens in the same spot and the
retired session's whole screen (with scrollback) and handoff note go to `<run>/history.html`, which the
history pane reloads. On that page you pick an agent (`supervisor`, `worker-1`, …), pick a retired
generation (`supervisor-1`, `supervisor-1.1`, …, `worker-1`, `worker-1.1`, …), switch between its screen
and its handoff note, or search every generation of every agent at once (Enter jumps to the next match).
Successors are named `<slot>.<generation>` for the supervisor and the workers alike: `supervisor-1` → `supervisor-1.1` → `supervisor-1.2`, `worker-1` → `worker-1.1` → `worker-1.2`.

## Service mode (resident fleet)

`start --mode service` keeps the fleet running with no goal and no end: it answers a stream of
requests. The supervisor only routes; each worker can have its own prompt; answers are JSON checked by
the daemon, not by a model.

```bash
cmux-fleet start --mode service --worker-names exit,reentry,rung --roles ~/my-roles \
    --supervisor-cli pi --worker-cli pi --pi-model local/my-model \
    --allow-unsandboxed-pi --allow-development-v2 --max-busy 2 --request-timeout 600
echo '{"id": "cu-1:r7", "to": "exit", "payload": {...},
       "reply": {"required": ["verdict", "param", "evidence"], "allowed": ["verdict", "param", "evidence", "note"]}}' \
  | cmux-fleet request -
cmux-fleet results --drain        # one JSON line per finished request
cmux-fleet pause --reason "market closed"; cmux-fleet resume
```

- `--worker-names`: worker slots named by role (lowercase, no dots; dots mark generations).
- `--roles DIR`: `DIR/<agent>.md` is appended to that agent's `AGENTS.md` as "Your role". One prompt per worker.
- `--supervisor-profile` / `--worker-profile`: separate Codex profiles, e.g. a local model for one side.
- `--max-busy N`: at most N workers work at once (a local model server may reject concurrent large
  requests). The supervisor is not counted.
- A request: `id` (single use, `[A-Za-z0-9][A-Za-z0-9:._-]*`), `payload` (any JSON, ≤ 200 000 chars),
  `reply.required` (keys the answer must have), optional `reply.allowed` (no other keys accepted), optional
  `to` (a worker: Python routes it, no supervisor turn), optional `instruction`, optional `timeout_s`.
  Without `to` the supervisor picks a worker or skips the request.
- The answer must be one JSON object. A bad answer gets one correction; a second bad answer is published
  as `invalid`. Result statuses: `ok`, `invalid`, `skipped`, `timeout`, `stalled`.
- `pause` stops delivery and rotation; requests keep queuing and still time out, so a caller never
  waits forever. A supervisor `BLOCKED`, a closed tab or a failed delivery pauses the fleet instead of
  ending it. Calendars (market hours, holidays) are the caller's business: pause and resume from outside.
- Callers on the same machine can skip the CLI: push `{"kind": "request", "request_id": ..., "spec": ...}`
  to `fx:events` on the private socket named in `~/.cmux-fleet/latest.json`, then read `fx:result:<id>`.

## Worker roster YAML (hot add / remove)

`start --roster roster.yaml` (instead of `--workers` / `--worker-names`) takes the workers from a file,
and the daemon re-reads it while the fleet runs:

```yaml
workers:
  - name: exit              # same rules as --worker-names
    role: roles/exit.md     # optional, relative to the yaml; overrides --roles DIR/exit.md
  - name: reentry
```

- **Add**: a new name is written to `config.json` (and `latest.json` if it points at this run), then
  spawned with the READY prompt as a tab at the end of the worker column. The supervisor sees the new
  list appended to its next message (`[Roster change] Workers you can dispatch to now: ...`).
- **Remove**: the worker takes no new work (dispatch to it is rejected), finishes what it already has,
  and when idle with an empty queue its screen goes to the history page and its tab is closed. Its
  workspace directory stays. Putting it back in the file before that cancels the removal.
- **Role change** for an existing worker applies at its next generation; it is not restarted.
- **Rejected as a whole** (roster kept, reasons in the control log as `roster.rejected`): invalid YAML,
  unknown keys, bad or duplicate names, zero workers, a missing role file, or a name already used in
  this run (a removed worker's name cannot come back). Changes are logged as `roster.changed`.
- Needs PyYAML in the Python that runs the daemon.

## Resume an interrupted run (`start --resume`)

`cmux-fleet start --resume <run-dir>` reattaches an unfinished Redis v2 run instead of creating one;
`--resume auto` takes the run in `~/.cmux-fleet/latest.json` and starts a new run when that one is
finished (`<run>/result.json` exists) or missing. A live daemon makes it a no-op, so a timer may call
`start --resume auto` repeatedly.

- Redis restarts from the run's own directory and socket; AOF brings back everything up to the last
  fsync (about 1 s). A Redis that is still up is reused.
- Official Claude workers whose tab still runs the CLI are reattached; every other agent (the task
  supervisor, Pi agents, closed tabs) starts as its next generation (`worker-1` -> `worker-1.1`) in place.
- Task: verified artifacts and accepted reports are kept; dispatches that were queued or in flight are
  re-queued by the daemon, and the new supervisor gets a summary (goal, artifacts verified/missing,
  completed dispatches with report excerpts, re-queued dispatches). Service: accepted requests without a
  result are routed again; a request with `"replay": false` is published as `timeout`.
- `--max-attempts N` (default 2, stored in config, can be changed on resume): one dispatch / request runs
  at most N times in total; over the cap the task is `blocked` / the request is `timeout`, naming the item.
- A finished task (any terminal status) or an explicit `stop` writes `<run>/result.json`; such a run is
  never resumed. Without `--resume`, a restarted daemon still holds uncertain work and pauses (no replay).

## Interface contracts (task spec `"contracts": true`)

Opt-in per task; without the key nothing changes. Rules: `docs/design.md` §18 (from the external spec
`tasks/plans/cmux-fleet-interface-contract-spec-20261013.md`).

1. **P0 – interfaces.** The supervisor's first reply is `phase: "interfaces"` with a plan (M0…Mn) and
   every interface (path in the producer's worker directory, `contains` / `json_keys`, `writers`,
   `decisions`, a self-passing `sample`), and no dispatch. The daemon rejects each gap by name.
2. **P1 – skeleton run.** The daemon sends `[skeleton]` to `skeleton_worker` (task spec, default the first
   worker); `<worker>/skeleton.md` needs one PASS line per interface and no FAIL. Pass → interfaces frozen
   at v1; fail → back to P0; a second fail blocks the task.
3. **P2 – parallel.** Dispatch items carry `id`, `provides`, `consumes` (`id@version`), `edits`,
   `depends_on` + `serial_reason`; the daemon holds dependent items until their prerequisites are done.
   A worker's `Interfaces: IF-x@n provided: ready` line is checked literally before the interface is
   `ready` and consumers get `[contract] ready`. A trailing `{"interface_change": ...}` object is a
   proposal the supervisor approves or rejects; breaking changes deprecate the old version and mark
   its consumers stale.
4. **COMPLETED** also needs every non-deprecated interface `ready`. Contract tasks may add
   `artifacts[].not_contains` and `artifacts[].cmd_exit` (argv, no shell, run in the shared output
   directory, `timeout_s` default 120 / max 600; output written to `path`).

The program writes `<run>/contracts.json` (copied to `<workspace>/contracts.json`) and
`<run>/contracts.log.jsonl`; agents only read them. Each agent directory gets `CONTRACTS.md`.

## Claude Code workers (optional)

The supervisor defaults to Codex (or select Pi below). Workers can be official [Claude Code](https://claude.com/claude-code) CLI sessions instead:

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

## Pi agents (v1 compatibility by default, Pi >= 1.1.0)

Pi can run the supervisor, workers, or both. Claude workers still run the **official `claude` CLI
as a separate process started by fleet**. Pi does not proxy Claude requests or consume Claude Code's
subscription credentials. This separation is not a guarantee against account limits or enforcement;
follow the providers' terms.

Example: a local-model Pi supervisor with official Claude Code workers:

```bash
cmux-fleet start --mode service --workers 2 \
  --supervisor-cli pi --pi-model local/my-model --pi-thinking off \
  --worker-cli claude --worker-model sonnet \
  --allow-unsandboxed-pi
```

Configure `local/my-model` in Pi's `models.json` first. `--pi-model` is required (no implicit
fallback to a personal/cloud default). To use the same Pi model for workers instead, select
`--worker-cli pi`. An all-Pi fleet needs neither a Codex binary nor a Codex profile.

- `--pi PATH`: Pi CLI binary, >= 1.1.0 (the `agent_settled` lifecycle is required).
- `--pi-agent-dir DIR`: source Pi directory, default `$PI_CODING_AGENT_DIR` or `~/.pi/agent`.
  Only its `auth.json` and `models.json` are linked into a fresh `<run>/pi-home`. Credentials are
  not printed. No Claude configuration/credentials are copied. Model-file credential commands
  are executable configuration: use a trusted source.
- Discovered extensions, skills, MCP, prompt templates, themes, project settings and context
  files are disabled. Only the bundled bridge extension loads, and the generated `AGENTS.md`
  is explicitly appended to the system prompt. Pi's read/bash/edit/write tools remain enabled.
- **Pi has no OS sandbox.** `--allow-unsandboxed-pi` is an explicit acknowledgement, not a sandbox
  switch. Role instructions are not a security boundary. Use a dedicated account/VM for untrusted tasks.
- Pi session names and terminal titles use the fleet's existing label (`supervisor-1.1`, `worker-1.1`,
  etc.), not the cwd or a separate naming scheme. The original slot/generation/history rules stay unchanged.
- In the supervisor TUI, ordinary typed text enters the daemon's existing request/task queue instead
  of bypassing it. In service mode Python assigns a real request ID, the worker answers in a `reply`
  JSON field, and the result is queued back to the supervisor for reporting. In task mode text starts
  a task or supplies a follow-up to the current task. Slash commands and daemon inbox pointers are
  not re-enqueued; images are rejected with a message (supply a file path instead).
- Pi reports completion only on `agent_settled`, after tools, retries and automatic continuations.
  Generation plus inbox delivery ID reject stale/duplicate completions, including after Escape.
  The ID comes only from the final daemon pointer in this agent's own `.inbox`, never an inbox path
  quoted in the instruction text (e.g. a supervisor's path forwarded to a worker).
  An unsuccessful startup never counts as READY; error/aborted partial JSON is never accepted as a result.
- Context usage comes from Pi's `getContextUsage()`; each generation writes its session under
  `<run>/pi-sessions/<slot>/g<N>/`. Fleet still handles idle-boundary rotation and history; Pi's
  automatic compaction remains a safety net within a long turn. `--compact-limit` is Codex-only.
- The Pi bridge callback runs through the fleet Python interpreter and **only its private Redis
  Unix socket**, not `localhost:6379`. A callback failure is shown by Pi; the fleet watchdog
  eventually handles a missing completion.

Optional live adapter smoke (uses a temporary private Redis, fake cmux, real Pi/model; **no Claude calls**):

```bash
python tests/pi_smoke.py --model local/my-model
```

This tests READY, JSON request/result and handoff to a new generation. It is not a real cmux TUI test.

## Redis v2 development runtime

V2 now connects the existing Fleet dispatcher to persistent Pi bridges. Select it explicitly
on a fresh isolated development run with `--transport-version 2 --allow-development-v2`
and `--supervisor-cli pi --worker-cli pi`, plus the existing explicit model/permission options.

Codex supervisor + official Claude Code workers (task mode, user decision 2026-10-12) also run on v2.
The daemon receives each delivery from Redis and types only its business text into the official
TUI (Claude panes get a bracketed paste); no `.inbox` file and no transport ID reach the model.
The Codex `notify` / Claude `Stop` hooks bind the session on READY and then settle only the delivery
this generation received, only when the typed text started that turn; the daemon validates it as for Pi.
Agents keep the original AGENTS.md reply formats. Six parallel Claude workers with an Astra supervisor:

```bash
# once: a Codex profile file for the supervisor model (Codex >= 0.156 reads ~/.codex/<name>.config.toml)
printf 'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n' > ~/.codex/fleet-astra.config.toml
cmux-fleet start --supervisor-cli codex --supervisor-profile fleet-astra \
  --worker-cli claude --worker-model sonnet --workers 6 --max-busy 0 \
  --allow-development-v2
cmux-fleet task goal.json
```

Validated locally with `--claude` pointing at the real Claude Code binary; the cmux `claude` shim found
first on PATH inside cmux was not exercised. Service mode, Claude supervisors, Codex workers and
Pi/official-CLI mixes stay refused with a reason
before provisioning or profile retirement.
New `start` commands now require Redis v2 (the default); `--transport-version 1` is rejected. Existing v1 configs remain readable for draining/status/stop only: do not edit their transport version in place. The file-delivery and native-Codex/Claude startup descriptions above are legacy behavior, not a supported new v2 launch path. Unsupported CLI combinations fail rather than silently falling back to files. The development opt-in is still required.

V2 deliveries use run-scoped Redis Streams and full business context rather than message files
or cmux paste. Structured `fleet_decision` / `fleet_result` tools collect candidates; settled plus
daemon validation commits them. UI cards are non-context session entries. Simple replies and
mechanical failures cause no supervisor reporting turn. Task reports still require supervisor
judgment and independent artifact acceptance. External request/result JSON remains compatible.

Timeout, interruption or disconnection holds the slot/PEL until reliable retirement confirms stop.
A same-run daemon restart retains sessions and pauses uncertain work; it never starts a second set
of agents or shuts Redis down as crash cleanup. Explicit v2 fleet stop retires runtimes before
shutting down Redis. New v2 Redis uses AOF everysec, with about a one-second host-crash loss window.

`cmux-fleet logs --config <isolated-config> --request <id> --slot worker-1 --generation 0
--event result --level error --json --follow` reads audit detail through the independent control
reader; it never ACKs work. Control failure does not determine scheduler commit.

**This is a development integration, not production acceptance.** The local merge candidate passed
the full regression suite, independent real-Redis race probes and installed Pi SDK/TUI contracts.
An earlier frozen candidate passed a real Redis + Fleet + Pi/FlashNext TUI smoke on owned PTYs
(cmux layout simulated); the final error-audit rerun was refused by the model server's memory guard. See the
[Redis mailbox integration report](reports/redis-mailbox-merge-20261012/validation.md).
Prepared effect intents can replay; ambiguous Redis script failures hold the run for inspection.
Full crash/slow-UI coverage, 1000-message end-to-end accounting, performance, capacity/AOF and
mixed-CLI validation remain gates. V2 browser-broker execution currently rejects explicitly.
See [the design baseline](docs/design.md#11-redis-通信与分层显示-v22026-10-11-新要求开发接线待验收).

Run the isolated acceptance suite on a parent that permits temporary Redis sockets:

```bash
python3 -m pytest -q tests/test_runtime_v2.py tests/test_message_bus_v2.py
python3 -m pytest -q tests/test_pi_v2_contract.py tests/test_runtime_v2_planning.py
python3 -m pytest -q tests
```

These tests use freshly created `/tmp/cmux-fleet-<token>.sock` instances with `port=0`, fake cmux
and no model/GUI/production registry. The installed Pi packages are required by the TS harness.

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
| `fx:events` | list | daemon BLPOP: `turn` (CLI callback), `pi_session`; `task` / `request` / `operator_input` / `pause` / `resume` / `stop` / `chrome` (with `request_id`) |
| `fx:reply:<request_id>` | string | request reply (24 h) |
| `fx:agent:<name>` | hash | role, pane, thread, generation, state (starting/idle/busy/handoff/dead), ctx_used, ctx_window, busy_since, stalls, handoff_* |
| `fx:inbox:<name>` | list | pending messages; delivered only when the agent is idle |
| `fx:replies:<name>` | list | last 5 replies (used for the handoff skeleton) |
| `fx:task` | hash | id, goal, status, completion, reason, deadline, artifacts, bad_replies, bounces |
| `fx:contracts` | string | contract tasks only: JSON phase / plan / registry / proposals / task table / per-worker report owners |
| `fx:log` | list | event log, last 2000 |
| `fx:seen:<sha256>` | string | notify de-duplication (SET NX, 24 h) |
| `fx:service` | hash | paused, reason, history_pane, bad_replies |
| `fx:req:<id>` | hash | an open request: spec, status (routing/dispatched), worker, created, attempts |
| `fx:reqq` | list | ids of open requests (timeout scan) |
| `fx:result:<id>` | string | finished request result JSON (7 days) |
| `fx:results` | list | finished results in order, last 1000 (`results --drain` pops them) |

## Test status

- **End-to-end on a clean second Mac (macOS 26.2, cmux 0.64.25, codex-cli 0.156.1, GPT-6-Sol via a
  ChatGPT login):** `start` → both agents READY in 7 s → a one-step task completed with the artifact
  check in 40 s → a two-step task during which the supervisor hit the handoff ratio, wrote its own
  handoff note, was retired into a read-only record pane and replaced by a successor that finished
  the task. Rotation, artifact verification, the update-dialog suppression and the isolated Codex home
  were all exercised live.
- Historical Pi-adapter suite (before dynamic routing and Redis-v2 foundations): **91 passed** locally, including the actual TypeScript bridge contract;
  isolated target tests: **90 passed, 1 skipped** because Node was not on that test process's PATH.
- Real Pi + Flash Next + cmux 0.65 validation covered READY, request/result, both-role handoff and
  history. A real supervisor TUI instruction to ask a worker to reply OK completed end-to-end.
  Control/history share a pane without restarting the daemon. See [design status](docs/design.md#10-验证与交付状态截至本次固化).
- A real Pi-supervisor / official-Claude-worker task has **not** been exercised; only that mixed CLI
  dispatch path is unit-tested. Development validation does not waive the review/deployment gate.
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

Most tests use in-memory Redis, fake cmux and synthetic transcripts. Redis v2 integration tests
start only owned temporary Redis processes; the TS contract harness starts a JSONL fixture child.
They do not start a model or a real cmux fleet.

## License

MIT
