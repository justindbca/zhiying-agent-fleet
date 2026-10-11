---
name: fleet-task-planner
description: "把复杂任务拆成可派发的子任务，并交给 cmux-fleet / Fleet agents 执行。适用于用户要求拆分任务、任务规划、并行派发、交给 fleet agent、像 Claude Code Task/subagent 一样调度执行、或需要 supervisor+workers 协作完成的工作。"
---

# Fleet Task Planner — 拆分任务并派发给 Fleet agents

本 skill 的目标：**主模型只做规划、投递、验收、汇总；具体执行交给 cmux-fleet 的 supervisor/worker agents。**

适用触发：
- “拆分这个任务” / “规划并派发” / “让 fleet agents 做”
- “仿照 Claude Code Task/subagent 机制”
- 大任务需要并行研究、实现、验证、审阅
- 用户显式 `/skill:fleet-task-planner ...`

不适用：
- 单步小改动：直接做，不要起 fleet。
- 交易执行、真盘链路、生产 daemon 重启、生产 Redis 写入等禁区：停止并报告需要人处理。

---

## 0. 总原则

1. **先规划，后派发**：不得边想边投；先写清任务树、worker 分工、产物路径、验收标准。
2. **prompt 必须自包含**：fleet worker 没有当前对话上下文；投递给 fleet 的 `goal` 必须包含背景、约束、禁令、输入、输出格式。
3. **由 fleet 执行**：不要在主会话里替 worker 完成实质任务；主会话只做编排与验收。
4. **文件产物优先**：task 模式结果必须落文件，daemon 只能做字面验收，主会话再读文件做质量验收。
5. **并发保守**：FlashNext / 本地模型用 `--max-busy 2`；默认 workers=2，除非用户明确要求更多。
6. **不碰生产禁区**：涉及 AGENTS.md 禁令时，直接停下报告“需要人做 X”。
7. **报告进 `reports/`**：规划书、角色文件、验收报告都写入 `reports/fleet-task-plans/<run_id>/`。

---

## 1. 前置检查

### 1.1 先查现有流程

在项目根目录执行：

```bash
rg -n "<用户任务关键词>|fleet|cmux-fleet|subagent|Task" memory/ references/ ~/.claude/projects/*us-stock-option*/memory/MEMORY.md .claude/skills .agents/skills 2>/dev/null || true
```

如当前环境有 EverMemOS MCP，先按项目规则查：

```text
search_memory(query="<中文关键词>", user_id="claude-code", retrieve_method="hybrid", top_k=5)
search_memory(query="<English keywords>", user_id="claude-code", retrieve_method="hybrid", top_k=5)
```

### 1.2 检查 fleet 可用性

```bash
FLEET_BIN=""
if command -v cmux-fleet >/dev/null 2>&1; then
  FLEET_BIN="cmux-fleet"
elif [ -x "$HOME/cmux-fleet/.venv/bin/cmux-fleet" ]; then
  FLEET_BIN="$HOME/cmux-fleet/.venv/bin/cmux-fleet"
elif [ -f "$HOME/Documents/projects/cmux-fleet-pi/cmux_fleet/fleet.py" ]; then
  FLEET_BIN="python3 $HOME/Documents/projects/cmux-fleet-pi/cmux_fleet/fleet.py"
fi
printf '%s\n' "$FLEET_BIN"
```

如果为空：报告“本机未找到 cmux-fleet，不能派发”。

检查状态：

```bash
$FLEET_BIN status 2>/dev/null || true
```

---

## 2. 拆分任务

把用户请求改写成一份 WBS：

```markdown
# Fleet Task Plan: <短标题>

## 用户目标
<原始目标的一句话复述>

## 边界与禁令
- 不触碰：<从 AGENTS.md / 用户要求提取>
- 只读/可写范围：<路径>
- 需要用户确认才做：<高风险动作>

## 子任务
| ID | Worker | 目标 | 输入 | 输出文件 | 验收标准 |
|---|---|---|---|---|---|
| T1 | worker-1 | ... | ... | worker-1/T1.md | ≥N 字，包含 ... |
| T2 | worker-2 | ... | ... | worker-2/T2.md | ≥N 字，包含 ... |

## 汇总方式
- supervisor 汇总：<如何整合>
- 主会话验收：读取哪些文件，检查哪些点
```

保存到：

```bash
RUN_ID="$(date +%Y%m%d-%H%M%S)-fleet-plan"
PLAN_DIR="reports/fleet-task-plans/$RUN_ID"
mkdir -p "$PLAN_DIR/roles"
# 将规划写入 "$PLAN_DIR/plan.md"
```

---

## 3. 生成角色文件

为 fleet `--roles` 生成最小角色提示。不要写长篇方法论，只写职责、禁令、输出格式。

