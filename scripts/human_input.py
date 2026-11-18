#!/usr/bin/env python3
"""
human_input.py — OS-level human input engine for X11 (XTest via libXtst).

No CDP, no WebDriver, no xdotool subprocess per event: every motion/key is a
real X server input event, indistinguishable from physical hardware at the
browser level (navigator.webdriver stays False, isTrusted=true).

Algorithms (5-layer, ported to XTest):
  1. Cubic Bezier path with randomized asymmetric control points
  2. Fitts's Law duration: t = a + b * log2(D/W + 1)
  3. Minimum-jerk velocity profile (5th order polynomial)
  4. Physiological tremor (base 0.3px, velocity-scaled)
  5. Overshoot + smooth correction on long ballistic moves

Typing: per-key hold time jitter, inter-key delay spread, 10% pause bursts,
explicit Shift down/up latency for capitals and symbols.

CLI:
  python3 human_input.py move 800 500
  python3 human_input.py click 800 500
  python3 human_input.py type "hello world"
  python3 human_input.py scroll 5
  python3 human_input.py key Return

Library:
  from human_input import Human
  h = Human(display=":1")
  h.click(800, 500)
  h.type("hello")
"""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import os
import random
import sys
import time

# --------------------------------------------------------------------------
# X11 / XTest bindings
# --------------------------------------------------------------------------

x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")

x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XFlush.argtypes = [ctypes.c_void_p]
x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
x11.XStringToKeysym.restype = ctypes.c_ulong
x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
x11.XKeysymToKeycode.restype = ctypes.c_ubyte
x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
x11.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]

xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                      ctypes.c_int, ctypes.c_ulong]
xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_ulong]

# --------------------------------------------------------------------------
# Keysym table for characters that don't map 1:1 by name
# --------------------------------------------------------------------------

SYMBOL_KEYSYMS = {
    " ": "space", "!": "exclam", '"': "quotedbl", "#": "numbersign",
    "$": "dollar", "%": "percent", "&": "ampersand", "'": "apostrophe",
    "(": "parenleft", ")": "parenright", "*": "asterisk", "+": "plus",
    ",": "comma", "-": "minus", ".": "period", "/": "slash",
    ":": "colon", ";": "semicolon", "<": "less", "=": "equal",
    ">": "greater", "?": "question", "@": "at", "[": "bracketleft",
    "\\": "backslash", "]": "bracketright", "^": "asciicircum",
    "_": "underscore", "`": "grave", "{": "braceleft", "|": "bar",
    "}": "braceright", "~": "asciitilde", "\n": "Return", "\t": "Tab",
}

SPECIAL_KEYS = {
    "enter": "Return", "return": "Return", "tab": "Tab", "esc": "Escape",
    "escape": "Escape", "backspace": "BackSpace", "delete": "Delete",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "space": "space", "shift": "Shift_L", "ctrl": "Control_L", "alt": "Alt_L",
    "win": "Super_L", "cmd": "Super_L",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5", "f6": "F6",
    "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _rand(rng):
    return random.uniform(rng[0], rng[1])


def _sleep_ms(ms):
    time.sleep(max(0.0, ms) / 1000.0)


def _min_jerk(t: float) -> float:
    """5th-order minimum-jerk profile: smooth accel + decel, zero jerk endpoints."""
    return t * t * t * (10 - 15 * t + 6 * t * t)


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
        u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
    )


# --------------------------------------------------------------------------
# Main engine
# --------------------------------------------------------------------------

