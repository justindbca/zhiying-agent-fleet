#!/usr/bin/env python3
"""cmux-fleet: one supervisor + N worker Codex sessions in cmux tabs, coordinated by a daemon.

Every agent is an interactive ``codex`` TUI in its own cmux tab. The daemon never reads
their screens: Codex's ``notify`` hook calls back into this file (``fleet.py notify``) at
the end of every turn, and the daemon routes the reply through a private Redis instance
that listens only on a Unix socket under /tmp (never localhost:6379).

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
  notify    Codex notify hook (internal)
  claude-stop  Claude Code Stop hook for claude workers (internal)
  actor     exec codex for one agent (internal)
"""
import argparse
import fcntl
import hashlib
import json
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
from cmux_fleet import handoff, history  # noqa: E402
from cmux_fleet.handoff import read_usage  # noqa: E402

SOCK_PREFIX = '/tmp/cmux-fleet-'
SESSION = 'cmux-fleet'  # cmux registry name
HOME = Path(os.environ.get('CMUX_FLEET_HOME') or (Path.home() / '.cmux-fleet'))
LATEST = HOME / 'latest.json'
USER_CODEX_HOME = Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex'))
EVENTS, TASK, LOG, NAMES = 'fx:events', 'fx:task', 'fx:log', 'fx:names'
# Service mode: a resident fleet that handles a stream of requests instead of one goal.
REQ, REQQ, RESULT, RESULTS, SERVICE = 'fx:req:', 'fx:reqq', 'fx:result:', 'fx:results', 'fx:service'
REQ_ID_RE = r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}'
REQ_HEAD = re.compile(r'^\[Request (' + REQ_ID_RE + r')\]')
WORKER_NAME_RE = r'[a-z][a-z0-9-]{0,30}'
# Display name of the supervisor's first generation; successors are supervisor-1.1, supervisor-1.2, ...
# (same <slot>.<generation> pattern as worker-1 -> worker-1.1). Redis keys and the cwd stay 'supervisor'.
SUPERVISOR_LABEL = 'supervisor-1'
RESULT_TTL = 7 * 86400
# Sandboxed agents cannot drive the user's browser themselves (and must never be allowed
# to kill it). If the operator supplies a broker script, the daemon runs these verbs for them.
CHROME_VERBS = {'check', 'tabs', 'newtab', 'go', 'url', 'js'}
THREAD_RE = r'[0-9a-f-]{36}'
DEFAULTS = {'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 100000, 'stall_timeout': 1800,
            'chrome_min_interval': 20, 'profile': 'fleet', 'worker_cli': 'codex', 'worker_model': 'sonnet',
            'claude_window': 200000, 'mode': 'task', 'max_busy': 0, 'request_timeout': 900}
CLAUDE_JSON = Path.home() / '.claude.json'


def load(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


def connect(cfg):
    import redis
    sock = Path(cfg['redis_socket'])
    if not sock.is_absolute() or not str(sock).startswith(SOCK_PREFIX):
        raise ValueError('private cmux-fleet Unix socket required')
    client = redis.Redis(unix_socket_path=str(sock), db=0, decode_responses=True,
                         socket_connect_timeout=3, socket_timeout=5)
    client.ping()  # Fail closed: never fall back to localhost:6379.
    return client


def agent_cwd(cfg, name):
    run = Path(cfg['run'])
    return run / 'agents' / name if name == 'supervisor' else run / 'workspace' / name


def key(kind, name):
    return 'fx:' + kind + ':' + name


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


def is_claude(cfg, name):
    return name != 'supervisor' and cfg.get('worker_cli') == 'claude'


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
    cfg = load(cfg_path)
    r = connect(cfg)
    # No hook-side de-dup: the Stop payload carries no turn id and the transcript may lag the final
    # message. The daemon drops a turn for a claude worker that is already idle (see on_turn).
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('session_id'), 'transcript': data.get('transcript_path', ''),
                                'msg_id': msg_id, 'text': text, 'at': time.time()}, ensure_ascii=False))
    return True


