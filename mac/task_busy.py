#!/usr/bin/env python3
"""Clawdmeter 'busy' bridge (Mac side) — Claude Code UserPromptSubmit hook.

Runs when you submit a prompt. It fire-and-forgets a POST to the meter so Clawd
shows the 'Working…' animation for the whole task, independent of how often the
status line refreshes. The task-done hook (Stop) clears it. No content leaves.
"""
import os
import subprocess

BASE = os.environ.get("CLAWDMETER_URL", "http://raspi-one.local:8080/update")
BUSY_URL = BASE.rsplit("/", 1)[0] + "/busy"


def main():
    try:
        subprocess.Popen(
            ["curl", "-s", "-m", "2", "-X", "POST", BUSY_URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
