#!/usr/bin/env python3
"""Quota-aware multi-harness probe adapter and routing engine for harness-loop.

Reuses open-source quota CLI probes (`quota-axi`, `codexbar`, `cclimits`) when
present, overlays read-only local CC Switch provider health/limits/spend and
native Codex session rate-limit telemetry (never reading API keys or OAuth
tokens), and ranks harnesses according to `--conserve`, `--prefer`, and
burn-priority strategies (`expiring_first`, `abundant_first`, `auto`, `manual`).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HARNESSES = ("agy", "claude", "codex")
PROBE_CHOICES = ("auto", "quota-axi", "codexbar", "cclimits", "local")
STRATEGY_CHOICES = ("auto", "expiring_first", "abundant_first", "manual")

SNAPSHOT_GLOBAL_REL = Path(".config") / "harness-loop" / "quota-snapshot.json"
SNAPSHOT_REPO_REL = Path(".agent-loop") / "quota-snapshot.json"

_DURATION_RE = re.compile(
    r"(?:(\d+(?:\.\d+)?)\s*d)?\s*(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?",
    re.IGNORECASE,
)


def _now_iso(ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).astimezone()
    return dt.isoformat(timespec="seconds")


def _parse_iso_or_epoch(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        return num / 1000.0 if num > 1e12 else num
    raw = str(val).strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        num = float(raw)
        return num / 1000.0 if num > 1e12 else num
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt.timestamp()
    except ValueError:
        return None


def parse_duration_to_hours(spec: Any, *, now_epoch: float | None = None) -> float | None:
    """Parse duration strings ('3d17h', '2h 15m', '45m') or timestamps to remaining hours."""
    if spec is None or spec == "":
        return None
    now = time.time() if now_epoch is None else float(now_epoch)
    ts = _parse_iso_or_epoch(spec)
    if ts is not None:
        return max(0.0, (ts - now) / 3600.0)
    raw = str(spec).strip()
    m = _DURATION_RE.fullmatch(raw)
    if not m or not any(m.groups()):
        return None
    days = float(m.group(1) or 0.0)
    hours = float(m.group(2) or 0.0)
    mins = float(m.group(3) or 0.0)
    secs = float(m.group(4) or 0.0)
    return days * 24.0 + hours + mins / 60.0 + secs / 3600.0


def _map_provider_name(raw: str) -> str | None:
    key = (raw or "").strip().lower()
    if key in ("claude", "claude-code", "claude_code"):
        return "claude"
    if key in ("codex", "openai-codex", "openai_codex"):
        return "codex"
    if key in ("agy", "antigravity", "antigravity-cli"):
        return "agy"
    return None


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(val)))


def _to_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool) or val == "":
        return None
    try:
        num = float(val)
        return num if math.isfinite(num) else None
    except (TypeError, ValueError):
        return None


def _window_hours_from_kind(win_id: str, kind: str = "", window_minutes: float | None = None) -> float:
    if window_minutes and window_minutes > 0:
        return float(window_minutes) / 60.0
    txt = f"{win_id} {kind}".lower()
    if "5h" in txt or "five_hour" in txt or "session" in txt:
        return 5.0
    if "7d" in txt or "seven_day" in txt or "week" in txt:
        return 168.0
    if "month" in txt or "30d" in txt or txt.endswith("mo"):
        return 720.0
    return 24.0


# ── Open-Source Probe JSON Adapters ───────────────────────────────────────


def parse_quota_axi_json(payload: Any, *, now_epoch: float | None = None) -> dict[str, dict[str, Any]]:
    """Parse `quota-axi --json` (schemaVersion 5) without inventing unobserved numbers."""
    now = time.time() if now_epoch is None else float(now_epoch)
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for item in payload["providers"]:
        if not isinstance(item, dict):
            continue
        harness = _map_provider_name(str(item.get("provider") or ""))
        if not harness:
            continue

        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        windows_raw = item.get("windows") if isinstance(item.get("windows"), list) else []
        windows_by_id: dict[str, dict[str, Any]] = {}
        norm_windows: list[dict[str, Any]] = []
        for w in windows_raw:
            if not isinstance(w, dict):
                continue
            wid = str(w.get("id") or w.get("label") or "")
            rem = _to_float(w.get("percentRemaining"))
            resets_at = w.get("resetsAt")
            resets_ep = _parse_iso_or_epoch(resets_at)
            hrs_reset = max(0.0, (resets_ep - now) / 3600.0) if resets_ep is not None else None
            win_hours = _window_hours_from_kind(wid, str(w.get("kind") or ""))
            entry = {
                "id": wid,
                "label": str(w.get("label") or wid),
                "kind": str(w.get("kind") or ""),
                "remaining_pct": rem,
                "used_pct": round(100.0 - rem, 2) if rem is not None else None,
                "resets_at": str(resets_at) if resets_at else None,
                "hours_to_reset": round(hrs_reset, 3) if hrs_reset is not None else None,
                "window_hours": win_hours,
            }
            norm_windows.append(entry)
            if wid:
                windows_by_id[wid] = entry

        qsem = item.get("quotaSemantics") if isinstance(item.get("quotaSemantics"), dict) else {}
        eff_list = qsem.get("effectiveAvailability") if isinstance(qsem.get("effectiveAvailability"), list) else []

        known_eff = [
            e for e in eff_list if isinstance(e, dict) and e.get("status") == "known"
        ]
        chosen = next((e for e in known_eff if e.get("scope") == "all_models"), None)
        if chosen is None and known_eff:
            # Prefer highest remaining headroom or spendPriority when a provider reports per-family scopes (e.g. agy).
            chosen = max(
                known_eff,
                key=lambda e: (
                    _to_float((e.get("selection") or {}).get("spendPriority")) or -999.0,
                    _to_float(e.get("effectivePercentRemaining")) or -1.0,
                ),
            )

        rem_pct: float | None = None
        spend_priority: float | None = None
        runway_hours: float | None = None
        runway_status: str | None = None
        limiting_win: dict[str, Any] | None = None

        if chosen:
            rem_pct = _to_float(chosen.get("effectivePercentRemaining"))
            sel = chosen.get("selection") if isinstance(chosen.get("selection"), dict) else {}
            spend_priority = _to_float(sel.get("spendPriority"))
            runway = chosen.get("runway") if isinstance(chosen.get("runway"), dict) else {}
            runway_status = str(runway.get("status") or "") or None
            rw_sec = _to_float(runway.get("usableRunwaySeconds"))
            if rw_sec is not None:
                runway_hours = round(rw_sec / 3600.0, 3)
            lim_ids = chosen.get("limitingWindowIds") if isinstance(chosen.get("limitingWindowIds"), list) else []
            for lid in lim_ids:
                if lid in windows_by_id:
                    limiting_win = windows_by_id[lid]
                    break

        if limiting_win is None and norm_windows:
            with_rem = [w for w in norm_windows if w.get("remaining_pct") is not None]
            if with_rem:
                limiting_win = min(with_rem, key=lambda w: w["remaining_pct"])
            else:
                limiting_win = norm_windows[0]

        if rem_pct is None and limiting_win:
            rem_pct = limiting_win.get("remaining_pct")

        scopes_summary: dict[str, Any] = {}
        for eff in known_eff:
            sc = str(eff.get("scope") or "")
            if sc:
                scopes_summary[sc] = {
                    "remaining_pct": _to_float(eff.get("effectivePercentRemaining")),
                    "spend_priority": _to_float((eff.get("selection") or {}).get("spendPriority")),
                }

        out[harness] = {
            "source": "quota-axi",
            "state": str(state.get("status") or "unknown"),
            "plan": str(item.get("plan") or "") or None,
            "remaining_pct": rem_pct,
            "used_pct": round(100.0 - rem_pct, 2) if rem_pct is not None else None,
            "spend_priority": spend_priority,
            "runway_status": runway_status,
            "runway_hours": runway_hours,
            "resets_at": limiting_win.get("resets_at") if limiting_win else None,
            "hours_to_reset": limiting_win.get("hours_to_reset") if limiting_win else None,
            "window_hours": limiting_win.get("window_hours") if limiting_win else None,
            "windows": norm_windows,
            "scopes": scopes_summary or None,
        }
    return out


def parse_codexbar_json(payload: Any, *, now_epoch: float | None = None) -> dict[str, dict[str, Any]]:
    """Parse `codexbar usage --format json` output."""
    now = time.time() if now_epoch is None else float(now_epoch)
    items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        items = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        if isinstance(payload.get("providers"), list):
            items = [x for x in payload["providers"] if isinstance(x, dict)]
        elif "provider" in payload or "usage" in payload:
            items = [payload]

    out: dict[str, dict[str, Any]] = {}
    for item in items:
        harness = _map_provider_name(str(item.get("provider") or item.get("id") or ""))
        if not harness:
            continue
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else item
        norm_windows: list[dict[str, Any]] = []
        for slot in ("primary", "secondary"):
            w = usage.get(slot)
            if not isinstance(w, dict):
                continue
            used = _to_float(w.get("usedPercent"))
            rem = _to_float(w.get("remainingPercent"))
            if rem is None and used is not None:
                rem = _clamp(100.0 - used, 0.0, 100.0)
            wmin = _to_float(w.get("windowMinutes"))
            resets_at = w.get("resetsAt")
            hrs_reset = parse_duration_to_hours(resets_at, now_epoch=now)
            norm_windows.append(
                {
                    "id": slot,
                    "label": slot,
                    "remaining_pct": rem,
                    "used_pct": round(100.0 - rem, 2) if rem is not None else used,
                    "resets_at": str(resets_at) if resets_at is not None else None,
                    "hours_to_reset": round(hrs_reset, 3) if hrs_reset is not None else None,
                    "window_hours": _window_hours_from_kind(slot, window_minutes=wmin),
                }
            )
        if not norm_windows:
            continue
        with_rem = [w for w in norm_windows if w.get("remaining_pct") is not None]
        tightest = min(with_rem, key=lambda w: w["remaining_pct"]) if with_rem else norm_windows[0]
        rc = usage.get("resetCredits") if isinstance(usage.get("resetCredits"), dict) else {}
        expires_at = rc.get("nextExpiresAt")
        out[harness] = {
            "source": "codexbar",
            "state": "fresh" if item.get("ok", True) else "error",
            "plan": str(item.get("plan") or usage.get("planName") or "") or None,
            "remaining_pct": tightest.get("remaining_pct"),
            "used_pct": tightest.get("used_pct"),
            "resets_at": tightest.get("resets_at"),
            "hours_to_reset": tightest.get("hours_to_reset"),
            "window_hours": tightest.get("window_hours"),
            "expires_at": str(expires_at) if expires_at else None,
            "hours_to_expiry": parse_duration_to_hours(expires_at, now_epoch=now),
            "windows": norm_windows,
        }
    return out


def _parse_money(val: Any) -> float | None:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    if isinstance(val, str):
        cleaned = re.sub(r"[^0-9.-]", "", val)
        return _to_float(cleaned)
    return None


def parse_cclimits_json(payload: Any, *, now_epoch: float | None = None) -> dict[str, dict[str, Any]]:
    """Parse `cclimits --json` or generic per-harness normalized quota dicts."""
    now = time.time() if now_epoch is None else float(now_epoch)
    if not isinstance(payload, dict):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for raw_k, entry in payload.items():
        harness = _map_provider_name(raw_k)
        if not harness or not isinstance(entry, dict):
            continue
        if entry.get("ok") is False:
            out[harness] = {"source": "cclimits", "state": "error", "error": entry.get("error")}
            continue

        norm_windows: list[dict[str, Any]] = []
        resets_map = entry.get("resets") if isinstance(entry.get("resets"), dict) else {}
        limits = entry.get("limits") if isinstance(entry.get("limits"), list) else []
        for lim in limits:
            if not isinstance(lim, dict):
                continue
            lbl = str(lim.get("label") or lim.get("id") or "window")
            rem = _to_float(lim.get("remaining_percent") or lim.get("remaining_pct"))
            used = _to_float(lim.get("percent") or lim.get("used_percent"))
            if rem is None and used is not None:
                rem = _clamp(100.0 - used, 0.0, 100.0)
            res_spec = lim.get("resets_at") or lim.get("resets_in") or resets_map.get(lbl)
            hrs_reset = parse_duration_to_hours(res_spec, now_epoch=now)
            norm_windows.append(
                {
                    "id": lbl,
                    "label": lbl,
                    "remaining_pct": rem,
                    "used_pct": round(100.0 - rem, 2) if rem is not None else used,
                    "resets_at": str(res_spec) if res_spec is not None else None,
                    "hours_to_reset": round(hrs_reset, 3) if hrs_reset is not None else None,
                    "window_hours": _window_hours_from_kind(lbl),
                }
            )

        models = entry.get("models") if isinstance(entry.get("models"), dict) else {}
        for m_name, m_val in models.items():
            if not isinstance(m_val, dict):
                continue
            used = _to_float(m_val.get("percent"))
            rem = _clamp(100.0 - used, 0.0, 100.0) if used is not None else None
            norm_windows.append(
                {
                    "id": m_name,
                    "label": m_name,
                    "remaining_pct": rem,
                    "used_pct": used,
                    "resets_at": None,
                    "hours_to_reset": None,
                    "window_hours": 24.0,
                }
            )

        rem_direct = _to_float(entry.get("remaining_pct") or entry.get("remaining_percent"))
        used_direct = _to_float(entry.get("used_pct") or entry.get("used_percent"))
        if rem_direct is None and used_direct is not None:
            rem_direct = _clamp(100.0 - used_direct, 0.0, 100.0)

        tightest: dict[str, Any] | None = None
        if norm_windows:
            with_rem = [w for w in norm_windows if w.get("remaining_pct") is not None]
            tightest = min(with_rem, key=lambda w: w["remaining_pct"]) if with_rem else norm_windows[0]
            if rem_direct is None:
                rem_direct = tightest.get("remaining_pct")

        res_spec = (
            (tightest.get("resets_at") if tightest else None)
            or entry.get("resets_at")
            or entry.get("resets_in")
        )
        hrs_reset = (
            tightest.get("hours_to_reset")
            if tightest and tightest.get("hours_to_reset") is not None
            else parse_duration_to_hours(res_spec, now_epoch=now)
        )
        exp_spec = entry.get("expires_at")
        hrs_exp = parse_duration_to_hours(exp_spec, now_epoch=now)

        out[harness] = {
            "source": "cclimits",
            "state": "fresh",
            "plan": str(entry.get("plan") or "") or None,
            "remaining_pct": rem_direct,
            "used_pct": round(100.0 - rem_direct, 2) if rem_direct is not None else used_direct,
            "resets_at": str(res_spec) if res_spec is not None else None,
            "hours_to_reset": hrs_reset,
            "window_hours": (tightest.get("window_hours") if tightest else None)
            or _to_float(entry.get("window_hours"))
            or 24.0,
            "expires_at": str(exp_spec) if exp_spec is not None else None,
            "hours_to_expiry": hrs_exp,
            "balance_usd": _parse_money(entry.get("balance") or entry.get("balance_usd")),
            "windows": norm_windows,
        }
    return out


def parse_external_probe_json(payload: Any, *, now_epoch: float | None = None) -> dict[str, dict[str, Any]]:
    """Auto-detect external probe JSON shape (`quota-axi`, `codexbar`, or `cclimits`/custom)."""
    if isinstance(payload, dict) and isinstance(payload.get("providers"), list):
        first = payload["providers"][0] if payload["providers"] else {}
        if isinstance(first, dict) and ("quotaSemantics" in first or "schemaVersion" in payload):
            return parse_quota_axi_json(payload, now_epoch=now_epoch)
        return parse_codexbar_json(payload, now_epoch=now_epoch)
    if isinstance(payload, list):
        return parse_codexbar_json(payload, now_epoch=now_epoch)
    if isinstance(payload, dict):
        if "usage" in payload and "provider" in payload:
            return parse_codexbar_json(payload, now_epoch=now_epoch)
        return parse_cclimits_json(payload, now_epoch=now_epoch)
    return {}


def _run_json_cmd(argv: list[str], timeout: int = 20) -> tuple[bool, Any, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, None, str(exc)
    if proc.returncode != 0 and not proc.stdout.strip():
        return False, None, (proc.stderr or f"exit={proc.returncode}").strip()
    raw = proc.stdout.strip()
    if not raw:
        return False, None, "empty output"
    try:
        return True, json.loads(raw), ""
    except json.JSONDecodeError as exc:
        return False, None, f"invalid json: {exc}"


def run_external_probe(
    mode: str = "auto",
    *,
    probe_cmd: str = "",
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Run an open-source probe CLI or custom JSON probe command safely."""
    custom = (probe_cmd or os.environ.get("HARNESS_LOOP_QUOTA_CMD") or "").strip()
    if custom:
        ok, data, err = _run_json_cmd(shlex.split(custom))
        parsed = parse_external_probe_json(data, now_epoch=now_epoch) if ok else {}
        return {"probe": "custom", "ok": ok and bool(parsed), "error": err, "harnesses": parsed}

    selected = (mode or "auto").strip().lower()
    if selected == "local":
        return {"probe": "local", "ok": True, "error": "", "harnesses": {}}

    q_bin = shutil.which("quota-axi")
    cb_bin = shutil.which("codexbar")
    cc_bin = shutil.which("cclimits")

    if selected == "auto":
        # Never run `npx` unexpectedly without explicit request; only invoke locally installed tools.
        if q_bin:
            selected = "quota-axi"
        elif cb_bin:
            selected = "codexbar"
        elif cc_bin:
            selected = "cclimits"
        else:
            return {"probe": "local", "ok": True, "error": "no external probe binary installed", "harnesses": {}}

    argv: list[str] = []
    if selected == "quota-axi":
        if q_bin:
            argv = [q_bin, "--provider", "claude,codex,agy", "--json"]
        elif shutil.which("npx"):
            argv = ["npx", "-y", "quota-axi", "--provider", "claude,codex,agy", "--json"]
        else:
            return {"probe": "quota-axi", "ok": False, "error": "quota-axi / npx not found", "harnesses": {}}
    elif selected == "codexbar":
        if not cb_bin:
            return {"probe": "codexbar", "ok": False, "error": "codexbar not found", "harnesses": {}}
        argv = [cb_bin, "usage", "--format", "json"]
    elif selected == "cclimits":
        if cc_bin:
            argv = [cc_bin, "--json"]
        elif shutil.which("npx"):
            argv = ["npx", "-y", "cclimits", "--json"]
        else:
            return {"probe": "cclimits", "ok": False, "error": "cclimits / npx not found", "harnesses": {}}
    else:
        return {"probe": selected, "ok": False, "error": f"unknown probe mode: {selected}", "harnesses": {}}

    ok, data, err = _run_json_cmd(argv)
    parsed = parse_external_probe_json(data, now_epoch=now_epoch) if ok else {}
    return {"probe": selected, "ok": ok and bool(parsed), "error": err, "harnesses": parsed}


