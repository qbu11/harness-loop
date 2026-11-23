# 三端互调网（AGY ↔ Claude Code ↔ Codex）与动态角色路由手册

> 无论你当前正处于 **Antigravity（AGY）**、**Claude Code（`claude`）** 还是 **Codex（`codex`）**，均可通过 `loopctl.py` 在任意中间环节向另外两端的任意一端**取经评审（只读 `consult`）**、**下达修改与执行任务（`delegate`）**、或**查阅对方的实时会话进度与历史上下文（`inspect`）**，全部记录自动汇总进同一份 `<repo>/.agent-loop/LATEST.md`。

---

## 1. 3×3 互调能力矩阵

| 当前发起方 | 向谁求助/派活 | 获取只读评审/改进意见（`consult`） | 指挥修改与执行（`delegate`） | 查询对方进度与日志（`inspect`） |
|---|---|---|---|---|
| **任意端**（`agy`/`claude`/`codex`） | **→ Antigravity (`agy`)** | `agy -p --mode plan --output-format json [--model <m>] [--conversation <id>] "<问题>"` | `agy -p --mode accept-edits --output-format json [--model <m>] [--conversation <id>] "<任务>"` | `loopctl.py inspect --harness agy --agy-id <id> --last 6 --json` |
| **任意端**（`agy`/`claude`/`codex`） | **→ Claude Code (`claude`)** | `claude -p --permission-mode plan --output-format json [--model <m>] [--resume <sid>] "<问题>"` | `claude --bg -n "<名>" [--model <m>] "<任务>"`（复杂需求自动加 `ultracode`） | `loopctl.py inspect --harness claude --repo "<repo>" --tail-turns 5 --json` |
| **任意端**（`agy`/`claude`/`codex`） | **→ Codex (`codex`)** | `codex exec -C "<repo>" -s read-only --json -o <reply.md> [-m <m>] "<问题>"` | `codex exec -C "<repo>" -s workspace-write --json -o <last.md> [-m <m>] "<任务>"` | `loopctl.py inspect --harness codex --repo "<repo>" --tail-turns 5 --json` |

> **不要手拼参数**：统一调用 `loopctl.py consult` 与 `loopctl.py delegate`，脚本会自动：
> 1. 根据 `manifest.roster` 解析显式指定的模型（或省略参数以继承目标 Harness 全局默认）；
> 2. 注入 `AGY_LOOP_DEPTH` 防无限互相嵌套（默认限制深度 `< 2`）；
> 3. 检查同仓库活跃写任务（避免两个执行端同时改同一份代码）；
> 4. 将去程提问/任务与回程意见/交付正文全部写入 `<repo>/.agent-loop/LATEST.md` 与 `events.jsonl`。

---

## 2. 本机模型别名与继承规则

> **核心纪律**：只要用户没有在本轮明确要求换模型，调用另外两个 Harness 时**不传 `--model` / `-m`**，一律沿用目标 Harness 自己的全局默认配置；也**禁止跨 Harness 传递认证或 Provider 环境变量**。

当用户明确点名角色模型（例如「这次用 Codex 的 **GPT-6.2-Sol** / **Codex GPT 6.2 Sol** 跑开发」）时，可通过 `init` 或 `set-roster` 绑定，`loopctl.py` 会自动把常见简称规范化为真实 CLI 模型 slug：

| Harness | 用户常说 | 真实 CLI 模型 slug（已核实） | 常见分工建议 |
|---|---|---|---|
| **Codex** | `GPT-6.2-Sol` / `Codex GPT 6.2 Sol` / `6.2-sol` | **`gpt-6.2-sol`** | 主力开发执行（`executor`）、复杂 bug 根治（需 Codex CLI `>= 0.171.0` 且账号已开放） |
| **Codex** | `GPT 6.2 Astra` / `6.2-astra` | `gpt-6.2-astra` | 高推理深度审查/设计 |
| **Codex** | `GPT-6.1-Sol` / `Codex GPT 6.1 Sol` / `gpt-6.1-sol` | `gpt-6.1-sol` | 备选 Sol 开发执行 |
| **Codex** | `GPT-6-Astra` / `GPT-6-Sol` | `gpt-6-astra` / `gpt-6-sol` | 常用全局默认 / 通用执行 |
| **Codex** | `Codex Auto Review` | `codex-auto-review` | 独立代码审校（`verifier` / `advisor`） |
| **Antigravity** | `Gemini 4 Neon XHigh` / `Orchestrator Neon` | `gemini-4-neon-xhigh` / `gemini-pro-orchestrator-neon` | 架构规划、Spec 编写、独立终审 |
| **Antigravity** | `Claude Opus 5.5 High` | `claude-opus-5-5-high` | 交互审查、文档撰稿 |
| **Claude Code** | `opus` / `sonnet` | 透传 `claude` 原生别名 | `ultracode` 多阶段施工或架构评审 |

> **实测防踩坑提示**：若显式指定的模型在当前账号/CLI 版本不可用（例如旧版 Codex CLI 返回 `400 model_not_found`），`loopctl.py` 会在非零退出时报错并拒绝绑定失败会话，**绝不静默偷换模型**；升级客户端或经用户确认改派模型（或去掉 `--model` 继承全局默认）后再重跑。

### 动态调整角色花名册示例
```bash
# 开局指定：AGY 当架构师+终审，Codex (GPT-6.2-Sol) 当执行官
python3 "$HOME/.agents/skills/agy-claude-loop/scripts/loopctl.py" init \
  --repo "<repo>" --slug "<slug>" --agy-id "<agy-id>" \
  --architect agy --executor "codex:GPT-6.2-Sol" --verifier agy --json

# 中途把终审改派给 Codex Auto Review，或加设 Claude 为顾问
python3 "$HOME/.agents/skills/agy-claude-loop/scripts/loopctl.py" set-roster \
  --repo "<repo>" --verifier "codex:codex-auto-review" --advisor claude --json
```

