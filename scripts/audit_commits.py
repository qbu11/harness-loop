#!/usr/bin/env python3
"""Deterministic Post-Verifier repository and gate auditor for harness-loop.

Read-only script that inspects git commit range <base>..<head>, verifies
worktree cleanliness, CUI-First commit ordering, absence of AI attribution,
forbidden-path protection, leftover failure screenshots, and report freshness.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_FRONTEND_PREFIXES = ("frontend/src/", "src/components/", "src/pages/")
DEFAULT_FORBIDDEN_PREFIXES = ("backend/data/", "docs/design-refs/")
DEFAULT_CUI_REGEX = r"(?i)(p0|cui|cli先行|后端|backend\b)"
DEFAULT_MILESTONE_REGEX = r"(?i)(?:^|[^A-Za-z0-9])(M\d+)(?=$|[^A-Za-z0-9])"
AI_ATTRIBUTION_RE = re.compile(
    r"(?i)(co-authored-by:\s*[^\n]*(claude|anthropic|openai|codex|gemini|copilot)|generated with \[?claude|noreply@anthropic\.com|🤖\s*generated)"
)


def run_git(repo: Path, args: list[str]) -> tuple[int, str, str]:
    """Execute a read-only git command inside repo."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def classify_path(
    rel_path: str,
    frontend_prefixes: tuple[str, ...],
    reports_dir: str,
    screenshots_dir: str,
) -> str:
    """Categorize a repo-relative path into layer buckets."""
    if any(rel_path.startswith(p) for p in frontend_prefixes):
        return "frontend"
    if rel_path.startswith(reports_dir.rstrip("/") + "/") or rel_path.startswith(
        screenshots_dir.rstrip("/") + "/"
    ):
        return "tests_or_reports"
    if (
        rel_path.startswith("docs/")
        or rel_path == "AGENTS.md"
        or rel_path.endswith(".md")
    ):
        return "docs"
    if (
        rel_path.startswith("e2e/")
        or rel_path.startswith("tests/")
        or rel_path.endswith("_test.rs")
        or rel_path.endswith(".test.ts")
        or rel_path.endswith(".test.tsx")
        or (rel_path.startswith("environment/") and rel_path.endswith(".json"))
        or (
            rel_path.startswith("scripts/")
            and any(
                k in Path(rel_path).name
                for k in ("acceptance", "visual", "smoke", "unit-frontend")
            )
            and "cli" not in Path(rel_path).name
        )
    ):
        return "tests_or_reports"
    if (
        rel_path.startswith("backend/")
        or rel_path.startswith("environment/")
        or rel_path.startswith("scripts/")
        or rel_path.startswith("cli/")
        or rel_path.endswith((".rs", ".py", ".sh", ".go", ".sql"))
    ):
        return "backend_or_cli"
    return "other"


def load_commit_records(
    repo: Path,
    base: str,
    head: str,
    frontend_prefixes: tuple[str, ...],
    reports_dir: str,
    screenshots_dir: str,
) -> list[dict[str, Any]]:
    """Load commit metadata and touched files for <base>..<head> in chronological order."""
    sep = "\x1e"
    field_sep = "\x1f"
    fmt = f"%H{field_sep}%h{field_sep}%ct{field_sep}%s{field_sep}%B{sep}"
    rc, out, err = run_git(repo, ["log", f"{base}..{head}", "--reverse", f"--format={fmt}"])
    if rc != 0:
        raise RuntimeError(f"git log failed for {base}..{head}: {err.strip()}")

    commits: list[dict[str, Any]] = []
    for chunk in out.split(sep):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        parts = chunk.split(field_sep, 4)
        if len(parts) != 5:
            continue
        full_hash, short_hash, ct_str, subject, body = parts
        rc_files, files_out, _ = run_git(
            repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", full_hash]
        )
        files = [f for f in files_out.splitlines() if f.strip()] if rc_files == 0 else []
        layers = sorted(
            {classify_path(f, frontend_prefixes, reports_dir, screenshots_dir) for f in files}
        )
        commits.append(
            {
                "hash": full_hash,
                "short": short_hash,
                "timestamp": int(ct_str),
                "subject": subject.strip(),
                "body": body.strip(),
                "files": files,
                "layers": layers,
            }
        )
    return commits


