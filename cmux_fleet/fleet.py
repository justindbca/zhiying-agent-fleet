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
  claude-stop  Claude Code Stop hook for claude agents (internal)
  claude-rewake  Claude Code asyncRewake Stop hook of hook-channel workers (internal)
  claude-stop-failure  Claude Code StopFailure hook of hook-channel / clear-after-result workers (internal)
  claude-clear  Claude Code SessionStart(clear) hook of clear-after-result workers (internal)
  claude-prompt  Claude Code UserPromptSubmit hook of a v2 claude supervisor (internal)
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
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # runnable as a script (actor/notify argv point at this file)
from cmux_fleet import advisor, brake, claude_hook, cli_pairs, codex_queue, contracts, fleet_config, handoff, history, kit as kitmod, lifecycle, lifecycle_close, lifecycle_deliver, lifecycle_hold, model_usage, named_subfleet, night, peer, pi_adapter, profile_runtime, profile_launch, roster, runtime_v2, redis_runtime, subfleet, tabs, watchdog  # noqa: E402
from cmux_fleet.platform import keep_awake_prefix  # noqa: E402
from cmux_fleet import clear_after_result, midturn, opencode_adapter  # noqa: E402
from cmux_fleet.agent_registry import AgentRegistry, AgentRegistryError  # noqa: E402
from cmux_fleet.assignment import assign_profiles  # noqa: E402
from cmux_fleet.handoff import read_usage  # noqa: E402
from cmux_fleet.message_bus import BusError  # noqa: E402
from cmux_fleet.planner import build_dispatch_plan  # noqa: E402
from cmux_fleet.router import route_task  # noqa: E402

SESSION = 'cmux-fleet'  # cmux registry name
HOME = Path(os.environ.get('CMUX_FLEET_HOME') or (Path.home() / '.cmux-fleet'))
LATEST = HOME / 'latest.json'


def pin_paths(cfg):
    """Apply the locations a run pinned in its config; the config wins over the environment.

    cmux starts the daemon, actors and hooks in fresh login shells that need not inherit the
    caller's CMUX_FLEET_HOME / CMUX_FLEET_STATE / CMUX_BIN / CMUX_FLEET_CONFIG, so `start` writes them into
    config.json (the private config file as `private_config`: an absolute path or 'none')."""
    global HOME, LATEST
    from cmux_fleet import cmux_manager
    if 'private_config' in cfg:
        fleet_config.pin(cfg['private_config'])
    if cfg.get('fleet_home'):
        HOME = Path(cfg['fleet_home'])
        LATEST = HOME / 'latest.json'
    if cfg.get('cmux_bin'):
        cmux_manager.CMUX = cfg['cmux_bin']
    if cfg.get('cmux_state_dir'):
        cmux_manager.STATE_DIR = Path(cfg['cmux_state_dir'])
    return cfg


def _pin_from(cfg_path):
    try:
        pin_paths(load(cfg_path))
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # the entry point itself reports an unreadable config
USER_CODEX_HOME = Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex'))
EVENTS, TASK, LOG, NAMES = 'fx:events', 'fx:task', 'fx:log', 'fx:names'
ROSTER = 'fx:roster'  # notify=1: tell the supervisor the current workers with its next message
# Service mode: a resident fleet that handles a stream of requests instead of one goal.
REQ, REQQ, RESULT, RESULTS, SERVICE = 'fx:req:', 'fx:reqq', 'fx:result:', 'fx:results', 'fx:service'
REQ_ID_RE = r'[A-Za-z0-9][A-Za-z0-9:._-]{0,127}'
REQ_HEAD = re.compile(r'^\[Request (' + REQ_ID_RE + r')\]')
SUPERVISOR_INSTRUCTION_HEAD = re.compile(r'^\[Supervisor instruction ([^\]\n]+)\] ', re.MULTILINE)
WORKER_NAME_RE = r'[a-z][a-z0-9-]{0,30}'
KIT_FLEET_RE = r'[a-z][a-z0-9-]*'  # start --kit-fleet NAME: <worker kit>/fleets/NAME/ (design.md §30)
# Display name of the supervisor's first generation; successors are supervisor-1.1, supervisor-1.2, ...
# (same <slot>.<generation> pattern as worker-1 -> worker-1.1). Redis keys and the cwd stay 'supervisor'.
SUPERVISOR_LABEL = 'supervisor-1'
RESULT_TTL = 7 * 86400
RESUME_CLIS = ('codex', 'claude')  # CLIs whose live process proves a reattachable pane (cmux_manager._is_cli);
# pi / opencode are never reattached: their bridge never reconnects after a Redis outage (next generation instead)
# Sandboxed agents cannot drive the user's browser themselves (and must never be allowed
# to kill it). If the operator supplies a broker script, the daemon runs these verbs for them.
CHROME_VERBS = {'check', 'tabs', 'newtab', 'go', 'url', 'js'}
THREAD_RE = r'[0-9a-f-]{36}'
DEFAULTS = {'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 100000, 'stall_timeout': 1200,
            'chrome_min_interval': 20, 'profile': 'fleet', 'worker_cli': 'codex', 'worker_model': 'sonnet',
            'supervisor_model': 'opus',
            'claude_window': 200000, 'mode': 'task', 'max_busy': 0, 'request_timeout': 900,
            'apply_profiles_live': False, 'allow_profile_live_switch': False, 'profile_ready_timeout': 180,
            'transport_version': 1, 'allow_development_v2': False, 'max_attempts': 2,
            'clear_after_result': False, 'clear_timeout': 20, **midturn.DEFAULTS}
CLAUDE_JSON = Path.home() / '.claude.json'
# opencode workers: the free OpenCode Zen tier serves a 200K context (paid: 1M); the model name itself comes only
# from config (--opencode-model). Concurrency of the free tier is untested: max_busy defaults to 2.
OPENCODE_WINDOW = 200000
OPENCODE_MAX_BUSY = 2
OPENCODE_MIN_VERSION = (2, 0, 29)
MODEL_KEYS = {'claude': 'worker_model', 'pi': 'pi_model', 'codex': 'codex_model', 'opencode': 'opencode_model'}
CONTRACTS = 'fx:contracts'  # contract tasks only: one JSON state document (cmux_fleet/contracts.py)
ESCALATIONS = 'fx:escalations'  # task mode: {task id: worker's split request / approval / delegation} (§20)
ITEMS = 'fx:items'  # plain task mode: {dispatch id: {to, instruction}} for dispatch items that carry an id
ESCALATION_DECISIONS = ('approve', 'revise', 'reject')
# Plan F' 8.3 / 8.4: escalations the supervisor rules on instead of the user (keys in fx:escalations).
DIALOG_KEY, BLOCKED_KEY = 'dialog:', 'blocked:'
NO_BLOCK = ('', 'none', '-', '无', 'n/a', 'na', 'nothing')


class NoStrike(ValueError):
    """A decision sent back for one correction that does not count toward the three-strike limit."""


def report_blocked(text):
    """The Blocked: part of a worker report (v2 settled JSON or v1 raw text); '' when it says none."""
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not (isinstance(value, dict) and 'done' in value):
        got = runtime_v2.tui_candidates('worker', 'worker', text or '')
        value = got[0]['value'] if got else {}
    raw = value.get('blocked') if isinstance(value, dict) else ''
    raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    rows = [x.strip().lstrip('-*• ').strip() for x in raw.splitlines()]
    return '\n'.join(x for x in rows if x.rstrip('.。').strip().lower() not in NO_BLOCK)
CMD_TIMEOUT, CMD_TIMEOUT_MAX = 120, 600  # artifact cmd_exit (contract tasks): seconds the daemon may block
BRAKE = 'fx:brake'  # budget card + counters + last trip of the current task / service (cmux_fleet/brake.py)
# Advisor online only (roster `advisor: on`): audit hash per consultation, its counter, dispatches awaiting
# advice per worker (instruction -> advise_note), and notes appended to the supervisor's next message.
ADVICE, ADVICE_SEQ, ADVICE_PENDING, ADVICE_NOTES = 'fx:advice:', 'fx:advice:seq', 'fx:advice:pending:', 'fx:advice:notes'
# Mid-task rescue (advisor plan §3.2): supervisor consult items waiting for an idle slot, relays per task+slot.
RESCUE, RESCUE_RELAYS = 'fx:advice:rescue', 'fx:advice:relays'
# Resident advisor slot (docs/design.md §27, v2 task mode): its tab sits in the supervisor's pane; the first
# generation is labelled 'advisor', successors advisor-1.1, advisor-1.2 (like supervisor-1.1).
ADVISOR, ADVISOR_BASE = advisor.SLOT, 'advisor-1'
ADVICE_WAIT = 180  # s a consultation may wait for the advisor (READY / queue), and again once it has it
ADVISOR_RESTART_GAP = 60  # s between two in-place restarts of a failed advisor tab
# The one rescue consultation out with the resident advisor; the tickets its handoff note lists.
RESCUE_PENDING, ADVICE_RECENT = 'fx:advice:rescue_pending', 'fx:advice:recent'
RESCUE_FIELDS = ('rescue_note', 'rescue_launch', 'rescue_hint', 'rescue_relay', 'advice_preset')


def load(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(path).with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


def connect(cfg, socket_timeout=5):
    import redis
    sock = redis_runtime.validate_socket(cfg)
    client = redis.Redis(unix_socket_path=str(sock), db=0, decode_responses=True,
                         socket_connect_timeout=3, socket_timeout=socket_timeout)
    client.ping()  # Fail closed: never fall back to localhost:6379.
    return client


def agent_cwd(cfg, name):
    if name == ADVISOR:
        return Path(cfg['run']) / 'advisor'  # the one-shot advisor's cwd too: one transcript directory per run
    if name == 'supervisor' and cfg.get('supervisor_cwd'):
        return Path(cfg['supervisor_cwd'])  # sub-fleet: the adopted worker pane keeps its own directory
    run = Path(cfg['run'])
    return run / 'agents' / name if name == 'supervisor' else run / 'workspace' / name


def key(kind, name):
    return 'fx:' + kind + ':' + name


def slot_base(name):
    """The label stem of a slot's successors: supervisor-1.1, advisor-1.1, worker-1.1."""
    return SUPERVISOR_LABEL if name == 'supervisor' else ADVISOR_BASE if name == ADVISOR else name


def manager_for(cfg):
    """The registry of one fleet: the main fleet is 'cmux-fleet', a sub-fleet 'cmux-fleet:<task id>'.

    Which terminal backend that registry talks to is a property of the run (``config.json``), decided once
    by ``start`` and followed by the daemon, the actors, ``--resume`` and every sub-fleet (docs/design.md
    §「终端后端」). A run's config without a backend (started before the choice existed) stays on cmux.
    """
    from cmux_fleet import backends
    extra = {k: cfg[k] for k in ('agents_title', 'control_title') if cfg.get(k)}
    if 'cmux_state_dir' in cfg:
        extra['state_dir'] = cfg['cmux_state_dir']
    return backends.build(backends.normalize_backend(cfg.get('backend')) or 'cmux',
                          cfg.get('session') or SESSION, **extra)


def run_backend(cfg):
    """The backend name of a run (the value fleet's capability checks and layouts branch on)."""
    from cmux_fleet import backends
    return backends.normalize_backend(cfg.get('backend')) or 'cmux'


def resolve_backend(requested=None, platform=None):
    """`start --backend`: (name, reason-or-None) -- the backend to write into config.json.

    No flag: cmux on macOS when cmux is usable there, otherwise WezTerm (design §5). An explicit choice
    that is not usable is refused here, before anything is provisioned, with what is missing.
    """
    from cmux_fleet import backends  # bound before any except clause below can run
    unknown = backends.InterfaceError
    try:
        name = backends.normalize_backend(requested)
    except unknown as exc:  # --backend is validated by argparse; a config is not
        return str(requested), str(exc)
    here = sys.platform if platform is None else platform
    if name is None and here != 'darwin':
        return backends.default_backend(here), None
    if name is None:  # no --backend on macOS: cmux when usable, else WezTerm -- unless WezTerm is missing
        name = backends.default_backend(here)
        if name == 'wezterm':
            try:
                backends.unavailable_reason('wezterm')
            except unknown:
                name = 'cmux'  # nothing to fall through to until WezTerm ships: cmux reports why it cannot run
    try:
        return backends.check_backend(name), None
    except unknown as exc:
        return name, str(exc)


def backends_check(name):
    """Is this run's backend usable right now? cmux asks ``cmux_manager.cmux_available()`` (the probe the
    operators and tests patch, as `start` always did); any other backend asks its own registry, which
    falls back to the backend class when the registry is a fake."""
    from cmux_fleet import backends, cmux_manager
    if name == 'cmux':  # one probe for both paths: tests (and operators) patch exactly this function
        try:
            return bool(cmux_manager.cmux_available())
        except Exception:
            return False
    try:
        probe = getattr(backends.build(name, SESSION), 'available', None)
        if probe is not None:  # the real registry (or a test fake that carries the interface method)
            return bool(probe())
        return bool(backends.backend_class(name).available())
    except Exception:
        return False


def unavailable_text(name):
    """What `start` / `resume` print when the chosen backend cannot run here (design §4.4, §5)."""
    from cmux_fleet import backends
    try:
        why = backends.unavailable_reason(name)
    except Exception as exc:  # a backend module that cannot even import
        why = str(exc)
    return 'backend ' + str(name) + ' cannot run here: ' + (why or 'not available')


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
    if name == ADVISOR:
        return 'claude'  # always the official Claude CLI, whatever --worker-cli the run has (§27)
    supervisor = name == 'supervisor'
    # Unset = a config written before the CLI choice existed: the original Codex. Any other value must be a
    # CLI this code knows; an unrecognised one is refused, never run as Codex (opencode brief 2026-10-16).
    cli = cfg.get('supervisor_cli' if supervisor else 'worker_cli') or 'codex'
    known = cli_pairs.CLIS if supervisor else cli_pairs.WORKER_CLIS
    if cli not in known:
        raise ValueError('unknown ' + ('supervisor' if supervisor else 'worker') + ' CLI ' + repr(cli) + ' for '
                         + str(name) + ' (known: ' + ', '.join(known) + '); refusing rather than running it as codex')
    return cli


def opencode_start_problem(args, binary):
    """Why `start` must refuse opencode workers, or None (binary / version / model / transport checked first)."""
    if args.supervisor_cli == 'opencode':
        return 'opencode runs only as a worker; pick pi, codex or claude for --supervisor-cli'
    if args.transport_version != 2:
        return 'opencode workers run only on transport v2 (in-process plugin + agent_bridge.py)'
    if not args.opencode_model or '/' not in args.opencode_model.strip('/'):
        return '--opencode-model provider/model is required for opencode workers (e.g. opencode/deepseek-v4.1-flash-free)'
    if not args.opencode_window or args.opencode_window < 1:
        return '--opencode-window must be a positive integer'
    try:
        found = subprocess.run([binary, '--version'], text=True, capture_output=True, timeout=15, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 'opencode --version failed: ' + str(exc)
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', found.split()[-1] if found.split() else '')
    if not match or tuple(map(int, match.groups())) < OPENCODE_MIN_VERSION:
        return 'opencode >= ' + '.'.join(map(str, OPENCODE_MIN_VERSION)) + ' is required (v2 plugin API); found: ' + found
    return None


def agent_cfg(cfg, name):
    """cfg with this agent's own pi model / thinking resolved (no profile launch)."""
    return pi_adapter.for_agent(cfg, name)


def is_claude(cfg, name):
    return agent_cli(cfg, name) == 'claude'


def is_pi(cfg, name):
    return agent_cli(cfg, name) == 'pi'


def is_opencode(cfg, name):
    return agent_cli(cfg, name) == 'opencode'


def is_codex(cfg, name):
    return agent_cli(cfg, name) == 'codex'


def is_bridge(cfg, name):
    """pi and opencode run fleet's in-process bridge (agent_bridge.py), not an official TUI hook."""
    return agent_cli(cfg, name) in ('pi', 'opencode')


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
        changed = False
        if not any(projects.get(str(d), {}).get('hasTrustDialogAccepted') for d in [cwd, *cwd.parents]):
            root = HOME.resolve() if HOME.exists() and HOME.resolve() in cwd.parents else cwd
            projects.setdefault(str(root), {})['hasTrustDialogAccepted'] = True
            changed = True
        # "Allow external CLAUDE.md file imports?" is asked per exact cwd (a trusted parent does not cover
        # it): pre-answer No so a fleet worker never stops on it and never loads the operator's personal
        # imports (macmini 2026-10-14: an advisor-respawned worker sat on this dialog). Never overrides an
        # answer already recorded for that directory.
        entry = projects.setdefault(str(cwd), {})
        if not entry.get('hasClaudeMdExternalIncludesWarningShown'):
            entry.setdefault('hasClaudeMdExternalIncludesApproved', False)
            entry['hasClaudeMdExternalIncludesWarningShown'] = True
            changed = True
        if not changed:
            return
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
    """File-only reader shared by idle checks and the daemon probe thread."""
    return midturn.claude_transcript_usage(path, window)


CLAUDE_1M = ('[1m]', 'opus', 'fable', 'sonnet')  # built-in: Claude models with a native 1,000,000-token window (code.claude.com/docs/en/model-config)


def handoff_limits(cli, model, cfg, table=None, worker=None, card=None):
    """Effective (window, ratio, source) for one agent (docs/design.md §22).

    Window and ratio are resolved separately, first hit wins: the roster worker entry, the
    roster ``handoff`` table (``<cli>:<model>`` then ``<cli>``), the config ``handoff`` table
    (same keys), then the running profile's agent card (``card`` = (profile id, ratio): ratio only,
    design.md §12 model cards), then built-ins / start flags. ``window`` None means "measured" (Codex
    rollout, Pi report). Only Claude needs a built-in: its transcript carries no window."""
    layers = [('worker', worker or {})]
    for where, tab in (('roster', table or {}), ('config', cfg.get('handoff') or {})):
        layers += [(where + ' ' + k, tab[k]) for k in ((cli + ':' + model) if model else None, cli) if k and k in tab]
    if card and card[1] is not None:
        layers.append(('card ' + card[0], {'ratio': card[1]}))
    window = ratio = None
    for where, entry in layers:
        if window is None and 'window' in entry:
            window, wsrc = int(entry['window']), where
        if ratio is None and 'ratio' in entry:
            ratio, rsrc = float(entry['ratio']), where
    if window is None:
        if cli == 'opencode':
            window, wsrc = int(cfg.get('opencode_window') or OPENCODE_WINDOW), 'opencode window'
        elif cli != 'claude':
            wsrc = 'measured'
        elif any(t in (model or '').lower() for t in CLAUDE_1M):
            window, wsrc = 1000000, 'built-in ' + (model or '')
        else:
            window, wsrc = int(cfg['claude_window']), '--claude-window'
    if ratio is None:
        ratio, rsrc = float(cfg['handoff_ratio']), '--handoff-ratio'
    return window, ratio, 'window: ' + wsrc + '; ratio: ' + rsrc


def claude_stop(cfg_path, name, generation, raw):
    """Claude Code Stop hook. Pushes a turn event unless background work will resume the turn.

    A Claude worker on the hook channel (cmux_fleet/claude_hook.py) then waits here for its next
    delivery and returns it as ``{"decision": "block", "reason": text}``; the CLI prints that.
    """
    started = time.monotonic()
    data = json.loads(raw or '{}')
    if data.get('background_tasks'):
        # Subagents still running in the background: the session will wake and stop again. Not settled,
        # not released; only audited (a held slot waiting on this was invisible before, plan E-2).
        turn_deferred(cfg_path, name, generation, data.get('background_tasks'))
        return False
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
    hooked = claude_hook.enabled(cfg, name, claude=True)
    deadline = started + claude_hook.hook_timeout(cfg) - claude_hook.margin(cfg)
    if hooked:
        hook_mark_stopping(cfg, name, int(generation))
    if cfg.get('transport_version') == 2:
        cfg, name, generation = subfleet.delegated_route(cfg, name, int(generation))
        # An adopted worker pane that now runs a sub-fleet's supervisor gets typed input: never wait there.
        hooked = hooked and claude_hook.enabled(cfg, name, claude=True)
        if not hooked:
            return tui_turn(cfg, name, int(generation), thread=data.get('session_id'),
                            transcript=data.get('transcript_path', ''), text=text,
                            inputs=claude_user_prompts(data.get('transcript_path', '')))
        try:
            tui_turn(cfg, name, int(generation), thread=data.get('session_id'),
                     transcript=data.get('transcript_path', ''), text=text,
                     inputs=claude_user_prompts(data.get('transcript_path', '')))
        except Exception as exc:  # still arm the channel: the daemon holds/expires the lease itself
            print('fleet claude-stop: turn not reported: ' + str(exc)[:300], file=sys.stderr)
        return claude_hook_next(cfg, name, int(generation), data.get('session_id'), deadline,
                                transcript=data.get('transcript_path', ''))
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
    if hooked and claude_hook_keepalive(cfg, r, name, int(generation), data.get('session_id')):
        r.close()
        return claude_hook_next(cfg, name, int(generation), data.get('session_id'), deadline,
                                transcript=data.get('transcript_path', ''))
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('session_id'), 'transcript': data.get('transcript_path', ''),
                                'msg_id': msg_id, 'text': text, 'at': time.time()}, ensure_ascii=False))
    if not hooked:
        return True
    r.close()
    return claude_hook_next(cfg, name, int(generation), data.get('session_id'), deadline,
                            transcript=data.get('transcript_path', ''))


def turn_deferred(cfg_path, name, generation, tasks):
    """Audit a Stop deferred by background tasks (v2 only). Never raises: the hook's answer is unchanged."""
    try:
        cfg = load(cfg_path)
        if cfg.get('transport_version') != 2:
            return
        r = connect(cfg)
        try:
            bus = runtime_v2.bus_for(cfg, r)
            r.xadd(bus.audit_key, {'event': 'turn.deferred', 'slot': name, 'run_id': bus.run_id,
                                   'generation': str(generation), 'level': 'info',
                                   'tasks': str(len(tasks) if isinstance(tasks, (list, dict)) else 1),
                                   'at': str(int(time.time() * 1000))})
        finally:
            r.close(); r.connection_pool.disconnect()
    except Exception as exc:  # noqa: BLE001 - an audit must never change the hook's answer
        print('fleet claude-stop: turn.deferred not audited: ' + str(exc)[:300], file=sys.stderr)


def hook_keys(cfg, r, name, generation):
    """Redis keys of one slot's hook channel: run-scoped on v2, the fleet's fx: space on v1."""
    prefix = runtime_v2.bus_for(cfg, r).prefix if cfg.get('transport_version') == 2 else 'fx'
    return claude_hook.keys(prefix, name, generation)


def hook_bind(r, k):
    try:
        bind = json.loads(r.get(k['bind']) or '{}')
    except ValueError:
        return {}
    return bind if isinstance(bind, dict) else {}


def claude_hook_keepalive(cfg, r, name, generation, thread):
    """True once for the turn our own keepalive started: it is never settled nor reported."""
    k = hook_keys(cfg, r, name, generation)
    bind = hook_bind(r, k)
    if bind.get('kind') != 'keepalive' or bind.get('session_id') != thread or bind.get('generation') != generation:
        return False
    r.delete(k['bind'])
    return True


def hook_delivered(r, k, thread, lease):
    """The hook of this session handed this very delivery to Claude (v2 binding, design.md §21)."""
    bind = hook_bind(r, k)
    return (bind.get('kind') == 'delivery' and bool(lease.get('message_id'))
            and bind.get('message_id') == lease.get('message_id') and bind.get('session_id') == thread
            and (not lease.get('current_delivery') or bind.get('delivery_id') == lease.get('current_delivery')))


def hook_session(cfg, r, name, generation, thread, parent):
    """(keys, alive, accept) of one hook process; shared by the Stop hook and its rewake fallback.

    ``accept(entry, via)`` re-validates an entry at hand-over: a delivery must still be this
    session's received lease (v2) or the pointer the daemon sent last (v1); anything else is
    dropped as stale (audit ``hook.dropped``). It writes the bind on success and returns the text.
    """
    bus = runtime_v2.bus_for(cfg, r) if cfg.get('transport_version') == 2 else None
    k = hook_keys(cfg, r, name, generation)

    def alive():
        if os.getppid() != parent:  # orphaned: never take a delivery a dead session cannot run
            return False
        info = r.hgetall(key('agent', name))
        # delegated: a sub-fleet adopts this pane and types its role change; let that prompt run
        return int(info.get('generation', -1)) == generation and info.get('state') not in ('dead', 'delegated')

    def audit(event, entry, **fields):
        if bus:
            r.xadd(bus.audit_key, dict({'event': event, 'slot': name, 'run_id': bus.run_id,
                                        'generation': str(generation), 'message_id': str(entry.get('message_id', '')),
                                        'at': str(int(time.time() * 1000))}, **fields))

    def drop(entry, why):
        audit('hook.dropped', entry, error=why, level='warn')
        return None

    def accept(entry, via='stop'):
        kind, info = entry.get('kind'), r.hgetall(key('agent', name))
        if entry.get('generation') != generation:
            return drop(entry, 'entry of another generation')
        if kind == 'delivery' and bus:
            lease = r.hgetall(bus.lease_key(name))
            if (lease.get('status') != 'received' or lease.get('generation') != str(generation)
                    or lease.get('session_id') != thread
                    or lease.get('message_id') != entry.get('message_id')
                    or lease.get('current_delivery') != entry.get('delivery_id')):
                return drop(entry, 'delivery is no longer this session\'s received lease')
        elif kind == 'nudge':
            if info.get('state') != 'starting':
                return drop(entry, 'startup nudge after startup')
        elif kind == 'pointer' and not bus:
            if info.get('msg_id') != entry.get('msg_id') or info.get('state') not in ('busy', 'handoff'):
                return drop(entry, 'pointer is not the last v1 delivery')
        else:
            return drop(entry, 'unknown hook entry')
        bind = {x: entry.get(x) for x in ('kind', 'message_id', 'delivery_id', 'msg_id') if x in entry}
        r.set(k['bind'], json.dumps(dict(bind, generation=generation, session_id=thread or '', via=via,
                                         at=time.time())), ex=claude_hook.BIND_TTL)
        if via != 'stop':
            audit('hook.rewake', entry, level='warn')
        return entry.get('text')
    return k, alive, accept


def hook_on_sigterm():
    if threading.current_thread() is threading.main_thread():
        # Claude Code stops a hook with SIGTERM: unwind so a popped entry goes back to the mailbox.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


def claude_hook_next(cfg, name, generation, thread, deadline, transcript=''):
    """Wait for this generation's next delivery; the block decision for it, or None (let Claude idle).

    None also when the operator typed into the pane meanwhile (``transcript``: claude_hook.OperatorQueue):
    the hook yields so that line runs now instead of after the next delivery or the keepalive.
    """
    import redis
    hook_on_sigterm()
    queue = claude_hook.OperatorQueue(transcript) if transcript else None
    parent = os.getppid()  # Claude Code (or its hook shell): once it is gone, nobody reads our answer
    while time.monotonic() < deadline and parent != 1:
        try:
            r = connect(cfg, socket_timeout=claude_hook.POLL + 10)
        except (redis.RedisError, OSError, ValueError):
            time.sleep(claude_hook.POLL)
            continue
        try:
            k, alive, accept = hook_session(cfg, r, name, generation, thread, parent)
            text, _ = claude_hook.wait(r, k, generation, thread, deadline, alive, accept, queue=queue)
            if text:
                return claude_hook.block(text)
            if time.monotonic() < deadline or not alive():
                return None
            # Claude Code would kill this hook soon and leave the session unarmed: one tiny turn re-arms it.
            r.set(k['bind'], json.dumps({'kind': 'keepalive', 'generation': generation, 'session_id': thread or '',
                                         'at': time.time()}), ex=claude_hook.BIND_TTL)
            return claude_hook.block(claude_hook.KEEPALIVE)
        except redis.RedisError:
            time.sleep(claude_hook.POLL)  # Redis restarting: keep the session armed until the deadline
        finally:
            r.close(); r.connection_pool.disconnect()
    return None


def hook_mark_stopping(cfg, name, generation):
    """The Stop hook is settling (before it waits): the rewake fallback must not take over meanwhile."""
    try:
        r = connect(cfg)
    except Exception:
        return
    try:
        r.set(hook_keys(cfg, r, name, generation)['stopping'], str(os.getpid()), ex=claude_hook.STOPPING_TTL)
    except Exception:
        pass
    finally:
        r.close()


def claude_rewake(cfg_path, name, generation, raw):
    """asyncRewake Stop hook: the fallback when no Stop hook waits (Escape, a killed hook at its timeout).

    It never recovers a StopFailure (API error) turn: that delivery was already taken from the mailbox,
    so it goes the watchdog's stall / frozen path (claude_stop_failure).

    It never acts while another turn of the session runs (``claude_hook.TurnWatch`` on the Stop's
    transcript): a turn the Stop hook did not start (operator after Escape or ctrl+x ctrl+s, a peer
    message, a background task) ends with its own Stop, whose hooks take over. Escape or a
    StopFailure ending that turn makes this fallback eligible again.

    Runs in the background at every Stop. Only the newest one per slot acts: if this generation's
    mailbox holds an entry while neither a waiting nor a settling Stop hook exists for
    ``claude_hook.GRACE`` seconds, it takes the entry and returns its text; the CLI writes it to
    stderr and exits 2, which wakes Claude Code with it (no typing). None: nothing to do.
    """
    import redis
    started = time.monotonic()
    data = json.loads(raw or '{}')
    cfg = load(cfg_path)
    generation = int(generation)
    own, returning = (cfg, name, generation), False
    if cfg.get('transport_version') == 2:
        cfg, name, generation = subfleet.delegated_route(cfg, name, generation)
        if (not claude_hook.enabled(cfg, name, claude=True) and (name, generation) != own[1:]
                and claude_hook.enabled(own[0], own[1], claude=True)):
            # The session supervises a sub-fleet (typed there). Its own worker channel has no Stop hook
            # waiting when it comes back: keep watching it, idle while delegated (§29 return handoff).
            (cfg, name, generation), returning = own, True
    if not claude_hook.enabled(cfg, name, claude=True):
        return None
    hook_on_sigterm()
    thread, parent = data.get('session_id'), os.getppid()
    deadline = started + claude_hook.hook_timeout(cfg) - claude_hook.margin(cfg)
    watch = claude_hook.TurnWatch(data.get('transcript_path') or '')
    mine = None
    while time.monotonic() < deadline and parent != 1:
        try:
            r = connect(cfg, socket_timeout=claude_hook.POLL + 10)
        except (redis.RedisError, OSError, ValueError):
            time.sleep(claude_hook.POLL)
            continue
        try:
            k, alive, accept = hook_session(cfg, r, name, generation, thread, parent)
            if mine is None:
                mine = r.incr(k['rewake'])
                r.expire(k['rewake'], claude_hook.BIND_TTL)
            unarmed = None
            while time.monotonic() < deadline:
                if returning and str(mine) == r.get(k['rewake']) and r.hget(key('agent', name), 'state') == 'delegated':
                    time.sleep(1)
                    continue
                if str(mine) != r.get(k['rewake']) or not alive():
                    return None  # a newer Stop has its own fallback, or this generation is gone
                if watch.poll():
                    try:
                        watch.ended(float(r.get(k['turnend']) or 0))
                    except ValueError:
                        pass
                if watch.busy or claude_hook.live(r, k) or not r.llen(k['mailbox']):
                    unarmed = None
                    time.sleep(1)
                    continue
                unarmed = unarmed or time.monotonic()
                if time.monotonic() - unarmed < claude_hook.GRACE:
                    time.sleep(1)
                    continue
                got = r.lpop(k['mailbox'])
                if not got:
                    continue
                try:
                    entry = json.loads(got)
                    text = accept(entry, via='rewake') if isinstance(entry, dict) else None
                except ValueError:
                    continue
                except BaseException:
                    r.lpush(k['mailbox'], got)
                    raise
                if text:
                    return text
            return None
        except redis.RedisError:
            time.sleep(claude_hook.POLL)
        finally:
            r.close(); r.connection_pool.disconnect()
    return None


def claude_posttool(cfg_path, name, generation):
    """Claude Code PostToolUse hook (design.md §34): the mid-turn handoff prompt the daemon registered for
    this slot and generation, taken once (midturn_send_pending 1 -> 0); None when there is nothing to send.
    Never raises: any failure (Redis down, stale generation) means no output, so normal work is untouched."""
    try:
        cfg = load(cfg_path)
        r = connect(cfg, socket_timeout=2)
        k = key('agent', name)
        info = r.hgetall(k)
        if info.get('midturn_send_pending') != '1' or str(info.get('generation')) != str(generation):
            return None
        prompt = midturn.registered_prompt(info, info['generation'])
        if not prompt:
            return None
        # Take it once: only the call that flips 1 -> 0 sends it (several tool calls can finish together).
        if not r.eval("if redis.call('HGET', KEYS[1], 'midturn_send_pending') == '1' then "
                      "redis.call('HSET', KEYS[1], 'midturn_send_pending', '0') "
                      "redis.call('HINCRBY', KEYS[1], 'midturn_type_attempts', 1) return 1 end return 0", 1, k):
            return None
        return prompt
    except Exception:
        return None


def claude_stop_failure(cfg_path, name, generation, raw):
    """Claude Code StopFailure hook (output ignored): an API error ended the turn, so no Stop hook
    waits for the next delivery. Record it; an in-flight delivery then goes the watchdog's stall path.

    A clear-after-result worker (typed delivery, docs/design.md §28) instead posts a ``stop_failure``
    event: the daemon fails its in-flight request at once and clears the context."""
    data = json.loads(raw or '{}')
    cfg = load(cfg_path)
    if clear_after_result.enabled(cfg, name):
        return _claude_context_hook(cfg_path, name, generation, raw, 'stop_failure')
    fields = {'slot': name, 'generation': str(generation), 'error': str(data.get('error') or 'unknown')[:200],
              'session_id': str(data.get('session_id') or ''), 'level': 'warn',
              'text': str(data.get('last_assistant_message') or '')[-500:], 'at': str(int(time.time() * 1000))}
    r = connect(cfg)
    try:
        if cfg.get('transport_version') == 2:
            cfg, name, generation = subfleet.delegated_route(cfg, name, int(generation))
            bus = runtime_v2.bus_for(cfg, r)
            r.xadd(bus.audit_key, dict(fields, event='hook.stop_failure', run_id=bus.run_id, slot=name))
        else:
            r.rpush(LOG, json.dumps(dict(fields, time=time.time(), kind='hook.stop_failure'), ensure_ascii=False))
        if claude_hook.enabled(cfg, name, claude=True):
            # The turn ended without a Stop: a waiting rewake fallback may act again (TurnWatch.ended).
            r.set(hook_keys(cfg, r, name, int(generation))['turnend'], str(time.time()), ex=claude_hook.BIND_TTL)
    finally:
        r.close()
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
            continue  # extended keys (not_contains / json_keys / cmd_exit) stay ignored, as before
        banned = item.get('not_contains', [])
        if not isinstance(banned, list) or not all(isinstance(x, str) and x for x in banned):
            raise ValueError('not_contains must be a list of literal strings')
        keys = item.get('json_keys')
        if keys is not None and (not isinstance(keys, list) or not keys
                                 or not all(isinstance(x, str) and x for x in keys)):
            raise ValueError('json_keys must be a non-empty list of key names')
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


def json_key_failures(text, keys):
    """json_keys: the file is one JSON object, or ends with one (a report closing with a verdict
    line like {"verdict": "PASS", "p0": 0}); every key must be present."""
    try:
        obj = json.loads(text)
    except ValueError:
        obj = contracts.last_json_object(text)[0]
    if not isinstance(obj, dict):
        return ['no JSON object (whole file or final line)']
    return ['missing JSON key ' + k for k in keys if k not in obj]


def check_artifacts(artifacts, root, extended=False, run_commands=False, commands=None):
    """Literal file checks only (exists / size / contains); returns failures (empty = pass).
    ``extended`` (contract tasks and frozen artifacts) adds not_contains, json_keys and cmd_exit;
    commands run only when ``run_commands`` (the COMPLETED gate), never from result.json / resume re-checks,
    and only those whose command_key is in ``commands`` (the caller's declared set); any other is reported, not run."""
    root = Path(root).resolve()
    failures = []
    for item in artifacts:
        path = (root / item['path']).resolve()
        if not path.is_relative_to(root):
            failures.append('outside workspace: ' + item['path'])
            continue
        if extended and run_commands and item.get('cmd_exit'):
            if command_key(item) not in (commands or ()):
                failures.append('cmd_exit not declared by the caller, not run: ' + item['path'])
                continue
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
            elif extended and item.get('json_keys'):
                failures += [x + ': ' + item['path'] for x in json_key_failures(text, item['json_keys'])]
    return failures


