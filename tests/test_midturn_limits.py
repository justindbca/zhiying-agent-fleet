"""T1 policy, bounded usage readers and fail-open hooks; no CLI or Redis server needed."""
import io
import json
import subprocess
import sys
import time

import pytest

from cmux_fleet import midturn


def test_example_pi_deepseek_200k_default_batch():
    l1, l2, c, t, r, source = midturn.limits('pi', 200000, .6, layer='A')
    assert l1 == 120000
    assert c == 183616
    assert t == 17000
    assert r == 17000 * 4 * 1 + 8000 == 76000
    assert c - r == 107616
    assert l2 == l1 == 120000
    assert 'midturn.clamped' in source
    assert 'no safe interval' in source


def test_example_pi_deepseek_200k_measured_single_tool():
    l1, l2, c, t, r, source = midturn.limits('pi', 200000, .6, layer='A', k=1)
    assert l1 == 120000
    assert c == 183616
    assert t == 17000
    assert r == 17000 * 1 * 1 + 8000 == 25000
    assert c - r == 158616
    assert l2 == 150000
    assert l1 <= l2 <= c - r
    assert 'midturn.clamped' not in source


def test_example_claude_opus_sonnet_1m():
    l1, l2, c, t, r, source = midturn.limits('claude', 1000000, .6, layer='A')
    assert l1 == 600000
    assert c == 967000
    assert t == 30000
    assert r == 30000 * 4 * 1 + 8000 == 128000
    assert c - r == 839000
    assert l2 == 750000
    assert l1 <= l2 <= c - r
    assert '2.1.247' in source


def test_example_claude_haiku_200k():
    l1, l2, c, t, r, source = midturn.limits('claude', 200000, .6, layer='A')
    assert l1 == 120000
    assert c == 167000
    assert t == 30000
    assert r == 30000 * 4 * 1 + 8000 == 128000
    assert c - r == 39000
    assert l2 == l1 == 120000
    assert 'assumption pending P5' in source
    assert 'midturn.clamped' in source


def test_example_codex_400k():
    l1, l2, c, t, r, source = midturn.limits('codex', 400000, .6, layer='A')
    assert l1 == 240000
    assert c == 360000
    assert t == 10000
    assert r == 10000 * 4 * 1 + 8000 == 48000
    assert c - r == 312000
    assert l2 == 300000
    assert l1 <= l2 <= c - r
    assert 'fleet-pinned model_auto_compact_token_limit=0.9W' in source


def test_example_flashnext_100k_card_ratio():
    l1, l2, c, t, r, source = midturn.limits('pi', 100000, .5, layer='A')
    assert l1 == 50000
    assert c == 83616
    assert t == 17000
    assert r == 17000 * 4 * 1 + 8000 == 76000
    assert c - r == 7616
    assert l2 == l1 == 50000
    assert 'midturn.clamped' in source


def test_b_layer_and_opencode_assumptions():
    a = midturn.limits('opencode', 400000, .6, layer='A')
    b = midturn.limits('opencode', 400000, .6)
    assert a[2:5] == (360000, 30000, 128000)
    assert b[2:5] == (360000, 30000, 248000)
    assert a[1] == b[1] == 240000
    assert 'assumption pending P6' in b[5]
    assert 'layer B' in b[5]
    assert all(value is False for value in midturn.MIDTURN_INJECT.values())


def test_default_clamps_to_ceiling_or_l1_and_keeps_resolved_source():
    result = midturn.limits('codex', 400000, .6, source='window: measured; ratio: card astra')
    assert result[:5] == (240000, 272000, 360000, 10000, 88000)
    assert result[5].startswith('window: measured; ratio: card astra;')
    assert 'midturn.clamped' in result[5]
    assert 'no safe interval' not in result[5]
    assert midturn.limits('claude', 1000000, .8, layer='A')[1] == 800000
    # Same numeric value, explicitly supplied: refuse rather than silently clamp it.
    with pytest.raises(ValueError, match='L1 <= L2 <= C - R'):
        midturn.limits('codex', 400000, .6, .75)
    assert midturn.limits('codex', 400000, .6, .75, explicit=False)[1] == 272000


@pytest.mark.parametrize('ratio', [.5, .9])
def test_explicit_inequality_is_rejected(ratio):
    with pytest.raises(ValueError, match='midturn_ratio violates'):
        midturn.limits('claude', 1000000, .6, ratio, layer='A')