def _extract_milestone(subject: str, milestone_re: re.Pattern[str] | None) -> str:
    """Extract normalized milestone token (e.g. 'M1', 'M2') from commit subject."""
    if not milestone_re:
        return ""
    m = milestone_re.search(subject)
    return m.group(1).upper() if m else ""


def check_cui_first(
    commits: list[dict[str, Any]],
    cui_regex: re.Pattern[str],
    frontend_prefixes: tuple[str, ...],
    milestone_regex: re.Pattern[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Verify CUI-First commit boundary and per-milestone backend-before-frontend ordering."""
    cui_violations: list[dict[str, Any]] = []
    feat_backend_indices: list[int] = []
    feat_frontend_indices: list[int] = []
    mixed_feat_commits: list[str] = []
    milestone_buckets: dict[str, dict[str, list[int]]] = {}

    for idx, c in enumerate(commits):
        frontend_files = [f for f in c["files"] if any(f.startswith(p) for p in frontend_prefixes)]
        if cui_regex.search(c["subject"]) and frontend_files:
            cui_violations.append({"commit": c["short"], "subject": c["subject"], "frontend_files": frontend_files})

        is_feat = c["subject"].lower().startswith("feat")
        has_backend = "backend_or_cli" in c["layers"]
        has_frontend = bool(frontend_files)
        if is_feat:
            if has_backend and has_frontend:
                mixed_feat_commits.append(f"{c['short']} {c['subject']}")
            if has_backend:
                feat_backend_indices.append(idx)
            if has_frontend:
                feat_frontend_indices.append(idx)
            ms = _extract_milestone(c["subject"], milestone_regex)
            if ms:
                bucket = milestone_buckets.setdefault(ms, {"backend": [], "frontend": []})
                if has_backend:
                    bucket["backend"].append(idx)
                if has_frontend:
                    bucket["frontend"].append(idx)

    order_ok = True
    order_detail = "ok"
    milestone_orders: dict[str, str] = {}
    if milestone_buckets:
        bad_details: list[str] = []
        for ms in sorted(milestone_buckets):
            b_idxs = milestone_buckets[ms]["backend"]
            f_idxs = milestone_buckets[ms]["frontend"]
            if b_idxs and f_idxs and min(f_idxs) < min(b_idxs):
                msg = (
                    f"[{ms}] frontend feat commit (idx {min(f_idxs)}) precedes "
                    f"backend feat commit (idx {min(b_idxs)})"
                )
                milestone_orders[ms] = msg
                bad_details.append(msg)
            else:
                milestone_orders[ms] = "ok"
        if bad_details:
            order_ok = False
            order_detail = "; ".join(bad_details)
    elif feat_backend_indices and feat_frontend_indices:
        if min(feat_frontend_indices) < min(feat_backend_indices):
            order_ok = False
            order_detail = (
                f"frontend feat commit (idx {min(feat_frontend_indices)}) precedes "
                f"backend feat commit (idx {min(feat_backend_indices)})"
            )
    elif not any(c["subject"].lower().startswith("feat") for c in commits):
        all_backend = [i for i, c in enumerate(commits) if "backend_or_cli" in c["layers"]]
        all_frontend = [i for i, c in enumerate(commits) if "frontend" in c["layers"]]
        if all_backend and all_frontend and min(all_frontend) < min(all_backend):
            order_ok = False
            order_detail = (
                f"frontend code commit (idx {min(all_frontend)}) precedes "
                f"backend code commit (idx {min(all_backend)})"
            )

    passed = not cui_violations and order_ok and not mixed_feat_commits
    return passed, {
        "cui_stage_frontend_violations": cui_violations,
        "mixed_feat_commits": mixed_feat_commits,
        "order_ok": order_ok,
        "order_detail": order_detail,
        "milestone_orders": milestone_orders,
    }


def read_json_at_ref_or_disk(repo: Path, head: str, rel_path: str, prefer_git: bool) -> dict[str, Any] | None:
    """Read a JSON file from git <head> or working tree disk safely."""
    raw = ""
    if prefer_git:
        rc, out, _ = run_git(repo, ["show", f"{head}:{rel_path}"])
        if rc == 0:
            raw = out
    if not raw:
        disk = repo / rel_path
        if disk.exists():
            raw = disk.read_text(encoding="utf-8", errors="replace")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def list_new_or_untracked_screenshots(
    repo: Path, base: str, head: str, screenshots_dir: str, include_worktree: bool
) -> tuple[list[str], list[str]]:
    """Return failure screenshots introduced in <base>..<head> or untracked in worktree, plus preexisting ones."""
    prefix = screenshots_dir.rstrip("/")
    prefix_slash = prefix + "/"

    rc_diff, diff_out, _ = run_git(
        repo, ["diff", "--name-status", f"{base}..{head}", "--", prefix]
    )
    changed_shots: list[str] = []
    if rc_diff == 0:
        for line in diff_out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and not parts[0].startswith("D"):
                changed_shots.append(parts[-1].strip())

    if include_worktree:
        rc_untracked, untracked_out, _ = run_git(
            repo, ["ls-files", "--others", "--exclude-standard", "--", prefix]
        )
        if rc_untracked == 0:
            changed_shots.extend(
                line.strip() for line in untracked_out.splitlines() if line.strip()
            )

    rc_all, all_out, _ = run_git(repo, ["ls-tree", "-r", "--name-only", head, "--", prefix])
    all_shots = [line.strip() for line in all_out.splitlines() if line.strip()] if rc_all == 0 else []
    if not all_shots and (repo / prefix).exists():
        all_shots = [str(p.relative_to(repo)) for p in sorted((repo / prefix).rglob("*.png"))]

    def _is_fail(p: str) -> bool:
        n = Path(p).name.lower()
        return p.startswith(prefix_slash) and ("failure" in n or "failed" in n)

    new_fails = sorted({p for p in changed_shots if _is_fail(p)})
    preexisting_fails = sorted({p for p in all_shots if _is_fail(p) and p not in new_fails})
    return new_fails, preexisting_fails


def inspect_reports(
    repo: Path,
    base: str,
    head: str,
    commits: list[dict[str, Any]],
    reports_dir: str,
    allow_known: set[str],
    prefer_git: bool,
    *,
    stage: str = "full",
) -> tuple[bool, dict[str, Any]]:
    """Check report zero-failure invariant and stage-appropriate freshness relative to code commits."""
    prefix = reports_dir.rstrip("/") + "/"
    touched_reports: dict[str, int] = {}
    for c in commits:
        for f in c["files"]:
            if f.startswith(prefix) and f.endswith(".json"):
                touched_reports[f] = max(touched_reports.get(f, 0), c["timestamp"])

    last_backend_ts = max(
        (c["timestamp"] for c in commits if "backend_or_cli" in c["layers"]),
        default=0,
    )
    last_frontend_ts = max(
        (c["timestamp"] for c in commits if "frontend" in c["layers"]),
        default=0,
    )
    last_code_ts = max(last_backend_ts, last_frontend_ts)

    gui_report_re = re.compile(
        r"(smoke|visual|ui|acceptance-report|research-acceptance|frontend-.*-acceptance|v(?:[6-9]|\d{2,}).*-acceptance)"
    )

    candidate_paths: list[str] = sorted(touched_reports.keys())
    if not candidate_paths:
        reports_root = repo / reports_dir
        if reports_root.exists():
            candidate_paths = [
                str(p.relative_to(repo))
                for p in sorted(reports_root.glob("*.json"))
                if p.name not in allow_known
            ]

    failed_reports: list[dict[str, Any]] = []
    stale_candidates: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    skipped_gui_reports: list[str] = []
    max_report_ts = 0
    has_fresh_cli_report = False

    for rel in candidate_paths:
        name = Path(rel).name
        if name in allow_known:
            continue
        is_gui_report = bool(gui_report_re.search(name))
        if stage == "cui" and is_gui_report:
            skipped_gui_reports.append(rel)
            continue
        data = read_json_at_ref_or_disk(repo, head, rel, prefer_git=prefer_git)
        if not data:
            continue
        failed_val = data.get("failed", data.get("checks_failed"))
        passed_val = data.get("passed", data.get("checks_passed"))
        if failed_val is None:
            continue

        report_ts = touched_reports.get(rel, 0)
        disk_report = repo / rel
        if not prefer_git and disk_report.exists():
            is_dirty_or_untracked = rel not in touched_reports
            if not is_dirty_or_untracked:
                rc_st, st_out, _ = run_git(repo, ["status", "--porcelain", "--", rel])
                is_dirty_or_untracked = rc_st == 0 and bool(st_out.strip())
            if is_dirty_or_untracked:
                report_ts = max(report_ts, int(disk_report.stat().st_mtime))
        max_report_ts = max(max_report_ts, report_ts)

        required_ts = last_backend_ts if stage == "cui" else (last_code_ts if is_gui_report else last_backend_ts)
        is_fresh = True
        if required_ts and report_ts and report_ts + 5 < required_ts:
            is_fresh = False
            stale_candidates.append({"report": rel, "report_ts": report_ts, "required_ts": required_ts})
        elif required_ts and report_ts and not is_gui_report:
            has_fresh_cli_report = True

        item = {"report": rel, "passed": passed_val, "failed": failed_val, "fresh": is_fresh}
        checked.append(item)
        if isinstance(failed_val, (int, float)) and failed_val > 0:
            failed_reports.append(item)

    older_milestone_reports: list[dict[str, Any]] = []
    if stage == "cui" and has_fresh_cli_report:
        older_milestone_reports = stale_candidates
        stale_reports: list[dict[str, Any]] = []
        for item in checked:
            if not item["fresh"]:
                item["older_milestone"] = True
    else:
        stale_reports = stale_candidates

    missing_frontend_report = bool(
        stage != "cui" and last_frontend_ts and checked and max_report_ts and max_report_ts + 5 < last_frontend_ts
    )
    passed = not failed_reports and not stale_reports and not missing_frontend_report
    return passed, {
        "stage": stage,
        "checked_count": len(checked),
        "checked": checked,
        "skipped_gui_reports": skipped_gui_reports,
        "failed_reports": failed_reports,
        "stale_reports": stale_reports,
        "older_milestone_reports": older_milestone_reports,
        "missing_frontend_report": missing_frontend_report,
        "last_backend_commit_ts": last_backend_ts,
        "last_frontend_commit_ts": last_frontend_ts,
        "last_code_commit_ts": last_code_ts,
    }


def audit(args: argparse.Namespace) -> dict[str, Any]:
    """Run all 7 deterministic audit checks and return structured report."""
    repo = Path(args.repo).resolve()
    frontend_prefixes = tuple(args.frontend_prefix) if args.frontend_prefix else DEFAULT_FRONTEND_PREFIXES
    forbidden_prefixes = tuple(args.forbid_path) if args.forbid_path else DEFAULT_FORBIDDEN_PREFIXES
    cui_regex = re.compile(args.cui_stage_regex)
    milestone_raw = getattr(args, "milestone_regex", DEFAULT_MILESTONE_REGEX)
    milestone_regex = re.compile(milestone_raw) if milestone_raw else None
    stage = getattr(args, "stage", "full")
    allow_known = set(args.allow_known_report or [])

    rc_head, head_sha, _ = run_git(repo, ["rev-parse", args.head])
    rc_cur, cur_sha, _ = run_git(repo, ["rev-parse", "HEAD"])
    prefer_git = args.skip_worktree_check or (rc_head == 0 and rc_cur == 0 and head_sha != cur_sha)

    checks: list[dict[str, Any]] = []

    if args.skip_worktree_check:
        checks.append({"name": "clean_worktree", "passed": True, "skipped": True, "detail": "skipped via --skip-worktree-check"})
    else:
        rc_st, st_out, _ = run_git(repo, ["status", "--short"])
        dirty = [line for line in st_out.splitlines() if line.strip()] if rc_st == 0 else ["git status error"]
        checks.append({"name": "clean_worktree", "passed": len(dirty) == 0, "detail": dirty})

    commits = load_commit_records(
        repo, args.base, args.head, frontend_prefixes, args.reports_dir, args.screenshots_dir
    )
    checks.append(
        {
            "name": "commit_chain_present",
            "passed": len(commits) > 0,
            "count": len(commits),
            "commits": [
                {"short": c["short"], "timestamp": c["timestamp"], "subject": c["subject"], "layers": c["layers"]}
                for c in commits
            ],
        }
    )

    ai_hits = [
        {"commit": c["short"], "subject": c["subject"]}
        for c in commits
        if AI_ATTRIBUTION_RE.search(c["body"])
    ]
    checks.append({"name": "no_ai_attribution", "passed": len(ai_hits) == 0, "hits": ai_hits})

    cui_ok, cui_detail = check_cui_first(commits, cui_regex, frontend_prefixes, milestone_regex=milestone_regex)
    checks.append({"name": "cui_first_discipline", "passed": cui_ok, **cui_detail})

    rc_diff, diff_out, _ = run_git(repo, ["diff", "--name-only", f"{args.base}..{args.head}"])
    all_changed = [f for f in diff_out.splitlines() if f.strip()] if rc_diff == 0 else []
    forbidden_hits = [f for f in all_changed if any(f.startswith(p) for p in forbidden_prefixes)]
    checks.append({"name": "forbidden_paths_untouched", "passed": len(forbidden_hits) == 0, "hits": forbidden_hits})

    failure_shots, preexisting_failure_shots = list_new_or_untracked_screenshots(
        repo, args.base, args.head, args.screenshots_dir, include_worktree=not args.skip_worktree_check
    )
    checks.append(
        {
            "name": "no_leftover_failure_screenshots",
            "passed": len(failure_shots) == 0,
            "hits": failure_shots,
            "preexisting_ignored": preexisting_failure_shots,
        }
    )

    reports_ok, reports_detail = inspect_reports(
        repo, args.base, args.head, commits, args.reports_dir, allow_known, prefer_git=prefer_git, stage=stage
    )
    checks.append({"name": "reports_zero_failed_and_fresh", "passed": reports_ok, **reports_detail})

    passed_count = sum(1 for c in checks if c["passed"])
    failed_count = len(checks) - passed_count
    return {
        "repo": str(repo),
        "range": f"{args.base}..{args.head}",
        "stage": stage,
        "passed": passed_count,
        "failed": failed_count,
        "ok": failed_count == 0,
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Post-Verifier git and gate audit tool")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--stage", choices=("full", "cui"), default="full")
    parser.add_argument("--milestone-regex", default=DEFAULT_MILESTONE_REGEX)
    parser.add_argument("--frontend-prefix", action="append", default=None)
    parser.add_argument("--cui-stage-regex", default=DEFAULT_CUI_REGEX)
    parser.add_argument("--forbid-path", action="append", default=None)
    parser.add_argument("--reports-dir", default="e2e/reports")
    parser.add_argument("--screenshots-dir", default="e2e/screenshots")
    parser.add_argument("--allow-known-report", action="append", default=None)
    parser.add_argument("--skip-worktree-check", action="store_true")
    parser.add_argument("--brief", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _compact_brief_report(report: dict[str, Any]) -> dict[str, Any]:
    """Produce a token-saving compact summary of an audit report."""
    checks = report.get("checks") or []
    cui_chk = next((c for c in checks if c.get("name") == "cui_first_discipline"), {})
    rep_chk = next((c for c in checks if c.get("name") == "reports_zero_failed_and_fresh"), {})
    if report.get("ok"):
        return {
            "ok": True,
            "brief": True,
            "repo": report["repo"],
            "range": report["range"],
            "stage": report["stage"],
            "passed": report["passed"],
            "failed": report["failed"],
            "milestone_orders": cui_chk.get("milestone_orders", {}),
            "reports": [
                {"report": r.get("report"), "passed": r.get("passed"), "failed": r.get("failed"), "fresh": r.get("fresh")}
                for r in (rep_chk.get("checked") or [])
                if r.get("fresh") or not r.get("older_milestone")
            ],
        }
    return {
        "ok": False,
        "brief": True,
        "repo": report["repo"],
        "range": report["range"],
        "stage": report["stage"],
        "passed": report["passed"],
        "failed": report["failed"],
        "failed_checks": [c for c in checks if not c.get("passed")],
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        report = audit(args)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.json:
        out_payload = _compact_brief_report(report) if args.brief else report
        print(json.dumps(out_payload, ensure_ascii=False, indent=2))
    else:
        print(f"Audit {report['range']}: {report['passed']} passed, {report['failed']} failed")
        for c in report["checks"]:
            mark = "PASS" if c["passed"] else "FAIL"
            print(f"  [{mark}] {c['name']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
