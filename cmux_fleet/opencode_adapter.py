"""opencode launch/configuration helpers and wire checks (fourth worker CLI).

Measured (worker-3 preflight, opencode 2.0.29): the plugin creates the session itself with an
opencode-generated id (a self-made session id is refused by Zen), the TUI shows it with
`--standalone --continue` on a fresh per-generation data dir, and the isolation that keeps only
the fleet role in the session instructions is "full2": every XDG dir + OPENCODE_CONFIG_DIR +
OPENCODE_TEST_HOME + OPENCODE_DISABLE_PROJECT_CONFIG, role in $OPENCODE_CONFIG_DIR/AGENTS.md.

The free tier only works from inside the opencode process (a direct Zen call answers
FreeTierError), so fleet runs the opencode TUI itself with an in-process plugin
(opencode_plugin/) that talks JSONL to agent_bridge.py, exactly like the Pi extension.
No model endpoint, credential or model name is embedded here: the model is configuration
(`opencode_model`, the free list changes often) and is checked on every settled turn.
"""
import json
import math
from pathlib import Path
import re

KIND = 'opencode_session'                 # bridge_session identity kind for opencode slots
DEFAULT_WINDOW = 200000                   # free-tier context window (paid tier is 1M)
PLUGIN_DIR = Path(__file__).with_name('opencode_plugin')
ERROR_CLASSES = ('free_tier_rejected', 'rate_limited', 'model_unavailable', 'model_lock', 'aborted', 'other')

# Deletes / moves / renames: the permission request is the user's call only (design §24, user 2026-10-14).
# These rules make opencode stop and ask; nothing in fleet ever answers them.
DELETE_SHELL = ('rm *', 'rmdir *', 'unlink *', 'mv *', 'shred *', 'trash *', 'rimraf *', 'truncate *',
                'git rm *', 'git mv *', 'git clean*', 'git reset --hard*', 'git checkout --*', 'git restore*',
                'git branch -D*', 'git branch -d*', 'git push --force*', 'git push -f*', 'find * -delete*',
                'dd *')

_FREE_TIER = re.compile(r'freetiererror|free tier can only be used', re.I)
_RATE = re.compile(r'rate[ -]?limit|too many requests|ratelimit', re.I)  # bare "429" in text is not used: token counts hit it
_UNAVAILABLE = re.compile(r'provider\.no-route|model unavailable|model not found|unknown model|no such model', re.I)
_ABORTED = re.compile(r'\baborted\b|\binterrupted\b|AbortError', re.I)


def model(cfg):
    """(providerID, modelID) from config `opencode_model` = "provider/model". Never a default."""
    value = cfg.get('opencode_model')
    if not isinstance(value, str) or value.count('/') < 1:
        raise ValueError('opencode_model provider/model is required for opencode agents')
    provider, _, ident = value.partition('/')
    if not provider or not ident or value != value.strip():
        raise ValueError('opencode_model must be provider/model')
    return provider, ident


def for_agent(cfg, name, overrides=None):
    """cfg as one agent sees it: `opencode_models[name]` over run-wide `opencode_model`;
    profile-launch overrides win. Idempotent like pi_adapter.for_agent."""
    out, overrides = dict(cfg), overrides or {}
    own = (cfg.get('opencode_models') or {}).get(name)
    if own:
        out['opencode_model'] = own
    if 'opencode_model' in overrides and own:
        out['opencode_models'] = {k: v for k, v in cfg['opencode_models'].items() if k != name}
    out.update(overrides)
    return out


def window(cfg):
    value = cfg.get('opencode_window', DEFAULT_WINDOW)
    if type(value) is not int or value <= 0:
        raise ValueError('opencode_window must be a positive integer')
    return value


INSTANCE_DIRS = ('cfg', 'xdgcfg', 'xdgstate', 'xdgcache', 'xdgdata', 'home')


def instance_dir(cfg, name, generation):
    """Per-generation opencode home: config, state, cache, data (its own opencode.db) and test home."""
    return Path(cfg['run']) / 'opencode' / (name + '-g' + str(generation))


def prepare_instance(cfg, name, generation, role_text):
    """A fresh instance directory per generation (never reused: a new DB means --continue can only
    pick this generation's session). The role is the only instruction file:
    $OPENCODE_CONFIG_DIR/AGENTS.md. Never write AGENTS.md into the worker cwd (opencode would load
    it as project / nested instructions)."""
    if not isinstance(role_text, str) or not role_text.strip():
        raise ValueError('opencode role instructions are required')
    root = instance_dir(cfg, name, generation)
    root.mkdir(parents=True, exist_ok=False)
    for sub in INSTANCE_DIRS:
        (root / sub).mkdir(mode=0o700)
    (root / 'cfg' / 'AGENTS.md').write_text(role_text, encoding='utf-8')
    return root


