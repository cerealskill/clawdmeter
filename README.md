<div align="center">

# 🟠 Clawdmeter

**A physical, always-on meter for your Claude Code usage.**
An animated Clawd mascot on a tiny TFT that shows your 5-hour and weekly rate-limit
windows in real time — pushed straight from your Claude Code status line.

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-3%20B%2B-C51A4A?logo=raspberrypi&logoColor=white)
![Display](https://img.shields.io/badge/Display-ILI9486%203.5%22-FF7A18)
![License](https://img.shields.io/badge/License-MIT-blue.svg)
![Status](https://img.shields.io/badge/status-working-brightgreen)

<br />

<table>
  <tr>
    <td align="center" width="33%"><img src="docs/img/splash.png" alt="Splash view — animated Clawd mascot asleep on a green face" /><br /><sub><b>Splash</b> · Clawd asleep (green &lt;20%)</sub></td>
    <td align="center" width="33%"><img src="docs/img/session.png" alt="Session view — cost, context, model and line counts" /><br /><sub><b>Session</b> · cost · context · lines</sub></td>
    <td align="center" width="33%"><img src="docs/img/trend.png" alt="Trend view — sparkline of 5h and weekly usage" /><br /><sub><b>Trend</b> · 5h &amp; weekly sparkline</sub></td>
  </tr>
</table>

</div>

---

## What it is

Claude Code exposes your rate-limit usage to its **status line**. Clawdmeter piggybacks
on that: a tiny bridge script on your Mac reads the usage percentages and fire-and-forgets
them to a Raspberry Pi driving a 3.5" TFT. The Pi renders an animated dashboard so you can
glance at a desk gadget instead of squinting at a terminal — *"how close am I to the limit?"*
answered by looking up.

Only **percentages** ever leave your machine. No tokens, no prompts, no credentials.

### Four views (tap the touchscreen to cycle)

| View | What it shows |
|------|---------------|
| 🟠 **Splash** | Full-screen animated Clawd — saccades, natural + double blinks, smiles. Face color tracks worst usage (green <20% → orange → yellow → red at 100%), turns into a **scrolling rainbow while you're coding**, and shows a green **"✓ Done!"** celebration when a task finishes. Sleeps after inactivity (closed eyes + `zzz`), wakes on activity. |
| 📊 **Usage** | The 5-hour and weekly rate-limit gauges with reset countdowns **and a "~time to 100%" burn-rate ETA**. The title reads **"Working…"** (rainbow) while coding; the Weekly bar is graded green→yellow→orange→red. |
| 💵 **Stats** | Session cost, context window %, lines added/removed, current model. |
| 📈 **Graph** | Sparkline trend of both windows (5h orange, 7d lime), sampled to disk. |

The view **auto-switches to Usage while you code and back to Splash when idle** (touch still cycles all four).

Extras — optional **[ntfy](https://ntfy.sh) phone push notifications** for: crossing **90%** usage (edge-triggered, re-arms below 85%), a **usage limit resetting** (clock-driven, fires even with Claude Code closed), and a **task finishing**. Plus **≥90% alert mode** (pulsing red, worried brow, screen shake) and auto brightness (100% by day, 45% at night).

---

## Architecture

```
┌────────────────────────┐        HTTP POST /update         ┌──────────────────────────┐
│  Mac — Claude Code      │        {5h%, 7d%, stats}         │  Raspberry Pi 3 B+        │
│                         │  ──────────────────────────────▶ │                          │
│  statusLine hook        │   (fire-and-forget curl,          │  clawdmeter.py (systemd)  │
│  → mac/statusline.py    │    percentages only)              │  HTTP :8080  ─┐           │
└────────────────────────┘                                    │               ▼           │
                                                              │   Pillow + numpy render   │
                                                              │   → /dev/fb (ILI9486 TFT) │
                                                              │   XPT2046 touch → views   │
                                                              └──────────────────────────┘
```

**Camino A (push from the status line).** Claude Code calls `statusline.py` on every
refresh with a JSON blob on stdin; the script extracts `rate_limits.five_hour / seven_day`
(`used_percentage` + `resets_at`) plus session stats, and POSTs them to the Pi. Querying
Anthropic's usage API directly was rejected — the `claude setup-token` token 403s (missing
the `user:profile` scope), so the status line is the clean source of truth.

> **Note:** the meter only updates while Claude Code is open. After ~120s with no push the
> UI dims to "stale" but keeps the last values; after 75s Clawd falls asleep.

---

## Hardware

| Part | Notes |
|------|-------|
| **Raspberry Pi 3 B+** | Any Pi with 40-pin GPIO + SPI works. Runs headless. |
| **3.5" RPi Display, 480×320** | **ILI9486** driver + **XPT2046** resistive touch, HAT-style over GPIO. |
| microSD + power | Standard Pi setup. |

The panel is happiest at **SPI 16 MHz** — at 24/32 MHz it goes green/corrupt. There's no
hardware backlight control (`max_brightness=0`), so brightness is done in software by
scaling the image before packing to RGB565.

---

## Setup — Device (Raspberry Pi)

### 1. Enable the display

Add to `/boot/firmware/config.txt` and reboot:

```ini
dtparam=spi=on
# Clawdmeter 3.5" TFT (ILI9486 480x320 + XPT2046 touch)
dtoverlay=piscreen,speed=16000000,rotate=270,fps=30
```

The framebuffer index (`fb0` vs `fb1`) isn't stable across boots, so the code
**auto-detects** the ILI9486 framebuffer by name — no hardcoded device path.

### 2. Install

```bash
sudo apt update && sudo apt install -y python3-pip
mkdir -p ~/clawdmeter
cp device/clawdmeter.py ~/clawdmeter/
pip3 install -r device/requirements.txt   # Pillow, numpy

# (optional) phone alerts — pick your OWN private, hard-to-guess topic:
export CLAWDMETER_NTFY_TOPIC="clawdmeter-$(openssl rand -hex 4)"
echo "Subscribe to https://ntfy.sh/$CLAWDMETER_NTFY_TOPIC in the ntfy app"
```

### 3. Run as a service

```bash
sudo cp device/clawdmeter.service /etc/systemd/system/
# to enable ntfy, uncomment the Environment= line in the unit first
sudo systemctl daemon-reload
sudo systemctl enable --now clawdmeter
```

Health check: `curl http://<pi>:8080/health`

---

## Setup — Mac (Claude Code bridge)

### 1. Install the bridge

```bash
mkdir -p ~/.claude/clawdmeter
cp mac/statusline.py mac/task_done.py ~/.claude/clawdmeter/
chmod +x ~/.claude/clawdmeter/*.py
```

### 2. Wire the status line and the "task done" hook

In `~/.claude/settings.json`. The `statusLine` feeds usage to the meter; the
`Stop` hook makes Clawd celebrate (and push a notification) when a task finishes:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/clawdmeter/statusline.py"
  },
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python3 ~/.claude/clawdmeter/task_done.py" } ] }
    ]
  }
}
```

### 3. Point it at your Pi (optional)

The default is `http://raspi-one.local:8080/update` (mDNS, survives IP changes).
Override if yours differs:

