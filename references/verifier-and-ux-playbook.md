# 实时监测、Post-Verifier 独立终审与实机体验手册

## 1. 全会话 `tmux` 可视化看进度 + 零 Token 守候

### A. 随时进 `tmux` 盯多端实时输出
通过 `loopctl.py delegate`（或 `consult`）派发的会话默认自动跑在 **`hl-<slug>`** tmux 会话中（不修改全局 `~/.tmux.conf`）：
- **接入总览看板与全波窗口**：
  ```bash
  python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py tmux attach --repo "<repo>"
  # 或直接：tmux attach -t hl-<slug>
  ```
- **常用快捷键**：鼠标点击底部状态栏切窗 · `Ctrl-b w` 列表选窗 · `Ctrl-b 0` 回 Window 0 总览看板 · `Ctrl-b d` 后台脱离（任务继续在后台运行）
- **终端快速查表 / 不进入 TUI 直接抓最近画面**：
  ```bash
  python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py tmux status --repo "<repo>"
  python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py tmux tail --repo "<repo>" --job-id "<job_id>" --lines 40
  ```

### B. 零 Token 本地监测 `watch`（支持单任务 & 整波多任务）
派活后，将 `loopctl.py watch` 挂为 AGY 后台命令。等待期间 **消耗 0 LLM Token**，目标一完成即同步结果进 `DIALOGUE.md` 并精准唤醒当前 AGY 会话：

```bash
# 单任务守候
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py watch \
  --repo "<repo>" --job-id "<job_id>" --stall-timeout 600 --timeout 7200 --json

# 整波多 Worktree 并发守候（等该波全部完成或任一失败汇总返回）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py watch \
  --repo "<repo>" --wave "<wave_name>" --stall-timeout 600 --timeout 7200 --json
```

**五合一监测信号与退出码**：
1. 捕获结构化终态（Codex `task_complete`/`turn_aborted`、Claude `end_turn`/`StopFailure`）与 tmux runner 原子 `exit_file`（`claude --bg` 保持由 `claude agents` + JSONL 判定，不因脱离 `claude attach` 误判结束）；
2. 进程与官方状态（`is_pid_alive` 过滤 UNIX `Z` 僵尸态）；
3. 各任务独立 `workdir` 的日志/Git/子进程活跃度；
4. `--stall-timeout`（默认 600s）连续静默卡死阈值。

| 退出码 | `reason` | 含义与下一步 |
|---|---|---|
| `0` | `COMPLETED` / `WAVE_COMPLETED` | 全部任务正常完工并入账，立即进入 **Stage 4 终审** |
| `1` | `BLOCKED_OR_FAILED` / `PROCESS_EXITED_FAILED` / `WAVE_PARTIAL_FAILED` | 存在失败或阻塞，附尾部诊断日志供立即排障 |
| `2` | `STALLED` | 超时无活跃输出与 Git 变动，唤醒执行协调者介入 |
| `124` | `TIMEOUT` | 达到总上限 `--timeout` |

> **统一只读回放命令（支持原 Worktree 已删除）**：  
> `python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py view --repo "<repo>"`  
> 按单个会话或 Job 直查：`view --claude-id <id>` / `view --codex-id <id>` / `view --agy-id <id>` / `view --repo "<repo>" --job-id <jid>`（加 `--full` 展开完整长文）。

---

## 2. Post-Verifier 四维独立终审清单（Stage 4）

> 核心要求：**严禁只照抄执行端汇报数字**。审查者必须独立跑核查、读 Diff、复跑快检并亲眼读图。

### 维度 A：确定性工程纪律核查
```bash
# CLI/后端里程碑阶段：验证当期 CLI 报告与每里程碑 CUI-First 顺序，跳过未到的 GUI 报告
python3 -B ~/.agents/skills/harness-loop/scripts/audit_commits.py \
  --repo "<repo>" --base "<base_commit>" --stage cui --json

# 全里程碑收口阶段：全量工作区、零 AI 署名、每里程碑 CUI-First、全部报告零失败且新鲜
python3 -B ~/.agents/skills/harness-loop/scripts/audit_commits.py \
  --repo "<repo>" --base "<base_commit>" --stage full --json
```

### 维度 B：逐文件 Diff 深审（高频隐患清单）
执行 `git diff <base_commit>..HEAD` 并重点核查：
1. **鉴权与路径安全**：ID 校验与路径规范化落沙盒内，非授权用户真返回 `403`。
2. **原子写与并发**：临时文件写盘 + `os.replace`/原子重命名；目录快照含 `until_commit` 时不含未来分支或未提交文件。
3. **缓存与数据一致性**：列表/摘要/预览是否误复用陈旧快照，主逻辑与批处理分支是否漏记状态或双记。
4. **向后兼容**：新增持久化字段有默认值（如 `#[serde(default)]`）；老数据、老路由别名继续通过。
5. **产品与文案红线**：无禁用工程术语、无禁词（如按 PRD 要求统一「大字版」）、无违规评价标尺。
6. **静默失败**：Toast 撤销闭包、空态按钮、快捷键边界不静默吞错，保留明确提示。

