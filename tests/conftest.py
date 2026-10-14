"""Suite-wide test defaults: neither no-typing channel ever types into a pane.

Codex: a fake `codex queue` for every test (Codex deliveries never type into a pane).
The real CLI is never run by the unit tests. A queued delivery is recorded in its own log,
`mgr.queued` (FakeMgr / test_subfleet's World), never in `mgr.sent`: `sent` stays the record of real
`send_prompt_to_idle` pastes only, so "nothing was typed" assertions keep their meaning. Its turn is
started in the rollout the way the real TUI does it (test_fleet.codex_rows). A test that installs its
own `codex_queue.enqueue` stub keeps it: then only the real deliver runs.
Guard (every test): a pane that received a queued Codex delivery must never also get a paste.

Claude: Claude workers get their deliveries through the Stop-hook channel (cmux_fleet/claude_hook.py) in
every test. A real hook blocks waiting for the next delivery, which an in-process test cannot do,
so tests/hook_sim.py stands in for that wait only (see its docstring). A module that runs the real
hook command in subprocesses sets ``HOOK_CHANNEL = True`` and gets no stand-in.
"""
import uuid

import pytest

import test_fleet  # first: puts the checkout on sys.path
import hook_sim  # noqa: E402
from cmux_fleet import codex_queue, fleet  # noqa: E402


@pytest.fixture(autouse=True)
def fake_codex_queue(monkeypatch):
    test_fleet.CODEX_QUEUE.clear()
    real_deliver = codex_queue.deliver

    def fake_enqueue(cfg, name, thread, text, **kw):
        if not thread:
            raise codex_queue.QueueError('failed: no bound Codex session to queue into')
        return {'queued_id': 'q-' + uuid.uuid4().hex, 'thread': thread}

    logs = []  # (queued, sent) pairs of every manager that took a queued delivery

    def deliver(f, slot, delivery, text, thread=None):
        got = real_deliver(f, slot, delivery, text, thread=thread)
        if codex_queue.enqueue is fake_enqueue:
            box = f.mgr if hasattr(f.mgr, 'sent') else f.mgr.w  # FakeMgr / test_subfleet WorldMgr
            if not hasattr(box, 'queued'):
                box.queued = []
            box.queued.append((f.agent(slot).get('pane'), text))  # what reached the agent (its token line is metadata)
            if not any(q is box.queued for q, _ in logs):
                logs.append((box.queued, box.sent))
            test_fleet.CODEX_QUEUE[got['thread']] = {'turn': 't-' + uuid.uuid4().hex, 'text': got['text'], 'body': text}
            _, rows = test_fleet.codex_rows(got['thread'], got['text'], pop=False)
            test_fleet.append_rollout(f.sessions, got['thread'], fleet.agent_cwd(f.runtime_cfg(slot), slot), rows)
        return got

    monkeypatch.setattr(codex_queue, 'enqueue', fake_enqueue)
    monkeypatch.setattr(codex_queue, 'deliver', deliver)
    yield
    for queued, sent in logs:
        codex_panes = {pane for pane, _ in queued if pane}
        typed = [(pane, text[:60]) for pane, text in sent if pane in codex_panes]
        assert not typed, 'a Codex pane got a paste: ' + repr(typed)


@pytest.fixture(autouse=True)
def _waiting_hook_stand_in(request, monkeypatch):
    if getattr(request.module, 'HOOK_CHANNEL', False):
        yield
        return
    hook_sim.install(monkeypatch)
    yield
    hook_sim.reset()


@pytest.fixture(autouse=True)
def _no_machine_wide_redis_sweep(monkeypatch):
    """start / --resume sweep every Redis of this user; tests must never touch a real fleet's
    (tests/test_orphan_redis.py calls reap_orphan_redis directly, scoped to its own root)."""
    from cmux_fleet import fleet
    monkeypatch.setattr(fleet, 'sweep_orphan_redis', lambda keep=None: None)