def extended_checks(task):
    """Contract tasks and caller-frozen artifacts use the extended checks (not_contains / json_keys / cmd_exit)."""
    return task.get('contracts') == '1' or task.get('artifacts_frozen') == '1'


def command_key(item):
    """Identity of an artifact's acceptance command: path + argv + timeout (all three are what runs)."""
    return json.dumps([item.get('path'), item.get('cmd_exit'), item.get('timeout_s', CMD_TIMEOUT)])


def caller_commands(task):
    """The cmd_exit commands the CALLER declared in the task spec (stored apart from the merged artifacts
    list at submit). A task stored without that field (older daemon) has none: nothing runs."""
    return {command_key(a) for a in json.loads(task.get('caller_artifacts') or '[]')
            if isinstance(a, dict) and a.get('cmd_exit')}


def command_guard(task, decision):
    """Supervisor decisions may not add or replace a cmd_exit: only an artifact identical to one the caller
    declared may carry one. Raises ValueError (the normal correction / three-strike path)."""
    allowed = caller_commands(task)
    for item in decision.get('artifacts') or []:
        if isinstance(item, dict) and 'cmd_exit' in item and command_key(item) not in allowed:
            raise ValueError('cmd_exit on ' + str(item.get('path')) + ' was not declared by the caller: a '
                             'supervisor decision may not add or replace acceptance commands (drop cmd_exit, or '
                             'repeat the caller\'s artifact unchanged)')


def validate_contract(raw):
    """Task spec ``contract``: {"artifacts_frozen": bool}. Returns the frozen flag."""
    raw = {} if raw is None else raw
    if (not isinstance(raw, dict) or set(raw) - {'artifacts_frozen'}
            or type(raw.get('artifacts_frozen', False)) is not bool):
        raise ValueError('contract may only have artifacts_frozen (true or false)')
    return raw.get('artifacts_frozen', False)


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


def roster_file(cfg):
    """The roster the daemon follows now: the night roster while night is on (§25), else --roster, else
    <run>/roster.yaml (created by the advisor switch when absent)."""
    followed = night.roster_for(cfg)
    return Path(followed) if followed else Path(cfg['run']) / 'roster.yaml'


def advisor_rosters(cfg):
    """Every roster file the advisor switch writes: the followed one first, then the other of a day /
    night pair, so a night on / off never flips the advisor (it is per run, not per roster)."""
    paths = [roster_file(cfg)]
    for other in (cfg.get('roster'), cfg.get('roster_night')):
        if other and Path(other) not in paths:
            paths.append(Path(other))
    return paths


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
    escalations = validate_escalations(decision)
    if 'advisor' in decision and decision['advisor'] not in ('on', 'off'):
        raise ValueError('advisor must be "on" or "off"')
    consult = decision.setdefault('consult', [])
    validate_consult(consult, workers)
    if 'artifacts' in decision:
        validate_artifacts(decision['artifacts'])
    withdraw = decision.setdefault('withdraw', [])
    if not isinstance(withdraw, list) or any(w not in workers for w in withdraw):
        raise ValueError('withdraw must be a list of worker names: ' + ', '.join(workers))
    if withdraw and decision['status'] != 'COMPLETED':
        raise ValueError('withdraw is only for COMPLETED: it drops the unsettled work of the listed workers')
    if decision['status'] == 'RUNNING' and not dispatch and not escalations and not consult and not others_active:
        raise ValueError('no worker is busy: RUNNING must dispatch at least one instruction')
    return decision


def validate_escalations(decision):
    """decision['escalations']: answers to [escalate] (task mode), [dialog] and [blocked] requests."""
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
    return escalations


def service_blocked(value, reply):
    """A service worker's 'I am stuck' answer (plan F' 8.4 in service mode): exactly {"blocked": "<text>"}, when
    the request's own reply keys do not use 'blocked'. Returns the reason, or '' for an ordinary answer."""
    if not isinstance(value, dict) or set(value) != {'blocked'} or not isinstance(value['blocked'], str):
        return ''
    if 'blocked' in reply.get('required', []) or 'blocked' in (reply.get('allowed') or []):
        return ''
    reason = value['blocked'].strip()
    return '' if reason.rstrip('.。').strip().lower() in NO_BLOCK else reason


def validate_consult(consult, workers):
    """Supervisor's mid-task consult items (advisor plan §3.2): {worker, task, finding, want?}."""
    if not isinstance(consult, list):
        raise ValueError('consult must be a list')
    for item in consult:
        if not isinstance(item, dict) or item.get('worker') not in workers:
            raise ValueError('consult.worker must be one of ' + ', '.join(workers))
        if not isinstance(item.get('task'), str) or not re.fullmatch(contracts.M_RE, item['task']):
            raise ValueError('consult.task must be the dispatch id of the task (M<n>, e.g. M0)')
        if not isinstance(item.get('finding'), str) or not item['finding'].strip():
            raise ValueError('consult.finding must say in your own words what went wrong')
        if not isinstance(item.get('want', ''), str):
            raise ValueError('consult.want must be text')
    if len({x['task'] for x in consult}) != len(consult):
        raise ValueError('consult: one item per task')


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
    for item in validate_escalations(decision):  # plan F': only [dialog] / [blocked] in service mode
        if not item['task'].startswith((DIALOG_KEY, BLOCKED_KEY)):
            raise ValueError('escalations: service mode answers only dialog:<worker>:g<n> / blocked:<worker>')
    if decision.get('subfleet') is not None:  # §29: approve / reject one external sub-fleet up / down
        named_subfleet.validate_decision(decision['subfleet'])
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
                if turn:
                    # Known turn id: judge that turn's own end, even if a later (queued) turn has started since.
                    if row.get('type') == 'event_msg' and payload.get('type') == 'task_complete' \
                            and payload.get('turn_id') == turn:
                        return (payload.get('last_agent_message') or '').strip() == text
                    if row.get('type') == 'event_msg' and payload.get('type') == 'task_started' and (
                            (payload.get('turn_attribution') or {}).get('turn_id') or payload.get('turn_id')) == turn:
                        return False  # this turn has not finished (yet)
                    continue
                if row.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                    return False
                if row.get('type') == 'event_msg' and payload.get('type') == 'task_complete':
                    return (payload.get('last_agent_message') or '').strip() == text
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
                        inputs=inputs if isinstance(inputs, list) else [],
                        turn=data.get('turn-id') or data.get('turn_id') or '')
    r.rpush(EVENTS, json.dumps({'kind': 'turn', 'agent': name, 'generation': int(generation),
                                'thread': data.get('thread-id') or data.get('thread_id'),
                                'turn': data.get('turn-id') or data.get('turn_id') or '',
                                'text': data.get('last-assistant-message') or data.get('last_assistant_message') or '',
                                'at': time.time()}, ensure_ascii=False))
    return True


class Prompt(str):
    """A transcript prompt with where it came from: ``hook`` (a Stop-hook feedback or rewake row, i.e. a
    hook-channel delivery) or ``user`` (typed / CLI argument). Plain strings count as ``hook``."""
    origin = 'hook'


