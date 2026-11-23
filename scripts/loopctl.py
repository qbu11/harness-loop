#!/usr/bin/env python3
"""Cross-harness bridge and traceable dialogue ledger CLI for agy-claude-loop.

Coordinates Antigravity (agy), Claude Code (claude), and Codex (codex, incl.
GPT-6.2-Sol) as interchangeable Architect / Executor / Verifier / Advisor roles,
supporting cross-harness read-only consultation (`consult`), write delegation
(`delegate`), live progress inspection (`inspect*`), and git-ignored traceable
dialogue logs (`<repo>/.agent-loop/LATEST.md`) using only the Python 3 stdlib.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOOP_DIR_NAME = ".agent-loop"
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

VALID_EVENTS = (
    "audit_summary",
    "doc_pack_ready",
    "handoff_to_cc",
    "cc_progress",
    "cc_delivery",
    "verifier_audit",
    "rework_to_cc",
    "ux_walkthrough",
    "signoff",
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
    "rework_to_cc": "阶段4 · 打回 Claude Code 返工",
    "ux_walkthrough": "阶段5 · 真实用户视角体验",
    "signoff": "阶段6 · 终审放行与结项汇报",
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


def ensure_manifest_v2(manifest: dict[str, Any]) -> dict[str, Any]:
    """Upgrade legacy v1 manifests in memory while preserving backward fields."""
    manifest.setdefault("complexity", "complex")
    roster = manifest.setdefault(
        "roster",
        {
            "architect": {"harness": "agy", "model": ""},
            "executor": {"harness": "claude", "model": ""},
            "verifier": {"harness": "agy", "model": ""},
        },
    )
    if isinstance(roster, dict):
        for role_key, fallback_h in (("architect", "agy"), ("executor", "claude"), ("verifier", "agy")):
            roster.setdefault(role_key, {"harness": fallback_h, "model": ""})

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

    manifest.setdefault("jobs", [])
    manifest.setdefault("synced_cc_assistant_ts", [])
    manifest.setdefault("synced_codex_ts", [])
    return manifest


def load_manifest(run_dir: Path) -> dict[str, Any]:
    """Read manifest.json from a run directory."""
    raw = json.loads((run_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    return ensure_manifest_v2(raw)


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    """Persist manifest.json deterministically."""
    ensure_manifest_v2(manifest)
    manifest["updated_at"] = now_iso()
    (run_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
    """Build native inspect/resume commands across AGY, Claude Code, and Codex."""
    ensure_manifest_v2(manifest)
    sessions = manifest["sessions"]
    agy_sid = sessions["agy"].get("session_id") or ""
    codex_sid = sessions["codex"].get("session_id") or ""
    repo_str = str(manifest.get("repo") or "").strip()

    agy_cmds: dict[str, str] = {}
    if agy_sid:
        agy_cmds["uri"] = f"conversation://{agy_sid}"
        if (AGY_CLI_BRAIN_ROOT / agy_sid).exists():
            agy_cmds["cli_resume"] = f"agy --conversation {agy_sid}"
        agy_cmds["terminal_view"] = (
            f"python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py view --agy-id {agy_sid} --last 10"
        )

    codex_cmds: dict[str, str] = {}
    if codex_sid:
        if repo_str:
            codex_cmds["resume"] = f'cd "{repo_str}" && codex resume {codex_sid}'
            codex_cmds["terminal_view"] = (
                f'python3 -B ~/.agents/skills/agy-claude-loop/scripts/loopctl.py view --repo "{repo_str}" --target codex'
            )
        else:
            codex_cmds["resume"] = f"codex resume {codex_sid}"

    return {
        "agy": agy_cmds,
        "claude": format_cc_commands(manifest),
        "codex": codex_cmds,
    }


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
) -> str:
    """Resolve model slug: explicit flag > matching roster role > global default ('')."""
    if explicit_model is not None and explicit_model != "":
        return normalize_model(harness, explicit_model)
    roster = manifest.get("roster") or {}
    if role_name and isinstance(roster.get(role_name), dict):
        spec = roster[role_name]
        if spec.get("harness") == harness and spec.get("model"):
            return str(spec["model"])
    for key in ("advisor", "executor", "verifier", "architect"):
        spec = roster.get(key)
        if isinstance(spec, dict) and spec.get("harness") == harness and spec.get("model"):
            return str(spec["model"])
    return ""


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
    complexity = manifest.get("complexity") or "complex"
    mode_label = "complex (ultracode)" if complexity == "complex" else "simple"

    lines: list[str] = [
        f"# 多 Harness 闭环对话台账 · {manifest.get('slug', '')}",
        "",
        f"> **Run ID**：`{manifest.get('run_id')}` · **复杂度/模式**：`{mode_label}` · **当前阶段**：`{manifest.get('phase')}` · **终审结论**：`{manifest.get('verdict')}` · **返工轮次**：`{manifest.get('round', 0)}` · **更新**：`{manifest.get('updated_at')}`",
        "",
        "## 当前角色花名册（Role Roster）",
        "",
        "| 角色 | Harness | 模型 |",
        "|---|---|---|",
    ]
    roster = manifest.get("roster") or {}
    for role_key in ("architect", "executor", "verifier", "advisor"):
        spec = roster.get(role_key)
        if isinstance(spec, dict) and spec.get("harness"):
            model_disp = f"`{spec['model']}`" if spec.get("model") else "_继承全局默认_"
            lines.append(f"| **{role_key}** | `{spec['harness']}` | {model_disp} |")

    lines.extend([
        "",
        "## 三端会话可追溯导航",
        "",
        "| 维度 | 标识 / 路径 | 快速查看 / 接入 |",
        "|---|---|---|",
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
    dialogue_path.write_text(content, encoding="utf-8")
    (repo / LOOP_DIR_NAME / LATEST_MD_FILE).write_text(content, encoding="utf-8")
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
    commits: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one structured event to events.jsonl, update manifest, and re-render DIALOGUE.md."""
    manifest = load_manifest(run_dir)
    if phase:
        manifest["phase"] = phase
    if verdict:
        manifest["verdict"] = verdict
    if event == "rework_to_cc":
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


