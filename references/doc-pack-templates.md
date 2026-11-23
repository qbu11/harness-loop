# 文档包写作规范与模板（AGY 撰写 → Claude Code / Codex 执行）

> 提炼自多轮闭环实战定型格式。  
> 核心原则：**Architect 在 Phase 2 只产出规格与交接文档（或按需只提交文档基线），不越界写业务实现；所有文档之间保持命名、接口字段、`data-testid` 与阶段编号 100% 一致；新项目可先用 `loopctl.py snippet --append <repo>` 在 `AGENTS.md` 写入三端角色口径。**

---

## 1. 标准五件套 / 四件套矩阵

| 文件名 | 定位 | 必填段落 |
|---|---|---|
| `docs/AUDIT-<slug>.md` | 前置审查单（可选，大迭代推荐） | ① 结论先行；② 目标用户画像爽点/留存卡点（10x 效率点在哪、哪里会劝退）；③ 底层架构与数据真实性审计；④ 近期必补 vs 中长期冻结 TODO |
| `docs/PRD-<slug>.md` | 产品与交互规格 | ① 背景与范围边界（明确本轮不做什么）；② 不可退化红线（顶栏锚点数/主栏数/美学禁忌）；③ 分模块行为、按钮文案、空态与错误提示；④ 设计变更对照表 |
| `docs/TECH-SPEC-<slug>.md` | 技术实现 Spec | ① 总体改动版图（ASCII 框图）；② 向后兼容数据模型扩展（`#[serde(default)]` / 默认值 / 迁移策略）；③ HTTP API 与 CLI 子命令契约；④ 前端 Store 与组件职责分工 |
| `docs/TEST-PLAN-<slug>.md` | 五层验收计划 | ① 门禁挂载点表（`U → C → S → G → V`）；② U 单元测试硬断言；③ C CLI 与环境校验；④ S 冒烟与 G GUI 真实浏览器交互；⑤ V 视觉验收与截图列表；⑥ 旧脚本迁移对照表 + `data-testid` 注册表 |
| `docs/HANDOFF-<slug>.md` | **Claude Code 唯一施工入口** | ① Stage 0 基线保护闸门；② 六条工程铁律；③ 分阶段执行路线图（先 `P0*` 后端/CLI，后 `GUI-*`，最后 `FINAL`）；④ 高概率踩坑预警；⑤ 串行终验命令；⑥ 可直接复制启动提示词 |

---

## 2. `HANDOFF-<slug>.md` 标准骨架

```markdown
# <项目名> · <迭代主题> 执行交接手册（给 Claude Code）

> **日期与基线**：YYYY-MM-DD · 基于 `<base_commit>`
> **配套文档**：[PRD-<slug>](PRD-<slug>.md) · [TECH-SPEC-<slug>](TECH-SPEC-<slug>.md) · [TEST-PLAN-<slug>](TEST-PLAN-<slug>.md) · [AGENTS.md](../AGENTS.md)

## 0. Stage 0：开工前基线检查与防回退闸门
1. 运行 `rtk git status --short` 与 `rtk git log -n 5 --oneline`，确认工作区干净（若存在前一轮已验证待收尾项，先按此处说明收口提交）。
2. 列出本轮**绝对禁止回退**的前序成果（具体提交 hash + 组件/行为）。

## 1. 工程铁律
1. **CUI-First**：全部 `P0*`（纯后端 / CLI / 数据契约）跑通单元测试与 CLI 验收并本地 commit 之前，**严禁修改任何前端源码目录**（如 `frontend/src/`）。
2. **向后兼容**：新增持久化字段必须有默认值（如 Rust `#[serde(default)]`），并用 `rg` 搜全库字面量构造点一次补齐。
3. **不造假 / 遵守产品红线**：<填入本项目核心红线，如零假数据、顶栏锚点上限、文案语言规范等>。
4. **安全与提交纪律**：禁用 `rm`（弃置文件移入 `~/.Trash/`）；禁止触碰禁区目录（如 `backend/data/`、`docs/design-refs/`）；Git 一律用 `rtk git`；每通过一个阶段即本地 commit（Conventional Commits，无 AI 署名，**不 `git push`**）。

## 2. 分阶段执行路线图
| 阶段 | 核心交付 | 详细依据 | 放行门禁 |
|---|---|---|---|
| `P0a` | 后端模型与核心接口 + CLI | Tech Spec §1 | 后端单测 + CLI 零回归 |
| `P0b` | 扩展与校验逻辑 | Tech Spec §2 | 全量 CLI 验收全绿 |
| `GUI-1` | 核心前端交互 | Tech Spec §3 | `tsc` + 前端单测全绿 |
| `FINAL` | E2E 与视觉终验 + DEVLOG | Test Plan §3-§5 | 五层串行全绿 + 逐张读图核验 |

## 3. 高概率踩坑预警
1. <根据实际代码审查写 3-8 条具体坑点：精确到文件名、函数名、容易漏改的同构路径>