def test_explicit_empty_interval_is_rejected_and_boundaries_are_legal():
    with pytest.raises(ValueError, match='midturn_ratio violates'):
        midturn.limits('pi', 200000, .6, .6, layer='A')
    assert midturn.limits('claude', 1000000, .6, .6, layer='A')[1] == 600000
    assert midturn.limits('claude', 1000000, .6, .839, layer='A')[1] == 839000


def test_measured_reserve_compact_and_margin_parameters():
    result = midturn.limits('pi', 200000, .6, layer='A', reserve_tokens=10000, k=1, b=3, h=1000)
    assert result[:5] == (120000, 138000, 190000, 17000, 52000)
    assert 'R=17000*1*3+1000=52000' in result[5]
    assert midturn.limits('codex', 400000, .6, compact_limit=350000)[2] == 350000
    assert 'explicit compact_limit' in midturn.limits('claude', 200000, .6, compact_limit=180000)[5]


@pytest.mark.parametrize('key,value', [
    ('midturn_ratio', True), ('midturn_ratio', '0.75'), ('midturn_ratio', None),
    ('midturn_ratio', 0), ('midturn_ratio', 1), ('midturn_ratio', float('nan')),
    ('midturn_ratio', float('inf')), ('midturn_max_splits', True),
    ('midturn_max_splits', 1.5), ('midturn_max_splits', -1),
    ('midturn_timeout', float('inf')), ('midturn_b_wait', -1), ('midturn_nudge', '180'),
])
def test_invalid_new_settings(key, value):
    with pytest.raises(ValueError):
        midturn.validate_settings({key: value})


@pytest.mark.parametrize('options', [
    {'window': True}, {'window': 0}, {'window': 2.5}, {'handoff_ratio': True},
    {'handoff_ratio': 1}, {'k': 0}, {'k': True}, {'b': 0}, {'b': 1.5},
    {'h': -1}, {'compact_limit': 0}, {'reserve_tokens': -1}, {'layer': 'C'}, {'cli': 'unknown'},
])
def test_invalid_limit_inputs(options):
    args = dict(cli='pi', window=200000, handoff_ratio=.6)
    args.update(options)
    with pytest.raises(ValueError):
        midturn.limits(**args)


def test_five_layer_resolution_each_key_independently():
    cfg = {'midturn_ratio': .61, 'handoff_timeout': 333,
           'handoff': {'pi': {'midturn_ratio': .63}, 'pi:model': {'midturn_ratio': .64}}}
    table = {'pi': {'midturn_ratio': .65}, 'pi:model': {'midturn_ratio': .66}}
    worker = {'midturn_ratio': .67, 'midturn_max_splits': 2}
    card = ('card-id', {'midturn_ratio': .62, 'midturn_nudge': 40})
    values, sources = midturn.resolve_settings('pi', 'model', cfg, table, worker, card)
    assert values['midturn_ratio'] == .67 and sources['midturn_ratio'] == 'worker'
    assert values['midturn_max_splits'] == 2
    assert values['midturn_nudge'] == 40 and sources['midturn_nudge'] == 'card card-id'
    assert values['midturn_timeout'] == 333 and sources['midturn_timeout'] == 'handoff_timeout'
    assert values['midturn_b_wait'] == 60
    assert midturn.resolve_settings('pi', 'model', cfg, table, card=card)[0]['midturn_ratio'] == .66
    assert midturn.resolve_settings('pi', 'other', cfg, table, card=card)[0]['midturn_ratio'] == .65
    assert midturn.resolve_settings('pi', 'model', cfg, card=card)[0]['midturn_ratio'] == .64
    assert midturn.resolve_settings('pi', 'other', cfg, card=card)[0]['midturn_ratio'] == .63
    flags = {'midturn_ratio': .61}
    assert midturn.resolve_settings('pi', 'model', flags, card=card)[0]['midturn_ratio'] == .62
    assert midturn.resolve_settings('pi', 'model', flags)[0]['midturn_ratio'] == .61
    assert midturn.resolve_settings('pi', 'model', flags)[1]['midturn_ratio'] == '--midturn-ratio'
    assert midturn.resolve_settings('pi', 'model', {})[0] == midturn.DEFAULTS
    assert midturn.resolve_settings('pi', 'model', {})[1]['midturn_ratio'] == 'default'
    assert worker == {'midturn_ratio': .67, 'midturn_max_splits': 2}


