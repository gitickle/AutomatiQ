"""
Centralized rich console for all AutomatiQ output.

Every module imports from here instead of using bare print() calls.
This gives us consistent styling, color-coded log levels, and
nice panels for agent output — all from a single shared Console.
"""

import atexit
import logging
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from ..core.cancel_standard import CancelToken, StopToken

_theme = Theme(
    {
        "info": "bold cyan",
        "warn": "bold yellow",
        "error": "bold red",
        "success": "bold green",
        "action": "magenta",
        "video": "blue",
        "ai": "bold magenta",
        "think": "italic",
        "exec": "bold yellow",
        "output": "white",
        "dim": "dim",
    }
)

console = Console(theme=_theme, highlight=False)

# ---------------------------------------------------------------------------
# File logger — writes timestamped entries + full tracebacks to
# output/logs/session_<timestamp>.log.  Initialized lazily by
# init_file_logger() which main.py calls after ensure_output_dirs().
# ---------------------------------------------------------------------------

_file_logger: logging.Logger | None = None


def init_file_logger(logs_dir: str) -> None:
    """Create the session log file and attach a file handler."""
    global _file_logger

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = f"{logs_dir}/session_{stamp}.log"

    _file_logger = logging.getLogger("automatiq.session")
    _file_logger.setLevel(logging.DEBUG)
    _file_logger.propagate = False

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-5s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    _file_logger.addHandler(handler)


def _log(level: int, msg: str) -> None:
    if _file_logger:
        _file_logger.log(level, msg)


def info(msg: str) -> None:
    first_line = str(msg).splitlines()[0] if str(msg).splitlines() else str(msg)
    console.print(f"[info]\\[INFO][/info] {escape(first_line)}")
    _log(logging.INFO, msg)


def warn(msg: str) -> None:
    first_line = str(msg).splitlines()[0] if str(msg).splitlines() else str(msg)
    console.print(f"[warn]\\[WARN][/warn] {escape(first_line)}")
    _log(logging.WARNING, msg)


def error(msg: str) -> None:
    first_line = str(msg).splitlines()[0] if str(msg).splitlines() else str(msg)
    console.print(f"[error]\\[ERROR][/error] {escape(first_line)}")
    _log(logging.ERROR, msg)


def success(msg: str) -> None:
    console.print(f"[success]\\[SUCCESS][/success] {escape(msg)}")
    _log(logging.INFO, msg)


def action(msg: str) -> None:
    console.print(f"[action]\\[ACTION][/action] {escape(msg)}")
    _log(logging.INFO, msg)


def video(msg: str) -> None:
    console.print(f"[video]\\[VIDEO][/video] {escape(msg)}")
    _log(logging.INFO, msg)


def ai(msg: str) -> None:
    console.print(f"[ai]\\[AI][/ai] {escape(msg)}")
    _log(logging.INFO, msg)


def think(text: str) -> None:
    quoted = Panel(Markdown(text), title="[think]Thinking[/think]", border_style="dim", padding=(0, 1))
    console.print(quoted)


def agent_markdown(text: str) -> None:
    """Print standard agent text output as free-floating Markdown."""
    console.print(Markdown(text))


def countdown(seconds: int, message: str = "Retrying", cancel_check=None) -> bool:
    """Live countdown on a single line. Returns True if cancelled via *cancel_check*."""
    with Live(
        Text(f"{message} in {seconds}s ...", style="dim"), console=console, refresh_per_second=2, transient=True
    ) as live:
        for remaining in range(seconds, 0, -1):
            if cancel_check and cancel_check():
                return True
            live.update(Text(f"{message} in {remaining}s ...", style="dim"))
            time.sleep(1)
    return False


def code_block(code: str, lang: str = "python") -> None:
    syntax = Syntax(code, lang, theme="monokai", line_numbers=True, word_wrap=True)
    console.print(Panel(syntax, title="[exec]EXEC[/exec]", border_style="yellow", padding=(0, 0)))