### 维度 C：独立复跑快速门禁
- 亲自复跑 `tsc`、单元测试、`cargo test` 与守卫脚本，确认实测数字与提交信息一致，不产生脏工作区。

### 维度 D：逐张读图目视核验
使用 `read_file` 打开本轮新增或更新的所有截图（`.png`），逐张核对：
- 折叠按钮/顶栏文字是否重叠或折行；
- 深/浅双主题与大字版下的文字对比度、卡片对齐、零毛玻璃（`backdrop-filter: none`）；
- 不同视口下宽屏分栏与窄屏降级是否无横向溢出、破图或乱码。

### 跨端对抗性代码审查规范（推荐 Codex `gpt-6.2-sol`）
重要分支或合并前，建议由独立后端审查模型（如 Codex `gpt-6.2-sol`）加扫一轮只读对抗审校（见 `doc-pack-templates.md §5`）：
- **按风险分级选择推理深度**：
  ```bash
  # 常规变更（默认 high）
  python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py consult \
    --repo "<repo>" --from agy --to codex --model gpt-6.2-sol --reasoning-effort high --prompt-file "<review.md>" --json

  # 触及资金/计费、权限与登录、对外发消息或关键外部/持久化数据写入（升至 xhigh）
  python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py consult \
    --repo "<repo>" --from agy --to codex --model gpt-6.2-sol --reasoning-effort xhigh --prompt-file "<review.md>" --json
  ```
- **两级发现标准**：
  - **阻断项（Blocking）**：仅当问题会导致算错钱、数据丢失或损坏、越权或登录鉴权漏洞、对外错发消息或错写关键外部数据、发布或核心链路事故，或破坏显式契约/测试，**且审查者能写清具体触发路径与 `文件:行号`** 时，才判为阻断；
  - **建议项（Advisory）**：依赖多重罕见巧合的极端假设、风格偏好或非关键重构仅记为建议，不挡放行。
- **最多审 2 轮**：达 `max_rounds`（默认 `2`）仍未过即拦截升级为 `ESCALATED`，由主控端汇总证据与选项交用户拍板。
- **纠错变检查棘轮（Ratchet）**：
  - 用户纠正或审查退回一类错时，在同次修复里尽量把该类规则沉淀为机械检查（类型约束、lint 或守卫测试，报错写明改法），或通过重构接口/目录让错误写法无法成立；确实做不到时再写进文字规则并注明原因；
  - 新增检查时，历史旧违规记入欠账清单只减不增，新代码零容忍；文字规则已被检查完全守住的，删掉冗余文字或仅留一句指向检查；
  - 每次改动前列全同类入口（深浅色、桌面与窄屏、文档/文案/测试/配置），改完反搜旧说法残留，按真实用户路径走通后再交卷。

### 裁决与返工
- 存在任何阻断或中高风险隐患 → 记录 `record --event verifier_audit --verdict NEEDS_FIX`，按 `doc-pack-templates.md §4` 生成精确 `文件:行号` 返工单并调 `loopctl.py delegate --rework` 打回修复。
- **自动返工硬上限**：`max_rounds` 默认 `2`；达到上限或同一错误跨轮复现时，继续 `delegate --rework` 会被强制拦截并置为 `ESCALATED`，此时改用 `loopctl.py record --event escalate --summary "<阻塞原因与备选方案>"` 升级用户决定（用户确认后可加 `--force` 放行）。
- 全部通过 → 记录 `record --event verifier_audit --verdict PASS`，进入 Stage 5。

---

## 3. Stage 5 交付前活体核验与真实用户体验（UX Walkthrough）

1. **交付本机预览前必跑活体检查**：
   - 运行项目活体检查脚本（如 `bash scripts/dev-status.sh` 或对应健康探针），确认运行中后端不落后于最新源码/二进制，且无核心路由 404。
2. **设定真实用户剧本（3–5 步连贯任务流）**：
   - 切换目标用户视角（而非测试工程师视角），完整走通核心漏斗或端到端工作流。
3. **执行体验并读图核对**：
   - Web 项目：委派 `browser` 子代理（默认 `Model="flash"`）真实操作并截图，主代理按 `BROWSER_HOST_FACTS` 用 `read_file` 复核截图；
   - 纯 CLI 项目：以真实用户样本完整跑通核心命令行流程并记录响应速度与报错可读性（无前端可交互界面时，若跳过 `ux_walkthrough` 须在 `signoff` 显式传 `--skip-ux`）。
4. **严格结项门禁（`signoff` → `DONE`）与收尾**：
   - 运行 `loopctl.py record --event ux_walkthrough` 与 `loopctl.py record --event signoff`；
   - **`signoff` 自动校验三项前置条件**（缺一则拒绝并报错）：① `verdict == PASS`；② 所有 `jobs` 无活跃或未闭环失败项；③ 本轮已记录 `ux_walkthrough`（或显式 `--skip-ux`）。全部通过后自动落为终态 `DONE`；
   - 向用户输出：① 门禁实测表；② 真实用户视角体验报告（爽点、轻微卡点与 1–3 条高价值下一步建议）；③ `tmux` / `view` / 各端恢复指令清单；④ 询问是否授权 `git push` 或运行 `loopctl.py tmux stop --all` 清理窗口。
