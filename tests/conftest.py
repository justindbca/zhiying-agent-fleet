"""Shared test setup: a fake `codex queue` for every test (Codex deliveries never type into a pane).

The real CLI is never run by the unit tests. A queued delivery is recorded in FakeMgr.sent (as
typing used to be, so the existing "what reached this pane" assertions keep working) and its turn
is started in the rollout the way the real TUI does it (test_fleet.codex_rows). A test that
installs its own `codex_queue.enqueue` stub keeps it: then only the real deliver runs.
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
            raise codex_queue.QueueError('no bound Codex session to queue into')
        return {'queued_id': 'q-' + uuid.uuid4().hex, 'client_id': 'c-' + uuid.uuid4().hex, 'thread': thread}

    def deliver(f, slot, delivery, text, thread=None):
        got = real_deliver(f, slot, delivery, text, thread=thread)
        if codex_queue.enqueue is fake_enqueue:
            log = f.mgr.sent if hasattr(f.mgr, 'sent') else f.mgr.w.sent  # FakeMgr / test_subfleet WorldMgr
            log.append((f.agent(slot).get('pane'), text))
            test_fleet.CODEX_QUEUE[got['thread']] = {'turn': 't-' + uuid.uuid4().hex, 'client_id': got['client_id'],
                                                     'text': text}
            _, rows = test_fleet.codex_rows(got['thread'], text, pop=False)
            test_fleet.append_rollout(f.sessions, got['thread'], fleet.agent_cwd(f.runtime_cfg(slot), slot), rows)
        return got

    monkeypatch.setattr(codex_queue, 'enqueue', fake_enqueue)
    monkeypatch.setattr(codex_queue, 'deliver', deliver)
