#!/usr/bin/env python3
"""Clawdmeter 'task done' bridge (Mac side).

Registered as a Claude Code `Stop` hook: Claude Code runs it with a JSON blob
on stdin every time the main agent finishes responding. We fire-and-forget a
POST to the Raspberry Pi meter so Clawd celebrates and pushes a phone alert.
Only the project (folder) name leaves the Mac — never any prompt or content.
"""
import json
import os
import subprocess
import sys

# Derive the /done endpoint from the same base as the statusline bridge.
BASE = os.environ.get("CLAWDMETER_URL", "http://raspi-one.local:8080/update")
DONE_URL = BASE.rsplit("/", 1)[0] + "/done"


def is_agent_run():
    """True when Claude is driven by OpenClaw (or another agent), not the
    interactive Claude Code terminal. Those runs notify through their own
    channels, so we stay quiet. Real `claude` in a terminal sets no OPENCLAW_*."""
    return any(k == "OPENCLAW" or k.startswith("OPENCLAW_") for k in os.environ)


def main():
    if is_agent_run():
        return
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    cwd = data.get("cwd") or data.get("workspace", {}).get("current_dir") or ""
    proj = os.path.basename(cwd.rstrip("/")) if cwd else ""
    payload = json.dumps({"project": proj})
    try:
        subprocess.Popen(
            ["curl", "-s", "-m", "2", "-X", "POST", DONE_URL,
             "-H", "Content-Type: application/json", "-d", payload],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