```bash
export CLAWDMETER_URL="http://<your-pi>.local:8080/update"
```

That's it — open Claude Code and the meter comes alive.

---

## Configuration

Tunables live at the top of `device/clawdmeter.py` (idle timeouts, day/night hours,
history sampling, alert thresholds). Runtime overrides via environment:

| Variable | Where | Default | Purpose |
|----------|-------|---------|---------|
| `CLAWDMETER_NTFY_TOPIC` | Pi | *(empty → off)* | Private ntfy topic for 90% phone alerts. |
| `CLAWDMETER_URL` | Mac | `http://raspi-one.local:8080/update` | Meter endpoint. |

Drop a `~/clawdmeter/splash.gif` on the Pi to replace the mascot with your own animation.

---

## Troubleshooting

- **Green / corrupt screen** → SPI too fast. Keep `speed=16000000` (16 MHz).
- **Blank panel but service is up** → partial framebuffer writes don't refresh this fbtft;
  the code blits a full frame each cycle (~6 fps ceiling). Check `curl .../health`.
- **Meter stuck / grey** → no push in the last ~120s. It only updates while Claude Code
  is open; confirm the status line is set and the Pi is reachable (`ping <pi>.local`).
- **No phone alerts** → `CLAWDMETER_NTFY_TOPIC` unset or you're not subscribed to that
  exact topic in the ntfy app.

---

## Repo layout

```
device/   clawdmeter.py · clawdmeter.service · requirements.txt   # runs on the Pi
mac/      statusline.py       # Claude Code status line -> usage push
          task_done.py        # Claude Code `Stop` hook -> "task done" celebration
```

---

## Privacy

The Mac bridge sends **only** usage percentages, reset timestamps, and coarse session stats
(cost, context %, line counts, model name) over your LAN to the Pi. No prompts, file contents,
tokens, or credentials ever leave your machine.

---

<div align="center">

MIT © cerealskill · Built for [Claude Code](https://claude.com/claude-code)

</div>
