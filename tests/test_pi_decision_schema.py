"""Pi supervisor (task mode) fleet_decision schema accepts dispatch[].id, advise_note, advisor on/off, consult[] and withdraw[]."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_pi_v2_contract import PI_ROOT


def test_pi_supervisor_schema_accepts_id_and_advise_note(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'cmux_fleet/pi_extension_v2.ts', extension)
    harness = tmp_path / 'harness.mjs'
    shutil.copy(root / 'tests/test_pi_decision_schema_v2.mjs', harness)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    env = dict(os.environ, CMUX_FLEET_PI_SLOT='supervisor', CMUX_FLEET_PI_MODE='task',
               CMUX_FLEET_PI_BRIDGE='["x","supervisor","0"]')
    result = subprocess.run([node, '--experimental-strip-types', str(harness), str(extension)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'decision schema contract passed' in result.stdout


def test_schema_source_declares_optional_keys():
    src = (Path(__file__).resolve().parents[1] / 'cmux_fleet/pi_extension_v2.ts').read_text()
    assert 'id: Type.Optional(Type.String()), advise_note: Type.Optional(Type.String())' in src
    assert 'advisor: Type.Optional(Type.Union([Type.Literal("on"), Type.Literal("off")]))' in src
    assert 'consult: Type.Optional(Type.Array(consult))' in src
    assert 'finding: Type.String({ minLength: 1 })' in src
    assert 'escalations: Type.Optional(Type.Array(escalation))' in src


def test_pi_service_supervisor_schema_accepts_escalations(tmp_path):
    node = shutil.which('node')
    if not node or not (PI_ROOT / 'node_modules/typebox').exists():
        pytest.skip('installed Pi/Node dependencies needed for actual TS contract harness')
    root = Path(__file__).resolve().parents[1]
    extension = tmp_path / 'extension.ts'
    shutil.copy(root / 'cmux_fleet/pi_extension_v2.ts', extension)
    harness = tmp_path / 'harness.mjs'
    shutil.copy(root / 'tests/test_pi_service_schema_v2.mjs', harness)
    (tmp_path / 'node_modules').symlink_to(PI_ROOT / 'node_modules', target_is_directory=True)
    env = dict(os.environ, CMUX_FLEET_PI_SLOT='supervisor', CMUX_FLEET_PI_MODE='service',
               CMUX_FLEET_PI_BRIDGE='["x","supervisor","0"]')
    result = subprocess.run([node, '--experimental-strip-types', str(harness), str(extension)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'service decision schema contract passed' in result.stdout


def test_service_schema_source_declares_escalations_and_the_blocked_answer():
    src = (Path(__file__).resolve().parents[1] / 'cmux_fleet/pi_extension_v2.ts').read_text()
    assert 'skip: Type.Optional(Type.Array(Type.Object({ reason: Type.String() }))),\n    escalations: Type.Optional(Type.Array(escalation)) })' in src
    assert 'if (!supervisor && active.body.reply && !blockedOnly)' in src
