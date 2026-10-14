"""Sub-fleets (docs/design.md §19): a worker of one fleet becomes the supervisor of a child fleet.

The child is a complete fleet of its own (daemon, private Redis, run directory under the
parent's ``sub/<task id>``, cmux registry ``cmux-fleet:<task id>``, its own workspace beside
the parent's). The worker's live pane is handed over, not respawned: the parent stops
delivering to the slot (``state=delegated``) and releases the surface from its registry; the
child daemon adopts it as its supervisor, rewrites its role file and asks it for READY. The
CLI process keeps the hooks it was started with (parent config, worker slot, generation), so
``delegated_route`` re-targets those callbacks to the child in Python.

Only official Claude Code worker panes can be handed over. A Pi bridge is bound to the parent's
Redis socket and never reconnects; Codex workers are outside the v2 support matrix.

Every side effect (Redis start, cmux windows, surface moves) runs from ``tick`` outside any v2
effect transaction. Callers inside a transaction (the parent's decision path) use ``request``
which only writes a request file; ``check`` gives the limit verdict synchronously.

Internal API
  check(fleet, slot, task_id, workers, ready=True) -> None | reason   (ready=False: limits only, not the slot state)
  request(run_dir, slot, task_id, goal, workers, output=None, proposal=None, contracts=False, outputs=None) -> request id
  spawn_child(fleet, slot, task_id, goal, workers, output=None, proposal=None, launch=None, contracts=False,
              outputs=None) -> record   (outputs: parent paths; the child writes them in its own workspace)
  tick(fleet)                    parent daemon loop: requests, completion, return of the pane
  stop_children(fleet)           explicit stop / tree stop (blocking; descendants first)
  resume_children(fleet)         root --resume: relaunch children that were running
  delegated_route(cfg, name, generation) -> (cfg, name, generation)  hook re-targeting
  tree(cfg) -> nested status for ``status``
  page_links(cfg) -> {'parent', 'children'} for the history/control page (both directions)
  follow_jump(fleet)             daemon tick: a clicked page link switches to that fleet's cmux workspace
"""
import json
import re
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from cmux_fleet import codex_queue, handoff

LIMITS = {'max_depth': 2, 'max_children': 3, 'max_agents_total': 12, 'child_workers_max': 3}
ACTIVE = ('starting', 'running', 'stopping')
TITLE_MAX = 60
SEP = ' › '
ROLE_FILES = ('AGENTS.md', 'CLAUDE.md', 'CONTRACTS.md')  # a contract sub-fleet writes its supervisor CONTRACTS.md
STOP_WAIT = 180  # seconds a tree stop waits for one child daemon to exit
LAUNCH_GRACE = 120  # seconds a just-launched child daemon has to take its lock before it counts as gone
JUMP_PARAM = 'fleet-jump'  # query marker a page link click adds (history.py); a plain reload has none
JUMP_POLL = 1.0  # seconds between two reads of the history pane URL
JUMP_MAX_AGE = 120  # an older click (daemon was down) only puts the page back, it never moves the view
EXHAUSTED = 'max-attempts'  # prefix of the refusal once a task used up its sub-fleet spawns


class Refused(ValueError):
    """A sub-fleet request the program refuses, with the reason for the operator/supervisor."""


def _fleet():
    from cmux_fleet import fleet
    return fleet


# ------------------------------------------------------------------ naming / titles
def task_level(task_id):
    """M0 -> 2 (main fleet + one sub-fleet level), M0.1 -> 3."""
    return task_id.count('.') + 2


def valid_child_id(parent_task_id, task_id):
    """Children of the main fleet are M<n>; children of sub-fleet M0 are M0.<n>."""
    prefix = (re.escape(parent_task_id) + r'\.') if parent_task_id else 'M'
    return isinstance(task_id, str) and re.fullmatch(prefix + r'\d+', task_id) is not None


def session_name(task_id=None):
    return 'cmux-fleet' + (':' + task_id if task_id else '')


def root_name(cfg, goal=''):
    """Human-readable main fleet name: start --name, else the task goal's head, else the run token."""
    if cfg.get('name'):
        return cfg['name']
    head = ' '.join((goal or '').split())
    if head:
        return head[:24] + ('…' if len(head) > 24 else '')
    return token(cfg)


def token(cfg):
    return cfg.get('root_token') or cfg.get('token') or Path(cfg['run']).name.rsplit('-', 1)[-1]


def title(path, limit=TITLE_MAX):
    """Workspace title from a tree path [root, slot, id, slot, id]: the last id reads '子 fleet <id>'.

    Too long: middle levels become '…'; the root name and the last two segments always stay."""
    segs = list(path[:-1]) + ['子 fleet ' + path[-1]] if len(path) > 1 else list(path)
    full = SEP.join(segs)
    if len(full) <= limit or len(segs) <= 3:
        return full
    tail = SEP.join(segs[-2:])
    room = limit - len(SEP + '…' + SEP + tail)
    head = segs[0] if len(segs[0]) <= max(room, 8) else segs[0][:max(room, 8) - 1] + '…'
    return head + SEP + '…' + SEP + tail


def placeholder_label(slot, task_id, workspace_title):
    return slot + ' → 已委派：子 fleet ' + task_id + '（看侧栏「' + workspace_title + '」）'


# ------------------------------------------------------------------ tree on disk
def children_dirs(run):
    sub = Path(run) / 'sub'
    return sorted(p for p in sub.iterdir() if (p / 'subfleet.json').is_file()) if sub.is_dir() else []


def record(child_run):
    try:
        return json.loads((Path(child_run) / 'subfleet.json').read_text())
    except (OSError, ValueError):
        return {}


def save_record(child_run, rec):
    _fleet().save(Path(child_run) / 'subfleet.json', rec)


def attempts_used(run, task_id):
    """Sub-fleets ever started for ``task_id`` here (``sub/M0``, ``sub/M0-attempt-<n>``; failed ones too)."""
    return sum(1 for d in children_dirs(run) if record(d).get('task_id') == task_id)


def active_children(run):
    return [d for d in children_dirs(run) if record(d).get('state') in ACTIVE]


def page_links(cfg):
    """Cross links of this fleet's history page: 'back to the parent fleet' and the sub-fleet list.

    Read-only, from disk (config + subfleet.json); every target is the other fleet's history page.
    The sidebar title is shown too: a file link moves the browser pane, it cannot switch cmux workspaces."""
    from cmux_fleet import history
    fleet = _fleet()
    parent = None
    if cfg.get('parent_config'):
        try:
            pcfg = fleet.load(cfg['parent_config'])
        except (OSError, ValueError):
            pcfg = None
        if pcfg:
            parent = {'task_id': pcfg.get('task_id') or '', 'url': history.page_url(pcfg['run']),
                      'title': pcfg.get('agents_title') or 'Fleet agents'}  # cmux_manager's default title
    children = []
    for d in children_dirs(cfg['run']):
        rec = record(d)
        children.append({'task_id': rec.get('task_id') or d.name, 'run': d.name, 'state': rec.get('state'),
                         'outcome': rec.get('outcome'), 'slot': rec.get('slot'), 'attempt': rec.get('attempt'),
                         'title': rec.get('title') or '', 'url': history.page_url(d)})
    return {'parent': parent, 'children': children}


def _page(url):
    return unquote(urlsplit(url).path)


