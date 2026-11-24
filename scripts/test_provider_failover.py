#!/usr/bin/env python3
"""Isolated unit and integration tests for CC Switch provider failover & checkpoint recovery."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import provider_failover as pf  # noqa: E402


LOOPCTL_PATH = SCRIPTS_DIR / "loopctl.py"

FAKE_SECRETS = (
    "sk-fake-opencode-secret-111",
    "sk-fake-deepseek-secret-222",
    "sk-fake-kimi-secret-333",
    "sk-fake-codex-backup-a-444",
    "sk-fake-codex-backup-b-555",
)


def _create_synthetic_cc_switch_db(db_path: Path) -> None:
    """Create an isolated SQLite v26 CC Switch database with synthetic test fixtures."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA user_version = 26")
        conn.execute(
            """
            CREATE TABLE providers (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL,
                website_url TEXT,
                category TEXT,
                created_at INTEGER,
                sort_index INTEGER,
                notes TEXT,
                meta TEXT NOT NULL DEFAULT '{}',
                is_current BOOLEAN NOT NULL DEFAULT 0,
                in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
                cost_multiplier TEXT NOT NULL DEFAULT '1.0',
                limit_daily_usd TEXT,
                limit_monthly_usd TEXT,
                provider_type TEXT,
                PRIMARY KEY (id, app_type)
            )
            """
        )
        rows = [
            (
                "prov-gw-primary",
                "claude",
                "Gateway Primary",
                json.dumps(
                    {
                        "model": "deepseek-v4.1-flash[1M]",
                        "env": {
                            "ANTHROPIC_API_KEY": "sk-fake-opencode-secret-111",
                            "ANTHROPIC_BASE_URL": "https://relay.example.invalid/go",
                            "ANTHROPIC_MODEL": "deepseek-v4.1-flash[1M]",
                        },
                    }
                ),
                "https://relay.example.invalid",
                "third_party",
                0,
                "slow relay",
                1,
                1,
            ),
            (
                "prov-ds-official",
                "claude",
                "DeepSeek Official",
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_API_KEY": "sk-fake-deepseek-secret-222",
                            "ANTHROPIC_BASE_URL": "https://api.deepseek.example.invalid/anthropic",
                            "ANTHROPIC_MODEL": "deepseek-v4-flash[1M]",
                        }
                    }
                ),
                "https://api.deepseek.example.invalid",
                "cn_official",
                1,
                "direct official",
                0,
                1,
            ),
            (
                "prov-kimi-plan",
                "claude",
                "Kimi Plan",
                json.dumps(
                    {
                        "model": "kimi-for-coding",
                        "env": {
                            "ANTHROPIC_AUTH_TOKEN": "sk-fake-kimi-secret-333",
                            "ANTHROPIC_BASE_URL": "https://api.kimi.example.invalid/coding/",
                            "ANTHROPIC_MODEL": "kimi-for-coding",
                        },
                    }
                ),
                "https://api.kimi.example.invalid",
                "cn_official",
                2,
                "coding plan",
                0,
                1,
            ),
            (
                "codex-official",
                "codex",
                "OpenAI Official",
                json.dumps({"auth": {}, "config": 'model = "gpt-6.2-sol"\n'}),
                "https://openai.example.invalid",
                "official",
                0,
                None,
                1,
                1,
            ),
            (
                "codex-backup-a",
                "codex",
                "Backup Relay A",
                json.dumps(
                    {
                        "auth": {"OPENAI_API_KEY": "sk-fake-codex-backup-a-444"},
                        "config": (
                            'model_provider = "relay_a"\n'
                            'model = "gpt-6.2-sol"\n'
                            '[model_providers.relay_a]\n'
                            'name = "RelayA"\n'
                            'base_url = "https://relay-a.example.invalid/v1"\n'
                            'wire_api = "responses"\n'
                        ),
                    }
                ),
                "https://relay-a.example.invalid",
                "aggregator",
                1,
                None,
                0,
                1,
            ),
            (
                "codex-backup-b",
                "codex",
                "Backup Relay B",
                json.dumps(
                    {
                        "auth": {"OPENAI_API_KEY": "sk-fake-codex-backup-b-555"},
                        "config": (
                            'model_provider = "relay_b"\n'
                            'model = "gpt-6.2-sol"\n'
                            '[model_providers.relay_b]\n'
                            'name = "RelayB"\n'
                            'base_url = "https://relay-b.example.invalid/v1"\n'
                            'wire_api = "responses"\n'
                        ),
                    }
                ),
                "https://relay-b.example.invalid",
                "aggregator",
                2,
                None,
                0,
                0,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO providers (id, app_type, name, settings_config, website_url, category, sort_index, notes, is_current, in_failover_queue)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _assert_no_secrets_in_text(test_case: unittest.TestCase, text: str, label: str = "output") -> None:
    for secret in FAKE_SECRETS:
        test_case.assertNotIn(secret, text, f"Secret leaked in {label}: {secret}")


def _write_fake_claude_shim(bin_dir: Path) -> Path:
    shim = bin_dir / "claude"
    shim.write_text(
        """#!/usr/bin/env python3
import hashlib, json, os, sys, time
from pathlib import Path

home = Path(os.environ["HOME"])
state_file = home / ".claude-mock" / "state.json"
argv_log = home / ".claude-mock" / "invocations.jsonl"
state_file.parent.mkdir(parents=True, exist_ok=True)

def load_state():
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {"agents": []}

def save_state(st):
    state_file.write_text(json.dumps(st, indent=2))

args = sys.argv[1:]
if not args:
    sys.exit(0)

if args[0] == "--version":
    print("2.1.323-mock (Claude Code)")
    sys.exit(0)

if args[0] == "agents":
    print(json.dumps(load_state()["agents"]))
    sys.exit(0)

if args[0] == "stop":
    bg_id = args[1] if len(args) > 1 else ""
    st = load_state()
    for a in st["agents"]:
        if a.get("id") == bg_id:
            a["status"] = "idle"
            a["state"] = "stopped"
    save_state(st)
    sys.exit(0)

# Parse --bg call
bg = "--bg" in args
resume_sid = ""
settings_file = ""
setting_sources = ""
prompt = args[-1]
i = 0
while i < len(args):
    if args[i] == "--resume" and i + 1 < len(args):
        resume_sid = args[i + 1]
        i += 2
    elif args[i] == "--settings" and i + 1 < len(args):
        settings_file = args[i + 1]
        i += 2
    elif args[i] == "--setting-sources" and i + 1 < len(args):
        setting_sources = args[i + 1]
        i += 2
    else:
        i += 1

cfg_env = {}
if settings_file and Path(settings_file).exists():
    cfg_env = json.loads(Path(settings_file).read_text()).get("env", {})

api_key = cfg_env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
with argv_log.open("a") as fh:
    fh.write(json.dumps({
        "args": args,
        "resume_sid": resume_sid,
        "settings_file": settings_file,
        "setting_sources": setting_sources,
        "api_key_used": api_key,
        "env_anthropic_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    }) + "\\n")

sid = resume_sid or "11111111-2222-3333-4444-555555555555"
bg_id = hashlib.sha1((sid + str(time.time())).encode()).hexdigest()[:8]
cwd = str(Path.cwd().resolve())
encoded_cwd = "".join(ch if ch.isalnum() else "-" for ch in cwd)
proj_dir = home / ".claude" / "projects" / encoded_cwd
proj_dir.mkdir(parents=True, exist_ok=True)
jsonl_path = proj_dir / f"{sid}.jsonl"

if api_key == "sk-fake-opencode-secret-111":
    # Primary stays busy and writes initial turn without finishing
    with jsonl_path.open("a") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "Primary Gateway working..."}]},
        }) + "\\n")
    st = load_state()
    st["agents"] = [a for a in st["agents"] if a.get("id") != bg_id]
    st["agents"].append({"id": bg_id, "sessionId": sid, "cwd": cwd, "status": "busy", "state": "running", "kind": "background"})
    save_state(st)
    print(f"Started background session {bg_id}")
    sys.exit(0)

if api_key == "sk-fake-deepseek-secret-222":
    # Backup succeeds on resume!
    st = load_state()
    st["agents"] = [a for a in st["agents"] if a.get("id") != bg_id]
    st["agents"].append({"id": bg_id, "sessionId": sid, "cwd": cwd, "status": "idle", "state": "done", "kind": "background"})
    save_state(st)
    with jsonl_path.open("a") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.500Z", time.gmtime()),
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Recovered cleanly on DeepSeek Official."}]},
        }) + "\\n")
    print(f"Resumed background session {bg_id}")
    sys.exit(0)

print("Unexpected key", file=sys.stderr)
sys.exit(1)
""",
        encoding="utf-8",
    )
    os.chmod(shim, 0o755)
    return shim


def _write_fake_codex_shim(bin_dir: Path) -> Path:
    shim = bin_dir / "codex"
    shim.write_text(
        """#!/usr/bin/env python3
import json, os, re, sys, time, uuid
from pathlib import Path

home = Path(os.environ["HOME"])
codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
argv_log = home / ".codex-mock" / "invocations.jsonl"
argv_log.parent.mkdir(parents=True, exist_ok=True)

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.171.0-mock")
    sys.exit(0)

is_resume = len(args) >= 2 and args[0] == "exec" and args[1] == "resume"
resume_sid = ""
if is_resume and len(args) >= 4:
    resume_sid = args[-2]
cwd = os.getcwd()
out_file = ""
i = 0
while i < len(args):
    if args[i] == "-C" and i + 1 < len(args):
        cwd = args[i + 1]
        i += 2
    elif args[i] == "-o" and i + 1 < len(args):
        out_file = args[i + 1]
        i += 2
    else:
        i += 1

prompt = args[-1] if args else ""
cfg_text = (codex_home / "config.toml").read_text() if (codex_home / "config.toml").exists() else ""
m_prov = re.search(r'(?m)^\\s*model_provider\\s*=\\s*["\\\']([^"\\\']+)["\\\']', cfg_text)
provider_tag = m_prov.group(1) if m_prov else "official"
behavior_file = home / ".codex-mock" / "behavior.json"
behavior = json.loads(behavior_file.read_text()) if behavior_file.exists() else {}

with argv_log.open("a") as fh:
    fh.write(json.dumps({
        "args": args,
        "is_resume": is_resume,
        "provider_tag": provider_tag,
        "codex_home": str(codex_home),
        "openai_env_leaked": bool(os.environ.get("OPENAI_API_KEY")),
        "prompt_head": prompt[:160],
    }) + "\\n")

sid = resume_sid or str(uuid.uuid4())
print(json.dumps({"type": "thread.started", "thread_id": sid}), flush=True)

sess_dir = codex_home / "sessions"
sess_dir.mkdir(parents=True, exist_ok=True)
rollout = sess_dir / f"rollout-2026-11-24T18-00-00-{sid}.jsonl"
if not rollout.exists():
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(Path(cwd).resolve())}}) + "\\n")

mode = behavior.get(provider_tag, "succeed")
if isinstance(mode, dict):
    delay = float(mode.get("delay", 0))
    if delay > 0:
        time.sleep(delay)
    action = mode.get("resume_action" if is_resume else "fresh_action", "succeed")
else:
    action = mode

if action == "sleep_hang":
    time.sleep(15)
    sys.exit(0)

if action == "fail_primary":
    with rollout.open("a") as fh:
        fh.write(json.dumps({"type": "event_msg", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "payload": {"type": "turn_aborted"}}) + "\\n")
    print("Upstream relay failed 503", file=sys.stderr, flush=True)
    sys.exit(1)

if action == "reject_resume_400":
    print("API Error: HTTP 400 invalid_request_error: invalid signature in thinking block", file=sys.stderr, flush=True)
    with rollout.open("a") as fh:
        fh.write(json.dumps({"type": "event_msg", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "payload": {"type": "turn_aborted"}}) + "\\n")
    sys.exit(1)

if action == "fail_401":
    print("HTTP 401 unauthorized invalid api key", file=sys.stderr, flush=True)
    sys.exit(1)

# succeed
msg = f"Completed on {provider_tag} (resume={is_resume})."
if out_file:
    Path(out_file).write_text(msg + "\\n")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": msg}}), flush=True)
with rollout.open("a") as fh:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.500Z", time.gmtime())
    fh.write(json.dumps({"type": "response_item", "timestamp": ts, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": msg}]}}) + "\\n")
    fh.write(json.dumps({"type": "event_msg", "timestamp": ts, "payload": {"type": "task_complete", "last_agent_message": msg}}) + "\\n")
sys.exit(0)
""",
        encoding="utf-8",
    )
    os.chmod(shim, 0o755)
    return shim


class ProviderFailoverTests(unittest.TestCase):
    def test_cc_switch_v26_readonly_list_resolve_and_zero_secret_leak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            db_path = fake_home / ".cc-switch" / "cc-switch.db"
            _create_synthetic_cc_switch_db(db_path)

            env = dict(os.environ, HOME=str(fake_home), HARNESS_LOOP_CC_SWITCH_DB=str(db_path))
            res = subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "providers", "list", "--harness", "claude", "--json"],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            _assert_no_secrets_in_text(self, res.stdout + res.stderr, "providers list")
            data = json.loads(res.stdout)
            self.assertEqual(data["count"], 3)
            self.assertEqual([p["name"] for p in data["providers"]], ["Gateway Primary", "DeepSeek Official", "Kimi Plan"])
            self.assertEqual(data["providers"][1]["auth_kind"], "api_key")
            self.assertEqual(data["providers"][2]["auth_kind"], "auth_token")

            # Test @queue expansion excludes current provider
            expanded = pf.expand_fallback_specs("claude", "@queue", exclude_ids=["prov-gw-primary"], db_path=db_path)
            self.assertEqual([p["name"] for p in expanded], ["DeepSeek Official", "Kimi Plan"])

            # Unique substring + exact resolution
            hit = pf.resolve_provider("claude", "DeepSeek", db_path=db_path)
            self.assertEqual(hit["id"], "prov-ds-official")
            with self.assertRaises(ValueError):
                pf.resolve_provider("codex", "Backup", db_path=db_path)

    def test_claude_runtime_isolation_and_0600_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            db_path = fake_home / ".cc-switch" / "cc-switch.db"
            _create_synthetic_cc_switch_db(db_path)
            rt_root = root / "runtime"

            user_settings = fake_home / ".claude" / "settings.json"
            user_settings.parent.mkdir(parents=True, exist_ok=True)
            user_settings.write_text(
                json.dumps(
                    {
                        "model": "legacy-user-model",
                        "language": "Chinese",
                        "permissions": {"defaultMode": "bypassPermissions"},
                        "env": {
                            "ANTHROPIC_API_KEY": "sk-fake-opencode-secret-111",
                            "ANTHROPIC_BASE_URL": "https://relay.example.invalid/go",
                            "ANTHROPIC_MODEL": "deepseek-v4.1-flash[1M]",
                            "HTTP_PROXY": "http://127.0.0.1:7897",
                        },
                    }
                ),
                encoding="utf-8",
            )

            rt_ds = pf.materialize_claude_runtime(
                "prov-ds-official",
                "demo-run",
                "job-1",
                db_path=db_path,
                user_settings_path=user_settings,
                runtime_root=rt_root,
            )
            ds_path = Path(rt_ds["settings_file"])
            self.assertEqual(stat.S_IMODE(ds_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(ds_path.parent.stat().st_mode), 0o700)
            ds_cfg = json.loads(ds_path.read_text(encoding="utf-8"))
            self.assertEqual(ds_cfg["model"], "deepseek-v4-flash[1M]")
            self.assertEqual(ds_cfg["env"]["ANTHROPIC_API_KEY"], "sk-fake-deepseek-secret-222")
            self.assertEqual(ds_cfg["env"]["HTTP_PROXY"], "http://127.0.0.1:7897")
            self.assertEqual(ds_cfg["permissions"]["defaultMode"], "bypassPermissions")

            rt_kimi = pf.materialize_claude_runtime(
                "prov-kimi-plan",
                "demo-run",
                "job-2",
                db_path=db_path,
                user_settings_path=user_settings,
                runtime_root=rt_root,
            )
            kimi_cfg = json.loads(Path(rt_kimi["settings_file"]).read_text(encoding="utf-8"))
            self.assertEqual(kimi_cfg["env"].get("ANTHROPIC_AUTH_TOKEN"), "sk-fake-kimi-secret-333")
            self.assertNotIn("ANTHROPIC_API_KEY", kimi_cfg["env"])
            self.assertEqual(kimi_cfg["model"], "kimi-for-coding")

            _assert_no_secrets_in_text(self, json.dumps(rt_ds), "rt_ds metadata")
            self.assertTrue(pf.cleanup_runtime_dir(rt_ds["runtime_dir"], runtime_root=rt_root))
            self.assertFalse(ds_path.exists())

    def test_codex_runtime_isolation_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            db_path = fake_home / ".cc-switch" / "cc-switch.db"
            _create_synthetic_cc_switch_db(db_path)
            rt_root = root / "runtime"
            workdir = root / "repo"
            workdir.mkdir()

            base_codex = fake_home / ".codex"
            (base_codex / "sessions").mkdir(parents=True)
            (base_codex / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            (base_codex / "auth.json").write_text('{"tokens": {"access": "oauth-demo"}}\n', encoding="utf-8")

            rt_a = pf.materialize_codex_runtime(
                "codex-backup-a",
                "demo-run",
                "job-codex-a",
                workdir,
                db_path=db_path,
                base_codex_home=base_codex,
                runtime_root=rt_root,
            )
            ch = Path(rt_a["codex_home"])
            self.assertEqual(stat.S_IMODE(ch.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((ch / "auth.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((ch / "config.toml").stat().st_mode), 0o600)
            self.assertIn(f'[projects."{workdir.resolve()}"]', (ch / "config.toml").read_text(encoding="utf-8"))
            self.assertTrue((ch / "sessions").is_symlink())
            self.assertTrue((ch / "AGENTS.md").is_symlink())
            self.assertIn("OPENAI_API_KEY", rt_a["env_unset"])
            _assert_no_secrets_in_text(self, json.dumps(rt_a), "rt_codex metadata")

            # Empty auth provider falls back cleanly to symlinked base auth.json
            rt_off = pf.materialize_codex_runtime(
                "codex-official",
                "demo-run",
                "job-codex-off",
                workdir,
                db_path=db_path,
                base_codex_home=base_codex,
                runtime_root=rt_root,
            )
            self.assertTrue((Path(rt_off["codex_home"]) / "auth.json").is_symlink())

    def test_checkpoint_builder_captures_git_and_jsonl_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: base"], cwd=repo, check=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=repo, text=True).strip()

            (repo / "app.py").write_text("print('step1')\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-q", "-am", "feat: step 1"], cwd=repo, check=True)
            (repo / "app.py").write_text("print('wip')\n", encoding="utf-8")
            (repo / "notes.txt").write_text("pending work\n", encoding="utf-8")

            cp = pf.build_session_checkpoint(
                job_id="claude-exec-001",
                harness="claude",
                workdir=repo,
                branch="main",
                base_commit=base_sha,
                session_id="sid-1234",
                from_provider="Gateway Primary",
                to_provider="DeepSeek Official",
                reason="Relay latency too high",
                original_prompt_text="Implement step 2 with token sk-fake-opencode-secret-111 redacted",
                recent_turns=[
                    {"role": "assistant", "timestamp": "2026-11-24T18:00:00Z", "tools": ["Edit"], "text": "Bearer abcdefghijklmnop should be scrubbed"}
                ],
                stdout_tail="Error with sk-fake-deepseek-secret-222",
            )
            md = pf.render_checkpoint_markdown(cp)
            self.assertIn("feat: step 1", md)
            self.assertIn("M app.py", md)
            self.assertIn("?? notes.txt", md)
            self.assertIn("app.py |", md)
            _assert_no_secrets_in_text(self, md, "checkpoint markdown")

    def test_manual_failover_tier1_native_resume_and_tier2_auto_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            db_path = fake_home / ".cc-switch" / "cc-switch.db"
            _create_synthetic_cc_switch_db(db_path)
            rt_root = root / "runtime"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            _write_fake_claude_shim(bin_dir)
            _write_fake_codex_shim(bin_dir)

            (fake_home / ".claude").mkdir(parents=True)
            (fake_home / ".claude" / "settings.json").write_text(
                json.dumps({"language": "Chinese", "env": {"ANTHROPIC_API_KEY": "sk-fake-opencode-secret-111"}}),
                encoding="utf-8",
            )
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".codex" / "config.toml").write_text('model = "gpt-6.2-sol"\n', encoding="utf-8")

            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
            (repo / "README.md").write_text("demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: init"], cwd=repo, check=True)

            env = dict(
                os.environ,
                HOME=str(fake_home),
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                HARNESS_LOOP_NO_TMUX="1",
                HARNESS_LOOP_CC_SWITCH_DB=str(db_path),
                HARNESS_LOOP_RUNTIME_DIR=str(rt_root),
                ANTHROPIC_API_KEY="sk-fake-opencode-secret-111",
                OPENAI_API_KEY="sk-fake-opencode-secret-111",
            )

            init_out = json.loads(
                subprocess.check_output(
                    [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "fo-demo", "--json"],
                    env=env, text=True,
                )
            )
            run_dir = Path(init_out["run_dir"])

            # Part A: Claude hot-switch Gateway Primary -> DeepSeek Official via Tier-1 --resume
            d_cc = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--to", "claude",
                        "--provider", "Gateway Primary", "--label", "cc-ui",
                        "--no-ultracode", "--prompt", "build component", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertTrue(d_cc["ok"])
            old_cc_job_id = d_cc["job_id"]

            fo_cc = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "failover",
                        "--repo", str(repo), "--job-id", old_cc_job_id,
                        "--to-provider", "DeepSeek Official",
                        "--reason", "Gateway too slow", "--resume-probe-seconds", "1.5", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertTrue(fo_cc["ok"])
            self.assertEqual(fo_cc["recovery_mode"], "native_resume")
            self.assertFalse(fo_cc["downgraded_from_resume"])
            self.assertTrue(Path(fo_cc["checkpoint_md"]).exists())
            invocations = [json.loads(l) for l in (fake_home / ".claude-mock" / "invocations.jsonl").read_text().splitlines()]
            self.assertEqual(invocations[-1]["api_key_used"], "sk-fake-deepseek-secret-222")
            self.assertEqual(invocations[-1]["env_anthropic_key"], "")
            self.assertEqual(invocations[-1]["setting_sources"], "project,local")
            self.assertEqual(invocations[-1]["resume_sid"], "11111111-2222-3333-4444-555555555555")

            # Part B: Codex Tier-1 400 signature mismatch -> automatic Tier-2 checkpoint handoff on same provider
            mock_behavior = fake_home / ".codex-mock" / "behavior.json"
            mock_behavior.parent.mkdir(parents=True, exist_ok=True)
            mock_behavior.write_text(
                json.dumps(
                    {
                        "official": "sleep_hang",
                        "relay_a": {"resume_action": "reject_resume_400", "fresh_action": "succeed"},
                    }
                ),
                encoding="utf-8",
            )
            d_cx = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--to", "codex",
                        "--provider", "OpenAI Official", "--label", "cx-api",
                        "--prompt", "build backend module", "--json",
                    ],
                    env=env, text=True,
                )
            )
            # Wait until fake codex emits thread.started with session_id before failover
            cx_stdout = Path(d_cx["job"]["stdout_jsonl"])
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if cx_stdout.exists() and "thread.started" in cx_stdout.read_text(encoding="utf-8", errors="replace"):
                    break
                time.sleep(0.05)
            self.assertIn("thread.started", cx_stdout.read_text(encoding="utf-8", errors="replace"))
            fo_cx = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "failover",
                        "--repo", str(repo), "--job-id", d_cx["job_id"],
                        "--to-provider", "Backup Relay A",
                        "--reason", "Primary slow", "--recovery-mode", "auto",
                        "--resume-probe-seconds", "3.0", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertTrue(fo_cx["ok"])
            self.assertEqual(fo_cx["recovery_mode"], "checkpoint_handoff")
            self.assertTrue(fo_cx["downgraded_from_resume"])

            w_cx = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "watch",
                        "--repo", str(repo), "--job-id", fo_cx["new_job"]["job_id"],
                        "--interval", "0.2", "--settle-seconds", "0.1", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(w_cx["reason"], "COMPLETED")
            cx_invocations = [json.loads(l) for l in (fake_home / ".codex-mock" / "invocations.jsonl").read_text().splitlines()]
            self.assertFalse(any(row["openai_env_leaked"] for row in cx_invocations))
            self.assertTrue(any(row["is_resume"] and row["provider_tag"] == "relay_a" for row in cx_invocations))
            self.assertTrue(any((not row["is_resume"]) and row["provider_tag"] == "relay_a" and "断点恢复" in row["prompt_head"] for row in cx_invocations))

            # Ensure zero fake secrets leaked anywhere inside repo ledger/checkpoints/CLI output
            for p in (run_dir / "manifest.json", run_dir / "events.jsonl", run_dir / "DIALOGUE.md", Path(fo_cx["checkpoint_md"])):
                _assert_no_secrets_in_text(self, p.read_text(encoding="utf-8"), p.name)
            _assert_no_secrets_in_text(self, json.dumps(fo_cc) + json.dumps(fo_cx) + json.dumps(w_cx), "cli json")

    def test_watch_auto_failover_tier1_downgrade_and_signoff_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            db_path = fake_home / ".cc-switch" / "cc-switch.db"
            _create_synthetic_cc_switch_db(db_path)
            rt_root = root / "runtime"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            _write_fake_codex_shim(bin_dir)
            (fake_home / ".codex").mkdir(parents=True)
            (fake_home / ".codex" / "config.toml").write_text('model = "gpt-6.2-sol"\n', encoding="utf-8")

            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
            (repo / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "lib.rs"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: init"], cwd=repo, check=True)

            env = dict(
                os.environ,
                HOME=str(fake_home),
                PATH=f"{bin_dir}:{os.environ['PATH']}",
                HARNESS_LOOP_NO_TMUX="1",
                HARNESS_LOOP_CC_SWITCH_DB=str(db_path),
                HARNESS_LOOP_RUNTIME_DIR=str(rt_root),
            )

            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "auto-fo", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "providers", "set-fallback",
                    "--repo", str(repo), "--harness", "codex",
                    "--primary", "OpenAI Official", "--providers", "Backup Relay A,Backup Relay B",
                    "--max-failovers", "3", "--json",
                ],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )

            # Delayed 400 on relay_a --resume ensures _attempt_watch_failover inside watch
            # catches the failure asynchronously and downgrades relay_a to Tier-2 checkpoint!
            mock_behavior = fake_home / ".codex-mock" / "behavior.json"
            mock_behavior.parent.mkdir(parents=True, exist_ok=True)
            mock_behavior.write_text(
                json.dumps(
                    {
                        "official": "fail_primary",
                        "relay_a": {"delay": 0.6, "resume_action": "reject_resume_400", "fresh_action": "succeed"},
                        "relay_b": "succeed",
                    }
                ),
                encoding="utf-8",
            )

            d_out = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--to", "codex",
                        "--label", "auto-task", "--prompt", "implement feature", "--json",
                    ],
                    env=env, text=True,
                )
            )
            w_out = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "watch",
                        "--repo", str(repo), "--job-id", d_out["job_id"],
                        "--auto-failover", "--resume-probe-seconds", "0.2",
                        "--interval", "0.15", "--settle-seconds", "0.1", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(w_out["reason"], "COMPLETED")
            self.assertTrue(w_out["synced_to_ledger"])
            self.assertEqual(len(w_out["failover_history"]), 2)
            self.assertEqual(w_out["failover_history"][0]["recovery_mode"], "native_resume")
            self.assertEqual(w_out["failover_history"][1]["recovery_mode"], "checkpoint_handoff")

            # Old FAILOVER_SWITCHED jobs must not block signoff once recovered job COMPLETED!
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "verifier_audit", "--verdict", "PASS", "--summary", "ok", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "signoff", "--skip-ux", "--summary", "done", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            st_done = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_done["phase"], "DONE")

            # Test max_failovers exhaustion cleanly stops without infinite loop
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "exhaust-fo", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            mock_behavior.write_text(
                json.dumps({"official": "fail_primary", "relay_a": "fail_401", "relay_b": "fail_401"}),
                encoding="utf-8",
            )
            d_ex = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--to", "codex",
                        "--provider", "OpenAI Official", "--label", "ex-task",
                        "--prompt", "will exhaust", "--json",
                    ],
                    env=env, text=True,
                )
            )
            w_ex = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "watch",
                    "--repo", str(repo), "--job-id", d_ex["job_id"],
                    "--auto-failover", "--fallback", "Backup Relay A", "--max-failovers", "1",
                    "--resume-probe-seconds", "0.1", "--interval", "0.15", "--settle-seconds", "0.1", "--json",
                ],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(w_ex.returncode, 1)
            ex_data = json.loads(w_ex.stdout)
            self.assertTrue(ex_data.get("failover_exhausted"))


if __name__ == "__main__":
    unittest.main()