---

## 3. 中间环节跨端求助与指挥示例

### 场景 A：Codex（如 `gpt-6.2-sol`）执行到一半拿不准架构/产品口径 → 向 AGY 只读咨询
```bash
python3 "$HOME/.agents/skills/agy-claude-loop/scripts/loopctl.py" consult \
  --repo "<repo>" --from codex --to agy \
  --summary "核对接口返回结构是否符合 PRD" \
  --prompt "请只读检查 docs/PRD-xxx.md 与我当前 git diff，确认分页字段命名是否需要保留旧字段兼容。" --json
```

### 场景 B：Claude Code 或 AGY 想在提交前让 GPT-6.2-Sol / Codex Auto Review 做一次对抗性审校
```bash
python3 "$HOME/.agents/skills/agy-claude-loop/scripts/loopctl.py" consult \
  --repo "<repo>" --from claude --to codex --model "Codex GPT 6.2 Sol" \
  --summary "请 GPT-6.2-Sol 审校未提交 diff" \
  --prompt "只读审阅当前工作区 git diff 与主要风险点，给出按文件:行号排序的问题清单，不要修改文件。" --json
```

### 场景 C：AGY 终审 `NEEDS_FIX` 后指挥 Codex（`gpt-6.2-sol`）按返工单修代码
```bash
python3 "$HOME/.agents/skills/agy-claude-loop/scripts/loopctl.py" delegate \
  --repo "<repo>" --from agy --to codex --role-name executor --model "GPT-6.2-Sol" \
  --prompt-file "<repo>/.agent-loop/<run-id>/ROUND-1-PROMPT.md" \
  --summary "第 1 轮返工交由 Codex GPT-6.2-Sol 修复" --background --json
```
随后使用零 Token 本地状态监测等待交卷并自动同步回台账（依赖 AGY 原生后台任务回调精准唤醒当前会话，零全局 Hook 污染）：
```bash
python3 -B "$HOME/.agents/skills/agy-claude-loop/scripts/loopctl.py" watch \
  --repo "<repo>" --harness codex --stall-timeout 600 --timeout 7200 --json
```
监测脚本四合一判据：**终态事件**（Codex `task_complete`/`turn_aborted`，Claude `end_turn`/`StopFailure`）+ **进程与官方状态**（PID 存活、`ps -o stat=` 识别僵尸进程、`claude agents --all --json`）+ **活跃度跟踪**（日志增长、Git 变更、子进程运行）+ **长静默卡死阈值**（`--stall-timeout`，默认 600s；彻底静默卡死以 exit `2` (`STALLED`) 唤醒 AGY 介入，异常崩溃以 exit `1` (`PROCESS_EXITED_FAILED`) 返回尾部诊断）。

### 实测工程注意事项与终端直达回看
- **Codex `workspace-write` `.git/index.lock` 沙箱边界**：在 macOS 上以 `-s workspace-write` 派发 Codex 时，`.git/` 受写保护（`git add` 建 `.git/index.lock` 会被拒绝）。在提示词中告知执行端「若 `.git` 受限，则保留通过测试的工作区改动并如实汇报」，切勿引导绕过沙箱；需要由 Codex 自动 commit 时改用 `--sandbox danger-full-access`。
- **终端一行直达双端对话**：
  - Codex 原生 TUI 续接必须带目录切换：`cd "<repo>" && codex resume <session_id>`（否则按 CWD 过滤会找不到会话）。
  - Antigravity Desktop 会话（`~/.gemini/antigravity/brain/`）与 `agy` CLI 会话（`~/.gemini/antigravity-cli/brain/`）分库存储，`agy --conversation <id>` 仅认 CLI 库；在终端统一回看推荐 `python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py view --repo "<repo>"`（或 `--agy-id <id>`）。

---

## 4. 开源社区与本机已有复用清单（不重复造轮子）

1. **已直接复用的本机能力**：
   - **Codex 复杂并行任务**：当 Codex 作为执行官需要并行拆解多模块/多文件流水线时，直接调用本机已安装的 [`codex-workflows`](~/.agents/skills/codex-workflows/SKILL.md)（App Server JavaScript Workflow Runner + `workspace` / `parallel` / `pipeline`），不在 `loopctl.py` 内重写第二套 Codex 调度器。
   - **Claude Code 复杂多阶段任务**：直接激活 Claude Code 原生 **`ultracode`** 关键字钩子与内置 `Workflow` 工具。
   - **Antigravity 浏览器体验与定时巡检**：直接复用 AGY 原生 `browser` 子代理与 `schedule` 工具。
2. **开源社区调研评估结论**：
   - **`openai/codex-plugin-cc`（⭐ 35.3k）**：OpenAI 官方 Claude Code 插件（`/codex:review`、`/codex:adversarial-review`、`/codex:rescue`）。若用户在 Claude Code 内想一键调起 Codex 对抗评审可随时安装作为快捷入口，但它仅覆盖 Claude → Codex 单向，仍需由 `agy-claude-loop` 承担 AGY ↔ CC ↔ Codex 三端总编排与统一可追溯台账。
   - **`openclaw/acpx`（⭐ 3.4k）**：ACP 无头多代理客户端。因其 `acpx antigravity` 需额外配置 `agy_acp_server.par` OAuth 且 `acpx claude` 会屏蔽用户全局设置并把日志隔离在 `~/.acpx/sessions/`，本链路继续优先直接调用三端本机原生 CLI，保留完整的本地会话可读性与 `claude attach` / `codex resume` 接管体验；仅当未来要接入 30+ 第三方 ACP 代理时再引入 `acpx` 作为可选适配器。