def _jump_target(f, url, rel):
    """(verdict, workspace, page to leave in this pane, reason) for a clicked link to page ``url``.

    Only this fleet's registered parent (config ``parent_config``) and its own children
    (``sub/*/subfleet.json``) are targets, and only with the matching ``rel``. A closed or
    unregistered target is shown in place (its history page stays readable); anything else
    puts this fleet's own page back."""
    from cmux_fleet import history
    fleet = _fleet()
    path = _page(url)
    if path == _page(history.page_url(f.run_dir)):
        return 'self', None, None, 'link to this fleet'
    clean = urlsplit(url)._replace(query='', fragment='').geturl()
    target = None
    if rel == 'parent' and f.cfg.get('parent_config'):
        try:
            pcfg = fleet.load(f.cfg['parent_config'])
        except (OSError, ValueError):
            return 'unregistered', None, clean, 'parent config unreadable'
        if path == _page(history.page_url(pcfg['run'])):
            target = pcfg
    elif rel == 'child':
        for d in children_dirs(f.run_dir):
            if path != _page(history.page_url(d)):
                continue
            if record(d).get('state') not in ACTIVE:
                return 'closed', None, clean, 'sub-fleet ' + d.name + ' is ' + str(record(d).get('state'))
            try:
                target = fleet.load(d / 'config.json')
            except (OSError, ValueError):
                return 'unregistered', None, clean, 'sub-fleet config unreadable'
    if target is None:
        return 'unrelated', None, None, 'not a registered ' + (rel or 'parent/child') + ' of this fleet'
    ws = fleet.manager_for(target).workspace_id()
    if not ws or ws == f.mgr.workspace_id():
        return 'unregistered', None, clean, 'no cmux workspace registered'
    return 'jump', ws, None, ''


def follow_jump(f, now=None):
    """Daemon tick: an explicit link click on this fleet's history pane becomes a workspace switch.

    The page's parent/child links reload the shown page as ``…?fleet-jump=<ms36>-<rand>&rel=parent|child&to=<page>``
    (history.py; the pane stays on the same file because WebKit may not load a file:// page outside the
    shown page's directory, while ``cmux browser goto`` can). A URL without that marker is never acted on
    (reload, hash changes). Each marker is
    handled once and recorded in Redis before acting, so repeated ticks and daemon restarts never
    switch twice; they only finish restoring the pane. The source pane goes back to this fleet's own
    page (last seen hash); the target fleet's pane is left alone. Returns the verdict or None."""
    from cmux_fleet import history
    fleet = _fleet()
    now = time.time() if now is None else now
    if not (f.cfg.get('parent_config') or children_dirs(f.run_dir)):
        return None  # no links on the page: no cmux call per tick
    if now < getattr(f, 'jump_next', 0) or not hasattr(f.mgr, 'browser_url'):
        return None
    f.jump_next = now + JUMP_POLL
    pane = f.r.hget(fleet.SERVICE, 'history_pane')
    url = pane and f.mgr.browser_url(pane)
    if not url:
        return None
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    own = history.page_url(f.run_dir)
    if JUMP_PARAM not in query:
        if parts.fragment and _page(url) == _page(own):
            f.r.hset(fleet.SERVICE, 'history_hash', parts.fragment)
        return None
    nonce = query[JUMP_PARAM][0]
    own_shown = _page(url) == _page(own) and parts.fragment  # clicked on our page: its state is in the URL
    hash_ = parts.fragment if own_shown else f.r.hget(fleet.SERVICE, 'history_hash')
    home = own + ('#' + hash_ if hash_ else '')
    try:
        done = json.loads(f.r.hget(fleet.SERVICE, 'history_jump') or '{}')
    except ValueError:
        done = {}
    if done.get('nonce') == nonce:  # already handled (repeated tick, restart): never switch again
        f.mgr.browser_goto(pane, done.get('page') or home)
        return 'repeat'
    try:
        age = now - int(nonce.split('-', 1)[0], 36) / 1000
    except ValueError:
        age = None
    rel = (query.get('rel') or [''])[0]
    to = (query.get('to') or [''])[0]
    if age is None or not -60 <= age <= JUMP_MAX_AGE or not to.startswith('file:'):
        verdict, ws, page, why = 'stale', None, None, 'request too old or malformed'
    else:
        verdict, ws, page, why = _jump_target(f, to, rel)
    page = page or home
    f.r.hset(fleet.SERVICE, 'history_jump', json.dumps({'nonce': nonce, 'verdict': verdict, 'page': page}))
    if verdict == 'jump' and not f.mgr.select_workspace(ws):
        verdict, why = 'failed', 'select-workspace ' + ws + ' failed'
        page = urlsplit(to)._replace(query='', fragment='').geturl()  # the user still sees what they clicked
        f.r.hset(fleet.SERVICE, 'history_jump', json.dumps({'nonce': nonce, 'verdict': verdict, 'page': page}))
    f.mgr.browser_goto(pane, page)
    f.log('history.jump', verdict=verdict, rel=rel, target=_page(to), workspace=ws or '', reason=why)
    return verdict


def root_config(cfg):
    path = cfg.get('root_config')
    return _fleet().load(path) if path else cfg


def limits(cfg):
    """Defaults <- root config 'limits' <- root roster YAML 'limits' (read now: hot)."""
    import yaml
    from cmux_fleet import roster
    root = root_config(cfg)
    out = dict(LIMITS, **{k: v for k, v in (root.get('limits') or {}).items() if k in LIMITS})
    if root.get('roster'):
        try:
            out.update(roster.load_limits(root['roster']))
        except (OSError, ValueError, yaml.YAMLError):
            pass  # a broken roster is refused by the roster reload itself; keep the last good limits
    return out


def tree_agents(run):
    """Agents alive in a fleet tree: supervisor + workers of the root, plus the workers of
    every active descendant (a sub-fleet supervisor is the delegated worker, not a new agent)."""
    fleet = _fleet()
    cfg = fleet.load(Path(run) / 'config.json')
    total = (0 if cfg.get('adopt') else 1) + len(cfg.get('workers') or [])
    for child in active_children(run):
        total += tree_agents(child)
    return total


# ------------------------------------------------------------------ limits / refusal reasons
def check(f, slot, task_id, workers, ready=True, named=None):
    """None when this sub-fleet may start now, else the reason (refusals are enforced by program).

    ``ready=False`` (escalate / approve time): limits and slot kind only; the worker is still
    settling its report, so idle / in-flight are checked again when the spawn runs.
    ``named``: a roster ``subfleets:`` entry (§29): ``task_id`` is its name; the M<n> id, depth and
    max_attempts rules (made for split tasks) do not apply, its workers run the entry's model."""
    fleet = _fleet()
    lim = limits(f.cfg)
    if not named and not valid_child_id(f.cfg.get('task_id'), task_id):
        want = (f.cfg['task_id'] + '.<n>') if f.cfg.get('task_id') else 'M<n>'
        return 'task id ' + repr(task_id) + ' must be ' + want + ' in this fleet'
    if not named and task_level(task_id) > lim['max_depth']:
        return ('max_depth ' + str(lim['max_depth']) + ': ' + task_id + ' would be level '
                + str(task_level(task_id)) + ' (main fleet is level 1)')
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        return 'workers must be a positive integer'
    if named and named.get('clear_after_result') is True:  # §28 in a named child: same rules as a root fleet
        if named.get('claude_hook_delivery', True) is not False:
            return ('sub-fleet ' + task_id + ': clear_after_result needs typed delivery, but Claude Stop-hook '
                    'delivery is on (the default): add claude_hook_delivery: false to its subfleets entry')
        if named['mode'] != 'service' or f.cfg.get('transport_version') != 2:
            return ('sub-fleet ' + task_id + ': clear_after_result needs mode: service and the typed '
                    'Redis v2 delivery path (transport_version 2)')
    if named and named.get('worker_domains'):  # same rule as start --worker-domains, checked at up
        problem = named_domains_problem(f.cfg.get('worker_kit'), named['worker_domains'])
        if problem:
            return 'sub-fleet ' + task_id + ': ' + problem
    if workers > lim['child_workers_max']:
        return 'child_workers_max ' + str(lim['child_workers_max']) + ': asked for ' + str(workers) + ' workers'
    live = active_children(f.run_dir)
    if any(record(d).get('task_id') == task_id for d in live):
        return 'sub-fleet ' + task_id + ' is already running'
    cap = int(f.cfg.get('max_attempts') or fleet.DEFAULTS['max_attempts'])
    used = 0 if named else attempts_used(f.run_dir, task_id)
    if used >= cap:  # acceptance 10/13 P1-4: escalate -> approve must not retry a sub-fleet forever
        return (EXHAUSTED + ' ' + str(cap) + ': sub-fleet ' + task_id + ' already started ' + str(used)
                + ' time(s); no new sub-fleet for it')
    if len(live) >= lim['max_children']:
        return ('max_children ' + str(lim['max_children']) + ': this fleet already runs '
                + ', '.join(record(d).get('task_id', '?') for d in live))
    root_run = Path(root_config(f.cfg)['run'])
    total = tree_agents(root_run)
    if total + workers > lim['max_agents_total']:
        return ('max_agents_total ' + str(lim['max_agents_total']) + ': the tree has ' + str(total)
                + ' agents, ' + str(workers) + ' more would exceed it')
    if slot not in f.active_workers():
        return slot + ' is not an active worker of this fleet'
    # The child's workers run this fleet's default worker runtime (a named entry: its own worker_model):
    # that model card caps them (per fleet).
    try:
        from cmux_fleet import model_usage
        from cmux_fleet.agent_registry import AgentRegistry
        problem = model_usage.default_problem(child_worker_cfg(f.cfg, named) if named else f.cfg,
                                              ['worker-' + str(i + 1) for i in range(workers)], AgentRegistry())
    except Exception as exc:  # unreadable cards: refuse rather than start uncapped
        problem = 'cannot read agent cards: ' + str(exc)
    if problem:
        return problem
    cli = fleet.agent_cli(f.runtime_cfg(slot), slot)
    if cli == 'pi':
        return ('pi worker panes cannot be handed over: the Pi bridge is bound to this fleet\'s Redis '
                'socket and never reconnects')
    if cli != 'claude' or not f.v2:
        return 'only official Claude Code worker panes of a v2 fleet can become sub-fleet supervisors'
    info = f.agent(slot)
    if not ready:
        return None
    if info.get('state') != 'idle' or info.get('runtime_ready') != '1':
        return slot + ' must be idle and READY (state ' + (info.get('state') or '?') + ')'
    if f.r.hget(f.v2.bus.lease_key(slot), 'current_delivery'):
        return slot + ' still has a delivery in flight'
    if not info.get('pane') or f.mgr.get_pane_status(info['pane']) is None:
        return slot + ' has no live tab to hand over'
    return None