# ---------------------------------------------------------------- trust gate
def validate_artifacts(artifacts):
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


def check_artifacts(artifacts, root):
    """Literal file checks only (exists / size / contains); returns failures (empty = pass)."""
    root = Path(root).resolve()
    failures = []
    for item in artifacts:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root):
            failures.append('outside workspace: ' + item['path'])
        elif not path.is_file():
            failures.append('artifact not written: ' + item['path'])
        elif path.stat().st_size > 32_000_000:
            failures.append('artifact too large to check: ' + item['path'])
        else:
            text = path.read_text(errors='replace')
            if len(text) < item.get('min_chars', 1) or not all(c in text for c in item.get('contains', [])):
                failures.append('artifact below requirements (length / required text): ' + item['path'])
    return failures


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
    if 'artifacts' in decision:
        validate_artifacts(decision['artifacts'])
    if decision['status'] == 'RUNNING' and not dispatch and not others_active:
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
            if _is_subagent(meta) or meta.get('cwd') != str(cwd):
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
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('thread-id') or data.get('thread_id'),
                                'text': data.get('last-assistant-message') or data.get('last_assistant_message') or '',
                                'at': time.time()}, ensure_ascii=False))
    return True


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
            '-c', 'notify=' + json.dumps(callback),
            '-c', 'developer_instructions=' + json.dumps(
                'Your role, rules and reply format are in AGENTS.md in the current directory. Follow it.')]
    if cfg.get('compact_limit'):
        # The daemon rotates sessions at handoff_ratio before each delivery; Codex's own
        # compaction is only a safety net for a single runaway turn.
        argv += ['-c', 'model_auto_compact_token_limit=' + str(int(cfg['compact_limit']))]
    return argv + [prompt]


def claude_argv(cfg_path, cfg, name, generation, prompt):
    cwd = agent_cwd(cfg, name)
    hook = shlex.join([cfg['python'], str(Path(__file__).resolve()), 'claude-stop', str(cfg_path), name, str(generation)])
    settings = cwd / '.fleet-settings.json'
    settings.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': hook, 'timeout': 30}]}]}}))
    argv = [cfg.get('claude') or 'claude', '--dangerously-skip-permissions', '--model', cfg.get('worker_model') or 'sonnet',
            '--settings', str(settings), '--add-dir', str(Path(cfg['run']) / 'workspace')]
    if (cwd / 'mcp.json').exists():
        argv += ['--mcp-config', str(cwd / 'mcp.json')]
    return argv + ['--', prompt]  # --add-dir / --mcp-config are variadic: end options first


