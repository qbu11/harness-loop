# Post-Verifier 独立审查、定时巡检与真实用户体验手册

## 1. 零 Token 状态监测（`watch`）与异步巡检协议

### A. 本地零 Token 状态监测 `watch`（优先，零轮询、零全局 Hook 污染）
- 后台派活（Codex / Claude Code）后，直接将 `loopctl.py watch` 作为 AGY 后台命令挂起；它在本地以纯 Python 每 2 秒低开销检查四合一信号，**等待期间消耗 0 LLM Token**，目标结束瞬间退出并借 AGY 原生后台任务通知唤醒**当前这个对应的 AGY 对话**（通过 `last_dispatch_epoch` 与 `--since-epoch` 避开历史回合误判，并用 `ps -o stat=` 过滤 UNIX `Z` 僵尸进程以免父进程未回收时误判存活）：
  ```bash
  python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py watch \
    --repo "<repo>" --harness <codex|claude> --stall-timeout 600 --timeout 7200 --json
  ```
- **返回状态与退出码语义**：
  - `0` (`COMPLETED`)：捕捉到结构化终态（Codex `task_complete` / Claude `end_turn`），自动 `--sync-ledger` 写入台账，立即进入 **Stage 4 Post-Verifier**；
  - `1` (`BLOCKED_OR_FAILED` / `PROCESS_EXITED_FAILED`)：捕捉到 `StopFailure` / `turn_aborted` 或进程异常提前退出，附带日志尾部诊断供立即排障；
  - `2` (`STALLED`)：进程仍在且未出现终态，且连续 `--stall-timeout`（默认 600s）无日志、无 Git 变动、无活跃子进程，主动唤醒 AGY 介入；
  - `124` (`TIMEOUT`)：达到总等候上限 `--timeout`（默认 7200s）。

> **方案对比留档（为什么选 `watch` 而非纯 Hook 或 LLM 定时轮询）**：
> | 方案 | 成本与延迟 | 工程短板 |
> |---|---|---|
> | **纯 `Stop` Hook** | 0 Token / 秒级 | 崩溃、OOM、被杀时不触发；Codex `hooks.json` 有交互式 `/hooks` 信任门禁且与 `config.toml` `notify`（如 Computer Use）互斥；AGY Desktop 无外部主动 push 接口 |
> | **LLM `schedule` 轮询** | 每次唤醒重吃上下文 Token | 估短空烧钱、估长干等，仅适合数小时超长任务兜底 |
> | **本地 `watch`（推荐）** | **0 等待 Token / 秒级** | 结合终态事件 + PID/僵尸态 + 日志/Git/子进程活跃度 + 卡死阈值，兼顾稳健、可移植与精确唤醒 |

### B. 手动切片查看与长耗时 `schedule` 巡检（补充保底）
当用户让 AGY 盯守一个后台或终端运行中的 Claude Code 会话（如 30 分钟–4 小时）：

1. **优先读取无侵入可观测源**：
   ```bash
   python3 ~/.agents/skills/harness-loop/scripts/loopctl.py inspect-cc --repo "<repo>" --tail-turns 4 --json
   ```
   - `inspect-cc` 结合 `claude agents --all --json` 与会话 `.jsonl` 末尾切片判定 `normalized_state`：
     - `RUNNING`：仍在跑测试或写代码，**不要打断它**。立刻调用 AGY 的 `schedule` 工具设下一跳定时器（如短活 `DurationSeconds=900`~`1800`，长活 `3600`），Prompt 中写清 repo、base_commit、session_id 与本步骤指令。
     - `BLOCKED_OR_FAILED`：立即向用户报错并展示 `.jsonl` 尾部阻塞/报错上下文。
     - `IDLE_COMPLETED`：加 `--sync-ledger` 把 Claude 最后完整回复同步进 `.agent-loop/<run-id>/DIALOGUE.md`，立即进入 **Stage 4 Post-Verifier 独立终审**。
2. **严禁绕过 `cmux` 访问限制**：若终端外部调 `cmux` 返回 `Access denied - only processes started inside cmux can connect`，属正常安全策略，直接使用 `claude agents --all --json`、`claude logs` 与 `.jsonl` 解析，不得尝试探测或绕过 socket 权限。

---

## 2. Post-Verifier 四维独立审查清单（Stage 4）

> **核心理念**：「验收脚本全绿」不等于「没有隐藏漏洞或体验断裂」。AGY 必须把自己的角色切换为独立 Reviewer，绝不照抄 Claude 自己的交卷数字。

### 维度 A：确定性工程与提交链核查
```bash
python3 ~/.agents/skills/harness-loop/scripts/audit_commits.py \
  --repo "<repo>" --base "<base_commit>" --head HEAD --json
```
核查：
- `clean_worktree` 为 `true`；
- `cui_first_discipline` 通过（纯后端/CLI 提交在前且零触碰前端源码）；
- `no_ai_attribution` 与 `forbidden_paths_untouched` 通过；
- `no_leftover_failure_screenshots` 通过；
- `reports_zero_failed_and_fresh` 通过（没有在末次改源码后漏重跑测试）。