# ------------------------------------------------------------------ requests (transaction-safe)
def request(run_dir, slot, task_id, goal, workers, output=None, proposal=None, contracts=False, outputs=None):
    """Queue a spawn for the fleet's next tick. Writes one file; no other side effect."""
    rid = 'sf-' + uuid.uuid4().hex[:12]
    folder = Path(run_dir) / 'subfleet-requests'
    folder.mkdir(parents=True, exist_ok=True)
    _fleet().save(folder / (rid + '.json'), {'id': rid, 'slot': slot, 'task_id': task_id, 'goal': goal,
                                             'workers': workers, 'output': output, 'proposal': proposal,
                                             'contracts': bool(contracts), 'outputs': outputs})
    return rid


def answer_path(run_dir, rid):
    return Path(run_dir) / 'subfleet-requests' / (rid + '.answer.json')


def process_requests(f):
    folder = f.run_dir / 'subfleet-requests'
    if not folder.is_dir():
        return
    fleet = _fleet()
    for path in sorted(folder.glob('sf-*.json')):
        if path.name.endswith('.answer.json'):
            continue
        try:
            spec = fleet.load(path)
            rec = spawn_child(f, spec['slot'], spec['task_id'], spec['goal'], spec['workers'],
                              output=spec.get('output'), proposal=spec.get('proposal'),
                              contracts=spec.get('contracts', False), outputs=spec.get('outputs'))
            answer = {'status': 'accepted', 'run': rec['run'], 'task_id': rec['task_id']}
        except Refused as exc:
            answer = {'status': 'rejected', 'reason': str(exc)}
        except Exception as exc:  # one bad request never kills the daemon nor re-crashes every tick
            answer = {'status': 'rejected', 'reason': 'bad request: ' + str(exc)}
        fleet.save(answer_path(f.run_dir, path.stem), answer)
        path.unlink(missing_ok=True)
        f.log('subfleet.request', request=path.stem, **answer)


# ------------------------------------------------------------------ dual display (supervisor_slot in tmux)
def tmux_slot(cfg, slot):
    """A worker named as ``supervisor_slot`` of a ``subfleets:`` entry runs in the fleet's own tmux server,
    so its one session can show in the parent's and the sub-fleet's workspace at once. Never in a sub-fleet."""
    if cfg.get('adopt') or slot == 'supervisor':
        return False
    return any(isinstance(e, dict) and e.get('supervisor_slot') == slot for e in cfg.get('subfleets') or [])


def tmux_socket(cfg):
    return str(Path(cfg['run']) / 'tmux.sock')


def tmux_spawn_args(cfg, slot, generation):
    """Extra spawn_interactive_cli arguments for ``slot``'s generation ({} for a plain cmux pane)."""
    from cmux_fleet import cmux_manager
    if not tmux_slot(cfg, slot):
        return {}
    return {'tmux_socket': tmux_socket(cfg), 'tmux_session': cmux_manager.tmux_session_name(slot, generation)}


def stop_tmux(cfg):
    """Fleet exit: end the fleet's tmux server (every slot was retired before)."""
    from cmux_fleet import cmux_manager
    if Path(tmux_socket(cfg)).exists():
        cmux_manager.tmux_kill_server(tmux_socket(cfg))


def parent_view(f, slot, sid):
    """Parent side of a delegation: the second-client info of a tmux slot's tab (None: plain cmux pane,
    which is released and moved instead)."""
    probe = getattr(f.mgr, 'tmux_view_info', None)
    return probe(sid) if probe and tmux_slot(f.cfg, slot) else None


def attach_child_view(f, sid, peer, info):
    """Child side: show the delegated session just before ``peer``; returns the child's tab of it (None:
    failed). tmux slot: a second client tab (the parent keeps its own); cmux pane: move the released tab."""
    if info.get('tmux_session'):
        return f.mgr.attach_view(info, peer)
    return sid if f.mgr.adopt(sid, peer, info) else None


def detach_child_view(f, sid):
    """Child side of the return: disconnect its client tab of a tmux slot (session untouched), or release
    the adopted cmux pane for the parent to move back. Returns the hand-back info (None: not ours)."""
    if not sid:
        return None
    probe = getattr(f.mgr, 'tmux_view_info', None)
    view = probe(sid) if probe else None
    if view is not None:
        return view if f.mgr.detach_view(sid) else None
    return f.mgr.release(sid)


def detach_child_views(child_mgr):
    """Parent ``collect``: disconnect client tabs a crashed child left behind before its tabs are closed."""
    meta = child_mgr._meta() if hasattr(child_mgr, '_meta') else {}
    for sid, info in list(meta.get('surfaces', {}).items()):
        if info.get('tmux_role') == 'view':
            child_mgr.detach_view(sid)


# ------------------------------------------------------------------ spawn
def child_lifecycle(parent):
    """A sub-fleet runs the lifecycle its parent's mode names (child_mode), default dev_task, never the
    parent's own (a service parent must not hand a task-mode child lifecycle service). Closing it stays
    with the parent (tick/collect); None for a parent without a lifecycle (older runs)."""
    from cmux_fleet import lifecycle
    if not parent.get('lifecycle'):
        return None
    child = lifecycle.mode_policy(parent['lifecycle'], 'child_mode', 'dev_task')
    if lifecycle.base_mode(child) != 'task':
        raise ValueError('child_mode ' + child + ' is not a task lifecycle')
    return child


NAMED_OVERRIDES = ('claude', 'worker_model', 'stall_timeout', 'stall_window', 'request_timeout', 'max_busy',
                   'clear_after_result', 'clear_timeout', 'claude_hook_delivery')


def child_worker_cfg(parent, named):
    """The worker runtime keys of a named sub-fleet (§29): the entry's value, else the built-in default,
    never the parent's (a read-only parent ``claude`` wrapper must not leak into the child)."""
    fleet = _fleet()
    cfg = {k: v for k, v in parent.items() if k not in NAMED_OVERRIDES}
    for k in NAMED_OVERRIDES:
        if k in named:
            cfg[k] = named[k]
        elif k in fleet.DEFAULTS:
            cfg[k] = fleet.DEFAULTS[k]
    cfg['worker_cli'] = 'claude'  # §19: only official Claude workers in a sub-fleet
    return cfg


