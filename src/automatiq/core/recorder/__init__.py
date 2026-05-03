"""Recorder sub-package — captures a full browser session (network + video + actions)
and compiles it into a structured workspace dump for the agent.
Usage: from automatiq.recorder import run_recording; run_recording("https://example.com")
"""

import asyncio
import logging
import os
import shutil
import tempfile
import urllib.request

from .. import config
from ..cancel_standard import CancelToken, StopToken
from .blocklist_db import BlocklistDB
from .browser_agent import BrowserAgent
from .data_compressor import compile_workspace
from .video_recorder import ActionVideoRecorder

logger = logging.getLogger(__name__)


def _init_blocklist() -> BlocklistDB:
    """Create (or open) the persistent blocklist DB and download any missing source files."""
    db = BlocklistDB(db_path=str(config.BLOCKLIST_DB))

    for name, url in config.BLOCKLIST_SOURCES.items():
        hosts_file = config.BLOCKLIST_DIR / f"{name}.txt"

        if not hosts_file.exists():
            logger.info(f"Downloading blocklist '{name}' ...")
            try:
                urllib.request.urlretrieve(url, str(hosts_file))
                logger.info(f"Saved {hosts_file.name}")
            except Exception as exc:
                logger.warning(f"Failed to download blocklist '{name}': {exc}")
                continue

        count = db.load_file(str(hosts_file), source_name=name, source_url=url)
        logger.debug(f"{name}: {count:,} domains")

    return db


def run_recording(
    url: str = "about:blank", cancel_token: CancelToken = None, stop_token: StopToken = None, skip_callback=None
) -> bool:
    """Run the full recording pipeline: browser -> video -> compile workspace.

    1. Launches Chrome with CDP instrumentation and screen capture.
    2. User browses freely; Ctrl+C stops the session.
    3. Compiles the captured data into output/workspace/session_dump/.
    """

    if not config.WORKSPACE_DIR.exists():
        config.ensure_output_dirs()

    temp_video_path = os.path.join(tempfile.gettempdir(), "automatiq_full_record.mp4")

    blocklist = _init_blocklist()

    _video_recorder = ActionVideoRecorder(fps=config.FPS, output_path=temp_video_path)
    _browser_agent = BrowserAgent(blocklist=blocklist)

    logger.info("[RULE] STARTING RECORDER")
    logger.info(f"Target URL : {url}")
    logger.info(f"AI Model   : {config.RECORDER_AI_MODEL}")
    logger.info(f"Blocklist  : {blocklist.total_enabled_domains()} domains loaded")
    logger.info("Press Ctrl+C to stop recording")

    session_data = None
    success = False

    try:
        _video_recorder.start()
        session_data = asyncio.run(_browser_agent.run_session(url=url, stop_token=stop_token))
    except KeyboardInterrupt:
        # If a raw SIGINT still sneaks through (e.g. from the async loop)
        logger.warning("KeyboardInterrupt caught in run_recording.")
        if stop_token:
            stop_token.stop()
    except Exception as exc:
        logger.error(f"Recording session failed: {exc}")
        logger.exception("Exception occurred")
    finally:
        video_start_unix = None
        try:
            video_start_unix = _video_recorder.stop()
        except Exception as exc:
            logger.error(f"Failed to stop video recorder: {exc}")
            logger.exception("Exception occurred")

        if stop_token:
            stop_token.reset()

        if cancel_token is None:
            cancel_token = CancelToken()

        def ask_user_to_skip(remaining: int) -> bool:
            if skip_callback:
                return skip_callback(remaining, cancel_token)
            cancel_token.reset()
            return True

        if session_data and video_start_unix:
            try:
                success = compile_workspace(
                    session_data=session_data,
                    full_video_path=temp_video_path,
                    video_start_unix=video_start_unix,
                    on_skip_requested=ask_user_to_skip,
                    cancel_token=cancel_token,
                    stop_token=stop_token,
                )
            except Exception as exc:
                logger.error(f"Workspace compilation raised unexpectedly: {exc}")
                logger.exception("Exception occurred")

            final_video_path = os.path.join(str(config.WORKSPACE_DIR), "session_dump", "full_record.mp4")
            if success and os.path.exists(temp_video_path):
                try:
                    os.makedirs(os.path.dirname(final_video_path), exist_ok=True)
                    shutil.move(temp_video_path, final_video_path)
                    logger.info(f"Full recording saved to {final_video_path}")
                except OSError as exc:
                    logger.error(f"Failed to move recording to workspace: {exc}")
                    logger.exception("Exception occurred")
        else:
            logger.warning("Session data or video timestamp missing. Skipping compilation.")

        blocked = _browser_agent.stats["blocked_by_blocklist"] if _browser_agent else 0
        if blocked:
            logger.info(f"Blocklist filtered {blocked} ad/tracker request(s)")

        try:
            blocklist.close()
        except Exception as exc:
            logger.warning(f"Failed to close blocklist DB: {exc}")

    return success
