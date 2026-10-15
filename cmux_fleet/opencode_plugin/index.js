// cmux-fleet bridge plugin for opencode (v2 plugin API: export default {id, setup(ctx)}).
// Port of pi_extension_v2.ts. Lives inside the worker's `opencode --standalone --continue` TUI,
// spawns agent_bridge.py (JSONL over stdin/stdout) and talks to the daemon only through it.
//
// Measured on opencode 2.0.29 (worker-3 preflight, 2026-10-15):
// - setup() creates the session with an opencode-generated id (a self-made session id is refused by
//   Zen with FreeTierError); the TUI shows it because it starts with --continue on a fresh data dir.
// - execution events carry only sessionID. The only exact link is the id ctx.session.prompt returns
//   (the inbox id) and the ordered session.inbox.delivered{inboxID} events inside one execution.
//   One execution drains the whole inbox, so a manual message can share an execution with ours.
// Rules: a fleet turn (startup or delivery) settles only when its execution delivered exactly one
// inbox id and that id is the one prompt() returned for it. Mixed with manual ids -> hold, reported,
// never success, never re-sent. Executions without our id are manual and never settle.
// The plugin never retries a model call, never switches models, never answers a delete permission.
import { appendFileSync, mkdirSync } from "node:fs"
import { spawn } from "node:child_process"
import { createInterface } from "node:readline"
import { join } from "node:path"
import { payload, classifyFailure, modelProblem, permissionClass, usageOf, turnMessages, catalogHas, stepProblems } from "./lib.js"

const env = process.env
const COMMAND = JSON.parse(env.CMUX_FLEET_OC_BRIDGE || "[]")
const SLOT = env.CMUX_FLEET_OC_SLOT || ""
const GENERATION = Number(env.CMUX_FLEET_OC_GEN ?? NaN)
const SERVICE = env.CMUX_FLEET_OC_MODE === "service"
const MODEL = env.CMUX_FLEET_OC_MODEL || ""
const WINDOW = Number(env.CMUX_FLEET_OC_WINDOW || 0) || null
const SESSION_DIR = env.CMUX_FLEET_OC_SESSION_DIR || ""
const STARTUP = env.CMUX_FLEET_OC_STARTUP || ""
const TITLE = env.CMUX_FLEET_OC_LABEL || SLOT
const CATALOG_WAIT_MS = 1000 * (Number(env.CMUX_FLEET_OC_CATALOG_WAIT_S) || 60)
const PROMPT_WAIT_MS = 30000