def named_domains_problem(worker_kit, domains):
    """None when every entry ``worker_domains`` name is in the parent's kit domain table, else the reason."""
    from cmux_fleet import kit as kitmod
    if not worker_kit:
        return 'worker_domains needs the parent fleet to run with --worker-kit (a domain kit)'
    try:
        kit = kitmod.Kit(worker_kit)
    except (OSError, ValueError) as exc:
        return 'worker_domains needs a domain kit; ' + str(worker_kit) + ' rejected: ' + str(exc)
    unknown = [d for d in domains if d not in kit.domains]
    if unknown:
        return ('worker_domains not in the domain table of ' + str(worker_kit) + ': ' + ', '.join(unknown)
                + ' (the table has: ' + ', '.join(kit.domains) + ')')
    return None


def child_config(f, slot, task_id, workers, rec_dir, sid, info, redis_cfg, name, named=None):
    fleet = _fleet()
    parent = child_worker_cfg(f.cfg, named) if named else f.cfg
    root_path = parent.get('root_config') or str(f.path)
    path = list(parent.get('title_path') or [name]) + [slot, task_id]
    # `advisor` is the parent's in-memory switch (its roster): never inherited. A sub-fleet's advisor is off
    # unless its own roster says `advisor: on` (named sub-fleets, §29; read by the child daemon at start).
    cfg = {k: v for k, v in parent.items() if k not in ('roster', 'worker_roles', 'roles', 'adopt',
                                                          'redis_socket', 'redis_runtime_dir', 'advisor',
                                                          'subfleets')}
    cfg.update(redis_cfg)
    mode = named['mode'] if named else 'task'
    life = None if mode == 'service' else child_lifecycle(parent)
    if named:  # its own roster (hot inside the child, §16), not numbered workers
        from cmux_fleet import roster
        names, roles, profiles = roster.load_entries(named['roster'])
        cfg.update(roster=named['roster'], worker_roles=roles, worker_profiles=profiles,
                   supervisor_role=named['supervisor_role'],
                   named={'name': named['name'], 'autostart': named['autostart']})
        cfg.pop('roster_night', None)
        if 'worker_domains' in named:  # the entry's floor replaces the parent's ([] = none); not written: inherited
            cfg['worker_domains'] = list(named['worker_domains'])
    else:
        names = ['worker-' + str(i + 1) for i in range(workers)]
    cfg.update(run=str(rec_dir), workers=names, mode=mode,
               lifecycle=life, initiator={'kind': 'fleet', 'id': str(f.path)},
               session=session_name(task_id), task_id=task_id, parent_config=str(f.path),
               root_config=root_path, root_token=token(parent), name=name, title_path=path,
               agents_title=title(path), control_title='Fleet control · ' + task_id, tab_prefix=task_id + '/',
               supervisor_cli=fleet.agent_cli(f.runtime_cfg(slot), slot), supervisor_cwd=str(fleet.agent_cwd(parent, slot)),
               adopt={'pane': sid, 'slot': slot, 'generation': int(f.agent(slot).get('generation') or 0),
                      'thread': f.agent(slot).get('thread') or '',  # Codex: the role change is queued to this session
                      'info': info, 'parent_workspace': f.mgr.workspace_id() if hasattr(f.mgr, 'workspace_id') else None})
    return cfg


def deliverables(parent_ws, slot, child_ws, outputs):
    """[[child path, parent path]] for the parent's deliverables of a sub-fleet.

    The child never writes outside its own workspace (its rules, the dispatch suffix): each parent
    path ``<parent ws>/<slot>/<rest>`` becomes ``<child ws>/worker-1/<rest>`` (worker-1 always exists;
    any other path: worker-1/<file name>). ``collect`` copies them back before the parent checks."""
    out = []
    for p in outputs or []:
        p = Path(p)
        try:
            rest = p.relative_to(Path(parent_ws) / slot)
        except ValueError:
            rest = Path(p.name)
        out.append([str(Path(child_ws) / 'worker-1' / rest), str(p)])
    return out


def goal_text(goal, task_id, deliver, proposal, parent_label):
    text = ('Sub-fleet ' + task_id + ' (delegated by ' + parent_label + '). Original instruction:\n' + goal)
    if proposal:
        text += '\nApproved split (dispatch these to your workers): ' + json.dumps(proposal, ensure_ascii=False)
    if deliver:
        text += ('\nDeliverables of ' + task_id + ': have worker-1 write ' + ', '.join(c for c, _ in deliver)
                 + ' (inside this sub-fleet\'s workspace; in a contract task declare each as a final interface '
                 'at that path). Nobody writes outside this sub-fleet\'s workspace: when this sub-fleet ends '
                 'COMPLETED, the parent fleet\'s daemon copies these files to the paths the parent checks. '
                 'Verify they exist before COMPLETED.')
    return text


def deliver_back(f, rec, child_cfg):
    """Trusted copy of a completed sub-fleet's deliverables to the parent's paths. A file the child
    wrote under another of its workers' directories (same relative path) counts when it is the only one."""
    child_ws = Path(child_cfg['run']) / 'workspace'
    for child, parent in rec.get('deliver') or []:
        rest = Path(child).relative_to(child_ws / 'worker-1')
        found = [p for p in [Path(child)] + [child_ws / w / rest for w in child_cfg.get('workers') or []
                                             if w != 'worker-1']
                 if p.is_file() and not p.is_symlink() and p.stat().st_size > 0]
        if not found or (found[0] != Path(child) and len(found) > 1):
            continue  # missing / ambiguous: the parent's own check reports the missing deliverable
        Path(parent).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(found[0], parent)
        f.log('subfleet.delivered', task=rec['task_id'], source=str(found[0]), target=parent)


def default_launch(child_cfg_path, child_cfg, mgr):
    fleet = _fleet()
    command = [child_cfg['python'], str(Path(fleet.__file__).resolve()), 'daemon', str(child_cfg_path)]
    caffeinate = shutil.which('caffeinate')
    if caffeinate:
        command = [caffeinate, '-i'] + command
    return mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'},
                            start_directory=str(child_cfg['run']))


# ------------------------------------------------------------------ parent-side view hooks (§29 / §3.1)
# Where the parent shows or takes back the delegated session's tab. A tmux slot (dual display) keeps its
# own parent tab and the child opens a second client (child side: attach_child_view / detach_child_view);
# a plain cmux pane takes the §19 move-surface (placeholder in the parent, surface released / adopted back).
def parent_attach_view(f, slot, sid, task_id, ws_title):
    """Parent side, at delegation: (placeholder tab or None, released / second-client info).

    Raises Refused when nothing was changed (the spawn rolls back)."""
    view = parent_view(f, slot, sid)
    if view is not None:  # tmux slot: the parent keeps its own tab of the session, nothing is released
        return '', view
    # spawn_window execs the command: keep the tab's process alive (an exited tab reads as a closed runtime)
    show = 'printf "%s\\n" ' + shlex.quote(placeholder_label(slot, task_id, ws_title)) + '; exec sleep 2147483647'
    ph = f.mgr.spawn_window('placeholder-' + slot, '/bin/bash -c ' + shlex.quote(show),
                            metadata={'type': 'placeholder'}, start_directory=str(f.run_dir))
    if not ph.success or not f.mgr.tab_into(ph.pane_id, sid):
        if ph.pane_id:
            f.mgr.kill_pane(ph.pane_id)
        raise Refused('could not open the placeholder tab for ' + slot)
    f.mgr.rename(ph.pane_id, placeholder_label(slot, task_id, ws_title))
    return ph.pane_id, (f.mgr.release(sid) or {})


