"""Platform seams: the few places where macOS and Linux differ.

`keep_awake_prefix()` is the only one this module owns today (design §6): the daemon should keep the
machine awake while a fleet runs, but the tool differs per platform and Linux boxes may not have one.
Callers prepend the returned list to a command; an empty list means "run it as is".
"""
import shutil
import sys


def keep_awake_prefix(platform=None, which=shutil.which):
    """Command prefix that keeps the machine awake for the lifetime of the spawned command.

    macOS: `caffeinate -i` (idle sleep only, as before). Linux: `systemd-inhibit --what=idle:sleep`
    when systemd-inhibit is on PATH. Anything else, or the tool missing: no prefix (`[]`) — the
    daemon still runs, the machine may sleep. `platform` / `which` are injectable so tests do not
    depend on the host actually being macOS or Linux.
    """
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        tool = which('caffeinate')
        return [tool, '-i'] if tool else []
    if platform.startswith('linux'):
        tool = which('systemd-inhibit')
        return [tool, '--what=idle:sleep', '--why=cmux-fleet'] if tool else []
    return []
