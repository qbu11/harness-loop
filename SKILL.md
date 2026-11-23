---
name: agy-claude-loop
description: Orchestrates the closed-loop Architect-Executor-PostVerifier workflow across Antigravity (agy), Claude Code, and Codex (including GPT-6.2-Sol / gpt-6.2-sol) with full bidirectional session traceability. Activate in any of the three harnesses when running dynamic role assignments, authoring or executing a PRD/Tech-Spec/Test-Plan/Handoff doc pack, consulting another harness mid-flight for review or improvement ideas, delegating implementation or rework across harnesses, monitoring sessions, running post-verifier audits, or doing live UX walkthroughs.
metadata:
  short-description: Traceable multi-harness loop across AGY, Claude Code, and Codex
---

# 三端动态闭环协作与可追溯工作流（Antigravity ↔ Claude Code ↔ Codex）

本 Skill 供 **Antigravity（`agy`）**、**Claude Code（`claude`）** 与 **Codex（`codex`，含 `GPT-6.2-Sol` / `gpt-6.2-sol`）** 三端共同调用，支持**按用户指定的角色花名册动态分工**，并让任意一个 Harness 在**任意中间环节**都能向其他端只读取经（`consult`）、指挥修改与执行（`delegate`）、或查阅实时进度（`inspect`）。

> **全链路透明可见保障**：每次闭环都在目标仓库生成 Git 自动忽略的共享对话台账 `<repo>/.agent-loop/LATEST.md`（实际存放在 `<repo>/.agent-loop/<run-id>/DIALOGUE.md`），汇总三端角色分工、全部互调提示词、执行交卷与终审裁决，并保留三端原生接管命令（`conversation://`、`claude attach` / `--resume`、`codex resume`）。

---

## 1. 动态角色分工与六阶段闭环

用户在启动任务时会说明各 Harness 与模型的分工（如「AGY 做架构与终审，Codex 的 GPT-6.2-Sol 做执行」或「Claude Code 用 `ultracode` 执行，中间请 Codex 审代码」）。未额外指定时沿用默认配置（`architect=agy`、`executor=claude`、`verifier=agy`），且未点名模型时一律**继承目标 Harness 全局默认**。

```text
[Phase 1: Architect 项目与用户视角审查]
   └─ 核对代码现状 + 目标用户爽点/留存卡点 + 数据/架构硬伤 → 写入 DIALOGUE.md
        ▼
[Phase 2: Architect 撰写文档包 & 派发给 Executor]
   └─ 产出 docs/{AUDIT,PRD,TECH-SPEC,TEST-PLAN,HANDOFF}-<slug>.md（不越界改业务代码）
   └─ 通过 delegate 派发给指定 Executor（Claude Code 复杂需求自动用 ultracode；Codex 用 gpt-6.2-sol）
        ▼
[Phase 3: 进度监测 & 任意中间环节跨端互调]
   └─ inspect --harness <agy|claude|codex> 追踪进度，--sync-ledger 同步交卷正文
   └─ 任意阶段均可用 consult（只读取经/评审）或 delegate（跨端派修）互通意见
        ▼
[Phase 4: Post-Verifier 独立终审]
   └─ audit_commits.py 确定性检查 + 逐文件 Diff 深审 + 独立复跑快速门禁 + 逐张读图
   ├─ 发现隐患 (NEEDS_FIX, round < 2) ─▶ 生成 FILE:LINE 返工单 ─▶ delegate / send-cc 打回返工 ─▶ 回 Phase 3
   └─ 审查通过 (PASS) ──────────────────▶ 进入 Phase 5
        ▼
[Phase 5: 真实用户视角全流程体验]
   └─ 委派 AGY browser 子代理按目标用户剧本走通实机链路，记录爽感、犹豫点与改进建议
        ▼
[Phase 6: 结项汇报与询问下一步]
   └─ 输出门禁实测表 + 用户体验总结 + 三端互聊 Session 追溯链接 → 询问用户下一步指令
```

---

## 2. 工具脚本速查（零外部依赖 Python 3）

脚本所在目录：`"$HOME/.agents/skills/agy-claude-loop/scripts"`；跨端互调详细命令与模型别名对照见 [references/cross-harness-mesh.md](references/cross-harness-mesh.md)。

