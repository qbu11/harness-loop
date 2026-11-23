#!/usr/bin/env python3
"""Zero-dependency isolated unit test suite for harness-loop scripts."""

from __future__ import annotations

import json
import os
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

import loopctl  # noqa: E402


LOOPCTL_PATH = SCRIPTS_DIR / "loopctl.py"


class ModelAliasAndArgvTests(unittest.TestCase):
    def test_normalize_model_codex_and_agy(self) -> None:
        cases = [
            ("codex", "Codex GPT 6.2 Sol", "gpt-6.2-sol"),
            ("codex", "GPT-6.2-Sol", "gpt-6.2-sol"),
            ("codex", "GPT 6.2 Sol", "gpt-6.2-sol"),
            ("codex", "6.2-sol", "gpt-6.2-sol"),
            ("codex", "GPT-6.1-Sol", "gpt-6.1-sol"),
            ("codex", "Codex GPT 6.1 Sol", "gpt-6.1-sol"),
            ("codex", "GPT-6-Astra", "gpt-6-astra"),
            ("codex", "Codex Auto Review", "codex-auto-review"),
            ("codex", "default", ""),
            ("codex", None, ""),
            ("agy", "Gemini 4 Neon XHigh", "gemini-4-neon-xhigh"),
            ("agy", "Claude Opus 5.5 High", "claude-opus-5-5-high"),
        ]
        for harness, raw, expected in cases:
            with self.subTest(harness=harness, raw=raw):
                self.assertEqual(loopctl.normalize_model(harness, raw), expected)

    def test_codex_exec_resume_options_precede_positionals(self) -> None:
        sid = "00000000-1111-2222-3333-444444444444"
        for builder, args in (
            (
                loopctl.build_consult_argv,
                ("codex", Path("/tmp/demo"), "check diff"),
            ),
            (
                loopctl.build_delegate_argv,
                ("codex", Path("/tmp/demo"), "fix bug"),
            ),
        ):
            argv = builder(*args, model="gpt-6.2-sol", session_id=sid)
            self.assertEqual(argv[1:3], ["exec", "resume"])
            self.assertLess(argv.index("-m"), argv.index(sid))
            self.assertEqual(argv[-2:], [sid, args[2]])

    def test_ultracode_prefix_applied_by_complexity(self) -> None:
        self.assertTrue(loopctl.apply_ultracode_prefix("do work", True).startswith("ultracode\n\n"))
        self.assertEqual(loopctl.apply_ultracode_prefix("do work", False), "do work")
        self.assertEqual(loopctl.apply_ultracode_prefix("ultracode\n\nalready", True), "ultracode\n\nalready")

    def test_format_all_harness_commands_portable(self) -> None:
        manifest = {
            "repo": "/tmp/sample-repo",
            "sessions": {
                "agy": {"session_id": "aaaa1111-bbbb-cccc-dddd-eeeeffff0000"},
                "claude": {},
                "codex": {"session_id": "11112222-3333-4444-5555-666677778888"},
            },
        }
        cmds = loopctl.format_all_harness_commands(manifest)
        self.assertEqual(cmds["agy"]["uri"], "conversation://aaaa1111-bbbb-cccc-dddd-eeeeffff0000")
        self.assertIn("python3 -B", cmds["agy"]["terminal_view"])
        self.assertIn("harness-loop", cmds["agy"]["terminal_view"])
        self.assertEqual(
            cmds["codex"]["resume"],
            'cd "/tmp/sample-repo" && codex resume 11112222-3333-4444-5555-666677778888',
        )

    def test_recursion_depth_guardrail_and_legacy_fallback(self) -> None:
        orig_new = os.environ.pop("HARNESS_LOOP_DEPTH", None)
        orig_old = os.environ.pop("AGY_LOOP_DEPTH", None)
        try:
            ok, depth, env = loopctl.check_recursion_depth(False)
            self.assertTrue(ok)
            self.assertEqual(depth, 0)
            self.assertEqual(env["HARNESS_LOOP_DEPTH"], "1")
            self.assertEqual(env["AGY_LOOP_DEPTH"], "1")

            os.environ["AGY_LOOP_DEPTH"] = "2"
            ok2, depth2, _ = loopctl.check_recursion_depth(False)
            self.assertFalse(ok2)
            self.assertEqual(depth2, 2)

            os.environ.pop("AGY_LOOP_DEPTH", None)
            os.environ["HARNESS_LOOP_DEPTH"] = "2"
            ok3, depth3, env3 = loopctl.check_recursion_depth(True)
            self.assertTrue(ok3)
            self.assertEqual(depth3, 2)
            self.assertEqual(env3["HARNESS_LOOP_DEPTH"], "3")
        finally:
            os.environ.pop("HARNESS_LOOP_DEPTH", None)
            os.environ.pop("AGY_LOOP_DEPTH", None)
            if orig_new is not None:
                os.environ["HARNESS_LOOP_DEPTH"] = orig_new
            if orig_old is not None:
                os.environ["AGY_LOOP_DEPTH"] = orig_old