def parent_detach_view(f, rec, child_mgr, child_run):
    """Parent side, on collect (the child daemon is gone): True when the delegated session is back in
    ``rec['pane']`` as the slot's tab (placeholder closed, tab renamed); False when it is lost."""
    if rec.get('view'):
        # tmux slot: the parent's tab never left; a crashed child may still show its client tab.
        detach_child_views(child_mgr)
        returned = (f.mgr.get_pane_status(rec['pane']) is not None
                    and f.mgr.has_cli_process(rec['pane'], _fleet().RESUME_CLIS))
        if returned:
            f.mgr.rename(rec['pane'], 'fx-' + f.cfg.get('tab_prefix', '') + f.label(rec['slot']))
        return returned
    back = {}
    try:
        back = _fleet().load(Path(child_run) / 'handback.json')
    except (OSError, ValueError):
        info = child_mgr.release(rec['pane'])  # crashed child: its registry still holds the pane
        if info is not None:
            back = {'pane': rec['pane'], 'info': info}
    ph = rec['placeholder']
    returned = bool(back) and back.get('pane') == rec['pane'] and f.mgr.adopt(rec['pane'], ph, back.get('info') or {})
    if returned:
        f.mgr.kill_pane(ph)
        f.mgr.rename(rec['pane'], 'fx-' + f.cfg.get('tab_prefix', '') + f.label(rec['slot']))
    return returned


def spawn_child(f, slot, task_id, goal, workers, output=None, proposal=None, launch=None, contracts=False,
                outputs=None, named=None):
    """Start sub-fleet ``task_id`` with ``slot``'s live pane as its supervisor. Outside transactions only.

    ``named``: a roster ``subfleets:`` entry (§29; ``task_id`` is its name): its own roster and worker
    runtime, its mode (service: no task, no deadline), and the run directory ``sub/<name>`` reused."""
    fleet = _fleet()
    from cmux_fleet import redis_runtime, runtime_v2
    service = bool(named) and named['mode'] == 'service'
    if not service and (not isinstance(goal, str) or not goal.strip()):
        raise Refused('a sub-fleet needs a goal')
    reason = check(f, slot, task_id, workers, named=named)
    if reason:
        raise Refused(reason)
    base = f.run_dir / 'sub'
    rec_dir = base / task_id
    attempt = 1
    if named:
        attempt = int(record(rec_dir).get('attempt') or 0) + 1
    while rec_dir.exists() and not named:
        attempt += 1
        rec_dir = base / (task_id + '-attempt-' + str(attempt))
    name = root_name(f.cfg, '' if f.cfg.get('adopt') or f.service else f.task().get('goal', ''))
    path = list(f.cfg.get('title_path') or [name]) + [slot, task_id]
    ws_title = title(path)
    child_mgr = fleet.manager_for({'session': session_name(task_id), 'cmux_state_dir': f.cfg.get('cmux_state_dir'),
                                   'agents_title': ws_title, 'control_title': 'Fleet control · ' + task_id})
    if child_mgr.session_exists():
        raise Refused('tabs of an earlier sub-fleet ' + task_id + ' are still open; close them first')
    if named and rec_dir.exists():
        archive_run(rec_dir)  # §29: one directory per name; the last run's files move to archive/
    info_now = f.agent(slot)
    sid = info_now['pane']
    deliver = deliverables(f.workspace(), slot, rec_dir / 'workspace', outputs or ([output] if output else []))
    rec = {'task_id': task_id, 'slot': slot, 'run': str(rec_dir), 'state': 'starting', 'attempt': attempt,
           'workers': workers, 'output': output, 'deliver': deliver, 'created': time.time(), 'title': ws_title,
           'placeholder': None, 'pane': sid, 'generation': int(info_now.get('generation') or 0)}
    if named:
        rec.update(name=task_id, named=True, mode=named['mode'], autostart=named['autostart'])
    cfg = released = None
    try:  # anything below fails -> rollback; SystemExit too (start_redis), never the parent daemon
        # 1. child run directory, config, private Redis, task
        (rec_dir / 'workspace').mkdir(parents=True)
        backup = rec_dir / 'handover'
        backup.mkdir()
        cwd = fleet.agent_cwd(f.cfg, slot)
        for fname in ROLE_FILES:
            if (cwd / fname).exists():
                shutil.copy2(cwd / fname, backup / fname)
        save_record(rec_dir, rec)
        redis_cfg = redis_runtime.prepare(f.cfg.get('redis_runtime_root') or redis_runtime.DEFAULT_ROOT,
                                          uuid.uuid4().hex[:8])
        cfg = child_config(f, slot, task_id, workers, rec_dir, sid, None, redis_cfg, name, named=named)
        child_r = fleet.start_redis(cfg)
        if not service:  # a service sub-fleet has no task: it answers requests until it is taken down
            hours = 16
            deadline = float(f.task().get('deadline') or 0)
            if deadline:
                hours = max((deadline - time.time()) / 3600, 0.05)
            bus = runtime_v2.bus_for(cfg, child_r)
            spec = {'goal': goal_text(goal, task_id, deliver, proposal, f.label(slot)), 'hours': hours}
            if contracts:  # spec §3.0: a sub-fleet of a contract task runs P0 / P1 first as well
                spec['contracts'] = True
            runtime_v2.post_event(bus, {'kind': 'task', 'request_id': 'subfleet-' + task_id, 'spec': spec},
                                  'subfleet-' + task_id)
        child_r.close(); child_r.connection_pool.disconnect()
        # 2. the parent's tab for the slot, then stop delivering to it (hooks now route to the child)
        rec['placeholder'], released = parent_attach_view(f, slot, sid, task_id, ws_title)
        rec['view'] = int(bool(released.get('tmux_session')))
        f.setf(slot, state='delegated', pane=rec['placeholder'] or sid, delegated_pane=sid, delegated_task=task_id,
               delegate_cfg=str(rec_dir / 'config.json'), bridge_available=0)
        cfg['adopt']['info'] = released
        fleet.save(rec_dir / 'config.json', cfg)
        child_mgr._save_meta({'workspace': None, 'control_workspace': None,
                              'surfaces': {} if rec['view'] else {sid: released}})
        result = (launch or default_launch)(rec_dir / 'config.json', cfg, child_mgr)
        if not result.success:
            raise RuntimeError('child daemon did not start: ' + str(result.error))
    except (Exception, SystemExit) as exc:
        rollback(f, rec_dir, rec, released, cfg)
        raise Refused('sub-fleet ' + task_id + ' could not start: ' + str(exc)) from exc
    rec['state'] = 'running'
    save_record(rec_dir, rec)
    f.refresh_history()  # the sub-fleet list on the parent's page
    f.log('subfleet.spawned', task=task_id, slot=slot, run=str(rec_dir), workers=workers, title=ws_title)
    return rec


def rollback(f, rec_dir, rec, released, cfg):
    """Undo a half-started spawn: the worker gets its pane back, the child Redis goes down."""
    fleet = _fleet()
    if cfg and cfg.get('redis_socket'):
        shutdown_redis(cfg)
    fleet.manager_for({'session': session_name(rec['task_id']), 'cmux_state_dir': f.cfg.get('cmux_state_dir')}
                      ).release(rec['pane'])
    if released is not None and not rec.get('view'):
        f.mgr._tag(rec['pane'], **{k: v for k, v in released.items() if isinstance(v, str)})
    f.setf(rec['slot'], state='idle', pane=rec['pane'], delegated_pane='', delegated_task='', delegate_cfg='')
    restore_role(f, rec)
    if rec.get('placeholder'):
        f.mgr.kill_pane(rec['placeholder'])
    rec['state'] = 'failed'
    rec['reason'] = 'start failed'
    if Path(rec_dir).is_dir():
        save_record(rec_dir, rec)


def archive_run(rec_dir):
    """Move everything of a named sub-fleet's last run into ``archive/<date>/<time>/`` (§29 N6)."""
    stamp = time.strftime('%Y-%m-%d/%H%M%S')
    dest = Path(rec_dir) / 'archive' / stamp
    n = 1
    while dest.exists():
        n += 1
        dest = Path(rec_dir) / 'archive' / (stamp + '-' + str(n))
    dest.mkdir(parents=True)
    for item in Path(rec_dir).iterdir():
        if item.name != 'archive':
            shutil.move(str(item), str(dest / item.name))


def restore_role(f, rec):
    fleet = _fleet()
    cwd = fleet.agent_cwd(f.cfg, rec['slot'])
    backup = Path(rec['run']) / 'handover'
    for fname in ROLE_FILES:
        if (backup / fname).exists():
            shutil.copy2(backup / fname, cwd / fname)


# ------------------------------------------------------------------ child side: adopt the pane
def adopted(cfg, slot='supervisor', generation=None):
    if slot != 'supervisor' or not cfg.get('adopt'):
        return False
    return generation is None or int(generation) == 0


