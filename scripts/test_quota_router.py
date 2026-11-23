#!/usr/bin/env python3
"""Isolated unit and integration tests for quota_router, loopctl quota commands, and --brief modes."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import quota_router  # noqa: E402

LOOPCTL_PATH = SCRIPTS_DIR / "loopctl.py"
AUDIT_PATH = SCRIPTS_DIR / "audit_commits.py"


def _create_fake_cc_switch(cc_dir: Path, now_ep: float, *, secret_marker: str = "SUPER-SECRET-TOKEN-DO-NOT-READ") -> None:
    cc_dir.mkdir(parents=True, exist_ok=True)
    (cc_dir / "settings.json").write_text(
        json.dumps(
            {
                "codexQuotaProtection": {
                    "enabled": True,
                    "fiveHourThreshold": 95,
                    "weeklyThreshold": 98,
                }
            }
        ),
        encoding="utf-8",
    )
    db = cc_dir / "cc-switch.db"
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE providers (
            id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL,
            settings_config TEXT NOT NULL, category TEXT, cost_multiplier TEXT NOT NULL DEFAULT '1.0',
            limit_daily_usd TEXT, limit_monthly_usd TEXT, meta TEXT NOT NULL DEFAULT '{}',
            is_current BOOLEAN NOT NULL DEFAULT 0,
            PRIMARY KEY (id, app_type)
        );
        CREATE TABLE provider_health (
            provider_id TEXT NOT NULL, app_type TEXT NOT NULL, is_healthy INTEGER NOT NULL DEFAULT 1,
            consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY (provider_id, app_type)
        );
        CREATE TABLE proxy_request_logs (
            request_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, app_type TEXT NOT NULL,
            model TEXT NOT NULL, total_cost_usd TEXT NOT NULL DEFAULT '0', created_at INTEGER NOT NULL
        );
        """
    )
    sec_json = json.dumps({"env": {"API_KEY": secret_marker}})
    cur.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p-codex", "codex", "OpenAI Official", sec_json, "official", "1.0", None, None, "{}", 1),
    )
    cur.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p-claude", "claude", "Claude Sub", sec_json, "official", "1.0", "50.0", None, "{}", 1),
    )
    cur.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("p-agy", "agy", "default", sec_json, "official", "1.0", None, None, "{}", 1),
    )
    cur.execute(
        "INSERT INTO provider_health VALUES (?, ?, ?, ?, ?)",
        ("p-claude", "claude", 1, 0, None),
    )
    cur.execute(
        "INSERT INTO provider_health VALUES (?, ?, ?, ?, ?)",
        ("p-codex", "codex", 1, 0, None),
    )
    cur.execute(
        "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?)",
        ("req-1", "p-claude", "claude", "claude-sonnet", "10.0", int(now_ep - 600)),
    )
    cur.execute(
        "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?)",
        ("req-2", "p-codex", "codex", "gpt-6.2-sol", "3.5", int(now_ep - 300)),
    )
    con.commit()
    con.close()


