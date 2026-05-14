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
from litellm.exceptions import (
    APIConnectionError,
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from . import config, events
from .cancel_standard import CancelRequestedException, CancelToken
from .guardrails import check_duplicate_thought, check_final_script_bounce, check_repeated_execution
from .history import compress_history, export_session_logs
from .ipython_sandbox import AgentSandbox
from .llm import call_llm_blocking, extract_message
from .prompts import MODE_INJECTIONS, SYSTEM_PROMPT
from .tools import AGENT_TOOLS, validate_tool_args

logger = logging.getLogger(__name__)
litellm.suppress_debug_info = True

_preloaded_sandbox = None

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


# -----------------
# AGENT LOOP
# -----------------


def run_agent(input_queue: queue.Queue, cancel_token: CancelToken = None):
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
    MAX_VALIDATION_RETRIES = 3

    exec_history: list[tuple[str, str, int]] = []
    consecutive_validation_failures = 0
    cell_counter = 0
    prev_description = ""
    MAX_LLM_RETRIES = 5
    BASE_BACKOFF = 10

    consecutive_autonomous_turns = 0
    total_steps = 0

    try:
        while True:
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

            if not needs_user_input:
                if consecutive_autonomous_turns >= config.MAX_AGENT_STEPS:
                    events.agent_text.send(
                        "core",
                        text=f"⚠️ Paused: Agent hit {config.MAX_AGENT_STEPS} consecutive turns without completing task.",
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "SYSTEM GUARDRAIL: You have reached the maximum consecutive turn limit. "
                                "Execution has been paused to wait for user guidance. "
                                "Please review the context and wait for the user's instructions."
                            ),
                        }
                    )
                    needs_user_input = True
                    continue
                consecutive_autonomous_turns += 1

            if needs_user_input:
                events.prompt_request_start.send("core")
                ip = input_queue.get()
                consecutive_autonomous_turns = 0
                events.prompt_request_end.send("core")
                if ip.strip().lower() == "q":
                    events.log_info.send("core", text="User requested exit.")
                    break
                messages.append({"role": "user", "content": ip})
                needs_user_input = False

            elif awaiting_tool_complete:
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
                        resp = run_cancellable(cancel_token, call_llm_blocking, compiled_messages, AGENT_TOOLS)
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
                    msg = extract_message(exc)
                    wait = BASE_BACKOFF * (2 ** (attempt - 1))
                    events.log_warn.send("core", text=f"LLM call failed (attempt {attempt}/{MAX_LLM_RETRIES}): {msg}")
                    events.log_traceback.send("core")
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
            except json.JSONDecodeError as exc:
                tool_args = {}
                validation_error = f"Invalid JSON arguments: {exc}"
            else:
                validation_error = validate_tool_args(tool_name, tool_args)

            if validation_error:
                consecutive_validation_failures += 1
                messages.append(msg_obj.model_dump(exclude_none=True))

                if consecutive_validation_failures >= MAX_VALIDATION_RETRIES:
                    events.log_error.send(
                        "core", text=f"Tool validation failed {MAX_VALIDATION_RETRIES} times. Bailing out."
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": f"SYSTEM: Validation failed repeatedly. Error: {validation_error}. Returning.",
                        }
                    )
                    needs_user_input = True
                    consecutive_validation_failures = 0
                else:
                    events.log_warn.send("core", text=f"Tool validation error: {validation_error}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": f"SYSTEM: Tool Validation Error: {validation_error}. Please try again.",
                        }
                    )
                continue

            consecutive_validation_failures = 0

            total_steps += 1
            events.step_start.send("core", step=total_steps, prompt_tokens=resp.usage.prompt_tokens)

            # Deduplicate logic based on description
            current_description = tool_args.get("description", "").strip() if tool_name == "execute_ipython" else ""
            duplicate_warning = check_duplicate_thought(current_description, prev_description)
            if duplicate_warning:
                events.log_warn.send("core", text="Exact duplicate description detected.")
                messages.append(msg_obj.model_dump(exclude_none=True))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": duplicate_warning,
                    }
                )
                continue

            if tool_name == "execute_ipython":
                prev_description = current_description

            # Append the LLM's assistant message
            messages.append(msg_obj.model_dump(exclude_none=True))
            if reasoning:
                events.agent_thought.send("core", text=reasoning)
            if content:
                events.agent_text.send("core", text=content)

            # Process the specific tool
            if tool_name == "final_submit":
                script_content = tool_args.get("final_python_script", "")

                final_script_bounces += 1
                should_bounce, bounce_message = check_final_script_bounce(
                    current_mode, final_script_bounces, MAX_FINAL_SCRIPT_BOUNCES
                )
                if should_bounce:
                    events.log_warn.send(
                        "core",
                        text="Final script submitted outside building mode (or verification needed) — bouncing back.",
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": bounce_message,
                        }
                    )
                    continue

                events.log_info.send("core", text="Agent submitted the final script.")
                events.tool_message.send("core", text=f"\n--- FINAL SCRIPT ---\n\n{script_content}\n")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": "Final script delivered successfully to the user. Awaiting feedback.",
                    }
                )
                needs_user_input = True
                continue

            elif tool_name == "execute_ipython":
                script_to_run = tool_args.get("ipython_script", "")
                desc = tool_args.get("description", "")

                display_script = f"# {desc}\n{script_to_run}" if desc else script_to_run

                cell_counter += 1
                current_cell = cell_counter

                is_repeated, repeat_warning = check_repeated_execution(display_script, exec_history)
                if is_repeated:
                    events.log_warn.send("core", text="Blocked: exact script ran multiple times.")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_name,
                            "content": repeat_warning,
                        }
                    )
                    continue

                try:
                    events.code_exec_start.send("core", script=display_script)
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
                exec_history.append((display_script.strip(), scr, current_cell))
                events.code_exec_output.send("core", output=scr)

                tool_response_content = f"<terminal_output>\n{scr}\n</terminal_output>"

                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "name": tool_name, "content": tool_response_content}
                )

                awaiting_tool_complete = False

            elif tool_name == "switch_mode":
                target_mode = tool_args.get("target_mode")
                context_memo = tool_args.get("context", "")
                events.mode_switch.send("core", mode=target_mode)

                current_mode = target_mode
                mode_injection = MODE_INJECTIONS.get(target_mode, "")

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
        events.log_traceback.send("core")
        sandbox.cancel()
    finally:
        try:
            export_session_logs(messages)
        except Exception as exc:
            events.log_error.send("core", text=f"Failed to save session logs: {exc}")
            events.log_traceback.send("core")
        sandbox.close()
