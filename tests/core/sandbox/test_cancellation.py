import sys
import threading
import time

from automatiq.core.ipython_sandbox import AgentSandbox


def test_soft_cancel_python_loop(sandbox: AgentSandbox):
    """Test soft cancellation where the kernel catches KeyboardInterrupt in a python busy loop."""
    sandbox.timeout_seconds = 10
    results = {}

    def run_slow_code():
        # Busy loops can catch KeyboardInterrupt
        code = (
            "import time\n"
            "try:\n"
            "    end = time.time() + 5\n"
            "    while time.time() < end:\n"
            "        pass\n"
            "except KeyboardInterrupt:\n"
            "    print('caught interrupt')\n"
        )
        try:
            results["out"] = sandbox.execute(code, custom_timeout=10)
        except Exception as e:
            results["out"] = f"EXCEPTION: {type(e).__name__} - {e}"

    t = threading.Thread(target=run_slow_code)
    t.start()

    time.sleep(0.5)  # Give it a moment to start executing

    sandbox.cancel()

    time.sleep(2.0)  # Wait for the background cancel worker to finish

    sandbox.close()

    # Ensure thread is joined before asserting, to prevent Pytest hangs on assertion failure
    t.join(timeout=8)

    assert sandbox.cancel_result == "preserved"

    # In newer Python versions (3.12+), IPython's run_cell bypasses user try..except
    # blocks for tight loops and returns the traceback directly, or triggers our Soft Timeout.
    # We just need to verify that an interrupt occurred (either caught natively, or fallback).
    out_lower = results.get("out", "").lower()
    assert "interrupt" in out_lower or "timeout" in out_lower or "cancelledbyuser" in out_lower


def test_cancel_native_sleep(sandbox: AgentSandbox):
    """Test cancellation of time.sleep().
    On Windows, sleep blocks the thread so KeyboardInterrupt cannot be caught, resulting in a wipe.
    On POSIX, SIGINT breaks the sleep natively, resulting in preserved state.
    """
    sandbox.timeout_seconds = 10
    results = {}

    def run_sleep_code():
        code = "import time\ntry:\n    time.sleep(5)\nexcept KeyboardInterrupt:\n    print('caught interrupt')\n"
        try:
            results["out"] = sandbox.execute(code, custom_timeout=10)
        except Exception as e:
            results["out"] = f"EXCEPTION: {type(e).__name__} - {e}"

    t = threading.Thread(target=run_sleep_code)
    t.start()

    time.sleep(0.5)

    sandbox.cancel()

    # Ensure thread is joined before closing, so we wait for execute() to finish its fallback start_process
    t.join(timeout=25)

    sandbox.close()

    if sys.platform == "win32":
        # On Windows, time.sleep() often blocks the thread so KeyboardInterrupt cannot be caught or takes too long,
        # resulting in a hard kill.
        assert sandbox.cancel_result == "lost"
        assert "CancelledByUser" in results.get("out", "")
    else:
        # Mac/Linux can interrupt it
        assert sandbox.cancel_result == "preserved"
        assert "caught interrupt" in results.get("out", "")


def test_soft_timeout(sandbox: AgentSandbox):
    """Test that execution times out correctly without manual cancellation."""
    # We use custom_timeout=1 and a busy loop.
    result = sandbox.execute("import time\nend = time.time() + 5\nwhile time.time() < end:\n    pass", custom_timeout=1)
    sandbox.close()
    assert "Status: ERROR" in result
    assert "[TIMEOUT: Execution interrupted. State preserved.]" in result
