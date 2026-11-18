# human-input-x11

**Human-like mouse and keyboard injection at the OS level for X11.**
No CDP. No WebDriver. No Playwright. The browser receives real X server input
events: `event.isTrusted === true`, `navigator.webdriver` stays `false`, and
nothing in the event stream is attributable to automation.

Built for anti-bot work where DataDome, PerimeterX, Cloudflare Bot Management,
Akamai and Kasada score **keystroke timing distributions and pointer
kinematics** — not just trust flags.

```
n=49  mean=182.0ms  sd=225.6  min=48  max=923  trusted=true
```
Real output from the instrumented test harness. sd≈226ms with a soft 48ms floor
and 923ms ceiling is the signature of human typing. Automation produces sd≈0.

---

## Why not JS `.click()` / `Input.dispatchKeyEvent`

CDP-injected events can be made `isTrusted: true`, and that fools naive
detectors. It does not fool behavioural scoring:

| Signal | JS / CDP injection | XTest injection |
|---|---|---|
| `isTrusted` | can be `true` | `true` |
| `navigator.webdriver` | patched out | never set |
| Mouse path | straight line or user-crafted | Bezier + tremor + overshoot |
| Inter-key timing | uniform | 70±40ms with pause bursts |
| Velocity profile | constant | minimum-jerk accel/decel |
| CDP session present | **yes** (detectable) | **no** |

The last row matters most: no debugger is attached, so there is no session to
probe, no `Runtime.enable` artefacts, no target attach.

---

## Install

Requires `libX11` and `libXtst` (present on virtually every Linux desktop):

```bash
ldconfig -p | grep -E 'libX11\.so|libXtst\.so'
# missing libXtst6:
#   Debian/Ubuntu  apt install libxtst6
#   Fedora         dnf install libXtst
```

```bash
git clone https://github.com/<you>/human-input-x11.git
cd human-input-x11
install -Dm755 scripts/human_input.py ~/.local/bin/human_input.py
```

Or use it in place — it has **zero pip dependencies** (stdlib + `ctypes` only).

---

## Usage

### CLI

```bash
export DISPLAY=:1        # your X display (:0 desktop, :1 VNC, etc.)

python3 scripts/human_input.py probe          # display sanity check
python3 scripts/human_input.py move 800 500
python3 scripts/human_input.py click 800 500
python3 scripts/human_input.py dclick 800 500
python3 scripts/human_input.py type "hello world"
python3 scripts/human_input.py scroll 5       # + up,  - down
python3 scripts/human_input.py key Return
python3 scripts/human_input.py idle 2
```

### Library

```python
import sys
sys.path.insert(0, "scripts")
from human_input import Human

h = Human(preset="default")      # default | careful | aggressive
h.click(400, 250)                # moves first, aims, then holds
h.type("text to enter", wpm=60)  # wpm overrides per-char delay
h.key("Return")
h.key("t", combo=["ctrl"])       # ctrl+t
h.scroll(-5)                     # wheel down
h.paste("\u2713 unicode")         # xclip + ctrl+v for non-Latin text
h.close()
```

### Presets

| Preset | Use for | Speed |
|---|---|---|
| `default` | most sites | ~70 WPM |
| `careful` | Cloudflare / DataDome / PerimeterX gated forms | ~45 WPM, 15% pause bursts |
| `aggressive` | low-risk bulk forms | ~110 WPM, low jitter |

---

## How the kinematics work

Five layers, all applied per move:

1. **Cubic Bezier path** — randomized asymmetric control points with
   perpendicular bias up to ±0.30·distance. Never a straight line.
2. **Fitts's Law duration** — `t = a + b·log2(D/W + 1)`, a=50ms, b=120ms,
   jittered ±15%. Far targets take longer, big targets shorter.
3. **Minimum-jerk velocity** — `s(t) = 10t³ − 15t⁴ + 6t⁵`. Real acceleration
   and deceleration with zero jerk at both endpoints.
4. **Physiological tremor** — 0.3px base amplitude, scaled with instantaneous
   speed, so fast segments wobble more.
5. **Overshoot + correction** — 15% chance on moves over 400px: overshoots by
   15–45px along the travel vector, then settles onto the target.

Typing:

- key hold 15–35ms per key
- inter-key delay 70±40ms
- 10% chance of a 400–1000ms pause burst (thinking/hesitation)
- explicit Shift down/up latency 30–70ms / 20–50ms for capitals and symbols

---

## Requirements

| Need | Why | Check |
|---|---|---|
| X11 display | XTest only works against an X server | `echo $DISPLAY`, `xdpyinfo` |
| `libX11`, `libXtst` | event injection | `ldconfig -p \| grep Xtst` |
| A focused window | keys go to whatever has focus | `xdotool getwindowfocus getwindowname` |
| `xclip` (optional) | only for `paste()` | `which xclip` |

---

## Platform notes

- **Wayland** — XTest goes through XWayland, so X11 clients work but native
  Wayland clients may ignore injected events. Force `GDK_BACKEND=x11` /
  `QT_QPA_PLATFORM=xcb`, or run a real X11 session / Xvfb.
- **Headless servers** — you need Xvfb or TigerVNC. There is nothing to inject
  into otherwise.
- **Containers** — mount `/tmp/.X11-unix`, set `DISPLAY`, sometimes `--ipc=host`.
- **macOS / Windows** — not supported. This engine is X11-only; use
  [`pyautogui`](https://pyautogui.readthedocs.io/) or
  [cua](https://github.com/trycua/cua) instead.

---

## Pitfalls

- **`xdotool windowactivate` fails** on bare X servers with no window manager
  (no `_NET_ACTIVE_WINDOW`). Use `xdotool windowraise` + `xdotool windowfocus <id>`,
  then confirm with `xdotool getwindowfocus getwindowname`.
- **Focus is per window, not per tab.** Click inside the target page before
  typing or keys land in the omnibox.
- **Coordinates are the framebuffer**, not the DOM. Screenshot first, then take
  coordinates from the image.
- **The engine caches cursor position.** After an external `xdotool` move the
  next `move()` starts from a stale origin — pass the true current point to
  `click()` or accept one long path.
- **Non-Latin text.** `type()` maps characters through the current X keymap and
  fails on absent glyphs. Use `paste()` for Cyrillic/CJK/emoji.
- **One `Human` per display.** Two concurrent instances interleave motion events
  and produce physically impossible kinematics that detectors flag.

---

## Testing

```bash
python3 tests/kinematics_test.py     # asserts path/jitter/timing invariants
python3 tests/smoke.py               # needs DISPLAY + a focused window
```

`kinematics_test.py` uses a monkeypatched event sink, so it runs headless in CI.

---

## Changelog

**1.0.0** — initial release: Bezier + Fitts + minimum-jerk + tremor + overshoot
mouse engine, human timed typing with pause bursts, three presets, paste via
xclip, CLI + library API.

## License

MIT — see [LICENSE](LICENSE).