def test_invalid_shadowed_setting_still_refuses_entry():
    with pytest.raises(ValueError):
        midturn.resolve_settings('pi', '', {'midturn_ratio': False}, worker={'midturn_ratio': .7})


def test_invalid_capability_and_settings_container_cannot_enable_a():
    with pytest.raises(ValueError, match='mapping'):
        midturn.validate_settings([])
    with pytest.raises(ValueError, match='a_available'):
        midturn.decide(750000, 1000000, 'claude', a_available='false')
    with pytest.raises(ValueError, match='explicit'):
        midturn.limits('claude', 1000000, .6, explicit='false')


@pytest.mark.parametrize('cli,window,used', [
    ('pi', 200000, 120000), ('claude', 1000000, 750000),
    ('codex', 400000, 300000), ('opencode', 1000000, 750000),
])
def test_all_clis_a_or_b_and_threshold_boundary(cli, window, used):
    assert midturn.decide(used - 1, window, cli, a_available=True) == 'none'
    assert midturn.decide(used, window, cli, a_available=True) == 'steer'
    assert midturn.decide(used, window, cli, a_available=False) == 'kill'
    assert midturn.decide(used, window, cli) == 'kill'  # no unproven A capability


@pytest.mark.parametrize('guard', ['prompt_wait', 'prompt_route', 'adopted', 'clearing',
                                  'clear_required', 'paused', 'profile_switching', 'advisor', 'expired'])
@pytest.mark.parametrize('ordered', [False, True])
def test_guards_precede_steering_killing_and_blocking(guard, ordered):
    assert midturn.decide(999999, 1000000, 'claude', ordered=ordered, elapsed=9999,
                           ready=True, midturn_splits=3, **{guard: True}) == 'none'


def test_no_delivery_no_usage_and_unregistered_slot():
    assert midturn.decide(800000, 1000000, 'claude', busy=False) == 'none'
    assert midturn.decide(800000, 1000000, 'claude', registered=False) == 'none'
    assert midturn.decide(None, 1000000, 'claude') == 'none'
    assert midturn.decide(800000, None, 'claude') == 'none'
    assert midturn.decide(800000, 0, 'claude') == 'none'


def test_split_limit_and_successor_ready_are_blocked_only_when_over_line():
    assert midturn.decide(750000, 1000000, 'claude', a_available=True, midturn_splits=2) == 'steer'
    assert midturn.decide(750000, 1000000, 'claude', a_available=True, midturn_splits=3) == 'blocked'
    assert midturn.decide(749999, 1000000, 'claude', a_available=True, midturn_splits=3) == 'none'
    assert midturn.decide(750000, 1000000, 'claude', midturn_max_splits=0) == 'blocked'
    assert midturn.decide(750000, 1000000, 'claude', a_available=True, busy=False, ready=True) == 'blocked'
    assert midturn.decide(749999, 1000000, 'claude', a_available=True, busy=False, ready=True) == 'none'


def test_a_timeout_b_wait_gap_and_emergency_boundaries():
    args = dict(used=750000, window=1000000, cli='claude', a_available=True, ordered=True)
    assert midturn.decide(**args, elapsed=899) == 'none'  # no duplicate A0
    assert midturn.decide(**args, elapsed=900) == 'kill'
    assert midturn.decide(**args, elapsed=900, tools_running=True, b_waited=59.99) == 'none'
    assert midturn.decide(**args, elapsed=900, tools_running=True, b_waited=60) == 'kill'
    args.update(used=936999, tools_running=True)
    assert midturn.decide(**args) == 'none'
    args['used'] = 937000  # C - T; no wait, even before A's timeout
    assert midturn.decide(**args) == 'kill'
    assert midturn.decide(937000, 1000000, 'claude', a_available=True, tools_running=True) == 'kill'


def test_b_unavailable_a_waits_for_tools_but_never_forever():
    args = dict(used=750000, window=1000000, cli='claude', a_available=False, tools_running=True)
    assert midturn.decide(**args, b_waited=59) == 'none'
    assert midturn.decide(**args, b_waited=60) == 'kill'
    assert midturn.decide(**args, midturn_b_wait=0) == 'kill'
    args['tools_running'] = False
    assert midturn.decide(**args) == 'kill'


