#!/usr/bin/env python3
"""Read-only CC Switch provider reader, isolated runtime materializer, and session checkpoint builder.

Supports manual and automatic failover across CC Switch providers without mutating
global user configs (`~/.claude/settings.json`, `~/.codex/config.toml`, `~/.cc-switch/cc-switch.db`)
and without leaking credentials into repositories, manifests, logs, or CLI output.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


CC_SWITCH_DB_ENV = "HARNESS_LOOP_CC_SWITCH_DB"
RUNTIME_DIR_ENV = "HARNESS_LOOP_RUNTIME_DIR"

SUPPORTED_PROVIDER_HARNESSES = ("claude", "codex", "agy")

CLAUDE_PROVIDER_ENV_PREFIXES = ("ANTHROPIC_",)
CLAUDE_PROVIDER_ENV_KEYS = frozenset(
    {
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
        "CLAUDE_CODE_AUTO_MODE_SERVER",
        "CLAUDE_CODE_DISABLE_ARTIFACT",
    }
)
CLAUDE_SHELL_ENV_UNSET = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_DISABLE_THINKING",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "CLAUDE_CODE_AUTO_MODE_SERVER",
    "CLAUDE_CODE_DISABLE_ARTIFACT",
)
CLAUDE_SAFE_TOP_LEVEL_DEFAULTS = (
    "permissions",
    "hooks",
    "enabledPlugins",
    "language",
    "skipDangerousModePermissionPrompt",
    "teammateMode",
    "terminalProgressBarEnabled",
    "verbose",
    "statusLine",
    "alwaysThinkingEnabled",
)

CODEX_ENV_UNSET = ("OPENAI_API_KEY", "OPENAI_BASE_URL")
CODEX_SYMLINK_ITEMS = (
    "sessions",
    "session_index.jsonl",
    "AGENTS.md",
    "skills",
    "rules",
    "models_cache.json",
)

RESUME_INCOMPATIBLE_PATTERNS = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"invalid.?signature",
        r"(thinking|redacted_thinking).*?(signature|block|invalid)",
        r"invalid_request_error",
        r"tool_use.*without.*tool_result",
        r"session.*not.?found|no conversation found",
        r"(model_provider|provider).*mismatch",
        r"previous_response_id",
        r"corrupt(?:ed)?.*session",
        r"\bhttp\s*400\b|status[:= ]+400\b",
    )
)

_SECRET_TEXT_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{8,}", re.IGNORECASE),
    re.compile(
        r"((?:ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY|API_KEY|AUTH_TOKEN)\s*[:=]\s*)([^\s\"',;]+)",
        re.IGNORECASE,
    ),
)


def now_iso() -> str:
    """Return local ISO-8601 timestamp rounded to seconds."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_cc_switch_db_path(db_path: Path | str | None = None) -> Path:
    """Resolve CC Switch SQLite database path dynamically without freezing HOME."""
    if db_path:
        return Path(db_path).expanduser().resolve()
    env_override = (os.environ.get(CC_SWITCH_DB_ENV) or "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (Path.home() / ".cc-switch" / "cc-switch.db").resolve()


def get_runtime_root(runtime_root: Path | str | None = None) -> Path:
    """Resolve isolated runtime directory root outside target repositories."""
    if runtime_root:
        return Path(runtime_root).expanduser().resolve()
    env_override = (os.environ.get(RUNTIME_DIR_ENV) or "").strip()
    if env_override:
        return Path(env_override).expanduser().resolve()
    return (Path.home() / ".cache" / "harness-loop" / "runtime").resolve()


def sanitize_slug(raw: str, max_len: int = 32) -> str:
    """Sanitize path segments to safe ASCII slug characters."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (raw or "").strip()).strip("-._")
    return (cleaned or "item")[:max_len]


def sanitize_base_url(url: str | None) -> str:
    """Strip userinfo, query string, and fragment from a provider URL."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw.split("?", 1)[0].split("#", 1)[0]
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        path = parsed.path.rstrip("/")
        return f"{parsed.scheme}://{host}{port}{path}"
    except ValueError:
        return ""


def redact_secret_like_strings(text: str) -> str:
    """Redact API keys and bearer tokens from text snippets before logging or checkpoints."""
    out = text or ""
    out = _SECRET_TEXT_PATTERNS[0].sub("sk-<REDACTED>", out)
    out = _SECRET_TEXT_PATTERNS[1].sub("Bearer <REDACTED>", out)
    out = _SECRET_TEXT_PATTERNS[2].sub(r"\1<REDACTED>", out)
    return out


def is_resume_incompatible_error(text: str) -> bool:
    """Detect whether an error or stdout tail indicates cross-provider --resume incompatibility."""
    sample = (text or "").strip()
    if not sample:
        return False
    return any(pat.search(sample) for pat in RESUME_INCOMPATIBLE_PATTERNS)


def _open_ro_sqlite(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"CC Switch database not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _parse_codex_toml_summary(config_text: str) -> tuple[str, str]:
    """Extract (model, base_url) from Codex TOML settings without printing secrets."""
    model = ""
    base_url = ""
    m_mod = re.search(r'(?m)^\s*model\s*=\s*["\']([^"\']+)["\']', config_text or "")
    if m_mod:
        model = m_mod.group(1).strip()
    m_url = re.search(r'(?m)^\s*base_url\s*=\s*["\']([^"\']+)["\']', config_text or "")
    if m_url:
        base_url = sanitize_base_url(m_url.group(1).strip())
    return model, base_url


def _summarize_provider_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convert raw CC Switch row into a credential-free public summary dict."""
    app_type = str(row.get("app_type") or "")
    raw_cfg_str = str(row.get("settings_config") or "{}")
    base_url = sanitize_base_url(row.get("website_url"))
    model = ""
    auth_kind = "none"

    try:
        parsed_cfg = json.loads(raw_cfg_str) if raw_cfg_str.strip() else {}
    except json.JSONDecodeError:
        parsed_cfg = {}

    if isinstance(parsed_cfg, dict):
        if app_type == "codex":
            toml_text = str(parsed_cfg.get("config") or "")
            c_model, c_url = _parse_codex_toml_summary(toml_text)
            model = c_model or model
            base_url = c_url or base_url
            auth_obj = parsed_cfg.get("auth")
            if isinstance(auth_obj, dict) and auth_obj:
                if auth_obj.get("OPENAI_API_KEY"):
                    auth_kind = "api_key"
                elif auth_obj.get("tokens") or auth_obj.get("auth_mode"):
                    auth_kind = "oauth"
                else:
                    auth_kind = "configured"
            elif re.search(r"requires_openai_auth\s*=\s*true", toml_text, re.IGNORECASE):
                auth_kind = "oauth"
        else:
            env_obj = parsed_cfg.get("env") if isinstance(parsed_cfg.get("env"), dict) else {}
            if isinstance(parsed_cfg.get("model"), str) and parsed_cfg.get("model"):
                model = parsed_cfg["model"].strip()
            elif isinstance(env_obj.get("ANTHROPIC_MODEL"), str) and env_obj.get("ANTHROPIC_MODEL"):
                model = env_obj["ANTHROPIC_MODEL"].strip()
            if isinstance(env_obj.get("ANTHROPIC_BASE_URL"), str) and env_obj.get("ANTHROPIC_BASE_URL"):
                base_url = sanitize_base_url(env_obj["ANTHROPIC_BASE_URL"])
            if env_obj.get("ANTHROPIC_AUTH_TOKEN"):
                auth_kind = "auth_token"
            elif env_obj.get("ANTHROPIC_API_KEY") or parsed_cfg.get("apiKeyHelper"):
                auth_kind = "api_key"

    return {
        "id": str(row.get("id") or ""),
        "app_type": app_type,
        "name": str(row.get("name") or ""),
        "is_current": bool(row.get("is_current")),
        "in_failover_queue": bool(row.get("in_failover_queue")),
        "sort_index": row.get("sort_index"),
        "category": str(row.get("category") or ""),
        "notes": str(row.get("notes") or ""),
        "base_url": base_url,
        "model": model,
        "auth_kind": auth_kind,
    }


def _fetch_provider_rows(
    harness: str | None = None,
    *,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    resolved_db = get_cc_switch_db_path(db_path)
    conn = _open_ro_sqlite(resolved_db)
    try:
        cur = conn.cursor()
        cols_info = cur.execute("PRAGMA table_info(providers)").fetchall()
        existing_cols = {str(r[1]) for r in cols_info}
        wanted = [
            "id",
            "app_type",
            "name",
            "settings_config",
            "website_url",
            "category",
            "sort_index",
            "notes",
            "is_current",
            "in_failover_queue",
        ]
        select_exprs = [col if col in existing_cols else f"NULL AS {col}" for col in wanted]
        sql = f"SELECT {', '.join(select_exprs)} FROM providers"
        params: list[Any] = []
        if harness:
            app_type = harness.strip().lower()
            sql += " WHERE LOWER(app_type) = ?"
            params.append(app_type)
        cur.execute(sql, params)
        rows: list[dict[str, Any]] = []
        for raw_row in cur.fetchall():
            rows.append(dict(zip(wanted, raw_row)))
        return rows
    finally:
        conn.close()


def list_cc_switch_providers(
    harness: str | None = None,
    *,
    queue_only: bool = False,
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """List sanitized CC Switch providers ordered by current -> sort_index -> name."""
    rows = _fetch_provider_rows(harness, db_path=db_path)
    items = [_summarize_provider_row(r) for r in rows]
    if queue_only:
        items = [it for it in items if it.get("in_failover_queue")]
    items.sort(
        key=lambda it: (
            0 if it.get("is_current") else 1,
            it["sort_index"] if isinstance(it.get("sort_index"), int) else 999999,
            it["name"].lower(),
            it["id"],
        )
    )
    return items


def resolve_provider(
    harness: str,
    selector: str,
    *,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Resolve a provider selector (id, exact name, id prefix, or unique substring)."""
    query = (selector or "").strip()
    if not query:
        raise ValueError(f"Empty provider selector for harness {harness!r}")
    items = list_cc_switch_providers(harness, db_path=db_path)
    if not items:
        raise ValueError(f"No CC Switch providers found for harness {harness!r}")

    # 1. Exact ID match
    for it in items:
        if it["id"] == query:
            return it

    # 2. Exact name match (case-insensitive)
    q_low = query.lower()
    exact_names = [it for it in items if (it["name"] or "").strip().lower() == q_low]
    if len(exact_names) == 1:
        return exact_names[0]
    if len(exact_names) > 1:
        opts = ", ".join(f"{it['name']}({it['id'][:8]})" for it in exact_names)
        raise ValueError(f"Ambiguous provider name {selector!r} for {harness}: {opts}")

    # 3. ID prefix match (>= 6 chars)
    if len(query) >= 6:
        pref = [it for it in items if it["id"].lower().startswith(q_low)]
        if len(pref) == 1:
            return pref[0]
        if len(pref) > 1:
            opts = ", ".join(f"{it['name']}({it['id'][:8]})" for it in pref)
            raise ValueError(f"Ambiguous provider ID prefix {selector!r}: {opts}")

    # 4. Unique substring match on name or notes
    subs = [it for it in items if q_low in (it["name"] or "").lower()]
    if len(subs) == 1:
        return subs[0]
    if len(subs) > 1:
        opts = ", ".join(f"{it['name']}({it['id'][:8]})" for it in subs)
        raise ValueError(f"Ambiguous provider selector {selector!r} for {harness}; matches: {opts}")

    avail = ", ".join(f"{it['name']}({it['id'][:8]})" for it in items if it.get("name"))
    raise ValueError(f"Provider {selector!r} not found for {harness}. Available: {avail}")


def expand_fallback_specs(
    harness: str,
    raw_specs: str | list[str] | tuple[str, ...],
    *,
    exclude_ids: tuple[str, ...] | list[str] = (),
    db_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Expand user-ordered fallback selectors and `@queue` into deduplicated provider summaries."""
    if isinstance(raw_specs, str):
        tokens = [tok.strip() for tok in raw_specs.split(",") if tok.strip()]
    else:
        tokens = [str(tok).strip() for tok in raw_specs if str(tok).strip()]

    excluded = {str(x).strip() for x in exclude_ids if str(x).strip()}
    seen_ids: set[str] = set()
    resolved: list[dict[str, Any]] = []

    for tok in tokens:
        if tok.lower() == "@queue":
            queued = list_cc_switch_providers(harness, queue_only=True, db_path=db_path)
            queued.sort(
                key=lambda it: (
                    it["sort_index"] if isinstance(it.get("sort_index"), int) else 999999,
                    it["name"].lower(),
                    it["id"],
                )
            )
            candidates = queued
        else:
            candidates = [resolve_provider(harness, tok, db_path=db_path)]
        for item in candidates:
            pid = item["id"]
            if pid in excluded or pid in seen_ids:
                continue
            seen_ids.add(pid)
            resolved.append(item)
    return resolved


def _load_provider_raw_config(
    harness: str,
    provider_id: str,
    *,
    db_path: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Internal helper returning (sanitized_summary, parsed_settings_config). Never log raw config."""
    rows = _fetch_provider_rows(harness, db_path=db_path)
    for row in rows:
        if str(row.get("id") or "") == provider_id:
            summary = _summarize_provider_row(row)
            raw_str = str(row.get("settings_config") or "{}")
            cfg = json.loads(raw_str) if raw_str.strip() else {}
            if not isinstance(cfg, dict):
                raise ValueError(f"Invalid settings_config structure for provider {provider_id!r}")
            return summary, cfg
    raise ValueError(f"Provider ID {provider_id!r} not found for {harness}")


def _write_secret_file_0600(path: Path, content: str) -> None:
    """Atomically write sensitive runtime config with strict 0600 permissions inside 0700 dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _is_claude_provider_env_key(key: str) -> bool:
    up = (key or "").strip().upper()
    return any(up.startswith(pref) for pref in CLAUDE_PROVIDER_ENV_PREFIXES) or up in CLAUDE_PROVIDER_ENV_KEYS


def materialize_claude_runtime(
    provider_id: str,
    run_slug: str,
    job_id: str,
    *,
    db_path: Path | str | None = None,
    user_settings_path: Path | str | None = None,
    runtime_root: Path | str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Synthesize an isolated 0600 Claude Code settings JSON for a CC Switch provider."""
    summary, target_cfg = _load_provider_raw_config("claude", provider_id, db_path=db_path)

    base_path = Path(user_settings_path).expanduser() if user_settings_path else (Path.home() / ".claude" / "settings.json")
    base_cfg: dict[str, Any] = {}
    if base_path.exists():
        try:
            loaded = json.loads(base_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                base_cfg = loaded
        except (OSError, json.JSONDecodeError):
            base_cfg = {}

    merged: dict[str, Any] = {}
    for key in CLAUDE_SAFE_TOP_LEVEL_DEFAULTS:
        if key in base_cfg:
            merged[key] = base_cfg[key]

    for key, val in target_cfg.items():
        if key in ("env", "model"):
            continue
        merged[key] = val

    clean_env: dict[str, str] = {}
    base_env = base_cfg.get("env") if isinstance(base_cfg.get("env"), dict) else {}
    for k, v in base_env.items():
        if _is_claude_provider_env_key(str(k)):
            continue
        if v is not None:
            clean_env[str(k)] = str(v)

    target_env = target_cfg.get("env") if isinstance(target_cfg.get("env"), dict) else {}
    for k, v in target_env.items():
        if v is not None:
            clean_env[str(k)] = str(v)
    merged["env"] = clean_env

    # Never inherit old model when target provider specifies its own model or ANTHROPIC_MODEL
    if isinstance(target_cfg.get("model"), str) and target_cfg["model"].strip():
        merged["model"] = target_cfg["model"].strip()
    elif clean_env.get("ANTHROPIC_MODEL"):
        merged["model"] = clean_env["ANTHROPIC_MODEL"].strip()

    root = get_runtime_root(runtime_root)
    job_runtime_dir = root / sanitize_slug(run_slug) / sanitize_slug(job_id, max_len=64)
    settings_file = job_runtime_dir / "claude-settings.json"
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        (root / sanitize_slug(run_slug)).mkdir(parents=True, exist_ok=True)
        os.chmod(root / sanitize_slug(run_slug), 0o700)
        _write_secret_file_0600(settings_file, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")

    return {
        "harness": "claude",
        "settings_file": str(settings_file),
        "runtime_dir": str(job_runtime_dir),
        "env_overrides": {},
        "env_unset": list(CLAUDE_SHELL_ENV_UNSET),
        "provider": summary,
    }


def materialize_codex_runtime(
    provider_id: str,
    run_slug: str,
    job_id: str,
    workdir: Path | str,
    *,
    db_path: Path | str | None = None,
    base_codex_home: Path | str | None = None,
    runtime_root: Path | str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Synthesize an isolated 0700/0600 CODEX_HOME for a CC Switch Codex provider."""
    summary, target_cfg = _load_provider_raw_config("codex", provider_id, db_path=db_path)

    base_home = Path(base_codex_home).expanduser() if base_codex_home else (Path.home() / ".codex")
    root = get_runtime_root(runtime_root)
    job_runtime_dir = root / sanitize_slug(run_slug) / sanitize_slug(job_id, max_len=64)
    codex_home = job_runtime_dir / "codex_home"

    workdir_str = str(Path(workdir).resolve())
    toml_text = str(target_cfg.get("config") or "").rstrip()
    trust_header = f'[projects."{workdir_str}"]'
    if trust_header not in toml_text:
        addition = f'\n\n{trust_header}\ntrust_level = "trusted"\n'
        toml_text = (toml_text + addition).lstrip("\n")
    else:
        toml_text = toml_text + "\n"

    auth_obj = target_cfg.get("auth")

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        (root / sanitize_slug(run_slug)).mkdir(parents=True, exist_ok=True)
        os.chmod(root / sanitize_slug(run_slug), 0o700)
        job_runtime_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(job_runtime_dir, 0o700)
        codex_home.mkdir(parents=True, exist_ok=True)
        os.chmod(codex_home, 0o700)

        _write_secret_file_0600(codex_home / "config.toml", toml_text)

        auth_dst = codex_home / "auth.json"
        if auth_dst.exists() or auth_dst.is_symlink():
            auth_dst.unlink()
        if isinstance(auth_obj, dict) and len(auth_obj) > 0:
            _write_secret_file_0600(auth_dst, json.dumps(auth_obj, ensure_ascii=False, indent=2) + "\n")
        elif (base_home / "auth.json").exists():
            os.symlink(str((base_home / "auth.json").resolve()), str(auth_dst))

        if base_home.exists():
            (base_home / "sessions").mkdir(parents=True, exist_ok=True)
        for item_name in CODEX_SYMLINK_ITEMS:
            src = base_home / item_name
            dst = codex_home / item_name
            if not src.exists():
                continue
            if dst.exists() or dst.is_symlink():
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                else:
                    continue
            os.symlink(str(src.resolve()), str(dst))

    return {
        "harness": "codex",
        "codex_home": str(codex_home),
        "runtime_dir": str(job_runtime_dir),
        "env_overrides": {"CODEX_HOME": str(codex_home)},
        "env_unset": list(CODEX_ENV_UNSET),
        "provider": summary,
    }


def materialize_provider_runtime(
    harness: str,
    provider_id: str,
    run_slug: str,
    job_id: str,
    workdir: Path | str,
    *,
    db_path: Path | str | None = None,
    runtime_root: Path | str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Materialize isolated runtime config for `claude` or `codex`; raise if unsupported."""
    h = (harness or "").strip().lower()
    if h == "claude":
        return materialize_claude_runtime(
            provider_id,
            run_slug,
            job_id,
            db_path=db_path,
            runtime_root=runtime_root,
            dry_run=dry_run,
        )
    if h == "codex":
        return materialize_codex_runtime(
            provider_id,
            run_slug,
            job_id,
            workdir,
            db_path=db_path,
            runtime_root=runtime_root,
            dry_run=dry_run,
        )
    raise ValueError(f"Runtime provider isolation is supported for claude/codex, not {harness!r}")


def _wipe_and_unlink_tree(path: Path) -> None:
    """Overwrite secret files before unlinking files/symlinks and empty dirs without shell rm."""
    if path.is_symlink() or path.is_file():
        if path.is_file() and not path.is_symlink() and path.name in ("claude-settings.json", "auth.json", "config.toml"):
            try:
                path.write_text("", encoding="utf-8")
            except OSError:
                pass
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        for child in list(path.iterdir()):
            _wipe_and_unlink_tree(child)
        path.rmdir()


def cleanup_runtime_dir(runtime_dir: Path | str, *, runtime_root: Path | str | None = None) -> bool:
    """Securely remove a single job runtime directory strictly inside runtime_root."""
    root = get_runtime_root(runtime_root).resolve()
    target = Path(runtime_dir).expanduser().resolve()
    if not target.exists():
        return False
    if target == root or not target.is_relative_to(root):
        raise ValueError(f"Refusing to cleanup runtime directory outside {root}: {target}")
    _wipe_and_unlink_tree(target)
    return True


def cleanup_run_runtimes(
    *,
    run_slug: str = "",
    clean_all: bool = False,
    runtime_root: Path | str | None = None,
) -> list[str]:
    """Clean up runtime directories for one run slug or the whole runtime root."""
    root = get_runtime_root(runtime_root).resolve()
    if not root.exists():
        return []
    removed: list[str] = []
    if clean_all:
        for child in list(root.iterdir()):
            if child.is_dir() or child.is_symlink():
                _wipe_and_unlink_tree(child)
                removed.append(str(child))
        return removed
    if not run_slug:
        return []
    target = (root / sanitize_slug(run_slug)).resolve()
    if target.exists() and target != root and target.is_relative_to(root):
        _wipe_and_unlink_tree(target)
        removed.append(str(target))
    return removed


def _run_git_ro(workdir: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def build_session_checkpoint(
    *,
    job_id: str,
    harness: str,
    workdir: Path,
    branch: str,
    base_commit: str,
    session_id: str,
    from_provider: dict[str, Any] | str,
    to_provider: dict[str, Any] | str,
    reason: str,
    original_prompt_file: str = "",
    original_prompt_text: str = "",
    recent_turns: list[dict[str, Any]] | None = None,
    last_stop_reason: str = "",
    stdout_tail: str = "",
) -> dict[str, Any]:
    """Build a zero-token deterministic checkpoint payload from local git and JSONL state."""
    from_label = (
        from_provider.get("name") or from_provider.get("id")
        if isinstance(from_provider, dict)
        else str(from_provider or "默认配置")
    )
    to_label = (
        to_provider.get("name") or to_provider.get("id")
        if isinstance(to_provider, dict)
        else str(to_provider or "")
    )
    head_short = _run_git_ro(workdir, ["rev-parse", "--short", "HEAD"]) or "NO_GIT"
    commits_raw = (
        _run_git_ro(workdir, ["log", f"{base_commit}..HEAD", "--oneline"])
        if base_commit and base_commit != "NO_GIT"
        else ""
    )
    commits = [line.strip() for line in commits_raw.splitlines() if line.strip()][:20]
    status_lines = [
        line.rstrip() for line in _run_git_ro(workdir, ["status", "--short"]).splitlines() if line.strip()
    ][:60]
    diff_stat = _run_git_ro(workdir, ["diff", "--stat"])
    cached_stat = _run_git_ro(workdir, ["diff", "--cached", "--stat"])

    prompt_text = original_prompt_text
    if not prompt_text and original_prompt_file:
        p = Path(original_prompt_file)
        if p.exists():
            try:
                prompt_text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                prompt_text = ""
    prompt_excerpt = redact_secret_like_strings(prompt_text[:1500].strip())

    clean_turns: list[dict[str, Any]] = []
    for turn in (recent_turns or [])[-6:]:
        if not isinstance(turn, dict):
            continue
        txt = str(turn.get("text") or turn.get("text_preview") or "").strip()
        txt = redact_secret_like_strings(txt[:500])
        clean_turns.append(
            {
                "role": str(turn.get("role") or ""),
                "timestamp": str(turn.get("timestamp") or ""),
                "tools": [str(t) for t in (turn.get("tools") or [])[:8]],
                "summary": txt,
            }
        )

    return {
        "captured_at": now_iso(),
        "job_id": job_id,
        "harness": harness,
        "workdir": str(workdir),
        "branch": branch,
        "base_commit": base_commit,
        "head_commit": head_short,
        "session_id": session_id,
        "from_provider": from_label,
        "to_provider": to_label,
        "reason": reason,
        "original_prompt_file": original_prompt_file,
        "original_prompt_excerpt": prompt_excerpt,
        "commits_since_base": commits,
        "git_status_short": status_lines,
        "git_diff_stat": diff_stat,
        "git_cached_diff_stat": cached_stat,
        "last_stop_reason": last_stop_reason,
        "recent_turns": clean_turns,
        "stdout_tail": redact_secret_like_strings((stdout_tail or "")[-1200:].strip()),
    }


def render_checkpoint_markdown(cp: dict[str, Any]) -> str:
    """Render deterministic checkpoint Markdown (used both as artifact and Tier-2 handoff prompt)."""
    lines: list[str] = [
        f"# 会话断点恢复交接单（{cp.get('job_id', '-')}）",
        "",
        "你正在接手一个因上游故障或手动切换而中断的会话，请**先核对工作区现状，不要重复已完成改动**，从断点继续推进并完成验证。",
        "",
        "## 1. 切换与工作区现场",
        f"- **Harness**：`{cp.get('harness', '-')}`",
        f"- **上游切换**：`{cp.get('from_provider', '-')}` → `{cp.get('to_provider', '-')}`",
        f"- **切换原因**：{cp.get('reason') or '未说明'}",
        f"- **原会话 ID**：`{cp.get('session_id') or '无'}`",
        f"- **工作目录 / 分支**：`{cp.get('workdir', '-')}` (`{cp.get('branch') or '-'}`)",
        f"- **Git 基线 → 当前**：`{cp.get('base_commit') or '-'}` → `{cp.get('head_commit') or '-'}`",
    ]
    if cp.get("original_prompt_file"):
        lines.append(f"- **原始任务文件**：`{cp['original_prompt_file']}`（完整需求以该文件为准）")

    commits = cp.get("commits_since_base") or []
    lines.extend(["", "## 2. 已落地提交（勿重复提交）"])
    if commits:
        lines.extend(f"- `{c}`" for c in commits)
    else:
        lines.append("- _尚无相对基线的新提交_")

    status_lines = cp.get("git_status_short") or []
    lines.extend(["", "## 3. 工作区未提交改动（`git status --short`）"])
    if status_lines:
        lines.extend(["```text", *status_lines, "```"])
    else:
        lines.append("- _工作区干净_")

    diff_stat = (cp.get("git_diff_stat") or "").strip()
    cached_stat = (cp.get("git_cached_diff_stat") or "").strip()
    if diff_stat or cached_stat:
        lines.extend(["", "## 4. 改动范围摘要（`git diff --stat`）", "```text"])
        if cached_stat:
            lines.append("[staged]\n" + cached_stat)
        if diff_stat:
            lines.append("[unstaged]\n" + diff_stat)
        lines.append("```")

    turns = cp.get("recent_turns") or []
    if turns:
        lines.extend(["", "## 5. 切换前最后进展（JSONL 摘要）"])
        for t in turns:
            role = t.get("role") or "?"
            ts = t.get("timestamp") or ""
            tools = f" [tools: {', '.join(t['tools'])}]" if t.get("tools") else ""
            lines.append(f"- **{role}** ({ts}){tools}: {t.get('summary') or ''}")

    tail = (cp.get("stdout_tail") or "").strip()
    if tail:
        lines.extend(["", "## 6. 中断前最后输出/报错", "```text", tail, "```"])

    if cp.get("original_prompt_excerpt"):
        lines.extend(["", "## 7. 原任务摘要（完整要求请读取原任务文件）", "```text", cp["original_prompt_excerpt"], "```"])

    lines.extend(
        [
            "",
            "## 8. 续作指令",
            "1. 运行 `git status` 与 `git diff` 确认现场（若任务文件存在请先对照验收标准）；",
            "2. 从中断位置继续完成剩余修改、测试与本地提交；",
            "3. 禁止重做已通过验证的步骤。",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_native_resume_prompt(cp: dict[str, Any], checkpoint_md_path: Path | str) -> str:
    """Build concise Tier-1 native `--resume` continuation instruction."""
    from_p = cp.get("from_provider") or "原上游"
    to_p = cp.get("to_provider") or "当前上游"
    reason = cp.get("reason") or "上游切换"
    return (
        f"已将当前会话从 `{from_p}` 切换至 `{to_p}`（原因：{reason}），"
        f"工作区与会话断点快照已保存至 `{checkpoint_md_path}`。\n"
        "请先用 `git status` / `git diff` 核对当前工作区改动，不要重复已完成步骤，从断点继续完成剩余任务与验证。"
    )
