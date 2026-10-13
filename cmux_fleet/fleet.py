#!/usr/bin/env python3
"""cmux-fleet: one supervisor + N workers in cmux tabs, coordinated by a daemon.

Agents use Codex by default, with optional Pi supervisors/workers and official Claude
Code workers. The daemon never reads screens: each CLI's hook/extension calls back at
the end of a turn. Replies route through a private Redis instance listening only on a
project-local Unix socket under .runtime/redis (never localhost:6379).

Subcommands
  start     provision the private Redis + the control surface (must run inside cmux)
  daemon    the coordinator loop (internal, spawned by ``start``)
  task      submit a goal to the supervisor (task mode)
  request   submit one request (service mode)
  results   print / drain finished request results (service mode)
  pause     stop delivering and rotating (service mode; requests keep queuing)
  resume    undo pause
  status    task + per-agent state, context usage, inbox depth
  stop      cancel the task, shut the daemon and its Redis down (tabs stay open)
  chrome    run a vetted browser verb through the daemon (for sandboxed agents, optional)
  agents    list or match offline agent profiles (does not contact a fleet)
  plan      build an offline dynamic dispatch plan (does not contact a fleet)
  notify    Codex notify hook (internal)
  claude-stop  Claude Code Stop hook for claude workers (internal)
  pi-event  Pi settled/session extension callback (internal)
  actor     exec the configured CLI for one agent (internal)
"""
import argparse
import collections
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # runnable as a script (actor/notify argv point at this file)
from cmux_fleet import advisor, contracts, handoff, history, kit as kitmod, peer, pi_adapter, profile_runtime, profile_launch, roster, runtime_v2, redis_runtime, subfleet  # noqa: E402
from cmux_fleet.agent_registry import AgentRegistry  # noqa: E402
from cmux_fleet.assignment import assign_profiles  # noqa: E402
from cmux_fleet.handoff import read_usage  # noqa: E402
from cmux_fleet.message_bus import BusError  # noqa: E402
from cmux_fleet.planner import build_dispatch_plan  # noqa: E402
from cmux_fleet.router import route_task  # noqa: E402

SESSION = 'cmux-fleet'  # cmux registry name
HOME = Path(os.environ.get('CMUX_FLEET_HOME') or (Path.home() / '.cmux-fleet'))
LATEST = HOME / 'latest.json'
USER_CODEX_HOME = Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex'))
EVENTS, TASK, LOG, NAMES = 'fx:events', 'fx:task', 'fx:log', 'fx:names'
ROSTER = 'fx:roster'  # notify=1: tell the supervisor the current workers with its next message
# Service mode: a resident fleet that handles a stream of requests instead of one goal.
REQ, REQQ, RESULT, RESULTS, SERVICE = 'fx:req:', 'fx:reqq', 'fx:result:', 'fx:results', 'fx:service'
REQ_ID_RE = r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}'
REQ_HEAD = re.compile(r'^\[Request (' + REQ_ID_RE + r')\]')
SUPERVISOR_INSTRUCTION_HEAD = re.compile(r'^\[Supervisor instruction ([^\]\n]+)\] ', re.MULTILINE)
WORKER_NAME_RE = r'[a-z][a-z0-9-]{0,30}'
# Display name of the supervisor's first generation; successors are supervisor-1.1, supervisor-1.2, ...
# (same <slot>.<generation> pattern as worker-1 -> worker-1.1). Redis keys and the cwd stay 'supervisor'.
SUPERVISOR_LABEL = 'supervisor-1'
RESULT_TTL = 7 * 86400
RESUME_CLIS = ('codex', 'claude', 'pi', 'node')  # process names that prove a reattachable CLI pane
# Sandboxed agents cannot drive the user's browser themselves (and must never be allowed
# to kill it). If the operator supplies a broker script, the daemon runs these verbs for them.
CHROME_VERBS = {'check', 'tabs', 'newtab', 'go', 'url', 'js'}
THREAD_RE = r'[0-9a-f-]{36}'
DEFAULTS = {'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 100000, 'stall_timeout': 1800,
            'chrome_min_interval': 20, 'profile': 'fleet', 'worker_cli': 'codex', 'worker_model': 'sonnet',
            'claude_window': 200000, 'mode': 'task', 'max_busy': 0, 'request_timeout': 900,
            'apply_profiles_live': False, 'allow_profile_live_switch': False, 'profile_ready_timeout': 180,
            'transport_version': 1, 'allow_development_v2': False, 'max_attempts': 2}
CLAUDE_JSON = Path.home() / '.claude.json'
CONTRACTS = 'fx:contracts'  # contract tasks only: one JSON state document (cmux_fleet/contracts.py)
ESCALATIONS = 'fx:escalations'  # task mode: {task id: worker's split request / approval / delegation} (§20)
ITEMS = 'fx:items'  # plain task mode: {dispatch id: {to, instruction}} for dispatch items that carry an id
ESCALATION_DECISIONS = ('approve', 'revise', 'reject')
CMD_TIMEOUT, CMD_TIMEOUT_MAX = 120, 600  # artifact cmd_exit (contract tasks): seconds the daemon may block
# Advisor runs only (start --advisor): audit hash per consultation, its counter, dispatches awaiting
# advice per worker (instruction -> advise_note), and notes appended to the supervisor's next message.
ADVICE, ADVICE_SEQ, ADVICE_PENDING, ADVICE_NOTES = 'fx:advice:', 'fx:advice:seq', 'fx:advice:pending:', 'fx:advice:notes'


def load(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


def connect(cfg):
    import redis
    sock = redis_runtime.validate_socket(cfg)
    client = redis.Redis(unix_socket_path=str(sock), db=0, decode_responses=True,
                         socket_connect_timeout=3, socket_timeout=5)
    client.ping()  # Fail closed: never fall back to localhost:6379.
    return client


def agent_cwd(cfg, name):
    if name == 'supervisor' and cfg.get('supervisor_cwd'):
        return Path(cfg['supervisor_cwd'])  # sub-fleet: the adopted worker pane keeps its own directory
    run = Path(cfg['run'])
    return run / 'agents' / name if name == 'supervisor' else run / 'workspace' / name


def key(kind, name):
    return 'fx:' + kind + ':' + name


def manager_for(cfg):
    """The cmux registry of one fleet: the main fleet is 'cmux-fleet', a sub-fleet 'cmux-fleet:<task id>'."""
    from cmux_fleet.cmux_manager import CmuxSessionManager
    extra = {k: cfg[k] for k in ('agents_title', 'control_title') if cfg.get(k)}
    if 'cmux_state_dir' in cfg:
        extra['state_dir'] = cfg['cmux_state_dir']
    return CmuxSessionManager(cfg.get('session') or SESSION, **extra)


def codex_home(cfg):
    """The Codex home the agents run in (isolated from the operator's own hooks/MCP servers)."""
    return Path(cfg.get('codex_home') or (HOME / 'codex-home'))


def sessions_dir(cfg):
    return codex_home(cfg) / 'sessions'


def prepare_codex_home(home, profiles):
    """Create the agents' Codex home: shared login, the chosen profile file, no hooks, no MCP.

    auth.json and <profile>.config.toml are symlinked from the operator's Codex home, so the
    login (and its token refreshes) stay shared without copying secrets. config.toml is written
    once; `trust_agent_dir` appends per-agent trust entries to it later.
    """
    profiles = [profiles] if isinstance(profiles, str) else list(dict.fromkeys(profiles))
    home.mkdir(parents=True, exist_ok=True)
    for name in ['auth.json'] + [p + '.config.toml' for p in profiles]:
        src, dst = USER_CODEX_HOME / name, home / name
        if src.exists() and not dst.exists():
            dst.symlink_to(src)
    for profile in profiles:
        if not (home / (profile + '.config.toml')).exists():
            raise SystemExit('profile file not found: ' + str(USER_CODEX_HOME / (profile + '.config.toml'))
                             + ' (Codex >= 0.156 keeps each profile in <CODEX_HOME>/<profile>.config.toml)')
    cfg_file = home / 'config.toml'
    if not cfg_file.exists():
        cfg_file.write_text('# Generated by cmux-fleet. Agents run with CODEX_HOME pointed here so the\n'
                            '# operator\'s own hooks, MCP servers and plugins do not run inside agents.\n'
                            '# auth.json and the profile file are symlinks into your real Codex home.\n'
                            'check_for_update_on_startup = false\n')
    return home


def trust_agent_dir(home, cwd):
    """Codex asks "Trust this folder?" per exact directory; pre-answer it for an agent cwd."""
    cfg_file = home / 'config.toml'
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    entry = '[projects.' + json.dumps(str(cwd)) + ']'
    text = cfg_file.read_text() if cfg_file.exists() else ''
    if entry not in text:
        with cfg_file.open('a') as f:
            f.write('\n' + entry + '\ntrust_level = "trusted"\n')


def agent_cli(cfg, name):
    return cfg.get('supervisor_cli' if name == 'supervisor' else 'worker_cli') or 'codex'


def is_claude(cfg, name):
    return agent_cli(cfg, name) == 'claude'


def is_pi(cfg, name):
    return agent_cli(cfg, name) == 'pi'


def trust_claude_dir(cwd):
    """Claude Code asks "trust this folder?" per directory, and a trusted parent covers its
    subdirectories. Write ~/.claude.json only when neither the agent cwd nor any parent is
    trusted yet, and then trust the fleet home (or the cwd outside it) once, so running Claude
    sessions are not raced on every spawn."""
    cwd = Path(cwd).resolve()
    with open(str(CLAUDE_JSON) + '.fleet.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Unreadable or half-written config: refuse rather than replace the user's file with {}.
        data = json.loads(CLAUDE_JSON.read_text()) if CLAUDE_JSON.exists() else {}
        projects = data.setdefault('projects', {})
        if any(projects.get(str(d), {}).get('hasTrustDialogAccepted') for d in [cwd, *cwd.parents]):
            return
        root = HOME.resolve() if HOME.exists() and HOME.resolve() in cwd.parents else cwd
        projects.setdefault(str(root), {})['hasTrustDialogAccepted'] = True
        tmp = CLAUDE_JSON.with_suffix('.json.fleet-tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(CLAUDE_JSON)


def install_kit(kit, cwd):
    """Copy a worker kit (.claude/agents, .claude/skills, mcp.json) into a claude worker's cwd."""
    if not kit:
        return
    kit = Path(kit).expanduser()
    if (kit / '.claude').is_dir():
        shutil.copytree(kit / '.claude', cwd / '.claude', dirs_exist_ok=True)
    if (kit / 'mcp.json').exists():
        shutil.copy(kit / 'mcp.json', cwd / 'mcp.json')


def claude_transcript_usage(path, window):
    """Context used by a Claude Code session: the last main-thread assistant usage in its transcript."""
    try:
        lines = _tail(Path(path), 524288)
    except OSError:
        return 0, window
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        usage = (row.get('message') or {}).get('usage') if row.get('type') == 'assistant' else None
        if usage and not row.get('isSidechain'):
            used = sum(int(usage.get(k) or 0) for k in
                       ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens'))
            return used, window
    return 0, window


CLAUDE_1M = ('[1m]', 'opus', 'fable', 'sonnet')  # built-in: Claude models with a native 1,000,000-token window (code.claude.com/docs/en/model-config)


def handoff_limits(cli, model, cfg, table=None, worker=None):
    """Effective (window, ratio, source) for one agent (docs/design.md §22).

    Window and ratio are resolved separately, first hit wins: the roster worker entry, the
    roster ``handoff`` table (``<cli>:<model>`` then ``<cli>``), the config ``handoff`` table
    (same keys), then built-ins / start flags. ``window`` None means "measured" (Codex rollout,
    Pi report). Only Claude needs a built-in: its transcript carries no window."""
    layers = [('worker', worker or {})]
    for where, tab in (('roster', table or {}), ('config', cfg.get('handoff') or {})):
        layers += [(where + ' ' + k, tab[k]) for k in ((cli + ':' + model) if model else None, cli) if k and k in tab]
    window = ratio = None
    for where, entry in layers:
        if window is None and 'window' in entry:
            window, wsrc = int(entry['window']), where
        if ratio is None and 'ratio' in entry:
            ratio, rsrc = float(entry['ratio']), where
    if window is None:
        if cli != 'claude':
            wsrc = 'measured'
        elif any(t in (model or '').lower() for t in CLAUDE_1M):
            window, wsrc = 1000000, 'built-in ' + (model or '')
        else:
            window, wsrc = int(cfg['claude_window']), '--claude-window'
    if ratio is None:
        ratio, rsrc = float(cfg['handoff_ratio']), '--handoff-ratio'
    return window, ratio, 'window: ' + wsrc + '; ratio: ' + rsrc


def claude_stop(cfg_path, name, generation, raw):
    """Claude Code Stop hook. Pushes a turn event unless background work will resume the turn."""
    data = json.loads(raw or '{}')
    if data.get('background_tasks'):
        return False  # subagents still running in the background: the session will wake and stop again
    text = data.get('last_assistant_message')
    if text is None:
        text = ''
        try:
            for line in reversed(_tail(Path(data.get('transcript_path', '')), 262144)):
                row = json.loads(line)
                if row.get('type') == 'assistant' and not row.get('isSidechain'):
                    parts = (row.get('message') or {}).get('content') or []
                    text = '\n'.join(p.get('text', '') for p in parts if isinstance(p, dict) and p.get('type') == 'text')
                    if text:
                        break
        except (OSError, ValueError):
            pass
    cfg = load(cfg_path)
    if cfg.get('transport_version') == 2:
        cfg, name, generation = subfleet.delegated_route(cfg, name, int(generation))
        return tui_turn(cfg, name, int(generation), thread=data.get('session_id'),
                        transcript=data.get('transcript_path', ''), text=text,
                        inputs=claude_user_prompts(data.get('transcript_path', '')))
    # Which delivery this turn answers: the last user message in the transcript that points at an
    # .inbox/msg-*.md file. User messages are written when the turn starts, so they are present even
    # when the final assistant message has not been flushed yet.
    msg_id = ''
    try:  # scan the whole transcript: tool output after the pointer can be megabytes
        with open(data.get('transcript_path', ''), errors='replace') as f:
            for line in f:
                if '.inbox/msg-' in line and '"type":"user"' in line.replace(' ', ''):
                    m = re.search(r'\.inbox/(msg-[0-9]{8}-[0-9]{6}-[0-9a-f]{6})\.md', line)
                    if m:
                        msg_id = m.group(1)
    except OSError:
        pass
    r = connect(cfg)
    # No hook-side de-dup: the Stop payload carries no turn id and the transcript may lag the final
    # message. The daemon drops a turn for a claude worker that is already idle (see on_turn).
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('session_id'), 'transcript': data.get('transcript_path', ''),
                                'msg_id': msg_id, 'text': text, 'at': time.time()}, ensure_ascii=False))
    return True


# ---------------------------------------------------------------- trust gate
def validate_artifacts(artifacts, extended=False):
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError('artifacts must be a non-empty list')
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or 'checks' in item:
            raise ValueError('an artifact may only have path / min_chars / contains')
        path = Path(item['path'])
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError('artifact path must stay inside the workspace')
        minimum = item.get('min_chars', 1)
        if type(minimum) is not int or not 1 <= minimum <= 8_000_000:
            raise ValueError('invalid min_chars')
        contains = item.get('contains', [])
        if not isinstance(contains, list) or not all(isinstance(x, str) for x in contains):
            raise ValueError('contains must be a list of literal strings')
        if not extended:
            continue  # contract-only keys (not_contains / cmd_exit) stay ignored, as before
        banned = item.get('not_contains', [])
        if not isinstance(banned, list) or not all(isinstance(x, str) and x for x in banned):
            raise ValueError('not_contains must be a list of literal strings')
        argv = item.get('cmd_exit')
        if argv is not None and (not isinstance(argv, list) or not argv
                                 or not all(isinstance(x, str) and x and '\x00' not in x for x in argv)):
            raise ValueError('cmd_exit must be a non-empty argv list of strings (no shell)')
        timeout = item.get('timeout_s', CMD_TIMEOUT)
        if type(timeout) is not int or not 1 <= timeout <= CMD_TIMEOUT_MAX:
            raise ValueError('timeout_s must be 1-' + str(CMD_TIMEOUT_MAX) + ' seconds')


def run_check_command(item, root, path):
    """Contract tasks: run an artifact's cmd_exit argv (no shell) in the workspace with a hard timeout
    and write its combined output to the artifact path; the literal checks then read that output.
    Returns a failure text or None. The daemon is blocked meanwhile, hence the short cap."""
    timeout = item.get('timeout_s', CMD_TIMEOUT)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(item['cmd_exit'], cwd=root, capture_output=True, text=True, errors='replace',
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        path.write_text('timed out after ' + str(timeout) + ' s\n')
        return 'command timed out after ' + str(timeout) + ' s: ' + item['path']
    except OSError as exc:
        return 'command could not start (' + str(exc) + '): ' + item['path']
    path.write_text(((done.stdout or '') + (done.stderr or ''))[-2_000_000:])
    return None if done.returncode == 0 else 'command exited ' + str(done.returncode) + ': ' + item['path']


def check_artifacts(artifacts, root, extended=False, run_commands=False):
    """Literal file checks only (exists / size / contains); returns failures (empty = pass).
    ``extended`` (contract tasks) adds not_contains and cmd_exit; commands run only when
    ``run_commands`` (the COMPLETED gate), never from result.json / resume re-checks."""
    root = Path(root).resolve()
    failures = []
    for item in artifacts:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root):
            failures.append('outside workspace: ' + item['path'])
            continue
        if extended and run_commands and item.get('cmd_exit'):
            problem = run_check_command(item, root, path)
            if problem:
                failures.append(problem)
                continue
        if not path.is_file():
            failures.append('artifact not written: ' + item['path'])
        elif path.stat().st_size > 32_000_000:
            failures.append('artifact too large to check: ' + item['path'])
        else:
            text = path.read_text(errors='replace')
            banned = [x for x in item.get('not_contains', []) if x in text] if extended else []
            if len(text) < item.get('min_chars', 1) or not all(c in text for c in item.get('contains', [])):
                failures.append('artifact below requirements (length / required text): ' + item['path'])
            elif banned:
                failures.append('artifact contains forbidden text ' + json.dumps(banned, ensure_ascii=False)
                                + ': ' + item['path'])
    return failures


def validate_agent_registry_path(value):
    """Validate a task-provided registry path without shell or environment expansion."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError('agent_registry must be a non-empty directory path')
    try:
        path = Path(value.strip()).expanduser()
        if not path.is_dir():
            raise ValueError('agent_registry must exist and be a directory: ' + str(path))
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith('agent_registry '):
            raise
        raise ValueError('invalid agent_registry path: ' + str(exc)) from exc
    return path


def parse_decision(text, workers, others_active):
    decision = json.loads(text[text.index('{'):text.rindex('}') + 1])
    if not isinstance(decision, dict) or 'checks' in decision:
        raise ValueError('decision must be a JSON object and may not submit host commands (checks)')
    if decision.get('status') not in ('RUNNING', 'COMPLETED', 'BLOCKED'):
        raise ValueError('status must be RUNNING / COMPLETED / BLOCKED')
    completion = decision.get('completion')
    if isinstance(completion, bool) or not isinstance(completion, int) or not 0 <= completion <= 100:
        raise ValueError('completion must be an integer 0-100')
    dispatch = decision.setdefault('dispatch', [])
    if not isinstance(dispatch, list):
        raise ValueError('dispatch must be a list')
    for item in dispatch:
        if not isinstance(item, dict) or item.get('to') not in workers:
            raise ValueError('dispatch.to must be one of ' + ', '.join(workers))
        if not isinstance(item.get('instruction'), str) or len(item['instruction'].strip()) < 12:
            raise ValueError('instruction must state the action and the expected output (>= 12 chars)')
        if 'id' in item and (not isinstance(item['id'], str) or not re.fullmatch(contracts.M_RE, item['id'])):
            raise ValueError('every dispatch item needs an id (M<n>, e.g. M0)')
        if 'advise_note' in item and not isinstance(item['advise_note'], str):
            raise ValueError('dispatch.advise_note must be text')
    escalations = decision.setdefault('escalations', [])
    if not isinstance(escalations, list):
        raise ValueError('escalations must be a list')
    for item in escalations:
        if (not isinstance(item, dict) or not isinstance(item.get('task'), str)
                or item.get('decision') not in ESCALATION_DECISIONS):
            raise ValueError('escalations: every item needs task and decision approve / revise / reject')
        workers_n = item.get('workers', 1)
        if isinstance(workers_n, bool) or not isinstance(workers_n, int) or workers_n < 1:
            raise ValueError('escalations: workers must be a positive integer')
        if not isinstance(item.get('note', ''), str):
            raise ValueError('escalations: note must be text')
    if len({x['task'] for x in escalations}) != len(escalations):
        raise ValueError('escalations: one answer per task')
    if 'artifacts' in decision:
        validate_artifacts(decision['artifacts'])
    if decision['status'] == 'RUNNING' and not dispatch and not escalations and not others_active:
        raise ValueError('no worker is busy: RUNNING must dispatch at least one instruction')
    return decision


def validate_request(spec, workers):
    """A service request: {id, payload, reply: {required, allowed?}, to?, instruction?, timeout_s?}."""
    if not isinstance(spec, dict):
        raise ValueError('a request must be a JSON object')
    if not isinstance(spec.get('id'), str) or not re.fullmatch(REQ_ID_RE, spec['id']):
        raise ValueError('id must match ' + REQ_ID_RE)
    if 'payload' not in spec or len(json.dumps(spec['payload'], ensure_ascii=False)) > 200_000:
        raise ValueError('payload is required and must be at most 200000 chars as JSON')
    reply = spec.get('reply')
    if not isinstance(reply, dict) or not isinstance(reply.get('required'), list) or not reply['required'] \
            or not all(isinstance(k, str) and k for k in reply['required']):
        raise ValueError('reply.required must be a non-empty list of key names')
    allowed = reply.get('allowed')
    if allowed is not None and (not isinstance(allowed, list) or not all(isinstance(k, str) for k in allowed)
                                or not set(reply['required']) <= set(allowed)):
        raise ValueError('reply.allowed must be a list of key names containing every required key')
    if spec.get('to') is not None and spec['to'] not in workers:
        raise ValueError('to must be one of ' + ', '.join(workers))
    if spec.get('instruction') is not None and not isinstance(spec['instruction'], str):
        raise ValueError('instruction must be a string')
    timeout = spec.get('timeout_s')
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not 10 <= timeout <= 86400):
        raise ValueError('timeout_s must be a number of seconds between 10 and 86400')
    if 'replay' in spec and type(spec['replay']) is not bool:
        raise ValueError('replay must be true or false (false: never re-run it on start --resume)')


def parse_reply(text, reply):
    """A worker's answer to a request: one JSON object with the required keys (and only allowed ones)."""
    try:
        obj = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except ValueError:
        raise ValueError('no JSON object found')
    if not isinstance(obj, dict):
        raise ValueError('the reply must be a JSON object')
    missing = [k for k in reply['required'] if k not in obj]
    if missing:
        raise ValueError('missing keys: ' + ', '.join(missing))
    if reply.get('allowed') is not None:
        extra = sorted(set(obj) - set(reply['allowed']))
        if extra:
            raise ValueError('keys not allowed: ' + ', '.join(extra))
    return obj


def parse_service_decision(text, workers):
    decision = json.loads(text[text.index('{'):text.rindex('}') + 1])
    if not isinstance(decision, dict) or 'checks' in decision:
        raise ValueError('decision must be a JSON object and may not submit host commands (checks)')
    if decision.setdefault('status', 'RUNNING') not in ('RUNNING', 'BLOCKED'):
        raise ValueError('status must be RUNNING / BLOCKED')
    for field in ('dispatch', 'skip'):
        if not isinstance(decision.setdefault(field, []), list):
            raise ValueError(field + ' must be a list')
    for item in decision['dispatch']:
        if not isinstance(item, dict) or item.get('to') not in workers:
            raise ValueError('dispatch.to must be one of ' + ', '.join(workers))
        if not isinstance(item.get('request_id'), str):
            raise ValueError('every dispatch item needs the request_id it answers')
        if not isinstance(item.get('instruction'), str) or len(item['instruction'].strip()) < 12:
            raise ValueError('instruction must state the action (>= 12 chars)')
    for item in decision['skip']:
        if not isinstance(item, dict) or not isinstance(item.get('request_id'), str) \
                or not isinstance(item.get('reason'), str):
            raise ValueError('every skip item needs request_id and reason')
    return decision


# ------------------------------------------------------------ codex rollouts
def rollouts(thread, sessions):
    base = Path(sessions)
    return sorted(base.glob('*/*/*/*' + thread + '*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)


def _meta(path):
    with path.open() as f:
        return json.loads(f.readline()).get('payload', {})


def _is_subagent(meta):
    return isinstance(meta.get('source'), dict) and 'subagent' in meta['source']


def _tail(path, size):
    with path.open('rb') as f:
        f.seek(max(0, path.stat().st_size - size))
        return f.read().decode('utf-8', 'replace').splitlines()


def completed_turn(cwd, data, sessions):
    """Codex also notifies for title-generation calls; require a real task_complete."""
    thread = data.get('thread-id') or data.get('thread_id')
    if not isinstance(thread, str) or not re.fullmatch(THREAD_RE, thread):
        return False
    text = (data.get('last-assistant-message') or data.get('last_assistant_message') or '').strip()
    turn = data.get('turn-id') or data.get('turn_id')
    for path in rollouts(thread, sessions):  # newest chunk of a resumed thread wins
        try:
            meta = _meta(path)
            # Codex records the real path (/tmp is /private/tmp on macOS); compare resolved paths.
            if _is_subagent(meta) or os.path.realpath(meta.get('cwd') or '') != os.path.realpath(cwd):
                continue
            for line in reversed(_tail(path, 262144)):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                payload = row.get('payload', {})
                if row.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                    return False
                if row.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
                    return ((payload.get('last_agent_message') or '').strip() == text
                            and (not turn or payload.get('turn_id') == turn))
            return False
        except (OSError, ValueError):
            continue
    return False


def active_children(thread, sessions):
    """Native Codex subagents spawned by this thread that have not finished yet."""
    base = Path(sessions)
    roots = rollouts(thread, base) if thread else []
    if not roots:
        return []
    cutoff = getattr(roots[-1].stat(), 'st_birthtime', 0)
    children = {}
    for path in base.glob('*/*/*/*.jsonl'):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            meta = _meta(path)
            parent = meta.get('source', {}).get('subagent', {}).get('thread_spawn', {}).get('parent_thread_id')
            if parent:
                children.setdefault(parent, []).append((meta.get('id'), path))
        except (OSError, ValueError, AttributeError):
            continue
    queue, busy, seen = [thread], [], set()
    while queue:
        parent = queue.pop()
        if parent in seen:
            continue
        seen.add(parent)
        for child, path in children.get(parent, []):
            queue.append(child)
            status = None
            for line in reversed(_tail(path, 65536)):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get('payload', {}).get('type')
                if row.get('type') == 'event_msg' and kind in ('task_started', 'task_complete', 'turn_aborted'):
                    status = kind
                    break
            if status not in ('task_complete', 'turn_aborted'):
                busy.append(child)
    return busy


# ------------------------------------------------------------ actor side
def notify(cfg_path, name, generation, payload, sessions=None):
    """Codex notify hook. The generation is baked into argv at spawn, so a late callback
    from a retired session keeps its old generation and is dropped as stale."""
    data = json.loads(payload)
    if data.get('type') != 'agent-turn-complete':
        return False
    cfg = load(cfg_path)
    sessions = sessions or sessions_dir(cfg)
    for _ in range(10):
        if completed_turn(agent_cwd(cfg, name), data, sessions):
            break
        time.sleep(.2)
    else:
        return False  # title/auxiliary call, not a turn end
    r = connect(cfg)
    digest = hashlib.sha256((name + str(generation) + payload).encode()).hexdigest()
    if not r.set('fx:seen:' + digest, '1', nx=True, ex=86400):
        return False
    if cfg.get('transport_version') == 2:
        r.close(); r.connection_pool.disconnect()
        inputs = data.get('input-messages') or data.get('input_messages') or []
        cfg, name, generation = subfleet.delegated_route(cfg, name, int(generation))
        return tui_turn(cfg, name, int(generation), thread=data.get('thread-id') or data.get('thread_id'),
                        transcript='', text=data.get('last-assistant-message') or data.get('last_assistant_message') or '',
                        inputs=inputs if isinstance(inputs, list) else [])
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('thread-id') or data.get('thread_id'),
                                'text': data.get('last-assistant-message') or data.get('last_assistant_message') or '',
                                'at': time.time()}, ensure_ascii=False))
    return True


