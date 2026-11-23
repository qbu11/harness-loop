# 多 Harness 互调网、Tmux 并发路由与工程边界手册

> 本文档说明跨端调用矩阵（`consult` / `delegate` / `inspect`）、Tmux 全会话可视化布局、按赛道花名册路由及真实 CLI 边界。

---

## 1. 跨端互调矩阵（内置 AGY ↔ Claude Code ↔ Codex；新 Harness 可插拔）

| 发起方 | 目标端 | 只读评审（`consult`） | 派活写码（`delegate`） | 实时进度（`inspect`） |
|---|---|---|---|---|
| 任意端 | **Antigravity (`agy`)** | `agy -p --mode plan --output-format json [--model <m>]` | `agy -p --mode accept-edits --output-format json [--model <m>]` | `inspect --harness agy --agy-id <id>` |
| 任意端 | **Claude Code (`claude`)** | `claude -p --permission-mode plan --output-format json [--model <m>]` | `claude --bg -n <名>`（自动进 tmux 窗执行 `claude attach`） | `inspect --harness claude --workdir <wt>` |
| 任意端 | **Codex (`codex`)** | `codex exec -C <workdir> -s read-only --json -o <reply.md> [-m <m>]` | `codex exec -C <workdir> -s workspace-write --json -o <last.md> [-m <m>]` | `inspect --harness codex --workdir <wt> [--job-id <id>]` |

**`loopctl.py` 自动执行的护栏与记账**：
1. 注入 `HARNESS_LOOP_DEPTH`（兼容旧变量 `AGY_LOOP_DEPTH`），默认限制嵌套 `< 2`；
2. 写冲突检查按 **目标 `workdir` 精确隔离**：不同 `git worktree` 可并发派活，同一工作目录禁止双写；
3. `fcntl.flock` + 原子写保障并发更新 `manifest.json` 与 `LATEST.md` 不丢记录；
4. 所有 `consult` / `delegate` / `delivery` / `audit` 全量留痕进 `<repo>/.agent-loop/<run-id>/DIALOGUE.md`。

---

## 2. Tmux 全会话实时看板

只要本机已安装 `tmux`，`delegate` 与 `consult` 默认自动在会话 **`hl-<slug>`** 中分配命名窗口运行（需临时关闭时传 `--no-tmux`）：
- **Window 0 (`dashboard`)**：自动刷新全波任务总览表（窗口号、状态、Wave、Harness/模型、耗时、最近活跃时间、Workdir/分支）。
- **Worker 窗口 (`1..N`)**：
  - Codex / AGY：内部通过 `_run-job` 流式解码打印思考摘要、执行命令、修改文件与最终回复，原始 JSONL 同步无损落盘并原子写入 `.exit.json`；
  - Claude Code：`--bg` 启动隔离后台任务，同时在 tmux 窗内运行 `claude attach <bg-id>` 实时呈现原生交互界面。

| 命令 | 用途 |
|---|---|
| `loopctl.py tmux attach --repo <repo> [--job-id <id>]` | 一键接入或切换到对应 tmux 会话/窗口 |
| `loopctl.py tmux status --repo <repo> [--json]` | 打印当前所有任务窗口实时状态表 |
| `loopctl.py tmux tail --repo <repo> [--job-id <id>] [--lines 40]` | 不进 tmux 直接抓取某窗口末尾 40 行画面 |
| `loopctl.py tmux stop --repo <repo> (--job-id <id> \| --all)` | 安全关闭指定窗口或整组会话 |

> **全局配置零侵入**：仅对该 `hl-<slug>` 会话局部设置 `mouse on`、`remain-on-exit on`、`allow-rename off`，不写 `~/.tmux.conf`。

---

## 3. 按赛道花名册（`--track`）与多 Worktree 并发

当项目前后端、不同波次使用不同模型或 Harness 时，用 `--track` 绑定赛道（未指定 `--model` 时默认继承目标 CLI 全局配置，禁止跨端透传认证环境变量）：