def test_order_survives_a_usage_drop_after_unexpected_compaction():
    assert midturn.decide(1000, 1000000, 'claude', ordered=True, elapsed=900, a_available=True) == 'kill'
    assert midturn.decide(1000, 1000000, 'claude', ordered=False, a_available=True) == 'none'
    assert midturn.decide(None, 1000000, 'claude', ordered=True, elapsed=899, a_available=True) == 'none'
    assert midturn.decide(None, 1000000, 'claude', ordered=True, elapsed=900, a_available=True) == 'kill'


def test_zero_waits_and_zero_split_limit_are_valid_config():
    values = {'midturn_max_splits': 0, 'midturn_timeout': 0, 'midturn_b_wait': 0, 'midturn_nudge': 0}
    midturn.validate_settings(values)
    assert midturn.resolve_settings('pi', '', values)[0]['midturn_max_splits'] == 0


def test_business_text_preserves_labels_instruction_and_existing_report_contract():
    instruction = 'Original "quoted" task\n\t下一行 {untouched}'
    text = midturn.continuation('worker-1.1', 'worker-1', '/handoffs/note.md', instruction)
    assert text.startswith('[Continuation] You are worker-1.1; worker-1 stopped')
    assert text.endswith(instruction)
    assert 're-check the step marked in progress' in text
    assert '已拆 3 次' in midturn.blocked_text(3)
    assert 'Done / Evidence / Not done / Blocked' in midturn.blocked_prompt(3)


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))


def claude_row(used, **fields):
    return dict(type='assistant', message={'usage': {'input_tokens': used}}, **fields)


def codex_row(used, window=400000):
    return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'last_token_usage': {'total_tokens': used}, 'model_context_window': window,
        'total_token_usage': {'total_tokens': 9000000}}}}


def test_claude_latest_main_thread_usage_includes_cache_and_output(tmp_path):
    path = tmp_path / 'transcript.jsonl'
    row = claude_row(100)
    row['message']['usage'].update(cache_read_input_tokens=200, cache_creation_input_tokens=30, output_tokens=4)
    write_rows(path, [claude_row(99999), row, claude_row(88888, isSidechain=True),
                      claude_row(77777, agentId='subagent'), {'type': 'user'}, [], None])
    with path.open('a') as stream:
        stream.write('{"type":"assistant","message":')  # concurrent writer not finished
    assert midturn.claude_transcript_usage(path, 1000000) == (334, 1000000)


@pytest.mark.parametrize('bad', [True, -1, '999', 1.5, None, float('nan')])
def test_bad_claude_usage_skips_row_without_erasing_previous(tmp_path, bad):
    path = tmp_path / 'claude.jsonl'
    write_rows(path, [claude_row(45), claude_row(bad), {'type': 'assistant', 'message': []}])
    assert midturn.claude_transcript_usage(path, 200000) == (45, 200000)


def test_codex_latest_context_not_cumulative_and_window_fallback(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, [codex_row(100000), codex_row(25000, 258400),
                      {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None}},
                      dict(codex_row(999999), type='response_item')])
    assert midturn.codex_rollout_usage(path, 400000) == (25000, 258400)
    write_rows(path, [codex_row(0, None)])
    assert midturn.codex_rollout_usage(path, 400000) == (0, 400000)


@pytest.mark.parametrize('used,window', [(True, 400000), ('9', 400000), (-1, 400000),
                                        (1.5, 400000), (4, True), (4, '400000'), (4, 0)])
def test_invalid_codex_usage_and_window_skip_row(tmp_path, used, window):
    path = tmp_path / 'rollout.jsonl'
    write_rows(path, [codex_row(40), codex_row(used, window)])
    assert midturn.codex_rollout_usage(path, 100000) == (40, 400000)


@pytest.mark.parametrize('reader', [midturn.claude_transcript_usage, midturn.codex_rollout_usage])
def test_missing_or_invalid_measurement_is_unknown_not_file_size(tmp_path, reader):
    path = tmp_path / 'missing.jsonl'
    assert reader(path, 200000) == (None, 200000)
    path.write_text('not JSON\n' * 1000 + '[]\nnull\n')
    assert reader(path, 200000) == (None, 200000)
    assert reader(tmp_path, 200000) == (None, 200000)


