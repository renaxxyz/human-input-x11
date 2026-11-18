#!/usr/bin/env python3
"""
Headless invariants test for the human_input kinematics.

No X server needed: the XTest event sink is monkeypatched into a recorder before
any motion is generated, so this runs in CI.

    python3 tests/kinematics_test.py
"""
from __future__ import annotations

import os
import sys
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import human_input as hi


class Recorder:
    """Stand-in for the X display object: records injected events."""

    def __init__(self):
        self.moves = []
        self.buttons = []
        self.keys = []
        self.width = 1920
        self.height = 1080

    def XDisplayWidth(self, dpy, screen):
        return self.width

    def XDisplayHeight(self, dpy, screen):
        return self.height

    def XFlush(self, dpy):
        pass

    def XCloseDisplay(self, dpy):
        pass

    def XStringToKeysym(self, name):
        return 0x41

    def XKeysymToKeycode(self, dpy, keysym):
        return 38


def make_human(preset="default"):
    rec = Recorder()

    class FakeX11:
        XOpenDisplay = staticmethod(lambda d: 1)
        XFlush = staticmethod(lambda d: None)
        XCloseDisplay = staticmethod(lambda d: None)
        XStringToKeysym = staticmethod(lambda n: 0x41)
        XKeysymToKeycode = staticmethod(lambda d, k: 38)
        XDisplayWidth = staticmethod(lambda d, s: 1920)
        XDisplayHeight = staticmethod(lambda d, s: 1080)

    class FakeXtst:
        @staticmethod
        def XTestFakeMotionEvent(dpy, screen, x, y, delay):
            rec.moves.append((x, y))

        @staticmethod
        def XTestFakeButtonEvent(dpy, btn, down, delay):
            rec.buttons.append((btn, down))

        @staticmethod
        def XTestFakeKeyEvent(dpy, kc, down, delay):
            rec.keys.append((kc, down))

    orig_x11, orig_xtst = hi.x11, hi.xtst
    hi.x11, hi.xtst = FakeX11, FakeXtst
    try:
        h = hi.Human(display=":0", preset=preset)
    finally:
        pass
    return h, rec, (orig_x11, orig_xtst)


FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def test_path_is_curved():
    h, rec, orig = make_human()
    try:
        rec.moves.clear()
        h.x, h.y = 100.0, 100.0
        h.move(900, 700)
        pts = rec.moves
        check("path emits > 25 points", len(pts) > 25, f"got {len(pts)}")

        # straight-line deviation: max perpendicular distance from the chord
        x0, y0 = 100.0, 100.0
        x1, y1 = 900.0, 700.0
        dx, dy = x1 - x0, y1 - y0
        norm = math.hypot(dx, dy)
        max_dev = 0.0
        for (px, py) in pts:
            dev = abs((px - x0) * dy - (py - y0) * dx) / norm
            max_dev = max(max_dev, dev)
        check("path deviates from straight line", max_dev > 5.0, f"max dev {max_dev:.1f}px")

        # Real pointers report at high rate and can repeat a pixel between
        # samples; a majority-distinct path is what matters. A perfectly
        # unique path would actually be less human, not more.
        same = sum(1 for a, b in zip(pts, pts[1:]) if a == b)
        frac = same / max(1, len(pts) - 1)
        check("path is mostly distinct points", frac < 0.15, f"{frac:.0%} duplicate")
    finally:
        hi.x11, hi.xtst = orig


def test_min_jerk_monotonic():
    prev = -1.0
    ok = True
    for i in range(101):
        v = hi._min_jerk(i / 100.0)
        if v < prev - 1e-9:
            ok = False
            break
        prev = v
    check("min-jerk profile monotonic 0->1", ok)
    check("min-jerk(0)=0", abs(hi._min_jerk(0.0)) < 1e-12)
    check("min-jerk(1)=1", abs(hi._min_jerk(1.0) - 1.0) < 1e-12)


def test_beziers_endpoints():
    p0, p1, p2, p3 = (0, 0), (10, 30), (70, -20), (100, 50)
    a = hi._bezier(p0, p1, p2, p3, 0.0)
    b = hi._bezier(p0, p1, p2, p3, 1.0)
    check("bezier t=0 hits p0", abs(a[0]) < 1e-9 and abs(a[1]) < 1e-9, str(a))
    check("bezier t=1 hits p3", abs(b[0] - 100) < 1e-9 and abs(b[1] - 50) < 1e-9, str(b))


def test_click_emits_press_release():
    h, rec, orig = make_human()
    try:
        rec.buttons.clear()
        h.x, h.y = 10.0, 10.0
        h.click(300, 300)
        downs = [b for b in rec.buttons if b[1] == 1]
        ups = [b for b in rec.buttons if b[1] == 0]
        check("click has 1 press", len(downs) == 1, str(downs))
        check("click has 1 release", len(ups) == 1, str(ups))
        rec.buttons.clear()
        h.click(310, 310, double=True)
        downs = [b for b in rec.buttons if b[1] == 1]
        check("double click has 2 presses", len(downs) == 2, str(len(downs)))
    finally:
        hi.x11, hi.xtst = orig


def test_type_emits_key_pairs():
    h, rec, orig = make_human(preset="aggressive")
    try:
        rec.keys.clear()
        h.type("abc")
        downs = [k for k in rec.keys if k[1] == 1]
        ups = [k for k in rec.keys if k[1] == 0]
        check("type abc -> 3 keydowns", len(downs) == 3, str(len(downs)))
        check("type abc -> 3 keyups", len(ups) == 3, str(len(ups)))
    finally:
        hi.x11, hi.xtst = orig


def test_presets_differ():
    a, _, o1 = make_human("careful")
    b, _, o2 = make_human("aggressive")
    try:
        check("careful slower than aggressive", a.typing_delay > b.typing_delay,
              f"{a.typing_delay} vs {b.typing_delay}")
        check("careful pauses more", a.pause_chance > b.pause_chance)
    finally:
        hi.x11, hi.xtst = o1
        hi.x11, hi.xtst = o2


def test_scroll_direction():
    h, rec, orig = make_human("aggressive")
    try:
        rec.buttons.clear()
        h.scroll(2)
        check("positive scroll = wheel up (button 4)",
              rec.buttons and all(b[0] == 4 for b in rec.buttons), str(rec.buttons))
        rec.buttons.clear()
        h.scroll(-2)
        check("negative scroll = wheel down (button 5)",
              rec.buttons and all(b[0] == 5 for b in rec.buttons), str(rec.buttons))
    finally:
        hi.x11, hi.xtst = orig


def main():
    print("human-input-x11 kinematics invariants")
    print()
    for fn in (
        test_min_jerk_monotonic,
        test_beziers_endpoints,
        test_path_is_curved,
        test_click_emits_press_release,
        test_type_emits_key_pairs,
        test_presets_differ,
        test_scroll_direction,
    ):
        print(fn.__name__)
        fn()
        print()

    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("all invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
