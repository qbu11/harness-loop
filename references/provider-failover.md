# CC Switch 供应商故障转移与两级会话断点恢复手册

> 面向直连配置、第三方中转站或编码订阅混用场景：支持**随时手动热切**（如主线路延迟过高）与 **`watch --auto-failover` 自动故障转移**，不改写全局配置，不落盘明文密钥，最大限度保住上下文。

---

## 1. 为什么在 `harness-loop` 编排层再做一层转移？

| 层级 | 职责范围 | 典型解决的问题 |
|---|---|---|
| **Layer 1：CC Switch 本地代理** | HTTP 请求级瞬断重试与熔断 | 偶发 `429`/`502`/`503` 时底层接口无感重发（需开启本地代理接管） |
| **Layer 2：`harness-loop` 编排层（本能力）** | **直连热切 + 慢响应人工接管 + 卡死/退出的断点恢复** | ① 直连模式下不改全局配置文件即时换上游；② 主中转响应过慢时**用户指定**切换到官方/另一订阅；③ 断线、超时或被安全规则中断后，按两级策略无缝续传 |

> **只读直读机制**：`loopctl.py` 直接以 SQLite 只读模式（`mode=ro`）读取 `~/.cc-switch/cc-switch.db`，不改写 CC Switch 数据库，也不依赖本地 CLI 版本。

---

## 2. 常用命令与工作流

```bash
# 1. 查看 CC Switch 中已配置的上游列表（脱敏展示，仅显示名称、ID、模型、URL 与认证类型）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py providers list --harness claude

# 2. 为当前闭环设定用户指定的备用链（按顺序尝试；支持 @queue 读取 CC Switch 已入队项）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py providers set-fallback \
  --repo "<repo>" --harness claude --primary "<主上游名>" --providers "<备选1>,<备选2>" --json

# 3. 派活时按需指定主上游或单次备用链
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py delegate \
  --repo "<repo>" --from agy --to claude --provider "<主上游名>" --fallback "<备选1>,<备选2>" \
  --prompt-file task.md --json

# 4. 运行中嫌慢或报异常时：立即手动热切到指定上游并保留断点继续
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py failover \
  --repo "<repo>" --job-id "<job_id>" --to-provider "<备选名>" --reason "主线路响应过慢" --json

# 5. 无人值守守候：卡死(STALLED) / 报错(BLOCKED_OR_FAILED/PROCESS_EXITED_FAILED) 自动顺延切换
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py watch \
  --repo "<repo>" --job-id "<job_id>" --auto-failover --fallback "<备选1>,<备选2>" --json

# 6. 收尾清理运行时目录中的临时隔离配置（覆写后删除）
python3 -B ~/.agents/skills/harness-loop/scripts/loopctl.py providers cleanup --repo "<repo>" --json
```

---

## 3. 两级会话断点恢复机制（Tier 1 → Tier 2）

直连切换上游时，原上游的 `session_id` 可能受限于不同 API 网关的模型签名、thinking 块校验或状态桶而不兼容。`harness-loop` 按两级机制保障不断档：

```text
[1. 零 Token 生成确定性断点] ──▶ 写入 <run_dir>/CHECKPOINT-<old_job>.md 与 .checkpoint.json
       │                          （原任务摘要、相对基线已提交记录、git status、diff --stat、最近对话）
       ▼
[2. Tier 1：原生 --resume 续接] ─▶ 使用隔离 0600 运行时拉起 claude --setting-sources project,local --settings ... --resume <sid>
       │                           或 CODEX_HOME=<isolated> codex exec resume <sid>
       ├─ 探活通过 ──▶ 继续在同一 worktree 与 tmux 窗执行
       └─ 触发 400/签名校验/会话不存在等不兼容报错（或无 --recovery-mode resume 强要求）
              ▼
[3. Tier 2：断点交接降级] ─────▶ 自动以干净新会话启动同一目标上游，注入 CHECKPOINT-*.md 现场从断点续作
```

---

## 4. 安全与防死循环边界

1. **绝不触碰全局默认配置**：每次切换只在 `~/.cache/harness-loop/runtime/<slug>/<job_id>/` 生成独立运行配置（目录 `0700`、凭证文件 `0600`），Claude Code 传 `--setting-sources project,local --settings <file>` 并清空子进程继承的冲突环境变量；Codex 切独立 `CODEX_HOME`（仅只读软链 `sessions/`、`AGENTS.md` 等非密文件）。
2. **仓库与台账零密钥**：`providers list`、`manifest.json`、`events.jsonl`、`DIALOGUE.md`、`LATEST.md` 与 `CHECKPOINT-*.md` 均只留存供应商名称/脱敏 URL，并自动对常见密钥格式（`sk-*`、`Bearer *`）做脱敏拦截。
3. **有限重试上限**：`--max-failovers`（默认 `2`）+ `tried_providers` 去重防回环；耗尽后输出 `failover_exhausted: true` 与断点快照路径并停机，不会无限重试。
4. **门禁兼容**：被替代的旧任务记为终态 `FAILOVER_SWITCHED`，只要新接手任务完成并走通终审，不会阻碍 `signoff`（`DONE`）。
