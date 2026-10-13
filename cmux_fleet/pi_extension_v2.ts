/** Redis v2 Pi adapter. agent_settled only reports; it never requests continuation. */
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

type Delivery = {
  run_id: string; slot: string; generation: number; session_id: string;
  message_id: string; delivery_id: string; stream_id: string; fence: number;
  body: { text: string; title: string; mode: string; purpose: string; reply?: { required: string[]; allowed?: string[] } };
};
type Candidate = { tool: string; value: Record<string, unknown> };
type Answer = { status: string; reason?: string };
type Frame = { kind: string; call_id?: string; answer?: Answer; delivery?: Delivery;
  event_id?: string; card?: { render: string }; reason?: string; message_id?: string };

export default function (pi: ExtensionAPI) {
  const command: string[] = JSON.parse(process.env.CMUX_FLEET_PI_BRIDGE || "[]");
  if (!command.length || !command.every((part) => typeof part === "string")) throw new Error("Fleet v2 bridge command missing");
  const slot = process.env.CMUX_FLEET_PI_SLOT || command.at(-2)!;
  const supervisor = slot === "supervisor";
  const service = process.env.CMUX_FLEET_PI_MODE === "service";
  let child: ChildProcessWithoutNullStreams | undefined;
  let context: ExtensionContext | undefined;
  let binding: string | undefined;
  let phase: "startup" | "manual" | "delivery" | "idle" = "startup";
  let ready = false;
  let startupBegan = false;
  let shutdown = false;
  let unhealthy = false;
  let active: Delivery | undefined;
  let candidates: Candidate[] = [];
  let failed = false;
  let lastError = "";
  let startupText = "";
  let sequence = 0;
  const pending = new Map<string, { resolve: (value: Answer) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  const seen = new Set<string>();
  const toolsRunning = new Set<string>();

  function metadata(ctx: ExtensionContext) {
    if (binding && ctx.sessionManager.getSessionId() !== binding) throw new Error("Fleet v2 session binding changed; restart required");
    const usage = ctx.getContextUsage();
    return { thread: ctx.sessionManager.getSessionId(), transcript: ctx.sessionManager.getSessionFile(),
      ctx_used: usage?.tokens ?? null, ctx_window: usage?.contextWindow ?? ctx.model?.contextWindow ?? null };
  }
  function title(ctx: ExtensionContext) {
    const label = ctx.sessionManager.getSessionName();
    if (ctx.mode === "tui" && label) ctx.ui.setTitle(label);
  }
  function fault() {
    unhealthy = true;
    context?.ui.setStatus("fleet", "通信中断，执行状态未确认；等待对账");
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("Fleet bridge unavailable")); }
    pending.clear();
  }
  function send(kind: string, fields: object = {}, ctx = context): Promise<Answer> {
    if (!child || shutdown || !ctx) return Promise.reject(new Error("Fleet bridge is not running"));
    const call_id = String(++sequence);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(call_id); reject(new Error("Fleet command unconfirmed; inspect control before retrying")); }, 35000);
      pending.set(call_id, { resolve, reject, timer });
      child!.stdin.write(JSON.stringify({ ...metadata(ctx), ...fields, kind, call_id }) + "\n", (error) => {
        if (error) { clearTimeout(timer); pending.delete(call_id); reject(error); }
      });
    });
  }
  function entry(eventId: string, render: string) {
    if (seen.has(eventId)) return;
    seen.add(eventId);
    pi.appendEntry("fleet-ui", { event_id: eventId, render });
  }
  function availableAfterNotification(ctx: ExtensionContext) {
    // Run on the next Node turn after the notification handler has returned.
    // No delivery can be offered while Pi is emitting agent_settled.
    setImmediate(() => {
      if (!shutdown && !unhealthy && ready && !active && ctx.isIdle() && !ctx.hasPendingMessages()) {
        send("availability", { idle: true }, ctx).catch(fault);
      }
    });
  }
  function handle(frame: Frame) {
    const ctx = context;
    if (!ctx || shutdown) return;
    if (frame.kind === "reply") {
      const item = pending.get(frame.call_id || "");
      if (item) { clearTimeout(item.timer); pending.delete(frame.call_id!); item.resolve(frame.answer!); }
    } else if (frame.kind === "ui") {
      if (frame.event_id && frame.card) entry(frame.event_id, frame.card.render);
    } else if (frame.kind === "fault") {
      fault();
    } else if (frame.kind === "completed") {
      if (active?.message_id === frame.message_id) { active = undefined; candidates = []; phase = "idle"; ctx.ui.setStatus("fleet", "等待派工"); }
    } else if (frame.kind === "delivery" && frame.delivery) {
      const env = frame.delivery;
      if (unhealthy || active || !ready || phase !== "idle" || !ctx.isIdle() || ctx.hasPendingMessages()) {
        // No steer, followUp, nextTurn or local hidden queue. PEL stays held.
        send("reject_delivery", { reason: "Pi is occupied by a manual/other run" }).catch(fault);
        return;
      }
      if (env.slot !== slot || env.session_id !== binding || env.generation !== Number(command.at(-1))) { fault(); return; }
      active = env; phase = "delivery"; candidates = []; failed = false; lastError = ""; toolsRunning.clear();
      pi.appendEntry("fleet-binding", { message_id: env.message_id, delivery_id: env.delivery_id });
      ctx.ui.setStatus("fleet", "执行派工任务");
      // Only body text enters model context. details are non-model transport metadata.
      // Installed Pi starts this at agent_start, without before_agent_start/input.
      pi.sendMessage({ customType: "fleet-delivery", content: env.body.text, display: true,
        details: { ...env, body: { title: env.body.title } } }, { triggerTurn: true });
    }
  }

  pi.registerEntryRenderer<{ render: string }>("fleet-ui", (entry) => new Text(entry.data?.render || "", 0, 0));
  pi.registerMessageRenderer<{ body: { title: string } }>("fleet-delivery", (message) => new Text(message.details?.body.title || "派工任务", 0, 0));
  const artifact = Type.Object({ path: Type.String(), min_chars: Type.Optional(Type.Integer({ minimum: 1 })), contains: Type.Optional(Type.Array(Type.String())) });
  const dispatch = Type.Object({ to: Type.String(), instruction: Type.String({ minLength: 12 }),
    id: Type.Optional(Type.String()), advise_note: Type.Optional(Type.String()) });
  const decisionSchema = service ? Type.Object({ status: Type.Union([Type.Literal("RUNNING"), Type.Literal("BLOCKED")]),
    reason: Type.Optional(Type.String()), dispatch: Type.Array(dispatch), skip: Type.Optional(Type.Array(Type.Object({ reason: Type.String() }))) }) :
    Type.Object({ status: Type.Union([Type.Literal("RUNNING"), Type.Literal("COMPLETED"), Type.Literal("BLOCKED")]),
      completion: Type.Integer({ minimum: 0, maximum: 100 }), reason: Type.Optional(Type.String()),
      dispatch: Type.Array(dispatch), artifacts: Type.Optional(Type.Array(artifact)),
      advisor: Type.Optional(Type.Union([Type.Literal("on"), Type.Literal("off")])) });
  pi.registerTool({
    name: supervisor ? "fleet_decision" : "fleet_result", label: supervisor ? "派工候选" : "结果候选",
    description: supervisor ? "Submit your final fleet decision for daemon validation. Route only this delivery's request; transport IDs are bound by code." :
      "Submit your final business JSON result for daemon acceptance. Identity is bound by code.",
    parameters: supervisor ? decisionSchema : Type.Object({ result: service ? Type.Record(Type.String(), Type.Any()) : Type.Object({
      done: Type.Array(Type.String(), { minItems: 1 }), evidence: Type.Array(Type.String(), { minItems: 1 }),
      not_done: Type.String(), blocked: Type.String() }) }),
    exposure: "model-only", executionMode: "sequential",
    async execute(_id, params, signal) {
      if (!active || phase !== "delivery" || active.body.purpose === "handoff" || signal?.aborted || unhealthy) throw new Error("No active fleet delivery for this tool");
      const value = supervisor ? params as Record<string, unknown> : (params as { result: Record<string, unknown> }).result;
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Candidate must be a business object");
      if (!supervisor && active.body.reply) {
        const spec = active.body.reply;
        if (spec.required.some((key) => !(key in value)) || spec.allowed?.some(() => Object.keys(value).some((key) => !spec.allowed!.includes(key)))) {
          throw new Error("Business result violates required/allowed reply keys");
        }
      }
      const candidate = { tool: supervisor ? "fleet_decision" : "fleet_result", value };
      candidates.push(candidate); // multiple/conflicting candidates never become success
      pi.appendEntry("fleet-candidate", { message_id: active.message_id, candidate });
      await send("candidate", { candidate });
      return { content: [{ type: "text", text: "候选已记录，等待回合结束与验收。" }], details: { pending: true },
        // Actual Pi only skips automatic follow-up if EVERY completed tool in this
        // batch returns terminate. No claim that this interrupts sibling tools.
        terminate: true };
    },
    renderCall: () => new Text(supervisor ? "提交派工候选" : "提交结果候选", 0, 0),
    renderResult: (result) => new Text((result.details as { pending?: boolean } | undefined)?.pending === true ? "候选已记录，等待验收。" :
      "候选未记录：工具执行或校验失败。详情见 control。", 0, 0),
  });

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.mode !== "tui") throw new Error("Fleet v2 requires the interactive Pi TUI");
    if (child) throw new Error("Fleet v2 cannot replace a bound session without retirement");
    context = ctx; binding = ctx.sessionManager.getSessionId(); title(ctx);
    for (const item of ctx.sessionManager.getBranch()) {
      if (item.type === "custom" && item.customType === "fleet-ui") {
        const data = item.data as { event_id?: string }; if (data?.event_id) seen.add(data.event_id);
      }
    }
    child = spawn(command[0], command.slice(1), { stdio: ["pipe", "pipe", "pipe"] });
    createInterface({ input: child.stdout }).on("line", (line) => {
      try { handle(JSON.parse(line)); } catch { fault(); }
    });
    child.stderr.on("data", () => ctx.ui.setStatus("fleet", "桥接错误；详情见 control"));
    child.on("error", fault); child.on("exit", () => { if (!shutdown) fault(); });
    const ack = await send("init", {}, ctx);
    if (ack.status !== "registered") { fault(); throw new Error(ack.reason || "Fleet binding rejected"); }
  });
  pi.on("input", async (event, ctx) => {
    context = ctx;
    if (!ready && phase === "startup" && !startupBegan) return { action: "continue" };
    if (!ready) { ctx.ui.notify("Fleet 尚未 READY，输入未受理；请先检查启动状态。", "error"); return { action: "handled" }; }
    if (event.source === "extension") return { action: "continue" };
    if (event.text.trim().startsWith("/")) return { action: "continue" };
    if (supervisor) {
      if (event.images?.length) { ctx.ui.notify("Fleet 输入支持文字；请提供图片文件路径。", "warning"); return { action: "handled" }; }
      try {
        const ack = await send("operator_input", { text: event.text, correlation: "ui-" + randomUUID() }, ctx);
        entry("input-" + randomUUID(), event.text); // preserved human text, no model call here
        ctx.ui.setStatus("fleet", ack.status === "accepted" ? "指令已排队" : "指令未确认；详情见 control");
        if (ack.status === "rejected") ctx.ui.notify(ack.reason || "输入被拒绝", "error");
      } catch { ctx.ui.notify("输入未确认；请先查看 control 再决定是否重试。", "error"); }
      return { action: "handled" };
    }
    if (active || !ctx.isIdle()) { ctx.ui.notify("派工执行期间请等待；手工输入未加入该任务。", "warning"); return { action: "handled" }; }
    phase = "manual";
    await send("availability", { idle: false }, ctx);
    return { action: "continue" };
  });
  pi.on("before_agent_start", async (_event, ctx) => {
    context = ctx; title(ctx);
    if (ready) phase = "manual"; // custom delivery never calls this hook
    else { phase = "startup"; startupBegan = true; }
    failed = false; lastError = ""; startupText = ""; toolsRunning.clear();
    if (ready) await send("availability", { idle: false }, ctx);
  });
  pi.on("agent_start", async (_event, ctx) => {
    context = ctx;
    if (phase === "delivery" && active) await send("started", {}, ctx);
  });
  pi.on("tool_execution_start", (event) => { toolsRunning.add(event.toolCallId); });
  pi.on("tool_execution_end", async (event, ctx) => {
    toolsRunning.delete(event.toolCallId);
    if (event.isError && phase === "delivery" && active) {
      // Audit only; this does not add a candidate or a model message.
      await send("candidate", { candidate: { tool: event.toolName, error: event.result } }, ctx);
    }
  });
  pi.on("message_end", async (event, ctx) => {
    if (event.message.role === "assistant") {
      failed = event.message.stopReason === "error" || event.message.stopReason === "aborted";
      lastError = event.message.errorMessage || "";
      if (failed && lastError && phase === "delivery" && active) {
        await send("candidate", { candidate: { tool: "model", error: lastError } }, ctx);
      }
      if (phase === "startup") startupText = event.message.content.filter((p) => p.type === "text").map((p) => p.text).join("\n");
    }
  });
  pi.on("agent_settled", async (event, ctx) => {
    context = ctx; title(ctx);
    // Notification only. No sendMessage, appendEntry or continuation at this boundary.
    if (phase === "startup") {
      const ack = await send("startup", { text: startupText, failed, aborted: event.aborted, error: lastError }, ctx);
      ready = ack.status === "ready"; phase = ready ? "idle" : "startup";
      if (!ready) {
        ctx.ui.setStatus("fleet", "启动失败：尚未 READY；详情见 control");
        ctx.ui.notify(ack.reason || "Fleet 启动未获 READY，禁止派工。", "error");
      }
      availableAfterNotification(ctx);
    } else if (phase === "delivery" && active) {
      const batch = candidates; candidates = []; phase = "idle";
      await send("settled", { candidates: batch, failed, aborted: event.aborted, unfinished: toolsRunning.size > 0 }, ctx);
      availableAfterNotification(ctx);
    } else if (phase === "manual") {
      phase = "idle";
      availableAfterNotification(ctx);
    }
  });
  pi.on("session_shutdown", async () => {
    if (shutdown) return;
    const process = child;
    // Stop is idempotent; stdin EOF also closes Python resources.
    if (process) {
      process.stdin.end(JSON.stringify({ kind: "shutdown", call_id: "shutdown" }) + "\n");
      shutdown = true;
      await new Promise<void>((resolve) => {
        if (process.exitCode !== null) { resolve(); return; }
        const timer = setTimeout(() => { process.kill("SIGTERM"); resolve(); }, 5000);
        process.once("exit", () => { clearTimeout(timer); resolve(); });
      });
    } else shutdown = true;
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("Fleet session shut down")); }
    pending.clear(); child = undefined;
  });
}