def start_adopted(f):
    """Child daemon first boot: spawn the workers, take over the delegated pane top-left, ask for READY."""
    fleet = _fleet()
    from cmux_fleet import history
    adopt = f.cfg['adopt']
    sid = adopt['pane']
    for name in f.workers:
        f.init_worker(name)
    f.setf('supervisor', label=fleet.SUPERVISOR_LABEL)
    for name in f.workers:
        f.spawn_ready(name)
    cwd = fleet.agent_cwd(f.cfg, 'supervisor')
    scope = f.role_scope('supervisor', f.runtime_cfg('supervisor'), {})
    if f.cfg.get('supervisor_role'):  # a named sub-fleet (§29): the entry's supervisor role file
        scope += '\n## 4. Your role (supervisor)\n\n' + Path(f.cfg['supervisor_role']).read_text()
    (cwd / 'AGENTS.md').write_text(scope)
    if fleet.is_claude(f.cfg, 'supervisor'):
        (cwd / 'CLAUDE.md').write_text(scope)
    f.setf('supervisor', role='supervisor', generation=0, state='starting', thread='', transcript='',
           ctx_used=0, ctx_window=0, msg_id='', msg_done='', busy_since=time.time(), delivered=0, retry_at=0,
           last_msg='', empty_tries=0, launch_ready=1, runtime_ready=0, current_profile='', idle_confirmed=0,
           pane=sid, bridge_available=0, startup_deadline=time.time() + float(f.cfg['profile_ready_timeout']))
    first = f.agent(f.workers[0])['pane']
    sid = attach_child_view(f, sid, first, adopt.get('info') or {})
    if not sid:
        f.setf('supervisor', state='dead', launch_ready=0)
        f.log('subfleet.adopt_failed', pane=sid)
        return f.finish('blocked', 'could not take over the delegated pane of ' + adopt['slot'])
    f.setf('supervisor', pane=sid)
    f.mgr.rename(sid, 'fx-' + f.cfg.get('tab_prefix', '') + fleet.SUPERVISOR_LABEL)
    history.write_page(f.run_dir, f.history_slots(), f.history_links())
    f.history_pane = f.mgr.arrange(sid, [f.agent(w)['pane'] for w in f.workers], history.page_url(f.run_dir))
    f.r.hset(fleet.SERVICE, mapping={'history_pane': f.history_pane or ''})
    if adopt.get('parent_workspace') and hasattr(f.mgr, 'place_after'):
        f.mgr.place_after(sibling_anchor(f.cfg))
    prompt = ('Your role changed. You are now ' + fleet.SUPERVISOR_LABEL + ' of sub-fleet ' + f.cfg['task_id']
              + ' (workers: ' + ', '.join(f.workers) + '). The files AGENTS.md / CLAUDE.md in your current '
              'directory were replaced with the supervisor rules: read AGENTS.md now and follow it from now on; '
              'your earlier worker rules no longer apply. '
              + ('Requests are delivered to you next. ' if f.service else 'The daemon sends the task next. ')
              + 'In this turn do no task work: your final reply must be exactly READY.')
    if codex_queue.is_codex(f.cfg, 'supervisor'):
        try:  # no typing: the adopted Codex session takes the role change from its queue
            codex_queue.deliver(f, 'supervisor', 'adopt', prompt, thread=adopt.get('thread') or '')
        except codex_queue.QueueError as exc:
            f.setf('supervisor', state='dead', launch_ready=0)
            return f.finish('blocked', 'could not queue the role change into the delegated pane: ' + str(exc)[:200])
    elif not f.mgr.send_prompt_to_idle(sid, prompt, bracketed=fleet.is_claude(f.cfg, 'supervisor')):
        f.setf('supervisor', state='dead', launch_ready=0)
        return f.finish('blocked', 'could not type the role change into the delegated pane')
    f.log('subfleet.adopted', pane=sid, slot=adopt['slot'], task=f.cfg['task_id'])


def sibling_anchor(cfg):
    """Sidebar order is the tree's pre-order: after the parent's workspace and its earlier children."""
    fleet = _fleet()
    parent = fleet.load(cfg['parent_config'])
    anchor = cfg['adopt'].get('parent_workspace')
    for d in children_dirs(parent['run']):
        if Path(d) == Path(cfg['run']):
            break
        if record(d).get('state') in ACTIVE:
            try:
                ws = fleet.manager_for(fleet.load(Path(d) / 'config.json')).workspace_id()
            except (OSError, ValueError):
                ws = None
            anchor = ws or anchor
    return anchor


def hand_back(f):
    """Child explicit stop: release the adopted pane (never kill it) for the parent to take back."""
    sid = f.agent('supervisor').get('pane')
    info = detach_child_view(f, sid)
    if info is None:
        return False
    if info.get('tmux_session'):  # the parent never gave its own tab away
        sid = f.cfg['adopt']['pane']
    _fleet().save(f.run_dir / 'handback.json', {'pane': sid, 'info': info, 'at': time.time()})
    f.log('subfleet.handed_back', pane=sid)
    return True


# ------------------------------------------------------------------ hooks
def delegated_route(cfg, name, generation):
    """Re-target a turn hook of a delegated worker pane to the sub-fleet that adopted it."""
    fleet = _fleet()
    for _ in range(8):  # bounded: max_depth is small
        try:
            r = fleet.connect(cfg)
        except Exception:
            return cfg, name, generation
        try:
            info = r.hgetall(fleet.key('agent', name))
        finally:
            r.close(); r.connection_pool.disconnect()
        if (info.get('state') != 'delegated' or not info.get('delegate_cfg')
                or int(info.get('generation', -1)) != int(generation)):
            return cfg, name, generation
        cfg, name, generation = fleet.load(info['delegate_cfg']), 'supervisor', 0
    return cfg, name, generation


# ------------------------------------------------------------------ parent tick: completion / return
def child_result(child_run):
    try:
        return _fleet().load(Path(child_run) / 'result.json')
    except (OSError, ValueError):
        return None


def redis_up(cfg):
    """Cheap check before connecting: a stopped private Redis removes its socket (avoids client retry backoff)."""
    return Path(cfg['redis_socket']).exists()


def shutdown_redis(cfg):
    if not redis_up(cfg):
        return
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    from cmux_fleet import redis_runtime
    # No client retry: SHUTDOWN closes the socket, and retry backoff would stall the parent daemon.
    client = redis.Redis(unix_socket_path=str(redis_runtime.validate_socket(cfg)), socket_timeout=5,
                         socket_connect_timeout=3, retry=Retry(NoBackoff(), 0))
    try:
        client.shutdown()
    except Exception:
        pass  # redis closes the socket while acknowledging shutdown
    finally:
        client.close()


def post_stop(child_cfg):
    fleet = _fleet()
    from cmux_fleet import runtime_v2
    if not redis_up(child_cfg):
        return False
    try:
        r = fleet.connect(child_cfg)
    except Exception:
        return False
    try:
        rid = 'stop-' + uuid.uuid4().hex[:8]
        runtime_v2.post_event(runtime_v2.bus_for(child_cfg, r), {'kind': 'stop', 'request_id': rid}, rid)
        return True
    finally:
        r.close(); r.connection_pool.disconnect()


def tick(f):
    """Parent daemon, every loop: start requested sub-fleets, collect finished ones."""
    if not f.v2:
        return
    process_requests(f)
    from cmux_fleet import named_subfleet
    named_subfleet.tick(f)  # §29: external up/down, autostart, drains, paused mirror
    fleet = _fleet()
    running = f.running()
    for d in active_children(f.run_dir):
        rec = record(d)
        cfg = child_config_or_fail(f, d, rec)
        if cfg is None:
            continue
        if rec['state'] == 'running':
            res = child_result(d)
            alive = fleet.daemon_running(d) or time.time() - float(rec.get('launched') or rec.get('created') or 0) < LAUNCH_GRACE
            if res is None and running and alive:
                continue
            if res is None and running and rec.get('named') and named_subfleet.revive(f, d, rec, cfg):
                continue  # §29: an autostart sub-fleet's daemon died: relaunched (crashloop: collected below)
            rec['outcome'] = (res or {}).get('status') or rec.get('outcome') or 'cancelled'  # §29: crashloop kept
            rec['reason'] = (res or {}).get('reason') or (
                'main task ended while the sub-fleet was running' if not running
                else 'the sub-fleet daemon exited without writing result.json')
            rec['state'] = 'stopping'
            save_record(d, rec)
            f.refresh_history()
            post_stop(cfg)  # a crashed child (Redis gone) is collected below all the same
            f.log('subfleet.stopping', task=rec['task_id'], outcome=rec['outcome'])
        if rec['state'] == 'stopping' and not fleet.daemon_running(d):
            collect(f, d, notify=running)


