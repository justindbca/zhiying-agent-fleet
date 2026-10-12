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
  check(fleet, slot, task_id, workers) -> None | reason
  request(run_dir, slot, task_id, goal, workers, output=None, proposal=None) -> request id
  spawn_child(fleet, slot, task_id, goal, workers, output=None, proposal=None, launch=None) -> record
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

LIMITS = {'max_depth': 2, 'max_children': 3, 'max_agents_total': 12, 'child_workers_max': 3}
ACTIVE = ('starting', 'running', 'stopping')
TITLE_MAX = 60
SEP = ' › '
STOP_WAIT = 180  # seconds a tree stop waits for one child daemon to exit
JUMP_PARAM = 'fleet-jump'  # query marker a page link click adds (history.py); a plain reload has none
JUMP_POLL = 1.0  # seconds between two reads of the history pane URL
JUMP_MAX_AGE = 120  # an older click (daemon was down) only puts the page back, it never moves the view


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
    hash_ = f.r.hget(fleet.SERVICE, 'history_hash')
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
    from cmux_fleet import roster
    root = root_config(cfg)
    out = dict(LIMITS, **{k: v for k, v in (root.get('limits') or {}).items() if k in LIMITS})
    if root.get('roster'):
        try:
            out.update(roster.load_limits(root['roster']))
        except (OSError, ValueError):
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
def check(f, slot, task_id, workers):
    """None when this sub-fleet may start now, else the reason (refusals are enforced by program)."""
    fleet = _fleet()
    lim = limits(f.cfg)
    if not valid_child_id(f.cfg.get('task_id'), task_id):
        want = (f.cfg['task_id'] + '.<n>') if f.cfg.get('task_id') else 'M<n>'
        return 'task id ' + repr(task_id) + ' must be ' + want + ' in this fleet'
    if task_level(task_id) > lim['max_depth']:
        return ('max_depth ' + str(lim['max_depth']) + ': ' + task_id + ' would be level '
                + str(task_level(task_id)) + ' (main fleet is level 1)')
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        return 'workers must be a positive integer'
    if workers > lim['child_workers_max']:
        return 'child_workers_max ' + str(lim['child_workers_max']) + ': asked for ' + str(workers) + ' workers'
    live = active_children(f.run_dir)
    if any(record(d).get('task_id') == task_id for d in live):
        return 'sub-fleet ' + task_id + ' is already running'
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
    cli = fleet.agent_cli(f.runtime_cfg(slot), slot)
    if cli == 'pi':
        return ('pi worker panes cannot be handed over: the Pi bridge is bound to this fleet\'s Redis '
                'socket and never reconnects')
    if cli != 'claude' or not f.v2:
        return 'only official Claude Code worker panes of a v2 fleet can become sub-fleet supervisors'
    info = f.agent(slot)
    if info.get('state') != 'idle' or info.get('runtime_ready') != '1':
        return slot + ' must be idle and READY (state ' + (info.get('state') or '?') + ')'
    if f.r.hget(f.v2.bus.lease_key(slot), 'current_delivery'):
        return slot + ' still has a delivery in flight'
    if not info.get('pane') or f.mgr.get_pane_status(info['pane']) is None:
        return slot + ' has no live tab to hand over'
    return None


# ------------------------------------------------------------------ requests (transaction-safe)
def request(run_dir, slot, task_id, goal, workers, output=None, proposal=None):
    """Queue a spawn for the fleet's next tick. Writes one file; no other side effect."""
    rid = 'sf-' + uuid.uuid4().hex[:12]
    folder = Path(run_dir) / 'subfleet-requests'
    folder.mkdir(parents=True, exist_ok=True)
    _fleet().save(folder / (rid + '.json'), {'id': rid, 'slot': slot, 'task_id': task_id, 'goal': goal,
                                             'workers': workers, 'output': output, 'proposal': proposal})
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
                              output=spec.get('output'), proposal=spec.get('proposal'))
            answer = {'status': 'accepted', 'run': rec['run'], 'task_id': rec['task_id']}
        except Refused as exc:
            answer = {'status': 'rejected', 'reason': str(exc)}
        except (KeyError, ValueError, OSError) as exc:
            answer = {'status': 'rejected', 'reason': 'bad request: ' + str(exc)}
        fleet.save(answer_path(f.run_dir, path.stem), answer)
        path.unlink(missing_ok=True)
        f.log('subfleet.request', request=path.stem, **answer)


