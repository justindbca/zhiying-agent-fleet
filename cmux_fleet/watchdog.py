"""Watchdog for transport v2 (docs/design.md §24): one of five states per slot, per check.

  idle       no delivery in flight and not starting: never acted on, however long (E1).
  working    a delivery is in flight and the transcript / rollout moved within stall_timeout.
  prompting  the screen shows a confirmation dialog. Whitelisted (prompt table) and verified -> save
             the screen and press its keys; anything else -> save the screen, mark the slot
             prompt_wait, pause dispatch, press nothing (the lease stays received: once a human
             answers in the pane the turn settles normally). Deletes / moves are never pressed.
  stalled    in flight, no activity for stall_timeout: Escape, at most stall_escapes times
             spread over stall_grace. Activity after that -> working again.
  frozen     stalled for stall_grace, or every Escape left the screen unchanged, or `ps` shows
             the pane's processes in uninterruptible wait on two checks in a row: save the
             screen, kill this fleet's own pane (its process tree), confirm the delivery
             stopped, and start the next generation in place from a skeleton handoff note.
             The cancelled work is never re-run automatically; the supervisor decides.

Screen capture, keys, kills and spawns are side effects: they run outside any Redis intent.
Only settlements (hold, queue, publish) go through RuntimeV2.transaction.
"""
import hashlib
import os
import re
import time
import uuid
from pathlib import Path

from cmux_fleet import advisor

STATES = ('idle', 'working', 'stalled', 'frozen', 'prompting')
DEFAULT_TABLE = Path(__file__).resolve().parent / 'config' / 'watchdog_prompts.yaml'
KEYS = frozenset(('Enter', 'Escape', 'Tab', 'Up', 'Down', 'Left', 'Right', 'Space',
                  'y', 'n', '1', '2', '3'))
# How an answered dialog's concrete operation is proven to be inside an authorisation the run already has.
VERIFY = {
    'own_cwd': 'every path on the dialog is the slot\'s own working directory (trusted at spawn)',
    'paths_in_run': 'every path on the dialog is inside the slot directory or the run workspace (--add-dir)',
}
DEFAULTS = {'stall_grace': 300, 'stall_escapes': 3, 'watch_interval': 5, 'capture_fail_alarm': 3,
            'dialog_escalation_timeout': 600}  # seconds the supervisor has to rule on a dialog it received
# The supervisor cannot rule on a worker's dialog now (plan F' 8.3 step 5): the user gets it.
SUPERVISOR_AWAY = ('held', 'dead', 'starting', 'retired', 'delegated', 'handoff', 'clearing', 'walled', 'reclaimed')
YES_CURSOR = re.compile(r'❯\s*(?:1\.\s*)?yes\b')
# Slot states the watchdog never touches: someone else owns the next step.
HANDS_OFF = ('held', 'dead', 'retired', 'delegated', 'handoff', 'clearing')


def setting(cfg, name):
    value = cfg.get(name, DEFAULTS[name])
    if type(value) not in (int, float) or value < 0:
        raise ValueError(name + ' must be a non-negative number')
    return value


# ---------------------------------------------------------------- delete classifier (plan F' 8.1)
# A dialog that may lose data is the user's call, always: never answered by the program or the supervisor.
# Strict on purpose: a non-delete dialog misread as a delete only asks the user once more; the reverse is
# not allowed. So this list only grows (watchdog_prompts.yaml `delete_markers:` adds, nothing removes), and
# a move / rename (mv) counts as a delete (user, 2026-10-14).
DELETE_MARKERS = [
    'rm', 'rmdir', 'unlink', 'del', 'delete', 'remove', 'rimraf', 'shred', 'trash', 'purge', 'wipe', 'erase',
    'truncate', 'drop table', 'drop database', 'drop schema', 'git clean', 'git reset --hard', 'git push --force',
    'git push -f', 'git branch -d', 'git checkout --', 'git restore', 'dd if=', '> /dev/null', '>/dev/null',
    'mv', 'move', 'rename', 'move-item', 'rename-item', 'remove-item',
    '删除', '清空', '移除', '清理', '移动', '改名', '重命名',
]


def _marker_re(phrase):
    """Case-insensitive, word-bounded at ASCII word edges (CJK phrases match as substrings); a single English
    word also matches its inflections (delete -> deleted / deleting, rm -> rms)."""
    word = phrase.lower()
    if re.fullmatch(r'[a-z]+', word):
        body = (re.escape(word[:-1]) + '(?:e|es|ed|ing)' if word.endswith('e') and len(word) > 2
                else re.escape(word) + '(?:s|es|ed|ing|d)?')
    else:
        body = re.escape(word)
    head = r'(?<![a-z0-9_])' if re.match(r'[a-z0-9_]', word) else ''
    tail = r'(?![a-z0-9_])' if re.search(r'[a-z0-9_]$', word) else ''
    return re.compile(head + body + tail)


def dialog_class(table, prompt, screen):
    """'delete' | 'other' for the dialog at the bottom of `screen`; None when the screen is empty (unreadable:
    the caller hands it to the user). Any delete word anywhere in the last tail_lines rows makes it a delete."""
    if (prompt or {}).get('name') == 'dangerous_rm' or (prompt or {}).get('verify') == 'rm_in_cwd':
        return 'delete'
    text = screen_tail(screen, table['tail_lines']).lower()
    if not text.strip():
        return None
    patterns = table.get('delete_markers') or [_marker_re(x) for x in DELETE_MARKERS]
    return 'delete' if any(p.search(text) for p in patterns) else 'other'