def delegated_lost(f, slot, why):
    """Liveness proved the delegated session of ``slot`` gone (its tmux session ended): take its running
    sub-fleet down like a planned down / a failed one (the child daemon stops, the next tick collects it and
    starts the slot's next generation; an autostart child comes up again from there). True: stop posted."""
    for d in active_children(f.run_dir):
        rec = record(d)
        if rec.get('slot') != slot or rec.get('state') != 'running':
            continue
        cfg = child_config_or_fail(f, d, rec)
        if cfg is None:
            continue
        if rec.get('named'):
            from cmux_fleet import named_subfleet
            named_subfleet.stop_child(f, d, rec, cfg, 'down (' + why + ')')
        else:
            rec.update(state='stopping', outcome='failed', reason=why)
            save_record(d, rec)
            post_stop(cfg)
            f.log('subfleet.stopping', task=rec['task_id'], outcome='failed', reason=why)
        f.refresh_history()
        return True
    return False


def repoint(f, slot, old, new):
    """Liveness reattached the closed tab ``old`` of a delegated tmux slot as ``new``: the return looks there."""
    for d in active_children(f.run_dir):
        rec = record(d)
        if rec.get('slot') == slot and rec.get('pane') == old:
            rec['pane'] = new
            save_record(d, rec)


def child_config_or_fail(f, d, rec):
    """A record without config.json is a spawn killed half-way (the spawn itself is synchronous):
    mark it failed instead of crashing every tick / stop / resume."""
    try:
        return _fleet().load(Path(d) / 'config.json')
    except (OSError, ValueError) as exc:
        rec.update(state='failed', reason='sub-fleet record without a readable config.json: ' + str(exc))
        save_record(d, rec)
        f.log('subfleet.failed', task=rec.get('task_id'), reason=rec['reason'])
        return None


