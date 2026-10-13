"""Fleet advisor: one short official-Claude consultation before each dispatch (advisor plan §3.1).

This module packs the advisor input, runs the one-shot advisor process (injectable for tests),
validates its configuration sheet and decides how the daemon applies it. It holds no Redis
state and never starts a worker: the daemon (fleet.py) does that through the existing Phase 5
respawn path or a domain hot-load. The advisor only ever sees what the supervisor wrote
(goal / instruction / advise_note), never a worker conversation.
"""
import json
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPT = HERE / 'agents' / 'advisor.md'
MATRIX = HERE / 'agents' / 'models' / 'capability_matrix.yaml'
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
DEFAULT_EFFORT = 'medium'
VERDICTS = ('enough', 'enough_simple', 'not_enough', 'overkill', 'untested', 'reserve')
TIMEOUT = 90
ATTEMPTS = 2  # one retry, then the fallback sheet (§8)
MODEL, EFFORT = 'fable', 'high'  # user boundary 2: Fable / high / official CLI, never pi


class AdviceError(ValueError):
    """The advisor did not produce a usable configuration sheet."""


def load_matrix(path=MATRIX):
    """Capability matrix (data, re-read per consultation). Unreadable -> {} plus the error text."""
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
    except Exception as exc:
        return {}, str(exc)
    return (data, None) if isinstance(data, dict) else ({}, 'capability matrix must be a mapping')


def runtime_model(cfg, cli):
    if cli == 'claude':
        return cfg.get('worker_model') or 'sonnet'
    return (cfg.get('pi_model') if cli == 'pi' else cfg.get('codex_model')) or ''


def runtime_effort(cfg, cli):
    return (cfg.get('worker_effort') or '') if cli == 'claude' else ''


def fingerprint(cli, model, effort):
    """The launch fingerprint: (model, effort). Effort only exists for Claude."""
    return (model or '', (effort or '') if cli == 'claude' else '')


def launch_cards(registry, cli):
    """model -> registry card that launches it on this CLI (the static `<cli>-<model>` card first)."""
    cards = {}
    for card in sorted(registry, key=lambda c: (c.id != cli + '-' + (c.model or ''), c.id)):
        if card.cli == cli and card.model and card.model not in cards:
            cards[card.model] = card
    return cards


def build_input(*, kind, goal, instruction, advise_note, slot, models, matrix, catalog):
    """Everything the advisor may see. No worker conversation, no other worker's output."""
    return {'kind': kind, 'goal': goal or '', 'instruction': instruction, 'advise_note': advise_note or '',
            'slot': slot, 'models': models, 'capability_matrix': matrix, 'domains': catalog}


def argv(cfg, run_dir):
    return [cfg.get('claude') or shutil.which('claude') or 'claude', '-p', '--model', MODEL, '--effort', EFFORT,
            '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands',
            '--settings', json.dumps({'autoMemoryEnabled': False}),
            '--system-prompt-file', str(Path(run_dir) / 'advisor.md'), '--output-format', 'json']


def run_claude(payload, *, cfg, run_dir, timeout=TIMEOUT):
    """One-shot official Claude CLI; cwd fixed to <run>/advisor/ (one transcript directory per run)."""
    run_dir = Path(run_dir)
    prompt = run_dir / 'advisor.md'
    if not prompt.exists():
        prompt.write_text(PROMPT.read_text())
    cwd = run_dir / 'advisor'
    cwd.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(argv(cfg, run_dir), input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                          text=True, timeout=timeout, cwd=str(cwd))
    if done.returncode != 0:
        raise AdviceError('advisor exited ' + str(done.returncode) + ': ' + (done.stderr or done.stdout)[-500:])
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise AdviceError('advisor output is not JSON: ' + done.stdout[-300:]) from exc


def sheet_text(envelope):
    """The configuration sheet object inside `--output-format json`'s `result` text."""
    if not isinstance(envelope, dict) or envelope.get('is_error'):
        raise AdviceError('advisor returned an error: ' + json.dumps(envelope, ensure_ascii=False)[:300])
    text = envelope.get('result')
    if not isinstance(text, str) or '{' not in text:
        raise AdviceError('advisor result has no JSON object')
    try:
        sheet = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except ValueError as exc:
        raise AdviceError('advisor sheet is not valid JSON: ' + str(exc)) from exc
    if not isinstance(sheet, dict):
        raise AdviceError('advisor sheet must be a JSON object')
    return sheet