def actor(cfg_path, name, prompt):
    cfg = load(cfg_path)
    generation = int(connect(cfg).hget(key('agent', name), 'generation') or 0)
    os.chdir(agent_cwd(cfg, name))
    if is_claude(cfg, name):
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
        self.run_dir = Path(self.cfg['run'])
        self.r, self.mgr = r, mgr
        self.sessions = Path(sessions) if sessions else sessions_dir(self.cfg)
        self.workers = list(self.cfg['workers'])
        self.names = ['supervisor'] + self.workers
        self.service = self.cfg.get('mode') == 'service'
        self.stopping = False
        self.next_liveness = 0
        self.last_chrome = 0

    # -- small helpers
    def agent(self, name):
        return self.r.hgetall(key('agent', name))

    def setf(self, name, **fields):
        self.r.hset(key('agent', name), mapping={k: str(v) for k, v in fields.items()})

    def task(self):
        return self.r.hgetall(TASK)

    # Each handoff successor is named slot.generation (worker-1 -> worker-1.1 -> worker-1.2);
    # the slot (redis key, cwd, notify argv) stays fixed so declared artifact paths survive.
    def label(self, slot):
        return self.agent(slot).get('label') or slot

    def labels(self):
        return [self.label(w) for w in self.workers]

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
        self.r.rpush(LOG, json.dumps(dict(time=time.time(), kind=kind, **data), ensure_ascii=False))
        self.r.ltrim(LOG, -2000, -1)
        print(time.strftime('%H:%M:%S'), kind, json.dumps(data, ensure_ascii=False)[:180], flush=True)

    def queue(self, name, text):
        self.r.rpush(key('inbox', name), text)

    def workspace(self):
        return self.run_dir / 'workspace'

    def fleet_cmd(self):
        return shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'chrome', '--config', str(self.path)])

    # -- actors
    def spawn(self, name, prompt):
        cwd = agent_cwd(self.cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        role = 'supervisor' if name == 'supervisor' else 'worker'
        scope = (HERE / 'agents' / (('service_' if self.service else '') + role + '.md')).read_text()
        extra = Path(self.cfg['roles']).expanduser() / (name + '.md') if self.cfg.get('roles') else None
        if extra and extra.is_file():  # this agent's own prompt (one per worker in service mode)
            scope += '\n## 4. Your role (' + name + ')\n\n' + extra.read_text()
        scope = scope.replace('{{WORKSPACE}}', str(self.workspace())).replace('{{FLEET_CHROME}}', self.fleet_cmd())
        (cwd / 'AGENTS.md').write_text(scope)
        if is_claude(self.cfg, name):
            (cwd / 'CLAUDE.md').write_text(scope)  # Claude Code reads CLAUDE.md, not AGENTS.md
            install_kit(self.cfg.get('worker_kit'), cwd)
            trust_claude_dir(cwd)
        else:
            trust_agent_dir(codex_home(self.cfg), cwd)
        generation = int(self.agent(name).get('generation', 0))
        self.setf(name, role=role, generation=generation, state='starting', thread='', pane='',
                  busy_since=time.time(), delivered=0)
        prompt_file = self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.md')
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt)
        result = self.mgr.spawn_interactive_cli(
            window_name='fx-' + self.label(name), prompt_file=str(prompt_file), start_directory=str(cwd),
            cli_command=shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'actor', str(self.path), name]),
            output_file=str(self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.terminal.log')),
            auto_close=False, metadata={'type': role, 'cli_type': 'claude' if is_claude(self.cfg, name) else 'codex',
                      'profile': self.cfg['profile']},
            env_vars={'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': str(codex_home(self.cfg))})
        if not result.success:
            raise RuntimeError('spawn failed: ' + name + ': ' + str(result.error))
        self.setf(name, pane=result.pane_id)
        self.log('spawn', agent=name, generation=generation, pane=result.pane_id)
        return result.pane_id

    def start_all(self):
        self.setf('supervisor', label=SUPERVISOR_LABEL)
        for name in self.workers:
            self.setf(name, label=name)
            self.r.hset(NAMES, mapping={name: name})
        for name in self.names:
            self.spawn(name, 'You are fleet agent ' + name + '. Read AGENTS.md in the current directory first. '
                             'There is no task yet: your final reply must be exactly READY.')
        history.write_page(self.run_dir, self.history_slots())
        # One page: supervisor on the left above the history browser, workers stacked on the right.
        self.history_pane = self.mgr.arrange(self.agent('supervisor')['pane'],
                                             [self.agent(w)['pane'] for w in self.workers],
                                             history.page_url(self.run_dir))
        self.r.hset(SERVICE, mapping={'history_pane': self.history_pane or ''})

    def history_slots(self):
        return [(n, self.label(n), SUPERVISOR_LABEL if n == 'supervisor' else n) for n in self.names]

    # -- events
    def handle(self, ev):
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

    def on_turn(self, ev):
        name, info = ev.get('agent'), self.agent(ev.get('agent', ''))
        if not info or ev.get('generation') != int(info.get('generation', 0)):
            self.log('turn.stale', agent=name, generation=ev.get('generation'))
            return
        if is_claude(self.cfg, name) and info.get('state') in ('busy', 'handoff', 'idle'):
            # A claude turn end counts only if it answers the message delivered last, once. (The spawn /
            # takeover prompt is a CLI argument without a msg id: that turn is accepted in 'starting'.)
            if info.get('state') == 'idle' or ev.get('msg_id') != info.get('msg_id') or info.get('msg_done') == info.get('msg_id'):
                self.log('turn.duplicate', agent=name, msg_id=ev.get('msg_id'), current=info.get('msg_id'))
                return
            self.setf(name, msg_done=ev.get('msg_id'))
        if ev.get('thread'):
            self.setf(name, thread=ev['thread'])
        if ev.get('transcript'):
            self.setf(name, transcript=ev['transcript'])
        if info.get('state') == 'handoff':
            self.finish_rotation(name)
            return
        was = info.get('state')
        self.setf(name, state='idle', stalls=0)
        if was == 'starting':  # READY after spawn / takeover
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
        self.queue('supervisor', tag + '\n' + text + '\n\nReply with the JSON format from AGENTS.md.')

    def others_active(self):
        return any(self.agent(w).get('state') != 'idle' or self.r.llen(key('inbox', w)) for w in self.workers)

    def on_supervisor(self, text):
        t = self.task()
        try:
            decision = parse_decision(text, self.labels() + list(self.r.hgetall(NAMES)), self.others_active())
        except (ValueError, TypeError, KeyError) as exc:
            bad = self.r.hincrby(TASK, 'bad_replies', 1)
            if bad >= 3:
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
            failures = (check_artifacts(artifacts, self.workspace()) if artifacts
                        else ['no artifacts were declared, nothing to verify'])
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
        for item in decision['dispatch']:
            slot = self.resolve(item['to'])
            self.queue(slot, '[Supervisor instruction ' + t.get('id', '') + '] ' + item['instruction'].strip()
                       + '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                       'subdirectory ' + slot + '/). Reply in the AGENTS.md format when done.')
        self.log('dispatch', count=len(decision['dispatch']), completion=decision['completion'])

    def submit(self, spec):
        if not isinstance(spec, dict) or not isinstance(spec.get('goal'), str) or not spec['goal'].strip():
            raise ValueError('a task needs a goal')
        if self.service:
            raise ValueError('this fleet runs in service mode: submit requests, not tasks')
        if self.running():
            raise ValueError('a task is already running; stop it or wait for it to finish')
        if 'artifacts' in spec:
            validate_artifacts(spec['artifacts'])
        hours = spec.get('hours', 16)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours < 1000:
            raise ValueError('hours must be a positive number')
        task_id = 'task-' + uuid.uuid4().hex[:10]
        self.r.delete(TASK)
        self.r.hset(TASK, mapping={'id': task_id, 'goal': spec['goal'], 'status': 'running', 'completion': 0,
                                   'reason': '', 'deadline': time.time() + hours * 3600, 'bad_replies': 0,
                                   'bounces': 0, 'artifacts': json.dumps(spec.get('artifacts', []), ensure_ascii=False)})
        self.queue('supervisor', '[New task ' + task_id + '] Goal: ' + spec['goal']
                   + '\nWorkers you can dispatch to: ' + ', '.join(self.labels())
                   + '\nShared output directory: ' + str(self.workspace())
                   + ' (artifact paths are relative to it, e.g. worker-1/report.md)'
                   + '\nArtifacts pre-declared by the operator: ' + json.dumps(spec.get('artifacts', []), ensure_ascii=False)
                   + '\nReply with the JSON format from AGENTS.md.')
        self.log('task.submitted', task=task_id)
        return task_id

    def finish(self, status, reason):
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
                self.setf(name, state='idle')

    # -- service mode
    def request(self, spec):
        if not self.service:
            raise ValueError('requests need a fleet started with --mode service')
        validate_request(spec, self.labels() + list(self.r.hgetall(NAMES)))
        rid = spec['id']
        if self.r.hgetall(REQ + rid) or self.r.get(RESULT + rid):
            raise ValueError('duplicate request id: ' + rid)
        self.r.hset(REQ + rid, mapping={'spec': json.dumps(spec, ensure_ascii=False), 'status': 'routing',
                                        'created': time.time(), 'attempts': 0, 'worker': ''})
        self.r.rpush(REQQ, rid)
        if spec.get('to'):
            self.assign(rid, self.resolve(spec['to']), spec.get('instruction'))
        else:
            self.queue('supervisor', '[Request ' + rid + '] A new request needs routing.'
                       + ('\nNote from the sender: ' + spec['instruction'] if spec.get('instruction') else '')
                       + '\nWorkers: ' + ', '.join(self.labels()) + '\nPayload (JSON):\n'
                       + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                       + '\nReply with the service decision JSON from AGENTS.md.')
        self.log('request', id=rid, to=spec.get('to') or 'supervisor')
        return rid

    def assign(self, rid, slot, instruction=None):
        spec = json.loads(self.r.hget(REQ + rid, 'spec'))
        self.r.hset(REQ + rid, mapping={'status': 'dispatched', 'worker': slot})
        self.queue(slot, self.request_message(rid, spec, instruction))

    def request_message(self, rid, spec, instruction=None, prefix=''):
        reply = spec['reply']
        keys = ', '.join(reply['required'])
        if reply.get('allowed') is not None:
            keys += ' (allowed keys: ' + ', '.join(reply['allowed']) + ')'
        return ('[Request ' + rid + '] ' + prefix + (instruction or 'Handle this request as your role describes.').strip()
                + '\nPayload (JSON):\n' + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                + '\nYour final reply must be exactly one JSON object with the keys: ' + keys
                + '. No text outside the JSON object.')

    def publish(self, rid, status, **fields):
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
            decision = parse_service_decision(text, self.labels() + list(self.r.hgetall(NAMES)))
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
        if self.service and self.paused():
            return  # nothing is delivered and nothing rotates; requests keep queuing (and may time out)
        cap = int(self.cfg.get('max_busy') or 0)
        busy = sum(self.agent(w).get('state') in ('busy', 'handoff') for w in self.workers)
        for name in self.names:
            info = self.agent(name)
            if name != 'supervisor' and cap and busy >= cap:
                continue  # e.g. a local model server that rejects concurrent large requests
            if (info.get('state') == 'idle' and self.r.llen(key('inbox', name))
                    and time.time() >= float(info.get('retry_at') or 0)):
                self.deliver(name)
                if name != 'supervisor' and self.agent(name).get('state') in ('busy', 'handoff'):
                    busy += 1

    def usage(self, name):
        if is_claude(self.cfg, name):
            path = self.agent(name).get('transcript')
            if not path:
                return 0, 0
            used, window = claude_transcript_usage(path, int(self.cfg['claude_window']))
            self.setf(name, ctx_used=used, ctx_window=window)
            return used, window
        thread = self.agent(name).get('thread')
        paths = rollouts(thread, self.sessions) if thread else []
        if not paths:
            return 0, 0
        used, window = read_usage(str(paths[0]), int(self.cfg['window']))
        self.setf(name, ctx_used=used, ctx_window=window)
        return used, window

    def deliver(self, name):
        used, window = self.usage(name)
        info = self.agent(name)
        if window and used >= float(self.cfg['handoff_ratio']) * window:
            if int(info.get('delivered') or 0) == 0:
                # A fresh session (only its READY turn so far) is already over the ratio: its
                # baseline (system prompt + AGENTS.md) exceeds the budget, so rotating would spawn
                # successors forever. Deliver anyway and say so once per generation.
                self.log('rotate.impossible', agent=name, used=used, window=window,
                         ratio=self.cfg['handoff_ratio'], reason='fresh session already over ratio; raise --handoff-ratio')
            elif active_children(info.get('thread'), self.sessions):
                # Hold the message: never feed a near-full session while its subagents run.
                self.log('rotate.deferred', agent=name, reason='native subagents still running')
                return
            else:
                self.start_rotation(name, used, window)
                return
        text = self.r.lpop(key('inbox', name))
        if not text:
            return
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

    def send(self, name, pane, text):
        """Codex takes the message pasted verbatim. Claude Code would submit a pasted newline as
        Enter, so a claude worker gets the full message as a file and a one-line pointer."""
        if not is_claude(self.cfg, name):
            return self.mgr.send_prompt_to_idle(pane, text)
        # Every message to a claude worker goes through a uniquely named file. The Stop hook reads the
        # id of the last such pointer from the transcript, which binds each turn end to its delivery.
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
        if not self.send(name, info['pane'], prompt):
            self.finish_rotation(name, timed_out=True)

    def skeleton(self, name, info):
        t = self.task()
        replies = self.r.lrange(key('replies', name), 0, -1)
        pending = self.r.lrange(key('inbox', name), 0, 0)
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
                         'the note, do no task work, and make your final reply exactly READY.')
        self.mgr.tab_into(new, old)  # the successor takes the retiring session's place on the page
        if not self.mgr.kill_pane(old):
            self.finish('blocked', 'could not close the old session ' + prev + '; two sessions hold slot ' + name)
            return
        self.refresh_history()
        self.log('rotate.done', agent=name, label=label, prev=prev, generation=generation + 1,
                 handoff=str(out), source=source, timed_out=timed_out)

    def refresh_history(self):
        history.write_page(self.run_dir, self.history_slots())
        pane = self.r.hget(SERVICE, 'history_pane')
        if pane:
            self.mgr.reload_browser(pane)

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
        stalls = int(info.get('stalls', 0)) + 1
        self.log('agent.stalled', agent=name, state=info.get('state'), idle_s=idle_s, stalls=stalls)
        self.mgr.send_key(info['pane'], 'Escape')  # abort the turn; an aborted turn fires no notify
        self.setf(name, state='idle', stalls=stalls, busy_since=time.time())
        if info.get('state') == 'starting':
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

    def tick(self):
        now = time.time()
        for name in self.names:
            info = self.agent(name)
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
                    self.setf(name, state='dead')
                    if self.running():
                        self.finish('blocked', name + ' tab was closed')

    def run(self):
        self.mgr.get_or_create_session()
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
def daemon(cfg_path):
    from cmux_fleet.cmux_manager import CmuxSessionManager
    cfg = load(cfg_path)
    with (Path(cfg['run']) / 'daemon.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fleet = Fleet(cfg_path, connect(cfg), CmuxSessionManager(SESSION))

        def stop(signum, frame):
            fleet.stopping = True
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        try:
            fleet.run()
        finally:
            try:
                fleet.r.shutdown()
            except Exception:
                pass  # redis closes the socket while acknowledging shutdown


def start(args):
    from cmux_fleet.cmux_manager import CmuxSessionManager, cmux_available
    if not cmux_available():
        raise SystemExit('run this inside a cmux terminal: cmux only accepts processes started in its windows')
    codex = args.codex or shutil.which('codex')
    redis_server = args.redis_server or shutil.which('redis-server')
    missing = [n for n, p in (('codex', codex), ('redis-server', redis_server)) if not p]
    if args.worker_cli == 'claude' and not (args.claude or shutil.which('claude')):
        missing.append('claude')
    if missing:
        raise SystemExit('not found on PATH: ' + ', '.join(missing) + ' (pass --codex / --redis-server)')
    mgr = CmuxSessionManager(SESSION)
    if mgr.session_exists():
        raise SystemExit('cmux-fleet tabs already exist; run `stop` and close them with the ✕ first')
    workers = (args.worker_names.split(',') if args.worker_names
               else ['worker-' + str(i + 1) for i in range(max(1, args.workers))])
    if len(set(workers)) != len(workers) or not all(re.fullmatch(WORKER_NAME_RE, w) and w != 'supervisor'
                                                    for w in workers):
        raise SystemExit('--worker-names: distinct names matching ' + WORKER_NAME_RE + ' (no dots: dots mark generations)')
    if args.roles and not Path(args.roles).expanduser().is_dir():
        raise SystemExit('--roles must be a directory of <agent-name>.md files')
    profiles = [args.supervisor_profile or args.profile]
    if args.worker_cli == 'codex':
        profiles.append(args.worker_profile or args.profile)
    home = prepare_codex_home(Path(args.codex_home).expanduser(), profiles)
    token = uuid.uuid4().hex[:8]
    run = Path(args.runs_dir).expanduser() / ('fleet-' + time.strftime('%Y%m%d-%H%M%S-') + token)
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': SOCK_PREFIX + token + '.sock', 'python': sys.executable,
           'workers': workers, 'mode': args.mode, 'roles': args.roles, 'max_busy': args.max_busy,
           'request_timeout': args.request_timeout, 'supervisor_profile': args.supervisor_profile,
           'worker_profile': args.worker_profile, 'profile': args.profile, 'codex': codex, 'redis_server': redis_server, 'codex_home': str(home),
           'handoff_ratio': args.handoff_ratio, 'handoff_timeout': args.handoff_timeout,
           'window': args.window, 'stall_timeout': args.stall_timeout,
           'compact_limit': args.compact_limit, 'chrome_script': args.chrome_script,
           'chrome_min_interval': args.chrome_min_interval, 'worker_cli': args.worker_cli,
           'worker_model': args.worker_model, 'worker_kit': args.worker_kit,
           'claude': args.claude or shutil.which('claude'), 'claude_window': args.claude_window}
    save(run / 'config.json', cfg)
    subprocess.run([redis_server, '--port', '0', '--unixsocket', cfg['redis_socket'],
                    '--unixsocketperm', '700', '--dir', str(run), '--dbfilename', 'fleet.rdb',
                    '--appendonly', 'no', '--save', '60 1', '--daemonize', 'yes',
                    '--pidfile', str(run / 'redis.pid'), '--logfile', str(run / 'redis.log')], check=True)
    for _ in range(30):
        try:
            connect(cfg)
            break
        except Exception:
            time.sleep(.1)
    else:
        raise SystemExit('private Redis did not become ready')
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
    r = connect(cfg)
    print(json.dumps({'mode': cfg.get('mode', 'task'), 'task': r.hgetall(TASK), 'service': r.hgetall(SERVICE),
                      'pending_requests': r.llen(REQQ), 'daemon_beat': float(r.get('fx:daemon:beat') or 0),
                      'agents': {n: dict(r.hgetall(key('agent', n)), inbox=r.llen(key('inbox', n)))
                                 for n in ['supervisor'] + cfg['workers']}}, ensure_ascii=False, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog='cmux-fleet', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('start', help='provision the private redis + cmux control surface (inside cmux)')
    s.add_argument('--workers', type=int, default=1)
    s.add_argument('--profile', default=DEFAULTS['profile'], help='codex config profile for every agent (default: fleet)')
    s.add_argument('--codex', help='codex binary (default: first on PATH)')
    s.add_argument('--redis-server', help='redis-server binary (default: first on PATH)')
    s.add_argument('--runs-dir', default=str(HOME / 'runs'), help='where run directories are created')
    s.add_argument('--codex-home', default=str(HOME / 'codex-home'),
                   help='isolated CODEX_HOME for the agents (login + profile symlinked from yours; no hooks/MCP)')
    s.add_argument('--handoff-ratio', type=float, default=DEFAULTS['handoff_ratio'],
                   help='rotate a session once its context use reaches this share of the window')
    s.add_argument('--handoff-timeout', type=float, default=DEFAULTS['handoff_timeout'],
                   help='seconds to wait for the model to write its handoff before using the skeleton')
    s.add_argument('--window', type=int, default=DEFAULTS['window'],
                   help='context window fallback when the rollout has not reported one yet')
    s.add_argument('--stall-timeout', type=float, default=DEFAULTS['stall_timeout'],
                   help='seconds without rollout activity before a busy agent is treated as stalled (0 = off)')
    s.add_argument('--compact-limit', type=int, help='codex model_auto_compact_token_limit (default: codex decides)')
    s.add_argument('--chrome-script', help='optional browser broker script run by the daemon for sandboxed agents')
    s.add_argument('--chrome-min-interval', type=float, default=DEFAULTS['chrome_min_interval'])
    s.add_argument('--worker-cli', choices=('codex', 'claude'), default=DEFAULTS['worker_cli'],
                   help='CLI for workers (the supervisor is always codex)')
    s.add_argument('--worker-model', default=DEFAULTS['worker_model'], help='claude workers: --model value')
    s.add_argument('--worker-kit', help='claude workers: dir with .claude/{agents,skills} and mcp.json copied into each worker cwd')
    s.add_argument('--claude', help='claude binary (default: first on PATH)')
    s.add_argument('--claude-window', type=int, default=DEFAULTS['claude_window'], help='claude workers: context window')
    s.add_argument('--mode', choices=('task', 'service'), default=DEFAULTS['mode'],
                   help='task: one goal until COMPLETED; service: resident, handles a stream of requests')
    s.add_argument('--worker-names', help='comma-separated worker names (default worker-1..N), e.g. exit,reentry,rung')
    s.add_argument('--roles', help='dir of <agent-name>.md files appended to that agent\'s AGENTS.md (one prompt per worker)')
    s.add_argument('--supervisor-profile', help='codex profile for the supervisor (default: --profile)')
    s.add_argument('--worker-profile', help='codex profile for codex workers (default: --profile)')
    s.add_argument('--max-busy', type=int, default=DEFAULTS['max_busy'],
                   help='at most this many workers working at once (0 = no limit)')
    s.add_argument('--request-timeout', type=float, default=DEFAULTS['request_timeout'],
                   help='service mode: seconds before an unanswered request is published as timeout')
    sub.add_parser('daemon', help='run the coordinator (internal)').add_argument('cfg')
    t = sub.add_parser('task', help='submit a goal: JSON file or - for stdin ({"goal":..., "hours":..., "artifacts":[...]})')
    t.add_argument('spec')
    for name in ('status', 'stop', 'resume'):
        sub.add_parser(name).add_argument('--config', default=str(LATEST))
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
    h = sub.add_parser('claude-stop', help='Claude Code Stop hook (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    a = sub.add_parser('actor', help='exec codex for one agent (internal)')
    for arg in ('cfg', 'agent', 'prompt'):
        a.add_argument(arg)
    args = p.parse_args(argv)
    if args.cmd == 'start':
        start(args)
    elif args.cmd == 'daemon':
        daemon(args.cfg)
    elif args.cmd == 'notify':
        notify(args.cfg, args.agent, args.generation, args.payload)
    elif args.cmd == 'claude-stop':
        claude_stop(args.cfg, args.agent, args.generation, sys.stdin.read())
    elif args.cmd == 'actor':
        actor(args.cfg, args.agent, args.prompt)
    else:
        cfg = load(args.config)
        if args.cmd == 'status':
            status(cfg)
        elif args.cmd == 'chrome':
            ans = request(cfg, {'kind': 'chrome', 'argv': args.argv}, wait=150)
            print(ans.get('out') or ans.get('reason') or json.dumps(ans, ensure_ascii=False))
            sys.exit(ans.get('rc', 1) if ans.get('status') == 'ok' else 2)
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
