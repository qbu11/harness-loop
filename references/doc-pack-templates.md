# 文档包与执行提示词模板（Architect 出稿 → 多端执行）

> **铁律**：Architect 在 Phase 1–2 只写规格包与交接文档，不改业务代码；所有文档的接口路径、字段名、`data-testid`、里程碑编号必须 100% 对齐。  
> **提示词编写原则**：分段固定（身份与目录 → 必读文档 → 文件边界与红线 → 交付与门禁 → 汇报格式），每条要求用可检查的动作或命令表述，避免模糊形容词。

---

## 1. 标准文档包矩阵

| 文档 | 定位 | 必填内容 |
|---|---|---|
| `docs/AUDIT-<slug>.md` | 前置审查单（大迭代必填） | ① 核心结论；② 目标用户爽点与卡点；③ 现存数据/架构隐患；④ 本期必做 vs 延后项 |
| `docs/PRD-<slug>.md` | 产品与交互规格 | ① 本期范围与明确不做项；② 不可退化红线（顶栏锚点/主栏数/美学与合规术语）；③ 功能行为、空态、错误文案 |
| `docs/FRONTEND-DESIGN-<slug>.md` | 视觉与前端规格（含 UI 时必填） | ① 浅/深色语义 Token 与对比度；② 字号缩放规则与禁词（如只写「大字版」）；③ ASCII 线框、零毛玻璃（`backdrop-filter: none`）与组件视觉边界 |
| `docs/TECH-SPEC-<slug>.md` | 技术实现 Spec | ① 多里程碑/波次版图；② 向后兼容字段（`#[serde(default)]`/默认值）；③ API 与 CLI 契约；④ 不可破坏的既有不变量（如状态枚举、历史兼容别名）；⑤ 前端 Store/组件分工 |
| `docs/TEST-PLAN-<slug>.md` | 五层验收计划 | ① `U → C → 环境/评分 → S → G → V` 门禁总表（按里程碑列清）；② 各层硬断言；③ **附录 A：受影响旧脚本精确迁移表**；④ **附录 B：新增 `data-testid` 注册表** |
| `docs/HANDOFF-<slug>.md` | **多端施工唯一总纲** | ① Stage 0 基线与禁回退提交表；② 六条工程铁律；③ Wave 并行拓扑 + **严格文件独占所有权表** + 胶水文件名单；④ 串行终验命令；⑤ 各端启动提示词（见 §3） |

---

## 2. `HANDOFF-<slug>.md` 精简骨架

```markdown
# <项目名> · <迭代主题> 并行施工交接手册

> **基线**：YYYY-MM-DD · `<base_commit>`  
> **配套**：PRD / FRONTEND-DESIGN / TECH-SPEC / TEST-PLAN / `AGENTS.md`

## 0. Stage 0：基线与禁回退清单
- 基线工作区干净，包含以上规格文档包。
- 绝对禁止回退的既有行为/路由：<列出 commit hash + 接口/断言，例如兼容别名与老 CLI 路由>。

## 1. 六条工程铁律
1. **按里程碑 CUI-First**：每个里程碑（M1 / M2）必须先合入纯后端与 CLI、跑通单元测试与 `cli-acceptance` 并本地提交，才允许启动该里程碑的前端工作流。
2. **Worktree 文件隔离**：并行子任务在各自独立 `git worktree` 中施工，**只准改分配给本 Wave 的独占文件**；公共胶水文件（路由汇总、`main.rs`/`mod.rs`、`TopBar`/`Workspace`/`api.ts`/`types.ts`）统一留到 Wave 集成步骤接线。复用依赖的软链（如 `frontend/node_modules`）禁止 `git add`。
3. **冻结核心契约与依赖**：禁止私改冻结的状态码/埋点枚举；禁止引入未批准的重型第三方库。
4. **视觉与术语红线**：<填入顶栏上限、主栏上限、禁词与视觉规范>。
5. **保护活体服务与隔离端口**：测试通过隔离运行时脚本完成，严禁 `pkill python` 等误杀本机服务；禁止改动保护数据目录。
6. **命令安全与提交规范**：禁用 `rm`（清理用 `mv ... ~/.Trash/`）；Git 用 `rtk git`；Conventional Commits 中文、无 AI 署名、禁止 `git push`；未指定模型时继承各端全局默认。

## 2. Wave 并行拓扑与文件所有权表

### Wave 1：<M1 纯后端 & 单测（严禁碰前端）>
| 分支 / Worktree | 执行端 | 独占允许修改文件（胶水文件留到集成步） | 核心交付 |
|---|---|---|---|
| `wt-1a` (`feat/1a`) | Claude Code / Codex | `backend/a.rs`, `backend/api/a.rs` | 模块 A + 单测 |
| `wt-1b` (`feat/1b`) | Codex / Claude Code | `backend/b.rs`, `backend/api/b.rs` | 模块 B + 单测 |
| **Wave 1 集成** | 主仓库 `main` | `main.rs`, `api/mod.rs`, `cli.rs`, CLI 验收脚本 | 合入 + CLI + 全门禁 |

### Wave 2：<M1 前端并行 & 终验>
| 分支 / Worktree | 执行端 | 独占允许修改文件 | 核心交付 |
|---|---|---|---|
| `wt-2a` / `wt-2b` | Claude Code | 各模块独立 Store / Component / `.test.ts` | 模块 UI + 单测 |
| **Wave 2 集成** | 主仓库 `main` | `App.tsx`, `TopBar.tsx`, `Workspace.tsx`, `api.ts`, `types.ts`, `e2e/` | 接线 + 旧 E2E 迁移 + 五层门禁 |

## 3. 五层终验串行命令
<按 U → CLI → Smoke → E2E → Visual 逐行贴出精确命令>
```

