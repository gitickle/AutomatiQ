"""Workspace compilation orchestrator — assembles the final session_dump layout."""

import json
import logging
import os
import shutil
from urllib.parse import urlparse

from ... import config, events
from ...cancel_standard import StopRequestedException
from ..ai_analyzer import VideoActionAnalyzer
from .actions import merge_and_annotate_actions
from .network import process_network_requests
from .serializers import MAGIKA_AVAILABLE, make_serializable, sanitize_filename
from .websockets import process_websocket_streams

logger = logging.getLogger(__name__)


def verify_timeline_files(session_dump_dir: str, timeline_events: list[dict]) -> bool:
    """Verifies that all files referenced in the timeline events exist on disk."""
    missing_files = []

    for event in timeline_events:
        if event.get("event_type") in ("network_request", "websocket_created", "websocket_closed") and "folder" in event:
            # Only check that the core transaction file we created exists
            transaction_path = os.path.join(session_dump_dir, event["folder"], "transaction.json")
            if not os.path.exists(transaction_path):
                missing_files.append(transaction_path)

        elif event.get("event_type") == "user_action" and event.get("ai_video_file"):
            # Only check that the video clip we created exists
            clip_path = os.path.join(session_dump_dir, event["ai_video_file"])
            if not os.path.exists(clip_path):
                missing_files.append(clip_path)

    if missing_files:
        events.log_warn.send("recorder", text=f"Timeline verification failed. Missing files: {missing_files}")
        return False
    return True