export default {
  id: "cmux-fleet.bridge",
  async setup(ctx) {
    if (!COMMAND.length || !COMMAND.every((p) => typeof p === "string")) throw new Error("Fleet opencode bridge command missing")
    if (COMMAND.at(-2) !== SLOT || Number(COMMAND.at(-1)) !== GENERATION) throw new Error("Fleet opencode slot/generation mismatch")
    if (!MODEL.includes("/") || !SESSION_DIR || !STARTUP.trim()) throw new Error("Fleet opencode environment incomplete")
    mkdirSync(SESSION_DIR, { recursive: true })
    const [providerID, ...rest] = MODEL.split("/"); const modelID = rest.join("/")

    let sid
    let ready = false, shutdown = false, unhealthy = false
    let exec = null               // current execution of our session: {inbox: [inbox ids in delivered order]}
    let turn = null               // fleet turn in flight: {kind: "startup"|"delivery", env?, text, promise, inboxId, startedSent, settled}
    let candidates = []
    let ctxUsed = null, usageCost = null, sequence = 0
    let catalogSeen = false
    const pending = new Map(), reported = new Map(), seenUi = new Set()

    const transcript = () => join(SESSION_DIR, sid + ".jsonl")
    const activity = (o) => { if (sid) try { appendFileSync(transcript(), JSON.stringify({ t: Date.now(), ...o }) + "\n") } catch {} }
    const metadata = () => ({ thread: sid, transcript: transcript(), ctx_used: ctxUsed, ctx_window: WINDOW })

    const child = spawn(COMMAND[0], COMMAND.slice(1), { stdio: ["pipe", "pipe", "pipe"] })
    function fault(reason) {
      if (unhealthy) return
      unhealthy = true; activity({ fault: String(reason || "bridge unavailable") })
      for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("Fleet bridge unavailable")) }
      pending.clear()
    }
    function send(kind, fields = {}) {
      if (shutdown || !sid) return Promise.reject(new Error("Fleet bridge is not running"))
      const call_id = String(++sequence)
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(call_id); reject(new Error("Fleet command unconfirmed")) }, 35000)
        pending.set(call_id, { resolve, reject, timer })
        child.stdin.write(JSON.stringify({ ...metadata(), ...fields, kind, call_id }) + "\n", (e) => {
          if (e) { clearTimeout(timer); pending.delete(call_id); reject(e) }
        })
      })
    }
    const sendSafe = (kind, fields) => send(kind, fields).catch((e) => fault(e))
    const idleNow = () => { if (ready && !turn && !exec && !unhealthy) sendSafe("availability", { idle: true }) }

    /** Hold: the fleet turn's outcome cannot be attributed. Reported, never success, never re-sent. */
    async function hold(reason) {
      activity({ hold: reason })
      await send("hold", { reason }).catch(() => {})
      fault("held: " + reason)
    }

    /** Inject a fleet turn; bind the opencode inbox id that prompt() returns to it (durably: activity log, storage, started event). */
    function inject(t) {
      const tag = t.kind === "delivery" ? t.env.delivery_id : "startup-g" + GENERATION
      t.promise = ctx.session.prompt({ sessionID: sid, text: t.text, delivery: "queue", metadata: { fleet: { delivery: tag } } })
        .then((r) => {
          if (!r || typeof r.id !== "string" || !r.id) throw new Error("prompt returned no inbox id")
          t.inboxId = r.id
          activity({ bind: tag, inbox_id: r.id, message_id: t.env?.message_id ?? null })
          try { Promise.resolve(ctx.storage?.set?.("fleet-binding-" + tag, { inbox_id: r.id, message_id: t.env?.message_id ?? null })).catch(() => {}) } catch {} // storage API not yet measured; activity log + started event are the durable record
          maybeStarted()
          return r.id
        })
      t.promise.catch((e) => activity({ prompt_error: String(e) }))
      return t.promise
    }
    /** `started` once our id is seen delivered in the running execution (either order of prompt return / event). */
    function maybeStarted() {
      if (turn?.kind === "delivery" && !turn.startedSent && turn.inboxId && exec?.inbox.includes(turn.inboxId)) {
        turn.startedSent = true
        sendSafe("started", { oc_message_id: turn.inboxId })
      }
    }
    async function boundId(t) {
      let timer
      const timeout = new Promise((resolve) => { timer = setTimeout(() => resolve(null), PROMPT_WAIT_MS) })
      try { return await Promise.race([t.promise.catch(() => null), timeout]) } finally { clearTimeout(timer) }
    }
    async function messagesOf(inboxId) {
      try { return turnMessages(await ctx.session.context({ sessionID: sid }), inboxId) } catch (e) { activity({ context_error: String(e) }); return null }
    }

    async function executionEnded(type, data) {
      const e = exec; exec = null
      const t = turn
      if (!t || t.settled) { activity({ manual_turn: type }); idleNow(); return }
      const id = await boundId(t)
      if (!id) {
        if (t.kind === "startup") return startupFailed("startup prompt id unknown (prompt failed or timed out)")
        return hold("delivery prompt id unknown (prompt failed or timed out); execution outcome unattributable")
      }
      if (!e || !e.inbox.includes(id)) { activity({ manual_turn: type, inbox: e?.inbox ?? [] }); return } // ours still queued
      if (e.inbox.length !== 1) {
        const reason = `execution mixed fleet ${id} with other input ${JSON.stringify(e.inbox.filter((x) => x !== id))}`
        if (t.kind === "startup") return startupFailed(reason)
        return hold(reason)
      }
      const outcome = type.split(".").pop()
      // interrupted (measured: {sessionID, reason:"user"}, Esc in the TUI) carries no error object.
      const failure = outcome === "succeeded" ? null
        : outcome === "interrupted" ? { cls: "aborted", text: "interrupted: " + (data?.reason ?? "unknown") }
        : classifyFailure(data?.error, outcome)
      const msgs = await messagesOf(id)
      if (t.kind === "startup") {
        const text = (msgs?.assistants ?? []).map((m) => m.text).join("\n")
        t.settled = true
        const ack = await send("startup", { text, failed: !!failure && outcome !== "aborted" && outcome !== "interrupted",
          aborted: outcome === "aborted" || outcome === "interrupted", error: failure ? failure.text : "", error_class: failure?.cls ?? null })
          .catch((err) => (fault(err), { status: "rejected" }))
        turn = null
        ready = ack.status === "ready"
        if (!ready) { activity({ startup_rejected: ack.reason ?? null }); fault("startup not READY: " + (ack.reason ?? "")); return } // no retry protocol
        idleNow(); return
      }
      // Delivery: exact, clean execution of this delivery.
      let failed = !!failure
      if (failure) await send("candidate", { candidate: { tool: "model", error: failure.text, error_class: failure.cls } }).catch(fault)
      if (!failure) {
        // Attribution: the assistant messages of this execution are its step.started/step.ended assistantMessageIDs
        // (measured; context() messages have no parent). Every step: locked model (step.started) and cost 0 (step.ended).
        // The context() assistants after our user message must be exactly those ids.
        const problems = stepProblems(MODEL, e.steps)
        const ctxIds = msgs ? msgs.assistants.map((m) => m.id).join(",") : null
        if (!problems.length && ctxIds !== [...e.steps.keys()].join(",")) problems.push(`model_lock: step ids ${[...e.steps.keys()].join(",")} != context assistants ${ctxIds}`)
        if (!problems.length) problems.push(...msgs.assistants.map((m) => modelProblem(MODEL, m)).filter(Boolean))
        // Cumulative session cost (usage.updated) only as a non-zero alarm; never for context size.
        if (usageCost !== null && !(Number.isFinite(usageCost) && usageCost === 0)) problems.push(`cost_nonzero: session usage cost ${usageCost}`)
        if (problems.length) {
          failed = true
          await send("candidate", { candidate: { tool: "model", error: problems[0], error_class: "model_lock" } }).catch(fault)
        }
      }
      const batch = candidates; candidates = []; t.settled = true
      await send("settled", { candidates: batch, failed, aborted: outcome === "aborted" || outcome === "interrupted", unfinished: false,
        oc_message_id: id, inbox: e.inbox }).catch(fault)
    }
    async function startupFailed(reason) {
      if (turn) turn.settled = true
      await send("startup", { text: "", failed: true, aborted: false, error: reason }).catch(() => {})
      turn = null; fault("startup failed: " + reason)
    }

    async function replyPermission(rid, decision) {
      // PENDING worker-3 retest: argument shape of ctx.permission.reply.
      await ctx.permission.reply({ sessionID: sid, requestID: rid, id: rid, reply: decision })
    }
    async function checkPermissions(evp) {
      // PENDING worker-3 retest: event names / ctx.permission.list signature. Each new request is reported once.
      let list = []
      try { list = await ctx.permission.list({ sessionID: sid }) } catch { list = [] }
      const items = Array.isArray(list) ? [...list] : [...(list?.data ?? [])]
      if (evp && typeof evp === "object" && (evp.id || evp.requestID)) items.push(evp)
      for (const req of items) {
        const rid = req?.id ?? req?.requestID
        if (!rid || reported.has(rid) || (req.sessionID && req.sessionID !== sid)) continue
        const cls = permissionClass(req)
        reported.set(rid, req); activity({ permission: rid, class: cls })
        sendSafe("permission", { request: { ...req, id: rid }, class: cls })
      }
    }

    function handle(frame) {
      if (shutdown) return
      if (frame.kind === "reply") {
        const p = pending.get(frame.call_id || "")
        if (p) { clearTimeout(p.timer); pending.delete(frame.call_id); p.resolve(frame.answer) }
      } else if (frame.kind === "ui") {
        if (frame.event_id && !seenUi.has(frame.event_id)) { seenUi.add(frame.event_id); activity({ ui: frame.card?.render ?? "" }) }
      } else if (frame.kind === "fault") {
        fault(frame.reason)
      } else if (frame.kind === "completed") {
        if (turn?.kind === "delivery" && turn.env.message_id === frame.message_id) { turn = null; candidates = []; idleNow() }
      } else if (frame.kind === "permission_reply") {
        // Second gate: only a request this plugin reported, re-classified here as 'other'. Deletes never.
        const req = reported.get(frame.request_id)
        if (!req || permissionClass(req) !== "other" || !["once", "reject"].includes(frame.decision)) { activity({ permission_refused: frame.request_id }); return }
        reported.delete(frame.request_id)
        replyPermission(frame.request_id, frame.decision)
          .then(() => activity({ permission_replied: frame.request_id, decision: frame.decision }))
          .catch((e) => activity({ permission_reply_error: String(e) }))
      } else if (frame.kind === "delivery" && frame.delivery) {
        const d = frame.delivery
        if (unhealthy || turn || exec || !ready) { sendSafe("reject_delivery", { reason: "opencode is occupied by a manual/other run" }); return }
        if (d.slot !== SLOT || d.session_id !== sid || d.generation !== GENERATION) { fault("delivery identity mismatch"); return }
        candidates = []
        turn = { kind: "delivery", env: d, text: d.body.text, inboxId: null, startedSent: false, settled: false }
        inject(turn).catch(() => {}) // a failed prompt is decided at execution end / by hold below
        turn.promise.catch((e) => { if (turn?.env === d && !exec) hold("delivery prompt failed: " + e) })
      }
    }
    createInterface({ input: child.stdout }).on("line", (line) => { let f; try { f = JSON.parse(line) } catch { return fault("bad bridge frame") } handle(f) })
    child.on("error", fault); child.on("exit", () => { if (!shutdown) fault("bridge exited") })

    await ctx.tool.transform((e) => e.add({
      name: "fleet_result",
      description: "Submit your final business JSON result for daemon acceptance. Identity is bound by code. Call exactly once when the fleet task is done.",
      input: SERVICE ? { type: "object", properties: { result: { type: "object" } }, required: ["result"] } : {
        type: "object", required: ["result"], properties: { result: { type: "object", additionalProperties: true,
          required: ["done", "evidence", "not_done", "blocked"], properties: {
            done: { type: "array", items: { type: "string" }, minItems: 1 }, evidence: { type: "array", items: { type: "string" }, minItems: 1 },
            not_done: { type: "string" }, blocked: { type: "string" } } } } },
      async execute(input) {
        // Only while an execution runs that already delivered this delivery's inbox id.
        if (unhealthy || turn?.kind !== "delivery" || turn.settled || turn.env.body?.purpose === "handoff"
            || !exec || !turn.inboxId || !exec.inbox.includes(turn.inboxId)) throw new Error("No active fleet delivery for this tool")
        const value = input?.result
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Candidate must be a business object")
        const blockedOnly = Object.keys(value).length === 1 && typeof value.blocked === "string"
        const spec = turn.env.body?.reply
        if (spec && !blockedOnly && (spec.required.some((k) => !(k in value)) || (spec.allowed && Object.keys(value).some((k) => !spec.allowed.includes(k)))))
          throw new Error("Business result violates required/allowed reply keys")
        const candidate = { tool: "fleet_result", value }
        candidates.push(candidate); activity({ candidate: turn.env.message_id })
        await send("candidate", { candidate })
        return { content: "候选已记录，等待回合结束与验收。" }
      },
    }))

    const ac = new AbortController()
    ;(async () => {
      for await (const ev of ctx.event.subscribe({ signal: ac.signal })) {
        const type = String(ev?.type || ""), d = payload(ev)
        if (type === "models-dev.refreshed") catalogSeen = true
        if (!sid || d?.sessionID !== sid) { if (sid && type.includes("permission") && !d?.sessionID) await checkPermissions(d); continue }
        activity({ ev: type, seq: ev?.durable?.seq ?? null, inbox: d.inboxID ?? null })
        if (type === "session.usage.updated") {
          // Session-cumulative (includes the title call): cost alarm only, never ctx_used.
          if (d.cost !== undefined) usageCost = typeof d.cost === "number" ? d.cost : NaN
        } else if (type === "session.step.started" || type === "session.step.ended") {
          const step = exec && d.assistantMessageID ? (exec.steps.get(d.assistantMessageID) ?? {}) : null
          if (step) exec.steps.set(d.assistantMessageID, { ...step, ...(type === "session.step.started" ? { model: d.model } : { cost: d.cost, ended: true }) })
          // opencode's own context measure: the last step's five-term token sum.
          if (type === "session.step.ended") { const u = usageOf(d.tokens); if (u !== null) ctxUsed = u }
        } else if (type === "session.execution.started") {
          exec = { inbox: [], steps: new Map() }
          if (ready && !turn) sendSafe("availability", { idle: false })
        } else if (type === "session.inbox.delivered") {
          if (exec) { exec.inbox.push(d.inboxID); maybeStarted() } else activity({ delivered_outside_execution: d.inboxID })
        } else if (type.startsWith("session.execution.")) {
          await executionEnded(type, d)
        } else if (type.includes("permission")) {
          await checkPermissions(d)
        }
      }
    })().catch((e) => fault("event stream: " + e))

    // Boot: own session (opencode-generated id) -> bridge init -> wait for the model catalogue -> startup turn.
    ;(async () => {
      const s = await ctx.session.create({ title: TITLE, model: { providerID, id: modelID } })
      if (!s || typeof s.id !== "string") throw new Error("session.create returned no id")
      sid = s.id; activity({ bound: sid, title: TITLE })
      const ack = await send("init")
      if (ack?.status !== "registered") throw new Error(ack?.reason || "Fleet binding rejected")
      const deadline = Date.now() + CATALOG_WAIT_MS
      let ok = false
      while (!ok && Date.now() < deadline && !unhealthy) {
        let listed = null
        try { listed = catalogHas(await ctx.model.list(), providerID, modelID) } catch { listed = null }
        ok = listed === true || (listed === null && catalogSeen)
        if (!ok) await new Promise((r) => setTimeout(r, 500))
      }
      if (!ok) { await startupFailed(`model ${MODEL} not available in the opencode catalogue within ${CATALOG_WAIT_MS / 1000}s`); return }
      activity({ catalog_ready: MODEL })
      turn = { kind: "startup", text: STARTUP, inboxId: null, settled: false }
      inject(turn).catch(() => {})
    })().catch(async (e) => {
      activity({ boot_error: String(e) })
      if (sid) await startupFailed("boot: " + e); else fault("boot: " + e)
    })

    return async () => {
      if (shutdown) return
      ac.abort()
      try { child.stdin.end(JSON.stringify({ kind: "shutdown", call_id: "shutdown" }) + "\n") } catch {}
      shutdown = true
      await new Promise((resolve) => {
        if (child.exitCode !== null) return resolve()
        const t = setTimeout(() => { child.kill("SIGTERM"); resolve() }, 5000)
        child.once("exit", () => { clearTimeout(t); resolve() })
      })
      for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("Fleet session shut down")) }
      pending.clear()
    }
  },
}
