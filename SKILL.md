---
name: harness-loop
description: Orchestrates multi-harness AI coding workflows across Antigravity (agy), Claude Code, Codex (incl. GPT-6.2-Sol), and extensible CLI/ACP harnesses with tmux live dashboards, isolated git worktree waves, zero-token monitoring, and bidirectional session traceability. Activate when planning doc packs, running parallel worktrees in tmux, consulting or delegating across harnesses, viewing logs, or auditing multi-milestone CUI-first deliveries.
metadata:
  short-description: Multi-harness tmux loop, worktree waves, and traceable audit
---

# 多 Harness 动态闭环与 Tmux 并行编排工作流（`harness-loop`）

本 Skill 用于协调多个 AI 编程 Harness 协同开发——内置 **Antigravity（`agy`）**、**Claude Code（`claude`）** 与 **Codex（`codex`，含 `gpt-6.2-sol`）**，角色花名册、共享台账 `.agent-loop/` 与 `tmux` 编排均与具体 Harness 解耦，可平顺扩展更多 CLI/ACP Harness。

> **两条核心保障（外加本分支额度路由）**：
> 1. **全程 Tmux 可视化**：派发与咨询会话默认自动运行在 `hl-<slug>` `tmux` 会话中（Window 0 为总览看板，每个工作流占独立命名窗），随时 `tmux attach` 或 `loopctl.py tmux tail` 查进度；
> 2. **全链路可追溯台账**：每次闭环都在目标仓库生成 Git 自动忽略的 `<repo>/.agent-loop/LATEST.md`，留存角色/赛道分配、提示词、交付实测、终审裁决与全端原生恢复命令；
> 3. **按额度选端与省主机 Token**：可用 `loopctl.py quota`（复用 `quota-axi` / `codexbar` / `cclimits` + 只读 CC Switch 本地保护线）配合 `--conserve <h>` 与 `--prefer <h>`，优先烧快到期/最富裕额度并在需要时开启 `--brief` + 异构交叉审校，详见 [references/quota-routing.md](references/quota-routing.md)。

---

## 1. 六阶段闭环与多里程碑并行模型

```text
[Phase 1: Architect 审查]   ──▶ init（设花名册/赛道）+ record --event audit_summary
[Phase 2: 规格包 + 派发]    ──▶ 产出 PRD / FRONTEND-DESIGN / TECH-SPEC / TEST-PLAN / HANDOFF
                            ──▶ 按 Wave + 独立 Worktree 调 delegate 并行启动进 tmux
[Phase 3: 进度与零Token守候]──▶ tmux attach / tmux status / tmux tail 实时看；watch --wave 零 Token 守候
                            ──▶ 任意端中途疑问用 consult（只读）或 inspect/view 互通
[Phase 4: Post-Verifier]    ──▶ audit_commits.py --stage <cui|full> + 读 Diff + 复跑快检 + 逐张读图
                            ├─ NEEDS_FIX（round < max_rounds，默认 2）▶ delegate --rework 回 Phase 3
                            └─ PASS ──────────────▶ 进入 Phase 5
[Phase 5: 交付实机体验]     ──▶ 跑活体健康检查（如 dev-status.sh）→ 扮演真实用户走通核心链路
[Phase 6: 结项或升级停机]   ──▶ signoff（DONE）| escalate（ESCALATED）| abort（ABORTED）
```

---

## 2. 闭环结束与升级停机条件（Exit Criteria）

闭环只有 **3 种合法终态**；`loopctl.py` 用硬门禁拦住超轮次自动返工与未达标强行结项：

