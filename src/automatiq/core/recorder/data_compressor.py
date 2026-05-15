import base64
import json
import logging
import os
import re
import shutil
import traceback
from urllib.parse import urlparse

from .. import config, events
from ..cancel_standard import StopRequestedException
from .ai_analyzer import VideoActionAnalyzer
from .video_recorder import ActionVideoRecorder

logger = logging.getLogger(__name__)

try:
    from magika import Magika

    magika_detector = Magika()
    MAGIKA_AVAILABLE = True
    events.log_info.send("recorder", text="Magika AI detector initialized successfully.")
except ImportError:
    magika_detector = None
    MAGIKA_AVAILABLE = False
    events.log_warn.send("recorder", text="Magika not installed. Skipping advanced content type detection.")

WORKSPACE_DIR = str(config.WORKSPACE_DIR)


def sanitize_filename(name: str) -> str:
    name = name.replace("https://", "").replace("http://", "")
    return re.sub(r"[^\w\-\.]", "_", name)[:100]


def make_serializable(obj):
    if isinstance(obj, str | int | float | bool | type(None)):
        return obj
    if isinstance(obj, bytes):
        return ""
    if isinstance(obj, list):
        return [make_serializable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: make_serializable(v) for k, v in obj.items()}
    return str(obj)


def get_header_val(headers, key):
    if not headers:
        return None
    key = key.lower()
    for k, v in headers.items():
        if k.lower() == key:
            return v
    return None


def extract_cookies_sent(item):
    names = set()
    details = item.get("cookies_sent_details", [])
    for ac in details:
        if ac.get("blockedReasons"):
            continue
        cookie = ac.get("cookie") or {}
        name = cookie.get("name")
        if name:
            names.add(name)
    if not names:
        raw = get_header_val(item.get("headers", {}), "cookie")
        if raw:
            for part in raw.split(";"):
                if "=" in part:
                    names.add(part.split("=")[0].strip())
    return sorted(names)


def extract_cookies_set(item):
    names = set()
    resp = item.get("response_data") or {}
    headers = resp.get("headers") or {}
    raw = get_header_val(headers, "set-cookie")
    if raw:
        for line in raw.split("\n"):
            if "=" in line:
                names.add(line.split("=")[0].strip())
    return sorted(list(names))


def detect_content_type(content, is_base64=False):
    if content is None or not MAGIKA_AVAILABLE or magika_detector is None:
        return None
    try:
        if is_base64:
            byte_content = base64.b64decode(content)
        elif isinstance(content, str):
            byte_content = content.encode("utf-8")
        elif isinstance(content, bytes):
            byte_content = content
        else:
            return None
        result = magika_detector.identify_bytes(byte_content)
        return {
            "label": result.output.label,
            "mime_type": result.output.mime_type,
            "extension": result.output.extensions[0] if result.output.extensions else "bin",
            "all_extensions": result.output.extensions,
            "description": result.output.description,
            "confidence": result.score,
            "is_text": result.output.is_text,
            "group": result.output.group,
        }
    except Exception as e:
        events.log_warn.send("recorder", text=f"Magika error: {e}")
        events.log_traceback.send("recorder")
        return {"label": "unknown", "mime_type": "application/octet-stream", "extension": "bin", "error": str(e)}


def save_content(path, content, is_base64=False):
    if content is None:
        return
    mode = "wb"
    if is_base64:
        try:
            data = base64.b64decode(content)
        except Exception as exc:
            events.log_warn.send("recorder", text=f"Base64 decode failed for {path}, saving raw content instead: {exc}")
            events.log_traceback.send("recorder")
            data = str(content).encode("utf-8")
    elif isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = str(content).encode("utf-8")
    try:
        with open(path, mode) as f:
            f.write(data)
    except OSError as exc:
        events.log_error.send("recorder", text=f"Failed to write content to {path}: {exc}")
        events.log_traceback.send("recorder")