# ------------------------------------------------------------------ spawn
def child_config(f, slot, task_id, workers, rec_dir, sid, info, redis_cfg, name):
    fleet = _fleet()
    parent = f.cfg
    root_path = parent.get('root_config') or str(f.path)
    path = list(parent.get('title_path') or [name]) + [slot, task_id]
    cfg = {k: v for k, v in parent.items() if k not in ('roster', 'worker_roles', 'roles', 'adopt',
                                                          'redis_socket', 'redis_runtime_dir')}
    cfg.update(redis_cfg)
    cfg.update(run=str(rec_dir), workers=['worker-' + str(i + 1) for i in range(workers)], mode='task',
               session=session_name(task_id), task_id=task_id, parent_config=str(f.path),
               root_config=root_path, root_token=token(parent), name=name, title_path=path,
               agents_title=title(path), control_title='Fleet control · ' + task_id, tab_prefix=task_id + '/',
               supervisor_cli=fleet.agent_cli(f.runtime_cfg(slot), slot), supervisor_cwd=str(fleet.agent_cwd(parent, slot)),
               adopt={'pane': sid, 'slot': slot, 'generation': int(f.agent(slot).get('generation') or 0),
                      'info': info, 'parent_workspace': f.mgr.workspace_id() if hasattr(f.mgr, 'workspace_id') else None})
    return cfg


def goal_text(goal, task_id, output, proposal, parent_label):
    text = ('Sub-fleet ' + task_id + ' (delegated by ' + parent_label + '). Original instruction:\n' + goal)
    if proposal:
        text += '\nApproved split (dispatch these to your workers): ' + json.dumps(proposal, ensure_ascii=False)
    if output:
        text += ('\nThe final deliverable of ' + task_id + ' must be written to ' + str(output)
                 + ' (absolute path, outside your own workspace). Dispatch a worker to write it; '
                 'verify it exists before COMPLETED.')
    return text


def default_launch(child_cfg_path, child_cfg, mgr):
    fleet = _fleet()
    command = [child_cfg['python'], str(Path(fleet.__file__).resolve()), 'daemon', str(child_cfg_path)]
    caffeinate = shutil.which('caffeinate')
    if caffeinate:
        command = [caffeinate, '-i'] + command
    return mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'},
                            start_directory=str(child_cfg['run']))


