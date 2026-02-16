"""
main.py — FRANZ agent loop.

Runs forever: each turn loads prior VLM output (the "story"), executes
actions from it via execute.py, captures a screenshot, sends everything
to the VLM, and stores the raw response as the new story (SST rule).

Launched automatically by panel.py. Produces no console output.
"""

import importlib
import json
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import config as franz_config

API: Final = "http://localhost:1234/v1/chat/completions"
MODEL: Final = "qwen3-vl-2b-instruct-1m"
WIDTH: Final = 512
HEIGHT: Final = 288
VISUAL_MARKS: Final = True
LOOP_DELAY: Final = 0.01
EXECUTE_ACTIONS: Final = True
SANDBOX: Final = True
PHYSICAL_EXECUTION: Final = False
EXECUTE_SCRIPT: Final = Path(__file__).parent / "execute.py"
STATE_FILE: Final = Path(__file__).parent / "state.json"

SYSTEM_PROMPT: Final = """\
You run in a loop. Each step:
1. You read what you wrote last time. That is your only memory.
2. You see a screenshot of the screen.
3. You see Python execution output showing what worked or failed.

Then you write your next output. What you write now becomes your memory next step.

TOOLS — put all calls inside a single ```python block:
  left_click(x, y)         — click at position, draws a dot
  right_click(x, y)        — right-click, draws a small square
  double_left_click(x, y)  — double-click, draws a dot
  drag(x1, y1, x2, y2)    — draw a line from start to end
  type("text")             — type text where you last clicked
  screenshot()             — request a fresh screenshot

Coordinates: 0 to 1000. Top-left is (0,0). Bottom-right is (1000,1000).

READING THE SCREENSHOT:
The screenshot has a timestamp at the bottom showing the current time.
Red numbered marks show your actions from last turn:
- Red circle with number = left_click
- Red diamond with number = right_click
- Red arrow with number = drag (arrow points from start to end)
- Red underline with number = type
White shapes on the black canvas are your permanent drawings from all turns.
Use the red marks to verify your actions landed where you intended.

RULES:
1. All tool calls MUST be inside one ```python block. Calls outside it are ignored.
2. type() only works after left_click(). Click first to set position.
3. drag() draws white lines. Use many short drags for curves.
4. If you see a Python error in feedback, read it and fix your code.
5. If feedback says SyntaxError about no block found, add a ```python block.
6. Write notes outside the code block to remember what you learned.

IMPORTANT RULES YOU MUST FOLLOW:

Rule 1: type() only works after left_click(). You must click first to set where text goes. If you type() without clicking first, it will fail.

Rule 2: drag() draws lines. Use many short drags to make curves and shapes.

Rule 3: If feedback says "malformed", read the error message. It tells you what was wrong. Fix it next time.

Rule 4: If feedback says "no actions found", it means you did not write any tool calls. Write at least one tool call in your output.

HOW TO WRITE YOUR OUTPUT:

Write what you learned. Be specific. Use short clear sentences. Examples of good things to write:
- "I learned that drag draws a white line between two points."
- "To draw a circle I need many short drags in a ring shape."
- "My plan: first draw the head, then the ears, then the eyes."
- "The last drag went from (300,200) to (350,250). I can see it on screen."

Write what you plan to do next.
When you discover something new about how a tool works, write it down as a rule. These rules help you remember.
When you find a good method for drawing something, write it down as a step-by-step plan.
Do not repeat the same action if it did not help last time. If something is not working, try a different approach and write down why you changed.

YOUR TASK: Draw a picture of a cat on the black screen. The cat should look like a real cat — with a round head, two pointed ears, eyes, nose, mouth, whiskers, and a body. Use drag() for lines and shapes. Use left_click() for dots. Take your time, work step by step, and build the drawing piece by piece across many turns.\
""".strip()


@dataclass(slots=True)
class ToolConfig:
    left_click: bool = True
    right_click: bool = True
    double_left_click: bool = True
    drag: bool = True
    type: bool = True
    screenshot: bool = True
    click: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}


TOOLS: Final = ToolConfig()


@dataclass(slots=True)
class PipelineState:
    story: str = ""
    turn: int = 0


def _load_state() -> PipelineState:
    try:
        o = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(o, dict):
            return PipelineState(story=str(o.get("story", "")), turn=int(o.get("turn", 0)))
    except Exception:
        pass
    return PipelineState()


def _save_state(st: PipelineState, prev_story: str, raw: str, er: dict[str, object]) -> None:
    try:
        STATE_FILE.write_text(json.dumps({
            "turn": st.turn, "story": st.story, "prev_story": prev_story,
            "vlm_raw": raw,
            "executed": er.get("executed", []), "malformed": er.get("malformed", []),
            "ignored": er.get("ignored", []), "wants_screenshot": er.get("wants_screenshot", False),
            "execute_actions": EXECUTE_ACTIONS, "tools": TOOLS.to_dict(),
            "timestamp": datetime.now().isoformat(),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _sampling_dict() -> dict[str, float | int]:
    return {
        "temperature": float(franz_config.TEMPERATURE),
        "top_p": float(franz_config.TOP_P),
        "max_tokens": int(franz_config.MAX_TOKENS),
    }


def _infer(screenshot_b64: str, prev_story: str, feedback: str) -> str:
    payload: dict[str, object] = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [{"type": "text", "text": prev_story}]},
            {"role": "user", "content": [
                {"type": "text", "text": feedback},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]},
        ],
        **_sampling_dict(),
    }
    body_bytes = json.dumps(payload).encode()
    req = urllib.request.Request(API, body_bytes, {"Content-Type": "application/json"})
    delay = 0.5
    last_err: Exception | None = None
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=None) as resp:
                return json.load(resp)["choices"][0]["message"]["content"]
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2.0, 8.0)
    raise RuntimeError(f"VLM request failed after retries: {last_err}")


def _run_executor(raw: str, sandbox_reset: bool) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, str(EXECUTE_SCRIPT)],
        input=json.dumps({
            "raw": raw, "tools": TOOLS.to_dict(), "execute": EXECUTE_ACTIONS,
            "physical_execution": PHYSICAL_EXECUTION, "sandbox": SANDBOX,
            "sandbox_reset": sandbox_reset, "width": WIDTH, "height": HEIGHT,
            "marks": VISUAL_MARKS,
        }),
        capture_output=True, text=True,
    )
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def main() -> None:
    state = _load_state()
    first_turn = state.turn == 0
    while True:
        state.turn += 1
        try:
            importlib.reload(franz_config)
        except Exception:
            pass
        prev_story = state.story
        sandbox_reset = first_turn
        first_turn = False
        er = _run_executor(prev_story, sandbox_reset)
        screenshot_b64 = str(er.get("screenshot_b64", ""))
        feedback = (str(er["feedback"]) if "feedback" in er
                    else "RuntimeError: executor subprocess failed. Retrying next turn.")
        raw = _infer(screenshot_b64, prev_story, feedback)
        state.story = raw
        _save_state(state, prev_story, raw, er)
        time.sleep(LOOP_DELAY)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