def merge_and_annotate_actions(
    actions: list[dict],
    full_video_path: str,
    video_start_unix: float,
    clips_dir: str,
    on_skip_requested: callable = None,
    cancel_token=None,
    stop_token=None,
) -> list[dict]:
    if not actions or not video_start_unix or not os.path.exists(full_video_path):
        return actions

    actions.sort(key=lambda x: x.get("timestamp_unix", 0))
    merged_clips = []
    current_cluster = []

    for action in actions:
        if not current_cluster:
            current_cluster.append(action)
        else:
            last_action_time = current_cluster[-1].get("timestamp_unix", 0)
            current_action_time = action.get("timestamp_unix", 0)

            if (current_action_time - last_action_time) <= config.MERGE_GAP_THRESHOLD_SECONDS:
                current_cluster.append(action)
            else:
                merged_clips.append(current_cluster)
                current_cluster = [action]

    if current_cluster:
        merged_clips.append(current_cluster)

    recorder = ActionVideoRecorder(fps=config.FPS)
    ai_analyzer = VideoActionAnalyzer()

    # Import CancelToken standard and cancellable runner from the parent package.
    from ..cancel_standard import CancelRequestedException, run_cancellable

    events.log_info.send("recorder", text=f"Extracting {len(merged_clips)} video action segments for AI...")
    for idx, cluster in enumerate(merged_clips):
        if stop_token and stop_token.is_stopped():
            events.log_error.send("recorder", text="Compilation completely aborted by user (Ctrl+C).")
            raise StopRequestedException("Compilation completely aborted by user.")

        if cancel_token and cancel_token.is_cancelled():
            remaining = len(merged_clips) - idx
            if on_skip_requested and on_skip_requested(remaining):
                events.log_warn.send("recorder", text=f"Skipping AI analysis for remaining {remaining} segment(s).")
                break
            events.log_info.send("recorder", text="Continuing AI analysis...")

        first_action_time_relative = cluster[0]["timestamp_unix"] - video_start_unix
        clip_start = max(0, first_action_time_relative - config.SEGMENT_PAD_SECONDS)

        last_action_time_relative = cluster[-1]["timestamp_unix"] - video_start_unix
        clip_end = last_action_time_relative + config.SEGMENT_PAD_SECONDS

        clip_filename = f"action_clip_{idx:03d}.mp4"
        clip_path = os.path.join(clips_dir, clip_filename)

        clip_ok = recorder.split_video(full_video_path, clip_path, clip_start, clip_end)

        if clip_ok:
            try:
                ai_description = run_cancellable(
                    cancel_token,
                    ai_analyzer.analyze_clip,
                    clip_path,
                    clip_end - clip_start,
                    raw_actions=cluster,
                )
            except CancelRequestedException:
                remaining = len(merged_clips) - idx
                if on_skip_requested and on_skip_requested(remaining):
                    events.log_warn.send("recorder", text=f"Skipping AI analysis for remaining {remaining} segment(s).")
                    break
                events.log_info.send("recorder", text="Continuing AI analysis...")
                continue
            events.log_info.send(
                "recorder", text=f"[AI] Segment {idx:03d} summary: {ai_description.get('macro_summary')}"
            )

            for action in cluster:
                action["ai_macro_summary"] = ai_description.get("macro_summary")
                action["ai_elements_interacted"] = ai_description.get("elements_interacted", [])
                action["ai_action_success"] = ai_description.get("action_success")
                action["ai_video_file"] = f"clips/{clip_filename}"
                action["video_start_sec"] = round(clip_start, 2)
                action["video_end_sec"] = round(clip_end, 2)
        else:
            events.log_warn.send(
                "recorder",
                text=f"Video split failed for segment {idx:03d} ({clip_start:.1f}s-{clip_end:.1f}s) "
                f"— skipping AI annotation for {len(cluster)} action(s)",
            )

    return actions


