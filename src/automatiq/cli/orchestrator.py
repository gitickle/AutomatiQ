import logging
import queue
import threading

from ..core import events
from ..core.cancel_standard import CancelToken
from ..core.main import run_agent
from .console import (
    code_block,
    countdown,
    error,
    info,
    output_panel,
    prompt,
    spinner,
    step_info,
    think,
    warn,
)

logger = logging.getLogger(__name__)

# Global state for UI elements that span events
_active_spinner = None
_first_prompt = True


@events.step_start.connect
def handle_step_start(sender, step, prompt_tokens, **kwargs):
    step_info(step, prompt_tokens)


@events.agent_thought.connect
def handle_agent_thought(sender, text, **kwargs):
    think(text)


@events.tool_message.connect
def handle_tool_message(sender, text, **kwargs):
    print(text)


@events.mode_switch.connect
def handle_mode_switch(sender, mode, **kwargs):
    info(f"Switching to {mode} mode")


@events.code_exec_start.connect
def handle_code_exec_start(sender, script=None, **kwargs):
    global _active_spinner
    if script is not None:
        code_block(script)
    if _active_spinner is None:
        _active_spinner = spinner("Running...")
        _active_spinner.__enter__()


@events.code_exec_output.connect
def handle_code_exec_output(sender, output, **kwargs):
    output_panel(output)


@events.code_exec_end.connect
def handle_code_exec_end(sender, **kwargs):
    global _active_spinner
    if _active_spinner:
        _active_spinner.__exit__(None, None, None)
        _active_spinner = None


@events.llm_request_start.connect
def handle_llm_request_start(sender, **kwargs):
    global _active_spinner
    if _active_spinner is None:
        _active_spinner = spinner("Thinking...")
        _active_spinner.__enter__()


@events.llm_request_end.connect
def handle_llm_request_end(sender, **kwargs):
    global _active_spinner
    if _active_spinner:
        _active_spinner.__exit__(None, None, None)
        _active_spinner = None


@events.log_info.connect
def handle_log_info(sender, text, **kwargs):
    info(text)


@events.log_warn.connect
def handle_log_warn(sender, text, **kwargs):
    warn(text)


@events.log_error.connect
def handle_log_error(sender, text, **kwargs):
    error(text)


def run_agent_cli(cancel_token: CancelToken = None):
    if cancel_token is None:
        cancel_token = CancelToken()

    input_queue = queue.Queue()

    @events.wait_start.connect
    def handle_wait_start(sender, seconds, reason, **kwargs):
        cancelled = countdown(seconds, message=reason, cancel_check=cancel_token.is_cancelled)
        if cancelled:
            cancel_token.reset()
            events.operation_cancelled.send("core")

    @events.prompt_request_start.connect
    def handle_prompt_request_start(sender, **kwargs):
        global _first_prompt
        if _first_prompt:
            info("Type in q to quit | Esc to cancel processing")
            _first_prompt = False

        try:
            ip = prompt()
        except (KeyboardInterrupt, EOFError):
            ip = "q"
        input_queue.put(ip)

    def backend_worker():
        try:
            run_agent(input_queue=input_queue, cancel_token=cancel_token)
        except Exception:
            logger.exception("Agent loop crashed")
        finally:
            events.agent_done.send("core")

    t = threading.Thread(target=backend_worker, daemon=True)
    t.start()

    try:
        # Since Blinker handlers are executed in the sender's thread (which is `backend_worker`),
        # they might run into threading issues if Rich wasn't thread-safe (it mostly is).
        # We need the main thread to stay alive until agent_done.
        done_event = threading.Event()

        @events.agent_done.connect
        def handle_agent_done(sender, **kwargs):
            done_event.set()

        # Wait for the backend thread to finish, keeping main thread alive for interrupts
        while not done_event.wait(timeout=0.1):
            pass

    except KeyboardInterrupt:
        info("Interrupted by user (Ctrl+C). Exiting...")
        cancel_token.cancel()
    finally:
        global _active_spinner
        if _active_spinner:
            _active_spinner.__exit__(None, None, None)
            _active_spinner = None
