#!/usr/bin/env bash
# Real-TUI smoke of one transport-v2 CLI pair: 1 supervisor + 2 workers, lifecycle smoke.
#
#   scripts/smoke_pair.sh <supervisor_cli> <worker_cli> <task|service>
#   e.g. scripts/smoke_pair.sh codex pi task      (night pair: Codex supervisor + Pi workers)
#        scripts/smoke_pair.sh codex claude service
#
# Run it inside a cmux terminal with the CLIs logged in. Environment (optional):
#   PI_MODEL=provider/model  required when a Pi agent is involved
#   SMOKE_PROFILE=<codex profile>  supervisor/worker Codex profile (default: fleet's own default)
#   SMOKE_TIMEOUT=900        seconds for READY + work
#   SMOKE_KEEP=1             keep the temp dir for inspection (processes are still stopped)
#
# Isolation: CMUX_FLEET_HOME / CMUX_FLEET_STATE / the private Redis runtime all live in one
# mktemp dir; never the production Redis or ~/.cmux-fleet, never another fleet. Cleanup stops
# only this run (fleet stop, its own cmux tabs, processes whose command line names the temp dir).
#
# v2 mandatory checks (design.md §11, master design v2 §4.12), each printed PASS/FAIL:
#   1 no message files      no .inbox / msg-*.md anywhere under the run
#   2 zero-model cards      results reach the supervisor as UI-only cards (no mechanical relay turn)
#   3 stale callback        a retired-generation callback is rejected (turn.stale / settle rejected)
#   4 human input           a manual line typed into worker-1 is not settled as a delivery
# Exit 0 only if the run finished and all four PASS. A PASS does not flip the pair table to
# verified: a human edits cmux_fleet/config/cli_pairs.yaml (status + verified_by).
set -u
SUP=${1:?supervisor cli}; WRK=${2:?worker cli}; MODE=${3:?task|service}
case "$MODE" in task|service) ;; *) echo "mode must be task or service" >&2; exit 2;; esac
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
FLEET="$ROOT/cmux_fleet/fleet.py"
TMP=$(mktemp -d /tmp/cfsmoke.XXXXXX)          # short path: Unix socket limit is 103 bytes
export CMUX_FLEET_HOME="$TMP/home" CMUX_FLEET_STATE="$TMP/state"
mkdir -p "$CMUX_FLEET_HOME" "$CMUX_FLEET_STATE"
CFG="$CMUX_FLEET_HOME/latest.json"
RESULT=1

cleanup() {
  set +e
  if [ -f "$CFG" ]; then
    "$PY" "$FLEET" stop --config "$CFG" >/dev/null 2>&1
    "$PY" - "$CFG" "$ROOT" <<'EOF' >/dev/null 2>&1
import sys
sys.path.insert(0, sys.argv[2])
from cmux_fleet import fleet
fleet.manager_for(fleet.load(sys.argv[1])).close_all()   # this run's own tabs (isolated state dir)
EOF
  fi
  sleep 2
  # Only processes this run started carry the temp dir in their command line.
  pids=$(pgrep -f "$TMP" || true)
  [ -n "$pids" ] && kill $pids 2>/dev/null
  sleep 1
  pids=$(pgrep -f "$TMP" || true)
  [ -n "$pids" ] && kill -9 $pids 2>/dev/null
  if [ "${SMOKE_KEEP:-0}" = 1 ]; then echo "kept: $TMP"; else rm -rf "$TMP"; fi
  left=$(pgrep -f "$TMP" || true)
  [ -n "$left" ] && echo "WARN leftover processes: $left" >&2
}
trap cleanup EXIT
trap 'exit 130' INT TERM

if ! "$PY" "$FLEET" start --help 2>/dev/null | grep -q -- '--lifecycle'; then
  echo "FAIL setup: this checkout's start has no --lifecycle (item D); unverified pairs can only start with --lifecycle smoke" >&2
  exit 3
fi