def process_network_requests(requests: list[dict], requests_dir: str, output_dir: str) -> tuple[list[dict], dict]:
    timeline_requests = []
    detection_stats = {"request_detected": 0, "response_detected": 0, "mismatches": 0}

    for idx, item in enumerate(requests):
        try:
            parsed_url = urlparse(item.get("url", ""))
            domain = parsed_url.netloc or "unknown"
            folder_name = f"{idx:03d}_{item.get('method', 'UNK')}_{sanitize_filename(domain)}"
            req_root = os.path.join(requests_dir, folder_name)
            os.makedirs(req_root, exist_ok=True)

            req_headers = item.get("headers", {})
            res_data = item.get("response_data") or {}
            res_headers = res_data.get("headers") or {}

            request_detection = None
            if item.get("post_data"):
                request_detection = detect_content_type(item["post_data"])
                if request_detection:
                    detection_stats["request_detected"] += 1

            response_detection = None
            if res_data and res_data.get("body"):
                response_detection = detect_content_type(res_data["body"], res_data.get("base64_encoded", False))
                if response_detection:
                    detection_stats["response_detected"] += 1

            declared_mime = res_data.get("mime_type", "unknown")
            detected_mime = response_detection.get("mime_type", "unknown") if response_detection else "unknown"
            if declared_mime != detected_mime and declared_mime != "unknown":
                detection_stats["mismatches"] += 1

            transaction_data = {
                "metadata": {
                    "index": idx,
                    "unique_id": item.get("unique_id"),
                    "method": item.get("method"),
                    "url": item.get("url"),
                    "status": res_data.get("status"),
                    "timing": {
                        "request_sent_unix": item.get("timestamp_unix"),
                        "response_received_unix": item.get("response_timing", {}).get("received_unix"),
                        "loading_finished_unix": item.get("response_timing", {}).get("finished_unix"),
                        "duration_ms": item.get("response_timing", {}).get("total_duration_ms"),
                    },
                    "security": {
                        "has_authorization": bool(get_header_val(req_headers, "authorization")),
                        "has_proxy_authorization": bool(get_header_val(req_headers, "proxy-authorization")),
                        "has_challenge": bool(get_header_val(res_headers, "www-authenticate")),
                    },
                },
                "request": {
                    "headers": req_headers,
                    "cookies_sent": extract_cookies_sent(item),
                    "cookies_sent_detailed": item.get("cookies_sent_details", []),
                    "content_detection": request_detection,
                    "has_payload": bool(item.get("post_data")),
                },
                "response": {
                    "headers": res_headers,
                    "cookies_set": extract_cookies_set(item),
                    "cookies_set_detailed": item.get("cookies_received_details", {}),
                    "content_detection": response_detection,
                    "has_body": bool(res_data.get("body")),
                    "mime_mismatch": declared_mime != detected_mime
                    if (response_detection and declared_mime != "unknown")
                    else False,
                },
            }

            with open(os.path.join(req_root, "transaction.json"), "w") as f:
                json.dump(make_serializable(transaction_data), f, indent=2)

            if item.get("post_data"):
                ext = request_detection.get("extension", "bin") if request_detection else "bin"
                save_content(os.path.join(req_root, f"req_payload.{ext}"), item["post_data"])

            if res_data and res_data.get("body"):
                ext = response_detection.get("extension", "bin") if response_detection else "bin"
                save_content(
                    os.path.join(req_root, f"res_body.{ext}"), res_data["body"], res_data.get("base64_encoded", False)
                )
                res_data.pop("body", None)

            timeline_requests.append(
                {
                    "timestamp": item.get("timestamp_unix", 0),
                    "timestamp_iso": item.get("timestamp_iso"),
                    "event_type": "network_request",
                    "method": item.get("method"),
                    "url": item.get("url"),
                    "status": res_data.get("status", -1),
                    "folder": f"requests/{folder_name}",
                }
            )

        except Exception as e:
            events.log_error.send("recorder", text=f"Failed to process request at index {idx}: {e}")
            events.log_traceback.send("recorder")
            error_filename = os.path.join(output_dir, f"CRASH_REPORT_{idx:03d}.txt")
            try:
                with open(error_filename, "w", encoding="utf-8") as debug_f:
                    debug_f.write(f"ERROR: {str(e)}\n" + "-" * 50 + "\n")
                    debug_f.write(traceback.format_exc() + "\n" + "-" * 50 + "\n")
            except OSError as write_exc:
                events.log_warn.send("recorder", text=f"Could not write crash report to {error_filename}: {write_exc}")
                events.log_traceback.send("recorder")
            continue

    return timeline_requests, detection_stats


