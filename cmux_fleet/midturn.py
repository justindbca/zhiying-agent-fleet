"""Mid-turn handoff policy and small adapters (midturn-handoff-design.md §§3–5).

The daemon owns ordering, settlement and rotation. This module neither imports Fleet nor
dispatches work. Pass the window / handoff ratio already resolved by design.md §22.
Times are seconds unless a name ends in _ms; bus message deadlines are milliseconds.
"""
import json
import math
import time


DEFAULTS = {'midturn_ratio': .75, 'midturn_max_splits': 3, 'midturn_timeout': 900,
            'midturn_nudge': 180, 'midturn_b_wait': 60}
# No A capability is enabled until its version-matched probe passes (P1–P6).
MIDTURN_INJECT = {'pi': False, 'claude': False, 'codex': False, 'opencode': False}
TOOL_LIMITS = {'pi': 17000, 'claude': 30000, 'codex': 10000, 'opencode': 30000}
TOOL_SOURCES = {'pi': '50KB / 3 bytes per token (estimate)',
                'claude': '2.1.280 tool caps (allow_large needs P7b on newer versions)',
                'codex': 'fleet-pinned tool_output_token_limit',
                'opencode': 'Claude cap (assumption pending P6)'}
MIDTURN_PROMPT = (
    '[FLEET MID-TURN HANDOFF] Context is {used}/{window}. Stop the current work at this point. '
    'Do not start any new step. Only edit the handoff note {path} (three `## ` sections, '
    'same rules as always; in `## In progress` write exactly where you stopped inside the '
    'current instruction, in `## Next steps` what remains of it). Do not send your final '
    'report or call fleet_result / fleet_decision. Then end your turn with the single word '
    'HANDOFF. Your successor continues this same instruction from the note.'
)
CONTINUATION_PROMPT = (
    '[Continuation] You are {label}; {prev} stopped this same instruction midway (context full). '
    'Handoff note: {note}. Continue from it: do not redo finished steps, re-check the step marked '
    'in progress. The original instruction follows unchanged:\n\n{instruction}'
)