@pytest.mark.parametrize('reader,row,size', [
    (midturn.claude_transcript_usage, claude_row, 524288),
    (midturn.codex_rollout_usage, codex_row, 131072),
])
def test_reader_only_reads_bounded_suffix_and_ignores_cut_first_row(tmp_path, monkeypatch, reader, row, size):
    path = tmp_path / 'large.jsonl'
    path.write_bytes((json.dumps(row(99999)) + '\n').encode() + b'x' * (size * 3)
                     + b'\n' + (json.dumps(row(42)) + '\n').encode())
    original_open = open
    reads = []

    class TailOnly:
        def __enter__(self):
            self.stream = original_open(path, 'rb')
            return self

        def __exit__(self, *args):
            self.stream.close()

        def seek(self, *args):
            return self.stream.seek(*args)

        def tell(self):
            return self.stream.tell()

        def read(self, count=-1):
            assert count == size
            assert self.stream.tell() == path.stat().st_size - size
            reads.append(count)
            return self.stream.read(count)

    monkeypatch.setattr(midturn, 'open', lambda *args: TailOnly(), raising=False)
    assert reader(path, 400000) == (42, 400000)
    assert reads == [size]


@pytest.mark.parametrize('cli', ['pi', 'opencode'])
def test_usage_frame_binding_and_overshoot(cli):
    frame = {'kind': 'usage', 'generation': 2, 'message_id': 'm-1', 'ctx_used': 200001,
             'ctx_window': 200000, 'slot': 'worker-1', 'session_id': 'session', 'cli': cli}
    args = dict(cli=cli, generation=2, message_id='m-1', slot='worker-1', session_id='session')
    assert midturn.parse_usage_frame(frame, **args) == (200001, 200000)
    assert midturn.parse_usage_frame(json.dumps(frame), **args) == (200001, 200000)
    assert midturn.parse_usage_frame(dict(frame, ctx_used=0), **args) == (0, 200000)
    for change in ({'generation': 1}, {'generation': True}, {'message_id': 'old'},
                   {'slot': 'worker-2'}, {'session_id': 'old'}, {'phase': 'manual'}, {'cli': 'claude'}):
        assert midturn.parse_usage_frame(dict(frame, **change), **args) is None


@pytest.mark.parametrize('change', [
    {'ctx_used': None}, {'ctx_used': True}, {'ctx_used': '4'}, {'ctx_used': -1},
    {'ctx_used': 1.5}, {'ctx_used': float('nan')}, {'ctx_window': 0}, {'ctx_window': -1},
    {'ctx_window': '200000'}, {'ctx_window': True}, {'kind': 'settled'}, {'message_id': None},
])
def test_invalid_usage_frame_preserves_previous_measurement(change):
    frame = dict(kind='usage', generation=0, message_id='m', ctx_used=4, ctx_window=200000)
    assert midturn.parse_usage_frame(dict(frame, **change), cli='pi', generation=0, message_id='m') is None


def test_usage_frame_missing_fields_and_invalid_envelope():
    args = dict(cli='pi', generation=0, message_id='m')
    frame = dict(kind='usage', generation=0, message_id='m', ctx_used=4, ctx_window=200000)
    for key in frame:
        partial = {k: v for k, v in frame.items() if k != key}
        assert midturn.parse_usage_frame(partial, **args) is None
    for bad in ('{', '[]', None, 3, b'\xff'):
        assert midturn.parse_usage_frame(bad, **args) is None
    assert midturn.parse_usage_frame(frame, **dict(args, message_id='')) is None
    assert midturn.parse_usage_frame(frame, **dict(args, generation=True)) is None
    assert midturn.parse_usage_frame(frame, **dict(args, cli='claude')) is None


def test_effective_age_freezes_accumulates_and_resumes_without_mutation():
    request = {'created': '100', 'clock_paused_ms': '1250', 'spec': '{}'}
    assert midturn.effective_age(request, 111.25) == 10
    paused = midturn.pause_clock(request, 111.25)
    assert 'clock_paused_at' not in request
    assert midturn.pause_clock(paused, 120) == paused
    assert midturn.effective_age(paused, 120) == 10
    assert midturn.effective_age(paused, 1000) == 10
    resumed = midturn.resume_clock(paused, 121.5)
    assert resumed['clock_paused_ms'] == 11500
    assert resumed['clock_paused_at'] == ''
    assert paused['clock_paused_at'] == 111.25
    assert midturn.effective_age(resumed, 122.5) == 11
    assert midturn.resume_clock(resumed, 999) == resumed
    again = midturn.resume_clock(midturn.pause_clock(resumed, 130), 135)
    assert again['clock_paused_ms'] == 16500
    assert midturn.effective_age(again, 140) == 23.5
    assert 140 - float(request['created']) == 40  # task hard hours still wall time


