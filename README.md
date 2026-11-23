# agy-claude-loop

> **Antigravity (`agy`) ↔ Claude Code (`claude`) ↔ Codex (`codex`, 含 `GPT-6.2-Sol`)** 三端动态闭环与可追溯编排 Skill。  
> 零外部依赖（纯 Python 3 标准库）、零等待 Token 消耗、零全局 Hook 侵入——新电脑一行命令装完即可直接调用。

---

## 一行零配置安装（新电脑直装）

```bash
curl -fsSL https://raw.githubusercontent.com/qbu11/agy-claude-loop/main/install.sh | bash
```

安装脚本会自动完成：
1. 将仓库拉取到规范目录 `~/.agents/skills/agy-claude-loop`；
2. 自动建立 **Claude Code**（`~/.claude/skills/`）、**Codex**（`~/.codex/skills/`）与 **Antigravity**（`~/.gemini/config/skills/`）软链接（若 `~/.gemini/config/skills` 本身已软链到 `~/.agents/skills`，会自动识别并跳过，不产生嵌套自链）；
3. 运行 `loopctl.py doctor` 输出三端 CLI 与挂载自检报告。

如果希望安装时顺便把三端协作角色写入某个项目仓库的 `AGENTS.md`：

```bash
curl -fsSL https://raw.githubusercontent.com/qbu11/agy-claude-loop/main/install.sh | bash -s -- --init-agents /path/to/your-repo
```

也可以手动克隆安装（或用 `npx skills`）：

```bash
git clone https://github.com/qbu11/agy-claude-loop.git ~/.agents/skills/agy-claude-loop
bash ~/.agents/skills/agy-claude-loop/install.sh
```

---

## 核心特性

1. **动态三端角色花名册（Architect / Executor / Verifier / Advisor）**
   - 默认由 **Antigravity (`agy`)** 任 Architect + Post-Verifier + 体验官，**Claude Code (`claude`)**（复杂需求自动首行加 `ultracode`）或 **Codex (`gpt-6.2-sol`)** 任 Executor，任意回合可用 `loopctl.py set-roster` 动态换将或指定具体模型。
   - **默认不覆盖目标 Harness 全局配置**：未显式传入 `--model` 时不传 `-m`/`--model`，且绝不跨端传递认证或 Provider 环境变量。
2. **3×3 跨端互调网（`consult` / `delegate` / `inspect`）**
   - `consult`：**只读取经与对抗审校**（强制 AGY `--mode plan`、Claude `--permission-mode plan`、Codex `-s read-only`），不修改业务文件；
   - `delegate`：**跨端派活改码**（同仓库单写者冲突保护 + `AGY_LOOP_DEPTH < 2` 防递归护栏）；
   - `inspect`：**纯只读实时切片检查**，可带 `--sync-ledger` 同步完整交卷正文。
3. **零 Token 四合一本地监测（`loopctl.py watch`）**
   - 不改各端全局 Hook 配置（避开 Codex `hooks.json` 交互式信任门禁与崩溃漏触发问题），也不靠 LLM `schedule` 空烧轮询 Token；
   - 通过 **终态事件（`task_complete` / `end_turn` / `StopFailure` / `turn_aborted`）+ PID 存活（含 `ps -o stat=` 识别 UNIX `Z` 僵尸态）+ 日志/Git/子进程活跃度 + 卡死阈值（`--stall-timeout`）** 实时监测，完工瞬间退出并借 AGY 原生后台任务回调精准唤醒当前对话。
4. **全链路透明台账与终端一行回看（`loopctl.py view`）**
   - 在目标仓库自动生成 git-ignored `<repo>/.agent-loop/LATEST.md`（实际存放在 `<repo>/.agent-loop/<run-id>/DIALOGUE.md`），带结构化事件、SHA-256 哈希与三端原生恢复命令；
   - 终端一行命令 `python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py view --repo <repo>` 同时回看 Codex + AGY 双端对话，并直接给出 `cd "<repo>" && codex resume <sid>`。
5. **Post-Verifier 确定性提交审计（`audit_commits.py`）**
   - 自动核查工作区洁净度、CUI-First 提交顺序、无 AI 署名、保护目录未触碰、零残留失败截图与验证报告新鲜度。

---

## 新项目接入模板（`AGENTS.md`）

运行以下命令即可给当前项目写入（或打印）三端各自的默认角色口径：

```bash
python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py snippet --append /path/to/repo
```

写入内容如下（每个 Harness 仅 1 行，打开仓库即读懂分工）：

```markdown
## 多 Harness 协作角色（默认口径，均调用 `agy-claude-loop`）

- **Antigravity（`agy`）**：默认任 **Architect + Post-Verifier + 体验官**——负责项目审查、撰写 PRD/Tech-Spec/Test-Plan/Handoff 规格包，用 `loopctl.py delegate` 派活并挂 `loopctl.py watch` 零 Token 守候，完工后独立四维终审、实机体验并向用户结项。
- **Claude Code（`claude`）**：默认任 **Executor**（复杂需求默认首行 `ultracode`）——严格按规格包 CUI-First 分阶段施工与提交，遇口径疑问可 `loopctl.py consult` 取经，收口前自跑 `audit_commits.py` 并回写 `.agent-loop/LATEST.md`。
- **Codex（`codex`，如 `GPT-6.2-Sol`）**：按用户指定任 **Executor / 审校顾问**——承担开发实现或对抗性代码审校，保持原生会话可追溯（`codex resume`），进度与交付同步至 `.agent-loop/LATEST.md`。
```

---

## 目录结构与自检

```text
agy-claude-loop/
├── SKILL.md                             # 三端共用的主规范入口
├── agents/openai.yaml                   # Codex / Agents UI 元数据
├── install.sh                           # 跨平台一键安装与软链脚本
├── references/
│   ├── cross-harness-mesh.md            # 3x3 互调矩阵、模型别名与实测注意项
│   ├── doc-pack-templates.md            # PRD / Tech-Spec / Test-Plan / Handoff 骨架
│   └── verifier-and-ux-playbook.md      # 零 Token watch 监测 + 四维终审 + 真实用户体验手册
└── scripts/
    ├── loopctl.py                       # 核心编排、consult/delegate/inspect/watch/view/doctor/snippet
    ├── audit_commits.py                 # Post-Verifier 7 项确定性工程审计
    └── test_loopctl.py                  # 零外部依赖隔离单元测试集
```

运行自带测试与环境诊断：

```bash
python3 -B -m unittest discover -s ~/.agents/skills/agy-claude-loop/scripts -v
python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py doctor
```

## License

MIT