def _number(value, name, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(name + ' must be a finite number >= ' + str(minimum))
    return value


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(name + ' must be an integer >= ' + str(minimum))
    return value


def _ratio(value, name):
    _number(value, name)
    if not 0 < value < 1:
        raise ValueError(name + ' must be between 0 and 1')
    return value


def validate_settings(settings):
    """Validate the new keys in a config/card/roster entry; leave existing keys to §22.

    Cross-field inequalities require the resolved window and are checked by limits().
    Zero splits disables splitting; zero wait/nudge/timeout requests immediate escalation.
    """
    if not isinstance(settings, dict):
        raise ValueError('midturn settings must be a mapping')
    for name in DEFAULTS.keys() & settings.keys():
        if name == 'midturn_ratio':
            _ratio(settings[name], name)
        elif name == 'midturn_max_splits':
            _integer(settings[name], name)
        else:
            _number(settings[name], name)


def resolve_settings(cli, model, cfg, table=None, worker=None, card=None):
    """(values, sources), first hit per key: worker > roster > config > card > flag.

    Tables use exact ``cli:model`` then ``cli`` keys, as in §22. ``card`` is
    (profile id, mapping of new keys). This resolves no window or handoff_ratio.
    A default midturn_ratio has source 'default'; pass explicit=False to limits() for it.
    """
    layers = [('worker', worker or {})]
    for where, tab in (('roster', table or {}), ('config', cfg.get('handoff') or {})):
        layers += [(where + ' ' + key, tab[key]) for key in
                   ((cli + ':' + model) if model else None, cli) if key and key in tab]
    if card:
        layers.append(('card ' + card[0], card[1]))
    values, sources = {}, {}
    for where, entry in layers + [('flag', cfg)]:
        validate_settings(entry)
        for key in DEFAULTS.keys() & entry.keys():
            if key not in values:
                values[key] = entry[key]
                sources[key] = '--' + key.replace('_', '-') if where == 'flag' else where
    for key, default in DEFAULTS.items():
        if key not in values:
            values[key], sources[key] = default, 'default'
    if sources['midturn_timeout'] == 'default' and 'handoff_timeout' in cfg:
        values['midturn_timeout'] = _number(cfg['handoff_timeout'], 'handoff_timeout')
        sources['midturn_timeout'] = 'handoff_timeout'
    return values, sources


def limits(cli, window, handoff_ratio, midturn_ratio=None, *, layer='B', k=4, b=None,
           h=8000, reserve_tokens=16384, compact_limit=None, explicit=None, source=''):
    """Return (L1, L2, C, T, R, source); inputs are §22's already resolved values.

    None means the default .75, which may be clamped. Explicit ratios violating either
    bound are rejected, including an empty interval. Default empty intervals fall back to
    L1 per §4.1; source explicitly records that C-R cannot be respected in that case.
    b defaults to 1 for A, 2 for B. k/h overrides are probe measurements supplied by caller.
    """
    if cli not in TOOL_LIMITS or layer not in ('A', 'B'):
        raise ValueError('unknown CLI or midturn layer')
    _integer(window, 'window', 1)
    _ratio(handoff_ratio, 'handoff_ratio')
    if explicit is None:
        explicit = midturn_ratio is not None
    if type(explicit) is not bool:
        raise ValueError('explicit must be boolean')
    ratio = DEFAULTS['midturn_ratio'] if midturn_ratio is None else midturn_ratio
    _ratio(ratio, 'midturn_ratio')
    _integer(k, 'k', 1)
    b = (1 if layer == 'A' else 2) if b is None else b
    _integer(b, 'b', 1)
    _integer(h, 'h')
    if compact_limit is not None:
        c = _integer(compact_limit, 'compact_limit', 1)
        csrc = 'explicit compact_limit'
    elif cli == 'pi':
        c = window - _integer(reserve_tokens, 'reserve_tokens')
        csrc = 'window - reserveTokens=' + str(reserve_tokens)
    elif cli == 'claude':
        c = window - 33000
        csrc = ('~967000 (1M, CHANGELOG 2.1.247)' if window == 1000000
                else 'window - 33000 (assumption pending P5)')
    else:
        c = math.floor(.9 * window)
        csrc = ('fleet-pinned model_auto_compact_token_limit=0.9W' if cli == 'codex'
                else '0.9W (assumption pending P6)')
    t = TOOL_LIMITS[cli]
    r = t * k * b + h
    l1, wanted = handoff_ratio * window, ratio * window
    ceiling = c - r
    if explicit and not l1 <= wanted <= ceiling:
        raise ValueError('midturn_ratio violates L1 <= L2 <= C - R: '
                         + str((l1, wanted, ceiling)))
    l2 = max(l1, min(wanted, ceiling))
    details = [source] if source else []
    details += ['midturn_ratio: ' + str(ratio) + (' explicit' if explicit else ' default'),
                'C=' + str(c) + ': ' + csrc, 'T=' + str(t) + ': ' + TOOL_SOURCES[cli],
                f'R={t}*{k}*{b}+{h}={r} (k: supplied/default 4; b: layer {layer}; '
                'H: supplied/default 8000)']
    if l2 != wanted or ceiling < l1:
        details.append('midturn.clamped: L2=' + str(l2))
    if ceiling < l1:
        details.append('no safe interval: C-R < L1; fallback L2=L1')
    return l1, l2, c, t, r, '; '.join(details)


def decide(used, window, cli, handoff_ratio=.6, midturn_ratio=None, *, busy=True,
           tools_running=False, prompt_wait=False, prompt_route=False, adopted=False,
           clearing=False, clear_required=False, paused=False, profile_switching=False,
           registered=True, advisor=False, ready=False, expired=False, midturn_splits=0,
           midturn_max_splits=3, a_available=None, ordered=False, elapsed=0, b_waited=0,
           midturn_timeout=900, midturn_b_wait=60, **limit_options):
    """Return none / steer / kill / blocked; the caller performs all effects.

    ready means *successor READY before continuation*, not a generally ready busy slot.
    ordered suppresses duplicate A0 steers. elapsed / b_waited exclude prompt-wait time;
    b_waited starts on entering B, including after A times out. A1/A2 remain daemon policy.
    An existing order stays active even if a CLI compaction lowers its reported usage or
    the next usage read fails. Keep passing the window captured when that order was made.
    """
    if (prompt_wait or prompt_route or adopted or clearing or clear_required or paused
            or profile_switching or not registered or advisor or expired):
        return 'none'
    if not busy and not ready:
        return 'none'
    _integer(midturn_splits, 'midturn_splits')
    _integer(midturn_max_splits, 'midturn_max_splits')
    for name, value in (('elapsed', elapsed), ('b_waited', b_waited),
                        ('midturn_timeout', midturn_timeout), ('midturn_b_wait', midturn_b_wait)):
        _number(value, name)
    if (used is None and not ordered) or window is None or window == 0:
        return 'none'
    if used is not None:
        _integer(used, 'used')
    available = MIDTURN_INJECT.get(cli, False) if a_available is None else a_available
    if type(available) is not bool:
        raise ValueError('a_available must be boolean')
    _, l2, c, t, _, _ = limits(cli, window, handoff_ratio, midturn_ratio,
                               layer='A' if available else 'B', **limit_options)
    over = used is not None and used >= l2
    if over and (ready or midturn_splits >= midturn_max_splits):
        return 'blocked'
    if not busy or (not over and not ordered):
        return 'none'
    urgent = used is not None and used >= c - t
    if available and not urgent and (not ordered or elapsed < midturn_timeout):
        return 'none' if ordered else 'steer'
    if urgent or not tools_running or b_waited >= midturn_b_wait:
        return 'kill'
    return 'none'


def continuation(label, prev, note, instruction):
    return CONTINUATION_PROMPT.format(label=label, prev=prev, note=note, instruction=instruction)


def blocked_text(splits):
    _integer(splits, 'midturn_splits')
    return '这条派工一个会话装不下，请拆小（已拆 ' + str(splits) + ' 次）'


def blocked_prompt(splits):
    return ('Stop work immediately. Report Done / Evidence / Not done / Blocked; '
            'in Blocked write: ' + blocked_text(splits))


def _tail_rows(path, size):
    """Read a bounded suffix, dropping a cut first line and malformed/unfinished JSON."""
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - size)
            stream.seek(start)
            tail = stream.read(size)
        if start:
            tail = tail.partition(b'\n')[2]
        for line in reversed(tail.splitlines()):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict):
                yield row
    except (OSError, TypeError, ValueError):
        return


