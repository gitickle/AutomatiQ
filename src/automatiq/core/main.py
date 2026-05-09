"""Agent loop — the core interactive session where the LLM investigates a
recorded browser session and produces a standalone automation/extraction script."""  # noqa: E501

import json
import logging
import os
import queue
import sys
import threading
import time

import litellm
import yaml
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from . import config, events
from .cancel_standard import CancelRequestedException, CancelToken
from .ipython_sandbox import AgentSandbox

logger = logging.getLogger(__name__)
litellm.suppress_debug_info = True

_preloaded_sandbox = None

# -----------------
# CONSTANTS / PROMPTS
# -----------------

SYSTEM_PROMPT = """\
You are a Web Automation Investigator. You reverse-engineer recorded browser sessions into standalone Python automation/extraction scripts. You work inside a persistent IPython environment.

<environment>
## Tools
You have three actions each turn:
1. **`execute_ipython`** — Run Python code or shell commands (`!command`) in a persistent IPython session. State persists across cells.
2. **`final_submit`** — Submit the final Python script once you have verified it in building mode. To communicate with the user, simply speak using standard conversational text in your response.
3. **`switch_mode`** — Switch your working mode. You have three modes: `reading`, `testing`, `building`. When you switch, write a research memo summarizing what you've learned so far — it will be your own context when you resume in the new mode.

## Shell Commands
Via `!command`: `rg` (ripgrep), `jq`, `grep`, `ls`, `cat`, `head`, `tail`, `sort`, `uniq`, `wc`, `awk`, `tr`, `base64`, `tee`, `cp`, `mv`, `mkdir`.
Use shell one-liners for fast searching across the dump. Use Python for parsing, HTTP requests, and the final script.

## Output Constraints
Output is capped at ~20KB. If truncated, use `%view_output Cell_N --offset M` to paginate. **Never draw conclusions from truncated output.** Always read the rest first.

## Session Dump Structure
```
session_dump/
├── SUMMARY.json              # session metadata, session_flow (AI chronological summary), statistics
├── session_metadata.json     # raw session metadata (start URL, timestamps, browser info)
├── timeline.json             # time-sorted interleaved user actions + network requests
├── clips/                    # video segments of user actions
│   └── action_clip_000.mp4   # one clip per action cluster (padded around the action)
└── requests/                 # one folder per HTTP transaction
    └── 000_GET_example.com/
        ├── transaction.json  # full request/response metadata, cookies, headers, content detection
        ├── req_payload.*     # request body (extension from Magika AI detection: .json, .txt, .js, etc.)
        └── res_body.*        # response body (extension from Magika AI detection)
```
- `SUMMARY.json` contains: `session` (metadata), `session_flow` (chronological AI-generated summaries of user actions with timestamps), `statistics` (total_requests, total_actions, methods breakdown, domains breakdown, status_codes, with_auth, with_cookies, content_detection stats).
- `timeline.json` interleaves two event types:
  - `user_action`: action type (click, input, keypress, page_changed), details, plus AI annotations from video analysis — `ai_macro_summary`, `ai_elements_interacted`, `ai_action_success`, `ai_video_file`, `video_start_sec`, `video_end_sec`.
  - `network_request`: method, url, status, and `folder` pointing to the request's directory in `requests/`.
  Use timestamps to correlate user actions with the network requests they triggered.
- `transaction.json` contains: `metadata` (method, url, status, timing with unix timestamps and duration_ms, security flags for authorization/challenge headers), `request` (headers, cookies_sent, content_detection from Magika, has_payload), `response` (headers, cookies_set, content_detection, has_body, mime_mismatch flag).
</environment>

<approach>
You work like a scientist. You observe, form beliefs, test them, and update your understanding based on what actually happens. You are honest about what you know vs. what you're guessing.

- **Be curious.** When you see output, actually read it. When it's truncated, paginate. When something looks interesting, dig deeper.
- **Be skeptical.** When you think you know something, test that specific belief before building on it. One cell, one question.
- **Be honest.** When you're guessing, know you're guessing. When something looks wrong, stop and investigate instead of moving on.
- **Be incremental.** Don't write 50 lines when 3 lines would answer your current question. Small experiments, clear results.
- **Know when to shift gears.** If you've been reading for a while and have beliefs worth testing, switch to testing mode. If tests keep failing, go back to reading. If everything's verified, start building.

## Working Modes

You operate in one of three modes. Each mode is a different lens on the same problem — not a rigid phase. You can switch at any time using `switch_mode`. When you switch, write a research memo capturing what you know, what's uncertain, and what to look at next. That memo is for yourself — the next mode will read it.

### Reading Mode
You're exploring the session dump. Reading files, grepping, following threads, building understanding. You haven't formed strong enough beliefs to test yet, or a test failed and you need to go back and look more carefully.

### Testing Mode
You have specific beliefs and you're verifying them against the live site. One hypothesis per cell. You're not writing the final script — you're running small experiments to confirm or refute what you think you know.

### Building Mode
You have enough verified pieces to assemble the final script. You're composing, running end-to-end, and checking that the output makes sense. If something breaks, figure out which piece failed and go back to testing that piece.

## Script Principles
- Use `requests.Session()` by default. Use `curl_cffi` with `impersonate="chromeXXX"` if you hit TLS fingerprinting (empty responses, 403s, challenge pages).
- Never hardcode ephemeral values (tokens, session IDs). Always extract them dynamically.
- If you don't know where a value comes from, go back to the dump.
- Only deliver the script after you've seen it produce correct output.
</approach>

<critical_rules>
1. **Truth above all.** Never lie to yourself. If something isn't working, admit it. If you're confused, admit it. If your output looks wrong, don't pretend it's right. If you're guessing, say so. Every other rule here is optional. This one is not.
2. **When lost, go back to reading mode.** It is fine to get confused. It is fine to get stuck. Whenever things aren't making sense — a response doesn't match expectations, a parse returns garbage, you're not sure what to try next — switch back to reading mode and look at the dump with fresh eyes. In very rare cases, if something is genuinely impossible, tell the user honestly. But exhaust your curiosity first.
3. **ALWAYS start by reading SUMMARY.json and timeline.json.** Before doing anything else — before writing any code, asking questions, or forming hypotheses — read the full SUMMARY.json (especially the `session_flow` section, which is a chronological AI-generated summary of every action the user performed) and the full timeline.json. Paginate if truncated. Do NOT skip this step.
4. **NEVER write code that hits the live site until you understand what you're trying to reproduce.** You should be able to explain what request you're about to make and why before you make it.
5. **Always write out your reasoning in the main message body before making a tool call.** Explain what you just observed, what you now believe, what you are about to do and why. Do NOT leave the message body empty.
</critical_rules>
"""  # noqa: E501