def spawn_child(f, slot, task_id, goal, workers, output=None, proposal=None, launch=None):
    """Start sub-fleet ``task_id`` with ``slot``'s live pane as its supervisor. Outside transactions only."""
    fleet = _fleet()
    from cmux_fleet import redis_runtime, runtime_v2
    if not isinstance(goal, str) or not goal.strip():
        raise Refused('a sub-fleet needs a goal')
    reason = check(f, slot, task_id, workers)
    if reason:
        raise Refused(reason)
    base = f.run_dir / 'sub'
    rec_dir = base / task_id
    attempt = 1
    while rec_dir.exists():
        attempt += 1
        rec_dir = base / (task_id + '-attempt-' + str(attempt))
    name = root_name(f.cfg, '' if f.cfg.get('adopt') else f.task().get('goal', ''))
    path = list(f.cfg.get('title_path') or [name]) + [slot, task_id]
    ws_title = title(path)
    child_mgr = fleet.manager_for({'session': session_name(task_id), 'cmux_state_dir': f.cfg.get('cmux_state_dir'),
                                   'agents_title': ws_title, 'control_title': 'Fleet control · ' + task_id})
    if child_mgr.session_exists():
        raise Refused('tabs of an earlier sub-fleet ' + task_id + ' are still open; close them first')
    info_now = f.agent(slot)
    sid = info_now['pane']
    # 1. placeholder in the parent, in the worker's place
    # spawn_window execs the command: keep the tab's process alive (an exited tab reads as a closed runtime)
    show = 'printf "%s\\n" ' + shlex.quote(placeholder_label(slot, task_id, ws_title)) + '; exec sleep 2147483647'
    ph = f.mgr.spawn_window('placeholder-' + slot, '/bin/bash -c ' + shlex.quote(show),
                            metadata={'type': 'placeholder'}, start_directory=str(f.run_dir))
    if not ph.success or not f.mgr.tab_into(ph.pane_id, sid):
        if ph.pane_id:
            f.mgr.kill_pane(ph.pane_id)
        raise Refused('could not open the placeholder tab for ' + slot)
    f.mgr.rename(ph.pane_id, placeholder_label(slot, task_id, ws_title))
    rec = {'task_id': task_id, 'slot': slot, 'run': str(rec_dir), 'state': 'starting', 'attempt': attempt,
           'workers': workers, 'output': output, 'created': time.time(), 'title': ws_title,
           'placeholder': ph.pane_id, 'pane': sid, 'generation': int(info_now.get('generation') or 0)}
    cfg = released = None
    try:  # anything below fails -> rollback; SystemExit too (start_redis), never the parent daemon
        # 2. child run directory, config, private Redis, task
        (rec_dir / 'workspace').mkdir(parents=True)
        backup = rec_dir / 'handover'
        backup.mkdir()
        cwd = fleet.agent_cwd(f.cfg, slot)
        for fname in ('AGENTS.md', 'CLAUDE.md'):
            if (cwd / fname).exists():
                shutil.copy2(cwd / fname, backup / fname)
        save_record(rec_dir, rec)
        redis_cfg = redis_runtime.prepare(f.cfg.get('redis_runtime_root') or redis_runtime.DEFAULT_ROOT,
                                          uuid.uuid4().hex[:8])
        cfg = child_config(f, slot, task_id, workers, rec_dir, sid, None, redis_cfg, name)
        child_r = fleet.start_redis(cfg)
        hours = 16
        deadline = float(f.task().get('deadline') or 0)
        if deadline:
            hours = max((deadline - time.time()) / 3600, 0.05)
        bus = runtime_v2.bus_for(cfg, child_r)
        spec = {'goal': goal_text(goal, task_id, output, proposal, f.label(slot)), 'hours': hours}
        runtime_v2.post_event(bus, {'kind': 'task', 'request_id': 'subfleet-' + task_id, 'spec': spec},
                              'subfleet-' + task_id)
        child_r.close(); child_r.connection_pool.disconnect()
        # 3. stop delivering to the slot and hand the surface over (hooks now route to the child)
        f.setf(slot, state='delegated', pane=ph.pane_id, delegated_pane=sid, delegated_task=task_id,
               delegate_cfg=str(rec_dir / 'config.json'), bridge_available=0)
        released = f.mgr.release(sid) or {}
        cfg['adopt']['info'] = released
        fleet.save(rec_dir / 'config.json', cfg)
        child_mgr._save_meta({'workspace': None, 'control_workspace': None, 'surfaces': {sid: released}})
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
    if released is not None:
        f.mgr._tag(rec['pane'], **{k: v for k, v in released.items() if isinstance(v, str)})
    f.setf(rec['slot'], state='idle', pane=rec['pane'], delegated_pane='', delegated_task='', delegate_cfg='')
    restore_role(f, rec)
    f.mgr.kill_pane(rec['placeholder'])
    rec['state'] = 'failed'
    rec['reason'] = 'start failed'
    if Path(rec_dir).is_dir():
        save_record(rec_dir, rec)