| 脚本与子命令 | 用途 | 核心参数 |
|---|---|---|
| `loopctl.py init` | 初始化闭环、角色花名册与自忽略 `.agent-loop/` | `--repo <dir> --slug <slug> [--complexity complex\|simple] [--architect agy] [--executor claude\|codex:gpt-6.2-sol] [--verifier agy] [--agy-id <uuid>] [--base-commit <sha>] --json` |
| `loopctl.py set-roster` | 运行中途动态调整各角色归属与模型 | `--repo <dir> [--architect ...] [--executor ...] [--verifier ...] [--advisor ...] --json` |
| `loopctl.py bind-session` | 绑定任意 Harness（`agy`/`claude`/`codex`）会话到台账 | `--repo <dir> --harness <agy\|claude\|codex> [--session-id <id>] [--model <m>] --json` |
| `loopctl.py consult` | **任意端 → 任意端只读咨询/评审**（写进 `DIALOGUE.md`） | `--repo <dir> --from <h> --to <h> [--model <m>] [--session-id <id>] (--prompt <txt> \| --prompt-file <p>) [--dry-run] --json` |
| `loopctl.py delegate` | **任意端 → 任意端派发改码/执行任务**（带单写者与深度护栏） | `--repo <dir> --from <h> --to <h> [--role-name executor] [--model <m>] [--ultracode \| --no-ultracode] [--background \| --sync] (--prompt <txt> \| --prompt-file <p>) [--dry-run] --json` |
| `loopctl.py inspect` | **纯只读**统一进度路由（支持 `--sync-ledger` 同步交卷） | `--harness <agy\|claude\|codex> --repo <dir> [--session-id <id> \| --agy-id <uuid>] [--sync-ledger] --json` |
| `loopctl.py watch` | **零 Token 四合一状态监测**（终态事件+PID/僵尸进程+活跃度+卡死阈值） | `--repo <dir> --harness <codex\|claude> [--pid <n>] [--since-epoch <ts>] [--stall-timeout 600] [--timeout 7200] --json` |
| `loopctl.py view` | 终端一行命令可读回放 Codex / AGY 对话与台账 | `--repo <dir> \| --agy-id <uuid> [--target all\|codex\|agy\|dialogue] [--last 10]` |
| `loopctl.py record` | 手工写入结构化对话/交卷/审查事件 | `--repo <dir> --role <agy\|claude\|codex\|user> --event <type> --summary <txt> [--body <txt> \| --body-file <p>] [--verdict PASS\|NEEDS_FIX] [--commits <csv>]` |
| `loopctl.py status` | 查看当前花名册、阶段状态与三端接管命令 | `--repo <dir> [--run-id <id>] --json` |
| `loopctl.py snippet` | 输出或幂等追加项目 `AGENTS.md` 三行角色约定 | `[--append <repo>] [--json]` |
| `loopctl.py doctor` | **纯只读**检查三端 CLI、软链挂载与 AGY 会话目录 | `[--json]` |
| `loopctl.py dispatch-cc` / `send-cc` / `bind-cc` | 保留的 Claude Code 专用快捷命令 | `--repo <dir> ... --json` |
| `audit_commits.py` | **纯只读** 7 项工程纪律核查（洁净区/CUI-First/无AI署名/禁区/截图/报告新鲜度） | `--repo <dir> --base <sha> [--head HEAD] [--skip-worktree-check] --json` |

---

## 3. 核心互通与安全纪律（三端通用）

1. **只读咨询 vs 写入派发分离**：
   - 想在任何中间步骤问别的模型「这个设计/这段 diff 有没有隐患？怎么改更好？」→ 调 **`loopctl.py consult`**（强制使用 AGY `--mode plan`、Claude `--permission-mode plan`、Codex `-s read-only`），绝不改文件；
   - 想让另一个 Harness 动手写代码、改 bug、跑重型验收 → 调 **`loopctl.py delegate`**（受同仓库单写者检查与 `AGY_LOOP_DEPTH < 2` 防递归护栏保护）。
