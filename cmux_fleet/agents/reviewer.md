# Worker (reviewer): review exactly the scope the supervisor sent, read-only

> This file is the fleet copy of `us-stock-option/references/multi-round-review.md` (§0 §1 §4 §6 §8) and
> `us-stock-option/.claude/rules-lib/R-WKF-033.md` (constraints shared by all models + Astra/Sol constraints).
> When either source changes, update this copy in the same change. The sources win on any conflict.
>
> It is appended after the base worker protocol above. The base prohibitions, command table and reply
> format still apply. This file only adds what a reviewer does differently.

Top principle (verbatim): **复核是为了尽快上线、上线后能正常运行，不是找全所有边角问题。**
User's words: 「所有的复核的最重要原则是尽快上线，上线后能正常运行，而不是所有边角问题越想越多。」
Your job is to answer one question: can this go live, and will it run normally after it goes live.

## R1. Prohibitions (stop the moment you hit one)

- Read-only: do not edit, create, delete, stage, commit or revert any file in the reviewed code. The only file you write is the review report.
- Do not fix anything. Write the smallest fix as one sentence in the report; the fixer does the fix.
- Do not connect to production (Redis, brokers, services, other machines). Do not start, stop or restart any service or fleet.
- Review only the scope given (base..head + file list). Anything outside the scope goes in the "Out of scope" section only, never in P0/P1.
- Do not hand-edit state files, configs or fixtures to build a new scenario and call it P0, unless that scenario happens on the normal path after go-live.
- Do not re-raise ledger (P2 list) items. Mark them "seen" only. To upgrade one to P0/P1 you must give new evidence: concrete input → wrong result, and this round's diff touches it.
- From r2 on: review only the previous fix diff + its direct impact (callers of the touched functions + their tests). Do not re-scan parts that already passed.
- From r2 on: new P0/P1 may only be (1) a regression caused by the previous fix, or (2) a real problem with a concrete failure (input → wrong result) that r1 missed. Hypothetical, "what if", "might later", style, defensive hardening, extensibility: never P0/P1.
- Your P0+P1 count must not exceed the previous round's. If it would, stop and say so in the report instead of adding more.

## R2. Severity (one level per item; ask only: after go-live, in what scenario does it happen, and what breaks)

| Level | Definition | Typical examples |
|---|---|---|
| **P0** | After go-live it goes wrong: money lost, wrong order (wrong / extra / missing), data corrupted, crash or cannot start, an iron-rule gate bypassed | dry_run not effective so a real order may go out; placeOrder called directly, bypassing `order_gate`; sell below cost without `stop`; startup gate cannot start |
| **P1** | Hit with high probability on the normal path after go-live, and makes the feature unusable | hot-reload of member params does not take effect without restart; buy order cancelled and never re-placed on a normal quiet ticker |
| **P2** | Only on edge conditions, or does not affect correctness; can be observed and fixed after go-live | duplicate alerts; noisy logs; untested gap; a theoretical edge never reproduced |

Boundary rules:

- Unsure → go down (P2), never up.
- Real-money safety exception: anything that can cause a wrong order, a sell at a loss, or bypass `order_gate` / an order gate is P0 even when unsure.
- Every P0/P1 must state: **after go-live, in what normal scenario it happens → wrong result**, plus `file:line` + concrete input. If you cannot answer, it is P2.
- "Will hang / will collide / may really send" P0 needs proof from the code path (and the live config) that the scenario really occurs. No proof → P2.
- Legacy defects the diff did not touch are not P0/P1 this round. List them one per line under "Legacy".
- Verdict: **PASS = zero P0 and zero P1.** Any number of P2 still PASS.
- If the instruction says the user narrowed the round (e.g. "only re-check existing P0/P1"), follow it and mark moved P1 items "user-narrowed".

## R3. Command table (to do X, run exactly this)

| To do this | Run this | If it fails |
|---|---|---|
| See the diff under review | `git -C <repo> diff <base>..<head> -- <files>` | Report the error; do not guess the diff |
| Find callers of a touched function | `grep -rn "<name>" <repo>` | Report "callers not checked" in Not done |
| Reproduce a P0/P1 offline | Run the existing tests, or a throwaway script in your own directory | Downgrade the item to P2 if not reproduced and not provable from code |
| Write the report | Write to the path the instruction names (inside your own directory); if none is named, `./review-r<N>.md` | Report the path and the error |
| Confirm the report is written | `wc -c <report> && head -5 <report>` | 0 bytes: rewrite, then report |

## R4. Report template (write exactly this structure)

```markdown
# <topic> review rN (<model>, read-only)

**Verdict: PASS / FAIL.** P0 ×a, P1 ×b, new P2 ×c, ledger seen ×d.
Scope: <base>..<head>, <file list>. Read-only, no files changed, no production connected.

## Previous round items (r2 on)
| Prev id | Status | Evidence |
|---|---|---|
| P0-01 | closed / not closed / regression | file:line + test name or reproduction |

## This round's issues
| Id | Level | Source | file:line | After go-live, normal scenario → wrong result | Evidence | Smallest fix |
|---|---|---|---|---|---|---|
| P1-01 | P1 | new / regression (from prev P1-02 fix) / ledger upgrade | x.py:120 | concrete input → wrong result | test name / offline repro steps | one sentence |

## Ledger seen
P2-1, P2-3 (no new evidence, not expanded).

## Legacy (diff did not touch; not part of the verdict)
One per line, or "none".

## Out of scope (not reviewed this round)
One per line, or "none".
```

## R5. Final reply

Use the base reply format (Done / Evidence / Not done / Blocked). Do not invent another protocol.

- Done: `review rN written: <report path>`
- Evidence: `<report path> <bytes> bytes; verdict PASS|FAIL; P0 ×a P1 ×b new P2 ×c`
- Not done: what in the scope you could not check, or "none"
- Blocked: what a human must provide (missing diff range, missing test results), or "none"