def find_cc_jsonl(repo: Path, session_id: str | None) -> Path | None:
    """Locate the JSONL transcript file for a Claude Code session."""
    proj_dir = encode_claude_project_dir(repo)
    if session_id:
        direct = proj_dir / f"{session_id}.jsonl"
        if direct.exists():
            return direct
        if CLAUDE_PROJECTS_ROOT.exists():
            for match in CLAUDE_PROJECTS_ROOT.glob(f"*/{session_id}.jsonl"):
                return match
    if proj_dir.exists():
        candidates = sorted(
            (p for p in proj_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


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


def parse_cc_jsonl_tail(path: Path, tail_turns: int) -> dict[str, Any]:
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
        "recent_turns": [{k: v for k, v in t.items() if k != "text"} for t in recent],
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
    """Enforce AGY_LOOP_DEPTH < 2 guardrail unless --allow-nested is given."""
    try:
        depth = int(os.environ.get("AGY_LOOP_DEPTH", "0"))
    except ValueError:
        depth = 0
    if depth >= 2 and not allow_nested:
        return False, depth, dict(os.environ)
    next_env = dict(os.environ)
    next_env["AGY_LOOP_DEPTH"] = str(depth + 1)
    return True, depth, next_env


def find_active_write_conflicts(repo: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect active Claude or background Codex/AGY write jobs in the same repo."""
    conflicts: list[dict[str, Any]] = []
    for a in query_claude_agents(repo):
        if a.get("status") == "busy" or a.get("state") == "running":
            conflicts.append({"harness": "claude", "session_id": a.get("sessionId"), "detail": a})
    for job in manifest.get("jobs") or []:
        if isinstance(job, dict) and job.get("mode") == "write" and is_pid_alive(job.get("pid")):
            conflicts.append({"harness": job.get("harness"), "job_id": job.get("job_id"), "pid": job.get("pid")})
    return conflicts


def _build_codex_exec_argv(
    binary: str,
    repo: Path,
    prompt_text: str,
    *,
    sandbox: str,
    out_target: str,
    model: str = "",
    session_id: str = "",
) -> list[str]:
    """Construct `codex exec` or `codex exec resume` argv with options before positionals."""
    if session_id:
        argv = [binary, "exec", "resume", "--json", "-o", out_target]
        if model:
            argv.extend(["-m", model])
        argv.extend([session_id, prompt_text])
        return argv
    argv = [binary, "exec", "-C", str(repo), "-s", sandbox, "--json", "-o", out_target]
    if model:
        argv.extend(["-m", model])
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

    roster: dict[str, dict[str, str]] = {
        "architect": parse_role_spec(args.architect),
        "executor": parse_role_spec(args.executor),
        "verifier": parse_role_spec(args.verifier),
    }
    if args.advisor:
        roster["advisor"] = parse_role_spec(args.advisor)

    docs = [d.strip() for d in (args.docs or "").split(",") if d.strip()]
    base_commit = args.base_commit or git_head_short(repo)
    sessions = default_sessions_dict()
    if agy_id:
        sessions["agy"]["session_id"] = agy_id
        sessions["agy"]["transcript_path"] = agy_log
    for role_spec in roster.values():
        h = role_spec["harness"]
        if role_spec.get("model") and not sessions[h].get("model"):
            sessions[h]["model"] = role_spec["model"]

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
        "verdict": "PENDING",
        "base_commit": base_commit,
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
    (repo / LOOP_DIR_NAME / LATEST_RUN_FILE).write_text(run_id + "\n", encoding="utf-8")
    dialogue_path = render_dialogue(repo, run_dir)

    out = {"ok": True, "run_id": run_id, "run_dir": str(run_dir), "dialogue_md": str(dialogue_path), "manifest": manifest}
    print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else str(dialogue_path))
    return 0


def cmd_set_roster(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    changes: dict[str, Any] = {}
    for role_key in ("architect", "executor", "verifier", "advisor"):
        val = getattr(args, role_key, "")
        if val:
            spec = parse_role_spec(val)
            manifest["roster"][role_key] = spec
            if spec["model"]:
                manifest["sessions"][spec["harness"]]["model"] = spec["model"]
            changes[role_key] = spec
    if not changes:
        print(json.dumps({"ok": False, "error": "no roster role flags provided"}, ensure_ascii=False), file=sys.stderr)
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


def cmd_record(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir = resolve_run_dir(repo, args.run_id)
    body = read_prompt_or_body(args.body, args.body_file)
    entry = append_event(
        repo,
        run_dir,
        role=args.role,
        event=args.event,
        summary=args.summary,
        body=body,
        phase=args.phase,
        verdict=args.verdict,
        commits=args.commits,
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

    round_num = int(manifest.get("round", 0)) + (1 if args.event == "rework_to_cc" else 0)
    prompt_file = run_dir / f"ROUND-{round_num}-PROMPT.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    append_event(
        repo,
        run_dir,
        role="agy",
        event=args.event,
        summary=args.summary or f"第 {round_num} 轮指令发送给 Claude Code",
        body=prompt_text,
        phase="CC_EXECUTING",
        verdict="NEEDS_FIX" if args.event == "rework_to_cc" else None,
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
    prompt_text = read_prompt_or_body(args.prompt, args.prompt_file)
    if not prompt_text.strip():
        print(json.dumps({"ok": False, "error": "empty prompt"}, ensure_ascii=False), file=sys.stderr)
        return 1

    model = resolve_harness_model(manifest, args.to, args.model)
    sid = args.session_id or manifest["sessions"][args.to].get("session_id") or ""
    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    jobs_dir = run_dir / JOBS_DIR
    jobs_dir.mkdir(parents=True, exist_ok=True)
    reply_file = jobs_dir / f"consult-{args.to}-{ts_tag}.md"
    argv = build_consult_argv(args.to, repo, prompt_text, model=model, session_id=sid, reply_file=reply_file)

    append_event(
        repo,
        run_dir,
        role=args.from_role,
        event="cross_consult",
        summary=args.summary or f"{args.from_role} 向 {args.to} 发起只读评审/咨询",
        body=prompt_text,
        extra={"target": args.to, "model": model},
    )

    if args.dry_run:
        out = {
            "ok": True,
            "dry_run": True,
            "from": args.from_role,
            "to": args.to,
            "model": model,
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

    try:
        proc = subprocess.run(
            argv,
            cwd=str(repo),
            env=next_env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "error": f"consult timeout after {args.timeout}s"}, ensure_ascii=False))
        return 1

    reply_text, new_sid = extract_reply_and_session(args.to, proc.stdout, reply_file=reply_file)
    if proc.returncode != 0 and not reply_text:
        print(
            json.dumps(
                {"ok": False, "returncode": proc.returncode, "stderr": proc.stderr.strip(), "stdout": proc.stdout.strip()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    final_sid = new_sid or sid
    if final_sid or model:
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
        extra={"target": args.from_role, "model": model, "session_id": final_sid},
    )
    out = {
        "ok": True,
        "from": args.from_role,
        "to": args.to,
        "model": model,
        "session_id": final_sid,
        "reply": reply_text,
        "dialogue_md": str(run_dir / DIALOGUE_FILE),
    }
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
    prompt_text = read_prompt_or_body(args.prompt, args.prompt_file)
    if not prompt_text.strip():
        print(json.dumps({"ok": False, "error": "empty prompt"}, ensure_ascii=False), file=sys.stderr)
        return 1

    model = resolve_harness_model(manifest, args.to, args.model, role_name=args.role_name)
    sid = args.session_id or manifest["sessions"][args.to].get("session_id") or ""
    use_ultracode = False
    if args.to == "claude":
        use_ultracode = (
            args.ultracode
            if args.ultracode is not None
            else manifest.get("complexity", "complex") == "complex"
        )
        prompt_text = apply_ultracode_prefix(prompt_text, use_ultracode)

    if not args.dry_run and not args.force:
        conflicts = find_active_write_conflicts(repo, manifest)
        if conflicts:
            print(
                json.dumps(
                    {"ok": False, "dispatched": False, "reason": "active_writer_conflict", "conflicts": conflicts},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2

    ts_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    prompt_file = run_dir / f"DELEGATE-{args.to}-{ts_tag}.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    session_name = args.name or f"AGY-Loop: {manifest.get('slug')} ({args.role_name})"

    jobs_dir = run_dir / JOBS_DIR
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_id = f"{args.to}-{ts_tag}"
    stdout_log = jobs_dir / f"{job_id}.jsonl"
    last_msg_file = jobs_dir / f"{job_id}-last.md"

    sync_mode = bool(args.sync)
    argv = build_delegate_argv(
        args.to,
        repo,
        prompt_text,
        model=model,
        session_id=sid,
        session_name=session_name,
        sandbox=args.sandbox,
        last_msg_file=last_msg_file,
        sync=sync_mode,
    )

    phase_hint = "CC_EXECUTING" if args.to == "claude" else f"{args.to.upper()}_EXECUTING"
    append_event(
        repo,
        run_dir,
        role=args.from_role,
        event="delegate_task",
        summary=args.summary or f"{args.from_role} 派发 {args.role_name} 任务给 {args.to}",
        body=prompt_text,
        phase=phase_hint,
        extra={"target": args.to, "model": model, "ultracode": use_ultracode},
    )

    if args.dry_run:
        out = {
            "ok": True,
            "dry_run": True,
            "dispatched": False,
            "from": args.from_role,
            "to": args.to,
            "role_name": args.role_name,
            "model": model,
            "ultracode": use_ultracode,
            "sync": sync_mode,
            "argv": argv,
            "prompt_file": str(prompt_file),
            "dialogue_md": str(run_dir / DIALOGUE_FILE),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    binary = harness_bin(args.to)
    if not binary:
        print(json.dumps({"ok": False, "error": f"{args.to} CLI not found"}, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.to == "claude" and sid:
        agents = query_claude_agents(repo)
        matched = next((a for a in agents if a.get("sessionId") == sid), None)
        if matched and matched.get("kind") == "interactive":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dispatched": False,
                        "delivery_mode": "manual_paste_to_interactive_session",
                        "session_id": sid,
                        "resume_cmd": f"claude --resume {sid}",
                        "prompt_file": str(prompt_file),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

    if args.to == "claude" and not sync_mode:
        dispatch_ts = round(time.time(), 3)
        proc = subprocess.run(
            argv,
            cwd=str(repo),
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
        agents = query_claude_agents(repo)
        matched = next((a for a in agents if (bg_id and a.get("id") == bg_id) or a.get("name") == session_name), None)
        manifest = load_manifest(run_dir)
        manifest["last_dispatch_epoch"] = dispatch_ts
        final_sid = (matched.get("sessionId") if matched else None) or sid
        jsonl = find_cc_jsonl(repo, final_sid or None)
        sync_manifest_session(
            manifest,
            "claude",
            session_id=final_sid or None,
            model=model or None,
            bg_id=(matched.get("id") if matched else None) or bg_id,
            name=(matched.get("name") if matched else None) or session_name,
            kind=(matched.get("kind") if matched else None) or "background",
            path=str(jsonl) if jsonl else None,
        )
        save_manifest(run_dir, manifest)
        render_dialogue(repo, run_dir)
        print(
            json.dumps(
                {
                    "ok": True,
                    "dispatched": True,
                    "harness": "claude",
                    "model": model,
                    "ultracode": use_ultracode,
                    "session_id": final_sid,
                    "commands": format_cc_commands(manifest),
                    "prompt_file": str(prompt_file),
                    "dialogue_md": str(run_dir / DIALOGUE_FILE),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not sync_mode:
        dispatch_ts = round(time.time(), 3)
        log_fh = stdout_log.open("w", encoding="utf-8")
        proc = subprocess.Popen(
            argv,
            cwd=str(repo),
            env=next_env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_fh.close()
        manifest = load_manifest(run_dir)
        manifest["last_dispatch_epoch"] = dispatch_ts
        job_meta = {
            "job_id": job_id,
            "harness": args.to,
            "role_name": args.role_name,
            "mode": "write",
            "pid": proc.pid,
            "model": model,
            "stdout_jsonl": str(stdout_log),
            "last_msg_file": str(last_msg_file),
            "prompt_file": str(prompt_file),
            "started_at": now_iso(),
        }
        manifest["jobs"].append(job_meta)
        sync_manifest_session(manifest, args.to, session_id=sid or None, model=model or None, last_job_id=job_id)
        save_manifest(run_dir, manifest)
        render_dialogue(repo, run_dir)
        print(
            json.dumps(
                {
                    "ok": True,
                    "dispatched": True,
                    "background": True,
                    "harness": args.to,
                    "job": job_meta,
                    "dialogue_md": str(run_dir / DIALOGUE_FILE),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    proc = subprocess.run(
        argv,
        cwd=str(repo),
        env=next_env,
        capture_output=True,
        text=True,
        timeout=args.timeout,
        check=False,
    )
    reply_text, new_sid = extract_reply_and_session(args.to, proc.stdout, reply_file=last_msg_file)
    final_sid = new_sid or sid
    if proc.returncode != 0:
        err_out = {
            "ok": False,
            "dispatched": True,
            "background": False,
            "returncode": proc.returncode,
            "harness": args.to,
            "model": model,
            "session_id": final_sid,
            "error_output": reply_text,
            "stderr": proc.stderr.strip(),
            "dialogue_md": str(run_dir / DIALOGUE_FILE),
        }
        print(json.dumps(err_out, ensure_ascii=False, indent=2))
        return 1

    manifest = load_manifest(run_dir)
    _sync_discovered_session(repo, manifest, args.to, session_id=final_sid, model=model)
    save_manifest(run_dir, manifest)
    append_event(
        repo,
        run_dir,
        role=args.to,
        event="delivery",
        summary=f"{args.to} 完成同步派发任务",
        body=reply_text,
        phase="POST_VERIFIER",
        extra={"model": model, "session_id": final_sid},
    )
    out = {
        "ok": True,
        "dispatched": True,
        "background": False,
        "harness": args.to,
        "model": model,
        "session_id": final_sid,
        "reply": reply_text,
        "dialogue_md": str(run_dir / DIALOGUE_FILE),
    }
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


def cmd_inspect_cc(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir, manifest = _try_load_run(repo, args.run_id)

    sid = args.session_id or manifest.get("cc_session_id") or ""
    agents = query_claude_agents(repo)
    agent_entry, sid = select_repo_agent(agents, sid or None)

    jsonl_path = find_cc_jsonl(repo, sid or None)
    if not sid and jsonl_path:
        sid = jsonl_path.stem
    jsonl_info = parse_cc_jsonl_tail(jsonl_path, args.tail_turns) if jsonl_path else None
    normalized_state = classify_cc_state(agent_entry, jsonl_info)

    base = manifest.get("base_commit") or ""
    commits_since_base: list[str] = []
    if base and base != "NO_GIT":
        rc, out, _ = run_git(repo, ["log", f"{base}..HEAD", "--oneline"])
        if rc == 0 and out:
            commits_since_base = out.splitlines()

    synced = False
    if args.sync_ledger and run_dir is not None and normalized_state == "IDLE_COMPLETED" and jsonl_info:
        last_msg = jsonl_info.get("last_assistant_message")
        msg_ts = last_msg.get("timestamp") if last_msg else None
        already = set(manifest.get("synced_cc_assistant_ts") or [])
        if last_msg and msg_ts and msg_ts not in already:
            manifest = load_manifest(run_dir)
            sync_manifest_session(
                manifest,
                "claude",
                session_id=sid or None,
                path=str(jsonl_path) if jsonl_path else None,
            )
            manifest.setdefault("synced_cc_assistant_ts", []).append(msg_ts)
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
            )
            synced = True

    result = {
        "harness": "claude",
        "repo": str(repo),
        "normalized_state": normalized_state,
        "session_id": sid,
        "agent_entry": agent_entry,
        "all_repo_agents": agents,
        "jsonl": jsonl_info,
        "git_status_short": git_status_short(repo),
        "commits_since_base": commits_since_base,
        "synced_to_ledger": synced,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_inspect_codex(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir, manifest = _try_load_run(repo, args.run_id)
    ensure_manifest_v2(manifest) if manifest else None

    sid = args.session_id or (manifest.get("sessions", {}).get("codex", {}).get("session_id") if manifest else "") or ""
    codex_jobs = [
        j for j in (manifest.get("jobs") or [])
        if isinstance(j, dict) and j.get("harness") == "codex"
    ]
    latest_job = codex_jobs[-1] if codex_jobs else None
    job_alive = is_pid_alive(latest_job.get("pid")) if latest_job else False

    if not sid and latest_job and latest_job.get("stdout_jsonl"):
        log_path = Path(latest_job["stdout_jsonl"])
        if log_path.exists():
            _, extracted_sid = extract_reply_and_session("codex", log_path.read_text(encoding="utf-8", errors="replace"))
            if extracted_sid:
                sid = extracted_sid

    rollout_path = find_codex_rollout(repo, sid or None)
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
        rc, out, _ = run_git(repo, ["log", f"{base}..HEAD", "--oneline"])
        if rc == 0 and out:
            commits_since_base = out.splitlines()

    synced = False
    if args.sync_ledger and run_dir is not None and normalized_state == "IDLE_COMPLETED" and rollout_info:
        last_msg = rollout_info.get("last_assistant_message")
        msg_ts = last_msg.get("timestamp") if last_msg else None
        already = set(manifest.get("synced_codex_ts") or [])
        if last_msg and msg_ts and msg_ts not in already:
            manifest = load_manifest(run_dir)
            sync_manifest_session(
                manifest,
                "codex",
                session_id=sid or None,
                name=thread_name or None,
                path=str(rollout_path) if rollout_path else None,
            )
            manifest.setdefault("synced_codex_ts", []).append(msg_ts)
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
            )
            synced = True

    result = {
        "harness": "codex",
        "repo": str(repo),
        "normalized_state": normalized_state,
        "session_id": sid,
        "thread_name": thread_name,
        "job_alive": job_alive,
        "latest_job": latest_job,
        "rollout": rollout_info,
        "git_status_short": git_status_short(repo),
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


def _print_codex_view(repo: Path, manifest: dict[str, Any], last_n: int) -> int:
    """Print readable Codex rollout turns and interactive resume command."""
    ensure_manifest_v2(manifest)
    codex_slot = manifest["sessions"]["codex"]
    sid = codex_slot.get("session_id") or ""
    model = codex_slot.get("model") or ""
    rollout_str = codex_slot.get("rollout_path") or ""
    rollout_path = Path(rollout_str) if rollout_str and Path(rollout_str).exists() else find_codex_rollout(repo, sid or None)

    print("=== Codex 对话记录 ===")
    print(f"Session ID: {sid or '未绑定'}")
    print(f"Model: {model or '继承全局默认'}")
    print(f"Rollout: {rollout_path or '未找到'}")
    if sid:
        print(f'Interactive Resume: cd "{repo}" && codex resume {sid}')
    if not rollout_path or not rollout_path.exists():
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
        print(t.get("text") or t.get("text_preview") or "")
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
    if not args.repo:
        if args.agy_id or args.target == "agy":
            return _print_agy_view(args.agy_id, args.last)
        print("错误：--target all/codex/dialogue 需要传入 --repo（或传入 --agy-id 直接查看 AGY 对话）", file=sys.stderr)
        return 1

    repo = Path(args.repo).resolve()
    if args.target == "agy":
        agy_id = _resolve_agy_id(args.agy_id, repo, args.run_id)
        return _print_agy_view(agy_id, args.last)

    run_dir = resolve_run_dir(repo, args.run_id)
    manifest = load_manifest(run_dir)
    if args.target == "codex":
        return _print_codex_view(repo, manifest, args.last)

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
    roster_str = " · ".join(
        f"{k}={v.get('harness')}{':' + v['model'] if v.get('model') else ''}"
        for k, v in roster.items()
        if isinstance(v, dict) and v.get("harness")
    )
    print(f"=== Run 概览 · {manifest.get('run_id')} ({manifest.get('slug')}) ===")
    print(f"阶段: {manifest.get('phase')} | 结论: {manifest.get('verdict')} | 轮次: {manifest.get('round', 0)} | Git: {manifest.get('base_commit')} -> {git_head_short(repo)}")
    print(f"分工: {roster_str}")
    print("\n=== 终端直达命令 ===")
    if cmds.get("codex", {}).get("resume"):
        print(f"Codex 交互接入: {cmds['codex']['resume']}")
    if cmds.get("agy", {}).get("terminal_view"):
        print(f"AGY 终端只读查看: {cmds['agy']['terminal_view']}")
    if cmds.get("agy", {}).get("cli_resume"):
        print(f"AGY CLI 恢复: {cmds['agy']['cli_resume']}")
    if cmds.get("agy", {}).get("uri"):
        print(f"AGY IDE 协议链接: {cmds['agy']['uri']}")
    if cmds.get("claude", {}).get("resume"):
        print(f"Claude Code 恢复: {cmds['claude']['resume']}")
    print(f"共享对话台账: {dialogue_md}")

    print("")
    rc_codex = _print_codex_view(repo, manifest, args.last)
    agy_id = _resolve_agy_id(args.agy_id, repo, args.run_id)
    if agy_id:
        print("")
        _print_agy_view(agy_id, args.last)
    return rc_codex


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
    repo: Path,
    sid: str,
    since_epoch: float,
    latest_job: dict[str, Any] | None = None,
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
            rollout = find_codex_rollout(repo, resolved_sid)
            if rollout:
                return resolved_sid, rollout, None
        if CODEX_SESSIONS_ROOT.exists():
            repo_resolved = repo.resolve()
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

    agents = query_claude_agents(repo)
    agent_entry, selected_sid = select_repo_agent(agents, resolved_sid or None)
    if selected_sid:
        p = find_cc_jsonl(repo, selected_sid)
        is_busy = bool(agent_entry and (agent_entry.get("status") == "busy" or agent_entry.get("state") == "running"))
        try:
            fresh = bool(p and p.stat().st_mtime >= since_epoch - 5.0)
        except OSError:
            fresh = False
        if p and (resolved_sid or is_busy or fresh):
            return selected_sid, p, agent_entry

    proj_dir = encode_claude_project_dir(repo)
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

    if agent_entry and resolved_sid and agent_entry.get("sessionId") == resolved_sid:
        return resolved_sid, None, agent_entry
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
) -> bool:
    """Sync completed delivery message from watch into manifest + DIALOGUE.md once."""
    if run_dir is None or not last_msg or not str(last_msg.get("text") or "").strip():
        return False

    manifest = load_manifest(run_dir)
    ts_key = "synced_codex_ts" if harness == "codex" else "synced_cc_assistant_ts"
    msg_ts = str(last_msg.get("timestamp") or "") or sha256_prefix(str(last_msg["text"]))
    already = set(manifest.get(ts_key) or [])
    if msg_ts in already:
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
    manifest.setdefault(ts_key, []).append(msg_ts)
    save_manifest(run_dir, manifest)

    ev_name = "delivery" if harness == "codex" else "cc_delivery"
    fallback_summary = "Codex 已交卷" if harness == "codex" else "Claude Code 已交卷"
    first_line = str(last_msg["text"]).splitlines()[0][:100] if str(last_msg["text"]).strip() else fallback_summary
    append_event(
        repo,
        run_dir,
        role=harness,
        event=ev_name,
        summary=first_line,
        body=str(last_msg["text"]),
        phase="POST_VERIFIER",
        commits=",".join(c.split()[0] for c in commits_since_base) if commits_since_base else None,
        extra={"model": model, "session_id": sid, "source": "watch"} if model or sid else {"source": "watch"},
    )
    return True


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
        "run_id": refreshed.get("run_id") or "",
        "session_id": sid,
        "elapsed_sec": round(elapsed_sec, 2),
        "synced_to_ledger": synced,
        "git_head": git_head_short(repo),
        "git_status_short": git_status_short(repo),
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
        print(f"[watch:{reason}] harness={harness} sid={sid or '-'} elapsed={payload['elapsed_sec']}s {preview}".strip())
    return exit_code


def cmd_watch(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    run_dir, manifest = _try_load_run(repo, args.run_id)
    if manifest:
        ensure_manifest_v2(manifest)

    harness = args.harness
    latest_job: dict[str, Any] | None = None
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

    tracked_pid = int(args.pid or (latest_job.get("pid") if latest_job and latest_job.get("pid") else 0))
    ever_saw_pid_alive = is_pid_alive(tracked_pid) if tracked_pid else False

    base = manifest.get("base_commit") or "" if manifest else ""
    commits_since_base: list[str] = []
    if base and base != "NO_GIT":
        rc, out_commits, _ = run_git(repo, ["log", f"{base}..HEAD", "--oneline"])
        if rc == 0 and out_commits:
            commits_since_base = out_commits.splitlines()

    start_mono = time.monotonic()
    last_activity_mono = start_mono
    last_git_check_mono = 0.0
    last_size = -1
    last_event_ts = ""
    last_git_sig = (git_head_short(repo), tuple(git_status_short(repo)))

    poll_interval = max(float(args.interval), 0.2)
    settle_sec = max(float(args.settle_seconds), 0.0)
    overall_deadline = start_mono + max(int(args.timeout), 1)

    def _finish(
        reason: str,
        code: int,
        cur_sid: str,
        log_path: Path | None,
        last_msg: dict[str, Any] | None,
        diagnostic: dict[str, Any] | None = None,
    ) -> int:
        nonlocal commits_since_base
        if base and base != "NO_GIT":
            rc_c, out_c, _ = run_git(repo, ["log", f"{base}..HEAD", "--oneline"])
            if rc_c == 0 and out_c:
                commits_since_base = out_c.splitlines()
        model = (
            str(manifest.get("sessions", {}).get(harness, {}).get("model") or "")
            or str((manifest.get("roster") or {}).get("executor", {}).get("model") or "")
        ) if manifest else ""
        synced = False
        if args.sync_ledger and reason == "COMPLETED" and run_dir is not None:
            synced = _sync_watch_delivery(
                repo,
                run_dir,
                harness,
                sid=cur_sid,
                model=model,
                log_path=log_path,
                last_msg=last_msg,
                commits_since_base=commits_since_base,
            )
        return _emit_watch_result(
            reason=reason,
            exit_code=code,
            harness=harness,
            repo=repo,
            run_dir=run_dir,
            manifest=manifest,
            sid=cur_sid,
            elapsed_sec=time.monotonic() - start_mono,
            synced=synced,
            commits_since_base=commits_since_base,
            last_msg=last_msg,
            diagnostic=diagnostic,
            as_json=bool(args.json),
        )

    while True:
        discovered_sid, log_path, agent_entry = _discover_fresh_log(
            harness, repo, sid, effective_since, latest_job=latest_job
        )
        if discovered_sid:
            sid = discovered_sid

        pid_alive = is_pid_alive(tracked_pid) if tracked_pid else False
        if pid_alive:
            ever_saw_pid_alive = True

        now_mono = time.monotonic()
        cur_size = -1
        cur_event_ts = ""

        if harness == "codex":
            rollout_info = parse_codex_rollout_tail(log_path, 5) if (log_path and log_path.exists()) else None
            if rollout_info:
                if rollout_info.get("session_id"):
                    sid = str(rollout_info["session_id"])
                cur_size = int(rollout_info.get("file_size_bytes") or -1)
                cur_event_ts = str(rollout_info.get("last_event_ts") or "")
                last_msg = rollout_info.get("last_assistant_message")
                msg_epoch = parse_iso_epoch(last_msg.get("timestamp")) if last_msg else 0.0
                evt_epoch = parse_iso_epoch(cur_event_ts)

                if rollout_info.get("has_aborted") and not rollout_info.get("has_task_complete") and evt_epoch >= effective_since:
                    return _finish("BLOCKED_OR_FAILED", 1, sid, log_path, last_msg, {"rollout": rollout_info})

                if rollout_info.get("has_task_complete") and max(msg_epoch, evt_epoch) >= effective_since:
                    if settle_sec > 0:
                        time.sleep(settle_sec)
                    refreshed_path = find_codex_rollout(repo, sid or None) or log_path
                    refreshed_info = parse_codex_rollout_tail(refreshed_path, 5) if (refreshed_path and refreshed_path.exists()) else rollout_info
                    final_msg = refreshed_info.get("last_assistant_message") or last_msg
                    return _finish("COMPLETED", 0, sid, refreshed_path, final_msg)

            if tracked_pid and ever_saw_pid_alive and not pid_alive:
                flush_wait = min(max(settle_sec, 0.5), 2.0)
                time.sleep(flush_wait)
                _, flush_path, _ = _discover_fresh_log("codex", repo, sid, effective_since, latest_job=latest_job)
                flush_path = flush_path or log_path
                flush_info = parse_codex_rollout_tail(flush_path, 5) if (flush_path and flush_path.exists()) else None
                if not flush_info:
                    flush_info = _synthesize_codex_rollout_from_job(latest_job, sid, effective_since)
                    if flush_info and flush_info.get("rollout_path"):
                        flush_path = Path(flush_info["rollout_path"])
                if flush_info:
                    f_msg = flush_info.get("last_assistant_message")
                    f_msg_ep = parse_iso_epoch(f_msg.get("timestamp")) if f_msg else 0.0
                    f_evt_ep = parse_iso_epoch(flush_info.get("last_event_ts"))
                    if flush_info.get("has_task_complete") and max(f_msg_ep, f_evt_ep) >= effective_since:
                        return _finish("COMPLETED", 0, sid, flush_path, f_msg)
                stdout_tail = _read_job_stdout_tail(latest_job)
                return _finish(
                    "PROCESS_EXITED_FAILED",
                    1,
                    sid,
                    flush_path,
                    None,
                    {"pid": tracked_pid, "stdout_tail": stdout_tail},
                )

        else:  # claude
            jsonl_info = parse_cc_jsonl_tail(log_path, 5) if (log_path and log_path.exists()) else None
            if jsonl_info:
                cur_size = int(jsonl_info.get("file_size_bytes") or -1)
                cur_event_ts = str(jsonl_info.get("last_record_ts") or "")
            last_msg = jsonl_info.get("last_assistant_message") if jsonl_info else None
            msg_epoch = parse_iso_epoch(last_msg.get("timestamp")) if last_msg else 0.0
            rec_epoch = parse_iso_epoch(jsonl_info.get("last_record_ts")) if jsonl_info else 0.0
            agent_busy = bool(agent_entry and (agent_entry.get("status") == "busy" or agent_entry.get("state") == "running"))

            if agent_entry and agent_entry.get("state") in ("blocked", "failed"):
                return _finish("BLOCKED_OR_FAILED", 1, sid, log_path, last_msg, {"agent_entry": agent_entry})
            if jsonl_info and jsonl_info.get("has_stop_failure") and rec_epoch >= effective_since:
                return _finish("BLOCKED_OR_FAILED", 1, sid, log_path, last_msg, {"jsonl": jsonl_info})

            if (
                jsonl_info
                and jsonl_info.get("last_stop_reason") == "end_turn"
                and last_msg
                and max(msg_epoch, rec_epoch) >= effective_since
                and not agent_busy
                and not pid_alive
            ):
                if settle_sec > 0:
                    time.sleep(settle_sec)
                re_agents = query_claude_agents(repo)
                re_entry, _ = select_repo_agent(re_agents, sid or None)
                re_busy = bool(re_entry and (re_entry.get("status") == "busy" or re_entry.get("state") == "running"))
                re_path = find_cc_jsonl(repo, sid or None) or log_path
                re_info = parse_cc_jsonl_tail(re_path, 5) if (re_path and re_path.exists()) else jsonl_info
                if not re_busy and re_info.get("last_stop_reason") != "tool_use":
                    final_msg = re_info.get("last_assistant_message") or last_msg
                    return _finish("COMPLETED", 0, sid, re_path, final_msg)

            if tracked_pid and ever_saw_pid_alive and not pid_alive:
                flush_wait = min(max(settle_sec, 0.5), 2.0)
                time.sleep(flush_wait)
                f_sid, flush_path, _ = _discover_fresh_log("claude", repo, sid, effective_since, latest_job=latest_job)
                if f_sid:
                    sid = f_sid
                flush_path = flush_path or log_path
                flush_info = parse_cc_jsonl_tail(flush_path, 5) if (flush_path and flush_path.exists()) else None
                f_msg = flush_info.get("last_assistant_message") if flush_info else None
                f_msg_ep = parse_iso_epoch(f_msg.get("timestamp")) if f_msg else 0.0
                f_rec_ep = parse_iso_epoch(flush_info.get("last_record_ts")) if flush_info else 0.0
                if flush_info and f_msg and max(f_msg_ep, f_rec_ep) >= effective_since and not flush_info.get("has_stop_failure"):
                    return _finish("COMPLETED", 0, sid, flush_path, f_msg)
                return _finish(
                    "PROCESS_EXITED_FAILED",
                    1,
                    sid,
                    flush_path,
                    None,
                    {"pid": tracked_pid, "jsonl": flush_info, "stdout_tail": _read_job_stdout_tail(latest_job)},
                )

        # Update activity trackers
        activity_detected = False
        if cur_size != last_size or cur_event_ts != last_event_ts:
            last_size = cur_size
            last_event_ts = cur_event_ts
            activity_detected = True
        if now_mono - last_git_check_mono >= 1.0:
            last_git_check_mono = now_mono
            cur_git_sig = (git_head_short(repo), tuple(git_status_short(repo)))
            if cur_git_sig != last_git_sig:
                last_git_sig = cur_git_sig
                activity_detected = True
        if tracked_pid and pid_alive and _has_active_child_process(tracked_pid):
            activity_detected = True
        if activity_detected:
            last_activity_mono = now_mono

        if args.stall_timeout > 0 and (now_mono - last_activity_mono) >= int(args.stall_timeout):
            return _finish(
                "STALLED",
                2,
                sid,
                log_path,
                None,
                {
                    "idle_sec": round(now_mono - last_activity_mono, 1),
                    "pid": tracked_pid,
                    "pid_alive": pid_alive,
                    "last_event_ts": last_event_ts,
                },
            )

        if now_mono >= overall_deadline:
            return _finish(
                "TIMEOUT",
                124,
                sid,
                log_path,
                None,
                {"pid": tracked_pid, "pid_alive": pid_alive, "last_event_ts": last_event_ts},
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
        "base_commit": manifest.get("base_commit"),
        "current_head": git_head_short(repo),
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


AGENTS_SNIPPET = """## 多 Harness 协作角色（默认口径，均调用 `agy-claude-loop`）

- **Antigravity（`agy`）**：默认任 **Architect + Post-Verifier + 体验官**——负责项目审查、撰写 PRD/Tech-Spec/Test-Plan/Handoff 规格包，用 `loopctl.py delegate` 派活并挂 `loopctl.py watch` 零 Token 守候，完工后独立四维终审、实机体验并向用户结项。
- **Claude Code（`claude`）**：默认任 **Executor**（复杂需求默认首行 `ultracode`）——严格按规格包 CUI-First 分阶段施工与提交，遇口径疑问可 `loopctl.py consult` 取经，收口前自跑 `audit_commits.py` 并回写 `.agent-loop/LATEST.md`。
- **Codex（`codex`，如 `GPT-6.2-Sol`）**：按用户指定任 **Executor / 审校顾问**——承担开发实现或对抗性代码审校，保持原生会话可追溯（`codex resume`），进度与交付同步至 `.agent-loop/LATEST.md`。
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
        "git": shutil.which("git"),
        "pgrep": shutil.which("pgrep"),
    }
    binaries: dict[str, dict[str, Any]] = {}
    for name, path in bin_checks.items():
        version = _probe_bin_version(path, "--version") if (path and name != "pgrep") else ""
        binaries[name] = {
            "installed": bool(path),
            "path": path or "",
            "version": version,
        }

    mount_paths = {
        "canonical": Path.home() / ".agents" / "skills" / "agy-claude-loop",
        "claude": Path.home() / ".claude" / "skills" / "agy-claude-loop",
        "codex": Path.home() / ".codex" / "skills" / "agy-claude-loop",
        "antigravity": Path.home() / ".gemini" / "config" / "skills" / "agy-claude-loop",
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

    print("=== agy-claude-loop doctor ===")
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
    parser = argparse.ArgumentParser(description="AGY <-> Claude Code <-> Codex traceable loop controller")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_ultracode_args(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group()
        group.add_argument("--ultracode", dest="ultracode", action="store_true", default=None)
        group.add_argument("--no-ultracode", dest="ultracode", action="store_false")

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
    p_init.add_argument("--json", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_roster = sub.add_parser("set-roster", help="Update dynamic role/harness/model assignments")
    p_roster.add_argument("--repo", default=".")
    p_roster.add_argument("--run-id", default=None)
    p_roster.add_argument("--role", choices=ROLES_WITH_USER, default="user")
    p_roster.add_argument("--architect", default="")
    p_roster.add_argument("--executor", default="")
    p_roster.add_argument("--verifier", default="")
    p_roster.add_argument("--advisor", default="")
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
    p_send.add_argument("--dry-run", action="store_true")
    p_send.add_argument("--json", action="store_true")
    p_send.set_defaults(func=cmd_send_cc)

    p_cons = sub.add_parser("consult", help="Cross-harness read-only consultation/review")
    p_cons.add_argument("--repo", default=".")
    p_cons.add_argument("--run-id", default=None)
    p_cons.add_argument("--from", dest="from_role", choices=ROLES_WITH_USER, required=True)
    p_cons.add_argument("--to", choices=HARNESSES, required=True)
    p_cons.add_argument("--model", default=None)
    p_cons.add_argument("--session-id", default="")
    p_cons.add_argument("--prompt", default="")
    p_cons.add_argument("--prompt-file", default="")
    p_cons.add_argument("--summary", default="")
    p_cons.add_argument("--timeout", type=int, default=300)
    p_cons.add_argument("--allow-nested", action="store_true")
    p_cons.add_argument("--dry-run", action="store_true")
    p_cons.add_argument("--json", action="store_true")
    p_cons.set_defaults(func=cmd_consult)

    p_del = sub.add_parser("delegate", help="Cross-harness write/execution delegation")
    p_del.add_argument("--repo", default=".")
    p_del.add_argument("--run-id", default=None)
    p_del.add_argument("--from", dest="from_role", choices=ROLES_WITH_USER, required=True)
    p_del.add_argument("--to", choices=HARNESSES, required=True)
    p_del.add_argument("--role-name", default="executor")
    p_del.add_argument("--model", default=None)
    p_del.add_argument("--session-id", default="")
    p_del.add_argument("--name", default="")
    p_del.add_argument("--prompt", default="")
    p_del.add_argument("--prompt-file", default="")
    p_del.add_argument("--summary", default="")
    _add_ultracode_args(p_del)
    mode_grp = p_del.add_mutually_exclusive_group()
    mode_grp.add_argument("--background", dest="sync", action="store_false", default=False)
    mode_grp.add_argument("--sync", dest="sync", action="store_true")
    p_del.add_argument("--sandbox", choices=("workspace-write", "danger-full-access"), default="workspace-write")
    p_del.add_argument("--timeout", type=int, default=600)
    p_del.add_argument("--force", action="store_true")
    p_del.add_argument("--allow-nested", action="store_true")
    p_del.add_argument("--dry-run", action="store_true")
    p_del.add_argument("--json", action="store_true")
    p_del.set_defaults(func=cmd_delegate)

    p_icc = sub.add_parser("inspect-cc", help="Inspect Claude Code session progress (read-only by default)")
    p_icc.add_argument("--repo", default=".")
    p_icc.add_argument("--run-id", default=None)
    p_icc.add_argument("--session-id", default="")
    p_icc.add_argument("--tail-turns", type=int, default=5)
    p_icc.add_argument("--sync-ledger", action="store_true")
    p_icc.add_argument("--json", action="store_true")
    p_icc.set_defaults(func=cmd_inspect_cc)

    p_icodex = sub.add_parser("inspect-codex", help="Inspect Codex session/rollout progress (read-only by default)")
    p_icodex.add_argument("--repo", default=".")
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
    p_insp.add_argument("--run-id", default=None)
    p_insp.add_argument("--session-id", default="")
    p_insp.add_argument("--agy-id", default="")
    p_insp.add_argument("--tail-turns", type=int, default=5)
    p_insp.add_argument("--last", type=int, default=None)
    p_insp.add_argument("--sync-ledger", action="store_true")
    p_insp.add_argument("--json", action="store_true")
    p_insp.set_defaults(func=cmd_inspect)

    p_view = sub.add_parser("view", help="Print readable Codex/AGY conversations and terminal commands")
    p_view.add_argument("--repo", default=None)
    p_view.add_argument("--run-id", default=None)
    p_view.add_argument("--agy-id", default="")
    p_view.add_argument("--target", choices=("all", "dialogue", "codex", "agy"), default="all")
    p_view.add_argument("--last", type=int, default=10)
    p_view.set_defaults(func=cmd_view)

    p_watch = sub.add_parser("watch", help="Zero-token local monitor waiting for Codex or Claude Code completion")
    p_watch.add_argument("--repo", default=".")
    p_watch.add_argument("--run-id", default=None)
    p_watch.add_argument("--harness", choices=("codex", "claude"), required=True)
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

    p_stat = sub.add_parser("status", help="Show current loop run status and traceable links")
    p_stat.add_argument("--repo", default=".")
    p_stat.add_argument("--run-id", default=None)
    p_stat.add_argument("--json", action="store_true")
    p_stat.set_defaults(func=cmd_status)

    p_snip = sub.add_parser("snippet", help="Print or append the 3-line AGENTS.md role snippet")
    p_snip.add_argument("--append", default="")
    p_snip.add_argument("--json", action="store_true")
    p_snip.set_defaults(func=cmd_snippet)

    p_doc = sub.add_parser("doctor", help="Check CLI binaries, skill symlinks, and AGY brain paths")
    p_doc.add_argument("--json", action="store_true")
    p_doc.set_defaults(func=cmd_doctor)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (FileNotFoundError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