# ---------------------------------------------------------------- prompt table
def parse_table(raw):
    """Validate a prompt table (YAML text or an already-loaded dict). Raises ValueError."""
    if isinstance(raw, (str, bytes)):
        import yaml
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ValueError('prompt table is not valid YAML: ' + str(exc).splitlines()[0])
    else:
        data = raw
    if not isinstance(data, dict):
        raise ValueError('prompt table must be a mapping')
    unknown = set(data) - {'tail_lines', 'max_hits', 'prompts', 'unknown_markers', 'delete_markers'}
    if unknown:
        raise ValueError('unknown prompt table keys: ' + ', '.join(sorted(unknown)))
    tail, hits = data.get('tail_lines', 25), data.get('max_hits', 3)
    if type(tail) is not int or not 1 <= tail <= 500:
        raise ValueError('tail_lines must be an integer 1-500')
    if type(hits) is not int or not 1 <= hits <= 20:
        raise ValueError('max_hits must be an integer 1-20')
    prompts, names = [], set()
    for entry in data.get('prompts') or []:
        if not isinstance(entry, dict) or set(entry) - {'name', 'match', 'any', 'keys', 'verify'}:
            raise ValueError('a prompt entry has exactly name / match / keys / verify (and optional any)')
        name = entry.get('name')
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('prompt names must be unique non-empty strings')
        names.add(name)
        for field in ('match', 'any'):
            value = entry.get(field, [] if field == 'any' else None)
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise ValueError(name + ': ' + field + ' must be a list of non-empty strings')
        if not entry['match']:
            raise ValueError(name + ': match must not be empty')
        keys = entry.get('keys')
        if not isinstance(keys, list) or not keys or not all(k in KEYS for k in keys):
            raise ValueError(name + ': keys must be a non-empty list of ' + ', '.join(sorted(KEYS)))
        if entry.get('verify') not in VERIFY:
            # Matching text is not an authorisation: every answered dialog names how its operation is checked.
            raise ValueError(name + ': verify must be one of ' + ', '.join(sorted(VERIFY)))
        prompts.append({'name': name, 'match': [x.lower() for x in entry['match']],
                        'any': [x.lower() for x in entry.get('any', [])], 'keys': list(keys),
                        'verify': entry['verify']})
    markers = data.get('unknown_markers') or []
    if not isinstance(markers, list) or not all(isinstance(x, str) and x.strip() for x in markers):
        raise ValueError('unknown_markers must be a list of non-empty strings')
    extra = data.get('delete_markers') or []
    if not isinstance(extra, list) or not all(isinstance(x, str) and x.strip() for x in extra):
        raise ValueError('delete_markers must be a list of non-empty strings')
    # The built-in delete words are always in force: the table can only add to them, never remove one.
    return {'tail_lines': tail, 'max_hits': hits, 'prompts': prompts,
            'unknown_markers': [x.lower() for x in markers],
            'delete_markers': [_marker_re(x) for x in DELETE_MARKERS + [x.strip() for x in extra]]}