# ── Read-Only Local CC Switch & Native Telemetry ──────────────────────────


def read_cc_switch_overlay(
    *,
    cc_switch_dir: Path | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Read non-secret provider metadata, health, USD limits, spend, and Codex quota protection from CC Switch."""
    root = (cc_switch_dir or (Path.home() / ".cc-switch")).expanduser()
    now = time.time() if now_epoch is None else float(now_epoch)
    result: dict[str, Any] = {
        "present": root.exists(),
        "db_present": (root / "cc-switch.db").exists(),
        "codex_quota_protection": {"enabled": False, "fiveHourThreshold": 95, "weeklyThreshold": 98},
        "harnesses": {h: {} for h in HARNESSES},
    }

    settings_file = root / "settings.json"
    if settings_file.exists():
        try:
            sdata = json.loads(settings_file.read_text(encoding="utf-8"))
            cqp = sdata.get("codexQuotaProtection")
            if isinstance(cqp, dict):
                result["codex_quota_protection"] = {
                    "enabled": bool(cqp.get("enabled", False)),
                    "fiveHourThreshold": float(cqp.get("fiveHourThreshold", 95)),
                    "weeklyThreshold": float(cqp.get("weeklyThreshold", 98)),
                }
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    db_path = root / "cc-switch.db"
    if not db_path.exists():
        return result

    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        cur = con.cursor()
        # Explicitly omit `settings_config` so API keys / credentials are never read.
        cur.execute(
            """
            SELECT id, app_type, name, category, cost_multiplier, limit_daily_usd, limit_monthly_usd, meta
            FROM providers
            WHERE is_current = 1 AND app_type IN ('agy', 'claude', 'codex')
            """
        )
        current_ids: dict[str, str] = {}
        for pid, app_type, name, category, cm_raw, ld_raw, lm_raw, meta_raw in cur.fetchall():
            if app_type not in result["harnesses"]:
                continue
            current_ids[app_type] = str(pid)
            usage_meta: dict[str, Any] = {}
            try:
                meta = json.loads(meta_raw or "{}")
                us = meta.get("usage_script") if isinstance(meta, dict) else None
                if isinstance(us, dict) and us.get("enabled"):
                    usage_meta = {
                        "enabled": True,
                        "templateType": us.get("templateType"),
                        "codingPlanProvider": us.get("codingPlanProvider"),
                        "autoQueryInterval": us.get("autoQueryInterval"),
                    }
            except (json.JSONDecodeError, TypeError):
                pass
            result["harnesses"][app_type] = {
                "provider_name": str(name or ""),
                "category": str(category or "") or None,
                "cost_multiplier": _to_float(cm_raw) or 1.0,
                "limit_daily_usd": _to_float(ld_raw),
                "limit_monthly_usd": _to_float(lm_raw),
                "usage_script": usage_meta or None,
                "is_healthy": 1,
                "consecutive_failures": 0,
            }

        try:
            cur.execute(
                """
                SELECT provider_id, app_type, is_healthy, consecutive_failures, last_error
                FROM provider_health
                WHERE app_type IN ('agy', 'claude', 'codex')
                """
            )
            for pid, app_type, is_healthy, fails, last_err in cur.fetchall():
                if current_ids.get(app_type) == str(pid):
                    slot = result["harnesses"][app_type]
                    slot["is_healthy"] = int(is_healthy if is_healthy is not None else 1)
                    slot["consecutive_failures"] = int(fails or 0)
                    if last_err:
                        slot["last_error"] = str(last_err)[:200]
        except sqlite3.Error:
            pass

        try:
            cut_5h = int(now - 5 * 3600)
            cut_24h = int(now - 24 * 3600)
            cut_7d = int(now - 7 * 86400)
            cur.execute(
                """
                SELECT app_type,
                       SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END),
                       SUM(CASE WHEN created_at >= ? THEN CAST(total_cost_usd AS REAL) ELSE 0.0 END),
                       SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END),
                       SUM(CASE WHEN created_at >= ? THEN CAST(total_cost_usd AS REAL) ELSE 0.0 END),
                       SUM(CASE WHEN created_at >= ? THEN CAST(total_cost_usd AS REAL) ELSE 0.0 END)
                FROM proxy_request_logs
                WHERE app_type IN ('agy', 'claude', 'codex') AND created_at >= ?
                GROUP BY app_type
                """,
                (cut_5h, cut_5h, cut_24h, cut_24h, cut_7d, cut_7d),
            )
            for app_type, req_5h, usd_5h, req_24h, usd_24h, usd_7d in cur.fetchall():
                if app_type in result["harnesses"]:
                    slot = result["harnesses"][app_type]
                    slot["spend_5h_usd"] = round(float(usd_5h or 0.0), 4)
                    slot["req_5h"] = int(req_5h or 0)
                    slot["spend_24h_usd"] = round(float(usd_24h or 0.0), 4)
                    slot["req_24h"] = int(req_24h or 0)
                    slot["spend_7d_usd"] = round(float(usd_7d or 0.0), 4)
        except sqlite3.Error:
            pass
    except sqlite3.Error as exc:
        result["db_error"] = str(exc)
    finally:
        if con is not None:
            con.close()

    return result


def read_codex_native_rate_limits(
    *,
    sessions_root: Path | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any] | None:
    """Inspect recent Codex rollout JSONL files for the latest embedded `rate_limits` event."""
    root = (sessions_root or (Path.home() / ".codex" / "sessions")).expanduser()
    now = time.time() if now_epoch is None else float(now_epoch)
    if not root.exists():
        return None
    try:
        candidates = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None

    for path in candidates[:15]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines[-200:]):
            if "rate_limits" not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = ev.get("payload") if isinstance(ev, dict) else None
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            if not isinstance(rl, dict):
                continue

            windows: list[dict[str, Any]] = []
            rate_limit_reached = bool(rl.get("rate_limit_reached_type"))
            for slot in ("primary", "secondary"):
                w = rl.get(slot)
                if not isinstance(w, dict):
                    continue
                used = _to_float(w.get("used_percent"))
                wmin = _to_float(w.get("window_minutes"))
                res_raw = _parse_iso_or_epoch(w.get("resets_at"))
                reset_elapsed = bool(res_raw is not None and res_raw <= now)
                eff_used = 0.0 if reset_elapsed else used
                rem = _clamp(100.0 - eff_used, 0.0, 100.0) if eff_used is not None else None
                hrs_reset = max(0.0, (res_raw - now) / 3600.0) if (res_raw is not None and not reset_elapsed) else None
                windows.append(
                    {
                        "id": slot,
                        "window_minutes": wmin,
                        "window_hours": _window_hours_from_kind(slot, window_minutes=wmin),
                        "used_percent": eff_used,
                        "raw_used_percent": used,
                        "remaining_pct": rem,
                        "resets_at": _now_iso(res_raw) if res_raw is not None else None,
                        "reset_elapsed": reset_elapsed,
                        "hours_to_reset": round(hrs_reset, 3) if hrs_reset is not None else None,
                    }
                )

            if not windows:
                continue
            tightest = min(
                (w for w in windows if w.get("remaining_pct") is not None),
                key=lambda w: w["remaining_pct"],
                default=windows[0],
            )
            return {
                "source": "codex_rollout",
                "observed_at": ev.get("timestamp"),
                "plan": str(rl.get("plan_type") or "") or None,
                "remaining_pct": tightest.get("remaining_pct"),
                "used_pct": tightest.get("used_percent"),
                "resets_at": tightest.get("resets_at"),
                "hours_to_reset": tightest.get("hours_to_reset"),
                "window_hours": tightest.get("window_hours"),
                "rate_limit_reached": rate_limit_reached and not bool(tightest.get("reset_elapsed")),
                "credits": rl.get("credits") if isinstance(rl.get("credits"), dict) else None,
                "windows": windows,
            }
    return None


def read_claude_stats_summary(*, claude_home: Path | None = None) -> dict[str, Any] | None:
    """Read local Claude Code stats-cache.json activity counters when available."""
    stats_path = ((claude_home or (Path.home() / ".claude")).expanduser()) / "stats-cache.json"
    if not stats_path.exists():
        return None
    try:
        data = json.loads(stats_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    daily = data.get("dailyActivity") if isinstance(data.get("dailyActivity"), list) else []
    recent = daily[-1] if daily else None
    return {
        "last_computed_date": data.get("lastComputedDate"),
        "recent_day": recent,
    }


# ── User Quota Snapshot Helpers ───────────────────────────────────────────


def default_global_snapshot_path() -> Path:
    return Path.home() / SNAPSHOT_GLOBAL_REL


def resolve_snapshot_read_paths(repo: Path | None = None, explicit: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    if explicit is not None:
        paths.append(explicit)
    if repo is not None:
        paths.append(Path(repo).resolve() / SNAPSHOT_REPO_REL)
    paths.append(default_global_snapshot_path())
    dedup: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = str(p)
        if key not in seen:
            seen.add(key)
            dedup.append(p)
    return dedup


def load_quota_snapshot(
    *,
    repo: Path | None = None,
    explicit: Path | None = None,
    now_epoch: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Load user-recorded subscription/window quota overrides (e.g. mirrored from CC Switch UI)."""
    now = time.time() if now_epoch is None else float(now_epoch)
    merged: dict[str, dict[str, Any]] = {}
    # Load global first so repo-local or explicit snapshot overrides it cleanly.
    for path in reversed(resolve_snapshot_read_paths(repo, explicit)):
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        harnesses = raw.get("harnesses") if isinstance(raw, dict) and isinstance(raw.get("harnesses"), dict) else raw
        if not isinstance(harnesses, dict):
            continue
        for h_name, item in harnesses.items():
            h = _map_provider_name(h_name)
            if not h or not isinstance(item, dict):
                continue
            exp_ep = _parse_iso_or_epoch(item.get("expires_at"))
            res_ep = _parse_iso_or_epoch(item.get("resets_at"))
            rem = _to_float(item.get("remaining_pct"))
            used = _to_float(item.get("used_pct"))
            if rem is None and used is not None:
                rem = _clamp(100.0 - used, 0.0, 100.0)
            merged[h] = {
                "source": "snapshot",
                "snapshot_path": str(path),
                "updated_at": item.get("updated_at"),
                "plan": item.get("plan"),
                "remaining_pct": rem,
                "used_pct": round(100.0 - rem, 2) if rem is not None else used,
                "resets_at": item.get("resets_at"),
                "hours_to_reset": max(0.0, (res_ep - now) / 3600.0) if res_ep is not None else None,
                "expires_at": item.get("expires_at"),
                "hours_to_expiry": max(0.0, (exp_ep - now) / 3600.0) if exp_ep is not None else None,
                "window_hours": _to_float(item.get("window_hours")) or 24.0,
                "balance_usd": _to_float(item.get("balance_usd")),
                "note": item.get("note") or None,
            }
    return merged


