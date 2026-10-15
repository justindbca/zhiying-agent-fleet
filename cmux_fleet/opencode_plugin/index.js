// cmux-fleet bridge plugin for opencode (v2 plugin API: export default {id, setup(ctx)}).
// 1:1 port of pi_extension_v2.ts: lives inside the worker's opencode TUI process, spawns
// agent_bridge.py (JSONL over stdin/stdout), injects deliveries in-process, reports the turn
// that a delivery started, and never settles a manual turn. It never retries, never switches
// models and never answers a delete-class permission request.
//
// PENDING LIVE TEST (worker-3): exact event / message field names, whether the TUI or the plugin
// creates the session, and whether ctx.session.prompt accepts our own message id. Field access goes
// through the tolerant helpers below; the binding mode is CMUX_FLEET_OC_BIND ("text" | "id").
import { appendFileSync, mkdirSync } from "node:fs"
import { spawn } from "node:child_process"
import { createInterface } from "node:readline"
import { payload, sessionOf, messageOf, textOf, roleOf, ocMessageId, modelProblem, classifyError, permissionClass, usageOf } from "./lib.js"
import { join } from "node:path"

const env = process.env
const COMMAND = JSON.parse(env.CMUX_FLEET_OC_BRIDGE || "[]")
const SLOT = env.CMUX_FLEET_OC_SLOT || ""
const GENERATION = Number(env.CMUX_FLEET_OC_GEN ?? NaN)
const SERVICE = env.CMUX_FLEET_OC_MODE === "service"
const MODEL = env.CMUX_FLEET_OC_MODEL || ""
const WINDOW = Number(env.CMUX_FLEET_OC_WINDOW || 0) || null
const SESSION_DIR = env.CMUX_FLEET_OC_SESSION_DIR || ""
const STARTUP = (env.CMUX_FLEET_OC_STARTUP || "").trim()
const BIND = env.CMUX_FLEET_OC_BIND === "id" ? "id" : "text"