### 维度 B：逐文件实质 Diff 深度审查（最容易抓出真问题）
执行 `git diff <base_commit>..HEAD -- <核心业务目录>` 并重点猎杀以下 7 类高频隐患：
1. **安全与鉴权边界**：下载/导出/文件读取路由是否校验 `id` 非空且不含 `..`/`/`/`\\`、是否用 `.canonicalize()` 验证路径严格落在授权沙盒根目录内、非管理员/非拥有者是否真返回 `403`。
2. **历史快照与分支泄漏**：按 `until_commit` 导出历史包时，`git bundle` 是否误用了 `--all` 把未来分支一起打包、是否把未提交/当前工作区文件混入历史包、临时导出目录是否用 RAII/`Drop` 清理。
3. **缓存复用偷懒**：离线评分/预览/重跑接口是否盲目复用旧产物（例如把截断包或过时提交的导出当成当前最新状态）。
4. **结构化指标或观测失真**：状态转移/遥测/评分聚合是否漏了某类 `target_type`（如修改大纲节点未关联底下卡片出处导致永远算未观察）、是否存在恒真占位逻辑、是否只在单路径记账而漏掉异步/批处理分支（或同步+异步双路径重复记账）。
5. **向后兼容与快捷键越界**：新增配置/数组后，快捷键索引映射（如 `Digit[0-3]`）是否匹配实际长度；新增字段是否都有 `#[serde(default)]`。
6. **静默失败与无效撤销**：Toast「撤销」按钮闭包内变量是否可能为 `undefined` 导致静默 `return`；空状态是否给了「一步到位」出口而不是死板弹窗提示「先去某处建完再回来」。
7. **产品红线与 UI 文案**：是否混入英文工程词（如 `Episode`、`UUID`、`fallback`）、评价性红绿灯打分、未授权训练导出通道。

### 维度 C：独立复跑验证（不修改工作区）
- 对快速门禁（编译、类型检查 `tsc --noEmit`、Rust `cargo test`、前端单测、Python `unittest`）**亲自重跑并核对实测输出**。
- 注意避开会覆写已提交 JSON 文件的脚本（或重跑前确认不会制造脏工作区；若仅读取长耗时 E2E 报告，核对其落盘时间戳晚于最后一次改代码提交，且源码与报告数量一一吻合）。

### 维度 D：逐张读图目视核验
用 `read_file` 打开本轮新增或修改的全部关键截图（`.png`），逐张核对：
- 折叠侧栏触发按钮（如 `[|]`）是否压住正文中首字、工具栏切换器文字或返回按钮；
- 纯文本/HTML 材料在缩略图位是否出现破图占位图标；
- 窄视口（`768px` / `1024px`）顶栏按钮是否互相挤压、换行或横向溢出；
- 浮窗、下拉菜单、Modal 是否存在未自动收起导致的叠压遮挡；
- 文字排版、字号层级与色彩规范是否自然可读。

### 裁决输出
- **发现任何阻断或中高风险隐患 → 裁决 `NEEDS_FIX`**：
  - 记录 `loopctl.py record --role agy --event verifier_audit --verdict NEEDS_FIX ...`；
  - 若当前返工轮次 `< 2`：生成精确到 `文件:行号` 的返工清单，调用 `loopctl.py send-cc` 自动续跑（若 CC 为交互式终端无法直发，则写入 `ROUND-N-PROMPT.md` 并向用户呈现一键复制提示词）；
  - 若返工已满 2 轮仍未收敛：按计划暂停并向用户报告分歧点，询问由用户指示或接管修复。
- **全部审查通过 → 裁决 `PASS`**：
  - 记录 `loopctl.py record --role agy --event verifier_audit --verdict PASS ...`，立即进入 **Stage 5 真实用户视角体验**。

---

## 3. Stage 5 真实用户视角全流程体验（Simulated Target Persona Walkthrough）

> 目的：跳出“测试工程师找 bug”视角，真正扮演目标用户（如 30–60 岁历史学者、课题组导师/助研、B 端管理员）在**真实运行的本地应用**里走一遍核心任务流，检验**爽感、效率提升感知、犹豫卡点与下一轮迭代灵感**。

### 执行步骤
1. **确认本地应用可访问且不打扰隔离验收**：
   - 检查项目常用 dev 端口（或只读运行状态脚本）；若尚未启动且用户允许本地预览，按项目标准方式启动开发服务（确保先确认无正在跑的占用端口测试）。
2. **设定真实用户剧本（Persona + Goal）**：
   - 明确身份（如「首次导入一批文献的历史系研究员 + 课题组导师」）；
   - 明确 3–5 步连贯任务（导入/查看材料 → 使用核心研究视图筛选并核对原页 → 写入大纲/导出成果 → 切换管理员体验后台闭环）。
3. **委派 `browser` 子代理执行真实交互体验**（注意遵守 `browser` 子代理规则：传 `Model="flash"`，不传未说明偏好，等待完成并核对 `BROWSER_HOST_FACTS` 与截图）：
   - 要求子代理像真实用户一样点击、输入、切视图、核对原页与导出，并汇报每一步的响应流畅度、视觉观感、是否出现让人犹豫/找不到按钮的时刻，以及截取关键页面截图。
   - 主代理用 `read_file` 查看 `BROWSER_HOST_FACTS` 中列出的最新截图。
   - *若当前项目为纯 CLI/库且无 Web UI*：则用真实样本数据亲自执行核心 CLI 用户路径替代浏览器体验，并如实记录终端体验反馈。
4. **记录体验结论（Stage 5）与结项提问（Stage 6）**：
   - 运行 `loopctl.py record --role agy --event ux_walkthrough` 与 `--event signoff`；
   - 向用户呈现结构化结项报告：
     1. 门禁实测表 + 返工与审查记录；
     2. **真实用户视角体验报告**（哪里用得爽 / 哪里还有轻微卡顿或可打磨点 / 下一轮最值得推进的 1–3 个产品改进建议）；
     3. **双端互聊 Session 可追溯链接**（`DIALOGUE.md` 路径、AGY 会话、Claude Code `attach` / `logs` / `--resume` 命令）；
     4. **主动询问用户**：是否授权 `git push`、或者下一步想推进哪一个体验改进 / 新需求。