def verify_timeline_files(session_dump_dir: str, timeline_events: list[dict]) -> bool:
    """Verifies that all files referenced in the timeline events exist on disk."""
    missing_files = []

    for event in timeline_events:
        if event.get("event_type") == "network_request" and "folder" in event:
            # Only check that the core transaction file we created exists
            transaction_path = os.path.join(session_dump_dir, event["folder"], "transaction.json")
            if not os.path.exists(transaction_path):
                missing_files.append(transaction_path)

        elif event.get("event_type") == "user_action" and "ai_video_file" in event:
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
    session_data: dict,
    full_video_path: str,
    video_start_unix: float,
    on_skip_requested: callable = None,
    cancel_token=None,
    stop_token=None,
) -> tuple[str | None, bool]:
    events.log_info.send("recorder", text="[RULE] Compiling Workspace")
    events.log_info.send("recorder", text="Extracting data, and analyzing video...")

    try:
        metadata = session_data.get("metadata", {})
        requests = session_data.get("requests", [])
        actions = session_data.get("actions", [])
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
        if requests:
            events.log_info.send(
                "recorder", text=f"Extracting {len(requests)} network requests and building transactions..."
            )
            network_events, detection_stats = process_network_requests(requests, requests_dir, session_dump_dir)
            timeline_events.extend(network_events)

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
                "total_requests": len(requests),
                "total_actions": len(actions),
                "methods": {},
                "domains": {},
                "status_codes": {},
                "with_auth": 0,
                "with_cookies": 0,
                "content_detection": detection_stats if MAGIKA_AVAILABLE else "Magika not available",
            },
        }

        for req in requests:
            method = req.get("method", "UNKNOWN")
            summary["statistics"]["methods"][method] = summary["statistics"]["methods"].get(method, 0) + 1
            domain = urlparse(req.get("url", "")).netloc
            if domain:
                summary["statistics"]["domains"][domain] = summary["statistics"]["domains"].get(domain, 0) + 1

            status = req.get("response_data", {}).get("status") if req.get("response_data") else None
            if status:
                summary["statistics"]["status_codes"][str(status)] = (
                    summary["statistics"]["status_codes"].get(str(status), 0) + 1
                )
            if get_header_val(req.get("headers", {}), "authorization"):
                summary["statistics"]["with_auth"] += 1
            if req.get("cookies_sent_details"):
                summary["statistics"]["with_cookies"] += 1

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

            config.OUTPUT_DIR = Path(final_output_dir)
            config.WORKSPACE_DIR = config.OUTPUT_DIR / "workspace"
            config.BLOCKLIST_DIR = config.OUTPUT_DIR / "blocklist"
            config.BLOCKLIST_DB = config.OUTPUT_DIR / "blocklist.db"

            # Update the returned video path to reflect the new directory
            final_video_path = os.path.join(final_output_dir, "workspace", "session_dump", "full_record.mp4")

        events.log_info.send("recorder", text=f"[SUCCESS] Workspace compiled successfully at {final_output_dir}")
        return final_video_path, True

    except StopRequestedException as e:
        events.log_error.send("recorder", text=str(e))
        return None, False
    except Exception as e:
        events.log_error.send("recorder", text=f"Workspace compilation failed: {e}")
        events.log_traceback.send("recorder")
        return None, False
