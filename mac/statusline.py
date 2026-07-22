#!/usr/bin/env python3
"""Clawdmeter statusline bridge (Mac side, Camino A).

Claude Code invokes this with a JSON blob on stdin for every statusline refresh.
We: (1) log the raw input so we can learn the exact schema, (2) extract the
usage rate limits, (3) fire-and-forget a POST to the Raspberry Pi meter, and
(4) print a normal status line to stdout. Only harmless percentages leave the
Mac — never any token or credential.
"""
import json
import os
import subprocess
import sys

# Where the meter lives. Override with CLAWDMETER_URL if your Pi has a
# different mDNS name / IP. The default mDNS name survives IP changes.
PI_URL = os.environ.get("CLAWDMETER_URL", "http://raspi-one.local:8080/update")
LOG_DIR = os.path.expanduser("~/.claude/clawdmeter")
RAW_LOG = os.path.join(LOG_DIR, "last_input.json")


def dig(d, *keys):
    """Return first present key from a dict (case-insensitive-ish)."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def extract_window(win):
    """Pull (utilization_pct, resets_at_iso) from a rate-limit window dict."""
    if not isinstance(win, dict):
        return None, None
    util = dig(win, "used_percentage", "utilization", "used_pct", "percent",
              "percentage", "used", "usage", "value")
    reset = dig(win, "resets_at", "reset_at", "resets", "reset",
                "reset_time", "resets_at_iso")
    return util, reset


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}

    # (1) log raw input for schema discovery / debugging
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(RAW_LOG, "w") as f:
            f.write(raw)
    except OSError:
        pass

    # (2) extract rate limits (defensive across possible key names)
    rl = dig(data, "rate_limits", "rateLimits", "usage") or {}
    five = dig(rl, "five_hour", "fiveHour", "session", "5h")
    seven = dig(rl, "seven_day", "sevenDay", "weekly", "7d")
    f_util, f_reset = extract_window(five)
    s_util, s_reset = extract_window(seven)

    model = dig(data.get("model", {}), "display_name", "name") or "Claude"
    cwd = data.get("workspace", {}).get("current_dir") or data.get("cwd") or ""
    proj = os.path.basename(cwd) if cwd else ""

    # status shown at the bottom of the meter
    status = model if not proj else f"{model} · {proj}"

    # session stats (cost, context %, lines) for the meter's 2nd screen
    cost = data.get("cost", {}) if isinstance(data.get("cost"), dict) else {}
    ctx = data.get("context_window", {}) if isinstance(data.get("context_window"), dict) else {}
    stats = {
        "cost_usd": cost.get("total_cost_usd"),
        "context_pct": dig(ctx, "used_percentage", "used_pct", "percentage"),
        "lines_added": cost.get("total_lines_added"),
        "lines_removed": cost.get("total_lines_removed"),
        "model": model,
    }

    # (3) fire-and-forget push to the Pi (never block the statusline)
    payload = {"status": status, "stats": stats}
    if f_util is not None or f_reset is not None:
        payload["five_hour"] = {"utilization": f_util, "resets_at": f_reset}
    if s_util is not None or s_reset is not None:
        payload["seven_day"] = {"utilization": s_util, "resets_at": s_reset}
    try:
        subprocess.Popen(
            ["curl", "-s", "-m", "2", "-X", "POST", PI_URL,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

    # (4) print a normal status line
    def pct(v):
        try:
            v = float(v)
            if v <= 1.0:
                v *= 100
            return f"{int(round(v))}%"
        except (TypeError, ValueError):
            return "—"

    line = f"⧗ {model}"
    if f_util is not None:
        line += f"  5h {pct(f_util)}"
    if s_util is not None:
        line += f"  7d {pct(s_util)}"
    print(line)


if __name__ == "__main__":
    main()
