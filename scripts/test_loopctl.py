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

    def test_codex_reasoning_effort_argv_and_dry_run(self) -> None:
        sid = "00000000-1111-2222-3333-444444444444"
        c_argv = loopctl.build_consult_argv(
            "codex",
            Path("/tmp/demo"),
            "review diff",
            model="gpt-6.2-sol",
            reasoning_effort="high",
        )
        self.assertIn("-m", c_argv)
        self.assertEqual(c_argv[c_argv.index("-m") + 1], "gpt-6.2-sol")
        self.assertIn("-c", c_argv)
        self.assertEqual(c_argv[c_argv.index("-c") + 1], 'model_reasoning_effort="high"')
        self.assertLess(c_argv.index("-c"), c_argv.index("review diff"))

        c_res_argv = loopctl.build_consult_argv(
            "codex",
            Path("/tmp/demo"),
            "round 2",
            model="gpt-6.2-sol",
            session_id=sid,
            reasoning_effort="xhigh",
        )
        self.assertEqual(c_res_argv[1:3], ["exec", "resume"])
        self.assertIn("-c", c_res_argv)
        self.assertEqual(c_res_argv[c_res_argv.index("-c") + 1], 'model_reasoning_effort="xhigh"')
        self.assertLess(c_res_argv.index("-c"), c_res_argv.index(sid))
        self.assertEqual(c_res_argv[-2:], [sid, "round 2"])

        d_argv = loopctl.build_delegate_argv(
            "codex",
            Path("/tmp/demo"),
            "impl",
            model="gpt-6.2-sol",
            reasoning_effort="xhigh",
        )
        self.assertIn("-c", d_argv)
        self.assertEqual(d_argv[d_argv.index("-c") + 1], 'model_reasoning_effort="xhigh"')
        self.assertLess(d_argv.index("-c"), d_argv.index("impl"))

        for harness in ("agy", "claude"):
            non_codex_c = loopctl.build_consult_argv(
                harness,
                Path("/tmp/demo"),
                "check",
                reasoning_effort="xhigh",
            )
            non_codex_d = loopctl.build_delegate_argv(
                harness,
                Path("/tmp/demo"),
                "do work",
                reasoning_effort="high",
            )
            self.assertNotIn("-c", non_codex_c)
            self.assertNotIn("-c", non_codex_d)
            self.assertEqual(non_codex_c[-1], "check")
            self.assertEqual(non_codex_d[-1], "do work")

        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            repo = Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            env = dict(os.environ, HOME=str(fake_home))

            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "effort-smoke", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            dry_c = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "consult",
                    "--repo", str(repo), "--from", "agy", "--to", "codex",
                    "--model", "6.2-sol", "--reasoning-effort", "xhigh",
                    "--prompt", "review", "--dry-run", "--json",
                ],
                env=env, capture_output=True, text=True, check=True,
            )
            cdata = json.loads(dry_c.stdout)
            self.assertTrue(cdata["ok"])
            self.assertEqual(cdata["model"], "gpt-6.2-sol")
            self.assertEqual(cdata["reasoning_effort"], "xhigh")
            self.assertIn("-c", cdata["argv"])
            self.assertEqual(cdata["argv"][cdata["argv"].index("-c") + 1], 'model_reasoning_effort="xhigh"')

            dry_d = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                    "--repo", str(repo), "--from", "agy", "--to", "codex",
                    "--model", "6.2-sol", "--reasoning-effort", "high",
                    "--prompt", "implement module", "--dry-run", "--json",
                ],
                env=env, capture_output=True, text=True, check=True,
            )
            ddata = json.loads(dry_d.stdout)
            self.assertTrue(ddata["ok"])
            self.assertEqual(ddata["model"], "gpt-6.2-sol")
            self.assertEqual(ddata["reasoning_effort"], "high")
            self.assertIn("-c", ddata["argv"])
            self.assertEqual(ddata["argv"][ddata["argv"].index("-c") + 1], 'model_reasoning_effort="high"')

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
            self.assertIn("tmux", doc["binaries"])
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

    def test_audit_commits_multimilestone_cui_stage_and_m2_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "good-ms"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

            def _commit(msg: str, offset_sec: int) -> str:
                env = dict(os.environ, GIT_AUTHOR_DATE=f"{1700000000 + offset_sec} +0000", GIT_COMMITTER_DATE=f"{1700000000 + offset_sec} +0000")
                subprocess.run(["git", "add", "."], cwd=repo, check=True)
                subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, env=env, check=True)
                return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

            (repo / "README.md").write_text("base\n", encoding="utf-8")
            base_sha = _commit("chore: base", 0)

            (repo / "backend").mkdir()
            (repo / "backend" / "a.rs").write_text("// m1 backend\n", encoding="utf-8")
            _commit("feat: M1-P0 backend api", 100)

            (repo / "e2e" / "reports").mkdir(parents=True)
            (repo / "e2e" / "reports" / "cli-acceptance.json").write_text(json.dumps({"passed": 10, "failed": 0}), encoding="utf-8")
            _commit("test: M1 cli report", 120)

            (repo / "frontend" / "src").mkdir(parents=True)
            (repo / "frontend" / "src" / "App.tsx").write_text("export {};\n", encoding="utf-8")
            _commit("feat: M1-GUI app shell", 200)

            (repo / "e2e" / "reports" / "visual-acceptance.json").write_text(json.dumps({"passed": 20, "failed": 0}), encoding="utf-8")
            _commit("test: M1 visual report", 220)

            (repo / "backend" / "b.rs").write_text("// m2 backend\n", encoding="utf-8")
            _commit("feat: M2-P0 triage engine", 300)

            (repo / "e2e" / "reports" / "v2-m2-cli.json").write_text(json.dumps({"passed": 15, "failed": 0}), encoding="utf-8")
            _commit("test: M2 cli acceptance", 320)

            cui_rep = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS_DIR / "audit_commits.py"), "--repo", str(repo), "--base", base_sha, "--stage", "cui", "--json"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(cui_rep.returncode, 0, cui_rep.stdout)
            cui_data = json.loads(cui_rep.stdout)
            self.assertTrue(cui_data["ok"], cui_data)
            self.assertEqual(cui_data["checks"][3]["milestone_orders"], {"M1": "ok", "M2": "ok"})

            full_rep = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS_DIR / "audit_commits.py"), "--repo", str(repo), "--base", base_sha, "--stage", "full", "--json"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(full_rep.returncode, 1, full_rep.stdout)
            full_data = json.loads(full_rep.stdout)
            rep_chk = next(c for c in full_data["checks"] if c["name"] == "reports_zero_failed_and_fresh")
            stale_names = [s["report"] for s in rep_chk.get("stale_reports") or []]
            self.assertIn("e2e/reports/visual-acceptance.json", stale_names)

            bad_repo = Path(td) / "bad-ms"
            bad_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "-q"], cwd=bad_repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=bad_repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=bad_repo, check=True)
            (bad_repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=bad_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: base"], cwd=bad_repo, check=True)
            bad_base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=bad_repo, text=True).strip()

            (bad_repo / "backend").mkdir()
            (bad_repo / "backend" / "m1.rs").write_text("// m1\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=bad_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "feat: M1-P0"], cwd=bad_repo, check=True)
            (bad_repo / "frontend" / "src").mkdir(parents=True)
            (bad_repo / "frontend" / "src" / "M1.tsx").write_text("export {};\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=bad_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "feat: M1-GUI"], cwd=bad_repo, check=True)

            (bad_repo / "frontend" / "src" / "M2.tsx").write_text("export {};\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=bad_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "feat: M2-GUI jumped ahead"], cwd=bad_repo, check=True)
            (bad_repo / "backend" / "m2.rs").write_text("// m2\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=bad_repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "feat: M2-P0 late"], cwd=bad_repo, check=True)

            bad_rep = subprocess.run(
                [sys.executable, "-B", str(SCRIPTS_DIR / "audit_commits.py"), "--repo", str(bad_repo), "--base", bad_base, "--stage", "cui", "--json"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(bad_rep.returncode, 1)
            bad_data = json.loads(bad_rep.stdout)
            cui_chk = next(c for c in bad_data["checks"] if c["name"] == "cui_first_discipline")
            self.assertFalse(cui_chk["passed"])
            self.assertIn("[M2]", cui_chk["order_detail"])


class V2WorktreeTmuxAndViewTests(unittest.TestCase):
    def test_track_roster_and_model_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "home"
            fake_home.mkdir()
            repo = Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            env = dict(os.environ, HOME=str(fake_home))

            init_out = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "init",
                        "--repo", str(repo), "--slug", "track-demo",
                        "--track", "backend_codex=codex:GPT-6.2-Sol",
                        "--track", "frontend=claude",
                        "--json",
                    ],
                    env=env, text=True,
                )
            )
            tracks = init_out["manifest"]["roster"]["tracks"]
            self.assertEqual(tracks["backend_codex"], {"harness": "codex", "model": "gpt-6.2-sol"})
            self.assertEqual(tracks["frontend"], {"harness": "claude", "model": ""})

            roster_out = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "set-roster",
                        "--repo", str(repo), "--track", "reviewer=codex:Codex Auto Review", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(roster_out["roster"]["tracks"]["reviewer"], {"harness": "codex", "model": "codex-auto-review"})

            d_codex = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--track", "backend_codex",
                        "--prompt", "build backend", "--dry-run", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(d_codex["to"], "codex")
            self.assertEqual(d_codex["model"], "gpt-6.2-sol")

            d_fe = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--track", "frontend",
                        "--prompt", "build ui", "--dry-run", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(d_fe["to"], "claude")
            self.assertEqual(d_fe["model"], "")

    def test_worktree_scoped_write_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "main"
            wt_other = Path(td) / "wt-other"
            repo.mkdir()
            wt_other.mkdir()
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
            try:
                manifest = {
                    "jobs": [
                        {
                            "job_id": "codex-main-1",
                            "harness": "codex",
                            "mode": "write",
                            "workdir": str(repo),
                            "pid": proc.pid,
                            "status": "RUNNING",
                        }
                    ]
                }
                self.assertEqual(len(loopctl.find_active_write_conflicts(repo, manifest, target_workdir=repo)), 1)
                self.assertEqual(loopctl.find_active_write_conflicts(repo, manifest, target_workdir=wt_other), [])
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_tmux_session_run_job_dashboard_tail_and_wave_watch(self) -> None:
        if not loopctl.tmux_bin():
            self.skipTest("tmux not installed")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            repo = root / "repo"
            wt_a = root / "wt-a"
            wt_b = root / "wt-b"
            for d in (repo, wt_a, wt_b):
                d.mkdir()
                subprocess.run(["git", "init", "-b", "main", "-q"], cwd=d, check=True)
                subprocess.run(["git", "config", "user.name", "Test"], cwd=d, check=True)
                subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=d, check=True)
                (d / "README.md").write_text("ok\n", encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=d, check=True)
                subprocess.run(["git", "commit", "-q", "-m", "chore: init"], cwd=d, check=True)

            fake_bin = root / "bin"
            fake_bin.mkdir()
            mock_codex = fake_bin / "codex"
            mock_codex.write_text(
                "\n".join([
                    f"#!{sys.executable}",
                    "import json, sys, time, pathlib",
                    "out_file = ''",
                    "args = sys.argv[1:]",
                    "if '-o' in args:",
                    "    out_file = args[args.index('-o') + 1]",
                    "tid = f'01a2ffff-0000-0000-0000-{int(time.time() * 1000) % 10**12:012d}'",
                    "print(json.dumps({'type': 'thread.started', 'thread_id': tid}), flush=True)",
                    "time.sleep(0.5)",
                    "cwd_name = pathlib.Path.cwd().name",
                    "msg = f'Completed work in {cwd_name}'",
                    "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': msg}}), flush=True)",
                    "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}), flush=True)",
                    "if out_file:",
                    "    pathlib.Path(out_file).write_text(msg + '\\n', encoding='utf-8')",
                ]) + "\n",
                encoding="utf-8",
            )
            mock_codex.chmod(0o755)

            sock = f"hl-test-{os.getpid()}-{int(time.time() * 1000) % 100000}"
            env = dict(
                os.environ,
                HOME=str(fake_home),
                PATH=f"{fake_bin}:{os.environ.get('PATH', '')}",
                HARNESS_LOOP_TMUX_SOCKET=sock,
            )
            try:
                subprocess.run(
                    [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "tmux-wave", "--json"],
                    env=env, check=True, stdout=subprocess.DEVNULL,
                )
                d1 = json.loads(
                    subprocess.check_output(
                        [
                            sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                            "--repo", str(repo), "--workdir", str(wt_a), "--wave", "w1",
                            "--label", "worker-a", "--from", "agy", "--to", "codex",
                            "--prompt", "task a", "--json",
                        ],
                        env=env, text=True,
                    )
                )
                d2 = json.loads(
                    subprocess.check_output(
                        [
                            sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                            "--repo", str(repo), "--workdir", str(wt_b), "--wave", "w1",
                            "--label", "worker-b", "--from", "agy", "--to", "codex",
                            "--prompt", "task b", "--json",
                        ],
                        env=env, text=True,
                    )
                )
                self.assertTrue(d1["tmux_session"])
                self.assertEqual(d1["tmux_session"], d2["tmux_session"])

                st = json.loads(
                    subprocess.check_output(
                        [sys.executable, "-B", str(LOOPCTL_PATH), "tmux", "status", "--repo", str(repo), "--json"],
                        env=env, text=True,
                    )
                )
                self.assertTrue(st["tmux_session_alive"])
                self.assertGreaterEqual(len(st["windows"]), 3)
                self.assertEqual(len(st["jobs"]), 2)

                tail_out = subprocess.check_output(
                    [sys.executable, "-B", str(LOOPCTL_PATH), "tmux", "tail", "--repo", str(repo), "--job-id", d1["job_id"], "--lines", "30"],
                    env=env, text=True,
                )
                self.assertIn("[harness-loop] Job:", tail_out)

                w_res = subprocess.run(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "watch",
                        "--repo", str(repo), "--wave", "w1",
                        "--interval", "0.2", "--settle-seconds", "0.1", "--stall-timeout", "10", "--timeout", "20", "--json",
                    ],
                    env=env, capture_output=True, text=True, check=False,
                )
                self.assertEqual(w_res.returncode, 0, w_res.stdout + w_res.stderr)
                w_data = json.loads(w_res.stdout)
                self.assertTrue(w_data["ok"])
                self.assertEqual(w_data["reason"], "WAVE_COMPLETED")
                self.assertEqual(w_data["completed_count"], 2)

                latest_txt = (repo / ".agent-loop" / "LATEST.md").read_text(encoding="utf-8")
                self.assertIn("Completed work in wt-a", latest_txt)
                self.assertIn("Completed work in wt-b", latest_txt)
                self.assertIn("tmux -L", latest_txt)
            finally:
                subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True, check=False)

    def test_universal_view_claude_codex_job_and_deleted_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

            cc_sid = "aaaa1111-2222-3333-4444-555566667777"
            cc_dir = fake_home / ".claude" / "projects" / "-tmp-deleted-wt-alpha"
            cc_dir.mkdir(parents=True)
            cc_jsonl = cc_dir / f"{cc_sid}.jsonl"
            cc_jsonl.write_text(
                "\n".join([
                    json.dumps({
                        "type": "user",
                        "timestamp": "2026-11-24T01:00:00Z",
                        "sessionId": cc_sid,
                        "cwd": "/tmp/nonexistent-deleted-wt-alpha",
                        "gitBranch": "feat/demo",
                        "message": {"role": "user", "content": "check module A"},
                    }),
                    json.dumps({
                        "type": "assistant",
                        "timestamp": "2026-11-24T01:00:05Z",
                        "sessionId": cc_sid,
                        "cwd": "/tmp/nonexistent-deleted-wt-alpha",
                        "gitBranch": "feat/demo",
                        "message": {
                            "role": "assistant",
                            "stop_reason": "end_turn",
                            "content": [
                                {"type": "tool_use", "name": "Read"},
                                {"type": "text", "text": "Module A verified CLEAN."},
                            ],
                        },
                    }),
                ]) + "\n",
                encoding="utf-8",
            )

            codex_sid = "99990000-1111-2222-3333-444455556666"
            codex_dir = fake_home / ".codex" / "sessions" / "2026" / "11" / "24"
            codex_dir.mkdir(parents=True)
            codex_rollout = codex_dir / f"rollout-2026-11-24T01-00-00-{codex_sid}.jsonl"
            codex_rollout.write_text(
                "\n".join([
                    json.dumps({
                        "timestamp": "2026-11-24T01:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": codex_sid, "cwd": "/tmp/nonexistent-deleted-wt-beta"},
                    }),
                    json.dumps({
                        "timestamp": "2026-11-24T01:00:08Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Codex batch verified cleanly."}],
                        },
                    }),
                ]) + "\n",
                encoding="utf-8",
            )

            env = dict(os.environ, HOME=str(fake_home))
            cc_prefix_view = subprocess.check_output(
                [sys.executable, "-B", str(LOOPCTL_PATH), "view", "--claude-id", "aaaa1111", "--last", "5", "--full"],
                env=env, text=True,
            )
            self.assertIn(cc_sid, cc_prefix_view)
            self.assertIn("Module A verified CLEAN.", cc_prefix_view)
            self.assertIn("Read", cc_prefix_view)

            codex_view = subprocess.check_output(
                [sys.executable, "-B", str(LOOPCTL_PATH), "view", "--codex-id", codex_sid, "--last", "5"],
                env=env, text=True,
            )
            self.assertIn("codex resume --all", codex_view)
            self.assertIn("Codex batch verified cleanly.", codex_view)