def claude_transcript_usage(path, window):
    """Latest main-thread usage in at most 512KB; missing usage is (None, window).

    A missing measurement must not masquerade as zero or a file-size estimate. No glob,
    transcript discovery, Redis access or writes: caller supplies this generation's file.
    """
    fields = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
    for row in _tail_rows(path, 524288):
        if row.get('type') != 'assistant' or row.get('isSidechain') or row.get('agentId'):
            continue
        message = row.get('message')
        usage = message.get('usage') if isinstance(message, dict) else None
        if not isinstance(usage, dict) or 'input_tokens' not in usage:
            continue
        values = [usage.get(key, 0) for key in fields]
        if all(type(value) is int and value >= 0 for value in values):
            return sum(values), window
    return None, window


def codex_rollout_usage(path, window):
    """Latest token_count.last_token_usage in at most 128KB; never cumulative totals."""
    for row in _tail_rows(path, 131072):
        payload = row.get('payload')
        if (row.get('type') != 'event_msg' or not isinstance(payload, dict)
                or payload.get('type') != 'token_count'):
            continue
        info = payload.get('info')
        if not isinstance(info, dict):
            continue
        usage = info.get('last_token_usage')
        if not isinstance(usage, dict):
            continue
        used = usage.get('total_tokens')
        measured = info.get('model_context_window')
        if type(used) is not int or used < 0:
            continue
        if measured is not None and (type(measured) is not int or measured <= 0):
            continue
        return used, window if measured is None else measured
    return None, window


def parse_usage_frame(frame, *, cli, generation, message_id, slot=None, session_id=None):
    """Validate a flat pi/opencode usage event; return (used, window) or None.

    The bridge adds generation/message_id from its active delivery before forwarding.
    Missing/null tokens, manual turns, stale bindings and non-integral numbers are ignored,
    preserving the daemon's previous measurement. Optional slot/session checks tighten the
    binding when available. Used may exceed window: an overshoot must still trigger policy.
    """
    if isinstance(frame, (str, bytes)):
        try:
            frame = json.loads(frame)
        except (ValueError, UnicodeError):
            return None
    if (cli not in ('pi', 'opencode') or not isinstance(frame, dict)
            or frame.get('kind') != 'usage' or frame.get('phase') == 'manual'
            or type(generation) is not int or generation < 0
            or not isinstance(message_id, str) or not message_id
            or type(frame.get('generation')) is not int or frame['generation'] != generation
            or frame.get('message_id') != message_id):
        return None
    if 'cli' in frame and frame['cli'] != cli:
        return None
    if slot is not None and frame.get('slot') != slot:
        return None
    if session_id is not None and frame.get('session_id') != session_id:
        return None
    used, window = frame.get('ctx_used'), frame.get('ctx_window')
    if type(used) is not int or used < 0 or type(window) is not int or window <= 0:
        return None
    return used, window