class Human:
    """Human-like X11 input driver."""

    def __init__(self, display: str | None = None, preset: str = "default"):
        disp = (display or os.environ.get("DISPLAY") or ":0").encode()
        self.dpy = x11.XOpenDisplay(disp)
        if not self.dpy:
            raise RuntimeError(f"cannot open X display {disp!r}")
        self.width = x11.XDisplayWidth(self.dpy, 0)
        self.height = x11.XDisplayHeight(self.dpy, 0)
        self._keysym_cache: dict[str, int] = {}
        self._keycode_cache: dict[int, int] = {}

        # cursor position tracking (X has no cheap getter without XQueryPointer)
        self.x = float(random.uniform(400, 700))
        self.y = float(random.uniform(45, 60))

        self._apply_preset(preset)

    # ---------------- config ----------------
    def _apply_preset(self, preset: str):
        if preset == "careful":
            self.typing_delay, self.typing_spread = 100.0, 50.0
            self.pause_chance, self.pause_range = 0.15, (500, 1200)
            self.key_hold = (20, 45)
            self.overshoot_chance, self.overshoot_px = 0.10, (10, 30)
            self.fitts_a, self.fitts_b = 90.0, 150.0
        elif preset == "aggressive":
            self.typing_delay, self.typing_spread = 40.0, 20.0
            self.pause_chance, self.pause_range = 0.03, (200, 500)
            self.key_hold = (12, 28)
            self.overshoot_chance, self.overshoot_px = 0.05, (3, 8)
            self.fitts_a, self.fitts_b = 40.0, 100.0
        else:  # default
            self.typing_delay, self.typing_spread = 70.0, 40.0
            self.pause_chance, self.pause_range = 0.10, (400, 1000)
            self.key_hold = (15, 35)
            self.overshoot_chance, self.overshoot_px = 0.15, (15, 45)
            self.fitts_a, self.fitts_b = 50.0, 120.0
        self.shift_down = (30, 70)
        self.shift_up = (20, 50)
        self.aim_delay = (60, 200)
        self.click_hold = (40, 150)

    # ---------------- low level ----------------
    def _flush(self):
        x11.XFlush(self.dpy)

    def _move_abs(self, x, y):
        xtst.XTestFakeMotionEvent(self.dpy, -1, int(round(x)), int(round(y)), 0)

    def _btn(self, b, down):
        xtst.XTestFakeButtonEvent(self.dpy, b, 1 if down else 0, 0)

    def _key(self, keycode, down):
        xtst.XTestFakeKeyEvent(self.dpy, keycode, 1 if down else 0, 0)

    def _keysym(self, name: str) -> int:
        if name in self._keysym_cache:
            return self._keysym_cache[name]
        ks = x11.XStringToKeysym(name.encode())
        self._keysym_cache[name] = ks
        return ks

    def _keycode(self, keysym: int) -> int:
        if keysym in self._keycode_cache:
            return self._keycode_cache[keysym]
        kc = x11.XKeysymToKeycode(self.dpy, keysym)
        self._keycode_cache[keysym] = kc
        return kc

    # ---------------- mouse ----------------
    def move(self, tx: float, ty: float, duration: float | None = None):
        """Bezier + Fitts + min-jerk + tremor + overshoot mouse move."""
        sx, sy = self.x, self.y
        dist = math.hypot(tx - sx, ty - sy)
        if dist < 1:
            return

        if duration is None:
            # Fitts's law: more distance -> longer, bigger target -> shorter
            target_w = max(8.0, min(120.0, dist * 0.15))
            duration = (self.fitts_a + self.fitts_b * math.log2(dist / target_w + 1)) / 1000.0
            duration = max(0.08, min(1.6, duration * random.uniform(0.85, 1.15)))

        # overshoot target on long ballistic moves
        ox, oy = tx, ty
        if dist > 400 and random.random() < self.overshoot_chance:
            ang = math.atan2(ty - sy, tx - sx)
            mag = random.uniform(*self.overshoot_px)
            ox = tx + math.cos(ang) * mag
            oy = ty + math.sin(ang) * mag

        # control points, asymmetric, perpendicular bias
        dx, dy = ox - sx, oy - sy
        perp_x, perp_y = -dy / dist, dx / dist
        b1 = random.uniform(-0.30, 0.30) * dist
        b2 = random.uniform(-0.30, 0.30) * dist
        p1 = (sx + dx * 0.25 + perp_x * b1, sy + dy * 0.25 + perp_y * b1)
        p2 = (sx + dx * 0.75 + perp_x * b2, sy + dy * 0.75 + perp_y * b2)

        steps = int(max(25, min(120, dist / 8)))
        t0 = time.perf_counter()
        prev = (sx, sy)
        for i in range(1, steps + 1):
            raw_t = i / steps
            t = _min_jerk(raw_t)
            px, py = _bezier((sx, sy), p1, p2, (ox, oy), t)
            # physiological tremor, scaled by instantaneous speed
            speed = math.hypot(px - prev[0], py - prev[1])
            amp = 0.3 + min(1.2, speed * 0.08)
            px += random.uniform(-amp, amp)
            py += random.uniform(-amp, amp)
            self._move_abs(px, py)
            prev = (px, py)
            # pace the events across the whole duration
            target_elapsed = duration * raw_t
            now = time.perf_counter() - t0
            if target_elapsed > now:
                time.sleep(target_elapsed - now)
        self._flush()

        # correction if we overshot
        if (ox, oy) != (tx, ty):
            self._move_abs(tx, ty)
            self._flush()

        self.x, self.y = float(tx), float(ty)

    def click(self, x: float | None = None, y: float | None = None,
              button: int = 1, double: bool = False):
        if x is not None and y is not None:
            self.move(x, y)
        # aim pause before pressing
        _sleep_ms(_rand(self.aim_delay))
        taps = 2 if double else 1
        for i in range(taps):
            self._btn(button, True)
            self._flush()
            _sleep_ms(_rand(self.click_hold))
            self._btn(button, False)
            self._flush()
            if i + 1 < taps:
                _sleep_ms(random.uniform(40, 90))

    def scroll(self, clicks: int, at: tuple[float, float] | None = None):
        """Wheel scroll with accel/decel and occasional settle pause."""
        if at:
            self.move(*at)
            _sleep_ms(random.uniform(100, 300))
        direction = 4 if clicks > 0 else 5          # 4 = up, 5 = down
        n = abs(clicks)
        i = 0
        while i < n:
            burst = min(n - i, random.randint(2, 3))
            for _ in range(burst):
                self._btn(direction, True)
                self._btn(direction, False)
                self._flush()
                _sleep_ms(random.uniform(60, 150))
                i += 1
            if i < n:
                _sleep_ms(random.uniform(150, 400))
                if random.random() < 0.15:
                    other = 5 if direction == 4 else 4
                    self._btn(other, True)
                    self._btn(other, False)
                    self._flush()
                    _sleep_ms(random.uniform(250, 500))
        self._flush()

    def idle(self, seconds: float = 1.0):
        """Small cursor drift, so the pointer never looks frozen."""
        end = time.time() + seconds
        while time.time() < end:
            self._move_abs(self.x + random.uniform(-3, 3), self.y + random.uniform(-3, 3))
            self._flush()
            time.sleep(random.uniform(0.3, 1.0))
        self._move_abs(self.x, self.y)
        self._flush()

    # ---------------- keyboard ----------------
    def _press(self, char: str):
        name = SYMBOL_KEYSYMS.get(char)
        keysym = self._keysym(name) if name else self._keysym(char)
        if not keysym:
            return False
        kc = self._keycode(keysym)
        if not kc:
            return False
        # does this keycode produce the wanted keysym unshifted?
        needs_shift = char.isupper() or (char in SYMBOL_KEYSYMS and
                                         char not in " \n\t" and
                                         not char.isalnum())
        if needs_shift:
            shift_kc = self._keycode(self._keysym("Shift_L"))
            self._key(shift_kc, True)
            self._flush()
            _sleep_ms(_rand(self.shift_down))
            self._key(kc, True)
            self._flush()
            _sleep_ms(_rand(self.key_hold))
            self._key(kc, False)
            self._flush()
            _sleep_ms(_rand(self.shift_up))
            self._key(shift_kc, False)
            self._flush()
        else:
            self._key(kc, True)
            self._flush()
            _sleep_ms(_rand(self.key_hold))
            self._key(kc, False)
            self._flush()
        return True

    def type(self, text: str, wpm: float | None = None):
        """Type a string with human timing."""
        if wpm:
            base = 60000.0 / (wpm * 5.0)     # ms per char
            self.typing_delay = base
            self.typing_spread = base * 0.5
        for i, ch in enumerate(text):
            if not self._press(ch):
                continue
            if i + 1 < len(text):
                if random.random() < self.pause_chance:
                    _sleep_ms(_rand(self.pause_range))
                else:
                    delay = self.typing_delay + random.uniform(-1, 1) * self.typing_spread
                    _sleep_ms(max(10, delay))

    def key(self, name: str, times: int = 1, combo: list[str] | None = None):
        """Press a named key (or a combo like ctrl+t)."""
        if combo:
            codes = [self._keycode(self._keysym(SPECIAL_KEYS.get(k.lower(), k))) for k in combo]
            for kc in codes:
                self._key(kc, True)
                self._flush()
                _sleep_ms(random.uniform(20, 50))
            for kc in reversed(codes):
                self._key(kc, False)
                self._flush()
                _sleep_ms(random.uniform(15, 40))
            return
        keysym = self._keysym(SPECIAL_KEYS.get(name.lower(), name))
        kc = self._keycode(keysym)
        for _ in range(times):
            _sleep_ms(random.uniform(40, 120))
            self._key(kc, True)
            self._flush()
            _sleep_ms(_rand(self.key_hold))
            self._key(kc, False)
            self._flush()

    def paste(self, text: str):
        """Clipboard paste for unicode / long text (xclip required)."""
        import subprocess
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(),
                       check=True, env={**os.environ})
        self.key("v", combo=["ctrl"])

    def close(self):
        try:
            x11.XCloseDisplay(self.dpy)
        except Exception:
            pass


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    h = Human(display=os.environ.get("DISPLAY"), preset=os.environ.get("HUMAN_PRESET", "default"))
    try:
        if cmd == "move":
            h.move(float(argv[2]), float(argv[3]))
        elif cmd == "click":
            h.click(float(argv[2]), float(argv[3]),
                    button=int(argv[4]) if len(argv) > 4 else 1)
        elif cmd == "dclick":
            h.click(float(argv[2]), float(argv[3]), double=True)
        elif cmd == "type":
            h.type(argv[2])
        elif cmd == "scroll":
            h.scroll(int(argv[2]))
        elif cmd == "key":
            h.key(argv[2])
        elif cmd == "idle":
            h.idle(float(argv[2]) if len(argv) > 2 else 1.0)
        elif cmd == "probe":
            print(f"display ok  {h.width}x{h.height}")
        else:
            print(f"unknown command {cmd}")
            return 2
    finally:
        h.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
