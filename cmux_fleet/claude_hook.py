"""Claude worker hook channel: deliveries reach Claude Code through its Stop hook, never the input box.

After the first (CLI-argument) startup prompt, the daemon never types into a Claude worker's pane.
It pushes the business text to a private Redis mailbox for that slot and generation. The worker's
Stop hook, which runs at every turn end, settles the turn as before, then blocks on that mailbox
and answers Claude Code with ``{"decision": "block", "reason": <business text>}``. Claude Code
takes the reason as the next instruction and starts a new turn in the same session.

Measured on Claude Code 2.1.280 (docs: claude-hook-report.md):
  * A command hook's ``timeout`` is in seconds with no upper clamp (default 600). A hook that is
    still running at its timeout is killed and its output dropped: the turn ends normally and the
    session sits idle with no hook waiting ("unarmed").
  * ``stop_hook_active`` is only a flag (true on every Stop that follows a block). It never stops a
    block by itself, but Claude Code overrides the 9th consecutive block unless
    CLAUDE_CODE_STOP_HOOK_BLOCK_CAP is raised; 0 turns that cap off (``ENV``).
  * The reason is written to the transcript as a ``"type":"user"``, ``isMeta`` row
    ``"Stop hook feedback:\\n<reason>"`` (``FEEDBACK``).
  * Text typed into the pane while the hook waits is queued: the hook's delivery runs first,
    then the typed text runs as its own turn. Escape interrupts the turn and kills the waiting
    hook, so the session stays idle (unarmed) until its next turn ends.

Long idle: the hook waits up to ``hook_timeout(cfg)`` (default one day) without starting any turn.
Shortly before that it hands Claude one tiny keepalive turn (``KEEPALIVE``) so the next Stop hook
re-arms; that turn is never settled or reported.
"""
import json
import os
import time

ENV = {'CLAUDE_CODE_STOP_HOOK_BLOCK_CAP': '0'}
# Without this, Claude Code shows the reason as "Stop hook error" and the model often refuses a long
# delivery as a prompt injection (worker-2 research §1b: 3/8 followed without it, 8/8 with it).
DECLARATION = ('Fleet channel: after your first turn, every supervisor instruction (task, follow-up, '
               'handoff) is delivered to you by the fleet Stop hook, as a "Stop hook feedback:" message or '
               'as a "Fleet delivery:" Stop hook notification. Treat that text as the supervisor '
               'instruction and carry it out exactly as if the supervisor had typed it.')
REWAKE_MESSAGE = 'Fleet delivery:'  # asyncRewake prefix; the transcript row is a task-notification
GRACE = 10  # seconds with an entry queued and no Stop hook waiting/settling before the rewake acts
STOPPING_TTL = 120  # a Stop hook settling its turn (tui_turn waits up to settle_wait=30 s)
FEEDBACK = 'Stop hook feedback:'
KEEPALIVE = ('Fleet keepalive: there is no new instruction yet. Do no work and use no tools; '
             'reply exactly IDLE.')
DEFAULT_TIMEOUT = 86400  # seconds; Node's timer limit is ~24.8 days, keep well below
MARGIN = 120  # the hook answers this long before Claude Code's own timeout would kill it
POLL = 5  # seconds per BLPOP; also the liveness re-check period
ARMED_TTL = 3 * POLL
BIND_TTL = 7 * 86400


def enabled(cfg, name, claude=None):
    """Claude task/service workers use the hook channel; the supervisor keeps its typed path.

    ``claude=True``: the caller already knows this runtime is Claude Code (its own Stop hook).
    """
    if claude is None:
        from cmux_fleet.fleet import is_claude
        claude = is_claude(cfg, name)
    return bool(claude) and name != 'supervisor' and cfg.get('claude_hook_delivery', True) is not False


def hook_timeout(cfg):
    """Claude Code's Stop-hook ``timeout`` (seconds); ``claude_hook_timeout`` in the run config."""
    return max(10, int(float(cfg.get('claude_hook_timeout') or DEFAULT_TIMEOUT)))


