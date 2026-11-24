#!/usr/bin/env python3
"""Cross-harness bridge and traceable dialogue ledger CLI for harness-loop.

Coordinates multiple AI coding harnesses — built-in Antigravity (agy), Claude Code
(claude), and Codex (codex, incl. GPT-6.2-Sol), extensible to future harnesses —
as interchangeable Architect / Executor / Verifier / Advisor roles, supporting
cross-harness read-only consultation (`consult`), write delegation (`delegate`),
live progress inspection (`inspect*`), and git-ignored traceable dialogue logs
(`<repo>/.agent-loop/LATEST.md`) using only the Python 3 stdlib.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SKILL_NAME = "harness-loop"
LOOP_DIR_NAME = ".agent-loop"
MANIFEST_LOCK_FILE = ".manifest.lock"
TMUX_SOCKET_ENV = "HARNESS_LOOP_TMUX_SOCKET"
TMUX_SESSION_PREFIX = "hl-"
EVENTS_FILE = "events.jsonl"
MANIFEST_FILE = "manifest.json"
DIALOGUE_FILE = "DIALOGUE.md"
LATEST_RUN_FILE = "latest_run.txt"
LATEST_MD_FILE = "LATEST.md"
JOBS_DIR = "jobs"

HARNESSES = ("agy", "claude", "codex")
ROLES_WITH_USER = ("agy", "claude", "codex", "user")

CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
AGY_BRAIN_ROOT = Path.home() / ".gemini" / "antigravity" / "brain"
AGY_CLI_BRAIN_ROOT = Path.home() / ".gemini" / "antigravity-cli" / "brain"
CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
CODEX_INDEX_FILE = Path.home() / ".codex" / "session_index.jsonl"

DEFAULT_MAX_ROUNDS = 2
TERMINAL_PHASES = ("DONE", "ESCALATED", "ABORTED")
CODEX_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")

VALID_EVENTS = (
    "audit_summary",
    "doc_pack_ready",
    "handoff_to_cc",
    "cc_progress",
    "cc_delivery",
    "verifier_audit",
    "rework",
    "rework_to_cc",
    "ux_walkthrough",
    "signoff",
    "escalate",
    "abort",
    "roster_set",
    "cross_consult",
    "cross_feedback",
    "delegate_task",
    "delivery",
)

EVENT_LABELS = {
    "audit_summary": "阶段1 · 项目与用户审查",
    "doc_pack_ready": "阶段2 · 四件套规格就绪",
    "handoff_to_cc": "阶段2/3 · 派发给 Claude Code",
    "cc_progress": "阶段3 · Claude Code 进度",
    "cc_delivery": "阶段3 · Claude Code 交卷",
    "verifier_audit": "阶段4 · Post-Verifier 独立终审",
    "rework": "阶段4 · 打回返工",
    "rework_to_cc": "阶段4 · 打回 Claude Code 返工",
    "ux_walkthrough": "阶段5 · 真实用户视角体验",
    "signoff": "阶段6 · 终审放行与结项汇报",
    "escalate": "异常停机 · 升级用户裁决",
    "abort": "用户中止 · 闭环取消",
    "roster_set": "角色编排 · 更新 Harness/模型分工",
    "cross_consult": "跨端咨询 · 发起只读评审/取经",
    "cross_feedback": "跨端反馈 · 收到只读建议",
    "delegate_task": "跨端派活 · 下达修改与执行任务",
    "delivery": "执行交卷 · 成果同步入账",
}

MODEL_ALIASES: dict[str, dict[str, str]] = {
    "codex": {
        "codex gpt 6 2 sol": "gpt-6.2-sol",
        "gpt 6 2 solver": "gpt-6.2-sol",
        "gpt 6 2 sol": "gpt-6.2-sol",
        "6 2 solver": "gpt-6.2-sol",
        "6 2 sol": "gpt-6.2-sol",
        "gpt6 2 sol": "gpt-6.2-sol",
        "gpt 6 2 astra": "gpt-6.2-astra",
        "6 2 astra": "gpt-6.2-astra",
        "codex gpt 6 1 sol": "gpt-6.1-sol",
        "gpt 6 1 solver": "gpt-6.1-sol",
        "gpt 6 1 sol": "gpt-6.1-sol",
        "6 1 solver": "gpt-6.1-sol",
        "6 1 sol": "gpt-6.1-sol",
        "codex gpt 6 astra": "gpt-6-astra",
        "gpt 6 astra": "gpt-6-astra",
        "6 astra": "gpt-6-astra",
        "codex gpt 6 sol": "gpt-6-sol",
        "gpt 6 sol": "gpt-6-sol",
        "6 sol": "gpt-6-sol",
        "gpt 6 1 terra": "gpt-6.1-terra",
        "gpt 6 luna": "gpt-6-luna",
        "codex auto review": "codex-auto-review",
    },
    "agy": {
        "gemini 4 neon xhigh": "gemini-4-neon-xhigh",
        "neon xhigh": "gemini-4-neon-xhigh",
        "gemini 4 neon high": "gemini-4-neon-high",
        "neon high": "gemini-4-neon-high",
        "gemini 4 carbon high": "gemini-4-carbon-high",
        "carbon high": "gemini-4-carbon-high",
        "orchestrator neon": "gemini-pro-orchestrator-neon",
        "orchestrator carbon": "gemini-pro-orchestrator",
        "claude opus 5 5 high": "claude-opus-5-5-high",
        "opus 5 5 high": "claude-opus-5-5-high",
    },
    "claude": {},
}


def now_iso() -> str:
    """Return local ISO-8601 timestamp rounded to seconds."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_prefix(text: str, length: int = 12) -> str:
    """Return lowercase hex sha256 prefix for traceability."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:length]


def _locate_bin(fallback_name: str, *candidates: Path) -> str | None:
    """Prefer active PATH binary, then portable user/system fallback paths."""
    found = shutil.which(fallback_name)
    if found:
        return found
    for candidate in candidates:
        if candidate and candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def claude_bin() -> str | None:
    """Locate the Claude Code CLI binary."""
    return _locate_bin(
        "claude",
        Path.home() / ".local" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    )


def agy_bin() -> str | None:
    """Locate the Antigravity (`agy`) CLI binary."""
    return _locate_bin(
        "agy",
        Path.home() / ".local" / "bin" / "agy",
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    )


def codex_bin() -> str | None:
    """Locate the Codex CLI binary."""
    nvm_bins = sorted(Path.home().glob(".nvm/versions/node/*/bin/codex"), reverse=True)
    return _locate_bin(
        "codex",
        *nvm_bins,
        Path.home() / ".local" / "bin" / "codex",
        Path.home() / ".volta" / "bin" / "codex",
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
    )


def tmux_bin() -> str | None:
    """Locate the native tmux binary unless disabled via HARNESS_LOOP_NO_TMUX=1."""
    if os.environ.get("HARNESS_LOOP_NO_TMUX") == "1":
        return None
    return _locate_bin(
        "tmux",
        Path("/opt/homebrew/bin/tmux"),
        Path("/usr/local/bin/tmux"),
        Path("/usr/bin/tmux"),
    )


def harness_bin(harness: str) -> str | None:
    """Locate native CLI binary for the given harness."""
    return {"agy": agy_bin, "claude": claude_bin, "codex": codex_bin}[harness]()


def normalize_model(harness: str, raw: str | None) -> str:
    """Normalize human model labels (e.g. 'Codex GPT 6.2 Sol' / 'GPT-6.2-Sol') to harness CLI slugs."""
    if not raw:
        return ""
    cleaned = raw.strip()
    key = re.sub(r"[^a-z0-9]+", " ", cleaned.lower()).strip()
    if not key or key in ("default", "inherit", "auto", "none"):
        return ""
    return MODEL_ALIASES.get(harness, {}).get(key, cleaned)


def parse_role_spec(spec: str) -> dict[str, str]:
    """Parse '<harness>[:<model>]' spec into normalized harness + model dict."""
    raw = spec.strip()
    if ":" in raw:
        harness_raw, model_raw = raw.split(":", 1)
    else:
        harness_raw, model_raw = raw, ""
    harness = harness_raw.strip().lower()
    if harness not in HARNESSES:
        raise ValueError(f"Unsupported harness {harness!r}; choose from {HARNESSES}")
    return {"harness": harness, "model": normalize_model(harness, model_raw)}


def parse_track_spec(spec: str) -> tuple[str, dict[str, str]]:
    """Parse '<track>=<harness>[:<model>]' into (track_name, role_spec)."""
    raw = spec.strip()
    if "=" not in raw:
        raise ValueError(f"Invalid --track spec {spec!r}; expected '<track>=<harness>[:<model>]'")
    track_name, role_raw = raw.split("=", 1)
    track = track_name.strip()
    if not track:
        raise ValueError(f"Empty track name in {spec!r}")
    return track, parse_role_spec(role_raw)


def run_git(repo: Path, args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a read-only git subcommand inside repo."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def git_head_short(repo: Path) -> str:
    """Return short HEAD SHA or 'NO_GIT'."""
    rc, out, _ = run_git(repo, ["rev-parse", "--short", "HEAD"])
    return out if rc == 0 and out else "NO_GIT"


def git_status_short(repo: Path) -> list[str]:
    """Return git status --short lines."""
    rc, out, _ = run_git(repo, ["status", "--short"])
    if rc != 0 or not out:
        return []
    return [line for line in out.splitlines() if line.strip()]


def resolve_worktree_branch(workdir: Path) -> str:
    """Return active branch name (or empty string on detached/non-git)."""
    rc, out, _ = run_git(workdir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0 or not out or out == "HEAD":
        return ""
    return out


def sanitize_tmux_name(raw: str, max_len: int = 24) -> str:
    """Normalize arbitrary slug/label into a safe tmux session or window name."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", (raw or "").strip()).strip("-_")
    if not cleaned:
        cleaned = "work"
    return cleaned[:max_len].rstrip("-_") or "work"


def default_tmux_session_name(manifest: dict[str, Any] | None, repo: Path) -> str:
    """Compute default tmux session name `hl-<slug-or-repo>` for this loop run."""
    if manifest and manifest.get("tmux_session"):
        return sanitize_tmux_name(str(manifest["tmux_session"]), max_len=36)
    base = (manifest.get("slug") if manifest else "") or repo.resolve().name or "loop"
    return f"{TMUX_SESSION_PREFIX}{sanitize_tmux_name(str(base), max_len=28)}"


def _tmux_argv(*args: str) -> list[str]:
    """Build tmux command argv honoring optional HARNESS_LOOP_TMUX_SOCKET."""
    binary = tmux_bin() or "tmux"
    sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
    if sock:
        return [binary, "-L", sock, *args]
    return [binary, *args]


def _should_use_tmux(explicit_flag: bool | None) -> bool:
    """Decide whether to launch inside tmux (default True when tmux is installed)."""
    if explicit_flag is False:
        return False
    if explicit_flag is True:
        if not tmux_bin():
            raise ValueError("Requested --tmux, but `tmux` binary was not found.")
        return True
    return bool(tmux_bin())


def _is_job_exit_recorded(job: dict[str, Any]) -> dict[str, Any] | None:
    """Read terminal exit_file metadata for a tmux/worker job if finalized."""
    if job.get("attach_bg_id"):
        return None
    ef = str(job.get("exit_file") or "").strip()
    if not ef:
        return None
    p = Path(ef)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) and "exit_code" in data else None
    except (OSError, json.JSONDecodeError):
        return None


def encode_claude_project_dir(repo_path: Path) -> Path:
    """Resolve ~/.claude/projects/<encoded-cwd> directory for repo_path."""
    resolved = str(repo_path.resolve())
    encoded = re.sub(r"[^A-Za-z0-9]", "-", resolved)
    candidate = CLAUDE_PROJECTS_ROOT / encoded
    if candidate.exists():
        return candidate
    if CLAUDE_PROJECTS_ROOT.exists():
        tail = encoded.split("-")[-1]
        matches = [
            p
            for p in CLAUDE_PROJECTS_ROOT.iterdir()
            if p.is_dir() and (p.name == encoded or (tail and p.name.endswith(f"-{tail}")))
        ]
        if len(matches) == 1:
            return matches[0]
        for p in matches:
            if encoded.lstrip("-") in p.name:
                return p
    return candidate