| 终态 (`phase`) | 触发方式 | 必须同时满足的条件 | 后续动作 |
|---|---|---|---|
| **`DONE`**（正常完工） | `record --event signoff` | ① 全波 `jobs` 无活跃/未闭环失败项、worktree 已合并；② `audit_commits.py --stage full` 返回 `0 failed`；③ 已记录 `verifier_audit --verdict PASS`；④ 已记 `ux_walkthrough`（纯脚本/无界面任务显式传 `--skip-ux`） | 输出门禁实测表与恢复命令；默认保留本地提交（未授权不 `git push`）与 `tmux` 供回看，确认清理时跑 `tmux stop --all` |
| **`ESCALATED`**（异常升级） | `record --event escalate` 或返工超限自动触发 | 命中任一停机红线：① `delegate --rework` / `send-cc` 达 `max_rounds`（默认 `2`）仍 `NEEDS_FIX`；② 同一错误跨轮复现；③ `watch` 报 `STALLED`（600s）或 `TIMEOUT`（7200s）；④ 缺凭据/沙箱/第三方硬阻塞；⑤ 需求冲突或修补须破契约 | 立即停止自动派活，列出阻塞证据与 2–3 个选项交用户拍板；用户确认后加 `--force` 放行 |
| **`ABORTED`**（用户中止） | `record --event abort` | 用户明确取消本轮目标或要求止损 | 按需保留或归档 worktree，停掉相关窗口后收口 |

---

## 3. `loopctl.py` 与 `audit_commits.py` 速查

脚本目录：`"$HOME/.agents/skills/harness-loop/scripts"`

| 命令 | 用途 | 核心参数 |
|---|---|---|
| `loopctl.py init` | 初始化闭环、花名册与赛道 | `--repo <dir> --slug <slug> [--conserve agy] [--prefer codex] [--quota-strategy expiring_first\|abundant_first\|auto\|manual] [--auto-roster] [--brief] [--max-rounds 2] --json` |
| `loopctl.py quota` | **探测各端额度、快照与选端推荐** | `[show \| set] [--repo <dir>] [--probe auto\|quota-axi\|codexbar\|cclimits\|local] [--conserve csv] [--prefer csv] [--strategy mode] [--apply] [--brief] --json` |
| `loopctl.py set-roster` | 运行中动态改角色/赛道/模型/策略 | `--repo <dir> [--executor ...] [--track k=h[:m]] [--conserve csv] [--prefer csv] [--auto-roster] --json` |
| `loopctl.py delegate` | **派发写任务（默认进 tmux 窗）** | `--repo <dir> [--workdir <wt>] [--wave w] [--track t] [--label lbl] [--to h] [--model m] [--rework] [--force] [--tmux\|--no-tmux] --json` |
| `loopctl.py consult` | **只读跨端评审（默认进 tmux 窗）** | `--repo <dir> [--workdir <wt>] --from <h> --to <h> (--prompt\|--prompt-file) [--no-tmux] --json` |
| `loopctl.py watch` | **零 Token 守候单任务/整波** | `--repo <dir> (--job-id id \| --wave w \| --jobs id1,id2 \| --all-active \| --harness h) [--stall-timeout 600] [--brief] --json` |
| `loopctl.py tmux` | 看板、接入、抓屏、停止 | `status \| dashboard \| attach [--job-id id] \| tail [--job-id id] [--lines 40] \| stop (--job-id id\|--all)` |
| `loopctl.py view` | **纯只读回放任意端会话全文** | `[--repo dir] [--target all\|dialogue\|claude\|codex\|agy] [--claude-id id] [--codex-id id] [--agy-id id] [--job-id id] [--last 10] [--full]` |
| `loopctl.py inspect` | 结构化只读切片 + 同步交卷 | `--harness <agy\|claude\|codex> --repo <dir> [--workdir <wt>] [--job-id id] [--sync-ledger] --json` |
| `loopctl.py record` | 写入事件、终审与停机终态 | `--repo <dir> --role <r> --event <type> --summary <txt> [--verdict PASS\|NEEDS_FIX] [--skip-ux] [--force] [--commits csv]` |
| `loopctl.py snippet` | 追加项目 `AGENTS.md` 三行分工 | `[--append <repo>] [--json]` |
| `loopctl.py doctor` | 自检各 CLI、`tmux` 与软链 | `[--json]` |
| `audit_commits.py` | 7 项工程纪律与多里程碑审计 | `--repo <dir> --base <sha> [--stage full\|cui] [--skip-worktree-check] [--brief] --json` |