---

## 3. 执行端启动提示词模板（三选一，人机通用）

> **选用规则**：
> - 派给 **Claude Code** 且属多文件/多步骤任务：保留首行 `ultracode`；单点小改去掉。
> - 派给 **Codex** 或 **AGY**：删除首行 `ultracode`；若 Codex 需拆分多模块流水线，可调用本机 `codex-workflows`。
> - 派发命令：`loopctl.py delegate --workdir <wt> --wave <wave> --label <name>`（默认在 `tmux` 独立窗口可见运行）。

### 模板 A：单线执行官（中小型单里程碑任务）

```text
ultracode

你是本次任务的执行官，工作目录：`<repo_path>`（基线 `<base_commit>`）。

## 1. 先读后改（必须按序阅读）
1. `AGENTS.md`
2. `docs/PRD-<slug>.md`
3. `docs/TECH-SPEC-<slug>.md`
4. `docs/TEST-PLAN-<slug>.md`
5. `docs/HANDOFF-<slug>.md`

## 2. 执行与安全纪律
1. **CUI-First**：先完成全部 `P0*` 后端与 CLI 阶段，跑通单测与 CLI 验收并本地 `rtk git commit`，再改前端源码。
2. **禁止猜改口径**：遇规格歧义或风险点，运行只读咨询：
   `python3 ~/.agents/skills/harness-loop/scripts/loopctl.py consult --repo "<repo_path>" --from <self> --to <architect|advisor> --prompt "<具体问题>" --json`
3. **红线**：禁用 `rm`（改用 `mv <path> ~/.Trash/`）；不动保护目录；不用 `git push`；提交不加 AI 署名；UI 遵循指定视觉与术语规范。

## 3. 交付与收口门禁
1. 完成后端、前端与全部端到端/视觉验收；
2. 逐张检查新生成截图（无遮挡、溢出、破图、乱码）并更新 `docs/DEVLOG.md`；
3. 自检并回写共享台账：
   python3 ~/.agents/skills/harness-loop/scripts/audit_commits.py --repo "<repo_path>" --base "<base_commit>" --stage full --json
   python3 ~/.agents/skills/harness-loop/scripts/loopctl.py record --repo "<repo_path>" --role <claude|codex|agy> --event delivery --summary "<一句话交付结论>" --commits "<hash1..hashN>" --body "<阶段提交 + 各门禁实测数字 + 遗留说明>" --json
```

### 模板 B：Wave 并行子执行官（隔离 Worktree，严守文件边界）