def restore_role(f, rec):
    fleet = _fleet()
    cwd = fleet.agent_cwd(f.cfg, rec['slot'])
    backup = Path(rec['run']) / 'handover'
    for fname in ('AGENTS.md', 'CLAUDE.md'):
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
    (cwd / 'AGENTS.md').write_text(scope)
    if fleet.is_claude(f.cfg, 'supervisor'):
        (cwd / 'CLAUDE.md').write_text(scope)
    f.setf('supervisor', role='supervisor', generation=0, state='starting', thread='', transcript='',
           ctx_used=0, ctx_window=0, msg_id='', msg_done='', busy_since=time.time(), delivered=0, retry_at=0,
           last_msg='', empty_tries=0, launch_ready=1, runtime_ready=0, current_profile='', idle_confirmed=0,
           pane=sid, bridge_available=0, startup_deadline=time.time() + float(f.cfg['profile_ready_timeout']))
    first = f.agent(f.workers[0])['pane']
    if not f.mgr.adopt(sid, first, adopt.get('info') or {}):
        f.setf('supervisor', state='dead', launch_ready=0)
        f.log('subfleet.adopt_failed', pane=sid)
        return f.finish('blocked', 'could not take over the delegated pane of ' + adopt['slot'])
    f.mgr.rename(sid, 'fx-' + f.cfg.get('tab_prefix', '') + fleet.SUPERVISOR_LABEL)
    history.write_page(f.run_dir, f.history_slots(), f.history_links())
    f.history_pane = f.mgr.arrange(sid, [f.agent(w)['pane'] for w in f.workers], history.page_url(f.run_dir))
    f.r.hset(fleet.SERVICE, mapping={'history_pane': f.history_pane or ''})
    if adopt.get('parent_workspace') and hasattr(f.mgr, 'place_after'):
        f.mgr.place_after(sibling_anchor(f.cfg))
    prompt = ('Your role changed. You are now ' + fleet.SUPERVISOR_LABEL + ' of sub-fleet ' + f.cfg['task_id']
              + ' (workers: ' + ', '.join(f.workers) + '). The files AGENTS.md / CLAUDE.md in your current '
              'directory were replaced with the supervisor rules: read AGENTS.md now and follow it from now on; '
              'your earlier worker rules no longer apply. The daemon sends the task next. In this turn do no task '
              'work: your final reply must be exactly READY.')
    if not f.mgr.send_prompt_to_idle(sid, prompt, bracketed=fleet.is_claude(f.cfg, 'supervisor')):
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
    info = f.mgr.release(sid) if sid else None
    if info is None:
        return False
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
    fleet = _fleet()
    running = f.running()
    for d in active_children(f.run_dir):
        rec = record(d)
        cfg = fleet.load(d / 'config.json')
        if rec['state'] == 'running':
            res = child_result(d)
            if res is None and running:
                continue
            rec['outcome'] = (res or {}).get('status') or 'cancelled'
            rec['reason'] = (res or {}).get('reason') or 'main task ended while the sub-fleet was running'
            rec['state'] = 'stopping'
            save_record(d, rec)
            f.refresh_history()
            post_stop(cfg)  # a crashed child (Redis gone) is collected below all the same
            f.log('subfleet.stopping', task=rec['task_id'], outcome=rec['outcome'])
        if rec['state'] == 'stopping' and not fleet.daemon_running(d):
            collect(f, d, notify=running)