def claude_user_prompts(path, count=3):
    """Last real user prompts of a Claude transcript (not tool results, meta or sidechains), as ``Prompt``.

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
                if row.get('type') != 'user' or row.get('isSidechain'):
                    continue
                content, origin = (row.get('message') or {}).get('content'), 'user'
                if (row.get('origin') or {}).get('kind') == 'task-notification':
                    # A finished background task continues the current turn's work; a hook-channel
                    # rewake (Stop hook feedback notification) is a delivery.
                    content, origin = claude_hook.rewake_text(content), 'hook'
                    if content is None:
                        continue
                if row.get('isMeta'):  # only a hook-channel delivery (Stop-hook block reason) counts
                    content, origin = claude_hook.feedback_text(content), 'hook'
                if isinstance(content, list):
                    content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
                if isinstance(content, str) and content.strip() and not content.startswith('[Request interrupted by user'):
                    prompt = Prompt(content)
                    prompt.origin = origin
                    out.append(prompt)
    except (OSError, TypeError):
        pass
    return list(out)


PROMPT_HOOK_TIMEOUT = 60  # seconds; Claude lets the prompt through when a hook times out or crashes
PROMPT_DEADLINE = 40  # the hook's own hard stop: it blocks the prompt itself well before Claude's timeout
PROMPT_WAIT = 20  # daemon acceptance wait, kept inside PROMPT_DEADLINE


class PromptDeadline(Exception):
    pass


def claude_prompt_main(cfg_path, name, generation, raw, deadline=PROMPT_DEADLINE):
    """Fail-closed wrapper of claude_prompt for the hook process: the decision JSON it returns
    always blocks unless claude_prompt explicitly let the prompt through. A crash (non-zero exit)
    or a hook timeout would make Claude Code pass the operator text to the model, bypassing the
    design.md §5 queue, so every error and the PROMPT_DEADLINE alarm end in a block instead."""
    def expire(signum, frame):
        raise PromptDeadline
    # The hook process runs this on its main thread; an embedding thread (tests) skips the alarm.
    armed = threading.current_thread() is threading.main_thread()
    if armed:
        previous = signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, deadline)
    try:
        reason = claude_prompt(cfg_path, name, generation, raw, wait=min(PROMPT_WAIT, max(1.0, deadline - 10)))
    except PromptDeadline:
        reason = 'Fleet 输入未确认（超过 ' + str(deadline) + ' 秒）；请先查看 control 再决定是否重试。'
    except Exception as exc:  # noqa: BLE001 - fail closed: never let operator text reach the model
        reason = 'Fleet 输入未受理（' + type(exc).__name__ + ': ' + str(exc)[:200] + '）；请先查看 control。'
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return reason
IMAGE_PLACEHOLDER = re.compile(r'\[Image #\d+\]')


def prompt_hook_slot(cfg, name, generation=None):
    """A fleet-launched v2 Claude supervisor (not a §19 adopted pane, whose hooks are the parent's)."""
    return (name == 'supervisor' and cfg.get('transport_version') == 2 and is_claude(cfg, name)
            and not subfleet.adopted(cfg, name, generation))


def claude_prompt(cfg_path, name, generation, raw, wait=PROMPT_WAIT):
    """UserPromptSubmit hook of a v2 Claude supervisor: the design.md §5 input boundary.

    Returns None to let the prompt reach the model (the daemon-typed delivery, the spawn
    prompt, slash commands), else the reason shown when the prompt is blocked. Operator
    text is never answered off-protocol: it becomes an operator_input event (original
    submit / queue / ui-<uuid> request semantics) and is typed back as a normal delivery.
    """
    data = json.loads(raw or '{}')
    prompt = data.get('prompt') if isinstance(data.get('prompt'), str) else ''
    cfg = load(cfg_path)
    generation = int(generation)
    if not prompt_hook_slot(cfg, name, generation) or not prompt.strip() or prompt.lstrip().startswith('/'):
        return None
    r = connect(cfg)
    try:
        bus = runtime_v2.bus_for(cfg, r)
        info = r.hgetall(key('agent', name))
        if int(info.get('generation', -1)) != generation or info.get('state') == 'dead':
            return 'Fleet：这个终端属于已退役的监工代，输入未受理。'
        if info.get('state') == 'starting':
            try:
                first = (Path(cfg['run']) / 'prompts' / (name + '-g' + str(generation) + '.md')).read_text()
            except OSError:
                first = ''
            if runtime_v2.turn_answers(first, [prompt]):
                return None
            return 'Fleet 尚未 READY，输入未受理；请先检查启动状态。'
        if prompt == midturn.registered_prompt(info, generation) and data.get('session_id') == info.get('thread'):
            return None
        lease = r.hgetall(bus.lease_key(name))
        if lease.get('status') == 'received' and lease.get('message_id'):
            msg = bus.message(lease['message_id'])
            if msg.get('generation') == generation and runtime_v2.turn_answers(msg['body']['text'], [prompt]):
                return None
        if IMAGE_PLACEHOLDER.search(prompt):
            return 'Fleet 输入只支持文字，图片未转交；请改用图片文件路径后重发。'
        correlation = 'ui-' + uuid.uuid4().hex
        try:
            runtime_v2.post_event(bus, dict(kind='operator_input', agent=name, generation=generation,
                                            run_id=bus.run_id, thread=data.get('session_id'), text=prompt,
                                            request_id=correlation), correlation)
            answer = runtime_v2.wait_reply(bus, correlation, timeout=wait)
        except (runtime_v2.redis.RedisError, runtime_v2.BusError, ValueError) as exc:
            return 'Fleet 输入未确认（' + str(exc)[:200] + '）；请先查看 control 再决定是否重试。'
        if answer.get('status') == 'accepted':
            return ('Fleet：指令已排队（' + str(answer.get('id') or answer.get('task_id')) + '），'
                    '监工会按派活流程处理；这段输入不会直接进入当前对话。')
        if answer.get('status') == 'rejected':
            return 'Fleet 输入被拒绝：' + str(answer.get('reason') or '原因未给出')
        return 'Fleet 输入未确认（' + correlation + '）；请先查看 control 再决定是否重试。'
    finally:
        r.close(); r.connection_pool.disconnect()


def tui_turn(cfg, name, generation, *, thread, transcript, text, inputs, wait=None, settle_wait=30, turn=''):
    """v2 turn end of an official interactive CLI (Codex notify / Claude Stop hook).

    First READY turn: bind the session, register the slot and report startup. Later turns:
    settle only the delivery this generation received, only if that delivery started the turn
    (Claude: its typed text; Codex: the rollout proves the queued delivery started `turn`, see
    codex_queue.bound). The daemon validates identity, lease, fence and schema as for Pi.
    """
    if name not in ['supervisor', ADVISOR] + cfg['workers'] or not runtime_v2.tui_slot(cfg, name):
        raise ValueError('turn hook must name an official-CLI v2 agent in this fleet')
    # Events are durable either way; waiting long only keeps this generation's startup
    # sequence (bind -> register -> READY) intact while the daemon is still spawning tabs.
    wait = float(cfg.get('profile_ready_timeout') or 180) if wait is None else wait
    r = connect(cfg)
    bus = runtime_v2.bus_for(cfg, r)
    try:
        info = r.hgetall(key('agent', name))
        base = dict(agent=name, generation=generation, run_id=bus.run_id, thread=thread, transcript=transcript)
        if clear_after_result.enabled(cfg, name):
            base['hook_pid'] = os.getpid()

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
        # The CLI of this generation, as delivery sees it (Fleet.runtime_cfg): a profile switch may change it.
        launch = json.loads(info.get('runtime_launch') or '{}')
        codex = codex_queue.is_codex(dict(cfg, **launch.get('overrides', {})), name)
        why = []

        def codex_bound(delivery):
            """Did this queued Codex delivery start the turn? Exact rollout binding, never the cumulative inputs."""
            # The record and its token are durable before the CLI runs, so a turn end faster than the CLI binds too.
            rec = codex_queue.record(r.hgetall(key('agent', name)))
            if rec['cq_delivery'] != delivery:
                why.append('queued delivery is ' + (rec['cq_delivery'] or 'none') + ', not ' + delivery)
                return False
            ok, reason = codex_queue.bound(rec, thread, turn, sessions_dir(cfg), generation)
            if not ok:
                why.append(reason)
                return False
            if not codex_queue.mark_bound(r, name, turn, rec['cq_token']):
                why.append('delivery abandoned or replaced while binding')
                return False
            return True
        if int(info.get('generation', -1)) != generation or info.get('state') == 'dead':
            return skipped('stale', 'hook generation is not the current runtime')
        if info.get('state') == 'starting':
            pending = codex_queue.record(info) if codex else {}
            if pending.get('cq_delivery') and codex_queue.unsettled(info):
                # A queued startup message (READY nudge / sub-fleet role change) must be the one this turn answers.
                if not codex_bound(pending['cq_delivery']):
                    return skipped('ignored', 'startup turn not bound to ' + pending['cq_delivery'] + ': ' + '; '.join(why))
            if info.get('thread') != thread:
                answer = call('bridge_session')
                if answer.get('status') != 'registered':
                    return answer
            bus.register_slot(name, generation, thread)
            return call('startup', text=text, failed=False, aborted=False)
        k = hook_keys(cfg, r, name, generation) if not codex and claude_hook.enabled(cfg, name) else None
        # Our own keepalive turn is never settled; it still ends a turn of this session (hold exit below).
        kept = bool(k) and claude_hook_keepalive(cfg, r, name, generation, thread)

        def answered(body_text, lease):
            if codex:  # official Codex: the queued delivery's token binding (codex_queue.bound)
                return codex_bound(lease.get('current_delivery') or '')
            # Hook channel: this session's Stop hook must have handed over this very delivery, and it
            # must be the row that started this turn (its isMeta "Stop hook feedback" row is the latest
            # prompt, see claude_user_prompts): an older feedback row never settles a manual turn, nor
            # does an operator line that repeats the delivery text word for word (origin 'user').
            if k is None:
                return runtime_v2.turn_answers(body_text, inputs)
            return (bool(inputs) and getattr(inputs[-1], 'origin', 'hook') == 'hook'
                    and runtime_v2.turn_answers(body_text, inputs[-1:]) and hook_delivered(r, k, thread, lease))

        def ignored(reason):
            # §21: a turn no delivery started (a peer SendMessage woke it, or the operator typed):
            # the daemon records it as unsolicited and forwards its Peer notices / question lines.
            if name != 'supervisor' and (text or '').strip():
                runtime_v2.post_event(bus, dict(base, kind='unsolicited', text=text[-8000:]))
            return skipped('ignored', reason)
        lease = r.hgetall(bus.lease_key(name))
        if (lease.get('status') in ('uncertain', 'expired') and lease.get('message_id')
                and info.get('state') in ('held', 'clearing')):
            # Hold exit (docs/design.md §24): any turn end of the held delivery's own session and
            # generation proves its execution stopped (Claude runs one turn at a time; a Stop with
            # background work never reaches here). Codex: its queue may still hold the item, so the
            # rollout must show the token's turn complete. The daemon releases; the result is not applied.
            msg = bus.message(lease['message_id'])
            if msg.get('generation') != generation or msg.get('session_id') != thread:
                return skipped('ignored', 'held delivery belongs to another generation/session')
            if codex:
                rec = codex_queue.record(info)
                ran = (codex_queue.token_turn(thread, rec['cq_token'], sessions_dir(cfg))
                       if rec['cq_token'] and rec['cq_delivery'] == lease.get('current_delivery') else None)
                if not ran or not ran['complete']:
                    return skipped('ignored', 'held Codex delivery ' + (lease.get('current_delivery') or '')
                                   + (' has not started' if not ran else ' is still running') + ' in the rollout')
                codex_queue.mark_bound(r, name, ran['turn'], rec['cq_token'])  # a live record: proven ran; abandoned stays
            return call('turn_ended', delivery={k: msg[k] for k in runtime_v2.IDENTITY})
        if kept:
            return skipped('ignored', 'hook channel keepalive turn')
        if lease.get('status') != 'received' or not lease.get('message_id'):
            return ignored('no received delivery (manual or duplicate turn)')
        msg = bus.message(lease['message_id'])
        if msg.get('generation') != generation or msg.get('session_id') != thread:
            return skipped('stale', 'delivery belongs to another generation/session')
        if not answered(msg['body']['text'], lease):
            return ignored('turn was not started by the delivery' + (': ' + '; '.join(why) if why else ''))
        return call('settled', delivery={k: msg[k] for k in runtime_v2.IDENTITY},
                    candidates=runtime_v2.tui_candidates(name, msg['body'].get('purpose'), text or '',
                                                         msg['body'].get('mode') or cfg.get('mode') or 'task'),
                    failed=False, aborted=False, unfinished=False,
                    wall_text=(text or '') if len((text or '').strip()) <= brake.TURN_WALL_MAX else '')
    finally:
        r.close(); r.connection_pool.disconnect()


def _claude_context_hook(cfg_path, name, generation, raw, kind):
    """Persist hooks without waiting inside Claude's lifecycle callback."""
    cfg, data = load(cfg_path), json.loads(raw or '{}')
    if (name not in cfg['workers'] or cfg.get('worker_cli') != 'claude'
            or cfg.get('transport_version') != 2):
        raise ValueError('context hook must name a Claude v2 worker')
    if not isinstance(data, dict):
        raise ValueError('hook JSON must be an object')
    if kind == 'cleared' and data.get('source') != 'clear':
        return False
    r = connect(cfg)
    try:
        bus = runtime_v2.bus_for(cfg, r)
        info = r.hgetall(key('agent', name))
        ev = dict(kind=kind, agent=name, generation=int(generation), run_id=bus.run_id,
                  thread=data.get('session_id'), transcript=data.get('transcript_path', ''),
                  hook_pid=os.getpid(), old_thread=info.get('thread'), clear_token=info.get('clear_token', ''))
        if kind == 'stop_failure':
            ev['error'] = str(data.get('error') or data.get('error_details')
                              or data.get('last_assistant_message') or 'Claude StopFailure')[:4000]
            lease = r.hgetall(bus.lease_key(name))
            if lease.get('message_id'):
                msg = bus.message(lease['message_id'])
                ev['delivery'] = {k: msg[k] for k in runtime_v2.IDENTITY}
        runtime_v2.post_event(bus, ev)
        return True
    finally:
        r.close(); r.connection_pool.disconnect()


def claude_clear(cfg_path, name, generation, raw):
    return _claude_context_hook(cfg_path, name, generation, raw, 'cleared')


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


REVIEW_POINTER = ('\n## Review instructions\n\nWhen an instruction asks you to review, audit or accept someone '
                  'else\'s change (复核 / 审查 / 验收 / review), first read REVIEWER.md in your current directory '
                  'and follow it for that instruction: the top principle is to go live quickly and run normally '
                  'after go-live, rounds narrow, P2 items are deferred. It does not apply to other work.\n')

# Every Claude Code the fleet starts: never self-update. An isolated config (CLAUDE_CONFIG_DIR) has no
# autoupdater setting of its own, and on 2026-10-14 01:36 such an auto-update broke the shared install.
CLAUDE_ENV = {'DISABLE_AUTOUPDATER': '1'}


def claude_argv(cfg_path, cfg, name, generation, prompt, resume=None):
    """``resume``: this generation continues that Claude session (``claude --resume``; a lost slot, §24)."""
    cwd = agent_cwd(cfg, name)
    hook = shlex.join([cfg['python'], str(Path(__file__).resolve()), 'claude-stop', str(cfg_path), name, str(generation)])
    settings = cwd / ('.fleet-settings-g' + str(generation) + '.json')
    # v2: the READY hook waits for the daemon, which may still be spawning other tabs.
    timeout = 30 if cfg.get('transport_version') != 2 else int(float(cfg.get('profile_ready_timeout') or 180)) + 30
    advised = cfg.get('advisor') is True and name != 'supervisor'
    hooked = claude_hook.enabled(cfg, name, claude=True)
    if hooked:
        timeout = claude_hook.hook_timeout(cfg)  # the hook waits here for the next delivery
    hooks = {'Stop': [{'hooks': [{'type': 'command', 'command': hook, 'timeout': timeout}]}]}
    if hooked:
        # Fallback when no Stop hook waits any more (Escape, hook timeout, StopFailure): a background
        # hook that wakes Claude with the delivery (exit 2) instead of typing it.
        hooks['Stop'][0]['hooks'].append({'type': 'command', 'timeout': timeout, 'asyncRewake': True,
                                          'rewakeMessage': claude_hook.REWAKE_MESSAGE, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-rewake', str(cfg_path), name, str(generation)])})
        # An API error ends the turn with StopFailure instead of Stop: no hook waits afterwards.
        hooks['StopFailure'] = [{'hooks': [{'type': 'command', 'timeout': 30, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-stop-failure', str(cfg_path), name, str(generation)])}]}]
    if name != ADVISOR:
        # Mid-turn handoff (design.md §34): the daemon never types into a Claude pane; this hook hands the
        # registered prompt to the model after the next tool call (decision block + reason).
        hooks['PostToolUse'] = [{'hooks': [{'type': 'command', 'timeout': 10, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-posttool', str(cfg_path), name, str(generation)])}]}]
    if name == ADVISOR:
        # Resident advisor (§27): the one-shot advisor's flags, only Read, and this file's hooks.
        settings.write_text(json.dumps({'hooks': hooks, 'autoMemoryEnabled': False,
                                        'env': dict(CLAUDE_ENV, **(claude_hook.ENV if hooked else {}))}))
        argv = advisor.resident_argv(cfg, cfg['run'], settings)
        if hooked:
            argv += ['--append-system-prompt', claude_hook.DECLARATION]
        return argv + ['--', prompt]  # --add-dir is variadic: end options first
    if clear_after_result.enabled(cfg, name):
        # exec ensures the PID reported by the hook is the process Claude waits for.
        hooks['Stop'][0]['hooks'][0]['command'] = 'exec ' + hook
        for event, verb in (('SessionStart', 'claude-clear'), ('StopFailure', 'claude-stop-failure')):
            entry = {'hooks': [{'type': 'command', 'timeout': 10, 'command': 'exec ' + shlex.join(
                [cfg['python'], str(Path(__file__).resolve()), verb, str(cfg_path), name, str(generation)])}]}
            if event == 'SessionStart':
                entry['matcher'] = 'clear'
            hooks[event] = [entry]
    if prompt_hook_slot(cfg, name, generation):
        # Operator input typed in the supervisor tab is queued as a daemon request (design.md §5).
        hooks['UserPromptSubmit'] = [{'hooks': [{'type': 'command', 'timeout': PROMPT_HOOK_TIMEOUT, 'command': shlex.join(
            [cfg['python'], str(Path(__file__).resolve()), 'claude-prompt', str(cfg_path), name, str(generation)])}]}]
    data = {'hooks': hooks, 'env': dict(CLAUDE_ENV, **(claude_hook.ENV if hooked else {}))}
    if advised:
        data['autoMemoryEnabled'] = False  # fleet workers have no memory layer (advisor plan §2)
    settings.write_text(json.dumps(data))
    argv = [cfg.get('claude') or 'claude', '--dangerously-skip-permissions', '--model', claude_model(cfg, name),
            # Unattended panes: a choice dialog would wait for a human forever (acceptance 10/13 P1-5).
            '--disallowedTools', 'AskUserQuestion',
            '--settings', str(settings), '--add-dir', str(Path(cfg['run']) / 'workspace')]
    if name != 'supervisor':  # stable per slot across generations (peer messages address it)
        argv += ['--name', claude_session_name(cfg, name)]
    if advised:
        # Only cwd/.claude (the linked domains) and cwd/mcp.json (the full kit set): no user-level
        # skills / agents / MCP servers. The Stop hook rides on --settings and still fires.
        argv += ['--setting-sources', 'project', '--strict-mcp-config']
        if cfg.get('worker_effort'):
            argv += ['--effort', cfg['worker_effort']]
    if hooked:
        argv += ['--append-system-prompt', claude_hook.DECLARATION]
    elif name != 'supervisor':  # typed / pasted delivery: the same promise for the pasted text
        argv += ['--append-system-prompt', claude_hook.TYPED_DECLARATION]
    if (cwd / 'mcp.json').exists():
        argv += ['--mcp-config', str(cwd / 'mcp.json')]
    if resume:
        argv += ['--resume', resume]
    return argv + ['--', prompt]  # --add-dir / --mcp-config are variadic: end options first


def claude_model(cfg, name):
    if name == ADVISOR:
        return advisor.MODEL
    if name == 'supervisor':
        return cfg.get('supervisor_model') or DEFAULTS['supervisor_model']
    return cfg.get('worker_model') or DEFAULTS['worker_model']


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
    cfg = agent_cfg(load(cfg_path), name)
    if name != ADVISOR:  # the advisor's CLI is its identity (§27), not a supervisor x worker pair choice
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
    cfg = agent_cfg(load(cfg_path), name)  # an advisor release may have rewritten this generation's snapshot
    os.chdir(agent_cwd(cfg, name))
    if is_pi(cfg, name):
        os.environ.update(pi_adapter.environment(cfg_path, cfg, name, generation))
        argv = pi_adapter.argv(cfg, name, generation, prompt, agent_cwd(cfg, name), label=info['label'])
        # pi rewrites its process title (ps shows only "pi"): keep the exact exec argv as launch evidence.
        record = Path(cfg['run']) / 'prompts' / (name + '-g' + str(generation) + '.argv.json')
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'slot': name, 'label': info['label'], 'generation': generation,
                                      'argv': argv, 'at': time.time()}, ensure_ascii=False) + '\n')
    elif is_opencode(cfg, name):
        # opencode TUI with fleet's plugin in-process (it spawns agent_bridge.py, like the pi extension).
        os.environ.update(opencode_adapter.environment(cfg_path, cfg, name, generation, label=info['label'],
                                                       startup_prompt=prompt,  # the plugin binds by this text
                                                       task_dir=agent_cwd(cfg, name)))
        argv = opencode_adapter.argv(cfg, name, generation, prompt, agent_cwd(cfg, name), label=info['label'])
        # Process cwd = this generation's empty launch dir (the plugin checks it before the startup turn);
        # the task dir stays outside the launch tree, so its files are never loaded as nested instructions.
        os.chdir(opencode_adapter.launch_cwd(cfg, name, generation))
        record = Path(cfg['run']) / 'prompts' / (name + '-g' + str(generation) + '.argv.json')
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'slot': name, 'label': info['label'], 'generation': generation,
                                      'argv': argv, 'at': time.time()}, ensure_ascii=False) + '\n')
    elif is_claude(cfg, name):
        # A daemon started from inside a Claude Code session must not pass its session markers on:
        # with CLAUDE_CODE_CHILD_SESSION set, Claude Code 2.1.280 saves no transcript, and the turn
        # settlement reads that transcript.
        for var in ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ID',
                    'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_PID'):
            os.environ.pop(var, None)
        os.environ.update(CLAUDE_ENV)
        if claude_hook.enabled(cfg, name, claude=True):
            os.environ.update(claude_hook.ENV)  # back-to-back deliveries are consecutive Stop blocks
        # Re-check trust at exec time: other Claude Code sessions rewrite ~/.claude.json and can drop
        # the entry spawn() wrote moments ago (real smoke 2026-10-13: worker-1 stuck on the trust dialog).
        trust_claude_dir(agent_cwd(cfg, name))
        argv = claude_argv(cfg_path, cfg, name, generation, prompt, resume=info.get('resume_session')
                           if info.get('resume_generation') == str(generation) else None)
    elif is_codex(cfg, name):
        argv = actor_argv(cfg_path, cfg, name, generation, prompt)
    else:  # agent_cli already refuses unknown CLIs; a new one must get its own branch here
        raise ValueError('no launch path for ' + agent_cli(cfg, name))
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
        # The resident advisor tab (§27) is a slot (bus, hooks, watchdog, handoff), never a worker. A restarted
        # daemon finds it from Redis; a new run starts it in start_all.
        advisor_info = self.r.hgetall(key('agent', ADVISOR))
        self.advisor_slot = advisor_info.get('role') == 'advisor' and advisor_info.get('state') != 'retired'
        self.names = self.slot_names()
        # Hot roster (start --roster): workers missing from `desired` are retiring. They finish
        # queued work, then retire; until then they still count as busy but take no new work.
        self.desired = list(self.workers)
        self.roster_stamp = self.roster_digest = None
        self.midturn_probe = midturn.Probe()
        self.handoff_table, self.worker_handoff = {}, {}  # roster `handoff:` / per-worker overrides (§22)
        self.roster_followed = None  # day or night roster file (night.json picks it)
        self.tab_specs, self.tabs_opened = None, False  # roster `tabs:` (§31): opened once per daemon start
        self.service = self.cfg.get('mode') == 'service'
        self.stopping = False
        self.next_liveness = 0
        self.last_chrome = 0
        self.explicit_stop = False
        self.resume_requested = False  # set by `daemon --resume` (operator ran start --resume)
        self.lifecycle_close = None  # set by lifecycle_close.request_close: why/how this run is being closed
        self.close_workspace_pending = False
        self.notifier = None  # tests: replaces the TG / parent channels (lifecycle_close.notify)
        if self.service:
            brake.validate_budget(self.cfg.get('budget'))
        elif self.cfg.get('budget') is not None:
            raise ValueError('config budget is for service mode; a task carries its own budget')
        argv = self.cfg.get('brake_notify')
        if argv is not None and (not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv)):
            raise ValueError('brake_notify must be an argv list of strings (no shell)')
        self.pending_notify = []  # brake trips recorded inside a v2 event transaction (notified on tick)
        self.v2 = runtime_v2.RuntimeV2(self) if self.transport_version == 2 else None
        self.kit, self.advisor_runner = None, None  # advisor online only; tests inject advisor_runner
        # The roster's `advisor: on|off` is the only switch (advisor plan §2.1). self.cfg['advisor'] is its
        # in-memory mirror, never saved to config.json; per-generation snapshots carry it to the actor.
        self.cfg.pop('advisor', None)
        if self.roster_advisor():
            if self.cfg.get('worker_kit'):
                self.kit = kitmod.Kit(self.cfg['worker_kit'])  # KitError (ValueError): refuse the whole kit
            self.cfg['advisor'] = True
            self.r.hset(SERVICE, mapping={'advisor': 'on', 'advisor_by': 'user', 'advisor_at': time.time()})
            self.log('advisor.on', who='user', at_start=True)
        elif self.r.hget(SERVICE, 'advisor') == 'on':  # resumed after the roster turned it off
            self.r.hset(SERVICE, mapping={'advisor': 'off', 'advisor_by': 'user', 'advisor_at': time.time()})

    # -- advisor switch (advisor plan §2.1). Switch and readiness are two questions (design §27):
    # advisor_enabled() = the roster switch (holds first generations, accepts consults, off notes);
    # advisor_ready() = a consultation can be answered now (the resident tab is idle / busy).
    def advisor_enabled(self):
        return self.cfg.get('advisor') is True

    advisor_online = advisor_enabled  # the earlier name of the switch

    def advisor_resident(self):
        """Consultations go to the resident advisor tab: transport v2, a main fleet (task or service mode,
        10-14 Studio ops fleet). Otherwise (v1, a sub-fleet, or a test that injects advisor_runner) the
        one-shot `claude -p` runner answers them at once, as before."""
        return (bool(self.v2) and not self.cfg.get('adopt') and not self.cfg.get('parent_config')
                and self.advisor_runner is None)

    def advisor_ready(self):
        if not self.advisor_enabled():
            return False
        return not self.advisor_resident() or (self.advisor_slot and self.agent(ADVISOR).get('state') in ('idle', 'busy'))

    def slot_names(self):
        return ['supervisor'] + self.workers + ([ADVISOR] if self.advisor_slot else [])

    def roster_advisor(self):
        """advisor on/off from the roster file at daemon start (a bad file: off; roster_tick reports it)."""
        path = roster_file(self.cfg)
        try:
            raw = path.read_bytes()
            roster.parse(raw, path.parent)
            return roster.advisor(raw)
        except (OSError, ValueError):
            return False

    def set_advisor(self, on, who, *, write=False):
        """Turn the advisor on / off without a restart. ``write``: also set it in the roster file (the
        supervisor's decision key). Returns None, or why nothing changed. Idempotent on equal values."""
        kit = self.kit
        if on and not self.advisor_online() and self.cfg.get('worker_kit'):
            try:
                kit = kitmod.Kit(self.cfg['worker_kit'])  # a bad kit refuses the switch, like at start
            except (OSError, ValueError) as exc:
                self.log('advisor.rejected', level='error', who=who, error=str(exc)[:500])
                return 'worker kit rejected: ' + str(exc)[:500]
        if write:
            try:
                for path in advisor_rosters(self.cfg):  # the roster stays the truth (day and night alike)
                    roster.write_advisor(path, on, self.desired)
            except (OSError, ValueError) as exc:
                self.log('advisor.rejected', level='error', who=who, error=str(exc)[:500])
                return 'roster not written: ' + str(exc)[:500]
        if on == self.advisor_online():
            return None
        if on:
            self.kit = kit
            self.cfg['advisor'] = True
        else:
            # Running workers and their sheet-launched generations stay; a consultation already made
            # finishes (a queued hot-load still applies). No new consultations, no slot stays held.
            self.cfg.pop('advisor', None)
            for name in self.workers:
                if self.r.llen(ADVICE_PENDING + name):
                    self.r.delete(ADVICE_PENDING + name)
                self.drop_advice_pending(name, 'advisor turned off')  # the message goes out with its runtime now
                info = self.agent(name)
                if info.get('advice_hold') == '1' and info.get('launch_ready') == '0':
                    self.release_default(name)
                else:
                    self.drop_advice_wait(name)
            rescue = json.loads(self.r.get(RESCUE_PENDING) or 'null')
            if rescue:  # the consult out with the resident advisor: answered like one queued while on
                self.r.delete(RESCUE_PENDING)
                self.advisor_abandon(rescue['ticket'], 'advisor turned off')
                if self.running():
                    self.queue('supervisor', self.consult_off_note([rescue['req']]) + '\nReply with the JSON format from AGENTS.md.')
        state = 'on' if on else 'off'
        self.r.hset(SERVICE, mapping={'advisor': state, 'advisor_by': who, 'advisor_at': time.time()})
        self.log('advisor.' + state, who=who)
        return None

    def advisor_decision(self, decision):
        """The supervisor's `advisor` key, and the off-gate for `consult` (advisor plan §2.1)."""
        if decision.get('advisor') in ('on', 'off'):
            error = self.set_advisor(decision['advisor'] == 'on', 'supervisor', write=True)
            if error:
                self.r.rpush(ADVICE_NOTES, '[advisor] advisor: "' + decision['advisor'] + '" 未生效：' + error)
        if decision.get('consult') and decision['status'] == 'RUNNING' and not self.advisor_online():
            note = self.consult_off_note(decision['consult'])
            if decision['dispatch']:
                self.r.rpush(ADVICE_NOTES, note)  # rides on the next supervisor message (a report will come)
            else:  # consult-only reply: nothing else would wake the supervisor
                self.queue('supervisor', note + '\nReply with the JSON format from AGENTS.md.')

    def consult_off_note(self, items):
        """The one `[advisor off]` answer to consult items (at intake, or queued while on then turned off)."""
        which = ', '.join(x['task'] + ' (' + self.label(self.resolve(x.get('slot') or x['worker'])) + ')' for x in items)
        return ('[advisor off] consult ' + which + ' 未执行：顾问不在线。先在 fleet_decision 里发 advisor: "on"，'
                '再发 consult；或自己派纠正性 dispatch / BLOCKED')

    # -- small helpers
    def agent(self, name):
        return self.r.hgetall(key('agent', name))

    def runtime_cfg(self, name):
        if name == ADVISOR:  # fixed: official Claude CLI, fable / high, no launch record (§27)
            cfg = {k: v for k, v in self.cfg.items() if k != 'advisor'}
            cfg.update(worker_cli='claude', worker_model=advisor.MODEL, worker_effort=advisor.EFFORT)
            return cfg
        launch = json.loads(self.agent(name).get('runtime_launch') or '{}')
        cfg = pi_adapter.for_agent(self.cfg, name, launch.get('overrides'))
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
            if self.r.llen(ADVICE_NOTES):
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

    def preflight_profile(self, launch, cli_change_ok=False):
        """Check dependencies before retiring anything. Never select a fallback CLI.
        cli_change_ok: only the roster (night) swap passes True -- each of its slots already passed the
        pair table in night.resolve_launches (BC review r1 P1-01); every other path keeps the refusal."""
        cfg = dict(self.cfg, **launch['overrides'])
        if (self.v2 and not cli_change_ok and cfg.get('worker_cli') != (self.cfg.get('worker_cli') or 'codex')):
            # The pair table admits whole-run supervisor x worker CLI pairs; a v2 profile live switch
            # that changes one slot's CLI (claude <-> pi bridge) mixes worker transports in one run,
            # which no smoke covers. Kept refused explicitly now that mixed pairs can be verified.
            raise ValueError('transport v2 profile live switch cannot change the worker CLI ('
                             + str(self.cfg.get('worker_cli')) + ' -> ' + str(cfg.get('worker_cli'))
                             + '); start a run with that worker CLI instead')
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
                                              cfg.get('pi_agent_dir') or str(Path.home() / '.pi/agent'),
                                              fleet_config.block('pi_context'))
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
        # An online advisor authorizes its sheets' respawns; every other safety step below is unchanged.
        if (not self.advisor_online()
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
                  runtime_profile='', profile_state='switching', launch_ready=0, retired_pane=old,
                  retired_launch=info.get('runtime_launch') or '')
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
            self.setf(name, retired_pane='', retired_launch='', launch_ready=1)
            self.refresh_history()
            self.log('profile.switch', agent=name, label=label, generation=generation + 1,
                     profile=launch['profile_id'], cli=launch['overrides']['worker_cli'])
        except (Exception, SystemExit) as exc:
            # Revoke launch before cleanup; even a late READY cannot revive this generation.
            self.setf(name, launch_ready=0, state='dead')
            new = new or self.agent(name).get('pane')
            if new == old:
                new = None
            cleaned = True
            if new:
                try:
                    if not self.mgr.kill_pane(new):
                        cleaned = False
                        self.log('profile.cleanup_failed', agent=name, pane=new)
                except Exception as cleanup:
                    cleaned = False
                    self.log('profile.cleanup_failed', agent=name, pane=new, reason=str(cleanup))
            # Roll back the reservation (model_usage): with the new tab gone the slot holds only what
            # its old generation runs, and only while that tab is still alive. A new tab that could
            # not be closed keeps holding the new model.
            if cleaned:  # the whole launch record, not only its runtime half
                self.setf(name, runtime_launch=info.get('runtime_launch') or '',
                          installed_domains=info.get('installed_domains') or '')
            try:
                gone = self.mgr.get_pane_status(old) is None
            except Exception:
                gone = False
            if gone:
                self.setf(name, retired_pane='', retired_launch='')
            self.block_profile(name, 'profile switch failed: ' + str(exc), dead=True)

    def workspace(self):
        return self.run_dir / 'workspace'

    def fleet_cmd(self):
        return shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'chrome', '--config', str(self.path)])

    def role_scope(self, name, cfg, launch):
        """The AGENTS.md text of one agent (also written by a sub-fleet over its adopted pane)."""
        role = 'supervisor' if name == 'supervisor' else 'worker'
        # v2 strips the [Request <id>] header (a transport id) before delivery and binds the id in
        # code, so an official-CLI service supervisor gets the JSON-text role without request ids.
        variant = ('_v2' if self.v2 and is_bridge(cfg, name) else
                   '_tui_v2' if self.v2 and self.service and role == 'supervisor' else '')
        scope = (HERE / 'agents' / (('service_' if self.service else '') + role + variant + '.md')).read_text()
        if launch.get('role_text') and launch['role_text'].strip() != scope.strip():
            scope += '\n## Assigned profile role\n\n' + launch['role_text']
        roles = self.cfg.get('worker_roles') or {}
        extra = (Path(roles[name]) if name in roles else
                 Path(self.cfg['roles']).expanduser() / (name + '.md') if self.cfg.get('roles') else None)
        if extra and extra.is_file():  # this agent's own prompt (one per worker in service mode)
            scope += '\n## 4. Your role (' + name + ')\n\n' + extra.read_text()
        if self.contract_phase():  # successors / roster-added workers inside a contract task
            scope += '\n' + self.contract_doc(role, name)
        if role == 'worker':  # every review inside a fleet follows the fleet copy of the review rules
            scope += REVIEW_POINTER
        scope = scope.replace('{{WORKSPACE}}', str(self.workspace())).replace('{{FLEET_CHROME}}', self.fleet_cmd())
        sandbox_notice = ('You run inside a sandbox. Driving the user\'s browser directly (AppleScript, GUI automation) '
                          'fails there with an error. That error means the sandbox stopped you, not that the browser '
                          'is broken. Browser actions go only through the broker command below, if enabled.')
        scope = scope.replace('{{SANDBOX_NOTICE}}', sandbox_notice if agent_cli(cfg, name) == 'codex'
                              else pi_adapter.UNSANDBOXED_SCOPE)
        if is_bridge(cfg, name):
            scope += '\n## Runtime permissions\n\n' + pi_adapter.UNSANDBOXED_SCOPE + '\n'
        return scope

    # -- actors
    def spawn(self, name, prompt, *, launch_held=False, advice_hold=False, advised=None):
        """``advised``: only a slot's first generation (spawn_ready) passes it, from the advisor switch;
        every later generation replays the slot's launch record (advised_layout), whatever the switch says."""
        if name == ADVISOR:
            return self.spawn_advisor(prompt, launch_held=launch_held)
        cfg = self.runtime_cfg(name)
        info = self.agent(name)
        generation = int(info.get('generation', 0))
        launch = json.loads(info.get('runtime_launch') or '{}')
        cfg_path = self.path
        if advised is None:
            advised = self.advised_layout(name)
        if not is_claude(cfg, name) and info.get('advice_domains'):
            self.setf(name, advice_domains='')  # a codex / pi sheet stages no kit: never left behind
        # This generation's argv (--effort / --setting-sources project) follows the layout, never the
        # in-memory switch that runtime_cfg copies from self.cfg.
        cfg.pop('advisor', None)
        if advised and is_claude(cfg, name):
            cfg['advisor'] = True
        # A held first spawn gets its model / effort written here on release; an advised Claude
        # generation keeps `advisor` in its own snapshot (the actor builds its argv from it).
        if launch or advice_hold or (advised and is_claude(cfg, name)):
            cfg_path = self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json')
            save(cfg_path, cfg)  # generation-specific callback config; never edit old snapshots
        cwd = agent_cwd(self.cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        if agent_cli(cfg, name) == 'codex' and not codex_queue.is_codex(cfg, name):
            self.log('queue.unsupported', agent=name, generation=generation, codex=cfg.get('codex') or '',
                     version=cfg.get('codex_version') or '', fallback='typed')
        role = 'supervisor' if name == 'supervisor' else 'worker'
        scope = self.role_scope(name, cfg, launch)
        if is_opencode(cfg, name):
            # The role lives only in this generation's fresh instance dir ($OPENCODE_CONFIG_DIR/AGENTS.md):
            # an AGENTS.md / CLAUDE.md in the cwd would be loaded as project / nested instructions.
            opencode_adapter.prepare_instance(cfg, name, generation, scope, task_dir=cwd)
            prompt = prompt.replace(' Read AGENTS.md in the current directory first.', '').replace(' Read AGENTS.md first.', '')
        else:
            (cwd / 'AGENTS.md').write_text(scope)
        if role == 'worker':
            (cwd / 'REVIEWER.md').write_text((HERE / 'agents' / 'reviewer.md').read_text())
        if is_claude(cfg, name) and advised:
            # CLAUDE.md = scope + core + domain rules. A held first generation has not started: its record
            # is written when the gate opens (release_hold with the sheet, release_default with nothing).
            self.install_advised_kit(name, cwd, scope, record=not advice_hold)
            # CLAUDE.md already carries the scope: do not make Claude read the same text twice.
            prompt = prompt.replace(' Read AGENTS.md in the current directory first.', '').replace(' Read AGENTS.md first.', '')
            trust_claude_dir(cwd)
        elif is_claude(cfg, name):
            (cwd / 'CLAUDE.md').write_text(scope)  # Claude Code reads CLAUDE.md, not AGENTS.md
            self.install_default_kit(cwd, scope)
            trust_claude_dir(cwd)
        elif is_codex(cfg, name):
            trust_agent_dir(codex_home(self.cfg), cwd)
        if codex_queue.unsettled(self.agent(name)):
            codex_queue.clear(self, name, 'respawn')  # audited; its token can never settle in the new session
        self.setf(name, role=role, generation=generation, state='starting', thread='', pane='',
                  transcript='', ctx_used=0, ctx_window=0, msg_id='', msg_done='',
                  busy_since=time.time(), delivered=0, retry_at=0, last_msg='', empty_tries=0,
                  launch_ready=int(not launch_held), runtime_ready=0, current_profile='', idle_confirmed=0,
                  **{k: '' for k in codex_queue.FIELDS})  # a new session starts without a queued delivery
        if self.v2:
            self.setf(name, bridge_available=0, startup_deadline=time.time() + float(self.cfg['profile_ready_timeout']))
        if advised or info.get('advice_hold'):  # a resumed, never-released held slot must not stay marked held
            self.setf(name, advice_hold=int(advice_hold))
        if launch:
            self.setf(name, profile_state='switching', runtime_profile=launch['profile_id'],
                      profile_ready_deadline=time.time() + float(self.cfg['profile_ready_timeout']))
        prompt_file = self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.md')
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt)
        result = self.mgr.spawn_interactive_cli(
            window_name='fx-' + self.cfg.get('tab_prefix', '') + self.label(name), prompt_file=str(prompt_file),
            start_directory=str(opencode_adapter.launch_cwd(cfg, name, generation) if is_opencode(cfg, name) else cwd),
            cli_command=shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'actor', str(cfg_path), name,
                                    '--generation', str(generation)]),
            output_file=str(self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.terminal.log')),
            auto_close=False, metadata={'type': role, 'cli_type': agent_cli(cfg, name),
                      'profile': (cfg.get('pi_model') if is_pi(cfg, name) else
                                  cfg.get('opencode_model') if is_opencode(cfg, name) else
                                  claude_model(cfg, name) if is_claude(cfg, name) else agent_profile(cfg, name))},
            env_vars=dict({'PATH': os.environ.get('PATH', '')}, **(
                pi_adapter.environment(cfg_path, cfg, name, generation) if is_pi(cfg, name)
                else opencode_adapter.environment(cfg_path, cfg, name, generation, label=self.label(name),
                                                  startup_prompt=prompt, task_dir=cwd) if is_opencode(cfg, name)
                else dict({'CODEX_HOME': str(codex_home(self.cfg))}, **(CLAUDE_ENV if is_claude(cfg, name) else {})))),
            **subfleet.tmux_spawn_args(self.cfg, name, generation))
        if not result.success:
            if result.pane_id:
                self.setf(name, pane=result.pane_id)
            raise RuntimeError('spawn failed: ' + name + ': ' + str(result.error))
        self.setf(name, pane=result.pane_id)
        self.log('spawn', agent=name, generation=generation, pane=result.pane_id)
        return result.pane_id

    def spawn_advisor(self, prompt, launch_held=False):
        """One generation of the resident advisor tab (§27): fixed argv (claude_argv), cwd <run>/advisor, the
        role prompt <run>/advisor.md; no AGENTS.md, no kit, no domains, no launch record."""
        name = ADVISOR
        generation = int(self.agent(name).get('generation') or 0)
        prompt = prompt.replace(' Read AGENTS.md in the current directory first.', '')  # its role is advisor.md
        cfg_path = self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json')
        save(cfg_path, self.runtime_cfg(name))
        cwd = agent_cwd(self.cfg, name)
        cwd.mkdir(parents=True, exist_ok=True)
        (self.run_dir / 'handoffs').mkdir(parents=True, exist_ok=True)  # --add-dir must exist
        (self.run_dir / 'advisor.md').write_text(advisor.PROMPT.read_text() + advisor.RESIDENT_NOTE)
        trust_claude_dir(cwd)
        self.setf(name, role='advisor', generation=generation, state='starting', thread='', pane='',
                  transcript='', ctx_used=0, ctx_window=0, msg_id='', msg_done='', busy_since=time.time(),
                  delivered=0, retry_at=0, last_msg='', empty_tries=0, launch_ready=int(not launch_held),
                  runtime_ready=0, idle_confirmed=0, bridge_available=0, spawn_failed=0, advisor_fault='',
                  startup_deadline=time.time() + float(self.cfg['profile_ready_timeout']))
        prompt_file = self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.md')
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt)
        result = self.mgr.spawn_interactive_cli(
            window_name='fx-' + self.cfg.get('tab_prefix', '') + self.label(name), prompt_file=str(prompt_file),
            start_directory=str(cwd),
            cli_command=shlex.join([self.cfg['python'], str(Path(__file__).resolve()), 'actor', str(cfg_path), name,
                                    '--generation', str(generation)]),
            output_file=str(self.run_dir / 'prompts' / (name + '-g' + str(generation) + '.terminal.log')),
            auto_close=False, metadata={'type': 'advisor', 'cli_type': 'claude', 'profile': advisor.MODEL},
            env_vars=dict({'PATH': os.environ.get('PATH', '')}, **CLAUDE_ENV))
        if not result.success:
            if result.pane_id:
                self.setf(name, pane=result.pane_id)
            raise RuntimeError('spawn failed: ' + name + ': ' + str(result.error))
        self.setf(name, pane=result.pane_id)
        self.log('spawn', agent=name, generation=generation, pane=result.pane_id)
        return result.pane_id

    def advised_layout(self, name):
        """Whether this slot's next generation is laid out advised (domain kit links + RULES in CLAUDE.md,
        --setting-sources project, --strict-mcp-config, --effort), read only from the slot's launch record:
        installed_domains '' = the default layout (user-level settings, full kit copy), a JSON list (even
        []) = advised; a sheet staged in advice_domains for this spawn counts as advised. The advisor switch
        is never asked: a handoff / respawn / resume successor launches exactly like its predecessor, and
        only an applied advisor sheet changes the record (2026-10-14)."""
        if name == 'supervisor':
            return False
        info = self.agent(name)
        return bool(info.get('advice_domains') or info.get('installed_domains'))

    def init_worker(self, name):
        self.setf(name, label=name, current_profile='', runtime_profile='', advisory_profile='',
                  runtime_ready=0, runtime_launch='', profile_task='', installed_domains='', advice_domains='',
                  **profile_runtime.ProfileApplication().metadata())
        self.r.hset(NAMES, mapping={name: name})

    def spawn_ready(self, name):
        # v2 + advisor online: a Claude worker waits behind the launch gate for its first sheet (no
        # default generation that the first dispatch would have to replace).
        # A named sub-fleet's supervisor_slot never waits: up / autostart need it idle and READY, and a sheet only
        # comes with a delivery, which a supervisor slot never gets before its sub-fleet is up (10-14 Studio deadlock).
        sub_supervisor = name in {e.get('supervisor_slot') for e in (self.cfg.get('subfleets') or [])}
        hold = (bool(self.v2) and self.advisor_online() and name != 'supervisor' and not sub_supervisor
                and is_claude(self.runtime_cfg(name), name))
        # The first generation is the one place the switch picks the layout; later ones replay the record.
        return self.spawn(name, 'You are fleet agent ' + name + '. Read AGENTS.md in the current directory first. '
                                'There is no task yet: your final reply must be exactly READY.',
                          launch_held=hold, advice_hold=hold,
                          advised=self.advisor_online() and name != 'supervisor')

    def start_all(self):
        if self.cfg.get('adopt'):
            return subfleet.start_adopted(self)  # sub-fleet: the delegated worker pane is the supervisor
        self.setf('supervisor', label=SUPERVISOR_LABEL)
        for name in self.workers:
            self.init_worker(name)
            self.apply_roster_launch(name)
        for name in self.names:
            self.spawn_ready(name)
        try:  # the fleet default is accepted as started; say when it is over a model card's ticket count
            over = model_usage.over_cap(self.model_usage(), AgentRegistry())
        except (AgentRegistryError, OSError):
            over = []
        if over:
            self.log('capacity.over', level='warning', models=over,
                     reason='card max_concurrent counts slots; later profile switches into these models wait')
        history.write_page(self.run_dir, self.history_slots(), self.history_links())
        # One page: supervisor above history/control on the left, worker tabs on the right.
        self.history_pane = self.mgr.arrange(self.agent('supervisor')['pane'],
                                             [self.agent(w)['pane'] for w in self.workers],
                                             history.page_url(self.run_dir))
        self.announce_history_page()
        self.r.hset(SERVICE, mapping={'history_pane': self.history_pane or ''})
        if self.advisor_resident() and self.advisor_enabled():
            self.advisor_start()  # its tab next to the supervisor's; the held first workers wait for it

    def history_slots(self):
        # Workers removed from the roster keep their history (they have entries but no slot).
        retired = [s for s in dict.fromkeys(e.get('slot') for e in history.entries(self.run_dir))
                   if s and s not in self.names]
        return [(n, self.label(n), SUPERVISOR_LABEL if n == 'supervisor' else n) for n in self.names + retired]

    def history_links(self):
        return subfleet.page_links(self.cfg)

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
            except named_subfleet.InProgress as exc:  # §29: a retry of an op still open
                answer = {'status': 'in_progress', 'id': exc.rid, 'op_status': exc.status}
            except (ValueError, TypeError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
        elif kind == 'operator_input':
            try:
                info = self.agent('supervisor')
                if (ev.get('agent') != 'supervisor'
                        or not (is_pi(self.cfg, 'supervisor') or prompt_hook_slot(self.cfg, 'supervisor', ev.get('generation')))
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
            answer = {'status': 'accepted'}
            if kind == 'pause':
                self.pause(ev.get('reason') or 'paused by operator')
            else:
                try:
                    self.brake_resume(ev.get('raise'))
                    self.resume()
                except (ValueError, TypeError) as exc:
                    answer = {'status': 'rejected', 'reason': str(exc)}
            self.r.set('fx:reply:' + ev['request_id'], json.dumps(answer, ensure_ascii=False), ex=86400)
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
            if hold and ok is None:  # sub-fleet attempts used up (escalate_intake): no new spawn
                s['tasks'][hold].update(status='failed', reason='escalate refused: ' + subfleet.EXHAUSTED
                                        + ' reached, no new sub-fleet')
                self.contract_log('task.failed', task=hold, by='program', reason=s['tasks'][hold]['reason'])
            elif hold:
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
            claimed = {i + '@' + str(v) for i, v in contracts.ready_claims(str(fields.get('interfaces', '')))}
            unclaimed = [contracts.ref(e) for e in current if e and e['status'] != 'ready'
                         and contracts.ref(e) not in claimed and not contracts.file_failures(e, ws)]
            if unclaimed:  # file passes, announcement line missing/garbled: say which literal line
                line = 'Interfaces: ' + ', '.join(x + ' provided: ready' for x in unclaimed)
                row['reason'] += ' (the report has no line `' + line + '`; ' + self.label(name) + ' was asked for it)'
                self.contract_queue(s, name, '[contract] ' + row['id'] + ' is not done: your report did not announce '
                                    + ', '.join(unclaimed) + '. If the file is written, reply in the AGENTS.md format '
                                    'with this exact line: ' + line, row['id'])
                notes.append('[contract] ' + row['id'] + ': the file passes but the report misses `' + line + '`; '
                             + self.label(name) + ' was asked for that line (no re-dispatch needed)')
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
        # Service mode holds only plan F' [dialog] / [blocked] records here (no split requests).
        raw = self.r.get(ESCALATIONS)
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
            if state is not None and str(exc).startswith(subfleet.EXHAUSTED):
                # Contract task out of sub-fleet attempts: the task fails here, the supervisor decides.
                return None, None, ('\n[escalate] refused for ' + self.label(name) + ': ' + str(exc)
                                    + '. The task is marked failed; re-dispatch it (another approach or worker) '
                                    'or end the task.'), None
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

    def report_item(self, name):
        """The task id a worker's next report answers ('' if none): its contract owner, else its running row,
        else the last plain dispatch id given to it."""
        state = self.contract_state()
        if state is not None:
            owners = state['owners'].get(name) or []
            if owners and owners[0] in state['tasks']:
                return owners[0]
            return next((t['id'] for t in state['tasks'].values() if t.get('to') == name and t['status'] == 'running'), '')
        ids = [i for i, row in json.loads(self.r.get(ITEMS) or '{}').items() if row.get('to') == name]
        return ids[-1] if ids else ''

    def blocked_intake(self, name, item, reason):
        """Plan F' 8.4: a worker's Blocked: goes to the supervisor to rule on (not straight to BLOCKED).
        Returns (escalation key, note appended to the report for the supervisor)."""
        key_ = BLOCKED_KEY + name
        esc = self.escalation_state()
        esc[key_] = {'kind': 'blocked', 'slot': name, 'item': item, 'reason': reason[:2000], 'status': 'escalated',
                     'asked_at': time.time()}
        self.save_escalations(esc)
        self.log('blocked.asked', agent=name, item=item, reason=reason[:300])
        who = self.label(name)
        return key_, ('\n[blocked] ' + who + ' says it is stuck on: ' + reason[:2000].replace('\n', '; ')
                      + '\nAnswer in your next JSON: "escalations": [{"task": "' + key_ + '", "decision": "approve" | '
                      '"revise" | "reject", "note": "..."}]. approve = it may go on the way it proposed (say in note '
                      'how far that authorisation goes); revise = another way (note required); reject = drop this '
                      'part and go on with the rest as the note says. Dispatching a new instruction to ' + who
                      + ' in the same reply also answers it. Reply status BLOCKED only for a delete / move, or for '
                      'what truly only the user can decide, and say which in reason.')

    def escalations_missed(self, decision):
        """Open dialog / blocked escalations whose message the supervisor already has but this RUNNING reply
        neither answers nor (blocked) dispatches over. The first miss is sent back without a strike; then a
        dialog goes to the user and a blocked miss counts as a strike."""
        if self.v2 is None or decision['status'] != 'RUNNING':
            return
        esc = self.escalation_state()
        answered = {x['task'] for x in decision['escalations']}
        # Task mode: a new instruction to (or consult on) the worker answers its Blocked. Service mode: a dispatch
        # routes another request, so it answers nothing.
        dispatched = set() if self.service else {BLOCKED_KEY + self.resolve(x['to']) for x in decision['dispatch']}
        dispatched |= {BLOCKED_KEY + self.resolve(c['worker']) for c in decision.get('consult', [])}
        missed = [k for k, rec in esc.items() if k.startswith((DIALOG_KEY, BLOCKED_KEY))
                  and rec.get('status') == 'escalated' and rec.get('message_id') and k not in answered
                  and k not in dispatched and self.v2.bus.status(rec['message_id']) not in ('', 'queued')]
        if not missed:
            return
        first = [k for k in missed if not esc[k].get('nudged')]
        for k in first:
            esc[k]['nudged'] = 1
        if first:
            self.save_escalations(esc)
            raise NoStrike('answer ' + ', '.join(first) + ' first: "escalations": [{"task": "' + first[0]
                           + '", "decision": ...}] (see the [dialog] / [blocked] lines)')
        for k in [k for k in missed if k.startswith(DIALOG_KEY)]:
            rec = esc.pop(k)
            self.save_escalations(esc)
            self.log('dialog.to_human', agent=rec['slot'], task=k, reason='supervisor did not answer')
            self.v2.audit('dialog.to_human', slot=rec['slot'], level='warn', reason='supervisor_unanswered', task=k)
            self.v2.prompt_wait(rec['slot'], rec['scope'], 'dialog ' + rec['prompt'] + ' not answered by the '
                                'supervisor twice; handed to the user', rec['screen'], kind='other')
        blocked = [k for k in missed if k.startswith(BLOCKED_KEY)]
        if blocked:
            raise ValueError('escalations: ' + ', '.join(blocked) + ' still waits for your answer (approve / revise / '
                             'reject, or dispatch to that worker)')

    def escalation_decision(self, decision, state):
        """Validate the supervisor's escalations against the open requests (no side effects)."""
        open_ = self.escalation_state()
        busy = {self.resolve(x['to']) for x in decision['dispatch']}
        out = []
        for item in decision['escalations']:
            rec = open_.get(item['task'])
            kind = (rec or {}).get('kind', 'split')
            if item['task'].startswith((DIALOG_KEY, BLOCKED_KEY)) and (not rec or rec.get('status') != 'escalated'):
                # The dialog went away / was answered in the pane / went to the user meanwhile: not a bad reply.
                self.log('escalate.stale_answer', task=item['task'], decision=item['decision'])
                continue
            if not rec or rec.get('status') != 'escalated':
                raise ValueError('escalations: ' + item['task'] + ' has no open escalate request')
            if kind == 'dialog':
                if item['decision'] not in ('approve', 'reject'):
                    raise ValueError('escalations: ' + item['task'] + ' is a dialog: answer approve or reject')
                out.append((item['task'], rec, item))
                continue
            if kind == 'blocked':
                if item['decision'] == 'revise' and not item.get('note', '').strip():
                    raise ValueError('escalations: revise of ' + item['task'] + ' needs a note saying the other way')
                if rec['slot'] in busy and not self.service:
                    raise ValueError('escalations: ' + item['task'] + ' is answered and ' + self.label(rec['slot'])
                                     + ' is dispatched to in the same reply; do one or the other')
                out.append((item['task'], rec, item))
                continue
            if item['task'] in {x.get('id') for x in decision['dispatch']}:
                raise ValueError('escalations: ' + item['task'] + ' is also dispatched in this reply; do one or the other')
            if item['decision'] == 'approve' and rec['slot'] in busy:
                raise ValueError('escalations: approve ' + item['task'] + ' hands ' + self.label(rec['slot'])
                                 + ' over to a sub-fleet; do not dispatch to it in the same reply')
            out.append((item['task'], rec, item))
        return out

    def escalation_apply(self, t, state, task_id, rec, answer):
        if rec.get('kind') == 'dialog':
            return self.dialog_answer(task_id, answer)
        if rec.get('kind') == 'blocked':
            return self.blocked_ruling(t, state, task_id, rec, answer)
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
            output, outputs, goal = None, None, rec['instruction']
            if row is not None:
                entries = [e for e in (contracts.current(state, i) for i in row.get('provides') or []) if e]
                outputs = [str(self.workspace() / e['path']) for e in entries]  # parent paths stay parent paths
                output = outputs[0] if outputs else None
                if entries:  # refs only: the child writes inside its own workspace (subfleet.goal_text)
                    goal += ('\nThe parent fleet verifies these interfaces of ' + task_id + ' (shapes in '
                             + str(self.workspace() / 'contracts.json') + ', read only): '
                             + ', '.join(contracts.ref(e) for e in entries))
                row.update(status='delegated', attempts=row.get('attempts', 0) + 1, reason='sub-fleet ' + task_id)
            if note:
                goal += '\nSupervisor note: ' + note
            rid = subfleet.request(self.run_dir, slot, task_id, goal, workers, output=output,
                                   proposal=rec['proposal'], contracts=state is not None, outputs=outputs)
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

    def dialog_answer(self, task_id, answer):
        """Plan F' 8.3: record the supervisor's ruling only. The watchdog presses the keys on its next tick,
        after re-checking identity, the screen digest and the delete classifier (no side effect here)."""
        esc = self.escalation_state()
        rec = esc[task_id]
        if rec.get('channel') == 'permission_reply':
            return self.permission_answer(task_id, rec, answer)
        rec.update(status='answered', decision=answer['decision'], note=answer.get('note', '').strip(),
                   answered_at=time.time())
        self.save_escalations(esc)
        self.log('dialog.answered', task=task_id, decision=answer['decision'])
        if self.v2:
            self.v2.audit('dialog.answered', slot=rec['slot'], level='info', task=task_id, decision=answer['decision'])

    def permission_answer(self, task_id, rec, answer):
        """opencode: the supervisor's ruling on a non-delete permission request goes to the slot's bridge as
        one permission_reply UI card naming this run / slot / generation / session / request (the bridge
        forwards it only if every field matches what it reported). No key is pressed. A request whose slot
        moved on (generation, session or delivery changed) is dropped and nothing is sent."""
        esc = self.escalation_state()
        esc.pop(task_id, None)
        self.save_escalations(esc)
        slot = rec['slot']
        info = self.agent(slot)
        lease = self.r.hgetall(self.v2.bus.lease_key(slot)) if self.v2 else {}
        pending = json.loads(info.get('oc_permission') or 'null') or {}
        scope = lease.get('current_delivery') if lease.get('status') == 'received' else 'startup-g' + str(info.get('generation', '0'))
        if (not self.v2 or str(info.get('generation', '')) != rec['generation'] or info.get('thread', '') != rec['session_id']
                or pending.get('request_id') != rec['request_id'] or scope != rec['scope']):
            self.log('dialog.stale_answer', task=task_id, decision=answer['decision'])
            if self.v2:
                self.v2.audit('dialog.stale', slot=slot, level='info', reason='identity', task=task_id)
            return
        decision = 'once' if answer['decision'] == 'approve' else 'reject'
        card = {'kind': 'permission_reply', 'run_id': self.v2.bus.run_id, 'slot': slot,
                'generation': int(rec['generation']), 'session_id': rec['session_id'], 'request_id': rec['request_id'],
                'class': 'other', 'decision': decision,
                'metadata': {'event_id': 'ui-ocreply-' + hashlib.sha256((slot + ':' + rec['request_id']).encode()).hexdigest()[:32]}}
        self.v2.ui(slot, card)
        self.setf(slot, prompt_wait='', prompt_wait_at=0, prompt_route='', prompt_channel='', oc_permission='')
        self.log('dialog.answered', task=task_id, decision=answer['decision'])
        self.v2.audit('opencode.permission_reply', slot=slot, level='info', task=task_id, decision=decision,
                      request_id=rec['request_id'], note=answer.get('note', '').strip())

    def blocked_ruling(self, t, state, task_id, rec, answer):
        """Plan F' 8.4: the supervisor's ruling goes to the worker as an instruction (contract task: under its row)."""
        esc = self.escalation_state()
        esc.pop(task_id, None)
        self.save_escalations(esc)
        slot, item, note = rec['slot'], rec.get('item') or '', answer.get('note', '').strip()
        if self.service:
            return self.service_ruling(task_id, rec, answer, note)
        text = ('[Supervisor instruction ' + t.get('id', '') + '] [Supervisor ruling] ' + ('[' + item + '] ' if item else '')
                + answer['decision'] + ((': ' + note) if note else '') + '\n(on what you reported as Blocked: '
                + rec.get('reason', '')[:500].replace('\n', '; ') + ')\nCarry on accordingly; reply in the AGENTS.md '
                'format (Done / Evidence / Not done / Blocked) when done.')
        row = state['tasks'].get(item) if state is not None and item else None
        if row is not None:
            row.update(status='running', reason='supervisor ruled on Blocked: ' + answer['decision'])
            self.contract_queue(state, slot, text, item)
        else:
            self.queue(slot, text)
        self.log('blocked.ruled', agent=slot, task=task_id, item=item, decision=answer['decision'])
        if self.v2:
            self.v2.audit('blocked.ruled', slot=slot, level='info', task=task_id, decision=answer['decision'])

    def service_ruling(self, task_id, rec, answer, note):
        """Service mode: the ruling goes back to the worker as the same request (its answer still settles it)."""
        slot, rid = rec['slot'], rec.get('request') or ''
        req = self.r.hgetall(REQ + rid) if rid else {}
        if req.get('status') != 'dispatched':  # timed out / answered meanwhile: nothing to rule on any more
            self.log('blocked.stale', agent=slot, task=task_id, request=rid)
            return
        self.queue(slot, self.request_message(rid, json.loads(req['spec']), (
            '[Supervisor ruling] ' + answer['decision'] + ((': ' + note) if note else '') + '\n(on what you reported '
            'as blocked: ' + rec.get('reason', '')[:500].replace('\n', '; ') + ')\nCarry on accordingly and answer the '
            'request.'), slot=slot))
        self.log('blocked.ruled', agent=slot, task=task_id, request=rid, decision=answer['decision'])
        if self.v2:
            self.v2.audit('blocked.ruled', slot=slot, level='info', task=task_id, decision=answer['decision'], request_id=rid)

    def service_blocked_intake(self, slot, rid, reason):
        """Service mode, plan F' 8.4: a worker answered {"blocked": ...}; the request stays dispatched and the
        supervisor rules on it. The [blocked] note goes to the supervisor bound to that request."""
        key_ = BLOCKED_KEY + slot
        who = self.label(slot)
        text = ('[blocked] ' + who + ' (request ' + rid + ') says it is stuck on: ' + reason[:2000].replace('\n', '; ')
                + '\nThe request is still open. Answer in your next JSON: "escalations": [{"task": "' + key_
                + '", "decision": "approve" | "revise" | "reject", "note": "..."}] with status RUNNING, dispatch [] and '
                'skip []. approve = it may go on the way it proposed (say in note how far that goes); revise = another '
                'way (note required); reject = do not do that part, answer with what it has. Reply status BLOCKED only '
                'for a delete / move, or for what truly only the user can decide, and say which in reason.')
        mid = self.v2.queue('supervisor', text, request_id=rid, kind='followup') if self.v2 else self.queue('supervisor', text)
        esc = self.escalation_state()
        esc[key_] = {'kind': 'blocked', 'slot': slot, 'item': '', 'request': rid, 'reason': reason[:2000],
                     'status': 'escalated', 'asked_at': time.time(), 'message_id': mid or ''}
        self.save_escalations(esc)
        self.log('blocked.asked', agent=slot, request=rid, reason=reason[:300])
        return key_

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
        items = [{'ref': k, 'change': kind, 'path': str(self.workspace() / state['interfaces'][k]['path']),
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
                record = self.startup_wall(name, ev.get('text', ''))
                if record:
                    self.brake_notify(record)
                    return
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
        if codex_queue.is_codex(self.runtime_cfg(name), name) and info.get('state') in ('busy', 'handoff', 'starting'):
            # A queued delivery settles only on the turn the rollout binds to it (a manual or repeated turn does not).
            rec = codex_queue.record(self.agent(name))
            if rec['cq_delivery'] and rec['cq_state'] not in ('', 'abandoned'):
                ok, why = codex_queue.bound(rec, ev.get('thread') or '', ev.get('turn') or '', self.sessions,
                                           ev.get('generation'))
                if not ok or rec['cq_turn'] == (ev.get('turn') or ''):
                    self.log('turn.unbound', agent=name, delivery=rec['cq_delivery'], turn=ev.get('turn'),
                             reason=why or 'turn already settled')
                    return
                if not codex_queue.mark_bound(self.r, name, ev['turn'], rec['cq_token']):
                    self.log('turn.unbound', agent=name, delivery=rec['cq_delivery'], turn=ev.get('turn'),
                             reason='delivery abandoned or replaced while binding')
                    return
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
        if self.running() and len(text.strip()) <= brake.TURN_WALL_MAX:
            record = self.on_wall(name, text, 'turn')
            if record:  # a wall message is not a report: keep the message for after resume (once per message)
                if info.get('last_msg') and info.get('wall_requeued') != (ev.get('msg_id') or info.get('msg_id') or '-'):
                    self.r.lpush(key('inbox', name), info['last_msg'])
                    self.setf(name, wall_requeued=ev.get('msg_id') or info.get('msg_id') or '-')
                self.brake_notify(record)
                return
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
        item = self.report_item(name)  # before contract_report consumes the owner of this report
        extra = self.contract_report(name, text)
        if extra is None:  # the contract gate ended the task
            return
        fields = contracts.report_fields(text) if self.contract_state() is None else {}
        if fields.get('escalate') is not None:  # plain task: no task table, same validation
            _, _, note, reply = self.escalate_intake(name, fields, None)
            if reply:
                self.queue(name, reply)
            extra += note
        blocked = report_blocked(text) if not self.service and name in self.workers else ''
        key_, note = self.blocked_intake(name, item, blocked) if blocked else (None, '')
        mid = self.queue('supervisor', tag + '\n' + text + extra + note + '\n\nReply with the JSON format from AGENTS.md.')
        if key_ and mid:  # the supervisor's reply to this very message must answer it (on_supervisor)
            esc = self.escalation_state()
            if key_ in esc:
                esc[key_]['message_id'] = mid
                self.save_escalations(esc)

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
        # A delivery still queued for the supervisor itself (typically another worker's report) guarantees
        # another supervisor turn, so "RUNNING, waiting" cannot stall the task.
        return bool(self.inbox_depth('supervisor')) or any(
            busy(self.agent(w)) or self.inbox_depth(w) for w in self.workers)

    def unsettled_reason(self, w):
        """Why worker ``w`` still has dispatched work out, or '' when everything it got has settled."""
        state = self.agent(w).get('state')
        if state in ('busy', 'held', 'delegated'):
            return {'busy': 'working on a delivery', 'held': 'delivery held, not settled',
                    'delegated': 'sub-fleet running'}[state]
        return 'delivery queued' if self.inbox_depth(w) else ''

    def unsettled_workers(self):
        return [w for w in self.workers if self.unsettled_reason(w)]

    def on_supervisor(self, text, invalid=None):
        """`invalid`: the v2 validator's own rejection; the correction must name it, not a parse error."""
        t = self.task()
        try:
            if invalid:
                raise ValueError(invalid)
            decision = parse_decision(text, self.dispatch_targets(), self.others_active())
            state = self.contract_state()
            if state is not None:
                state, rows, held, changes, skeleton = self.contract_decision(state, decision)
                rows = {r['id']: r for r in rows + held}
            command_guard(t, decision)  # after the shape checks: a malformed cmd_exit keeps its own message
            # Validate the whole dispatch before queuing any part of it. Rejections
            # use the existing supervisor correction/three-strike blocked path.
            prepared = []
            for item in decision['dispatch'] if decision['status'] == 'RUNNING' else []:
                slot = self.resolve(item['to'])
                instruction = item['instruction'].strip()
                prepared.append((slot, instruction, self.prepare_worker_profile(slot, instruction)))
            escalations = self.escalation_decision(decision, state) if decision['status'] == 'RUNNING' else []
            consults = self.consult_decision(decision, state) if decision['status'] == 'RUNNING' else []
            self.escalations_missed(decision)
        except (ValueError, TypeError, KeyError) as exc:
            # P0 gap lists never count toward the three-strike limit (user 10/13): the supervisor
            # keeps fixing interfaces until all checks pass; the task `hours` budget is the bound.
            # Same for the first reply that leaves a [dialog] / [blocked] question open (plan F' 8.3 / 8.4).
            p0 = str(exc).startswith('P0') or isinstance(exc, NoStrike)
            bad = int(self.r.hget(TASK, 'bad_replies') or 0) if p0 else self.r.hincrby(TASK, 'bad_replies', 1)
            if not p0 and bad >= 3:
                self.finish('blocked', 'supervisor gave no valid decision JSON three times in a row: ' + str(exc))
            else:
                self.queue('supervisor', 'Your reply is not a valid decision JSON (' + str(exc)
                           + '). Resend one JSON object in the AGENTS.md format, nothing else.')
            return
        artifacts = json.loads(t.get('artifacts') or '[]')
        added = [a for a in decision.get('artifacts', []) if a not in artifacts]
        if t.get('artifacts_frozen') == '1':
            if added:  # layer 1 of the contract belongs to the caller: never widened or narrowed here
                self.log('contract.append_ignored', task=t.get('id', ''), dropped=added)
        else:
            artifacts += added
        self.brake_count('rounds')
        self.r.hset(TASK, mapping={'bad_replies': 0, 'completion': decision['completion'],
                                   'reason': decision.get('reason', ''),
                                   'artifacts': json.dumps(artifacts, ensure_ascii=False)})
        self.advisor_decision(decision)  # before the dispatches below: `advisor: "on"` advises them already
        if decision['status'] == 'BLOCKED':
            self.finish('blocked', decision.get('reason', 'needs a human'))
            return
        if decision['status'] == 'COMPLETED':
            withdrawn = {self.resolve(w) for w in decision['withdraw']}
            pending = [w for w in self.unsettled_workers() if w not in withdrawn]
            if pending:
                # Never end a task over work still out: every dispatched delivery settles first, or the
                # supervisor withdraws it explicitly (10/13: COMPLETED while workers still ran).
                names = ', '.join(self.label(w) + ' (' + self.unsettled_reason(w) + ')' for w in pending)
                self.log('complete.refused', workers=pending)
                bounces = self.r.hincrby(TASK, 'bounces', 1)
                if bounces >= 3:
                    self.finish('blocked', 'supervisor claimed completion three times while workers had unsettled '
                                'deliveries: ' + names)
                else:
                    self.queue('supervisor', 'COMPLETED refused: ' + names + ' still have unsettled deliveries. Wait '
                               'for their reports (RUNNING with an empty dispatch), or reply COMPLETED again with '
                               '"withdraw": [' + ', '.join('"' + self.label(w) + '"' for w in pending)
                               + '] to drop that work explicitly.')
                return
            if withdrawn:
                self.log('complete.withdrawn', workers=sorted(withdrawn))
            extended = state is not None or t.get('artifacts_frozen') == '1'
            failures = (check_artifacts(artifacts, self.workspace(), extended, extended, caller_commands(t))
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
            if self.advisor_online():
                # Only supervisor dispatches are advised (never [contract] notices or report corrections).
                # Registered before the depends_on hold: a held row is advised when the daemon releases it.
                self.r.rpush(ADVICE_PENDING + slot, json.dumps({'instruction': instruction, 'note': item.get('advise_note', ''),
                                                                'task': item.get('id', '')}, ensure_ascii=False))
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
        # A new instruction to (or a consult on) a worker that reported Blocked answers that too (plan F' 8.4).
        redispatched += [BLOCKED_KEY + slot for slot in [s for s, _, _ in prepared]
                         + [self.resolve(c['worker']) for c in decision['consult']]
                         if esc.get(BLOCKED_KEY + slot, {}).get('status') == 'escalated']
        if redispatched:  # the supervisor answered the split by dispatching the task again
            self.save_escalations({k: v for k, v in esc.items() if k not in redispatched})
        for task_id, rec, answer in escalations:
            self.escalation_apply(t, state, task_id, rec, answer)
        self.consult_apply(consults)
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
        frozen = validate_contract(spec.get('contract'))
        if frozen and not spec.get('artifacts'):
            raise ValueError('contract.artifacts_frozen needs the caller\'s non-empty artifacts list')
        if 'artifacts' in spec:
            # Auto-planned task specs may explicitly say there are no pre-declared artifacts.
            # Keep the legacy non-auto validation unchanged.
            if not (auto_agents and spec['artifacts'] == []):
                validate_artifacts(spec['artifacts'], extended=use_contracts or frozen)
        budget = brake.validate_budget(spec.get('budget'))
        hours = spec.get('hours', 16)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not 0 < hours < 1000:
            raise ValueError('hours must be a positive number')
        life_spec = {}
        if not self.cfg.get('parent_config'):  # a sub-fleet's deliverable is checked by its parent
            if self.cfg.get('lifecycle') == 'dev_task':
                life_spec['deliver'] = lifecycle_deliver.validate_deliver(spec.get('deliver'))
            elif self.cfg.get('lifecycle') == 'review_loop':
                if 'review_file' not in spec:
                    raise ValueError('review_loop needs "review_file": the reviewer verdict JSON it reads each round')
                life_spec['review_file'] = lifecycle_deliver.validate_review_file(spec['review_file'])
        registry_path = validate_agent_registry_path(spec.get('agent_registry'))
        dispatch_plan = profile_assignments = None
        if auto_agents:
            registry = AgentRegistry(registry_path)
            # Official Claude workers: prefer claude cards so a live switch never leaves the CLI
            # (v2 Codex supervisor + Claude workers rejects Pi/Codex workers). Pi/Codex fleets unchanged.
            # Card hosts filter only when the operator named this machine (config 'host');
            # max_concurrent counts the models the slots run now (a slot that already runs one is reused).
            usage = self.model_usage(registry)
            plan = build_dispatch_plan(spec['goal'], registry,
                                       'claude' if self.cfg.get('worker_cli') == 'claude' else None,
                                       host=self.cfg.get('host') or None,
                                       occupancy=usage.for_planning(self.active_workers()))
            dispatch_plan = plan.to_dict()
            profile_assignments = assign_profiles(plan, self.active_workers(), registry,
                                                  running={s: m for s, (_, m) in usage.slots.items()})
        task_id = 'task-' + uuid.uuid4().hex[:10]
        self.r.delete(TASK)
        fields = {'id': task_id, 'goal': spec['goal'], 'status': 'running', 'completion': 0,
                  'lifecycle_spec': json.dumps(life_spec, ensure_ascii=False),
                  'reason': '', 'deadline': time.time() + hours * 3600, 'bad_replies': 0,
                  'bounces': 0, 'artifacts': json.dumps(spec.get('artifacts', []), ensure_ascii=False),
                  'caller_artifacts': json.dumps(spec.get('artifacts', []), ensure_ascii=False),
                  'apply_profiles_live': json.dumps(spec.get('apply_profiles_live', False))}
        if use_contracts:
            fields['contracts'] = 1
        if frozen:
            fields['artifacts_frozen'] = 1
        if dispatch_plan is not None:
            fields.update(
                auto_agents=1,
                agent_registry=str(registry.directory),
                dispatch_plan=json.dumps(dispatch_plan, ensure_ascii=False, separators=(',', ':')),
                profile_assignments=json.dumps(
                    profile_assignments, ensure_ascii=False, separators=(',', ':')),
            )
        self.r.hset(TASK, mapping=fields)
        self.brake_start(budget)
        prompt = ('[New task ' + task_id + '] Goal: ' + spec['goal']
                  + '\nWorkers you can dispatch to: ' + ', '.join(self.labels())
                  + '\nShared output directory: ' + str(self.workspace())
                  + ' (artifact paths are relative to it, e.g. worker-1/report.md)'
                  + '\nArtifacts pre-declared by the operator: '
                  + json.dumps(spec.get('artifacts', []), ensure_ascii=False))
        if frozen:
            prompt += ('\nThese artifacts are frozen by the caller: they are the acceptance contract. Artifacts you '
                       'add in a decision are ignored and audited; to change them, reply BLOCKED and say why.')
        if budget:
            prompt += '\nBudget card (the daemon pauses the fleet at any line): ' + json.dumps(budget, ensure_ascii=False)
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
            for name in ['supervisor'] + self.workers:  # never the advisor: it sees only consultations
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
        for w in self.workers:  # an earlier task's unadvised dispatches never match this one
            if self.r.llen(ADVICE_PENDING + w):
                self.r.delete(ADVICE_PENDING + w)
        if self.r.llen(RESCUE):  # an earlier task's consults and rescue leftovers never act in this one
            self.r.delete(RESCUE)
        rescue = json.loads(self.r.get(RESCUE_PENDING) or 'null')
        if rescue:
            self.r.delete(RESCUE_PENDING)
            self.advisor_abandon(rescue['ticket'], 'a new task started')
        for w in self.workers:
            info = self.agent(w)
            self.drop_advice_wait(w)
            self.drop_advice_pending(w, 'a new task started')
            left = {k: '' for k in RESCUE_FIELDS + ('advice_domains',) if info.get(k)}
            if left:
                self.setf(w, **left)
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
            codex_queue.clear(self, name, 'task ' + status)
            pane = self.agent(name).get('pane')
            if pane and self.agent(name).get('state') == 'busy':
                # An aborted turn fires no notify, so mark idle here; tabs stay open.
                self.mgr.send_key(pane, 'Escape')
                self.setf(name, state='idle', idle_confirmed=0)

    # -- service mode
    def request(self, spec):
        if not self.service:
            raise ValueError('requests need a fleet started with --mode service')
        if named_subfleet.is_op(spec):  # §29: own branch, never REQQ / validate_request / request_timeout
            return named_subfleet.request(self, spec)
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
                       + '\nWorkers: ' + ', '.join(self.labels())
                       + '\nWorker status now (state, queue, current request, role):\n' + self.worker_board()
                       + '\nPayload (JSON):\n'
                       + json.dumps(spec['payload'], ensure_ascii=False, indent=1)
                       + '\nReply with the service decision JSON from AGENTS.md.')

    def worker_board(self):
        """One line per dispatchable worker for a service routing message (docs/design.md §32):
        the supervisor picks an idle worker and the reviewer by role from this, not by guessing."""
        current = {}
        for rid in self.r.lrange(REQQ, 0, -1):
            req = self.r.hgetall(REQ + rid)
            if req.get('status') == 'dispatched' and req.get('worker'):
                current.setdefault(req['worker'], []).append(rid)
        roles = self.cfg.get('worker_roles') or {}
        lines = []
        for w in self.active_workers():
            state, depth = self.agent(w).get('state') or 'unknown', self.inbox_depth(w)
            line = '- ' + self.label(w) + ': ' + state + (', ' + str(depth) + ' queued' if depth else '')
            if current.get(w):
                line += ', has request ' + ', '.join(current[w])
            role = (Path(roles[w]) if w in roles else
                    Path(self.cfg['roles']).expanduser() / (w + '.md') if self.cfg.get('roles') else None)
            if role and role.is_file():
                first = next((x.strip('# ').strip() for x in role.read_text().splitlines() if x.strip()), '')
                line += ', role: ' + first[:120]
            lines.append(line)
        return '\n'.join(lines)

    def assign(self, rid, slot, instruction=None):
        spec = json.loads(self.r.hget(REQ + rid, 'spec'))
        self.r.hset(REQ + rid, mapping={'status': 'dispatched', 'worker': slot})
        self.queue(slot, self.request_message(rid, spec, instruction, slot=slot))

    def request_message(self, rid, spec, instruction=None, prefix='', slot=None):
        reply = spec['reply']
        keys = ', '.join(reply['required'])
        if reply.get('allowed') is not None:
            keys += ' (allowed keys: ' + ', '.join(reply['allowed']) + ')'
        # Only Pi has the fleet_result tool; official CLIs (Codex/Claude TUIs) keep the
        # service_worker.md wording and are mapped by runtime_v2.tui_candidates.
        if self.v2 and not (slot and runtime_v2.tui_slot(self.cfg, slot)):
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

    def on_service_supervisor(self, text, invalid=None):
        try:
            if invalid:
                raise ValueError(invalid)
            decision = parse_service_decision(text, self.dispatch_targets())
            escalations = self.escalation_decision(decision, None) if decision['status'] == 'RUNNING' else []
            self.escalations_missed(decision)
        except (ValueError, TypeError, KeyError) as exc:
            # The first reply that leaves a [dialog] / [blocked] open is sent back without a strike (plan F').
            bad = (int(self.r.hget(SERVICE, 'bad_replies') or 0) if isinstance(exc, NoStrike)
                   else self.r.hincrby(SERVICE, 'bad_replies', 1))
            if bad >= 3:
                self.r.hset(SERVICE, mapping={'bad_replies': 0})
                self.pause('supervisor gave no valid decision JSON three times in a row: ' + str(exc))
            else:
                self.queue('supervisor', 'Your reply is not a valid service decision JSON (' + str(exc)
                           + '). Resend one JSON object in the AGENTS.md format, nothing else.')
            return
        self.r.hset(SERVICE, mapping={'bad_replies': 0, 'reason': decision.get('reason', '')})
        self.brake_count('rounds')
        for item in decision['dispatch']:
            if self.r.hget(REQ + item['request_id'], 'status') != 'routing':
                self.log('dispatch.unknown_request', id=item['request_id'])
                continue
            self.assign(item['request_id'], self.resolve(item['to']), item['instruction'])
        for item in decision['skip']:
            if self.r.hget(REQ + item['request_id'], 'status') == 'routing':
                self.publish(item['request_id'], 'skipped', worker='supervisor', reason=item['reason'])
        for task_id, rec, answer in escalations:
            self.escalation_apply({}, None, task_id, rec, answer)
        if decision.get('subfleet'):
            named_subfleet.decide(self, decision['subfleet'])
        if decision['status'] == 'BLOCKED':
            self.pause(decision.get('reason') or 'supervisor needs a human')

    def expire_requests(self, now):
        for rid in self.r.lrange(REQQ, 0, -1):
            req = self.r.hgetall(REQ + rid)
            if not req:
                self.r.lrem(REQQ, 0, rid)
                continue
            limit = json.loads(req['spec']).get('timeout_s') or float(self.cfg['request_timeout'])
            if midturn.effective_age(req, now) >= limit:
                self.publish(rid, 'timeout', worker=req.get('worker') or 'supervisor',
                             error='no valid answer within ' + str(int(limit)) + ' s (' + req.get('status', '') + ')')

    # -- delivery + rotation
    def pump(self):
        if self.paused():
            return  # nothing is delivered and nothing rotates; requests keep queuing (and may time out)
        if not self.service:
            self.rescue_tick()  # advisor rescue (§3.2): outside any v2 transaction, like the pre-dispatch advice
        cap = int(self.cfg.get('max_busy') or 0)
        card = (self.brake_budget() or {}).get('max_busy')
        if card:  # the budget card can only tighten --max-busy
            cap = min(cap, card) if cap else card
        busy = sum(self.agent(w).get('state') in ('busy', 'handoff', 'held') for w in self.workers)
        for name in self.names:
            info = self.agent(name)
            if name in self.workers and cap and busy >= cap:
                continue  # e.g. a local model server that rejects concurrent large requests
            if (info.get('advice_hold') == '1' and info.get('state') == 'starting'
                    and info.get('launch_ready') == '0' and (self.inbox_depth(name) or not self.advisor_online())):
                if self.advisor_online():
                    self.release_hold(name)  # first sheet decides model / effort / domains
                else:
                    self.release_default(name)  # advisor went offline: never stay held
                continue
            if (info.get('state') == 'idle' and self.inbox_depth(name)
                    and time.time() >= float(info.get('retry_at') or 0)):
                self.deliver(name)
                if name in self.workers and self.agent(name).get('state') in ('busy', 'handoff'):
                    busy += 1
            elif info.get('state') == 'idle' and not self.inbox_depth(name) and (name == 'supervisor' or name in self.workers):
                self.idle_rotate(name)
                if name in self.workers and self.agent(name).get('state') == 'handoff':
                    busy += 1

    def idle_rotate(self, name):
        """Rotate an idle agent over its ratio as soon as its turn settles, not at its next delivery
        (docs/design.md §32): a session that ends a long turn past the ratio and then gets nothing
        would otherwise keep its full context until the CLI's own compaction. Checked once per
        (generation, delivered), with the same guards as ``deliver``."""
        info = self.agent(name)
        mark = str(info.get('generation') or 0) + ':' + str(info.get('delivered') or 0)
        if int(info.get('delivered') or 0) == 0 or info.get('idle_rotate_mark') == mark:
            return  # a fresh session (READY only) is never rotated here; rotate.impossible stays deliver's
        if name != 'supervisor' and name in self.profile_swaps():
            return
        if self.v2 and not runtime_v2.tui_slot(self.runtime_cfg(name), name) and info.get('bridge_available') != '1':
            return  # not settled on the bus yet: look again next pump
        self.setf(name, idle_rotate_mark=mark)
        used, window = self.usage(name)
        ratio = float(self.agent(name).get('handoff_ratio') or self.cfg['handoff_ratio'])
        if subfleet.adopted(self.cfg, name, info.get('generation')) or not window or used < ratio * window:
            return
        if agent_cli(self.runtime_cfg(name), name) == 'codex' and active_children(info.get('thread'), self.sessions):
            return  # deliver() defers it again at the next message
        self.start_rotation(name, used, window)

    def midturn_settings(self, name):
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        return midturn.resolve_settings(cli, str(cfg.get(MODEL_KEYS[cli]) or ''), self.cfg,
                                        self.handoff_table)[0]

    def midturn_guard(self, name, info):
        """Same ownership/profile/clear boundaries as normal rotation; dialogs are hands-off."""
        owns = getattr(self.mgr, 'owns', None)
        return (name not in self.names or name == ADVISOR or self.paused()
                or subfleet.adopted(self.cfg, name, info.get('generation'))
                or info.get('state') in ('delegated', 'clearing') or info.get('clear_required') == '1'
                or info.get('prompt_wait') or info.get('profile_state') == 'switching'
                or name in self.profile_swaps() or not info.get('pane')
                or owns is not None and not owns(info['pane']))

    def midturn_targets(self):
        """Only the main loop touches Redis; the file thread receives plain snapshots."""
        targets = {}
        for name in self.names:
            info = self.agent(name)
            if name == ADVISOR or info.get('state') != 'busy':
                continue
            lease = self.r.hgetall(self.v2.bus.lease_key(name))
            if lease.get('status') != 'received' or not lease.get('message_id'):
                continue
            msg = self.v2.bus.message(lease['message_id'])
            if msg['body'].get('purpose') == 'handoff':
                continue
            cfg = self.runtime_cfg(name)
            cli = agent_cli(cfg, name)
            override = self.handoff_limits(name)[0]
            targets[name] = dict(cli=cli, generation=int(info.get('generation') or 0),
                message_id=lease['message_id'], thread=info.get('thread') or '',
                path=info.get('transcript') if cli == 'claude' else '',
                directory=str(self.run_dir / 'pi-sessions' / name / ('g' + str(info.get('generation') or 0)))
                    if cli == 'pi' else str(self.sessions),
                window=override or int(info.get('ctx_window') or (self.cfg['window'] if cli == 'codex' else 0)),
                override=override)
        return targets

    def midturn_apply(self):
        """Apply file results and type prompts on the daemon thread, outside event transactions."""
        if not self.v2:
            return
        targets = self.midturn_targets()
        self.midturn_probe.update(targets)
        for name, result in self.midturn_probe.take().items():
            target = targets.get(name)
            if not target or any(target[k] != result[k] for k in ('generation', 'message_id')):
                continue
            info = self.agent(name)
            if result['used'] is None:
                if result['cli'] == 'opencode' and info.get('midturn_no_usage_g') != str(result['generation']):
                    self.v2.transaction('midturn-no-usage-' + uuid.uuid4().hex,
                        lambda name=name, result=result: self.v2.midturn_no_usage(name, result['generation']))
                continue
            used, window = result['used'], result['window']
            settings = self.midturn_settings(name)
            self.setf(name, ctx_used=used, ctx_window=window, midturn_ratio=settings['midturn_ratio'],
                      midturn_line=settings['midturn_ratio'] * window)
            # control's audit renderer includes status on its event line; keep the live numbers there
            # without changing its schema or the separate control_view module (outside M1's file scope).
            self.v2.audit('midturn.usage', slot=name,
                          status='ctx=' + str(used) + '/' + str(window) + ' mid=' + str(int(settings['midturn_ratio'] * window)))
            if self.midturn_guard(name, info) or info.get('midturn_typed_g') == str(result['generation']):
                continue
            msg = self.v2.bus.message(result['message_id'])
            splits = self.v2.midturn_splits(msg)
            action = midturn.decide(used, window, midturn_splits=splits, **settings)
            if action == 'blocked':
                self.v2.transaction('midturn-limit-' + result['message_id'],
                    lambda name=name, msg=msg: self.v2.midturn_blocked(name, msg))
            elif action == 'steer':
                self.midturn_order(name, msg, used, window, settings)
        self.midturn_timeouts()

    def midturn_order(self, name, msg, used, window, settings):
        now = time.time()
        if msg['deadline_at'] <= int(now * 1000):
            return
        if self.service:
            req = self.r.hgetall(REQ + msg['request_id'])
            if req and midturn.effective_age(req, now) >= float(json.loads(req['spec']).get('timeout_s') or self.cfg['request_timeout']):
                return
        hid = 'fx-' + uuid.uuid4().hex[:16]
        draft = agent_cwd(self.cfg, name) / '.handoff' / ('draft-' + hid + '.md')
        # Single-line paste even for a run directory containing control characters.
        if any(c in str(draft) for c in '\n\r\t'):
            self.v2.audit('midturn.type_failed', slot=name, level='error', error='handoff path contains a control character')
            return
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(handoff.partial_template())
        prompt = midturn.MIDTURN_PROMPT.format(midturn_id=hid, used=used, window=window, path=draft)
        def order():
            fields = midturn.pause_message(msg, int(now * 1000), midturn_retry=settings['midturn_retry'],
                                           profile_ready_timeout=float(self.cfg['profile_ready_timeout']))
            fields = {k: fields[k] for k in ('deadline_at', 'deadline_remaining_ms', 'clock_paused_at')}
            fields.update(midturn_id=hid, midturn_at=now, midturn_used=used)
            self.v2.context['authorization'] = {
                self.v2.bus.msg_key(msg['message_id']): {'status': 'received', 'generation': str(msg['generation']),
                    'delivery_id': msg['delivery_id'], 'session_id': msg['session_id']},
                self.v2.bus.lease_key(name): {'status': 'received', 'message_id': msg['message_id'],
                    'current_delivery': msg['delivery_id'], 'generation': str(msg['generation'])}}
            self.r.hset(self.v2.bus.msg_key(msg['message_id']), mapping=fields)
            self.setf(name, midturn_id=hid, midturn_draft=draft, midturn_mtime=draft.stat().st_mtime_ns,
                      midturn_typed_g=msg['generation'], midturn_deadline=now + settings['midturn_retry'],
                      midturn_tries=1, midturn_prompt=prompt, midturn_send_pending=1, midturn_type_attempts=0,
                      midturn_request=msg['request_id'], midturn_message=msg['message_id'],
                      midturn_instruction=msg.get('midturn_instruction') or msg['body']['text'], midturn_wait_at='')
            self.v2.midturn_pause_request(msg['request_id'], now)
            self.v2.audit('midturn.ordered', slot=name, message_id=msg['message_id'], detail=fields)
            self.v2.midturn_card(name, self.label(name) + ' 上下文 ' + str(used) + '/' + str(window) + '，已发中途交接指令')
        self.v2.transaction('midturn-order-' + hid, order)

    def midturn_type(self, name):
        info = self.agent(name)
        if is_claude(self.runtime_cfg(name), name):
            return  # delivered by the PostToolUse hook (claude_posttool), never typed into the pane
        prompt = midturn.registered_prompt(info, info['generation'])
        if not prompt or self.midturn_guard(name, info):
            return
        attempts = int(info.get('midturn_type_attempts') or 0) + 1
        # Record before typing: a crash never silently replays a possibly accepted paste.
        self.setf(name, midturn_type_attempts=attempts, midturn_send_pending=0)
        try:
            ok = self.mgr.send_prompt_to_idle(info['pane'], prompt,
                                             bracketed=is_claude(self.runtime_cfg(name), name))
        except Exception as exc:
            ok = False
            self.v2.audit('midturn.type_failed', slot=name, level='error', error=str(exc))
        if not ok:
            self.setf(name, midturn_send_pending=int(attempts < 2))
            self.v2.audit('midturn.type_failed', slot=name, level='error', detail={'attempt': attempts})

    def midturn_timeouts(self):
        now = time.time()
        for name in self.names:
            info = self.agent(name)
            if not info.get('midturn_id') or info.get('state') != 'busy':
                continue
            lease = self.r.hgetall(self.v2.bus.lease_key(name))
            if lease.get('status') != 'received' or lease.get('message_id') != info.get('midturn_message'):
                continue
            if info.get('midturn_wait_at'):
                elapsed = max(0, now - float(info['midturn_wait_at']))
                def unfreeze():
                    self.setf(name, midturn_wait_at=now if info.get('prompt_wait') else '',
                              midturn_deadline=float(info['midturn_deadline']) + elapsed)
                    key_ = self.v2.bus.msg_key(lease['message_id'])
                    self.r.hset(key_, mapping={'deadline_at': int(self.r.hget(key_, 'deadline_at')) + round(elapsed * 1000)})
                # Extend throughout the wait, so bridge lease renewals also see a live message deadline.
                self.v2.transaction('midturn-wait-' + uuid.uuid4().hex, unfreeze)
                info = self.agent(name)
            if info.get('prompt_wait'):
                if not info.get('midturn_wait_at'):
                    self.setf(name, midturn_wait_at=now)
                continue  # dialog protection precedes even a paused-fleet timeout
            if now >= float(info['midturn_deadline']):
                if self.midturn_guard(name, info):
                    self.v2.transaction('midturn-held-' + uuid.uuid4().hex,
                        lambda name=name: self.v2.hold(name, 'midturn timeout; rotation guarded'))
                    continue
                if int(info.get('midturn_tries') or 1) < 2:
                    retry = self.midturn_settings(name)['midturn_retry']
                    self.setf(name, midturn_tries=2, midturn_deadline=now + retry,
                              midturn_send_pending=1, midturn_type_attempts=0)
                    self.v2.audit('midturn.retry', slot=name)
                else:
                    try:
                        screen = self.mgr.capture_output(info['pane']) or ''
                    except Exception:
                        screen = ''
                    self.v2.watchdog.rotate(name, 'midturn timeout', screen, reason='midturn')
                    continue
            if self.agent(name).get('midturn_send_pending') == '1':
                self.midturn_type(name)

    def handoff_limits(self, name):
        """(window or None = measured, ratio, source) for this agent's current runtime model."""
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        model = cfg.get(MODEL_KEYS[cli]) or ''
        limits = handoff_limits(cli, str(model), self.cfg, self.handoff_table, self.worker_handoff.get(name),
                                card=self.card_handoff_ratio(name))
        self.setf(name, handoff_window=limits[0] or 'measured', handoff_ratio=limits[1], handoff_source=limits[2])
        return limits

    def usage(self, name):
        override = self.handoff_limits(name)[0]
        if is_pi(self.runtime_cfg(name), name):
            info = self.agent(name)
            return int(info.get('ctx_used') or 0), override or int(info.get('ctx_window') or self.cfg['window'])
        if is_opencode(self.runtime_cfg(name), name):
            # Bridge-reported usage (assistant tokens); the window is the configured one (free tier 200K),
            # never a model's self-reported size and never a Codex rollout.
            info = self.agent(name)
            return int(info.get('ctx_used') or 0), override or int(self.cfg.get('opencode_window') or OPENCODE_WINDOW)
        if is_claude(self.runtime_cfg(name), name):
            path = self.agent(name).get('transcript')
            if not path:
                return 0, 0
            used, window = claude_transcript_usage(path, override)
            if used is None:
                return int(self.agent(name).get('ctx_used') or 0), window
            self.setf(name, ctx_used=used, ctx_window=window)
            return used, window
        if not is_codex(self.runtime_cfg(name), name):
            raise ValueError('no usage reader for ' + agent_cli(self.runtime_cfg(name), name))
        thread = self.agent(name).get('thread')
        paths = rollouts(thread, self.sessions) if thread else []
        if not paths:
            return 0, 0
        used, window = read_usage(str(paths[0]), int(self.cfg['window']))
        window = override or window
        if used is None:
            return int(self.agent(name).get('ctx_used') or 0), window
        self.setf(name, ctx_used=used, ctx_window=window)
        return used, window

    def model_usage(self, registry=None):
        """Which models each worker slot holds (model_usage: one slot counts once per model; a tab
        still alive keeps its model, also an old generation's or a dead slot's)."""
        registry = registry or AgentRegistry(self.task().get('agent_registry') or None)

        def alive(pane):
            try:
                return self.mgr.get_pane_status(pane) is not None
            except Exception:
                return True  # unknown: keep holding rather than free a ticket early
        return model_usage.Usage.from_agents({w: self.agent(w) for w in self.workers}, self.cfg, registry,
                                             alive=alive)

    def card_gate(self, name, card, registry):
        """Before ``name`` starts a generation on ``card`` (None = the fleet default runtime):
        ('block', why) if this machine may never run it, ('wait', why) while its model is at
        max_concurrent on other slots, else None. An unmapped default is not capped."""
        if card is None:
            card = model_usage.default_card(self.cfg, registry)
            if card is None:
                return None
        problem = model_usage.host_problem(card, self.cfg.get('host') or None)
        if problem:
            return 'block', problem
        problem = self.model_usage(registry).admit(name, card)
        return ('wait', problem) if problem else None

    def card_handoff_ratio(self, name):
        """(profile id, ratio) from the slot's running profile card (own or from its model card), or
        None. One layer of handoff_limits (design.md §22, below the roster / config tables)."""
        profile = self.agent(name).get('runtime_profile')
        if not profile:
            return None
        try:
            card = AgentRegistry(self.task().get('agent_registry') or None).get(profile)
        except (AgentRegistryError, KeyError, OSError):
            return None
        return (profile, card.handoff_ratio) if card is not None and card.handoff_ratio is not None else None

    def handoff_ratio(self, name):
        """The slot's profile card ratio, else the fleet's --handoff-ratio (card layer only; the full
        resolution incl. roster / config tables is handoff_limits)."""
        card = Fleet.card_handoff_ratio(self, name)
        return card[1] if card else float(self.cfg['handoff_ratio'])

    def deliver(self, name):
        info = self.agent(name)
        if info.get('state') != 'idle':
            return
        pending_ids = self.v2.bus.pending(name, 1) if self.v2 else []
        next_msg = self.v2.bus.message(pending_ids[0]) if pending_ids else {}
        continuing = next_msg.get('kind') == 'continuation'
        if not continuing and name != 'supervisor' and info.get('roster_swap_deferred') != '1' and name in self.profile_swaps():
            return  # the next generation (new profile) takes this queue once READY (a deferred swap keeps working)
        if self.v2 and not runtime_v2.tui_slot(self.runtime_cfg(name), name) and info.get('bridge_available') != '1':
            return
        pending = self.v2.pending_text(name) if self.v2 else self.r.lrange(key('inbox', name), 0, 0)
        if not pending:
            return
        text = pending[0]
        if name in self.workers and not self.service:
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
            if head and not continuing:
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
                            gate = self.card_gate(name, registry.get(application.desired_profile), registry)
                            if gate and gate[0] == 'block':
                                raise ValueError(gate[1])
                            if gate:  # keep the message queued; retried every tick until a slot frees up
                                if info.get('capacity_wait') != gate[1]:
                                    self.log('profile.capacity_wait', agent=name,
                                             profile=application.desired_profile, reason=gate[1])
                                self.setf(name, profile_state='pending', profile_reason=gate[1], capacity_wait=gate[1])
                                return
                            if info.get('capacity_wait'):
                                self.setf(name, capacity_wait='')
                            self.switch_profile(name, launch)
                            return  # never pop/deliver until this generation returns READY
                        if profile_launch.encode_launch(launch) != info.get('runtime_launch'):
                            raise ValueError('profile launch definition changed since spawn; submit a new task after repair')
                    except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
                        self.block_profile(name, str(exc))
                        return
                text = profile_runtime.profile_advisory(name, application) + text[head.start():]
                if (self.advisor_online() and application.state not in ('switching', 'applied-live')
                        and not self.advise_before_delivery(name, text)):
                    return  # respawning / blocked: never pop/deliver until the new generation returns READY
        # Validate profile intent before even the existing context rotation path.
        # Leave the original message queued across handoff; READY triggers another
        # idle-boundary validation. This daemon remains the sole inbox consumer.
        used, window = self.usage(name)
        if (continuing and int(info.get('delivered') or 0) == 0 and window
                and used >= self.midturn_settings(name)['midturn_ratio'] * window):
            self.v2.transaction('midturn-impossible-' + next_msg['message_id'],
                                lambda: self.v2.midturn_blocked(name, next_msg, impossible=True))
            return
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
        if self.agent(name).get('advice_hotload'):  # set by a consultation made while online
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
        self.brake_count('calls')
        if not self.send(name, self.agent(name)['pane'], text):
            self.r.lpush(key('inbox', name), text)
            self.finish('blocked', 'delivery failed: ' + name)
            return
        self.setf(name, last_msg=text)
        self.log('deliver', agent=name, text=text[:120])

    # -- advisor (roster advisor: on; advisor plan §3.1, pre-dispatch path)
    def floor_domains(self):
        """start --worker-domains: domains every Claude worker gets, advised or not (the advisor only adds)."""
        return list(self.cfg.get('worker_domains') or [])

    def drop_floor(self, raw):
        """A sheet's domains without the pinned ones: a card whitelist never rejects a sheet for echoing them."""
        floor = self.floor_domains()
        if floor and isinstance(raw, dict) and isinstance(raw.get('domains'), list):
            return dict(raw, domains=[d for d in raw['domains'] if d not in floor])
        return raw

    def install_default_kit(self, cwd, scope):
        """The default (non-advised) layout: the old kit copy, plus the --worker-domains links and their rules
        when the kit is a domain kit (a domain kit has no .claude/: without a floor only mcp.json is copied).
        installed_domains stays '' (default layout)."""
        install_kit(self.cfg.get('worker_kit'), cwd)
        floor = self.floor_domains()
        if not floor or not self.cfg.get('worker_kit'):
            return
        try:
            kit = self.kit or kitmod.Kit(self.cfg['worker_kit'])
        except (OSError, ValueError) as exc:
            self.log('kit.floor_unavailable', level='error', domains=floor, error=str(exc)[:500])
            return
        floor = [d for d in floor if d in kit.domains]
        kit.link_domains(cwd, floor)
        (cwd / 'CLAUDE.md').write_text(kit.claude_md(scope, floor))

    def install_advised_kit(self, name, cwd, scope, record=True):
        """Advisor runs: Claude CLAUDE.md = role scope + project core + this generation's domain rules;
        cwd/.claude/{skills,agents} cleared, then the domains linked; full kit mcp.json. ``record``: write
        installed_domains (the slot's launch record); False for a held generation that has not started."""
        info = self.agent(name)
        domains = json.loads(info.get('advice_domains') or info.get('installed_domains') or '[]')
        domains = list(dict.fromkeys(self.floor_domains() + domains))  # --worker-domains: the advisor only adds
        if self.kit is None and self.cfg.get('worker_kit'):  # off at daemon start: an inherited layout still needs it
            try:
                self.kit = kitmod.Kit(self.cfg['worker_kit'])
            except (OSError, ValueError) as exc:
                self.log('advice.kit_unavailable', level='error', agent=name, domains=domains, error=str(exc)[:500])
        if self.kit:
            domains = [d for d in domains if d in self.kit.domains]
            self.kit.install(cwd, domains)
            (cwd / 'CLAUDE.md').write_text(self.kit.claude_md(scope, domains))
        else:
            kitmod.Kit.clear(cwd)
            domains = []
            (cwd / 'CLAUDE.md').write_text(scope)
        if record:
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

    def advice_context(self, name, cli, cfg):
        """What one pre-dispatch consultation of ``name`` is checked against (re-read per consultation and,
        for a resident one, again when its answer is read)."""
        try:
            registry, registry_error = AgentRegistry(), None
        except (OSError, ValueError) as exc:  # a broken card file: treat as advisor unavailable (§8), never exit
            registry, registry_error = (), 'agent registry unreadable: ' + str(exc)[:400]
        cards = advisor.launch_cards(registry, cli)
        model, effort = advisor.runtime_model(cfg, cli), advisor.runtime_effort(cfg, cli)
        current = advisor.fingerprint(cli, model, effort)
        models = list(dict.fromkeys(([model] if model else []) + list(cards)))
        whitelist = (cards[model].kit_domains if model in cards else None)
        matrix, matrix_error = advisor.load_matrix()
        catalog = self.kit.domains_catalog() if self.kit and cli == 'claude' else []

        def check(raw):
            raw = self.drop_floor(raw)
            target = raw.get('model')
            wl = cards[target].kit_domains if target in cards else whitelist
            return advisor.validate(raw, cli=cli, models=models, current=current,
                                    catalog=[c['name'] for c in catalog], whitelist=wl, matrix=matrix, cards=cards)
        return dict(registry=registry, registry_error=registry_error, cards=cards, model=model, effort=effort,
                    current=current, models=models, matrix=matrix, matrix_error=matrix_error, catalog=catalog,
                    check=check)

    def advise(self, name, entry, cli, cfg, kind='dispatch', identity=''):
        """One consultation: (sheet, current, cards, registry) with the validated or fallback sheet, or None
        while the resident advisor has not answered (the caller keeps its message queued and asks again on a
        later pump through advice_poll; design §27). The one-shot runner answers at once (one retry).
        ``kind`` is reserved for the mid-task rescue path (phase 2: consult / respawn / reassign)."""
        ctx = self.advice_context(name, cli, cfg)
        info = self.agent(name)
        installed = json.loads(info.get('installed_domains') or '[]')
        preset = json.loads(info.get('advice_preset') or '{}')
        if (kind == 'dispatch' and entry.get('task') and preset.get('task') == entry['task']
                and preset.get('run_task') == self.task().get('id')):
            # The supervisor re-dispatches a task the advisor reassigned here: that sheet holds (no second consult).
            self.setf(name, advice_preset='')
            self.log('advice.preset', agent=name, ticket=preset['sheet']['ticket'], task=entry['task'])
            sheet = {k: v for k, v in preset['sheet'].items() if k not in ('action', 'to', 'handoff_hint')}
            return sheet, ctx['current'], ctx['cards'], ctx['registry']
        payload = advisor.build_input(
            kind=kind, goal=self.task().get('goal', ''), instruction=entry['instruction'], advise_note=entry.get('note', ''),
            slot={'name': name, 'cli': cli, 'generation': int(info.get('generation') or 0), 'model': ctx['model'],
                  'effort': ctx['effort'], 'installed_domains': installed,
                  'pinned_domains': self.floor_domains() if cli == 'claude' else []},
            models=ctx['models'], matrix=ctx['matrix'], catalog=ctx['catalog'])
        view = json.dumps({k: payload[k] for k in ('goal', 'instruction', 'advise_note', 'slot')},
                          ensure_ascii=False)[:4000]
        started = time.time()
        if self.advisor_resident() and not ctx['registry_error']:
            ticket = self.new_ticket()
            if self.advisor_request(ticket, name, kind, payload, view):
                self.setf(name, advice_pending=json.dumps({'ticket': ticket, 'msg': identity, 'kind': kind},
                                                          ensure_ascii=False))
                return None
            sheet, envelope, errors = None, None, ['advisor tab could not be started']
        else:
            runner = self.advisor_runner or (lambda data: advisor.run_claude(data, cfg=self.cfg, run_dir=self.run_dir))
            if ctx['registry_error']:
                sheet, envelope, errors = None, None, [ctx['registry_error']]
            else:
                sheet, envelope, errors = advisor.consult(payload, runner, ctx['check'])
            ticket = self.new_ticket()
        return self.advice_finish(name, kind, ticket, sheet, envelope, errors, ctx, view, started)

    def advice_finish(self, name, kind, ticket, sheet, envelope, errors, ctx, view, started):
        """Close a pre-dispatch consultation: the fallback sheet when there is no valid one (§8), the audit hash
        fx:advice:<ticket>, the supervisor note, the slot's advice_last."""
        info = self.agent(name)
        model, effort, models = ctx['model'], ctx['effort'], ctx['models']
        unavailable = sheet is None
        if unavailable:
            last = json.loads(info.get('advice_last') or '{}')
            if last.get('task') == self.task().get('id') and last.get('sheet', {}).get('model') in models:
                sheet = dict(last['sheet'], reason='advisor unavailable: previous sheet of this task')
            else:  # the slot's running runtime, no domains: never forces a respawn (§8 reading, see report)
                sheet = {'model': model, 'effort': effort, 'domains': [], 'reason': 'advisor unavailable: running runtime, domains []'}
        sheet['ticket'] = ticket
        audit = {'slot': name, 'task': self.task().get('id', ''), 'kind': kind, 'input': view,
                 'output': json.dumps(sheet, ensure_ascii=False), 'errors': json.dumps(errors, ensure_ascii=False),
                 'elapsed_s': round(time.time() - started, 3), 'status': 'unavailable' if unavailable else 'ok',
                 'at': time.time()}
        audit.update(advisor.usage_fields(envelope))
        if ctx['matrix_error']:
            audit['matrix_error'] = ctx['matrix_error'][:500]
        self.r.hset(ADVICE + ticket, mapping=audit)
        self.advice_recent(ticket)
        if unavailable:
            self.log('advice.unavailable', agent=name, ticket=ticket, errors=errors)
            self.r.rpush(ADVICE_NOTES, '[advisor unavailable] 按缺省派出 ' + self.label(name) + '（' + sheet['reason'] + '）')
        if sheet.get('capability_gap'):
            self.log('capability.gap', agent=name, ticket=ticket, capability=sheet.get('capability'), model=sheet['model'])
        self.setf(name, advice_last=json.dumps({'task': self.task().get('id', ''), 'sheet': sheet}, ensure_ascii=False))
        return sheet, ctx['current'], ctx['cards'], ctx['registry']

    def advice_poll(self, name, pending):
        """A pre-dispatch consultation out with the resident advisor: None while it is still out, else what
        advise() returns for it (the validated sheet, or the fallback after one correction / the time limit)."""
        cfg, ctx = self.runtime_cfg(name), {}

        def context():  # read the cards / matrix only once there is an answer to check (not on every pump)
            if not ctx:
                ctx.update(self.advice_context(name, agent_cli(cfg, name), cfg))
            return ctx
        state, sheet, errors = self.advisor_poll(pending['ticket'], lambda raw: context()['check'](raw))
        if state == 'wait':
            return None
        self.setf(name, advice_pending='')
        t = self.r.hgetall(ADVICE + pending['ticket'])
        return self.advice_finish(name, pending.get('kind') or 'dispatch', pending['ticket'], sheet, None, errors,
                                  context(), t.get('input', ''), float(t.get('started') or time.time()))

    def advice_pending_for(self, name, identity):
        """The resident consultation this queued message waits for, or None. A pending consultation of another
        message (that message left the queue) is abandoned."""
        pending = json.loads(self.agent(name).get('advice_pending') or 'null')
        if pending and pending.get('msg') != identity:
            self.drop_advice_pending(name, 'the advised message left the queue')
            return None
        return pending

    def drop_advice_pending(self, name, why):
        pending = json.loads(self.agent(name).get('advice_pending') or 'null')
        if pending:
            self.setf(name, advice_pending='')
            self.advisor_abandon(pending['ticket'], why)

    # -- resident advisor tab (docs/design.md §27): consultations are queued to it and read back later
    def new_ticket(self):
        seq = self.r.hincrby(ADVICE_SEQ, 'n', 1)
        return 'adv-' + str(self.cfg.get('token') or self.run_dir.name[-8:]) + '-' + str(seq)

    def advice_recent(self, ticket):
        """Tickets the advisor's handoff skeleton lists (sheets and reasons only, never worker text)."""
        if self.advisor_resident():
            self.r.rpush(ADVICE_RECENT, ticket)
            self.r.ltrim(ADVICE_RECENT, -10, -1)

    def advisor_send(self, ticket, request, error=''):
        """Queue one consultation (or its correction) on the advisor's bus inbox; returns the message id."""
        head = ('[Advisor correction ' + ticket + '] The daemon refused your last sheet: ' + ' '.join(error.split())[:500]
                + '\nReply again with one corrected JSON sheet for this input.' if error
                else '[Advisor consultation ' + ticket + ']')
        return self.v2.queue(ADVISOR, head + '\n' + request, request_id=ticket, kind='advisor')

    def advisor_request(self, ticket, slot, kind, payload, view):
        """Queue a consultation for the resident advisor; its audit hash fx:advice:<ticket> is its state.
        False when the tab cannot take it (it could not be started): the caller falls back at once (§8)."""
        info = self.agent(ADVISOR)
        if not self.advisor_enabled() or info.get('spawn_failed') == '1':
            return False
        now, state = time.time(), info.get('state') or 'not started'
        ahead = self.inbox_depth(ADVISOR) + (state == 'busy')
        request = json.dumps(payload, ensure_ascii=False)
        mid = self.advisor_send(ticket, request)
        self.r.hset(ADVICE + ticket, mapping={
            'slot': slot, 'task': self.task().get('id', ''), 'kind': kind, 'input': view, 'request': request,
            'status': 'pending', 'mid': mid, 'attempt': 1, 'phase': 'queued', 'deadline': now + ADVICE_WAIT,
            'started': now, 'errors': '[]', 'at': now})
        self.log('advice.queued', agent=slot, ticket=ticket, path=kind, advisor=state, ahead=ahead)
        if state != 'idle' or ahead:  # merge seam M1: say once why the supervisor hears nothing for a while
            why = ('顾问未就绪（' + state + '）' if state not in ('idle', 'busy') else '前面还有 ' + str(ahead) + ' 条会诊')
            self.r.rpush(ADVICE_NOTES, '[advisor] waiting: ' + self.label(slot) + ' 的 ' + kind + ' 会诊排队中，' + why
                         + '；最多等 ' + str(ADVICE_WAIT) + ' 秒，超时按缺省')
        return True

    def advisor_poll(self, ticket, check, request=None):
        """('wait' | 'ok' | 'unavailable', sheet or None, errors) of a resident consultation. An unusable answer
        gets one correction; a second one, the time limit, or a failed / cancelled advisor turn: unavailable.
        ``request``: the input as it is now (rescue: idle slots may have changed), sent with the correction."""
        t = self.r.hgetall(ADVICE + ticket)
        errors = json.loads(t.get('errors') or '[]')
        now = time.time()
        if t.get('status') == 'answered':
            try:
                return 'ok', check(advisor.sheet_text({'result': t.get('raw') or ''})), errors
            except (advisor.AdviceError, ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc)[:500])
            attempt = int(t.get('attempt') or 1)
            if attempt >= advisor.ATTEMPTS or not self.advisor_enabled():
                return 'unavailable', None, errors
            request = request or t.get('request') or ''
            mid = self.advisor_send(ticket, request, error=errors[-1])
            self.r.hset(ADVICE + ticket, mapping={'status': 'pending', 'mid': mid, 'attempt': attempt + 1, 'request': request,
                                                  'phase': 'queued', 'deadline': now + ADVICE_WAIT, 'raw': '',
                                                  'errors': json.dumps(errors, ensure_ascii=False)})
            self.log('advice.correction', ticket=ticket, attempt=attempt + 1, error=errors[-1])
            return 'wait', None, errors
        if t.get('status') != 'pending':  # failed (a wall), abandoned, or already closed
            return 'unavailable', None, errors
        msg = self.v2.bus.message(t['mid']) if t.get('mid') else {}
        status = msg.get('status')
        if status in ('uncertain', 'expired') or (status == 'committed' and msg.get('outcome') != 'completed'):
            # The advisor turn failed, was held (fault, closed tab) or cancelled (restart / resume): §8 at once.
            errors.append('advisor turn ' + (msg.get('outcome') or status) + ': '
                          + str(msg.get('uncertain_reason') or msg.get('cancel_reason') or '')[:300])
            return 'unavailable', None, errors
        if self.agent(ADVISOR).get('spawn_failed') == '1':
            errors.append('advisor tab could not be started')
            return 'unavailable', None, errors
        if status in ('offered', 'received') and t.get('phase') == 'queued':
            # The advisor has it now: the time limit starts again for the answer itself.
            self.r.hset(ADVICE + ticket, mapping={'phase': 'answering', 'deadline': now + ADVICE_WAIT})
            return 'wait', None, errors
        if now >= float(t.get('deadline') or 0):
            errors.append('advisor gave no sheet within ' + str(ADVICE_WAIT) + ' s ('
                          + ('waiting for the advisor' if t.get('phase') == 'queued' else 'answering') + ')')
            if status == 'queued':
                self.advisor_cancel(t['mid'], 'consultation timed out')
            return 'unavailable', None, errors
        return 'wait', None, errors

    def advisor_cancel(self, mid, why):
        if self.v2 and self.r.hget(self.v2.bus.msg_key(mid), 'status') == 'queued':
            self.v2.cancel_queued(ADVISOR, {'message_id': mid}, why)

    def advisor_abandon(self, ticket, why):
        """Nobody waits for this consultation any more: closed as unavailable, its queued message withdrawn."""
        t = self.r.hgetall(ADVICE + ticket)
        if t.get('status') not in ('pending', 'answered'):
            return
        errors = json.loads(t.get('errors') or '[]') + [why]
        self.r.hset(ADVICE + ticket, mapping={'status': 'unavailable', 'effect': 'abandoned',
                                              'errors': json.dumps(errors, ensure_ascii=False)})
        if t.get('mid'):
            self.advisor_cancel(t['mid'], why)
        self.log('advice.abandoned', ticket=ticket, reason=why)

    def advisor_settled(self, ticket, mid, text):
        """The advisor's turn for one consultation ended (RuntimeV2.settle, inside its transaction): keep the raw
        answer for the waiting slot to validate. Never a report, never task state (§27)."""
        t = self.r.hgetall(ADVICE + ticket)
        if t.get('status') != 'pending' or t.get('mid') != mid:
            self.log('advice.late', ticket=ticket, message_id=mid)  # timed out / abandoned meanwhile: ignored
            return
        if len(text.strip()) <= brake.TURN_WALL_MAX and brake.classify_wall(text):
            # A usage wall instead of a sheet: this consultation falls back now, the tab restarts in place.
            errors = json.loads(t.get('errors') or '[]') + ['advisor hit a usage wall: ' + ' '.join(text.split())[:200]]
            self.r.hset(ADVICE + ticket, mapping={'status': 'failed', 'errors': json.dumps(errors, ensure_ascii=False)})
            self.setf(ADVISOR, advisor_fault='usage wall: ' + ' '.join(text.split())[:200])
            return
        self.r.hset(ADVICE + ticket, mapping={'status': 'answered', 'raw': text[-20000:]})

    def advisor_skeleton(self, info, t):
        """The advisor's handoff note (§27): recent sheets and their reasons from fx:advice:*, no worker text."""
        done = []
        for ticket in self.r.lrange(ADVICE_RECENT, 0, -1):
            a = self.r.hgetall(ADVICE + ticket)
            if a:
                done.append(ticket + ' ' + a.get('kind', '') + ' ' + a.get('slot', '') + ' [' + a.get('status', '')
                            + ']: ' + ' '.join((a.get('output') or '').split())[:400])
        return handoff.build_skeleton(
            info['handoff_id'], SESSION, 'fleet advisor: answer each consultation with one configuration sheet '
            '(advisor.md); the lowest model / effort that is enough, the smallest set of domains',
            done_lines=done, inprogress='none (handoff happens at an idle boundary)',
            next_lines=['read this note, reply READY, then answer each consultation the daemon sends on its own input'],
            pending='none', task=t.get('id', ''), hop=int(info['generation']) + 1,
            key_files=[str(self.run_dir / 'advisor.md')],
            risks=['earlier sheets are history, not input: never copy one without checking the matrix again',
                   'zero memory: this note carries no worker conversation, only the daemon\'s audit of sheets'])

    def advisor_start(self):
        """The first generation of the advisor tab (run start, or the switch turned on), in the supervisor's
        pane. A failed spawn falls back every consultation at once (§8) and is retried by advisor_tick."""
        info = self.agent(ADVISOR)
        generation = int(info['generation']) + 1 if info.get('generation') else 0
        self.setf(ADVISOR, label='advisor' if generation == 0 else slot_base(ADVISOR) + '.' + str(generation),
                  generation=generation, handoff_id='', restart_at=time.time() + ADVISOR_RESTART_GAP)
        self.advisor_slot = True
        self.names = self.slot_names()
        try:
            pane = self.spawn(ADVISOR, 'You are the fleet advisor ' + self.label(ADVISOR) + '. There is no '
                              'consultation yet: your final reply must be exactly READY.')
        except (Exception, SystemExit) as exc:
            self.setf(ADVISOR, state='dead', spawn_failed=1, launch_ready=0)
            self.log('advisor.spawn_failed', level='error', error=str(exc)[:500])
            return
        self.place_advisor(pane)
        self.refresh_history()
        self.log('advisor.started', generation=generation, pane=pane)

    def place_advisor(self, pane):
        """The advisor's tab joins the supervisor's pane (after the supervisor tab): the fixed explicit window."""
        sup = self.agent('supervisor').get('pane')
        if not (sup and self.mgr.tab_into(pane, sup, before=False)):
            self.log('advisor.placement_failed', level='warning', pane=pane, supervisor=sup or '')

    def advisor_tick(self):
        """Resident advisor tab lifecycle, outside any v2 transaction (§27): start it when the switch turns on,
        retire it once its current consultation is done when the switch turns off, restart it in place after
        a fault (watchdog hold, closed tab, wall, startup timeout / failure). Never pauses or blocks the fleet."""
        if not self.advisor_resident():
            return
        on, info = self.advisor_enabled(), self.agent(ADVISOR)
        if not self.advisor_slot:
            if on:
                self.advisor_start()
            return
        busy = self.r.hget(self.v2.bus.lease_key(ADVISOR), 'status') in ('offered', 'received')
        if not on:
            if not busy and info.get('state') not in ('busy', 'handoff'):
                self.advisor_retire()
            return
        failed = info.get('state') in ('dead', 'held') or info.get('advisor_fault')
        if failed and not self.paused() and time.time() >= float(info.get('restart_at') or 0):
            self.advisor_restart(info.get('advisor_fault') or ('startup failed' if info.get('state') == 'dead'
                                                                else 'held'))

    def advisor_retire(self):
        info = self.agent(ADVISOR)
        for mid in self.v2.bus.pending(ADVISOR):  # their tickets were abandoned when the switch went off
            self.v2.cancel_queued(ADVISOR, {'message_id': mid}, 'advisor turned off')
        if info.get('pane') and not self.v2.retire(ADVISOR, 'advisor turned off'):
            self.log('advisor.retire_failed', level='error', pane=info['pane'])
            return
        history.record(self.run_dir, slot=ADVISOR, label=self.label(ADVISOR), generation=int(info.get('generation') or 0),
                       handoff='', snapshot='', source='retired', timed_out=False)
        self.setf(ADVISOR, state='retired', pane='', launch_ready=0, runtime_ready=0)
        self.advisor_slot = False
        self.names = self.slot_names()
        self.refresh_history()
        self.log('advisor.retired')

    def advisor_restart(self, reason, source='advisor-restart'):
        """Next generation of the advisor tab next to the supervisor: after a fault, for a §22 handoff
        (``source='handoff'``) and on start --resume. Every failure here is an advisor fault (design §27 §2c-6):
        nothing raises into the daemon, nothing blocks the task, pauses the fleet or holds another slot. On
        failure the queued consultations are withdrawn (their slots fall back to the default on their next
        pump) and advisor_tick tries the next generation again after ADVISOR_RESTART_GAP."""
        info = self.agent(ADVISOR)
        generation, old, prev = int(info.get('generation') or 0), info.get('pane'), self.label(ADVISOR)
        self.setf(ADVISOR, restart_at=time.time() + ADVISOR_RESTART_GAP, advisor_fault='', handoff_id='')
        try:
            alive = bool(old) and self.mgr.get_pane_status(old) is not None
            hid = ('handoff-' if source == 'handoff' else 'restart-') + uuid.uuid4().hex[:8]
            note = self.run_dir / 'handoffs' / (ADVISOR + '-g' + str(generation) + '-' + hid + '.md')
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(self.skeleton(ADVISOR, dict(info, handoff_id=hid)))
            snapshot = ''
            if alive:  # retire first: two advisor sessions never run at once
                snapshot = str(note.with_suffix('.terminal.txt'))
                self.mgr.capture_to(old, snapshot)
                if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                    return self.advisor_failed('held', reason, 'old advisor tab ' + prev + ' did not close')
            self.v2.confirm_retired(ADVISOR)  # its in-flight consultation is cancelled: that slot falls back (§8)
            history.record(self.run_dir, slot=ADVISOR, label=prev, generation=generation, handoff=str(note),
                           snapshot=snapshot, source=source, timed_out=source != 'handoff')
            label = slot_base(ADVISOR) + '.' + str(generation + 1)
            self.setf(ADVISOR, generation=generation + 1, label=label)
            why = ('took over at its context threshold' if source == 'handoff' else
                   'the advisor tab ' + prev + ' failed: ' + ' '.join(reason.split())[:200])
            pane = self.spawn(ADVISOR, '[Advisor takeover] You are ' + label + ' (' + why + '). Read the handoff note '
                              + str(note) + '. In this turn only read the note and make your final reply exactly READY.')
        except (Exception, SystemExit) as exc:
            return self.advisor_failed('dead', reason, str(exc))
        self.place_advisor(pane)
        try:
            self.refresh_history()
        except Exception as exc:  # the page is cosmetic: never an advisor failure
            self.log('advisor.history_failed', level='warning', error=str(exc)[:300])
        self.log('advisor.restarted', prev=prev, label=label, reason=reason, source=source)

    def advisor_failed(self, state, reason, error):
        """The next advisor generation could not be started: dead (no tab) or held (old tab still there).
        Queued consultations fall back now; advisor_tick retries after the gap."""
        fields = dict(state=state, launch_ready=0, runtime_ready=0, advisor_fault=('restart failed: ' + error)[:300])
        if state == 'dead':
            fields['spawn_failed'] = 1
        self.setf(ADVISOR, **fields)
        try:
            for mid in self.v2.bus.pending(ADVISOR):
                self.v2.cancel_queued(ADVISOR, {'message_id': mid}, 'advisor restart failed')
        except Exception as exc:
            self.log('advisor.cancel_failed', level='error', error=str(exc)[:300])
        self.log('advisor.restart_failed', level='error', reason=reason, error=error[:500])

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

    def advice_waiting(self, name, identity):
        """The sheet a slot holds while its model is at max_concurrent (this queued message only), or None."""
        waiting = json.loads(self.agent(name).get('advice_wait') or '{}')
        return waiting.get('sheet') if identity and waiting.get('msg') == identity else None

    def advice_gate(self, name, identity, sheet, card, registry):
        """Fleet.card_gate before an advisor sheet starts a generation (same meaning as deliver()'s
        Phase 5 switch): host mismatch raises (the caller blocks the task, no silent swap); a full
        model keeps the message queued, profile_state=pending, audits profile.capacity_wait once per
        reason and returns True (retried every tick). None = may go on."""
        gate = self.card_gate(name, card, registry)
        if gate and gate[0] == 'block':
            raise ValueError(gate[1])
        info = self.agent(name)
        if gate:
            if info.get('capacity_wait') != gate[1]:
                self.log('profile.capacity_wait', agent=name, profile=card.id if card else '', reason=gate[1])
            self.setf(name, profile_state='pending', profile_reason=gate[1], capacity_wait=gate[1],
                      advice_wait=json.dumps({'msg': identity, 'sheet': sheet}, ensure_ascii=False))
            return True
        if info.get('capacity_wait') or info.get('advice_wait'):
            self.setf(name, capacity_wait='', advice_wait='')
        return None

    def advice_switch(self, name, identity, sheet, launch, card, registry):
        """Respawn for an advisor sheet once card_gate admits it; False = never deliver now.

        The sheet becomes the slot's launch record (runtime_launch via switch_profile, advice_domains for
        the successor's kit) only when a successor really starts. A full model, or a codex slot whose
        native subagents still run (switch_profile defers without spawning), keeps the sheet in
        advice_wait for this message: retried every tick, never re-advised, never delivered to the old
        runtime as if advised."""
        if self.advice_gate(name, identity, sheet, card, registry):
            return False
        self.setf(name, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
        try:
            self.switch_profile(name, launch)
        except BaseException:
            self.setf(name, advice_domains='')  # nothing launched with it: never staged for another spawn
            raise
        if self.agent(name).get('state') == 'starting':
            self.record_advice(name, sheet, 'respawn')
        else:
            self.setf(name, advice_domains='', advice_wait=json.dumps({'msg': identity, 'sheet': sheet}, ensure_ascii=False))
        return False

    def drop_advice_wait(self, name):
        """The advisor went off while this slot's sheet waited (model ticket / codex subagents): nothing was
        launched with it, so the slot keeps its record and the queued message goes to its running runtime."""
        if self.agent(name).get('advice_wait'):
            self.setf(name, advice_wait='', capacity_wait='', profile_state='', profile_reason='')
            self.log('advice.wait_dropped', agent=name)

    def advise_before_delivery(self, name, text):
        """True = go on delivering (no change, or a hot-load queued); False = respawning / blocked."""
        identity = self.advice_identity(name, text)
        info = self.agent(name)
        waiting = self.advice_waiting(name, identity)
        if waiting is not None:  # a decided respawn waiting for its model ticket: gate again, never re-advise
            cli = agent_cli(self.runtime_cfg(name), name)
            try:
                registry = AgentRegistry()
                cards = advisor.launch_cards(registry, cli)
                return self.advice_switch(name, identity, waiting, self.advice_launch(cli, waiting, cards, registry),
                                          cards[waiting['model']], registry)
            except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
                self.block_profile(name, str(exc))
                return False
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        pending = self.advice_pending_for(name, identity)
        if pending is not None:  # out with the resident advisor (§27): the message stays queued meanwhile
            result = self.advice_poll(name, pending)
        else:
            if identity and info.get('advice_msg') == identity:
                return True  # already advised (e.g. delivered again after the respawn's READY)
            entry = self.take_pending_advice(name, text)
            if entry is None:
                return True
            self.setf(name, advice_msg=identity)
            result = self.advise(name, entry, cli, cfg, identity=identity)
        if result is None:
            return False
        sheet, current, cards, registry = result
        installed = json.loads(info.get('installed_domains') or '[]')
        target = advisor.fingerprint(cli, sheet['model'], sheet.get('effort'))
        effect = advisor.decide(current, target, installed, sheet['domains'])
        try:
            if effect == 'respawn':
                launch = self.advice_launch(cli, sheet, cards, registry)
                # Phase 5 path behind the model card gate; blocks the task itself on failure
                return self.advice_switch(name, identity, sheet, launch, cards[sheet['model']], registry)
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
        identity = self.advice_identity(name, text)
        sheet = self.advice_waiting(name, identity)
        try:
            if sheet is None:
                pending = self.advice_pending_for(name, identity)
                if pending is not None:
                    result = self.advice_poll(name, pending)
                else:
                    entry = self.take_pending_advice(name, text) or {'instruction': text[:2000], 'note': ''}
                    result = self.advise(name, entry, 'claude', self.runtime_cfg(name), identity=identity)
                    self.setf(name, advice_msg=identity)
                if result is None:
                    return  # held until the resident advisor answers, at most ADVICE_WAIT (§27)
                if self.advisor_resident() and self.r.hget(ADVICE + result[0]['ticket'], 'status') == 'unavailable':
                    # The advisor tab could not start or did not answer in time (180 s): the user's rule is the
                    # plain default launch; the [advisor unavailable] note is already queued (advice_finish).
                    self.release_default(name)
                    return
                sheet, current, cards, registry = result
            else:  # waited for a model ticket with this sheet: gate again, never re-advise
                registry = AgentRegistry()
                cards = advisor.launch_cards(registry, 'claude')
            launch = self.advice_launch('claude', sheet, cards, registry) if sheet['model'] in cards else {}
            # An unreadable registry (advisor unavailable) has no cards to gate; the fallback never switches.
            if (not isinstance(registry, tuple)
                    and self.advice_gate(name, identity, sheet, cards[sheet['model']] if launch else None, registry)):
                return  # model full: keep holding the first generation until a ticket frees up
            # Admitted: only now does the sheet become this slot's launch record (staged domains + launch).
            self.setf(name, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
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

    def release_default(self, name):
        """The advisor went offline while this Claude slot waited for its first sheet: open its gate with
        the slot defaults, laid out like a spawn without the advisor (snapshot, CLAUDE.md, kit copy)."""
        generation = int(self.agent(name).get('generation') or 0)
        launch = json.loads(self.agent(name).get('runtime_launch') or '{}')
        cfg = self.runtime_cfg(name)
        cfg.pop('advisor', None)
        self.drop_advice_wait(name)
        # It launches with the slot defaults, and that is its record (installed_domains '' = default layout;
        # runtime_launch as it was). Only a held first generation gets here, so no running record is cleared.
        self.setf(name, installed_domains='', advice_domains='')
        try:
            save(self.run_dir / 'runtime' / (name + '-g' + str(generation) + '.json'), cfg)
            cwd = agent_cwd(self.cfg, name)
            scope = self.role_scope(name, cfg, launch)
            (cwd / 'AGENTS.md').write_text(scope)
            kitmod.Kit.clear(cwd)
            (cwd / 'CLAUDE.md').write_text(scope)
            self.install_default_kit(cwd, scope)
        except (OSError, ValueError) as exc:
            self.setf(name, advice_hold=0)
            self.block_profile(name, 'release without advisor failed: ' + str(exc), dead=True)
            return
        fields = dict(advice_hold=0, launch_ready=1)
        if self.v2:
            fields['startup_deadline'] = time.time() + float(self.cfg['profile_ready_timeout'])
        self.setf(name, **fields)
        self.log('advice.released_default', agent=name)

    # -- advisor mid-task rescue (advisor plan §3.2 / §4.2: consult -> respawn | reassign | give_up)
    def consult_decision(self, decision, state):
        """Validate the supervisor's consult items against the task table (no side effects)."""
        dispatched = {self.resolve(x['to']) for x in decision['dispatch']}
        ids = {x.get('id') for x in decision['dispatch']}
        waiting = {json.loads(x)['task'] for x in self.r.lrange(RESCUE, 0, -1)}
        out = []
        for item in decision['consult']:
            slot, task_id = self.resolve(item['worker']), item['task']
            owner, instruction, status = self.escalation_item(task_id, state)
            if owner != slot or not instruction or status in ('escalated', 'delegated'):
                raise ValueError('consult: ' + task_id + ' is not a task of ' + self.label(slot)
                                 + ' (consult names the dispatch id that worker last ran)')
            if slot in dispatched or task_id in ids:
                raise ValueError('consult: ' + task_id + ' / ' + self.label(slot) + ' is also dispatched in this '
                                 'reply; consult first, then act on the advisor\'s answer')
            if task_id in waiting:
                raise ValueError('consult: ' + task_id + ' is already with the advisor; wait for its answer')
            out.append({'slot': slot, 'task': task_id, 'finding': ' '.join(item['finding'].split())[:2000],
                        'want': ' '.join(item.get('want', '').split())[:1000]})
        return out

    def consult_apply(self, consults):
        for c in consults:  # offline: advisor_decision already answered with the one [advisor off] note
            if self.advisor_online():
                self.r.rpush(RESCUE, json.dumps(dict(c, run_task=self.task().get('id', '')), ensure_ascii=False))
                self.log('advice.consult', agent=c['slot'], task=c['task'])
            else:
                self.log('advice.consult_off', agent=c['slot'], task=c['task'])

    def rescue_tick(self):
        """Ready notes of reassigned slots; then one consult request whose worker is idle."""
        if not self.running():
            return
        for w in self.workers:
            info = self.agent(w)
            if info.get('rescue_note') and info.get('state') == 'idle':
                self.setf(w, rescue_note='')
                self.queue('supervisor', info['rescue_note'])
        pending = json.loads(self.r.get(RESCUE_PENDING) or 'null')
        if pending:  # the one rescue out with the resident advisor (§27): read its answer, nothing new meanwhile
            if pending['req'].get('run_task') != self.task().get('id'):
                self.r.delete(RESCUE_PENDING)
                self.advisor_abandon(pending['ticket'], 'left over from an earlier task')
            elif (self.r.hget(ADVICE + pending['ticket'], 'status') != 'pending'
                    or self.advisor_poll(pending['ticket'], None)[0] != 'wait'):
                self.rescue(pending['req'], pending)  # answered or over: rebuild its check and act
            return
        for raw in self.r.lrange(RESCUE, 0, -1):
            req = json.loads(raw)
            if req.get('run_task') != self.task().get('id'):
                self.r.lrem(RESCUE, 1, raw)  # left over from an earlier task
                continue
            if self.agent(req['slot']).get('state') != 'idle' or self.inbox_depth(req['slot']):
                continue  # judged at the worker's next idle boundary
            self.r.lrem(RESCUE, 1, raw)
            if not self.advisor_online():  # accepted while online, turned off before the worker went idle
                self.queue('supervisor', self.consult_off_note([req]) + '\nReply with the JSON format from AGENTS.md.')
            else:
                self.rescue(req)
            return  # at most one consultation per pump

    def advice_options(self, name, registry):
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        cards = advisor.launch_cards(registry, cli)
        model, effort = advisor.runtime_model(cfg, cli), advisor.runtime_effort(cfg, cli)
        info = self.agent(name)
        return {'name': name, 'cli': cli, 'cards': cards, 'model': model, 'effort': effort,
                'current': advisor.fingerprint(cli, model, effort),
                'models': list(dict.fromkeys(([model] if model else []) + list(cards))),
                'generation': int(info.get('generation') or 0),
                'installed_domains': json.loads(info.get('installed_domains') or '[]')}

    def rescue_candidates(self, name):
        """Idle slots a reassign may go to (a v2 Claude slot still held for its first sheet counts)."""
        out = []
        for w in self.active_workers():
            info = self.agent(w)
            held = (info.get('advice_hold') == '1' and info.get('state') == 'starting'
                    and info.get('launch_ready') == '0')
            if (w != name and (info.get('state') == 'idle' or held) and not self.inbox_depth(w)
                    and not info.get('rescue_note') and not info.get('rescue_relay')):
                out.append(w)
        return out

    def rescue(self, req, pending=None):
        """One rescue consultation. Unavailable advisor = no action, the supervisor is told (§8). With the
        resident advisor it is queued (RESCUE_PENDING) and acted on once answered, at the worker's idle
        boundary (``pending``: that consultation, read again on each pump)."""
        name, task_id, t = req['slot'], req['task'], self.task()
        state = self.contract_state()
        instruction = self.escalation_item(task_id, state)[1] or ''
        relay_key = t.get('id', '') + '|' + task_id + '|' + name
        relays = int(self.r.hget(RESCUE_RELAYS, relay_key) or 0)
        try:
            registry, registry_error = AgentRegistry(), None
        except (OSError, ValueError) as exc:
            registry, registry_error = (), 'agent registry unreadable: ' + str(exc)[:400]
        candidates = self.rescue_candidates(name)
        opts = {w: self.advice_options(w, registry) for w in [name] + candidates}
        actions = advisor.rescue_actions(relays, candidates)
        matrix, matrix_error = advisor.load_matrix()
        catalog = self.kit.domains_catalog() if self.kit else []
        names = [c['name'] for c in catalog]

        def view(o, models=False):
            out = {k: o[k] for k in ('name', 'cli', 'generation', 'model', 'effort', 'installed_domains')}
            out['pinned_domains'] = self.floor_domains() if o['cli'] == 'claude' else []
            return dict(out, models=o['models']) if models else out
        last = json.loads(self.agent(name).get('advice_last') or '{}')
        payload = advisor.build_rescue_input(
            goal=t.get('goal', ''), task=task_id, instruction=instruction, finding=req['finding'], want=req['want'],
            slot=view(opts[name], True), previous_sheet=last.get('sheet') if last.get('task') == t.get('id') else None,
            relays=relays, actions=actions, candidates=[view(opts[c], True) for c in candidates],
            matrix=matrix, catalog=catalog)

        def check_slot(target, raw):
            raw = self.drop_floor(raw)
            o = opts[target or name]
            wl = (o['cards'][raw.get('model')].kit_domains if raw.get('model') in o['cards']
                  else o['cards'][o['model']].kit_domains if o['model'] in o['cards'] else None)
            return advisor.validate(raw, cli=o['cli'], models=o['models'], current=o['current'],
                                    catalog=names if o['cli'] == 'claude' else [], whitelist=wl, matrix=matrix,
                                    cards=o['cards'])

        def check(raw):
            return advisor.validate_rescue(raw, actions=actions, check_slot=check_slot, candidates=candidates)
        shown = json.dumps({k: payload[k] for k in ('goal', 'task', 'instruction', 'finding', 'want', 'slot',
                                                    'relays', 'actions')}, ensure_ascii=False)[:4000]
        started, envelope = time.time(), None
        if pending is not None:
            if (self.r.hget(ADVICE + pending['ticket'], 'status') == 'answered'
                    and (self.agent(name).get('state') != 'idle' or self.inbox_depth(name))):
                return  # answered: acted on at the worker's next idle boundary
            # The correction carries the input as it is now: a reassign target that went busy meanwhile is
            # refused against the current idle slots and the advisor sees those (review astra r1 P2-01).
            state_, sheet, errors = self.advisor_poll(pending['ticket'], check, json.dumps(payload, ensure_ascii=False))
            if state_ == 'wait':
                return
            self.r.delete(RESCUE_PENDING)
            ticket = pending['ticket']
            started = float(self.r.hget(ADVICE + ticket, 'started') or started)
        elif self.advisor_resident() and not registry_error:
            ticket = self.new_ticket()
            if self.advisor_request(ticket, name, 'rescue', payload, shown):
                self.r.set(RESCUE_PENDING, json.dumps({'ticket': ticket, 'req': req}, ensure_ascii=False))
                return
            sheet, errors = None, ['advisor tab could not be started']
        else:
            runner = self.advisor_runner or (lambda data: advisor.run_claude(data, cfg=self.cfg, run_dir=self.run_dir))
            if registry_error:
                sheet, errors = None, [registry_error]
            else:
                sheet, envelope, errors = advisor.consult(payload, runner, check)
            ticket = self.new_ticket()
        audit = {'slot': name, 'task': t.get('id', ''), 'kind': 'rescue', 'item': task_id,
                 'input': shown,
                 'output': json.dumps(sheet, ensure_ascii=False), 'errors': json.dumps(errors, ensure_ascii=False),
                 'elapsed_s': round(time.time() - started, 3), 'status': 'unavailable' if sheet is None else 'ok',
                 'action': (sheet or {}).get('action', 'none'), 'at': time.time()}
        audit.update(advisor.usage_fields(envelope))
        if matrix_error:
            audit['matrix_error'] = matrix_error[:500]
        self.r.hset(ADVICE + ticket, mapping=audit)
        self.advice_recent(ticket)
        who = task_id + ' (' + self.label(name) + ')'
        if sheet is None:
            self.log('advice.unavailable', agent=name, ticket=ticket, path='rescue', errors=errors)
            self.queue('supervisor', '[advisor unavailable] consult ' + who + '：顾问不可用，未做任何动作（'
                       + (errors[-1] if errors else '')[:300] + '）。Decide yourself: dispatch a correction, or BLOCKED.'
                       '\nReply with the JSON format from AGENTS.md.')
            return
        sheet['ticket'] = ticket
        self.log('advice.rescue', agent=name, ticket=ticket, task=task_id, action=sheet['action'])
        if sheet.get('capability_gap'):
            self.log('capability.gap', agent=name, ticket=ticket, capability=sheet.get('capability'), model=sheet['model'])
        if sheet['action'] == 'give_up':
            self.queue('supervisor', '[advisor] give_up ' + task_id + ': ' + sheet['reason']
                       + '\nNo configuration of ' + self.label(name) + ' or another slot is expected to finish '
                       + task_id + ': reply BLOCKED with this reason.\nReply with the JSON format from AGENTS.md.')
            return
        target = sheet.get('to', name)
        self.setf(target, advice_last=json.dumps({'task': t.get('id', ''), 'sheet': sheet}, ensure_ascii=False))
        try:
            if sheet['action'] == 'respawn':
                if self.rescue_respawn(name, task_id, sheet, opts[name], registry, state):
                    self.r.hincrby(RESCUE_RELAYS, relay_key, 1)
            else:
                self.rescue_reassign(name, task_id, sheet, opts[target], registry, state)
        except (OSError, ValueError, RuntimeError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
            self.block_profile(target, 'advisor ' + sheet['action'] + ' failed: ' + str(exc))

    def rescue_message(self, task_id, state, slot):
        """The original instruction of a task, in the same envelope the supervisor's dispatch had."""
        t = self.task()
        if state is not None:
            row = state['tasks'][task_id]
            return row.get('message') or self.contract_message(t.get('id', ''), row)
        instruction = self.escalation_item(task_id, None)[1]
        return ('[Supervisor instruction ' + t.get('id', '') + '] [' + task_id + '] ' + instruction
                + '\nShared output directory: ' + str(self.workspace()) + ' (write only inside your own '
                'subdirectory ' + slot + '/). Reply in the AGENTS.md format when done.')

    def rescue_respawn(self, name, task_id, sheet, opt, registry, state):
        """Same slot, next generation: the old one writes the three-section handoff, the successor starts
        with the new sheet (finish_rotation), reads the note, returns READY, then gets the task again."""
        launch = None
        if advisor.fingerprint(opt['cli'], sheet['model'], sheet.get('effort')) != opt['current']:
            launch = self.advice_launch(opt['cli'], sheet, opt['cards'], registry)
            gate = self.card_gate(name, opt['cards'][sheet['model']], registry)  # same gate as pre-dispatch (P2-9)
            if gate and gate[0] == 'block':
                raise ValueError(gate[1])
            if gate:  # model full: nothing done, the relay is not counted; the supervisor decides
                self.r.hset(ADVICE + sheet['ticket'], mapping={'effect': 'capacity_wait'})
                self.log('profile.capacity_wait', agent=name, profile=launch['profile_id'], reason=gate[1], path='rescue')
                self.queue('supervisor', '[advisor] respawn ' + task_id + ' (' + self.label(name) + ') 未执行：'
                           + gate[1] + '。Decide yourself: dispatch a correction, consult again later, or BLOCKED.'
                           '\nReply with the JSON format from AGENTS.md.')
                return False
            self.preflight_profile(launch)  # Phase 5: dependencies first, never a fallback CLI
        text = (self.rescue_message(task_id, state, name) + '\n[advisor] 上一代已做：见交接单；本代领域：'
                + (', '.join(sheet['domains']) or '无') + '。')
        if state is not None:
            row = state['tasks'][task_id]
            row.update(status='running', attempts=row.get('attempts', 0) + 1, reason='advisor respawn')
            self.contract_queue(state, name, text, task_id)  # queued behind the handoff, delivered after READY
            self.save_contract(state)
        else:
            self.queue(name, text)
        self.setf(name, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False),
                  rescue_hint=sheet.get('handoff_hint', ''), rescue_relay=1,
                  rescue_launch=profile_launch.encode_launch(launch) if launch else '')
        self.record_advice(name, sheet, 'respawn')
        used, window = self.usage(name)
        self.start_rotation(name, used, window, prompt=handoff.RESCUE_PROMPT)
        return True

    def rescue_reassign(self, name, task_id, sheet, opt, registry, state):
        """Take the task back from the stuck worker and start the target slot per the sheet; the
        supervisor re-dispatches (dispatch stays with the supervisor). The old worker gets no handoff."""
        to, t = sheet['to'], self.task()
        if state is not None:
            row = state['tasks'][task_id]
            row.update(status='running', attempts=row.get('attempts', 0) + 1,
                       reason='reassigned by the advisor: ' + self.label(name) + ' -> ' + self.label(to)
                       + '; waits for the supervisor to dispatch it again')
            self.contract_log('task.reassigned', task=task_id, by='advisor', frm=name, to=to)
            self.save_contract(state)
        else:
            items = json.loads(self.r.get(ITEMS) or '{}')
            items.setdefault(task_id, {})['to'] = ''
            self.r.set(ITEMS, json.dumps(items, ensure_ascii=False))
        shown = sheet['model'] + ('/' + sheet['effort'] if sheet.get('effort') else '')

        def note():  # after a switch the slot's label is the new generation's
            return ('[advisor] ' + task_id + ' → ' + self.label(to) + ' (' + shown + ', domains: ['
                    + ', '.join(sheet['domains']) + '])：reassign，' + self.label(name) + ' 不再负责 ' + task_id
                    + '；请重新 dispatch ' + task_id + ' 给 ' + self.label(to) + '（' + sheet['reason'] + '）'
                    + '\nReply with the JSON format from AGENTS.md.')
        # The re-dispatch of this task to `to` uses this sheet instead of a second consultation.
        self.setf(to, advice_preset=json.dumps({'task': task_id, 'run_task': t.get('id', ''), 'sheet': sheet},
                                               ensure_ascii=False))
        info = self.agent(to)
        held = info.get('advice_hold') == '1' and info.get('state') == 'starting'
        if not held and advisor.fingerprint(opt['cli'], sheet['model'], sheet.get('effort')) != opt['current']:
            launch = self.advice_launch(opt['cli'], sheet, opt['cards'], registry)
            gate = self.card_gate(to, opt['cards'][sheet['model']], registry)  # same gate as pre-dispatch (P2-9)
            if gate and gate[0] == 'block':
                raise ValueError(gate[1])
            if gate is None:
                self.setf(to, advice_domains=json.dumps(sheet['domains'], ensure_ascii=False))
                try:
                    self.switch_profile(to, launch)  # Phase 5 idle switch; the note goes out after its READY
                except BaseException:
                    self.setf(to, advice_domains='')
                    raise
                if self.agent(to).get('state') == 'starting':
                    self.record_advice(to, sheet, 'respawn')
                    self.setf(to, rescue_note=note())
                    return
                self.setf(to, advice_domains='')  # deferred (codex subagents): nothing launched with it
            # Model full or switch deferred: the preset carries the sheet; the re-dispatch applies it through
            # advice_gate / advice_switch like any pre-dispatch sheet. Tell the supervisor now.
            if self.task().get('status') == 'running':
                self.queue('supervisor', note())
            return
        self.r.hset(ADVICE + sheet['ticket'], mapping={'effect': 'reassign'})
        self.queue('supervisor', note())  # held slot: released by the re-dispatch with this sheet

    def send(self, name, pane, text):
        """Codex accepts verbatim paste; Claude and Pi use a file pointer with a delivery id."""
        if self.v2:
            raise ValueError('v2 deliveries use Redis authorization; file/paste fallback forbidden')
        if codex_queue.is_codex(self.runtime_cfg(name), name):
            # The TUI takes it from its own queue; on_turn settles only the turn the rollout binds to it.
            try:
                codex_queue.deliver(self, name, codex_queue.new_delivery_id('v1'), text)
            except codex_queue.QueueError:
                return False  # never falls back to typing
            return True
        # Claude's Stop hook reads the pointer from its transcript; Pi's extension captures it
        # from before_agent_start. Both bind each turn end to exactly one daemon delivery.
        box = agent_cwd(self.cfg, name) / '.inbox'
        box.mkdir(exist_ok=True)
        mid = 'msg-' + time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        msg = box / (mid + '.md')
        msg.write_text(text)
        self.setf(name, msg_id=mid)
        first = ' '.join(text.split('\n', 1)[0].split())[:200]
        pointer = first + ' -- the full message is in ' + str(msg) + ' . Read that whole file first and follow it.'
        if claude_hook.enabled(self.runtime_cfg(name), name):
            # The Stop hook hands the pointer over; on_turn binds by the msg id in its feedback row.
            generation = int(self.agent(name).get('generation') or 0)
            k = claude_hook.keys('fx', name, generation)
            if not claude_hook.post(self.r, k, {'kind': 'pointer', 'msg_id': mid, 'generation': generation,
                                                 'text': pointer}):
                self.log('hook.unarmed', agent=name, msg_id=mid,
                         reason='no Stop hook waiting; the next turn end of this session picks it up')
            return True
        return self.mgr.send_prompt_to_idle(pane, pointer)

    def start_rotation(self, name, used, window, prompt=None):
        info = self.agent(name)
        if info.get('state') == 'clearing' or info.get('clear_required') == '1':
            return
        if name == ADVISOR:
            # §22 handoff of the advisor (§27): it has only Read and cannot write a draft, so no handoff turn;
            # the daemon's skeleton (recent sheets) is its note. Failures stay advisor faults (advisor_restart).
            self.log('rotate.start', agent=name, used=used, window=window, generation=info.get('generation'))
            return self.advisor_restart('context ' + str(used) + '/' + str(window), source='handoff')
        hid = uuid.uuid4().hex[:10]
        draft = agent_cwd(self.cfg, name) / '.handoff' / ('draft-' + hid + '.md')
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(handoff.partial_template())
        self.setf(name, state='handoff', handoff_id=hid, handoff_draft=draft,
                  handoff_mtime=draft.stat().st_mtime_ns,
                  handoff_deadline=time.time() + float(self.cfg['handoff_timeout']))
        self.log('rotate.start', agent=name, used=used, window=window, generation=info.get('generation'))
        prompt = ((prompt or handoff.DUMP_PROMPT).format(path=draft, used=used, window=window)
                  + ' This is a same-task handoff: do not open windows, do not continue the task itself.')
        if self.v2:
            self.v2.offer_handoff(name, prompt)
            return
        if not self.send(name, info['pane'], prompt):
            self.finish_rotation(name, timed_out=True)

    def skeleton(self, name, info):
        t = self.task()
        if name == ADVISOR:
            return self.advisor_skeleton(info, t)
        replies = self.r.lrange(key('replies', name), 0, -1)
        pending = self.v2.pending_text(name) if self.v2 else self.r.lrange(key('inbox', name), 0, 0)
        back = json.loads(info.get('subfleet_return') or '{}')  # §29: the slot comes back from a named sub-fleet
        facts, open_ids = subfleet.return_lines(back) if back else ([], 'none')
        return handoff.build_skeleton(
            info['handoff_id'], SESSION, t.get('goal') or (
                ('resident service: route each request to a worker' if name == 'supervisor'
                 else 'resident service: answer each request as role ' + name) if self.service
                else '(no task yet, standing by)'),
            done_lines=facts + ['recent reply: ' + ' '.join(x.split())[:600] for x in replies],
            inprogress=(handoff.midturn_progress(info.get('midturn_message', ''), info['midturn_instruction'])
                        if info.get('midturn_instruction') else 'none (handoff happens at an idle boundary)'),
            next_lines=['read this note, reply READY, then wait for the daemon to deliver the next message'
                        + (' (pending: ' + ' '.join(pending[0].split())[:300] + ')' if pending else '')],
            pending=open_ids, task=t.get('id', ''), hop=int(info['generation']) + 1,
            key_files=[str(self.workspace()), str(agent_cwd(self.cfg, name) / 'AGENTS.md')],
            risks=['role ' + name + ' and task ' + t.get('id', 'none') + ' are unchanged',
                   'anything the model claims as done must be checked against the artifacts'])

    def finish_rotation(self, name, timed_out=False):
        codex_queue.clear(self, name, 'rotation')  # its token can never settle (Codex's queue is not edited)
        back = self.agent(name).get('subfleet_return')
        if self.v2 and timed_out and not (back and not self.v2.handoff_taken(name)):
            # (§29 return: a handoff the returned session never took ran nothing; retiring it needs no hold)
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
        if info.get('rescue_hint'):  # advisor rescue: its handoff_hint goes after the merge, never dropped
            out.write_text(handoff.add_next_step(out.read_text(), info['rescue_hint']))
        old = info['pane']
        snapshot = out.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        prev = self.label(name)
        label = slot_base(name) + '.' + str(generation + 1)  # supervisor-1 -> supervisor-1.1, worker-1 -> worker-1.1
        self.setf(name, generation=generation + 1, handoff_id='', label=label)
        if name in self.workers:
            self.setf(name, prev_label=prev)
            self.r.hset(NAMES, mapping={label: name})  # old names keep routing to this slot
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff=str(out),
                       snapshot=str(snapshot), source=source, timed_out=timed_out)
        if info.get('rescue_launch'):  # advisor rescue respawn: the successor starts with the new sheet's runtime
            self.setf(name, runtime_launch=info['rescue_launch'], rescue_launch='')
        if back:  # §29: the successor is told where its predecessor was, not "same task"
            self.setf(name, subfleet_return='')
            prompt = subfleet.return_prompt(json.loads(back)['name'], str(out)).format(label=label)
        else:
            prompt = ('[Same-task takeover] You are ' + label + ' (taking over from ' + prev
                      + '). Read the handoff note ' + str(out) + ' and continue from it. In this turn only read '
                      'the note, do no task work, and make your final reply exactly READY.')
        new = self.spawn(name, prompt, launch_held=bool(self.v2))
        placed = self.mgr.tab_into(new, old)  # the successor takes the retiring session's place on the page
        if (self.v2 and not placed) or not self.mgr.kill_pane(old) or (self.v2 and self.mgr.get_pane_status(old) is not None):
            if self.v2:
                self.setf(name, state='dead', launch_ready=0, retired_pane=old,
                          retired_launch=info.get('runtime_launch') or '')  # the old pane's launch (before rescue_launch)
            if self.v2:
                self.v2.transaction('midturn-rotate-failed-' + uuid.uuid4().hex, lambda: self.v2.midturn_clear(name))
            self.finish('blocked', 'could not close the old session ' + prev + '; two sessions hold slot ' + name)
            return
        if self.v2:
            self.v2.confirm_retired(name)
            self.v2.transaction('midturn-rotated-' + uuid.uuid4().hex, lambda: self.v2.midturn_clear(name, resume=False))
            self.setf(name, launch_ready=1, retired_pane='', retired_launch='')
        self.refresh_history()
        self.log('rotate.done', agent=name, label=label, prev=prev, generation=generation + 1,
                 handoff=str(out), source=source, timed_out=timed_out)
        if info.get('rescue_relay'):
            self.setf(name, rescue_relay='', rescue_hint='')
            self.r.rpush(ADVICE_NOTES, '[advisor] respawn ' + prev + ' → ' + label + ' done')

    def refresh_history(self):
        history.write_page(self.run_dir, self.history_slots(), self.history_links())
        pane = self.r.hget(SERVICE, 'history_pane')
        if pane and getattr(self.mgr, 'supports_browser', True):
            self.mgr.reload_browser(pane)

    def announce_history_page(self):
        """A backend with no browser surface (``supports_browser`` false, design §4.3) does not show
        ``<run>/history.html``: print its path in the control pane and open it once with the system opener
        when there is a desktop (macOS ``open`` / Linux ``xdg-open``; otherwise the path is all you get)."""
        if getattr(self.mgr, 'supports_browser', True):
            return
        page = history.page_path(self.run_dir)
        try:
            self.log('history.page', page=str(page))
        except Exception:
            pass  # logging must never break the layout
        print('[fleet] history page: ' + str(page) + ' (backend ' + str(self.cfg.get('backend') or 'cmux')
              + ' has no embedded browser)', flush=True)
        opener = shutil.which('open' if sys.platform == 'darwin' else 'xdg-open')
        if not opener:
            return
        try:
            subprocess.Popen([opener, str(page)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass  # no desktop session: the printed path is still there

    # -- hot roster (start --roster, or <run>/roster.yaml written by the advisor switch)
    def roster_tick(self):
        path = roster_file(self.cfg)
        if not self.cfg.get('roster') and not path.exists():
            return
        self.night_tick(path)
        if path != self.roster_followed:  # night on/off: follow the other file from now on
            self.roster_followed = path
            self.roster_stamp = self.roster_digest = None
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
        self.profile_swap_tick()

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
            workers, roles, profiles = roster.parse_entries(raw, Path(path).parent)
            table, per_worker = roster.handoff(raw)
            subfleets = roster.subfleets(raw, Path(path).parent)  # §29 (exempt from the used-name rule)
            if subfleets and subfleet.backend_problem(self.cfg):  # §33.4: none on WezTerm; keep the old roster
                raise ValueError(subfleet.backend_problem(self.cfg))
            # A slot name used earlier in this run still has Redis state and a workspace.
            used = [n + ': name was already used in this run (agent state or workspace exists); pick a new name'
                    for n in workers if n not in self.workers
                    and (self.r.exists(key('agent', n)) or (self.workspace() / n).exists())]
            if used:
                raise ValueError('\n'.join(used))
            # An added slot (or a newly declared / retargeted sub-fleet's workers) ends with -<number> (§3);
            # the run's own names, old style or not, are never re-checked here.
            problems = roster.row_errors(workers, keep=self.workers) + roster.subfleet_suffix_errors(
                subfleets, self.cfg.get('subfleets'))
            if problems:
                raise ValueError('\n'.join(problems))
            # Added rows without a profile run the fleet default: its model card caps them with the
            # live slots (C). Rows with a profile are counted by night.resolve_launches (B).
            pi_models, pi_thinkings = roster.pi_settings(raw)
            adds = [n for n in workers if n not in self.workers and not profiles.get(n)]
            if adds:
                problem = self.default_capacity_problem(adds, dict(self.cfg, pi_models=dict(
                    {k: v for k, v in (self.cfg.get('pi_models') or {}).items() if k not in workers}, **pi_models)))
                if problem:
                    raise ValueError(problem)
            night.resolve_launches(self.cfg, profiles, workers)  # unknown card, refused pair, card limits: reject all
            problem = roster.pi_cli_problem(pi_models, pi_thinkings, self.cfg.get('worker_cli'))
            unset = [n for n in adds if self.cfg.get('worker_cli') == 'pi' and not pi_models.get(n)
                     and not self.cfg.get('pi_model')]
            if problem or unset:
                raise ValueError(problem or 'no pi model for ' + ', '.join(unset) + ': give the row a pi_model '
                                 '(the run has no --pi-model fallback)')
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
        old_profiles = dict(self.cfg.get('worker_profiles') or {})
        results.update({n: 'profile ' + (profiles.get(n) or 'run default') + '; next generation once idle'
                        for n in workers if n in self.workers and profiles.get(n) != old_profiles.get(n)})
        self.cfg['worker_profiles'] = profiles
        # Own pi model / thinking per row (the supervisor's entry comes from start and stays).
        old_pi = (dict(self.cfg.get('pi_models') or {}), dict(self.cfg.get('pi_thinkings') or {}))
        keep = [w for w in self.workers if w not in workers] + ['supervisor']  # retiring slots keep theirs
        self.cfg['pi_models'] = dict({k: v for k, v in old_pi[0].items() if k in keep}, **pi_models)
        self.cfg['pi_thinkings'] = dict({k: v for k, v in old_pi[1].items() if k in keep}, **pi_thinkings)
        results.update({n: 'pi_model ' + (self.cfg['pi_models'].get(n) or 'run default') + ' thinking '
                        + (self.cfg['pi_thinkings'].get(n) or 'run default') + '; next generation once idle'
                        for n in workers if n in self.workers and n not in results
                        and (old_pi[0].get(n), old_pi[1].get(n)) != (pi_models.get(n), pi_thinkings.get(n))})
        pi_changed = old_pi != (self.cfg['pi_models'], self.cfg['pi_thinkings'])
        self.desired = workers
        # Retiring workers keep their role until gone (a handoff successor may still finish work).
        roles = dict({w: old_roles[w] for w in self.workers if w not in workers and w in old_roles}, **roles)
        self.cfg['worker_roles'] = roles
        if adds:
            self.workers = self.workers + adds
            self.names = self.slot_names()
        # §29: a hot-added supervisor_slot must be known before it spawns (tmux slot + no advisor hold read
        # cfg['subfleets']); written after the adds, it started as a plain held tab and its sub-fleet waited (10-14).
        self.tab_specs = roster.tabs(raw, Path(path).parent)  # opened once by tabs.open_all (§31)
        subs_changed = subfleets != (self.cfg.get('subfleets') or [])
        if subs_changed:
            self.cfg['subfleets'] = subfleets
        if adds or roles != old_roles or profiles != old_profiles or pi_changed or subs_changed:
            # Hooks and bridges check membership in the config file: write it before spawning.
            self.persist_roster()
        # Before the adds: a worker added together with `advisor: on` is spawned advised (held for its sheet).
        self.set_advisor(roster.advisor(raw), 'user')  # no-op when equal (e.g. the supervisor's own write)
        for name in adds:
            results[name] = self.add_worker(name)
        if adds or any(n not in workers for n in before) or any(n not in before for n in workers):
            self.r.hset(ROSTER, mapping={'notify': 1})
        if results or changed:
            self.log('roster.changed', before=before, after=self.desired, results=results, handoff=changed)
        for name in self.names:  # status shows the effective values without waiting for a delivery
            if self.agent(name):
                self.handoff_limits(name)
        if subfleets or named_subfleet.declared(self):
            named_subfleet.declare(self, subfleets)

    def default_capacity_problem(self, adds, cfg=None):
        """None if new default slots ``adds`` fit the default model's card next to the live slots
        (``cfg``: the config with the new roster's per-slot pi models; default self.cfg)."""
        cfg = cfg or self.cfg
        try:
            registry = AgentRegistry(self.task().get('agent_registry') or None)
        except (AgentRegistryError, OSError) as exc:
            return 'cannot read agent cards to check the default model: ' + str(exc)
        keys = {model_usage.default_model(pi_adapter.for_agent(cfg, n), registry) for n in adds} - {None}
        if not keys:
            return None
        usage = self.model_usage(registry)
        held = {slot for key in keys for slot in usage.holders(key)}
        return model_usage.default_problem(cfg, sorted(held | set(adds)), registry)

    def persist_roster(self):
        self.cfg['workers'][:] = self.workers  # in place: the control view holds this list
        fields = {'workers': list(self.workers), 'worker_roles': self.cfg.get('worker_roles') or {},
                  'worker_profiles': self.cfg.get('worker_profiles') or {},
                  'pi_models': self.cfg.get('pi_models') or {}, 'pi_thinkings': self.cfg.get('pi_thinkings') or {},
                  'subfleets': self.cfg.get('subfleets') or []}
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
            self.apply_roster_launch(name)
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
        self.names = self.slot_names()
        (self.cfg.get('worker_roles') or {}).pop(name, None)
        self.persist_roster()  # its workspace stays: declared artifact paths must still verify
        self.refresh_history()
        self.log('roster.retired', agent=name, label=label, workers=self.workers)

    # -- roster profiles / night switch (docs/design.md §25)
    def apply_roster_launch(self, name):
        """A slot that the roster gives a profile starts (or is added) running that card."""
        profile = (self.cfg.get('worker_profiles') or {}).get(name)
        self.setf(name, roster_pi=self.roster_pi(name))
        if profile:
            launch = night.resolve_launches(self.cfg, {name: profile})[name]
            self.setf(name, runtime_launch=profile_launch.encode_launch(launch), roster_profile=profile)

    def roster_pi(self, name):
        """This slot's own pi model / thinking as the roster wants it (compared to roster_pi in Redis)."""
        return json.dumps([(self.cfg.get('pi_models') or {}).get(name) or '',
                           (self.cfg.get('pi_thinkings') or {}).get(name) or ''])

    def roster_card_gate(self, name, profile):
        """Feature C's per-slot card gate (Fleet.card_gate) before a roster swap onto ``profile``:
        None, ('wait', why) while the model is at max_concurrent elsewhere, ('block', why). Without C: None."""
        if not hasattr(self, 'card_gate'):
            return None
        registry = AgentRegistry(self.cfg.get('agent_registry') or None)
        return self.card_gate(name, registry.get(profile), registry)

    def profile_swaps(self):
        """Active slots whose roster profile differs from the one their generation runs."""
        wanted = self.cfg.get('worker_profiles') or {}
        # roster_pi missing = a slot started before per-agent pi models: never swapped for that alone
        return [n for n in self.active_workers() if (self.agent(n).get('roster_profile') or '') != (wanted.get(n) or '')
                or self.agent(n).get('roster_pi') not in (None, self.roster_pi(n))]

    def night_tick(self, path):
        """Record a night on/off transition (audit + brake card placeholder for the budget code)."""
        on = bool(self.cfg.get('roster_night')) and night.read_state(self.cfg['run'])['on']
        prev = self.r.hget(night.NIGHT, 'on')
        if prev == str(int(on)):
            return
        if prev is not None or on:
            self.log('night.' + ('on' if on else 'off'), roster=str(path), brake_card='night' if on else 'day',
                     supervisor='unchanged')
        self.r.hset(night.NIGHT, mapping={'on': int(on), 'brake_card': 'night' if on else 'day', 'since': time.time()})

    def profile_swap_tick(self):
        pending = self.profile_swaps()
        for name in self.active_workers():
            if name not in pending and (self.agent(name).get('roster_swap_deferred') == '1'
                                        or self.agent(name).get('roster_capacity_wait')):
                # roster changed back: a later deferral / capacity wait is audited again
                self.setf(name, roster_swap_deferred=0, roster_capacity_wait='')
        live = False
        if pending and not self.service:
            try:
                live = profile_runtime.task_live_setting(self.task(), self.cfg['allow_profile_live_switch'])
            except profile_runtime.ProfileRuntimeError:
                live = True
        for name in pending:
            info = self.agent(name)
            if live:  # a live task assignment owns this slot's runtime until the task ends
                if info.get('roster_swap_deferred') != '1':
                    self.setf(name, roster_swap_deferred=1)
                    self.log('roster.profile_deferred', agent=name, reason='task apply_profiles_live owns the slot runtime')
                continue
            if (info.get('state') != 'idle' or info.get('idle_confirmed') == '0'
                    or (self.v2 and self.r.hget(self.v2.bus.lease_key(name), 'current_delivery'))):
                continue  # in-flight turns finish first
            if self.swap_profile(name) is False:
                return  # blocked: the task is finished as blocked, leave the other slots alone
        if self.r.hget(night.NIGHT, 'brief_due') == '1' and not self.profile_swaps() and not any(
                self.agent(w).get('state') == 'starting' for w in self.active_workers()):
            self.r.hset(night.NIGHT, mapping={'brief_due': 0})
            self.breakpoint_brief()

    def swap_profile(self, name):
        """Same slot, next generation, the roster's profile; like a handoff, the queue stays on the slot."""
        profile = (self.cfg.get('worker_profiles') or {}).get(name) or ''
        info = self.agent(name)
        try:
            launch = night.resolve_launches(self.cfg, {name: profile})[name] if profile else None
            gate = self.roster_card_gate(name, profile) if launch else None
            if gate and gate[0] == 'wait':
                if info.get('roster_capacity_wait') != gate[1]:  # audit each new reason once
                    self.setf(name, roster_capacity_wait=gate[1])
                    self.log('roster.capacity_wait', agent=name, profile=profile, reason=gate[1])
                return None  # queue stays held on this slot; retried next tick (no old-card work meanwhile)
            if gate:
                raise ValueError(gate[1])  # host mismatch never clears: block, never a silent swap
            if launch:
                self.preflight_profile(launch, cli_change_ok=True)
        except (OSError, ValueError, KeyError, SystemExit, subprocess.SubprocessError) as exc:
            return self.block_profile(name, 'roster profile ' + (profile or 'run default') + ': ' + str(exc)) or False
        generation, old, prev = int(info['generation']), info.get('pane'), self.label(name)
        if not old or self.mgr.get_pane_status(old) is None:
            return self.block_profile(name, 'roster profile switch: the old runtime tab is missing', dead=True) or False
        handoff_id = uuid.uuid4().hex[:8]
        note = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-' + handoff_id + '-profile.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(self.skeleton(name, dict(info, handoff_id=handoff_id)))
        snapshot = note.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff=str(note),
                       snapshot=str(snapshot), source='roster-profile', timed_out=False)
        label = name + '.' + str(generation + 1)
        # Fence old callbacks before the successor exists (same order as an idle profile switch).
        self.setf(name, generation=generation + 1, label=label, prev_label=prev, handoff_id='',
                  runtime_launch=profile_launch.encode_launch(launch) if launch else '', runtime_profile='',
                  roster_profile=profile, roster_pi=self.roster_pi(name), roster_swap_deferred=0, roster_capacity_wait='', launch_ready=0,
                  retired_pane=old)
        self.r.hset(NAMES, mapping={label: name})
        self.r.hset(night.NIGHT, mapping={'brief_due': 1})
        try:
            new = self.spawn(name, '[Same-task takeover] You are ' + label + ' (taking over from ' + prev
                             + ' on profile ' + (profile or 'run default') + '). Read the handoff note ' + str(note)
                             + ' and continue from it. In this turn only read the note, do no task work, and make '
                             'your final reply exactly READY.', launch_held=True)
            if not self.mgr.tab_into(new, old):
                raise RuntimeError('could not place the successor in the worker pane')
            if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                raise RuntimeError('could not confirm retirement of the old runtime ' + prev)
        except (Exception, SystemExit) as exc:
            self.setf(name, launch_ready=0, state='dead')
            return self.block_profile(name, 'roster profile switch failed: ' + str(exc), dead=True) or False
        if self.v2:
            self.v2.confirm_retired(name)
        self.setf(name, launch_ready=1, retired_pane='')
        self.refresh_history()
        self.log('roster.profile_switched', agent=name, label=label, prev=prev, generation=generation + 1,
                 profile=profile or 'run default', cli=agent_cli(self.runtime_cfg(name), name), handoff=str(note),
                 **({'pi_model': self.runtime_cfg(name).get('pi_model') or ''} if is_pi(self.runtime_cfg(name), name) else {}))
        return True

    def breakpoint_brief(self):
        """After a profile/night swap: tell the supervisor where the task stands (resume's summary)."""
        t = self.task()
        if self.service or t.get('status') != 'running':
            return self.log('night.brief_skipped', reason='no running task')
        wanted = self.cfg.get('worker_profiles') or {}
        slots, kept = [], []
        for w in self.active_workers():
            slots.append('- ' + self.label(w) + ': ' + (wanted.get(w) or 'run default') + ' ('
                         + agent_cli(self.runtime_cfg(w), w) + ')')
            pending = self.v2.pending_text(w) if self.v2 else self.r.lrange(key('inbox', w), 0, 0)
            if pending:
                kept.append('- ' + self.label(w) + ' (' + str(self.inbox_depth(w)) + ' queued): '
                            + ' '.join(pending[0].split())[:200])
        night_on = self.r.hget(night.NIGHT, 'on') == '1'
        self.queue('supervisor', '[' + ('Night' if night_on else 'Day') + ' switch ' + t['id'] + '] Workers moved to '
                   'their ' + ('night' if night_on else 'day') + ' profiles at an idle boundary: same names, new '
                   'generations that only read a handoff note. Work they finished stays finished.'
                   + '\nGoal: ' + t.get('goal', '')
                   + '\nWorkers now:\n' + '\n'.join(slots)
                   + '\nWorkers you can dispatch to: ' + ', '.join(self.labels())
                   + '\nShared output directory: ' + str(self.workspace())
                   + (self.v2.progress(t) if self.v2 else '')
                   + '\nQueued instructions kept on their slot (the new generation gets them; do NOT dispatch them again):\n'
                   + ('\n'.join(kept) or '- none')
                   + '\nDispatch only work that is still missing. Reply with the JSON format from AGENTS.md.')
        self.log('night.brief', workers=self.labels(), kept=len(kept))

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
        screen = self.capture_screen(name, info) if self.running() else ''
        if self.v2:
            def stalled():
                self.v2.hold(name, 'no activity for ' + str(idle_s) + ' s; stop unconfirmed', public_status='stalled')
                return self.on_wall(name, screen, 'screen')
            record = self.v2.transaction('stall-' + uuid.uuid4().hex, stalled)
            if record:
                self.brake_notify(record)
            return None
        record = self.on_wall(name, screen, 'screen')
        if record:
            self.brake_notify(record)
        stalls = int(info.get('stalls', 0)) + 1
        self.log('agent.stalled', agent=name, state=info.get('state'), idle_s=idle_s, stalls=stalls)
        self.mgr.send_key(info['pane'], 'Escape')  # abort the turn; an aborted turn fires no notify
        self.setf(name, state='idle', idle_confirmed=0, stalls=stalls, busy_since=time.time())
        queued = False  # a Codex item that never started is still in the TUI's queue (no official withdraw)
        if codex_queue.unsettled(info):
            rec = codex_queue.record(info)
            if codex_queue.token_turn(rec['cq_thread'], rec['cq_token'], self.sessions):
                codex_queue.clear(self, name, 'stalled turn aborted')  # it left the queue: a retry is not a duplicate
            else:
                queued = True
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
            elif queued:
                # Its queued item has not started and still runs once the TUI is free: wait for it (its own
                # turn settles it) instead of queueing the same message a second time.
                self.setf(name, state='busy')
                self.log('queue.kept', agent=name, delivery=codex_queue.record(info)['cq_delivery'])
            else:
                self.r.lpush(key('inbox', name), info['last_msg'])  # one retry of the same message
        else:
            self.report(name, 'STALLED: no activity for ' + minutes + ' min, its turn was aborted. The worker is '
                        'idle again. Dispatch a smaller, concrete instruction, or reply BLOCKED if a human is needed.')

    # -- brake (cmux_fleet/brake.py): budget card per pool + wall table
    def brake_budget(self):
        return json.loads(self.r.hget(BRAKE, 'budget') or 'null')

    def brake_start(self, budget):
        """A new card: counters restart (task submit; service: first tick)."""
        if self.paused() and (self.r.hget(SERVICE, 'reason') or '').startswith('brake: '):
            self.resume()  # a brake pause belongs to the card it tripped, not to the next task
        base = brake.pool_reading(brake.read_quota(self.cfg.get('quota_file')), (budget or {}).get('pool'))
        self.r.delete(BRAKE)
        self.r.hset(BRAKE, mapping={'budget': json.dumps(budget), 'calls': 0, 'rounds': 0, 'started': time.time(),
                                    'usd_base': '' if base is None else base, 'tripped': ''})

    def brake_count(self, counter):
        self.r.hincrby(BRAKE, counter, 1)

    def brake_tick(self, now):
        """Pause at any budget line. Returns the trip record (notify after the transaction) or None."""
        if self.service and self.cfg.get('budget') and not self.r.hget(BRAKE, 'budget'):
            self.brake_start(brake.validate_budget(self.cfg['budget']))
        state = self.r.hgetall(BRAKE)
        budget = json.loads(state.get('budget') or 'null')
        if not budget or not self.running() or self.paused():
            return None
        quota = brake.read_quota(self.cfg.get('quota_file'))
        if brake.pool_meter(budget.get('pool')) == 'usd_equiv' and state.get('usd_base') in (None, ''):
            base = brake.pool_reading(quota, budget['pool'], now)
            if base is not None:  # the first known reading is the baseline: spend before it is not ours
                self.r.hset(BRAKE, mapping={'usd_base': base})
                state['usd_base'] = base
        found = brake.trips(self.brake_card(budget), brake.counters(state, quota, now))
        if found:
            return self.brake_trip('budget', 'budget line reached: ' + brake.describe(found), lines=found)
        return None

    def brake_card(self, budget):
        """The card as evaluated (default usd_equiv cash cap, day or night per B's fx:night)."""
        return brake.effective(budget, self.r.hget('fx:night', 'brake_card') == 'night')

    def brake_trip(self, kind, reason, **detail):
        record = dict(kind=kind, reason=reason, at=time.time(), task=self.task().get('id', ''), **detail)
        self.r.hset(BRAKE, mapping={'tripped': json.dumps(record, ensure_ascii=False)})
        if not self.paused():
            self.pause('brake: ' + reason)
            # The pause this brake owns (the first trip's reason and agent): later trips overwrite `tripped`.
            self.r.hset(BRAKE, mapping={'paused_by': json.dumps({'reason': 'brake: ' + reason, 'kind': kind,
                                                                 'agent': detail.get('agent', '')})})
        self.log('brake.tripped', **{('brake' if k == 'kind' else k): v for k, v in record.items() if k != 'at'})
        return record

    def brake_notify(self, record):
        """Side effects of a trip, outside any v2 transaction: <run>/brake.json and the operator's notify argv."""
        state = self.r.hgetall(BRAKE)
        card = json.loads(state.get('budget') or 'null')
        save(self.run_dir / 'brake.json', dict(record, budget=card, effective_budget=self.brake_card(card),
             counters=brake.counters(state, brake.read_quota(self.cfg.get('quota_file')))))
        argv = self.cfg.get('brake_notify')
        if argv:
            try:
                subprocess.Popen(list(argv) + ['[cmux-fleet brake] ' + record['reason']], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as exc:
                self.log('brake.notify_failed', error=str(exc))

    def brake_resume(self, raises):
        """Operator resume: refused while a budget line is still reached (resume --raise LIMIT=VALUE)."""
        if raises is not None and (not isinstance(raises, list) or not all(isinstance(x, str) for x in raises)):
            raise ValueError('raise must be a list of LIMIT=VALUE strings')
        state = self.r.hgetall(BRAKE)
        budget = json.loads(state.get('budget') or 'null')
        tripped = json.loads(state.get('tripped') or 'null')
        if raises:
            if not budget:
                raise ValueError('--raise needs a budget card on the current task / service')
            budget = brake.parse_raise(raises, budget)
            if budget.get('pool') != json.loads(state['budget']).get('pool'):
                raise ValueError('--raise cannot change the pool')
        if budget:  # any resume, whatever paused the run (a later wall trip or a hold can overwrite the record)
            found = brake.trips(self.brake_card(budget), brake.counters(state, brake.read_quota(self.cfg.get('quota_file'))))
            if found:
                raise ValueError('budget still over the line (' + brake.describe(found)
                                 + '); resume with --raise LIMIT=<higher value>')
        if raises:
            self.r.hset(BRAKE, mapping={'budget': json.dumps(budget)})
            self.log('brake.raised', budget=budget)
        if tripped:
            self.r.hset(BRAKE, mapping={'tripped': ''})
        self.r.hset(BRAKE, mapping={'paused_by': ''})

    def on_wall(self, name, text, source):
        """A wall on a turn text or a stalled screen: decide (downgrade / stop), record, pause."""
        wall = brake.classify_wall(text)
        if not wall:
            return None
        cfg = self.runtime_cfg(name)
        cli = agent_cli(cfg, name)
        model = (cfg.get('pi_model') if cli == 'pi' else cfg.get('codex_model') if cli == 'codex'
                 else cfg.get('opencode_model') if cli == 'opencode'
                 else cfg.get('supervisor_model') if name == 'supervisor' else cfg.get('worker_model')) or ''
        action = brake.wall_action(wall, cli, model)
        if action['action'] == 'downgrade' and name not in self.workers:
            # Master design v2 binding table: a supervisor wall is not answered by swapping its model.
            action = {'action': 'stop', 'cli': cli, 'from': model,
                      'advice': 'supervisor slot: no automatic downgrade; resume after the reset or restart it'}
        self.setf(name, wall=json.dumps(dict(wall, **action), ensure_ascii=False))
        if action['action'] == 'stop' and name in self.workers:
            self.setf(name, wall_downgrade='', wall_switch='')  # a stop wins over an earlier pending downgrade
        if action['action'] == 'downgrade':
            pending = json.loads(self.agent(name).get('wall_downgrade') or 'null')
            if not pending or pending.get('to') != action['to']:
                self.setf(name, wall_downgrade=json.dumps(dict(action, key=brake.MODEL_KEY[cli], source=source,
                                                               at=uuid.uuid4().hex[:12]), ensure_ascii=False))
        what = ('same CLI one tier down: ' + action['from'] + ' -> ' + action['to'] if action['action'] == 'downgrade'
                else action['advice'])
        return self.brake_trip('wall', self.label(name) + ' hit a ' + wall['scope'] + ' wall ("' + wall['line'][:120]
                               + '"); ' + what, agent=name, source=source, wall=wall, decision=action)

    def notify_later(self, record):
        """A trip recorded inside a v2 event transaction: brake.json / notify argv run on the next tick."""
        if record:
            self.pending_notify.append(record)

    def startup_wall(self, name, text):
        """A new generation answered its startup prompt with a wall instead of READY: walled (not dead),
        fleet paused resumable. A model-pool wall is downgraded by wall_tick; any other wall is retried
        (same launch) once the operator resumes."""
        if len((text or '').strip()) > brake.TURN_WALL_MAX:
            return None
        record = self.on_wall(name, text, 'startup')
        if record:
            self.setf(name, state='walled', runtime_ready=0, bridge_available=0)
        return record

    def wall_tick(self):
        """Wall side effects (spawn / kill), outside any v2 transaction. At an idle boundary run the pending
        same-CLI downgrade as the slot's next generation; once that generation is READY lift the wall's own
        pause; retry a startup-walled slot after the operator resumed."""
        for name in self.active_workers():
            info = self.agent(name)
            req = json.loads(info.get('wall_downgrade') or 'null')
            if req and info.get('wall_switch') == req['at']:
                if info.get('state') == 'idle' and info.get('runtime_ready') == '1':
                    self.wall_switched(name, req)
                continue
            in_flight = self.v2 and self.r.hget(self.v2.bus.lease_key(name), 'current_delivery')
            current = self.runtime_cfg(name)
            if req and (agent_cli(current, name) != req['cli'] or (current.get(req['key']) or '') != (req['from'] or '')):
                # The slot already runs something else (B's roster / night swap, a live profile): never undo it.
                self.setf(name, wall_downgrade='', wall_switch='')
                self.log('brake.downgrade_superseded', agent=name, wanted=req, cli=agent_cli(current, name),
                         model=current.get(req['key'], ''))
                req = None
            if req and info.get('state') in ('idle', 'walled') and not in_flight:
                launch = json.loads(info.get('runtime_launch') or '{}')
                launch = {'profile_id': launch.get('profile_id') or 'wall-downgrade', 'role_text': launch.get('role_text', ''),
                          'overrides': dict(launch.get('overrides', {}), worker_cli=req['cli'], **{req['key']: req['to']})}
                self.setf(name, wall_switch=req['at'])
                self.wall_rotate(name, launch, 'same-CLI downgrade after a model-pool wall: ' + str(req['from'])
                                 + ' -> ' + req['to'])
            elif info.get('state') == 'walled' and not self.paused():
                self.wall_rotate(name, json.loads(info.get('runtime_launch') or 'null'), 'retry after the wall pause was resumed')

    def wall_switched(self, name, req):
        self.setf(name, wall_downgrade='', wall_switch='')
        owner = json.loads(self.r.hget(BRAKE, 'paused_by') or 'null') or {}
        ours = (owner.get('kind') == 'wall' and owner.get('agent') == name
                and self.r.hget(SERVICE, 'reason') == owner.get('reason'))
        budget = self.brake_budget()
        over = budget and brake.trips(self.brake_card(budget), brake.counters(
            self.r.hgetall(BRAKE), brake.read_quota(self.cfg.get('quota_file'))))
        self.log('brake.downgraded', agent=name, label=self.label(name), cli=req['cli'], to=req['to'],
                 resumed=bool(self.paused() and ours and not over))
        if self.paused() and ours and not over:
            # Only the wall's own pause: an operator pause, an F hold or a budget line keeps the fleet paused.
            self.r.hset(BRAKE, mapping={'tripped': '', 'paused_by': ''})
            self.resume()

    def wall_rotate(self, name, launch, why):
        """Same slot, next generation (optionally another launch), queue kept on the slot. Like B's
        roster profile swap: skeleton handoff, saved screen, held launch, tab into place, old tab killed,
        v2 delivery confirmed retired. Failure blocks the task like any profile switch failure."""
        info = self.agent(name)
        generation, old, prev = int(info['generation']), info.get('pane'), self.label(name)
        if not old or self.mgr.get_pane_status(old) is None:
            return self.block_profile(name, why + ': the old runtime tab is missing', dead=True) or False
        handoff_id = uuid.uuid4().hex[:8]
        note = self.run_dir / 'handoffs' / (name + '-g' + str(generation) + '-' + handoff_id + '-wall.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(self.skeleton(name, dict(info, handoff_id=handoff_id)))
        snapshot = note.with_suffix('.terminal.txt')
        self.mgr.capture_to(old, str(snapshot))
        history.record(self.run_dir, slot=name, label=prev, generation=generation, handoff=str(note),
                       snapshot=str(snapshot), source='wall', timed_out=False)
        label = name + '.' + str(generation + 1)
        self.setf(name, generation=generation + 1, label=label, prev_label=prev, handoff_id='',
                  runtime_launch=profile_launch.encode_launch(launch) if launch else '', runtime_profile='',
                  launch_ready=0, runtime_ready=0, retired_pane=old)
        self.r.hset(NAMES, mapping={label: name})
        try:
            new = self.spawn(name, '[Same-task takeover] You are ' + label + ' (taking over from ' + prev + ': ' + why
                             + '). Read the handoff note ' + str(note) + ' and continue from it. In this turn only '
                             'read the note, do no task work, and make your final reply exactly READY.', launch_held=True)
            if not self.mgr.tab_into(new, old):
                raise RuntimeError('could not place the successor in the worker pane')
            if not self.mgr.kill_pane(old) or self.mgr.get_pane_status(old) is not None:
                raise RuntimeError('could not confirm retirement of the old runtime ' + prev)
        except (Exception, SystemExit) as exc:
            self.setf(name, launch_ready=0, state='dead')
            return self.block_profile(name, why + ' failed: ' + str(exc), dead=True) or False
        if self.v2:
            self.v2.confirm_retired(name)
        self.setf(name, launch_ready=1, retired_pane='')
        self.refresh_history()
        self.log('brake.wall_rotated', agent=name, label=label, prev=prev, generation=generation + 1, why=why,
                 cli=agent_cli(self.runtime_cfg(name), name), handoff=str(note))
        return True

    def wall_gate(self, name, screen, source='watch'):
        """E before F: the watchdog calls this on every screen it captures, BEFORE the prompt whitelist,
        the unknown-dialog hold and the stall ladder. A wall on the bottom of the screen trips the brake
        (pause, record, notify) and returns the trip record: the caller then presses no key, holds nothing
        and leaves the slot alone this tick (a wall menu is never a dialog to answer). None: no wall."""
        if not brake.screen_wall(screen):
            return None
        if name == ADVISOR:
            # §27: the advisor's wall never pauses the fleet. Its consultation falls back to the default
            # (the held message) and advisor_tick restarts the tab in place.
            if self.agent(name).get('state') != 'held':
                self.v2.transaction('advisor-wall-' + uuid.uuid4().hex,
                                    lambda: self.v2.hold(name, 'usage wall on the advisor tab'))
            return {'kind': 'wall', 'agent': name, 'advisor': True}
        tripped = json.loads(self.r.hget(BRAKE, 'tripped') or 'null')
        if self.paused() and tripped and tripped.get('kind') == 'wall' and tripped.get('agent') == name:
            return tripped  # already braked on this agent's wall: hands off, no second notify
        tail = '\n'.join((screen or '').rstrip().splitlines()[-brake.GATE_TAIL:])
        if self.v2:
            record = self.v2.transaction('brake-wall-' + uuid.uuid4().hex, lambda: self.on_wall(name, tail, source))
        else:
            record = self.on_wall(name, tail, source)
        if record:
            self.brake_notify(record)
        return record

    def stalled_screens(self, now):
        """Screens of busy agents past stall_timeout (only while a task / service is running)."""
        limit = float(self.cfg['stall_timeout'])
        if limit <= 0 or not self.running() or self.paused():
            return {}
        return {name: self.capture_screen(name, info) for name, info in ((n, self.agent(n)) for n in self.names if n != ADVISOR)
                if info.get('state') == 'busy' and now - self.last_activity(info) >= limit}

    def capture_screen(self, name, info):
        """The tail of an agent's screen for wall detection ('' if it cannot be read)."""
        pane = info.get('pane')
        if not pane:
            return ''
        path = self.run_dir / 'brake' / (name + '-screen.txt')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.mgr.capture_to(pane, str(path))
            return '\n'.join(path.read_text(errors='replace').splitlines()[-60:])
        except (OSError, AttributeError, RuntimeError, subprocess.SubprocessError):
            return ''

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
                          ('advisor', self.advisor_tick),  # resident advisor tab: start / retire / restart (§27)
                          ('lifecycle', lambda: lifecycle_close.tick(self)),  # mode policy (done gate, hold, timeouts, idle); before result.json
                          ('result', self.write_result), ('contracts', self.sync_contract_files),
                          ('subfleet', lambda: subfleet.tick(self)),  # side effects, outside any v2 transaction
                          ('tabs', lambda: tabs.open_all(self)),  # companion tabs (§31): once per daemon start
                          ('escalations', self.escalation_tick), ('notices', self.relay_notices),
                          ('vetoes', self.apply_vetoes)):
            try:
                run()
            except BusError:
                raise
            except Exception as exc:
                self.log('tick.error', part=part, error=repr(exc))
        try:  # a parent/child page link was clicked: switch to that fleet's workspace (never fatal)
            subfleet.follow_jump(self)
        except Exception as exc:
            self.log('history.jump_error', error=str(exc))
        if self.v2:
            # v2 holds a stalled slot inside RuntimeV2.tick (Fleet.on_stall is v1 only): read the
            # screens of slots about to be held first, so a wall pauses the fleet instead.
            screens = self.stalled_screens(time.time())
            record = self.v2.transaction('brake-' + uuid.uuid4().hex, lambda: next(
                (r for r in [self.on_wall(n, text, 'screen') for n, text in screens.items()] if r), None)
                or self.brake_tick(time.time()))
        else:
            record = self.brake_tick(time.time())
        for done in [r for r in self.pending_notify + [record] if r]:
            self.brake_notify(done)
        self.pending_notify = []
        self.wall_tick()  # spawn / kill: outside any v2 transaction
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
        if not self.mgr.get_or_create_session() and run_backend(self.cfg) != 'cmux':
            # No session, no fleet: stop with the backend's reason (Redis and the run stay for --resume).
            # cmux is left as it was: its session is a `cmux ping`, and a daemon running inside a cmux tab
            # has always carried on past a missed ping (workspaces are created on the first spawn).
            why = getattr(self.mgr, 'last_error', '') or 'no reason given'
            self.log('backend.session_failed', backend=run_backend(self.cfg), reason=why)
            raise SystemExit('backend ' + run_backend(self.cfg) + ' could not create its session: ' + why)
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
    extended = extended_checks(task)
    result = {
        'status': status or task.get('status', ''), 'reason': task.get('reason', '') if reason is None else reason,
        'task_id': task.get('id', ''), 'goal': task.get('goal', ''), 'at': time.time(),
        'artifacts': [dict(a, problems=check_artifacts([a], workspace, extended)) for a in artifacts]}
    if contract_summary:
        result['contracts'] = contract_summary
    try:
        cfg = load(Path(run) / 'config.json')
    except (OSError, ValueError):
        cfg = {}
    result.update(mode=cfg.get('lifecycle'), base_mode=cfg.get('mode', 'task'))  # mode = lifecycle (None: legacy run)
    save(Path(run) / 'result.json', result)


def start_redis(cfg, tries=30):
    """Start the run's private Redis on its pinned socket and directory. v2 has AOF on, so a
    restart with the same --dir reloads everything written up to the last fsync (about 1 s)."""
    redis_dir = Path(cfg['redis_runtime_dir'])
    if cfg.get('run'):
        save(redis_dir / REDIS_OWNER, {'run': str(cfg['run'])})  # lets reap_orphan_redis find the run later
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


REDIS_OWNER = 'owner.json'
ORPHAN_REDIS_GRACE = 120  # seconds between start_redis and the daemon taking its lock


def _own_processes():
    """(pid, command line) of this user's processes; [] when ps fails."""
    try:
        out = subprocess.run(['ps', '-xww', '-o', 'pid=,command='], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(' ')
        if pid.isdigit():
            rows.append((int(pid), command))
    return rows


def _run_alive(run, procs):
    """A daemon holds the run's lock, or any live process (actor, hook, child fleet) names the run."""
    if (Path(run) / 'daemon.lock').exists() and daemon_running(run):
        return True
    return any(str(run) in command for pid, command in procs if 'redis-server' not in command)


def reap_orphan_redis(keep=None, grace=ORPHAN_REDIS_GRACE, root=None):
    """Shut down private Redis instances whose run was abandoned without `stop` (v2 keeps Redis
    up when a daemon dies, for --resume). Lossless: SHUTDOWN saves, and --resume restarts Redis
    from the same directory. Only instances with an owner record are touched; a sub-fleet's Redis
    is left to its parent while any ancestor run is alive. `root` limits the sweep to one runtime
    root (tests). Returns the sockets shut down."""
    procs = _own_processes()
    reaped = []
    for pid, command in procs:
        if 'redis-server' not in command or 'unixsocket:' not in command:
            continue
        sock = Path(command.split('unixsocket:', 1)[1].split()[0])
        if root is not None and Path(root).resolve() not in sock.parents:
            continue
        owner_file = sock.parent / REDIS_OWNER
        try:
            run = Path(load(owner_file)['run'])
            if time.time() - owner_file.stat().st_mtime < grace:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue  # started before owner records, or not a fleet instance: leave it
        if keep is not None and Path(keep) == run:
            continue
        chain = [run] + [a.parent for a in run.parents if a.name == 'sub']  # the run, then each ancestor run
        if any(_run_alive(r, procs) for r in chain):
            continue
        try:
            import redis
            redis.Redis(unix_socket_path=str(sock), socket_connect_timeout=3, socket_timeout=10).shutdown()
        except Exception as exc:
            if sock.exists():
                print('orphan Redis not shut down:', sock, str(exc)[:200])
                continue
        print('shut down orphan Redis of', run)
        reaped.append(str(sock))
    return reaped


def sweep_orphan_redis(keep=None):
    """reap_orphan_redis for start / start --resume: housekeeping never blocks a start."""
    try:
        reap_orphan_redis(keep=keep)
    except Exception as exc:
        print('orphan Redis sweep skipped:', str(exc)[:200])


def daemon_running(run):
    """True while a daemon holds the run's exclusive lock."""
    try:
        with (Path(run) / 'daemon.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    return False


def reclaim_minutes(value):
    """start --worker-idle-reclaim-min: 'off' or a positive number of minutes."""
    if value == 'off':
        return value
    try:
        minutes = float(value)
    except ValueError:
        minutes = 0
    if not minutes > 0:
        raise argparse.ArgumentTypeError('expected minutes > 0 or off')
    return minutes


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
    if (run / 'result.json').exists() and not resumable(run):
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


def resumable(run):
    """True when the run was stopped by a lifecycle hold timeout (stop_keep_resumable)."""
    try:
        return load(Path(run) / 'result.json').get('resumable') is True
    except (OSError, ValueError):
        return False


def resume(run, args):
    """start --resume: reattach an unfinished v2 run. False when it turned out finished (auto starts anew)."""
    path = run / 'config.json'
    cfg = load(path)
    from cmux_fleet import cmux_manager
    pin_paths(cfg)  # a resumed run keeps its own locations; an older config gets the current ones pinned
    cfg.setdefault('fleet_home', str(HOME.expanduser().resolve()))
    cfg.setdefault('cmux_bin', cmux_manager.CMUX)
    cfg.setdefault('cmux_state_dir', str(cfg.get('cmux_state_dir') or cmux_manager.STATE_DIR))
    if 'codex_queue' not in cfg:  # a run started before the probe: decided once here, kept for the rest of the run
        cfg['codex_queue'], cfg['codex_version'], _ = codex_queue.capability(cfg.get('codex'))
    try:
        runtime_v2.validate_config(cfg)
    except ValueError as exc:
        raise SystemExit('cannot resume ' + str(run) + ': ' + str(exc)) from exc
    # A resumed run follows its own backend (§5); an older run without one keeps cmux. A run whose
    # daemon is still alive is returned below before anything is spawned, so its backend is not probed.
    live_run = daemon_running(run)
    if live_run:  # an idempotent --resume: nothing to do, and nothing to probe
        print('this run still has a live daemon; nothing to resume:', run)
        return True
    # A config without `backend` (a run started before the choice existed) is a cmux run: never re-decided
    # here, so a resume where cmux does not answer is refused as before instead of moving the run to WezTerm.
    backend, why = resolve_backend(cfg.get('backend') or 'cmux')
    if why:  # refused before anything is provisioned: Redis stays as it is
        raise SystemExit('cannot resume ' + str(run) + ': backend ' + str(backend)
                         + ' is not usable here: ' + why)
    cfg['backend'] = backend
    if args.max_attempts is not None:
        cfg['max_attempts'] = args.max_attempts
    if args.roster:  # applied by the normal hot-reload on the daemon's first tick
        roster_path = Path(args.roster).expanduser().resolve()
        try:
            workers = roster.load(roster_path)[0]
            # Only added slots and newly declared sub-fleets need -<number> (§3): stored names are kept
            problems = roster.row_errors(workers, keep=cfg.get('workers') or ()) + roster.subfleet_suffix_errors(
                roster.load_subfleets(roster_path), cfg.get('subfleets'))
            if problems:
                raise ValueError('\n'.join(problems))
        except (OSError, ValueError) as exc:
            raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + str(exc)) from exc
        cfg['roster'] = str(roster_path)
        cfg['subfleets'] = roster.load_subfleets(roster_path)  # respawned supervisor_slots start in tmux
    if getattr(args, 'advisor', False):  # sugar: writes `advisor: on` into this run's roster (§2.1)
        if cfg.get('worker_kit'):
            try:
                kitmod.Kit(cfg['worker_kit'])  # refuse a bad kit before provisioning, like a bad roster
            except (OSError, ValueError) as exc:
                raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
        try:
            for roster_file_path in advisor_rosters(cfg):  # not `path`: that is config.json, saved below
                roster.write_advisor(roster_file_path, True, cfg['workers'])
        except (OSError, ValueError) as exc:
            raise SystemExit('--advisor: roster not written: ' + str(exc)) from exc
    if not live_run and not backends_check(backend):  # a live daemon keeps its own backend running
        raise SystemExit(unavailable_text(backend))
    if cfg.get('subfleets') and subfleet.backend_problem(cfg):
        raise SystemExit('cannot resume ' + str(run) + ': ' + subfleet.backend_problem(cfg))
    sweep_orphan_redis(keep=run)
    try:
        r = connect(cfg)  # only the daemon died: its Redis is still up
    except Exception:
        if not Path(cfg['redis_runtime_dir']).is_dir():
            raise SystemExit('cannot resume ' + str(run) + ': its Redis runtime directory is gone')
        r = start_redis(cfg, tries=300)  # reloads the AOF from the same directory
    task = {} if cfg.get('mode') == 'service' else r.hgetall(TASK)
    if task.get('status', 'running') != 'running' and resumable(run):
        try:  # the lifecycle hold stopped it on purpose: the operator's --resume reopens the task
            lifecycle_hold.reopen(r, task, getattr(args, 'extend_hours', None))
        except ValueError as exc:
            if args.resume != 'auto':
                raise SystemExit('cannot resume ' + str(run) + ': ' + str(exc)) from exc
            try:  # auto: like a finished run, start a new one (Redis down again, AOF directory kept)
                r.shutdown()
            except Exception:
                pass  # redis closes the socket while acknowledging shutdown
            print('--resume auto: ' + str(run) + ': ' + str(exc) + '; starting a new run')
            return False
        (run / 'result.json').unlink()
        task = r.hgetall(TASK)
        print('reopened ' + task.get('id', '') + ' (stopped by the lifecycle hold) for --resume')
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
    command = keep_awake_prefix() + command  # keep_awake_prefix: caffeinate / systemd-inhibit / nothing
    result = mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(run))
    if not result.success:  # Redis stays up: start --resume can be retried
        raise SystemExit(result.error or 'backend ' + backend + ': the daemon tab could not be opened')
    save(LATEST, dict(cfg, config=str(path)))
    print('fleet resumed (' + cfg.get('mode', 'task') + ' mode). Run directory:', run)
    return True


def daemon(cfg_path, resume=False):
    cfg = load(cfg_path)
    fleet = None
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
                subfleet.stop_tmux(cfg)  # the fleet's own tmux server goes last (§19 dual display)
    if fleet is not None:
        lifecycle_close.after_daemon(fleet)  # lock released, Redis down: close this run's workspace


def resolve_supervisor(args, default):
    """Fill the supervisor/worker CLIs and the supervisor's pi model/thinking from the pair table
    default. Precedence for a pi supervisor (first hit wins):
      model:    --supervisor-pi-model (or a mapped old --supervisor-profile) > explicit --pi-model
                > default.supervisor_pi_model (only when the supervisor CLI came from the table default)
      thinking: --supervisor-pi-thinking (or the mapped profile's) > explicit --pi-thinking
                > default.supervisor_pi_thinking (same condition) > off
    The table default never overrides a flag the operator typed. An old Codex profile flag on a
    defaulted pi supervisor is mapped (cli_pairs.LEGACY_SUPERVISOR_PROFILES) or refused with the
    new usage (ValueError) -- never a silent official Codex TUI."""
    explicit = getattr(args, 'supervisor_cli', None)
    args.supervisor_cli = explicit or default.get('supervisor_cli') or 'codex'
    args.worker_cli = getattr(args, 'worker_cli', None) or default.get('worker_cli') or 'codex'
    model, thinking = getattr(args, 'supervisor_pi_model', None), getattr(args, 'supervisor_pi_thinking', None)
    if args.supervisor_cli != 'pi':
        if model or thinking:
            raise ValueError('--supervisor-pi-model / --supervisor-pi-thinking need a pi supervisor '
                             '(--supervisor-cli pi, or the default)')
        if (args.supervisor_cli == 'codex' and default.get('supervisor_profile')
                and not getattr(args, 'supervisor_profile', None) and not getattr(args, 'profile', None)):
            args.supervisor_profile = default['supervisor_profile']
        args.pi_models, args.pi_thinkings = {}, {}
        args.pi_thinking = getattr(args, 'pi_thinking', None) or 'off'
        return
    legacy = getattr(args, 'supervisor_profile', None) or (
        getattr(args, 'profile', None) if args.worker_cli != 'codex' else None)
    if legacy and not explicit:
        mapped_model, mapped_thinking = cli_pairs.legacy_supervisor_profile(legacy)
        if model and model != mapped_model:
            raise ValueError('--supervisor-profile ' + legacy + ' maps to ' + mapped_model + ' but --supervisor-pi-model '
                             'says ' + model + '; pass only --supervisor-pi-model')
        model, thinking = mapped_model, thinking or mapped_thinking
        print('--supervisor-profile/--profile ' + legacy + ' is the old Codex profile; now running the pi supervisor '
              'on ' + model + ' (thinking ' + thinking + '). Use --supervisor-cli codex --supervisor-profile ' + legacy
              + ' for the official Codex TUI.', file=sys.stderr)
        args.supervisor_profile = None
    elif getattr(args, 'supervisor_profile', None):
        raise ValueError('--supervisor-profile is a Codex profile; a pi supervisor takes --supervisor-pi-model '
                         'provider/model [--supervisor-pi-thinking LEVEL]')
    run_model, run_thinking = getattr(args, 'pi_model', None), getattr(args, 'pi_thinking', None)
    if not explicit:  # the table default only fills what neither a supervisor nor a run-wide flag set
        model = model or (None if run_model else default.get('supervisor_pi_model'))
        thinking = thinking or (None if run_thinking else default.get('supervisor_pi_thinking'))
    args.pi_thinking = run_thinking or 'off'  # unset --pi-thinking is still "off" for the run
    args.supervisor_pi_model, args.supervisor_pi_thinking = model, thinking
    args.pi_models = {'supervisor': model} if model else {}
    args.pi_thinkings = {'supervisor': thinking} if thinking else {}


def start(args):
    if getattr(args, 'max_attempts', None) is not None and args.max_attempts < 1:
        raise SystemExit('--max-attempts must be at least 1')
    kit_fleet = getattr(args, 'kit_fleet', None)
    if kit_fleet is not None:  # flag checks only: a resumed run keeps its copy even when the kit is gone
        if not re.fullmatch(KIT_FLEET_RE, kit_fleet):
            raise SystemExit('--kit-fleet: name must match ' + KIT_FLEET_RE)
        if args.roster or args.workers is not None or args.worker_names:
            raise SystemExit('--kit-fleet replaces --roster / --workers / --worker-names; pass only one of them')
    if getattr(args, 'resume', None):
        run = resume_target(args.resume)
        if run and resume(run, args):
            return
    if args.transport_version != 2:
        raise SystemExit('new fleet instances require Redis transport v2; v1 is only readable for existing runs')
    # The private config file is resolved once, here, before the first block read; config.json records it
    # and every process of the run reads that file (pin_paths), not whatever its environment says.
    private_config = fleet_config.from_env()
    fleet_config.pin(private_config)
    # A new run without an explicit CLI takes the pair table's default combination (pi supervisor on
    # the table's model, e.g. openai-codex/gpt-6-astra thinking high, + official Claude workers); the
    # config written below always names both CLIs, so the codex fallback in pair_problem only serves
    # old configs. The official Codex supervisor stays available with --supervisor-cli codex.
    default = cli_pairs.load_pairs().default
    try:
        resolve_supervisor(args, default)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.profile = getattr(args, 'profile', None) or DEFAULTS['profile']
    lifecycle_problem = None
    try:  # --lifecycle decides cfg['mode'] (its base) before the CLI-pair check reads both keys
        args.lifecycle, args.mode = lifecycle.resolve_start(getattr(args, 'lifecycle', None), getattr(args, 'mode', None))
    except ValueError as exc:
        if getattr(args, 'lifecycle', None):
            raise SystemExit(str(exc)) from exc
        lifecycle_problem = str(exc)  # missing --lifecycle: refused below, after the config checks
        args.lifecycle, args.mode = None, getattr(args, 'mode', None) or DEFAULTS['mode']
    budget = None
    if getattr(args, 'budget', None):
        if args.mode != 'service':
            raise SystemExit('--budget is the service budget card; a task carries its own "budget" in the task spec')
        try:
            budget = brake.validate_budget(json.loads(args.budget))
        except ValueError as exc:
            raise SystemExit('--budget rejected: ' + str(exc)) from exc
    try:
        runtime_v2.validate_config(vars(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    roster_path, night_path, worker_roles, worker_profiles, profile_clis = None, None, {}, {}, set()
    if kit_fleet is not None:  # validated in the kit like --roster; copied into the run below
        args.roster = str(kit_fleet_roster(args.worker_kit, kit_fleet))
    if getattr(args, 'night_roster', None) and not args.roster:
        raise SystemExit('--night-roster needs --roster (the day roster with the same worker names)')
    if args.roster:  # validate before any provisioning
        if args.workers is not None or args.worker_names:
            raise SystemExit('--roster replaces --workers / --worker-names; pass only one of them')
        roster_path = Path(args.roster).expanduser().resolve()
        probe = dict(DEFAULTS, **{k: v for k, v in vars(args).items() if v is not None})
        try:
            workers, worker_roles, worker_profiles = roster.load_entries(roster_path)
            # New slots end with -<number> (docs/design.md §3), a declared sub-fleet's workers too
            problems = roster.row_errors(workers) + roster.subfleet_suffix_errors(roster.load_subfleets(roster_path))
            if problems:
                raise ValueError('\n'.join(problems))
            launches = list(night.resolve_launches(probe, worker_profiles, workers).values())
            roster_pi = roster.pi_settings(roster_path.read_bytes())
            problem = roster.pi_cli_problem(*roster_pi, args.worker_cli)
            if problem:
                raise ValueError(problem)
        except (OSError, ValueError) as exc:
            raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + str(exc)) from exc
        args.pi_models = dict(roster_pi[0], **args.pi_models)
        args.pi_thinkings = dict(roster_pi[1], **args.pi_thinkings)
        if getattr(args, 'night_roster', None):
            night_path = Path(args.night_roster).expanduser().resolve()
            try:
                night.check_rosters(probe, roster_path, night_path)
                launches += night.resolve_launches(probe, roster.load_entries(night_path)[2]).values()
                problem = roster.pi_cli_problem(*roster.pi_settings(night_path.read_bytes()), args.worker_cli)
                if problem:
                    raise ValueError(problem)
            except (OSError, ValueError) as exc:
                raise SystemExit('--night-roster: ' + str(exc)) from exc
        profile_clis = {launch['overrides']['worker_cli'] for launch in launches}
    elif args.worker_names:  # new slots end with -<number> (docs/design.md §3), checked before provisioning
        problems = roster.suffix_errors(args.worker_names.split(','), where=lambda i, n: '--worker-names: ' + n)
        if problems:
            raise SystemExit('\n'.join(problems))
    if lifecycle_problem:
        raise SystemExit(lifecycle_problem)
    gate = lifecycle.clock_gate_problem(args.lifecycle)
    if gate:
        raise SystemExit(gate)
    try:
        initiator = lifecycle_close.initiator_for_start(getattr(args, 'initiator', None))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    # A start without profiles runs every worker on the fleet default: its model card caps it too.
    try:
        problem = model_usage.default_problem(
            {'worker_cli': args.worker_cli, 'worker_model': args.worker_model, 'pi_model': args.pi_model,
             'pi_models': args.pi_models, 'host': args.host.strip().lower() if args.host and args.host.strip() else None},
            [w for w in workers if not worker_profiles.get(w)] if roster_path else (args.worker_names.split(',') if args.worker_names else
                                         ['worker-' + str(i + 1) for i in range(max(1, args.workers or 1))]),
            AgentRegistry())
    except (AgentRegistryError, OSError) as exc:
        raise SystemExit('cannot read agent cards: ' + str(exc)) from exc
    if problem:
        raise SystemExit(problem)
    # The kit is checked wherever the advisor comes online at start: the --advisor sugar, or a roster that
    # already says `advisor: on` (the daemon would refuse it only after Redis and the windows are up).
    if args.worker_kit and (getattr(args, 'advisor', False) or (roster_path and roster.advisor(roster_path.read_bytes()))):
        try:
            kitmod.Kit(args.worker_kit)  # refuse a bad kit before provisioning, like a bad roster
        except (OSError, ValueError) as exc:
            raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
    worker_domains = parse_worker_domains(getattr(args, 'worker_domains', None), args.worker_kit)
    backend, why = resolve_backend(getattr(args, 'backend', None))
    if why:  # the chosen backend cannot run here: refuse before provisioning
        if getattr(args, 'backend', None) is None:  # the default (cmux on a Mac with cmux): its own reason
            raise SystemExit(why)
        raise SystemExit('--backend ' + str(args.backend) + ' cannot run here: ' + why)
    if not backends_check(backend):
        raise SystemExit(unavailable_text(backend))
    if roster_path and roster.load_subfleets(roster_path) and subfleet.backend_problem({'backend': backend}):
        raise SystemExit('--roster ' + str(roster_path) + ' rejected:\n' + subfleet.backend_problem({'backend': backend}))
    clis = {args.supervisor_cli, args.worker_cli} | profile_clis  # night cards need their CLI at start
    codex = args.codex or shutil.which('codex')
    pi = args.pi or shutil.which('pi')
    redis_server = args.redis_server or shutil.which('redis-server')
    opencode = args.opencode or shutil.which('opencode')
    binaries = {'codex': codex, 'pi': pi, 'claude': args.claude or shutil.which('claude'),
                'opencode': opencode, 'redis-server': redis_server}
    missing = [n for n in sorted(clis | {'redis-server'}) if not binaries[n] or not shutil.which(binaries[n])]
    if missing:
        raise SystemExit('binaries not found: ' + ', '.join(missing) + ' (pass the corresponding binary flag)')
    queue_ok, codex_version, queue_why = codex_queue.capability(codex)  # pinned for the run (config.json)
    if 'codex' in clis and not queue_ok:
        print('official Codex: `codex queue` channel unsupported (' + queue_why + '); Codex slots use typed delivery',
              file=sys.stderr)
    if 'pi' in clis:
        if not args.allow_unsandboxed_pi:
            raise SystemExit('pi has no sandbox: explicitly pass --allow-unsandboxed-pi after reviewing permissions')
        unset = [n for n in (['supervisor'] if args.supervisor_cli == 'pi' else [])
                 + (([w for w in workers if not worker_profiles.get(w)] if roster_path else ['workers'])
                    if args.worker_cli == 'pi' else [])
                 if not args.pi_models.get(n) and not args.pi_model]
        if unset:
            raise SystemExit('--pi-model provider/model is required for pi agents (or --supervisor-pi-model / '
                             'roster pi_model); no model for: ' + ', '.join(unset))
        version = subprocess.run([pi, '--version'], text=True, capture_output=True, timeout=15, check=True).stdout.strip()
        match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version)
        if not match or tuple(map(int, match.groups())) < (1, 1, 0):
            raise SystemExit('pi >= 1.1.0 is required (agent_settled); found: ' + version)
    if 'opencode' in clis:
        problem = opencode_start_problem(args, opencode)
        if problem:
            raise SystemExit(problem)
    if getattr(args, 'name', None) is not None and not args.name.strip():
        raise SystemExit('--name must not be empty')
    titles = {'agents_title': 'Fleet ' + args.name.strip()} if getattr(args, 'name', None) else {}
    titles['backend'] = backend  # the registry this fleet gets; the run's config keeps it for resume (§5)
    state_dir = getattr(args, 'cmux_state_dir', None)
    mgr = manager_for(dict(titles, cmux_state_dir=str(Path(state_dir).expanduser().resolve())) if state_dir else titles)
    if mgr.session_exists():
        raise SystemExit('cmux-fleet tabs already exist; run `stop` and close them with the ✕ first')
    if not roster_path:
        workers = (args.worker_names.split(',') if args.worker_names
                   else ['worker-' + str(i + 1) for i in range(max(1, args.workers or 1))])
    if len(set(workers)) != len(workers) or not all(re.fullmatch(WORKER_NAME_RE, w) and w not in ('supervisor', ADVISOR)
                                                    for w in workers):
        raise SystemExit('--worker-names: distinct names matching ' + WORKER_NAME_RE + ', not supervisor / advisor '
                         '(no dots: dots mark generations)')
    if args.roles and not Path(args.roles).expanduser().is_dir():
        raise SystemExit('--roles must be a directory of <agent-name>.md files')
    profiles = [args.supervisor_profile or args.profile] if args.supervisor_cli == 'codex' else []
    if args.worker_cli == 'codex':
        profiles.append(args.worker_profile or args.profile)
    home = Path(getattr(args, 'codex_home', None) or HOME / 'codex-home').expanduser()
    if profiles:
        prepare_codex_home(home, profiles)
    sweep_orphan_redis()
    token = uuid.uuid4().hex[:8]
    try:
        redis_cfg = redis_runtime.prepare(args.redis_runtime_dir, token)
    except (OSError, ValueError) as exc:
        raise SystemExit('cannot prepare private Redis runtime: ' + str(exc)) from exc
    run = Path(getattr(args, 'runs_dir', None) or HOME / 'runs').expanduser() / ('fleet-' + time.strftime('%Y%m%d-%H%M%S-') + token)
    (run / 'workspace').mkdir(parents=True)
    if kit_fleet is not None:  # the kit is read-only and versioned: the run follows (and hot-edits) its own copy
        kit_roster = roster_path
        roster_path = (copy_kit_fleet(kit_roster.parent, run / 'fleet-config') / 'roster.yaml').resolve()
        workers, worker_roles, worker_profiles = roster.load_entries(roster_path)  # role paths into the copy
    from cmux_fleet import backends, cmux_manager
    cfg = {'run': str(run), 'python': sys.executable, **redis_cfg,
           'backend': backend,  # decided once here; the daemon, actors, --resume and sub-fleets read it
           'cmux_state_dir': str(mgr.meta_path.parent),
           'fleet_home': str(HOME.expanduser().resolve()), 'cmux_bin': cmux_manager.CMUX,
           'private_config': private_config,
           'workers': workers, 'mode': args.mode, 'lifecycle': args.lifecycle, 'roles': args.roles,
           'max_busy': OPENCODE_MAX_BUSY if args.max_busy is None and args.worker_cli == 'opencode'
                       else DEFAULTS['max_busy'] if args.max_busy is None else args.max_busy,
           **({'budget': budget} if budget else {}),
           **({'brake_notify': shlex.split(args.brake_notify)} if getattr(args, 'brake_notify', None) else {}),
           'request_timeout': args.request_timeout, 'supervisor_profile': args.supervisor_profile,
           'clear_after_result': args.clear_after_result, 'clear_timeout': args.clear_timeout,
           **({'worker_idle_reclaim_min': None if reclaim == 'off' else reclaim}
              if (reclaim := getattr(args, 'worker_idle_reclaim_min', None)) is not None else {}),
           **({'claude_hook_delivery': False} if getattr(args, 'claude_hook_delivery', True) is False else {}),
           'worker_profile': args.worker_profile, 'profile': args.profile, 'codex': codex,
           'codex_queue': queue_ok, 'codex_version': codex_version, 'redis_server': redis_server, 'codex_home': str(home),
           'handoff_ratio': args.handoff_ratio, 'handoff_timeout': args.handoff_timeout,
           'window': args.window, 'stall_timeout': args.stall_timeout,
           'stall_grace': args.stall_grace, 'stall_escapes': args.stall_escapes,
           'stall_window': args.stall_window, 'stall_cpu_pct': args.stall_cpu_pct,
           **({'prompt_table': str(Path(args.prompt_table).expanduser().resolve())} if args.prompt_table else {}),
           'compact_limit': args.compact_limit, 'chrome_script': args.chrome_script,
           'chrome_min_interval': args.chrome_min_interval, 'worker_cli': args.worker_cli,
           'worker_model': args.worker_model, 'worker_kit': args.worker_kit,
           **({'worker_domains': worker_domains} if worker_domains else {}),
           'supervisor_model': args.supervisor_model,
           'claude': args.claude or shutil.which('claude'), 'claude_window': args.claude_window,
           'host': args.host.strip().lower() if args.host and args.host.strip() else None,
           'supervisor_cli': args.supervisor_cli, 'pi': pi, 'pi_model': args.pi_model,
           **({'opencode': opencode, 'opencode_model': args.opencode_model, 'opencode_window': args.opencode_window}
              if 'opencode' in clis else {}),
           'pi_thinking': args.pi_thinking, 'pi_models': args.pi_models, 'pi_thinkings': args.pi_thinkings, 'allow_unsandboxed_pi': args.allow_unsandboxed_pi,
           'pi_agent_dir': args.pi_agent_dir, 'allow_profile_live_switch': args.allow_profile_live_switch,
           'transport_version': args.transport_version, 'allow_development_v2': args.allow_development_v2,
           'max_attempts': args.max_attempts or DEFAULTS['max_attempts'], 'token': token,
           'initiator': initiator}
    if getattr(args, 'advisor', False):  # sugar: the switch lives in the roster, never in config.json (§2.1)
        try:
            for path in advisor_rosters({'run': str(run), 'roster': roster_path and str(roster_path),
                                         'roster_night': night_path and str(night_path)}):
                roster.write_advisor(path, True, workers)
        except (OSError, ValueError) as exc:
            raise SystemExit('--advisor: roster not written: ' + str(exc)) from exc
    if getattr(args, 'name', None):
        cfg.update(name=args.name.strip(), **titles)
    if roster_path:
        # subfleets before the first spawn: a supervisor_slot starts in the fleet's tmux (§29 / dual display)
        cfg.update(roster=str(roster_path), worker_roles=worker_roles, worker_profiles=worker_profiles,
                   subfleets=roster.load_subfleets(roster_path))
    if night_path:
        cfg['roster_night'] = str(night_path)
    if kit_fleet is not None:
        cfg['kit_fleet'] = {'name': kit_fleet, 'kit': str(Path(args.worker_kit).expanduser().resolve()),
                            'roster_md5': hashlib.md5(kit_roster.read_bytes()).hexdigest()}
    if 'pi' in clis:
        cfg['pi_home'] = str(pi_adapter.prepare_home(run / 'pi-home', args.pi_agent_dir, fleet_config.block('pi_context')))
    save(run / 'config.json', cfg)
    start_redis(cfg)
    command = [cfg['python'], str(Path(__file__).resolve()), 'daemon', str(run / 'config.json')]
    command = keep_awake_prefix() + command  # keep_awake_prefix: caffeinate / systemd-inhibit / nothing
    result = mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(run))
    if not result.success:
        connect(cfg).shutdown()
        raise SystemExit(result.error or 'backend ' + backend + ': the daemon tab could not be opened')
    if hasattr(mgr, 'claim_run'):
        mgr.claim_run(str(run))  # the lifecycle closer only closes a registry tagged with this run
    save(LATEST, dict(cfg, config=str(run / 'config.json')))
    print('fleet started (' + (args.lifecycle or '-') + ' lifecycle, ' + args.mode + ' mode): supervisor + ' + str(len(cfg['workers']))
          + ' worker(s) initialising. Run directory:', run)


def night_cmd(cfg, state):
    """`night on|off`: validate both rosters, then write <run>/night.json; the daemon does the rest."""
    run = cfg.get('run')
    if not run or not Path(run).is_dir():
        raise SystemExit('night: no run directory in ' + str(cfg.get('config') or cfg))
    if state == 'status':
        return dict(night.read_state(run), roster=night.roster_for(cfg))
    if not cfg.get('roster_night'):
        raise SystemExit('night: this run has no night roster (start it with --roster and --night-roster)')
    try:
        night.check_rosters(dict(DEFAULTS, **cfg), cfg['roster'], cfg['roster_night'])
    except (OSError, ValueError) as exc:
        raise SystemExit('night ' + state + ' refused, nothing changed:\n' + str(exc)) from exc
    return dict(night.write_state(run, state == 'on'), roster=cfg['roster_night' if state == 'on' else 'roster'])


def kit_fleet_roster(worker_kit, name):
    """start --kit-fleet NAME: <worker kit>/fleets/NAME/roster.yaml, or SystemExit naming what is missing."""
    if not worker_kit:
        raise SystemExit('--kit-fleet needs --worker-kit (the kit that carries fleets/' + name + '/roster.yaml)')
    path = Path(worker_kit).expanduser().resolve() / 'fleets' / name / 'roster.yaml'
    if not path.is_file():
        raise SystemExit('--kit-fleet ' + name + ': the worker kit has no ' + str(path))
    return path


def copy_kit_fleet(source, target):
    """Copy a kit fleet directory (roster, sub-rosters, roles) into the run, writable (the kit is read-only)."""
    shutil.copytree(source, target)
    for root, dirs, files in os.walk(target):
        for entry in [root] + [os.path.join(root, f) for f in files]:
            os.chmod(entry, os.stat(entry).st_mode | stat.S_IWUSR)
    return target


def parse_worker_domains(value, worker_kit):
    """start --worker-domains a,b: [] when not given; refused unless every name is in the kit's domain table."""
    if value is None:
        return []
    names = list(dict.fromkeys(d.strip() for d in value.split(',') if d.strip()))
    if not names:
        raise SystemExit('--worker-domains is empty')
    if not worker_kit:
        raise SystemExit('--worker-domains needs --worker-kit (a domain kit with context_domains.yaml)')
    try:
        kit = kitmod.Kit(worker_kit)
    except (OSError, ValueError) as exc:
        raise SystemExit('--worker-domains needs a domain kit; --worker-kit rejected: ' + str(exc)) from exc
    unknown = [d for d in names if d not in kit.domains]
    if unknown:
        raise SystemExit('--worker-domains not in the domain table of ' + str(worker_kit) + ': '
                         + ', '.join(unknown) + ' (the table has: ' + ', '.join(kit.domains) + ')')
    return names


def advisor_switch(cfg, on):
    """`fleet advisor on|off`: only edits the run's roster; the daemon's roster_tick applies it."""
    run_cfg = load(Path(cfg['run']) / 'config.json')
    if on and run_cfg.get('worker_kit'):
        try:
            kitmod.Kit(run_cfg['worker_kit'])
        except (OSError, ValueError) as exc:
            raise SystemExit('--worker-kit rejected: ' + str(exc)) from exc
    try:
        paths = [roster.write_advisor(path, on, run_cfg['workers']) for path in advisor_rosters(run_cfg)]
    except (OSError, ValueError) as exc:
        raise SystemExit('roster not changed: ' + str(exc)) from exc
    return ('advisor ' + ('on' if on else 'off') + ' written to ' + ', '.join(map(str, paths))
            + '; the daemon applies it on its next tick')


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
    print(json.dumps({'mode': cfg.get('mode', 'task'), 'lifecycle': cfg.get('lifecycle'), 'task': r.hgetall(TASK), 'service': r.hgetall(SERVICE),
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


def cli():
    """The argument parser (main); split out so a test can parse flags without running a fleet."""
    p = argparse.ArgumentParser(prog='cmux-fleet', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('start', help='provision the private redis + cmux control surface (inside cmux)')
    s.add_argument('--workers', type=int, help='number of workers worker-1..N (default 1)')
    s.add_argument('--transport-version', type=int, choices=(2,), default=2,
                   help='new runs require Redis v2; existing v1 runs must be drained and replaced, not hot-flipped')
    s.add_argument('--allow-development-v2', action='store_true',
                   help='opt into incomplete v2 acceptance; no production-readiness claim')
    s.add_argument('--profile', default=None,
                   help='codex config profile for every Codex agent (default: fleet). With the default pi '
                        'supervisor an old supervisor profile (fleet-astra) maps to its pi model, others are refused')
    s.add_argument('--codex', help='codex binary (default: first on PATH)')
    s.add_argument('--redis-server', help='redis-server binary (default: first on PATH)')
    s.add_argument('--redis-runtime-dir', default=str(redis_runtime.DEFAULT_ROOT),
                   help='private Redis runtime root (default: project .runtime/redis; no /tmp fallback)')
    s.add_argument('--fleet-home', help='fleet home (latest.json, default runs/codex-home); pinned in config.json'
                   ' (default: $CMUX_FLEET_HOME or ~/.cmux-fleet)')
    s.add_argument('--backend', choices=('cmux', 'wezterm'),
                   help='terminal backend of this run, written into config.json and followed by --resume and '
                        'sub-fleets (default: cmux on macOS when cmux is usable, otherwise wezterm)')
    s.add_argument('--cmux-bin', help='cmux binary; pinned in config.json (default: $CMUX_BIN or the app bundle)')
    s.add_argument('--runs-dir', help='where run directories are created (default: <fleet home>/runs)')
    s.add_argument('--cmux-state-dir', help='cmux registry directory; pinned in config.json'
                   ' (default: $CMUX_FLEET_STATE or ~/.local/state/cmux-fleet)')
    s.add_argument('--codex-home', help='isolated CODEX_HOME for the agents (login + profile symlinked from yours;'
                   ' no hooks/MCP; default: <fleet home>/codex-home)')
    s.add_argument('--handoff-ratio', type=float, default=DEFAULTS['handoff_ratio'],
                   help='rotate a session once its context use reaches this share of the window '
                        '(lowest-priority default; roster handoff: overrides per CLI/model, docs/design.md §22)')
    s.add_argument('--handoff-timeout', type=float, default=DEFAULTS['handoff_timeout'],
                   help='seconds to wait for the model to write its handoff before using the skeleton')
    s.add_argument('--window', type=int, default=DEFAULTS['window'],
                   help='context window fallback when the rollout has not reported one yet')
    s.add_argument('--stall-timeout', type=float, default=DEFAULTS['stall_timeout'],
                   help='seconds without rollout activity before a busy agent is treated as stalled (0 = off); '
                        'in transport v2 the hard limit behind --stall-window')
    s.add_argument('--stall-window', type=float, default=watchdog.DEFAULTS['stall_window'],
                   help='transport v2: seconds the transcript, the screen and the pane process-tree CPU must all '
                        'stay still before a busy agent is treated as stalled (0 = off, only --stall-timeout)')
    s.add_argument('--stall-cpu-pct', type=float, default=watchdog.DEFAULTS['stall_cpu_pct'],
                   help='transport v2: CPU (%% of one core, averaged over --stall-window) the pane process tree '
                        'must use to count as moving; an idle TUI uses about 1 %%')
    s.add_argument('--stall-grace', type=float, default=watchdog.DEFAULTS['stall_grace'],
                   help='transport v2: seconds a stalled agent gets after its first Escape before it is treated '
                        'as frozen (killed and restarted in place)')
    s.add_argument('--stall-escapes', type=int, default=watchdog.DEFAULTS['stall_escapes'],
                   help='transport v2: Escape presses for a stalled agent; all of them leaving the screen '
                        'unchanged means frozen')
    s.add_argument('--prompt-table', help='transport v2: watchdog dialog whitelist YAML (hot-reloaded; default '
                                          'config/fleet.yaml#watchdog_prompts)')
    s.add_argument('--compact-limit', type=int, help='codex model_auto_compact_token_limit (default: codex decides)')
    s.add_argument('--chrome-script', help='optional browser broker script run by the daemon for sandboxed agents')
    s.add_argument('--chrome-min-interval', type=float, default=DEFAULTS['chrome_min_interval'])
    s.add_argument('--supervisor-cli', choices=cli_pairs.CLIS, default=None,
                   help='CLI for the supervisor (default: fleet.yaml#cli_pairs default)')
    s.add_argument('--supervisor-model', default=DEFAULTS['supervisor_model'], help='claude supervisor: --model value')
    s.add_argument('--worker-cli', choices=cli_pairs.WORKER_CLIS, default=None,
                   help='CLI for workers (default: fleet.yaml#cli_pairs default; opencode is worker-only)')
    s.add_argument('--opencode', help='opencode binary (>= 2.0.29; default: first on PATH)')
    s.add_argument('--opencode-model', default=None,
                   help='opencode workers: provider/model, e.g. opencode/deepseek-v4.1-flash-free (required; no code '
                   'default, the free list changes; never replaced by a paid model)')
    s.add_argument('--opencode-window', type=int, default=OPENCODE_WINDOW,
                   help='opencode workers: context window for the handoff check (default 200000 = free tier)')
    s.add_argument('--pi', help='pi binary (>= 1.1.0; default: first on PATH)')
    s.add_argument('--pi-model', help='explicit provider/model for every pi agent without its own model '
                   '(--supervisor-pi-model, roster pi_model); no implicit cloud fallback')
    s.add_argument('--supervisor-pi-model', default=None,
                   help='pi supervisor: provider/model (default: --pi-model if given, else fleet.yaml#cli_pairs '
                        'default.supervisor_pi_model when the supervisor CLI comes from the default)')
    s.add_argument('--supervisor-pi-thinking', choices=pi_adapter.THINKING, default=None,
                   help='pi supervisor: thinking level (default: --pi-thinking if given, else fleet.yaml#cli_pairs '
                        'default.supervisor_pi_thinking when the supervisor CLI comes from the default, else off)')
    s.add_argument('--pi-thinking', choices=pi_adapter.THINKING, default=None,
                   help='pi thinking level for every pi agent without its own (default: off; an explicit value '
                        'also beats fleet.yaml#cli_pairs default.supervisor_pi_thinking)')
    s.add_argument('--pi-agent-dir', default=os.environ.get('PI_CODING_AGENT_DIR') or str(Path.home() / '.pi/agent'),
                   help='source of auth.json/models.json only, linked into a fresh per-run pi home')
    s.add_argument('--allow-unsandboxed-pi', action='store_true',
                   help='acknowledge that pi tools run with your OS permissions, without a sandbox')
    s.add_argument('--allow-profile-live-switch', action='store_true',
                   help='allow task apply_profiles_live=true to respawn idle workers for assigned profiles')
    s.add_argument('--worker-model', default=DEFAULTS['worker_model'], help='claude workers: --model value')
    s.add_argument('--advisor', action='store_true',
                   help='start with the advisor online: only writes "advisor: on" into the roster (--roster, else '
                        '<run>/roster.yaml). Online, the advisor (official Claude CLI, fable/high) is consulted before '
                        'every dispatch, picks worker model / effort / domains and authorizes the respawns its sheets '
                        'need. Default off; turn it on / off while running with `advisor on|off`')
    s.add_argument('--worker-domains', help='claude workers: comma-separated domains of the --worker-kit domain table '
                   'every Claude worker always gets (advisor sheets only add to them; with the advisor off they '
                   'are linked into the default layout too)')
    s.add_argument('--worker-kit', help='claude workers: dir with .claude/{agents,skills} and mcp.json copied into each worker cwd')
    s.add_argument('--claude', help='claude binary (default: first on PATH)')
    s.add_argument('--claude-window', type=int, default=DEFAULTS['claude_window'], help='claude models not in the built-in 1M list ([1m]/opus/fable/sonnet): context window (§22)')
    s.add_argument('--worker-idle-reclaim-min', metavar='MIN|off', type=reclaim_minutes,
                   help='service: this run\'s idle-worker reclaim in minutes, or off (default: fleet.yaml#lifecycle_modes)')
    s.add_argument('--clear-after-result', action='store_true', help='clear Claude service worker context after every terminal result')
    s.add_argument('--clear-timeout', type=float, default=20, help='seconds to confirm context clear (default: 20)')
    s.add_argument('--no-claude-hook-delivery', dest='claude_hook_delivery', action='store_false',
                   help='Claude workers: typed delivery instead of the Stop-hook channel (required by --clear-after-result)')
    s.add_argument('--lifecycle', metavar='MODE',
                   help='required: ' + ' | '.join(lifecycle.MODE_NAMES) + ' (config/fleet.yaml#lifecycle_modes); '
                        'decides what done means, how the run is closed, and --mode')
    s.add_argument('--host', default=None,
                   help='this machine for agent-card hosts (e.g. studio); unset = cards are not filtered by host')
    s.add_argument('--mode', choices=('task', 'service'), default=None,
                   help='transport mode, derived from --lifecycle (base); passing a different one is refused')
    s.add_argument('--worker-names', help='comma-separated worker names (default worker-1..N), each ending with '
                   '-<number>, e.g. exit-1,reentry-1,rung-1')
    s.add_argument('--roster', help='worker roster YAML (workers: [{name, role?}]); edits while running add or '
                                    'retire workers. Replaces --workers / --worker-names')
    s.add_argument('--kit-fleet', metavar='NAME',
                   help='take the roster from the --worker-kit: copies <kit>/fleets/NAME/ to <run>/fleet-config/, then '
                        'runs like --roster <run>/fleet-config/roster.yaml; --resume keeps the copy (design.md §30)')
    s.add_argument('--night-roster', help='night roster YAML: the same worker names as --roster, each with its night '
                                          'profile; `night on|off` switches between them (docs/design.md §25)')
    s.add_argument('--roles', help='dir of <agent-name>.md files appended to that agent\'s AGENTS.md (one prompt per worker)')
    s.add_argument('--supervisor-profile', help='codex profile for a Codex supervisor (default: --profile); without '
                   '--supervisor-cli, fleet-astra maps to the pi supervisor on openai-codex/gpt-6-astra, others are refused')
    s.add_argument('--worker-profile', help='codex profile for codex workers (default: --profile)')
    s.add_argument('--max-busy', type=int, default=None,
                   help='at most this many workers working at once (0 = no limit; default 0, '
                   'or 2 for opencode workers: free-tier concurrency is untested)')
    s.add_argument('--budget', help='service mode: budget card JSON, e.g. \'{"pool": "pool-a", "max_usd": 40, '
                   '"max_calls": 200}\' (pool ids: $CMUX_FLEET_CONFIG or ~/.cmux-fleet/fleet.yaml; tasks put "budget" '
                   'in the task spec); the daemon pauses at any line')
    s.add_argument('--brake-notify', help='command run (no shell) with one message argument when the brake pauses '
                   'the fleet, e.g. "/usr/local/bin/tg-send --chat ops"')
    s.add_argument('--request-timeout', type=float, default=DEFAULTS['request_timeout'],
                   help='service mode: seconds before an unanswered request is published as timeout')
    s.add_argument('--resume', metavar='RUN_DIR|auto',
                   help='reattach an unfinished v2 run instead of creating one (auto: the run in latest.json; '
                        'a finished or missing run starts a new one). Other start flags except --roster / '
                        '--max-attempts are ignored when a run is reattached')
    s.add_argument('--name', help='human-readable fleet name used in sub-fleet titles (default: task goal or run token)')
    s.add_argument('--max-attempts', type=int,
                   help='times one dispatch/request may run in total across start --resume (default 2)')
    s.add_argument('--initiator', metavar='KIND:ID',
                   help='who started this run, notified when it closes (claude:<session>, launchd:<label>, '
                        'program:<id>; default $CMUX_FLEET_INITIATOR or the launchd label)')
    s.add_argument('--extend-hours', type=float,
                   help='with --resume of a run the lifecycle hold stopped for its time budget: hours to add')
    nt = sub.add_parser('night', help='switch the worker roster to the night (on) or day (off) profiles; status prints it')
    nt.add_argument('state', choices=('on', 'off', 'status'))
    nt.add_argument('--config', default=str(LATEST))
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
    lc = sub.add_parser('lifecycle-close', help='close a stopped run\'s cmux workspace (internal; detached)')
    lc.add_argument('cfg')
    dm = sub.add_parser('daemon', help='run the coordinator (internal)')
    dm.add_argument('cfg')
    dm.add_argument('--resume', action='store_true', help='reconcile and replay (internal; from start --resume)')
    t = sub.add_parser(
        'task', help='submit a goal: JSON file or - for stdin '
        '({"goal":..., "hours":..., "artifacts":[...], "auto_agents":true, "agent_registry":...})')
    t.add_argument('spec')
    for name in ('status', 'stop', 'resume'):
        sp = sub.add_parser(name)
        sp.add_argument('--config', default=str(LATEST))
    sp.add_argument('--raise', dest='raise_', action='append', metavar='LIMIT=VALUE',
                    help='after a budget pause: raise a budget line (e.g. max_calls=400); repeatable')
    av = sub.add_parser('advisor', help='turn the advisor on / off for a run: edits its roster (--roster, else '
                                        '<run>/roster.yaml); the daemon applies it without a restart')
    av.add_argument('state', choices=('on', 'off'))
    av.add_argument('--config', default=str(LATEST))
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
    h = sub.add_parser('claude-rewake', help='Claude Code asyncRewake Stop hook of hook-channel workers (internal)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-posttool', help='Claude Code PostToolUse hook: mid-turn handoff prompt (internal)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-stop-failure', help='Claude Code StopFailure hook (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-clear', help='Claude Code SessionStart(clear) hook of clear-after-result workers (internal)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('claude-prompt', help='Claude Code UserPromptSubmit hook (internal; hook JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    h = sub.add_parser('pi-event', help='Pi extension callback (internal; event JSON on stdin)')
    for arg in ('cfg', 'agent', 'generation'):
        h.add_argument(arg)
    a = sub.add_parser('actor', help='exec the configured CLI for one agent (internal)')
    for arg in ('cfg', 'agent', 'prompt'):
        a.add_argument(arg)
    a.add_argument('--generation', type=int, help='pinned spawn generation (internal)')
    return p


def main(argv=None):
    p = cli()
    args = p.parse_args(argv)
    if args.cmd == 'start':
        pin_paths({'fleet_home': args.fleet_home and str(Path(args.fleet_home).expanduser().resolve()),
                   'cmux_bin': args.cmux_bin and str(Path(args.cmux_bin).expanduser().resolve())})
        args.runs_dir = args.runs_dir or str(HOME / 'runs')
        args.codex_home = args.codex_home or str(HOME / 'codex-home')
        start(args)
        return
    if getattr(args, 'cfg', None):  # internal entry points: daemon, actor, hooks, notify, lifecycle-close
        _pin_from(args.cfg)
    if args.cmd == 'daemon':
        daemon(args.cfg, args.resume)
    elif args.cmd == 'lifecycle-close':
        lifecycle_close.close_workspace(args.cfg)
    elif args.cmd == 'notify':
        notify(args.cfg, args.agent, args.generation, args.payload)
    elif args.cmd == 'claude-stop':
        out = claude_stop(args.cfg, args.agent, args.generation, sys.stdin.read())
        if isinstance(out, dict) and out.get('decision') == 'block':  # hook channel: the next delivery
            print(json.dumps(out, ensure_ascii=False))
    elif args.cmd == 'claude-rewake':
        text = claude_rewake(args.cfg, args.agent, args.generation, sys.stdin.read())
        if text:  # exit 2 wakes Claude Code with stderr as "<rewakeMessage> <text>"
            sys.stderr.write(text)
            sys.exit(2)
    elif args.cmd == 'claude-clear':
        claude_clear(args.cfg, args.agent, args.generation, sys.stdin.read())
    elif args.cmd == 'claude-posttool':
        sys.stdin.read()
        prompt = claude_posttool(args.cfg, args.agent, args.generation)
        if prompt:
            print(json.dumps({'decision': 'block', 'reason': prompt}))
    elif args.cmd == 'claude-stop-failure':
        claude_stop_failure(args.cfg, args.agent, args.generation, sys.stdin.read())
    elif args.cmd == 'claude-prompt':
        reason = claude_prompt_main(args.cfg, args.agent, args.generation, sys.stdin.read())
        if reason:  # nothing at all on pass: a UserPromptSubmit hook's stdout becomes model context
            print(json.dumps({'decision': 'block', 'reason': reason}, ensure_ascii=False))
    elif args.cmd == 'pi-event':
        pi_event(args.cfg, args.agent, args.generation, sys.stdin.read(1000001))
    elif args.cmd == 'actor':
        actor(args.cfg, args.agent, args.prompt, args.generation)
    elif args.cmd in ('agents', 'plan'):
        print_inspection(args)
    else:
        cfg = pin_paths(load(args.config))
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
        elif args.cmd == 'night':
            print(json.dumps(night_cmd(cfg, args.state), ensure_ascii=False))
        elif args.cmd == 'advisor':
            print(advisor_switch(cfg, args.state == 'on'))
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
            if getattr(args, 'raise_', None):
                ev['raise'] = args.raise_
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