args=(start --lifecycle smoke --supervisor-cli "$SUP" --worker-cli "$WRK" --workers 2
      --allow-development-v2 --redis-runtime-dir "$TMP/r" --name "smoke-$SUP-$WRK-$MODE")
[ "$MODE" = service ] && args+=(--mode service)
if [ "$SUP" = pi ] || [ "$WRK" = pi ]; then
  : "${PI_MODEL:?PI_MODEL=provider/model is required for pi agents}"
  args+=(--allow-unsandboxed-pi --pi-model "$PI_MODEL")
fi
[ -n "${SMOKE_PROFILE:-}" ] && args+=(--profile "$SMOKE_PROFILE")
echo "smoke: $SUP+$WRK/$MODE in $TMP"
if ! (cd "$TMP" && "$PY" "$FLEET" "${args[@]}"); then
  echo "FAIL setup: start refused or failed (see above)" >&2
  exit 4
fi

"$PY" - "$CFG" "$MODE" "${SMOKE_TIMEOUT:-900}" "$ROOT" <<'EOF'
import json, sys, time, uuid
cfg_path, mode, timeout, root = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
sys.path.insert(0, root)
from pathlib import Path
from cmux_fleet import fleet, runtime_v2

cfg = fleet.load(cfg_path)
r = fleet.connect(cfg)
bus = runtime_v2.bus_for(cfg, r)
run = Path(cfg['run'])
names = ['supervisor'] + cfg['workers']
deadline = time.time() + timeout
checks = {}


def audit():
    return [f for _, f in r.xrange(bus.audit_key)]


def agent(n):
    return r.hgetall(fleet.key('agent', n))


def wait(pred, what):
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(3)
    print('FAIL timeout waiting for ' + what)
    return False


if not wait(lambda: all(agent(n).get('runtime_ready') == '1' and agent(n).get('state') == 'idle' for n in names),
            'READY of supervisor + 2 workers'):
    sys.exit(1)
print('ok READY: ' + ', '.join(n + '=' + agent(n).get('state', '?') for n in names))

# 4 human input: a manual line in worker-1's tab before any delivery must not settle anything.
w1 = agent('worker-1')
mgr = fleet.manager_for(cfg)
claude = cfg.get('worker_cli') == 'claude'
typed = mgr.send_prompt_to_idle(w1['pane'], 'Manual operator note: reply with the word noted.', bracketed=claude)
time.sleep(45)
before = audit()
manual_settled = [a for a in before if a.get('slot') == 'worker-1' and a.get('event') == 'committed']
manual_seen = [a for a in before if a.get('slot') == 'worker-1' and a.get('event') in ('turn.ignored',)]
checks['4 human input'] = bool(typed) and not manual_settled
print('   manual line typed=%s ignored-audit=%d committed=%d' % (typed, len(manual_seen), len(manual_settled)))

# The work: two files (task) or two requests (service), one per worker.
if mode == 'task':
    spec = {'goal': 'Smoke: worker-1 writes worker-1/ok1.txt containing OK; worker-2 writes worker-2/ok2.txt '
                    'containing OK. Dispatch both in one decision, declare both files as artifacts '
                    '(contains OK), then COMPLETED.', 'hours': 0.25,
            'artifacts': [{'path': 'worker-1/ok1.txt', 'contains': ['OK']},
                          {'path': 'worker-2/ok2.txt', 'contains': ['OK']}]}
    rid = 'smoke-task'
    runtime_v2.post_event(bus, dict(kind='task', request_id=rid, spec=spec), rid)
    done = wait(lambda: r.hget(fleet.TASK, 'status') in ('completed', 'blocked', 'budget_exhausted'), 'task end')
    status = r.hget(fleet.TASK, 'status')
    print('   task status=%s reason=%s' % (status, r.hget(fleet.TASK, 'reason')))
    work_ok = done and status == 'completed'
    rids = []