MODE_INJECTIONS = {
    "reading": (
        "You are now in **reading mode**. "
        "Build your understanding of what happened in the recorded session. "
        "Explore the session dump. Read files, grep, follow values(tokens, keys, cookies) that "
        "help to recreate the goal request. "
        "When you have specific beliefs worth testing against the live site, "
        "switch to testing mode and write down what you've learned."
        "In reading mode, your aim is not only to extract info from the session_dump,"
        "But as well as from the user. You need to ask objective, "
        "quantitative and qualitative questions as well. so clarify what user wants by asking"
    ),
    "testing": (
        "You are now in testing mode. "
        "Verify your assumptions against the live site, one at a time, one cell, one question. "
        "If something does not match your expectations, investigate it or return to reading mode to dig deeper. "
        "Crazy out-of-box thinking when debugging, try to follow your hunch and"
        " exhaust your own hypotheses before attempting to give up. "
        "Always try not to hardcode temporary values(cookies, tokens) in your script. Instead go back to reading mode,"
        " to figure out how they are created or extracted"
        "If you cannot figure out what is going wrong, return to reading mode. "
        "Once you have enough verified pieces, switch to building mode."
    ),
    "building": (
        "You are now in building mode. "
        "Assemble your verified pieces into the final script and run it end-to-end. "
        "Validate that it runs correctly and that the output makes sense. "
        "Do not accept hardcoded values, headers, or tokens. "
        "Everything must be dynamically bootstrapped before the target request is made. "
        "If any hardcoded information is found, return to reading mode to figure out how to derive those values "
        "If anything fails, identify the broken piece and return to testing mode for that piece only."
    ),
}

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "execute_ipython",
            "description": "Execute a python or ipython script in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ipython_script": {
                        "type": "string",
                        "description": (
                            "A single IPython cell to execute. Supports standard Python plus IPython features: "
                            "magic commands (%cd, %timeit, %%timeit, %run, %store, %macro, %edit, %prun), "
                            "shell access via ! prefix (!ls, var = !cmd), "
                            "dynamic introspection (obj?, obj??), "
                            "namespace search with wildcards (%psearch, *pattern*), "
                            "auto-parentheses (%autocall: sin 3 -> sin(3)), "
                            "$ variable expansion in shell commands (!echo $myvar), "
                            "and custom sandbox commands: "
                            "%reset (wipe all variables, imports, and history — fresh kernel), "
                            "%restore (re-run all previously successful cells to recover state after a crash or reset), "
                            "%view_output Cell_N [--offset Y] (page through long output of a past cell). "
                            "Code must be syntactically complete — no dangling blocks or unclosed brackets."
                        ),
                    },
                },
                "required": ["ipython_script"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_mode",
            "description": "Switch the agent's current mode (reading, testing, building).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_mode": {
                        "type": "string",
                        "enum": ["reading", "testing", "building"],
                        "description": "The mode you want to switch to.",
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "A research memo for yourself in the next mode. Write down what "
                            "you were investigating, observed, confident about, etc."
                        ),
                    },
                },
                "required": ["target_mode", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_submit",
            "description": "Submit the final python script once you have successfully built and verified it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "final_python_script": {
                        "type": "string",
                        "description": "The complete, raw python script you have built and successfully tested.",
                    },
                },
                "required": ["final_python_script"],
            },
        },
    },
]