def output_panel(text: str) -> None:
    console.print(Panel(escape(text), title="[output]OUTPUT[/output]", border_style="dim", padding=(0, 1)))


def step_info(step: int, prompt_tokens: int) -> None:
    console.print(f"[dim]Step {step} | Prompt tokens: {prompt_tokens}[/dim]")


def rule(title: str = "", style: str = "dim") -> None:
    console.print(Rule(title=title, style=style))


def detail(msg: str) -> None:
    _log(logging.DEBUG, msg)
    from ..core import config

    if config.VERBOSE:
        console.print(f"  [dim]{escape(msg)}[/dim]")


def print_exception() -> None:
    """Single line error to terminal + plain traceback to log file."""
    exc_type, exc_val, _ = sys.exc_info()
    if exc_val:
        console.print(f"[error]\\[ERROR][/error] {escape(str(exc_val).splitlines()[0])}")
    else:
        console.print("[error]\\[ERROR][/error] Unknown Exception")

    if _file_logger:
        _file_logger.error(traceback.format_exc())


def log_exception() -> None:
    """Plain traceback to the log file only — nothing on terminal."""
    if _file_logger:
        _file_logger.error(traceback.format_exc())


def spinner(message: str = "Working..."):
    """Returns a rich Status context manager for use with `with` blocks."""
    return console.status(f"[dim]{message}[/dim]", spinner="aesthetic", spinner_style="cyan")


def prompt() -> str:
    return console.input("[bold green]>>> [/bold green]")


def start_cli_listeners(cancel_token: CancelToken, stop_token: StopToken) -> threading.Event | None:
    if not sys.stdin.isatty():
        return None

    active = threading.Event()
    active.set()

    # Catch raw OS SIGINT to prevent KeyboardInterrupt from crashing the main thread on the first press
    try:

        def sigint_handler(signum, frame):
            if stop_token.is_stopped():
                # Second press: Force a hard exit
                print("\n[ERROR] Force quit requested (Double Ctrl+C). Shutting down immediately.")
                os._exit(1)
            else:
                stop_token.stop()

        signal.signal(signal.SIGINT, sigint_handler)
    except (ValueError, OSError):
        pass  # Fails gracefully if not running in the main thread

    if sys.platform == "win32":
        import msvcrt

        def _listen():
            while active.is_set():
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    if key == b"\x1b":  # ESC
                        cancel_token.cancel()
                        while msvcrt.kbhit():
                            msvcrt.getch()
                    elif key == b"\x03":  # Ctrl+C
                        if stop_token.is_stopped():
                            print("\n[ERROR] Force quit requested (Double Ctrl+C). Shutting down immediately.")
                            os._exit(1)
                        else:
                            stop_token.stop()
                        while msvcrt.kbhit():
                            msvcrt.getch()
                time.sleep(0.05)
    else:
        import select
        import termios
        import tty

        def _listen():
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            # Remove ISIG flag so Ctrl+C (\x03) comes through as a normal char
            new = termios.tcgetattr(fd)
            new[3] = new[3] & ~termios.ISIG
            termios.tcsetattr(fd, termios.TCSANOW, new)

            def _restore():
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
                    termios.tcflush(fd, termios.TCIFLUSH)
                except Exception:
                    pass

            atexit.register(_restore)

            try:
                tty.setcbreak(fd)
                while active.is_set():
                    r, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if r:
                        key = os.read(fd, 1)
                        if key == b"\x1b":  # ESC
                            cancel_token.cancel()
                            termios.tcflush(fd, termios.TCIFLUSH)
                        elif key == b"\x03":  # Ctrl+C
                            if stop_token.is_stopped():
                                print("\n[ERROR] Force quit requested (Double Ctrl+C). Shutting down immediately.")
                                os._exit(1)
                            else:
                                stop_token.stop()
                            termios.tcflush(fd, termios.TCIFLUSH)
            finally:
                _restore()

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    return active
