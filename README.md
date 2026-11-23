# harness-loop

> 多 Harness 动态闭环协作与 `tmux` 并行编排 Skill —— 内置 **Antigravity (`agy`) ↔ Claude Code (`claude`) ↔ Codex (`codex`, 含 `GPT-6.2-Sol`)**，并可平顺扩展更多 CLI/ACP Harness。  
> 纯 Python 3 标准库实现（零外部依赖）、全会话 `tmux` 实时可视化、零等待 Token 消耗。

---

## 一行零配置安装（新电脑直装）

```bash
curl -fsSL https://raw.githubusercontent.com/qbu11/harness-loop/main/install.sh | bash
```

安装脚本自动完成：
1. 克隆到规范目录 `~/.agents/skills/harness-loop`；
2. 挂载 **Claude Code**（`~/.claude/skills/`）、**Codex**（`~/.codex/skills/`）与 **Antigravity**（`~/.gemini/config/skills/`）软链；
3. 运行 `loopctl.py doctor` 输出各端 CLI、`tmux` 与软链状态。

安装同时为项目写入协作约定：

```bash
curl -fsSL https://raw.githubusercontent.com/qbu11/harness-loop/main/install.sh | bash -s -- --init-agents /path/to/your-repo
```

---

## 核心能力

1. **全会话默认跑在 `tmux`（Window 0 总览看板 + 独立命名窗）**
   - `delegate` 与 `consult` 默认在 `hl-<slug>` 会话中开启独立窗口执行（Codex/AGY 实时解码流式进度，Claude Code `--bg` 自动 `attach` 进窗），局部开启 `mouse on` 与 `remain-on-exit on`，不改全局 `~/.tmux.conf`。
   - 随时 `loopctl.py tmux attach --repo <repo>` 进场鼠标切窗或 `Ctrl-b d` 脱离，也可不进 TUI 直接 `loopctl.py tmux status` / `tmux tail --job-id <id>` 抓屏。
2. **多 Git Worktree 并行波次 & 按赛道花名册（`--track`）**
   - `delegate --workdir <wt> --wave <w> --track <name>` 支持多路隔离并行，写冲突检查精确限定到目标 `workdir`，`fcntl` 可重入文件锁 + 原子写保护 `manifest.json` 并发更新。
3. **跨端只读咨询 & 零 Token 整波守候**
   - `consult`（强制各端只读模式）负责跨端评审；`watch --wave <w>` 综合终态事件、原子退出码、PID/僵尸进程、工作区活跃度与静默卡死阈值，全波完工自动汇总入账。
4. **全端统一可读回放（无视 Worktree 删存）**
   - `loopctl.py view --repo <repo>` 一行汇总台账与各端记录；
   - 支持 `--claude-id <uuid或8位bg_id>`、`--codex-id <uuid>`、`--agy-id <uuid>`、`--job-id <id>`、`--full`，即使原 Worktree 已删除仍可秒级回放对话。
5. **多里程碑提交审计（`audit_commits.py --stage cui|full`）**
   - 独立检查每里程碑（`M1`, `M2`...）后端先于前端纪律、工作区洁净、无 AI 署名、无残留失败截图与测试报告新鲜度。

---

## 常用命令速查

```bash
# 1. 初始化并绑定赛道模型（未显式指定则继承目标 CLI 全局默认）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py init \
  --repo . --slug my-feature --track backend_codex=codex:GPT-6.2-Sol --track frontend=claude --json

# 2. 在隔离 worktree 派发并行任务（自动进 tmux + 看板）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py delegate \
  --repo . --workdir ../wt-backend --wave wave1 --track backend_codex --label backend --from agy --prompt-file task.md --json

# 3. 查看全波实时进度或进入 tmux
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py tmux status --repo .
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py tmux attach --repo .

# 4. 零 Token 等待整波完成
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py watch --repo . --wave wave1 --json

# 5. 回看任意 Claude / Codex / AGY 会话内容
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py view --claude-id <id>
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py view --codex-id <id>
```

---

## 目录结构与自检

```text
harness-loop/
├── SKILL.md                             # 主规范入口
├── agents/openai.yaml                   # Agents UI 元数据
├── install.sh                           # 跨平台一键安装与软链脚本
├── references/
│   ├── cross-harness-mesh.md            # 跨端互调、tmux 编排、赛道模型与沙箱边界
│   ├── doc-pack-templates.md            # 六件套文档骨架 + 3 套执行提示词 + 返工模板
│   └── verifier-and-ux-playbook.md      # tmux/watch 守候 + 四维终审 + 实机用户体验
└── scripts/
    ├── loopctl.py                       # 核心引擎（init/delegate/consult/tmux/watch/view/doctor）
    ├── audit_commits.py                 # 多里程碑 CUI-First 与门禁审计
    └── test_loopctl.py                  # 隔离单元测试集（含真实独立 tmux 测试）
```

运行测试与环境自检：

```bash
python3 -B -m unittest discover -s ~/.agents/skills/harness-loop/scripts -v
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py doctor
```

## License

MIT