def isolation_env(cfg, name, generation):
    root = instance_dir(cfg, name, generation)
    return {'XDG_CONFIG_HOME': str(root / 'xdgcfg'), 'XDG_STATE_HOME': str(root / 'xdgstate'),
            'XDG_CACHE_HOME': str(root / 'xdgcache'), 'XDG_DATA_HOME': str(root / 'xdgdata'),
            'OPENCODE_CONFIG_DIR': str(root / 'cfg'), 'OPENCODE_TEST_HOME': str(root / 'home'),
            'OPENCODE_DISABLE_PROJECT_CONFIG': '1', 'OPENCODE_DISABLE_AUTOUPDATE': '1'}


def session_dir(cfg, name, generation):
    """Per-generation directory where the plugin keeps its activity log `<session>.jsonl`.
    opencode stores sessions in its own database; this log is fleet's transcript (bridge
    identity, watchdog last_activity)."""
    return Path(cfg['run']) / 'opencode-sessions' / name / ('g' + str(generation))


def permission_rules(cfg):
    """opencode `permission` config: delete-class shell commands ask (held for the user),
    everything else runs inside the agent directory as the run already allows. No global --auto."""
    shell = {'*': 'allow'}
    shell.update({pattern: 'ask' for pattern in DELETE_SHELL})
    return {'bash': shell, 'shell': shell, 'edit': 'allow', 'webfetch': 'allow',
            'external_directory': 'ask', 'doom_loop': 'ask'}


def config_content(cfg):
    """OPENCODE_CONFIG_CONTENT: only the fleet plugin, the locked model, permission rules."""
    provider, ident = model(cfg)
    return json.dumps({'plugin': [str(PLUGIN_DIR)], 'model': provider + '/' + ident,
                       'autoupdate': False, 'share': 'disabled', 'permission': permission_rules(cfg)},
                      ensure_ascii=False)


def argv(cfg, name, generation, prompt, cwd, *, label):
    """Interactive opencode TUI with its own private server; the plugin lives in this process,
    creates the session and injects the startup message itself (CMUX_FLEET_OC_STARTUP), so the
    prompt is not on argv. --continue shows the plugin's session (only one in the fresh DB)."""
    model(cfg)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('opencode startup prompt is required')
    return [cfg.get('opencode') or 'opencode', '--standalone', '--continue', str(cwd)]


def environment(cfg_path, cfg, name, generation, *, label=None, startup_prompt=None):
    """Environment for the opencode process: full2 isolation (prepare_instance first), the fleet
    plugin and its bridge argv. startup_prompt is required: the plugin injects it and binds the
    startup turn by the inbox id prompt() returns. The plugin waits (bounded, catalogue_wait_s)
    until the locked model is in the fresh DB's model catalogue before the startup turn."""
    provider, ident = model(cfg)
    if not isinstance(startup_prompt, str) or not startup_prompt.strip():
        raise ValueError('opencode startup prompt is required')
    wait = cfg.get('opencode_catalog_wait_s', 60)
    if type(wait) is not int or not 1 <= wait <= 600:
        raise ValueError('opencode_catalog_wait_s must be an integer 1-600')
    env = dict(isolation_env(cfg, name, generation))
    env.update({
        'OPENCODE_CONFIG_CONTENT': config_content(cfg),
        'CMUX_FLEET_OC_BRIDGE': json.dumps([cfg['python'], str(Path(__file__).with_name('agent_bridge.py')),
                                            str(cfg_path), name, str(generation)]),
        'CMUX_FLEET_OC_SLOT': name, 'CMUX_FLEET_OC_GEN': str(generation),
        'CMUX_FLEET_OC_MODE': cfg.get('mode', 'task'),
        'CMUX_FLEET_OC_MODEL': provider + '/' + ident,
        'CMUX_FLEET_OC_WINDOW': str(window(cfg)),
        'CMUX_FLEET_OC_SESSION_DIR': str(session_dir(cfg, name, generation)),
        'CMUX_FLEET_OC_STARTUP': startup_prompt,
        'CMUX_FLEET_OC_CATALOG_WAIT_S': str(wait),
        'CMUX_FLEET_OC_LABEL': label or name,
    })
    return env