`$PLAN_DIR/roles/supervisor.md`：

```markdown
# Your role: Fleet Supervisor

你负责把用户目标拆给 workers，跟踪进度，最后整合。不要自己完成 worker 的实质工作。

硬性规则：
- 只派发计划内任务；遇到禁区或缺输入，返回 BLOCKED。
- 每个 worker 输出必须写到指定文件。
- 最终汇总写到 `supervisor/final.md`。
- 汇总必须列出每个 worker 的文件路径、结论、未解决问题。
```

每个 worker 一份，例如 `$PLAN_DIR/roles/worker-1.md`：

```markdown
# Your role: worker-1

你只负责计划中的 worker-1 子任务。

硬性规则：
- 先阅读任务 goal 中指定的文件；不要假设当前对话上下文。
- 不触碰生产禁区，不运行真盘/下单/重启生产服务命令。
- 结果写到指定输出文件。
- 末尾必须有：结论、证据、风险、下一步。
```

---

## 4. 投递给 fleet

### 4.1 如果已有 fleet 正在跑

优先复用现有 fleet，避免一台机器多套 fleet：

```bash
$FLEET_BIN status
```

若已有 task 正在运行，不要覆盖；报告当前状态并询问是否等待/停止旧任务。

### 4.2 启动 task 模式 fleet（必要时）

默认本机开发 / Pi supervisor 组合：

```bash
$FLEET_BIN start --mode task --workers 2 \
  --supervisor-cli pi \
  --pi-model flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP \
  --pi-thinking off \
  --worker-cli claude --worker-model sonnet \
  --roles "$PLAN_DIR/roles" \
  --allow-unsandboxed-pi \
  --max-busy 2
```

如果该命令因本机版本不支持 `--supervisor-cli pi` 失败，退回已安装 fleet 的默认 profile，但必须在报告中写明退回原因：

```bash
$FLEET_BIN start --mode task --workers 2 --roles "$PLAN_DIR/roles" --max-busy 2
```

### 4.3 构造 task JSON

`$PLAN_DIR/task.json`：

```json
{
  "goal": "<自包含任务说明：背景、输入、禁令、子任务表、输出路径、最终汇总要求>",
  "hours": 2,
  "artifacts": [
    {"path": "worker-1/T1.md", "min_chars": 1000, "contains": ["结论", "证据", "风险"]},
    {"path": "worker-2/T2.md", "min_chars": 1000, "contains": ["结论", "证据", "风险"]},
    {"path": "supervisor/final.md", "min_chars": 1200, "contains": ["汇总", "未解决问题", "文件路径"]}
  ]
}
```

投递：

```bash
$FLEET_BIN task "$PLAN_DIR/task.json"
```

---

## 5. 监控与验收

监控：

```bash
$FLEET_BIN status
```

找到 run 目录：

```bash
python3 - <<'PY'
import json, os
for p in [os.path.expanduser('~/.cmux-fleet/latest.json'), os.path.expanduser('~/.cmux-fleet-pi-validation/latest.json')]:
    if os.path.exists(p):
        d=json.load(open(p)); print(d.get('run') or d.get('run_dir') or d)
PY
```

验收时读取：

```text
<run>/workspace/worker-1/T1.md
<run>/workspace/worker-2/T2.md
<run>/workspace/supervisor/final.md
```

主会话必须做二次验收：
- 文件是否存在、长度是否达标；
- 是否覆盖用户目标；
- 是否违反禁令；
- 是否有编造、未引用证据、未执行检查却声称执行。

验收报告写到：

```bash
$PLAN_DIR/validation.md
```

---

## 6. 最终回复模板

```markdown
已用 Fleet agents 执行。

- 规划书：reports/fleet-task-plans/<run_id>/plan.md
- fleet run：<run_dir>
- worker 产物：
  - worker-1: <path>
  - worker-2: <path>
  - supervisor: <path>
- 主会话验收：PASS / FAIL / BLOCKED

结论：<三到五条要点>

未解决问题 / 需要人确认：
- <如有>
```

---

## 7. 常见错误

- **只口头说已派发但没调用 fleet**：不允许。要么执行 `$FLEET_BIN task`，要么报告无法派发的具体原因。
- **worker prompt 不自包含**：不允许。必须包含背景、路径、输出格式和禁令。
- **让 worker 写生产配置/下单/重启服务**：不允许。worker 只做分析、提案、只读验证。
- **多次 `date +%s` 造成路径不一致**：统一一次性生成 `RUN_ID` / `PLAN_DIR` 后复用。
- **在 macmini 再起一套 fleet**：不允许。macmini 可能已有板块轮动 fleet；先 status，必要时报告等待。
- **把内容质量交给 daemon 验收**：不允许。daemon 只做字面验收，主会话必须二次读文件。
