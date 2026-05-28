import logging
import queue
import threading

from ..core import events
from ..core.cancel_standard import CancelToken, StopToken
from ..core.main import run_agent
from .console import (
    agent_markdown,
    code_block,
    countdown,
    error,
    info,
    log_exception,
    output_panel,
    prompt,
    spinner,
    step_info,
    think,
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


@events.agent_text.connect
def handle_agent_text(sender, text, **kwargs):
    agent_markdown(text)


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
        _active_spinner = spinner("Running...(Press Esc to Stop)")
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
        _active_spinner = spinner("Thinking...(Press Esc to Stop)")
        _active_spinner.__enter__()


@events.llm_request_end.connect
def handle_llm_request_end(sender, **kwargs):
    global _active_spinner
    if _active_spinner:
        _active_spinner.__exit__(None, None, None)
        _active_spinner = None


def run_agent_cli(cancel_token: CancelToken = None, stop_token: StopToken = None, target: str | None = None):
    if cancel_token is None:
        cancel_token = CancelToken()
    if stop_token is None:
        stop_token = StopToken()

    input_queue = queue.Queue()

    @events.wait_start.connect
    def handle_wait_start(sender, seconds, reason, **kwargs):
        # We pass stop_check to countdown if we update countdown later, but for now
        # let's just use stop_token.is_stopped inside the loop
        def should_abort():
            if stop_token.is_stopped():
                from ..core.cancel_standard import StopRequestedException

                raise StopRequestedException()
            return cancel_token.is_cancelled()

        cancelled = countdown(seconds, message=reason, cancel_check=should_abort)
        if cancelled:
            cancel_token.reset()
            events.operation_cancelled.send("core")

    @events.prompt_request_start.connect
    def handle_prompt_request_start(sender, **kwargs):
        global _first_prompt
        if _first_prompt:
            info("Type in q to quit | Esc to cancel processing | Ctrl+Enter for newline")
            _first_prompt = False

        try:
            ip = prompt()
        except (KeyboardInterrupt, EOFError):
            ip = "q"
        input_queue.put(ip)

    def backend_worker():
        try:
            # We don't have run_agent signature yet, assuming it only takes cancel_token right now
            run_agent(input_queue=input_queue, cancel_token=cancel_token, target=target)
        except Exception as exc:
            error(f"Agent loop crashed: {exc}")
            log_exception()
        finally:
            events.agent_done.send("core")

    t = threading.Thread(target=backend_worker, daemon=True)
    t.start()

    try:
        done_event = threading.Event()

        @events.agent_done.connect
        def handle_agent_done(sender, **kwargs):
            done_event.set()

        while not done_event.wait(timeout=0.1):
            if stop_token.is_stopped():
                info("Abort requested via UI StopToken (Ctrl+C). Exiting...")
                break

        # Wait for backend worker to finish cleanup (export_session_logs + sandbox.close)
        # before the main thread exits. Capped at 5s in case of a hang.
        done_event.wait(timeout=5.0)

    except KeyboardInterrupt:
        info("Interrupted by OS user signal (Ctrl+C). Exiting...")
        stop_token.stop()
        done_event.wait(timeout=5.0)
    finally:
        global _active_spinner
        if _active_spinner:
            _active_spinner.__exit__(None, None, None)
            _active_spinner = None