## 4. 串行终验命令
<逐行贴出 U / C / S / G / V 的精确命令>

## 5. 可直接复制发给 Claude Code 的启动提示词
<见下文模板>
```

---

## 3. 发给 Executor（Claude Code / Codex / AGY）的启动提示词模板

> 该模板同时用于：① `loopctl.py delegate` / `dispatch-cc` 自动后台启动；② 写入 `HANDOFF-<slug>.md` §5 供用户一键粘贴。  
> **执行端适配规则**：
> - 若执行端为 **Claude Code** 且属于多阶段/跨前后端复杂需求：**保留首行连写关键字 `ultracode`**（触发 Claude Code 原生 `Workflow` 编排）；单点小修再删去。
> - 若执行端为 **Codex（如 `gpt-6.2-sol` / GPT-6.2-Sol）** 或 **AGY**：删去首行 `ultracode`；若 Codex 遇到可并行的多模块拆解任务，可结合本机 `~/.agents/skills/codex-workflows` 编排。注意 `-s workspace-write` 沙箱默认保护 `.git/` 目录，提示词中需说明「若 `git commit` 因 `.git/index.lock` 受限则保留工作区已验证改动并如实汇报」（需要 Codex 自提交时再传 `--sandbox danger-full-access`）。

```text
ultracode

请先核对当前工作区状态（`rtk git status --short` 为空，基线 `<base_commit>`，切勿回退 <列出不可回退的近期提交或关键修复>）。

确认后，先运行以下命令读取本次闭环台账与上游设计上下文：
python3 ~/.agents/skills/harness-loop/scripts/loopctl.py status --repo "<repo_path>" --json
python3 ~/.agents/skills/harness-loop/scripts/loopctl.py bind-session --repo "<repo_path>" --run-id "<run_id>" --harness "<claude|codex|agy>" --json

随后完整阅读以下文档并严格按 `docs/HANDOFF-<slug>.md` 分阶段执行：
1. `docs/PRD-<slug>.md`
2. `docs/TECH-SPEC-<slug>.md`
3. `docs/TEST-PLAN-<slug>.md`
4. `docs/HANDOFF-<slug>.md`
5. `AGENTS.md`

执行纪律：
1. 严格执行 **CUI-First**：先按序完成全部 `P0*` 后端与 CLI 阶段，每步跑通对应单元测试与 CLI 验收并本地 `rtk git commit` 后，才允许改动前端源码；
2. 中途对 PRD/架构有疑问或需要第二意见时，可运行 `loopctl.py consult --repo "<repo_path>" --from "<self>" --to "<architect|advisor>"` 只读获取建议，严禁自行猜改产品契约；
3. 完成前端阶段与全量端到端/视觉验收后，逐张检查新增截图无裁切、重叠、破图或乱码，并更新 `docs/DEVLOG.md`；
4. 全过程禁用 `rm`（弃置文件移入 `~/.Trash/`），不触碰保护目录，提交用 `rtk git` 且不加 AI 署名、不 `git push`；
5. 收口前运行自检并将结构化交卷单写入共享台账：
   python3 ~/.agents/skills/harness-loop/scripts/audit_commits.py --repo "<repo_path>" --base "<base_commit>" --json
   python3 ~/.agents/skills/harness-loop/scripts/loopctl.py record --repo "<repo_path>" --run-id "<run_id>" --role "<claude|codex|agy>" --event delivery --summary "<一句话交付总结>" --commits "<提交链>" --body "<分阶段commit + 门禁实测数字 + 遗留说明>" --json
```

---

## 4. Post-Verifier 返工提示词模板（`NEEDS_FIX` 时使用）

```text
Post-Verifier 独立代码审查与视觉核验发现了以下具体问题，请在当前分支逐一修净并完成回归验证：

1. **[严重度/分类] `<相对路径:行号>` — `<一句话问题>`**
   - 现状与风险：<贴出实际查到的代码/截图/报错证据>
   - 修复要求：<明确改法，不留模糊空间>
   - 补齐测试：<要求补充的单测或 E2E 断言>

...（逐条编号列出）...

完成后请执行：
1. 串行重跑受影响门禁与全量验收：<列出精确命令>
2. 逐张读图复核更新后的截图：<列出截图路径>
3. 更新 `docs/DEVLOG.md` 并本地 `rtk git commit`（无 AI 署名，不 `git push`）
4. 自检并写入新一轮交卷记录：
   python3 ~/.agents/skills/harness-loop/scripts/audit_commits.py --repo "<repo_path>" --base "<base_commit>" --json
   python3 ~/.agents/skills/harness-loop/scripts/loopctl.py record --repo "<repo_path>" --run-id "<run_id>" --role "<claude|codex|agy>" --event delivery --summary "<返工修复完成摘要>" --commits "<新增commit>" --body "<逐条修复对照 + 最新门禁数字>" --json
```