def collect(f, child_run, notify=True):
    """The child daemon has exited: take the pane back (or respawn the slot), close the child's tabs."""
    fleet = _fleet()
    rec = record(child_run)
    cfg = fleet.load(Path(child_run) / 'config.json')
    slot = rec['slot']
    child_mgr = fleet.manager_for(cfg)
    shutdown_redis(cfg)  # no orphan Redis even if the child crashed
    back = {}
    try:
        back = fleet.load(Path(child_run) / 'handback.json')
    except (OSError, ValueError):
        info = child_mgr.release(rec['pane'])  # crashed child: its registry still holds the pane
        if info is not None:
            back = {'pane': rec['pane'], 'info': info}
    ph = rec['placeholder']
    returned = bool(back) and back.get('pane') == rec['pane'] and f.mgr.adopt(rec['pane'], ph, back.get('info') or {})
    restore_role(f, rec)
    child_mgr.close_all()
    child_mgr.meta_path.unlink(missing_ok=True)
    f.setf(slot, delegated_pane='', delegated_task='', delegate_cfg='')
    if returned:
        # The adopted session may still be mid-turn (parent task ended, stop): abort that turn so its
        # late Stop cannot settle as the READY of the return. Second place Escape counts as accepted stop.
        f.mgr.send_key(rec['pane'], 'Escape')
        f.mgr.kill_pane(ph)
        f.mgr.rename(rec['pane'], 'fx-' + f.cfg.get('tab_prefix', '') + f.label(slot))
        f.setf(slot, pane=rec['pane'], state='starting', launch_ready=1, runtime_ready=0, bridge_available=0,
               startup_deadline=time.time() + float(f.cfg['profile_ready_timeout']), busy_since=time.time())
        # Always told (also on a stop): the session still believes it is a supervisor until then.
        prompt = ('Sub-fleet ' + rec['task_id'] + ' has finished (' + rec.get('outcome', '') + '). Your role '
                  'changed back: you are fleet agent ' + f.label(slot) + ', a worker of the main fleet again. '
                  'AGENTS.md / CLAUDE.md in your current directory were restored to the worker rules: read '
                  'AGENTS.md now and follow it. Do no task work in this turn: your final reply must be exactly READY.')
        if not f.mgr.send_prompt_to_idle(rec['pane'], prompt, bracketed=fleet.is_claude(f.runtime_cfg(slot), slot)):
            f.setf(slot, state='dead', launch_ready=0)
            f.log('subfleet.return_failed', task=rec['task_id'], slot=slot)
    else:
        # The original session is gone (or could not be moved back): next generation in the placeholder's place.
        f.setf(slot, pane=ph, state='idle')
        f.v2.respawn([slot], True)
        f.log('subfleet.slot_respawned', task=rec['task_id'], slot=slot)
    ok = rec.get('outcome') == 'completed'
    missing = rec.get('output') and not (Path(rec['output']).is_file() and Path(rec['output']).stat().st_size > 0)
    if ok and missing:
        ok, rec['reason'] = False, 'completed but the deliverable ' + rec['output'] + ' is missing or empty'
    rec['state'] = 'returned' if ok else 'failed'
    rec['finished'] = time.time()
    save_record(child_run, rec)
    f.refresh_history()
    f.log('subfleet.' + rec['state'], task=rec['task_id'], slot=slot, outcome=rec.get('outcome'),
          reason=rec.get('reason', ''), attempt=rec.get('attempt'))
    if notify and f.running():
        f.queue('supervisor', '[Sub-fleet ' + rec['task_id'] + ' ' + ('completed' if ok else 'failed') + '] '
                + f.label(slot) + ' ran sub-fleet ' + rec['task_id'] + ' (attempt ' + str(rec.get('attempt', 1))
                + '): ' + (rec.get('reason') or rec.get('outcome') or '') + '. ' + f.label(slot)
                + ' is a worker again' + ('' if returned else ' (new generation: the delegated session was lost)')
                + '. Continue the current task using the AGENTS.md decision JSON.')
    return rec


# ------------------------------------------------------------------ tree stop / resume / status
def stop_children(f, wait=STOP_WAIT):
    """Stop every active child (each stops its own children first), then take the panes back."""
    fleet = _fleet()
    ok = True
    for d in active_children(f.run_dir):
        rec = record(d)
        cfg = fleet.load(d / 'config.json')
        if rec['state'] != 'stopping':
            rec.update(state='stopping', outcome=rec.get('outcome') or 'stopped', reason='main fleet stopped')
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
        if rec['state'] == 'stopping':
            collect(f, d)
            continue
        cfg = fleet.load(d / 'config.json')
        if not redis_up(cfg):
            fleet.start_redis(cfg, tries=300)  # reloads its AOF from the same directory
        mgr = fleet.manager_for(cfg)
        if launch:
            launch(d / 'config.json', cfg, mgr)
        else:
            command = [cfg['python'], str(Path(fleet.__file__).resolve()), 'daemon', str(d / 'config.json'), '--resume']
            mgr.spawn_window('control', shlex.join(command), metadata={'type': 'daemon'}, start_directory=str(d))
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