def _stored_number(record, key, default=0):
    """Redis hashes contain strings; reject nonfinite/negative clock values."""
    value = record.get(key, default)
    if value in (None, ''):
        value = default
    if isinstance(value, bool):
        raise ValueError('invalid ' + key)
    return _number(float(value), key)


def _paused_at(record):
    value = record.get('clock_paused_at')
    return None if value is None or value == '' else _stored_number(record, 'clock_paused_at')


def effective_age(req, now):
    """Service request age in seconds, excluding completed and current pauses (§5.4).

    This is not the task hours budget: that hard budget continues to use wall time.
    """
    _number(now, 'now')
    created = _stored_number(req, 'created')
    paused = _paused_at(req)
    end = min(now, paused) if paused is not None else now
    return max(0, end - created - _stored_number(req, 'clock_paused_ms') / 1000)


def pause_clock(record, now):
    """Copy with an idempotent pause marker; callers must check expiry before ordering."""
    _number(now, 'now')
    result = dict(record)
    if _paused_at(record) is None:
        result['clock_paused_at'] = now
    return result


def resume_clock(record, now):
    """Copy with actual paused milliseconds accumulated and marker cleared, on every exit."""
    _number(now, 'now')
    result = dict(record)
    paused = _paused_at(record)
    if paused is not None:
        result['clock_paused_ms'] = round(_stored_number(record, 'clock_paused_ms')
                                         + max(0, now - paused) * 1000)
        result['clock_paused_at'] = ''
    return result


def pause_message(message, now_ms, *, midturn_timeout=900, midturn_b_wait=60):
    """Snapshot remaining bus deadline and protect the old delivery during handoff.

    Only the daemon renews the lease. The returned fields belong in the ordering transaction.
    """
    _integer(now_ms, 'now_ms')
    _number(midturn_timeout, 'midturn_timeout')
    _number(midturn_b_wait, 'midturn_b_wait')
    result = dict(message)
    if _paused_at(message) is not None:
        return result
    deadline = _stored_number(message, 'deadline_at')
    if deadline <= now_ms:
        raise ValueError('message deadline already expired; cannot order midturn handoff')
    result = pause_clock(message, now_ms / 1000)
    result['deadline_remaining_ms'] = round(deadline - now_ms)
    result['deadline_at'] = round(deadline + (midturn_timeout + midturn_b_wait) * 1000)
    return result


def resume_message(message, now_ms):
    """Continuation offer: restore exactly the original remaining duration, once."""
    _integer(now_ms, 'now_ms')
    result = resume_clock(message, now_ms / 1000)
    if _paused_at(message) is not None:
        if 'deadline_remaining_ms' not in message:
            raise ValueError('paused message lacks deadline_remaining_ms')
        result['deadline_at'] = now_ms + round(_stored_number(message, 'deadline_remaining_ms'))
    return result


ORDER_FIELD = 'order'
HOOK_IO_TIMEOUT = .05  # caller's Redis connect/read timeout, no retries; never use the Stop reader
HOOK_TIMEOUT = 5  # CLI hook declaration timeout, not a blocking Redis timeout


def order_key(prefix, slot, generation):
    _integer(generation, 'generation')
    return prefix + ':midturn:' + slot + ':g' + str(generation)