// ---------------------------------------------------------------- plugin
export default {
  id: "cmux-fleet.bridge",
  async setup(ctx) {
    if (!COMMAND.length || !COMMAND.every((p) => typeof p === "string")) throw new Error("Fleet opencode bridge command missing")
    if (COMMAND.at(-2) !== SLOT || Number(COMMAND.at(-1)) !== GENERATION) throw new Error("Fleet opencode slot/generation mismatch")
    if (!MODEL || !SESSION_DIR || !STARTUP) throw new Error("Fleet opencode environment incomplete")
    mkdirSync(SESSION_DIR, { recursive: true })

    let sid                       // bound opencode session (the one the fleet startup message runs in)
    let startupMsg                // opencode message id of the fleet startup message
    let ready = false, shutdown = false, unhealthy = false
    let phase = "startup"         // startup | idle | delivery | manual
    let running = false           // an execution of the bound session is in progress
    let active                    // current fleet delivery envelope
    let activeMsg                 // opencode message id bound to `active`
    let lastUser                  // last user message id seen in the bound session
    let candidates = [], failed = false, lastError = "", startupText = "", assistants = []
    let ctxUsed = null, sequence = 0
    const pending = new Map(), reported = new Map(), seenUi = new Set()

    const transcript = () => join(SESSION_DIR, sid + ".jsonl")
    const activity = (o) => { if (sid) try { appendFileSync(transcript(), JSON.stringify({ t: Date.now(), ...o }) + "\n") } catch {} }
    const metadata = () => ({ thread: sid, transcript: transcript(), ctx_used: ctxUsed, ctx_window: WINDOW })

    const child = spawn(COMMAND[0], COMMAND.slice(1), { stdio: ["pipe", "pipe", "pipe"] })
    function fault(reason) {
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

    async function inject(envlp) {
      const args = { sessionID: sid, text: envlp.body.text, delivery: "queue" }
      if (BIND === "id") { activeMsg = ocMessageId(envlp.delivery_id); args.id = activeMsg }
      activity({ deliver: envlp.message_id, oc_message: activeMsg ?? null })
      try { await ctx.storage?.set?.("fleet-binding", { message_id: envlp.message_id, delivery_id: envlp.delivery_id, oc_message_id: activeMsg ?? null }) } catch {}
      await ctx.session.prompt(args)
    }
    async function replyPermission(rid, decision) {
      // PENDING LIVE TEST: argument shape of ctx.permission.reply.
      await ctx.permission.reply({ sessionID: sid, requestID: rid, id: rid, reply: decision })
    }
    async function handle(frame) {
      if (shutdown) return
      if (frame.kind === "reply") {
        const p = pending.get(frame.call_id || "")
        if (p) { clearTimeout(p.timer); pending.delete(frame.call_id); p.resolve(frame.answer) }
      } else if (frame.kind === "ui") {
        if (frame.event_id && !seenUi.has(frame.event_id)) { seenUi.add(frame.event_id); activity({ ui: frame.card?.render ?? "" }) }
      } else if (frame.kind === "fault") {
        fault(frame.reason)
      } else if (frame.kind === "completed") {
        if (active?.message_id === frame.message_id) { active = undefined; activeMsg = undefined; candidates = []; if (phase === "delivery") phase = "idle" }
      } else if (frame.kind === "permission_reply") {
        // Second gate: only a request this plugin reported as 'other', and never a delete (re-classified here).
        const req = reported.get(frame.request_id)
        if (!req || permissionClass(req) !== "other" || !["once", "reject"].includes(frame.decision)) { activity({ permission_refused: frame.request_id }); return }
        reported.delete(frame.request_id)
        try { await replyPermission(frame.request_id, frame.decision); activity({ permission_replied: frame.request_id, decision: frame.decision }) }
        catch (e) { activity({ permission_reply_error: String(e) }) }
      } else if (frame.kind === "delivery" && frame.delivery) {
        const d = frame.delivery
        if (unhealthy || active || !ready || phase !== "idle" || running) {
          sendSafe("reject_delivery", { reason: "opencode is occupied by a manual/other run" }); return
        }
        if (d.slot !== SLOT || d.session_id !== sid || d.generation !== GENERATION) { fault("delivery identity mismatch"); return }
        active = d; activeMsg = undefined; phase = "delivery"; candidates = []; failed = false; lastError = ""; assistants = []
        try { await inject(d) } catch (e) { failed = true; lastError = "inject failed: " + e; fault(lastError) }
      }
    }
    createInterface({ input: child.stdout }).on("line", (line) => { try { handle(JSON.parse(line)) } catch { fault("bad bridge frame") } })
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
        if (!active || phase !== "delivery" || active.body?.purpose === "handoff" || unhealthy) throw new Error("No active fleet delivery for this tool")
        const value = input?.result
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Candidate must be a business object")
        const blockedOnly = Object.keys(value).length === 1 && typeof value.blocked === "string"
        const spec = active.body?.reply
        if (spec && !blockedOnly && (spec.required.some((k) => !(k in value)) || (spec.allowed && Object.keys(value).some((k) => !spec.allowed.includes(k)))))
          throw new Error("Business result violates required/allowed reply keys")
        const candidate = { tool: "fleet_result", value }
        candidates.push(candidate); activity({ candidate: active.message_id })
        await send("candidate", { candidate })
        return { content: "候选已记录，等待回合结束与验收。" }
      },
    }))

    async function checkPermissions(evp) {
      // PENDING LIVE TEST: event names / ctx.permission.list signature. Report each new request once.
      let list = []
      try { list = await ctx.permission.list({ sessionID: sid }) } catch { list = [] }
      const items = Array.isArray(list) ? list : (list?.data ?? [])
      if (evp && typeof evp === "object" && (evp.id || evp.requestID)) items.push(evp)
      for (const req of items) {
        const rid = req?.id ?? req?.requestID
        if (!rid || reported.has(rid) || (sessionOf(req) && sessionOf(req) !== sid)) continue
        const cls = permissionClass(req)
        reported.set(rid, req); activity({ permission: rid, class: cls })
        sendSafe("permission", { request: { ...req, id: rid }, class: cls })
      }
    }

    async function executionEnded(type, p) {
      running = false
      const outcome = type.split(".").pop()
      const err = p?.error?.message ?? p?.error ?? p?.message ?? ""
      const bad = outcome !== "succeeded"
      if (phase === "startup") {
        const ack = await send("startup", { text: startupText, failed: bad && outcome !== "aborted", aborted: outcome === "aborted", error: bad ? String(err || outcome) : "" })
          .catch((e) => (fault(e), { status: "rejected" }))
        ready = ack.status === "ready"; phase = ready ? "idle" : "startup"
        // A nudged startup ("reply exactly READY") is typed by the daemon; it must come back as startup again.
        if (!ready) startupMsg = undefined
      } else if (phase === "delivery" && active) {
        if (bad) { failed = true; lastError = String(err || outcome) }
        if (lastError) await send("candidate", { candidate: { tool: "model", error: lastError, error_class: classifyError(lastError) } }).catch(fault)
        // Model lock: every assistant message of this turn must be the locked free model at cost 0.
        const problems = assistants.length ? assistants.map((a) => modelProblem(MODEL, a)).filter(Boolean) : ["model_lock: no assistant message to check"]
        if (!bad && problems.length) {
          failed = true
          await send("candidate", { candidate: { tool: "model", error: problems[0], error_class: "model_lock" } }).catch(fault)
        }
        const batch = candidates; candidates = []; phase = "idle"
        await send("settled", { candidates: batch, failed, aborted: outcome === "aborted", unfinished: false }).catch(fault)
      } else if (phase === "manual") {
        phase = ready ? "idle" : "startup" // manual turns never settle (nor count as startup)
      }
      if (ready && !active && !unhealthy && phase === "idle") sendSafe("availability", { idle: true })
    }

    const ac = new AbortController()
    ;(async () => {
      for await (const ev of ctx.event.subscribe({ signal: ac.signal })) {
        const type = String(ev?.type || ""), p = payload(ev), s = sessionOf(p)
        // Bind: the session in which the fleet startup message appears (never "the first execution").
        if (!sid && s && roleOf(p) === "user" && textOf(p).trim() === STARTUP) {
          sid = s; startupMsg = messageOf(p); lastUser = startupMsg
          activity({ bound: sid, startup_message: startupMsg ?? null })
          const ack = await send("init").catch((e) => (fault(e), { status: "rejected" }))
          if (ack.status !== "registered") { fault(ack.reason || "Fleet binding rejected"); continue }
        }
        if (!sid || (s && s !== sid)) continue
        activity({ ev: type })
        if (roleOf(p) === "user") {
          const mid = messageOf(p); if (mid) lastUser = mid
          if (BIND === "text" && active && !activeMsg && textOf(p) === active.body.text) activeMsg = mid
          if (!ready && phase === "startup" && textOf(p).trim() === STARTUP) startupMsg = mid
        } else if (roleOf(p) === "assistant") {
          const info = p.info ?? p.message ?? p
          if (info?.tokens) { const u = usageOf(info.tokens); if (u !== null) ctxUsed = u }
          if (phase === "delivery" && info && (info.model || info.modelID)) {
            const key = info.id ?? assistants.length
            const i = assistants.findIndex((a) => (a.id ?? -1) === key); i >= 0 ? (assistants[i] = info) : assistants.push(info)
          }
          if (phase === "startup") { const t = textOf(info); if (t) startupText = t }
          if (info?.error && phase === "delivery") lastError = String(info.error?.message ?? info.error?.data?.message ?? info.error)
        }
        if (type === "session.usage.updated" && p.tokens) { const u = usageOf(p.tokens); if (u !== null) ctxUsed = u }
        if (type.includes("permission")) await checkPermissions(p)
        if (type === "session.execution.started") {
          running = true
          const trigger = messageOf(p) ?? lastUser
          if (phase === "delivery" && active && activeMsg && trigger === activeMsg) { await send("started").catch(fault) }
          else if (!ready && startupMsg && trigger === startupMsg) { phase = "startup" }
          else if (ready) { if (phase !== "delivery") phase = "manual"; sendSafe("availability", { idle: false }) }
          // Not ready and not our startup message: a manual turn before READY; neither startup nor settled.
          else phase = "manual"
        } else if (type.startsWith("session.execution.") && type !== "session.execution.started") {
          await executionEnded(type, p)
        }
      }
    })().catch((e) => fault("event stream: " + e))

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
