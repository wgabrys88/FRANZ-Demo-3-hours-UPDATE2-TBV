"""
execute.py — FRANZ action executor.

Extracts the ```python block from VLM output, exec()s it in a sandboxed
namespace (no builtins), records canonical action strings, optionally
sends Win32 input, delegates to capture.py for screenshot, and returns
structured JSON with Python-native feedback to main.py via stdout.
"""

import ctypes
import ctypes.wintypes
import json
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Final

import config as franz_config

_MOVE_STEPS: Final = 20
_STEP_DELAY: Final = 0.01
_CLICK_DELAY: Final = 0.12
CAPTURE_SCRIPT: Final = Path(__file__).parent / "capture.py"

INPUT_MOUSE: Final = 0
INPUT_KEYBOARD: Final = 1
MOUSEEVENTF_LEFTDOWN: Final = 0x0002
MOUSEEVENTF_LEFTUP: Final = 0x0004
MOUSEEVENTF_RIGHTDOWN: Final = 0x0008
MOUSEEVENTF_RIGHTUP: Final = 0x0010
MOUSEEVENTF_MOVE: Final = 0x0001
MOUSEEVENTF_ABSOLUTE: Final = 0x8000
KEYEVENTF_KEYUP: Final = 0x0002
KEYEVENTF_UNICODE: Final = 0x0004

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


_user32: ctypes.WinDLL | None = None
_screen_w: int = 0
_screen_h: int = 0


def _init_win32() -> None:
    global _user32, _screen_w, _screen_h
    if _user32 is not None:
        return
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _screen_w = _user32.GetSystemMetrics(0)
    _screen_h = _user32.GetSystemMetrics(1)
    _user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = ctypes.c_uint


def _send_inputs(items: list[INPUT]) -> None:
    if not items:
        return
    assert _user32 is not None
    n = len(items)
    arr = (INPUT * n)(*items)
    if _user32.SendInput(n, arr, ctypes.sizeof(INPUT)) != n:
        raise OSError(ctypes.get_last_error())


def _send_mouse(flags: int, abs_x: int | None = None, abs_y: int | None = None) -> None:
    i = INPUT()
    i.type = INPUT_MOUSE
    f = flags
    dx = dy = 0
    if abs_x is not None and abs_y is not None:
        dx, dy, f = abs_x, abs_y, f | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE
    i.u.mi = MOUSEINPUT(dx, dy, 0, f, 0, 0)
    _send_inputs([i])


def _send_unicode(text: str) -> None:
    items: list[INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        code = 0x000D if ch == "\n" else ord(ch)
        for fl in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.u.ki = KEYBDINPUT(0, code, fl, 0, 0)
            items.append(inp)
    _send_inputs(items)


def _to_px(v: int, dim: int) -> int:
    return int((max(0, min(1000, v)) / 1000) * dim)


def _to_abs(x_px: int, y_px: int) -> tuple[int, int]:
    return (
        max(0, min(65535, int((x_px / max(1, _screen_w - 1)) * 65535))),
        max(0, min(65535, int((y_px / max(1, _screen_h - 1)) * 65535))),
    )


def _cursor_pos() -> tuple[int, int]:
    assert _user32 is not None
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _smooth_move(tx: int, ty: int) -> None:
    sx, sy = _cursor_pos()
    dx, dy = tx - sx, ty - sy
    for i in range(_MOVE_STEPS + 1):
        t = i / _MOVE_STEPS
        t = t * t * (3.0 - 2.0 * t)
        _send_mouse(0, *_to_abs(int(sx + dx * t), int(sy + dy * t)))
        time.sleep(_STEP_DELAY)


def _mouse_click(down: int, up: int) -> None:
    _send_mouse(down)
    time.sleep(0.02)
    _send_mouse(up)


def _do_left_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)


def _do_right_click(x: int, y: int) -> None:
    _smooth_move(_to_px(x, _screen_w), _to_px(y, _screen_h))
    time.sleep(_CLICK_DELAY)
    _mouse_click(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)


def _do_double_left_click(x: int, y: int) -> None:
    _do_left_click(x, y)
    time.sleep(0.06)
    _do_left_click(x, y)


def _do_drag(x1: int, y1: int, x2: int, y2: int) -> None:
    _smooth_move(_to_px(x1, _screen_w), _to_px(y1, _screen_h))
    time.sleep(0.08)
    _send_mouse(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.06)
    _smooth_move(_to_px(x2, _screen_w), _to_px(y2, _screen_h))
    time.sleep(0.06)
    _send_mouse(MOUSEEVENTF_LEFTUP)


_FENCE_RE: Final = re.compile(r"```python[ \t]*\n(.*?)```", re.DOTALL)


def _extract_block(raw: str) -> str | None:
    m = _FENCE_RE.search(raw)
    return m.group(1) if m else None