# -----------------
# LIFECYCLE & HELPERS
# -----------------


@events.preload_start.connect
def handle_preload_start(sender, **kwargs):
    global _preloaded_sandbox
    if _preloaded_sandbox is None:
        workspace = str(config.WORKSPACE_DIR)
        os.makedirs(workspace, exist_ok=True)
        _preloaded_sandbox = AgentSandbox(
            working_dir=workspace,
            timeout_seconds=config.SANDBOX_TIMEOUT_SECONDS,
            bin_path=str(config.BIN_DIR),
        )
    events.preload_end.send("core")


def run_cancellable(token: CancelToken, fn, *args, **kwargs):
    """Run *fn* in a thread, returning early if *token* is cancelled."""
    result_box = [None]
    error_box = [None]
    done = threading.Event()

    def worker():
        try:
            result_box[0] = fn(*args, **kwargs)
        except Exception as exc:
            error_box[0] = exc
        finally:
            done.set()

    token.reset()
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while not done.is_set():
        if token.is_cancelled():
            token.reset()
            raise CancelRequestedException("Cancelled via token")
        done.wait(timeout=0.15)
    if error_box[0] is not None:
        raise error_box[0]
    return result_box[0]


def compress_history(messages: list[dict], cutoff_turn=20) -> list[dict]:
    """Truncates massive tool outputs from older messages to save tokens."""
    if len(messages) <= cutoff_turn:
        return messages

    compressed = []
    # System prompt is index 0
    # Everything before (len(messages) - cutoff_turn) gets compressed if it's a huge tool output
    threshold_idx = len(messages) - cutoff_turn

    for i, msg in enumerate(messages):
        if i < threshold_idx and msg.get("role") == "tool":
            content_str = str(msg.get("content", ""))
            # If the tool output is large, truncate it to save context window
            if len(content_str) > 1000:
                compressed.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.get("tool_call_id"),
                        "name": msg.get("name"),
                        "content": "<Truncated older tool output to save tokens>",
                    }
                )
                continue
        compressed.append(msg)
    return compressed


# -----------------
# AGENT LOOP
# -----------------