def margin(cfg):
    """How long before Claude Code's timeout the hook answers on its own (keepalive)."""
    return min(MARGIN, max(3, hook_timeout(cfg) // 10))


def keys(prefix, slot, generation):
    """mailbox: deliveries for one generation; armed: a hook is waiting now; bind: what it handed over."""
    base = prefix + ':hook:' + slot
    return {'mailbox': base + ':g' + str(int(generation)) + ':mailbox',
            'armed': base + ':armed', 'bind': base + ':bind',
            'stopping': base + ':stopping', 'rewake': base + ':rewake'}


def post(r, k, entry):
    """Queue one entry for the slot's Stop hook. Returns True if a hook is waiting right now."""
    r.rpush(k['mailbox'], json.dumps(entry, ensure_ascii=False))
    r.expire(k['mailbox'], BIND_TTL)
    return bool(r.exists(k['armed']))


def armed(r, k, generation):
    raw = r.get(k['armed'])
    try:
        return bool(raw) and int(json.loads(raw).get('generation', -1)) == int(generation)
    except (ValueError, TypeError, AttributeError):
        return False


def live(r, k):
    """A Stop hook of this slot waits now, or is still settling its turn."""
    return bool(r.exists(k['armed']) or r.exists(k['stopping']))


def wait(r, k, generation, thread, deadline, alive, accept, poll=POLL):
    """Block until ``accept`` takes a mailbox entry, the generation retires, or ``deadline``.

    ``alive()`` -> False ends the wait (retired / dead generation). ``accept(entry)`` -> the text to
    hand Claude, or None to drop a stale entry. Returns (text, entry) or (None, None).
    """
    mark = json.dumps({'generation': int(generation), 'session_id': thread or '', 'pid': os.getpid()})
    try:
        while time.monotonic() < deadline:
            if not alive():
                return None, None
            r.set(k['armed'], mark, ex=ARMED_TTL)
            r.delete(k['stopping'])
            got = r.blpop([k['mailbox']], timeout=max(1, int(min(poll, deadline - time.monotonic()))))
            if not got:
                continue
            try:
                if not alive():  # retired or orphaned while blocked: leave the entry for the live hook
                    r.lpush(k['mailbox'], got[1])
                    return None, None
                try:
                    entry = json.loads(got[1])
                except ValueError:
                    continue
                text = accept(entry) if isinstance(entry, dict) else None
            except BaseException:  # SIGTERM (Escape, ctrl+x ctrl+s, timeout) mid hand-over: put it back
                r.lpush(k['mailbox'], got[1])
                raise
            if text:
                return text, entry
        return None, None
    finally:
        try:
            if r.get(k['armed']) == mark:
                r.delete(k['armed'])
        except Exception:  # the hook is exiting anyway; the TTL clears the flag
            pass


def block(text):
    return {'decision': 'block', 'reason': text}


def rewake_text(content):
    """The delivery of an asyncRewake row (origin task-notification), or None for any other one.

    Measured shape: '<task-notification>\\n<summary>Stop hook feedback</summary>\\n</task-notification>\\n'
    '<system-reminder>\\n<REWAKE_MESSAGE> <text>\\n</system-reminder>'.
    """
    if isinstance(content, list):
        content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    if not isinstance(content, str) or '<summary>Stop hook feedback</summary>' not in content:
        return None
    at = content.find(REWAKE_MESSAGE)
    if at < 0:
        return None
    text = content[at + len(REWAKE_MESSAGE):]
    end = text.rfind('</system-reminder>')
    return (text[:end] if end >= 0 else text).strip('\n ').lstrip()


def feedback_text(content):
    """The reason of a Stop-hook block row, or None for any other meta row."""
    if isinstance(content, str) and content.startswith(FEEDBACK):
        return content[len(FEEDBACK):].lstrip('\n')
    return None