def _make_namespace(
    tools: dict[str, bool], master: bool, physical: bool, restricted: bool,
) -> tuple[dict[str, object], list[str], list[str], list[bool]]:
    executed: list[str] = []
    ignored: list[str] = []
    wants_ss: list[bool] = [False]

    def gate(name: str) -> bool:
        return master and tools.get(name, True)

    def _check_xy(name: str, x: object, y: object) -> tuple[int, int]:
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise TypeError(f"{name}(x, y) requires numbers, got ({type(x).__name__}, {type(y).__name__})")
        return int(x), int(y)

    def left_click(x: int, y: int) -> None:
        ix, iy = _check_xy("left_click", x, y)
        canon = f"left_click({ix}, {iy})"
        if not gate("left_click"):
            ignored.append(canon); return
        if physical:
            _do_left_click(ix, iy)
        executed.append(canon)

    def right_click(x: int, y: int) -> None:
        ix, iy = _check_xy("right_click", x, y)
        canon = f"right_click({ix}, {iy})"
        if not gate("right_click"):
            ignored.append(canon); return
        if physical:
            _do_right_click(ix, iy)
        executed.append(canon)

    def double_left_click(x: int, y: int) -> None:
        ix, iy = _check_xy("double_left_click", x, y)
        canon = f"double_left_click({ix}, {iy})"
        if not gate("double_left_click"):
            ignored.append(canon); return
        if physical:
            _do_double_left_click(ix, iy)
        executed.append(canon)

    def drag(x1: int, y1: int, x2: int, y2: int) -> None:
        for name, v in [("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)]:
            if not isinstance(v, (int, float)):
                raise TypeError(f"drag() argument '{name}' must be a number, got {type(v).__name__}")
        vals = int(x1), int(y1), int(x2), int(y2)
        canon = "drag({}, {}, {}, {})".format(*vals)
        if not gate("drag"):
            ignored.append(canon); return
        if physical:
            _do_drag(*vals)
        executed.append(canon)

    def type_(text: str) -> None:
        if not isinstance(text, str):
            raise TypeError(f"type() requires a string argument, got {type(text).__name__}")
        canon = f"type({json.dumps(text)})"
        if not gate("type"):
            ignored.append(canon); return
        if physical:
            _send_unicode(text)
        executed.append(canon)

    def click(x: int, y: int) -> None:
        left_click(x, y)

    def screenshot() -> None:
        wants_ss[0] = True
        ignored.append("screenshot()")

    ns: dict[str, object] = {
        "left_click": left_click, "right_click": right_click,
        "double_left_click": double_left_click, "drag": drag,
        "type": type_, "click": click, "screenshot": screenshot,
    }
    if restricted:
        ns["__builtins__"] = {}
    return ns, executed, ignored, wants_ss


def _build_feedback(executed: list[str], malformed: list[str], no_block: bool) -> str:
    if no_block:
        return "SyntaxError: no ```python block found in output. Wrap your tool calls in ```python ... ```"
    if malformed:
        n = len(executed)
        suffix = f"\n{n} action{'s' if n != 1 else ''} executed before error." if n else "\n0 actions executed."
        return "\n".join(malformed) + suffix
    n = len(executed)
    return f">>> OK: {n} action{'s' if n != 1 else ''} executed." if n else ">>> OK: 0 actions executed (empty ```python block)."


def _run_capture(
    actions: list[str], width: int, height: int,
    marks: bool, sandbox: bool, sandbox_reset: bool,
) -> tuple[str, list[str]]:
    r = subprocess.run(
        [sys.executable, str(CAPTURE_SCRIPT)],
        input=json.dumps({
            "actions": actions, "width": width, "height": height,
            "marks": marks, "sandbox": sandbox, "sandbox_reset": sandbox_reset,
        }),
        capture_output=True, text=True,
    )
    if not r.stdout:
        return "", actions
    try:
        obj = json.loads(r.stdout)
        applied = obj.get("applied", actions)
        if not isinstance(applied, list):
            applied = actions
        return str(obj.get("screenshot_b64", "")), [str(a) for a in applied]
    except json.JSONDecodeError:
        return "", actions


def main() -> None:
    req = json.loads(sys.stdin.read() or "{}")
    raw = str(req.get("raw", ""))
    tools = req.get("tools", {}) if isinstance(req.get("tools"), dict) else {}
    master = bool(req.get("execute", True))
    width, height = int(req.get("width", 0)), int(req.get("height", 0))
    marks = bool(req.get("marks", True))
    sandbox = bool(req.get("sandbox", False))
    sandbox_reset = bool(req.get("sandbox_reset", False))
    physical = bool(req.get("physical_execution", False)) and not sandbox
    restricted = bool(getattr(franz_config, "RESTRICTED_EXEC", True))
    if physical:
        _init_win32()

    malformed: list[str] = []
    code = _extract_block(raw)
    no_block = code is None
    ns, executed, ignored, wants_ss = _make_namespace(tools, master, physical, restricted)
    if code is not None:
        try:
            exec(code, ns)
        except Exception:
            malformed.append(traceback.format_exc().strip())

    mark_only = ["timestamp()"]
    if wants_ss[0]:
        mark_only.append("screenshot()")
    screenshot_b64, applied = _run_capture(
        executed + mark_only, width, height, marks, sandbox, sandbox_reset,
    )

    if sandbox:
        applied_set = set(applied)
        not_applied = [a for a in executed if a not in applied_set]
        if not_applied:
            for a in not_applied:
                malformed.append(
                    f"RuntimeError: {a} had no visible effect "
                    "(type() requires a prior left_click() to set cursor position)"
                )
            executed = [a for a in executed if a in applied_set]

    sys.stdout.write(json.dumps({
        "executed": executed, "malformed": malformed, "ignored": ignored,
        "wants_screenshot": wants_ss[0], "screenshot_b64": screenshot_b64,
        "feedback": _build_feedback(executed, malformed, no_block),
    }))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