def run_agent(input_queue: queue.Queue = None, cancel_token: CancelToken = None):
    events.agent_start.send("core")
    """Interactive agent loop. Reads from the workspace produced by the recorder."""
    if cancel_token is None:
        cancel_token = CancelToken()

    workspace_dir = config.WORKSPACE_DIR
    session_dump = workspace_dir / "session_dump"
    if not session_dump.exists() or not any(session_dump.iterdir()):
        events.log_error.send("core", text=f"No recorded session found at {session_dump}")
        events.log_info.send(
            "core", text="Run 'automatiq record <url>' first, or use 'automatiq run <url>' for one-shot."
        )
        sys.exit(1)
    workspace = str(workspace_dir)
    os.makedirs(workspace, exist_ok=True)

    global _preloaded_sandbox
    if _preloaded_sandbox is not None:
        sandbox = _preloaded_sandbox
        _preloaded_sandbox = None
    else:
        sandbox = AgentSandbox(
            working_dir=workspace,
            timeout_seconds=config.SANDBOX_TIMEOUT_SECONDS,
            bin_path=str(config.BIN_DIR),
        )

    # Initial state
    current_mode = "reading"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{MODE_INJECTIONS['reading']}\n\nSession started. You are in reading mode."},
    ]

    needs_user_input = True
    awaiting_tool_complete = False
    awaiting_mode_switch = False
    scr = ""
    mode_switch_notification = ""
    final_script_bounces = 0
    MAX_FINAL_SCRIPT_BOUNCES = 1

    exec_history: list[tuple[str, str, int]] = []
    consecutive_execs = 0
    cell_counter = 0
    MAX_CONSECUTIVE_EXECS = 12
    prev_thought = ""
    MAX_LLM_RETRIES = 5
    BASE_BACKOFF = 10

    def _extract_message(exc):
        """Pull a readable summary from an exception, stripping litellm wrapper noise."""
        import re

        def _clean(raw):
            s = str(raw)
            s = re.sub(r"^litellm\.\w+:\s*", "", s)
            s = re.sub(r"^\w+Exception\s+\w+\s*-\s*", "", s)
            json_match = re.search(r"\{.*\}", s, re.DOTALL)
            if json_match:
                try:
                    body = json.loads(json_match.group())
                    if "error" in body:
                        err = body["error"]
                        if isinstance(err, dict) and "message" in err:
                            return err["message"]
                        return str(err)
                    if "message" in body:
                        return body["message"]
                except json.JSONDecodeError:
                    pass
            return s.split("\n")[0][:300]

        return _clean(exc)

    def _call_llm(msgs):
        """Blocking LLM call - runs inside the interruptible wrapper."""
        kwargs = dict(
            model=config.AGENT_MODEL,
            messages=msgs,
            tools=AGENT_TOOLS,
            tool_choice="auto",
            temperature=0.3,
            timeout=30,
        )
        if config.API_BASE:
            kwargs["api_base"] = config.API_BASE

        # Enable extended thinking/reasoning for models that support it
        if litellm.supports_reasoning(model=config.AGENT_MODEL):
            kwargs["reasoning_effort"] = "high"

        return litellm.completion(**kwargs)

    try:
        for step in range(config.MAX_AGENT_STEPS):
            if sandbox.cancel_result is not None:
                cr = sandbox._cancel_result
                sandbox._cancel_result = None
                if cr == "lost":
                    messages.append(
                        {
                            "role": "user",
                            "content": "SYSTEM: Execution cancelled by user — process was force-killed. State lost. "
                            "Run %restore to recover previous variables.",
                        }
                    )
                elif cr == "preserved":
                    messages.append(
                        {
                            "role": "user",
                            "content": "SYSTEM: Execution interrupted by user. State preserved — variables are intact.",
                        }
                    )
                awaiting_tool_complete = False
                awaiting_mode_switch = False
                needs_user_input = True
                continue

            if needs_user_input:
                events.prompt_request_start.send("core")
                if input_queue is not None:
                    ip = input_queue.get()
                    events.prompt_request_end.send("core")
                else:
                    try:
                        ip = input(">>> ")
                    except EOFError:
                        ip = "q"
                if ip.strip().lower() == "q":
                    events.log_info.send("core", text="User requested exit.")
                    break
                messages.append({"role": "user", "content": ip})
                needs_user_input = False
                consecutive_execs = 0

            elif awaiting_tool_complete:
                # Handled via tool loop logic
                awaiting_tool_complete = False

            elif awaiting_mode_switch:
                messages.append({"role": "user", "content": mode_switch_notification})
                awaiting_mode_switch = False

            # Compress history to save tokens
            compiled_messages = compress_history(messages, cutoff_turn=20)

            resp = None
            aborted = False
            for attempt in range(1, MAX_LLM_RETRIES + 1):
                try:
                    events.llm_request_start.send("core")
                    try:
                        resp = run_cancellable(cancel_token, _call_llm, compiled_messages)
                    finally:
                        events.llm_request_end.send("core")
                    break
                except CancelRequestedException:
                    events.log_info.send("core", text="Cancelled by token. Returning to prompt.")
                    events.operation_cancelled.send("core")
                    aborted = True
                    break
                except (
                    RateLimitError,
                    ServiceUnavailableError,
                    APIConnectionError,
                    Timeout,
                    APIError,
                ) as exc:
                    msg = _extract_message(exc)
                    wait = BASE_BACKOFF * (2 ** (attempt - 1))
                    events.log_warn.send("core", text=f"LLM call failed (attempt {attempt}/{MAX_LLM_RETRIES}): {msg}")
                    logger.exception("Exception occurred")
                    if attempt < MAX_LLM_RETRIES:
                        events.log_warn.send("core", text=f"Retrying in {wait}s ...")
                        events.wait_start.send("core", seconds=wait, reason="Retrying")

                        cancelled = False
                        for _ in range(wait):
                            if cancel_token.is_cancelled():
                                cancelled = True
                                break
                            time.sleep(1)
                        if cancelled:
                            cancel_token.reset()
                            events.log_info.send("core", text="Cancelled by token. Returning to prompt.")
                            events.operation_cancelled.send("core")
                            aborted = True
                            break
                    else:
                        events.log_error.send("core", text="Max retries exceeded. Returning to prompt.")
                        aborted = True
                        break

            if aborted or resp is None:
                needs_user_input = True
                awaiting_tool_complete = False
                awaiting_mode_switch = False
                continue

            msg_obj = resp.choices[0].message
            tool_calls = msg_obj.tool_calls

            # Extract reasoning and content natively
            reasoning = getattr(msg_obj, "reasoning_content", None)
            content = msg_obj.content or ""

            if not tool_calls:
                messages.append(msg_obj.model_dump(exclude_none=True))
                if reasoning:
                    events.agent_thought.send("core", text=reasoning)
                if content:
                    events.agent_text.send("core", text=content)
                needs_user_input = True
                continue

            tool_call = tool_calls[0]
            tool_name = tool_call.function.name
            try:
                tool_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                tool_args = {}

            events.step_start.send("core", step=step, prompt_tokens=resp.usage.prompt_tokens)

            # Deduplicate logic based on both reasoning and content
            current_thought = f"{reasoning or ''}\n{content}".strip()

            if current_thought == prev_thought and current_thought:
                events.log_warn.send("core", text="Exact duplicate message body detected.")
                messages.append(msg_obj.model_dump(exclude_none=True))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": (
                            "SYSTEM: Your reasoning is identical to the previous turn — "
                            "word for word. You are looping. Either:\n"
                            "1. Switch to a different mode for a fresh perspective, or\n"
                            "2. Tell the user what you've found so far and ask for guidance.\n"
                            "Do NOT repeat the same action."
                        ),
                    }
                )
                continue

            prev_thought = current_thought

            # Append the LLM's assistant message (contains BOTH text and tool_calls)
            messages.append(msg_obj.model_dump(exclude_none=True))
            if reasoning:
                events.agent_thought.send("core", text=reasoning)
            if content:
                events.agent_text.send("core", text=content)

            # Process the specific tool
            if tool_name == "final_submit":
                script_content = tool_args.get("final_python_script", "")

                if current_mode != "building":
                    events.log_warn.send("core", text="Final script submitted outside building mode — bouncing back.")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": (
                                "Hey, it seems you are trying to finish the script while not in building mode. "
                                "If stuck, or the output isn't working, switch to reading or testing mode "
                                "as you wish. We have only one True RULE: Truth and truth alone."
                            ),
                        }
                    )
                    continue

                final_script_bounces += 1
                if final_script_bounces <= MAX_FINAL_SCRIPT_BOUNCES:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": (
                                "Hi there, looks like you have created the final script. "
                                "I just came here to verify if you have actually tested it or not. "
                                "In case the script isn't running, don't worry, just go back to "
                                "reading mode or testing mode. They will take care of the validity. "
                                "If test and read modes actually say they can't find any way "
                                "to make this work, then you can yield before the user that you "
                                "can't find any solution by writing that in normal text and halting. "
                                "\nIf you have already tested it, then just submit it again."
                            ),
                        }
                    )
                    continue

                events.log_info.send("core", text="Agent submitted the final script.")
                events.tool_message.send("core", text=f"\n--- FINAL SCRIPT ---\n\n{script_content}\n")
                break

            elif tool_name == "execute_ipython":
                script_to_run = tool_args.get("ipython_script", "")
                consecutive_execs += 1
                cell_counter += 1
                current_cell = cell_counter
                repeat_count = 0
                matched_cell = None

                for prev_script, _prev_output, prev_cell in exec_history:
                    if script_to_run.strip() == prev_script:
                        repeat_count += 1
                        matched_cell = prev_cell

                if repeat_count >= 2 and matched_cell is not None:
                    events.log_warn.send(
                        "core", text=f"Blocked: script ran {repeat_count}x (last: Cell_{matched_cell})."
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": (
                                f"SYSTEM: This exact script has already been executed {repeat_count} times "
                                f"with the same output. It was NOT executed again. "
                                f"Use %view_output Cell_{matched_cell} to review the previous output. "
                                f"Try a fundamentally different approach."
                            ),
                        }
                    )
                    continue

                events.code_exec_start.send("core", script=script_to_run)
                try:
                    events.code_exec_start.send("core")
                    try:
                        scr = run_cancellable(cancel_token, sandbox.execute, script_to_run)
                    finally:
                        events.code_exec_end.send("core")
                except CancelRequestedException:
                    sandbox.cancel()
                    events.log_info.send("core", text="Cancelled by token. Returning to prompt.")
                    events.operation_cancelled.send("core")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": "SYSTEM: Execution cancelled by user.",
                        }
                    )
                    needs_user_input = True
                    continue

                output_match_cell = None
                for _prev_script, prev_output, prev_cell in exec_history:
                    if prev_output and scr == prev_output and len(scr) > 100:
                        output_match_cell = prev_cell
                        break

                if output_match_cell is not None:
                    scr = (
                        f"The output is the same as Cell_{output_match_cell}. "
                        f"Use %view_output Cell_{output_match_cell} if you need to review it."
                    )
                exec_history.append((script_to_run.strip(), scr, current_cell))
                events.code_exec_output.send("core", output=scr)

                tool_response_content = f"<terminal_output>\n{scr}\n</terminal_output>"

                if consecutive_execs >= MAX_CONSECUTIVE_EXECS:
                    tool_response_content += (
                        f"\nSYSTEM: You have been running code for {consecutive_execs} consecutive turns "
                        f"without switching modes or talking to the user. "
                        f"Take a step back and evaluate your progress:\n"
                        f"- Are you making forward progress or going in circles?\n"
                        f"- Would switching to a different mode help?\n"
                        f"- Is there something you should ask the user about?\n"
                        f"If you are stuck, switch modes. "
                        f"If you have findings, share them with the user."
                    )

                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "name": tool_name, "content": tool_response_content}
                )

                # Automatically proceed to next step (no user input needed) unless cancelled
                awaiting_tool_complete = False

            elif tool_name == "switch_mode":
                target_mode = tool_args.get("target_mode", "reading")
                if target_mode not in ["reading", "testing", "building"]:
                    target_mode = "reading"

                context_memo = tool_args.get("context", "")
                consecutive_execs = 0
                events.mode_switch.send("core", mode=target_mode)

                current_mode = target_mode
                mode_injection = MODE_INJECTIONS.get(target_mode, "")

                # Give the tool a success response, and queue up the next user message
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": "Mode switched successfully.",
                    }
                )

                mode_switch_notification = (
                    f"{mode_injection}\n\n--- Research memo from previous mode ---\n{context_memo}"
                )
                awaiting_mode_switch = True

    except Exception as exc:
        events.log_error.send("core", text=f"Unexpected error: {exc}")
        logger.exception("Exception occurred")
        sandbox.cancel()
    finally:
        try:
            _export_session_logs(messages)
        except Exception as exc:
            events.log_error.send("core", text=f"Failed to save session logs: {exc}")
            logger.exception("Exception occurred")
        sandbox.close()


def _export_session_logs(messages: list[dict]):
    """Write session logs to output/history/."""

    class _SessionDumper(yaml.Dumper):
        pass

    def multiline_presenter(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    _SessionDumper.add_representer(str, multiline_presenter)
    history_dir = str(config.HISTORY_DIR)
    os.makedirs(history_dir, exist_ok=True)

    # Save the full trace
    uncompressed_path = os.path.join(history_dir, "messages_full.yaml")
    with open(uncompressed_path, "w", encoding="utf-8") as f:
        yaml.dump(messages, f, Dumper=_SessionDumper, sort_keys=False, allow_unicode=True)
    logger.info(f"Saved full session history to {uncompressed_path}")

    # Save the compressed version exactly as the LLM saw it
    compressed = compress_history(messages, cutoff_turn=20)
    compressed_path = os.path.join(history_dir, "messages_compressed.yaml")
    with open(compressed_path, "w", encoding="utf-8") as f:
        yaml.dump(compressed, f, Dumper=_SessionDumper, sort_keys=False, allow_unicode=True)
    logger.info(f"Saved compressed session history to {compressed_path}")
