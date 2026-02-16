"""
================================================================================
FRANZ - Agentic Visual Loop for Windows 11
================================================================================

Technical Reference Document
Version: 2.0
Platform: Windows 11, Python 3.13+, stdlib only (zero pip dependencies)
License: Proprietary


TABLE OF CONTENTS
-----------------
1. System Overview
2. Architecture Diagram
3. Data Flow
4. File Inventory
5. Core Design Principles
6. Execution Environment Modes
7. Feedback Mechanism
8. Logging Architecture
9. Simulated Multi-Turn Execution Trace
10. Hardware and Software Requirements
11. Configuration Reference
12. Startup and Usage


================================================================================
1. SYSTEM OVERVIEW
================================================================================

FRANZ is a self-narrative, self-adaptive AI agent that operates in a
continuous loop. A Vision Language Model (VLM) observes a screenshot,
writes free-form text (its memory) and a Python code block (its actions),
and receives the result as Python-native feedback plus a new screenshot.

The VLM's own output from turn N is passed back verbatim as input to
turn N+1. This creates a self-reinforcing knowledge loop: observations
become rules, strategies become methods, and the growing narrative
functions as both working memory and accumulated expertise.

The system consists of five Python files and one HTML dashboard. There
are no external dependencies. All inter-process communication uses JSON
over stdin/stdout or HTTP over localhost.


================================================================================
2. ARCHITECTURE DIAGRAM
================================================================================

    +------------------------------------------------------------------+
    |                        panel.py (entry point)                    |
    |  +------------------+  +------------------+  +----------------+  |
    |  | Proxy :1234      |  | Dashboard :8080  |  | main.py        |  |
    |  | (reverse proxy)  |  | (SSE + HTML)     |  | (subprocess)   |  |
    |  +--------+---------+  +--------+---------+  +-------+--------+  |
    |           |                     |                     |          |
    +-----------|---------------------|---------------------|----------+
                |                     |                     |
                |   +-----------------+                     |
                |   | SSE broadcast                         |
                |   | + screenshot PNG                      |
                |   | + batched JSON                        |
                |   v                                       |
                | panel_log/                                |
                |   run_YYYYMMDD_HHMMSS/                    |
                |     turns_0001_0015.json                  |
                |     turn_0001.png                         |
                |     turn_0002.png                         |
                |     ...                                   |
                |                                           |
    +-----------v-----------+                   +-----------v-----------+
    |   VLM (LM Studio)    |                   |      main.py          |
    |   localhost:1235      |                   |                       |
    |                       |                   |  1. Load state.json   |
    |  Receives:            |<--HTTP POST-------+  2. Run execute.py   |
    |    [0] system prompt  |                   |  3. Get feedback+img  |
    |    [1] prior VLM text |---HTTP response-->+  4. Call VLM via :1234|
    |    [2] feedback+image |                   |  5. Store response    |
    |                       |                   |  6. Save state.json   |
    |  Returns:             |                   |  7. Sleep, repeat     |
    |    narrative + code   |                   |                       |
    +-----------------------+                   +-----------+-----------+
                                                            |
                                                +-----------v-----------+
                                                |     execute.py        |
                                                |     (subprocess)      |
                                                |                       |
                                                |  1. Extract ```python |
                                                |  2. exec() in sandbox |
                                                |  3. Record actions    |
                                                |  4. Call capture.py   |
                                                |  5. Reconcile applied |
                                                |  6. Build feedback    |
                                                |  7. Return JSON       |
                                                +-----------+-----------+
                                                            |
                                                +-----------v-----------+
                                                |     capture.py        |
                                                |     (subprocess)      |
                                                |                       |
                                                |  1. Load/create BMP   |
                                                |  2. Apply drawings    |
                                                |  3. Draw marks (copy) |
                                                |  4. Resize to output  |
                                                |  5. Encode PNG        |
                                                |  6. Return base64     |
                                                +-----------------------+


    Data Flow Per Turn:
    ===================

    main.py --stdin JSON--> execute.py --stdin JSON--> capture.py
            <-stdout JSON--            <-stdout JSON--

    main.py --HTTP POST---> panel.py --HTTP POST---> VLM :1235
            <--HTTP resp---          <--HTTP resp---


================================================================================
3. DATA FLOW
================================================================================

Each turn proceeds as follows:

    Step 1: main.py reads state.json to get the prior VLM output (story).

    Step 2: main.py spawns execute.py as a subprocess, passing:
            - The raw VLM text (for code block extraction)
            - Tool configuration, sandbox settings, image dimensions

    Step 3: execute.py extracts the first ```python fenced block from
            the VLM text using a regex. If no block exists, it sets
            no_block=True and skips execution.

    Step 4: execute.py calls exec() on the extracted code inside a
            restricted namespace. Only tool functions are available.
            Any exception produces a real Python traceback.

    Step 5: execute.py spawns capture.py, passing the list of recorded
            canonical action strings (e.g. "left_click(500, 300)").

    Step 6: capture.py loads the persistent sandbox BMP canvas (or
            captures the real desktop), applies white drawings for
            each action, draws ephemeral red marks on a copy, resizes
            to output dimensions, encodes as PNG, and returns base64.

    Step 7: execute.py reconciles which actions were actually applied
            by capture.py. Actions that had no effect (e.g. type()
            without prior click) are moved to malformed with a
            RuntimeError message.

    Step 8: execute.py builds a Python-native feedback string and
            returns everything to main.py as JSON.

    Step 9: main.py sends an HTTP POST to localhost:1234 (panel.py
            proxy) containing three messages:
              [0] system prompt (bootstrap)
              [1] the prior VLM output text, VERBATIM (SST)
              [2] Python feedback string + base64 PNG screenshot

    Step 10: panel.py intercepts the request, parses a copy for
             logging and SST verification, forwards the original
             bytes to the VLM at localhost:1235, receives the
             response, forwards original bytes back to main.py,
             saves the screenshot PNG, and batches the turn log.

    Step 11: main.py receives the VLM response, stores it as the
             new story in state.json, and loops.


================================================================================
4. FILE INVENTORY
================================================================================

    config.py      4 lines    Hot-reloadable sampling + execution config
    main.py       ~160 lines  Agent loop: state, executor, VLM inference
    execute.py    ~230 lines  Code extraction, sandboxed exec, feedback
    capture.py    ~520 lines  Sandbox canvas, marks, GDI capture, PNG
    panel.py      ~280 lines  Reverse proxy, SSE dashboard, logging
    panel.html     (varies)   Live dashboard UI (served by panel.py)


================================================================================
5. CORE DESIGN PRINCIPLES
================================================================================

5.1 SINGLE SOURCE OF TRUTH (SST)

    The VLM's raw text output from turn N is forwarded to turn N+1
    without ANY modification. No trimming, no cleaning, no truncation,
    no encoding change, no concatenation with other data. If the VLM
    produces malformed, empty, or nonsensical output, it is forwarded
    as-is. The pipeline never rewrites the model's output.

    SST is enforced structurally:
    - The story is always messages[1] (user message #1)
    - Executor feedback is always messages[2] (user message #2)
    - They are never merged, concatenated, or interleaved

    SST is verified externally by panel.py, which stores the VLM
    response text from turn N and compares it character-by-character
    to messages[1] in the request for turn N+1. Violations are logged
    with the exact divergence position and surrounding characters.

5.2 SELF-NARRATIVE MEMORY

    The VLM's output serves dual purpose:
    - The ```python block contains executable actions
    - Everything outside the block is narrative memory

    The narrative naturally accumulates transferable knowledge. If
    the story were given to a fresh process as context, that process
    would inherit the accumulated skill and understanding.

5.3 PYTHON-NATIVE FEEDBACK

    All feedback to the VLM mimics a Python REPL:
    - Success: ">>> OK: 3 actions executed."
    - Error: real Python traceback + "2 actions executed before error."
    - No block: "SyntaxError: no ```python block found..."
    - Failed action: "RuntimeError: type(...) had no visible effect..."

    No artificial error vocabulary. The VLM generates Python and
    receives Python output. This leverages the model's training data
    for natural error comprehension and correction.

5.4 STATELESS API DESIGN

    Each VLM call is a complete, self-contained API request with
    three messages. There is no conversation history, no session
    state, no token accumulation across turns. The system prompt
    provides capabilities, the story provides memory, and the
    feedback provides grounding. The VLM server can be restarted
    between any two turns without data loss.

5.5 SANDBOX TRANSPARENCY

    When SANDBOX=True (default), no physical input is sent to the OS.
    Actions are recorded and drawn as white shapes on a persistent
    BMP canvas. From the VLM's perspective, actions "happened" because
    the screenshot shows the result. The only difference between
    sandbox and real mode is whether Win32 SendInput is called.

5.6 PIPELINE TRANSPARENCY

    panel.py never modifies bytes flowing through it. It reads copies
    for inspection. The main.py to VLM channel is byte-identical with
    or without panel.py in the path. Removing panel.py and pointing
    main.py directly at the VLM produces identical behavior.


================================================================================
6. EXECUTION ENVIRONMENT MODES
================================================================================

Controlled by a single boolean in config.py:

    RESTRICTED_EXEC: bool = True

6.1 RESTRICTED MODE (RESTRICTED_EXEC = True, default)

    The VLM's Python code is exec()'d with __builtins__ set to an
    empty dict. Only tool functions exist in the namespace:

        left_click, right_click, double_left_click, drag,
        type, click, screenshot

    Any attempt to use standard Python features produces real Python
    errors:

        import os        -> ImportError: __import__ not found
        print("hello")   -> NameError: name 'print' is not defined
        range(10)        -> NameError: name 'range' is not defined
        open("file")     -> NameError: name 'open' is not defined
        eval("1+1")      -> NameError: name 'eval' is not defined

    These are genuine CPython error messages, not custom strings.
    The VLM reads them and naturally understands the constraint.

6.2 UNRESTRICTED MODE (RESTRICTED_EXEC = False)

    The VLM's Python code is exec()'d with default builtins intact.
    All standard Python features work alongside tool functions:

        import math
        for i in range(12):
            angle = math.radians(i * 30)
            drag(500 + int(100*math.cos(angle)), 400 + int(100*math.sin(angle)),
                 500 + int(120*math.cos(angle)), 400 + int(120*math.sin(angle)))

    The VLM can use loops, conditionals, math, string operations,
    list comprehensions, and any installed library.

6.3 IMPLEMENTATION

    The switch is exactly one line of code in execute.py:

        if restricted:
            ns["__builtins__"] = {}

    When absent, Python's default __builtins__ module remains in
    the exec() namespace. No custom error handlers, no manual
    validation, no whitelist checking. Python itself enforces the
    boundary.


================================================================================
7. FEEDBACK MECHANISM
================================================================================

Feedback is constructed by execute.py and delivered as plain text in
messages[2] alongside the screenshot. Three categories:

7.1 SUCCESS

    When all actions execute without error:

        >>> OK: 5 actions executed.

7.2 EXECUTION ERROR

    When exec() raises an exception, the real traceback is captured:

        Traceback (most recent call last):
          File "<string>", line 3, in <module>
        TypeError: left_click(x, y) requires numbers, got (str, str)

        2 actions executed before error.

    Actions recorded before the error are preserved and applied.

7.3 NO CODE BLOCK

    When no ```python fenced block is found:

        SyntaxError: no ```python block found in output. Wrap your
        tool calls in ```python ... ```

7.4 SANDBOX RECONCILIATION

    When an action executes in the namespace but has no visible effect
    on the canvas (e.g. type() without prior click position):

        RuntimeError: type("hello") had no visible effect (type()
        requires a prior left_click() to set cursor position)

    This is appended to malformed and reflected in the feedback.
    The action is removed from the executed list so counts are
    accurate.


================================================================================
8. LOGGING ARCHITECTURE
================================================================================

All logging is centralized in panel.py. No other file produces log
artifacts. main.py and execute.py produce no console output (launched
with stdout/stderr redirected to DEVNULL).

8.1 LOG DIRECTORY STRUCTURE

    panel_log/
      run_20250612_143022/           <-- timestamped per session
        turns_0001_0015.json         <-- 15 turns per JSON file
        turns_0016_0030.json
        turns_0031_0037.json         <-- partial batch (flushed on shutdown)
        turn_0001.png                <-- screenshot from wire traffic
        turn_0002.png
        turn_0003.png
        ...

8.2 WHAT IS LOGGED PER TURN

    Each JSON entry in a batch file contains:

        turn                  int       Turn sequence number
        timestamp             str       ISO 8601 datetime
        latency_ms            float     VLM round-trip time in milliseconds

        request.model         str       Model name from payload
        request.sst_text      str       Full SST message text
        request.sst_text_length int     Character count
        request.feedback_text str       Python-native feedback string
        request.has_image     bool      Whether screenshot was included
        request.sampling      dict      temperature, top_p, max_tokens
        request.messages_count int      Number of messages in payload
        request.body_size_bytes int     Raw request size
        request.parse_error   str|null  Parsing failure description

        response.status       int       HTTP status code
        response.vlm_text     str       Full VLM response text
        response.vlm_text_length int    Character count
        response.finish_reason str      stop, length, etc.
        response.usage        dict      prompt_tokens, completion_tokens
        response.body_size_bytes int    Raw response size
        response.parse_error  str|null  Parsing failure description
        response.error        str       Upstream error detail

        sst_check.verified    bool      Whether verification was performed
        sst_check.match       bool      Whether SST matched
        sst_check.detail      str       Human-readable verification result

    The image_data_uri field is stripped from JSON logs to avoid
    multi-megabyte files. Screenshots are saved separately as PNGs.

8.3 SCREENSHOT PROVENANCE

    Screenshots saved by panel.py are decoded from the actual base64
    data URI flowing through the proxy wire. These are the exact
    images the VLM received, not reconstructed copies. This provides
    ground-truth SST validation artifacts.

8.4 BATCH FLUSHING

    Turns accumulate in memory (TURNS_PER_LOG_FILE = 15 by default).
    When the batch is full, it is written atomically. On shutdown
    (Ctrl-C), remaining turns are flushed to a partial batch file.


================================================================================
9. SIMULATED MULTI-TURN EXECUTION TRACE
================================================================================

The following traces the exact data flow for several turns, including
both well-formed and malformed VLM outputs.

--- TURN 1 (fresh start, state.story = "") ---

    main.py: prev_story = ""
    main.py: sandbox_reset = True (first turn)
    main.py: calls execute.py with raw=""

    execute.py: _extract_block("") returns None
    execute.py: no_block = True
    execute.py: calls capture.py with actions=["timestamp()"]
    capture.py: sandbox_reset=True, creates black BMP canvas
    capture.py: draws timestamp watermark on copy
    capture.py: returns base64 PNG of black screen with timestamp

    execute.py: feedback = "SyntaxError: no ```python block found..."
    execute.py: returns JSON to main.py

    main.py: sends to VLM:
      [0] system prompt
      [1] "" (empty SST -- no prior output)
      [2] "SyntaxError: no ```python block..." + screenshot

    panel.py: intercepts, verifies SST (empty, first turn = OK)
    panel.py: saves turn_0001.png, batches log entry
    panel.py: forwards to VLM :1235, forwards response back

    VLM responds with:
      "I see a black screen. I will start drawing a cat head.
       ```python
       drag(400, 300, 450, 250)
       drag(450, 250, 500, 300)
       ```"

    main.py: state.story = <VLM response above>
    main.py: saves state.json

--- TURN 2 (normal execution) ---

    main.py: prev_story = "I see a black screen..."
    main.py: sandbox_reset = False
    main.py: calls execute.py with raw=prev_story

    execute.py: _extract_block finds the ```python block
    execute.py: code = "drag(400, 300, 450, 250)\ndrag(450, 250, 500, 300)\n"
    execute.py: exec() runs both calls successfully
    execute.py: executed = ["drag(400, 300, 450, 250)", "drag(450, 250, 500, 300)"]
    execute.py: calls capture.py with those actions + "timestamp()"

    capture.py: loads existing BMP canvas
    capture.py: draws two white lines on canvas (persistent)
    capture.py: saves updated BMP
    capture.py: draws red arrow marks on copy (ephemeral)
    capture.py: draws timestamp watermark on copy
    capture.py: returns base64 PNG

    execute.py: feedback = ">>> OK: 2 actions executed."
    execute.py: returns JSON to main.py

    main.py: sends to VLM:
      [1] "I see a black screen..." (exact prior output, SST)
      [2] ">>> OK: 2 actions executed." + screenshot showing lines

    panel.py: verifies SST matches stored turn-1 response (match)

--- TURN 3 (VLM makes a Python error) ---

    VLM responded in turn 2 with:
      "Good, I can see two white lines. Now I will draw more.
       ```python
       drag(400, 300, 450, 250)
       left_click("center", "top")
       drag(500, 300, 550, 250)
       ```"

    execute.py: exec() runs drag(400,300,450,250) successfully
    execute.py: exec() runs left_click("center", "top")
    execute.py: _check_xy raises TypeError
    execute.py: traceback captured, exec stops

    executed = ["drag(400, 300, 450, 250)"]
    malformed = ["Traceback (most recent call last):\n  File \"<string>\", line 2...\n
                  TypeError: left_click(x, y) requires numbers, got (str, str)"]

    feedback = "Traceback (most recent call last):\n  File \"<string>\", line 2...\n
                TypeError: left_click(x, y) requires numbers, got (str, str)\n
                1 action executed before error."

    The third drag() was never reached. One line was drawn on the
    canvas. The VLM receives the real traceback and can fix the error.

--- TURN 4 (VLM tries import in restricted mode) ---

    VLM responded with:
      "I need to calculate circle points.
       ```python
       import math
       for i in range(12):
           drag(500, 400, 500 + int(50*math.cos(i)), 400 + int(50*math.sin(i)))
       ```"

    execute.py: RESTRICTED_EXEC = True
    execute.py: exec() hits "import math"
    execute.py: Python raises: ImportError: __import__ not found

    executed = []
    malformed = ["Traceback (most recent call last):\n  File \"<string>\", line 1...\n
                  ImportError: __import__ not found"]

    feedback = "Traceback (most recent call last):\n  File \"<string>\", line 1...\n
                ImportError: __import__ not found\n0 actions executed."

    No canvas changes. Screenshot shows previous state unchanged.
    The VLM learns it cannot use imports and will use flat calls.

--- TURN 4 ALTERNATIVE (same code, unrestricted mode) ---

    execute.py: RESTRICTED_EXEC = False
    execute.py: exec() runs import math successfully
    execute.py: exec() runs the for loop with range()
    execute.py: 12 drag() calls recorded and drawn

    executed = ["drag(500, 400, 550, 400)", "drag(500, 400, 545, 442)", ...]
    feedback = ">>> OK: 12 actions executed."

    The VLM successfully used math to compute circle coordinates.

--- TURN 5 (VLM output has no code block) ---

    VLM responded with:
      "I need to think about this differently. The head should be
       round. I will use many short drags arranged in a circle.
       My plan: 16 drags, each spanning about 22 degrees of arc."

    execute.py: _extract_block returns None (no ```python block)
    execute.py: no_block = True

    feedback = "SyntaxError: no ```python block found in output.
                Wrap your tool calls in ```python ... ```"

    No canvas changes. The VLM's narrative is still stored as the
    story (SST) and will be visible to it next turn. The plan text
    persists in memory even though no actions were taken.

--- TURN 6 (type() without prior click) ---

    VLM responded with:
      "I will label the drawing.
       ```python
       type("CAT")
       ```"

    execute.py: exec() runs type_("CAT") successfully
    execute.py: executed = ['type("CAT")']
    execute.py: calls capture.py

    capture.py: _sandbox_apply processes type("CAT")
    capture.py: st["last_x"] is None (no prior click in this session)
    capture.py: skips the type action, does NOT add to applied list
    capture.py: applied = [] (only timestamp, which is mark-only)

    execute.py: reconciliation detects type("CAT") not in applied_set
    execute.py: moves it to malformed:
      'RuntimeError: type("CAT") had no visible effect
       (type() requires a prior left_click() to set cursor position)'
    execute.py: executed becomes []

    feedback = 'RuntimeError: type("CAT") had no visible effect
                (type() requires a prior left_click() to set cursor position)\n
                0 actions executed.'

    The VLM learns it must click before typing.


================================================================================
10. HARDWARE AND SOFTWARE REQUIREMENTS
================================================================================

    Operating System:    Windows 11 (required for Win32 GDI and SendInput)
    Python:              3.13 or later
    Dependencies:        None (stdlib only, zero pip packages)
    VLM Backend:         LM Studio, vLLM, or any OpenAI-compatible API
                         serving a vision-language model at localhost:1235
    Network:             Localhost only (127.0.0.1 ports 1234, 1235, 8080)
    Display:             Required (GDI screen metrics used for coordinate
                         mapping even in sandbox mode)
    Disk:                sandbox_canvas.bmp (~24MB at 1920x1080x24bpp)
                         panel_log/ (~50KB per 15 turns + ~100KB per PNG)
    Memory:              ~50MB baseline + VLM server memory


================================================================================
11. CONFIGURATION REFERENCE
================================================================================

config.py (hot-reloaded every turn by main.py):

    TEMPERATURE      float   0.7     VLM sampling temperature
    TOP_P            float   0.9     VLM nucleus sampling threshold
    MAX_TOKENS       int     2048    Maximum response tokens
    RESTRICTED_EXEC  bool    True    Sandbox execution environment

main.py constants:

    API              str     VLM endpoint URL (via panel proxy)
    MODEL            str     Model identifier for API payload
    WIDTH            int     Output screenshot width in pixels
    HEIGHT           int     Output screenshot height in pixels
    VISUAL_MARKS     bool    Draw red numbered marks on screenshots
    LOOP_DELAY       float   Seconds between turns (0.01)
    EXECUTE_ACTIONS  bool    Master gate for action execution
    SANDBOX          bool    Use persistent canvas instead of real desktop
    PHYSICAL_EXECUTION bool  Send real Win32 input events

panel.py constants:

    PROXY_PORT              int     1234    Proxy listen port
    UPSTREAM_URL            str             VLM server URL at :1235
    DASHBOARD_PORT          int     8080    Dashboard listen port
    TURNS_PER_LOG_FILE      int     15      Turns per batch JSON file
    MAIN_STARTUP_DELAY      float   10.0    Seconds before launching main.py
    MAIN_RESTART_DELAY      float   3.0     Seconds before restarting on crash
    MAX_SSE_CLIENTS         int     20      Maximum concurrent dashboard clients
    SSE_KEEPALIVE_SEC       float   15.0    SSE keepalive interval

capture.py constants:

    MARK_SCALE              float   1.8     Visual mark size multiplier


================================================================================
12. STARTUP AND USAGE
================================================================================

12.1 PREREQUISITES

    1. Install Python 3.13+ on Windows 11
    2. Start a VLM server (e.g. LM Studio) on localhost:1235
       with a vision-language model loaded
    3. Place all files in the same directory

12.2 LAUNCH

    python panel.py

    This single command:
    - Cleans __pycache__
    - Creates panel_log/run_YYYYMMDD_HHMMSS/
    - Starts the reverse proxy on :1234
    - Starts the dashboard on :8080
    - Waits 10 seconds for the VLM server to be ready
    - Launches main.py as a managed subprocess
    - Monitors main.py and restarts on crash

12.3 MONITORING

    Open http://127.0.0.1:8080/ in a browser for the live dashboard.
    The panel.py console shows one-line summaries per turn:

        [panel] turn=1 latency=2341ms status=200 sst=OK vlm_len=847 finish=stop
        [panel] turn=2 latency=1892ms status=200 sst=OK vlm_len=923 finish=stop

12.4 LIVE TUNING

    Edit config.py while the system is running. Changes take effect
    on the next turn (main.py reloads config.py every iteration).

12.5 SHUTDOWN

    Press Ctrl-C in the panel.py console. This:
    - Sets the shutdown event
    - Terminates main.py (graceful, then forced after 5s)
    - Flushes remaining log batch to disk
    - Shuts down proxy and dashboard servers

12.6 RESUMING

    The system resumes from state.json automatically. If state.json
    exists with turn > 0, the agent continues from where it left off
    with the canvas intact. Delete state.json and sandbox_canvas.bmp
    to start fresh.


================================================================================
END OF DOCUMENT
================================================================================
"""