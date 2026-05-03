import tempfile
import time

import pytest

from automatiq.core import config
from automatiq.core.ipython_sandbox import AgentSandbox


@pytest.fixture
def sandbox():
    """Provides a fresh, isolated AgentSandbox instance for each test."""
    with tempfile.TemporaryDirectory() as temp_dir:
        sb = AgentSandbox(working_dir=temp_dir, timeout_seconds=2, bin_path=str(config.BIN_DIR))
        try:
            yield sb
        finally:
            sb.close()
            # Give Windows processes a moment to fully release file locks
            time.sleep(0.5)