class CliWorkflowAndSnippetTests(unittest.TestCase):
    def test_init_roster_status_view_and_snippet_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            repo = Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            env = dict(os.environ, HOME=str(fake_home))

            init_res = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH),
                    "init", "--repo", str(repo), "--slug", "unit-smoke",
                    "--architect", "agy", "--executor", "codex:Codex GPT 6.2 Sol", "--verifier", "claude",
                    "--json",
                ],
                env=env, capture_output=True, text=True, check=True,
            )
            init_data = json.loads(init_res.stdout)
            self.assertEqual(init_data["manifest"]["roster"]["executor"], {"harness": "codex", "model": "gpt-6.2-sol"})
            self.assertTrue((repo / ".agent-loop" / "LATEST.md").exists())

            roster_res = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH),
                    "set-roster", "--repo", str(repo), "--advisor", "codex:Codex Auto Review", "--json",
                ],
                env=env, capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(roster_res.stdout)["roster"]["advisor"], {"harness": "codex", "model": "codex-auto-review"})

            status_res = subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo), "--json"],
                env=env, capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(status_res.stdout)["slug"], "unit-smoke")

            view_res = subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "view", "--repo", str(repo), "--target", "dialogue"],
                env=env, capture_output=True, text=True, check=True,
            )
            self.assertIn("unit-smoke", view_res.stdout)

            # snippet --append is idempotent
            s1 = json.loads(
                subprocess.run(
                    [sys.executable, "-B", str(LOOPCTL_PATH), "snippet", "--append", str(repo), "--json"],
                    env=env, capture_output=True, text=True, check=True,
                ).stdout
            )
            self.assertEqual(s1["status"], "created")
            s2 = json.loads(
                subprocess.run(
                    [sys.executable, "-B", str(LOOPCTL_PATH), "snippet", "--append", str(repo), "--json"],
                    env=env, capture_output=True, text=True, check=True,
                ).stdout
            )
            self.assertEqual(s2["status"], "already_present")
            agents_txt = (repo / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(agents_txt.count("## 多 Harness 协作角色"), 1)
            self.assertIn("harness-loop", agents_txt)

    def test_doctor_json_schema(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            env = dict(os.environ, HOME=str(fake_home))
            res = subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "doctor", "--json"],
                env=env, capture_output=True, text=True, check=True,
            )
            doc = json.loads(res.stdout)
            self.assertTrue(doc["ok"])
            self.assertIn("binaries", doc)
            self.assertIn("skill_mounts", doc)
            self.assertIn("agy_brain_roots", doc)
            self.assertIn("harness-loop", doc["skill_mounts"]["canonical"]["path"])
            self.assertEqual(Path(doc["skill_root"]).resolve(), SCRIPTS_DIR.parent.resolve())


class WatchAndAuditTests(unittest.TestCase):
    def test_watch_detects_process_crash_and_zombie(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            repo = Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            env = dict(os.environ, HOME=str(fake_home))
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "crash", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            # Parent intentionally does not wait until watch finishes to test zombie-reaping protection
            p_fail = subprocess.Popen([sys.executable, "-c", "import time,sys; time.sleep(0.35); sys.exit(1)"])
            try:
                res = subprocess.run(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "watch",
                        "--repo", str(repo), "--harness", "codex", "--pid", str(p_fail.pid),
                        "--interval", "0.2", "--settle-seconds", "0.2", "--stall-timeout", "5", "--json",
                    ],
                    env=env, capture_output=True, text=True, check=False,
                )
            finally:
                p_fail.wait(timeout=5)
            data = json.loads(res.stdout)
            self.assertEqual(res.returncode, 1)
            self.assertEqual(data["reason"], "PROCESS_EXITED_FAILED")

    def test_watch_stall_and_stale_rollout_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            repo = Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

            sess_dir = fake_home / ".codex" / "sessions" / "2026" / "11" / "23"
            sess_dir.mkdir(parents=True)
            old_sid = "11111111-2222-3333-4444-555555555555"
            old_rollout = sess_dir / f"rollout-2026-11-23T10-00-00-{old_sid}.jsonl"
            old_ts = "2026-11-23T02:00:00Z"
            old_rollout.write_text(
                "\n".join([
                    json.dumps({"timestamp": old_ts, "type": "session_meta", "payload": {"id": old_sid, "cwd": str(repo.resolve())}}),
                    json.dumps({"timestamp": old_ts, "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "stale"}}),
                ]) + "\n",
                encoding="utf-8",
            )
            stale_epoch = time.time() - 600
            os.utime(old_rollout, (stale_epoch, stale_epoch))

            env = dict(os.environ, HOME=str(fake_home))
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "stale", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "bind-session", "--repo", str(repo), "--harness", "codex", "--session-id", old_sid],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            res = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "watch",
                    "--repo", str(repo), "--harness", "codex", "--session-id", old_sid,
                    "--since-epoch", str(time.time()), "--interval", "0.2",
                    "--stall-timeout", "1", "--timeout", "3", "--json",
                ],
                env=env, capture_output=True, text=True, check=False,
            )
            data = json.loads(res.stdout)
            self.assertEqual(res.returncode, 2)
            self.assertEqual(data["reason"], "STALLED")

    def test_audit_commits_clean_cui_first(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / "README.md").write_text("init\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "chore: base"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            (repo / "cli").mkdir()
            (repo / "cli" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "cli/app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "feat(p0): add cli entrypoint"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            (repo / "frontend" / "src").mkdir(parents=True)
            (repo / "frontend" / "src" / "App.tsx").write_text("export {};\n", encoding="utf-8")
            subprocess.run(["git", "add", "frontend/src/App.tsx"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "feat: add frontend view"], cwd=repo, check=True, stdout=subprocess.DEVNULL)

            report = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS_DIR / "audit_commits.py"), "--repo", str(repo), "--base", base_sha, "--json"],
                capture_output=True, text=True, check=True,
            )
            data = json.loads(report.stdout)
            self.assertTrue(data["ok"], data)
            self.assertEqual(data["failed"], 0, data)


if __name__ == "__main__":
    unittest.main()
