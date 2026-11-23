# 按订阅额度与剩余寿命智能选端（Quota-Aware Routing）

本分支扩展 `harness-loop`：在开跑前与每轮派活前探测各端剩余额度、重置倒计时与订阅到期日，让用户用极简参数控制 **「省谁的额度」**（`--conserve`）与 **「先烧谁的额度」**（`--prefer` / `--strategy`）。

---

## 1. 复用开源探测层（只读，绝不碰凭据明文）

`quota_router.py` 按以下顺序组合信号，不自造各厂商 OAuth 爬虫，也不读取 CC Switch 的 `settings_config`（API Key）或账号令牌：

| 层级 | 来源（开源引用 / 本机数据） | 获取字段 |
|---|---|---|
| **1. 开源探针 CLI（可选）** | **`quota-axi`**（[`kunchenguid/quota-axi`](https://github.com/kunchenguid/quota-axi)，首选）、**`codexbar`**（[`steipete/CodexBar`](https://github.com/steipete/CodexBar)）、**`cclimits`**（[`cruzanstx/cclimits`](https://github.com/cruzanstx/cclimits)），或自定义 `--probe-cmd` | 剩余 `%`、`resetsAt`、套餐名；其中 `quota-axi` 直接提供 `spendPriority`（正数=按当前流速到重置时会白白作废，越高越该先烧）与 `runway` |
| **2. 本机 Codex 原生遥测** | `~/.codex/sessions/**/rollout-*.jsonl` 末尾 `rate_limits` | 5h / 周窗已用百分比与精确 `resets_at`（窗口已过期自动归零，不误锁） |
| **3. CC Switch 只读叠加** | `~/.cc-switch/cc-switch.db`（`mode=ro`）与 `settings.json` | 当前激活通道名称与倍率、`provider_health` 连续失败惩罚、`proxy_request_logs` 近 5h/24h/7d 花费、日/月 USD 限额、以及 **`codexQuotaProtection`**（如 5h≥95% / 周≥98% 自动硬停保护） |
| **4. 本地订阅快照** | `loopctl.py quota set` 写入 `.agent-loop/quota-snapshot.json` 或 `~/.config/harness-loop/quota-snapshot.json` | 补录 CC Switch 桌面端界面显示的 5h/周窗余量或月度订阅到期日 `expires_at` |

---

## 2. 策略与打分逻辑（透明可解释）

- **硬停保护（`eligible = false`）**：CLI 未安装、剩余 `≤ 5%`、`rate_limited` 或触发 `codexQuotaProtection` 阈值，直接跳过该 Harness。
- **四个核心分数项**：
  1. `abundance`：已知剩余百分比（未知时按 `55.0` 扣减近 24h 花费惩罚）。
  2. `expiry_urgency`：月度订阅到期时间 `expires_at` 在 168h（7 天）内且余量 `≥ 15%`，按剩余占比与临近度最高加 `+45`。
  3. `reset_urgency`：若 `quota-axi` 返回 `spendPriority`，直接换算为 `clamp(spendPriority * 15, -35, +45)`；否则按当前限速窗接近重置程度最高加 `+30`。
  4. `user_adjust`：`--conserve` 减 `45`；`--prefer` 首位加 `35`、次位加 `20`。
- **四种策略（`--strategy`）**：
  - `expiring_first`：`0.8 * abundance + 1.8 * (expiry_urgency + reset_urgency) + user_adjust` —— **优先烧快到期或快重置会作废的富裕额度**。
  - `abundant_first`：`1.2 * abundance + 0.5 * (expiry_urgency + reset_urgency) + user_adjust` —— **优先消耗余量最充沛的一端**。
  - `auto`：`1.0 * abundance + 1.0 * urgency + user_adjust`。
  - `manual`：严格按 `--prefer` 手动顺位派发，同时保留 `--conserve` 与保护停机检查。

---

## 3. 「省 AGY 模式」工作流自动切换

一旦配置 `--conserve agy`，`recommend_roster` 与 `loopctl.py` 会自动建议并开启以下降本组合：

1. **Phase 1 摘要化**：先派 `executor`（如 `codex` / `claude`）只读扫代码并吐出结构化摘要，AGY 直接拍板写规格包，少吞几十个源码文件。
2. **Phase 4 异构交叉审**：派另一端（如 `claude` 审 `codex`、`codex` 审 `claude`）做第一轮全量 Diff 对抗审查，AGY 只抽看标红的 20–40 行并做终裁。
3. **合波集成交给 `claude`**：避免 Codex macOS 沙箱 `.git/index.lock` 与 `127.0.0.1:0` 限制，由 Claude Code 合分支并跑全量门禁，仅回传摘要数字。
4. **精简工具回包 `--brief`**：`init --conserve agy` 默认开启 `brief=true`，让 `watch` 与 `audit_commits.py --brief` 只回传核心状态与文件路径，并只抽检 3–5 张关键/异常截图；每个里程碑可开新对话从 `LATEST.md` 零负担续接。

---

## 4. 常用命令示例

```bash
# 1. 只读探测本机额度与 CC Switch 状态，预览推荐分工（零消耗）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py quota \
  --conserve agy --strategy expiring_first

# 2. 使用已安装的 quota-axi / npx 实时探测官方订阅与 spendPriority
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py quota \
  --probe quota-axi --conserve agy --strategy expiring_first

# 3. 补录一张 CC Switch 桌面端上的临期订阅快照（如 Claude 还剩 72%、72h 后到期）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py quota set \
  --harness claude --remaining 72 --expires-at "2026-11-27T23:59:59+08:00" --plan "Pro" --repo .

# 4. 初始化闭环并自动按额度分配角色、轨道与 --brief（先烧 Codex、省着 AGY）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py init \
  --repo . --slug my-feature --conserve agy --prefer codex \
  --quota-strategy expiring_first --auto-roster --json

# 5. 运行中把策略动态应用到现有闭环（例如 Codex 快撞线，改优先烧 Claude）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py quota \
  --repo . --conserve agy,codex --prefer claude --strategy abundant_first --apply --json
```
