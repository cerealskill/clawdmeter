#!/usr/bin/env python3
"""Clawdmeter - Claude Code usage meter for a 3.5" ILI9486 TFT (480x320).

Two views:
  * SPLASH  - animated Claude mascot (or a custom GIF at ~/clawdmeter/splash.gif)
  * USAGE   - the 5h / weekly usage dashboard

Touching the screen flips SPLASH -> USAGE; after a few idle seconds the meter
drifts back to the splash. Usage data is pushed over HTTP by the Mac's Claude
Code statusline (Camino A) - only percentages ever reach this device.
"""
import colorsys
import glob
import json
import math
import os
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def find_fb():
    """Locate the ILI9486 TFT framebuffer by name (fb0/fb1 order isn't stable)."""
    for p in sorted(glob.glob("/sys/class/graphics/fb*/name")):
        try:
            if "ili9486" in open(p).read().lower():
                return "/dev/" + p.split("/")[-2]
        except OSError:
            pass
    return "/dev/fb1"


FB = find_fb()
W, H = 480, 320
HOME = os.path.expanduser("~/clawdmeter")
STATE_FILE = os.path.join(HOME, "state.json")
HIST_FILE = os.path.join(HOME, "history.json")
GIF_FILE = os.path.join(HOME, "splash.gif")
# ntfy push: install the "ntfy" app and subscribe to your own topic to get
# alerts. Set CLAWDMETER_NTFY_TOPIC to a private, hard-to-guess string of your
# choice (anyone who knows the topic can read/publish to it). Empty = disabled.
NTFY_TOPIC = os.environ.get("CLAWDMETER_NTFY_TOPIC", "")
NTFY_URL = ("https://ntfy.sh/" + NTFY_TOPIC) if NTFY_TOPIC else ""
NOTIFY_AT = 0.90          # push when worst usage crosses this
NOTIFY_REARM = 0.85       # re-arm once it drops back below this
HIST_EVERY = 55           # seconds between history samples
HIST_MAX = 800            # samples kept (~12h at 1/min)
PORT = 8080
STALE_SECS = 120          # dim the "live" dot after this long with no push
IDLE_TO_SPLASH = 20       # seconds on a data view with no touch -> back to SPLASH
AUTO_USAGE_WINDOW = 45    # push within this many seconds => auto-show USAGE (else SPLASH)
ACTIVE_WINDOW = 12        # seconds since last push => actively coding (awake)
SLEEP_AFTER = 75          # seconds with no push => Clawd falls asleep
ALERT_LEVEL = 0.90        # usage fraction at/above which the alert face kicks in
BRIGHT_DAY = 1.00         # daytime brightness
BRIGHT_NIGHT = 0.45       # night-time brightness
DAY_START, DAY_END = 7, 20  # local hours [07:00, 20:00) count as "day"


def current_brightness():
    h = datetime.now().hour
    return BRIGHT_DAY if DAY_START <= h < DAY_END else BRIGHT_NIGHT

# ---- palette ----
BG = (11, 11, 13)
CARD = (26, 26, 29)
TRACK = (44, 44, 49)
WHITE = (240, 240, 242)
GRAY = (140, 140, 148)
DIM = (90, 90, 98)
ORANGE = (255, 90, 31)
LIME = (182, 230, 79)
PILL_BG = (58, 47, 74)
PILL_TX = (206, 190, 230)
GREEN_DOT = (120, 210, 120)
RED = (232, 72, 52)
RED_DK = (150, 40, 30)

DJV = "/usr/share/fonts/truetype/dejavu/"
def _f(name, size):
    return ImageFont.truetype(DJV + name, size)

F_TITLE  = _f("DejaVuSans-Bold.ttf", 30)
F_PCT    = _f("DejaVuSans-Bold.ttf", 46)
F_PILL   = _f("DejaVuSans-Bold.ttf", 16)
F_RESET  = _f("DejaVuSans.ttf", 17)
F_STATUS = _f("DejaVuSans-Bold.ttf", 18)
F_BRAND  = _f("DejaVuSans-Bold.ttf", 34)
F_HINT   = _f("DejaVuSans.ttf", 16)

_lock = threading.Lock()
_state = {
    "five_hour": {"utilization": None, "resets_at": None},
    "seven_day": {"utilization": None, "resets_at": None},
    "status": "waiting for Claude…",
    "stats": {"cost_usd": None, "context_pct": None,
              "lines_added": None, "lines_removed": None, "model": None},
    "updated": 0.0,
}
VIEWS = ("splash", "usage", "stats", "graph")
_view = "splash"          # one of VIEWS
_last_touch = 0.0
_history = []             # [(ts, five_pct, seven_pct), ...]
_hist_last = 0.0
_alerted = False          # edge-trigger state for the 90% push
_reset_fired = {"five_hour": None, "seven_day": None}  # resets_at already alerted