```text
你是 **<Wave-X> 执行官（<Harness/模型>）**，在独立 Worktree **`<worktree_path>`**（分支 `<branch_name>`，基线 `<base_commit>`）完成 **<本路核心职责>**。

## 1. 必读上下文
1. `<worktree_path>/AGENTS.md`
2. `<worktree_path>/docs/TECH-SPEC-<slug>.md`（重点：<精确章节>）
3. `<worktree_path>/docs/TEST-PLAN-<slug>.md`（重点：<精确用例编号，如 U-V9.1>）
4. **先读关联真实源码**：<若为前端，先读真实后端结构/API 响应字段与待改组件；若为后端，先读数据结构与路由规范>

## 2. 严格文件所有权边界（严禁越界）
- **只准新建 / 修改以下文件**：
  - <精确列出允许修改的每一个文件路径及改法范围>
- **严禁触碰**：
  - <列出共享胶水文件，如 `main.rs`, `api/mod.rs`, `Workspace.tsx`, `TopBar.tsx`, `api.ts`, `types.ts`>
  - 其他 Wave 的独占文件；前端任务禁止改后端；后端 `P0` 任务禁止碰 `frontend/`；
  - 禁止将复用软链（如 `frontend/node_modules`）或临时产物纳入 `git add`；
  - 禁用 `rm`、禁止 `git push`、提交信息无 AI 署名。

## 3. 具体交付项
1. <按文件编号列出字段、函数签名、路由、行为与边界条件>
2. <列出必须补齐的单元测试用例与具体断言>

## 4. 验证与沙箱处理
- **本路必跑验证**：
  <列出本模块类型检查、构建、纯单测命令>
- **若使用 Codex `-s workspace-write` 沙箱遇系统限制**：
  1. 沙箱禁止绑定本地端口（如 `127.0.0.1:0`）时：运行 compile/check 与本模块非网络单测（如 `cargo check --tests` + `cargo test -- <module>`），**勿绕过沙箱**；
  2. 沙箱保护 `.git/index.lock` 导致无法 `git commit` 时：保留通过验证的工作区改动并写明待主宿主验证提交。
- **可独立提交时**：核对 `rtk git status --short` 无越界文件后提交：`rtk git commit -m "<commit_msg>"`。

## 5. 收口汇报（写给上游或用户）
仅列出实测事实：分支名、Commit Hash（或「已保存待宿主提交」）、改动文件列表、各验证命令精确通过/失败数、已发现未解决风险。
```

### 模板 C：Wave 集成与里程碑终验官（主仓库收口）

```text
ultracode

你是 **<Wave 集成与 M-X 门禁执行官>**，在主仓库 **`<repo_path>`**（分支 `main`）合并并行分支、完成胶水接线，并跑通 **<当前里程碑>** 全量门禁。

## 1. 先读后合并
1. 核对前置分支状态与各分支实测提交：<列出分支名与预计合入文件>；
2. 逐个合入各子分支，仅在 `main` 上修改以下胶水文件完成接线：
   - <列出路由注册、CLI 子命令、TopBar/Workspace/App 入口、api.ts/types.ts 封装>
3. 严禁修改前置已冻结里程碑的后端/不变量代码（除非修复合入揭示的真实缺陷并附回归测试）。

## 2. 测试迁移与脚本更新
- 按 `docs/TEST-PLAN-<slug>.md` 附录 A 迁移受影响的旧 E2E/视觉断言（**禁止削弱不相关断言**）；
- 补全本里程碑新增 CLI / E2E / 视觉脚本与注册的 `data-testid` 校验。

## 3. 串行跑通门禁再提交
按以下顺序串行执行，任何失败必须修到 0 失败：
<逐行贴出当前阶段命令：CLI 阶段只跑到 cli-acceptance + 环境校验；GUI 阶段跑全 U → C → S → G → V，并在完成后逐张读取新截图确认界面无遮挡与溢出>

## 4. 工程自检与写入台账
python3 ~/.agents/skills/harness-loop/scripts/audit_commits.py --repo "<repo_path>" --base "<base_commit>" --stage <cui|full> --json
python3 ~/.agents/skills/harness-loop/scripts/loopctl.py record --repo "<repo_path>" --role <claude|codex|agy> --event delivery --summary "<一句话交付与实测总结>" --commits "<commit_list>" --body "<阶段Commit对照 + 实测通过数字 + 截图与报告路径>" --json
```

---

## 4. Post-Verifier 返工提示词模板（`NEEDS_FIX`，由 `delegate --rework` 派发）

```text
Post-Verifier 独立审查发现以下问题，请在 `<workdir>` 逐条修复并复验：

1. **[严重度] `<相对路径:行号>` — `<一句话问题>`**
   - 现状与风险：<实际查到的代码/报错/截图证据>
   - 修复要求：<明确改法，不留模糊空间>
   - 补齐测试：<要求补充的单测或 E2E 断言>

完成后请执行：
1. 重跑受影响门禁：<列出精确命令>
2. 若涉 UI，逐张重看受影响截图：<列出路径>
3. 核对洁净后本地 `rtk git commit`（无 AI 署名、不 `git push`）
4. 自检并记入台账：
   python3 ~/.agents/skills/harness-loop/scripts/audit_commits.py --repo "<repo_path>" --base "<base_commit>" --stage <cui|full> --json
   python3 ~/.agents/skills/harness-loop/scripts/loopctl.py record --repo "<repo_path>" --role <claude|codex|agy> --event delivery --summary "<返工完成摘要>" --commits "<新增commit>" --body "<逐条修复对照 + 最新门禁数字>" --json
```