def query_claude_agents(repo: Path | None = None) -> list[dict[str, Any]]:
    """Call `claude agents --all --json` and optionally filter by repo cwd."""
    binary = claude_bin()
    if not binary:
        return []
    try:
        proc = subprocess.run(
            [binary, "agents", "--all", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            return []
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []

    if repo is None:
        return [item for item in data if isinstance(item, dict)]

    repo_resolved = repo.resolve()
    matched: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cwd = item.get("cwd")
        if not cwd:
            continue
        try:
            if Path(cwd).resolve() == repo_resolved:
                matched.append(item)
        except OSError:
            continue
    return matched


def ensure_loop_root(repo: Path) -> Path:
    """Ensure <repo>/.agent-loop exists and is self-ignored from git."""
    root = repo / LOOP_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    return root


def resolve_run_dir(repo: Path, run_id: str | None) -> Path:
    """Resolve run directory from explicit run_id or latest_run.txt."""
    root = repo / LOOP_DIR_NAME
    if not run_id:
        latest_file = root / LATEST_RUN_FILE
        if not latest_file.exists():
            raise FileNotFoundError(f"No {LATEST_RUN_FILE} found under {root}; run `init` first.")
        run_id = latest_file.read_text(encoding="utf-8").strip()
    run_dir = root / run_id
    if not (run_dir / MANIFEST_FILE).exists():
        raise FileNotFoundError(f"Run manifest not found: {run_dir / MANIFEST_FILE}")
    return run_dir


def default_sessions_dict() -> dict[str, dict[str, Any]]:
    """Return canonical session slot structure for all three harnesses."""
    return {
        "agy": {"session_id": "", "model": "", "transcript_path": ""},
        "claude": {"session_id": "", "bg_id": "", "name": "", "kind": "", "model": "", "jsonl_path": ""},
        "codex": {"session_id": "", "name": "", "model": "", "rollout_path": "", "last_job_id": ""},
    }


_MANIFEST_LOCK_MUTEX = threading.Lock()
_MANIFEST_RLOCKS: dict[str, threading.RLock] = {}
_MANIFEST_FDS: dict[str, tuple[Any, int]] = {}


@contextmanager
def _manifest_lock(run_dir: Path) -> Iterator[None]:
    """Thread-safe, re-entrant cross-process lock serializing manifest/ledger updates."""
    key = str(run_dir.resolve())
    with _MANIFEST_LOCK_MUTEX:
        rlock = _MANIFEST_RLOCKS.setdefault(key, threading.RLock())
    rlock.acquire()
    try:
        fh, depth = _MANIFEST_FDS.get(key, (None, 0))
        if fh is None:
            run_dir.mkdir(parents=True, exist_ok=True)
            fh = (run_dir / MANIFEST_LOCK_FILE).open("a+", encoding="utf-8")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except BaseException:
                fh.close()
                raise
        _MANIFEST_FDS[key] = (fh, depth + 1)
        try:
            yield
        finally:
            cur_fh, cur_depth = _MANIFEST_FDS[key]
            if cur_depth <= 1:
                _MANIFEST_FDS.pop(key, None)
                try:
                    fcntl.flock(cur_fh.fileno(), fcntl.LOCK_UN)
                finally:
                    cur_fh.close()
            else:
                _MANIFEST_FDS[key] = (cur_fh, cur_depth - 1)
    finally:
        rlock.release()


def ensure_manifest_v2(manifest: dict[str, Any]) -> dict[str, Any]:
    """Upgrade legacy v1 manifests in memory while preserving backward fields."""
    manifest.setdefault("complexity", "complex")
    roster = manifest.setdefault(
        "roster",
        {
            "architect": {"harness": "agy", "model": ""},
            "executor": {"harness": "claude", "model": ""},
            "verifier": {"harness": "agy", "model": ""},
            "tracks": {},
        },
    )
    if isinstance(roster, dict):
        for role_key, fallback_h in (("architect", "agy"), ("executor", "claude"), ("verifier", "agy")):
            roster.setdefault(role_key, {"harness": fallback_h, "model": ""})
        if not isinstance(roster.get("tracks"), dict):
            roster["tracks"] = {}

    sessions = manifest.setdefault("sessions", default_sessions_dict())
    for h, defaults in default_sessions_dict().items():
        slot = sessions.setdefault(h, dict(defaults))
        for k, v in defaults.items():
            slot.setdefault(k, v)

    if manifest.get("agy_conversation_id") and not sessions["agy"].get("session_id"):
        sessions["agy"]["session_id"] = manifest["agy_conversation_id"]
    if manifest.get("agy_transcript_path") and not sessions["agy"].get("transcript_path"):
        sessions["agy"]["transcript_path"] = manifest["agy_transcript_path"]
    if manifest.get("cc_session_id") and not sessions["claude"].get("session_id"):
        sessions["claude"]["session_id"] = manifest["cc_session_id"]
    if manifest.get("cc_bg_id") and not sessions["claude"].get("bg_id"):
        sessions["claude"]["bg_id"] = manifest["cc_bg_id"]
    if manifest.get("cc_session_name") and not sessions["claude"].get("name"):
        sessions["claude"]["name"] = manifest["cc_session_name"]
    if manifest.get("cc_kind") and not sessions["claude"].get("kind"):
        sessions["claude"]["kind"] = manifest["cc_kind"]
    if manifest.get("cc_jsonl_path") and not sessions["claude"].get("jsonl_path"):
        sessions["claude"]["jsonl_path"] = manifest["cc_jsonl_path"]

    manifest.setdefault("max_rounds", DEFAULT_MAX_ROUNDS)
    manifest.setdefault("terminal_reason", "")
    manifest.setdefault("jobs", [])
    manifest.setdefault("tmux_session", "")
    manifest.setdefault("synced_cc_assistant_ts", [])
    manifest.setdefault("synced_codex_ts", [])
    return manifest


def load_manifest(run_dir: Path) -> dict[str, Any]:
    """Read manifest.json from a run directory."""
    raw = json.loads((run_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    return ensure_manifest_v2(raw)


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    """Persist manifest.json atomically and deterministically."""
    ensure_manifest_v2(manifest)
    manifest["updated_at"] = now_iso()
    target = run_dir / MANIFEST_FILE
    tmp = run_dir / f".{MANIFEST_FILE}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, target)


def sync_manifest_session(
    manifest: dict[str, Any],
    harness: str,
    *,
    session_id: str | None = None,
    model: str | None = None,
    path: str | None = None,
    bg_id: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    last_job_id: str | None = None,
) -> None:
    """Update unified sessions[harness] and sync legacy top-level fields."""
    ensure_manifest_v2(manifest)
    slot = manifest["sessions"][harness]
    if session_id:
        slot["session_id"] = session_id
    if model:
        slot["model"] = model
    if name:
        slot["name"] = name
    if harness == "agy":
        if session_id:
            manifest["agy_conversation_id"] = session_id
        if path:
            slot["transcript_path"] = path
            manifest["agy_transcript_path"] = path
    elif harness == "claude":
        if session_id:
            manifest["cc_session_id"] = session_id
        if bg_id:
            slot["bg_id"] = bg_id
            manifest["cc_bg_id"] = bg_id
        if name:
            manifest["cc_session_name"] = name
        if kind:
            slot["kind"] = kind
            manifest["cc_kind"] = kind
        if path:
            slot["jsonl_path"] = path
            manifest["cc_jsonl_path"] = path
    elif harness == "codex":
        if path:
            slot["rollout_path"] = path
        if last_job_id:
            slot["last_job_id"] = last_job_id


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    """Load append-only events.jsonl entries."""
    events_path = run_dir / EVENTS_FILE
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError:
            continue
    return events


def format_cc_commands(manifest: dict[str, Any]) -> dict[str, str]:
    """Build exact user-copyable CLI commands for inspecting or resuming Claude Code."""
    sid = manifest.get("cc_session_id") or ""
    bg_id = manifest.get("cc_bg_id") or ""
    name = manifest.get("cc_session_name") or ""
    target = bg_id or (f'"{name}"' if " " in name else name)
    cmds: dict[str, str] = {}
    if target:
        cmds["attach"] = f"claude attach {target}"
        cmds["logs"] = f"claude logs {target}"
    if sid:
        cmds["resume"] = f"claude --resume {sid}"
    return cmds


def format_all_harness_commands(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Build native inspect/resume/tmux commands across AGY, Claude Code, and Codex."""
    ensure_manifest_v2(manifest)
    sessions = manifest["sessions"]
    agy_sid = sessions["agy"].get("session_id") or ""
    codex_sid = sessions["codex"].get("session_id") or ""
    cc_cmds = format_cc_commands(manifest)
    cc_ref = sessions["claude"].get("session_id") or sessions["claude"].get("bg_id") or ""
    if cc_ref:
        cc_cmds["terminal_view"] = (
            f"python3 -B ~/.agents/skills/{SKILL_NAME}/scripts/loopctl.py view --claude-id {cc_ref} --last 10"
        )
    repo_str = str(manifest.get("repo") or "").strip()

    agy_cmds: dict[str, str] = {}
    if agy_sid:
        agy_cmds["uri"] = f"conversation://{agy_sid}"
        if (AGY_CLI_BRAIN_ROOT / agy_sid).exists():
            agy_cmds["cli_resume"] = f"agy --conversation {agy_sid}"
        agy_cmds["terminal_view"] = (
            f"python3 -B ~/.agents/skills/{SKILL_NAME}/scripts/loopctl.py view --agy-id {agy_sid} --last 10"
        )

    codex_cmds: dict[str, str] = {}
    if codex_sid:
        if repo_str:
            codex_cmds["resume"] = f'cd "{repo_str}" && codex resume {codex_sid}'
            codex_cmds["terminal_view"] = (
                f'python3 -B ~/.agents/skills/{SKILL_NAME}/scripts/loopctl.py view --repo "{repo_str}" --target codex'
            )
        else:
            codex_cmds["resume"] = f"codex resume {codex_sid}"

    out: dict[str, dict[str, str]] = {
        "agy": agy_cmds,
        "claude": cc_cmds,
        "codex": codex_cmds,
    }
    tmux_sess = str(manifest.get("tmux_session") or "").strip()
    if not tmux_sess and manifest.get("jobs") and repo_str:
        tmux_sess = default_tmux_session_name(manifest, Path(repo_str))
    if tmux_sess:
        sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
        sock_flag = f"-L {sock} " if sock else ""
        tmux_cmds: dict[str, str] = {"session": tmux_sess, "attach": f"tmux {sock_flag}attach -t {tmux_sess}"}
        if repo_str:
            tmux_cmds["status"] = f'python3 -B ~/.agents/skills/{SKILL_NAME}/scripts/loopctl.py tmux status --repo "{repo_str}"'
        out["tmux"] = tmux_cmds
    return out


def apply_ultracode_prefix(prompt_text: str, enabled: bool) -> str:
    """Prepend `ultracode` keyword when enabled unless already present."""
    if not enabled or re.search(r"\bultracode\b", prompt_text, re.IGNORECASE):
        return prompt_text
    return f"ultracode\n\n{prompt_text.lstrip()}"


def resolve_harness_model(
    manifest: dict[str, Any],
    harness: str,
    explicit_model: str | None,
    *,
    role_name: str | None = None,
    track: str | None = None,
) -> str:
    """Resolve model slug: explicit flag > roster.tracks[track] > role > harness fallback."""
    if explicit_model is not None and explicit_model != "":
        return normalize_model(harness, explicit_model)
    roster = manifest.get("roster") or {}
    tracks = roster.get("tracks") if isinstance(roster.get("tracks"), dict) else {}
    if track and isinstance(tracks.get(track), dict):
        t_spec = tracks[track]
        if t_spec.get("harness") == harness:
            return str(t_spec.get("model") or "")
    if role_name and isinstance(roster.get(role_name), dict):
        spec = roster[role_name]
        if spec.get("harness") == harness and spec.get("model"):
            return str(spec["model"])
    for key in ("advisor", "executor", "verifier", "architect"):
        spec = roster.get(key)
        if isinstance(spec, dict) and spec.get("harness") == harness and spec.get("model"):
            return str(spec["model"])
    return ""


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically via sibling temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _summarize_job_status(job: dict[str, Any]) -> str:
    """Compute concise status label for a recorded job without slow CLI calls."""
    recorded = str(job.get("status") or "").strip()
    if recorded in ("COMPLETED", "BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED", "STALLED", "STOPPED"):
        return recorded
    ex = _is_job_exit_recorded(job)
    if ex is not None:
        return "COMPLETED" if int(ex.get("exit_code", 1)) == 0 else "PROCESS_EXITED_FAILED"
    pid = int(job.get("pid") or 0)
    if pid > 0 and is_pid_alive(pid):
        return "RUNNING"
    if job.get("attach_bg_id"):
        return recorded or "BACKGROUND"
    return recorded or "UNKNOWN"


def render_dialogue(repo: Path, run_dir: Path) -> Path:
    """Render DIALOGUE.md and mirror it to <repo>/.agent-loop/LATEST.md."""
    manifest = load_manifest(run_dir)
    events = load_events(run_dir)
    current_head = git_head_short(repo)
    base_commit = manifest.get("base_commit") or current_head

    sessions = manifest["sessions"]
    agy_id = sessions["agy"].get("session_id") or ""
    agy_link = f"[{agy_id}](conversation://{agy_id})" if agy_id else "未绑定"
    agy_log = sessions["agy"].get("transcript_path") or "—"

    cc_sid = sessions["claude"].get("session_id") or "未绑定"
    cc_kind = sessions["claude"].get("kind") or "—"
    cc_jsonl = sessions["claude"].get("jsonl_path") or "—"

    codex_sid = sessions["codex"].get("session_id") or "未绑定"
    codex_rollout = sessions["codex"].get("rollout_path") or "—"

    all_cmds = format_all_harness_commands(manifest)
    tmux_sess = default_tmux_session_name(manifest, repo) if manifest.get("tmux_session") or manifest.get("jobs") else ""
    complexity = manifest.get("complexity") or "complex"
    mode_label = "complex (ultracode)" if complexity == "complex" else "simple"

    term_reason = str(manifest.get("terminal_reason") or "").strip()
    term_part = f" · **终止原因**：`{term_reason}`" if term_reason else ""
    lines: list[str] = [
        f"# 多 Harness 闭环对话台账 · {manifest.get('slug', '')}",
        "",
        f"> **Run ID**：`{manifest.get('run_id')}` · **复杂度/模式**：`{mode_label}` · **当前阶段**：`{manifest.get('phase')}` · **终审结论**：`{manifest.get('verdict')}` · **返工轮次**：`{manifest.get('round', 0)}/{manifest.get('max_rounds', DEFAULT_MAX_ROUNDS)}`{term_part} · **更新**：`{manifest.get('updated_at')}`",
        "",
        "## 当前角色花名册（Role Roster）",
        "",
        "| 角色 / 赛道 | Harness | 模型 |",
        "|---|---|---|",
    ]
    roster = manifest.get("roster") or {}
    for role_key in ("architect", "executor", "verifier", "advisor"):
        spec = roster.get(role_key)
        if isinstance(spec, dict) and spec.get("harness"):
            model_disp = f"`{spec['model']}`" if spec.get("model") else "_继承全局默认_"
            lines.append(f"| **{role_key}** | `{spec['harness']}` | {model_disp} |")
    tracks = roster.get("tracks") if isinstance(roster.get("tracks"), dict) else {}
    for t_name, t_spec in tracks.items():
        if isinstance(t_spec, dict) and t_spec.get("harness"):
            model_disp = f"`{t_spec['model']}`" if t_spec.get("model") else "_继承全局默认_"
            lines.append(f"| `track:{t_name}` | `{t_spec['harness']}` | {model_disp} |")

    lines.extend([
        "",
        "## 多端会话与 Tmux 实时导航",
        "",
        "| 维度 | 标识 / 路径 | 快速查看 / 接入 |",
        "|---|---|---|",
    ])
    if tmux_sess:
        sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
        sock_flag = f"-L {sock} " if sock else ""
        lines.append(f"| **Tmux 总览实时看进度** | `tmux {sock_flag}attach -t {tmux_sess}` | Window 0 为全波总览看板（`Ctrl-b w` 选窗，`Ctrl-b d` 脱离） |")
    lines.extend([
        f"| **Antigravity (agy)** | {agy_link} | 日志：`{agy_log}` |",
        f"| **Claude Code (claude)** | `{cc_sid}` (`{cc_kind}`) | 日志：`{cc_jsonl}` |",
        f"| **Codex (codex)** | `{codex_sid}` | 日志：`{codex_rollout}` |",
        f"| **Git 基线 → 当前** | `{base_commit}` → `{current_head}` | 仓库：`{manifest.get('repo')}` |",
    ])
    for h_key, label in (("agy", "接入 AGY"), ("claude", "实时看/接管 CC"), ("codex", "接入 Codex")):
        cmds = all_cmds.get(h_key) or {}
        if cmds:
            cmd_parts = [f"`{v}`" for v in cmds.values()]
            lines.append(f"| **{label}** | {' · '.join(cmd_parts)} | 本地终端 / IDE |")

    jobs = [j for j in (manifest.get("jobs") or []) if isinstance(j, dict)]
    if jobs:
        lines.extend([
            "",
            "## 执行任务清单（Jobs / Worktree / Tmux 窗口）",
            "",
            "| Job ID | Wave | Harness (模型) | 工作目录 / 分支 | Tmux 窗口 | 状态 |",
            "|---|---|---|---|---|---|",
        ])
        for j in jobs[-20:]:
            jid = j.get("job_id") or "-"
            wave = j.get("wave") or "-"
            h_disp = j.get("harness") or "-"
            if j.get("model"):
                h_disp = f"{h_disp}:`{j['model']}`"
            wd = j.get("workdir") or str(repo)
            br = j.get("branch") or ""
            wd_disp = f"`{Path(wd).name}`" + (f" (`{br}`)" if br else "")
            t_target = j.get("tmux_target") or "-"
            st = _summarize_job_status(j)
            lines.append(f"| `{jid}` | `{wave}` | {h_disp} | {wd_disp} | `{t_target}` | `{st}` |")

    docs = manifest.get("docs") or []
    if docs:
        lines.extend(["", "## 规格与交接文档", ""])
        for doc in docs:
            doc_path = repo / doc if not Path(doc).is_absolute() else Path(doc)
            lines.append(f"- [{doc}](file://{doc_path})")

    lines.extend(["", "## 双向交接与审查时间线", ""])
    if not events:
        lines.append("_暂无事件记录。_")
    else:
        for idx, ev in enumerate(events, 1):
            label = EVENT_LABELS.get(ev.get("event", ""), ev.get("event", "event"))
            role = ev.get("role", "unknown").upper()
            ts = ev.get("timestamp", "")
            head = ev.get("git_head", "")
            summary = ev.get("summary", "")
            body_hash = ev.get("body_sha256", "")
            commits = ev.get("commits") or ""
            verdict = ev.get("verdict") or ""
            target = ev.get("target") or ""
            model_used = ev.get("model") or ""
            lines.append(f"### {idx}. [{role}] {label}（{ts}）")
            meta_bits = [f"事件：`{ev.get('event')}`", f"HEAD：`{head}`", f"摘要哈希：`{body_hash}`"]
            if target:
                meta_bits.append(f"目标：`{target}`")
            if model_used:
                meta_bits.append(f"模型：`{model_used}`")
            if verdict:
                meta_bits.append(f"裁决：`{verdict}`")
            if commits:
                meta_bits.append(f"提交：`{commits}`")
            lines.append("- " + " · ".join(meta_bits))
            if summary:
                lines.append(f"- **摘要**：{summary}")
            body = (ev.get("body") or "").rstrip()
            if body:
                lines.extend(["", "<details>", "<summary>展开完整内容</summary>", "", body, "", "</details>"])
            lines.append("")

    content = "\n".join(lines).rstrip() + "\n"
    dialogue_path = run_dir / DIALOGUE_FILE
    _atomic_write_text(dialogue_path, content)
    _atomic_write_text(repo / LOOP_DIR_NAME / LATEST_MD_FILE, content)
    return dialogue_path


def append_event(
    repo: Path,
    run_dir: Path,
    *,
    role: str,
    event: str,
    summary: str,
    body: str,
    phase: str | None = None,
    verdict: str | None = None,
    terminal_reason: str | None = None,
    commits: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one structured event to events.jsonl, update manifest, and re-render DIALOGUE.md."""
    with _manifest_lock(run_dir):
        manifest = load_manifest(run_dir)
        if phase:
            manifest["phase"] = phase
        if verdict:
            manifest["verdict"] = verdict
        if terminal_reason is not None:
            manifest["terminal_reason"] = terminal_reason
        if event in ("rework_to_cc", "rework"):
            manifest["round"] = int(manifest.get("round", 0)) + 1

        entry: dict[str, Any] = {
            "timestamp": now_iso(),
            "role": role,
            "event": event,
            "summary": summary,
            "body": body,
            "body_sha256": sha256_prefix(body or summary),
            "git_head": git_head_short(repo),
        }
        if phase:
            entry["phase"] = phase
        if verdict:
            entry["verdict"] = verdict
        if commits:
            entry["commits"] = commits
        if extra:
            entry.update(extra)

        with (run_dir / EVENTS_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

        save_manifest(run_dir, manifest)
        dialogue_path = render_dialogue(repo, run_dir)
        entry["dialogue_md"] = str(dialogue_path)
        return entry


def read_prompt_or_body(text_arg: str | None, file_arg: str | None) -> str:
    """Resolve content from --body/--prompt or --body-file/--prompt-file."""
    if file_arg:
        return Path(file_arg).read_text(encoding="utf-8")
    return text_arg or ""


def find_agy_transcript(agy_id: str) -> Path | None:
    """Locate AGY transcript.jsonl across both desktop and CLI brain directories."""
    for root in (AGY_BRAIN_ROOT, AGY_CLI_BRAIN_ROOT):
        candidate = root / agy_id / ".system_generated" / "logs" / "transcript.jsonl"
        if candidate.exists():
            return candidate
    return None


def list_recent_agy_sessions(limit: int = 8) -> list[dict[str, str]]:
    """List recent AGY conversation directories from desktop and CLI roots."""
    entries: list[tuple[float, dict[str, str]]] = []
    for source, root in (("desktop", AGY_BRAIN_ROOT), ("cli", AGY_CLI_BRAIN_ROOT)):
        if not root.exists():
            continue
        for d in root.iterdir():
            t = d / ".system_generated" / "logs" / "transcript.jsonl"
            if d.is_dir() and t.exists():
                entries.append(
                    (t.stat().st_mtime, {"agy_id": d.name, "source": source, "transcript": str(t)})
                )
    entries.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in entries[:limit]]


def find_cc_jsonl_global(claude_id: str) -> Path | None:
    """Locate a Claude Code JSONL globally by full UUID or 8-char bg_id prefix."""
    cid = (claude_id or "").strip()
    if not cid or not CLAUDE_PROJECTS_ROOT.exists():
        return None
    direct_matches = sorted(
        CLAUDE_PROJECTS_ROOT.glob(f"*/{cid}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if direct_matches:
        return direct_matches[0]
    prefix_matches = sorted(
        CLAUDE_PROJECTS_ROOT.glob(f"*/{cid}-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if prefix_matches:
        return prefix_matches[0]
    for agent in query_claude_agents(None):
        if agent.get("id") == cid or agent.get("sessionId") == cid:
            full_sid = str(agent.get("sessionId") or "")
            if full_sid:
                hits = list(CLAUDE_PROJECTS_ROOT.glob(f"*/{full_sid}.jsonl"))
                if hits:
                    return hits[0]
    return None


def find_cc_jsonl(repo: Path, session_id: str | None) -> Path | None:
    """Locate the JSONL transcript file for a Claude Code session (with global fallback)."""
    proj_dir = encode_claude_project_dir(repo)
    if session_id:
        direct = proj_dir / f"{session_id}.jsonl"
        if direct.exists():
            return direct
        if len(session_id) < 36 and proj_dir.exists():
            pref = sorted(
                (p for p in proj_dir.glob(f"{session_id}-*.jsonl") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if pref:
                return pref[0]
        global_hit = find_cc_jsonl_global(session_id)
        if global_hit:
            return global_hit
    if proj_dir.exists():
        candidates = sorted(
            (p for p in proj_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def read_cc_session_meta(path: Path) -> dict[str, str]:
    """Extract sessionId, cwd, and gitBranch from the head of a Claude JSONL file."""
    meta = {"session_id": path.stem, "cwd": "", "git_branch": ""}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(30):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("sessionId") and not meta["session_id"]:
                    meta["session_id"] = str(rec["sessionId"])
                if rec.get("cwd") and not meta["cwd"]:
                    meta["cwd"] = str(rec["cwd"])
                if rec.get("gitBranch") and not meta["git_branch"]:
                    meta["git_branch"] = str(rec["gitBranch"])
                if meta["cwd"] and meta["session_id"]:
                    break
    except OSError:
        pass
    return meta


def _read_tail_lines(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Read tail lines efficiently without loading huge multi-hundred-MB logs into memory."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            raw = fh.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            return lines[1:] if len(lines) > 1 else lines
        return fh.read().decode("utf-8", errors="replace").splitlines()


def _extract_claude_content_summary(content: Any) -> tuple[str, list[str]]:
    """Extract readable text and tool names from a Claude message content field."""
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("<task-notification>"):
            first = text.splitlines()[1] if len(text.splitlines()) > 1 else text
            return f"[task-notification] {first[:140]}", []
        return text, []
    if not isinstance(content, list):
        return "", []
    texts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif btype == "tool_use":
            name = block.get("name") or "tool"
            tools.append(str(name))
        elif btype == "tool_result":
            tools.append("tool_result")
    return "\n".join(texts).strip(), tools


def _parse_iso_age_seconds(ts: str | None) -> float | None:
    """Return seconds elapsed since an ISO-8601 timestamp, or None on parse error."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    except ValueError:
        return None


def parse_cc_jsonl_tail(
    path: Path,
    tail_turns: int,
    *,
    include_full_text: bool = False,
) -> dict[str, Any]:
    """Parse recent turns and latest assistant/user telemetry from a Claude JSONL log."""
    lines = _read_tail_lines(path)
    turns: list[dict[str, Any]] = []
    last_record_type: str | None = None
    last_record_ts: str | None = None
    last_stop_reason: str | None = None
    last_assistant_text_msg: dict[str, Any] | None = None
    has_stop_failure = False

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if rtype:
            last_record_type = rtype
        if ts:
            last_record_ts = ts
        if rtype == "StopFailure":
            has_stop_failure = True

        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        if rtype not in ("user", "assistant") or not msg:
            continue

        text, tools = _extract_claude_content_summary(msg.get("content"))
        sr = msg.get("stop_reason")
        if rtype == "assistant" and sr:
            last_stop_reason = sr
            has_stop_failure = False

        if rtype == "user" and not text and tools == ["tool_result"]:
            continue

        turn = {
            "role": rtype,
            "timestamp": ts,
            "stop_reason": sr,
            "tools": tools,
            "text": text,
        }
        turns.append(turn)
        if rtype == "assistant" and text:
            last_assistant_text_msg = turn

    recent = turns[-tail_turns:] if tail_turns > 0 else turns
    for item in recent:
        item["text_preview"] = (
            item["text"][:1200] + "\n...[truncated]"
            if len(item["text"]) > 1200
            else item["text"]
        )

    return {
        "jsonl_path": str(path),
        "file_size_bytes": path.stat().st_size,
        "last_record_type": last_record_type,
        "last_record_ts": last_record_ts,
        "last_record_age_sec": round(_parse_iso_age_seconds(last_record_ts) or -1, 1),
        "last_stop_reason": last_stop_reason,
        "has_stop_failure": has_stop_failure,
        "last_assistant_message": last_assistant_text_msg,
        "recent_turns": [
            (dict(t) if include_full_text else {k: v for k, v in t.items() if k != "text"})
            for t in recent
        ],
    }


def classify_cc_state(agent_entry: dict[str, Any] | None, jsonl_info: dict[str, Any] | None) -> str:
    """Normalize Claude Code live state into RUNNING, IDLE_COMPLETED, BLOCKED_OR_FAILED, or UNKNOWN."""
    if agent_entry:
        status = agent_entry.get("status")
        state = agent_entry.get("state")
        if status == "busy" or state == "running":
            return "RUNNING"
        if state in ("blocked", "failed"):
            return "BLOCKED_OR_FAILED"
        if status == "idle" or state == "done":
            if jsonl_info and jsonl_info.get("has_stop_failure"):
                return "BLOCKED_OR_FAILED"
            return "IDLE_COMPLETED"

    if jsonl_info:
        if jsonl_info.get("has_stop_failure"):
            return "BLOCKED_OR_FAILED"
        age = jsonl_info.get("last_record_age_sec", -1)
        if jsonl_info.get("last_stop_reason") == "end_turn" and jsonl_info.get("last_assistant_message"):
            return "IDLE_COMPLETED"
        if 0 <= age < 180 and jsonl_info.get("last_stop_reason") == "tool_use":
            return "RUNNING"
        if jsonl_info.get("last_assistant_message"):
            return "IDLE_COMPLETED"
    return "UNKNOWN"


def lookup_codex_thread_names() -> dict[str, str]:
    """Read thread_name mapping from ~/.codex/session_index.jsonl."""
    names: dict[str, str] = {}
    if not CODEX_INDEX_FILE.exists():
        return names
    for raw in CODEX_INDEX_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("id") and obj.get("thread_name"):
            names[str(obj["id"])] = str(obj["thread_name"])
    return names


def read_codex_session_meta(path: Path) -> dict[str, Any]:
    """Read the leading session_meta payload from a Codex rollout JSONL."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(10):
                line = fh.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "session_meta":
                    pl = rec.get("payload")
                    return pl if isinstance(pl, dict) else {}
    except OSError:
        return {}
    return {}


def find_codex_rollout(repo: Path, session_id: str | None) -> Path | None:
    """Locate Codex rollout-*.jsonl matching session_id or repo cwd."""
    if not CODEX_SESSIONS_ROOT.exists():
        return None
    if session_id:
        matches = sorted(
            CODEX_SESSIONS_ROOT.rglob(f"*{session_id}.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0]
        return None

    repo_resolved = repo.resolve()
    candidates = sorted(
        CODEX_SESSIONS_ROOT.rglob("rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in candidates[:60]:
        meta = read_codex_session_meta(p)
        cwd = meta.get("cwd")
        if not cwd:
            continue
        try:
            if Path(cwd).resolve() == repo_resolved:
                return p
        except OSError:
            continue
    return None


def _extract_codex_message_text(content: Any) -> str:
    """Extract text from Codex response_item content list."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts).strip()


def parse_codex_rollout_tail(
    path: Path,
    tail_turns: int,
    *,
    include_full_text: bool = False,
) -> dict[str, Any]:
    """Parse session_meta, recent turns, and task_complete from a Codex rollout JSONL."""
    meta = read_codex_session_meta(path)
    lines = _read_tail_lines(path)
    turns: list[dict[str, Any]] = []
    last_event_type: str | None = None
    last_event_ts: str | None = None
    last_task_complete: dict[str, Any] | None = None
    last_assistant_msg: dict[str, Any] | None = None
    has_aborted = False

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        pl = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ptype = pl.get("type")

        if ts:
            last_event_ts = ts
        if rtype:
            last_event_type = f"{rtype}/{ptype}" if ptype else str(rtype)

        if rtype == "event_msg" and ptype == "turn_aborted":
            has_aborted = True
        elif rtype == "event_msg" and ptype == "task_complete":
            has_aborted = False
            msg_text = str(pl.get("last_agent_message") or "").strip()
            if msg_text or last_task_complete is None:
                last_task_complete = {
                    "timestamp": ts,
                    "text": msg_text,
                    "error": pl.get("error"),
                }
        elif rtype == "response_item" and ptype == "message":
            role = pl.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _extract_codex_message_text(pl.get("content"))
            if not text:
                continue
            if role == "user" and (
                text.startswith("# AGENTS.md instructions")
                or text.startswith("<environment_context>")
                or text.startswith("<permissions instructions>")
                or text.startswith("<collaboration_mode>")
            ):
                continue
            turn = {"role": role, "timestamp": ts, "text": text}
            turns.append(turn)
            if role == "assistant":
                last_assistant_msg = turn

    final_msg = None
    if last_task_complete and last_task_complete.get("text"):
        final_msg = {"role": "codex", "timestamp": last_task_complete["timestamp"], "text": last_task_complete["text"]}
    elif last_assistant_msg:
        final_msg = last_assistant_msg

    recent = turns[-tail_turns:] if tail_turns > 0 else turns
    for item in recent:
        item["text_preview"] = (
            item["text"][:1200] + "\n...[truncated]"
            if len(item["text"]) > 1200
            else item["text"]
        )

    return {
        "rollout_path": str(path),
        "session_id": meta.get("id") or "",
        "cwd": meta.get("cwd") or "",
        "originator": meta.get("originator") or "",
        "file_size_bytes": path.stat().st_size,
        "last_event_type": last_event_type,
        "last_event_ts": last_event_ts,
        "last_event_age_sec": round(_parse_iso_age_seconds(last_event_ts) or -1, 1),
        "has_task_complete": last_task_complete is not None,
        "has_aborted": has_aborted,
        "last_assistant_message": final_msg,
        "recent_turns": [
            (dict(t) if include_full_text else {k: v for k, v in t.items() if k != "text"})
            for t in recent
        ],
    }


def is_pid_alive(pid: int | None) -> bool:
    """Return True if a local OS process with pid is currently alive (and not a zombie)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        ps = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        stat = ps.stdout.strip()
        if ps.returncode == 0 and stat.startswith("Z"):
            return False
    except (OSError, subprocess.SubprocessError):
        pass
    return True


def classify_codex_state(job_alive: bool, rollout_info: dict[str, Any] | None) -> str:
    """Normalize Codex state into RUNNING, IDLE_COMPLETED, BLOCKED_OR_FAILED, or UNKNOWN."""
    if job_alive:
        return "RUNNING"
    if rollout_info:
        if rollout_info.get("has_aborted") and not rollout_info.get("has_task_complete"):
            return "BLOCKED_OR_FAILED"
        if rollout_info.get("has_task_complete") or rollout_info.get("last_assistant_message"):
            return "IDLE_COMPLETED"
        age = rollout_info.get("last_event_age_sec", -1)
        if 0 <= age < 180:
            return "RUNNING"
    return "UNKNOWN"


def clean_agy_user_text(raw: str) -> str:
    """Extract clean user prompt text from AGY USER_INPUT step content."""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    return raw.strip()


def check_recursion_depth(allow_nested: bool) -> tuple[bool, int, dict[str, str]]:
    """Enforce HARNESS_LOOP_DEPTH < 2 guardrail (with AGY_LOOP_DEPTH fallback)."""
    depth = 0
    for var_name in ("HARNESS_LOOP_DEPTH", "AGY_LOOP_DEPTH"):
        try:
            depth = max(depth, int(os.environ.get(var_name, "0")))
        except ValueError:
            continue
    if depth >= 2 and not allow_nested:
        return False, depth, dict(os.environ)
    next_env = dict(os.environ)
    next_depth = str(depth + 1)
    next_env["HARNESS_LOOP_DEPTH"] = next_depth
    next_env["AGY_LOOP_DEPTH"] = next_depth
    return True, depth, next_env


def find_active_write_conflicts(
    repo: Path,
    manifest: dict[str, Any],
    target_workdir: Path | None = None,
) -> list[dict[str, Any]]:
    """Detect active Claude or background write jobs targeting the same workdir."""
    scope_dir = (target_workdir or repo).resolve()
    conflicts: list[dict[str, Any]] = []
    seen_bg_or_sid: set[str] = set()
    for a in query_claude_agents(scope_dir):
        if a.get("status") == "busy" or a.get("state") == "running":
            sid = str(a.get("sessionId") or "")
            bg = str(a.get("id") or "")
            if sid:
                seen_bg_or_sid.add(sid)
            if bg:
                seen_bg_or_sid.add(bg)
            conflicts.append(
                {"harness": "claude", "workdir": str(scope_dir), "session_id": sid, "bg_id": bg, "detail": a}
            )
    for job in manifest.get("jobs") or []:
        if not isinstance(job, dict) or job.get("mode") != "write":
            continue
        try:
            job_wd = Path(str(job.get("workdir") or repo)).resolve()
        except OSError:
            continue
        if job_wd != scope_dir:
            continue
        if job.get("status") in ("COMPLETED", "BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED", "STALLED", "STOPPED"):
            continue
        if _is_job_exit_recorded(job) is not None:
            continue
        if job.get("attach_bg_id"):
            continue
        pid = int(job.get("pid") or 0)
        if pid > 0 and is_pid_alive(pid):
            conflicts.append(
                {"harness": job.get("harness"), "workdir": str(job_wd), "job_id": job.get("job_id"), "pid": pid}
            )
    return conflicts


def _tmux_run(args: list[str], *, timeout: int = 8) -> tuple[int, str, str]:
    """Execute a tmux command honoring HARNESS_LOOP_TMUX_SOCKET."""
    try:
        proc = subprocess.run(
            _tmux_argv(*args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _tmux_session_exists(sess: str) -> bool:
    rc, _, _ = _tmux_run(["has-session", "-t", f"={sess}"])
    return rc == 0


def _tmux_list_windows(sess: str) -> list[dict[str, Any]]:
    """List windows in a tmux session with stable IDs and pane liveness."""
    fmt = "#{window_index}|#{window_id}|#{window_name}|#{pane_id}|#{pane_pid}|#{pane_dead}"
    rc, out, _ = _tmux_run(["list-windows", "-t", f"={sess}", "-F", fmt])
    if rc != 0 or not out:
        return []
    windows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 6:
            continue
        try:
            win_idx = int(parts[0])
        except ValueError:
            win_idx = -1
        try:
            pane_pid = int(parts[4])
        except ValueError:
            pane_pid = 0
        windows.append(
            {
                "window_index": win_idx,
                "window_id": parts[1],
                "window_name": parts[2],
                "pane_id": parts[3],
                "pane_pid": pane_pid,
                "pane_dead": parts[5] == "1",
            }
        )
    return windows


def _apply_tmux_session_options(sess: str) -> None:
    """Configure mouse & remain-on-exit strictly scoped to this harness-loop tmux session."""
    for opts in (
        ["set-option", "-t", f"={sess}", "mouse", "on"],
        ["set-option", "-t", f"={sess}", "history-limit", "50000"],
        ["set-option", "-t", f"={sess}", "-w", "remain-on-exit", "on"],
        ["set-option", "-t", f"={sess}", "-w", "automatic-rename", "off"],
        ["set-option", "-t", f"={sess}", "-w", "allow-rename", "off"],
    ):
        _tmux_run(opts)


def ensure_tmux_session(sess: str, repo: Path) -> bool:
    """Ensure tmux session exists with Window 0 running `tmux dashboard`."""
    if not tmux_bin():
        return False
    if _tmux_session_exists(sess):
        _apply_tmux_session_options(sess)
        return True
    dash_cmd = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "tmux",
        "dashboard",
        "--repo",
        str(repo),
        "--interval",
        "3",
    ]
    sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
    argv = ["new-session", "-d", "-s", sess, "-n", "dashboard", "-c", str(repo)]
    if sock:
        argv.extend(["-e", f"{TMUX_SOCKET_ENV}={sock}"])
    argv.extend(dash_cmd)
    rc, _, err = _tmux_run(argv, timeout=10)
    if rc != 0 and not _tmux_session_exists(sess):
        raise RuntimeError(f"Failed to start tmux session {sess}: {err}")
    _apply_tmux_session_options(sess)
    return True


def _launch_in_tmux(
    sess: str,
    repo: Path,
    *,
    spec_path: Path,
    win_name: str,
    workdir: Path,
    pid_file: Path | None = None,
    wait_for_child_pid: bool = True,
) -> dict[str, Any]:
    """Open a named tmux window executing `_run-job --spec <spec_path>` and return metadata."""
    if not ensure_tmux_session(sess, repo):
        raise RuntimeError("tmux binary unavailable")

    sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
    fmt = "#{window_index}|#{window_id}|#{pane_id}|#{pane_pid}"
    cmd = [
        "new-window",
        "-d",
        "-P",
        "-F",
        fmt,
        "-t",
        f"={sess}:",
        "-n",
        win_name,
        "-c",
        str(workdir),
    ]
    if sock:
        cmd.extend(["-e", f"{TMUX_SOCKET_ENV}={sock}"])
    cmd.extend([sys.executable, "-B", str(Path(__file__).resolve()), "_run-job", "--spec", str(spec_path)])
    rc, out, err = _tmux_run(cmd, timeout=10)
    if rc != 0 or "|" not in out:
        raise RuntimeError(f"tmux new-window failed in {sess}: {err or out}")

    parts = out.splitlines()[-1].split("|")
    win_idx = int(parts[0])
    win_id = parts[1]
    pane_id = parts[2]
    pane_pid = int(parts[3]) if parts[3].isdigit() else 0

    for opts in (
        ["set-option", "-t", win_id, "-w", "remain-on-exit", "on"],
        ["set-option", "-t", win_id, "-w", "automatic-rename", "off"],
        ["set-option", "-t", win_id, "-w", "allow-rename", "off"],
    ):
        _tmux_run(opts)

    child_pid = 0
    if pid_file and wait_for_child_pid:
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if pid_file.exists():
                try:
                    pdata = json.loads(pid_file.read_text(encoding="utf-8", errors="replace"))
                    child_pid = int(pdata.get("child_pid") or 0)
                    if child_pid > 0:
                        break
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
            time.sleep(0.05)

    return {
        "tmux_session": sess,
        "tmux_window_index": win_idx,
        "tmux_window_id": win_id,
        "tmux_window_name": win_name,
        "tmux_pane_id": pane_id,
        "tmux_pane_pid": pane_pid,
        "tmux_target": f"{sess}:{win_idx}",
        "child_pid": child_pid,
    }


def _format_live_stream_line(harness: str, raw_line: str) -> str:
    """Render raw CLI stream/JSON lines into concise timestamped progress text for tmux."""
    stripped = raw_line.strip()
    if not stripped:
        return ""
    hhmmss = datetime.now().strftime("%H:%M:%S")
    if harness == "codex" and stripped.startswith("{"):
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return f"[{hhmmss}] {stripped}"
        if isinstance(ev, dict):
            etype = ev.get("type")
            if etype == "thread.started":
                return f"[{hhmmss}] [SESSION] thread_id={ev.get('thread_id') or '-'}"
            if etype == "turn.started":
                return f"[{hhmmss}] [TURN] started"
            if etype == "turn.completed":
                usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
                return (
                    f"[{hhmmss}] [USAGE] input={usage.get('input_tokens', '-')} "
                    f"cached={usage.get('cached_input_tokens', '-')} output={usage.get('output_tokens', '-')}"
                )
            if etype in ("item.started", "item.completed"):
                item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
                itype = item.get("type")
                if itype == "command_execution":
                    cmd_txt = str(item.get("command") or "").strip().splitlines()[0][:140]
                    if etype == "item.started":
                        return f"[{hhmmss}] [CMD] $ {cmd_txt}"
                    code = item.get("exit_code")
                    return f"[{hhmmss}] [CMD exit={code}] $ {cmd_txt}"
                if itype == "reasoning" and etype == "item.completed":
                    txt = " ".join(str(item.get("text") or "").split())[:180]
                    return f"[{hhmmss}] [THINK] {txt}" if txt else ""
                if itype == "file_change" and etype == "item.completed":
                    changes = item.get("changes") or []
                    if isinstance(changes, list):
                        summary = ", ".join(
                            f"{c.get('kind', '?')}:{c.get('path', '?')}"
                            for c in changes[:8]
                            if isinstance(c, dict)
                        )
                        return f"[{hhmmss}] [FILES] {summary}" if summary else ""
                if itype == "agent_message" and etype == "item.completed":
                    msg_txt = str(item.get("text") or "").strip()
                    return f"\n[{hhmmss}] [CODEX REPLY]\n{msg_txt}\n" if msg_txt else ""
                if itype == "error":
                    return f"[{hhmmss}] [ERROR] {item.get('message') or item}"
            if etype == "error":
                return f"[{hhmmss}] [ERROR] {ev.get('message') or ev}"
            return ""
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                txt = str(obj.get("result") or obj.get("response") or obj.get("text") or obj.get("content") or "").strip()
                if txt:
                    return f"\n[{hhmmss}] [{harness.upper()}]\n{txt}\n"
        except json.JSONDecodeError:
            pass
    return f"[{hhmmss}] {raw_line.rstrip()}"


def cmd__run_job(args: argparse.Namespace) -> int:
    """Internal pane runner started inside tmux to preserve streams, PID, and exit code."""
    spec_path = Path(args.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    job_id = str(spec.get("job_id") or "")
    wave = str(spec.get("wave") or "-")
    harness = str(spec.get("harness") or "")
    model = str(spec.get("model") or "") or "全局默认"
    workdir = Path(str(spec.get("workdir") or ".")).resolve()
    branch = str(spec.get("branch") or "")
    attach_bg_id = str(spec.get("attach_bg_id") or "")
    resume_hint = str(spec.get("resume_hint") or "")
    prompt_file = str(spec.get("prompt_file") or "")

    print("=" * 72)
    print(f"[harness-loop] Job: {job_id} | Wave: {wave} | Harness: {harness} ({model})")
    print(f"Workdir: {workdir}" + (f" [branch: {branch}]" if branch else ""))
    if prompt_file:
        print(f"Prompt : {prompt_file}")
    if resume_hint:
        print(f"Resume : {resume_hint}")
    print("Tmux   : 鼠标点底栏切窗 | Ctrl-b w 选窗 | Ctrl-b 0 回看板 | Ctrl-b d 后台离开(不中断任务)")
    print("=" * 72, flush=True)

    env = dict(os.environ)
    for k, v in (spec.get("env_overrides") or {}).items():
        env[str(k)] = str(v)

    if attach_bg_id:
        binary = harness_bin("claude") or "claude"
        proc = subprocess.run([binary, "attach", attach_bg_id], cwd=str(workdir), env=env, check=False)
        print("\n" + "-" * 72)
        print(f"[harness-loop] 已退出 attach 视图 (exit={proc.returncode})。")
        print(f"后台若仍在执行，可随时运行: claude logs {attach_bg_id}  或回到 Window 0 查看实时状态。")
        if resume_hint:
            print(f"恢复/接管命令: {resume_hint}")
        print("-" * 72, flush=True)
        return 0

    argv = [str(x) for x in (spec.get("argv") or [])]
    stdout_log = Path(str(spec.get("stdout_jsonl") or ""))
    stderr_log = stdout_log.with_suffix(".stderr.log") if stdout_log.name else None
    pid_file = Path(str(spec["pid_file"])) if spec.get("pid_file") else None
    exit_file = Path(str(spec["exit_file"])) if spec.get("exit_file") else None

    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    start_ts = time.monotonic()
    out_fh = stdout_log.open("w", encoding="utf-8", buffering=1)
    err_fh = stderr_log.open("w", encoding="utf-8", buffering=1) if stderr_log else None
    print_lock = threading.Lock()

    proc = subprocess.Popen(
        argv,
        cwd=str(workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if pid_file:
        _atomic_write_text(
            pid_file,
            json.dumps(
                {
                    "wrapper_pid": os.getpid(),
                    "child_pid": proc.pid,
                    "started_at": now_iso(),
                },
                ensure_ascii=False,
            ),
        )

    def _drain_stderr() -> None:
        if not proc.stderr:
            return
        for err_line in proc.stderr:
            if err_fh:
                err_fh.write(err_line)
                err_fh.flush()
            clean_err = err_line.rstrip()
            if clean_err:
                with print_lock:
                    print(f"[stderr] {clean_err}", flush=True)

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    try:
        if proc.stdout:
            for line in proc.stdout:
                out_fh.write(line)
                out_fh.flush()
                rendered = _format_live_stream_line(harness, line)
                if rendered:
                    with print_lock:
                        print(rendered, flush=True)
    finally:
        rc = proc.wait()
        err_thread.join(timeout=5)
        out_fh.close()
        if err_fh:
            err_fh.close()

    elapsed = round(time.monotonic() - start_ts, 2)
    if exit_file:
        _atomic_write_text(
            exit_file,
            json.dumps(
                {
                    "exit_code": rc,
                    "child_pid": proc.pid,
                    "wrapper_pid": os.getpid(),
                    "elapsed_sec": elapsed,
                    "finished_at": now_iso(),
                },
                ensure_ascii=False,
            ),
        )

    print("\n" + "=" * 72)
    print(f"[harness-loop] Job {job_id} 结束 | exit_code={rc} | elapsed={elapsed}s")
    if resume_hint:
        print(f"继续/查看: {resume_hint}")
    print("=" * 72, flush=True)
    return rc


def _collect_job_live_row(repo: Path, job: dict[str, Any], win_by_id: dict[str, dict[str, Any]], win_by_idx: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Gather current live status row for one manifest job entry."""
    jid = str(job.get("job_id") or "")
    harness = str(job.get("harness") or "")
    workdir = Path(str(job.get("workdir") or repo)).resolve()
    branch = str(job.get("branch") or "")
    sid = str(job.get("session_id") or "")
    bg_id = str(job.get("bg_id") or "")
    log_file = str(job.get("stdout_jsonl") or "")
    model = str(job.get("model") or "")

    win_id = str(job.get("tmux_window_id") or "")
    win_idx = job.get("tmux_window_index")
    win_info = win_by_id.get(win_id) or (win_by_idx.get(win_idx) if isinstance(win_idx, int) else None)
    pane_dead = bool(win_info.get("pane_dead")) if win_info else None

    state = str(job.get("status") or "")
    ex = _is_job_exit_recorded(job)
    if ex is not None:
        state = "COMPLETED" if int(ex.get("exit_code", 1)) == 0 else "PROCESS_EXITED_FAILED"
    elif state in ("COMPLETED", "BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED", "STALLED", "STOPPED"):
        pass
    elif harness == "claude" and (bg_id or sid) and job.get("attach_bg_id"):
        agents = query_claude_agents(workdir)
        matched = next(
            (a for a in agents if (bg_id and a.get("id") == bg_id) or (sid and a.get("sessionId") == sid)),
            None,
        )
        if matched:
            st = matched.get("state")
            stat = matched.get("status")
            if stat == "busy" or st == "running":
                state = "RUNNING"
            elif st in ("blocked", "failed"):
                state = "BLOCKED_OR_FAILED"
            elif st == "done" or stat == "idle":
                state = "COMPLETED"
            if matched.get("sessionId") and not sid:
                sid = str(matched["sessionId"])
        else:
            cc_p = find_cc_jsonl(workdir, sid or None)
            if cc_p:
                info = parse_cc_jsonl_tail(cc_p, 2)
                norm = classify_cc_state(None, info)
                state = "COMPLETED" if norm == "IDLE_COMPLETED" else norm
            elif not state:
                state = "RUNNING"
    else:
        pid = int(job.get("pid") or 0)
        if pid > 0 and is_pid_alive(pid):
            state = "RUNNING"
        elif not state:
            state = "UNKNOWN"

    last_age_str = "-"
    candidate_log = Path(log_file) if log_file and Path(log_file).exists() else None
    if not candidate_log and harness == "codex" and sid:
        candidate_log = find_codex_rollout(workdir, sid)
    if not candidate_log and harness == "claude" and sid:
        candidate_log = find_cc_jsonl(workdir, sid)
    if candidate_log and candidate_log.exists():
        try:
            age_s = max(0, int(time.time() - candidate_log.stat().st_mtime))
            last_age_str = f"{age_s}s前" if age_s < 120 else f"{age_s // 60}m前"
        except OSError:
            pass

    elapsed_str = "-"
    if job.get("started_at"):
        st_ep = parse_iso_epoch(str(job["started_at"]))
        fin_ep = parse_iso_epoch(str(job.get("finished_at") or "")) or time.time()
        if st_ep > 0:
            sec = max(0, int(fin_ep - st_ep))
            elapsed_str = f"{sec}s" if sec < 120 else f"{sec // 60}m{sec % 60:02d}s"

    return {
        "job_id": jid,
        "wave": str(job.get("wave") or "-"),
        "label": str(job.get("label") or ""),
        "harness": harness,
        "model": model,
        "mode": str(job.get("mode") or "write"),
        "workdir": str(workdir),
        "branch": branch,
        "session_id": sid,
        "bg_id": bg_id,
        "tmux_target": str(job.get("tmux_target") or "-"),
        "tmux_window_index": win_idx,
        "tmux_window_id": win_id,
        "pane_dead": pane_dead,
        "state": state or "UNKNOWN",
        "elapsed": elapsed_str,
        "last_activity": last_age_str,
    }


def _render_tmux_status_table(repo: Path, manifest: dict[str, Any], as_lines: bool = False) -> tuple[dict[str, Any], str]:
    """Collect and render human-readable tmux dashboard/status view."""
    ensure_manifest_v2(manifest)
    sess = default_tmux_session_name(manifest, repo)
    sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
    sock_flag = f"-L {sock} " if sock else ""
    sess_alive = _tmux_session_exists(sess) if tmux_bin() else False
    windows = _tmux_list_windows(sess) if sess_alive else []
    win_by_id = {w["window_id"]: w for w in windows}
    win_by_idx = {w["window_index"]: w for w in windows}

    rows = [
        _collect_job_live_row(repo, j, win_by_id, win_by_idx)
        for j in (manifest.get("jobs") or [])
        if isinstance(j, dict)
    ]
    payload = {
        "ok": True,
        "run_id": manifest.get("run_id"),
        "slug": manifest.get("slug"),
        "phase": manifest.get("phase"),
        "verdict": manifest.get("verdict"),
        "tmux_session": sess,
        "tmux_session_alive": sess_alive,
        "tmux_attach_cmd": f"tmux {sock_flag}attach -t {sess}",
        "windows": windows,
        "jobs": rows,
    }

    lines = [
        f"=== harness-loop Tmux 总览 | {manifest.get('slug')} ({manifest.get('run_id')}) ===",
        f"阶段: {manifest.get('phase')} | 结论: {manifest.get('verdict')} | Tmux: {sess} ({'在线' if sess_alive else '未启动'}) | 时间: {datetime.now().strftime('%H:%M:%S')}",
        f"接入: tmux {sock_flag}attach -t {sess}",
        "",
        f"{'Win':<14} {'State':<20} {'Harness(Model)':<22} {'Wave':<10} {'Elapsed':<9} {'Activity':<9} {'Workdir / Job ID'}",
        "-" * 108,
    ]
    if not rows:
        lines.append("(暂无已派发任务；使用 `loopctl.py delegate ...` 启动任务后将自动出现在此看板)")
    else:
        for r in rows[-20:]:
            h_col = f"{r['harness']}({r['model']})" if r["model"] else f"{r['harness']}(default)"
            wd_name = Path(r["workdir"]).name
            br_tag = f" [{r['branch']}]" if r["branch"] else ""
            lines.append(
                f"{r['tmux_target']:<14} {r['state']:<20} {h_col:<22} {r['wave']:<10} {r['elapsed']:<9} {r['last_activity']:<9} {wd_name}{br_tag} · {r['job_id']}"
            )
    lines.extend([
        "",
        "快捷键: 鼠标点击底部状态栏切窗 | Ctrl-b w 选窗口 | Ctrl-b n/p 上下窗 | Ctrl-b d 后台脱离(任务继续跑)",
    ])
    return payload, "\n".join(lines)


def cmd_tmux_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    payload, table_text = _render_tmux_status_table(repo, manifest)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(table_text)
    return 0


def cmd_tmux_dashboard(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    interval = max(float(args.interval), 1.0)
    try:
        while True:
            try:
                run_dir = resolve_run_dir(repo, args.run_id)
                manifest = load_manifest(run_dir)
                _, text = _render_tmux_status_table(repo, manifest)
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                text = f"等待闭环台账就绪 ({repo})...\n{exc}"
            if sys.stdout.isatty():
                sys.stdout.write("\033[H\033[2J" + text + "\n")
            else:
                sys.stdout.write(text + "\n\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_tmux_attach(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    sess = default_tmux_session_name(manifest, repo)
    sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
    sock_flag = f"-L {sock} " if sock else ""

    target_win = ""
    if args.job_id:
        job = next((j for j in (manifest.get("jobs") or []) if isinstance(j, dict) and j.get("job_id") == args.job_id), None)
        if not job:
            raise ValueError(f"Job not found: {args.job_id}")
        target_win = str(job.get("tmux_window_id") or job.get("tmux_target") or "")

    attach_cmd = f"tmux {sock_flag}attach -t {sess}"
    if args.print_only:
        if args.json:
            print(json.dumps({"ok": True, "tmux_session": sess, "target_window": target_win, "attach_cmd": attach_cmd}, ensure_ascii=False, indent=2))
        else:
            print(attach_cmd)
        return 0

    if not tmux_bin():
        raise ValueError("tmux binary not found")
    ensure_tmux_session(sess, repo)
    if target_win:
        _tmux_run(["select-window", "-t", target_win])
    if os.environ.get("TMUX"):
        argv = _tmux_argv("switch-client", "-t", f"={sess}")
    else:
        argv = _tmux_argv("attach-session", "-t", f"={sess}")
    os.execvp(argv[0], argv)
    return 0


def cmd_tmux_tail(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    jobs = [j for j in (manifest.get("jobs") or []) if isinstance(j, dict)]
    job: dict[str, Any] | None = None
    if args.job_id:
        job = next((j for j in jobs if j.get("job_id") == args.job_id), None)
        if not job:
            raise ValueError(f"Job not found: {args.job_id}")
    elif jobs:
        job = jobs[-1]

    if job:
        target = str(job.get("tmux_pane_id") or job.get("tmux_window_id") or job.get("tmux_target") or "")
    else:
        target = f"={default_tmux_session_name(manifest, repo)}:dashboard"

    if not target:
        raise ValueError("Selected job has no tmux pane target")

    lines_n = max(int(args.lines), 1)
    rc, out, err = _tmux_run(["capture-pane", "-p", "-t", target, "-S", f"-{lines_n}"], timeout=8)
    if rc != 0:
        raise RuntimeError(f"tmux capture-pane failed for {target}: {err}")
    if args.json:
        print(json.dumps({"ok": True, "target": target, "job_id": job.get("job_id") if job else "", "output": out}, ensure_ascii=False, indent=2))
    else:
        print(out)
    return 0


def cmd_tmux_stop(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    with _manifest_lock(run_dir):
        manifest = load_manifest(run_dir)
        sess = default_tmux_session_name(manifest, repo)
        stopped: list[str] = []
        if args.all:
            if tmux_bin() and _tmux_session_exists(sess):
                _tmux_run(["kill-session", "-t", f"={sess}"])
            for j in manifest.get("jobs") or []:
                if isinstance(j, dict) and j.get("status") not in ("COMPLETED", "BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED"):
                    j["status"] = "STOPPED"
                    stopped.append(str(j.get("job_id") or ""))
        elif args.job_id:
            job = next((j for j in (manifest.get("jobs") or []) if isinstance(j, dict) and j.get("job_id") == args.job_id), None)
            if not job:
                raise ValueError(f"Job not found: {args.job_id}")
            target = str(job.get("tmux_window_id") or job.get("tmux_target") or "")
            if tmux_bin() and target:
                _tmux_run(["kill-window", "-t", target])
            job["status"] = "STOPPED"
            stopped.append(str(job.get("job_id") or ""))
        else:
            raise ValueError("Specify --job-id <id> or --all")
        save_manifest(run_dir, manifest)
        render_dialogue(repo, run_dir)
    out = {"ok": True, "tmux_session": sess, "stopped_jobs": stopped}
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else f"Stopped: {', '.join(stopped) or sess}")
    return 0


def _build_codex_exec_argv(
    binary: str,
    repo: Path,
    prompt_text: str,
    *,
    sandbox: str,
    out_target: str,
    model: str = "",
    session_id: str = "",
    reasoning_effort: str = "",
) -> list[str]:
    """Construct `codex exec` or `codex exec resume` argv with options before positionals."""
    effort = (reasoning_effort or "").strip().lower()
    if session_id:
        argv = [binary, "exec", "resume", "--json", "-o", out_target]
        if model:
            argv.extend(["-m", model])
        if effort:
            argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
        argv.extend([session_id, prompt_text])
        return argv
    argv = [binary, "exec", "-C", str(repo), "-s", sandbox, "--json", "-o", out_target]
    if model:
        argv.extend(["-m", model])
    if effort:
        argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
    argv.append(prompt_text)
    return argv


def build_consult_argv(
    to_harness: str,
    repo: Path,
    prompt_text: str,
    *,
    model: str = "",
    session_id: str = "",
    reply_file: Path | None = None,
    reasoning_effort: str = "",
) -> list[str]:
    """Construct read-only consultation CLI argv for agy, claude, or codex."""
    binary = harness_bin(to_harness) or to_harness
    if to_harness == "agy":
        argv = [binary, "-p", "--mode", "plan", "--output-format", "json"]
        if model:
            argv.extend(["--model", model])
        if session_id:
            argv.extend(["--conversation", session_id])
        argv.append(prompt_text)
        return argv

    if to_harness == "claude":
        argv = [binary, "-p", "--permission-mode", "plan", "--output-format", "json"]
        if model:
            argv.extend(["--model", model])
        if session_id:
            argv.extend(["--resume", session_id])
        argv.append(prompt_text)
        return argv

    # codex read-only
    out_target = str(reply_file) if reply_file else "/tmp/codex-consult-reply.md"
    return _build_codex_exec_argv(
        binary,
        repo,
        prompt_text,
        sandbox="read-only",
        out_target=out_target,
        model=model,
        session_id=session_id,
        reasoning_effort=reasoning_effort,
    )


def build_delegate_argv(
    to_harness: str,
    repo: Path,
    prompt_text: str,
    *,
    model: str = "",
    session_id: str = "",
    session_name: str = "",
    sandbox: str = "workspace-write",
    last_msg_file: Path | None = None,
    sync: bool = False,
    reasoning_effort: str = "",
) -> list[str]:
    """Construct execution/write CLI argv for agy, claude, or codex."""
    binary = harness_bin(to_harness) or to_harness
    if to_harness == "claude":
        if sync:
            argv = [binary, "-p", "--permission-mode", "acceptEdits", "--output-format", "json"]
            if session_id:
                argv.extend(["--resume", session_id])
        else:
            argv = [binary, "--bg"]
            if session_id:
                argv.extend(["--resume", session_id])
            elif session_name:
                argv.extend(["-n", session_name])
        if model:
            argv.extend(["--model", model])
        argv.append(prompt_text)
        return argv

    if to_harness == "codex":
        out_target = str(last_msg_file) if last_msg_file else "/tmp/codex-delegate-reply.md"
        return _build_codex_exec_argv(
            binary,
            repo,
            prompt_text,
            sandbox=sandbox,
            out_target=out_target,
            model=model,
            session_id=session_id,
            reasoning_effort=reasoning_effort,
        )

    # agy accept-edits
    argv = [binary, "-p", "--mode", "accept-edits", "--output-format", "json"]
    if model:
        argv.extend(["--model", model])
    if session_id:
        argv.extend(["--conversation", session_id])
    argv.append(prompt_text)
    return argv


def extract_reply_and_session(
    harness: str,
    stdout_text: str,
    reply_file: Path | None = None,
) -> tuple[str, str]:
    """Extract reply body and discovered session/thread ID from CLI output."""
    sid = ""
    reply = ""
    if harness == "codex":
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                if ev.get("type") == "thread.started" and ev.get("thread_id"):
                    sid = str(ev["thread_id"])
                elif ev.get("type") == "item.completed":
                    item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
                    if item.get("type") == "agent_message" and item.get("text"):
                        reply = str(item["text"]).strip()
        if reply_file and reply_file.exists():
            file_txt = reply_file.read_text(encoding="utf-8", errors="replace").strip()
            if file_txt:
                reply = file_txt
        return reply or stdout_text.strip(), sid

    stripped = stdout_text.strip()
    if stripped:
        try:
            obj = json.loads(stripped)
            if isinstance(obj, dict):
                sid = str(obj.get("session_id") or obj.get("conversation_id") or obj.get("id") or "")
                reply = str(
                    obj.get("result")
                    or obj.get("response")
                    or obj.get("text")
                    or obj.get("content")
                    or stripped
                ).strip()
                return reply, sid
        except json.JSONDecodeError:
            pass
    return stripped, sid


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    ensure_loop_root(repo)
    run_id = args.run_id or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{args.slug}"
    run_dir = repo / LOOP_DIR_NAME / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / JOBS_DIR).mkdir(parents=True, exist_ok=True)

    agy_id = args.agy_id or ""
    agy_log = str(find_agy_transcript(agy_id) or "") if agy_id else ""

    tracks: dict[str, dict[str, str]] = {}
    for raw_t in getattr(args, "track", None) or []:
        t_name, t_spec = parse_track_spec(raw_t)
        tracks[t_name] = t_spec

    roster: dict[str, Any] = {
        "architect": parse_role_spec(args.architect),
        "executor": parse_role_spec(args.executor),
        "verifier": parse_role_spec(args.verifier),
        "tracks": tracks,
    }
    if args.advisor:
        roster["advisor"] = parse_role_spec(args.advisor)

    docs = [d.strip() for d in (args.docs or "").split(",") if d.strip()]
    base_commit = args.base_commit or git_head_short(repo)
    sessions = default_sessions_dict()
    if agy_id:
        sessions["agy"]["session_id"] = agy_id
        sessions["agy"]["transcript_path"] = agy_log
    for k_role in ("architect", "executor", "verifier", "advisor"):
        r_spec = roster.get(k_role)
        if isinstance(r_spec, dict) and r_spec.get("model"):
            h = r_spec["harness"]
            if not sessions[h].get("model"):
                sessions[h]["model"] = r_spec["model"]
    for t_spec in tracks.values():
        h = t_spec["harness"]
        if t_spec.get("model") and not sessions[h].get("model"):
            sessions[h]["model"] = t_spec["model"]

    tmux_sess = sanitize_tmux_name(args.tmux_session, max_len=36) if getattr(args, "tmux_session", "") else f"{TMUX_SESSION_PREFIX}{sanitize_tmux_name(args.slug or repo.name, max_len=28)}"

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "slug": args.slug,
        "title": args.title or args.slug,
        "complexity": args.complexity,
        "repo": str(repo),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "phase": "SPEC_AND_HANDOFF",
        "round": 0,
        "max_rounds": max(int(getattr(args, "max_rounds", DEFAULT_MAX_ROUNDS)), 0),
        "terminal_reason": "",
        "verdict": "PENDING",
        "base_commit": base_commit,
        "tmux_session": tmux_sess,
        "roster": roster,
        "sessions": sessions,
        "jobs": [],
        "agy_conversation_id": agy_id,
        "agy_transcript_path": agy_log,
        "cc_session_id": "",
        "cc_bg_id": "",
        "cc_session_name": "",
        "cc_kind": "",
        "cc_jsonl_path": "",
        "docs": docs,
        "synced_cc_assistant_ts": [],
        "synced_codex_ts": [],
    }
    save_manifest(run_dir, manifest)
    (run_dir / EVENTS_FILE).touch(exist_ok=True)
    _atomic_write_text(repo / LOOP_DIR_NAME / LATEST_RUN_FILE, run_id + "\n")
    dialogue_path = render_dialogue(repo, run_dir)

    out = {"ok": True, "run_id": run_id, "run_dir": str(run_dir), "dialogue_md": str(dialogue_path), "manifest": manifest}
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else str(dialogue_path))
    return 0


def cmd_set_roster(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    changes: dict[str, Any] = {}
    with _manifest_lock(run_dir):
        manifest = load_manifest(run_dir)
        for role_key in ("architect", "executor", "verifier", "advisor"):
            val = getattr(args, role_key, "")
            if val:
                spec = parse_role_spec(val)
                manifest["roster"][role_key] = spec
                if spec["model"]:
                    manifest["sessions"][spec["harness"]]["model"] = spec["model"]
                changes[role_key] = spec
        for raw_t in getattr(args, "track", None) or []:
            t_name, t_spec = parse_track_spec(raw_t)
            manifest["roster"].setdefault("tracks", {})[t_name] = t_spec
            if t_spec["model"]:
                manifest["sessions"][t_spec["harness"]]["model"] = t_spec["model"]
            changes[f"track:{t_name}"] = t_spec
        if not changes:
            print(json.dumps({"ok": False, "error": "no roster role or track flags provided"}, ensure_ascii=False), file=sys.stderr)
            return 1
        save_manifest(run_dir, manifest)
    summary = "更新角色分工：" + ", ".join(
        f"{k}={v['harness']}" + (f"({v['model']})" if v["model"] else "")
        for k, v in changes.items()
    )
    append_event(
        repo,
        run_dir,
        role=args.role,
        event="roster_set",
        summary=summary,
        body=json.dumps(manifest["roster"], ensure_ascii=False, indent=2),
    )
    out = {"ok": True, "run_id": manifest["run_id"], "roster": manifest["roster"], "dialogue_md": str(run_dir / DIALOGUE_FILE)}
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else summary)
    return 0


def select_repo_agent(
    agents: list[dict[str, Any]], session_id: str | None
) -> tuple[dict[str, Any] | None, str]:
    """Select matching Claude agent entry by session_id or prioritize active busy/running agent."""
    if session_id:
        return next((a for a in agents if a.get("sessionId") == session_id), None), session_id
    if not agents:
        return None, ""
    entry = sorted(
        agents,
        key=lambda a: (
            1 if (a.get("status") == "busy" or a.get("state") == "running") else 0,
            int(a.get("startedAt") or 0),
        ),
        reverse=True,
    )[0]
    return entry, entry.get("sessionId") or ""


def _bind_harness_session(
    repo: Path,
    run_dir: Path,
    harness: str,
    *,
    session_id: str = "",
    model: str = "",
    bg_id: str = "",
    name: str = "",
    kind: str = "",
) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    slot = manifest["sessions"][harness]
    sid = session_id or slot.get("session_id") or ""
    norm_model = normalize_model(harness, model) if model else ""
    path_str = ""

    if harness == "claude":
        agents = query_claude_agents(repo)
        matched, sid = select_repo_agent(agents, sid or None)
        jsonl_path = find_cc_jsonl(repo, sid or None)
        if not sid and jsonl_path:
            sid = jsonl_path.stem
        path_str = str(jsonl_path) if jsonl_path else ""
        bg_id = bg_id or (matched.get("id", "") if matched else "")
        name = name or (matched.get("name", "") if matched else "")
        kind = kind or (matched.get("kind", "") if matched else "")
    elif harness == "codex":
        rollout = find_codex_rollout(repo, sid or None)
        if rollout:
            path_str = str(rollout)
            if not sid:
                sid = str(read_codex_session_meta(rollout).get("id") or "")
        if sid and not name:
            name = lookup_codex_thread_names().get(sid, "")
    elif harness == "agy":
        if sid:
            t = find_agy_transcript(sid)
            if t:
                path_str = str(t)

    sync_manifest_session(
        manifest,
        harness,
        session_id=sid,
        model=norm_model,
        path=path_str,
        bg_id=bg_id,
        name=name,
        kind=kind,
    )
    save_manifest(run_dir, manifest)
    dialogue_path = render_dialogue(repo, run_dir)
    return {
        "ok": True,
        "run_id": manifest["run_id"],
        "harness": harness,
        "session_id": sid,
        "cc_session_id": manifest.get("cc_session_id"),
        "commands": format_all_harness_commands(manifest),
        "dialogue_md": str(dialogue_path),
    }


def cmd_bind_cc(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    out = _bind_harness_session(
        repo,
        run_dir,
        "claude",
        session_id=args.session_id,
        bg_id=args.bg_id,
        name=args.name,
        kind=args.kind,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out["session_id"])
    return 0


def cmd_bind_session(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    out = _bind_harness_session(
        repo,
        run_dir,
        args.harness,
        session_id=args.session_id,
        model=args.model,
        bg_id=args.bg_id,
        name=args.name,
        kind=args.kind,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else out["session_id"])
    return 0


def _safe_resolve_path(raw: str) -> str:
    """Resolve a path string safely even if the target directory was deleted."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    try:
        return str(Path(cleaned).resolve())
    except OSError:
        return cleaned


def _evaluate_signoff_gate(
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    skip_ux: bool = False,
) -> tuple[bool, list[str], dict[str, Any]]:
    """Enforce hard completion checks before recording signoff (DONE)."""
    blockers: list[str] = []

    cur_verdict = str(manifest.get("verdict") or "PENDING")
    if cur_verdict != "PASS":
        blockers.append(f"终审结论当前为 {cur_verdict}，需先通过 verifier_audit 并记录 --verdict PASS")

    jobs = [j for j in (manifest.get("jobs") or []) if isinstance(j, dict)]
    terminal_set = {"COMPLETED", "BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED", "STALLED", "STOPPED"}
    completed_idx_by_workdir: dict[str, int] = {}
    completed_idx_by_label: dict[str, int] = {}
    job_statuses: list[str] = []

    for idx, j in enumerate(jobs):
        st = _summarize_job_status(j)
        job_statuses.append(st)
        if st == "COMPLETED":
            wd = _safe_resolve_path(str(j.get("workdir") or ""))
            lbl = str(j.get("label") or "").strip()
            if wd:
                completed_idx_by_workdir[wd] = idx
            if lbl:
                completed_idx_by_label[lbl] = idx

    active_jobs: list[str] = []
    unresolved_failed_jobs: list[str] = []
    for idx, (j, st) in enumerate(zip(jobs, job_statuses)):
        jid = str(j.get("job_id") or "-")
        if st not in terminal_set:
            active_jobs.append(f"{jid}({st})")
        elif st in ("BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED", "STALLED"):
            wd = _safe_resolve_path(str(j.get("workdir") or ""))
            lbl = str(j.get("label") or "").strip()
            superseded = (wd and completed_idx_by_workdir.get(wd, -1) > idx) or (
                lbl and completed_idx_by_label.get(lbl, -1) > idx
            )
            if not superseded:
                unresolved_failed_jobs.append(f"{jid}({st})")

    if active_jobs:
        blockers.append(f"仍有活跃或未结束任务: {', '.join(active_jobs)}")
    if unresolved_failed_jobs:
        blockers.append(f"存在未闭环的失败/卡死任务: {', '.join(unresolved_failed_jobs)}")

    write_dispatch_events = ("delegate_task", "rework", "rework_to_cc", "handoff_to_cc")
    last_dispatch_idx = -1
    for idx, ev in enumerate(events):
        if ev.get("event") in write_dispatch_events:
            last_dispatch_idx = idx
    has_ux = any(ev.get("event") == "ux_walkthrough" for ev in events[last_dispatch_idx + 1 :])
    if not skip_ux and not has_ux:
        blockers.append("缺少本轮实机体验记录 (event=ux_walkthrough)；纯后端/脚本任务可显式传 --skip-ux")

    details = {
        "verdict": cur_verdict,
        "active_jobs": active_jobs,
        "unresolved_failed_jobs": unresolved_failed_jobs,
        "has_ux_walkthrough": has_ux,
        "skip_ux": skip_ux,
    }
    return len(blockers) == 0, blockers, details


def _enforce_dispatch_loop_guards(
    repo: Path,
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    from_role: str,
    is_rework: bool,
    force: bool,
    dry_run: bool,
) -> tuple[bool, int | None]:
    """Block dispatching on terminal runs or past max_rounds unless --force is passed."""
    cur_phase = str(manifest.get("phase") or "")
    if cur_phase in TERMINAL_PHASES and not force:
        print(
            json.dumps(
                {
                    "ok": False,
                    "dispatched": False,
                    "reason": "run_already_terminal",
                    "phase": cur_phase,
                    "terminal_reason": manifest.get("terminal_reason", ""),
                    "hint": "当前闭环已处于终态，需用户确认后加 --force 才能重新派活。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return False, 2

    if is_rework:
        cur_round = int(manifest.get("round", 0))
        max_rounds = int(manifest.get("max_rounds", DEFAULT_MAX_ROUNDS))
        if cur_round >= max_rounds and not force:
            if not dry_run:
                append_event(
                    repo,
                    run_dir,
                    role=from_role,
                    event="escalate",
                    summary=f"达到自动返工上限 ({cur_round}/{max_rounds})，强制停机升级用户裁决",
                    body=f"自动返工轮次已达上限 ({cur_round}/{max_rounds})，停止继续自动打回，等待用户决策或明确授权 --force。",
                    phase="ESCALATED",
                    terminal_reason=f"auto_rework_cap ({cur_round}/{max_rounds})",
                )
            print(
                json.dumps(
                    {
                        "ok": False,
                        "dispatched": False,
                        "reason": "max_rework_rounds_reached",
                        "round": cur_round,
                        "max_rounds": max_rounds,
                        "phase": "ESCALATED" if not dry_run else cur_phase,
                        "hint": "已达自动返工上限，请升级用户决定，或经用户授权后加 --force 放行。",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return False, 2

    return True, None


def cmd_record(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    body = read_prompt_or_body(args.body, args.body_file)
    phase = args.phase
    terminal_reason: str | None = None
    extra: dict[str, Any] = {}

    if args.event == "signoff":
        manifest = load_manifest(run_dir)
        events = load_events(run_dir)
        skip_ux = bool(getattr(args, "skip_ux", False))
        force = bool(getattr(args, "force", False))
        ok_gate, blockers, gate_details = _evaluate_signoff_gate(manifest, events, skip_ux=skip_ux)
        if not ok_gate and not force:
            err_payload = {
                "ok": False,
                "reason": "signoff_gate_failed",
                "blockers": blockers,
                "details": gate_details,
            }
            if args.json:
                print(json.dumps(err_payload, ensure_ascii=False, indent=2))
            else:
                print("[signoff 拦截] 尚不满足闭环结束条件：")
                for b in blockers:
                    print(f"  - {b}")
            return 2
        phase = phase or "DONE"
        terminal_reason = "completed"
        if skip_ux:
            extra["ux_skipped"] = True
        if force and not ok_gate:
            extra["forced_signoff"] = True
    elif args.event == "escalate":
        phase = phase or "ESCALATED"
        terminal_reason = args.summary
    elif args.event == "abort":
        phase = phase or "ABORTED"
        terminal_reason = args.summary

    entry = append_event(
        repo,
        run_dir,
        role=args.role,
        event=args.event,
        summary=args.summary,
        body=body,
        phase=phase,
        verdict=args.verdict,
        terminal_reason=terminal_reason,
        commits=args.commits,
        extra=extra or None,
    )
    if args.json:
        print(json.dumps({"ok": True, "event": entry}, ensure_ascii=False, indent=2))
    else:
        print(f"[{entry['role']}] {entry['event']} -> {entry['dialogue_md']}")
    return 0


def cmd_dispatch_cc(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    prompt_text = read_prompt_or_body(args.prompt, args.prompt_file)
    if not prompt_text.strip():
        print(json.dumps({"ok": False, "error": "empty prompt"}, ensure_ascii=False), file=sys.stderr)
        return 1

    use_ultracode = (
        args.ultracode
        if args.ultracode is not None
        else manifest.get("complexity", "complex") == "complex"
    )
    prompt_text = apply_ultracode_prefix(prompt_text, use_ultracode)

    active_agents = query_claude_agents(repo)
    busy = [a for a in active_agents if a.get("status") == "busy" or a.get("state") == "running"]
    if busy and not args.force:
        payload = {"ok": False, "dispatched": False, "reason": "busy_session_exists", "busy_sessions": busy}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    prompt_file = run_dir / "LAUNCH-PROMPT.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    session_name = args.name or f"AGY-Loop: {manifest.get('slug')}"

    append_event(
        repo,
        run_dir,
        role="agy",
        event="handoff_to_cc",
        summary=f"向 Claude Code 派发执行任务（会话名：{session_name}，ultracode={use_ultracode}）",
        body=prompt_text,
        phase="CC_EXECUTING",
    )

    if args.dry_run:
        out = {
            "ok": True,
            "dispatched": False,
            "dry_run": True,
            "ultracode": use_ultracode,
            "prompt_file": str(prompt_file),
            "session_name": session_name,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    binary = claude_bin()
    if not binary:
        print(json.dumps({"ok": False, "error": "claude CLI not found"}, ensure_ascii=False), file=sys.stderr)
        return 1

    dispatch_ts = round(time.time(), 3)
    proc = subprocess.run(
        [binary, "--bg", "-n", session_name, prompt_text],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    if proc.returncode != 0:
        out = {"ok": False, "dispatched": False, "returncode": proc.returncode, "stdout": stdout, "stderr": stderr}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    m = re.search(r"\b([0-9a-f]{8})\b", stdout)
    bg_id = m.group(1) if m else ""
    time.sleep(1.5)
    agents = query_claude_agents(repo)
    matched = next(
        (a for a in agents if (bg_id and a.get("id") == bg_id) or a.get("name") == session_name),
        None,
    )
    manifest = load_manifest(run_dir)
    manifest["last_dispatch_epoch"] = dispatch_ts
    if matched:
        sid = matched.get("sessionId") or ""
        jsonl = find_cc_jsonl(repo, sid or None)
        sync_manifest_session(
            manifest,
            "claude",
            session_id=sid,
            bg_id=matched.get("id") or bg_id,
            name=matched.get("name") or session_name,
            kind=matched.get("kind") or "background",
            path=str(jsonl) if jsonl else None,
        )
    save_manifest(run_dir, manifest)
    if matched:
        render_dialogue(repo, run_dir)

    out = {
        "ok": True,
        "dispatched": True,
        "ultracode": use_ultracode,
        "bg_id": manifest.get("cc_bg_id") or bg_id,
        "session_id": manifest.get("cc_session_id"),
        "session_name": session_name,
        "commands": format_cc_commands(manifest),
        "prompt_file": str(prompt_file),
        "dialogue_md": str(run_dir / DIALOGUE_FILE),
        "raw_stdout": stdout,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_send_cc(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    is_rework = args.event == "rework_to_cc"
    ok_guard, guard_rc = _enforce_dispatch_loop_guards(
        repo,
        run_dir,
        manifest,
        from_role="agy",
        is_rework=is_rework,
        force=bool(getattr(args, "force", False)),
        dry_run=bool(args.dry_run),
    )
    if not ok_guard:
        return int(guard_rc or 2)

    prompt_text = read_prompt_or_body(args.prompt, args.prompt_file)
    if not prompt_text.strip():
        print(json.dumps({"ok": False, "error": "empty prompt"}, ensure_ascii=False), file=sys.stderr)
        return 1

    default_ultra = (
        manifest.get("complexity", "complex") == "complex"
        if args.event == "handoff_to_cc"
        else False
    )
    use_ultracode = args.ultracode if args.ultracode is not None else default_ultra
    prompt_text = apply_ultracode_prefix(prompt_text, use_ultracode)

    round_num = int(manifest.get("round", 0)) + (1 if is_rework else 0)
    prompt_file = run_dir / f"ROUND-{round_num}-PROMPT.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    send_verdict = "NEEDS_FIX" if is_rework else ("PENDING" if manifest.get("verdict") == "PASS" else None)
    append_event(
        repo,
        run_dir,
        role="agy",
        event=args.event,
        summary=args.summary or f"第 {round_num} 轮指令发送给 Claude Code",
        body=prompt_text,
        phase="CC_EXECUTING",
        verdict=send_verdict,
        terminal_reason="",
    )

    sid = args.session_id or manifest.get("cc_session_id") or ""
    agents = query_claude_agents(repo)
    matched = next((a for a in agents if sid and a.get("sessionId") == sid), None)

    if matched and (matched.get("status") == "busy" or matched.get("state") == "running"):
        out = {
            "ok": True,
            "sent": False,
            "reason": "session_busy",
            "ultracode": use_ultracode,
            "session_id": sid,
            "prompt_file": str(prompt_file),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if matched and matched.get("kind") == "interactive":
        if args.cmux_target and os.environ.get("CMUX_SURFACE_ID") and not args.dry_run:
            cmux_bin = "/Applications/cmux.app/Contents/Resources/bin/cmux"
            dispatch_ts = round(time.time(), 3)
            proc = subprocess.run(
                [cmux_bin, "agent", "message", args.cmux_target, "--from", "AGY-Verifier", prompt_text],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if proc.returncode == 0:
                manifest = load_manifest(run_dir)
                manifest["last_dispatch_epoch"] = dispatch_ts
                save_manifest(run_dir, manifest)
                print(json.dumps({"ok": True, "sent": True, "delivery_mode": "cmux_agent_message", "ultracode": use_ultracode, "session_id": sid, "prompt_file": str(prompt_file)}, ensure_ascii=False, indent=2))
                return 0
        out = {
            "ok": True,
            "sent": False,
            "delivery_mode": "manual_paste_to_interactive_session",
            "ultracode": use_ultracode,
            "session_id": sid,
            "resume_cmd": f"claude --resume {sid}" if sid else "",
            "prompt_file": str(prompt_file),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.dry_run or not sid:
        out = {"ok": True, "sent": False, "dry_run": args.dry_run, "ultracode": use_ultracode, "session_id": sid, "prompt_file": str(prompt_file)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    binary = claude_bin()
    if not binary:
        print(json.dumps({"ok": False, "error": "claude CLI not found"}, ensure_ascii=False), file=sys.stderr)
        return 1

    dispatch_ts = round(time.time(), 3)
    proc = subprocess.run(
        [binary, "--bg", "--resume", sid, prompt_text],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode == 0:
        manifest = load_manifest(run_dir)
        manifest["last_dispatch_epoch"] = dispatch_ts
        save_manifest(run_dir, manifest)
    out = {
        "ok": proc.returncode == 0,
        "sent": proc.returncode == 0,
        "delivery_mode": "claude_bg_resume",
        "ultracode": use_ultracode,
        "session_id": sid,
        "prompt_file": str(prompt_file),
        "raw_stdout": proc.stdout.strip(),
        "raw_stderr": proc.stderr.strip(),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if proc.returncode == 0 else 1


def _sync_discovered_session(
    repo: Path,
    manifest: dict[str, Any],
    harness: str,
    *,
    session_id: str,
    model: str,
) -> None:
    """Sync discovered session metadata (including Codex rollout/thread name) into manifest."""
    extra_kwargs: dict[str, Any] = {}
    if harness == "codex" and session_id:
        rollout_path = find_codex_rollout(repo, session_id)
        thread_name = lookup_codex_thread_names().get(session_id, "")
        if thread_name:
            extra_kwargs["name"] = thread_name
        if rollout_path:
            extra_kwargs["path"] = str(rollout_path)
    sync_manifest_session(
        manifest,
        harness,
        session_id=session_id or None,
        model=model or None,
        **extra_kwargs,
    )


def _resolve_tmux_session_from_args(manifest: dict[str, Any], repo: Path, args: argparse.Namespace) -> str:
    explicit = (getattr(args, "tmux_session", "") or "").strip()
    if explicit:
        return sanitize_tmux_name(explicit, max_len=36)
    return default_tmux_session_name(manifest, repo)


def _record_job_in_manifest(
    repo: Path,
    run_dir: Path,
    job_meta: dict[str, Any],
    *,
    harness: str,
    session_id: str = "",
    model: str = "",
    bg_id: str = "",
    name: str = "",
    kind: str = "",
    log_path: str = "",
    dispatch_ts: float | None = None,
    tmux_sess: str = "",
) -> dict[str, Any]:
    """Thread/process-safely record or update a job entry and sync manifest session."""
    with _manifest_lock(run_dir):
        manifest = load_manifest(run_dir)
        if tmux_sess and not manifest.get("tmux_session"):
            manifest["tmux_session"] = tmux_sess
        if dispatch_ts is not None:
            manifest["last_dispatch_epoch"] = dispatch_ts
        jobs = manifest.setdefault("jobs", [])
        existing_idx = next(
            (i for i, j in enumerate(jobs) if isinstance(j, dict) and j.get("job_id") == job_meta.get("job_id")),
            None,
        )
        if existing_idx is not None:
            jobs[existing_idx] = job_meta
        else:
            jobs.append(job_meta)
        sync_manifest_session(
            manifest,
            harness,
            session_id=session_id or None,
            model=model or None,
            path=log_path or None,
            bg_id=bg_id or None,
            name=name or None,
            kind=kind or None,
            last_job_id=str(job_meta.get("job_id") or "") or None,
        )
        save_manifest(run_dir, manifest)
        render_dialogue(repo, run_dir)
        return manifest


def cmd_consult(args: argparse.Namespace) -> int:
    ok_depth, depth, next_env = check_recursion_depth(args.allow_nested)
    if not ok_depth:
        print(
            json.dumps(
                {"ok": False, "reason": "max_recursion_depth", "depth": depth},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    workdir = Path(args.workdir).resolve() if getattr(args, "workdir", "") else repo
    if not workdir.exists():
        raise FileNotFoundError(f"Workdir not found: {workdir}")
    branch = resolve_worktree_branch(workdir)

    prompt_text = read_prompt_or_body(args.prompt, args.prompt_file)
    if not prompt_text.strip():
        print(json.dumps({"ok": False, "error": "empty prompt"}, ensure_ascii=False), file=sys.stderr)
        return 1

    model = resolve_harness_model(manifest, args.to, args.model)
    reasoning_effort = (getattr(args, "reasoning_effort", "") or "").strip().lower()
    sid = args.session_id or manifest["sessions"][args.to].get("session_id") or ""
    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    rand4 = secrets.token_hex(2)
    job_id = f"consult-{args.to}-{ts_tag}-{rand4}"
    jobs_dir = run_dir / JOBS_DIR
    jobs_dir.mkdir(parents=True, exist_ok=True)
    reply_file = jobs_dir / f"{job_id}-reply.md"
    stdout_log = jobs_dir / f"{job_id}.jsonl"
    pid_file = jobs_dir / f"{job_id}.pid.json"
    exit_file = jobs_dir / f"{job_id}.exit.json"
    spec_file = jobs_dir / f"{job_id}.spec.json"
    prompt_file = run_dir / f"CONSULT-{job_id}.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    argv = build_consult_argv(
        args.to,
        workdir,
        prompt_text,
        model=model,
        session_id=sid,
        reply_file=reply_file,
        reasoning_effort=reasoning_effort,
    )
    use_tmux = _should_use_tmux(getattr(args, "tmux", None)) if not args.dry_run else bool(
        getattr(args, "tmux", None) if getattr(args, "tmux", None) is not None else tmux_bin()
    )
    tmux_sess = _resolve_tmux_session_from_args(manifest, repo, args)
    win_name = sanitize_tmux_name(getattr(args, "label", "") or f"c-{args.to}", max_len=18)

    append_event(
        repo,
        run_dir,
        role=args.from_role,
        event="cross_consult",
        summary=args.summary or f"{args.from_role} 向 {args.to} 发起只读评审/咨询",
        body=prompt_text,
        extra={
            "target": args.to,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "workdir": str(workdir),
            "job_id": job_id,
        },
    )

    if args.dry_run:
        out = {
            "ok": True,
            "dry_run": True,
            "job_id": job_id,
            "from": args.from_role,
            "to": args.to,
            "workdir": str(workdir),
            "branch": branch,
            "tmux": use_tmux,
            "tmux_session": tmux_sess if use_tmux else "",
            "model": model,
            "reasoning_effort": reasoning_effort,
            "session_id": sid,
            "argv": argv,
            "dialogue_md": str(run_dir / DIALOGUE_FILE),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    binary = harness_bin(args.to)
    if not binary:
        print(json.dumps({"ok": False, "error": f"{args.to} CLI not found"}, ensure_ascii=False), file=sys.stderr)
        return 1

    env_overrides = {
        "HARNESS_LOOP_DEPTH": next_env["HARNESS_LOOP_DEPTH"],
        "AGY_LOOP_DEPTH": next_env["AGY_LOOP_DEPTH"],
    }
    tmux_info: dict[str, Any] = {}
    returncode = 1
    stdout_text = ""
    stderr_text = ""

    if use_tmux:
        spec_payload = {
            "job_id": job_id,
            "wave": "consult",
            "label": getattr(args, "label", "") or win_name,
            "harness": args.to,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "mode": "consult",
            "workdir": str(workdir),
            "branch": branch,
            "argv": argv,
            "env_overrides": env_overrides,
            "prompt_file": str(prompt_file),
            "stdout_jsonl": str(stdout_log),
            "last_msg_file": str(reply_file),
            "pid_file": str(pid_file),
            "exit_file": str(exit_file),
            "resume_hint": f'cd "{workdir}" && codex resume {sid}' if (args.to == "codex" and sid) else "",
        }
        _atomic_write_text(spec_file, json.dumps(spec_payload, ensure_ascii=False, indent=2))
        tmux_info = _launch_in_tmux(
            tmux_sess,
            repo,
            spec_path=spec_file,
            win_name=win_name,
            workdir=workdir,
            pid_file=pid_file,
            wait_for_child_pid=True,
        )
        job_meta = {
            "job_id": job_id,
            "wave": "consult",
            "label": getattr(args, "label", "") or win_name,
            "harness": args.to,
            "role_name": "advisor",
            "mode": "consult",
            "workdir": str(workdir),
            "branch": branch,
            "pid": tmux_info["child_pid"] or tmux_info["tmux_pane_pid"],
            "tmux_session": tmux_sess,
            "tmux_window_index": tmux_info["tmux_window_index"],
            "tmux_window_id": tmux_info["tmux_window_id"],
            "tmux_window_name": win_name,
            "tmux_pane_id": tmux_info["tmux_pane_id"],
            "tmux_target": tmux_info["tmux_target"],
            "model": model,
            "stdout_jsonl": str(stdout_log),
            "last_msg_file": str(reply_file),
            "prompt_file": str(prompt_file),
            "pid_file": str(pid_file),
            "exit_file": str(exit_file),
            "status": "RUNNING",
            "started_at": now_iso(),
        }
        _record_job_in_manifest(repo, run_dir, job_meta, harness=args.to, model=model, tmux_sess=tmux_sess)

        deadline = time.monotonic() + max(int(args.timeout), 1)
        exit_data: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            exit_data = _is_job_exit_recorded(job_meta)
            if exit_data is not None:
                break
            time.sleep(0.25)
        if exit_data is None:
            print(json.dumps({"ok": False, "job_id": job_id, "tmux_target": tmux_info.get("tmux_target"), "error": f"consult timeout after {args.timeout}s"}, ensure_ascii=False))
            return 1
        returncode = int(exit_data.get("exit_code", 1))
        stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace") if stdout_log.exists() else ""
        stderr_log = stdout_log.with_suffix(".stderr.log")
        stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace") if stderr_log.exists() else ""
        job_meta["exit_code"] = returncode
        job_meta["finished_at"] = exit_data.get("finished_at") or now_iso()
    else:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(workdir),
                env=next_env,
                capture_output=True,
                text=True,
                timeout=args.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(json.dumps({"ok": False, "error": f"consult timeout after {args.timeout}s"}, ensure_ascii=False))
            return 1
        returncode = proc.returncode
        stdout_text = proc.stdout
        stderr_text = proc.stderr

    reply_text, new_sid = extract_reply_and_session(args.to, stdout_text, reply_file=reply_file)
    if returncode != 0 and not reply_text:
        if use_tmux:
            job_meta["status"] = "PROCESS_EXITED_FAILED"
            _record_job_in_manifest(repo, run_dir, job_meta, harness=args.to, model=model, tmux_sess=tmux_sess)
        print(
            json.dumps(
                {"ok": False, "returncode": returncode, "stderr": stderr_text.strip(), "stdout": stdout_text.strip()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    final_sid = new_sid or sid
    if use_tmux:
        job_meta["status"] = "COMPLETED" if returncode == 0 else "PROCESS_EXITED_FAILED"
        job_meta["session_id"] = final_sid
        _record_job_in_manifest(
            repo,
            run_dir,
            job_meta,
            harness=args.to,
            session_id=final_sid,
            model=model,
            tmux_sess=tmux_sess,
        )
    elif final_sid or model:
        with _manifest_lock(run_dir):
            manifest = load_manifest(run_dir)
            _sync_discovered_session(repo, manifest, args.to, session_id=final_sid, model=model)
            save_manifest(run_dir, manifest)
            render_dialogue(repo, run_dir)

    append_event(
        repo,
        run_dir,
        role=args.to,
        event="cross_feedback",
        summary=f"{args.to} 返回只读评审意见（回复给 {args.from_role}）",
        body=reply_text,
        extra={"target": args.from_role, "model": model, "session_id": final_sid, "job_id": job_id},
    )
    out: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "from": args.from_role,
        "to": args.to,
        "workdir": str(workdir),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "session_id": final_sid,
        "reply": reply_text,
        "dialogue_md": str(run_dir / DIALOGUE_FILE),
    }
    if tmux_info:
        out["tmux_target"] = tmux_info.get("tmux_target")
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else reply_text)
    return 0


def cmd_delegate(args: argparse.Namespace) -> int:
    ok_depth, depth, next_env = check_recursion_depth(args.allow_nested)
    if not ok_depth:
        print(
            json.dumps(
                {"ok": False, "reason": "max_recursion_depth", "depth": depth},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    is_rework = bool(getattr(args, "rework", False))
    ok_guard, guard_rc = _enforce_dispatch_loop_guards(
        repo,
        run_dir,
        manifest,
        from_role=args.from_role,
        is_rework=is_rework,
        force=bool(args.force),
        dry_run=bool(args.dry_run),
    )
    if not ok_guard:
        return int(guard_rc or 2)

    track = (getattr(args, "track", "") or "").strip()

    to_harness = (getattr(args, "to", "") or "").strip().lower()
    if not to_harness and track:
        t_spec = (manifest.get("roster") or {}).get("tracks", {}).get(track)
        if isinstance(t_spec, dict) and t_spec.get("harness"):
            to_harness = str(t_spec["harness"])
    if not to_harness:
        r_spec = (manifest.get("roster") or {}).get(args.role_name)
        if isinstance(r_spec, dict) and r_spec.get("harness"):
            to_harness = str(r_spec["harness"])
    if to_harness not in HARNESSES:
        raise ValueError(f"Specify --to <{'|'.join(HARNESSES)}> or valid --track")

    workdir = Path(args.workdir).resolve() if getattr(args, "workdir", "") else repo
    if not workdir.exists():
        raise FileNotFoundError(f"Workdir not found: {workdir}")
    branch = resolve_worktree_branch(workdir)

    prompt_text = read_prompt_or_body(args.prompt, args.prompt_file)
    if not prompt_text.strip():
        print(json.dumps({"ok": False, "error": "empty prompt"}, ensure_ascii=False), file=sys.stderr)
        return 1

    model = resolve_harness_model(manifest, to_harness, args.model, role_name=args.role_name, track=track or None)
    reasoning_effort = (getattr(args, "reasoning_effort", "") or "").strip().lower()
    sid = args.session_id or manifest["sessions"][to_harness].get("session_id") or ""
    use_ultracode = False
    if to_harness == "claude":
        use_ultracode = (
            args.ultracode
            if args.ultracode is not None
            else manifest.get("complexity", "complex") == "complex"
        )
        prompt_text = apply_ultracode_prefix(prompt_text, use_ultracode)

    if not args.dry_run and not args.force:
        conflicts = find_active_write_conflicts(repo, manifest, target_workdir=workdir)
        if conflicts:
            print(
                json.dumps(
                    {"ok": False, "dispatched": False, "reason": "active_writer_conflict", "workdir": str(workdir), "conflicts": conflicts},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2

    wave = (getattr(args, "wave", "") or "").strip()
    label = (getattr(args, "label", "") or "").strip()
    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    rand4 = secrets.token_hex(2)
    label_slug = sanitize_tmux_name(label or track or args.role_name, max_len=16)
    job_id = f"{to_harness}-{label_slug}-{ts_tag}-{rand4}"
    prompt_file = run_dir / f"DELEGATE-{job_id}.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    session_name = args.name or f"{manifest.get('slug')}: {label or track or args.role_name}"

    jobs_dir = run_dir / JOBS_DIR
    jobs_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = jobs_dir / f"{job_id}.jsonl"
    last_msg_file = jobs_dir / f"{job_id}-last.md"
    pid_file = jobs_dir / f"{job_id}.pid.json"
    exit_file = jobs_dir / f"{job_id}.exit.json"
    spec_file = jobs_dir / f"{job_id}.spec.json"

    sync_mode = bool(args.sync)
    argv = build_delegate_argv(
        to_harness,
        workdir,
        prompt_text,
        model=model,
        session_id=sid,
        session_name=session_name,
        sandbox=args.sandbox,
        last_msg_file=last_msg_file,
        sync=sync_mode,
        reasoning_effort=reasoning_effort,
    )

    use_tmux = _should_use_tmux(getattr(args, "tmux", None)) if not args.dry_run else bool(
        getattr(args, "tmux", None) if getattr(args, "tmux", None) is not None else tmux_bin()
    )
    tmux_sess = _resolve_tmux_session_from_args(manifest, repo, args)
    sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
    sock_flag = f"-L {sock} " if sock else ""
    win_name = sanitize_tmux_name(label or track or f"{to_harness}-{args.role_name}", max_len=18)

    next_round = int(manifest.get("round", 0)) + (1 if is_rework else 0)
    phase_hint = "CC_EXECUTING" if to_harness == "claude" else f"{to_harness.upper()}_EXECUTING"
    del_event = "rework" if is_rework else "delegate_task"
    del_verdict = "NEEDS_FIX" if is_rework else ("PENDING" if manifest.get("verdict") == "PASS" else None)
    default_summary = (
        f"{args.from_role} 发起第 {next_round} 轮返工 ({label or track or args.role_name}) 给 {to_harness}"
        if is_rework
        else f"{args.from_role} 派发 {label or track or args.role_name} 任务给 {to_harness}"
    )
    append_event(
        repo,
        run_dir,
        role=args.from_role,
        event=del_event,
        summary=args.summary or default_summary,
        body=prompt_text,
        phase=phase_hint,
        verdict=del_verdict,
        terminal_reason="",
        extra={
            "target": to_harness,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "ultracode": use_ultracode,
            "workdir": str(workdir),
            "branch": branch,
            "wave": wave,
            "track": track,
            "job_id": job_id,
            "rework": is_rework,
            "round": next_round,
        },
    )

    if args.dry_run:
        out = {
            "ok": True,
            "dry_run": True,
            "dispatched": False,
            "job_id": job_id,
            "from": args.from_role,
            "to": to_harness,
            "role_name": args.role_name,
            "rework": is_rework,
            "round": next_round,
            "track": track,
            "wave": wave,
            "label": label,
            "workdir": str(workdir),
            "branch": branch,
            "tmux": use_tmux,
            "tmux_session": tmux_sess if use_tmux else "",
            "tmux_window_name": win_name if use_tmux else "",
            "model": model,
            "reasoning_effort": reasoning_effort,
            "ultracode": use_ultracode,
            "sync": sync_mode,
            "argv": argv,
            "prompt_file": str(prompt_file),
            "dialogue_md": str(run_dir / DIALOGUE_FILE),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    binary = harness_bin(to_harness)
    if not binary:
        print(json.dumps({"ok": False, "error": f"{to_harness} CLI not found"}, ensure_ascii=False), file=sys.stderr)
        return 1

    if to_harness == "claude" and sid:
        agents = query_claude_agents(workdir)
        matched = next((a for a in agents if a.get("sessionId") == sid), None)
        if matched and matched.get("kind") == "interactive":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dispatched": False,
                        "delivery_mode": "manual_paste_to_interactive_session",
                        "workdir": str(workdir),
                        "session_id": sid,
                        "resume_cmd": f'cd "{workdir}" && claude --resume {sid}',
                        "prompt_file": str(prompt_file),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

    env_overrides = {
        "HARNESS_LOOP_DEPTH": next_env["HARNESS_LOOP_DEPTH"],
        "AGY_LOOP_DEPTH": next_env["AGY_LOOP_DEPTH"],
    }

    if to_harness == "claude" and not sync_mode:
        dispatch_ts = round(time.time(), 3)
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            env=next_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        stdout = proc.stdout.strip()
        if proc.returncode != 0:
            print(json.dumps({"ok": False, "dispatched": False, "returncode": proc.returncode, "stderr": proc.stderr.strip(), "stdout": stdout}, ensure_ascii=False, indent=2))
            return 1
        m = re.search(r"\b([0-9a-f]{8})\b", stdout)
        bg_id = m.group(1) if m else ""
        time.sleep(1.5)
        agents = query_claude_agents(workdir)
        matched = next((a for a in agents if (bg_id and a.get("id") == bg_id) or a.get("name") == session_name), None)
        final_sid = (matched.get("sessionId") if matched else None) or sid
        matched_bg = (matched.get("id") if matched else None) or bg_id
        matched_name = (matched.get("name") if matched else None) or session_name
        matched_kind = (matched.get("kind") if matched else None) or "background"
        jsonl = find_cc_jsonl(workdir, final_sid or matched_bg or None)
        if not final_sid and jsonl:
            final_sid = jsonl.stem

        resume_hint = f'cd "{workdir}" && claude --resume {final_sid}' if final_sid else (f"claude attach {matched_bg}" if matched_bg else "")
        tmux_info: dict[str, Any] = {}
        attach_target = matched_bg or final_sid
        if use_tmux and attach_target:
            spec_payload = {
                "job_id": job_id,
                "wave": wave or "-",
                "label": label or win_name,
                "harness": "claude",
                "model": model,
                "mode": "write",
                "workdir": str(workdir),
                "branch": branch,
                "attach_bg_id": attach_target,
                "prompt_file": str(prompt_file),
                "resume_hint": resume_hint,
                "env_overrides": env_overrides,
            }
            _atomic_write_text(spec_file, json.dumps(spec_payload, ensure_ascii=False, indent=2))
            tmux_info = _launch_in_tmux(
                tmux_sess,
                repo,
                spec_path=spec_file,
                win_name=win_name,
                workdir=workdir,
                pid_file=None,
                wait_for_child_pid=False,
            )

        job_meta = {
            "job_id": job_id,
            "wave": wave,
            "label": label or win_name,
            "track": track,
            "harness": "claude",
            "role_name": args.role_name,
            "mode": "write",
            "workdir": str(workdir),
            "branch": branch,
            "pid": 0,
            "bg_id": matched_bg,
            "attach_bg_id": attach_target,
            "session_id": final_sid,
            "name": matched_name,
            "kind": matched_kind,
            "model": model,
            "stdout_jsonl": str(jsonl) if jsonl else "",
            "prompt_file": str(prompt_file),
            "tmux_session": tmux_info.get("tmux_session", ""),
            "tmux_window_index": tmux_info.get("tmux_window_index"),
            "tmux_window_id": tmux_info.get("tmux_window_id", ""),
            "tmux_window_name": tmux_info.get("tmux_window_name", ""),
            "tmux_pane_id": tmux_info.get("tmux_pane_id", ""),
            "tmux_pane_pid": tmux_info.get("tmux_pane_pid", 0),
            "tmux_target": tmux_info.get("tmux_target", ""),
            "status": "RUNNING",
            "started_at": now_iso(),
        }
        updated_manifest = _record_job_in_manifest(
            repo,
            run_dir,
            job_meta,
            harness="claude",
            session_id=final_sid,
            model=model,
            bg_id=matched_bg,
            name=matched_name,
            kind=matched_kind,
            log_path=str(jsonl) if jsonl else "",
            dispatch_ts=dispatch_ts,
            tmux_sess=tmux_info.get("tmux_session", ""),
        )
        out: dict[str, Any] = {
            "ok": True,
            "dispatched": True,
            "background": True,
            "harness": "claude",
            "job_id": job_id,
            "rework": is_rework,
            "round": next_round,
            "job": job_meta,
            "workdir": str(workdir),
            "branch": branch,
            "model": model,
            "ultracode": use_ultracode,
            "bg_id": matched_bg,
            "session_id": final_sid,
            "commands": format_cc_commands(updated_manifest),
            "prompt_file": str(prompt_file),
            "dialogue_md": str(run_dir / DIALOGUE_FILE),
        }
        if tmux_info:
            out["tmux_session"] = tmux_sess
            out["tmux_target"] = tmux_info["tmux_target"]
            out["tmux_attach_cmd"] = f"tmux {sock_flag}attach -t {tmux_sess}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not sync_mode:
        dispatch_ts = round(time.time(), 3)
        tmux_info = {}
        if use_tmux:
            spec_payload = {
                "job_id": job_id,
                "wave": wave or "-",
                "label": label or win_name,
                "harness": to_harness,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "mode": "write",
                "workdir": str(workdir),
                "branch": branch,
                "argv": argv,
                "env_overrides": env_overrides,
                "prompt_file": str(prompt_file),
                "stdout_jsonl": str(stdout_log),
                "last_msg_file": str(last_msg_file),
                "pid_file": str(pid_file),
                "exit_file": str(exit_file),
                "resume_hint": f'cd "{workdir}" && codex resume {sid}' if (to_harness == "codex" and sid) else "",
            }
            _atomic_write_text(spec_file, json.dumps(spec_payload, ensure_ascii=False, indent=2))
            tmux_info = _launch_in_tmux(
                tmux_sess,
                repo,
                spec_path=spec_file,
                win_name=win_name,
                workdir=workdir,
                pid_file=pid_file,
                wait_for_child_pid=True,
            )
            tracked_pid = tmux_info["child_pid"] or tmux_info["tmux_pane_pid"]
        else:
            log_fh = stdout_log.open("w", encoding="utf-8")
            proc = subprocess.Popen(
                argv,
                cwd=str(workdir),
                env=next_env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log_fh.close()
            tracked_pid = proc.pid

        job_meta = {
            "job_id": job_id,
            "wave": wave,
            "label": label or win_name,
            "track": track,
            "harness": to_harness,
            "role_name": args.role_name,
            "mode": "write",
            "workdir": str(workdir),
            "branch": branch,
            "pid": tracked_pid,
            "session_id": sid,
            "model": model,
            "stdout_jsonl": str(stdout_log),
            "last_msg_file": str(last_msg_file),
            "prompt_file": str(prompt_file),
            "pid_file": str(pid_file) if use_tmux else "",
            "exit_file": str(exit_file) if use_tmux else "",
            "tmux_session": tmux_info.get("tmux_session", ""),
            "tmux_window_index": tmux_info.get("tmux_window_index"),
            "tmux_window_id": tmux_info.get("tmux_window_id", ""),
            "tmux_window_name": tmux_info.get("tmux_window_name", ""),
            "tmux_pane_id": tmux_info.get("tmux_pane_id", ""),
            "tmux_pane_pid": tmux_info.get("tmux_pane_pid", 0),
            "tmux_target": tmux_info.get("tmux_target", ""),
            "status": "RUNNING",
            "started_at": now_iso(),
        }
        _record_job_in_manifest(
            repo,
            run_dir,
            job_meta,
            harness=to_harness,
            session_id=sid,
            model=model,
            dispatch_ts=dispatch_ts,
            tmux_sess=tmux_info.get("tmux_session", ""),
        )
        out = {
            "ok": True,
            "dispatched": True,
            "background": True,
            "harness": to_harness,
            "job_id": job_id,
            "rework": is_rework,
            "round": next_round,
            "job": job_meta,
            "dialogue_md": str(run_dir / DIALOGUE_FILE),
        }
        if tmux_info:
            out["tmux_session"] = tmux_sess
            out["tmux_target"] = tmux_info["tmux_target"]
            out["tmux_attach_cmd"] = f"tmux {sock_flag}attach -t {tmux_sess}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # sync_mode
    tmux_info = {}
    returncode = 1
    stdout_text = ""
    stderr_text = ""
    if use_tmux:
        spec_payload = {
            "job_id": job_id,
            "wave": wave or "-",
            "label": label or win_name,
            "harness": to_harness,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "mode": "write",
            "workdir": str(workdir),
            "branch": branch,
            "argv": argv,
            "env_overrides": env_overrides,
            "prompt_file": str(prompt_file),
            "stdout_jsonl": str(stdout_log),
            "last_msg_file": str(last_msg_file),
            "pid_file": str(pid_file),
            "exit_file": str(exit_file),
        }
        _atomic_write_text(spec_file, json.dumps(spec_payload, ensure_ascii=False, indent=2))
        tmux_info = _launch_in_tmux(
            tmux_sess,
            repo,
            spec_path=spec_file,
            win_name=win_name,
            workdir=workdir,
            pid_file=pid_file,
            wait_for_child_pid=True,
        )
        job_meta = {
            "job_id": job_id,
            "wave": wave,
            "label": label or win_name,
            "track": track,
            "harness": to_harness,
            "role_name": args.role_name,
            "mode": "write",
            "workdir": str(workdir),
            "branch": branch,
            "pid": tmux_info["child_pid"] or tmux_info["tmux_pane_pid"],
            "model": model,
            "stdout_jsonl": str(stdout_log),
            "last_msg_file": str(last_msg_file),
            "prompt_file": str(prompt_file),
            "pid_file": str(pid_file),
            "exit_file": str(exit_file),
            "tmux_session": tmux_sess,
            "tmux_window_index": tmux_info["tmux_window_index"],
            "tmux_window_id": tmux_info["tmux_window_id"],
            "tmux_window_name": win_name,
            "tmux_pane_id": tmux_info["tmux_pane_id"],
            "tmux_target": tmux_info["tmux_target"],
            "status": "RUNNING",
            "started_at": now_iso(),
        }
        _record_job_in_manifest(repo, run_dir, job_meta, harness=to_harness, model=model, tmux_sess=tmux_sess)
        deadline = time.monotonic() + max(int(args.timeout), 1)
        exit_data = None
        while time.monotonic() < deadline:
            exit_data = _is_job_exit_recorded(job_meta)
            if exit_data is not None:
                break
            time.sleep(0.25)
        if exit_data is None:
            print(json.dumps({"ok": False, "dispatched": True, "background": False, "job_id": job_id, "error": f"delegate timeout after {args.timeout}s"}, ensure_ascii=False))
            return 1
        returncode = int(exit_data.get("exit_code", 1))
        stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace") if stdout_log.exists() else ""
        stderr_log = stdout_log.with_suffix(".stderr.log")
        stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace") if stderr_log.exists() else ""
        job_meta["exit_code"] = returncode
        job_meta["finished_at"] = exit_data.get("finished_at") or now_iso()
    else:
        proc = subprocess.run(
            argv,
            cwd=str(workdir),
            env=next_env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
        returncode = proc.returncode
        stdout_text = proc.stdout
        stderr_text = proc.stderr

    reply_text, new_sid = extract_reply_and_session(to_harness, stdout_text, reply_file=last_msg_file)
    final_sid = new_sid or sid
    if returncode != 0:
        if use_tmux:
            job_meta["status"] = "PROCESS_EXITED_FAILED"
            job_meta["session_id"] = final_sid
            _record_job_in_manifest(repo, run_dir, job_meta, harness=to_harness, session_id=final_sid, model=model, tmux_sess=tmux_sess)
        err_out = {
            "ok": False,
            "dispatched": True,
            "background": False,
            "returncode": returncode,
            "harness": to_harness,
            "job_id": job_id,
            "model": model,
            "session_id": final_sid,
            "error_output": reply_text,
            "stderr": stderr_text.strip(),
            "dialogue_md": str(run_dir / DIALOGUE_FILE),
        }
        print(json.dumps(err_out, ensure_ascii=False, indent=2))
        return 1

    if use_tmux:
        job_meta["status"] = "COMPLETED"
        job_meta["session_id"] = final_sid
        _record_job_in_manifest(repo, run_dir, job_meta, harness=to_harness, session_id=final_sid, model=model, tmux_sess=tmux_sess)
    else:
        with _manifest_lock(run_dir):
            manifest = load_manifest(run_dir)
            _sync_discovered_session(repo, manifest, to_harness, session_id=final_sid, model=model)
            save_manifest(run_dir, manifest)
    append_event(
        repo,
        run_dir,
        role=to_harness,
        event="delivery",
        summary=f"{to_harness} 完成同步派发任务",
        body=reply_text,
        phase="POST_VERIFIER",
        extra={"model": model, "session_id": final_sid, "job_id": job_id, "workdir": str(workdir)},
    )
    out = {
        "ok": True,
        "dispatched": True,
        "background": False,
        "harness": to_harness,
        "job_id": job_id,
        "rework": is_rework,
        "round": next_round,
        "workdir": str(workdir),
        "model": model,
        "session_id": final_sid,
        "reply": reply_text,
        "dialogue_md": str(run_dir / DIALOGUE_FILE),
    }
    if tmux_info:
        out["tmux_target"] = tmux_info.get("tmux_target")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _try_load_run(repo: Path, run_id: str | None) -> tuple[Path | None, dict[str, Any]]:
    if run_id or (repo / LOOP_DIR_NAME / LATEST_RUN_FILE).exists():
        try:
            rd = resolve_run_dir(repo, run_id)
            return rd, load_manifest(rd)
        except FileNotFoundError:
            return None, {}
    return None, {}


def _find_job_in_manifest(manifest: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    """Find a recorded job entry by exact job_id."""
    if not job_id or not manifest:
        return None
    for j in manifest.get("jobs") or []:
        if isinstance(j, dict) and j.get("job_id") == job_id:
            return j
    return None


def cmd_inspect_cc(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir, manifest = _try_load_run(repo, args.run_id)
    job = _find_job_in_manifest(manifest, getattr(args, "job_id", ""))
    workdir = (
        Path(args.workdir).resolve()
        if getattr(args, "workdir", "")
        else (Path(job["workdir"]).resolve() if (job and job.get("workdir")) else repo)
    )

    sid = args.session_id or (job.get("session_id") if job else "") or manifest.get("cc_session_id") or ""
    agents = query_claude_agents(workdir)
    agent_entry, sid = select_repo_agent(agents, sid or None)

    jsonl_path = find_cc_jsonl(workdir, sid or (job.get("bg_id") if job else None) or None)
    if not sid and jsonl_path:
        sid = jsonl_path.stem
    jsonl_info = parse_cc_jsonl_tail(jsonl_path, args.tail_turns) if jsonl_path else None
    normalized_state = classify_cc_state(agent_entry, jsonl_info)

    base = manifest.get("base_commit") or ""
    commits_since_base: list[str] = []
    if base and base != "NO_GIT":
        rc, out, _ = run_git(workdir if workdir.exists() else repo, ["log", f"{base}..HEAD", "--oneline"])
        if rc == 0 and out:
            commits_since_base = out.splitlines()

    synced = False
    if args.sync_ledger and run_dir is not None and normalized_state == "IDLE_COMPLETED" and jsonl_info:
        last_msg = jsonl_info.get("last_assistant_message")
        msg_ts = last_msg.get("timestamp") if last_msg else None
        job_id = str(job.get("job_id") or "") if job else ""
        with _manifest_lock(run_dir):
            manifest = load_manifest(run_dir)
            already = set(manifest.get("synced_cc_assistant_ts") or [])
            dedup_key = _delivery_dedup_key(str(msg_ts or ""), job_id=job_id, sid=sid) if msg_ts else ""
            sid_key = f"{sid}:{msg_ts}" if (sid and msg_ts) else ""
            is_synced = bool(msg_ts) and (
                dedup_key in already
                or (not job_id and ((sid_key and sid_key in already) or (not sid and msg_ts in already)))
            )
            if last_msg and msg_ts and not is_synced:
                sync_manifest_session(
                    manifest,
                    "claude",
                    session_id=sid or None,
                    path=str(jsonl_path) if jsonl_path else None,
                )
                recorded = manifest.setdefault("synced_cc_assistant_ts", [])
                recorded.append(dedup_key)
                if sid_key and sid_key != dedup_key and sid_key not in recorded:
                    recorded.append(sid_key)
                save_manifest(run_dir, manifest)
                first_line = last_msg["text"].splitlines()[0][:100] if last_msg["text"] else "Claude Code 已交卷"
                append_event(
                    repo,
                    run_dir,
                    role="claude",
                    event="cc_delivery",
                    summary=first_line,
                    body=last_msg["text"],
                    phase="POST_VERIFIER",
                    commits=",".join(c.split()[0] for c in commits_since_base) if commits_since_base else None,
                    extra={"workdir": str(workdir), "job_id": job.get("job_id")} if job else None,
                )
                synced = True

    result = {
        "harness": "claude",
        "repo": str(repo),
        "workdir": str(workdir),
        "job_id": job.get("job_id") if job else "",
        "normalized_state": normalized_state,
        "session_id": sid,
        "agent_entry": agent_entry,
        "all_repo_agents": agents,
        "jsonl": jsonl_info,
        "git_status_short": git_status_short(workdir) if workdir.exists() else [],
        "commits_since_base": commits_since_base,
        "synced_to_ledger": synced,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_inspect_codex(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir, manifest = _try_load_run(repo, args.run_id)
    ensure_manifest_v2(manifest) if manifest else None

    job = _find_job_in_manifest(manifest, getattr(args, "job_id", ""))
    if job and job.get("harness") == "codex":
        latest_job: dict[str, Any] | None = job
    else:
        codex_jobs = [
            j for j in (manifest.get("jobs") or [])
            if isinstance(j, dict) and j.get("harness") == "codex"
        ]
        latest_job = codex_jobs[-1] if codex_jobs else None

    workdir = (
        Path(args.workdir).resolve()
        if getattr(args, "workdir", "")
        else (Path(latest_job["workdir"]).resolve() if (latest_job and latest_job.get("workdir")) else repo)
    )
    sid = (
        args.session_id
        or (latest_job.get("session_id") if latest_job else "")
        or (manifest.get("sessions", {}).get("codex", {}).get("session_id") if manifest else "")
        or ""
    )
    job_alive = is_pid_alive(latest_job.get("pid")) if latest_job else False

    if not sid and latest_job and latest_job.get("stdout_jsonl"):
        log_path = Path(latest_job["stdout_jsonl"])
        if log_path.exists():
            _, extracted_sid = extract_reply_and_session("codex", log_path.read_text(encoding="utf-8", errors="replace"))
            if extracted_sid:
                sid = extracted_sid

    rollout_path = find_codex_rollout(workdir, sid or None)
    rollout_info = parse_codex_rollout_tail(rollout_path, args.tail_turns) if rollout_path else None
    if not sid and rollout_info:
        sid = rollout_info.get("session_id") or ""

    if latest_job and not rollout_info and latest_job.get("last_msg_file"):
        lmf = Path(latest_job["last_msg_file"])
        if lmf.exists() and not job_alive:
            txt = lmf.read_text(encoding="utf-8", errors="replace").strip()
            if txt:
                rollout_info = {
                    "rollout_path": str(lmf),
                    "session_id": sid,
                    "has_task_complete": True,
                    "has_aborted": False,
                    "last_assistant_message": {"role": "codex", "timestamp": now_iso(), "text": txt},
                    "recent_turns": [],
                }

    normalized_state = classify_codex_state(job_alive, rollout_info)
    thread_name = lookup_codex_thread_names().get(sid, "") if sid else ""

    base = manifest.get("base_commit") or ""
    commits_since_base: list[str] = []
    if base and base != "NO_GIT":
        rc, out, _ = run_git(workdir if workdir.exists() else repo, ["log", f"{base}..HEAD", "--oneline"])
        if rc == 0 and out:
            commits_since_base = out.splitlines()

    synced = False
    if args.sync_ledger and run_dir is not None and normalized_state == "IDLE_COMPLETED" and rollout_info:
        last_msg = rollout_info.get("last_assistant_message")
        msg_ts = last_msg.get("timestamp") if last_msg else None
        job_id = str(latest_job.get("job_id") or "") if latest_job else ""
        with _manifest_lock(run_dir):
            manifest = load_manifest(run_dir)
            already = set(manifest.get("synced_codex_ts") or [])
            dedup_key = _delivery_dedup_key(str(msg_ts or ""), job_id=job_id, sid=sid) if msg_ts else ""
            sid_key = f"{sid}:{msg_ts}" if (sid and msg_ts) else ""
            is_synced = bool(msg_ts) and (
                dedup_key in already
                or (not job_id and ((sid_key and sid_key in already) or (not sid and msg_ts in already)))
            )
            if last_msg and msg_ts and not is_synced:
                sync_manifest_session(
                    manifest,
                    "codex",
                    session_id=sid or None,
                    name=thread_name or None,
                    path=str(rollout_path) if rollout_path else None,
                )
                recorded = manifest.setdefault("synced_codex_ts", [])
                recorded.append(dedup_key)
                if sid_key and sid_key != dedup_key and sid_key not in recorded:
                    recorded.append(sid_key)
                save_manifest(run_dir, manifest)
                first_line = last_msg["text"].splitlines()[0][:100] if last_msg["text"] else "Codex 已交卷"
                append_event(
                    repo,
                    run_dir,
                    role="codex",
                    event="delivery",
                    summary=first_line,
                    body=last_msg["text"],
                    phase="POST_VERIFIER",
                    commits=",".join(c.split()[0] for c in commits_since_base) if commits_since_base else None,
                    extra={"workdir": str(workdir), "job_id": latest_job.get("job_id")} if latest_job else None,
                )
                synced = True

    result = {
        "harness": "codex",
        "repo": str(repo),
        "workdir": str(workdir),
        "job_id": latest_job.get("job_id") if latest_job else "",
        "normalized_state": normalized_state,
        "session_id": sid,
        "thread_name": thread_name,
        "job_alive": job_alive,
        "latest_job": latest_job,
        "rollout": rollout_info,
        "git_status_short": git_status_short(workdir) if workdir.exists() else [],
        "commits_since_base": commits_since_base,
        "synced_to_ledger": synced,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _read_agy_transcript_turns(transcript: Path, last_n: int) -> list[dict[str, Any]]:
    """Extract readable USER_INPUT and PLANNER_RESPONSE turns from an AGY transcript."""
    turns: list[dict[str, Any]] = []
    for raw in _read_tail_lines(transcript, max_bytes=8 * 1024 * 1024):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rtype = rec.get("type")
        content = rec.get("content") or ""
        if rtype == "USER_INPUT" and content:
            turns.append({
                "step_index": rec.get("step_index"),
                "role": "user",
                "created_at": rec.get("created_at"),
                "text": clean_agy_user_text(content),
                "truncated": "content" in (rec.get("truncated_fields") or []),
            })
        elif rtype == "PLANNER_RESPONSE" and content:
            turns.append({
                "step_index": rec.get("step_index"),
                "role": "agy",
                "created_at": rec.get("created_at"),
                "text": content.strip(),
                "truncated": "content" in (rec.get("truncated_fields") or []),
            })
    return turns[-last_n:] if last_n > 0 else turns


def _resolve_agy_id(agy_id: str, repo: str | Path | None, run_id: str | None) -> str:
    """Resolve an AGY conversation ID from --agy-id or the repo run manifest."""
    if agy_id:
        return agy_id
    if repo:
        _, manifest = _try_load_run(Path(repo).resolve(), run_id)
        return str(manifest.get("agy_conversation_id") or "")
    return ""


def _print_agy_view(agy_id: str, last_n: int) -> int:
    """Print readable AGY conversation turns for terminal inspection."""
    if not agy_id:
        recent_dirs = list_recent_agy_sessions(limit=8)
        print("未指定或未绑定 AGY conversation ID，最近会话：", file=sys.stderr)
        for item in recent_dirs:
            print(f"  - {item['agy_id']} ({item['source']})", file=sys.stderr)
        return 1

    transcript = find_agy_transcript(agy_id)
    if not transcript:
        print(f"未找到 AGY 会话日志：{agy_id}", file=sys.stderr)
        return 1

    source = "cli" if AGY_CLI_BRAIN_ROOT in transcript.parents else "desktop"
    turns = _read_agy_transcript_turns(transcript, last_n)

    print("=== Antigravity (AGY) 对话记录 ===")
    print(f"Conversation ID: {agy_id} (source: {source})")
    print(f"URI: conversation://{agy_id}")
    print(f"Transcript: {transcript}")
    if source == "cli":
        print(f"CLI Resume: agy --conversation {agy_id}")
    if not turns:
        print("\n(暂无可读文本轮次)")
        return 0

    for t in turns:
        role_label = "USER" if t["role"] == "user" else "AGY"
        trunc_note = " [truncated]" if t.get("truncated") else ""
        print(f"\n--- [step_{t.get('step_index')}] [{role_label}] ({t.get('created_at') or ''}){trunc_note} ---")
        print(t.get("text") or "")
    return 0


def _print_claude_view(
    repo: Path | None,
    manifest: dict[str, Any] | None,
    *,
    claude_id: str = "",
    last_n: int = 10,
    full_text: bool = False,
    job: dict[str, Any] | None = None,
) -> int:
    """Print readable Claude Code JSONL turns and native attach/resume commands."""
    cc_slot = (manifest or {}).get("sessions", {}).get("claude", {}) if manifest else {}
    cid = (
        claude_id
        or (job.get("session_id") if job else "")
        or (job.get("bg_id") if job else "")
        or cc_slot.get("session_id")
        or cc_slot.get("bg_id")
        or ""
    )
    workdir = Path(job["workdir"]).resolve() if (job and job.get("workdir")) else repo

    jsonl_path: Path | None = None
    if job and job.get("stdout_jsonl") and Path(str(job["stdout_jsonl"])).exists():
        jsonl_path = Path(str(job["stdout_jsonl"]))
    if not jsonl_path and workdir:
        jsonl_path = find_cc_jsonl(workdir, cid or None)
    if not jsonl_path and cid:
        jsonl_path = find_cc_jsonl_global(cid)
    if not jsonl_path and cc_slot.get("jsonl_path") and Path(str(cc_slot["jsonl_path"])).exists():
        jsonl_path = Path(str(cc_slot["jsonl_path"]))

    meta = read_cc_session_meta(jsonl_path) if jsonl_path else {"session_id": cid, "cwd": "", "git_branch": ""}
    sid = meta.get("session_id") or cid
    cwd = meta.get("cwd") or (str(workdir) if workdir else "")
    branch = meta.get("git_branch") or (job.get("branch") if job else "") or ""

    all_agents = query_claude_agents(None) if cid else []
    agent = next(
        (a for a in all_agents if (sid and a.get("sessionId") == sid) or (cid and a.get("id") == cid)),
        None,
    )
    bg_id = (agent.get("id") if agent else "") or (job.get("bg_id") if job else "") or cc_slot.get("bg_id") or (cid if len(cid) == 8 else "")
    state = (agent.get("state") if agent else "") or (job.get("status") if job else "") or ""

    print("=== Claude Code 对话记录 ===")
    print(f"Session ID: {sid or '未绑定'}" + (f" (bg: {bg_id})" if bg_id else "") + (f" [state={state}]" if state else ""))
    if cwd:
        cwd_note = "" if Path(cwd).exists() else " [目录已清理]"
        br_note = f" | Branch: {branch}" if branch else ""
        print(f"Workdir: {cwd}{cwd_note}{br_note}")
    print(f"Log: {jsonl_path or '未找到'}")
    if bg_id:
        print(f"Terminal Quick View: claude logs {bg_id}  |  claude attach {bg_id}")
    if sid:
        if cwd and Path(cwd).exists():
            print(f'Interactive Resume: cd "{cwd}" && claude --resume {sid}')
        elif repo:
            print(f'Interactive Resume: cd "{repo}" && claude --resume {sid}')
        else:
            print(f"Interactive Resume: claude --resume {sid}")

    if not jsonl_path or not jsonl_path.exists():
        print("\n(未找到 Claude Code JSONL 日志)")
        return 0 if (sid or bg_id) else 1

    info = parse_cc_jsonl_tail(jsonl_path, last_n, include_full_text=True)
    turns = info.get("recent_turns") or []
    if not turns:
        print("\n(暂无可读文本轮次)")
        return 0

    for idx, t in enumerate(turns, 1):
        role_label = "USER" if t.get("role") == "user" else "CLAUDE"
        tools = t.get("tools") or []
        tools_tag = f" [tools: {', '.join(tools[:8])}{'...' if len(tools) > 8 else ''}]" if tools else ""
        print(f"\n--- [{idx}] [{role_label}] ({t.get('timestamp') or ''}){tools_tag} ---")
        body = (t.get("text") if full_text else (t.get("text_preview") or t.get("text"))) or ""
        if body:
            print(body)
        elif tools:
            print(f"(tool calls only: {', '.join(tools)})")
    return 0


def _print_codex_view(
    repo: Path | None,
    manifest: dict[str, Any] | None,
    last_n: int,
    *,
    codex_id: str = "",
    full_text: bool = False,
    job: dict[str, Any] | None = None,
) -> int:
    """Print readable Codex rollout turns and interactive resume command."""
    if manifest:
        ensure_manifest_v2(manifest)
    codex_slot = (manifest or {}).get("sessions", {}).get("codex", {})
    sid = (
        codex_id
        or (job.get("session_id") if job else "")
        or codex_slot.get("session_id")
        or ""
    )
    model = (job.get("model") if job else "") or codex_slot.get("model") or ""
    workdir = Path(job["workdir"]).resolve() if (job and job.get("workdir")) else repo

    rollout_str = codex_slot.get("rollout_path") or ""
    rollout_path: Path | None = None
    if sid:
        rollout_path = find_codex_rollout(workdir or repo or Path.cwd(), sid)
    if not rollout_path and rollout_str and Path(rollout_str).exists() and not codex_id and not job:
        rollout_path = Path(rollout_str)
    if not rollout_path and (workdir or repo):
        rollout_path = find_codex_rollout(workdir or repo or Path.cwd(), None)

    meta = read_codex_session_meta(rollout_path) if rollout_path else {}
    if not sid and meta.get("id"):
        sid = str(meta["id"])
    cwd = str(meta.get("cwd") or (workdir if workdir else repo or ""))

    print("=== Codex 对话记录 ===")
    print(f"Session ID: {sid or '未绑定'}")
    print(f"Model: {model or '继承全局默认'}")
    if cwd:
        cwd_note = "" if Path(cwd).exists() else " [原 worktree 已清理]"
        print(f"Workdir: {cwd}{cwd_note}")
    print(f"Rollout: {rollout_path or '未找到'}")
    if sid:
        if cwd and Path(cwd).exists():
            print(f'Interactive Resume: cd "{cwd}" && codex resume {sid}')
        else:
            fallback_dir = str(repo) if repo else str(Path.cwd())
            print(f'Interactive Resume: codex resume --all -C "{fallback_dir}" {sid}')
    if not rollout_path or not rollout_path.exists():
        if job and job.get("last_msg_file") and Path(str(job["last_msg_file"])).exists():
            print(f"\n--- [1] [CODEX last_msg] ---\n{Path(str(job['last_msg_file'])).read_text(encoding='utf-8', errors='replace').strip()}")
            return 0
        print("\n(未找到 Codex rollout 日志)")
        return 0 if sid else 1

    info = parse_codex_rollout_tail(rollout_path, last_n, include_full_text=True)
    turns = info.get("recent_turns") or []
    if not turns:
        last_msg = info.get("last_assistant_message")
        if last_msg and last_msg.get("text"):
            print(f"\n--- [1] [CODEX] ({last_msg.get('timestamp') or ''}) ---")
            print(last_msg["text"])
        else:
            print("\n(暂无可读文本轮次)")
        return 0

    for idx, t in enumerate(turns, 1):
        role_label = "USER" if t.get("role") == "user" else "CODEX"
        print(f"\n--- [{idx}] [{role_label}] ({t.get('timestamp') or ''}) ---")
        print((t.get("text") if full_text else (t.get("text_preview") or t.get("text"))) or "")
    return 0


def cmd_inspect_agy(args: argparse.Namespace) -> int:
    agy_id = _resolve_agy_id(args.agy_id, args.repo, args.run_id)
    if not agy_id:
        recent_dirs = list_recent_agy_sessions(limit=8)
        print(json.dumps({"ok": False, "error": "missing --agy-id", "recent_agy_sessions": recent_dirs}, ensure_ascii=False, indent=2))
        return 1

    transcript = find_agy_transcript(agy_id)
    if not transcript:
        print(json.dumps({"ok": False, "error": f"transcript not found for {agy_id}"}, ensure_ascii=False), file=sys.stderr)
        return 1

    last_n = getattr(args, "last", None)
    if last_n is None:
        last_n = getattr(args, "tail_turns", 6)

    out = {
        "ok": True,
        "harness": "agy",
        "agy_conversation_id": agy_id,
        "conversation_uri": f"conversation://{agy_id}",
        "transcript_path": str(transcript),
        "recent_messages": _read_agy_transcript_turns(transcript, last_n),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    claude_id = (getattr(args, "claude_id", "") or "").strip()
    codex_id = (getattr(args, "codex_id", "") or "").strip()
    agy_arg = (getattr(args, "agy_id", "") or "").strip()
    job_id = (getattr(args, "job_id", "") or "").strip()
    full_text = bool(getattr(args, "full", False))

    repo = Path(args.repo).resolve() if args.repo else None
    manifest: dict[str, Any] = {}
    run_dir: Path | None = None
    if repo:
        try:
            run_dir = resolve_run_dir(repo, args.run_id)
            manifest = load_manifest(run_dir)
        except FileNotFoundError:
            manifest = {}

    if claude_id:
        return _print_claude_view(repo, manifest or None, claude_id=claude_id, last_n=args.last, full_text=full_text)
    if codex_id:
        return _print_codex_view(repo, manifest or None, args.last, codex_id=codex_id, full_text=full_text)

    if not repo:
        if agy_arg or args.target == "agy":
            return _print_agy_view(agy_arg, args.last)
        print("错误：--target all/dialogue 需要传入 --repo，或使用 --claude-id / --codex-id / --agy-id 直接查看指定会话", file=sys.stderr)
        return 1

    if args.target == "agy":
        agy_id = _resolve_agy_id(agy_arg, repo, args.run_id)
        return _print_agy_view(agy_id, args.last)

    if run_dir is None:
        run_dir = resolve_run_dir(repo, args.run_id)
        manifest = load_manifest(run_dir)

    if job_id:
        job = _find_job_in_manifest(manifest, job_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")
        print(f"=== Job 详情 · {job_id} ===")
        print(
            f"Wave: {job.get('wave') or '-'} | Harness: {job.get('harness')} ({job.get('model') or '默认'}) | "
            f"Workdir: {job.get('workdir')} | Tmux: {job.get('tmux_target') or '-'}\n"
        )
        jh = job.get("harness")
        if jh == "claude":
            return _print_claude_view(repo, manifest, last_n=args.last, full_text=full_text, job=job)
        if jh == "codex":
            return _print_codex_view(repo, manifest, args.last, full_text=full_text, job=job)
        return _print_agy_view(_resolve_agy_id(agy_arg, repo, args.run_id), args.last)

    if args.target == "claude":
        return _print_claude_view(repo, manifest, last_n=args.last, full_text=full_text)
    if args.target == "codex":
        return _print_codex_view(repo, manifest, args.last, full_text=full_text)

    latest_md = repo / LOOP_DIR_NAME / LATEST_MD_FILE
    dialogue_md = latest_md if latest_md.exists() else (run_dir / DIALOGUE_FILE)
    if args.target == "dialogue":
        if not dialogue_md.exists():
            print(f"未找到台账文件：{dialogue_md}", file=sys.stderr)
            return 1
        print(dialogue_md.read_text(encoding="utf-8").rstrip())
        return 0

    # target == "all"
    cmds = format_all_harness_commands(manifest)
    roster = manifest.get("roster") or {}
    roster_parts = [
        f"{k}={v.get('harness')}{':' + v['model'] if v.get('model') else ''}"
        for k, v in roster.items()
        if isinstance(v, dict) and v.get("harness")
    ]
    for t_name, t_spec in (roster.get("tracks") or {}).items():
        if isinstance(t_spec, dict) and t_spec.get("harness"):
            roster_parts.append(f"track:{t_name}={t_spec['harness']}{':' + t_spec['model'] if t_spec.get('model') else ''}")

    print(f"=== Run 概览 · {manifest.get('run_id')} ({manifest.get('slug')}) ===")
    print(f"阶段: {manifest.get('phase')} | 结论: {manifest.get('verdict')} | 轮次: {manifest.get('round', 0)} | Git: {manifest.get('base_commit')} -> {git_head_short(repo)}")
    print(f"分工: {' · '.join(roster_parts)}")
    print("\n=== 终端直达命令 ===")
    if cmds.get("tmux", {}).get("attach"):
        print(f"Tmux 实时看全波: {cmds['tmux']['attach']}")
    if cmds.get("claude", {}).get("logs"):
        print(f"Claude Code 日志: {cmds['claude']['logs']}  |  {cmds['claude'].get('attach', '')}")
    if cmds.get("claude", {}).get("terminal_view"):
        print(f"Claude 只读回放: {cmds['claude']['terminal_view']}")
    if cmds.get("codex", {}).get("resume"):
        print(f"Codex 交互接入: {cmds['codex']['resume']}")
    if cmds.get("agy", {}).get("terminal_view"):
        print(f"AGY 终端只读查看: {cmds['agy']['terminal_view']}")
    if cmds.get("agy", {}).get("cli_resume"):
        print(f"AGY CLI 恢复: {cmds['agy']['cli_resume']}")
    if cmds.get("agy", {}).get("uri"):
        print(f"AGY IDE 协议链接: {cmds['agy']['uri']}")
    print(f"共享对话台账: {dialogue_md}")

    if manifest.get("jobs"):
        print("")
        _, table_text = _render_tmux_status_table(repo, manifest)
        print(table_text)

    if manifest.get("sessions", {}).get("claude", {}).get("session_id") or manifest.get("sessions", {}).get("claude", {}).get("bg_id"):
        print("")
        _print_claude_view(repo, manifest, last_n=args.last, full_text=full_text)
    if manifest.get("sessions", {}).get("codex", {}).get("session_id"):
        print("")
        _print_codex_view(repo, manifest, args.last, full_text=full_text)
    agy_id = _resolve_agy_id(agy_arg, repo, args.run_id)
    if agy_id:
        print("")
        _print_agy_view(agy_id, args.last)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    if args.harness == "agy":
        return cmd_inspect_agy(args)
    if args.harness == "claude":
        return cmd_inspect_cc(args)
    return cmd_inspect_codex(args)


def parse_iso_epoch(ts: str | None) -> float:
    """Parse an ISO-8601 timestamp into UNIX epoch seconds, or 0.0 on missing/invalid."""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _has_active_child_process(pid: int | None) -> bool:
    """Best-effort check whether pid currently has live child processes."""
    if not pid or pid <= 0:
        return False
    try:
        r = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _discover_fresh_log(
    harness: str,
    workdir: Path,
    sid: str,
    since_epoch: float,
    latest_job: dict[str, Any] | None = None,
    *,
    bg_id: str = "",
) -> tuple[str, Path | None, dict[str, Any] | None]:
    """Discover session ID and active transcript/rollout log for harness since `since_epoch`."""
    resolved_sid = sid
    if harness == "codex":
        if not resolved_sid and latest_job and latest_job.get("stdout_jsonl"):
            log_path = Path(latest_job["stdout_jsonl"])
            if log_path.exists():
                _, extracted_sid = extract_reply_and_session(
                    "codex", log_path.read_text(encoding="utf-8", errors="replace")
                )
                if extracted_sid:
                    resolved_sid = extracted_sid
        if resolved_sid:
            rollout = find_codex_rollout(workdir, resolved_sid)
            if rollout:
                return resolved_sid, rollout, None
        if CODEX_SESSIONS_ROOT.exists():
            repo_resolved = workdir.resolve()
            candidates = sorted(
                CODEX_SESSIONS_ROOT.rglob("rollout-*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for p in candidates[:40]:
                try:
                    if p.stat().st_mtime < since_epoch - 5.0:
                        break
                except OSError:
                    continue
                meta = read_codex_session_meta(p)
                cwd = meta.get("cwd")
                if not cwd:
                    continue
                try:
                    if Path(cwd).resolve() == repo_resolved:
                        return str(meta.get("id") or resolved_sid), p, None
                except OSError:
                    continue
        return resolved_sid, None, None

    if harness != "claude":
        if latest_job and latest_job.get("stdout_jsonl"):
            lp = Path(str(latest_job["stdout_jsonl"]))
            if lp.exists():
                return resolved_sid, lp, None
        return resolved_sid, None, None

    agents = query_claude_agents(workdir)
    agent_entry = None
    selected_sid = resolved_sid
    if bg_id:
        agent_entry = next((a for a in agents if a.get("id") == bg_id), None)
        if agent_entry and agent_entry.get("sessionId"):
            selected_sid = str(agent_entry["sessionId"])
    if not agent_entry:
        agent_entry, selected_sid = select_repo_agent(agents, selected_sid or None)

    if selected_sid:
        p = find_cc_jsonl(workdir, selected_sid)
        is_busy = bool(agent_entry and (agent_entry.get("status") == "busy" or agent_entry.get("state") == "running"))
        try:
            fresh = bool(p and p.stat().st_mtime >= since_epoch - 5.0)
        except OSError:
            fresh = False
        if p and (resolved_sid or is_busy or fresh):
            return selected_sid, p, agent_entry

    proj_dir = encode_claude_project_dir(workdir)
    if proj_dir.exists():
        recent_jsonls: list[Path] = []
        for p in proj_dir.glob("*.jsonl"):
            if not p.is_file():
                continue
            try:
                if p.stat().st_mtime >= since_epoch - 5.0:
                    recent_jsonls.append(p)
            except OSError:
                continue
        if recent_jsonls:
            recent_jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            cand = recent_jsonls[0]
            cand_sid = cand.stem
            matched_agent = next((a for a in agents if a.get("sessionId") == cand_sid), None)
            return cand_sid, cand, matched_agent

    if agent_entry and selected_sid and agent_entry.get("sessionId") == selected_sid:
        return selected_sid, None, agent_entry
    return resolved_sid, None, None


def _synthesize_codex_rollout_from_job(
    latest_job: dict[str, Any] | None,
    sid: str,
    effective_since: float,
) -> dict[str, Any] | None:
    """Fallback reading latest_job.last_msg_file if fresh rollout was not captured yet."""
    if not latest_job or not latest_job.get("last_msg_file"):
        return None
    lmf = Path(latest_job["last_msg_file"])
    try:
        if not lmf.exists() or lmf.stat().st_mtime < effective_since - 1.0:
            return None
        txt = lmf.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not txt:
        return None
    return {
        "rollout_path": str(lmf),
        "session_id": sid,
        "file_size_bytes": lmf.stat().st_size,
        "last_event_ts": now_iso(),
        "has_task_complete": True,
        "has_aborted": False,
        "last_assistant_message": {"role": "codex", "timestamp": now_iso(), "text": txt},
        "recent_turns": [],
    }


def _read_job_stdout_tail(latest_job: dict[str, Any] | None, max_lines: int = 30) -> str:
    """Read tail lines from latest_job.stdout_jsonl for failure diagnostics."""
    if not latest_job or not latest_job.get("stdout_jsonl"):
        return ""
    p = Path(latest_job["stdout_jsonl"])
    try:
        if not p.exists():
            return ""
        lines = _read_tail_lines(p, max_bytes=256 * 1024)
        return "\n".join(lines[-max_lines:]).strip()
    except OSError:
        return ""


def _delivery_dedup_key(msg_ts: str, *, job_id: str = "", sid: str = "") -> str:
    """Build a per-job/per-session delivery dedup key so same-second wave finishes never collide."""
    scope = (job_id or sid).strip()
    return f"{scope}:{msg_ts}" if scope else msg_ts


def _sync_watch_delivery(
    repo: Path,
    run_dir: Path | None,
    harness: str,
    *,
    sid: str,
    model: str,
    log_path: Path | None,
    last_msg: dict[str, Any] | None,
    commits_since_base: list[str],
    workdir: Path | None = None,
    job_id: str = "",
) -> bool:
    """Sync completed delivery message from watch into manifest + DIALOGUE.md once."""
    if run_dir is None or not last_msg or not str(last_msg.get("text") or "").strip():
        return False

    with _manifest_lock(run_dir):
        manifest = load_manifest(run_dir)
        ts_key = "synced_codex_ts" if harness == "codex" else "synced_cc_assistant_ts"
        msg_ts = str(last_msg.get("timestamp") or "") or sha256_prefix(str(last_msg["text"]))
        already = set(manifest.get(ts_key) or [])
        dedup_key = _delivery_dedup_key(msg_ts, job_id=job_id, sid=sid)
        sid_key = f"{sid}:{msg_ts}" if sid else ""
        is_synced = dedup_key in already
        if not job_id:
            is_synced = is_synced or bool(sid_key and sid_key in already) or (not sid and msg_ts in already)
        if is_synced:
            _sync_discovered_session(repo, manifest, harness, session_id=sid, model=model)
            save_manifest(run_dir, manifest)
            return False

        extra_path = str(log_path) if log_path else None
        thread_name = lookup_codex_thread_names().get(sid, "") if (harness == "codex" and sid) else ""
        sync_manifest_session(
            manifest,
            harness,
            session_id=sid or None,
            model=model or None,
            name=thread_name or None,
            path=extra_path,
        )
        recorded = manifest.setdefault(ts_key, [])
        recorded.append(dedup_key)
        if sid_key and sid_key != dedup_key and sid_key not in recorded:
            recorded.append(sid_key)
        save_manifest(run_dir, manifest)

        ev_name = "delivery" if harness == "codex" else "cc_delivery"
        fallback_summary = f"{harness} 已交卷"
        first_line = str(last_msg["text"]).splitlines()[0][:100] if str(last_msg["text"]).strip() else fallback_summary
        extra: dict[str, Any] = {"source": "watch"}
        if model:
            extra["model"] = model
        if sid:
            extra["session_id"] = sid
        if workdir:
            extra["workdir"] = str(workdir)
        if job_id:
            extra["job_id"] = job_id
        append_event(
            repo,
            run_dir,
            role=harness,
            event=ev_name,
            summary=first_line,
            body=str(last_msg["text"]),
            phase="POST_VERIFIER",
            commits=",".join(c.split()[0] for c in commits_since_base) if commits_since_base else None,
            extra=extra,
        )
        return True


def _update_job_terminal_state(
    repo: Path,
    run_dir: Path | None,
    job_id: str,
    *,
    reason: str,
    sid: str,
    log_path: Path | None,
    exit_code: int | None = None,
) -> None:
    """Update a job entry in manifest.json when it reaches terminal state."""
    if not run_dir or not job_id:
        return
    with _manifest_lock(run_dir):
        manifest = load_manifest(run_dir)
        for j in manifest.get("jobs") or []:
            if isinstance(j, dict) and j.get("job_id") == job_id:
                j["status"] = reason
                j["finished_at"] = j.get("finished_at") or now_iso()
                if sid:
                    j["session_id"] = sid
                if log_path and not j.get("stdout_jsonl"):
                    j["stdout_jsonl"] = str(log_path)
                if exit_code is not None:
                    j["exit_code"] = exit_code
                break
        save_manifest(run_dir, manifest)
        render_dialogue(repo, run_dir)


def _emit_watch_result(
    *,
    reason: str,
    exit_code: int,
    harness: str,
    repo: Path,
    run_dir: Path | None,
    manifest: dict[str, Any],
    sid: str,
    elapsed_sec: float,
    synced: bool,
    commits_since_base: list[str],
    last_msg: dict[str, Any] | None = None,
    diagnostic: dict[str, Any] | None = None,
    as_json: bool = False,
    workdir: Path | None = None,
    job_id: str = "",
) -> int:
    """Format and print standardized watch exit payload."""
    refreshed = load_manifest(run_dir) if run_dir else manifest
    cmds = format_all_harness_commands(refreshed) if refreshed else {}
    dialogue_md = str(run_dir / DIALOGUE_FILE) if run_dir else ""
    payload: dict[str, Any] = {
        "ok": reason == "COMPLETED",
        "reason": reason,
        "harness": harness,
        "repo": str(repo),
        "workdir": str(workdir or repo),
        "run_id": refreshed.get("run_id") or "",
        "job_id": job_id,
        "session_id": sid,
        "elapsed_sec": round(elapsed_sec, 2),
        "synced_to_ledger": synced,
        "git_head": git_head_short(workdir or repo),
        "git_status_short": git_status_short(workdir or repo),
        "commits_since_base": commits_since_base,
        "last_assistant_message": last_msg,
        "dialogue_md": dialogue_md,
        "commands": cmds,
    }
    if diagnostic:
        payload["diagnostic"] = diagnostic
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        preview = ""
        if last_msg and last_msg.get("text"):
            preview = str(last_msg["text"]).splitlines()[0][:120]
        print(f"[watch:{reason}] harness={harness} job={job_id or '-'} sid={sid or '-'} elapsed={payload['elapsed_sec']}s {preview}".strip())
    return exit_code


class _WatchTarget:
    """Per-job state tracker supporting both single-job and parallel wave monitoring."""

    def __init__(
        self,
        *,
        repo: Path,
        workdir: Path,
        harness: str,
        sid: str,
        bg_id: str,
        tracked_pid: int,
        effective_since: float,
        job_meta: dict[str, Any] | None,
        settle_sec: float,
        stall_timeout: int,
    ) -> None:
        self.repo = repo
        self.workdir = workdir
        self.harness = harness
        self.sid = sid
        self.bg_id = bg_id
        self.job_id = str(job_meta.get("job_id") or "") if job_meta else ""
        self.model = str(job_meta.get("model") or "") if job_meta else ""
        self.tracked_pid = tracked_pid
        self.ever_saw_pid_alive = is_pid_alive(tracked_pid) if tracked_pid else False
        self.effective_since = effective_since
        self.job_meta = job_meta
        self.settle_sec = settle_sec
        self.stall_timeout = stall_timeout

        self.start_mono = time.monotonic()
        self.last_activity_mono = self.start_mono
        self.last_git_check_mono = 0.0
        self.last_size = -1
        self.last_event_ts = ""
        self.last_git_sig = (git_head_short(workdir), tuple(git_status_short(workdir)))
        self.terminal: dict[str, Any] | None = None

    def _extract_job_completion_msg(self, log_path: Path | None) -> tuple[str, Path | None, dict[str, Any] | None]:
        cur_sid = self.sid
        chosen_path = log_path
        if self.harness == "codex":
            info = parse_codex_rollout_tail(chosen_path, 5) if (chosen_path and chosen_path.exists()) else None
            if not info:
                info = _synthesize_codex_rollout_from_job(self.job_meta, cur_sid, self.effective_since)
                if info and info.get("rollout_path"):
                    chosen_path = Path(info["rollout_path"])
            if info and info.get("session_id"):
                cur_sid = str(info["session_id"])
            last_msg = info.get("last_assistant_message") if info else None
            if (not last_msg or not last_msg.get("text")) and self.job_meta and self.job_meta.get("stdout_jsonl"):
                jl = Path(str(self.job_meta["stdout_jsonl"]))
                if jl.exists():
                    txt, found_sid = extract_reply_and_session("codex", jl.read_text(encoding="utf-8", errors="replace"))
                    if found_sid and not cur_sid:
                        cur_sid = found_sid
                    if txt:
                        last_msg = {"role": "codex", "timestamp": now_iso(), "text": txt}
            return cur_sid, chosen_path, last_msg
        if self.harness == "claude":
            info = parse_cc_jsonl_tail(chosen_path, 5) if (chosen_path and chosen_path.exists()) else None
            if info and info.get("last_assistant_message"):
                return cur_sid, chosen_path, info["last_assistant_message"]
        if self.job_meta and self.job_meta.get("stdout_jsonl"):
            jl = Path(str(self.job_meta["stdout_jsonl"]))
            if jl.exists():
                txt, found_sid = extract_reply_and_session(
                    self.harness,
                    jl.read_text(encoding="utf-8", errors="replace"),
                    reply_file=Path(str(self.job_meta["last_msg_file"])) if self.job_meta.get("last_msg_file") else None,
                )
                if found_sid and not cur_sid:
                    cur_sid = found_sid
                if txt:
                    return cur_sid, jl, {"role": self.harness, "timestamp": now_iso(), "text": txt}
        return cur_sid, chosen_path, None

    def poll_once(self) -> dict[str, Any] | None:
        if self.terminal is not None:
            return self.terminal

        discovered_sid, log_path, agent_entry = _discover_fresh_log(
            self.harness,
            self.workdir,
            self.sid,
            self.effective_since,
            latest_job=self.job_meta,
            bg_id=self.bg_id,
        )
        if discovered_sid:
            self.sid = discovered_sid

        # Signal 0: Atomic tmux runner exit_file (authoritative for codex/agy/sync workers)
        ex = _is_job_exit_recorded(self.job_meta) if self.job_meta else None
        if ex is not None:
            rc = int(ex.get("exit_code", 1))
            if self.settle_sec > 0 and rc == 0:
                time.sleep(min(self.settle_sec, 0.5))
                d_sid, ref_path, _ = _discover_fresh_log(
                    self.harness, self.workdir, self.sid, self.effective_since, latest_job=self.job_meta, bg_id=self.bg_id
                )
                if d_sid:
                    self.sid = d_sid
                if ref_path:
                    log_path = ref_path
            if rc == 0:
                self.sid, log_path, final_msg = self._extract_job_completion_msg(log_path)
                self.terminal = {
                    "reason": "COMPLETED",
                    "exit_code": 0,
                    "proc_exit_code": 0,
                    "sid": self.sid,
                    "log_path": log_path,
                    "last_msg": final_msg,
                    "diagnostic": None,
                }
                return self.terminal
            self.terminal = {
                "reason": "PROCESS_EXITED_FAILED",
                "exit_code": 1,
                "proc_exit_code": rc,
                "sid": self.sid,
                "log_path": log_path,
                "last_msg": None,
                "diagnostic": {"exit_code": rc, "stdout_tail": _read_job_stdout_tail(self.job_meta)},
            }
            return self.terminal

        pid_alive = is_pid_alive(self.tracked_pid) if self.tracked_pid else False
        if pid_alive:
            self.ever_saw_pid_alive = True

        now_mono = time.monotonic()
        cur_size = -1
        cur_event_ts = ""

        if self.harness == "codex":
            rollout_info = parse_codex_rollout_tail(log_path, 5) if (log_path and log_path.exists()) else None
            if rollout_info:
                if rollout_info.get("session_id"):
                    self.sid = str(rollout_info["session_id"])
                cur_size = int(rollout_info.get("file_size_bytes") or -1)
                cur_event_ts = str(rollout_info.get("last_event_ts") or "")
                last_msg = rollout_info.get("last_assistant_message")
                msg_epoch = parse_iso_epoch(last_msg.get("timestamp")) if last_msg else 0.0
                evt_epoch = parse_iso_epoch(cur_event_ts)

                if rollout_info.get("has_aborted") and not rollout_info.get("has_task_complete") and evt_epoch >= self.effective_since:
                    self.terminal = {
                        "reason": "BLOCKED_OR_FAILED",
                        "exit_code": 1,
                        "proc_exit_code": None,
                        "sid": self.sid,
                        "log_path": log_path,
                        "last_msg": last_msg,
                        "diagnostic": {"rollout": rollout_info},
                    }
                    return self.terminal

                if rollout_info.get("has_task_complete") and max(msg_epoch, evt_epoch) >= self.effective_since:
                    if self.settle_sec > 0:
                        time.sleep(self.settle_sec)
                    refreshed_path = find_codex_rollout(self.workdir, self.sid or None) or log_path
                    refreshed_info = parse_codex_rollout_tail(refreshed_path, 5) if (refreshed_path and refreshed_path.exists()) else rollout_info
                    final_msg = refreshed_info.get("last_assistant_message") or last_msg
                    self.terminal = {
                        "reason": "COMPLETED",
                        "exit_code": 0,
                        "proc_exit_code": 0,
                        "sid": self.sid,
                        "log_path": refreshed_path,
                        "last_msg": final_msg,
                        "diagnostic": None,
                    }
                    return self.terminal

            if self.tracked_pid and self.ever_saw_pid_alive and not pid_alive:
                flush_wait = min(max(self.settle_sec, 0.5), 2.0)
                time.sleep(flush_wait)
                _, flush_path, _ = _discover_fresh_log("codex", self.workdir, self.sid, self.effective_since, latest_job=self.job_meta)
                flush_path = flush_path or log_path
                flush_info = parse_codex_rollout_tail(flush_path, 5) if (flush_path and flush_path.exists()) else None
                if not flush_info:
                    flush_info = _synthesize_codex_rollout_from_job(self.job_meta, self.sid, self.effective_since)
                    if flush_info and flush_info.get("rollout_path"):
                        flush_path = Path(flush_info["rollout_path"])
                if flush_info:
                    f_msg = flush_info.get("last_assistant_message")
                    f_msg_ep = parse_iso_epoch(f_msg.get("timestamp")) if f_msg else 0.0
                    f_evt_ep = parse_iso_epoch(flush_info.get("last_event_ts"))
                    if flush_info.get("has_task_complete") and max(f_msg_ep, f_evt_ep) >= self.effective_since:
                        self.terminal = {
                            "reason": "COMPLETED",
                            "exit_code": 0,
                            "proc_exit_code": 0,
                            "sid": self.sid,
                            "log_path": flush_path,
                            "last_msg": f_msg,
                            "diagnostic": None,
                        }
                        return self.terminal
                self.terminal = {
                    "reason": "PROCESS_EXITED_FAILED",
                    "exit_code": 1,
                    "proc_exit_code": None,
                    "sid": self.sid,
                    "log_path": flush_path,
                    "last_msg": None,
                    "diagnostic": {"pid": self.tracked_pid, "stdout_tail": _read_job_stdout_tail(self.job_meta)},
                }
                return self.terminal

        elif self.harness == "claude":
            jsonl_info = parse_cc_jsonl_tail(log_path, 5) if (log_path and log_path.exists()) else None
            if jsonl_info:
                cur_size = int(jsonl_info.get("file_size_bytes") or -1)
                cur_event_ts = str(jsonl_info.get("last_record_ts") or "")
            last_msg = jsonl_info.get("last_assistant_message") if jsonl_info else None
            msg_epoch = parse_iso_epoch(last_msg.get("timestamp")) if last_msg else 0.0
            rec_epoch = parse_iso_epoch(jsonl_info.get("last_record_ts")) if jsonl_info else 0.0
            agent_busy = bool(agent_entry and (agent_entry.get("status") == "busy" or agent_entry.get("state") == "running"))

            if agent_entry and agent_entry.get("state") in ("blocked", "failed"):
                self.terminal = {
                    "reason": "BLOCKED_OR_FAILED",
                    "exit_code": 1,
                    "proc_exit_code": None,
                    "sid": self.sid,
                    "log_path": log_path,
                    "last_msg": last_msg,
                    "diagnostic": {"agent_entry": agent_entry},
                }
                return self.terminal
            if jsonl_info and jsonl_info.get("has_stop_failure") and rec_epoch >= self.effective_since:
                self.terminal = {
                    "reason": "BLOCKED_OR_FAILED",
                    "exit_code": 1,
                    "proc_exit_code": None,
                    "sid": self.sid,
                    "log_path": log_path,
                    "last_msg": last_msg,
                    "diagnostic": {"jsonl": jsonl_info},
                }
                return self.terminal

            if (
                jsonl_info
                and jsonl_info.get("last_stop_reason") == "end_turn"
                and last_msg
                and max(msg_epoch, rec_epoch) >= self.effective_since
                and not agent_busy
                and not pid_alive
            ):
                if self.settle_sec > 0:
                    time.sleep(self.settle_sec)
                re_agents = query_claude_agents(self.workdir)
                re_entry, _ = select_repo_agent(re_agents, self.sid or None)
                re_busy = bool(re_entry and (re_entry.get("status") == "busy" or re_entry.get("state") == "running"))
                re_path = find_cc_jsonl(self.workdir, self.sid or None) or log_path
                re_info = parse_cc_jsonl_tail(re_path, 5) if (re_path and re_path.exists()) else jsonl_info
                if not re_busy and re_info.get("last_stop_reason") != "tool_use":
                    final_msg = re_info.get("last_assistant_message") or last_msg
                    self.terminal = {
                        "reason": "COMPLETED",
                        "exit_code": 0,
                        "proc_exit_code": 0,
                        "sid": self.sid,
                        "log_path": re_path,
                        "last_msg": final_msg,
                        "diagnostic": None,
                    }
                    return self.terminal

            if self.tracked_pid and self.ever_saw_pid_alive and not pid_alive:
                flush_wait = min(max(self.settle_sec, 0.5), 2.0)
                time.sleep(flush_wait)
                f_sid, flush_path, _ = _discover_fresh_log("claude", self.workdir, self.sid, self.effective_since, latest_job=self.job_meta, bg_id=self.bg_id)
                if f_sid:
                    self.sid = f_sid
                flush_path = flush_path or log_path
                flush_info = parse_cc_jsonl_tail(flush_path, 5) if (flush_path and flush_path.exists()) else None
                f_msg = flush_info.get("last_assistant_message") if flush_info else None
                f_msg_ep = parse_iso_epoch(f_msg.get("timestamp")) if f_msg else 0.0
                f_rec_ep = parse_iso_epoch(flush_info.get("last_record_ts")) if flush_info else 0.0
                if flush_info and f_msg and max(f_msg_ep, f_rec_ep) >= self.effective_since and not flush_info.get("has_stop_failure"):
                    self.terminal = {
                        "reason": "COMPLETED",
                        "exit_code": 0,
                        "proc_exit_code": 0,
                        "sid": self.sid,
                        "log_path": flush_path,
                        "last_msg": f_msg,
                        "diagnostic": None,
                    }
                    return self.terminal
                self.terminal = {
                    "reason": "PROCESS_EXITED_FAILED",
                    "exit_code": 1,
                    "proc_exit_code": None,
                    "sid": self.sid,
                    "log_path": flush_path,
                    "last_msg": None,
                    "diagnostic": {"pid": self.tracked_pid, "jsonl": flush_info, "stdout_tail": _read_job_stdout_tail(self.job_meta)},
                }
                return self.terminal

        else:  # agy or generic script
            if log_path and log_path.exists():
                try:
                    st = log_path.stat()
                    cur_size = int(st.st_size)
                    cur_event_ts = str(st.st_mtime)
                except OSError:
                    pass
            if self.tracked_pid and self.ever_saw_pid_alive and not pid_alive:
                self.terminal = {
                    "reason": "PROCESS_EXITED_FAILED",
                    "exit_code": 1,
                    "proc_exit_code": None,
                    "sid": self.sid,
                    "log_path": log_path,
                    "last_msg": None,
                    "diagnostic": {"pid": self.tracked_pid, "stdout_tail": _read_job_stdout_tail(self.job_meta)},
                }
                return self.terminal

        # Activity trackers
        activity_detected = False
        if cur_size != self.last_size or cur_event_ts != self.last_event_ts:
            self.last_size = cur_size
            self.last_event_ts = cur_event_ts
            activity_detected = True
        if now_mono - self.last_git_check_mono >= 1.0:
            self.last_git_check_mono = now_mono
            cur_git_sig = (git_head_short(self.workdir), tuple(git_status_short(self.workdir)))
            if cur_git_sig != self.last_git_sig:
                self.last_git_sig = cur_git_sig
                activity_detected = True
        if self.tracked_pid and pid_alive and _has_active_child_process(self.tracked_pid):
            activity_detected = True
        if activity_detected:
            self.last_activity_mono = now_mono

        if self.stall_timeout > 0 and (now_mono - self.last_activity_mono) >= int(self.stall_timeout):
            self.terminal = {
                "reason": "STALLED",
                "exit_code": 2,
                "proc_exit_code": None,
                "sid": self.sid,
                "log_path": log_path,
                "last_msg": None,
                "diagnostic": {
                    "idle_sec": round(now_mono - self.last_activity_mono, 1),
                    "pid": self.tracked_pid,
                    "pid_alive": pid_alive,
                    "last_event_ts": self.last_event_ts,
                },
            }
            return self.terminal
        return None


def _build_tracker_from_job(repo: Path, manifest: dict[str, Any], job: dict[str, Any], args: argparse.Namespace) -> _WatchTarget:
    harness = str(job.get("harness") or "")
    workdir = Path(str(job.get("workdir") or repo)).resolve()
    sid = str(job.get("session_id") or "")
    bg_id = str(job.get("bg_id") or "")
    tracked_pid = int(job.get("pid") or 0)
    started_ep = parse_iso_epoch(str(job.get("started_at") or ""))
    if args.since_epoch > 0:
        effective_since = float(args.since_epoch)
    elif started_ep > 0:
        effective_since = started_ep - 2.0
    else:
        effective_since = time.time() - 1.0
    return _WatchTarget(
        repo=repo,
        workdir=workdir,
        harness=harness,
        sid=sid,
        bg_id=bg_id,
        tracked_pid=tracked_pid,
        effective_since=effective_since,
        job_meta=job,
        settle_sec=max(float(args.settle_seconds), 0.0),
        stall_timeout=int(args.stall_timeout),
    )


def _select_wave_jobs(manifest: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    jobs = [j for j in (manifest.get("jobs") or []) if isinstance(j, dict)]
    jobs_arg = (getattr(args, "jobs", "") or "").strip()
    wave_arg = (getattr(args, "wave", "") or "").strip()
    all_active = bool(getattr(args, "all_active", False))

    if jobs_arg:
        wanted = [x.strip() for x in jobs_arg.split(",") if x.strip()]
        selected: list[dict[str, Any]] = []
        for jid in wanted:
            found = next((j for j in jobs if j.get("job_id") == jid), None)
            if not found:
                raise ValueError(f"Job not found: {jid}")
            selected.append(found)
        return selected

    if wave_arg:
        matched = [j for j in jobs if str(j.get("wave") or "") == wave_arg]
        if not matched:
            raise ValueError(f"No jobs found for --wave {wave_arg!r}")
        return matched

    if all_active:
        terminal_set = {"COMPLETED", "BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED", "STALLED", "STOPPED"}
        return [j for j in jobs if _summarize_job_status(j) not in terminal_set]

    return []


def cmd_watch(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir, manifest = _try_load_run(repo, args.run_id)
    if manifest:
        ensure_manifest_v2(manifest)

    job_id_arg = (getattr(args, "job_id", "") or "").strip()
    jobs_arg = (getattr(args, "jobs", "") or "").strip()
    wave_arg = (getattr(args, "wave", "") or "").strip()
    all_active = bool(getattr(args, "all_active", False))
    is_wave_mode = bool(jobs_arg or wave_arg or all_active)

    poll_interval = max(float(args.interval), 0.2)
    settle_sec = max(float(args.settle_seconds), 0.0)
    overall_deadline = time.monotonic() + max(int(args.timeout), 1)
    base = manifest.get("base_commit") or "" if manifest else ""

    def _repo_commits(target_dir: Path) -> list[str]:
        if base and base != "NO_GIT":
            rc, out_c, _ = run_git(target_dir, ["log", f"{base}..HEAD", "--oneline"])
            if rc == 0 and out_c:
                return out_c.splitlines()
        return []

    if is_wave_mode:
        if not manifest:
            raise FileNotFoundError(f"No active run manifest under {repo / LOOP_DIR_NAME}")
        wave_jobs = _select_wave_jobs(manifest, args)
        if not wave_jobs:
            out = {
                "ok": True,
                "reason": "NO_ACTIVE_JOBS",
                "wave": wave_arg or "-",
                "total": 0,
                "completed_count": 0,
                "failed_count": 0,
                "stalled_count": 0,
                "timeout_count": 0,
                "jobs": [],
                "dialogue_md": str(run_dir / DIALOGUE_FILE) if run_dir else "",
            }
            print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else "[watch:NO_ACTIVE_JOBS]")
            return 0

        trackers = [_build_tracker_from_job(repo, manifest, j, args) for j in wave_jobs]
        wave_start = time.monotonic()
        results_by_id: dict[str, dict[str, Any]] = {}

        while True:
            all_done = True
            for tr in trackers:
                if tr.job_id in results_by_id:
                    continue
                res = tr.poll_once()
                if res is None:
                    all_done = False
                    continue

                elapsed_j = round(time.monotonic() - tr.start_mono, 2)
                commits_j = _repo_commits(tr.workdir)
                _update_job_terminal_state(
                    repo,
                    run_dir,
                    tr.job_id,
                    reason=res["reason"],
                    sid=res["sid"],
                    log_path=res["log_path"],
                    exit_code=res.get("proc_exit_code"),
                )
                synced_j = False
                if args.sync_ledger and res["reason"] == "COMPLETED" and run_dir is not None:
                    synced_j = _sync_watch_delivery(
                        repo,
                        run_dir,
                        tr.harness,
                        sid=res["sid"],
                        model=tr.model,
                        log_path=res["log_path"],
                        last_msg=res["last_msg"],
                        commits_since_base=commits_j,
                        workdir=tr.workdir,
                        job_id=tr.job_id,
                    )
                results_by_id[tr.job_id] = {
                    "job_id": tr.job_id,
                    "harness": tr.harness,
                    "workdir": str(tr.workdir),
                    "reason": res["reason"],
                    "exit_code": res["exit_code"],
                    "session_id": res["sid"],
                    "elapsed_sec": elapsed_j,
                    "synced_to_ledger": synced_j,
                    "commits_since_base": commits_j,
                    "last_assistant_message": res["last_msg"],
                    "diagnostic": res["diagnostic"],
                }
                print(
                    f"[wave-watch] {tr.job_id} ({tr.harness}) -> {res['reason']} ({elapsed_j}s)",
                    file=sys.stderr,
                )
                if getattr(args, "fail_fast", False) and res["reason"] != "COMPLETED":
                    all_done = True
                    break

            if all_done or len(results_by_id) >= len(trackers):
                break
            if time.monotonic() >= overall_deadline:
                break
            time.sleep(poll_interval)

        for tr in trackers:
            if tr.job_id not in results_by_id:
                status_left = "TIMEOUT" if time.monotonic() >= overall_deadline else "STILL_RUNNING"
                results_by_id[tr.job_id] = {
                    "job_id": tr.job_id,
                    "harness": tr.harness,
                    "workdir": str(tr.workdir),
                    "reason": status_left,
                    "exit_code": 124 if status_left == "TIMEOUT" else 1,
                    "session_id": tr.sid,
                    "elapsed_sec": round(time.monotonic() - tr.start_mono, 2),
                    "synced_to_ledger": False,
                    "commits_since_base": _repo_commits(tr.workdir),
                    "last_assistant_message": None,
                    "diagnostic": {"pid": tr.tracked_pid},
                }

        ordered = [results_by_id[tr.job_id] for tr in trackers]
        completed_cnt = sum(1 for r in ordered if r["reason"] == "COMPLETED")
        stalled_cnt = sum(1 for r in ordered if r["reason"] == "STALLED")
        timeout_cnt = sum(1 for r in ordered if r["reason"] == "TIMEOUT")
        failed_cnt = sum(1 for r in ordered if r["reason"] in ("BLOCKED_OR_FAILED", "PROCESS_EXITED_FAILED", "STILL_RUNNING"))

        if completed_cnt == len(ordered):
            overall_reason, exit_code = "WAVE_COMPLETED", 0
        elif failed_cnt > 0:
            overall_reason, exit_code = "WAVE_PARTIAL_FAILED", 1
        elif stalled_cnt > 0:
            overall_reason, exit_code = "STALLED", 2
        else:
            overall_reason, exit_code = "TIMEOUT", 124

        refreshed = load_manifest(run_dir) if run_dir else manifest
        sess = default_tmux_session_name(refreshed, repo)
        sock = (os.environ.get(TMUX_SOCKET_ENV) or "").strip()
        sock_flag = f"-L {sock} " if sock else ""
        summary_out = {
            "ok": overall_reason == "WAVE_COMPLETED",
            "reason": overall_reason,
            "wave": wave_arg or "-",
            "total": len(ordered),
            "completed_count": completed_cnt,
            "failed_count": failed_cnt,
            "stalled_count": stalled_cnt,
            "timeout_count": timeout_cnt,
            "elapsed_sec": round(time.monotonic() - wave_start, 2),
            "jobs": ordered,
            "tmux_session": sess,
            "tmux_attach_cmd": f"tmux {sock_flag}attach -t {sess}",
            "dialogue_md": str(run_dir / DIALOGUE_FILE) if run_dir else "",
        }
        if args.json:
            print(json.dumps(summary_out, ensure_ascii=False, indent=2))
        else:
            print(f"[watch:{overall_reason}] total={len(ordered)} ok={completed_cnt} failed={failed_cnt} stalled={stalled_cnt} timeout={timeout_cnt}")
        return exit_code

    # Single-job or legacy single-harness mode
    latest_job: dict[str, Any] | None = None
    if job_id_arg:
        if not manifest:
            raise FileNotFoundError(f"No run manifest found under {repo / LOOP_DIR_NAME}")
        latest_job = _find_job_in_manifest(manifest, job_id_arg)
        if not latest_job:
            raise ValueError(f"Job not found: {job_id_arg}")
        harness = args.harness or str(latest_job.get("harness") or "")
        workdir = Path(args.workdir).resolve() if getattr(args, "workdir", "") else Path(str(latest_job.get("workdir") or repo)).resolve()
        if args.since_epoch > 0:
            effective_since = float(args.since_epoch)
        else:
            st_ep = parse_iso_epoch(str(latest_job.get("started_at") or ""))
            effective_since = (st_ep - 2.0) if st_ep > 0 else (time.time() - 1.0)
        sid = args.session_id or str(latest_job.get("session_id") or "")
        bg_id = str(latest_job.get("bg_id") or "")
        tracked_pid = int(args.pid or (latest_job.get("pid") or 0))
    else:
        harness = (args.harness or "").strip()
        if not harness:
            raise ValueError("Specify --harness <codex|claude> or --job-id / --wave / --jobs / --all-active")
        workdir = Path(args.workdir).resolve() if getattr(args, "workdir", "") else repo
        if manifest:
            matching_jobs = [
                j for j in (manifest.get("jobs") or []) if isinstance(j, dict) and j.get("harness") == harness
            ]
            latest_job = matching_jobs[-1] if matching_jobs else None
        if args.since_epoch > 0:
            effective_since = float(args.since_epoch)
        elif manifest and manifest.get("last_dispatch_epoch") and manifest.get("phase") in ("CC_EXECUTING", "CODEX_EXECUTING"):
            effective_since = float(manifest["last_dispatch_epoch"])
        else:
            effective_since = time.time() - 1.0
        if args.session_id:
            sid = args.session_id
        elif args.since_epoch > 0:
            sid = ""
        elif manifest:
            sid = str(manifest.get("sessions", {}).get(harness, {}).get("session_id") or "")
        else:
            sid = ""
        bg_id = str(latest_job.get("bg_id") or "") if latest_job else ""
        tracked_pid = int(args.pid or (latest_job.get("pid") if latest_job and latest_job.get("pid") else 0))

    tracker = _WatchTarget(
        repo=repo,
        workdir=workdir,
        harness=harness,
        sid=sid,
        bg_id=bg_id,
        tracked_pid=tracked_pid,
        effective_since=effective_since,
        job_meta=latest_job,
        settle_sec=settle_sec,
        stall_timeout=int(args.stall_timeout),
    )

    while True:
        res = tracker.poll_once()
        if res is not None:
            commits_since_base = _repo_commits(workdir)
            if latest_job and latest_job.get("job_id"):
                _update_job_terminal_state(
                    repo,
                    run_dir,
                    str(latest_job["job_id"]),
                    reason=res["reason"],
                    sid=res["sid"],
                    log_path=res["log_path"],
                    exit_code=res.get("proc_exit_code"),
                )
            model = (
                str(latest_job.get("model") or "")
                if latest_job
                else ""
            ) or (
                str(manifest.get("sessions", {}).get(harness, {}).get("model") or "")
                or str((manifest.get("roster") or {}).get("executor", {}).get("model") or "")
                if manifest else ""
            )
            synced = False
            if args.sync_ledger and res["reason"] == "COMPLETED" and run_dir is not None:
                synced = _sync_watch_delivery(
                    repo,
                    run_dir,
                    harness,
                    sid=res["sid"],
                    model=model,
                    log_path=res["log_path"],
                    last_msg=res["last_msg"],
                    commits_since_base=commits_since_base,
                    workdir=workdir,
                    job_id=tracker.job_id,
                )
            return _emit_watch_result(
                reason=res["reason"],
                exit_code=res["exit_code"],
                harness=harness,
                repo=repo,
                run_dir=run_dir,
                manifest=manifest,
                sid=res["sid"],
                elapsed_sec=time.monotonic() - tracker.start_mono,
                synced=synced,
                commits_since_base=commits_since_base,
                last_msg=res["last_msg"],
                diagnostic=res["diagnostic"],
                as_json=bool(args.json),
                workdir=workdir,
                job_id=tracker.job_id,
            )

        if time.monotonic() >= overall_deadline:
            commits_since_base = _repo_commits(workdir)
            return _emit_watch_result(
                reason="TIMEOUT",
                exit_code=124,
                harness=harness,
                repo=repo,
                run_dir=run_dir,
                manifest=manifest,
                sid=tracker.sid,
                elapsed_sec=time.monotonic() - tracker.start_mono,
                synced=False,
                commits_since_base=commits_since_base,
                last_msg=None,
                diagnostic={
                    "pid": tracker.tracked_pid,
                    "pid_alive": is_pid_alive(tracker.tracked_pid) if tracker.tracked_pid else False,
                    "last_event_ts": tracker.last_event_ts,
                },
                as_json=bool(args.json),
                workdir=workdir,
                job_id=tracker.job_id,
            )
        time.sleep(poll_interval)


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    events = load_events(run_dir)
    out = {
        "run_id": manifest.get("run_id"),
        "slug": manifest.get("slug"),
        "complexity": manifest.get("complexity"),
        "phase": manifest.get("phase"),
        "verdict": manifest.get("verdict"),
        "round": manifest.get("round"),
        "max_rounds": manifest.get("max_rounds", DEFAULT_MAX_ROUNDS),
        "terminal_reason": manifest.get("terminal_reason", ""),
        "base_commit": manifest.get("base_commit"),
        "current_head": git_head_short(repo),
        "tmux_session": default_tmux_session_name(manifest, repo),
        "roster": manifest.get("roster"),
        "sessions": manifest.get("sessions"),
        "jobs": manifest.get("jobs"),
        "agy_conversation_id": manifest.get("agy_conversation_id"),
        "cc_session_id": manifest.get("cc_session_id"),
        "cc_commands": format_cc_commands(manifest),
        "harness_commands": format_all_harness_commands(manifest),
        "dialogue_md": str(run_dir / DIALOGUE_FILE),
        "latest_md": str(repo / LOOP_DIR_NAME / LATEST_MD_FILE),
        "event_count": len(events),
        "last_event": events[-1] if events else None,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


AGENTS_SNIPPET = f"""## 多 Harness 协作角色（默认口径，均调用 `{SKILL_NAME}`）

- **Antigravity（`agy`）**：默认任 **Architect + Post-Verifier + 体验官**——负责项目审查、撰写 PRD/Tech-Spec/Test-Plan/Handoff 规格包，用 `loopctl.py delegate`（默认跑进 `tmux` 总览看板与分窗）派活并挂 `loopctl.py watch` 零 Token 守候，完工后独立四维终审、实机体验并向用户结项。
- **Claude Code（`claude`）**：默认任 **Executor**（复杂需求默认首行 `ultracode`）——严格按规格包 CUI-First 分阶段施工与提交，遇口径疑问可 `loopctl.py consult` 取经，收口前自跑 `audit_commits.py` 并回写 `.agent-loop/LATEST.md`。
- **Codex（`codex`，如 `GPT-6.2-Sol`）**：按用户指定任 **Executor / 审校顾问**——承担独立模块实现或合并前两级对抗性代码审校（可用 `--reasoning-effort high|xhigh` 分级、默认最多 2 轮），保持原生会话可追溯（`codex resume`），进度与交付同步至 `.agent-loop/LATEST.md`。
"""


def cmd_snippet(args: argparse.Namespace) -> int:
    """Print or idempotently append the 3-line multi-harness AGENTS.md snippet."""
    append_target = (args.append or "").strip()
    if not append_target:
        if args.json:
            print(json.dumps({"ok": True, "snippet": AGENTS_SNIPPET}, ensure_ascii=False, indent=2))
        else:
            print(AGENTS_SNIPPET.rstrip())
        return 0

    target = Path(append_target).expanduser().resolve()
    agents_path = (target / "AGENTS.md") if target.is_dir() else target
    if not agents_path.parent.exists():
        raise FileNotFoundError(f"Target directory not found: {agents_path.parent}")

    if agents_path.exists():
        existing = agents_path.read_text(encoding="utf-8", errors="replace")
        if "## 多 Harness 协作角色" in existing:
            status = "already_present"
        else:
            sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            agents_path.write_text(existing + sep + AGENTS_SNIPPET, encoding="utf-8")
            status = "appended"
    else:
        agents_path.write_text("# AGENTS.md\n\n" + AGENTS_SNIPPET, encoding="utf-8")
        status = "created"

    out = {"ok": True, "status": status, "path": str(agents_path)}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"[{status}] {agents_path}")
    return 0


def _probe_bin_version(bin_path: str | None, flag: str = "--version") -> str:
    """Best-effort short version line for doctor checks."""
    if not bin_path:
        return ""
    try:
        r = subprocess.run([bin_path, flag], capture_output=True, text=True, timeout=4, check=False)
        raw = (r.stdout or "").strip() or (r.stderr or "").strip()
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("(node:") and not line.startswith("(Use "):
                return line[:120]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def cmd_doctor(args: argparse.Namespace) -> int:
    """Read-only environment, binary, symlink, and brain-root self-check."""
    skill_root = Path(__file__).resolve().parent.parent
    canonical_real = os.path.realpath(str(skill_root))

    bin_checks = {
        "agy": agy_bin(),
        "claude": claude_bin(),
        "codex": codex_bin(),
        "tmux": tmux_bin(),
        "git": shutil.which("git"),
        "pgrep": shutil.which("pgrep"),
    }
    binaries: dict[str, dict[str, Any]] = {}
    for name, path in bin_checks.items():
        if not path or name == "pgrep":
            version = ""
        elif name == "tmux":
            version = _probe_bin_version(path, "-V")
        else:
            version = _probe_bin_version(path, "--version")
        binaries[name] = {
            "installed": bool(path),
            "path": path or "",
            "version": version,
        }

    mount_paths = {
        "canonical": Path.home() / ".agents" / "skills" / SKILL_NAME,
        "claude": Path.home() / ".claude" / "skills" / SKILL_NAME,
        "codex": Path.home() / ".codex" / "skills" / SKILL_NAME,
        "antigravity": Path.home() / ".gemini" / "config" / "skills" / SKILL_NAME,
    }
    skill_mounts: dict[str, dict[str, Any]] = {}
    for label, mp in mount_paths.items():
        exists = mp.exists()
        real = os.path.realpath(str(mp)) if exists else ""
        skill_mounts[label] = {
            "path": str(mp),
            "exists": exists,
            "is_symlink": mp.is_symlink(),
            "realpath": real,
            "matches_canonical": bool(exists and real == canonical_real),
        }

    report = {
        "ok": True,
        "python_version": sys.version.split()[0],
        "skill_root": str(skill_root),
        "binaries": binaries,
        "skill_mounts": skill_mounts,
        "agy_brain_roots": {
            "desktop": {"path": str(AGY_BRAIN_ROOT), "exists": AGY_BRAIN_ROOT.exists()},
            "cli": {"path": str(AGY_CLI_BRAIN_ROOT), "exists": AGY_CLI_BRAIN_ROOT.exists()},
        },
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"=== {SKILL_NAME} doctor ===")
    print(f"Python: {report['python_version']}")
    print(f"Skill Root: {report['skill_root']}")
    print("\n[Binaries]")
    for name, info in binaries.items():
        if info["installed"]:
            ver = f" ({info['version']})" if info["version"] else ""
            print(f"  OK  {name}: {info['path']}{ver}")
        else:
            print(f"  --  {name}: not found")
    print("\n[Skill Mounts]")
    for label, info in skill_mounts.items():
        mark = "OK" if info["matches_canonical"] else ("WARN" if info["exists"] else "--")
        print(f"  {mark:4s} {label}: {info['path']} -> {info['realpath'] or 'missing'}")
    print("\n[AGY Brain Roots]")
    for k, info in report["agy_brain_roots"].items():
        print(f"  {'OK' if info['exists'] else '--':4s} {k}: {info['path']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Multi-harness traceable loop controller (Antigravity, Claude Code, Codex & extensible harnesses)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_ultracode_args(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group()
        group.add_argument("--ultracode", dest="ultracode", action="store_true", default=None)
        group.add_argument("--no-ultracode", dest="ultracode", action="store_false")

    def _add_tmux_args(p: argparse.ArgumentParser) -> None:
        tgroup = p.add_mutually_exclusive_group()
        tgroup.add_argument("--tmux", dest="tmux", action="store_true", default=None)
        tgroup.add_argument("--no-tmux", dest="tmux", action="store_false")
        p.add_argument("--tmux-session", default="")

    p_init = sub.add_parser("init", help="Initialize a new traceable loop run")
    p_init.add_argument("--repo", default=".")
    p_init.add_argument("--slug", required=True)
    p_init.add_argument("--title", default="")
    p_init.add_argument("--complexity", choices=("complex", "simple"), default="complex")
    p_init.add_argument("--run-id", default="")
    p_init.add_argument("--agy-id", default="")
    p_init.add_argument("--base-commit", default="")
    p_init.add_argument("--docs", default="")
    p_init.add_argument("--architect", default="agy")
    p_init.add_argument("--executor", default="claude")
    p_init.add_argument("--verifier", default="agy")
    p_init.add_argument("--advisor", default="")
    p_init.add_argument("--track", action="append", default=None, help="Bind named track (<name>=<harness>[:<model>])")
    p_init.add_argument("--tmux-session", default="")
    p_init.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    p_init.add_argument("--json", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_roster = sub.add_parser("set-roster", help="Update dynamic role/track/harness/model assignments")
    p_roster.add_argument("--repo", default=".")
    p_roster.add_argument("--run-id", default=None)
    p_roster.add_argument("--role", choices=ROLES_WITH_USER, default="user")
    p_roster.add_argument("--architect", default="")
    p_roster.add_argument("--executor", default="")
    p_roster.add_argument("--verifier", default="")
    p_roster.add_argument("--advisor", default="")
    p_roster.add_argument("--track", action="append", default=None, help="Update named track (<name>=<harness>[:<model>])")
    p_roster.add_argument("--json", action="store_true")
    p_roster.set_defaults(func=cmd_set_roster)

    p_bind = sub.add_parser("bind-cc", help="Bind a Claude Code session to the current run")
    p_bind.add_argument("--repo", default=".")
    p_bind.add_argument("--run-id", default=None)
    p_bind.add_argument("--session-id", default="")
    p_bind.add_argument("--bg-id", default="")
    p_bind.add_argument("--name", default="")
    p_bind.add_argument("--kind", default="")
    p_bind.add_argument("--json", action="store_true")
    p_bind.set_defaults(func=cmd_bind_cc)

    p_bsess = sub.add_parser("bind-session", help="Bind any harness session (agy/claude/codex) to the run")
    p_bsess.add_argument("--repo", default=".")
    p_bsess.add_argument("--run-id", default=None)
    p_bsess.add_argument("--harness", choices=HARNESSES, required=True)
    p_bsess.add_argument("--session-id", default="")
    p_bsess.add_argument("--model", default="")
    p_bsess.add_argument("--bg-id", default="")
    p_bsess.add_argument("--name", default="")
    p_bsess.add_argument("--kind", default="")
    p_bsess.add_argument("--json", action="store_true")
    p_bsess.set_defaults(func=cmd_bind_session)

    p_rec = sub.add_parser("record", help="Record an event into the shared dialogue ledger")
    p_rec.add_argument("--repo", default=".")
    p_rec.add_argument("--run-id", default=None)
    p_rec.add_argument("--role", choices=ROLES_WITH_USER, required=True)
    p_rec.add_argument("--event", choices=VALID_EVENTS, required=True)
    p_rec.add_argument("--phase", default=None)
    p_rec.add_argument("--verdict", choices=("PENDING", "PASS", "NEEDS_FIX"), default=None)
    p_rec.add_argument("--summary", required=True)
    p_rec.add_argument("--body", default="")
    p_rec.add_argument("--body-file", default="")
    p_rec.add_argument("--commits", default="")
    p_rec.add_argument("--skip-ux", action="store_true")
    p_rec.add_argument("--force", action="store_true")
    p_rec.add_argument("--json", action="store_true")
    p_rec.set_defaults(func=cmd_record)

    p_disp = sub.add_parser("dispatch-cc", help="Launch a Claude Code background session cleanly")
    p_disp.add_argument("--repo", default=".")
    p_disp.add_argument("--run-id", default=None)
    p_disp.add_argument("--prompt", default="")
    p_disp.add_argument("--prompt-file", default="")
    p_disp.add_argument("--name", default="")
    _add_ultracode_args(p_disp)
    p_disp.add_argument("--force", action="store_true")
    p_disp.add_argument("--dry-run", action="store_true")
    p_disp.add_argument("--json", action="store_true")
    p_disp.set_defaults(func=cmd_dispatch_cc)

    p_send = sub.add_parser("send-cc", help="Send rework/follow-up instructions to a Claude Code session")
    p_send.add_argument("--repo", default=".")
    p_send.add_argument("--run-id", default=None)
    p_send.add_argument("--session-id", default="")
    p_send.add_argument("--prompt", default="")
    p_send.add_argument("--prompt-file", default="")
    p_send.add_argument("--event", choices=("rework_to_cc", "handoff_to_cc"), default="rework_to_cc")
    p_send.add_argument("--summary", default="")
    p_send.add_argument("--cmux-target", default="")
    _add_ultracode_args(p_send)
    p_send.add_argument("--force", action="store_true")
    p_send.add_argument("--dry-run", action="store_true")
    p_send.add_argument("--json", action="store_true")
    p_send.set_defaults(func=cmd_send_cc)

    p_cons = sub.add_parser("consult", help="Cross-harness read-only consultation/review")
    p_cons.add_argument("--repo", default=".")
    p_cons.add_argument("--workdir", default="")
    p_cons.add_argument("--run-id", default=None)
    p_cons.add_argument("--from", dest="from_role", choices=ROLES_WITH_USER, required=True)
    p_cons.add_argument("--to", choices=HARNESSES, required=True)
    p_cons.add_argument("--model", default=None)
    p_cons.add_argument(
        "--reasoning-effort",
        choices=CODEX_REASONING_EFFORTS,
        default="",
        help="Codex reasoning effort override (-c model_reasoning_effort=<level>)",
    )
    p_cons.add_argument("--session-id", default="")
    p_cons.add_argument("--label", default="")
    p_cons.add_argument("--prompt", default="")
    p_cons.add_argument("--prompt-file", default="")
    p_cons.add_argument("--summary", default="")
    p_cons.add_argument("--timeout", type=int, default=300)
    _add_tmux_args(p_cons)
    p_cons.add_argument("--allow-nested", action="store_true")
    p_cons.add_argument("--dry-run", action="store_true")
    p_cons.add_argument("--json", action="store_true")
    p_cons.set_defaults(func=cmd_consult)

    p_del = sub.add_parser("delegate", help="Cross-harness write/execution delegation")
    p_del.add_argument("--repo", default=".")
    p_del.add_argument("--workdir", default="")
    p_del.add_argument("--run-id", default=None)
    p_del.add_argument("--from", dest="from_role", choices=ROLES_WITH_USER, required=True)
    p_del.add_argument("--to", choices=HARNESSES, required=False, default="")
    p_del.add_argument("--role-name", default="executor")
    p_del.add_argument("--track", default="")
    p_del.add_argument("--wave", default="")
    p_del.add_argument("--label", default="")
    p_del.add_argument("--model", default=None)
    p_del.add_argument(
        "--reasoning-effort",
        choices=CODEX_REASONING_EFFORTS,
        default="",
        help="Codex reasoning effort override (-c model_reasoning_effort=<level>)",
    )
    p_del.add_argument("--session-id", default="")
    p_del.add_argument("--name", default="")
    p_del.add_argument("--prompt", default="")
    p_del.add_argument("--prompt-file", default="")
    p_del.add_argument("--summary", default="")
    _add_ultracode_args(p_del)
    _add_tmux_args(p_del)
    mode_grp = p_del.add_mutually_exclusive_group()
    mode_grp.add_argument("--background", dest="sync", action="store_false", default=False)
    mode_grp.add_argument("--sync", dest="sync", action="store_true")
    p_del.add_argument("--sandbox", choices=("workspace-write", "danger-full-access"), default="workspace-write")
    p_del.add_argument("--timeout", type=int, default=600)
    p_del.add_argument("--rework", action="store_true")
    p_del.add_argument("--force", action="store_true")
    p_del.add_argument("--allow-nested", action="store_true")
    p_del.add_argument("--dry-run", action="store_true")
    p_del.add_argument("--json", action="store_true")
    p_del.set_defaults(func=cmd_delegate)

    p_icc = sub.add_parser("inspect-cc", help="Inspect Claude Code session progress (read-only by default)")
    p_icc.add_argument("--repo", default=".")
    p_icc.add_argument("--workdir", default="")
    p_icc.add_argument("--job-id", default="")
    p_icc.add_argument("--run-id", default=None)
    p_icc.add_argument("--session-id", default="")
    p_icc.add_argument("--tail-turns", type=int, default=5)
    p_icc.add_argument("--sync-ledger", action="store_true")
    p_icc.add_argument("--json", action="store_true")
    p_icc.set_defaults(func=cmd_inspect_cc)

    p_icodex = sub.add_parser("inspect-codex", help="Inspect Codex session/rollout progress (read-only by default)")
    p_icodex.add_argument("--repo", default=".")
    p_icodex.add_argument("--workdir", default="")
    p_icodex.add_argument("--job-id", default="")
    p_icodex.add_argument("--run-id", default=None)
    p_icodex.add_argument("--session-id", default="")
    p_icodex.add_argument("--tail-turns", type=int, default=5)
    p_icodex.add_argument("--sync-ledger", action="store_true")
    p_icodex.add_argument("--json", action="store_true")
    p_icodex.set_defaults(func=cmd_inspect_codex)

    p_iagy = sub.add_parser("inspect-agy", help="Inspect Antigravity conversation transcript (read-only)")
    p_iagy.add_argument("--repo", default=None)
    p_iagy.add_argument("--run-id", default=None)
    p_iagy.add_argument("--agy-id", default="")
    p_iagy.add_argument("--last", type=int, default=6)
    p_iagy.add_argument("--json", action="store_true")
    p_iagy.set_defaults(func=cmd_inspect_agy)

    p_insp = sub.add_parser("inspect", help="Unified inspect router for agy, claude, or codex")
    p_insp.add_argument("--harness", choices=HARNESSES, required=True)
    p_insp.add_argument("--repo", default=".")
    p_insp.add_argument("--workdir", default="")
    p_insp.add_argument("--job-id", default="")
    p_insp.add_argument("--run-id", default=None)
    p_insp.add_argument("--session-id", default="")
    p_insp.add_argument("--agy-id", default="")
    p_insp.add_argument("--tail-turns", type=int, default=5)
    p_insp.add_argument("--last", type=int, default=None)
    p_insp.add_argument("--sync-ledger", action="store_true")
    p_insp.add_argument("--json", action="store_true")
    p_insp.set_defaults(func=cmd_inspect)

    p_view = sub.add_parser("view", help="Print readable Claude/Codex/AGY conversations and terminal commands")
    p_view.add_argument("--repo", default=None)
    p_view.add_argument("--run-id", default=None)
    p_view.add_argument("--agy-id", default="")
    p_view.add_argument("--claude-id", default="")
    p_view.add_argument("--codex-id", default="")
    p_view.add_argument("--job-id", default="")
    p_view.add_argument("--target", choices=("all", "dialogue", "claude", "codex", "agy"), default="all")
    p_view.add_argument("--last", type=int, default=10)
    p_view.add_argument("--full", action="store_true")
    p_view.set_defaults(func=cmd_view)

    p_watch = sub.add_parser("watch", help="Zero-token local monitor waiting for single job or parallel wave completion")
    p_watch.add_argument("--repo", default=".")
    p_watch.add_argument("--workdir", default="")
    p_watch.add_argument("--run-id", default=None)
    p_watch.add_argument("--harness", choices=HARNESSES, required=False, default="")
    p_watch.add_argument("--job-id", default="")
    p_watch.add_argument("--jobs", default="")
    p_watch.add_argument("--wave", default="")
    p_watch.add_argument("--all-active", action="store_true")
    p_watch.add_argument("--fail-fast", action="store_true")
    p_watch.add_argument("--session-id", default="")
    p_watch.add_argument("--pid", type=int, default=0)
    p_watch.add_argument("--since-epoch", type=float, default=0.0)
    p_watch.add_argument("--interval", type=float, default=2.0)
    p_watch.add_argument("--settle-seconds", type=float, default=1.5)
    p_watch.add_argument("--stall-timeout", type=int, default=600)
    p_watch.add_argument("--timeout", type=int, default=7200)
    sync_grp = p_watch.add_mutually_exclusive_group()
    sync_grp.add_argument("--sync-ledger", dest="sync_ledger", action="store_true", default=True)
    sync_grp.add_argument("--no-sync-ledger", dest="sync_ledger", action="store_false")
    p_watch.add_argument("--json", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    p_tmux = sub.add_parser("tmux", help="Inspect, attach, tail, or stop tmux dashboard and worker windows")
    tmux_sub = p_tmux.add_subparsers(dest="tmux_cmd", required=True)

    p_ts = tmux_sub.add_parser("status", help="Show live tmux window and job status table")
    p_ts.add_argument("--repo", default=".")
    p_ts.add_argument("--run-id", default=None)
    p_ts.add_argument("--json", action="store_true")
    p_ts.set_defaults(func=cmd_tmux_status)

    p_td = tmux_sub.add_parser("dashboard", help="Auto-refreshing live overview dashboard for tmux Window 0")
    p_td.add_argument("--repo", default=".")
    p_td.add_argument("--run-id", default=None)
    p_td.add_argument("--interval", type=float, default=3.0)
    p_td.set_defaults(func=cmd_tmux_dashboard)

    p_ta = tmux_sub.add_parser("attach", help="Attach or switch into the run's tmux session")
    p_ta.add_argument("--repo", default=".")
    p_ta.add_argument("--run-id", default=None)
    p_ta.add_argument("--job-id", default="")
    p_ta.add_argument("--print-only", action="store_true")
    p_ta.add_argument("--json", action="store_true")
    p_ta.set_defaults(func=cmd_tmux_attach)

    p_tt = tmux_sub.add_parser("tail", help="Capture recent output lines from a tmux worker pane without attaching")
    p_tt.add_argument("--repo", default=".")
    p_tt.add_argument("--run-id", default=None)
    p_tt.add_argument("--job-id", default="")
    p_tt.add_argument("--lines", type=int, default=40)
    p_tt.add_argument("--json", action="store_true")
    p_tt.set_defaults(func=cmd_tmux_tail)

    p_tk = tmux_sub.add_parser("stop", help="Stop one job window or the whole run tmux session")
    p_tk.add_argument("--repo", default=".")
    p_tk.add_argument("--run-id", default=None)
    p_tk.add_argument("--job-id", default="")
    p_tk.add_argument("--all", action="store_true")
    p_tk.add_argument("--json", action="store_true")
    p_tk.set_defaults(func=cmd_tmux_stop)

    p_rjob = sub.add_parser("_run-job", help=argparse.SUPPRESS)
    p_rjob.add_argument("--spec", required=True)
    p_rjob.set_defaults(func=cmd__run_job)

    p_stat = sub.add_parser("status", help="Show current loop run status and traceable links")
    p_stat.add_argument("--repo", default=".")
    p_stat.add_argument("--run-id", default=None)
    p_stat.add_argument("--json", action="store_true")
    p_stat.set_defaults(func=cmd_status)

    p_snip = sub.add_parser("snippet", help="Print or append the 3-line AGENTS.md role snippet")
    p_snip.add_argument("--append", default="")
    p_snip.add_argument("--json", action="store_true")
    p_snip.set_defaults(func=cmd_snippet)

    p_doc = sub.add_parser("doctor", help="Check CLI binaries, tmux, skill symlinks, and AGY brain paths")
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