def validate(raw, *, cli, models, current, catalog, whitelist, matrix, cards):
    """Normalized sheet {model, effort, domains, reason, capability?, capability_gap?, effort_ignored?}.

    current = the slot's running fingerprint; cards = model -> registry card for this CLI."""
    model = raw.get('model')
    if model not in models:
        raise AdviceError('model must be one of ' + ', '.join(models) + ' for a ' + cli + ' slot')
    sheet = {'model': model}
    if cli == 'claude':
        effort = raw.get('effort', DEFAULT_EFFORT)
        if effort not in EFFORTS:
            raise AdviceError('effort must be one of ' + ', '.join(EFFORTS))
        sheet['effort'] = effort
    else:
        sheet['effort'] = ''
        if raw.get('effort'):
            sheet['effort_ignored'] = str(raw['effort'])[:20]
    domains = raw.get('domains', [])
    if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains) or len(set(domains)) != len(domains):
        raise AdviceError('domains must be a list of distinct domain names')
    if cli != 'claude' and domains:
        raise AdviceError('a ' + cli + ' slot takes no domains (only Claude slots load domain kits)')
    known = set(catalog)
    allowed = known if whitelist is None else known & set(whitelist)
    unknown = [d for d in domains if d not in allowed]
    if unknown:
        raise AdviceError('domains not in the domain table / this card\'s kit.domains: ' + ', '.join(unknown))
    sheet['domains'] = domains
    reason = raw.get('reason')
    if not isinstance(reason, str) or not reason.strip():
        raise AdviceError('reason must cite the capability matrix')
    sheet['reason'] = ' '.join(reason.split())[:500]
    capability = raw.get('capability')
    if capability is not None:
        domain, _, task = str(capability).partition('/')
        row = (matrix.get(domain) or {}).get(task) if isinstance(matrix.get(domain), dict) else None
        if not isinstance(row, dict):
            raise AdviceError('capability ' + str(capability) + ' is not a row of the capability matrix')
        verdict = (row.get(model) or {}).get('verdict', 'untested')
        if verdict == 'untested':
            tested = [c.get('verdict') for m, c in row.items() if isinstance(c, dict) and c.get('verdict') != 'untested']
            if any(v != 'not_enough' for v in tested):
                raise AdviceError('untested cell ' + capability + ' × ' + model + ' while a tested model is enough')
            sheet['capability_gap'] = True
        sheet['capability'] = capability
    if fingerprint(cli, model, sheet['effort']) != current and model not in cards:
        raise AdviceError('no registry card launches model ' + model + ' on ' + cli)
    return sheet


def decide(current, target, installed, domains):
    """respawn (model / effort changed) | hotload (only new domains) | none. Fewer domains never act."""
    if current != target:
        return 'respawn'
    return 'hotload' if [d for d in domains if d not in installed] else 'none'


def consult(payload, runner, check, attempts=ATTEMPTS):
    """(sheet, envelope, errors). Failures (process, timeout, non-JSON, invalid sheet) retry once."""
    errors = []
    for _ in range(attempts):
        try:
            envelope = runner(payload)
            return check(sheet_text(envelope)), envelope, errors
        except (AdviceError, ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
            errors.append(str(exc)[:500])
    return None, None, errors


def usage_fields(envelope):
    """Advisor token / cost accounting from `--output-format json`."""
    usage = (envelope or {}).get('usage') or {}
    fields = {k: int(usage.get(k) or 0) for k in
              ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')}
    fields['total_cost_usd'] = float((envelope or {}).get('total_cost_usd') or 0)
    return fields


def notice(label, sheet, effect):
    shown = sheet['model'] + ('/' + sheet['effort'] if sheet.get('effort') else '')
    return ('[advisor] ' + label + ' → ' + shown + ', domains: [' + ', '.join(sheet['domains']) + ']（'
            + sheet['reason'] + '；' + effect + '）')