def claude_user_prompts(path, count=3):
    """Last real user prompts of a Claude transcript (not tool results, meta or sidechains).

    Scan the whole file like the v1 pointer scan: tool output after the prompt can be
    megabytes, so a tail read would lose the prompt and silently drop the settlement.
    """
    out = collections.deque(maxlen=count)
    try:
        with open(path, errors='replace') as f:
            for line in f:
                if '"user"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('type') != 'user' or row.get('isSidechain') or row.get('isMeta'):
                    continue
                content = (row.get('message') or {}).get('content')
                if isinstance(content, list):
                    content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
                if isinstance(content, str) and content.strip():
                    out.append(content)
    except (OSError, TypeError):
        pass
    return list(out)


def tui_turn(cfg, name, generation, *, thread, transcript, text, inputs, wait=None, settle_wait=30):
    """v2 turn end of an official interactive CLI (Codex notify / Claude Stop hook).

    First READY turn: bind the session, register the slot and report startup. Later turns:
    settle only the delivery this generation received, only if the typed delivery text
    started the turn. The daemon validates identity, lease, fence and schema as for Pi.
    """
    if name not in ['supervisor'] + cfg['workers'] or not runtime_v2.tui_slot(cfg, name):
        raise ValueError('turn hook must name an official-CLI v2 agent in this fleet')
    # Events are durable either way; waiting long only keeps this generation's startup
    # sequence (bind -> register -> READY) intact while the daemon is still spawning tabs.
    wait = float(cfg.get('profile_ready_timeout') or 180) if wait is None else wait
    r = connect(cfg)
    bus = runtime_v2.bus_for(cfg, r)
    try:
        info = r.hgetall(key('agent', name))
        base = dict(agent=name, generation=generation, run_id=bus.run_id, thread=thread, transcript=transcript)

        def call(kind, **fields):
            correlation = 'h-' + uuid.uuid4().hex
            runtime_v2.post_event(bus, dict(base, kind=kind, request_id=correlation, **fields), correlation)
            # Events are durable; a long wait only matters for the READY sequence. A turn
            # end must not freeze a blocking Claude Stop hook for minutes.
            return runtime_v2.wait_reply(bus, correlation, timeout=wait if kind != 'settled' else settle_wait)

        def skipped(status, reason):
            # Visible in control/logs: an ignored turn otherwise leaves no trace.
            r.xadd(bus.audit_key, {'event': 'turn.' + status, 'slot': name, 'run_id': bus.run_id,
                                   'generation': str(generation), 'error': reason, 'level': 'warn',
                                   'at': str(int(time.time() * 1000))})
            return {'status': status, 'reason': reason}
        if int(info.get('generation', -1)) != generation or info.get('state') == 'dead':
            return skipped('stale', 'hook generation is not the current runtime')
        if info.get('state') == 'starting':
            if info.get('thread') != thread:
                answer = call('bridge_session')
                if answer.get('status') != 'registered':
                    return answer
            bus.register_slot(name, generation, thread)
            return call('startup', text=text, failed=False, aborted=False)
        def ignored(reason):
            # §21: a turn no delivery started (a peer SendMessage woke it, or the operator typed):
            # the daemon records it as unsolicited and forwards its Peer notices / question lines.
            if name != 'supervisor' and (text or '').strip():
                runtime_v2.post_event(bus, dict(base, kind='unsolicited', text=text[-8000:]))
            return skipped('ignored', reason)
        lease = r.hgetall(bus.lease_key(name))
        if lease.get('status') != 'received' or not lease.get('message_id'):
            return ignored('no received delivery (manual or duplicate turn)')
        msg = bus.message(lease['message_id'])
        if msg.get('generation') != generation or msg.get('session_id') != thread:
            return skipped('stale', 'delivery belongs to another generation/session')
        if not runtime_v2.turn_answers(msg['body']['text'], inputs):
            return ignored('turn was not started by the typed delivery')
        return call('settled', delivery={k: msg[k] for k in runtime_v2.IDENTITY},
                    candidates=runtime_v2.tui_candidates(name, msg['body'].get('purpose'), text or ''),
                    failed=False, aborted=False, unfinished=False)
    finally:
        r.close(); r.connection_pool.disconnect()


def agent_profile(cfg, name):
    """Codex profile for one agent: the role-specific one if set, else the shared --profile."""
    role = 'supervisor_profile' if name == 'supervisor' else 'worker_profile'
    return cfg.get(role) or cfg.get('profile') or DEFAULTS['profile']


def actor_argv(cfg_path, cfg, name, generation, prompt):
    callback = [cfg['python'], str(Path(__file__).resolve()), 'notify', str(cfg_path), name, str(generation)]
    argv = [cfg.get('codex') or 'codex', '--profile', agent_profile(cfg, name),
            '--no-alt-screen',
            '--sandbox', 'workspace-write', '-c', 'sandbox_workspace_write.network_access=true',
            '-c', 'approval_policy="never"',
            '-c', 'check_for_update_on_startup=false',  # the update dialog would eat the first paste
            # The operator's skills (~/.agents/skills) are listed to every session regardless of CODEX_HOME;
            # a local model took that list as its task and ran a skill instead of replying READY.
            '-c', 'skills.include_instructions=false',
            '-c', 'notify=' + json.dumps(callback),
            '-c', 'developer_instructions=' + json.dumps(
                'Your role, rules and reply format are in AGENTS.md in the current directory. Follow it.')]
    if cfg.get('codex_model'):
        argv += ['--model', cfg['codex_model']]
    if cfg.get('compact_limit'):
        # The daemon rotates sessions at handoff_ratio before each delivery; Codex's own
        # compaction is only a safety net for a single runaway turn.
        argv += ['-c', 'model_auto_compact_token_limit=' + str(int(cfg['compact_limit']))]
    return argv + [prompt]


def claude_argv(cfg_path, cfg, name, generation, prompt):
    cwd = agent_cwd(cfg, name)
    hook = shlex.join([cfg['python'], str(Path(__file__).resolve()), 'claude-stop', str(cfg_path), name, str(generation)])
    settings = cwd / ('.fleet-settings-g' + str(generation) + '.json')
    # v2: the READY hook waits for the daemon, which may still be spawning other tabs.
    timeout = 30 if cfg.get('transport_version') != 2 else int(float(cfg.get('profile_ready_timeout') or 180)) + 30
    advised = cfg.get('advisor') is True and name != 'supervisor'
    data = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': hook, 'timeout': timeout}]}]}}
    if advised:
        data['autoMemoryEnabled'] = False  # fleet workers have no memory layer (advisor plan §2)
    settings.write_text(json.dumps(data))
    argv = [cfg.get('claude') or 'claude', '--dangerously-skip-permissions', '--model', cfg.get('worker_model') or 'sonnet',
            '--settings', str(settings), '--add-dir', str(Path(cfg['run']) / 'workspace')]
    if name != 'supervisor':  # stable per slot across generations (peer messages address it)
        argv += ['--name', claude_session_name(cfg, name)]
    if advised:
        # Only cwd/.claude (the linked domains) and cwd/mcp.json (the full kit set): no user-level
        # skills / agents / MCP servers. The Stop hook rides on --settings and still fires.
        argv += ['--setting-sources', 'project', '--strict-mcp-config']
        if cfg.get('worker_effort'):
            argv += ['--effort', cfg['worker_effort']]
    if (cwd / 'mcp.json').exists():
        argv += ['--mcp-config', str(cwd / 'mcp.json')]
    return argv + ['--', prompt]  # --add-dir / --mcp-config are variadic: end options first


def claude_session_name(cfg, name):
    """f<main run token>[-<sub-fleet task id>]-<worker slot>, e.g. f1a2b3c4d-M0-worker-2."""
    return 'f' + subfleet.token(cfg) + ('-' + cfg['task_id'] if cfg.get('task_id') else '') + '-' + name


def pi_event(cfg_path, name, generation, raw):
    """Pi's bundled extension sends session metadata and final settlement over stdin."""
    cfg = load(cfg_path)
    if cfg.get('transport_version') == 2:
        raise ValueError('v2 Pi sessions must use the persistent bound bridge')
    if name not in ['supervisor'] + cfg['workers'] or not is_pi(cfg, name):
        raise ValueError('pi callback must name a pi agent in this fleet')
    if len(raw) > 1000000:
        raise ValueError('pi event too large')
    ev = pi_adapter.event(cfg, name, int(generation), json.loads(raw))
    ev['at'] = time.time()
    if ev['kind'] == 'operator_input':
        print(json.dumps(request(cfg, ev), ensure_ascii=False))
    else:
        connect(cfg).rpush(EVENTS, json.dumps(ev, ensure_ascii=False))


def actor(cfg_path, name, prompt, generation=None):
    cfg = load(cfg_path)
    runtime_v2.validate_config(cfg)
    r = connect(cfg)
    info = r.hgetall(key('agent', name))
    # Every daemon launch pins this value in argv, never adopts a newer generation.
    generation = int(info.get('generation') or 0) if generation is None else generation
    deadline = time.monotonic() + 60
    while True:
        if generation != int(info.get('generation', -1)) or info.get('state') == 'dead':
            raise RuntimeError('actor generation was retired or blocked before launch')
        if info.get('launch_ready', '1') == '1':
            break
        if info.get('advice_hold') == '1':
            deadline = time.monotonic() + 60  # held for the first advisor sheet: wait for it or the run's end
        if time.monotonic() >= deadline:
            raise RuntimeError('profile launch gate timed out; old runtime retirement unconfirmed')
        time.sleep(.1)
        info = r.hgetall(key('agent', name))
    cfg = load(cfg_path)  # an advisor release may have rewritten this generation's snapshot
    os.chdir(agent_cwd(cfg, name))
    if is_pi(cfg, name):
        os.environ.update(pi_adapter.environment(cfg_path, cfg, name, generation))
        argv = pi_adapter.argv(cfg, name, generation, prompt, agent_cwd(cfg, name), label=info['label'])
    elif is_claude(cfg, name):
        os.environ.pop('CLAUDECODE', None)
        argv = claude_argv(cfg_path, cfg, name, generation, prompt)
    else:
        argv = actor_argv(cfg_path, cfg, name, generation, prompt)
    os.execvp(argv[0], argv)