def collect(f, child_run, notify=True):
    """The child daemon has exited: take the pane back (or respawn the slot), close the child's tabs."""
    fleet = _fleet()
    rec = record(child_run)
    cfg = fleet.load(Path(child_run) / 'config.json')
    slot = rec['slot']
    child_mgr = fleet.manager_for(cfg)
    back = return_facts(f, rec, cfg) if rec.get('named') else None  # read before its Redis goes
    shutdown_redis(cfg)  # no orphan Redis even if the child crashed
    ph = rec['placeholder']
    returned = parent_detach_view(f, rec, child_mgr, child_run)
    restore_role(f, rec)
    child_mgr.close_all()
    child_mgr.meta_path.unlink(missing_ok=True)
    f.setf(slot, delegated_pane='', delegated_task='', delegate_cfg='')
    # A Claude worker on the hook channel is never typed into after its launch prompt: instead of the
    # role-change prompt, its next generation takes the returned session's place (CLI-argument prompt).
    hooked = returned and f.v2 is not None and f.v2.hooked(slot)
    if back and hooked:
        # §29 (user 10-14): the returned session writes a handoff note (design §4 rotation on its own hook
        # channel: the delivery waits in its mailbox, the session's rewake fallback hands it over), then the
        # next generation of the slot reads it. Untaken / timed out: the daemon's skeleton note instead.
        f.mgr.kill_pane(ph)
        f.setf(slot, pane=rec['pane'], state='idle', subfleet_return=json.dumps(back, ensure_ascii=False))
        f.start_rotation(slot, 0, 0, prompt=handoff.SUBFLEET_RETURN_PROMPT.replace('<name>', rec['name']))
        f.log('subfleet.return_handoff', task=rec['task_id'], slot=slot, generation=f.agent(slot).get('generation'))
    elif back and not returned and f.v2 is not None:
        # §29: the delegated session is gone, nobody can write a note: the skeleton note, then the next generation
        f.setf(slot, pane=ph or rec['pane'], state='idle', subfleet_return=json.dumps(back, ensure_ascii=False),
               handoff_id=uuid.uuid4().hex[:10])
        note = f.run_dir / 'handoffs' / (slot + '-g' + str(f.agent(slot).get('generation') or 0) + '-return.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f.skeleton(slot, f.agent(slot)))
        f.setf(slot, handoff_id='', subfleet_return='')
        f.v2.respawn([slot], True, prompt=return_prompt(rec['name'], str(note)), handoff=str(note))
        f.log('subfleet.slot_respawned', task=rec['task_id'], slot=slot, handoff=str(note))
    elif hooked:
        f.mgr.kill_pane(ph)
        f.setf(slot, pane=rec['pane'], state='idle')
        f.v2.respawn([slot], True)
        f.log('subfleet.slot_respawned', task=rec['task_id'], slot=slot, returned=True)
    elif returned:
        # The adopted session may still be mid-turn (parent task ended, stop): abort that turn so its
        # late Stop cannot settle as the READY of the return. Second place Escape counts as accepted stop.
        f.mgr.send_key(rec['pane'], 'Escape')
        f.setf(slot, pane=rec['pane'], state='starting', launch_ready=1, runtime_ready=0, bridge_available=0,
               startup_deadline=time.time() + float(f.cfg['profile_ready_timeout']), busy_since=time.time())
        # Always told (also on a stop): the session still believes it is a supervisor until then.
        prompt = ('Sub-fleet ' + rec['task_id'] + ' has finished (' + rec.get('outcome', '') + '). Your role '
                  'changed back: you are fleet agent ' + f.label(slot) + ', a worker of the main fleet again. '
                  'AGENTS.md / CLAUDE.md in your current directory were restored to the worker rules: read '
                  'AGENTS.md now and follow it. Do no task work in this turn: your final reply must be exactly READY.')
        if codex_queue.is_codex(f.runtime_cfg(slot), slot):
            try:  # no typing: the returning Codex session takes it from its queue
                codex_queue.deliver(f, slot, 'restore', prompt)
            except codex_queue.QueueError as exc:
                f.setf(slot, state='dead', launch_ready=0)
                f.log('subfleet.return_failed', task=rec['task_id'], slot=slot, error=str(exc)[:200])
        elif not f.mgr.send_prompt_to_idle(rec['pane'], prompt, bracketed=fleet.is_claude(f.runtime_cfg(slot), slot)):
            f.setf(slot, state='dead', launch_ready=0)
            f.log('subfleet.return_failed', task=rec['task_id'], slot=slot)
    else:
        # The original session is gone (or could not be moved back): next generation in the placeholder's place.
        f.setf(slot, pane=ph or rec['pane'], state='idle')
        f.v2.respawn([slot], True)
        f.log('subfleet.slot_respawned', task=rec['task_id'], slot=slot)
    ok = rec.get('outcome') == 'completed'
    if ok:  # before the checks below (also on stop/resume collects); never blocks the pane's return
        try:
            deliver_back(f, rec, cfg)
        except (OSError, ValueError) as exc:
            f.log('subfleet.deliver_failed', task=rec['task_id'], reason=str(exc))
    missing = rec.get('output') and not (Path(rec['output']).is_file() and Path(rec['output']).stat().st_size > 0)
    if ok and missing:
        ok, rec['reason'] = False, 'completed but the deliverable ' + rec['output'] + ' is missing or empty'
    rec['state'] = 'returned' if ok else 'failed'
    if rec.get('named'):  # §29: taken down on purpose is not a failure; the parent hash says down
        from cmux_fleet import named_subfleet
        rec['state'] = 'down' if rec.get('outcome') in named_subfleet.DOWN_OUTCOMES else 'failed'
    rec['finished'] = time.time()
    save_record(child_run, rec)
    f.refresh_history()
    f.log('subfleet.' + rec['state'], task=rec['task_id'], slot=slot, outcome=rec.get('outcome'),
          reason=rec.get('reason', ''), attempt=rec.get('attempt'))
    if rec.get('named'):
        named_subfleet.collected(f, rec, returned)
        return rec  # never a "[Sub-fleet … failed]" notice: the parent supervisor does not own it
    if notify and f.running():
        extra = f.escalation_finished(rec, ok)
        f.queue('supervisor', '[Sub-fleet ' + rec['task_id'] + ' ' + ('completed' if ok else 'failed') + '] '
                + f.label(slot) + ' ran sub-fleet ' + rec['task_id'] + ' (attempt ' + str(rec.get('attempt', 1))
                + '): ' + (rec.get('reason') or rec.get('outcome') or '') + '. ' + f.label(slot)
                + ' is a worker again' + ('' if returned and not hooked else ' (new generation: the delegated session was '
                                          + ('retired' if hooked else 'lost') + ')')
                + '.' + extra + '\nContinue the current task using the AGENTS.md decision JSON.')
    return rec


def return_facts(f, rec, cfg):
    """What the next generation of a named sub-fleet's slot learns (its handoff skeleton, §29): up / down
    times, the requests the sub-fleet answered and those still open when it went down."""
    from cmux_fleet import named_subfleet
    h = named_subfleet.state(f, rec['name'])
    out = {'name': rec['name'], 'up_at': h.get('up_at') or h.get('started_at') or '', 'down_at': time.time(),
           'outcome': rec.get('outcome') or '', 'at': time.time()}
    # Still up (a crash, a parent stop): read it now; a planned down read it before its stop (stop_child)
    out.update(child_requests(cfg) if redis_up(cfg) else rec.get('requests') or {'answered': [], 'open': []})
    return out


def child_requests(cfg):
    """{answered: [[id, status]], open: [id]} of a running child's Redis (newest RETURN_IDS each)."""
    fleet = _fleet()
    out = {'answered': [], 'open': []}
    try:
        r = fleet.connect(cfg)
        try:
            for raw in r.lrange(fleet.RESULTS, -RETURN_IDS, -1):
                res = json.loads(raw)
                out['answered'].append([str(res.get('id')), str(res.get('status'))])
            out['open'] = sorted(k[len(fleet.REQ):] for k in r.scan_iter(match=fleet.REQ + '*'))[:RETURN_IDS]
        finally:
            r.close()
    except Exception as exc:  # unreadable: the note says so, the return goes on
        out['error'] = str(exc)[:200]
    return out


RETURN_IDS = 30  # request ids listed in a return note (newest answered / open)


def return_lines(ret):
    """(done lines, pending text) of a §29 return note."""
    def when(x):
        try:
            return time.strftime('%Y-%m-%d %H:%M', time.localtime(float(x)))
        except (TypeError, ValueError):
            return '?'
    done = ['this slot supervised named sub-fleet ' + ret['name'] + ' from ' + when(ret.get('up_at')) + ' to '
            + when(ret.get('down_at')) + ' (outcome ' + (ret.get('outcome') or '?') + '); it is down now',
            'requests the sub-fleet answered (newest ' + str(RETURN_IDS) + '): '
            + (', '.join(i + ' ' + st for i, st in ret.get('answered') or []) or 'none')]
    if ret.get('error'):
        done.append('the sub-fleet\'s Redis could not be read at down: ' + ret['error'])
    still = ret.get('open') or []
    return done, ('sub-fleet requests still unanswered at down: ' + ', '.join(still)) if still else 'none'


def return_prompt(name, note):
    """Launch prompt of the slot's next generation after a §29 return (never "resumed after an interruption")."""
    return ('You are fleet agent {label}, a worker of the main fleet. The previous session of this slot supervised '
            'sub-fleet ' + name + ', which is now down; that role is over. Read the handoff note ' + note
            + ' and AGENTS.md in the current directory. In this turn only read them, do no task work, and make '
            'your final reply exactly READY.')


# ------------------------------------------------------------------ tree stop / resume / status
def stop_children(f, wait=STOP_WAIT):
    """Stop every active child (each stops its own children first), then take the panes back."""
    fleet = _fleet()
    ok = True
    for d in active_children(f.run_dir):
        rec = record(d)
        cfg = child_config_or_fail(f, d, rec)
        if cfg is None:
            continue
        if rec['state'] != 'stopping':
            rec.update(state='stopping', outcome=rec.get('outcome') or ('down' if rec.get('named') else 'stopped'),
                       reason='main fleet stopped')
            save_record(d, rec)
            post_stop(cfg)
        deadline = time.monotonic() + wait
        while fleet.daemon_running(d) and time.monotonic() < deadline:
            time.sleep(.5)
        if fleet.daemon_running(d):
            ok = False
            f.log('subfleet.stop_unconfirmed', task=rec['task_id'])
            continue
        collect(f, d, notify=False)
    return ok


def resume_children(f, launch=None):
    """Root --resume: children whose daemon is gone are relaunched with --resume (their Redis restarted)."""
    fleet = _fleet()
    for d in active_children(f.run_dir):
        rec = record(d)
        if fleet.daemon_running(d):
            continue
        cfg = child_config_or_fail(f, d, rec)
        if cfg is None:
            continue
        if rec['state'] == 'stopping':
            collect(f, d)
            continue
        if rec.get('named') and not rec.get('autostart'):
            # §29 N5: an externally started sub-fleet waits for the next external up, never a stale "up"
            rec.update(state='stopping', outcome='down', reason='parent fleet resumed')
            save_record(d, rec)
            collect(f, d, notify=False)
            continue
        if not redis_up(cfg):
            fleet.start_redis(cfg, tries=300)  # reloads its AOF from the same directory
        mgr = fleet.manager_for(cfg)
        if launch:
            launch(d / 'config.json', cfg, mgr)
        else:
            command = [cfg['python'], str(Path(fleet.__file__).resolve()), 'daemon', str(d / 'config.json'), '--resume']
            mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(d))
        rec['launched'] = time.time()
        save_record(d, rec)
        f.log('subfleet.resumed', task=rec['task_id'])


def tree(cfg):
    """Status of every sub-fleet under this run, recursively (read-only; no daemon contact)."""
    fleet = _fleet()
    out = {}
    for d in children_dirs(cfg['run']):
        rec = record(d)
        try:
            child = fleet.load(d / 'config.json')
        except (OSError, ValueError):
            child = None
        item = {k: rec.get(k) for k in ('state', 'slot', 'outcome', 'reason', 'attempt', 'title', 'workers')}
        if rec.get('named'):
            item.update(name=rec.get('name'), mode=rec.get('mode'), autostart=rec.get('autostart'))
        item.update(run=str(d), daemon_alive=fleet.daemon_running(d), result=child_result(d))
        if child:
            item['subfleets'] = tree(child)
        out[rec.get('task_id', d.name) + ('' if d.name == rec.get('task_id') else ' (' + d.name + ')')] = item
    return out


def find_run(root_cfg, task_id):
    """Run directory of the active sub-fleet ``task_id`` anywhere in the tree (None: not found)."""
    if not task_id:
        return Path(root_cfg['run'])
    for d in active_children(root_cfg['run']):
        if record(d).get('task_id') == task_id:
            return d
        found = find_run(_fleet().load(d / 'config.json'), task_id)
        if found:
            return found
    return None


def cli_spawn(root_cfg, args, wait=60):
    """Operator/test entry: queue a spawn on the owning fleet and wait for its daemon's answer."""
    parent_id = args.task_id.rsplit('.', 1)[0] if '.' in args.task_id else None
    run = find_run(root_cfg, parent_id)
    if run is None:
        return {'status': 'rejected', 'reason': 'no running sub-fleet ' + parent_id}
    rid = request(run, args.slot, args.task_id, args.goal, args.workers, output=args.output)
    path = answer_path(run, rid)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if path.exists():
            return _fleet().load(path)
        time.sleep(.2)
    return {'status': 'pending', 'request': rid, 'answer_file': str(path)}


if __name__ == '__main__':  # pragma: no cover
    sys.exit('use: cmux-fleet subfleet spawn ...')