def update_quota_snapshot(
    path: Path,
    harness: str,
    *,
    remaining_pct: float | None = None,
    used_pct: float | None = None,
    resets_in: str | None = None,
    resets_at: str | None = None,
    expires_at: str | None = None,
    window_hours: float | None = None,
    plan: str | None = None,
    balance_usd: float | None = None,
    note: str | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Atomically write or update one harness entry in quota-snapshot.json."""
    h = _map_provider_name(harness)
    if not h:
        raise ValueError(f"Unsupported harness: {harness}")

    now = time.time() if now_epoch is None else float(now_epoch)
    data: dict[str, Any] = {"version": 1, "updated_at": _now_iso(now), "harnesses": {}}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
                if not isinstance(data.get("harnesses"), dict):
                    data["harnesses"] = {}
        except (OSError, json.JSONDecodeError):
            pass

    entry: dict[str, Any] = dict(data["harnesses"].get(h) or {})
    if remaining_pct is not None:
        entry["remaining_pct"] = _clamp(remaining_pct, 0.0, 100.0)
        entry["used_pct"] = round(100.0 - entry["remaining_pct"], 2)
    elif used_pct is not None:
        u = _clamp(used_pct, 0.0, 100.0)
        entry["used_pct"] = u
        entry["remaining_pct"] = round(100.0 - u, 2)

    if resets_at is not None:
        ep = _parse_iso_or_epoch(resets_at)
        entry["resets_at"] = _now_iso(ep) if ep is not None else resets_at
    elif resets_in is not None:
        hrs = parse_duration_to_hours(resets_in, now_epoch=now)
        if hrs is None:
            raise ValueError(f"Invalid duration or timestamp for --resets-in: {resets_in!r}")
        entry["resets_at"] = _now_iso(now + hrs * 3600.0)

    if expires_at is not None:
        ep = _parse_iso_or_epoch(expires_at)
        entry["expires_at"] = _now_iso(ep) if ep is not None else expires_at
    if window_hours is not None:
        entry["window_hours"] = float(window_hours)
    if plan is not None:
        entry["plan"] = plan
    if balance_usd is not None:
        entry["balance_usd"] = float(balance_usd)
    if note is not None:
        entry["note"] = note
    entry["updated_at"] = _now_iso(now)

    data["updated_at"] = _now_iso(now)
    data["harnesses"][h] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return data


# ── Telemetry Fusion, Scoring, and Roster Recommendation ──────────────────


def collect_quota_state(
    *,
    repo: Path | None = None,
    probe: str = "auto",
    probe_cmd: str = "",
    snapshot_path: Path | None = None,
    cc_switch_dir: Path | None = None,
    codex_sessions_root: Path | None = None,
    claude_home: Path | None = None,
    binaries_available: dict[str, bool] | None = None,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Collect normalized per-harness quota signals from open-source probes + CC Switch + snapshots."""
    now = time.time() if now_epoch is None else float(now_epoch)
    ext = run_external_probe(probe, probe_cmd=probe_cmd, now_epoch=now)
    cc_overlay = read_cc_switch_overlay(cc_switch_dir=cc_switch_dir, now_epoch=now)
    codex_native = read_codex_native_rate_limits(sessions_root=codex_sessions_root, now_epoch=now)
    claude_stats = read_claude_stats_summary(claude_home=claude_home)
    snapshots = load_quota_snapshot(repo=repo, explicit=snapshot_path, now_epoch=now)

    cqp = cc_overlay.get("codex_quota_protection") or {}
    cqp_enabled = bool(cqp.get("enabled", False))
    five_hr_thr = float(cqp.get("fiveHourThreshold", 95))
    weekly_thr = float(cqp.get("weeklyThreshold", 98))

    states: dict[str, dict[str, Any]] = {}
    for h in HARNESSES:
        installed = (
            bool(binaries_available[h])
            if (binaries_available is not None and h in binaries_available)
            else bool(shutil.which(h))
        )
        cc_info = (cc_overlay.get("harnesses") or {}).get(h) or {}
        ext_info = (ext.get("harnesses") or {}).get(h) or {}
        snap_info = snapshots.get(h) or {}

        source_chain: list[str] = []
        remaining_pct: float | None = None
        used_pct: float | None = None
        spend_priority: float | None = None
        runway_status: str | None = None
        runway_hours: float | None = None
        resets_at: str | None = None
        hours_to_reset: float | None = None
        expires_at: str | None = None
        hours_to_expiry: float | None = None
        window_hours: float = 24.0
        plan_name: str | None = None
        balance_usd: float | None = None
        windows: list[dict[str, Any]] = []
        protected_stop = False
        protected_reason = ""

        # 1. External open-source probe (quota-axi / codexbar / cclimits)
        if ext_info and ext_info.get("state") not in ("error", "auth_required", "unavailable"):
            source_chain.append(str(ext_info.get("source") or ext.get("probe") or "external"))
            remaining_pct = _to_float(ext_info.get("remaining_pct"))
            used_pct = _to_float(ext_info.get("used_pct"))
            spend_priority = _to_float(ext_info.get("spend_priority"))
            runway_status = ext_info.get("runway_status")
            runway_hours = _to_float(ext_info.get("runway_hours"))
            resets_at = ext_info.get("resets_at")
            hours_to_reset = _to_float(ext_info.get("hours_to_reset"))
            expires_at = ext_info.get("expires_at")
            hours_to_expiry = _to_float(ext_info.get("hours_to_expiry"))
            window_hours = _to_float(ext_info.get("window_hours")) or window_hours
            plan_name = ext_info.get("plan")
            balance_usd = _to_float(ext_info.get("balance_usd"))
            windows = ext_info.get("windows") or []
            if ext_info.get("state") == "rate_limited":
                protected_stop = True
                protected_reason = f"{source_chain[-1]}:rate_limited"

        # 2. Native Codex rollout fallback (only fills gaps when external probe didn't supply Codex)
        if h == "codex" and codex_native and remaining_pct is None:
            source_chain.append("codex_rollout")
            remaining_pct = _to_float(codex_native.get("remaining_pct"))
            used_pct = _to_float(codex_native.get("used_pct"))
            resets_at = codex_native.get("resets_at")
            hours_to_reset = _to_float(codex_native.get("hours_to_reset"))
            window_hours = _to_float(codex_native.get("window_hours")) or window_hours
            plan_name = plan_name or codex_native.get("plan")
            windows = codex_native.get("windows") or windows
            if codex_native.get("rate_limit_reached"):
                protected_stop = True
                protected_reason = "codex:rate_limit_reached"

        # 3. CC Switch daily/monthly USD cap fallback + health/spend overlay
        if cc_info:
            source_chain.append("cc-switch")
            if remaining_pct is None:
                ld = _to_float(cc_info.get("limit_daily_usd"))
                lm = _to_float(cc_info.get("limit_monthly_usd"))
                s24 = _to_float(cc_info.get("spend_24h_usd")) or 0.0
                if ld and ld > 0:
                    used = _clamp((s24 / ld) * 100.0, 0.0, 100.0)
                    used_pct = round(used, 2)
                    remaining_pct = round(100.0 - used, 2)
                    window_hours = 24.0
                    dt_now = datetime.fromtimestamp(now, tz=timezone.utc).astimezone()
                    next_day = (dt_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    hours_to_reset = round((next_day.timestamp() - now) / 3600.0, 3)
                    resets_at = next_day.isoformat(timespec="seconds")
                elif lm and lm > 0:
                    s7d = _to_float(cc_info.get("spend_7d_usd")) or s24
                    used = _clamp((s7d / lm) * 100.0, 0.0, 100.0)
                    used_pct = round(used, 2)
                    remaining_pct = round(100.0 - used, 2)
                    window_hours = 720.0

        # 4. Explicit user quota snapshot overrides stale or missing numbers
        if snap_info:
            source_chain.append("snapshot")
            if snap_info.get("remaining_pct") is not None:
                remaining_pct = float(snap_info["remaining_pct"])
                used_pct = round(100.0 - remaining_pct, 2)
            if snap_info.get("resets_at") is not None:
                resets_at = snap_info["resets_at"]
                hours_to_reset = _to_float(snap_info.get("hours_to_reset"))
            if snap_info.get("expires_at") is not None:
                expires_at = snap_info["expires_at"]
                hours_to_expiry = _to_float(snap_info.get("hours_to_expiry"))
            if snap_info.get("window_hours"):
                window_hours = float(snap_info["window_hours"])
            if snap_info.get("plan"):
                plan_name = str(snap_info["plan"])
            if snap_info.get("balance_usd") is not None:
                balance_usd = float(snap_info["balance_usd"])

        # Enforce CC Switch codexQuotaProtection across whichever telemetry yielded Codex windows.
        if h == "codex" and cqp_enabled:
            check_wins = windows or (
                [{"window_hours": window_hours, "used_pct": used_pct}] if used_pct is not None else []
            )
            for w in check_wins:
                wh = _to_float(w.get("window_hours")) or 24.0
                up = _to_float(w.get("used_percent") if "used_percent" in w else w.get("used_pct"))
                if up is None and w.get("remaining_pct") is not None:
                    up = 100.0 - float(w["remaining_pct"])
                if up is None:
                    continue
                if wh <= 6.0 and up >= five_hr_thr:
                    protected_stop = True
                    protected_reason = f"codexQuotaProtection 5h ({up:.1f}% >= {five_hr_thr:.0f}%)"
                    break
                if wh > 6.0 and up >= weekly_thr:
                    protected_stop = True
                    protected_reason = f"codexQuotaProtection weekly ({up:.1f}% >= {weekly_thr:.0f}%)"
                    break

        if remaining_pct is not None and remaining_pct <= 5.0 and not protected_stop:
            protected_stop = True
            protected_reason = f"remaining_pct <= 5% ({remaining_pct:.1f}%)"

        is_healthy = int(cc_info.get("is_healthy", 1))
        consec_fails = int(cc_info.get("consecutive_failures", 0))
        degraded = (is_healthy == 0 and consec_fails >= 3)

        states[h] = {
            "harness": h,
            "installed": installed,
            "sources": source_chain,
            "plan": plan_name,
            "provider_name": cc_info.get("provider_name") or None,
            "provider_category": cc_info.get("category"),
            "cost_multiplier": _to_float(cc_info.get("cost_multiplier")) or 1.0,
            "remaining_pct": round(remaining_pct, 2) if remaining_pct is not None else None,
            "used_pct": round(used_pct, 2) if used_pct is not None else None,
            "spend_priority": round(spend_priority, 4) if spend_priority is not None else None,
            "runway_status": runway_status,
            "runway_hours": runway_hours,
            "resets_at": resets_at,
            "hours_to_reset": round(hours_to_reset, 3) if hours_to_reset is not None else None,
            "expires_at": expires_at,
            "hours_to_expiry": round(hours_to_expiry, 3) if hours_to_expiry is not None else None,
            "window_hours": window_hours,
            "balance_usd": balance_usd,
            "spend_5h_usd": cc_info.get("spend_5h_usd"),
            "spend_24h_usd": cc_info.get("spend_24h_usd"),
            "spend_7d_usd": cc_info.get("spend_7d_usd"),
            "usage_script": cc_info.get("usage_script"),
            "is_healthy": bool(is_healthy),
            "consecutive_failures": consec_fails,
            "degraded": degraded,
            "protected_stop": protected_stop,
            "protected_reason": protected_reason or None,
            "scopes": ext_info.get("scopes") if ext_info else None,
            "claude_stats": claude_stats if h == "claude" else None,
        }

    return {
        "generated_at": _now_iso(now),
        "probe_used": ext.get("probe") or "local",
        "probe_ok": bool(ext.get("ok")),
        "probe_error": ext.get("error") or None,
        "codex_quota_protection": cqp,
        "harnesses": states,
    }


def _compute_urgency(info: dict[str, Any]) -> tuple[float, float, float]:
    """Compute (total_urgency, expiry_urgency, reset_urgency) using quota-axi spendPriority when available."""
    rem = _to_float(info.get("remaining_pct"))
    rem_factor = (rem / 100.0) if rem is not None else 0.5

    expiry_urgency = 0.0
    hrs_exp = _to_float(info.get("hours_to_expiry"))
    if hrs_exp is not None and hrs_exp <= 168.0 and (rem is None or rem >= 15.0):
        expiry_urgency = rem_factor * max(0.0, 1.0 - hrs_exp / 168.0) * 45.0

    reset_urgency = 0.0
    sp = _to_float(info.get("spend_priority"))
    if sp is not None:
        # Consume quota-axi's native spendPriority scalar directly (positive = unused allowance nearing reset).
        reset_urgency = _clamp(sp * 15.0, -35.0, 45.0)
    else:
        hrs_res = _to_float(info.get("hours_to_reset"))
        win_hrs = _to_float(info.get("window_hours")) or 24.0
        if hrs_res is not None and win_hrs > 0 and hrs_res <= win_hrs and (rem is None or rem >= 20.0):
            reset_urgency = rem_factor * max(0.0, 1.0 - hrs_res / win_hrs) * 30.0

    return round(expiry_urgency + reset_urgency, 2), round(expiry_urgency, 2), round(reset_urgency, 2)


def score_harnesses(
    collected: dict[str, Any],
    *,
    conserve: list[str] | tuple[str, ...] = (),
    prefer: list[str] | tuple[str, ...] = (),
    strategy: str = "auto",
) -> list[dict[str, Any]]:
    """Score and sort all harnesses with explainable component breakdown."""
    strat = strategy if strategy in STRATEGY_CHOICES else "auto"
    conserve_set = {h.strip().lower() for h in conserve if h.strip()}
    prefer_list = [h.strip().lower() for h in prefer if h.strip()]

    ranked: list[dict[str, Any]] = []
    for h in HARNESSES:
        info = dict((collected.get("harnesses") or {}).get(h) or {"harness": h})
        installed = bool(info.get("installed"))
        protected_stop = bool(info.get("protected_stop"))
        degraded = bool(info.get("degraded"))

        eligible = installed and not protected_stop
        ineligible_reason = ""
        if not installed:
            ineligible_reason = "binary_not_found"
        elif protected_stop:
            ineligible_reason = str(info.get("protected_reason") or "quota_protected_or_exhausted")

        rem = _to_float(info.get("remaining_pct"))
        spend24 = _to_float(info.get("spend_24h_usd")) or 0.0
        cm = _to_float(info.get("cost_multiplier")) or 1.0
        abundance = rem if rem is not None else max(20.0, 55.0 - min(20.0, spend24 * cm * 0.5))

        urgency_total, expiry_u, reset_u = _compute_urgency(info)

        user_adjust = 0.0
        if h in conserve_set:
            user_adjust -= 45.0
        if h in prefer_list:
            idx = prefer_list.index(h)
            user_adjust += 35.0 if idx == 0 else 20.0

        health_penalty = -50.0 if degraded else 0.0

        if not eligible:
            score = -1000.0
        elif strat == "expiring_first":
            score = 0.8 * abundance + 1.8 * urgency_total + user_adjust + health_penalty
        elif strat == "abundant_first":
            score = 1.2 * abundance + 0.5 * urgency_total + user_adjust + health_penalty
        elif strat == "manual":
            if h in prefer_list:
                base_pref = 200.0 - prefer_list.index(h) * 30.0
            elif h in conserve_set:
                base_pref = -100.0
            else:
                base_pref = 0.0
            score = base_pref + 0.25 * abundance + 0.1 * urgency_total + health_penalty
        else:  # auto
            score = abundance + urgency_total + user_adjust + health_penalty

        ranked.append(
            {
                **info,
                "eligible": eligible,
                "ineligible_reason": ineligible_reason or None,
                "conserved": h in conserve_set,
                "preferred_rank": (prefer_list.index(h) + 1) if h in prefer_list else None,
                "score": round(score, 2),
                "breakdown": {
                    "abundance": round(abundance, 2),
                    "expiry_urgency": expiry_u,
                    "reset_urgency": reset_u,
                    "urgency_total": urgency_total,
                    "user_adjust": user_adjust,
                    "health_penalty": health_penalty,
                    "strategy": strat,
                },
            }
        )

    ranked.sort(
        key=lambda r: (
            1 if r["eligible"] else 0,
            float(r["score"]),
            1 if not r["conserved"] else 0,
            float(r.get("remaining_pct") if r.get("remaining_pct") is not None else -1.0),
        ),
        reverse=True,
    )
    return ranked


def recommend_roster(
    ranked: list[dict[str, Any]],
    *,
    conserve: list[str] | tuple[str, ...] = (),
    prefer: list[str] | tuple[str, ...] = (),
    strategy: str = "auto",
) -> dict[str, Any]:
    """Derive recommended roles, tracks, and token-saving tips from ranked harnesses."""
    conserve_set = {h.strip().lower() for h in conserve if h.strip()}
    eligible = [r for r in ranked if r.get("eligible")]
    by_name = {r["harness"]: r for r in ranked}

    executor = eligible[0]["harness"] if eligible else (ranked[0]["harness"] if ranked else "claude")
    other_eligible = [r["harness"] for r in eligible if r["harness"] != executor]
    if other_eligible:
        advisor = other_eligible[0]
    else:
        advisor = executor

    agy_eligible = bool(by_name.get("agy", {}).get("eligible"))
    if agy_eligible and "agy" not in conserve_set:
        verifier = "agy"
    else:
        verifier = advisor if advisor != executor else ("agy" if agy_eligible else executor)

    # Gatekeeper prefers Claude Code so git commit and localhost tests do not hit Codex macOS sandbox limits.
    claude_ok = bool(by_name.get("claude", {}).get("eligible"))
    if claude_ok and ("claude" not in conserve_set or not other_eligible):
        gatekeeper = "claude"
        gatekeeper_note = ""
    else:
        gatekeeper = executor
        gatekeeper_note = (
            "Codex macOS workspace-write 默认禁止写 .git/index.lock 与绑定 127.0.0.1；"
            "合分支与本地 HTTP 验收建议由宿主或 Claude 执行"
            if gatekeeper == "codex"
            else ""
        )

    brief_recommended = "agy" in conserve_set
    conserve_tips: list[str] = []
    if "agy" in conserve_set:
        conserve_tips = [
            f"Phase 1 先派 {executor} 只读扫描代码吐出结构化摘要，AGY 直接拍板写规格包",
            f"Phase 4 用 `{advisor}` 对 `{executor}` 改动做第一轮全量 Diff 对抗审查，AGY 只抽查标红行",
            f"合波集成与跑全量测试交给 `{gatekeeper}`，仅回传摘要数字",
            "截图以自动化脚本卡指标，AGY 仅 `read_file` 抽检 3-5 张核心/异常截图；工具加 `--brief` 并按 Milestone 切新对话",
        ]

    return {
        "strategy": strategy,
        "conserve": sorted(conserve_set),
        "prefer": [h.strip().lower() for h in prefer if h.strip()],
        "executor": executor,
        "advisor": advisor,
        "verifier": verifier,
        "architect": "agy" if by_name.get("agy", {}).get("installed") else executor,
        "gatekeeper": gatekeeper,
        "gatekeeper_note": gatekeeper_note or None,
        "tracks": {
            "primary_coding": executor,
            "cross_review": advisor,
            "integration_gatekeeper": gatekeeper,
        },
        "brief_recommended": brief_recommended,
        "conserve_tips": conserve_tips,
    }


def format_quota_table(
    collected: dict[str, Any],
    ranked: list[dict[str, Any]],
    recommendation: dict[str, Any],
) -> str:
    """Format a compact, human-readable quota + routing summary table."""
    lines = [
        f"=== harness-loop 额度探测与路由建议 ({collected.get('generated_at', '')}) ===",
        f"探针: {collected.get('probe_used', 'local')} | 策略: {recommendation.get('strategy', 'auto')}"
        f" | 省额度: {','.join(recommendation.get('conserve') or []) or '无'}"
        f" | 优先烧: {','.join(recommendation.get('prefer') or []) or '自动'}",
        "",
        f"{'Harness':<8} {'可用':<5} {'剩余%':<7} {'花钱优先级/到期':<20} {'CC Switch通道':<20} {'近24h':<8} {'总分':<7} {'来源'}",
        "-" * 98,
    ]
    for r in ranked:
        h = r["harness"]
        ok_txt = "STOP" if r.get("protected_stop") else ("OK" if r.get("eligible") else "OFF")
        rem = f"{r['remaining_pct']:.0f}%" if r.get("remaining_pct") is not None else "未知"
        if r.get("spend_priority") is not None:
            urg = f"sp={r['spend_priority']:+.2f}"
        elif r.get("hours_to_expiry") is not None:
            urg = f"到期{r['hours_to_expiry']:.1f}h"
        elif r.get("hours_to_reset") is not None:
            urg = f"重置{r['hours_to_reset']:.1f}h"
        else:
            urg = "-"
        prov = str(r.get("provider_name") or "-")[:18]
        s24 = f"${r['spend_24h_usd']:.2f}" if r.get("spend_24h_usd") is not None else "-"
        sc = f"{r['score']:.1f}" if r.get("eligible") else "-"
        srcs = ",".join(r.get("sources") or []) or "-"
        lines.append(f"{h:<8} {ok_txt:<5} {rem:<7} {urg:<20} {prov:<20} {s24:<8} {sc:<7} {srcs}")
        if r.get("protected_reason"):
            lines.append(f"         [保护停用] {r['protected_reason']}")

    lines.append("")
    lines.append(
        "推荐分工: "
        f"executor={recommendation['executor']} | advisor/交叉审={recommendation['advisor']} | "
        f"verifier={recommendation['verifier']} | gatekeeper={recommendation['gatekeeper']}"
        + (" | 建议开启 --brief" if recommendation.get("brief_recommended") else "")
    )
    if recommendation.get("gatekeeper_note"):
        lines.append(f"提示: {recommendation['gatekeeper_note']}")
    for tip in recommendation.get("conserve_tips") or []:
        lines.append(f"  - {tip}")
    return "\n".join(lines)


def _split_csv(val: str) -> list[str]:
    return [x.strip().lower() for x in (val or "").split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quota-aware multi-harness router")
    sub = parser.add_subparsers(dest="cmd")

    def _add_query_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=None)
        p.add_argument("--probe", choices=PROBE_CHOICES, default="auto")
        p.add_argument("--probe-cmd", default="")
        p.add_argument("--snapshot", default="")
        p.add_argument("--conserve", default="")
        p.add_argument("--prefer", default="")
        p.add_argument("--strategy", choices=STRATEGY_CHOICES, default="auto")
        p.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Probe quotas and print recommended routing")
    _add_query_flags(p_show)

    p_set = sub.add_parser("set", help="Record or update subscription quota snapshot")
    p_set.add_argument("--harness", choices=HARNESSES, required=True)
    p_set.add_argument("--remaining", type=float, default=None)
    p_set.add_argument("--used", type=float, default=None)
    p_set.add_argument("--resets-in", default=None)
    p_set.add_argument("--resets-at", default=None)
    p_set.add_argument("--expires-at", default=None)
    p_set.add_argument("--window-hours", type=float, default=None)
    p_set.add_argument("--plan", default=None)
    p_set.add_argument("--balance", type=float, default=None)
    p_set.add_argument("--note", default=None)
    p_set.add_argument("--repo", default=".")
    p_set.add_argument("--global", dest="use_global", action="store_true")
    p_set.add_argument("--snapshot", default="")
    p_set.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "set":
        target = (
            Path(args.snapshot).expanduser().resolve()
            if args.snapshot
            else (
                default_global_snapshot_path()
                if args.use_global
                else Path(args.repo).resolve() / SNAPSHOT_REPO_REL
            )
        )
        updated = update_quota_snapshot(
            target,
            args.harness,
            remaining_pct=args.remaining,
            used_pct=args.used,
            resets_in=args.resets_in,
            resets_at=args.resets_at,
            expires_at=args.expires_at,
            window_hours=args.window_hours,
            plan=args.plan,
            balance_usd=args.balance,
            note=args.note,
        )
        out = {"ok": True, "snapshot_path": str(target), "entry": updated["harnesses"][args.harness]}
        print(json.dumps(out, ensure_ascii=False, indent=2) if args.json else f"Saved {args.harness} -> {target}")
        return 0

    if args.cmd is None:
        args = p_show.parse_args(argv)

    repo = Path(args.repo).resolve() if getattr(args, "repo", None) else None
    snap = Path(args.snapshot).expanduser().resolve() if getattr(args, "snapshot", "") else None
    collected = collect_quota_state(repo=repo, probe=args.probe, probe_cmd=args.probe_cmd, snapshot_path=snap)
    conserve = _split_csv(args.conserve)
    prefer = _split_csv(args.prefer)
    ranked = score_harnesses(collected, conserve=conserve, prefer=prefer, strategy=args.strategy)
    rec = recommend_roster(ranked, conserve=conserve, prefer=prefer, strategy=args.strategy)
    payload = {"ok": True, "collected": collected, "ranked": ranked, "recommendation": rec}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_quota_table(collected, ranked, rec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
