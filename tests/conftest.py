"""Suite-wide test defaults.

Claude workers get their deliveries through the Stop-hook channel (cmux_fleet/claude_hook.py) in
every test. A real hook blocks waiting for the next delivery, which an in-process test cannot do,
so tests/hook_sim.py stands in for that wait only (see its docstring). A module that runs the real
hook command in subprocesses sets ``HOOK_CHANNEL = True`` and gets no stand-in.
"""
import pytest

import hook_sim


@pytest.fixture(autouse=True)
def _waiting_hook_stand_in(request, monkeypatch):
    if getattr(request.module, 'HOOK_CHANNEL', False):
        yield
        return
    hook_sim.install(monkeypatch)
    yield
    hook_sim.reset()