def test_pause_at_zero_redis_strings_and_clock_backwards():
    request = {'created': '0', 'clock_paused_at': '0', 'clock_paused_ms': '0'}
    assert midturn.effective_age(request, 10) == 0
    assert midturn.resume_clock(request, 10)['clock_paused_ms'] == 10000
    request = dict(created=50, clock_paused_at=100, clock_paused_ms=1000)
    assert midturn.resume_clock(request, 99)['clock_paused_ms'] == 1000
    assert midturn.effective_age(request, 10) == 0


def test_message_deadline_pause_resume_and_no_double_extension():
    message = {'deadline_at': '200000', 'midturn_splits': '2', 'replay': '1'}
    paused = midturn.pause_message(message, 150000)
    assert paused['deadline_at'] == 1160000  # original + (900 + 60) seconds
    assert paused['deadline_remaining_ms'] == 50000
    assert paused['clock_paused_at'] == 150
    assert midturn.pause_message(paused, 180000) == paused
    resumed = midturn.resume_message(paused, 500000)
    assert resumed['deadline_at'] == 550000
    assert resumed['clock_paused_ms'] == 350000
    assert resumed['clock_paused_at'] == ''
    assert resumed['midturn_splits'] == '2' and resumed['replay'] == '1'
    assert midturn.resume_message(resumed, 800000) == resumed
    assert message == {'deadline_at': '200000', 'midturn_splits': '2', 'replay': '1'}


@pytest.mark.parametrize('at', [200000, 200001])
def test_already_expired_message_cannot_be_paused(at):
    with pytest.raises(ValueError, match='already expired'):
        midturn.pause_message({'deadline_at': '200000'}, at)


@pytest.mark.parametrize('value', ['nan', 'inf', -1, True])
def test_invalid_clock_storage_rejected(value):
    with pytest.raises(ValueError):
        midturn.effective_age({'created': 10, 'clock_paused_ms': value}, 20)


@pytest.mark.parametrize('exit_reason', ['kill_failed', 'blocked', 'completed', 'cancelled'])
def test_every_noncontinuation_exit_can_restore_clock(exit_reason):
    request = midturn.pause_clock({'created': '100', 'status': exit_reason}, 105)
    resumed = midturn.resume_clock(request, 110)
    assert resumed['clock_paused_ms'] == 5000
    assert midturn.effective_age(resumed, 120) == 15


class FakeRedis:
    """Only the hook's HGET + atomic notice claim; no server or retries."""
    def __init__(self, record=None):
        self.key = midturn.order_key('private-run', 'worker-1', 2)
        self.order = json.dumps(record) if record is not None else None
        self.calls = []
        self.hid, self.tools, self.last = None, 0, 0
        self.replace_on_eval = False

    def hget(self, key, field):
        self.calls.append(('hget', key, field))
        return self.order if key == self.key else None

    def eval(self, script, count, key, raw, hid, now):
        self.calls.append(('eval', key))
        assert script == midturn.LUA_NOTICE and count == 1
        if self.replace_on_eval:
            self.order = None
        if self.order != raw or key != self.key:
            return 0
        if self.hid == hid:
            self.tools += 1
        if self.hid != hid or self.tools >= 3 or now - self.last >= 120:
            self.hid, self.tools, self.last = hid, 0, now
            return 1
        return 0


def hook_order(**changes):
    record = midturn.order_record('worker-1', 2, 'session-2', 'm-2', 'h-2',
                                  '/handoffs/中文 "note".md', 750000, 1000000, 2000,
                                  transcript='/sessions/main.jsonl')
    return dict(record, **changes)


def hook_data(**changes):
    return dict(hook_event_name='PostToolUse', session_id='session-2',
                transcript_path='/sessions/main.jsonl', **changes)


def run_hook(r, data=None, now=1000, generation=2):
    return midturn.posttool_hook(r, 'private-run', 'worker-1', generation,
                                 hook_data() if data is None else data, now=now)


def test_hook_miss_is_one_hget_under_200ms_and_exit_zero():
    r, output = FakeRedis(), io.StringIO()
    start = time.perf_counter()
    assert midturn.posttool_main(r, 'private-run', 'worker-1', 2, hook_data(), output=output) == 0
    assert time.perf_counter() - start < .2
    assert output.getvalue() == ''
    assert r.calls == [('hget', 'private-run:midturn:worker-1:g2', 'order')]


