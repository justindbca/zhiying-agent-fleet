// Drives cmux_fleet/opencode_plugin/index.js with a fake opencode ctx. argv: <plugin index.js> <scenario>.
// Prints {frames: [...bridge frames...]} as JSON. Scenarios: manual_first, mixed_startup.
import { readFileSync, existsSync, mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, dirname } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const [plugin, scenario] = process.argv.slice(2)
const dir = mkdtempSync(join(tmpdir(), "ocharness-"))
const log = join(dir, "frames.jsonl")
const MODEL = "opencode/deepseek-v4.1-flash-free"
Object.assign(process.env, {
  OC_FAKE_LOG: log,
  CMUX_FLEET_OC_BRIDGE: JSON.stringify([process.execPath, join(dirname(fileURLToPath(import.meta.url)), "oc_fake_bridge.mjs"), "cfg.json", "worker-1", "0"]),
  CMUX_FLEET_OC_SLOT: "worker-1", CMUX_FLEET_OC_GEN: "0", CMUX_FLEET_OC_MODE: "task", CMUX_FLEET_OC_MODEL: MODEL,
  CMUX_FLEET_OC_WINDOW: "200000", CMUX_FLEET_OC_SESSION_DIR: join(dir, "sessions"), CMUX_FLEET_OC_STARTUP: "Reply READY only.",
  CMUX_FLEET_OC_CATALOG_WAIT_S: "2", CMUX_FLEET_OC_LABEL: "worker-1",
})
const SID = "ses_harness01"
const queue = [], waiters = []
const push = (type, data) => { const ev = { type, data: { sessionID: SID, ...data } }; const w = waiters.shift(); w ? w(ev) : queue.push(ev) }
const next = () => queue.length ? Promise.resolve(queue.shift()) : new Promise((r) => waiters.push(r))
let prompted = 0
const contextList = [
  { id: "msg_manual", type: "user" }, { id: "a_manual", type: "assistant", content: [{ type: "text", text: "hi human" }],
    model: { providerID: "opencode", id: "deepseek-v4.1-flash-free" }, cost: 0 }, { type: "idle" },
  { id: "msg_startup", type: "user" }, { id: "a_ready", type: "assistant", content: [{ type: "text", text: "READY" }],
    model: { providerID: "opencode", id: "deepseek-v4.1-flash-free" }, cost: 0 }, { type: "idle" },
]
const ctx = {
  session: {
    create: async () => ({ id: SID }),
    prompt: async () => { prompted++; return { id: "msg_startup" } },
    context: async () => contextList,
  },
  model: { list: async () => [{ providerID: "opencode", id: "deepseek-v4.1-flash-free" }] },
  tool: { transform: async (fn) => fn({ add() {} }) },
  permission: { list: async () => [], reply: async () => {} },
  storage: { set: async () => {}, get: async () => undefined },
  event: { subscribe: () => ({ [Symbol.asyncIterator]() { return { next: async () => ({ value: await next(), done: false }) } } }) },
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const frames = () => existsSync(log) ? readFileSync(log, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []
const until = async (pred, ms = 3000) => { const end = Date.now() + ms; while (Date.now() < end) { if (pred()) return true; await sleep(20) } return false }

const mod = await import(pathToFileURL(plugin).href)
const dispose = await mod.default.setup(ctx)
await until(() => prompted > 0)
const step = (id) => {
  push("session.step.started", { assistantMessageID: id, model: { providerID: "opencode", id: "deepseek-v4.1-flash-free" } })
  push("session.step.ended", { assistantMessageID: id, cost: 0, tokens: { input: 10, output: 1, reasoning: 0, cache: { read: 0, write: 0 } } })
}
const out = { prompted }
if (scenario === "manual_first") {
  // A human types before the startup message is taken: that execution delivers only the manual id.
  push("session.execution.started", {}); push("session.inbox.delivered", { inboxID: "msg_manual" }); step("a_manual")
  push("session.execution.succeeded", {})
  await sleep(300)
  out.after_manual = frames().map((f) => f.kind)
  push("session.execution.started", {}); push("session.inbox.delivered", { inboxID: "msg_startup" }); step("a_ready")
  push("session.execution.succeeded", {})
  await until(() => frames().some((f) => f.kind === "startup"))
} else if (scenario === "mixed_startup") {
  push("session.execution.started", {}); push("session.inbox.delivered", { inboxID: "msg_manual" })
  push("session.inbox.delivered", { inboxID: "msg_startup" }); step("a_ready"); push("session.execution.succeeded", {})
  await until(() => frames().some((f) => f.kind === "startup"))
}
await sleep(100)
out.frames = frames()
await dispose()
console.log(JSON.stringify(out))
process.exit(0)
