"""cmux backend: every agent lives in a cmux tab/split that the user can close with the ✕.

cmux only accepts commands from processes started inside a cmux window, so the daemon
must run in a cmux surface. Every call is scoped by workspace UUID (refs such as
``surface:N`` renumber), and every destructive or typing call first checks that the
surface is one this manager spawned. Ownership is kept in
``~/.local/state/cmux-fleet/<session>.json``.

Hard-won cmux facts (all measured, not documented):

* ``send`` interprets ``\\n`` / ``\\t`` escapes, so verbatim text goes through
  ``set-buffer`` + ``paste-buffer``.
* Do **not** wrap pasted text in bracketed-paste markers (``ESC[200~`` ... ``ESC[201~``).
  The Codex TUI treats the ESC as a bare Escape key and shows ``[200~`` literally in the
  composer (verified on codex-cli 0.156.1). Codex's own paste-burst detection already
  keeps a multi-line paste in the composer until the explicit Enter.
* ``new-workspace --focus false`` still switches the user's view; select the previous
  workspace back afterwards.
* ``pipe-pane`` is a one-shot snapshot, not a stream. The final screen is saved when a
  surface is closed; the real transcript is Codex's rollout file.
* Start a surface's program with ``new-surface --command`` / ``new-workspace --command``
  (cmux ≥ 0.64.25): cmux types the command into the login shell once the prompt is up.
  Typing it ourselves races the shell startup; typed-ahead text gets truncated and leaves
  the shell waiting at a quote prompt (seen on a slow login shell). The typing path is kept
  only as a fallback for older cmux, and it fails closed when no prompt appears.
* Keep the typed command short. Before the line editor is up, macOS tty input is limited to
  1024 bytes per line, and a ~1.5 kB command (an inlined PATH) was silently truncated or
  left sitting unexecuted at the prompt. Every spawn therefore writes a small launcher
  script and the surface runs ``exec bash <launcher>``.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CMUX = os.environ.get('CMUX_BIN') or '/Applications/cmux.app/Contents/Resources/bin/cmux'
STATE_DIR = Path(os.environ.get('CMUX_FLEET_STATE') or (Path.home() / '.local/state/cmux-fleet'))


@dataclass
class SpawnResult:
    success: bool
    pane_id: Optional[str] = None
    window_name: Optional[str] = None
    error: Optional[str] = None


@dataclass
class PaneStatus:
    pane_id: str
    window_name: str
    alive: bool
    exit_code: Optional[int] = None
    metadata: Dict[str, str] = field(default_factory=dict)


def _run(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CMUX_QUIET='1')
    return subprocess.run([CMUX, *args], capture_output=True, text=True, env=env)


def cmux_available() -> bool:
    try:
        return _run('ping').stdout.strip() == 'PONG'
    except OSError:
        return False


def kill_process_tree(pid: int, term_timeout: float = 3.0) -> None:
    """SIGTERM a process and all its descendants, then SIGKILL whatever survives."""
    pids: List[int] = []

    def collect(parent: int) -> None:
        try:
            result = subprocess.run(['pgrep', '-P', str(parent)], capture_output=True, text=True)
        except OSError:
            result = None
        if result and result.returncode == 0:
            for child in result.stdout.splitlines():
                try:
                    collect(int(child))
                except ValueError:
                    continue
        pids.append(parent)

    collect(pid)
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + term_timeout
    alive = pids
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = []
        for p in pids:
            try:
                os.kill(p, 0)
            except ProcessLookupError:
                continue
            alive.append(p)
    for p in alive:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


class CmuxSessionManager:
    """Agents in one cmux workspace; the daemon in a separate control workspace."""

    def __init__(self, session_name: str = 'cmux-fleet', agents_title: str = 'Fleet agents',
                 control_title: str = 'Fleet control'):
        self.session_name = session_name
        self.agents_title = agents_title
        self.control_title = control_title
        self.meta_path = STATE_DIR / (session_name + '.json')

    # -- ownership registry -------------------------------------------------
    def _meta(self) -> Dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {'workspace': None, 'control_workspace': None, 'surfaces': {}}

    def _save_meta(self, meta: Dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        tmp.replace(self.meta_path)

    def _tag(self, sid: str, **info: str) -> None:
        meta = self._meta()
        meta['surfaces'].setdefault(sid, {}).update(info)
        self._save_meta(meta)

    def _surfaces(self, workspace: Optional[str]) -> Dict[str, Dict]:
        """Live terminal surfaces of a workspace: {surface_id: {pane_id, tty, title}}."""
        if not workspace:
            return {}
        p = _run('--json', '--id-format', 'uuids', 'tree', '--workspace', workspace)
        if p.returncode:
            return {}
        found = {}
        for window in json.loads(p.stdout).get('windows', []):
            for ws in window.get('workspaces', []):
                if ws.get('id') != workspace:
                    continue
                for pane in ws.get('panes', []):
                    for s in pane.get('surfaces', []):
                        found[s['id']] = {'pane_id': s.get('pane_id'), 'tty': s.get('tty'), 'title': s.get('title')}
        return found

    def _where(self, sid: str) -> Optional[str]:
        """Workspace holding an owned, live surface; None if not ours or gone."""
        meta = self._meta()
        if sid not in meta['surfaces']:
            return None
        for ws in (meta.get('workspace'), meta.get('control_workspace')):
            if sid in self._surfaces(ws):
                return ws
        return None

    def owns(self, sid: str) -> bool:
        return self._where(sid) is not None

    # -- session ------------------------------------------------------------
    def session_exists(self) -> bool:
        meta = self._meta()
        return bool(self._surfaces(meta.get('workspace')) or self._surfaces(meta.get('control_workspace')))

    def get_or_create_session(self) -> bool:
        return cmux_available()  # workspaces are created on first spawn

    def _new_surface(self, control: bool = False, command: Optional[str] = None) -> Tuple[Optional[str], bool]:
        """A fresh shell tab in the agents workspace (created if closed) or the control one.

        Returns (surface_id, ran): ``ran`` is True when cmux itself started ``command`` in the
        new shell, False when the caller still has to type it (older cmux without --command).
        """
        meta = self._meta()
        key = 'control_workspace' if control else 'workspace'
        live = self._surfaces(meta.get(key))
        if live:
            anchor = next(iter(live.values()))['pane_id']
            base = ['--id-format', 'uuids', 'new-surface', '--workspace', meta[key], '--pane', anchor, '--focus', 'false']
            ran = False
            p = _run(*base, '--command', command) if command else _run(*base)
            if command and p.returncode:  # cmux too old for new-surface --command: create, then type
                p = _run(*base)
            else:
                ran = bool(command)
            parts = p.stdout.split()
            if p.returncode or len(parts) < 2:
                return None, False
            return parts[1], ran
        name = self.control_title if control else self.agents_title
        before = _run('--id-format', 'uuids', 'current-workspace').stdout.strip()
        extra = ['--command', command] if command else []
        p = _run('--id-format', 'uuids', 'new-workspace', '--name', name, '--focus', 'false', *extra)
        if before:  # --focus false still switches the view; give the user their workspace back
            _run('select-workspace', '--workspace', before)
        if p.returncode:
            return None
        ref = p.stdout.split()[-1]  # prints a ref (workspace:N); pin the UUID right away
        t = _run('--json', '--id-format', 'uuids', 'tree', '--workspace', ref)
        if t.returncode:
            return None
        ids = [ws['id'] for w in json.loads(t.stdout).get('windows', []) for ws in w.get('workspaces', [])
               if ws.get('title') == name]
        if len(ids) != 1:
            return None
        ws = ids[0]
        meta[key] = ws
        self._save_meta(meta)
        live = self._surfaces(ws)
        return (next(iter(live)), bool(command)) if live else (None, False)

    def _launcher(self, name: str, script: str, folder: Optional[Path] = None) -> str:
        """Write ``script`` to a launcher file and return the short command that runs it."""
        folder = folder or (STATE_DIR / 'launch')
        folder.mkdir(parents=True, exist_ok=True)
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name)
        path = folder / (safe + '-' + uuid.uuid4().hex[:6] + '.sh')
        path.write_text('#!/bin/bash\n' + script + '\n')
        path.chmod(0o700)
        return 'exec /bin/bash ' + shlex.quote(str(path))

    def _type_command(self, sid: str, command: str) -> bool:
        ws = self._where(sid)
        if not ws:
            return False
        for _ in range(300):  # wait for the login shell prompt itself, not just "Last login"
            screen = _run('read-screen', '--workspace', ws, '--surface', sid, '--lines', '5')
            lines = [line.rstrip() for line in screen.stdout.splitlines() if line.strip()]
            if screen.returncode == 0 and lines and lines[-1].endswith(('%', '$', '#')):
                break
            time.sleep(.2)
        else:
            return False  # never type ahead of the prompt: the text gets truncated
        return self._paste(ws, sid, command)

    def _paste(self, ws: str, sid: str, text: str) -> bool:
        # Verbatim delivery; no bracketed-paste markers (see module docstring).
        name = 'fleet-' + uuid.uuid4().hex
        if _run('set-buffer', '--name', name, text).returncode:
            return False
        if _run('paste-buffer', '--name', name, '--workspace', ws, '--surface', sid).returncode:
            return False
        time.sleep(1)  # Codex paste-burst detection otherwise swallows the Enter.
        return _run('send-key', '--workspace', ws, '--surface', sid, 'Enter').returncode == 0

    def spawn_window(self, window_name: str, command: str, metadata: Optional[Dict[str, str]] = None,
                     start_directory: Optional[str] = None) -> SpawnResult:
        control = (metadata or {}).get('type') == 'daemon'
        full = self._launcher(window_name, 'cd ' + shlex.quote(start_directory or '.') + ' && exec ' + command)
        sid, ran = self._new_surface(control=control, command=full)
        if not sid:
            return SpawnResult(success=False, error='cmux surface creation failed (run inside cmux)')
        self._tag(sid, type=(metadata or {}).get('type', ''), label=window_name)
        if not ran and not self._type_command(sid, full):
            return SpawnResult(success=False, error='cmux command delivery failed')
        self.rename(sid, window_name)
        return SpawnResult(success=True, pane_id=sid, window_name=window_name)

    def spawn_interactive_cli(self, window_name: str, cli_command: str, prompt_file: Optional[str] = None,
                              output_file: Optional[str] = None, metadata: Optional[Dict[str, str]] = None,
                              start_directory: Optional[str] = None, auto_close: bool = True,
                              env_vars: Optional[Dict[str, str]] = None, **_) -> SpawnResult:
        inner = cli_command
        if prompt_file:
            inner = f'{cli_command} "$(<{prompt_file})"'
        if not auto_close:
            inner = f'{inner}; exec {os.environ.get("SHELL") or "/bin/zsh"} -l'
        env = ' '.join(shlex.quote(k + '=' + v) for k, v in (env_vars or {}).items() if v is not None)
        command = ('env ' + env + ' ' if env else '') + 'bash -lc ' + shlex.quote(inner)
        full = self._launcher(window_name, 'cd ' + shlex.quote(start_directory or '.') + ' && exec ' + command,
                              Path(output_file).parent if output_file else None)
        sid, ran = self._new_surface(command=full)
        if not sid:
            return SpawnResult(success=False, error='cmux surface creation failed (run inside cmux)')
        self._tag(sid, type=(metadata or {}).get('type', ''), label=window_name, output_file=output_file or '')
        if not ran and not self._type_command(sid, full):
            return SpawnResult(success=False, error='cmux command delivery failed')
        self.rename(sid, window_name)
        return SpawnResult(success=True, pane_id=sid, window_name=window_name)

    # -- status / io ----------------------------------------------------------
    def get_pane_status(self, pane_id: str) -> Optional[PaneStatus]:
        if not self.owns(pane_id):
            return None
        info = self._meta()['surfaces'][pane_id]
        return PaneStatus(pane_id=pane_id, window_name=info.get('label', ''), alive=True,
                          metadata={k: v for k, v in info.items() if isinstance(v, str)})

    def has_cli_process(self, pane_id: str) -> bool:
        """A live surface may only contain a shell after its Codex process exits."""
        ws = self._where(pane_id)
        if not ws:
            return False
        tty = self._surfaces(ws)[pane_id].get('tty')
        if not tty:
            return False
        result = subprocess.run(['ps', '-t', tty, '-o', 'comm='], capture_output=True, text=True)
        return result.returncode == 0 and any(Path(line.strip()).name == 'codex' for line in result.stdout.splitlines())

    def capture_output(self, pane_id: str, lines: int = 2000) -> str:
        ws = self._where(pane_id)
        if not ws:
            return ''
        p = _run('read-screen', '--workspace', ws, '--surface', pane_id, '--scrollback', '--lines', str(lines))
        return p.stdout if p.returncode == 0 else ''

    def send_prompt_to_idle(self, pane_id: str, text: str) -> bool:
        ws = self._where(pane_id)
        return bool(ws) and self._paste(ws, pane_id, text)

    def send_key(self, pane_id: str, key: str) -> bool:
        ws = self._where(pane_id)
        return bool(ws) and _run('send-key', '--workspace', ws, '--surface', pane_id, key).returncode == 0

    def rename(self, pane_id: str, label: str) -> None:
        ws = self._where(pane_id)
        if ws:
            _run('rename-tab', '--workspace', ws, '--surface', pane_id, label)
        self._tag(pane_id, label=label)

    def focus(self, pane_id: str) -> bool:
        ws = self._where(pane_id)
        if not ws:
            return False
        _run('select-workspace', '--workspace', ws)
        pane = self._surfaces(ws)[pane_id]['pane_id']
        return _run('focus-pane', '--workspace', ws, '--pane', pane).returncode == 0

    # -- lifecycle ------------------------------------------------------------
    def kill_pane(self, pane_id: str) -> bool:
        meta = self._meta()
        if pane_id not in meta['surfaces']:
            return False  # never touch a surface this fleet did not spawn
        ws = self._where(pane_id)
        if not ws:  # already closed (e.g. by the ✕)
            meta['surfaces'].pop(pane_id)
            self._save_meta(meta)
            return True
        info = self._surfaces(ws)[pane_id]
        output_file = self._meta()['surfaces'][pane_id].get('output_file')
        if output_file:  # cmux has no streaming pipe-pane; keep the final screen instead
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(self.capture_output(pane_id))
        if info.get('tty'):
            ps = subprocess.run(['ps', '-t', info['tty'], '-o', 'pid=,ppid='], capture_output=True, text=True)
            rows = [line.split() for line in ps.stdout.splitlines() if line.strip()]
            pids = {int(r[0]) for r in rows}
            for pid, ppid in ((int(r[0]), int(r[1])) for r in rows):
                if ppid not in pids:  # tree roots on this tty
                    kill_process_tree(pid)
        p = _run('close-surface', '--workspace', ws, '--surface', pane_id)
        if p.returncode and 'confirmation_required' in p.stdout + p.stderr:
            # cmux >= 0.65 refuses to close a surface that still shows a running process; this
            # surface is ours and its process tree was just killed above.
            _run('close-surface', '--workspace', ws, '--surface', pane_id, '--force')
        meta = self._meta()
        meta['surfaces'].pop(pane_id, None)
        self._save_meta(meta)
        return pane_id not in self._surfaces(ws)

    def place_right_of(self, pane_id: str, peer: str) -> bool:
        """Put an agent in its own split to the right of its peer's pane."""
        ws = self._where(pane_id)
        if not ws or self._where(peer) != ws:
            return False
        live = self._surfaces(ws)
        if live[pane_id]['pane_id'] != live[peer]['pane_id']:
            return True  # already side by side
        return _run('split-off', '--workspace', ws, '--surface', pane_id, 'right', '--focus', 'false').returncode == 0

    def capture_to(self, pane_id: str, path: str) -> None:
        """Save a retiring agent's whole screen (with scrollback) for the history page."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.capture_output(pane_id, 100000), encoding='utf-8')

    def arrange(self, supervisor: str, workers: List[str], history_url: str) -> Optional[str]:
        """One page: supervisor top-left, the history browser below it, the workers as tabs of one pane
        on the right (worker-1, worker-2, ... in order).

        Returns the history browser surface (None if it could not be created).
        """
        ws = self._where(supervisor)
        if not ws:
            return None
        first = None
        for w in workers:
            if self._where(w) != ws:
                continue
            if first is None:
                self.place_right_of(w, supervisor)
                first = w
            else:
                self.tab_into(w, first, before=False)
        pane = self._surfaces(ws)[supervisor]['pane_id']
        p = _run('--id-format', 'uuids', 'new-surface', '--type', 'browser', '--workspace', ws, '--pane', pane,
                 '--url', history_url, '--focus', 'false')
        parts = p.stdout.split()
        if p.returncode or len(parts) < 2:
            return None
        sid = parts[1]
        self._tag(sid, type='history', label='history')
        _run('split-off', '--workspace', ws, '--surface', sid, 'down', '--focus', 'false')
        self.rename(sid, 'history')
        return sid

    def reload_browser(self, surface: str) -> bool:
        return bool(self._where(surface)) and _run('browser', '--surface', surface, 'reload').returncode == 0

    def tab_into(self, pane_id: str, peer: str, before: bool = True) -> bool:
        """Move a surface into the pane holding ``peer`` as another tab: just before ``peer`` (a successor
        takes its predecessor's place in the tab row) or, with ``before=False``, at the end of the row."""
        ws = self._where(pane_id)
        if not ws or self._where(peer) != ws:
            return False
        target = self._surfaces(ws)[peer]['pane_id']
        place = ['--before', peer] if before else []
        return _run('move-surface', '--workspace', ws, '--surface', pane_id, '--pane', target, *place,
                    '--focus', 'false').returncode == 0