class QuotaAdapterAndScoringTests(unittest.TestCase):
    def test_parse_quota_axi_codexbar_cclimits_and_durations(self) -> None:
        now_ep = 1800000000.0
        self.assertAlmostEqual(quota_router.parse_duration_to_hours("3d17h", now_epoch=now_ep), 89.0, places=2)
        self.assertAlmostEqual(quota_router.parse_duration_to_hours("2h 15m", now_epoch=now_ep), 2.25, places=2)
        self.assertAlmostEqual(quota_router.parse_duration_to_hours(now_ep + 7200, now_epoch=now_ep), 2.0, places=2)

        q_payload = {
            "generatedAt": "2026-11-24T00:00:00.000Z",
            "schemaVersion": 5,
            "providers": [
                {
                    "provider": "claude",
                    "plan": "pro",
                    "state": {"status": "fresh", "stale": False},
                    "windows": [
                        {"id": "five_hour", "label": "session", "kind": "session", "percentRemaining": 82, "resetsAt": quota_router._now_iso(now_ep + 7200)},
                        {"id": "seven_day", "label": "week", "kind": "weekly", "percentRemaining": 64, "resetsAt": quota_router._now_iso(now_ep + 300000)},
                    ],
                    "quotaSemantics": {
                        "status": "known",
                        "effectiveAvailability": [
                            {
                                "scope": "all_models",
                                "status": "known",
                                "effectivePercentRemaining": 64,
                                "limitingWindowIds": ["seven_day"],
                                "runway": {"status": "projected_exhaustion", "usableRunwaySeconds": 298906},
                                "selection": {"status": "known", "spendPriority": -0.3798},
                            }
                        ],
                    },
                },
                {
                    "provider": "agy",
                    "state": {"status": "fresh", "stale": False},
                    "windows": [],
                    "quotaSemantics": {
                        "status": "known",
                        "effectiveAvailability": [
                            {
                                "scope": "gemini",
                                "status": "known",
                                "effectivePercentRemaining": 88,
                                "limitingWindowIds": [],
                                "selection": {"status": "known", "spendPriority": 0.4102},
                            },
                            {
                                "scope": "claude_gpt",
                                "status": "known",
                                "effectivePercentRemaining": 90,
                                "limitingWindowIds": [],
                                "selection": {"status": "known", "spendPriority": 0.3687},
                            },
                        ],
                    },
                },
            ],
        }
        parsed_q = quota_router.parse_external_probe_json(q_payload, now_epoch=now_ep)
        self.assertEqual(parsed_q["claude"]["remaining_pct"], 64.0)
        self.assertAlmostEqual(parsed_q["claude"]["spend_priority"], -0.3798, places=4)
        self.assertEqual(parsed_q["agy"]["remaining_pct"], 88.0)
        self.assertIn("claude_gpt", parsed_q["agy"]["scopes"])

        cb_payload = [
            {
                "provider": "codex",
                "plan": "Pro",
                "usage": {
                    "primary": {"usedPercent": 15.0, "windowMinutes": 300, "resetsAt": quota_router._now_iso(now_ep + 3600)},
                    "secondary": {"usedPercent": 40.0, "windowMinutes": 10080, "resetsAt": quota_router._now_iso(now_ep + 86400)},
                    "resetCredits": {"available": 1, "nextExpiresAt": quota_router._now_iso(now_ep + 7200)},
                },
            }
        ]
        parsed_cb = quota_router.parse_external_probe_json(cb_payload, now_epoch=now_ep)
        self.assertEqual(parsed_cb["codex"]["source"], "codexbar")
        self.assertEqual(parsed_cb["codex"]["remaining_pct"], 60.0)
        self.assertAlmostEqual(parsed_cb["codex"]["hours_to_expiry"], 2.0, places=2)

        cc_payload = {
            "claude": {"ok": True, "limits": [{"label": "5h", "percent": 15.2}, {"label": "7d", "percent": 42.0}], "resets": {"5h": "3h24m", "7d": "2d1h"}},
            "codex": {"ok": True, "limits": [{"label": "5h", "percent": 5.0}], "resets": {"5h": "1h"}},
        }
        parsed_cc = quota_router.parse_cclimits_json(cc_payload, now_epoch=now_ep)
        self.assertAlmostEqual(parsed_cc["claude"]["remaining_pct"], 58.0, places=1)
        self.assertAlmostEqual(parsed_cc["codex"]["remaining_pct"], 95.0, places=1)

    def test_cc_switch_sqlite_zero_secret_and_codex_protection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now_ep = time.time()
            cc_dir = root / ".cc-switch"
            secret = "SK-SECRET-XYZ-987654"
            _create_fake_cc_switch(cc_dir, now_ep, secret_marker=secret)

            sess_dir = root / ".codex" / "sessions" / "2026" / "11" / "24"
            sess_dir.mkdir(parents=True)
            rollout = sess_dir / "rollout-2026-11-24T01-00-00-01a20000-0000-0000-0000-000000000001.jsonl"
            rollout.write_text(
                json.dumps(
                    {
                        "timestamp": quota_router._now_iso(now_ep - 30),
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "plan_type": "pro",
                                "primary": {"used_percent": 96.0, "window_minutes": 300, "resets_at": int(now_ep + 3600)},
                                "secondary": {"used_percent": 25.0, "window_minutes": 10080, "resets_at": int(now_ep + 200000)},
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            collected = quota_router.collect_quota_state(
                probe="local",
                cc_switch_dir=cc_dir,
                codex_sessions_root=root / ".codex" / "sessions",
                claude_home=root / ".claude",
                binaries_available={"agy": True, "claude": True, "codex": True},
                now_epoch=now_ep,
            )
            dumped = json.dumps(collected, ensure_ascii=False)
            self.assertNotIn(secret, dumped)

            codex_st = collected["harnesses"]["codex"]
            self.assertTrue(codex_st["protected_stop"])
            self.assertIn("codexQuotaProtection", codex_st["protected_reason"])

            claude_st = collected["harnesses"]["claude"]
            # $10 spent out of $50 daily limit -> 80% remaining
            self.assertEqual(claude_st["remaining_pct"], 80.0)
            self.assertEqual(claude_st["provider_name"], "Claude Sub")

            ranked = quota_router.score_harnesses(collected, conserve=["agy"], prefer=["codex"], strategy="expiring_first")
            rec = quota_router.recommend_roster(ranked, conserve=["agy"], prefer=["codex"], strategy="expiring_first")
            # Codex must be skipped despite --prefer codex because codexQuotaProtection triggered.
            self.assertEqual(rec["executor"], "claude")
            self.assertTrue(rec["brief_recommended"])

    def test_scoring_strategies_and_user_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            now_ep = 1800000000.0
            bins = {"agy": True, "claude": True, "codex": True}

            # Case A: quota-axi spendPriority under expiring_first (positive spendPriority wins)
            probe_script = root / "mock_probe.py"
            probe_data = {
                "schemaVersion": 5,
                "providers": [
                    {
                        "provider": "claude",
                        "state": {"status": "fresh"},
                        "windows": [],
                        "quotaSemantics": {
                            "effectiveAvailability": [
                                {"scope": "all_models", "status": "known", "effectivePercentRemaining": 60, "selection": {"spendPriority": 1.8}}
                            ]
                        },
                    },
                    {
                        "provider": "codex",
                        "state": {"status": "fresh"},
                        "windows": [],
                        "quotaSemantics": {
                            "effectiveAvailability": [
                                {"scope": "all_models", "status": "known", "effectivePercentRemaining": 85, "selection": {"spendPriority": -0.8}}
                            ]
                        },
                    },
                    {
                        "provider": "agy",
                        "state": {"status": "fresh"},
                        "windows": [],
                        "quotaSemantics": {
                            "effectiveAvailability": [
                                {"scope": "all_models", "status": "known", "effectivePercentRemaining": 90, "selection": {"spendPriority": 0.2}}
                            ]
                        },
                    },
                ],
            }
            probe_script.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({probe_data!r}))\n", encoding="utf-8")
            probe_script.chmod(0o755)

            collected = quota_router.collect_quota_state(
                probe_cmd=str(probe_script),
                cc_switch_dir=root / "none-cc",
                codex_sessions_root=root / "none-codex",
                claude_home=root / "none-claude",
                binaries_available=bins,
                now_epoch=now_ep,
            )
            ranked_exp = quota_router.score_harnesses(collected, conserve=["agy"], strategy="expiring_first")
            self.assertEqual(ranked_exp[0]["harness"], "claude")

            ranked_ab = quota_router.score_harnesses(collected, conserve=["agy"], strategy="abundant_first")
            self.assertEqual(ranked_ab[0]["harness"], "codex")

            rec_conserve_agy = quota_router.recommend_roster(ranked_ab, conserve=["agy"], prefer=["codex"], strategy="abundant_first")
            self.assertEqual(rec_conserve_agy["executor"], "codex")
            self.assertEqual(rec_conserve_agy["advisor"], "claude")
            self.assertEqual(rec_conserve_agy["gatekeeper"], "claude")
            self.assertTrue(rec_conserve_agy["brief_recommended"])

            # Case B: fallback billing expiry urgency (55% expiring in 24h beats non-expiring 85% pool)
            snap_file = root / "snap.json"
            quota_router.update_quota_snapshot(
                snap_file, "claude", remaining_pct=55, expires_at=quota_router._now_iso(now_ep + 24 * 3600), now_epoch=now_ep
            )
            quota_router.update_quota_snapshot(snap_file, "codex", remaining_pct=85, now_epoch=now_ep)
            quota_router.update_quota_snapshot(snap_file, "agy", remaining_pct=90, now_epoch=now_ep)
            collected_b = quota_router.collect_quota_state(
                probe="local",
                snapshot_path=snap_file,
                cc_switch_dir=root / "none-cc",
                codex_sessions_root=root / "none-codex",
                claude_home=root / "none-claude",
                binaries_available=bins,
                now_epoch=now_ep,
            )
            ranked_b = quota_router.score_harnesses(collected_b, conserve=["agy"], strategy="expiring_first")
            self.assertEqual(ranked_b[0]["harness"], "claude")
            rec_codex_conserve = quota_router.recommend_roster(
                quota_router.score_harnesses(collected_b, conserve=["codex"], prefer=["claude"], strategy="auto"),
                conserve=["codex"],
                prefer=["claude"],
                strategy="auto",
            )
            self.assertEqual(rec_codex_conserve["executor"], "claude")


class QuotaCliAndBriefIntegrationTests(unittest.TestCase):
    def test_loopctl_quota_init_delegate_apply_and_brief_watch_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_bin = root / "bin"
            repo = root / "repo"
            fake_bin.mkdir(parents=True)
            repo.mkdir(parents=True)

            # Create mock claude, codex, and agy binaries in fake_bin
            for bname in ("agy", "claude", "codex"):
                bp = fake_bin / bname
                bp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                bp.chmod(0o755)

            subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            base_sha = subprocess.check_output(["git", "commit", "-q", "-m", "chore: init"], cwd=repo, text=True)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            _create_fake_cc_switch(fake_home / ".cc-switch", time.time())
            env = dict(os.environ, HOME=str(fake_home), PATH=f"{fake_bin}:{os.environ.get('PATH', '')}")

            subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "quota", "set",
                    "--repo", str(repo), "--harness", "codex", "--remaining", "88", "--resets-in", "2h", "--json",
                ],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )

            init_out = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "init",
                        "--repo", str(repo), "--slug", "quota-demo",
                        "--conserve", "agy", "--prefer", "codex",
                        "--quota-strategy", "expiring_first", "--probe", "local", "--auto-roster", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(init_out["manifest"]["roster"]["executor"]["harness"], "codex")
            self.assertEqual(init_out["manifest"]["roster"]["advisor"]["harness"], "claude")
            self.assertTrue(init_out["manifest"]["quota_policy"]["brief"])

            # delegate without --to should auto-route to codex via quota_router
            d_auto = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--prompt", "implement api", "--dry-run", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(d_auto["to"], "codex")
            self.assertEqual(d_auto["routing_source"], "quota_router:expiring_first")

            # Re-apply conserving both agy & codex and preferring claude
            q_apply = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "quota",
                        "--repo", str(repo), "--probe", "local",
                        "--conserve", "agy,codex", "--prefer", "claude",
                        "--strategy", "manual", "--apply", "--brief", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertTrue(q_apply["applied"])
            self.assertEqual(q_apply["recommendation"]["executor"], "claude")

            d_after = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--prompt", "now claude", "--dry-run", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(d_after["to"], "claude")

            # Verify watch --brief truncates verbose assistant output and omits commands
            sess_dir = fake_home / ".codex" / "sessions" / "2026" / "11" / "24"
            sess_dir.mkdir(parents=True)
            sid = "33333333-4444-5555-6666-777777777777"
            long_reply = "\n".join([f"Line {i}: " + ("x" * 70) for i in range(1, 12)])
            ts_now = quota_router._now_iso(time.time() + 1.0)
            roll = sess_dir / f"rollout-2026-11-24T01-00-00-{sid}.jsonl"
            roll.write_text(
                "\n".join([
                    json.dumps({"timestamp": ts_now, "type": "session_meta", "payload": {"id": sid, "cwd": str(repo.resolve())}}),
                    json.dumps({"timestamp": ts_now, "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": long_reply}}),
                ]) + "\n",
                encoding="utf-8",
            )
            w_brief = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "watch",
                        "--repo", str(repo), "--harness", "codex", "--session-id", sid,
                        "--since-epoch", str(time.time() - 5),
                        "--interval", "0.2", "--settle-seconds", "0.1", "--brief", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertTrue(w_brief["brief"])
            self.assertNotIn("commands", w_brief)
            self.assertTrue(w_brief["last_assistant_message"]["truncated"])
            self.assertLessEqual(len(w_brief["last_assistant_message"]["text"]), 340)

            # Verify audit_commits.py --brief on a clean pass
            (repo / "backend").mkdir()
            (repo / "backend" / "app.rs").write_text("// ok\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "feat(p0): backend ready"], cwd=repo, check=True)
            audit_brief = json.loads(
                subprocess.check_output(
                    [sys.executable, "-B", str(AUDIT_PATH), "--repo", str(repo), "--base", base_sha, "--stage", "cui", "--skip-worktree-check", "--brief", "--json"],
                    env=env, text=True,
                )
            )
            self.assertTrue(audit_brief["ok"])
            self.assertTrue(audit_brief["brief"])
            self.assertNotIn("checks", audit_brief)


if __name__ == "__main__":
    unittest.main()