def event(cfg, name, generation, data):
    """Validate the opencode adapter identity before anything reaches the private Redis."""
    if not isinstance(data, dict) or data.get('kind') != KIND:
        raise ValueError('invalid opencode event kind')
    thread = data.get('thread')
    if not isinstance(thread, str) or not re.fullmatch(r'ses_[A-Za-z0-9]{6,64}', thread):
        raise ValueError('opencode event requires an opencode session id (ses_...)')
    transcript = data.get('transcript')
    if not isinstance(transcript, str) or not transcript:
        raise ValueError('opencode event requires the plugin activity log')
    path = Path(transcript).resolve()
    if (path.suffix != '.jsonl' or path.stem != thread
            or not path.is_relative_to(session_dir(cfg, name, generation).resolve())):
        raise ValueError('opencode activity log must be <session>.jsonl in this agent generation directory')
    for key in ('ctx_used', 'ctx_window'):
        value = data.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('invalid opencode context usage')
    fields = ('kind', 'thread', 'transcript', 'ctx_used', 'ctx_window')
    return dict({k: data[k] for k in fields if k in data}, agent=name, generation=generation)


def _cost_ok(cost):
    return type(cost) in (int, float) and math.isfinite(cost) and cost == 0


def model_problem(cfg, info):
    """None when the assistant message really used the locked free model; otherwise the reason.
    info = {'model': {'providerID', 'id'}, 'cost'} (assistant message fields; flat providerID/modelID
    accepted too). Strict: cost must be a finite number equal to 0 (negative / NaN / missing fail)."""
    provider, ident = model(cfg)
    if not isinstance(info, dict):
        return 'model_lock: no assistant message to check'
    used = info.get('model') if isinstance(info.get('model'), dict) else {}
    got_provider = used.get('providerID', info.get('providerID'))
    got_id = used.get('id', used.get('modelID', info.get('modelID')))
    if got_provider != provider or got_id != ident:
        return 'model_lock: expected %s/%s, got %s/%s' % (provider, ident, got_provider, got_id)
    cost = info.get('cost')
    if type(cost) is bool or not _cost_ok(cost):
        return 'cost_nonzero: expected cost 0, got %r' % (cost,)
    return None


def classify_error(text, status=None):
    """Error class for a failed turn. The plugin never retries or switches models on any of them;
    free-tier refusals and 429s are provider-side and reported apart from task failures."""
    text = str(text or '')
    if _FREE_TIER.search(text):
        return 'free_tier_rejected'
    if status == 429 or _RATE.search(text):
        return 'rate_limited'
    if _UNAVAILABLE.search(text):
        return 'model_unavailable'
    if text.startswith('model_lock:') or text.startswith('cost_nonzero:'):
        return 'model_lock'
    if _ABORTED.search(text):
        return 'aborted'
    return 'other'


def classify_failure(error):
    """execution.failed data.error = {type, message, status, response: {body}} (measured:
    provider.auth 403 with a FreeTierError body; provider.no-route "Model unavailable")."""
    if not isinstance(error, dict):
        return classify_error(error)
    body = (error.get('response') or {}).get('body') if isinstance(error.get('response'), dict) else None
    text = ' | '.join(str(x) for x in (error.get('type'), error.get('status'), error.get('message'), body)
                      if x not in (None, ''))
    status = error.get('status') if type(error.get('status')) is int else None
    return classify_error(text, status)


def permission_text(req):
    """All text of a permission request that may name what it does (tool, patterns, command)."""
    if not isinstance(req, dict):
        return ''
    parts = [str(req.get(k) or '') for k in ('permission', 'type', 'title', 'command')]
    for key in ('patterns', 'pattern'):
        value = req.get(key)
        parts.extend(str(x) for x in value) if isinstance(value, list) else parts.append(str(value or ''))
    meta = req.get('metadata')
    if isinstance(meta, dict):
        parts.extend(str(v) for v in meta.values() if isinstance(v, (str, int, float)))
    return '\n'.join(p for p in parts if p)


def permission_class(req):
    """'delete' | 'other'. Uses the watchdog delete word list (deletes, moves, renames). Unreadable
    requests count as delete: strict on purpose, a wrong 'delete' only asks the user once more."""
    from cmux_fleet import watchdog
    text = permission_text(req).lower()
    if not text.strip():
        return 'delete'
    return 'delete' if any(watchdog._marker_re(x).search(text) for x in watchdog.DELETE_MARKERS) else 'other'