class TableCache:
    """Hot reload by (mtime, size). A bad edit keeps the previous table and is audited once."""

    def __init__(self, path):
        self.path = Path(path)
        self.stamp = None
        self.table = None

    def get(self, audit):
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError as exc:
            stamp = ('missing', str(exc))
        if stamp == self.stamp and self.table is not None:
            return self.table
        self.stamp = stamp
        try:
            table = parse_table(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            audit('watch.table_rejected', level='error', error=str(exc)[:500], path=str(self.path))
            if self.table is None:
                self.table = parse_table({})  # nothing whitelisted: every dialog is held
            return self.table
        if self.table is not None:
            audit('watch.table_reloaded', level='info', path=str(self.path),
                  prompts=[p['name'] for p in table['prompts']])
        self.table = table
        return table


def screen_tail(screen, lines):
    rows = (screen or '').rstrip().splitlines()
    return '\n'.join(rows[-lines:])


def match_prompt(table, screen):
    """(whitelisted entry or None, unknown marker or None) for the bottom of the screen."""
    text = screen_tail(screen, table['tail_lines']).lower()
    if not text:
        return None, None
    for entry in table['prompts']:
        if all(m in text for m in entry['match']) and (not entry['any'] or any(a in text for a in entry['any'])):
            return entry, None
    for marker in table['unknown_markers']:
        if marker in text:
            return None, marker
    return None, None


PATH_RE = re.compile(r'(?<![\w$.~/-])(~?/[^\s"\'`<>|;&()]+)')


def _real(path):
    return Path(os.path.realpath(os.path.expanduser(path.rstrip('?.,:!'))))


def _inside(path, root, strict=False):
    return path == root and not strict or root in path.parents


def unwrap_paths(text):
    """Re-join a long path the TUI wrapped over several lines (Claude 2.1.280 wraps at its box width).

    A run of consecutive lines that are each one space-free token, the first starting with '/' or '~/',
    is one path. Joining two separate paths by mistake yields a path that is in no grant: held, safe.
    """
    out, block = [], []
    for row in text.splitlines() + ['']:
        token = row.strip()
        if token and ' ' not in token and (block or token.startswith(('/', '~/'))):
            block.append(token)
            continue
        if block:
            out.append(''.join(block))
            block = []
        out.append(row)
    return '\n'.join(out[:-1])


RULE_RE = re.compile(r'^\s*─{10,}\s*$')


def dialog_box(text):
    """The rows below the last full-width rule: the Claude dialog's own box, without the shell lines above it.

    S1a 2026-10-13: the pane's `exec /bin/bash <run>/prompts/<slot>.sh` line sat above the trust box and was
    read as a dialog path, so a trust dialog for the slot's own directory was held. No rule: the whole tail.
    A rule below the dialog's path leaves no path to read: held, safe.
    """
    rows = text.splitlines()
    rules = [i for i, row in enumerate(rows) if RULE_RE.match(row)]
    return '\n'.join(rows[rules[-1] + 1:]) if rules else text


def authorize(entry, screen, lines, cwd, workspace):
    """(True, why) only when the dialog's concrete operation is inside an existing grant; else (False, why).

    A dialog whose scope cannot be read off the screen is never authorised: the caller holds it.
    """
    text = screen_tail(screen, lines)
    cwd, workspace = _real(str(cwd)), _real(str(workspace))
    kind = entry.get('verify')
    if kind in ('own_cwd', 'paths_in_run'):
        # own_cwd (trust dialogs) reads only the dialog box; paths_in_run keeps the whole tail (layout not captured).
        scope = dialog_box(text) if kind == 'own_cwd' else text
        paths = [_real(m) for m in PATH_RE.findall(unwrap_paths(scope))]
        if not paths:
            return False, 'no path on the dialog: its scope cannot be checked'
        roots = (cwd,) if kind == 'own_cwd' else (cwd, workspace)
        bad = [str(p) for p in paths if not any((p == r) if kind == 'own_cwd' else _inside(p, r) for r in roots)]
        if bad:
            return False, 'path outside the granted area: ' + ', '.join(bad[:3])
        return True, ', '.join(sorted({str(p) for p in paths}))[:300]
    return False, 'no verification rule'


def uninterruptible(stats):
    """macOS `ps` STAT 'U' (Linux 'D'): a process stuck in the kernel (E5: autofs read)."""
    return any(s[:1] in ('U', 'D') for s in stats or ())


def classify(*, active, in_flight, age, stall_timeout, prev, since, now, grace, escapes, max_escapes,
             screen_same, proc_frozen, prompt=None, unknown=None, prompt_hits=0, max_hits=3, moved=False):
    """Pure state decision. `since` = when the current stall began; `moved` = activity after it."""
    if not active:
        return 'idle'
    if in_flight and proc_frozen >= 2:
        return 'frozen'
    if prompt is not None and prompt_hits < max_hits:
        return 'prompting'
    if unknown is not None:
        return 'prompting'
    if not in_flight:
        return 'working'  # starting without a delivery: the READY deadline governs it
    stuck_dialog = prompt is not None  # answered max_hits times and still there
    if prev == 'stalled' and not stuck_dialog:
        if moved:
            return 'working'
        if now - since >= grace or (escapes >= max_escapes and screen_same):
            return 'frozen'
        return 'stalled'
    if stuck_dialog or (stall_timeout > 0 and age >= stall_timeout):
        if prev == 'stalled' and (now - since >= grace or (escapes >= max_escapes and screen_same)):
            return 'frozen'
        return 'stalled'
    return 'working'


# ---------------------------------------------------------------- driver
class Watchdog:
    def __init__(self, runtime):
        self.rt = runtime
        self.f = runtime.f
        self.cache = TableCache(self.f.cfg.get('prompt_table') or DEFAULT_TABLE)
        self.next_check = {}

    def audit(self, event, **fields):
        self.rt.audit(event, **fields)

    def set_state(self, slot, info, state, **evidence):
        prev = info.get('watch_state') or 'idle'
        if prev != state:
            self.audit('agent.state', slot=slot, level='warn' if state in ('stalled', 'frozen', 'prompting') else 'info',
                       detail=dict({'from': prev, 'to': state, 'generation': info.get('generation', '')}, **evidence))
        self.f.setf(slot, watch_state=state)

    def save_screen(self, slot, info, screen, tag):
        folder = self.f.run_dir / 'watchdog'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / '{}-g{}-{}-{}.txt'.format(slot, info.get('generation', '0'), int(time.time() * 1000), tag)
        path.write_text(screen or '', encoding='utf-8')
        return str(path)

    def capture(self, slot, info, pane):
        read = getattr(self.f.mgr, 'capture_output', None)
        screen = read(pane, 200) if read else ''
        if screen:
            if info.get('capture_fail', '0') != '0':
                self.f.setf(slot, capture_fail=0)
            return screen
        fails = int(info.get('capture_fail') or 0) + 1
        self.f.setf(slot, capture_fail=fails)
        if fails == int(setting(self.f.cfg, 'capture_fail_alarm')):
            # E6: thousands of silent capture failures hid a quota wall. Say it once per streak.
            self.audit('watch.capture_fail', slot=slot, level='error', count=fails,
                       error='screen capture failed ' + str(fails) + ' times in a row; dialogs are invisible')
        return ''

    def proc_stats(self, pane):
        probe = getattr(self.f.mgr, 'process_states', None)
        return probe(pane) if probe else []

    def authorized(self, slot, info, prompt, screen, table):
        """This run's own pane, and the dialog's concrete operation inside a grant the run already has."""
        from cmux_fleet import subfleet
        from cmux_fleet.fleet import agent_cwd
        owns = getattr(self.f.mgr, 'owns', None)
        if owns is not None and not owns(info['pane']):
            return False, 'pane ' + info['pane'] + ' is not in this fleet\'s registry'
        if subfleet.adopted(self.f.cfg, slot, info.get('generation')):
            return False, 'adopted pane belongs to the parent fleet'
        return authorize(prompt, screen, table['tail_lines'], agent_cwd(self.f.cfg, slot),
                         Path(self.f.cfg['run']) / 'workspace')

    def tick(self, now=None):
        now = time.time() if now is None else now
        table = self.cache.get(self.audit)
        interval = float(setting(self.f.cfg, 'watch_interval'))
        for slot in list(self.f.names):
            info = self.f.agent(slot)
            state, pane = info.get('state'), info.get('pane')
            if state in HANDS_OFF or info.get('handoff_id') or not pane:
                if info.get('prompt_wait'):  # held / retired / handed off: the dialog wait is someone else's now
                    self.clear_prompt_wait(slot, 'hands_off')
                continue
            lease = self.f.r.hgetall(self.rt.bus.lease_key(slot))
            in_flight = bool(lease.get('current_delivery')) and lease.get('status') == 'received'
            active = in_flight or state == 'starting'
            scope = lease.get('current_delivery') or ('startup-g' + str(info.get('generation', '0')))
            if not active:
                if info.get('prompt_wait'):  # the delivery settled / the startup READY came
                    self.clear_prompt_wait(slot, 'inactive')
                if info.get('watch_state', 'idle') != 'idle' or info.get('stall_escapes', '0') != '0':
                    self.set_state(slot, info, 'idle')
                    self.f.setf(slot, stall_escapes=0, watch_since=0, proc_frozen=0, prompt_hits=0)
                continue
            if now < self.next_check.get(slot, 0):
                continue
            self.next_check[slot] = now + interval
            if info.get('prompt_wait'):
                if info['prompt_wait'] != scope:  # that delivery settled, a new one / generation came
                    self.drop_dialog(slot, info, 'identity')
                    self.clear_prompt_wait(slot, 'scope_changed')
                elif info.get('prompt_route') == 'supervisor':
                    if self.supervised(slot, info, lease, scope, table, now):
                        continue
                elif self.f.last_activity(info) <= float(info.get('prompt_wait_at') or 0):
                    # A human answers this dialog (B-2): no screen read, no stall ladder, not a single key,
                    # whether or not the dialog is still in the captured tail.
                    continue
                else:  # the transcript moved: the human answered and the agent went on
                    self.clear_prompt_wait(slot, 'activity')
                    self.set_state(slot, info, 'working')
                info = self.f.agent(slot)
            self.check(slot, info, lease, in_flight, table, now)

    # ------------------------------------------------------------ plan F' 8.3: non-delete dialog -> supervisor
    def dialog_key(self, slot, info):
        from cmux_fleet.fleet import DIALOG_KEY
        return DIALOG_KEY + slot + ':g' + str(info.get('generation', '0'))

    def supervisor_away(self):
        """Why the supervisor cannot rule on a worker's dialog now ('' when it can)."""
        if not self.f.running():  # task mode between tasks (service mode always runs)
            return 'no running task'
        if self.f.paused():
            return 'dispatch is paused'
        sup = self.f.agent('supervisor')
        if sup.get('state') in SUPERVISOR_AWAY or sup.get('handoff_id') or sup.get('prompt_wait'):
            return 'supervisor is ' + (sup.get('state') or 'missing') + (' at a dialog' if sup.get('prompt_wait') else '')
        return ''

    def escalate(self, slot, info, lease, scope, prompt, unknown, screen, path, table):
        """Hand a non-delete dialog to the supervisor (plan F' 8.3). False: it goes to the user instead."""
        from cmux_fleet import subfleet
        owns = getattr(self.f.mgr, 'owns', None)
        if slot == 'supervisor':
            return False  # nobody can rule on the supervisor's own dialog but the user
        if (owns is not None and not owns(info['pane'])) or subfleet.adopted(self.f.cfg, slot, info.get('generation')):
            return False  # keys are never pressed on a pane this fleet does not own
        away = self.supervisor_away()
        if away:
            self.audit('dialog.to_human', slot=slot, level='warn', reason='supervisor_unavailable', detail=away)
            return False
        tail = screen_tail(screen, table['tail_lines'])
        if prompt is not None:
            keys = list(prompt['keys'])
        else:  # an unknown dialog: Enter only when the cursor visibly sits on Yes
            keys = ['Enter'] if YES_CURSOR.search(tail.lower()) else None
        rec = {'kind': 'dialog', 'slot': slot, 'generation': str(info.get('generation', '0')),
               'session_id': info.get('thread', ''), 'scope': scope, 'delivery_id': lease.get('current_delivery', ''),
               'prompt': prompt['name'] if prompt is not None else 'unknown:' + unknown,
               'screen': path, 'digest': hashlib.sha256(tail.encode()).hexdigest()[:16],
               'keys_approve': keys, 'keys_reject': ['Escape']}
        self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, lambda: self.rt.prompt_escalate(slot, rec, tail))
        return True

    def drop_dialog(self, slot, info, reason):
        """Pop this slot's dialog escalation (if any) and audit why it is stale."""
        from cmux_fleet.fleet import DIALOG_KEY
        prefix = DIALOG_KEY + slot + ':g'  # any generation of this slot
        esc = self.f.escalation_state()
        stale = [k for k in esc if k.startswith(prefix)]
        if stale:
            self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, lambda: self.f.save_escalations(
                {k: v for k, v in esc.items() if k not in stale}))
            self.audit('dialog.stale', slot=slot, level='info', reason=reason, task=', '.join(stale))

    def to_user(self, slot, info, scope, rec, why, reason):
        """A supervisor-routed dialog falls back to the user (plan F' 8.3 step 4 / 5)."""
        def act():
            esc = self.f.escalation_state()
            esc.pop(self.dialog_key(slot, info), None)
            self.f.save_escalations(esc)
            self.audit('dialog.' + ('reclassified_delete' if why == 'reclassified_delete' else 'to_human'), slot=slot,
                       level='warn', reason=why, task=self.dialog_key(slot, info))
            self.rt.prompt_wait(slot, scope, reason, (rec or {}).get('screen', ''),
                                kind='delete' if why == 'reclassified_delete' else 'other')
        self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, act)

    def supervised(self, slot, info, lease, scope, table, now):
        """One tick of a dialog waiting for the supervisor. True: done with this slot this tick."""
        key = self.dialog_key(slot, info)
        esc = self.f.escalation_state()
        rec = esc.get(key)
        if rec is None or rec.get('scope') != scope:  # e.g. a new task cleared fx:escalations
            self.to_user(slot, info, scope, rec, 'record_gone', 'dialog escalation record gone; handed to the user')
            return True
        if self.f.last_activity(info) > float(info.get('prompt_wait_at') or 0):
            # A human answered in the pane meanwhile: nothing to press any more.
            self.drop_dialog(slot, info, 'activity')
            self.clear_prompt_wait(slot, 'activity')
            self.set_state(slot, info, 'working')
            return False
        if rec.get('status') == 'answered':
            return self.answer_dialog(slot, info, scope, key, rec, table)
        away = self.supervisor_away()
        if away and away != 'dispatch is paused':
            self.to_user(slot, info, scope, rec, 'supervisor_unavailable', 'supervisor unavailable (' + away
                         + '); dialog handed to the user')
            return True
        if not float(rec.get('delivered_at') or 0):
            # The clock starts when the supervisor receives the [dialog] message, not while it is queued.
            if self.rt.bus.status(rec.get('message_id') or '') not in ('', 'queued'):
                rec['delivered_at'] = now
                esc[key] = rec
                self.rt.transaction('watch-dialog-' + uuid.uuid4().hex, lambda: self.f.save_escalations(esc))
            return True
        limit = float(setting(self.f.cfg, 'dialog_escalation_timeout'))
        if now - float(rec['delivered_at']) >= limit:
            minutes = str(int(limit // 60))
            self.to_user(slot, info, scope, rec, 'supervisor_timeout', 'supervisor did not rule on the dialog within '
                         + minutes + ' min; handed to the user')
        return True

    def answer_dialog(self, slot, info, scope, key, rec, table):
        """Press the supervisor's answer, only after every check passes on a fresh screen (plan F' 8.3 step 4)."""
        from cmux_fleet import subfleet
        pane = info['pane']
        screen = self.capture(slot, info, pane)
        if not screen or self.f.wall_gate(slot, screen) is not None:
            return True  # nothing readable / a wall is the brake's: try again next tick
        owns = getattr(self.f.mgr, 'owns', None)
        if (str(info.get('generation', '0')) != rec['generation'] or info.get('thread', '') != rec['session_id']
                or scope != rec['scope'] or (owns is not None and not owns(pane))
                or subfleet.adopted(self.f.cfg, slot, info.get('generation'))):
            self.drop_dialog(slot, info, 'identity')
            self.clear_prompt_wait(slot, 'identity')
            return True
        tail = screen_tail(screen, table['tail_lines'])
        prompt, unknown = match_prompt(table, screen)
        if prompt is None and unknown is None:  # the dialog is gone (answered in the pane, or the agent moved on)
            self.drop_dialog(slot, info, 'dialog_gone')
            self.clear_prompt_wait(slot, 'dialog_gone')
            self.set_state(slot, info, 'working')
            return False
        same = (prompt['name'] if prompt is not None else 'unknown:' + unknown) == rec['prompt']
        if not same or hashlib.sha256(tail.encode()).hexdigest()[:16] != rec['digest']:
            # Another dialog (or this one changed): never press the old answer; classify and route it afresh.
            self.drop_dialog(slot, info, 'dialog_changed')
            self.clear_prompt_wait(slot, 'dialog_changed')
            return False
        if dialog_class(table, prompt, screen) != 'other':
            self.to_user(slot, info, scope, rec, 'reclassified_delete', 'dialog ' + rec['prompt'] + ' now reads as a '
                         'delete / move; handed to the user, nothing pressed')
            return True
        if rec['decision'] == 'approve' and not rec.get('keys_approve'):
            self.to_user(slot, info, scope, rec, 'no_safe_key', 'the supervisor approved dialog ' + rec['prompt']
                         + ' but no safe key is known; handed to the user')
            self.rt.queue('supervisor', '[dialog] ' + key + ': no safe key to press for approve; handed to the user.')
            return True
        keys = rec['keys_approve'] if rec['decision'] == 'approve' else rec.get('keys_reject') or ['Escape']
        sent = [k for k in keys if k in KEYS and self.f.mgr.send_key(pane, k)]
        if prompt is not None:
            hits = int(info.get('prompt_hits') or 0) if info.get('prompt_scope') == scope else 0
            self.f.setf(slot, prompt_scope=scope, prompt_hits=hits + 1)
        self.audit('watch.prompt_answered', slot=slot, level='warn', prompt=rec['prompt'], keys=sent, screen=rec['screen'],
                   authorized='supervisor:' + rec['decision'], task=key, delivery_id=rec.get('delivery_id', ''))
        self.drop_dialog(slot, info, 'answered')
        self.clear_prompt_wait(slot, 'answered')
        self.set_state(slot, info, 'working')
        return True

    def clear_prompt_wait(self, slot, reason):
        info = self.f.agent(slot)
        self.f.setf(slot, prompt_wait='', prompt_wait_at=0, prompt_route='')
        self.audit('watch.prompt_cleared', slot=slot, level='info', reason=reason, scope=info.get('prompt_wait', ''))

    def wait_for_user(self, slot, scope, reason, path, kind):
        """Hand a dialog to the user (plan F B-1 / F' 8.2): mark the slot, pause dispatch, press nothing.
        The lease is untouched: once the human answers, the turn ends and settles normally."""
        self.rt.transaction('watch-prompt-' + uuid.uuid4().hex, lambda: self.rt.prompt_wait(
            slot, scope, reason + '; nothing pressed, screen saved to ' + path, path, kind=kind))

    def check(self, slot, info, lease, in_flight, table, now):
        pane = info['pane']
        cfg = self.f.cfg
        screen = self.capture(slot, info, pane)
        if screen and self.f.wall_gate(slot, screen) is not None:
            # E before F (docs/design.md §23): a usage wall is the brake's. No key, no hold, no kill this
            # tick; a wall menu (e.g. "❯ 1. Yes, switch…") is never a dialog to answer.
            return
        digest = hashlib.sha256(screen.encode()).hexdigest()[:16] if screen else ''
        frozen_seen = int(info.get('proc_frozen') or 0) + 1 if uninterruptible(self.proc_stats(pane)) else 0
        self.f.setf(slot, proc_frozen=frozen_seen)
        prompt, unknown = match_prompt(table, screen)
        scope = lease.get('current_delivery') or ('startup-g' + str(info.get('generation', '0')))
        hits = int(info.get('prompt_hits') or 0) if info.get('prompt_scope') == scope else 0
        prev = info.get('watch_state') or 'idle'
        since = float(info.get('watch_since') or 0)
        stall_timeout = float(cfg.get('stall_timeout') or 0)
        age = now - self.f.last_activity(info) if in_flight else 0
        moved = prev == 'stalled' and self.f.last_activity(info) > since
        escapes = int(info.get('stall_escapes') or 0)
        state = classify(active=True, in_flight=in_flight, age=age, stall_timeout=stall_timeout, prev=prev,
                         since=since, now=now, grace=float(setting(cfg, 'stall_grace')), escapes=escapes,
                         max_escapes=int(setting(cfg, 'stall_escapes')),
                         screen_same=bool(digest) and digest == info.get('esc_digest'), proc_frozen=frozen_seen,
                         prompt=prompt, unknown=unknown, prompt_hits=hits, max_hits=table['max_hits'], moved=moved)
        if state == 'prompting' and prompt is not None:
            path = self.save_screen(slot, info, screen, 'prompt-' + prompt['name'])
            allowed, reason = self.authorized(slot, info, prompt, screen, table)
            kind = dialog_class(table, prompt, screen)
            if allowed and kind != 'other':
                # Never press keys on anything that may delete / move data, even on a whitelisted dialog.
                allowed, reason = False, 'the screen shows a delete / move: the user decides'
            if not allowed:
                # Keep the scene: nothing pressed, a human reads the saved screen and answers in the pane.
                self.set_state(slot, info, state, prompt=prompt['name'], authorized=False)
                self.audit('watch.prompt_unverified', slot=slot, level='error', prompt=prompt['name'], reason=reason,
                           screen=path, delivery_id=lease.get('current_delivery', ''), dialog_class=kind or 'unread')
                if kind == 'other' and self.escalate(slot, info, lease, scope, prompt, None, screen, path, table):
                    return
                self.wait_for_user(slot, scope, 'dialog ' + prompt['name'] + ' not answered: ' + reason, path, kind)
                return
            sent = [k for k in prompt['keys'] if self.f.mgr.send_key(pane, k)]
            self.f.setf(slot, prompt_scope=scope, prompt_hits=hits + 1)
            self.set_state(slot, info, state, prompt=prompt['name'])
            self.audit('watch.prompt_answered', slot=slot, level='warn', prompt=prompt['name'], keys=sent,
                       hits=hits + 1, screen=path, authorized=reason, delivery_id=lease.get('current_delivery', ''))
        elif state == 'prompting':
            path = self.save_screen(slot, info, screen, 'prompt-unknown')
            kind = dialog_class(table, None, screen)
            self.set_state(slot, info, state, marker=unknown)
            self.audit('watch.prompt_unknown', slot=slot, level='error', marker=unknown, screen=path,
                       dialog_class=kind or 'unread')
            if kind == 'other' and self.escalate(slot, info, lease, scope, None, unknown, screen, path, table):
                return
            self.wait_for_user(slot, scope, 'unknown confirmation dialog on screen (' + unknown + ')', path, kind)
        elif state == 'working':
            if prev != 'working' or escapes:
                self.f.setf(slot, stall_escapes=0, watch_since=0, esc_digest='')
            self.set_state(slot, info, state, **({'resumed_after_s': int(now - since)} if prev == 'stalled' else {}))
        elif state == 'stalled':
            if prev != 'stalled':
                since, escapes = now, 0
                self.f.setf(slot, watch_since=now, stall_escapes=0)
                self.set_state(slot, info, state, idle_s=int(age), dialog=prompt['name'] if prompt else '')
            spacing = float(setting(cfg, 'stall_grace')) / max(1, int(setting(cfg, 'stall_escapes')))
            last = float(info.get('esc_at') or 0) if prev == 'stalled' else 0
            if escapes < int(setting(cfg, 'stall_escapes')) and now - last >= spacing:
                self.f.mgr.send_key(pane, 'Escape')
                self.f.setf(slot, stall_escapes=escapes + 1, esc_at=now, esc_digest=digest)
                self.audit('watch.escape', slot=slot, level='warn', escapes=escapes + 1, idle_s=int(age))
        elif state == 'frozen':
            why = ('processes in uninterruptible wait' if frozen_seen >= 2 else
                   str(escapes) + ' Escape presses left the screen unchanged' if escapes and digest == info.get('esc_digest')
                   else 'no activity for ' + str(int(now - since)) + ' s after the first Escape')
            self.set_state(slot, info, state, reason=why)
            self.rotate(slot, why, screen)

    # ------------------------------------------------------------ frozen: kill + next generation
    def rotate(self, slot, why, screen):
        """Kill this fleet's own frozen session and start the next generation in its place."""
        from cmux_fleet import history, subfleet
        from cmux_fleet.fleet import NAMES, SUPERVISOR_LABEL
        f, rt = self.f, self.rt
        info = f.agent(slot)
        pane, generation = info['pane'], int(info.get('generation') or 0)
        path = self.save_screen(slot, info, screen, 'frozen')
        if f.paused():
            # Nothing is (re)started while the run is paused (closed market, operator hold).
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
                slot, 'frozen (' + why + ') while the run is paused; not restarted, screen saved to ' + path))
            return False
        if subfleet.adopted(f.cfg, slot, info.get('generation')):
            # The parent fleet owns this pane: never kill it from here.
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
                slot, 'frozen (' + why + '); adopted pane belongs to the parent fleet, not killed'))
            return False
        owns = getattr(f.mgr, 'owns', None)
        if owns is not None and not owns(pane):
            # R18 / default #13: only a pane this fleet's cmux registry proves it spawned is killed.
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
                slot, 'frozen (' + why + '); pane ' + pane + ' is not in this fleet\'s registry, not killed'))
            return False
        if slot == advisor.SLOT:
            # §27: one advisor fault branch (retire, next generation by the supervisor, queued consultations fall
            # back on failure); its in-flight consultation is cancelled and that slot falls back. Never raises.
            f.advisor_restart('frozen: ' + why)
            self.next_check.pop(slot, None)
            return f.agent(slot).get('state') == 'starting'
        lease = f.r.hgetall(rt.bus.lease_key(slot))
        msg = rt.bus.message(lease['message_id']) if lease.get('message_id') else {}
        hid = 'frozen-' + uuid.uuid4().hex[:8]
        note = f.run_dir / 'handoffs' / (slot + '-g' + str(generation) + '-' + hid + '.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f.skeleton(slot, dict(info, handoff_id=hid)))
        snapshot = note.with_suffix('.terminal.txt')
        f.mgr.capture_to(pane, str(snapshot))
        prev = f.label(slot)
        label = (SUPERVISOR_LABEL if slot == 'supervisor' else slot) + '.' + str(generation + 1)
        # Order (same as respawn / handoff, keeps the layout): start the successor launch-held, put it in
        # the old tab's place, kill the old process tree, confirm retirement, only then open the launch
        # gate. Until then the successor cannot run work: it is 'starting' (pump delivers to 'idle'
        # only), the old lease still holds the delivery (offer needs an idle lease; register_slot
        # refuses 'busy'), and its READY is refused while launch_ready=0. Every failure below removes
        # the successor again, so at most one session of this slot is ever left.
        f.setf(slot, generation=generation + 1, label=label, handoff_id='', watch_state='frozen',
               stall_escapes=0, watch_since=0, esc_digest='', proc_frozen=0, prompt_hits=0,
               prev_label='' if slot == 'supervisor' else prev)
        if slot != 'supervisor':
            f.r.hset(NAMES, mapping={label: slot})
        try:
            new = f.spawn(slot, '[Same-task takeover] You are ' + label + ' (taking over from ' + prev + ', which '
                          'froze and was stopped). Read the handoff note ' + str(note) + '. In this turn only read '
                          'the note, do no task work, and make your final reply exactly READY.', launch_held=True)
        except RuntimeError as exc:
            return self.abort_rotation(slot, info, label, f.agent(slot).get('pane'), path,
                                       'the next generation did not start (' + str(exc)[:300] + ')')
        if not f.mgr.tab_into(new, pane):
            return self.abort_rotation(slot, info, label, new, path, 'the next generation could not take the old tab')
        if not f.mgr.kill_pane(pane) or f.mgr.get_pane_status(pane) is not None:
            return self.abort_rotation(slot, info, label, new, path, 'the frozen session ' + prev + ' did not exit')
        rt.confirm_retired(slot)  # the process tree is gone: its delivery is cancelled, execution stopped
        f.setf(slot, launch_ready=1, retired_pane='')
        history.record(f.run_dir, slot=slot, label=prev, generation=generation, handoff=str(note),
                       snapshot=str(snapshot), source='frozen', timed_out=True)
        f.refresh_history()
        self.audit('watch.frozen_restarted', slot=slot, level='error', prev=prev, label=label, reason=why,
                   screen=path, handoff=str(note), message_id=msg.get('message_id', ''))
        self.next_check.pop(slot, None)
        self.tell(slot, prev, label, why, msg)
        return True

    def abort_rotation(self, slot, old, label, new, screen, why):
        """Undo a failed rotation: remove the successor, give the slot back to the frozen session, hold it.

        The old delivery is never cancelled here (only confirm_retired does that, after a confirmed
        kill), so the held slot still shows the work as uncertain and dispatch stays paused.
        """
        from cmux_fleet.fleet import NAMES
        f, rt = self.f, self.rt
        gone = not new or new == old.get('pane') or (f.mgr.kill_pane(new) and f.mgr.get_pane_status(new) is None)
        self.audit('watch.rotate_failed', slot=slot, level='error', reason=why, old_pane=old.get('pane', ''),
                   new_pane=new or '', successor_removed=gone, screen=screen)
        if not gone:
            # Both sessions may still be alive. The slot is held (its delivery uncertain, dispatch paused) and
            # stays launch-held: the successor's READY is refused, so it can never be offered work.
            text = ('watchdog could not stop the frozen session of ' + slot + ' (' + why + ') nor remove its '
                    'successor ' + str(new) + '; two sessions may hold slot ' + slot + '. Close one by hand.')
            f.setf(slot, launch_ready=0, retired_pane=old.get('pane', ''))
            rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(slot, text))
            rt.finish('blocked', text)
            return False
        f.setf(slot, **{k: v for k, v in old.items() if k != 'watch_state'})
        f.setf(slot, watch_state='frozen', launch_ready=old.get('launch_ready', '1'))
        if slot != 'supervisor':
            f.r.hdel(NAMES, label)
        rt.transaction('watch-frozen-' + uuid.uuid4().hex, lambda: rt.hold(
            slot, 'frozen; ' + why + '. Successor removed, frozen session left as it was (screen ' + screen + ')'))
        return False

    def tell(self, slot, prev, label, why, msg):
        """The cancelled work is not re-run: the supervisor (or the request's caller) decides."""
        from cmux_fleet.fleet import REQ
        f, rt = self.f, self.rt
        if not msg or not f.running():
            return
        if slot == 'supervisor':
            text = (msg.get('body') or {}).get('text', '')
            note = ('\n\n[watchdog] The previous supervisor generation ' + prev + ' froze while handling this message ('
                    + why + ') and was restarted. It may have acted on it already: check the current state before '
                    'dispatching again.')
            rt.queue('supervisor', text + note, request_id=msg.get('request_id'), kind=msg.get('kind'))
            return
        rid = msg.get('request_id')
        if f.service:
            if rid and f.r.hget(REQ + rid, 'status') == 'dispatched':
                rt.transaction('watch-publish-' + uuid.uuid4().hex, lambda: rt.publish(
                    rid, 'stalled', worker=label, error=prev + ' froze (' + why + '); restarted, the request was not re-run'))
            return
        if msg.get('kind') != 'worker':
            return
        f.queue('supervisor', '[' + label + ' report (took over from ' + prev + ')]\nFROZEN: ' + prev + ' stopped '
                'responding (' + why + '). The watchdog killed its session and started ' + label + ' from a '
                'handoff note. Its instruction was cancelled and NOT re-run; it may be partly done. Check the '
                'workspace, then dispatch again or reply BLOCKED if a human is needed.'
                '\n\nReply with the JSON format from AGENTS.md.')


def pane_tty_stats(tty):
    """`ps` STAT of every process on a tty (cmux surface). Empty when ps fails."""
    import subprocess
    if not tty:
        return []
    p = subprocess.run(['ps', '-t', os.path.basename(tty), '-o', 'stat='], capture_output=True, text=True)
    return [line.strip() for line in p.stdout.splitlines() if line.strip()] if p.returncode == 0 else []
