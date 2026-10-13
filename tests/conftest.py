"""Shared test setup: a fake `codex queue` for every test (Codex deliveries never type into a pane).

The real CLI is never run by the unit tests. A queued delivery is recorded in its own log,
`mgr.queued` (FakeMgr / test_subfleet's World), never in `mgr.sent`: `sent` stays the record of real
`send_prompt_to_idle` pastes only, so "nothing was typed" assertions keep their meaning. Its turn is
started in the rollout the way the real TUI does it (test_fleet.codex_rows). A test that installs its
own `codex_queue.enqueue` stub keeps it: then only the real deliver runs.

Guard (every test): a pane that received a queued Codex delivery must never also get a paste.
"""
import uuid

import pytest

import test_fleet  # first: puts the checkout on sys.path
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
