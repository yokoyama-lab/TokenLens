#!/usr/bin/env python3
"""TokenLens - local CLI for AI coding assistant quota visibility."""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Colours ─────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"

NO_COLOR = os.environ.get("NO_COLOR") is not None
LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def c(text: str, *codes: str) -> str:
    if NO_COLOR or not sys.stdout.isatty():
        return text
    return "".join(codes) + text + RESET


def safe_text(value: object) -> str:
    text = str(value)
    return CONTROL_CHARS.sub("?", text).replace("\n", " ").replace("\r", " ")


def fmt_tokens(value: int | None) -> str:
    if value is None:
        return "unknown"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def fmt_value(value: int | None, unit: str) -> str:
    if value is None:
        return "unknown"
    if unit == "requests":
        return f"{value:,}"
    return fmt_tokens(value)


def fmt_observed_at(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return safe_text(value)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def fmt_short_datetime(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return safe_text(value)
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def make_gauge(used: int | None, limit: int | None, width: int = 30) -> str:
    if used is None or limit is None or limit <= 0:
        return c("— unavailable —", DIM)

    ratio = min(max(used / limit, 0.0), 1.0)
    filled = int(ratio * width)
    empty = width - filled
    pct = ratio * 100

    if pct >= 90:
        colour = RED
    elif pct >= 80:
        colour = YELLOW
    else:
        colour = GREEN

    return f"{c('█' * filled, colour)}{c('░' * empty, DIM)}  {pct:.0f}%"


def gh_binary() -> str | None:
    path = shutil.which("gh")
    if not path:
        return None
    return str(Path(path).resolve())


def is_safe_path(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    return (
        not path.is_symlink()
        and resolved.is_file()
        and (resolved == root_resolved or root_resolved in resolved.parents)
    )


def safe_glob_files(root: Path, pattern: str) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.glob(pattern) if is_safe_path(path, root)]


def classify_cli_error(message: str) -> str:
    text = safe_text(message).lower()
    if not text:
        return "Request failed."
    if "404" in text or "not found" in text:
        return "Endpoint not available for this account or plan."
    if "401" in text or "403" in text or "forbidden" in text or "requires authentication" in text:
        return "Authentication or permissions are insufficient."
    if "timed out" in text or "timeout" in text:
        return "Request timed out."
    if "could not resolve host" in text or "connection refused" in text or "network" in text:
        return "Network error while contacting the service."
    return "Request failed."


# ── Static metadata ────────────────────────────────────────────────

CONFIG_VERSION = 1
CONFIG_DIR = Path.home() / ".config" / "tokenlens"
CONFIG_PATH = CONFIG_DIR / "config.json"
PROVIDER_ORDER = ("claude", "codex", "gemini", "copilot")

DEFAULT_PROVIDER_SETTINGS = {
    "claude": {
        "limit": 0,
        "limit_type": "tokens",
        "window": "5h",
        "label": "",
        "weekly_limit": 0,
        "weekly_window": "7d",
        "weekly_label": "Weekly",
        "warn_threshold": 0.20,
        "critical_threshold": 0.10,
        "enabled": True,
    },
    "codex": {
        "limit": 0,
        "limit_type": "tokens",
        "window": "month",
        "label": "",
        "warn_threshold": 0.20,
        "critical_threshold": 0.10,
        "enabled": True,
    },
    "gemini": {
        "limit": 0,
        "limit_type": "requests",
        "window": "today",
        "label": "",
        "warn_threshold": 0.20,
        "critical_threshold": 0.10,
        "enabled": True,
    },
    "copilot": {
        "limit": 0,
        "limit_type": "tokens",
        "window": "month",
        "label": "",
        "warn_threshold": 0.20,
        "critical_threshold": 0.10,
        "enabled": True,
    },
}

PROVIDER_INFO = {
    "claude": {
        "name": "Claude Code",
        "support": "estimated",
        "data_source": "Local Claude session JSONL logs",
        "required_auth": "No additional auth if local logs exist",
        "limitations": "Depends on local session logs; values are estimated from usage records.",
        "manual_check": {
            "label": "Anthropic Console Claude Code analytics",
            "url": "https://console.anthropic.com/claude_code",
            "note": "Available for supported Anthropic Console and org roles.",
        },
    },
    "codex": {
        "name": "OpenAI Codex",
        "support": "estimated",
        "data_source": "Local Codex SQLite state DB",
        "required_auth": "No additional auth if local DB exists",
        "limitations": "Relies on the local Codex state schema; quota resets are configured manually.",
        "manual_check": {
            "label": "OpenAI usage dashboard",
            "url": "https://platform.openai.com/usage",
            "note": "Shows organization usage in the OpenAI Platform dashboard.",
        },
    },
    "gemini": {
        "name": "Gemini CLI",
        "support": "estimated",
        "data_source": "Local Gemini session JSON files",
        "required_auth": "No additional auth if local sessions exist",
        "limitations": "Request counting is estimated from local session messages.",
        "manual_check": {
            "label": "Google AI Studio usage",
            "url": "https://aistudio.google.com/usage",
            "note": "Use AI Studio to inspect Gemini API and quota usage.",
        },
    },
    "copilot": {
        "name": "GitHub Copilot",
        "support": "mixed",
        "data_source": "GitHub CLI + Copilot APIs when available",
        "required_auth": "`gh auth login`",
        "limitations": "Some usage endpoints require plan or admin access; unavailable states are common.",
        "manual_check": {
            "label": "GitHub Billing & licensing",
            "url": "https://github.com/settings/billing",
            "note": "Open the Copilot section or premium request analytics in billing settings.",
        },
    },
}

SOURCE_META = {
    "official_api": ("official", "high"),
    "local_logs": ("estimated", "medium"),
    "local_db": ("estimated", "medium"),
    "unavailable": ("unavailable", "low"),
}

STATUS_EXIT_CODES = {
    "ok": 0,
    "warn": 10,
    "critical": 20,
    "unknown": 30,
}


# ── Config ─────────────────────────────────────────────────────────

def default_config() -> dict:
    return {
        "config_version": CONFIG_VERSION,
        "providers": {
            key: dict(values) for key, values in DEFAULT_PROVIDER_SETTINGS.items()
        },
    }


def parse_limit_value(value: str) -> int:
    text = value.strip().upper()
    multipliers = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
    if text[-1] in multipliers:
        return int(float(text[:-1]) * multipliers[text[-1]])
    return int(float(text))


def parse_threshold(value: str) -> float:
    text = value.strip()
    if text.endswith("%"):
        ratio = float(text[:-1]) / 100.0
    else:
        ratio = float(text)
        if ratio > 1:
            ratio /= 100.0
    if ratio < 0 or ratio > 1:
        raise ValueError("threshold must be between 0 and 1 (or 0% and 100%)")
    return ratio


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    truthy = {"1", "true", "yes", "on", "enabled"}
    falsy = {"0", "false", "no", "off", "disabled"}
    if lowered in truthy:
        return True
    if lowered in falsy:
        return False
    raise ValueError("expected one of true/false/yes/no/on/off")


def normalize_provider_settings(raw_settings: dict, defaults: dict) -> dict:
    merged = dict(defaults)
    if not isinstance(raw_settings, dict):
        return merged

    for key in (
        "limit",
        "limit_type",
        "window",
        "label",
        "weekly_limit",
        "weekly_window",
        "weekly_label",
        "warn_threshold",
        "critical_threshold",
        "enabled",
    ):
        if key in raw_settings:
            merged[key] = raw_settings[key]

    if merged.get("limit_type") not in ("tokens", "requests"):
        merged["limit_type"] = defaults["limit_type"]

    try:
        merged["limit"] = int(merged.get("limit", 0))
    except (TypeError, ValueError):
        merged["limit"] = defaults["limit"]

    try:
        merged["weekly_limit"] = int(merged.get("weekly_limit", 0))
    except (TypeError, ValueError):
        merged["weekly_limit"] = defaults.get("weekly_limit", 0)

    try:
        merged["warn_threshold"] = float(merged.get("warn_threshold", defaults["warn_threshold"]))
    except (TypeError, ValueError):
        merged["warn_threshold"] = defaults["warn_threshold"]

    try:
        merged["critical_threshold"] = float(merged.get("critical_threshold", defaults["critical_threshold"]))
    except (TypeError, ValueError):
        merged["critical_threshold"] = defaults["critical_threshold"]

    merged["warn_threshold"] = min(max(merged["warn_threshold"], 0.0), 1.0)
    merged["critical_threshold"] = min(max(merged["critical_threshold"], 0.0), 1.0)
    if merged["critical_threshold"] > merged["warn_threshold"]:
        merged["critical_threshold"], merged["warn_threshold"] = (
            merged["warn_threshold"],
            merged["critical_threshold"],
        )

    merged["enabled"] = bool(merged.get("enabled", defaults["enabled"]))
    merged["window"] = str(merged.get("window", defaults["window"]))
    merged["label"] = str(merged.get("label", defaults["label"]))
    merged["weekly_window"] = str(merged.get("weekly_window", defaults.get("weekly_window", "7d")))
    merged["weekly_label"] = str(merged.get("weekly_label", defaults.get("weekly_label", "Weekly")))
    return merged


def load_config() -> dict:
    config = default_config()
    if not CONFIG_PATH.exists():
        return config

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return config

    provider_blob = raw.get("providers", raw) if isinstance(raw, dict) else {}
    for key, defaults in DEFAULT_PROVIDER_SETTINGS.items():
        config["providers"][key] = normalize_provider_settings(
            provider_blob.get(key, {}),
            defaults,
        )

    if isinstance(raw, dict) and "config_version" in raw:
        config["config_version"] = raw["config_version"]
    return config


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


# ── Time windows ───────────────────────────────────────────────────

def day_start_local(dt: datetime) -> datetime:
    local_dt = dt.astimezone(LOCAL_TZ)
    return local_dt.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_window(window: str) -> tuple[datetime, datetime]:
    now = datetime.now(LOCAL_TZ)
    if window == "today":
        return day_start_local(now), now
    if window == "yesterday":
        end = day_start_local(now)
        return end - timedelta(days=1), end
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now
    if window.endswith("h"):
        hours = int(window[:-1])
        return now - timedelta(hours=hours), now
    if window.endswith("d"):
        days = int(window[:-1])
        return now - timedelta(days=days), now
    raise ValueError(
        "window must be one of: today, yesterday, month, <hours>h, <days>d"
    )


def describe_reset(window: str, observed_at: datetime) -> dict:
    local_now = observed_at.astimezone(LOCAL_TZ)

    if window == "today":
        next_reset = day_start_local(local_now) + timedelta(days=1)
        return {
            "reset_kind": "fixed",
            "reset_at": next_reset.isoformat(),
            "reset_note": "Resets at the next local midnight.",
        }

    if window == "month":
        if local_now.month == 12:
            next_reset = local_now.replace(
                year=local_now.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            next_reset = local_now.replace(
                month=local_now.month + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        return {
            "reset_kind": "fixed",
            "reset_at": next_reset.isoformat(),
            "reset_note": "Resets at the start of the next local calendar month.",
        }

    if window == "yesterday":
        return {
            "reset_kind": "historical",
            "reset_at": None,
            "reset_note": "Historical window; it already closed at the last local midnight.",
        }

    if window.endswith("h") or window.endswith("d"):
        return {
            "reset_kind": "rolling",
            "reset_at": None,
            "reset_note": "Rolling window; usage ages out continuously instead of resetting at one fixed time.",
        }

    return {
        "reset_kind": "unknown",
        "reset_at": None,
        "reset_note": None,
    }


def build_secondary_quota(
    key: str,
    label: str,
    window: str,
    limit: int,
    unit: str,
    provider_cfg: dict,
    raw: dict,
    observed_at: datetime,
) -> dict:
    used = raw_used_value(raw, unit)
    remaining = max(limit - used, 0) if used is not None else None
    status = compute_status(source_kind_and_confidence(raw)[0], used, limit, provider_cfg)
    reset_info = describe_reset(window, observed_at)
    return {
        "key": key,
        "label": label,
        "window": window,
        "unit": unit,
        "used": used,
        "remaining": remaining,
        "limit": limit,
        "status": status,
        "reset_kind": reset_info["reset_kind"],
        "reset_at": reset_info["reset_at"],
        "reset_note": reset_info["reset_note"],
    }


# ── Provider collection ────────────────────────────────────────────

def collect_claude(start: datetime, end: datetime) -> dict:
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return {"error": "Claude Code data directory not found"}

    total_input = 0
    total_output = 0
    total_cache_create = 0
    total_cache_read = 0
    session_count = 0
    models = set()

    jsonl_files = [
        path for path in safe_glob_files(claude_dir, "*/*.jsonl") if "/subagents/" not in str(path)
    ]

    for path in jsonl_files:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < start:
                continue
        except OSError:
            continue

        file_has_match = False
        with open(path, "r", errors="replace", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if record.get("type") != "assistant":
                    continue

                ts_str = record.get("timestamp", "")
                if not ts_str:
                    continue

                try:
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                if timestamp < start or timestamp > end:
                    continue

                message = record.get("message", {})
                if not isinstance(message, dict):
                    continue

                usage = message.get("usage", {})
                if not usage:
                    continue

                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
                total_cache_create += usage.get("cache_creation_input_tokens", 0)
                total_cache_read += usage.get("cache_read_input_tokens", 0)

                model = message.get("model", "")
                if model and not model.startswith("<"):
                    models.add(model)

                if not file_has_match:
                    file_has_match = True
                    session_count += 1

    return {
        "provider": PROVIDER_INFO["claude"]["name"],
        "source": "local_logs",
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_creation_tokens": total_cache_create,
        "cache_read_tokens": total_cache_read,
        "total_tokens": total_input + total_output + total_cache_create + total_cache_read,
        "sessions": session_count,
        "models": sorted(models),
    }


def find_codex_db() -> Path | None:
    home = Path.home() / ".codex"
    preferred = home / "state_5.sqlite"
    if preferred.exists() and is_safe_path(preferred, home):
        return preferred
    candidates = sorted(safe_glob_files(home, "state_*.sqlite"))
    return candidates[-1] if candidates else None


def collect_codex(start: datetime, end: datetime) -> dict:
    db_path = find_codex_db()
    if db_path is None:
        return {"error": "Codex state database not found"}

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        start_epoch = int(start.timestamp())
        end_epoch = int(end.timestamp())

        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_used), 0) "
            "FROM threads WHERE created_at >= ? AND created_at <= ?",
            (start_epoch, end_epoch),
        )
        window_threads, window_tokens = cursor.fetchone()

        cursor.execute(
            "SELECT DISTINCT model_provider FROM threads "
            "WHERE created_at >= ? AND created_at <= ? "
            "AND model_provider IS NOT NULL",
            (start_epoch, end_epoch),
        )
        models = [row[0] for row in cursor.fetchall()]

        cursor.execute("SELECT COUNT(*), COALESCE(SUM(tokens_used), 0) FROM threads")
        all_time_sessions, all_time_tokens = cursor.fetchone()
        conn.close()

        return {
            "provider": PROVIDER_INFO["codex"]["name"],
            "source": "local_db",
            "total_tokens": window_tokens,
            "sessions": window_threads,
            "models": models,
            "all_time_tokens": all_time_tokens,
            "all_time_sessions": all_time_sessions,
        }
    except sqlite3.Error as exc:
        return {"error": f"Codex DB error: {exc}"}


def collect_gemini(start: datetime, end: datetime) -> dict:
    gemini_tmp = Path.home() / ".gemini" / "tmp"
    if not gemini_tmp.exists():
        return {"error": "Gemini CLI data directory not found"}

    total_input = 0
    total_output = 0
    total_cached = 0
    total_thoughts = 0
    total_tool = 0
    total_requests = 0
    session_count = 0
    models = set()
    per_model: dict[str, dict] = {}

    for path in safe_glob_files(gemini_tmp, "*/chats/session-*.json"):
        match = re.match(r"session-(\d{4}-\d{2}-\d{2})T", path.name)
        if not match:
            continue

        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d").replace(
                tzinfo=LOCAL_TZ
            )
        except ValueError:
            continue

        if file_date < day_start_local(start) or file_date > end:
            continue

        try:
            with open(path, "r", errors="replace", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue

        session_has_tokens = False
        for message in payload.get("messages", []):
            tokens = message.get("tokens")
            if not tokens:
                continue

            ts_str = message.get("timestamp", "")
            if ts_str:
                try:
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if timestamp < start or timestamp > end:
                        continue
                except ValueError:
                    pass

            inp = tokens.get("input", 0)
            out = tokens.get("output", 0)
            cached = tokens.get("cached", 0)
            thoughts = tokens.get("thoughts", 0)
            tool = tokens.get("tool", 0)

            total_input += inp
            total_output += out
            total_cached += cached
            total_thoughts += thoughts
            total_tool += tool
            total_requests += 1

            model = message.get("model", "")
            if model:
                models.add(model)
                if model not in per_model:
                    per_model[model] = {
                        "requests": 0,
                        "input": 0,
                        "output": 0,
                        "cached": 0,
                        "thoughts": 0,
                    }
                per_model[model]["requests"] += 1
                per_model[model]["input"] += inp
                per_model[model]["output"] += out
                per_model[model]["cached"] += cached
                per_model[model]["thoughts"] += thoughts

            session_has_tokens = True

        if session_has_tokens:
            session_count += 1

    return {
        "provider": PROVIDER_INFO["gemini"]["name"],
        "source": "local_logs",
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cached_tokens": total_cached,
        "thought_tokens": total_thoughts,
        "tool_tokens": total_tool,
        "total_tokens": total_input + total_output + total_cached + total_thoughts + total_tool,
        "total_requests": total_requests,
        "sessions": session_count,
        "models": sorted(models),
        "per_model": per_model,
    }


def collect_copilot(_: datetime, __: datetime) -> dict:
    gh_bin = gh_binary()
    if gh_bin is None:
        return {"error": "GitHub CLI (gh) not installed"}

    try:
        user_result = subprocess.run(
            [gh_bin, "api", "/user"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {"error": "GitHub API request timed out"}

    if user_result.returncode != 0:
        return {"error": "GitHub CLI not authenticated (run `gh auth login`)"}

    json.loads(user_result.stdout)

    try:
        usage_result = subprocess.run(
            [gh_bin, "api", "/user/copilot/usage", "-H", "Accept: application/vnd.github+json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {"error": "GitHub Copilot API request timed out"}

    if usage_result.returncode == 0:
        return {
            "provider": PROVIDER_INFO["copilot"]["name"],
            "source": "official_api",
            "payload_redacted": True,
            "note": "Official payload available but not normalized into quota fields.",
        }

    billing_result = subprocess.run(
        [gh_bin, "api", "/user/copilot/billing", "-H", "Accept: application/vnd.github+json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if billing_result.returncode == 0:
        return {
            "provider": PROVIDER_INFO["copilot"]["name"],
            "source": "official_api",
            "payload_redacted": True,
            "note": "Official billing payload available but not normalized into quota fields.",
        }

    return {
        "provider": PROVIDER_INFO["copilot"]["name"],
        "source": "unavailable",
        "payload_redacted": True,
        "note": "Usage API not accessible (may require org admin or Copilot Business/Enterprise).",
    }


COLLECTORS = {
    "claude": collect_claude,
    "codex": collect_codex,
    "gemini": collect_gemini,
    "copilot": collect_copilot,
}


# ── Normalization ──────────────────────────────────────────────────

def source_kind_and_confidence(raw: dict) -> tuple[str, str]:
    if "error" in raw:
        return "unavailable", "low"
    return SOURCE_META.get(raw.get("source", "unavailable"), ("estimated", "low"))


def raw_used_value(raw: dict, unit: str) -> int | None:
    key = "total_requests" if unit == "requests" else "total_tokens"
    return raw.get(key)


def compute_status(source_kind: str, used: int | None, limit: int | None, provider_cfg: dict) -> str:
    if source_kind == "unavailable" or used is None or limit is None or limit <= 0:
        return "unknown"

    remaining_ratio = max(limit - used, 0) / limit
    if remaining_ratio <= provider_cfg["critical_threshold"]:
        return "critical"
    if remaining_ratio <= provider_cfg["warn_threshold"]:
        return "warn"
    return "ok"


def normalize_details(raw: dict) -> dict:
    details = {}
    allowed_keys = {
        "error",
        "note",
        "payload_redacted",
        "sessions",
        "models",
        "total_requests",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cache_creation_tokens",
        "cache_read_tokens",
        "cached_tokens",
        "thought_tokens",
        "tool_tokens",
        "all_time_tokens",
        "all_time_sessions",
        "per_model",
    }
    redacted_keys = {"usage", "billing", "user"}
    for key, value in raw.items():
        if key in redacted_keys:
            details["sensitive_payload_redacted"] = True
            continue
        if key in allowed_keys:
            details[key] = value
    unexpected = sorted(
        key for key in raw.keys() if key not in allowed_keys | redacted_keys | {"provider", "source"}
    )
    if unexpected:
        details["unexpected_fields_redacted"] = unexpected
    return details


def normalize_provider_result(
    provider_key: str,
    raw: dict,
    provider_cfg: dict,
    window: str,
    start: datetime,
    end: datetime,
    observed_at: datetime,
    secondary_quotas: list[dict] | None = None,
) -> dict:
    info = PROVIDER_INFO[provider_key]
    source_kind, confidence = source_kind_and_confidence(raw)
    unit = provider_cfg["limit_type"]
    limit = provider_cfg["limit"] if provider_cfg["limit"] > 0 else None
    used = raw_used_value(raw, unit)
    remaining = max(limit - used, 0) if limit is not None and used is not None else None
    status = compute_status(source_kind, used, limit, provider_cfg)
    reset_info = describe_reset(window, observed_at)

    return {
        "provider": provider_key,
        "name": info["name"],
        "status": status,
        "source_kind": source_kind,
        "confidence": confidence,
        "window": window,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "observed_at": observed_at.isoformat(),
        "unit": unit,
        "used": used,
        "remaining": remaining,
        "limit": limit,
        "label": provider_cfg.get("label") or None,
        "enabled": provider_cfg.get("enabled", True),
        "warn_threshold": provider_cfg["warn_threshold"],
        "critical_threshold": provider_cfg["critical_threshold"],
        "reset_kind": reset_info["reset_kind"],
        "reset_at": reset_info["reset_at"],
        "reset_note": reset_info["reset_note"],
        "secondary_quotas": secondary_quotas or [],
        "manual_check": info["manual_check"],
        "details": normalize_details(raw),
    }


def overall_status(results: list[dict]) -> str:
    statuses = {item["status"] for item in results}
    if "critical" in statuses:
        return "critical"
    if "warn" in statuses:
        return "warn"
    if "unknown" in statuses:
        return "unknown"
    return "ok"


def window_label(results: list[dict]) -> str:
    windows = sorted({item["window"] for item in results})
    if len(windows) == 1:
        return windows[0]
    return "mixed"


def selected_providers(config: dict, requested: list[str]) -> list[str]:
    if requested:
        for provider in requested:
            if provider not in COLLECTORS:
                raise ValueError(
                    f"unknown provider '{provider}' (available: {', '.join(PROVIDER_ORDER)})"
                )
        return requested

    return [
        provider
        for provider in PROVIDER_ORDER
        if config["providers"][provider].get("enabled", True)
    ]


def collect_status_results(config: dict, requested: list[str], explicit_window: str | None) -> list[dict]:
    observed_at = datetime.now(LOCAL_TZ)
    results = []

    for provider in selected_providers(config, requested):
        provider_cfg = config["providers"][provider]
        window = explicit_window or provider_cfg["window"]
        start, end = parse_window(window)
        raw = COLLECTORS[provider](start, end)
        secondary_quotas = []

        if provider == "claude" and provider_cfg.get("weekly_limit", 0) > 0:
            weekly_window = provider_cfg.get("weekly_window", "7d")
            weekly_start, weekly_end = parse_window(weekly_window)
            weekly_raw = COLLECTORS[provider](weekly_start, weekly_end)
            secondary_quotas.append(
                build_secondary_quota(
                    key="weekly",
                    label=provider_cfg.get("weekly_label", "Weekly"),
                    window=weekly_window,
                    limit=provider_cfg["weekly_limit"],
                    unit=provider_cfg["limit_type"],
                    provider_cfg=provider_cfg,
                    raw=weekly_raw,
                    observed_at=observed_at,
                )
            )

        results.append(
            normalize_provider_result(
                provider,
                raw,
                provider_cfg,
                window,
                start,
                end,
                observed_at,
                secondary_quotas=secondary_quotas,
            )
        )

    return results


# ── Text rendering ─────────────────────────────────────────────────

def status_colour(status: str) -> str:
    if status == "ok":
        return GREEN
    if status == "warn":
        return YELLOW
    if status == "critical":
        return RED
    return DIM


def source_label(item: dict) -> str:
    return f"{item['source_kind']}/{item['confidence']}"


def display_status_provider(item: dict) -> None:
    print()
    label_suffix = f"  {c(safe_text(item['label']), DIM)}" if item.get("label") else ""
    print(
        f"  {c(safe_text(item['name']), BOLD, CYAN)}  "
        f"[{c(item['status'], status_colour(item['status']))}] "
        f"{c(safe_text(source_label(item)), DIM)}{label_suffix}"
    )

    details = item["details"]
    used = item["used"]
    limit = item["limit"]

    if used is not None and limit is not None:
        print(f"    {make_gauge(used, limit)}")
        print(
            f"    {'Used:':<16} {c(fmt_value(used, item['unit']), BOLD, WHITE)}"
            f"  /  {fmt_value(limit, item['unit'])} {item['unit']}"
        )
        print(
            f"    {'Remaining:':<16} "
            f"{c(fmt_value(item['remaining'], item['unit']), BOLD, status_colour(item['status']))} "
            f"{item['unit']}"
        )
    elif used is not None:
        print(
            f"    {'Used:':<16} "
            f"{c(fmt_value(used, item['unit']), BOLD, WHITE)} {item['unit']}"
        )
        print(f"    {c('Configure a limit to enable remaining quota warnings.', DIM)}")
    else:
        print(f"    {c('Usage data unavailable for this provider.', DIM)}")

    print(f"    {'Window:':<16} {item['window']}")
    print(f"    {'Observed at:':<16} {fmt_observed_at(item['observed_at'])}")
    if item.get("reset_at"):
        print(f"    {'Resets at:':<16} {fmt_short_datetime(item['reset_at'])}")
    elif item.get("reset_note"):
        print(f"    {'Reset:':<16} {safe_text(item['reset_note'])}")
    if item.get("manual_check"):
        print(f"    {'Check page:':<16} {safe_text(item['manual_check']['url'])}")

    if item.get("provider") == "claude" and not item.get("secondary_quotas") and item["limit"] is not None:
        print(f"    {'Weekly:':<16} configure claude.weekly_limit to show 7d remaining")

    if details.get("sessions"):
        print(f"    {'Sessions:':<16} {details['sessions']}")
    if details.get("models"):
        print(f"    {'Models:':<16} {', '.join(safe_text(model) for model in details['models'])}")
    if details.get("total_requests"):
        print(f"    {'Requests:':<16} {details['total_requests']:,}")
    if details.get("total_tokens") and item["unit"] != "tokens":
        print(f"    {'Tokens seen:':<16} {fmt_tokens(details['total_tokens'])}")
    if details.get("all_time_tokens"):
        print(
            f"    {'All-time:':<16} {fmt_tokens(details['all_time_tokens'])} "
            f"({details.get('all_time_sessions', '?')} sessions)"
        )
    if details.get("note"):
        print(f"    {c('Note:', DIM)} {safe_text(details['note'])}")
    if item.get("reset_at") and item.get("reset_note"):
        print(f"    {c('Reset note:', DIM)} {safe_text(item['reset_note'])}")
    for quota in item.get("secondary_quotas", []):
        quota_label = f"{quota['label']}:"
        print(
            f"    {c(safe_text(quota_label), DIM):<16} "
            f"{fmt_value(quota['remaining'], quota['unit'])} / "
            f"{fmt_value(quota['limit'], quota['unit'])} {quota['unit']} left"
        )
        if quota.get("reset_at"):
            print(f"    {c('  resets at:', DIM):<16} {fmt_short_datetime(quota['reset_at'])}")
        elif quota.get("reset_note"):
            print(f"    {c('  reset:', DIM):<16} {safe_text(quota['reset_note'])}")
    if item.get("manual_check", {}).get("note"):
        print(f"    {c('Manual check:', DIM)} {safe_text(item['manual_check']['note'])}")
    if details.get("error"):
        print(f"    {c('Error:', RED)} {safe_text(details['error'])}")


def display_status_summary(results: list[dict], show_bar: bool) -> None:
    overall = overall_status(results)
    print()
    print(c("  ╔══════════════════════════════════════════════════════╗", DIM))
    print(
        c("  ║", DIM)
        + c("  TokenLens  ", BOLD, MAGENTA)
        + c(
            f"—  status: {overall:<8} window: {window_label(results):<8}",
            DIM,
        )
        + c("║", DIM)
    )
    print(c("  ╚══════════════════════════════════════════════════════╝", DIM))

    for item in results:
        display_status_provider(item)

    measurable = [item for item in results if item["used"] is not None and item["limit"] is not None]
    if show_bar and measurable:
        print()
        print(c("  Quota overview:", BOLD))
        for item in measurable:
            print(
                f"    {item['name']:<16} "
                f"{make_gauge(item['used'], item['limit'], width=24)}  "
                f"{fmt_value(item['remaining'], item['unit'])} {item['unit']} left"
            )
    print()


# ── Doctor ─────────────────────────────────────────────────────────

def doctor_status_from_checks(checks: list[dict]) -> str:
    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        return "critical"
    if "warn" in statuses:
        return "warn"
    return "ok"


def doctor_check(status: str, title: str, message: str) -> dict:
    return {"status": status, "title": title, "message": message}


def run_cmd(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def doctor_claude() -> dict:
    checks = []
    next_steps = []
    data_dir = Path.home() / ".claude" / "projects"

    if data_dir.exists():
        checks.append(doctor_check("ok", "Data directory", "Claude data directory is present"))
        count = len(list(data_dir.glob("*/*.jsonl")))
        if count:
            checks.append(doctor_check("ok", "Session logs", f"Found {count} JSONL session files"))
        else:
            checks.append(doctor_check("warn", "Session logs", "No Claude session JSONL files found yet"))
            next_steps.append("Run Claude Code at least once to generate local session logs.")
    else:
        checks.append(doctor_check("fail", "Data directory", "Claude data directory is missing"))
        next_steps.append("Run Claude Code once on this machine to create ~/.claude/projects.")

    return {
        "provider": "claude",
        "name": PROVIDER_INFO["claude"]["name"],
        "status": doctor_status_from_checks(checks),
        "checks": checks,
        "next_steps": next_steps,
    }


def doctor_codex() -> dict:
    checks = []
    next_steps = []
    db_path = find_codex_db()

    if db_path is None:
        checks.append(doctor_check("fail", "State DB", "No Codex state_*.sqlite database found"))
        next_steps.append("Run Codex once on this machine to create the local state database.")
    else:
        checks.append(doctor_check("ok", "State DB", "Codex state database is present"))
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM threads")
            count = cursor.fetchone()[0]
            conn.close()
            checks.append(doctor_check("ok", "Threads table", f"Readable threads table with {count} rows"))
            if count == 0:
                checks.append(doctor_check("warn", "Usage rows", "Codex DB exists but has no recorded threads"))
        except sqlite3.Error:
            checks.append(doctor_check("fail", "Threads table", "Unable to read the Codex DB."))
            next_steps.append("Inspect the Codex DB schema; local state may have changed.")

    return {
        "provider": "codex",
        "name": PROVIDER_INFO["codex"]["name"],
        "status": doctor_status_from_checks(checks),
        "checks": checks,
        "next_steps": next_steps,
    }


def doctor_gemini() -> dict:
    checks = []
    next_steps = []
    tmp_dir = Path.home() / ".gemini" / "tmp"

    if tmp_dir.exists():
        checks.append(doctor_check("ok", "Data directory", "Gemini data directory is present"))
        count = len(list(tmp_dir.glob("*/chats/session-*.json")))
        if count:
            checks.append(doctor_check("ok", "Session files", f"Found {count} Gemini session files"))
        else:
            checks.append(doctor_check("warn", "Session files", "Gemini tmp exists but no session files were found"))
            next_steps.append("Run Gemini CLI once to create local session files.")
    else:
        checks.append(doctor_check("fail", "Data directory", "Gemini data directory is missing"))
        next_steps.append("Run Gemini CLI once on this machine to create ~/.gemini/tmp.")

    return {
        "provider": "gemini",
        "name": PROVIDER_INFO["gemini"]["name"],
        "status": doctor_status_from_checks(checks),
        "checks": checks,
        "next_steps": next_steps,
    }


def doctor_copilot() -> dict:
    checks = []
    next_steps = []

    gh_bin = gh_binary()
    if gh_bin is None:
        checks.append(doctor_check("fail", "GitHub CLI", "`gh` is not installed"))
        next_steps.append("Install GitHub CLI (`gh`) to enable Copilot checks.")
        return {
            "provider": "copilot",
            "name": PROVIDER_INFO["copilot"]["name"],
            "status": doctor_status_from_checks(checks),
            "checks": checks,
            "next_steps": next_steps,
        }

    checks.append(doctor_check("ok", "GitHub CLI", "`gh` is installed"))
    code, _, stderr = run_cmd([gh_bin, "auth", "status"], timeout=10)
    if code == 0:
        checks.append(doctor_check("ok", "Authentication", "GitHub CLI is authenticated"))
    else:
        checks.append(
            doctor_check(
                "fail",
                "Authentication",
                classify_cli_error(stderr) if stderr else "GitHub CLI is not authenticated.",
            )
        )
        next_steps.append("Run `gh auth login` before checking Copilot usage.")
        return {
            "provider": "copilot",
            "name": PROVIDER_INFO["copilot"]["name"],
            "status": doctor_status_from_checks(checks),
            "checks": checks,
            "next_steps": next_steps,
        }

    code, _, stderr = run_cmd(
        [gh_bin, "api", "/user/copilot/usage", "-H", "Accept: application/vnd.github+json"],
        timeout=15,
    )
    if code == 0:
        checks.append(doctor_check("ok", "Usage API", "Copilot usage API is reachable"))
    else:
        checks.append(
            doctor_check(
                "warn",
                "Usage API",
                classify_cli_error(stderr) if stderr else "Copilot usage API is not available for this account or plan.",
            )
        )
        next_steps.append("Copilot usage endpoints may require a supported plan or additional permissions.")

    return {
        "provider": "copilot",
        "name": PROVIDER_INFO["copilot"]["name"],
        "status": doctor_status_from_checks(checks),
        "checks": checks,
        "next_steps": next_steps,
    }


DOCTORS = {
    "claude": doctor_claude,
    "codex": doctor_codex,
    "gemini": doctor_gemini,
    "copilot": doctor_copilot,
}


def collect_doctor_results(config: dict, requested: list[str]) -> list[dict]:
    results = []
    for provider in selected_providers(config, requested):
        result = DOCTORS[provider]()
        result["enabled"] = config["providers"][provider].get("enabled", True)
        results.append(result)
    return results


def display_doctor(results: list[dict]) -> None:
    print()
    print(c("  TokenLens doctor", BOLD, MAGENTA))
    print()

    for result in results:
        print(
            f"  {c(safe_text(result['name']), BOLD, CYAN)}  "
            f"[{c(result['status'], status_colour(result['status']))}]"
        )
        for check in result["checks"]:
            colour = GREEN if check["status"] == "ok" else YELLOW if check["status"] == "warn" else RED
            print(f"    {c(check['status'].upper(), colour):<8} {safe_text(check['title'])}: {safe_text(check['message'])}")
        if result["next_steps"]:
            for step in result["next_steps"]:
                print(f"    {c('Next:', DIM)} {safe_text(step)}")
        print()


# ── Providers listing ──────────────────────────────────────────────

def provider_listing(config: dict) -> list[dict]:
    items = []
    for key in PROVIDER_ORDER:
        info = PROVIDER_INFO[key]
        cfg = config["providers"][key]
        items.append(
            {
                "provider": key,
                "name": info["name"],
                "support": info["support"],
                "data_source": info["data_source"],
                "required_auth": info["required_auth"],
                "limitations": info["limitations"],
                "manual_check": info["manual_check"],
                "enabled": cfg["enabled"],
                "default_window": cfg["window"],
                "unit": cfg["limit_type"],
            }
        )
    return items


def display_providers(items: list[dict]) -> None:
    print()
    print(c("  TokenLens providers", BOLD, MAGENTA))
    print()
    for item in items:
        state = "enabled" if item["enabled"] else "disabled"
        print(f"  {c(safe_text(item['name']), BOLD, CYAN)}  [{safe_text(item['support'])}]  {c(state, DIM)}")
        print(f"    {'Source:':<14} {safe_text(item['data_source'])}")
        print(f"    {'Auth:':<14} {safe_text(item['required_auth'])}")
        print(f"    {'Default:':<14} {item['default_window']} / {item['unit']}")
        print(f"    {'Check page:':<14} {safe_text(item['manual_check']['url'])}")
        print(f"    {'Limits:':<14} {safe_text(item['limitations'])}")
        print()


# ── Config command ────────────────────────────────────────────────

def display_config(config: dict) -> None:
    print()
    print(c("  TokenLens config", BOLD, MAGENTA))
    print(f"  {c(str(CONFIG_PATH), DIM)}")
    print(f"  {c(f'config_version={config['config_version']}', DIM)}")
    print()

    for provider in PROVIDER_ORDER:
        values = config["providers"][provider]
        limit = values["limit"]
        limit_str = fmt_value(limit, values["limit_type"]) if limit else c("not set", DIM)
        unit = values["limit_type"]
        enabled = "enabled" if values["enabled"] else "disabled"
        label = f"  ({values['label']})" if values.get("label") else ""
        print(
            f"    {provider:<10}"
            f"limit: {limit_str:<10}  "
            f"type: {unit:<8}  "
            f"window: {values['window']:<8}  "
            f"warn: {int(values['warn_threshold'] * 100):>2}%  "
            f"critical: {int(values['critical_threshold'] * 100):>2}%  "
            f"{enabled}{label}"
        )
        if provider == "claude":
            weekly_limit = values.get("weekly_limit", 0)
            weekly_limit_str = fmt_value(weekly_limit, values["limit_type"]) if weekly_limit else c("not set", DIM)
            print(
                f"              "
                f"weekly_limit: {weekly_limit_str:<10}  "
                f"weekly_window: {values.get('weekly_window', '7d'):<8}  "
                f"weekly_label: {values.get('weekly_label', 'Weekly')}"
            )

    print()
    print(f"  {c('Set fields:', DIM)}")
    print("    tokenlens config set claude.limit 500M")
    print("    tokenlens config set claude.weekly_limit 2G")
    print("    tokenlens config set gemini.warn_threshold 15%")
    print("    tokenlens config set copilot.enabled false")
    print("    tokenlens config set claude.label 'Max 5x'")
    print()


def run_config_command(args: argparse.Namespace, config: dict) -> int:
    action = args.config_action or "show"

    if action == "show":
        display_config(config)
        return 0

    if action == "reset":
        save_config(default_config())
        print("  Config reset to defaults.")
        return 0

    if action != "set":
        print("Usage: tokenlens config [show|set <provider.field> <value>|reset]", file=sys.stderr)
        return 1

    provider, _, field = args.key.partition(".")
    if provider not in PROVIDER_ORDER or not field:
        print("Error: use <provider>.<field> (e.g. claude.limit)", file=sys.stderr)
        return 1

    provider_cfg = config["providers"][provider]
    value = args.value

    try:
        if field == "limit":
            provider_cfg["limit"] = parse_limit_value(value)
        elif field == "limit_type":
            if value not in ("tokens", "requests"):
                raise ValueError("limit_type must be 'tokens' or 'requests'")
            provider_cfg["limit_type"] = value
        elif field == "window":
            parse_window(value)
            provider_cfg["window"] = value
        elif field == "label":
            provider_cfg["label"] = value
        elif field == "weekly_limit":
            provider_cfg["weekly_limit"] = parse_limit_value(value)
        elif field == "weekly_window":
            parse_window(value)
            provider_cfg["weekly_window"] = value
        elif field == "weekly_label":
            provider_cfg["weekly_label"] = value
        elif field == "warn_threshold":
            provider_cfg["warn_threshold"] = parse_threshold(value)
            if provider_cfg["critical_threshold"] > provider_cfg["warn_threshold"]:
                provider_cfg["critical_threshold"] = provider_cfg["warn_threshold"]
        elif field == "critical_threshold":
            provider_cfg["critical_threshold"] = parse_threshold(value)
            if provider_cfg["critical_threshold"] > provider_cfg["warn_threshold"]:
                raise ValueError("critical_threshold must be less than or equal to warn_threshold")
        elif field == "enabled":
            provider_cfg["enabled"] = parse_bool(value)
        else:
            raise ValueError(
                "unknown field (use: limit, limit_type, window, label, "
                "weekly_limit, weekly_window, weekly_label, "
                "warn_threshold, critical_threshold, enabled)"
            )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    save_config(config)
    display_config(config)
    return 0


# ── Command runners ────────────────────────────────────────────────

def status_payload(results: list[dict]) -> dict:
    overall = overall_status(results)
    return {
        "observed_at": results[0]["observed_at"] if results else datetime.now(LOCAL_TZ).isoformat(),
        "overall_status": overall,
        "exit_code": STATUS_EXIT_CODES[overall],
        "results": results,
    }


def doctor_payload(results: list[dict]) -> dict:
    statuses = {item["status"] for item in results}
    overall = "critical" if "critical" in statuses else "warn" if "warn" in statuses else "ok"
    return {
        "overall_status": overall,
        "exit_code": STATUS_EXIT_CODES[overall],
        "results": results,
    }


def run_status_command(args: argparse.Namespace, config: dict) -> int:
    try:
        results = collect_status_results(config, args.provider, args.window)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    payload = status_payload(results)
    if args.json_output:
        print(json.dumps(payload, indent=2))
    else:
        display_status_summary(results, show_bar=args.bar or not args.provider)
    return payload["exit_code"]


def run_doctor_command(args: argparse.Namespace, config: dict) -> int:
    try:
        results = collect_doctor_results(config, args.provider)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    payload = doctor_payload(results)
    if args.json_output:
        print(json.dumps(payload, indent=2))
    else:
        display_doctor(results)
    return payload["exit_code"]


def run_providers_command(args: argparse.Namespace, config: dict) -> int:
    items = provider_listing(config)
    if args.json_output:
        print(json.dumps({"providers": items}, indent=2))
    else:
        display_providers(items)
    return 0


# ── CLI ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenlens",
        description="Local CLI for AI coding assistant quota visibility.",
    )
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser(
        "status",
        help="Show provider quota status",
    )
    status_parser.add_argument(
        "provider",
        nargs="*",
        help="Provider(s) to check: claude, codex, gemini, copilot",
    )
    status_parser.add_argument(
        "-w",
        "--window",
        default=None,
        help="Override time window for all selected providers",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit normalized JSON output",
    )
    status_parser.add_argument(
        "--bar",
        action="store_true",
        help="Show bar chart comparison",
    )
    status_parser.add_argument(
        "-r",
        "--remaining",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run provider diagnostics",
    )
    doctor_parser.add_argument(
        "provider",
        nargs="*",
        help="Provider(s) to diagnose: claude, codex, gemini, copilot",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit doctor results as JSON",
    )

    providers_parser = subparsers.add_parser(
        "providers",
        help="List provider support details",
    )
    providers_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit provider listing as JSON",
    )

    config_parser = subparsers.add_parser(
        "config",
        help="Show or update configuration",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_action")

    config_subparsers.add_parser("show", help="Show configuration")
    config_subparsers.add_parser("reset", help="Reset configuration to defaults")

    config_set_parser = config_subparsers.add_parser("set", help="Set a config value")
    config_set_parser.add_argument("key", help="Config key, for example claude.limit")
    config_set_parser.add_argument("value", help="New value")

    return parser


def routed_argv(argv: list[str]) -> list[str]:
    commands = {"status", "doctor", "providers", "config"}
    if argv and argv[0] in {"-h", "--help"}:
        return argv
    if not argv:
        return ["status"]
    if argv[0] in commands:
        return argv
    return ["status"] + argv


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(routed_argv(sys.argv[1:]))
    config = load_config()

    if args.command == "status":
        return run_status_command(args, config)
    if args.command == "doctor":
        return run_doctor_command(args, config)
    if args.command == "providers":
        return run_providers_command(args, config)
    if args.command == "config":
        return run_config_command(args, config)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