else:
    rids = ['smoke-r1', 'smoke-r2']
    for rid, w in zip(rids, cfg['workers']):
        # No 'to': the supervisor must route each request itself (one routing turn per request).
        spec = {'id': rid, 'payload': {'message': 'Smoke: route this to ' + w + '; it replies with reply = "OK from ' + w + '".'},
                'reply': {'required': ['reply'], 'allowed': ['reply']}, 'timeout_s': 600}
        runtime_v2.post_event(bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    done = wait(lambda: all(r.get(fleet.RESULT + x) for x in rids), 'both public results')
    results = {x: json.loads(r.get(fleet.RESULT + x) or '{}') for x in rids}
    for x, v in results.items():
        print('   %s status=%s result=%s' % (x, v.get('status'), v.get('result')))
    work_ok = done and all(v.get('status') == 'ok' for v in results.values())
time.sleep(15)  # let cards render (tui_cards refresh) and late hooks land
events = audit()

# 1 no message files
files = [str(p) for p in run.rglob('.inbox')] + [str(p) for p in run.rglob('msg-*.md')]
checks['1 no message files'] = not files

# 2 zero-model cards: results surface as UI cards; no "[Operator result" relay is ever delivered;
#   service: the supervisor gets exactly one routing delivery per request (no mechanical relay turn).
sup_delivers = [json.loads(a.get('detail') or '{}') for a in events
                if a.get('event') == 'deliver' and a.get('slot') == 'supervisor']
relays = [d for d in sup_delivers if str(d.get('text', '')).startswith('[Operator result')]
cards = r.xlen(bus.prefix + ':ui:supervisor')
rendered = [a for a in events if a.get('event') == 'ui.rendered' and a.get('slot') == 'supervisor']
ok2 = cards > 0 and not relays
if runtime_v2.tui_slot(cfg, 'supervisor'):
    ok2 = ok2 and bool(rendered) and (run / 'cards' / 'supervisor.log').is_file()
if mode == 'service':
    ok2 = ok2 and len(sup_delivers) == len(rids)
checks['2 zero-model cards'] = ok2
print('   supervisor deliveries=%d relays=%d ui cards=%d rendered=%d' % (len(sup_delivers), len(relays), cards, len(rendered)))

# 3 stale callback: a retired generation's turn end is refused by the daemon.
if runtime_v2.tui_slot(cfg, 'supervisor'):
    late = fleet.tui_turn(cfg, 'supervisor', int(agent('supervisor').get('generation', 0)) + 7,
                          thread=agent('supervisor').get('thread') or str(uuid.uuid4()), transcript='',
                          text='{"status":"RUNNING","dispatch":[]}', inputs=[], wait=5)
    ok3 = late.get('status') == 'stale'
else:
    n0 = len(events)
    gen = int(agent('worker-1').get('generation', 0)) + 7
    runtime_v2.post_event(bus, dict(kind='settled', agent='worker-1', generation=gen, run_id=bus.run_id,
                                    thread='stale-' + uuid.uuid4().hex, transcript='', delivery={},
                                    candidates=[], failed=False, aborted=False, unfinished=False),
                          'stale-' + uuid.uuid4().hex)
    time.sleep(5)
    tail = audit()[n0:]
    ok3 = any(a.get('event', '').startswith(('settle.', 'turn.stale', 'event.rejected')) for a in tail) \
        and not any(a.get('event') == 'committed' for a in tail)
checks['3 stale callback'] = ok3

errors = [a for a in events if a.get('level') == 'error']
print('   audit errors=%d' % len(errors))
for a in errors[:5]:
    print('     ' + json.dumps({k: a.get(k) for k in ('event', 'slot', 'error')}, ensure_ascii=False))
print(('PASS' if work_ok else 'FAIL') + ' work finished')
for name in sorted(checks):
    print(('PASS ' if checks[name] else 'FAIL ') + name)
sys.exit(0 if work_ok and all(checks.values()) else 1)
EOF
RESULT=$?
echo "smoke $SUP+$WRK/$MODE: $([ $RESULT = 0 ] && echo PASS || echo FAIL)"
exit $RESULT