---

## 4. 通用纪律与防踩坑规则

1. **只读咨询 vs 写入派发分离**：拿不准架构/接口时调用 `consult`（强制只读）；写代码用 `delegate`（带同 `workdir` 防双写锁 + `HARNESS_LOOP_DEPTH < 2` 防递归）。
2. **默认继承目标 Harness 全局模型**：未显式指定模型时不传 `--model`/`-m`，且绝不跨端传递认证或 Provider 环境变量。详细模型别名与多赛道用法见 [references/cross-harness-mesh.md](references/cross-harness-mesh.md)。
3. **复杂任务优先原生强编排**：Claude Code 多阶段任务保留首行 `ultracode`；Codex 多模块流结合 `codex-workflows`；注意 Codex macOS `workspace-write` 对 `.git/index.lock` 只读与禁绑本地端口限制。
4. **文档包、并行提示词与验收**：
   - 文档包骨架与三套执行官提示词模板见 [references/doc-pack-templates.md](references/doc-pack-templates.md)；
   - Tmux 监测、四维终审与实机体验规范见 [references/verifier-and-ux-playbook.md](references/verifier-and-ux-playbook.md)。
5. **操作安全红线**：禁用 `rm`（清理用 `mv <path> ~/.Trash/`）；Git 用 `rtk git`（若项目约定），不加 AI 署名、未经授权不 `git push`。

---

## 5. 终端直达与追溯清单（回复末尾必附）

- **Tmux 实时看板**：`tmux attach -t <tmux_session>`（或 `loopctl.py tmux attach --repo "<repo>"`）
- **闭环总台账**：`[LATEST.md](file://<repo>/.agent-loop/LATEST.md)`
- **一行统一回看**：`python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py view --repo "<repo>"`
- **按单会话跨目录回放**：`view --claude-id <id>` / `view --codex-id <id>` / `view --agy-id <id>`
- **原生接管命令**：
  - Claude Code：`claude logs <bg_id>` / `claude attach <bg_id>` / `cd "<workdir>" && claude --resume <sid>`
  - Codex：`cd "<workdir>" && codex resume <sid>`（原 worktree 已清理时：`codex resume --all -C "<repo>" <sid>`）
  - Antigravity：`conversation://<agy_id>`（CLI 会话另支持 `agy --conversation <agy_id>`）

---

## 6. 新项目 `AGENTS.md` 三行接入模板

运行 `python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py snippet --append <repo>`：

```markdown
## 多 Harness 协作角色（默认口径，均调用 `harness-loop`）

- **Antigravity（`agy`）**：默认任 **Architect + Post-Verifier + 体验官**——负责项目审查、撰写 PRD/Tech-Spec/Test-Plan/Handoff 规格包，用 `loopctl.py delegate`（默认跑进 `tmux` 总览看板与分窗）派活并挂 `loopctl.py watch` 零 Token 守候，完工后独立四维终审、实机体验并向用户结项。
- **Claude Code（`claude`）**：默认任 **Executor**（复杂需求默认首行 `ultracode`）——严格按规格包 CUI-First 分阶段施工与提交，遇口径疑问可 `loopctl.py consult` 取经，收口前自跑 `audit_commits.py` 并回写 `.agent-loop/LATEST.md`。
- **Codex（`codex`，如 `GPT-6.2-Sol`）**：按用户指定任 **Executor / 审校顾问**——承担开发实现或对抗性代码审校，保持原生会话可追溯（`codex resume`），进度与交付同步至 `.agent-loop/LATEST.md`。
```