def order_record(slot, generation, thread, message_id, hid, draft, used, window,
                 deadline, *, transcript, layer='A'):
    """JSON record stored as hash field 'order' at order_key(), by the daemon transaction.

    thread/transcript are the bound *main* session, never discovered by the hook. The daemon
    removes this field on settlement/retirement and while prompt-wait guards suspend injection.
    All expiry times here are seconds. Throttle fields live beside this immutable order.
    """
    _integer(generation, 'generation')
    _integer(used, 'used')
    _integer(window, 'window', 1)
    _number(deadline, 'deadline')
    for value in (slot, thread, message_id, hid, draft, transcript):
        if not isinstance(value, str) or not value:
            raise ValueError('order identity, draft and main transcript must be nonempty strings')
    if layer not in ('A', 'B'):
        raise ValueError('invalid midturn layer')
    return {'slot': slot, 'generation': generation, 'thread': thread, 'message_id': message_id,
            'midturn_hid': hid, 'midturn_draft': draft, 'midturn_used': used, 'window': window,
            'midturn_deadline': deadline, 'midturn_layer': layer, 'transcript': transcript}


# A hit costs one extra round trip, atomically fencing cancellation/replacement and simultaneous
# PostToolUse processes. Never recreate a deleted order or overwrite daemon-owned order fields.
LUA_NOTICE = """
if redis.call('HGET', KEYS[1], 'order') ~= ARGV[1] then return 0 end
local hid, now = ARGV[2], tonumber(ARGV[3])
local previous = redis.call('HGET', KEYS[1], 'notice_hid')
local tools = 0
if previous == hid then tools = redis.call('HINCRBY', KEYS[1], 'notice_tools', 1) end
local last = tonumber(redis.call('HGET', KEYS[1], 'notice_at') or '0')
if previous ~= hid or tools >= 3 or now - last >= 120 then
  redis.call('HSET', KEYS[1], 'notice_hid', hid, 'notice_tools', 0, 'notice_at', ARGV[3])
  return 1
end
return 0
"""


def posttool_hook(r, prefix, slot, generation, data, *, now=None):
    """Return additionalContext JSON or ''; fail open on all input / Redis errors.

    Inject a short-timeout Redis client (HOOK_IO_TIMEOUT, no retries). No client creation,
    config reads, transcript reads, polling or usage calculation happens here. A miss is one
    HGET. Claude and a probe-enabled Codex PostToolUse adapter can share this entry point.
    Unknown provenance is skipped: require the exact bound main transcript and session,
    and reject subagent markers even when they reuse their parent's session_id.
    """
    try:
        key = order_key(prefix, slot, generation)
        raw = r.hget(key, ORDER_FIELD)
        if not raw:
            return ''
        record = json.loads(raw)
        if isinstance(data, (str, bytes)):
            data = json.loads(data)
        if not isinstance(record, dict) or not isinstance(data, dict):
            return ''
        if any(data.get(name) for name in ('agent_id', 'agentId', 'parent_agent_id',
                                            'isSidechain', 'is_sidechain', 'subagent_id')):
            return ''
        if data.get('hook_event_name') != 'PostToolUse':
            return ''
        # Validate the stored schema too; malformed/unbound orders must never reach a model.
        checked = order_record(record['slot'], record['generation'], record['thread'],
                               record['message_id'], record['midturn_hid'], record['midturn_draft'],
                               record['midturn_used'], record['window'], record['midturn_deadline'],
                               transcript=record['transcript'], layer=record['midturn_layer'])
        if (checked['slot'] != slot or checked['generation'] != generation
                or checked['midturn_layer'] != 'A' or record.get('suspended')
                or checked['thread'] != data.get('session_id')
                or checked['transcript'] != data.get('transcript_path')):
            return ''
        # Some adapters can also supply the active message; never accept a mismatch.
        if 'message_id' in data and data['message_id'] != checked['message_id']:
            return ''
        at = time.time() if now is None else _number(now, 'now')
        if at >= checked['midturn_deadline']:
            return ''
        text = MIDTURN_PROMPT.format(used=checked['midturn_used'], window=checked['window'],
                                     path=checked['midturn_draft'])
        if r.eval(LUA_NOTICE, 1, key, raw, checked['midturn_hid'], at) != 1:
            return ''
        return json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse',
                                                  'additionalContext': text}}, ensure_ascii=False)
    except Exception:  # fail-open is deliberate: a diagnostic hook cannot stall every tool call
        return ''


def posttool_main(r, prefix, slot, generation, data, *, now=None, output=None):
    """Hook command body: stdout empty on miss/failure, exit status always zero."""
    text = posttool_hook(r, prefix, slot, generation, data, now=now)
    if text:
        if output is None:
            import sys
            output = sys.stdout
        output.write(text + '\n')
    return 0