```bash
# 1. 初始化闭环并声明各赛道默认分工
python3 -B "$HOME/.agents/skills/harness-loop/scripts/loopctl.py" init \
  --repo "<repo>" --slug "<slug>" --agy-id "<agy-id>" \
  --track backend_claude=claude --track backend_codex=codex:GPT-6.2-Sol \
  --track frontend=claude --json

# 2. 在两个隔离 Git Worktree 中并波派活（自动进入 tmux 分窗 + 看板）
python3 -B "$HOME/.agents/skills/harness-loop/scripts/loopctl.py" delegate \
  --repo "<repo>" --workdir "<wt-1a>" --wave "wave1" --track backend_claude \
  --label "w1a-files" --from agy --prompt-file "<p1a.md>" --json

python3 -B "$HOME/.agents/skills/harness-loop/scripts/loopctl.py" delegate \
  --repo "<repo>" --workdir "<wt-1b>" --wave "wave1" --track backend_codex \
  --label "w1b-outline" --from agy --prompt-file "<p1b.md>" --json

# 3. 零 Token 守候整波全部完成
python3 -B "$HOME/.agents/skills/harness-loop/scripts/loopctl.py" watch \
  --repo "<repo>" --wave "wave1" --stall-timeout 600 --json
```

### 本机常用模型别名（显式指定时可用）

| Harness | 常见简称 | 真实 CLI slug | 说明 |
|---|---|---|---|
| **Codex** | `GPT-6.2-Sol` / `6.2-sol` | `gpt-6.2-sol` | 复杂后端实现、对抗性代码审校 |
| **Codex** | `GPT-6.2-Astra` / `GPT-6-Astra` | `gpt-6.2-astra` / `gpt-6-astra` | 高推理深度设计/审校 |
| **Codex** | `Codex Auto Review` | `codex-auto-review` | 独立只读审校 |
| **Antigravity** | `Gemini 4 Neon XHigh` / `Claude Opus 5.5 High` | `gemini-4-neon-xhigh` / `claude-opus-5-5-high` | 架构、文档包、终审 |
| **Claude Code** | `opus` / `sonnet` | 原生透传 | 配合 `ultracode` 跑多阶段工作流 |

> 显式指定的模型若在目标客户端不可用，CLI 返回非零时 `loopctl.py` 立即报错并拒绝绑定会话，不静默替换模型。

---

## 4. 实测命令行与沙箱边界提示

1. **Codex macOS `workspace-write` 双沙箱边界**：
   - `.git/` 目录只读（`git add/commit` 建 `.git/index.lock` 会被拒绝）；
   - 禁止绑定本地回环端口（`127.0.0.1:0` HTTP 集成测试会报 `Operation not permitted`）。
   - **处理方式**：提示词明确告知在沙箱内跑通 compile/check + 本模块非网络单测，保留干净改动由主宿主验收提交；确需由 Codex 自提交时再显式加 `--sandbox danger-full-access`。
2. **原生 Session 恢复与已删 Worktree 查看**：
   - Codex 原生 TUI 按 CWD 过滤：Worktree 仍在时用 `cd "<wt>" && codex resume <sid>`；Worktree 已删时用 `codex resume --all -C "<main_repo>" <sid>` 或 `loopctl.py view --codex-id <sid>`。
   - Claude Code：可用 `claude logs <bg_id>` / `claude attach <bg_id>` / `cd "<wt>" && claude --resume <sid>`，或跨目录无视 worktree 删存直接用 `loopctl.py view --claude-id <id>`。
   - Antigravity Desktop 与 CLI 会话分库（`~/.gemini/antigravity/brain/` vs `antigravity-cli/brain/`），`agy --conversation <id>` 仅认 CLI 库；统一终端只读查看用 `loopctl.py view --agy-id <id>`。
3. **不重复造轮子**：
   - Codex 多文件内部并行流水线直接复用 `codex-workflows`；Claude Code 复杂多步任务用首行关键字 `ultracode`；
   - Tmux 架构借鉴 `ccmux`、`dmux`、`workmux` 与 `claude --worktree --tmux`，保持纯 Python 标准库 + 原生 `tmux`，未来扩新 Harness 只需在 `loopctl.py` 注册对应 CLI 与日志解析器。