class ExitCriteriaAndGatesTests(unittest.TestCase):
    def test_rework_round_cap_escalation_and_abort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / "README.md").write_text("init\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: init"], cwd=repo, check=True)
            env = dict(os.environ, HOME=str(fake_home))

            init_out = json.loads(
                subprocess.check_output(
                    [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "exit-cap", "--max-rounds", "2", "--json"],
                    env=env, text=True,
                )
            )
            self.assertEqual(init_out["manifest"]["max_rounds"], 2)

            r1 = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--to", "codex",
                        "--rework", "--prompt", "fix 1", "--dry-run", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertTrue(r1["rework"])
            self.assertEqual(r1["round"], 1)

            r2 = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--to", "claude",
                        "--rework", "--prompt", "fix 2", "--dry-run", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(r2["round"], 2)

            st_mid = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_mid["round"], 2)
            self.assertEqual(st_mid["verdict"], "NEEDS_FIX")

            r3 = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                    "--repo", str(repo), "--from", "agy", "--to", "codex",
                    "--rework", "--prompt", "fix 3", "--json",
                ],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(r3.returncode, 2, r3.stdout + r3.stderr)
            r3_data = json.loads(r3.stdout)
            self.assertEqual(r3_data["reason"], "max_rework_rounds_reached")
            self.assertEqual(r3_data["phase"], "ESCALATED")

            st_esc = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_esc["phase"], "ESCALATED")
            self.assertIn("auto_rework_cap", st_esc["terminal_reason"])

            blocked_norm = subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                    "--repo", str(repo), "--from", "agy", "--to", "codex",
                    "--prompt", "resume blocked", "--dry-run", "--json",
                ],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(blocked_norm.returncode, 2, blocked_norm.stdout)
            self.assertEqual(json.loads(blocked_norm.stdout)["reason"], "run_already_terminal")

            forced = json.loads(
                subprocess.check_output(
                    [
                        sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                        "--repo", str(repo), "--from", "agy", "--to", "codex",
                        "--rework", "--force", "--prompt", "fix 3 forced", "--dry-run", "--json",
                    ],
                    env=env, text=True,
                )
            )
            self.assertEqual(forced["round"], 3)

            subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "record",
                    "--repo", str(repo), "--role", "agy", "--event", "escalate",
                    "--summary", "need user choice", "--json",
                ],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            st_esc2 = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_esc2["phase"], "ESCALATED")
            self.assertEqual(st_esc2["terminal_reason"], "need user choice")

            subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "record",
                    "--repo", str(repo), "--role", "user", "--event", "abort",
                    "--summary", "user stopped", "--json",
                ],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            st_abt = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_abt["phase"], "ABORTED")
            self.assertEqual(st_abt["terminal_reason"], "user stopped")

    def test_signoff_exit_gate_and_stale_pass_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_home = root / "home"
            fake_home.mkdir()
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / "README.md").write_text("init\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "chore: init"], cwd=repo, check=True)
            env = dict(os.environ, HOME=str(fake_home))

            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "gate-run", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )

            s_fail1 = subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "signoff", "--summary", "done?", "--json"],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(s_fail1.returncode, 2)
            d1 = json.loads(s_fail1.stdout)
            self.assertEqual(d1["reason"], "signoff_gate_failed")
            self.assertEqual(len(d1["blockers"]), 2)

            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "verifier_audit", "--verdict", "PASS", "--summary", "all clear", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            s_fail2 = subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "signoff", "--summary", "done?", "--json"],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(s_fail2.returncode, 2)
            self.assertIn("ux_walkthrough", " ".join(json.loads(s_fail2.stdout)["blockers"]))

            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "signoff", "--skip-ux", "--summary", "cli complete", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            st_done = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_done["phase"], "DONE")
            self.assertEqual(st_done["terminal_reason"], "completed")

            subprocess.run(
                [
                    sys.executable, "-B", str(LOOPCTL_PATH), "delegate",
                    "--repo", str(repo), "--from", "agy", "--to", "codex",
                    "--force", "--prompt", "next wave", "--dry-run", "--json",
                ],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            st_reset = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_reset["verdict"], "PENDING")
            self.assertEqual(st_reset["terminal_reason"], "")

            init2 = json.loads(
                subprocess.check_output(
                    [sys.executable, "-B", str(LOOPCTL_PATH), "init", "--repo", str(repo), "--slug", "jobs-gate", "--json"],
                    env=env, text=True,
                )
            )
            run_dir2 = Path(init2["run_dir"])
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "verifier_audit", "--verdict", "PASS", "--summary", "pass", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "ux_walkthrough", "--summary", "ux ok", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )

            m_path = run_dir2 / "manifest.json"
            m_data = json.loads(m_path.read_text(encoding="utf-8"))
            wt1 = str((root / "wt1").resolve())
            wt2 = str((root / "wt2").resolve())
            m_data["jobs"] = [
                {"job_id": "j-fail", "workdir": wt1, "label": "w1", "status": "PROCESS_EXITED_FAILED"},
                {"job_id": "j-run", "workdir": wt2, "label": "w2", "status": "RUNNING", "pid": os.getpid()},
            ]
            m_path.write_text(json.dumps(m_data, ensure_ascii=False, indent=2), encoding="utf-8")

            s_fail3 = subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "signoff", "--summary", "try", "--json"],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(s_fail3.returncode, 2)
            d3 = json.loads(s_fail3.stdout)
            self.assertTrue(d3["details"]["active_jobs"])
            self.assertTrue(d3["details"]["unresolved_failed_jobs"])

            m_data["jobs"][1]["status"] = "COMPLETED"
            m_data["jobs"].append({"job_id": "j-retry-ok", "workdir": wt1, "label": "w1", "status": "COMPLETED"})
            m_path.write_text(json.dumps(m_data, ensure_ascii=False, indent=2), encoding="utf-8")

            subprocess.run(
                [sys.executable, "-B", str(LOOPCTL_PATH), "record", "--repo", str(repo), "--role", "agy", "--event", "signoff", "--summary", "all superseded & done", "--json"],
                env=env, check=True, stdout=subprocess.DEVNULL,
            )
            st_done2 = json.loads(
                subprocess.check_output([sys.executable, "-B", str(LOOPCTL_PATH), "status", "--repo", str(repo)], env=env, text=True)
            )
            self.assertEqual(st_done2["phase"], "DONE")


if __name__ == "__main__":
    unittest.main()
