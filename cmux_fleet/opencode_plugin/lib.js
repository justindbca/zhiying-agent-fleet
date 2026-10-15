// Pure helpers of the cmux-fleet opencode plugin. Kept out of index.js: the opencode v2 loader
// treats named function exports of the plugin entry as v1 plugins (LoadError).
import { createHash } from "node:crypto"

export function payload(ev) { return ev?.properties ?? ev?.data ?? ev ?? {} }
export function findKey(o, keys, depth = 0) {
  if (!o || typeof o !== "object" || depth > 5) return undefined
  for (const k of keys) if (typeof o[k] === "string" && o[k]) return o[k]
  for (const v of Object.values(o)) { const s = findKey(v, keys, depth + 1); if (s) return s }
  return undefined
}
export const sessionOf = (o) => findKey(o, ["sessionID", "sessionId"])
export const messageOf = (o) => findKey(o, ["messageID", "messageId"]) ?? (o?.info?.id || o?.message?.id || o?.id)
export function textOf(o) {
  if (!o || typeof o !== "object") return ""
  if (typeof o.text === "string") return o.text
  const parts = o.parts ?? o.content ?? o.message?.parts ?? o.message?.content ?? o.info?.parts
  if (Array.isArray(parts)) return parts.map((p) => (typeof p === "string" ? p : p?.text || "")).join("")
  return ""
}
export function roleOf(o) { return o?.role ?? o?.info?.role ?? o?.message?.role }
export const ocMessageId = (deliveryId) => "msg_" + createHash("sha256").update(String(deliveryId)).digest("hex").slice(0, 26)

/** Model lock: same rule as opencode_adapter.model_problem (strict cost: finite number, exactly 0). */
export function modelProblem(locked, info) {
  const [provider, ...rest] = String(locked).split("/"); const id = rest.join("/")
  if (!provider || !id) return "model_lock: no locked model configured"
  if (!info || typeof info !== "object") return "model_lock: no assistant message to check"
  const m = (info.model && typeof info.model === "object") ? info.model : {}
  const gotProvider = m.providerID ?? info.providerID
  const gotId = m.id ?? m.modelID ?? info.modelID
  if (gotProvider !== provider || gotId !== id) return `model_lock: expected ${provider}/${id}, got ${gotProvider}/${gotId}`
  const cost = info.cost
  if (typeof cost !== "number" || !Number.isFinite(cost) || cost !== 0) return `cost_nonzero: expected cost 0, got ${JSON.stringify(cost)}`
  return null
}

/** Same classes and order as opencode_adapter.classify_error. */
export function classifyError(text) {
  const t = String(text || "")
  if (/freetiererror|free tier can only be used/i.test(t)) return "free_tier_rejected"
  if (/(?<!\d)429(?!\d)|rate[ -]?limit|too many requests/i.test(t)) return "rate_limited"
  if (/model unavailable|model not found|unknown model|no such model|model .{0,80}not available/i.test(t)) return "model_unavailable"
  if (t.startsWith("model_lock:") || t.startsWith("cost_nonzero:")) return "model_lock"
  if (/\baborted\b|\binterrupted\b|AbortError/i.test(t)) return "aborted"
  return "other"
}

// Mirrors watchdog.DELETE_MARKERS (moves / renames count as deletes). Strict: unreadable = delete.
const DELETE_WORDS = ["rm", "rmdir", "unlink", "del", "delete", "remove", "rimraf", "shred", "trash", "purge", "wipe",
  "erase", "truncate", "drop table", "drop database", "drop schema", "git clean", "git reset --hard", "git push --force",
  "git push -f", "git branch -d", "git checkout --", "git restore", "dd if=", "> /dev/null", ">/dev/null", "mv", "move",
  "rename", "move-item", "rename-item", "remove-item", "删除", "清空", "移除", "清理", "移动", "改名", "重命名"]
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
const DELETE_RE = DELETE_WORDS.map((w) => {
  let body = /^[a-z]+$/.test(w) ? (w.endsWith("e") && w.length > 2 ? esc(w.slice(0, -1)) + "(?:e|es|ed|ing)" : esc(w) + "(?:s|es|ed|ing|d)?") : esc(w)
  const head = /^[a-z0-9_]/.test(w) ? "(?<![a-z0-9_])" : ""
  const tail = /[a-z0-9_]$/.test(w) ? "(?![a-z0-9_])" : ""
  return new RegExp(head + body + tail)
})
export function permissionText(req) {
  if (!req || typeof req !== "object") return ""
  const parts = ["permission", "type", "title", "command"].map((k) => String(req[k] ?? ""))
  for (const k of ["patterns", "pattern"]) { const v = req[k]; Array.isArray(v) ? parts.push(...v.map(String)) : parts.push(String(v ?? "")) }
  if (req.metadata && typeof req.metadata === "object") for (const v of Object.values(req.metadata)) if (["string", "number"].includes(typeof v)) parts.push(String(v))
  return parts.filter(Boolean).join("\n")
}
export function permissionClass(req) {
  const t = permissionText(req).toLowerCase()
  if (!t.trim()) return "delete"
  return DELETE_RE.some((re) => re.test(t)) ? "delete" : "other"
}

export function usageOf(tokens) {
  if (!tokens || typeof tokens !== "object") return null
  const n = (v) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0)
  const total = n(tokens.input) + n(tokens.output) + n(tokens.reasoning) + n(tokens.cache?.read) + n(tokens.cache?.write)
  return Math.round(total)
}