# ------------------------------------------------------------ daemon
class Fleet:
    def __init__(self, cfg_path, r, mgr, sessions=None):
        self.path = Path(cfg_path).resolve()
        self.cfg = dict(DEFAULTS, **load(self.path))
        self.transport_version = runtime_v2.validate_config(self.cfg)
        profile_runtime.validate_live_setting(self.cfg['apply_profiles_live'],
                                              allowed=self.cfg['allow_profile_live_switch'])
        timeout = self.cfg['profile_ready_timeout']
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('profile_ready_timeout must be a finite positive number')
        bad = roster.handoff_errors({'handoff': self.cfg.get('handoff')})  # static config table (§22)
        if bad:
            raise ValueError('config ' + '; '.join(bad))
        self.run_dir = Path(self.cfg['run'])
        self.r, self.mgr = r, mgr
        self.sessions = Path(sessions) if sessions else sessions_dir(self.cfg)
        self.workers = list(self.cfg['workers'])
        self.names = ['supervisor'] + self.workers
        # Hot roster (start --roster): workers missing from `desired` are retiring. They finish
        # queued work, then retire; until then they still count as busy but take no new work.
        self.desired = list(self.workers)
        self.roster_stamp = self.roster_digest = None
        self.handoff_table, self.worker_handoff = {}, {}  # roster `handoff:` / per-worker overrides (§22)
        self.service = self.cfg.get('mode') == 'service'
        self.stopping = False
        self.next_liveness = 0
        self.last_chrome = 0
        self.explicit_stop = False
        self.resume_requested = False  # set by `daemon --resume` (operator ran start --resume)
        self.v2 = runtime_v2.RuntimeV2(self) if self.transport_version == 2 else None
        self.kit, self.advisor_runner = None, None  # advisor runs only; tests inject advisor_runner
        if 'advisor' in self.cfg and type(self.cfg['advisor']) is not bool:
            raise ValueError('advisor must be a boolean')
        if self.cfg.get('advisor') is True and self.cfg.get('worker_kit'):
            self.kit = kitmod.Kit(self.cfg['worker_kit'])  # KitError (ValueError): refuse the whole kit

    # -- small helpers
    def agent(self, name):
        return self.r.hgetall(key('agent', name))

    def runtime_cfg(self, name):
        launch = json.loads(self.agent(name).get('runtime_launch') or '{}')
        cfg = dict(self.cfg, **launch.get('overrides', {}))
        if (launch.get('extras') or {}).get('effort'):
            cfg['worker_effort'] = launch['extras']['effort']  # advisor launch field (Claude --effort)
        return cfg

    def setf(self, name, **fields):
        self.r.hset(key('agent', name), mapping={k: str(v) for k, v in fields.items()})

    def task(self):
        return self.r.hgetall(TASK)

    # Each handoff successor is named slot.generation (worker-1 -> worker-1.1 -> worker-1.2);
    # the slot (redis key, cwd, notify argv) stays fixed so declared artifact paths survive.
    def label(self, slot):
        return self.agent(slot).get('label') or slot

    def labels(self):
        return [self.label(w) for w in self.active_workers()]

    def active_workers(self):
        return [w for w in self.workers if w in self.desired]

    def dispatch_targets(self):
        """Live labels plus older generation names, only for workers not retiring."""
        active = self.active_workers()
        return self.labels() + [n for n, slot in self.r.hgetall(NAMES).items() if slot in active]

    def resolve(self, name):
        return self.r.hget(NAMES, name) or name

    def running(self):
        return self.service or self.task().get('status') == 'running'

    def paused(self):
        return self.r.hget(SERVICE, 'paused') == '1'

    def pause(self, reason):
        self.r.hset(SERVICE, mapping={'paused': 1, 'reason': reason, 'at': time.time()})
        self.log('service.paused', reason=reason)

    def resume(self):
        self.r.hset(SERVICE, mapping={'paused': 0, 'reason': '', 'at': time.time()})
        self.log('service.resumed')

    def log(self, kind, **data):
        if self.v2:
            self.v2.log(kind, data)
            return
        self.r.rpush(LOG, json.dumps(dict(time=time.time(), kind=kind, **data), ensure_ascii=False))
        self.r.ltrim(LOG, -2000, -1)
        print(time.strftime('%H:%M:%S'), kind, json.dumps(data, ensure_ascii=False)[:180], flush=True)

    def queue(self, name, text):
        if name == 'supervisor' and self.r.hget(ROSTER, 'notify') == '1':
            # Appended, never prepended: the queue parses request/task headers at the start.
            self.r.hset(ROSTER, mapping={'notify': 0})
            text += '\n[Roster change] Workers you can dispatch to now: ' + ', '.join(self.labels())
        if name == 'supervisor':
            text += peer.drain(self)  # [peer CC] / unsolicited / violation notes (docs/design.md §21)
            if self.cfg.get('advisor') is True and self.r.llen(ADVICE_NOTES):
                # Appended, never sent alone: a lone note forces a no-dispatch RUNNING (same as §16 / §21).
                text += '\n' + '\n'.join(self.r.lrange(ADVICE_NOTES, 0, -1))
                self.r.delete(ADVICE_NOTES)
        if self.v2:
            return self.v2.queue(name, text)
        self.r.rpush(key('inbox', name), text)

    def inbox_depth(self, name):
        return self.v2.depth(name) if self.v2 else self.r.llen(key('inbox', name))

    def prepare_worker_profile(self, slot, instruction):
        """Authorize intent without spawning; recheck immediately before delivery."""
        task = self.task()
        try:
            registry = AgentRegistry(task.get('agent_registry') or None) if task.get('profile_assignments') else None
            return profile_runtime.prepare_profile(
                slot, instruction, task, self.agent(slot), registry,
                allow_profile_live_switch=self.cfg['allow_profile_live_switch'])
        except (OSError, TypeError, ValueError) as exc:
            blocked = profile_runtime.ProfileApplication(state='blocked', reason=str(exc))
            self.record_worker_profile(slot, blocked)
            self.log('profile.blocked', agent=slot, reason=str(exc))
            raise profile_runtime.ProfileRuntimeError(str(exc)) from exc

    def record_worker_profile(self, slot, application):
        self.setf(slot, **application.metadata(), profile_task=self.task().get('id', ''))

    def block_profile(self, name, reason, *, dead=False):
        fields = dict(profile_state='blocked', profile_reason=reason)
        if dead:
            fields.update(state='dead', launch_ready=0, runtime_ready=0, current_profile='')
        self.setf(name, **fields)
        self.log('profile.blocked', agent=name, reason=reason)
        self.finish('blocked', name + ': ' + reason)

    def preflight_profile(self, launch):
        """Check dependencies before retiring anything. Never select a fallback CLI."""
        cfg = dict(self.cfg, **launch['overrides'])
        runtime_v2.validate_config(cfg)  # reject unsupported v2 CLI before retirement
        cli = cfg['worker_cli']
        binary = cfg.get(cli) or cli
        if not shutil.which(binary):
            raise ValueError('profile CLI binary not found: ' + binary)
        if cli == 'pi':
            if not cfg.get('allow_unsandboxed_pi'):
                raise ValueError('pi profile requires --allow-unsandboxed-pi')
            version = subprocess.run([binary, '--version'], capture_output=True, text=True,
                                     timeout=15, check=True).stdout.strip()
            match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
            if not match or tuple(map(int, match.groups())) < (1, 1, 0):
                raise ValueError('pi >= 1.1.0 required; found: ' + version)
            if not self.cfg.get('pi_home'):
                home = pi_adapter.prepare_home(self.run_dir / 'pi-home',
                                              cfg.get('pi_agent_dir') or str(Path.home() / '.pi/agent'))
                self.cfg['pi_home'] = str(home)
            if not Path(self.cfg['pi_home']).is_dir():
                raise ValueError('configured isolated pi_home does not exist')
        elif cli == 'codex':
            prepare_codex_home(codex_home(cfg), agent_profile(cfg, 'worker'))

    def switch_profile(self, name, launch):
        """Same slot, successor generation, gated CLI exec until old tab is retired."""
        info = self.agent(name)
        if name not in self.workers or info.get('state') != 'idle':
            raise ValueError('profile switch requires an idle worker slot')
        if info.get('idle_confirmed') == '0':
            raise ValueError('worker idle state is unconfirmed after an abort; restart this dev slot before switching')
        # start --advisor authorizes advisor-driven respawns; every other safety step below is unchanged.
        if (self.cfg.get('advisor') is not True
                and not profile_runtime.task_live_setting(self.task(), self.cfg['allow_profile_live_switch'])):
            raise ValueError('profile switch requires task apply_profiles_live=true')
        if agent_cli(self.runtime_cfg(name), name) == 'codex' and active_children(info.get('thread'), self.sessions):
            self.setf(name, profile_state='pending', profile_reason='waiting for native subagents to finish')
            return
        self.preflight_profile(launch)
        generation, old, prev = int(info['generation']), info['pane'], self.label(name)
        if not old or self.mgr.get_pane_status(old) is None:
            raise ValueError('old runtime tab is missing; cannot safely replace it')
        label = name + '.' + str(generation + 1)
        note = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-profile.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text('Idle profile switch: ' + prev + ' -> ' + label + '\n'
                        'Target profile: ' + launch['profile_id'] + '\n'
                        'The queued instruction is retained by the daemon. No task work before READY.\n')
        snapshot = note.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        history.record(self.run_dir, slot=name, label=prev, generation=generation,
                       handoff=str(note), snapshot=str(snapshot), source='profile-switch', timed_out=False)
        # Fence old callbacks before touching shared role files or creating a successor.
        self.setf(name, state='starting', generation=generation + 1, label=label, prev_label=prev,
                  runtime_launch=profile_launch.encode_launch(launch), current_profile='', runtime_ready=0,
                  runtime_profile='', profile_state='switching', launch_ready=0, retired_pane=old)
        self.r.hset(NAMES, mapping={label: name})
        new = None
        try:
            new = self.spawn(name, 'You are fleet agent ' + label + '. Read AGENTS.md first. '
                             'This is an idle runtime profile switch. Do no task work. Reply exactly READY.',
                             launch_held=True)
            if not self.mgr.tab_into(new, old):
                raise RuntimeError('could not place successor in the worker pane')
            if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                raise RuntimeError('could not confirm retirement of old runtime ' + prev)
            self.setf(name, retired_pane='', launch_ready=1)
            self.refresh_history()
            self.log('profile.switch', agent=name, label=label, generation=generation + 1,
                     profile=launch['profile_id'], cli=launch['overrides']['worker_cli'])
        except (Exception, SystemExit) as exc:
            # Revoke launch before cleanup; even a late READY cannot revive this generation.
            self.setf(name, launch_ready=0, state='dead')
            new = new or self.agent(name).get('pane')
            if new == old:
                new = None
            if new:
                try:
                    if not self.mgr.kill_pane(new):
                        self.log('profile.cleanup_failed', agent=name, pane=new)
                except Exception as cleanup:
                    self.log('profile.cleanup_failed', agent=name, pane=new, reason=str(cleanup))
            self.block_profile(name, 'profile switch failed: ' + str(exc), dead=True)

    def workspace(self):
        return self.run_dir / 'workspace'

    def fleet_cmd(self):
        return shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'chrome', '--config', str(self.path)])

    def role_scope(self, name, cfg, launch):
        """The AGENTS.md text of one agent (also written by a sub-fleet over its adopted pane)."""
        role = 'supervisor' if name == 'supervisor' else 'worker'
        scope = (HERE / 'agents' / (('service_' if self.service else '') + role
                                  + ('_v2' if self.v2 and is_pi(cfg, name) else '') + '.md')).read_text()
        if launch.get('role_text') and launch['role_text'].strip() != scope.strip():
            scope += '\n## Assigned profile role\n\n' + launch['role_text']
        roles = self.cfg.get('worker_roles') or {}
        extra = (Path(roles[name]) if name in roles else
                 Path(self.cfg['roles']).expanduser() / (name + '.md') if self.cfg.get('roles') else None)
        if extra and extra.is_file():  # this agent's own prompt (one per worker in service mode)
            scope += '\n## 4. Your role (' + name + ')\n\n' + extra.read_text()
        if self.contract_phase():  # successors / roster-added workers inside a contract task
            scope += '\n' + self.contract_doc(role, name)
        scope = scope.replace('{{WORKSPACE}}', str(self.workspace())).replace('{{FLEET_CHROME}}', self.fleet_cmd())
        sandbox_notice = ('You run inside a sandbox. Driving the user\'s browser directly (AppleScript, GUI automation) '
                          'fails there with an error. That error means the sandbox stopped you, not that the browser '
                          'is broken. Browser actions go only through the broker command below, if enabled.')
        scope = scope.replace('{{SANDBOX_NOTICE}}', sandbox_notice if agent_cli(cfg, name) == 'codex'
                              else pi_adapter.UNSANDBOXED_SCOPE)
        if is_pi(cfg, name):
            scope += '\n## Runtime permissions\n\n' + pi_adapter.UNSANDBOXED_SCOPE + '\n'
        return scope

    # -- actors
    def spawn(self, name, prompt, *, launch_held=False, advice_hold=False):
        cfg = self.runtime_cfg(name)
        info = self.agent(name)
        generation = int(info.get('generation', 0))
        launch = json.loads(info.get('runtime_launch') or '{}')
        cfg_path = self.path
        if launch or advice_hold:  # a held first spawn gets its model / effort written here on release
            cfg_path = self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json')
            save(cfg_path, cfg)  # generation-specific callback config; never edit old snapshots
        cwd = agent_cwd(self.cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        role = 'supervisor' if name == 'supervisor' else 'worker'
        scope = self.role_scope(name, cfg, launch)
        (cwd / 'AGENTS.md').write_text(scope)
        advised = self.cfg.get('advisor') is True and name != 'supervisor'
        if is_claude(cfg, name) and advised:
            self.install_advised_kit(name, cwd, scope)  # CLAUDE.md = scope + core + domain rules
            # CLAUDE.md already carries the scope: do not make Claude read the same text twice.
            prompt = prompt.replace(' Read AGENTS.md in the current directory first.', '').replace(' Read AGENTS.md first.', '')
            trust_claude_dir(cwd)
        elif is_claude(cfg, name):
            (cwd / 'CLAUDE.md').write_text(scope)  # Claude Code reads CLAUDE.md, not AGENTS.md
            install_kit(self.cfg.get('worker_kit'), cwd)
            trust_claude_dir(cwd)
        elif not is_pi(cfg, name):
            trust_agent_dir(codex_home(self.cfg), cwd)
        self.setf(name, role=role, generation=generation, state='starting', thread='', pane='',
                  transcript='', ctx_used=0, ctx_window=0, msg_id='', msg_done='',
                  busy_since=time.time(), delivered=0, retry_at=0, last_msg='', empty_tries=0,
                  launch_ready=int(not launch_held), runtime_ready=0, current_profile='', idle_confirmed=0)
        if self.v2:
            self.setf(name, bridge_available=0, startup_deadline=time.time() + float(self.cfg['profile_ready_timeout']))
        if advised:
            self.setf(name, advice_hold=int(advice_hold))
        if launch:
            self.setf(name, profile_state='switching', runtime_profile=launch['profile_id'],
                      profile_ready_deadline=time.time() + float(self.cfg['profile_ready_timeout']))
        prompt_file = self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.md')
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt)
        result = self.mgr.spawn_interactive_cli(
            window_name='fx-' + self.cfg.get('tab_prefix', '') + self.label(name), prompt_file=str(prompt_file), start_directory=str(cwd),
            cli_command=shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'actor', str(cfg_path), name,
                                    '--generation', str(generation)]),
            output_file=str(self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.terminal.log')),
            auto_close=False, metadata={'type': role, 'cli_type': agent_cli(cfg, name),
                      'profile': (cfg.get('pi_model') if is_pi(cfg, name) else
                                  cfg.get('worker_model') if is_claude(cfg, name) else agent_profile(cfg, name))},
            env_vars=dict({'PATH': os.environ.get('PATH', '')}, **(
                pi_adapter.environment(cfg_path, cfg, name, generation) if is_pi(cfg, name)
                else {'CODEX_HOME': str(codex_home(self.cfg))})))
        if not result.success:
            if result.pane_id:
                self.setf(name, pane=result.pane_id)
            raise RuntimeError('spawn failed: ' + name + ': ' + str(result.error))
        self.setf(name, pane=result.pane_id)
        self.log('spawn', agent=name, generation=generation, pane=result.pane_id)
        return result.pane_id

    def init_worker(self, name):
        self.setf(name, label=name, current_profile='', runtime_profile='', advisory_profile='',
                  runtime_ready=0, runtime_launch='', profile_task='',
                  **profile_runtime.ProfileApplication().metadata())
        self.r.hset(NAMES, mapping={name: name})

    def spawn_ready(self, name):
        # v2 + advisor: a Claude worker waits behind the launch gate for its first sheet (no default
        # generation that the first dispatch would have to replace).
        hold = bool(self.v2) and self.cfg.get('advisor') is True and name != 'supervisor' and is_claude(self.runtime_cfg(name), name)
        return self.spawn(name, 'You are fleet agent ' + name + '. Read AGENTS.md in the current directory first. '
                                'There is no task yet: your final reply must be exactly READY.',
                          launch_held=hold, advice_hold=hold)

    def start_all(self):
        if self.cfg.get('adopt'):
            return subfleet.start_adopted(self)  # sub-fleet: the delegated worker pane is the supervisor
        self.setf('supervisor', label=SUPERVISOR_LABEL)
        for name in self.workers:
            self.init_worker(name)
        for name in self.names:
            self.spawn_ready(name)
        history.write_page(self.run_dir, self.history_slots())
        # One page: supervisor above history/control on the left, worker tabs on the right.
        self.history_pane = self.mgr.arrange(self.agent('supervisor')['pane'],
                                             [self.agent(w)['pane'] for w in self.workers],
                                             history.page_url(self.run_dir))
        self.r.hset(SERVICE, mapping={'history_pane': self.history_pane or ''})

    def history_slots(self):
        # Workers removed from the roster keep their history (they have entries but no slot).
        retired = [s for s in dict.fromkeys(e.get('slot') for e in history.entries(self.run_dir))
                   if s and s not in self.names]
        return [(n, self.label(n), SUPERVISOR_LABEL if n == 'supervisor' else n) for n in self.names + retired]

    # -- events
    def handle(self, ev, *, _v2_internal=False):
        if self.v2 and not _v2_internal:
            return runtime_v2.post_event(self.v2.bus, ev)
        kind = ev.get('kind')
        if kind == 'task':
            try:
                answer = {'status': 'accepted', 'task_id': self.submit(ev.get('spec'))}
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'request':
            try:
                answer = {'status': 'accepted', 'id': self.request(ev.get('spec'))}
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'operator_input':
            try:
                info = self.agent('supervisor')
                if (ev.get('agent') != 'supervisor' or not is_pi(self.cfg, 'supervisor')
                        or ev.get('generation') != int(info.get('generation', -1))
                        or ev.get('thread') != info.get('thread')
                        or info.get('state') not in ('idle', 'busy')):
                    raise ValueError('supervisor is not ready or this input belongs to an old session')
                if not isinstance(ev.get('text'), str) or not 1 <= len(ev['text'].strip()) <= 32000:
                    raise ValueError('operator input must be 1-32000 chars of text')
                if self.service:
                    rid = self.request({'id': 'ui-' + uuid.uuid4().hex,
                                        'payload': {'message': ev['text']},
                                        'instruction': 'The operator typed the payload message in your terminal. '
                                                       'Route it to a worker; do not invent request IDs. '
                                                       'The worker puts its answer text in the reply field.',
                                        'reply': {'required': ['reply'], 'allowed': ['reply']}})
                    self.r.hset(REQ + rid, mapping={'operator': '1'})
                    answer = {'status': 'accepted', 'id': rid}
                elif self.running():
                    self.queue('supervisor', '[Operator instruction] ' + ev['text']
                               + '\nContinue the current task using the AGENTS.md decision JSON.')
                    answer = {'status': 'accepted', 'task_id': self.task()['id']}
                else:
                    answer = {'status': 'accepted', 'task_id': self.submit({'goal': ev['text']})}
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind in ('pause', 'resume'):
            if kind == 'pause':
                self.pause(ev.get('reason') or 'paused by operator')
            else:
                self.resume()
            self.r.set('fx:reply:' + ev['request_id'], json.dumps({'status': 'accepted'}), ex=86400)
        elif kind == 'stop':
            if not self.service and self.running():
                self.finish('cancelled', 'stopped by operator')
            self.stopping = True
            self.r.set('fx:reply:' + ev['request_id'], json.dumps({'status': 'accepted'}), ex=86400)
        elif kind == 'chrome':
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(self.chrome(ev.get('argv')), ensure_ascii=False), ex=3600)
        elif kind == 'peer':
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(peer.broker(self, ev), ensure_ascii=False), ex=3600)
        elif kind == 'pi_session':
            name = ev.get('agent')
            info = self.agent(name or '')
            if info and is_pi(self.runtime_cfg(name), name) and ev.get('generation') == int(info.get('generation', 0)):
                self.pi_metadata(name, ev)
        elif kind == 'turn':
            self.on_turn(ev)

    def chrome(self, argv):
        script = self.cfg.get('chrome_script')
        if not script:
            return {'status': 'rejected', 'reason': 'browser broker disabled (start with --chrome-script)'}
        if not isinstance(argv, list) or not argv or argv[0] not in CHROME_VERBS \
                or not all(isinstance(a, str) for a in argv):
            return {'status': 'rejected', 'reason': 'allowed verbs: ' + ' / '.join(sorted(CHROME_VERBS))}
        if argv[0] == 'js':
            if len(argv) != 3:
                return {'status': 'rejected', 'reason': 'usage: js <tab-index> <script-file>'}
            path = Path(argv[2]).resolve()
            if not path.is_relative_to(self.run_dir.resolve()) or not path.is_file():
                return {'status': 'rejected', 'reason': 'js script must be a file inside this run directory'}
            argv = argv[:2] + [str(path)]
        # Pace page actions here, not in the prompt: models do not honour "wait N seconds",
        # and an unpaced night of page loads can get the operator's IP firewalled.
        wait = self.last_chrome + float(self.cfg['chrome_min_interval']) - time.time()
        if argv[0] != 'check' and wait > 0:
            time.sleep(wait)
        self.last_chrome = time.time()
        try:
            p = subprocess.run([script] + argv, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {'status': 'error', 'rc': -1, 'out': 'browser broker timed out after 120 s'}
        self.log('chrome', argv=argv[:2], rc=p.returncode)
        return {'status': 'ok', 'rc': p.returncode, 'out': (p.stdout + p.stderr)[-200000:]}

    # -- interface contracts (task spec "contracts": true; docs/design.md §18)
    def contract_state(self):
        raw = None if self.service else self.r.get(CONTRACTS)
        return json.loads(raw) if raw else None

    def contract_phase(self):
        state = self.contract_state()
        return state['phase'] if state and self.running() else None

    def save_contract(self, state):
        self.r.set(CONTRACTS, json.dumps(state, ensure_ascii=False, separators=(',', ':')))
        self.sync_contract_files(state)

    def sync_contract_files(self, state=None):
        """Copy the registry view to <run>/contracts.json and <workspace>/contracts.json.
        Redis is the truth; tick() re-syncs after a v2 intent replay or a resume."""
        state = state or self.contract_state()
        if state is None:
            return
        text = json.dumps(contracts.view(state, self.cfg.get('worker_cli') or 'codex', peer.runtime(self)),
                          ensure_ascii=False, indent=2)
        if text == getattr(self, 'contracts_text', None):
            return
        for path in (self.run_dir / 'contracts.json', self.workspace() / 'contracts.json'):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.tmp')
            tmp.write_text(text + '\n')
            os.replace(tmp, path)
        self.contracts_text = text

    def contract_log(self, action, **data):
        with open(self.run_dir / 'contracts.log.jsonl', 'a') as out:
            out.write(json.dumps(dict(time=time.time(), action=action, **data), ensure_ascii=False) + '\n')
        self.log('contract.' + action, **data)

    def contract_queue(self, state, slot, text, owner=None):
        """Queue to a worker and record what its report will answer (a task id, or None for a notice)."""
        state['owners'].setdefault(slot, []).append(owner)
        self.queue(slot, text)

    def contract_notify(self, state, task_ids, text):
        """[contract] notices go to the workers of running tasks only; others read contracts.json."""
        slots = dict.fromkeys(state['tasks'][m]['to'] for m in task_ids
                              if m in state['tasks'] and state['tasks'][m]['status'] == 'running')
        for slot in slots:
            self.contract_queue(state, slot, text + '\nAct on it per CONTRACTS.md; reply in the AGENTS.md format '
                                '(Done / Evidence / Not done / Blocked) when done.')

    def contract_doc(self, role, name=None):
        text = (HERE / 'agents' / ('contracts_' + role + '.md')).read_text()
        text += peer.doc(self, role, name)  # §21: 5-A (Claude) or 5-B (Pi / Codex) by this agent's CLI
        return text.replace('{{WORKSPACE}}', str(self.workspace()))

    def contract_decision(self, state, decision):
        """Validate a decision against the contract phase. Returns (state, rows to queue now, held rows,
        approved/rejected changes, skeleton?) without side effects; ValueError carries the spec texts."""
        s = copy.deepcopy(state)
        if 'artifacts' in decision:  # before any status: COMPLETED runs cmd_exit right away
            validate_artifacts(decision['artifacts'], extended=True)
        if decision['status'] != 'RUNNING':
            return s, [], [], [], False
        if s['phase'] == 'interfaces':
            s['plan'], s['interfaces'] = contracts.p0(decision)
            s['tasks'] = {p['id']: {'id': p['id'], 'status': 'pending', 'attempts': 0, 'stale': [],
                                    'provides': p['provides'], 'consumes': p['consumes'], 'depends_on': []}
                          for p in s['plan']}
            s['phase'], s['skeleton_tries'] = 'skeleton', s['skeleton_tries'] + 1
            return s, [], [], [], True
        if s['phase'] == 'skeleton':
            if decision['dispatch'] or decision.get('interfaces') or decision.get('phase'):
                raise ValueError('P1: the skeleton run is in progress; do not dispatch or change interfaces until '
                                 '"[contract] interfaces frozen at v1"')
            return s, [], [], [], False
        if decision.get('phase') == 'interfaces':
            raise ValueError('interfaces are frozen at v1: change one with change "compatible" or "breaking"')
        changes = contracts.supervisor_interfaces(s, decision.get('interfaces'))
        now, held = contracts.dispatch(s, decision['dispatch'], self.resolve)
        return s, now, held, changes, False

    def contract_apply(self, t, state, changes, skeleton):
        """Side effects of an accepted decision: change notices, the P1 skeleton delivery."""
        for key_, kind, notify in changes:
            if kind == 'veto':  # the sub-fleet that registered it withdraws it on its next tick
                folder = Path(notify['run']) / 'subfleet-vetoes'
                folder.mkdir(parents=True, exist_ok=True)
                save(folder / (uuid.uuid4().hex[:12] + '.json'), {'ref': key_, 'note': notify['note']})
                self.contract_log('vetoed', by='supervisor', iface=key_, run=notify['run'], note=notify['note'])
                continue
            self.contract_log('approved' if kind != 'rejected' else 'rejected', by='supervisor', iface=key_, change=kind)
            self.contract_notify(state, notify, '[contract] ' + ('rejected ' + key_ if kind == 'rejected'
                                 else 'approved ' + key_ + ' (' + kind + '); read it in contracts.json'))
        if skeleton:
            worker = state['skeleton_worker']
            self.contract_log('p0.passed', interfaces=sorted(state['interfaces']), attempt=state['skeleton_tries'])
            self.save_contract(state)  # the skeleton worker reads contracts.json first
            self.contract_queue(state, worker, '[Supervisor instruction ' + t.get('id', '') + '] [skeleton] '
                                'P1 skeleton run (interfaces are not frozen yet; no real work). Read '
                                + str(self.workspace() / 'contracts.json') + '; in dependency order feed each upstream '
                                'sample into its consumer (code: a minimal stub per interface, real importable '
                                'signatures with raise NotImplementedError, one call chain; research: walk the outline '
                                'using the samples). Write ./skeleton.md, one line per interface: '
                                '`IF-x@1 · producer → consumer · PASS/FAIL · evidence`. Never edit the samples.'
                                '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                                'subdirectory ' + worker + '/). Reply in the AGENTS.md format when done.',
                                contracts.SKELETON)
        self.notify_parent(state, [(k, kind) for k, kind, _ in changes if kind not in ('rejected', 'veto')])

    def contract_message(self, task_id, row):
        def names(k):
            return ', '.join(row.get(k) or []) or 'none'
        return ('[Supervisor instruction ' + task_id + '] [' + row['id'] + '] ' + row['instruction']
                + '\nInterface contracts are on (follow CONTRACTS.md in your directory; shapes in '
                + str(self.workspace() / 'contracts.json') + '). Task ' + row['id'] + ': provides ' + names('provides')
                + '; consumes ' + names('consumes') + '; edits ' + names('edits') + '.'
                + '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                'subdirectory ' + row['to'] + '/). Reply in the AGENTS.md format when done.')

    def contract_gate(self, state):
        """COMPLETED: every non-deprecated interface must be ready."""
        out = []
        for key_, e in sorted(state['interfaces'].items()):
            if e['status'] not in ('ready', 'deprecated'):
                detail = contracts.file_failures(e, self.workspace()) or ['the producer has not announced ready']
                out.append(key_ + ' not ready (' + '; '.join(detail) + ')')
        return out

    def contract_report(self, name, text):
        """Apply a worker report to the task table. Returns extra lines for the supervisor,
        or None when the report ended the task."""
        state = self.contract_state()
        if state is None:
            return ''
        s, ws, notes = copy.deepcopy(state), self.workspace(), []
        owners = s['owners'].get(name) or []
        owner = owners.pop(0) if owners else None
        fields = contracts.report_fields(text)
        if owner == contracts.SKELETON:
            path = ws / name / 'skeleton.md'
            failures = contracts.skeleton_verdict(path.read_text(errors='replace') if path.is_file() else None,
                                                  s['interfaces'])
            self.contract_log('skeleton', by=name, attempt=s['skeleton_tries'], failures=failures)
            if not failures:
                for e in s['interfaces'].values():
                    e.update(status='declared', approved_by='supervisor')
                s['phase'] = 'parallel'
                self.notify_parent(s, [(k, 'new') for k in sorted(s['interfaces'])])
                notes.append('[contract] interfaces frozen at v1. Dispatch M0…Mn now: every dispatch item has id, '
                             'provides and consumes (id@version) per the plan; consumers start from the samples.')
            elif contracts.SKELETON_TRIES and s['skeleton_tries'] >= contracts.SKELETON_TRIES:
                self.save_contract(s)
                self.finish('blocked', 'P1: skeleton run failed ' + str(s['skeleton_tries']) + ' times: '
                            + '; '.join(failures))
                return None
            else:
                s['phase'] = 'interfaces'
                notes.append('[contract] skeleton run FAILED (try ' + str(s['skeleton_tries'])
                             + (' of ' + str(contracts.SKELETON_TRIES) if contracts.SKELETON_TRIES else '')
                             + '): ' + '; '.join(failures)
                             + '\nFix the interfaces in a new reply with phase "interfaces" (full plan + interfaces, '
                             'no dispatch).')
        row = s['tasks'].get(owner) if owner and owner != contracts.SKELETON else None
        mine = row or next((t for t in s['tasks'].values() if t.get('to') == name and t['status'] == 'running'), None)
        hold = None  # an escalate report is not a completion: the task row stays open
        if fields.get('escalate') is not None:
            task_id, ok, note, reply = self.escalate_intake(name, fields, s)
            hold = task_id if task_id in s['tasks'] else (mine or {}).get('id')
            if hold:
                s['tasks'][hold].update(status='escalated' if ok else 'running',
                                        reason='asked to split' if ok else 'escalate refused; worker continues')
            if reply:
                self.contract_queue(s, name, reply, hold)
            notes.append(note)
        change = fields.get('interface_change')
        if change is not None:
            try:
                if mine is None:
                    raise ValueError('interface_change: you have no task in the task table')
                proposal = contracts.worker_change(s, change, name, mine['id'], ws)
                p = s['pending'][proposal]
                self.contract_log('proposed', by=name, iface=proposal, change=p['change'], reason=p['reason'])
                notes.append('[contract] proposal ' + proposal + ' (' + p['change'] + ') from ' + self.label(name)
                             + ' / ' + mine['id'] + ': ' + p['reason'] + '\nDecide it in your next JSON: '
                             'interfaces: [{"id": "' + p['id'] + '", "version": ' + str(p['version'])
                             + ', "decision": "approve"}] (or "reject").')
            except ValueError as exc:
                self.contract_log('rejected', by=name, error=str(exc))
                self.contract_queue(s, name, '[contract] your interface_change was rejected: ' + str(exc)
                                    + '\nFix it and propose again; keep working on the current version. Reply in '
                                    'the AGENTS.md format (Done / Evidence / Not done / Blocked).')
                notes.append('[contract] rejected interface_change from ' + self.label(name) + ': ' + str(exc))
        for iface, version in contracts.ready_claims(str(fields.get('interfaces', ''))):
            key_ = iface + '@' + str(version)
            e = s['interfaces'].get(key_)
            producer = s['tasks'].get(e['producer']) if e else None
            if not e or e['status'] == 'deprecated' or not producer or producer.get('to') != name:
                notes.append('[contract] not ready ' + key_ + ': only the producer may announce a registered, '
                             'current interface ready')
                continue
            failures = contracts.file_failures(e, ws)
            if failures:
                self.contract_notify(s, [e['producer']] + e['consumers'], '[contract] not ready ' + key_ + ': '
                                     + '; '.join(failures))
            else:
                e['status'] = 'ready'
                self.contract_notify(s, e['consumers'], '[contract] ready ' + key_ + ': switch your input from '
                                     './stubs/ to ' + e['path'] + ' and re-run.')
            self.contract_log('ready' if not failures else 'not_ready', by=name, iface=key_, failures=failures)
            notes.append('[contract] ' + ('ready ' if not failures else 'not ready ') + key_
                         + ((': ' + '; '.join(failures)) if failures else ''))
        if row and row['id'] != hold:
            current = [contracts.current(s, i) for i in row.get('provides') or []]
            unready = [r for r, e in zip(row.get('provides') or [], current) if not e or e['status'] != 'ready']
            row['status'] = 'failed' if unready else 'done'
            row['reason'] = ('reported, but not ready: ' + ', '.join(unready)) if unready else 'outputs verified'
            self.contract_log('task.' + row['status'], task=row['id'], by=name, reason=row['reason'])
            for held in contracts.releasable(s) if not unready else []:
                held['status'] = 'running'
                held['attempts'] = held.get('attempts', 0) + 1
                self.contract_queue(s, held['to'], held['message'], held['id'])
                self.contract_log('task.released', task=held['id'])
        if fields.get('peer_notices') and str(fields['peer_notices']).strip().lower() != 'none':
            self.contract_log('peer_notices', by=name, line=str(fields['peer_notices']))
            notes += peer.violations(self, name, fields['peer_notices'])
        question = str(fields.get('question', '')).strip()
        if question and question.lower() != 'none':
            notes.append('[worker question] ' + self.label(name) + ': ' + question)
        notes.append(contracts.table_line(s))
        self.save_contract(s)
        return '\n' + '\n'.join(notes)

    # ------------------------------------------------------------ escalate: worker asks to split (§20)
    def escalation_state(self):
        raw = None if self.service else self.r.get(ESCALATIONS)
        return json.loads(raw) if raw else {}

    def save_escalations(self, state):
        self.r.set(ESCALATIONS, json.dumps(state, ensure_ascii=False, separators=(',', ':')))

    def escalation_item(self, task_id, state):
        """(worker slot, original instruction) of a task id: the contract task table, else plain dispatch ids."""
        if state is not None:
            row = state['tasks'].get(task_id) or {}
            return row.get('to'), row.get('instruction'), row.get('status')
        row = json.loads(self.r.get(ITEMS) or '{}').get(task_id) or {}
        return row.get('to'), row.get('instruction'), 'running' if row else None

    def escalate_check(self, name, fields, state):
        reason, proposal = fields.get('escalate'), fields.get('proposal')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('escalate must be a one-sentence reason')
        if not isinstance(proposal, list) or not proposal or not all(
                isinstance(p, dict) and isinstance(p.get('id'), str) and isinstance(p.get('instruction'), str)
                and len(p['instruction'].strip()) >= 12 and isinstance(p.get('output'), str) and p['output'].strip()
                for p in proposal):
            raise ValueError('proposal must be a non-empty list of {id, instruction (>= 12 chars), output}')
        ids = [p['id'] for p in proposal]
        parents = {i.rsplit('.', 1)[0] for i in ids if re.fullmatch(contracts.M_RE + r'\.\d+', i)}
        if len(parents) != 1 or len(set(ids)) != len(ids) or not all('.' in i for i in ids):
            raise ValueError('proposal ids must be distinct and all <your task id>.<n> (e.g. M0.0, M0.1)')
        task_id = parents.pop()
        slot, instruction, status = self.escalation_item(task_id, state)
        if slot is None and state is None and not json.loads(self.r.get(ITEMS) or '{}'):
            raise ValueError('escalate needs a task id: the supervisor must dispatch with "id" (M<n>) '
                             'or run a contracts task')
        if slot != name or status not in ('running', 'escalated'):
            raise ValueError(task_id + ' is not a running task of ' + self.label(name))
        workers = fields.get('workers', len(proposal))
        if isinstance(workers, bool) or not isinstance(workers, int):
            raise ValueError('workers must be a positive integer')
        refused = subfleet.check(self, name, task_id, workers, ready=False)
        if refused:
            raise ValueError(refused)
        return task_id, {'slot': name, 'reason': reason.strip(), 'proposal': proposal, 'workers': workers,
                         'instruction': instruction or '', 'status': 'escalated'}

    def escalate_intake(self, name, fields, state):
        """A worker report carrying ``escalate``. Returns (task id, accepted?, note for the supervisor,
        reply for the worker or None). Refusals carry the program's reason to both."""
        try:
            task_id, rec = self.escalate_check(name, fields, state)
        except ValueError as exc:
            self.log('escalate.refused', agent=name, reason=str(exc))
            return None, False, ('\n[escalate] refused for ' + self.label(name) + ': ' + str(exc)
                                 + ' (the worker was told and continues its task)'), \
                ('[escalate] your split request was refused by the program: ' + str(exc)
                 + '\nContinue your current task yourself (or fix the escalate JSON and send it again). '
                 'Reply in the AGENTS.md format (Done / Evidence / Not done / Blocked).')
        esc = self.escalation_state()
        esc[task_id] = rec
        self.save_escalations(esc)
        self.log('escalate.asked', agent=name, task=task_id, workers=rec['workers'], reason=rec['reason'])
        who = self.label(name)
        pieces = '\n'.join('- ' + p['id'] + ': ' + p['instruction'].strip() + ' -> ' + p['output'] for p in rec['proposal'])
        note = ('\n[escalate] ' + who + ' asks to split ' + task_id + ' into ' + str(len(rec['proposal']))
                + ' pieces run by a sub-fleet of ' + str(rec['workers']) + ' workers. Reason: ' + rec['reason']
                + '\n' + pieces + '\nAnswer in your next JSON: "escalations": [{"task": "' + task_id
                + '", "decision": "approve" | "revise" | "reject", "workers": <n, optional>, "note": "<optional>"}]. '
                'approve: the program starts sub-fleet ' + task_id + ' with ' + who + ' as its supervisor; '
                'revise: your note goes back to ' + who + '; reject: ' + who + ' does ' + task_id + ' itself. '
                'Do not dispatch to ' + who + ' in the reply that approves.')
        return task_id, True, note, None

    def escalation_decision(self, decision, state):
        """Validate the supervisor's escalations against the open requests (no side effects)."""
        open_ = self.escalation_state()
        busy = {self.resolve(x['to']) for x in decision['dispatch']}
        out = []
        for item in decision['escalations']:
            rec = open_.get(item['task'])
            if not rec or rec.get('status') != 'escalated':
                raise ValueError('escalations: ' + item['task'] + ' has no open escalate request')
            if item['task'] in {x.get('id') for x in decision['dispatch']}:
                raise ValueError('escalations: ' + item['task'] + ' is also dispatched in this reply; do one or the other')
            if item['decision'] == 'approve' and rec['slot'] in busy:
                raise ValueError('escalations: approve ' + item['task'] + ' hands ' + self.label(rec['slot'])
                                 + ' over to a sub-fleet; do not dispatch to it in the same reply')
            out.append((item['task'], rec, item))
        return out

    def escalation_apply(self, t, state, task_id, rec, answer):
        esc = self.escalation_state()
        note = answer.get('note', '').strip()
        row = state['tasks'].get(task_id) if state is not None else None
        slot = rec['slot']
        if answer['decision'] == 'approve':
            workers = answer.get('workers', rec['workers'])
            refused = subfleet.check(self, slot, task_id, workers, ready=False)
            if refused:  # a limit, not a malformed reply: no strike, the request stays open
                self.log('escalate.approve_refused', task=task_id, reason=refused)
                self.queue('supervisor', '[escalate] approve of ' + task_id + ' refused by the program: ' + refused
                           + '. ' + task_id + ' still waits for your answer (approve with fewer workers / revise / '
                           'reject). Reply with the JSON format from AGENTS.md.')
                return
            output, goal = None, rec['instruction']
            if row is not None:
                paths = [e['path'] for e in (contracts.current(state, i) for i in row.get('provides') or []) if e]
                output = str(self.workspace() / paths[0]) if paths else None
                if paths:
                    goal += ('\nThe parent fleet verifies these interfaces of ' + task_id + ' (shapes in '
                             + str(self.workspace() / 'contracts.json') + '): '
                             + ', '.join(str(self.workspace() / x) for x in paths))
                row.update(status='delegated', attempts=row.get('attempts', 0) + 1, reason='sub-fleet ' + task_id)
            if note:
                goal += '\nSupervisor note: ' + note
            rid = subfleet.request(self.run_dir, slot, task_id, goal, workers, output=output,
                                   proposal=rec['proposal'], contracts=state is not None)
            rec.update(status='approved', rid=rid, workers=workers, note=note)
            esc[task_id] = rec
            self.log('escalate.approved', task=task_id, request=rid, workers=workers)
        else:
            esc.pop(task_id, None)
            text = ('[escalate] the supervisor asks you to revise your split of ' + task_id + ': ' + (note or '-')
                    + '\nSend a new escalate JSON at the end of your report, or continue ' + task_id + ' yourself.'
                    if answer['decision'] == 'revise' else
                    '[escalate] the supervisor rejected splitting ' + task_id + (': ' + note if note else '')
                    + '\nDo ' + task_id + ' yourself now.')
            text = '[Supervisor instruction ' + t.get('id', '') + '] [' + task_id + '] ' + text + \
                '\nReply in the AGENTS.md format (Done / Evidence / Not done / Blocked) when done.'
            if row is not None:
                row.update(status='running', reason=answer['decision'] + 'd split')
                self.contract_queue(state, slot, text, task_id)
            else:
                self.queue(slot, text)
            self.log('escalate.' + answer['decision'] + 'd', task=task_id, note=note)
        self.save_escalations(esc)

    def escalation_tick(self):
        """Approved splits: read the spawn answer; a refusal reopens the request for the supervisor."""
        if not self.running():
            return
        esc, changed = self.escalation_state(), False
        for task_id, rec in esc.items():
            if rec.get('status') != 'approved':
                continue
            try:
                answer = load(subfleet.answer_path(self.run_dir, rec['rid']))
            except (OSError, ValueError):
                continue
            changed = True
            if answer.get('status') == 'accepted':
                rec.update(status='delegated', run=answer.get('run'))
                self.log('escalate.delegated', task=task_id, run=answer.get('run'))
                continue
            rec['status'] = 'escalated'
            state = self.contract_state()
            if state is not None and task_id in state['tasks']:
                state['tasks'][task_id].update(status='escalated', reason='sub-fleet refused')
                self.save_contract(state)
            self.log('escalate.spawn_refused', task=task_id, reason=answer.get('reason'))
            self.queue('supervisor', '[escalate] sub-fleet ' + task_id + ' could not start: ' + str(answer.get('reason'))
                       + '\n' + task_id + ' waits for your answer again (escalations: approve with fewer workers / '
                       'revise / reject). Reply with the JSON format from AGENTS.md.')
        if changed:
            self.save_escalations(esc)

    def escalation_finished(self, rec, ok):
        """A delegated task came back (subfleet.collect). Returns extra lines for the supervisor."""
        task_id = rec['task_id']
        esc = self.escalation_state()
        if esc.pop(task_id, None) is None:
            return ''  # an operator spawn, not an escalation
        self.save_escalations(esc)
        state = self.contract_state()
        row = state['tasks'].get(task_id) if state is not None else None
        if row is None:
            return ''
        failures = [] if ok else ['sub-fleet failed (attempt ' + str(rec.get('attempt', 1)) + '): '
                                  + str(rec.get('reason') or rec.get('outcome'))]
        entries = [contracts.current(state, i) for i in row.get('provides') or []]
        if ok:
            for i, e in zip(row.get('provides') or [], entries):
                failures += ([i + ' is not registered'] if e is None else contracts.file_failures(e, self.workspace()))
        notes = []
        if not failures:
            for e in entries:
                if e['status'] != 'ready':
                    e['status'] = 'ready'
                    self.contract_notify(state, e['consumers'], '[contract] ready ' + contracts.ref(e)
                                         + ': switch your input from ./stubs/ to ' + e['path'] + ' and re-run.')
                    notes.append('[contract] ready ' + contracts.ref(e))
            row.update(status='done', reason='sub-fleet outputs verified')
            for held in contracts.releasable(state):
                held['status'] = 'running'
                held['attempts'] = held.get('attempts', 0) + 1
                self.contract_queue(state, held['to'], held['message'], held['id'])
                self.contract_log('task.released', task=held['id'])
        else:
            row.update(status='failed', reason='; '.join(failures))
        self.contract_log('task.' + row['status'], task=task_id, by='sub-fleet', reason=row['reason'])
        notes.append(contracts.table_line(state))
        self.save_contract(state)
        return '\n' + '\n'.join(notes)

    # ------------------------------------------------------------ sub-fleet interfaces: notify the top, veto (§20)
    def notify_parent(self, state, changes):
        """A sub-fleet's interfaces take effect at once; the top supervisor is told and may veto."""
        if not changes or not self.cfg.get('parent_config'):
            return
        folder = Path(load(self.cfg['parent_config'])['run']) / 'subfleet-notices'
        folder.mkdir(parents=True, exist_ok=True)
        items = [{'ref': k, 'change': kind, 'path': state['interfaces'][k]['path'],
                  'producer': state['interfaces'][k]['producer']} for k, kind in changes if k in state['interfaces']]
        save(folder / (uuid.uuid4().hex[:12] + '.json'),
             {'origin_run': str(self.run_dir), 'task': self.cfg.get('task_id'), 'interfaces': items})

    def relay_notices(self):
        """Parent tick: pass a sub-fleet notice up the tree; the top fleet tells its supervisor."""
        folder = self.run_dir / 'subfleet-notices'
        for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
            if self.cfg.get('parent_config'):
                up = Path(load(self.cfg['parent_config'])['run']) / 'subfleet-notices'
                up.mkdir(parents=True, exist_ok=True)
                os.replace(path, up / path.name)
                continue
            try:
                note = load(path)
            except (OSError, ValueError):
                note = None
            path.unlink(missing_ok=True)
            if not note or not self.running():
                continue
            state = self.contract_state()
            if state is not None:
                state.setdefault('child_interfaces', {}).update({i['ref']: note['origin_run'] for i in note['interfaces']})
                self.save_contract(state)
            self.log('subfleet.interfaces', task=note.get('task'), interfaces=[i['ref'] for i in note['interfaces']])
            self.queue('supervisor', '[sub-fleet interfaces] sub-fleet ' + str(note.get('task')) + ' registered: '
                       + '; '.join(i['ref'] + ' (' + i['change'] + ', ' + i['path'] + ')' for i in note['interfaces'])
                       + '\nThey are in effect already. To withdraw one, add to your next JSON "interfaces": [{"id": '
                       '"<id>", "version": <n>, "decision": "veto", "note": "<why>"}]. Reply with the JSON format '
                       'from AGENTS.md.')

    def apply_vetoes(self):
        """Sub-fleet tick: the top supervisor vetoed one of our interfaces: withdraw it (deprecated)."""
        folder = self.run_dir / 'subfleet-vetoes'
        for path in sorted(folder.glob('*.json')) if folder.is_dir() else []:
            try:
                veto = load(path)
            except (OSError, ValueError):
                veto = None
            path.unlink(missing_ok=True)
            state = self.contract_state() if veto and self.running() else None
            e = state['interfaces'].get(veto['ref']) if state else None
            if not e or e['status'] == 'deprecated':
                continue
            e.update(status='deprecated', deprecated_by='veto by the top supervisor')
            self.contract_log('vetoed', by='top supervisor', iface=veto['ref'], note=veto.get('note', ''))
            self.contract_notify(state, [e['producer']] + e['consumers'], '[contract] ' + veto['ref']
                                 + ' was vetoed by the top supervisor: ' + (veto.get('note') or '-') + '. Stop using it.')
            self.queue('supervisor', '[contract] the top supervisor vetoed ' + veto['ref'] + ': ' + (veto.get('note') or '-')
                       + '\nIt is withdrawn (deprecated). Re-plan the tasks that provide or consume it (declare a '
                       'replacement under a new id). Reply with the JSON format from AGENTS.md.')
            self.save_contract(state)

    def pi_metadata(self, name, ev):
        fields = {k: ev[k] for k in ('thread', 'transcript', 'ctx_used', 'ctx_window')
                  if ev.get(k) is not None}
        if fields:
            self.setf(name, **fields)

    def on_turn(self, ev):
        if self.v2:
            raise ValueError('v2 accepts bound structured settlement events only')
        name, info = ev.get('agent'), self.agent(ev.get('agent', ''))
        if not info or ev.get('generation') != int(info.get('generation', 0)):
            self.log('turn.stale', agent=name, generation=ev.get('generation'))
            return
        if info.get('state') == 'dead' or (is_pi(self.runtime_cfg(name), name) and info.get('state') not in ('starting', 'busy', 'handoff')):
            self.log('turn.duplicate', agent=name, msg_id=ev.get('msg_id'))
            return
        if info.get('state') == 'starting':
            if info.get('launch_ready', '1') != '1':
                self.log('turn.launch_held', agent=name, generation=ev.get('generation'))
                return
            if ev.get('failed') or ev.get('aborted') or ev.get('text', '').strip() != 'READY':
                self.block_profile(name, 'startup did not return READY; check its terminal', dead=True)
                return
        if (is_claude(self.runtime_cfg(name), name) or is_pi(self.runtime_cfg(name), name)) and info.get('state') in ('busy', 'handoff', 'idle'):
            # A Claude/Pi turn counts only if it answers the message delivered last, once. (The spawn /
            # takeover prompt is a CLI argument without a msg id: that turn is accepted in 'starting'.)
            if info.get('state') == 'idle' or ev.get('msg_id') != info.get('msg_id') or info.get('msg_done') == info.get('msg_id'):
                self.log('turn.duplicate', agent=name, msg_id=ev.get('msg_id'), current=info.get('msg_id'))
                if name != 'supervisor' and is_claude(self.runtime_cfg(name), name) and ev.get('text', '').strip():
                    peer.unsolicited(self, name, ev['text'][-8000:])  # e.g. woken by a peer SendMessage
                return
            self.setf(name, msg_done=ev.get('msg_id'))
        if is_pi(self.runtime_cfg(name), name):
            self.pi_metadata(name, ev)
            failed = ev.get('failed') or ev.get('aborted')
            if failed and info.get('state') == 'handoff':
                self.finish_rotation(name, timed_out=True)
                return
            if failed:
                ev = dict(ev, text='')  # never publish a partial/error response as a successful result
        if ev.get('thread'):
            self.setf(name, thread=ev['thread'])
        if ev.get('transcript'):
            self.setf(name, transcript=ev['transcript'])
        if info.get('state') == 'handoff':
            self.finish_rotation(name)
            return
        was = info.get('state')
        self.setf(name, state='idle', stalls=0, idle_confirmed=1)
        if was == 'starting':  # READY after spawn / takeover
            self.setf(name, runtime_ready=1, current_profile=info.get('runtime_profile', ''))
            if info.get('runtime_profile'):
                self.setf(name, profile_state='ready', profile_reason='runtime returned READY; queued assignment awaits revalidation')
            self.log('ready', agent=name, generation=info.get('generation'))
            return
        text = ev.get('text', '')
        if not text.strip() and info.get('last_msg'):
            # Empty turn: the model backend dropped the request (seen with a local server
            # under memory pressure). Re-deliver the same message after a backoff instead of
            # spending a supervisor turn on it.
            tries = int(info.get('empty_tries', 0)) + 1
            self.log('turn.empty', agent=name, tries=tries)
            if tries <= 3:
                self.r.lpush(key('inbox', name), info['last_msg'])
                self.setf(name, empty_tries=tries, retry_at=time.time() + 30 * tries)
                return
        self.setf(name, empty_tries=0)
        self.r.rpush(key('replies', name), text[-4000:])
        self.r.ltrim(key('replies', name), -5, -1)
        if not self.running():
            self.log('turn.outside_task', agent=name)
            return
        if self.service:
            if name == 'supervisor':
                self.on_service_supervisor(text)
            else:
                self.on_service_worker(name, info, text)
            return
        if name == 'supervisor':
            self.on_supervisor(text)
        else:
            prev = info.get('prev_label')
            self.report(name, text[-8000:], prev)
            if prev:
                self.setf(name, prev_label='')

    def report(self, name, text, prev=None):
        tag = '[' + self.label(name) + ' report' + (' (took over from ' + prev + ')' if prev else '') + ']'
        extra = self.contract_report(name, text)
        if extra is None:  # the contract gate ended the task
            return
        fields = contracts.report_fields(text) if self.contract_state() is None else {}
        if fields.get('escalate') is not None:  # plain task: no task table, same validation
            _, _, note, reply = self.escalate_intake(name, fields, None)
            if reply:
                self.queue(name, reply)
            extra += note
        self.queue('supervisor', tag + '\n' + text + extra + '\n\nReply with the JSON format from AGENTS.md.')

    def others_active(self):
        # Contract tasks: P0 / P1 replies legitimately dispatch nothing (spec §3.0), so the
        # "no worker is busy: RUNNING must dispatch" rule does not apply in those phases.
        if self.contract_phase() in ('interfaces', 'skeleton'):
            return True
        def busy(info):
            # A Claude worker held for its first advisor sheet with nothing queued is not working (P1-01).
            if info.get('advice_hold') == '1' and info.get('state') == 'starting':
                return False
            return info.get('state') != 'idle'
        return any(busy(self.agent(w)) or self.inbox_depth(w) for w in self.workers)

    def on_supervisor(self, text):
        t = self.task()
        try:
            decision = parse_decision(text, self.dispatch_targets(), self.others_active())
            state = self.contract_state()
            if state is not None:
                state, rows, held, changes, skeleton = self.contract_decision(state, decision)
                rows = {r['id']: r for r in rows + held}
            # Validate the whole dispatch before queuing any part of it. Rejections
            # use the existing supervisor correction/three-strike blocked path.
            prepared = []
            for item in decision['dispatch'] if decision['status'] == 'RUNNING' else []:
                slot = self.resolve(item['to'])
                instruction = item['instruction'].strip()
                prepared.append((slot, instruction, self.prepare_worker_profile(slot, instruction)))
            escalations = self.escalation_decision(decision, state) if decision['status'] == 'RUNNING' else []
        except (ValueError, TypeError, KeyError) as exc:
            # P0 gap lists never count toward the three-strike limit (user 10/13): the supervisor
            # keeps fixing interfaces until all checks pass; the task `hours` budget is the bound.
            p0 = str(exc).startswith('P0')
            bad = int(self.r.hget(TASK, 'bad_replies') or 0) if p0 else self.r.hincrby(TASK, 'bad_replies', 1)
            if not p0 and bad >= 3:
                self.finish('blocked', 'supervisor gave no valid decision JSON three times in a row: ' + str(exc))
            else:
                self.queue('supervisor', 'Your reply is not a valid decision JSON (' + str(exc)
                           + '). Resend one JSON object in the AGENTS.md format, nothing else.')
            return
        artifacts = json.loads(t.get('artifacts') or '[]')
        artifacts += [a for a in decision.get('artifacts', []) if a not in artifacts]
        self.r.hset(TASK, mapping={'bad_replies': 0, 'completion': decision['completion'],
                                   'reason': decision.get('reason', ''),
                                   'artifacts': json.dumps(artifacts, ensure_ascii=False)})
        if decision['status'] == 'BLOCKED':
            self.finish('blocked', decision.get('reason', 'needs a human'))
            return
        if decision['status'] == 'COMPLETED':
            failures = (check_artifacts(artifacts, self.workspace(), state is not None, state is not None)
                        if artifacts else ['no artifacts were declared, nothing to verify'])
            failures += self.contract_gate(state) if state is not None else []
            if not failures:
                self.finish('completed', decision.get('reason', 'artifacts verified'))
                return
            bounces = self.r.hincrby(TASK, 'bounces', 1)
            if bounces >= 3:
                self.finish('blocked', 'supervisor claimed completion three times without passing verification: '
                            + '; '.join(failures))
            else:
                self.queue('supervisor', 'Independent verification failed, the task cannot end: ' + '; '.join(failures)
                           + '\nDispatch corrective instructions to the workers (RUNNING + dispatch).')
            return
        if state is not None:
            self.contract_apply(t, state, changes, skeleton)
        for item, (slot, instruction, application) in zip(decision['dispatch'], prepared):
            # Metadata describes the next instruction, not the last appended one.
            # A busy worker records intent only; delivery re-evaluates at idle.
            advisory = profile_runtime.profile_advisory(slot, application)
            if self.cfg.get('advisor') is True:
                # Only supervisor dispatches are advised (never [contract] notices or report corrections).
                # Registered before the depends_on hold: a held row is advised when the daemon releases it.
                self.r.rpush(ADVICE_PENDING + slot, json.dumps({'instruction': instruction,
                                                                'note': item.get('advise_note', '')}, ensure_ascii=False))
            if state is not None:
                row = rows[item['id']]
                row['message'] = advisory + self.contract_message(t.get('id', ''), row)
                if row['status'] == 'pending':  # held until its depends_on are done
                    self.contract_log('task.held', task=row['id'], waits=row['depends_on'])
                    continue
                row['attempts'] = row.get('attempts', 0) + 1
            if not self.inbox_depth(slot):
                self.record_worker_profile(slot, application)
            if state is not None:
                self.contract_queue(state, slot, row['message'], row['id'])
                continue
            if item.get('id'):  # plain task: an id makes the item escalatable (§20)
                items = json.loads(self.r.get(ITEMS) or '{}')
                items[item['id']] = {'to': slot, 'instruction': instruction}
                self.r.set(ITEMS, json.dumps(items, ensure_ascii=False))
            self.queue(slot, advisory + '[Supervisor instruction ' + t.get('id', '') + '] '
                       + ('[' + item['id'] + '] ' if item.get('id') else '') + instruction
                       + '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                       'subdirectory ' + slot + '/). Reply in the AGENTS.md format when done.')
        esc = self.escalation_state()
        redispatched = [x['id'] for x in decision['dispatch'] if esc.get(x.get('id'), {}).get('status') == 'escalated']
        if redispatched:  # the supervisor answered the split by dispatching the task again
            self.save_escalations({k: v for k, v in esc.items() if k not in redispatched})
        for task_id, rec, answer in escalations:
            self.escalation_apply(t, state, task_id, rec, answer)
        if state is not None:
            self.save_contract(state)
        self.log('dispatch', count=len(decision['dispatch']), completion=decision['completion'])

    def submit(self, spec):
        if not isinstance(spec, dict) or not isinstance(spec.get('goal'), str) or not spec['goal'].strip():
            raise ValueError('a task needs a goal')
        if self.service:
            raise ValueError('this fleet runs in service mode: submit requests, not tasks')
        if self.running():
            raise ValueError('a task is already running; stop it or wait for it to finish')
        auto_agents = spec.get('auto_agents', False)
        if type(auto_agents) is not bool:
            raise ValueError('auto_agents must be a boolean')
        profile_runtime.validate_live_setting(spec.get('apply_profiles_live', False),
                                              allowed=self.cfg['allow_profile_live_switch'])
        if spec.get('apply_profiles_live') and not auto_agents:
            raise ValueError('apply_profiles_live requires auto_agents and profile assignments')
        use_contracts = spec.get('contracts', False)
        if type(use_contracts) is not bool:
            raise ValueError('contracts must be a boolean')
        skeleton_worker = spec.get('skeleton_worker', (self.active_workers() or [None])[0])
        if use_contracts and skeleton_worker not in self.active_workers():
            raise ValueError('skeleton_worker must be one of ' + ', '.join(self.active_workers()))
        if 'artifacts' in spec:
            # Auto-planned task specs may explicitly say there are no pre-declared artifacts.
            # Keep the legacy non-auto validation unchanged.
            if not (auto_agents and spec['artifacts'] == []):
                validate_artifacts(spec['artifacts'], extended=use_contracts)
        hours = spec.get('hours', 16)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours < 1000:
            raise ValueError('hours must be a positive number')
        registry_path = validate_agent_registry_path(spec.get('agent_registry'))
        dispatch_plan = profile_assignments = None
        if auto_agents:
            registry = AgentRegistry(registry_path)
            # Official Claude workers: prefer claude cards so a live switch never leaves the CLI
            # (v2 Codex supervisor + Claude workers rejects Pi/Codex workers). Pi/Codex fleets unchanged.
            plan = build_dispatch_plan(spec['goal'], registry,
                                       'claude' if self.cfg.get('worker_cli') == 'claude' else None)
            dispatch_plan = plan.to_dict()
            profile_assignments = assign_profiles(plan, self.active_workers(), registry)
        task_id = 'task-' + uuid.uuid4().hex[:10]
        self.r.delete(TASK)
        fields = {'id': task_id, 'goal': spec['goal'], 'status': 'running', 'completion': 0,
                  'reason': '', 'deadline': time.time() + hours * 3600, 'bad_replies': 0,
                  'bounces': 0, 'artifacts': json.dumps(spec.get('artifacts', []), ensure_ascii=False),
                  'apply_profiles_live': json.dumps(spec.get('apply_profiles_live', False))}
        if use_contracts:
            fields['contracts'] = 1
        if dispatch_plan is not None:
            fields.update(
                auto_agents=1,
                agent_registry=str(registry.directory),
                dispatch_plan=json.dumps(dispatch_plan, ensure_ascii=False, separators=(',', ':')),
                profile_assignments=json.dumps(
                    profile_assignments, ensure_ascii=False, separators=(',', ':')),
            )
        self.r.hset(TASK, mapping=fields)
        prompt = ('[New task ' + task_id + '] Goal: ' + spec['goal']
                  + '\nWorkers you can dispatch to: ' + ', '.join(self.labels())
                  + '\nShared output directory: ' + str(self.workspace())
                  + ' (artifact paths are relative to it, e.g. worker-1/report.md)'
                  + '\nArtifacts pre-declared by the operator: '
                  + json.dumps(spec.get('artifacts', []), ensure_ascii=False))
        if dispatch_plan is not None:
            prompt += ('\nAuto-agent dispatch plan (profile ids are recommendations, not worker names): '
                       + fields['dispatch_plan']
                       + '\nProfile assignments to stable worker slots: ' + fields['profile_assignments']
                       + '\nUse each assignment worker value for dispatch.to; never use agent_id/profile as a worker name.'
                       + '\nInclude "Recommended profile: <agent_id>" in each worker instruction so the profile stays '
                       'assigned while the worker slot remains authoritative.'
                       + '\nInclude "Subtask: <subtask_id>" to bind the instruction to its assignment. '
                       'Only the daemon may apply profiles; workers must not switch their own runtime.'
                       + (' Live profile switching is enabled for this task.' if spec.get('apply_profiles_live')
                          else ' Profile application is advisory metadata only.')
                       + '\nDo not dispatch dependent subtasks until every id in depends_on has finished.')
        if use_contracts:
            self.contracts_text = None
            self.save_contract(contracts.new_state(skeleton_worker))
            for name in self.names:
                cwd = agent_cwd(self.cfg, name)
                cwd.mkdir(parents=True, exist_ok=True)
                (cwd / 'CONTRACTS.md').write_text(self.contract_doc('supervisor' if name == 'supervisor' else 'worker', name))
            prompt += ('\nInterface contracts are ON for this task: read CONTRACTS.md in your current directory now '
                       'and follow it. Your first reply is phase "interfaces": a full plan (M0…Mn) and every '
                       'interface with its sample, and an empty dispatch. The P1 skeleton worker is '
                       + skeleton_worker + '.')
        elif self.r.get(CONTRACTS) is not None:
            self.r.delete(CONTRACTS)  # an earlier contract task's state never leaks into this one
        for stale in (ESCALATIONS, ITEMS):
            if self.r.get(stale) is not None:
                self.r.delete(stale)
        if self.cfg.get('advisor') is True:  # an earlier task's unadvised dispatches never match this one
            for w in self.workers:
                if self.r.llen(ADVICE_PENDING + w):
                    self.r.delete(ADVICE_PENDING + w)
        self.queue('supervisor', prompt + '\nReply with the JSON format from AGENTS.md.')
        (self.run_dir / 'result.json').unlink(missing_ok=True)  # a new task: this run is not finished
        self.log('task.submitted', task=task_id)
        return task_id

    def finish(self, status, reason):
        if self.v2:
            return self.v2.finish(status, reason)
        if self.service:  # a resident fleet never ends: hold everything for a human instead
            self.pause(status + ': ' + reason)
            return
        self.r.hset(TASK, mapping={'status': status, 'reason': reason})
        self.log('task.' + status, reason=reason)
        for name in self.names:
            self.r.delete(key('inbox', name))
            pane = self.agent(name).get('pane')
            if pane and self.agent(name).get('state') == 'busy':
                # An aborted turn fires no notify, so mark idle here; tabs stay open.
                self.mgr.send_key(pane, 'Escape')
                self.setf(name, state='idle', idle_confirmed=0)

    # -- service mode
    def request(self, spec):
        if not self.service:
            raise ValueError('requests need a fleet started with --mode service')
        validate_request(spec, self.dispatch_targets())
        rid = spec['id']
        if (self.r.hgetall(REQ + rid) or self.r.get(RESULT + rid)
                or (self.v2 and self.r.get(self.v2.prefix + ':public:' + runtime_v2.digest(rid)))):
            raise ValueError('duplicate request id: ' + rid)
        self.r.hset(REQ + rid, mapping={'spec': json.dumps(spec, ensure_ascii=False), 'status': 'routing',
                                        'created': time.time(), 'attempts': 0, 'worker': ''})
        self.r.rpush(REQQ, rid)
        self.route(rid, spec)
        self.log('request', id=rid, to=spec.get('to') or 'supervisor')
        return rid

    def route(self, rid, spec):
        if spec.get('to'):
            self.assign(rid, self.resolve(spec['to']), spec.get('instruction'))
        else:
            self.queue('supervisor', '[Request ' + rid + '] A new request needs routing.'
                       + ('\nNote from the sender: ' + spec['instruction'] if spec.get('instruction') else '')
                       + '\nWorkers: ' + ', '.join(self.labels()) + '\nPayload (JSON):\n'
                       + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                       + '\nReply with the service decision JSON from AGENTS.md.')

    def assign(self, rid, slot, instruction=None):
        spec = json.loads(self.r.hget(REQ + rid, 'spec'))
        self.r.hset(REQ + rid, mapping={'status': 'dispatched', 'worker': slot})
        self.queue(slot, self.request_message(rid, spec, instruction))

    def request_message(self, rid, spec, instruction=None, prefix=''):
        reply = spec['reply']
        keys = ', '.join(reply['required'])
        if reply.get('allowed') is not None:
            keys += ' (allowed keys: ' + ', '.join(reply['allowed']) + ')'
        if self.v2:
            return ('[Request ' + rid + '] ' + prefix + (instruction or 'Handle this request as your role describes.').strip()
                    + '\nPayload (JSON):\n' + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                    + '\nUse fleet_result with a business JSON object with keys: ' + keys + '.')
        return ('[Request ' + rid + '] ' + prefix + (instruction or 'Handle this request as your role describes.').strip()
                + '\nPayload (JSON):\n' + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                + '\nYour final reply must be exactly one JSON object with the keys: ' + keys
                + '. No text outside the JSON object.')

    def publish(self, rid, status, **fields):
        if self.v2:
            return self.v2.publish(rid, status, **fields)
        req = self.r.hgetall(REQ + rid)
        now = time.time()
        rec = dict(id=rid, status=status, at=now, elapsed_s=round(now - float(req.get('created') or now), 1), **fields)
        raw = json.dumps(rec, ensure_ascii=False)
        self.r.set(RESULT + rid, raw, ex=RESULT_TTL)
        self.r.rpush(RESULTS, raw)
        self.r.ltrim(RESULTS, -1000, -1)
        self.r.delete(REQ + rid)
        self.r.lrem(REQQ, 0, rid)
        self.log('result', id=rid, status=status)
        if req.get('operator') == '1':
            self.queue('supervisor', '[Operator result ' + rid + '] ' + raw
                       + '\nThis request is finished. Report the worker result to the operator in the reason '
                       'field of the usual service decision JSON: status RUNNING, dispatch [], skip []. '
                       'Do not dispatch it again or invent a new request ID.')

    def on_service_worker(self, name, info, text):
        rid = info.get('request')
        self.setf(name, request='')
        if not rid:
            self.log('turn.no_request', agent=name)
            return
        req = self.r.hgetall(REQ + rid)
        if req.get('status') != 'dispatched':
            self.log('result.late', id=rid, agent=name)  # timed out or already answered
            return
        spec = json.loads(req['spec'])
        try:
            result = parse_reply(text, spec['reply'])
        except ValueError as exc:
            if self.r.hincrby(REQ + rid, 'attempts', 1) < 2:
                self.r.lpush(key('inbox', name), self.request_message(
                    rid, spec, 'Resend your answer as the JSON object only.',
                    prefix='Your last reply was rejected (' + str(exc) + '). '))
                return
            self.publish(rid, 'invalid', worker=self.label(name), error=str(exc), raw=text[-2000:])
            return
        self.publish(rid, 'ok', worker=self.label(name), result=result)

    def on_service_supervisor(self, text):
        try:
            decision = parse_service_decision(text, self.dispatch_targets())
        except (ValueError, TypeError, KeyError) as exc:
            bad = self.r.hincrby(SERVICE, 'bad_replies', 1)
            if bad >= 3:
                self.r.hset(SERVICE, mapping={'bad_replies': 0})
                self.pause('supervisor gave no valid decision JSON three times in a row: ' + str(exc))
            else:
                self.queue('supervisor', 'Your reply is not a valid service decision JSON (' + str(exc)
                           + '). Resend one JSON object in the AGENTS.md format, nothing else.')
            return
        self.r.hset(SERVICE, mapping={'bad_replies': 0, 'reason': decision.get('reason', '')})
        for item in decision['dispatch']:
            if self.r.hget(REQ + item['request_id'], 'status') != 'routing':
                self.log('dispatch.unknown_request', id=item['request_id'])
                continue
            self.assign(item['request_id'], self.resolve(item['to']), item['instruction'])
        for item in decision['skip']:
            if self.r.hget(REQ + item['request_id'], 'status') == 'routing':
                self.publish(item['request_id'], 'skipped', worker='supervisor', reason=item['reason'])
        if decision['status'] == 'BLOCKED':
            self.pause(decision.get('reason') or 'supervisor needs a human')

    def expire_requests(self, now):
        for rid in self.r.lrange(REQQ, 0, -1):
            req = self.r.hgetall(REQ + rid)
            if not req:
                self.r.lrem(REQQ, 0, rid)
                continue
            limit = json.loads(req['spec']).get('timeout_s') or float(self.cfg['request_timeout'])
            if now - float(req['created']) >= limit:
                self.publish(rid, 'timeout', worker=req.get('worker') or 'supervisor',
                             error='no valid answer within ' + str(int(limit)) + ' s (' + req.get('status', '') + ')')

    # -- delivery + rotation
    def pump(self):
        if (self.service or self.v2) and self.paused():
            return  # nothing is delivered and nothing rotates; requests keep queuing (and may time out)
        cap = int(self.cfg.get('max_busy') or 0)
        busy = sum(self.agent(w).get('state') in ('busy', 'handoff', 'held') for w in self.workers)
        for name in self.names:
            info = self.agent(name)
            if name != 'supervisor' and cap and busy >= cap:
                continue  # e.g. a local model server that rejects concurrent large requests
            if (info.get('advice_hold') == '1' and info.get('state') == 'starting'
                    and info.get('launch_ready') == '0' and self.inbox_depth(name)):
                self.release_hold(name)  # advisor runs: first sheet decides model / effort / domains
                continue
            if (info.get('state') == 'idle' and self.inbox_depth(name)
                    and time.time() >= float(info.get('retry_at') or 0)):
                self.deliver(name)
                if name != 'supervisor' and self.agent(name).get('state') in ('busy', 'handoff'):
                    busy += 1

    def handoff_limits(self, name):
        """(window or None = measured, ratio, source) for this agent's current runtime model."""
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        model = cfg.get({'claude': 'worker_model', 'pi': 'pi_model'}.get(cli, 'codex_model')) or ''
        limits = handoff_limits(cli, str(model), self.cfg, self.handoff_table, self.worker_handoff.get(name))
        self.setf(name, handoff_window=limits[0] or 'measured', handoff_ratio=limits[1], handoff_source=limits[2])
        return limits

    def usage(self, name):
        override = self.handoff_limits(name)[0]
        if is_pi(self.runtime_cfg(name), name):
            info = self.agent(name)
            return int(info.get('ctx_used') or 0), override or int(info.get('ctx_window') or self.cfg['window'])
        if is_claude(self.runtime_cfg(name), name):
            path = self.agent(name).get('transcript')
            if not path:
                return 0, 0
            used, window = claude_transcript_usage(path, override)
            self.setf(name, ctx_used=used, ctx_window=window)
            return used, window
        thread = self.agent(name).get('thread')
        paths = rollouts(thread, self.sessions) if thread else []
        if not paths:
            return 0, 0
        used, window = read_usage(str(paths[0]), int(self.cfg['window']))
        window = override or window
        self.setf(name, ctx_used=used, ctx_window=window)
        return used, window

    def deliver(self, name):
        info = self.agent(name)
        if info.get('state') != 'idle':
            return
        if self.v2 and not runtime_v2.tui_slot(self.cfg, name) and info.get('bridge_available') != '1':
            return
        pending = self.v2.pending_text(name) if self.v2 else self.r.lrange(key('inbox', name), 0, 0)
        if not pending:
            return
        text = pending[0]
        if name != 'supervisor' and not self.service:
            head = SUPERVISOR_INSTRUCTION_HEAD.search(text)
            if self.v2:
                mid = self.v2.bus.pending(name, 1)[0]
                msg = self.v2.bus.message(mid)
                # v2 stores the task binding as metadata; original profile validator
                # still receives the business instruction at the idle boundary.
                if msg['request_id'] != self.task().get('id'):
                    self.finish('blocked', 'stale task instruction for ' + name)
                    return
                head = SUPERVISOR_INSTRUCTION_HEAD.search('[Supervisor instruction ' + msg['request_id'] + '] ' + text)
                text = '[Supervisor instruction ' + msg['request_id'] + '] ' + text
            if head:
                if head.group(1) != self.task().get('id'):
                    self.finish('blocked', 'stale task instruction for ' + name)
                    return
                try:
                    # Strip only the daemon-generated advisory; reauthorize the
                    # original instruction against this task after queueing/READY/
                    # handoff. Queue and callback wire formats stay unchanged.
                    application = self.prepare_worker_profile(name, text[head.end():])
                except profile_runtime.ProfileRuntimeError as exc:
                    self.finish('blocked', 'profile validation before delivery to ' + name + ': ' + str(exc))
                    return
                self.record_worker_profile(name, application)
                if application.state in ('switching', 'applied-live'):
                    try:
                        registry = AgentRegistry(self.task().get('agent_registry') or None)
                        launch = profile_launch.resolve_launch(registry.get(application.desired_profile), registry, self.cfg)
                        if application.state == 'switching':
                            self.switch_profile(name, launch)
                            return  # never pop/deliver until this generation returns READY
                        if profile_launch.encode_launch(launch) != info.get('runtime_launch'):
                            raise ValueError('profile launch definition changed since spawn; submit a new task after repair')
                    except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
                        self.block_profile(name, str(exc))
                        return
                text = profile_runtime.profile_advisory(name, application) + text[head.start():]
                if (self.cfg.get('advisor') is True and application.state not in ('switching', 'applied-live')
                        and not self.advise_before_delivery(name, text)):
                    return  # respawning / blocked: never pop/deliver until the new generation returns READY
        # Validate profile intent before even the existing context rotation path.
        # Leave the original message queued across handoff; READY triggers another
        # idle-boundary validation. This daemon remains the sole inbox consumer.
        used, window = self.usage(name)
        ratio = float(self.agent(name).get('handoff_ratio') or self.cfg['handoff_ratio'])  # set by usage()
        if subfleet.adopted(self.cfg, name, info.get('generation')):
            window = 0  # an adopted pane goes back to its parent fleet: never replace it with a successor
        if window and used >= ratio * window:
            if int(info.get('delivered') or 0) == 0:
                # A fresh session (only its READY turn so far) is already over the ratio: its
                # baseline (system prompt + AGENTS.md) exceeds the budget, so rotating would spawn
                # successors forever. Deliver anyway and say so once per generation.
                self.log('rotate.impossible', agent=name, used=used, window=window,
                         ratio=ratio, reason='fresh session already over ratio; raise handoff_ratio (roster) or --handoff-ratio')
            elif agent_cli(self.runtime_cfg(name), name) == 'codex' and active_children(info.get('thread'), self.sessions):
                # Hold the message: never feed a near-full session while its subagents run.
                self.log('rotate.deferred', agent=name, reason='native subagents still running')
                return
            else:
                self.start_rotation(name, used, window)
                return
        if self.cfg.get('advisor') is True and self.agent(name).get('advice_hotload'):
            text = self.apply_hotload(name, text)
        if self.v2:
            return self.v2.deliver(name)
        self.r.lpop(key('inbox', name))
        if self.service:
            head = REQ_HEAD.match(text)
            if head and not self.r.hgetall(REQ + head.group(1)):
                self.log('deliver.dropped', agent=name, id=head.group(1), reason='request already finished')
                return
            if name != 'supervisor':
                self.setf(name, request=head.group(1) if head else '')
        self.setf(name, state='busy', busy_since=time.time(), delivered=int(info.get('delivered') or 0) + 1)
        if not self.send(name, self.agent(name)['pane'], text):
            self.r.lpush(key('inbox', name), text)
            self.finish('blocked', 'delivery failed: ' + name)
            return
        self.setf(name, last_msg=text)
        self.log('deliver', agent=name, text=text[:120])

    # -- advisor (start --advisor; advisor plan §3.1, pre-dispatch path)
    def install_advised_kit(self, name, cwd, scope):
        """Advisor runs: Claude CLAUDE.md = role scope + project core + this generation's domain rules;
        cwd/.claude/{skills,agents} cleared, then the domains linked; full kit mcp.json."""
        info = self.agent(name)
        domains = json.loads(info.get('advice_domains') or info.get('installed_domains') or '[]')
        if self.kit:
            domains = [d for d in domains if d in self.kit.domains]
            self.kit.install(cwd, domains)
            (cwd / 'CLAUDE.md').write_text(self.kit.claude_md(scope, domains))
        else:
            kitmod.Kit.clear(cwd)
            domains = []
            (cwd / 'CLAUDE.md').write_text(scope)
        self.setf(name, installed_domains=json.dumps(domains, ensure_ascii=False), advice_domains='')

    def advice_identity(self, name, text):
        if self.v2:
            mids = self.v2.bus.pending(name, 1)
            return mids[0] if mids else ''
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def take_pending_advice(self, name, text):
        """The supervisor dispatch this queued text carries, or None (notices / corrections)."""
        for raw in self.r.lrange(ADVICE_PENDING + name, 0, -1):
            entry = json.loads(raw)
            if entry['instruction'] and entry['instruction'] in text:
                self.r.lrem(ADVICE_PENDING + name, 1, raw)
                return entry
        return None

    def advise(self, name, entry, cli, cfg, kind='dispatch'):
        """One consultation (with one retry). Returns the validated or fallback sheet.

        ``kind`` is reserved for the mid-task rescue path (phase 2: consult / respawn / reassign)."""
        registry = AgentRegistry()
        cards = advisor.launch_cards(registry, cli)
        model, effort = advisor.runtime_model(cfg, cli), advisor.runtime_effort(cfg, cli)
        current = advisor.fingerprint(cli, model, effort)
        models = list(dict.fromkeys(([model] if model else []) + list(cards)))
        whitelist = (cards[model].kit_domains if model in cards else None)
        info = self.agent(name)
        installed = json.loads(info.get('installed_domains') or '[]')
        matrix, matrix_error = advisor.load_matrix()
        catalog = self.kit.domains_catalog() if self.kit and cli == 'claude' else []
        payload = advisor.build_input(
            kind=kind, goal=self.task().get('goal', ''), instruction=entry['instruction'], advise_note=entry.get('note', ''),
            slot={'name': name, 'cli': cli, 'generation': int(info.get('generation') or 0), 'model': model,
                  'effort': effort, 'installed_domains': installed},
            models=models, matrix=matrix, catalog=catalog)

        def check(raw):
            target = raw.get('model')
            wl = cards[target].kit_domains if target in cards else whitelist
            return advisor.validate(raw, cli=cli, models=models, current=current,
                                    catalog=[c['name'] for c in catalog], whitelist=wl, matrix=matrix, cards=cards)
        runner = self.advisor_runner or (lambda data: advisor.run_claude(data, cfg=self.cfg, run_dir=self.run_dir))
        started = time.time()
        sheet, envelope, errors = advisor.consult(payload, runner, check)
        seq = self.r.hincrby(ADVICE_SEQ, 'n', 1)
        ticket = 'adv-' + str(self.cfg.get('token') or self.run_dir.name[-8:]) + '-' + str(seq)
        unavailable = sheet is None
        if unavailable:
            last = json.loads(info.get('advice_last') or '{}')
            if last.get('task') == self.task().get('id') and last.get('sheet', {}).get('model') in models:
                sheet = dict(last['sheet'], reason='advisor unavailable: previous sheet of this task')
            else:  # the slot's running runtime, no domains: never forces a respawn (§8 reading, see report)
                sheet = {'model': model, 'effort': effort, 'domains': [], 'reason': 'advisor unavailable: running runtime, domains []'}
        sheet['ticket'] = ticket
        audit = {'slot': name, 'task': self.task().get('id', ''), 'kind': kind,
                 'input': json.dumps({k: payload[k] for k in ('goal', 'instruction', 'advise_note', 'slot')},
                                     ensure_ascii=False)[:4000],
                 'output': json.dumps(sheet, ensure_ascii=False), 'errors': json.dumps(errors, ensure_ascii=False),
                 'elapsed_s': round(time.time() - started, 3), 'status': 'unavailable' if unavailable else 'ok',
                 'at': time.time()}
        audit.update(advisor.usage_fields(envelope))
        if matrix_error:
            audit['matrix_error'] = matrix_error[:500]
        self.r.hset(ADVICE + ticket, mapping=audit)
        if unavailable:
            self.log('advice.unavailable', agent=name, ticket=ticket, errors=errors)
            self.r.rpush(ADVICE_NOTES, '[advisor unavailable] 按缺省派出 ' + self.label(name) + '（' + sheet['reason'] + '）')
        if sheet.get('capability_gap'):
            self.log('capability.gap', agent=name, ticket=ticket, capability=sheet.get('capability'), model=sheet['model'])
        self.setf(name, advice_last=json.dumps({'task': self.task().get('id', ''), 'sheet': sheet}, ensure_ascii=False))
        return sheet, current, cards, registry

    def advice_launch(self, cli, sheet, cards, registry):
        card = cards[sheet['model']]
        extras = {'effort': sheet['effort']} if cli == 'claude' and sheet.get('effort') else None
        return profile_launch.resolve_launch(card, registry, self.cfg, extras=extras)

    def record_advice(self, name, sheet, effect):
        shown = sheet['model'] + '/' + (sheet.get('effort') or '-') + '/' + (','.join(sheet['domains']) or '-')
        self.setf(name, advice_ticket=sheet['ticket'], advice_reason=sheet['reason'], advice_display=shown)
        self.r.hset(ADVICE + sheet['ticket'], mapping={'effect': effect})
        self.log('advice', agent=name, ticket=sheet['ticket'], model=sheet['model'], effort=sheet.get('effort', ''),
                 domains=sheet['domains'], effect=effect)
        self.r.rpush(ADVICE_NOTES, advisor.notice(self.label(name), sheet, effect))

    def advise_before_delivery(self, name, text):
        """True = go on delivering (no change, or a hot-load queued); False = respawning / blocked."""
        identity = self.advice_identity(name, text)
        info = self.agent(name)
        if identity and info.get('advice_msg') == identity:
            return True  # already advised (e.g. delivered again after the respawn's READY)
        entry = self.take_pending_advice(name, text)
        if entry is None:
            return True
        self.setf(name, advice_msg=identity)
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        sheet, current, cards, registry = self.advise(name, entry, cli, cfg)
        installed = json.loads(info.get('installed_domains') or '[]')
        target = advisor.fingerprint(cli, sheet['model'], sheet.get('effort'))
        effect = advisor.decide(current, target, installed, sheet['domains'])
        try:
            if effect == 'respawn':
                launch = self.advice_launch(cli, sheet, cards, registry)
                self.record_advice(name, sheet, effect)
                self.setf(name, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
                self.switch_profile(name, launch)  # Phase 5 path; blocks the task itself on failure
                return False
            frozen = info.get('runtime_launch')
            if frozen and sheet['model'] in cards and json.loads(frozen).get('profile_id') == cards[sheet['model']].id:
                drift = profile_runtime.launch_drift(frozen, self.advice_launch(cli, sheet, cards, registry))
                if drift:
                    raise ValueError('advisor launch ' + drift + ' changed since spawn; submit a new task after repair')
        except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
            self.block_profile(name, str(exc))
            return False
        if effect == 'hotload':
            new = [d for d in sheet['domains'] if d not in installed]
            self.setf(name, advice_hotload=json.dumps(new, ensure_ascii=False))
        self.record_advice(name, sheet, effect)
        return True

    def apply_hotload(self, name, text):
        """Link the new domains' skills / agents and put their rules in front of this instruction."""
        new = json.loads(self.agent(name).get('advice_hotload') or '[]')
        installed = json.loads(self.agent(name).get('installed_domains') or '[]')
        linked = self.kit.link_domains(agent_cwd(self.cfg, name), new) if self.kit else {'skills': [], 'agents': []}
        prefix = ((self.kit.rules_text(new).lstrip() if self.kit else '')
                  + '\n[本任务领域：' + ', '.join(new) + '；已链入 skill：' + (', '.join(linked['skills']) or '无')
                  + '；agent：' + (', '.join(linked['agents']) or '无') + ']\n\n')
        text = prefix + text
        if self.v2:  # the TUI receives the bus body, so rewrite it before the offer
            from cmux_fleet.message_bus import _json
            mid = self.v2.bus.pending(name, 1)[0]
            body = self.v2.bus.message(mid)['body']
            body['text'] = prefix + body['text']
            self.r.hset(self.v2.bus.msg_key(mid), mapping={'body': _json(body)})
        self.setf(name, advice_hotload='', installed_domains=json.dumps(installed + new, ensure_ascii=False))
        self.log('advice.hotload', agent=name, domains=new, linked=linked)
        return text

    def release_hold(self, name):
        """First dispatch to a held Claude worker: write its launch from the sheet, install, open the gate."""
        if self.v2:
            mid = self.v2.bus.pending(name, 1)[0]
            text = self.v2.bus.message(mid)['body']['text']
        else:
            text = self.r.lrange(key('inbox', name), 0, 0)[0]
        entry = self.take_pending_advice(name, text) or {'instruction': text[:2000], 'note': ''}
        cfg = self.runtime_cfg(name)
        sheet, current, cards, registry = self.advise(name, entry, 'claude', cfg)
        self.setf(name, advice_msg=self.advice_identity(name, text),
                  advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
        try:
            launch = self.advice_launch('claude', sheet, cards, registry) if sheet['model'] in cards else {}
            generation = int(self.agent(name).get('generation') or 0)
            if launch:
                self.setf(name, runtime_launch=profile_launch.encode_launch(launch), runtime_profile=launch['profile_id'],
                          profile_state='switching')
            cfg = self.runtime_cfg(name)
            save(self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json'), cfg)
            cwd = agent_cwd(self.cfg, name)
            scope = self.role_scope(name, cfg, launch)
            (cwd / 'AGENTS.md').write_text(scope)
            self.install_advised_kit(name, cwd, scope)
        except (OSError, ValueError, KeyError) as exc:
            self.setf(name, advice_hold=0)
            self.block_profile(name, 'advisor release failed: ' + str(exc), dead=True)
            return
        self.record_advice(name, sheet, 'launch')  # the held first generation starts with this sheet
        fields = dict(advice_hold=0, launch_ready=1)
        if self.v2:
            fields['startup_deadline'] = time.time() + float(self.cfg['profile_ready_timeout'])
        if launch:
            fields['profile_ready_deadline'] = time.time() + float(self.cfg['profile_ready_timeout'])
        self.setf(name, **fields)
        self.log('advice.released', agent=name, ticket=sheet['ticket'])

    def send(self, name, pane, text):
        """Codex accepts verbatim paste; Claude and Pi use a file pointer with a delivery id."""
        if self.v2:
            raise ValueError('v2 deliveries use Redis authorization; file/paste fallback forbidden')
        if not (is_claude(self.runtime_cfg(name), name) or is_pi(self.runtime_cfg(name), name)):
            return self.mgr.send_prompt_to_idle(pane, text)
        # Claude's Stop hook reads the pointer from its transcript; Pi's extension captures it
        # from before_agent_start. Both bind each turn end to exactly one daemon delivery.
        box = agent_cwd(self.cfg, name) / '.inbox'
        box.mkdir(exist_ok=True)
        mid = 'msg-' + time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        msg = box / (mid + '.md')
        msg.write_text(text)
        self.setf(name, msg_id=mid)
        first = ' '.join(text.split('\n', 1)[0].split())[:200]
        return self.mgr.send_prompt_to_idle(pane, first + ' -- the full message is in ' + str(msg)
                                            + ' . Read that whole file first and follow it.')

    def start_rotation(self, name, used, window):
        info = self.agent(name)
        hid = uuid.uuid4().hex[:10]
        draft = agent_cwd(self.cfg, name) / '.handoff' / ('draft-' + hid + '.md')
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(handoff.partial_template())
        self.setf(name, state='handoff', handoff_id=hid, handoff_draft=draft,
                  handoff_mtime=draft.stat().st_mtime_ns,
                  handoff_deadline=time.time() + float(self.cfg['handoff_timeout']))
        self.log('rotate.start', agent=name, used=used, window=window, generation=info.get('generation'))
        prompt = (handoff.DUMP_PROMPT.format(path=draft, used=used, window=window)
                  + ' This is a same-task handoff: do not open windows, do not continue the task itself.')
        if self.v2:
            self.v2.offer_handoff(name, prompt)
            return
        if not self.send(name, info['pane'], prompt):
            self.finish_rotation(name, timed_out=True)

    def skeleton(self, name, info):
        t = self.task()
        replies = self.r.lrange(key('replies', name), 0, -1)
        pending = self.v2.pending_text(name) if self.v2 else self.r.lrange(key('inbox', name), 0, 0)
        return handoff.build_skeleton(
            info['handoff_id'], SESSION, t.get('goal') or (
                ('resident service: route each request to a worker' if name == 'supervisor'
                 else 'resident service: answer each request as role ' + name) if self.service
                else '(no task yet, standing by)'),
            done_lines=['recent reply: ' + ' '.join(x.split())[:600] for x in replies],
            inprogress='none (handoff happens at an idle boundary)',
            next_lines=['read this note, reply READY, then wait for the daemon to deliver the next message'
                        + (' (pending: ' + ' '.join(pending[0].split())[:300] + ')' if pending else '')],
            pending='none', task=t.get('id', ''), hop=int(info['generation']) + 1,
            key_files=[str(self.workspace()), str(agent_cwd(self.cfg, name) / 'AGENTS.md')],
            risks=['role ' + name + ' and task ' + t.get('id', 'none') + ' are unchanged',
                   'anything the model claims as done must be checked against the artifacts'])

    def finish_rotation(self, name, timed_out=False):
        if self.v2 and timed_out:
            # An unfinished handoff can still run tools. Retire it reliably before
            # using a skeleton or advancing generation; Escape alone proves nothing.
            self.v2.transaction('handoff-timeout-' + uuid.uuid4().hex,
                                lambda: self.v2.hold(name, 'handoff deadline expired'))
        info = self.agent(name)
        generation = int(info['generation'])
        base = self.skeleton(name, info)
        draft = Path(info['handoff_draft'])
        out = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-' + info['handoff_id'] + '.md')
        out.parent.mkdir(parents=True, exist_ok=True)
        source = 'skeleton'
        if not timed_out and draft.exists() and draft.stat().st_mtime_ns > int(info['handoff_mtime']):
            out.write_text(handoff.merge_partial(base, draft.read_text()))
            source = 'model'
            if not handoff.validate(str(out))[0]:
                source = 'skeleton'
        if source == 'skeleton':
            out.write_text(base)  # never fail a rotation because the model did not write
        old = info['pane']
        snapshot = out.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        prev = self.label(name)
        base = SUPERVISOR_LABEL if name == 'supervisor' else name
        label = base + '.' + str(generation + 1)  # supervisor-1 -> supervisor-1.1, worker-1 -> worker-1.1
        self.setf(name, generation=generation + 1, handoff_id='', label=label)
        if name != 'supervisor':
            self.setf(name, prev_label=prev)
            self.r.hset(NAMES, mapping={label: name})  # old names keep routing to this slot
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff=str(out),
                       snapshot=str(snapshot), source=source, timed_out=timed_out)
        new = self.spawn(name, '[Same-task takeover] You are ' + label + ' (taking over from ' + prev
                         + '). Read the handoff note ' + str(out) + ' and continue from it. In this turn only read '
                         'the note, do no task work, and make your final reply exactly READY.',
                         launch_held=bool(self.v2))
        placed = self.mgr.tab_into(new, old)  # the successor takes the retiring session's place on the page
        if (self.v2 and not placed) or not self.mgr.kill_pane(old) or (self.v2 and self.mgr.get_pane_status(old) is not None):
            if self.v2:
                self.setf(name, state='dead', launch_ready=0, retired_pane=old)
            self.finish('blocked', 'could not close the old session ' + prev + '; two sessions hold slot ' + name)
            return
        if self.v2:
            self.v2.confirm_retired(name)
            self.setf(name, launch_ready=1, retired_pane='')
        self.refresh_history()
        self.log('rotate.done', agent=name, label=label, prev=prev, generation=generation + 1,
                 handoff=str(out), source=source, timed_out=timed_out)

    def refresh_history(self):
        history.write_page(self.run_dir, self.history_slots())
        pane = self.r.hget(SERVICE, 'history_pane')
        if pane:
            self.mgr.reload_browser(pane)

    # -- hot roster (start --roster)
    def roster_tick(self):
        path = self.cfg.get('roster')
        if not path:
            return
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        if stamp != self.roster_stamp:
            self.roster_stamp = stamp
            self.reload_roster(path)
        for name in [w for w in self.workers if w not in self.desired]:
            info = self.agent(name)
            # Retire only between turns: no queued work and (v2) no delivery in flight.
            if (info.get('state') in ('idle', 'dead') and not self.inbox_depth(name)
                    and not (self.v2 and self.r.hget(self.v2.bus.lease_key(name), 'current_delivery'))):
                self.retire_worker(name)

    def reload_roster(self, path):
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raw, problem = b'', 'cannot read roster: ' + str(exc)
        else:
            problem = None
        digest = hashlib.sha256(raw).hexdigest() + (problem or '')
        if digest == self.roster_digest:
            return  # touched, not changed
        self.roster_digest = digest
        try:
            if problem:
                raise ValueError(problem)
            workers, roles = roster.parse(raw, Path(path).parent)
            table, per_worker = roster.handoff(raw)
            # A slot name used earlier in this run still has Redis state and a workspace.
            used = [n + ': name was already used in this run (agent state or workspace exists); pick a new name'
                    for n in workers if n not in self.workers
                    and (self.r.exists(key('agent', n)) or (self.workspace() / n).exists())]
            if used:
                raise ValueError('\n'.join(used))
        except ValueError as exc:
            self.log('roster.rejected', level='error', path=str(path), errors=str(exc).split('\n'),
                     kept=self.desired)
            return
        before, old_roles = list(self.desired), dict(self.cfg.get('worker_roles') or {})
        adds = [n for n in workers if n not in self.workers]
        results = {n: 'retire cancelled' for n in workers if n in self.workers and n not in self.desired}
        results.update({n: 'retiring when idle' for n in self.desired if n not in workers})
        results.update({n: 'role changed; applies at its next generation' for n in workers
                        if n in self.workers and roles.get(n) != old_roles.get(n)})
        # Handoff thresholds (§22): next delivery check uses them; retiring workers keep theirs.
        per_worker = dict({w: v for w, v in self.worker_handoff.items() if w in self.workers and w not in workers},
                          **per_worker)
        changed = {n: {'before': self.worker_handoff.get(n) or {}, 'after': per_worker.get(n) or {}}
                   for n in sorted(set(per_worker) | set(self.worker_handoff))
                   if per_worker.get(n) != self.worker_handoff.get(n)}
        if table != self.handoff_table:
            changed['table'] = {'before': self.handoff_table, 'after': table}
        self.handoff_table, self.worker_handoff = table, per_worker
        self.desired = workers
        # Retiring workers keep their role until gone (a handoff successor may still finish work).
        roles = dict({w: old_roles[w] for w in self.workers if w not in workers and w in old_roles}, **roles)
        self.cfg['worker_roles'] = roles
        if adds:
            self.workers = self.workers + adds
            self.names = ['supervisor'] + self.workers
        if adds or roles != old_roles:
            # Hooks and bridges check membership in the config file: write it before spawning.
            self.persist_roster()
        for name in adds:
            results[name] = self.add_worker(name)
        if adds or any(n not in workers for n in before) or any(n not in before for n in workers):
            self.r.hset(ROSTER, mapping={'notify': 1})
        if results or changed:
            self.log('roster.changed', before=before, after=self.desired, results=results, handoff=changed)
        for name in self.names:  # status shows the effective values without waiting for a delivery
            if self.agent(name):
                self.handoff_limits(name)

    def persist_roster(self):
        self.cfg['workers'][:] = self.workers  # in place: the control view holds this list
        fields = {'workers': list(self.workers), 'worker_roles': self.cfg.get('worker_roles') or {}}
        save(self.path, dict(load(self.path), **fields))
        try:
            latest = load(LATEST)
            if Path(latest.get('config') or '').resolve() == self.path:
                save(LATEST, dict(latest, **fields))
        except (OSError, ValueError):
            pass  # latest.json belongs to another run or is missing: nothing to update

    def add_worker(self, name):
        self.init_worker(name)
        try:
            pane = self.spawn_ready(name)
        except (Exception, SystemExit) as exc:
            self.setf(name, state='dead', launch_ready=0, runtime_ready=0)
            return 'spawn failed: ' + str(exc)
        peer = next((p for p in (self.agent(w).get('pane') for w in self.workers if w != name)
                     if p and self.mgr.get_pane_status(p) is not None), None)
        placed = (self.mgr.tab_into(pane, peer, before=False) if peer
                  else self.mgr.place_right_of(pane, self.agent('supervisor').get('pane')))
        self.refresh_history()
        return 'added' if placed else 'added; could not move it into the worker column'

    def retire_worker(self, name):
        info, label = self.agent(name), self.label(name)
        generation, pane = int(info.get('generation') or 0), info.get('pane')
        snapshot = ''
        if pane and self.mgr.get_pane_status(pane) is not None:
            snapshot = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-retired.terminal.txt')
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            self.mgr.capture_to(pane, str(snapshot))
        if not pane:
            ok = True  # never spawned: nothing to close
        elif self.v2:
            ok = self.v2.retire(name, 'removed from roster')  # holds the slot if the close is unconfirmed
        else:
            ok = self.mgr.kill_pane(pane) and self.mgr.get_pane_status(pane) is None
        if not ok:
            self.log('roster.retire_failed', agent=name, pane=pane)
            return
        history.record(self.run_dir, slot=name, label=label, generation=generation, handoff='',
                       snapshot=str(snapshot), source='retired', timed_out=False)
        self.setf(name, state='retired', launch_ready=0, runtime_ready=0)
        self.workers = [w for w in self.workers if w != name]
        self.names = ['supervisor'] + self.workers
        (self.cfg.get('worker_roles') or {}).pop(name, None)
        self.persist_roster()  # its workspace stays: declared artifact paths must still verify
        self.refresh_history()
        self.log('roster.retired', agent=name, label=label, workers=self.workers)

    # -- stall watchdog
    def last_activity(self, info):
        """Newest of: when we handed the agent its message, and its rollout file's mtime."""
        last = float(info.get('busy_since') or 0)
        if info.get('transcript'):  # claude worker: main transcript + its subagent transcripts
            main = Path(info['transcript'])
            for path in [main] + list(main.with_suffix('').glob('**/*.jsonl')):
                try:
                    last = max(last, path.stat().st_mtime)
                except OSError:
                    pass
            return last
        thread = info.get('thread')
        paths = rollouts(thread, self.sessions) if thread else []
        if paths:
            last = max(last, paths[0].stat().st_mtime)
        return last

    def check_stalls(self, now):
        limit = float(self.cfg['stall_timeout'])
        if limit <= 0:
            return
        for name in self.names:
            info = self.agent(name)
            if info.get('state') not in ('busy', 'starting'):
                continue
            last = self.last_activity(info)
            if not last or now - last < limit:
                continue
            self.on_stall(name, info, int(now - last))

    def on_stall(self, name, info, idle_s):
        if self.v2:
            return self.v2.transaction('stall-' + uuid.uuid4().hex,
                lambda: self.v2.hold(name, 'no activity for ' + str(idle_s) + ' s; stop unconfirmed', public_status='stalled'))
        stalls = int(info.get('stalls', 0)) + 1
        self.log('agent.stalled', agent=name, state=info.get('state'), idle_s=idle_s, stalls=stalls)
        self.mgr.send_key(info['pane'], 'Escape')  # abort the turn; an aborted turn fires no notify
        self.setf(name, state='idle', idle_confirmed=0, stalls=stalls, busy_since=time.time())
        if info.get('state') == 'starting':
            self.setf(name, state='dead', runtime_ready=0, current_profile='',
                      profile_state='blocked', profile_reason='startup timed out before READY')
            if self.running():
                self.finish('blocked', name + ' never became ready (' + str(idle_s) + ' s without activity)')
            return
        if not self.running():
            return
        minutes = str(idle_s // 60)
        if self.service and name != 'supervisor':
            rid = info.get('request')
            self.setf(name, request='')
            if rid and self.r.hget(REQ + rid, 'status') == 'dispatched':
                self.publish(rid, 'stalled', worker=self.label(name),
                             error='no activity for ' + minutes + ' min, the turn was aborted')
            return
        if name == 'supervisor':
            if stalls >= 2 or not info.get('last_msg'):
                self.finish('blocked', 'supervisor stalled twice (' + minutes + ' min without activity)')
            else:
                self.r.lpush(key('inbox', name), info['last_msg'])  # one retry of the same message
        else:
            self.report(name, 'STALLED: no activity for ' + minutes + ' min, its turn was aborted. The worker is '
                        'idle again. Dispatch a smaller, concrete instruction, or reply BLOCKED if a human is needed.')

    def write_result(self):
        """Task terminal: write <run>/result.json once per task (start --resume refuses a finished run)."""
        t = {} if self.service else self.task()
        if t.get('status', 'running') == 'running':
            return
        try:
            if load(self.run_dir / 'result.json').get('task_id') == t.get('id', ''):
                return
        except (OSError, ValueError):
            pass
        state = self.contract_state()
        summary = None if state is None else {
            'phase': state['phase'], 'interfaces': {k: e['status'] for k, e in sorted(state['interfaces'].items())},
            'tasks': {m: x['status'] for m, x in sorted(state['tasks'].items())},
            'pending_user_decisions': [dict(d, interface=k) for k, e in sorted(state['interfaces'].items())
                                       for d in e.get('decisions') or [] if d.get('status') == 'pending_user']}
        save_result(self.run_dir, t, contract_summary=summary)

    def tick(self):
        # Each part is guarded: under v2 this runs in the daemon loop, and one failing part (a bad
        # roster, a broken sub-fleet record, a full disk) must not kill the daemon. Bus errors stay fatal.
        for part, run in (('roster', self.roster_tick),  # the only place the roster changes
                          ('result', self.write_result), ('contracts', self.sync_contract_files),
                          ('subfleet', lambda: subfleet.tick(self)),  # side effects, outside any v2 transaction
                          ('escalations', self.escalation_tick), ('notices', self.relay_notices),
                          ('vetoes', self.apply_vetoes)):
            try:
                run()
            except BusError:
                raise
            except Exception as exc:
                self.log('tick.error', part=part, error=repr(exc))
        if self.v2:
            return self.v2.tick()
        now = time.time()
        for name in self.names:
            info = self.agent(name)
            if (info.get('state') == 'starting' and info.get('runtime_profile')
                    and now >= float(info.get('profile_ready_deadline', '0'))):
                self.block_profile(name, 'profile runtime timed out before READY', dead=True)
                continue
            if info.get('state') == 'handoff' and now >= float(info.get('handoff_deadline', 0)):
                self.finish_rotation(name, timed_out=True)
        t = self.task()
        if t.get('status') == 'running' and now >= float(t.get('deadline', 0)):
            self.finish('budget_exhausted', 'time budget exhausted')
        if self.service:
            self.expire_requests(now)
        self.check_stalls(now)
        if now >= self.next_liveness:
            self.next_liveness = now + 30
            for name in self.names:
                pane = self.agent(name).get('pane')
                if pane and self.mgr.get_pane_status(pane) is None:
                    self.setf(name, state='dead', runtime_ready=0, current_profile='',
                              profile_state='blocked', profile_reason='runtime tab was closed')
                    if self.running():
                        self.finish('blocked', name + ' tab was closed')

    def run(self):
        self.mgr.get_or_create_session()
        if self.v2:
            return self.v2.run()
        self.start_all()
        while not self.stopping:
            self.r.set('fx:daemon:beat', str(time.time()))  # liveness for operators: a hung loop stops beating
            item = self.r.blpop(EVENTS, timeout=2)
            if item:
                try:
                    self.handle(json.loads(item[1]))
                except ValueError as exc:
                    self.log('event.bad', error=str(exc))
            self.pump()
            self.tick()


# ------------------------------------------------------------ operator CLI
def save_result(run, task, status=None, reason=None, contract_summary=None):
    """<run>/result.json (atomic): the run is finished. Artifacts are re-checked on disk here."""
    workspace = Path(run) / 'workspace'
    artifacts = json.loads(task.get('artifacts') or '[]')
    extended = task.get('contracts') == '1'
    result = {
        'status': status or task.get('status', ''), 'reason': task.get('reason', '') if reason is None else reason,
        'task_id': task.get('id', ''), 'goal': task.get('goal', ''), 'at': time.time(),
        'artifacts': [dict(a, problems=check_artifacts([a], workspace, extended)) for a in artifacts]}
    if contract_summary:
        result['contracts'] = contract_summary
    save(Path(run) / 'result.json', result)


def start_redis(cfg, tries=30):
    """Start the run's private Redis on its pinned socket and directory. v2 has AOF on, so a
    restart with the same --dir reloads everything written up to the last fsync (about 1 s)."""
    redis_dir = Path(cfg['redis_runtime_dir'])
    subprocess.run([cfg['redis_server'], '--port', '0', '--unixsocket', cfg['redis_socket'],
                    '--unixsocketperm', '700', '--dir', str(redis_dir), '--dbfilename', 'fleet.rdb',
                    '--appendonly', 'yes' if cfg.get('transport_version') == 2 else 'no',
                    '--appendfsync', 'everysec', '--save', '60 1', '--daemonize', 'yes',
                    '--pidfile', str(redis_dir / 'redis.pid'), '--logfile', str(redis_dir / 'redis.log')], check=True)
    for _ in range(tries):
        try:
            return connect(cfg)
        except Exception:
            time.sleep(.1)
    raise SystemExit('private Redis did not become ready')


def daemon_running(run):
    """True while a daemon holds the run's exclusive lock."""
    try:
        with (Path(run) / 'daemon.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    return False


def resume_target(value):
    """The run to reattach for start --resume, or None: 'auto' with nothing unfinished starts a new run."""
    auto = value == 'auto'
    try:
        run = Path(load(LATEST)['run']) if auto else Path(value).expanduser().resolve()
    except (OSError, ValueError, KeyError, TypeError):
        print('--resume auto: no latest run; starting a new run')
        return None
    if run.name == 'config.json':
        run = run.parent
    problem = None
    if (run / 'result.json').exists():
        problem = 'run already finished (result.json exists)'
    else:
        try:
            if load(run / 'config.json').get('transport_version') != 2:
                problem = 'only Redis v2 runs can be resumed'
        except (OSError, ValueError):
            problem = 'no readable config.json'
    if problem and not auto:
        raise SystemExit('cannot resume ' + str(run) + ': ' + problem)
    if problem:
        print('--resume auto: ' + str(run) + ': ' + problem + '; starting a new run')
        return None
    return run


def resume(run, args):
    """start --resume: reattach an unfinished v2 run. False when it turned out finished (auto starts anew)."""
    from cmux_fleet.cmux_manager import CmuxSessionManager, cmux_available
    path = run / 'config.json'
    cfg = load(path)
    try:
        runtime_v2.validate_config(cfg)
    except ValueError as exc:
        raise SystemExit('cannot resume ' + str(run) + ': ' + str(exc)) from exc
    if daemon_running(run):
        print('this run still has a live daemon; nothing to resume:', run)
        return True
    if args.max_attempts is not None:
        cfg['max_attempts'] = args.max_attempts
    if args.roster:  # applied by the normal hot-reload on the daemon's first tick
        roster_path = Path(args.roster).expanduser().resolve()
        try:
            roster.load(roster_path)
        except (OSError, ValueError) as exc:
            raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + str(exc)) from exc
        cfg['roster'] = str(roster_path)
    if getattr(args, 'advisor', False) and args.worker_kit:
        try:
            kitmod.Kit(args.worker_kit)  # refuse a bad kit before provisioning, like a bad roster
        except (OSError, ValueError) as exc:
            raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
    if not cmux_available():
        raise SystemExit('run this inside a cmux terminal: cmux only accepts processes started in its windows')
    try:
        r = connect(cfg)  # only the daemon died: its Redis is still up
    except Exception:
        if not Path(cfg['redis_runtime_dir']).is_dir():
            raise SystemExit('cannot resume ' + str(run) + ': its Redis runtime directory is gone')
        r = start_redis(cfg, tries=300)  # reloads the AOF from the same directory
    task = {} if cfg.get('mode') == 'service' else r.hgetall(TASK)
    if task.get('status', 'running') != 'running':
        save_result(run, task)
        try:
            r.shutdown()
        except Exception:
            pass  # redis closes the socket while acknowledging shutdown
        if args.resume != 'auto':
            raise SystemExit('cannot resume ' + str(run) + ': task already ' + task['status'])
        print('--resume auto: ' + str(run) + ': task already ' + task['status'] + '; starting a new run')
        return False
    save(path, cfg)
    mgr = manager_for(cfg)
    command = [cfg['python'], str(Path(__file__).resolve()), 'daemon', str(path), '--resume']
    caffeinate = shutil.which('caffeinate')
    if caffeinate:
        command = [caffeinate, '-i'] + command
    result = mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(run))
    if not result.success:
        raise SystemExit(result.error)  # Redis stays up: start --resume can be retried
    save(LATEST, dict(cfg, config=str(path)))
    print('fleet resumed (' + cfg.get('mode', 'task') + ' mode). Run directory:', run)
    return True


def daemon(cfg_path, resume=False):
    cfg = load(cfg_path)
    with (Path(cfg['run']) / 'daemon.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # cmux creates a fresh login shell: it does not inherit the start command's
        # CMUX_FLEET_STATE. Pin the registry in config, never fall back for new runs.
        fleet = Fleet(cfg_path, connect(cfg), manager_for(cfg))
        fleet.resume_requested = resume

        def stop(signum, frame):
            fleet.stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        try:
            fleet.run()
        finally:
            if not fleet.v2 or fleet.explicit_stop:
                try:
                    fleet.r.shutdown()
                except Exception:
                    pass  # redis closes the socket while acknowledging shutdown


def start(args):
    from cmux_fleet.cmux_manager import CmuxSessionManager, cmux_available
    if getattr(args, 'max_attempts', None) is not None and args.max_attempts < 1:
        raise SystemExit('--max-attempts must be at least 1')
    if getattr(args, 'resume', None):
        run = resume_target(args.resume)
        if run and resume(run, args):
            return
    if args.transport_version != 2:
        raise SystemExit('new fleet instances require Redis transport v2; v1 is only readable for existing runs')
    try:
        runtime_v2.validate_config(vars(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    roster_path, worker_roles = None, {}
    if args.roster:  # validate before any provisioning
        if args.workers is not None or args.worker_names:
            raise SystemExit('--roster replaces --workers / --worker-names; pass only one of them')
        roster_path = Path(args.roster).expanduser().resolve()
        try:
            workers, worker_roles = roster.load(roster_path)
        except (OSError, ValueError) as exc:
            raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + str(exc)) from exc
    if getattr(args, 'advisor', False) and args.worker_kit:
        try:
            kitmod.Kit(args.worker_kit)  # refuse a bad kit before provisioning, like a bad roster
        except (OSError, ValueError) as exc:
            raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
    if not cmux_available():
        raise SystemExit('run this inside a cmux terminal: cmux only accepts processes started in its windows')
    clis = {args.supervisor_cli, args.worker_cli}
    codex = args.codex or shutil.which('codex')
    pi = args.pi or shutil.which('pi')
    redis_server = args.redis_server or shutil.which('redis-server')
    binaries = {'codex': codex, 'pi': pi, 'claude': args.claude or shutil.which('claude'),
                'redis-server': redis_server}
    missing = [n for n in sorted(clis | {'redis-server'}) if not binaries[n] or not shutil.which(binaries[n])]
    if missing:
        raise SystemExit('binaries not found: ' + ', '.join(missing) + ' (pass the corresponding binary flag)')
    if 'pi' in clis:
        if not args.allow_unsandboxed_pi:
            raise SystemExit('pi has no sandbox: explicitly pass --allow-unsandboxed-pi after reviewing permissions')
        if not args.pi_model:
            raise SystemExit('--pi-model provider/model is required for pi agents')
        version = subprocess.run([pi, '--version'], text=True, capture_output=True, timeout=15, check=True).stdout.strip()
        match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (1, 1, 0):
            raise SystemExit('pi >= 1.1.0 is required (agent_settled); found: ' + version)
    if getattr(args, 'name', None) is not None and not args.name.strip():
        raise SystemExit('--name must not be empty')
    titles = {'agents_title': 'Fleet ' + args.name.strip()} if getattr(args, 'name', None) else {}
    mgr = manager_for(titles)
    if mgr.session_exists():
        raise SystemExit('cmux-fleet tabs already exist; run `stop` and close them with the ✕ first')
    if not roster_path:
        workers = (args.worker_names.split(',') if args.worker_names
                   else ['worker-' + str(i + 1) for i in range(max(1, args.workers or 1))])
    if len(set(workers)) != len(workers) or not all(re.fullmatch(WORKER_NAME_RE, w) and w != 'supervisor'
                                                    for w in workers):
        raise SystemExit('--worker-names: distinct names matching ' + WORKER_NAME_RE + ' (no dots: dots mark generations)')
    if args.roles and not Path(args.roles).expanduser().is_dir():
        raise SystemExit('--roles must be a directory of <agent-name>.md files')
    profiles = [args.supervisor_profile or args.profile] if args.supervisor_cli == 'codex' else []
    if args.worker_cli == 'codex':
        profiles.append(args.worker_profile or args.profile)
    home = Path(args.codex_home).expanduser()
    if profiles:
        prepare_codex_home(home, profiles)
    token = uuid.uuid4().hex[:8]
    try:
        redis_cfg = redis_runtime.prepare(args.redis_runtime_dir, token)
    except (OSError, ValueError) as exc:
        raise SystemExit('cannot prepare private Redis runtime: ' + str(exc)) from exc
    run = Path(args.runs_dir).expanduser() / ('fleet-' + time.strftime('%Y%m%d-%H%M%S-') + token)
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'python': sys.executable, **redis_cfg,
           'cmux_state_dir': str(mgr.meta_path.parent),
           'workers': workers, 'mode': args.mode, 'roles': args.roles, 'max_busy': args.max_busy,
           'request_timeout': args.request_timeout, 'supervisor_profile': args.supervisor_profile,
           'worker_profile': args.worker_profile, 'profile': args.profile, 'codex': codex, 'redis_server': redis_server, 'codex_home': str(home),
           'handoff_ratio': args.handoff_ratio, 'handoff_timeout': args.handoff_timeout,
           'window': args.window, 'stall_timeout': args.stall_timeout,
           'compact_limit': args.compact_limit, 'chrome_script': args.chrome_script,
           'chrome_min_interval': args.chrome_min_interval, 'worker_cli': args.worker_cli,
           'worker_model': args.worker_model, 'worker_kit': args.worker_kit,
           'claude': args.claude or shutil.which('claude'), 'claude_window': args.claude_window,
           'supervisor_cli': args.supervisor_cli, 'pi': pi, 'pi_model': args.pi_model,
           'pi_thinking': args.pi_thinking, 'allow_unsandboxed_pi': args.allow_unsandboxed_pi,
           'pi_agent_dir': args.pi_agent_dir, 'allow_profile_live_switch': args.allow_profile_live_switch,
           'transport_version': args.transport_version, 'allow_development_v2': args.allow_development_v2,
           'max_attempts': args.max_attempts or DEFAULTS['max_attempts'], 'token': token}
    if getattr(args, 'advisor', False):
        cfg['advisor'] = True
    if getattr(args, 'name', None):
        cfg.update(name=args.name.strip(), **titles)
    if roster_path:
        cfg.update(roster=str(roster_path), worker_roles=worker_roles)
    if 'pi' in clis:
        cfg['pi_home'] = str(pi_adapter.prepare_home(run / 'pi-home', args.pi_agent_dir))
    save(run / 'config.json', cfg)
    start_redis(cfg)
    command = [cfg['python'], str(Path(__file__).resolve()), 'daemon', str(run / 'config.json')]
    caffeinate = shutil.which('caffeinate')
    if caffeinate:
        command = [caffeinate, '-i'] + command
    result = mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(run))
    if not result.success:
        connect(cfg).shutdown()
        raise SystemExit(result.error)
    save(LATEST, dict(cfg, config=str(run / 'config.json')))
    print('fleet started (' + args.mode + ' mode): supervisor + ' + str(len(cfg['workers']))
          + ' worker(s) initialising. Run directory:', run)


def request(cfg, event, wait=30):
    import redis as redis_lib
    r = connect(cfg)
    event['request_id'] = uuid.uuid4().hex
    if cfg.get('transport_version', 1) == 2:
        bus = runtime_v2.bus_for(cfg, r)
        try:
            runtime_v2.post_event(bus, event, event['request_id'])
            return runtime_v2.wait_reply(bus, event['request_id'], timeout=wait)
        finally:
            r.close(); r.connection_pool.disconnect()
    r.rpush(EVENTS, json.dumps(event, ensure_ascii=False))
    for _ in range(wait * 10):
        try:
            raw = r.get('fx:reply:' + event['request_id'])
        except redis_lib.ConnectionError:
            if event['kind'] == 'stop':  # the daemon shuts its private redis right after replying
                return {'status': 'stopped'}
            raise
        if raw:
            return json.loads(raw)
        time.sleep(.1)
    return {'status': 'pending', 'request_id': event['request_id']}


def status(cfg):
    if cfg.get('transport_version', 1) == 2:
        from cmux_fleet.control_view import snapshot
        out = snapshot(cfg)
        children = subfleet.tree(cfg)
        if children:
            out['subfleets'] = children  # the whole tree under this fleet
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    r = connect(cfg)
    print(json.dumps({'mode': cfg.get('mode', 'task'), 'task': r.hgetall(TASK), 'service': r.hgetall(SERVICE),
                      'pending_requests': r.llen(REQQ), 'daemon_beat': float(r.get('fx:daemon:beat') or 0),
                      'agents': {n: dict(r.hgetall(key('agent', n)), inbox=r.llen(key('inbox', n)))
                                 for n in ['supervisor'] + cfg['workers']}}, ensure_ascii=False, indent=2))


def print_inspection(args):
    """Print an offline registry, route, or plan without loading fleet config."""
    registry = AgentRegistry(args.registry)
    if args.cmd == 'agents' and args.agents_cmd == 'list':
        result = {'agents': registry.as_list()}
    elif args.cmd == 'agents':
        result = route_task(args.text, registry).to_dict()
    else:
        plan = build_dispatch_plan(args.goal, registry)
        result = plan.to_dict()
        if args.workers:
            workers = [worker.strip() for worker in args.workers.split(',')]
            result['profile_assignments'] = assign_profiles(plan, workers, registry)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog='cmux-fleet', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('start', help='provision the private redis + cmux control surface (inside cmux)')
    s.add_argument('--workers', type=int, help='number of workers worker-1..N (default 1)')
    s.add_argument('--transport-version', type=int, choices=(2,), default=2,
                   help='new runs require Redis v2; existing v1 runs must be drained and replaced, not hot-flipped')
    s.add_argument('--allow-development-v2', action='store_true',
                   help='opt into incomplete v2 acceptance; no production-readiness claim')
    s.add_argument('--profile', default=DEFAULTS['profile'], help='codex config profile for every agent (default: fleet)')
    s.add_argument('--codex', help='codex binary (default: first on PATH)')
    s.add_argument('--redis-server', help='redis-server binary (default: first on PATH)')
    s.add_argument('--redis-runtime-dir', default=str(redis_runtime.DEFAULT_ROOT),
                   help='private Redis runtime root (default: project .runtime/redis; no /tmp fallback)')
    s.add_argument('--runs-dir', default=str(HOME / 'runs'), help='where run directories are created')
    s.add_argument('--codex-home', default=str(HOME / 'codex-home'),
                   help='isolated CODEX_HOME for the agents (login + profile symlinked from yours; no hooks/MCP)')
    s.add_argument('--handoff-ratio', type=float, default=DEFAULTS['handoff_ratio'],
                   help='rotate a session once its context use reaches this share of the window '
                        '(lowest-priority default; roster handoff: overrides per CLI/model, docs/design.md §22)')
    s.add_argument('--handoff-timeout', type=float, default=DEFAULTS['handoff_timeout'],
                   help='seconds to wait for the model to write its handoff before using the skeleton')
    s.add_argument('--window', type=int, default=DEFAULTS['window'],
                   help='context window fallback when the rollout has not reported one yet')
    s.add_argument('--stall-timeout', type=float, default=DEFAULTS['stall_timeout'],
                   help='seconds without rollout activity before a busy agent is treated as stalled (0 = off)')
    s.add_argument('--compact-limit', type=int, help='codex model_auto_compact_token_limit (default: codex decides)')
    s.add_argument('--chrome-script', help='optional browser broker script run by the daemon for sandboxed agents')
    s.add_argument('--chrome-min-interval', type=float, default=DEFAULTS['chrome_min_interval'])
    s.add_argument('--supervisor-cli', choices=('codex', 'pi'), default='codex', help='CLI for the supervisor')
    s.add_argument('--worker-cli', choices=('codex', 'claude', 'pi'), default=DEFAULTS['worker_cli'],
                   help='CLI for workers')
    s.add_argument('--pi', help='pi binary (>= 1.1.0; default: first on PATH)')
    s.add_argument('--pi-model', help='explicit provider/model for all pi agents; no implicit cloud fallback')
    s.add_argument('--pi-thinking', choices=('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'), default='off')
    s.add_argument('--pi-agent-dir', default=os.environ.get('PI_CODING_AGENT_DIR') or str(Path.home() / '.pi/agent'),
                   help='source of auth.json/models.json only, linked into a fresh per-run pi home')
    s.add_argument('--allow-unsandboxed-pi', action='store_true',
                   help='acknowledge that pi tools run with your OS permissions, without a sandbox')
    s.add_argument('--allow-profile-live-switch', action='store_true',
                   help='allow task apply_profiles_live=true to respawn idle workers for assigned profiles')
    s.add_argument('--worker-model', default=DEFAULTS['worker_model'], help='claude workers: --model value')
    s.add_argument('--advisor', action='store_true',
                   help='consult the advisor (official Claude CLI, fable/high) before every dispatch; it picks the '
                        'worker model / effort / domains and authorizes the respawns its sheets need')
    s.add_argument('--worker-kit', help='claude workers: dir with .claude/{agents,skills} and mcp.json copied into each worker cwd')
    s.add_argument('--claude', help='claude binary (default: first on PATH)')
    s.add_argument('--claude-window', type=int, default=DEFAULTS['claude_window'], help='claude models not in the built-in 1M list ([1m]/opus/fable/sonnet): context window (§22)')
    s.add_argument('--mode', choices=('task', 'service'), default=DEFAULTS['mode'],
                   help='task: one goal until COMPLETED; service: resident, handles a stream of requests')
    s.add_argument('--worker-names', help='comma-separated worker names (default worker-1..N), e.g. exit,reentry,rung')
    s.add_argument('--roster', help='worker roster YAML (workers: [{name, role?}]); edits while running add or '
                                    'retire workers. Replaces --workers / --worker-names')
    s.add_argument('--roles', help='dir of <agent-name>.md files appended to that agent\'s AGENTS.md (one prompt per worker)')
    s.add_argument('--supervisor-profile', help='codex profile for the supervisor (default: --profile)')
    s.add_argument('--worker-profile', help='codex profile for codex workers (default: --profile)')
    s.add_argument('--max-busy', type=int, default=DEFAULTS['max_busy'],
                   help='at most this many workers working at once (0 = no limit)')
    s.add_argument('--request-timeout', type=float, default=DEFAULTS['request_timeout'],
                   help='service mode: seconds before an unanswered request is published as timeout')
    s.add_argument('--resume', metavar='RUN_DIR|auto',
                   help='reattach an unfinished v2 run instead of creating one (auto: the run in latest.json; '
                        'a finished or missing run starts a new one). Other start flags except --roster / '
                        '--max-attempts are ignored when a run is reattached')
    s.add_argument('--name', help='human-readable fleet name used in sub-fleet titles (default: task goal or run token)')
    s.add_argument('--max-attempts', type=int,
                   help='times one dispatch/request may run in total across start --resume (default 2)')
    ag = sub.add_parser('agents', help='inspect offline agent profiles without starting or contacting a fleet')
    ag_sub = ag.add_subparsers(dest='agents_cmd', required=True)
    ag_list = ag_sub.add_parser('list', help='list validated agent cards as JSON')
    ag_list.add_argument('--registry', help='agent card directory (default: project or package agents/)')
    ag_list.add_argument('--json', action='store_true', help='JSON output (already the default)')
    ag_match = ag_sub.add_parser('match', help='select an agent profile for task text')
    ag_match.add_argument('text')
    ag_match.add_argument('--registry', help='agent card directory (default: project or package agents/)')
    ag_match.add_argument('--json', action='store_true', help='JSON output (already the default)')
    pl = sub.add_parser('plan', help='build an offline rule-based dispatch plan as JSON')
    pl.add_argument('goal')
    pl.add_argument('--registry', help='agent card directory (default: project or package agents/)')
    pl.add_argument('--workers', help='comma-separated stable worker slots; include profile assignments when supplied')
    pl.add_argument('--json', action='store_true', help='JSON output (already the default)')
    sf = sub.add_parser('subfleet', help='sub-fleets (operator/test entry; the daemon executes it)')
    sf_sub = sf.add_subparsers(dest='subfleet_cmd', required=True)
    sp = sf_sub.add_parser('spawn', help='hand a worker pane over as supervisor of a new sub-fleet')
    sp.add_argument('--slot', required=True, help='worker slot of the owning fleet, e.g. worker-1')
    sp.add_argument('--task-id', required=True, help='M<n> under the main fleet, M0.<n> under sub-fleet M0')
    sp.add_argument('--goal', required=True)
    sp.add_argument('--workers', type=int, default=2)
    sp.add_argument('--output', help='absolute path of the deliverable the parent expects')
    sp.add_argument('--config', default=str(LATEST))
    dm = sub.add_parser('daemon', help='run the coordinator (internal)')
    dm.add_argument('cfg')
    dm.add_argument('--resume', action='store_true', help='reconcile and replay (internal; from start --resume)')
    t = sub.add_parser(
        'task', help='submit a goal: JSON file or - for stdin '
        '({"goal":..., "hours":..., "artifacts":[...], "auto_agents":true, "agent_registry":...})')
    t.add_argument('spec')
    for name in ('status', 'stop', 'resume'):
        sub.add_parser(name).add_argument('--config', default=str(LATEST))
    ls = sub.add_parser('logs', help='read-only v2 audit timeline/details')
    ls.add_argument('--config', default=str(LATEST))
    for field in ('request', 'slot', 'event', 'level'):
        ls.add_argument('--' + field)
    ls.add_argument('--generation', type=int)
    ls.add_argument('--after', default='0-0', help='resume after this Redis audit Stream ID')
    ls.add_argument('--limit', type=int, default=100, help='maximum records per read/export page')
    ls.add_argument('--json', action='store_true')
    ls.add_argument('--follow', action='store_true')
    q = sub.add_parser('request', help='service mode: submit a request (JSON file or - for stdin)')
    q.add_argument('spec')
    q.add_argument('--config', default=str(LATEST))
    ps = sub.add_parser('pause', help='service mode: stop delivering and rotating')
    ps.add_argument('--reason', default='paused by operator')
    ps.add_argument('--config', default=str(LATEST))
    rs = sub.add_parser('results', help='service mode: print finished results (oldest first)')
    rs.add_argument('--drain', action='store_true', help='remove the printed results from the list')
    rs.add_argument('--config', default=str(LATEST))
    t.add_argument('--config', default=str(LATEST))
    n = sub.add_parser('notify', help='codex notify hook (internal)')
    for arg in ('cfg', 'agent', 'generation', 'payload'):
        n.add_argument(arg)
    c = sub.add_parser('chrome', help='run a vetted browser verb through the daemon (sandboxed agents)')
    c.add_argument('--config', default=str(LATEST))
    c.add_argument('argv', nargs=argparse.REMAINDER)
    c = sub.add_parser('peer', help='send a peer notice to a worker through the daemon (contract tasks, §21)')
    c.add_argument('--config', default=str(LATEST))
    c.add_argument('--from', dest='sender', required=True)
    c.add_argument('--to', required=True)
    c.add_argument('--iface', required=True)
    c.add_argument('--text-file', required=True)
    h = sub.add_parser('claude-stop', help='Claude Code Stop hook (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('pi-event', help='Pi extension callback (internal; event JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    a = sub.add_parser('actor', help='exec the configured CLI for one agent (internal)')
    for arg in ('cfg', 'agent', 'prompt'):
        a.add_argument(arg)
    a.add_argument('--generation', type=int, help='pinned spawn generation (internal)')
    args = p.parse_args(argv)
    if args.cmd == 'start':
        start(args)
    elif args.cmd == 'daemon':
        daemon(args.cfg, args.resume)
    elif args.cmd == 'notify':
        notify(args.cfg, args.agent, args.generation, args.payload)
    elif args.cmd == 'claude-stop':
        claude_stop(args.cfg, args.agent, args.generation, sys.stdin.read())
    elif args.cmd == 'pi-event':
        pi_event(args.cfg, args.agent, args.generation, sys.stdin.read(1000001))
    elif args.cmd == 'actor':
        actor(args.cfg, args.agent, args.prompt, args.generation)
    elif args.cmd in ('agents', 'plan'):
        print_inspection(args)
    else:
        cfg = load(args.config)
        if args.cmd == 'logs':
            from cmux_fleet.control_view import iter_events
            for record in iter_events(cfg, after=args.after, limit=args.limit,
                                      request_id=args.request, slot=args.slot,
                                      generation=args.generation, event=args.event, level=args.level,
                                      follow=args.follow):
                print(json.dumps(record, ensure_ascii=False) if args.json else
                      runtime_v2.sanitize_text(json.dumps(record, ensure_ascii=False)), flush=True)
        elif args.cmd == 'status':
            status(cfg)
        elif args.cmd == 'subfleet':
            print(json.dumps(subfleet.cli_spawn(cfg, args), ensure_ascii=False))
        elif args.cmd == 'chrome':
            ans = request(cfg, {'kind': 'chrome', 'argv': args.argv}, wait=150)
            print(ans.get('out') or ans.get('reason') or json.dumps(ans, ensure_ascii=False))
            sys.exit(ans.get('rc', 1) if ans.get('status') == 'ok' else 2)
        elif args.cmd == 'peer':
            ans = request(cfg, {'kind': 'peer', 'from': args.sender, 'to': args.to, 'iface': args.iface,
                                'text_file': str(Path(args.text_file).resolve())})
            ok = ans.get('status') == 'ok'
            print('peer: delivered to ' + ', '.join(ans['delivered']) if ok else
                  ans.get('reason') or json.dumps(ans, ensure_ascii=False))
            sys.exit(0 if ok else 2)
        elif args.cmd == 'stop':
            print(json.dumps(request(cfg, {'kind': 'stop'}), ensure_ascii=False))
        elif args.cmd in ('pause', 'resume'):
            ev = {'kind': args.cmd, 'reason': getattr(args, 'reason', '')}
            print(json.dumps(request(cfg, ev), ensure_ascii=False))
        elif args.cmd == 'results':
            r = connect(cfg)
            while True:
                raw = r.lpop(RESULTS) if args.drain else None
                if not args.drain:
                    for raw in r.lrange(RESULTS, 0, -1):
                        print(raw)
                    break
                if not raw:
                    break
                print(raw)
        elif args.cmd == 'request':
            spec = json.loads(sys.stdin.read() if args.spec == '-' else Path(args.spec).read_text())
            print(json.dumps(request(cfg, {'kind': 'request', 'spec': spec}), ensure_ascii=False))
        else:
            spec = json.loads(sys.stdin.read() if args.spec == '-' else Path(args.spec).read_text())
            print(json.dumps(request(cfg, {'kind': 'task', 'spec': spec}), ensure_ascii=False))


if __name__ == '__main__':
    main()
