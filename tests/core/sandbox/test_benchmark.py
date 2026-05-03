import tempfile

from automatiq.core import config
from automatiq.core.ipython_sandbox import AgentSandbox


def test_benchmark_sandbox_startup(benchmark):
    """Measures the end-to-end time of initializing the sandbox and executing the first command."""

    def setup_and_run():
        with tempfile.TemporaryDirectory() as temp_dir:
            sb = AgentSandbox(working_dir=temp_dir, timeout_seconds=2, bin_path=str(config.BIN_DIR))
            try:
                # The first execute() call will start the worker process if it hasn't started yet
                sb.execute("pass")
            finally:
                sb.close()

    benchmark.pedantic(setup_and_run, iterations=5, rounds=5)


def test_benchmark_execution_latency(benchmark, sandbox):
    """Measures the round-trip overhead of sending a command and reading the output."""
    # Start the worker process first
    sandbox.execute("pass")

    def run_command():
        sandbox.execute("print('x')")

    benchmark.pedantic(run_command, iterations=10, rounds=10)


def test_benchmark_state_transfer(benchmark, sandbox):
    """Measures the latency overhead when variables persist between executions."""
    sandbox.execute("x = 0")

    def increment_and_read():
        sandbox.execute("x += 1\nprint(x)")

    benchmark.pedantic(increment_and_read, iterations=10, rounds=10)