2. **模型继承与显式覆盖规则**：
   - 未显式指定模型时，**绝不传 `--model` / `-m`**，一律沿用目标 Harness 自己的全局默认配置，且**严禁跨 Harness 串用认证/Provider 环境变量**；
   - 用户明确指定某角色用特定模型（如 **`GPT-6.2-Sol`** / **`Codex GPT 6.2 Sol`** → `gpt-6.2-sol`）时，写入 `roster` 或通过 `--model` 传入；常见别名对照见 [references/cross-harness-mesh.md](references/cross-harness-mesh.md#2-本机模型别名与继承规则)。
3. **复杂任务优先复用各端原生强编排**：
   - **Claude Code**：复杂需求默认在提示词首行保留精确连写关键字 **`ultracode`**，驱动原生 `Workflow` 并行与 `P0* → GUI-* → FINAL` 严格串行阶段；
   - **Codex**：单任务用 `codex exec -m gpt-6.2-sol`；若存在可拆分的独立多模块并行流，直接复用本机已安装的 [`codex-workflows`](~/.agents/skills/codex-workflows/SKILL.md)（或官方 workflow），不另起轮子。
4. **仓库与文件安全红线**：
   - 严禁使用 `rm`（弃置文件用 `mv <path> ~/.Trash/`）；禁止触碰 `AGENTS.md` / `HANDOFF` 划定的保护目录；提交用 `rtk git`（若项目约定）且不加 AI 署名、不未经授权 `git push`；
   - 当 `cmux` 在外部返回 `Access denied` 时属正常隔离策略，直接走 `loopctl.py` 读写原生 CLI 与会话日志。

---

## 4. 按当前担任的角色执行

### 角色 A：当你担任 **Architect**（默认 Antigravity，或用户指定的任意端）
1. **Phase 1 审查**：核对仓库现状、目标用户爽点与架构/数据缺口；运行 `loopctl.py init`（带入 `--architect` / `--executor` / `--verifier` 与 `--agy-id`）并用 `loopctl.py record --event audit_summary` 写清结论。必要时可用 `loopctl.py consult` 先拉取另一端的第二意见。
2. **Phase 2 撰稿与派发**：按 [references/doc-pack-templates.md](references/doc-pack-templates.md) 产出 `docs/{AUDIT,PRD,TECH-SPEC,TEST-PLAN,HANDOFF}-<slug>.md`（**Phase 1–2 不越界代写业务代码**）。随后根据 `roster.executor` 派发：
   - 若交由后台自动执行：运行 `loopctl.py delegate --from <self> --to <executor_harness> --role-name executor --prompt-file <提示词> --background --json`；
   - 若交由用户手动粘贴到终端会话：输出 `HANDOFF` 启动提示词（记入 `record --event delegate_task`）。
3. **Phase 3 零 Token 守候与巡检**：
   - 后台派活（`delegate --background` 或 `dispatch-cc`）后，优先挂载零 LLM 等待成本的本地监测脚本 `loopctl.py watch --repo <repo> --harness <codex|claude> --json`：综合终态事件（`task_complete` / `end_turn` / `StopFailure` / `turn_aborted`）、进程/官方状态、活跃度变更（日志/Git/子进程）与可配置卡死超时，**任务一结束立刻同步进 `DIALOGUE.md` 并借助 AGY 原生后台任务回调精准唤醒当前 AGY 对话**，无需全局 Hook 侵入、也不消耗轮询 Token。
   - 如需人为介入查看实时切片或超长多小时兜底，可配合 `loopctl.py inspect --harness <executor_harness> --repo <repo> --json` 与 [references/verifier-and-ux-playbook.md](references/verifier-and-ux-playbook.md) §1。

### 角色 B：当你担任 **Executor**（Claude Code、Codex `gpt-6.2-sol` 或 AGY）
1. **开工建联**：运行 `loopctl.py status --repo . --json` 与 `loopctl.py bind-session --repo . --harness <self> --json`；若需回顾上游讨论背景，执行 `loopctl.py inspect --harness agy --repo . --last 5 --json`。
2. **CUI-First 分阶段施工**：先全绿完成并提交全部纯后端/CLI 阶段（`P0*`），再修改前端源码；遇到架构/需求疑点或高风险改动时，可随时调 `loopctl.py consult --from <self> --to <architect|advisor>` 获取另一端的只读改进建议。
3. **交卷自检与回写**：收口前运行 `audit_commits.py --repo . --base "<base_commit>" --json` 确认 `failed == 0` 且工作区干净，再执行：
   ```bash
   python3 "$HOME/.agents/skills/agy-claude-loop/scripts/loopctl.py" record \
     --repo . --role <claude|codex|agy> --event delivery \
     --summary "<一句话总结交付与门禁实测>" --commits "<hash1..hashN>" \
     --body "<阶段Commit对照 + 各层实测数字 + 与Spec细微差异>" --json
   ```

### 角色 C：当你担任 **Post-Verifier**（默认 AGY，或用户指定的任意端）与 **真实用户体验官**
1. **Phase 4 独立终审**：严格按 [references/verifier-and-ux-playbook.md](references/verifier-and-ux-playbook.md) §2 执行四维核查（`audit_commits.py` + 逐文件 Diff 猎杀隐患 + 独立复跑快速校验 + 逐张读图），需要多模型交叉复核时可加调 `loopctl.py consult`。
   - **`NEEDS_FIX`**：记入 `verifier_audit`，生成精确到 `文件:行号` 的返工单，调用 `loopctl.py delegate`（或 `send-cc`）打回原执行端续修（最多自动返工 2 轮，满 2 轮升级用户决策）。
   - **`PASS`**：记入 `verifier_audit`，进入 Phase 5。
2. **Phase 5 & 6 真实用户体验 + 结项询问**：按 [references/verifier-and-ux-playbook.md](references/verifier-and-ux-playbook.md) §3 切换真实用户画像体验实机流程并沉淀建议，运行 `record --event ux_walkthrough` 与 `--event signoff`，输出门禁表、体验报告与下述追溯清单，最后主动询问用户下一步。

---

## 5. 三端互聊 Session 必附追溯清单与终端直达命令

每次交接、跨端咨询、进度汇报或终审放行，回复用户时末尾必须附上实时可查入口：
- **共享闭环台账**：`[LATEST.md](file://<repo>/.agent-loop/LATEST.md)`（汇总角色花名册、每一轮三端互调记录与 SHA-256 哈希）
- **一行命令同时回看 Codex + AGY 对话**：`python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py view --repo "<repo>"`
- **Antigravity 会话**：
  - Desktop 会话（`~/.gemini/antigravity/brain/<agy-id>/`）与 `agy` CLI 会话（`~/.gemini/antigravity-cli/brain/<agy-id>/`）分库存储；Desktop 会话在终端可用 `python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py view --agy-id <agy-id> --last 10` 查看，IDE 内点击 `conversation://<agy-id>`；仅 CLI 会话额外支持 `agy --conversation <agy-id>`。
- **Claude Code 会话**：`claude attach <bg-id>` / `claude logs <bg-id>` / `claude --resume <cc-session-id>`
- **Codex 会话**：`cd "<repo>" && codex resume <codex-session-id>`（日志：`~/.codex/sessions/**/rollout-*-<codex-session-id>.jsonl`）

---

## 6. 新项目接入模板（`AGENTS.md` 各端一行角色声明）

在新仓库开局时运行 `python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py snippet --append <repo>`（或手动写入 `AGENTS.md`），让三端 Agent 打开仓库即可读懂各自默认职责：

```markdown
## 多 Harness 协作角色（默认口径，均调用 `agy-claude-loop`）

- **Antigravity（`agy`）**：默认任 **Architect + Post-Verifier + 体验官**——负责项目审查、撰写 PRD/Tech-Spec/Test-Plan/Handoff 规格包，用 `loopctl.py delegate` 派活并挂 `loopctl.py watch` 零 Token 守候，完工后独立四维终审、实机体验并向用户结项。
- **Claude Code（`claude`）**：默认任 **Executor**（复杂需求默认首行 `ultracode`）——严格按规格包 CUI-First 分阶段施工与提交，遇口径疑问可 `loopctl.py consult` 取经，收口前自跑 `audit_commits.py` 并回写 `.agent-loop/LATEST.md`。
- **Codex（`codex`，如 `GPT-6.2-Sol`）**：按用户指定任 **Executor / 审校顾问**——承担开发实现或对抗性代码审校，保持原生会话可追溯（`codex resume`），进度与交付同步至 `.agent-loop/LATEST.md`。
```