@pytest.mark.parametrize('failure', [ConnectionError, TimeoutError, OSError, ValueError])
def test_hook_unavailable_redis_is_silent_exit_zero(failure):
    class Unavailable:
        def hget(self, *args):
            raise failure('unavailable')

    output = io.StringIO()
    assert midturn.posttool_main(Unavailable(), 'private-run', 'worker-1', 2,
                                 hook_data(), output=output) == 0
    assert output.getvalue() == ''


def test_hook_emits_exact_additional_context_json_and_exit_zero():
    r, output = FakeRedis(hook_order()), io.StringIO()
    assert midturn.posttool_main(r, 'private-run', 'worker-1', 2, json.dumps(hook_data()),
                                 now=1000, output=output) == 0
    result = json.loads(output.getvalue())
    assert result == {'hookSpecificOutput': {'hookEventName': 'PostToolUse',
        'additionalContext': midturn.MIDTURN_PROMPT.format(
            used=750000, window=1000000, path='/handoffs/中文 "note".md')}}
    assert 'fleet_result / fleet_decision' in output.getvalue()


@pytest.mark.parametrize('change', [
    {'session_id': 'other'}, {'transcript_path': '/sessions/subagents/a.jsonl'},
    {'transcript_path': None}, {'hook_event_name': 'Stop'}, {'agent_id': 'child'},
    {'agentId': 'child'}, {'parent_agent_id': 'parent'}, {'isSidechain': True},
    {'is_sidechain': True}, {'subagent_id': 'child'}, {'message_id': 'other'},
])
def test_hook_rejects_unbound_or_child_session(change):
    r = FakeRedis(hook_order())
    assert run_hook(r, dict(hook_data(), **change)) == ''
    assert len(r.calls) == 1


@pytest.mark.parametrize('change', [
    {'generation': 1}, {'generation': True}, {'slot': 'worker-2'}, {'thread': 'other'},
    {'midturn_layer': 'B'}, {'midturn_deadline': 999}, {'midturn_deadline': 1000},
    {'midturn_draft': ''}, {'window': True}, {'message_id': ''}, {'suspended': True},
])
def test_hook_rejects_stale_invalid_or_suspended_order(change):
    r = FakeRedis(hook_order(**change))
    assert run_hook(r) == ''
    assert len(r.calls) == 1


def test_hook_generation_key_wrong_prefix_and_atomic_cancellation():
    r = FakeRedis(hook_order())
    assert run_hook(r, generation=1) == ''
    assert midturn.posttool_hook(r, 'another-run', 'worker-1', 2, hook_data(), now=1000) == ''
    r.replace_on_eval = True
    assert run_hook(r) == ''
    assert r.order is None


def test_hook_reminds_after_three_more_tools_or_120_seconds():
    r = FakeRedis(hook_order())
    assert run_hook(r, now=1000)
    assert run_hook(r, now=1001) == ''
    assert run_hook(r, now=1002) == ''
    assert run_hook(r, now=1003)
    assert run_hook(r, now=1122.999) == ''
    assert run_hook(r, now=1123)
    r.order = json.dumps(hook_order(midturn_hid='new-order'))
    assert run_hook(r, now=1124)  # a new order is never throttled by the previous one


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '{}', b'\xff'])
def test_hook_malformed_order_fails_open(raw):
    r = FakeRedis()
    r.order = raw
    assert run_hook(r) == ''


def test_hook_malformed_input_and_notice_write_failure_are_silent():
    for raw in ('{', '[]', 'null', '{}', b'\xff'):
        assert run_hook(FakeRedis(hook_order()), raw) == ''

    class WriteFailure(FakeRedis):
        def eval(self, *args):
            raise TimeoutError('Redis read timed out')

    output = io.StringIO()
    assert midturn.posttool_main(WriteFailure(hook_order()), 'private-run', 'worker-1', 2,
                                 hook_data(), now=1000, output=output) == 0
    assert output.getvalue() == ''


def test_module_standalone_import_and_hook_do_not_load_fleet_or_redis():
    script = '''
import sys
from cmux_fleet import midturn
class Missing:
    def hget(self, *args): return None
assert midturn.posttool_main(Missing(), 'private-run', 'worker-1', 0, '{}') == 0
assert 'cmux_fleet.fleet' not in sys.modules
assert 'cmux_fleet.runtime_v2' not in sys.modules
assert 'redis' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