def compile_workspace(
    session_name: str | None,
    temp_data_dir: str,
    full_video_path: str,
    video_start_unix: float,
    on_skip_requested: callable = None,
    cancel_token=None,
    stop_token=None,
) -> tuple[str | None, bool]:
    events.log_info.send("recorder", text="[RULE] Compiling Workspace")
    events.log_info.send("recorder", text="Extracting data, and analyzing video...")

    try:
        # Load metadata
        metadata_path = os.path.join(temp_data_dir, "metadata.json")
        metadata = {}
        if os.path.exists(metadata_path):
            with open(metadata_path, encoding="utf-8") as f:
                metadata = json.load(f)

        # Load actions
        actions = []
        actions_path = os.path.join(temp_data_dir, "actions.jsonl")
        if os.path.exists(actions_path):
            with open(actions_path, encoding="utf-8") as f:
                for line in f:
                    actions.append(json.loads(line))

        requests_path = os.path.join(temp_data_dir, "requests.jsonl")
        total_requests = metadata.get("total_requests", 0)

        timeline_events = []

        # If we used a temporary name, let's figure out a fallback based on domains
        fallback_session_name = "recording"
        if not session_name:
            domain_counts = {}
            for action in actions:
                url = action.get("newUrl") or action.get("url")
                if url:
                    domain = urlparse(url).netloc
                    if domain:
                        domain_counts[domain] = domain_counts.get(domain, 0) + 1

            if domain_counts:
                most_common = max(domain_counts, key=domain_counts.get)
                fallback_session_name = sanitize_filename(most_common)

        # We will do all processing in the current OUTPUT_DIR (the tmp_dir)
        output_dir = str(config.OUTPUT_DIR)
        workspace_dir = str(config.WORKSPACE_DIR)
        session_dump_dir = os.path.join(workspace_dir, "session_dump")
        clips_dir = os.path.join(session_dump_dir, "clips")
        requests_dir = os.path.join(session_dump_dir, "requests")

        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(session_dump_dir, exist_ok=True)
        os.makedirs(clips_dir, exist_ok=True)
        os.makedirs(requests_dir, exist_ok=True)

        with open(os.path.join(output_dir, "session_metadata.json"), "w") as f:
            json.dump(make_serializable({"status": "in_progress", "original_metadata": metadata}), f, indent=2)

        if actions:
            actions = merge_and_annotate_actions(
                actions, full_video_path, video_start_unix, clips_dir, on_skip_requested, cancel_token, stop_token
            )
            for action in actions:
                timeline_events.append(
                    {
                        "timestamp": action.get("timestamp_unix", 0),
                        "timestamp_iso": action.get("timestamp_iso"),
                        "event_type": "user_action",
                        "action": action.get("type"),
                        "details": {
                            k: v
                            for k, v in action.items()
                            if k
                            not in [
                                "timestamp_unix",
                                "timestamp_iso",
                                "type",
                                "ai_macro_summary",
                                "ai_elements_interacted",
                                "ai_action_success",
                                "ai_video_file",
                                "video_start_sec",
                                "video_end_sec",
                            ]
                        },
                        "ai_macro_summary": action.get("ai_macro_summary"),
                        "ai_elements_interacted": action.get("ai_elements_interacted"),
                        "ai_action_success": action.get("ai_action_success"),
                        "ai_video_file": action.get("ai_video_file"),
                        "video_start_sec": action.get("video_start_sec"),
                        "video_end_sec": action.get("video_end_sec"),
                    }
                )

        detection_stats = {}
        network_stats = {"methods": {}, "domains": {}, "status_codes": {}, "with_auth": 0, "with_cookies": 0}
        if os.path.exists(requests_path):
            events.log_info.send(
                "recorder", text=f"Extracting {total_requests} network requests and building transactions..."
            )
            network_events, detection_stats, network_stats = process_network_requests(
                requests_path, temp_data_dir, requests_dir, session_dump_dir
            )
            timeline_events.extend(network_events)

        # Process WebSocket streams
        ws_connections_path = os.path.join(temp_data_dir, "ws_connections.jsonl")
        ws_frames_path = os.path.join(temp_data_dir, "ws_frames.jsonl")
        ws_stats = {}
        if os.path.exists(ws_connections_path):
            events.log_info.send("recorder", text="Extracting WebSocket connections and frames...")
            ws_output_dir = os.path.join(session_dump_dir, "websockets")
            os.makedirs(ws_output_dir, exist_ok=True)
            ws_timeline_events, ws_stats = process_websocket_streams(ws_connections_path, ws_frames_path, ws_output_dir)
            timeline_events.extend(ws_timeline_events)

        timeline_events.sort(key=lambda x: x["timestamp"])
        with open(os.path.join(session_dump_dir, "timeline.json"), "w") as f:
            json.dump(make_serializable(timeline_events), f, indent=2)

        session_flow = []
        seen_summaries = set()
        for action in actions:
            text = action.get("ai_macro_summary")
            if text and text not in seen_summaries:
                seen_summaries.add(text)
                session_flow.append(
                    {
                        "timestamp_iso": action.get("timestamp_iso"),
                        "timestamp_unix": action.get("timestamp_unix"),
                        "summary": text,
                    }
                )

        summary = {
            "session": metadata,
            "session_flow": session_flow,
            "statistics": {
                "total_requests": total_requests,
                "total_actions": len(actions),
                "methods": network_stats["methods"],
                "domains": network_stats["domains"],
                "status_codes": network_stats["status_codes"],
                "with_auth": network_stats["with_auth"],
                "with_cookies": network_stats["with_cookies"],
                "content_detection": detection_stats if MAGIKA_AVAILABLE else "Magika not available",
                "websockets": ws_stats if ws_stats else None,
            },
        }

        with open(os.path.join(session_dump_dir, "SUMMARY.json"), "w") as f:
            json.dump(make_serializable(summary), f, indent=2)

        # Move the video file into the output directory before verifying
        final_video_path = os.path.join(session_dump_dir, "full_record.mp4")
        if os.path.exists(full_video_path):
            shutil.move(full_video_path, final_video_path)

        # Verify files referenced in timeline exist
        files_verified = verify_timeline_files(session_dump_dir, timeline_events)

        # Update and finalize metadata
        with open(os.path.join(output_dir, "session_metadata.json"), "w") as f:
            final_meta = {"status": "completed", "files_verified": files_verified, "original_metadata": metadata}
            json.dump(make_serializable(final_meta), f, indent=2)

        # Determine final session name and rename output directory if needed
        final_output_dir = output_dir
        if not session_name:
            analyzer_for_name = VideoActionAnalyzer()
            ai_session_name = analyzer_for_name.generate_session_name(session_flow, fallback_session_name)

            base_output_dir = os.path.join(os.getcwd(), ai_session_name)
            final_output_dir = base_output_dir
            idx = 1
            while os.path.exists(final_output_dir):
                final_output_dir = f"{base_output_dir}_{idx:02d}"
                idx += 1

            shutil.move(output_dir, final_output_dir)

            # Update config globally so everything works smoothly later
            from pathlib import Path

            # Import the config module dynamically using standard relative import to modify global state
            from ... import config as global_config

            global_config.OUTPUT_DIR = Path(final_output_dir)
            global_config.WORKSPACE_DIR = global_config.OUTPUT_DIR / "workspace"
            global_config.BLOCKLIST_DIR = global_config.OUTPUT_DIR / "blocklist"
            global_config.BLOCKLIST_DB = global_config.OUTPUT_DIR / "blocklist.db"

            # Update the returned video path to reflect the new directory
            final_video_path = os.path.join(final_output_dir, "workspace", "session_dump", "full_record.mp4")

        # Cleanup temp_data_dir
        try:
            shutil.rmtree(temp_data_dir)
        except Exception as e:
            events.log_warn.send("recorder", text=f"Could not clean up temporary data directory {temp_data_dir}: {e}")

        # --- Crash report handling ---
        if metadata.get("session_crashed"):
            crash_timestamp = metadata.get("crash_timestamp", "unknown")
            crash_error = metadata.get("crash_error", "unknown")

            from rich.panel import Panel

            from ....cli.console import console, save_crash_report

            save_crash_report(crash_timestamp=crash_timestamp, crash_error=crash_error)

            console.print(
                Panel(
                    f"[bold yellow]A crash occurred during recording at[/bold yellow] [bold]{crash_timestamp}[/bold]\n\n"
                    "[green]The recording was still saved.[/green] "
                    "[dim]A few actions and requests may have been lost due to the abrupt termination.[/dim]\n\n"
                    "[bold red]See automatiq_crash_report.log for details.[/bold red]",
                    title="[bold red]CRASH DETECTED[/bold red]",
                    border_style="red",
                    padding=(1, 2),
                )
            )

        events.log_info.send("recorder", text=f"[SUCCESS] Workspace compiled successfully at {final_output_dir}")

        try:
            from ....cli.console import rename_file_logger

            rename_file_logger(os.path.basename(final_output_dir))
        except Exception as e:
            events.log_warn.send("recorder", text=f"Could not rename log file to match session: {e}")

        return final_video_path, True

    except StopRequestedException as e:
        events.log_error.send("recorder", text=str(e))
        return None, False
    except Exception as e:
        events.log_error.send("recorder", text=f"Workspace compilation failed: {e}")
        events.log_traceback.send("recorder")
        return None, False