# ---------- data helpers ----------
def norm_pct(v):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, v))   # Claude Code sends 0..100 percentages


def reset_epoch(val):
    """Parse a resets_at value (epoch or ISO string) to epoch seconds, or None."""
    if val is None or val == "":
        return None
    try:
        if isinstance(val, (int, float)) or (
                isinstance(val, str) and val.strip().lstrip("-").isdigit()):
            return float(val)
        s = str(val).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def fmt_reset(val):
    if val is None or val == "":
        return "—"
    try:
        if isinstance(val, (int, float)) or (
                isinstance(val, str) and val.strip().lstrip("-").isdigit()):
            dt = datetime.fromtimestamp(float(val), tz=timezone.utc)
        else:
            s = str(val).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return "—"
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if delta <= 0:
        return "now"
    d, h = int(delta // 86400), int((delta % 86400) // 3600)
    m, sec = int((delta % 3600) // 60), int(delta % 60)
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {sec}s"
    return f"{sec}s"


# ---------- framebuffer ----------
def pack565(img):
    a = np.asarray(img, dtype=np.uint16)
    bright = current_brightness()
    if bright < 0.999:
        a = (a.astype(np.float32) * bright).astype(np.uint16)
    r = (a[:, :, 0] >> 3) & 0x1F
    g = (a[:, :, 1] >> 2) & 0x3F
    b = (a[:, :, 2] >> 3) & 0x1F
    return ((r << 11) | (g << 5) | b).astype("<u2").tobytes()


STRIDE = W * 2   # bytes per row (RGB565)


def blit_bytes(buf):
    with open(FB, "wb") as fb:
        fb.write(buf)


def blit(img):
    blit_bytes(pack565(img))


def blit_region(img, y0, y1):
    """Write only rows [y0, y1) — far fewer bytes over SPI = smoother anim."""
    y0 = max(0, y0)
    y1 = min(H, y1)
    buf = pack565(img.crop((0, y0, W, y1)))
    with open(FB, "r+b") as fb:
        fb.seek(y0 * STRIDE)
        fb.write(buf)


# ---------- Clawd mascot (shared by usage header + splash) ----------
def draw_bug(d, cx, cy, s, wiggle=0.0, blink=False):
    """Draw the red Claude bug centred at (cx, cy). s = half-width of head."""
    ax = int(s * 0.5 * math.sin(wiggle))
    # antennae
    d.line([(cx - s * 0.5, cy - s), (cx - s * 0.7 + ax, cy - s * 1.8)],
           fill=RED, width=max(2, s // 10))
    d.line([(cx + s * 0.5, cy - s), (cx + s * 0.7 + ax, cy - s * 1.8)],
           fill=RED, width=max(2, s // 10))
    d.ellipse([cx - s * 0.7 + ax - 4, cy - s * 1.8 - 4,
               cx - s * 0.7 + ax + 4, cy - s * 1.8 + 4], fill=RED)
    d.ellipse([cx + s * 0.7 + ax - 4, cy - s * 1.8 - 4,
               cx + s * 0.7 + ax + 4, cy - s * 1.8 + 4], fill=RED)
    # head
    d.rounded_rectangle([cx - s, cy - s, cx + s, cy + s],
                        radius=int(s * 0.35), fill=RED)
    # eyes
    ew, eh = max(2, s // 6), max(2, s // 3)
    ey = cy - s * 0.1
    ex = s * 0.42
    if blink:
        d.rectangle([cx - ex - ew, ey + eh // 2, cx - ex + ew, ey + eh // 2 + 3], fill=BG)
        d.rectangle([cx + ex - ew, ey + eh // 2, cx + ex + ew, ey + eh // 2 + 3], fill=BG)
    else:
        d.rounded_rectangle([cx - ex - ew, ey - eh, cx - ex + ew, ey + eh], radius=2, fill=BG)
        d.rounded_rectangle([cx + ex - ew, ey - eh, cx + ex + ew, ey + eh], radius=2, fill=BG)


# Claude Code pixel mascot — terracotta creature (15x11 grid, '#'=body, ' '=hole)
CC_TERRA = (180, 100, 76)
CC_ART = [
    "...#########...",
    "...#########...",
    "..###########..",
    "..##..###..##..",
    "..##..###..##..",
    "..###########..",
    "..###########..",
    "...#########...",
    "...#########...",
    "...#.#...#.#...",
    "...#.#...#.#...",
]


def draw_cc_logo(d, cx, cy, px, color=CC_TERRA):
    """Draw the Claude Code pixel mascot centred at (cx, cy); px = cell size."""
    gw, gh = len(CC_ART[0]), len(CC_ART)
    ox = cx - gw * px / 2.0
    oy = cy - gh * px / 2.0
    for gy, row in enumerate(CC_ART):
        for gx, ch in enumerate(row):
            if ch == "#":
                x = ox + gx * px
                y = oy + gy * px
                d.rectangle([x, y, x + px - 1, y + px - 1], fill=color)


# ---------- USAGE view ----------
def draw_card(d, img, y0, pct, label, bar_color, reset_txt, rainbow_phase=None):
    x0, x1, y1 = 20, W - 20, y0 + 100
    d.rounded_rectangle([x0, y0, x1, y1], radius=16, fill=CARD)
    pct_txt = "—" if pct is None else f"{int(round(pct))}%"
    d.text((x0 + 22, y0 + 14), pct_txt, font=F_PCT, fill=WHITE)
    pw = d.textlength(label, font=F_PILL)
    px1 = x1 - 18
    px0 = px1 - (pw + 26)
    py0 = y0 + 20
    d.rounded_rectangle([px0, py0, px1, py0 + 30], radius=15, fill=PILL_BG)
    d.text((px0 + 13, py0 + 6), label, font=F_PILL, fill=PILL_TX)
    bx0, bx1, by0, by1 = x0 + 22, x1 - 22, y0 + 66, y0 + 80
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=7, fill=TRACK)
    if pct:
        fill_w = int((bx1 - bx0) * min(pct, 100) / 100)
        if fill_w >= 14:
            if rainbow_phase is not None:
                fill_rainbow_rounded(img, bx0, by0, bx0 + fill_w, by1, 7, rainbow_phase)
            else:
                d.rounded_rectangle([bx0, by0, bx0 + fill_w, by1], radius=7, fill=bar_color)
    d.text((x0 + 22, y0 + 84), f"Resets in {reset_txt}", font=F_RESET, fill=GRAY)


_rb_cols_cache = {}


def _rainbow_columns(width, period, phase):
    """Per-column RGB for a horizontal rainbow, cached by (width, quantised phase)."""
    key = (width, int(round(period)), int(phase * 60) % int(period * 60 or 1))
    cols = _rb_cols_cache.get(key)
    if cols is None:
        cols = np.empty((width, 3), np.uint8)
        for x in range(width):
            h = ((x / period) - phase) % 1.0        # minus -> scrolls left->right
            r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
            cols[x] = (int(r * 255), int(g * 255), int(b * 255))
        if len(_rb_cols_cache) > 512:
            _rb_cols_cache.clear()
        _rb_cols_cache[key] = cols
    return cols


def fill_rainbow_rounded(img, x0, y0, x1, y1, radius, phase, period=90.0):
    """Fill a rounded-rect bar with the same scrolling horizontal rainbow."""
    w, h = int(x1 - x0), int(y1 - y0)
    if w <= 0 or h <= 0:
        return
    cols = _rainbow_columns(w, period, phase)
    grad = np.broadcast_to(cols[None, :, :], (h, w, 3)).copy()
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.paste(Image.fromarray(grad, "RGB"), (int(x0), int(y0)), mask)


def draw_rainbow_text(img, x, y, text, font, phase, period=90.0):
    """Draw `text` filled with a scrolling horizontal rainbow (glyph origin x,y)."""
    l, t, r, b = font.getbbox(text)
    tw, th = r - l, b - t
    if tw <= 0 or th <= 0:
        return
    mask = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(mask).text((-l, -t), text, font=font, fill=255)
    cols = _rainbow_columns(tw, period, phase)
    grad = np.broadcast_to(cols[None, :, :], (th, tw, 3)).copy()
    img.paste(Image.fromarray(grad, "RGB"),
              (int(round(x + l)), int(round(y + t))), mask)


def build_usage():
    with _lock:
        st = json.loads(json.dumps(_state))
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    draw_cc_logo(d, 36, 34, 2)
    now = time.time()
    working = st["updated"] > 0 and (now - st["updated"]) < ACTIVE_WINDOW
    if working:                                   # actively coding -> scrolling rainbow
        title = "Working" + "." * (1 + int(now * 2) % 3)
        adv = d.textlength("Working...", font=F_TITLE)   # fixed centre, no jitter
        draw_rainbow_text(img, (W - adv) / 2, 14, title, F_TITLE, now * 0.6)
    else:
        title = "Usage"
        tw = d.textlength(title, font=F_TITLE)
        d.text(((W - tw) / 2, 14), title, font=F_TITLE, fill=WHITE)
    fresh = st["updated"] > 0 and (time.time() - st["updated"]) < STALE_SECS
    d.ellipse([W - 42, 22, W - 28, 36], fill=GREEN_DOT if fresh else GRAY)
    draw_card(d, img, 58, norm_pct(st["five_hour"]["utilization"]), "Current",
              ORANGE, fmt_reset(st["five_hour"]["resets_at"]),
              rainbow_phase=(now * 0.6 if working else None))
    draw_card(d, img, 168, norm_pct(st["seven_day"]["utilization"]), "Weekly",
              LIME, fmt_reset(st["seven_day"]["resets_at"]))
    status = st.get("status") or ""
    stxt = f"✳ {status}"
    sw = d.textlength(stxt, font=F_STATUS)
    d.text(((W - sw) / 2, 290), stxt, font=F_STATUS, fill=ORANGE if fresh else GRAY)
    return img


def build_stats():
    with _lock:
        s = dict(_state["stats"])
        fresh = _state["updated"] > 0 and (time.time() - _state["updated"]) < STALE_SECS
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    draw_cc_logo(d, 36, 34, 2)
    title = "Session"
    tw = d.textlength(title, font=F_TITLE)
    d.text(((W - tw) / 2, 14), title, font=F_TITLE, fill=WHITE)
    d.ellipse([W - 42, 22, W - 28, 36], fill=GREEN_DOT if fresh else GRAY)

    def money(v):
        return f"${v:.2f}" if isinstance(v, (int, float)) else "—"

    def pctv(v):
        return f"{int(round(v))}%" if isinstance(v, (int, float)) else "—"

    rows = [
        ("Cost", money(s.get("cost_usd")), WHITE),
        ("Context", pctv(s.get("context_pct")), WHITE),
        ("Model", s.get("model") or "—", WHITE),
    ]
    y = 66
    for label, val, col in rows:
        d.rounded_rectangle([20, y, W - 20, y + 52], radius=12, fill=CARD)
        d.text((40, y + 15), label, font=F_RESET, fill=GRAY)
        vw = d.textlength(val, font=F_STATUS)
        d.text((W - 40 - vw, y + 14), val, font=F_STATUS, fill=col)
        y += 60

    # lines +/- row, coloured
    d.rounded_rectangle([20, y, W - 20, y + 52], radius=12, fill=CARD)
    d.text((40, y + 15), "Lines", font=F_RESET, fill=GRAY)
    la, lr = s.get("lines_added"), s.get("lines_removed")
    if isinstance(la, (int, float)) or isinstance(lr, (int, float)):
        minus = f"-{int(lr or 0)}"
        plus = f"+{int(la or 0)}"
        mw = d.textlength(minus, font=F_STATUS)
        pw = d.textlength(plus, font=F_STATUS)
        d.text((W - 40 - mw, y + 14), minus, font=F_STATUS, fill=RED)
        d.text((W - 40 - mw - 12 - pw, y + 14), plus, font=F_STATUS, fill=LIME)
    else:
        d.text((W - 40 - d.textlength("—", font=F_STATUS), y + 14), "—",
               font=F_STATUS, fill=WHITE)

    hint = "tap to cycle"
    hw = d.textlength(hint, font=F_HINT)
    d.text(((W - hw) / 2, 298), hint, font=F_HINT, fill=DIM)
    return img


def build_graph():
    with _lock:
        hist = list(_history)
        fa = norm_pct(_state["five_hour"]["utilization"])
        fb = norm_pct(_state["seven_day"]["utilization"])
        fresh = _state["updated"] > 0 and (time.time() - _state["updated"]) < STALE_SECS
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    draw_cc_logo(d, 36, 34, 2)
    title = "Trend"
    tw = d.textlength(title, font=F_TITLE)
    d.text(((W - tw) / 2, 14), title, font=F_TITLE, fill=WHITE)
    d.ellipse([W - 42, 22, W - 28, 36], fill=GREEN_DOT if fresh else GRAY)

    px0, py0, px1, py1 = 30, 66, W - 20, 248
    d.rounded_rectangle([px0, py0, px1, py1], radius=12, fill=CARD)
    for frac in (0.0, 0.5, 1.0):                      # 0 / 50 / 100% gridlines
        gy = py1 - (py1 - py0) * frac
        d.line([(px0 + 8, gy), (px1 - 8, gy)], fill=(52, 52, 58), width=1)

    samples = [s for s in hist if s[1] is not None or s[2] is not None]
    if len(samples) < 2:
        msg = "gathering data…"
        mw = d.textlength(msg, font=F_RESET)
        d.text(((W - mw) / 2, (py0 + py1) / 2 - 10), msg, font=F_RESET, fill=GRAY)
    else:
        t0, t1 = samples[0][0], samples[-1][0]
        span = max(1.0, t1 - t0)

        def pt(ts, pct):
            x = px0 + 10 + (px1 - px0 - 20) * (ts - t0) / span
            y = py1 - 8 - (py1 - py0 - 16) * min(100.0, max(0.0, pct)) / 100.0
            return (x, y)

        for idx, color in ((1, ORANGE), (2, LIME)):
            line = [pt(s[0], s[idx]) for s in samples if s[idx] is not None]
            if len(line) >= 2:
                d.line(line, fill=color, width=2, joint="curve")

    # legend + current values
    d.rectangle([40, 266, 58, 278], fill=ORANGE)
    d.text((64, 262), f"5h {int(round(fa)) if fa is not None else '—'}%",
           font=F_RESET, fill=WHITE)
    d.rectangle([210, 266, 228, 278], fill=LIME)
    d.text((234, 262), f"7d {int(round(fb)) if fb is not None else '—'}%",
           font=F_RESET, fill=WHITE)
    hint = "tap to cycle"
    hw = d.textlength(hint, font=F_HINT)
    d.text(((W - hw) / 2, 298), hint, font=F_HINT, fill=DIM)
    return img


# ---------- SPLASH view: full-screen Clawd face with animated eyes ----------
# face colour by usage level: <20% green, 20% orange, 60% yellow, 100% red
GREEN_TOP = (74, 200, 96)
GREEN_BOT = (40, 150, 70)
ORANGE_TOP = (240, 138, 44)
ORANGE_BOT = (216, 96, 34)
YELLOW_TOP = (245, 206, 54)
YELLOW_BOT = (228, 176, 36)
RED_TOP = (214, 46, 32)
RED_BOT = (150, 20, 16)
EYE_DARK = (30, 24, 26)        # near-black eyes
USE_TXT = (245, 245, 245)      # subtle usage line (white)

_YS = np.linspace(0.0, 1.0, H)[:, None]
_grad_cache = {}


def _lerp3(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _level_colors(level):
    """green (<20%) -> orange (20%) -> yellow (60%) -> red (100%)."""
    if level < 0.20:
        return GREEN_TOP, GREEN_BOT
    t = (level - 0.20) / 0.80          # remap 20%..100% onto 0..1
    if t <= 0.5:
        u = t / 0.5
        return _lerp3(ORANGE_TOP, YELLOW_TOP, u), _lerp3(ORANGE_BOT, YELLOW_BOT, u)
    u = (t - 0.5) / 0.5
    return _lerp3(YELLOW_TOP, RED_TOP, u), _lerp3(YELLOW_BOT, RED_BOT, u)


def splash_bg(level):
    """Gradient face tinted by usage level (0..1). Cached per 2%."""
    level = max(0.0, min(1.0, level))
    key = int(round(level * 50))
    img = _grad_cache.get(key)
    if img is None:
        top_c, bot_c = _level_colors(key / 50.0)
        top = np.array(top_c, dtype=np.float32)
        bot = np.array(bot_c, dtype=np.float32)
        col = (top * (1 - _YS) + bot * _YS).astype(np.uint8)
        arr = np.broadcast_to(col[:, None, :], (H, W, 3)).copy()
        img = Image.fromarray(arr, "RGB")
        _grad_cache[key] = img
    return img


ALERT_DK = (120, 12, 10)       # alert pulse: dark red
ALERT_BR = (240, 60, 42)       # alert pulse: bright red


def alert_bg(pulse):
    """Solid red that pulses dark<->bright (0..1). Not cached (changes每frame)."""
    c = _lerp3(ALERT_DK, ALERT_BR, pulse)
    top = np.array(c, dtype=np.float32)
    bot = np.array(_lerp3((90, 8, 8), c, 0.7), dtype=np.float32)
    col = (top * (1 - _YS) + bot * _YS).astype(np.uint8)
    arr = np.broadcast_to(col[:, None, :], (H, W, 3)).copy()
    return Image.fromarray(arr, "RGB")


EYE_HALF = 33          # half-height of a fully-open eye
EYE_HW = 30            # half-width of an eye


def draw_eye(d, cx, cy, openness, happy=False):
    """openness 0..1 squashes the eye vertically (natural blink); happy = ^."""
    if happy:
        w = 13
        d.line([(cx - 31, cy + 15), (cx, cy - 17)], fill=EYE_DARK, width=w)
        d.line([(cx, cy - 17), (cx + 31, cy + 15)], fill=EYE_DARK, width=w)
        for px, py in [(cx - 31, cy + 15), (cx, cy - 17), (cx + 31, cy + 15)]:
            d.ellipse([px - w / 2, py - w / 2, px + w / 2, py + w / 2], fill=EYE_DARK)
        return
    half = max(4, int(EYE_HALF * openness))
    d.rounded_rectangle([cx - EYE_HW, cy - half, cx + EYE_HW, cy + half],
                        radius=min(11, half), fill=EYE_DARK)


def draw_closed_eye(d, cx, cy):
    """Peaceful closed eye: a gentle downward arc (for sleeping)."""
    d.arc([cx - EYE_HW, cy - 16, cx + EYE_HW, cy + 18], start=20, end=160,
          fill=EYE_DARK, width=7)


def draw_worried_brow(d, cx, cy, inner_dx):
    """Angry/urgent eyebrow above an eye; inner_dx points toward face centre."""
    outer = (cx - inner_dx * 0.9, cy - 40)
    inner = (cx + inner_dx * 0.9, cy - 26)   # inner end lower = tense
    d.line([outer, inner], fill=EYE_DARK, width=8)


def draw_zzz(d, t):
    """Floating z z z near the top-right while sleeping."""
    phase = (t * 0.6) % 1.0
    base_x, base_y = 300, 150
    for i in range(3):
        f = (phase + i / 3.0) % 1.0
        x = base_x + i * 26 + f * 10
        y = base_y - 60 - i * 26 - f * 24
        sz = 18 + i * 8
        alpha = int(200 * (1 - f))
        col = (200 + alpha // 6, 200, 205)
        d.text((x, y), "z", font=_f("DejaVuSans-Bold.ttf", sz), fill=col)


# ---- living-eyes behaviour (updated once per frame, single-threaded) ----
_anim = {
    "lx": 0.0, "ly": 0.0, "tx": 0.0, "ty": 0.0, "next_look": 0.0,
    "open": 1.0, "blinking": False, "blink_t0": 0.0, "blink_dur": 0.28,
    "double": False, "next_blink": 0.0,
    "happy_until": 0.0, "next_happy": 6.0, "last": 0.0,
}


def _update_eyes(now, busy=False):
    a = _anim
    dt = min(0.12, now - a["last"]) if a["last"] else 0.0
    a["last"] = now

    # saccades: glance to a new spot, sometimes recenter (faster when busy)
    if now >= a["next_look"]:
        if random.random() < 0.30:
            a["tx"], a["ty"] = 0.0, 0.0
        else:
            a["tx"] = random.uniform(-17, 17)
            a["ty"] = random.uniform(-9, 7)
        lo, hi = (0.4, 1.3) if busy else (1.1, 3.8)
        a["next_look"] = now + random.uniform(lo, hi)
    k = min(1.0, dt * 13)                       # fast ease = darting motion
    a["lx"] += (a["tx"] - a["lx"]) * k
    a["ly"] += (a["ty"] - a["ly"]) * k

    # natural blink (squash to a line and back), sometimes a double blink
    if not a["blinking"] and now >= a["next_blink"]:
        a["blinking"] = True
        a["blink_t0"] = now
        a["double"] = random.random() < 0.3
        a["next_blink"] = now + random.uniform(2.2, 6.0)
    if a["blinking"]:
        p = (now - a["blink_t0"]) / a["blink_dur"]
        if p >= 1.0:
            if a["double"]:
                a["double"] = False
                a["blink_t0"] = now
                p = 0.0
            else:
                a["blinking"] = False
        a["open"] = abs(1.0 - 2.0 * min(1.0, p)) if a["blinking"] else 1.0
    else:
        a["open"] = 1.0

    # occasional spontaneous smile
    if a["happy_until"] < now and now >= a["next_happy"]:
        if random.random() < 0.6:
            a["happy_until"] = now + random.uniform(1.0, 1.9)
        a["next_happy"] = now + random.uniform(7, 15)


def build_splash(t):
    with _lock:
        fa = norm_pct(_state["five_hour"]["utilization"])
        fb = norm_pct(_state["seven_day"]["utilization"])
        updated = _state["updated"]
    level = max(fa or 0.0, fb or 0.0) / 100.0     # face colour follows worst limit
    sleeping = (updated == 0.0) or (t - updated > SLEEP_AFTER)
    working = (not sleeping) and updated > 0 and (t - updated) < ACTIVE_WINDOW
    alert = (level >= ALERT_LEVEL) and not sleeping
    _update_eyes(t, busy=working)
    a = _anim

    if alert:
        img = alert_bg(0.5 + 0.5 * math.sin(t * 5.0))      # pulsing red
    else:
        img = splash_bg(level).copy()
    d = ImageDraw.Draw(img)

    ex_l, ex_r = 172, 308
    if sleeping:
        cy = 150 + int(round(math.sin(t * 1.1) * 4))       # slow deep breathing
        draw_closed_eye(d, ex_l, cy)
        draw_closed_eye(d, ex_r, cy)
        draw_zzz(d, t)
    elif alert:
        j = int(round(math.sin(t * 22) * 3))               # nervous shake
        draw_eye(d, ex_l + j, 150, 1.0)
        draw_eye(d, ex_r + j, 150, 1.0)
        draw_worried_brow(d, ex_l + j, 150, EYE_HW)
        draw_worried_brow(d, ex_r + j, 150, -EYE_HW)
    else:
        happy = a["happy_until"] > t
        bob = math.sin(t * 1.8) * 3
        dx = int(round(a["lx"]))
        cy = 150 + int(round(a["ly"] + bob))
        openness = a["open"]
        if working and not happy:
            openness = min(openness, 0.72)     # focused squint
            cy += 4                            # eyes down at "the work"
        draw_eye(d, ex_l + dx, cy, openness, happy)
        draw_eye(d, ex_r + dx, cy, openness, happy)

    # subtle usage line at the bottom (like the reference)
    if fa is not None or fb is not None:
        av = "—" if fa is None else f"{int(round(fa))}%"
        bv = "—" if fb is None else f"{int(round(fb))}%"
        txt = f"5H {av} USED · 7D {bv} USED"
        tw = d.textlength(txt, font=F_HINT)
        d.text(((W - tw) / 2, 296), txt, font=F_HINT, fill=USE_TXT)
    return img


def load_gif_frames():
    """Return [(rgb565_bytes, duration_s), ...] from splash.gif, or None."""
    if not os.path.exists(GIF_FILE):
        return None
    try:
        im = Image.open(GIF_FILE)
        frames = []
        for i in range(getattr(im, "n_frames", 1)):
            im.seek(i)
            fr = im.convert("RGB")
            # contain into 480x320, centred on black
            fr.thumbnail((W, H))
            canvas = Image.new("RGB", (W, H), BG)
            canvas.paste(fr, ((W - fr.width) // 2, (H - fr.height) // 2))
            dur = im.info.get("duration", 80) / 1000.0
            frames.append((pack565(canvas), max(0.03, dur)))
            if len(frames) >= 120:
                break
        return frames or None
    except Exception:
        return None


# ---------- render loop ----------
EYE_BAND = (96, 204)   # rows the eyes/animation live in (covers look + blink)


def render_loop():
    global _view
    gif = load_gif_frames()
    gi = 0
    last_full = 0.0
    while True:
        now = time.time()
        with _lock:
            updated = _state["updated"]
        active = updated > 0 and (now - updated) < AUTO_USAGE_WINDOW
        # Auto-drive the view by activity, unless the user is navigating by touch:
        # coding right now -> USAGE, stopped -> SPLASH. Touch overrides for a grace.
        if now - _last_touch > IDLE_TO_SPLASH:
            _view = "usage" if active else "splash"
        v = _view
        if v in ("usage", "stats", "graph"):
            try:
                blit({"usage": build_usage, "stats": build_stats,
                      "graph": build_graph}[v]())
            except Exception:
                pass
            time.sleep(0.12 if (v == "usage" and active) else 0.4)  # smooth rainbow
        else:
            try:
                if gif:
                    buf, dur = gif[gi % len(gif)]
                    blit_bytes(buf)
                    gi += 1
                    time.sleep(dur)
                else:
                    blit(build_splash(time.time()))
                    time.sleep(0.01)
            except Exception:
                time.sleep(0.2)


# ---------- touch ----------
def touch_loop():
    global _view, _last_touch
    try:
        from evdev import InputDevice, ecodes, list_devices
    except ImportError:
        return
    dev = None
    for path in list_devices():
        try:
            d = InputDevice(path)
            if "ads7846" in d.name.lower() or "touch" in d.name.lower():
                dev = d
                break
        except OSError:
            continue
    if dev is None:
        return
    for event in dev.read_loop():
        if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH \
                and event.value == 1:
            _last_touch = time.time()
            _view = VIEWS[(VIEWS.index(_view) + 1) % len(VIEWS)]


# ---------- persistence ----------
def save_state():
    os.makedirs(HOME, exist_ok=True)
    with _lock:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        with _lock:
            _state.update(data)
            _state["updated"] = 0.0
    except (OSError, ValueError):
        pass


def save_history():
    try:
        with _lock:
            data = list(_history)
        with open(HIST_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def load_history():
    global _history
    try:
        with open(HIST_FILE) as f:
            _history = [tuple(x) for x in json.load(f)][-HIST_MAX:]
    except (OSError, ValueError):
        _history = []


def send_ntfy(title, message):
    if not NTFY_URL:  # topic not configured -> notifications disabled
        return
    try:
        subprocess.Popen(
            ["curl", "-s", "-m", "6", "-H", f"Title: {title}",
             "-H", "Priority: high", "-H", "Tags: warning",
             "-d", message, NTFY_URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def record_and_notify():
    """Sample history (throttled) and fire the 90% push (edge-triggered)."""
    global _hist_last, _alerted
    now = time.time()
    with _lock:
        fa = norm_pct(_state["five_hour"]["utilization"])
        fb = norm_pct(_state["seven_day"]["utilization"])
    level = max(fa or 0.0, fb or 0.0) / 100.0
    if now - _hist_last >= HIST_EVERY:
        _hist_last = now
        with _lock:
            _history.append((now, fa, fb))
            del _history[:-HIST_MAX]
        save_history()
    if level >= NOTIFY_AT and not _alerted:
        _alerted = True
        which = "5h" if (fa or 0) >= (fb or 0) else "weekly"
        send_ntfy("⚠️ Claude usage high",
                  f"{which} at {int(round(level * 100))}% — approaching the limit")
    elif level < NOTIFY_REARM:
        _alerted = False


def check_resets():
    """Fire a push when a usage window's reset time arrives (edge-triggered).

    Works without any push because it's clock-driven: the moment 'now' passes a
    window's resets_at, quota is back. Each reset alerts once; a new (future)
    resets_at re-arms it automatically.
    """
    now = time.time()
    for key, label in (("five_hour", "5-hour"), ("seven_day", "weekly")):
        with _lock:
            ra = _state[key]["resets_at"]
        ep = reset_epoch(ra)
        if ep is None:
            continue
        if now >= ep and _reset_fired.get(key) != ep:
            _reset_fired[key] = ep
            send_ntfy("♻️ Limit reset",
                      f"Your {label} limit just reset — full quota again")


def reset_watch_loop():
    """Background clock watcher so resets fire even with Claude Code closed."""
    while True:
        try:
            check_resets()
        except Exception:
            pass
        time.sleep(20)


# ---------- http ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "view": _view, "state": _state})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/update":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            self._json(400, {"error": "bad json"})
            return
        with _lock:
            for k in ("five_hour", "seven_day", "stats"):
                if isinstance(payload.get(k), dict):
                    _state[k].update(payload[k])
            if "status" in payload:
                _state["status"] = payload["status"]
            _state["updated"] = time.time()
        save_state()
        record_and_notify()
        self._json(200, {"ok": True})


def main():
    import sys
    if "--test" in sys.argv:
        with _lock:
            _state["five_hour"] = {"utilization": 50, "resets_at": _iso_in(hours=1, minutes=22)}
            _state["seven_day"] = {"utilization": 11, "resets_at": _iso_in(days=6, hours=8)}
            _state["status"] = "Musing…"
            _state["updated"] = time.time()
        blit(build_usage())
        print("usage test frame rendered to", FB)
        return
    if "--splash-test" in sys.argv:
        for _ in range(120):
            blit(build_splash(time.time()))
            time.sleep(0.07)
        return
    load_state()
    load_history()
    # suppress a stale "reset" alert on boot for windows already past their reset
    now0 = time.time()
    for key in ("five_hour", "seven_day"):
        ep = reset_epoch(_state[key]["resets_at"])
        if ep is not None and now0 >= ep:
            _reset_fired[key] = ep
    threading.Thread(target=render_loop, daemon=True).start()
    threading.Thread(target=touch_loop, daemon=True).start()
    threading.Thread(target=reset_watch_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"clawdmeter listening on :{PORT}")
    srv.serve_forever()


def _iso_in(days=0, hours=0, minutes=0):
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(days=days, hours=hours, minutes=minutes)).isoformat()


if __name__ == "__main__":
    main()
