import asyncio
import base64
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime

import zendriver as zd
from zendriver import cdp

from .. import events
from .blocklist_db import BlocklistDB

logger = logging.getLogger(__name__)


class TimestampConverter:
    """Converts CDP MonotonicTime to human-readable ISO 8601 timestamps."""

    def __init__(self):
        self.monotonic_to_wall_offset: float | None = None
        self.offsets_collected = []

    def calibrate(self, monotonic_time: float, wall_time: float) -> None:
        if len(self.offsets_collected) >= 5:
            return
        offset = wall_time - monotonic_time
        self.offsets_collected.append(offset)
        self.monotonic_to_wall_offset = sum(self.offsets_collected) / len(self.offsets_collected)

    def to_unix_timestamp(self, monotonic_time: float) -> float:
        if self.monotonic_to_wall_offset is None:
            self.monotonic_to_wall_offset = time.time() - monotonic_time
        return monotonic_time + self.monotonic_to_wall_offset

    def to_iso8601(self, monotonic_time: float) -> str:
        unix_timestamp = self.to_unix_timestamp(monotonic_time)
        dt = datetime.fromtimestamp(unix_timestamp, tz=UTC)
        return dt.isoformat(timespec="milliseconds")

    def current_iso8601(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")


class BrowserAgent:
    """Manages the headless/UI browser session, CDP event handlers, and data collection."""

    def __init__(self, telemetry_js_path=None, visuals_js_path=None, blocklist: BlocklistDB | None = None):
        _js_dir = os.path.join(os.path.dirname(__file__), "js")
        self.telemetry_js_path = telemetry_js_path or os.path.join(_js_dir, "telemetry.js")
        self.visuals_js_path = visuals_js_path or os.path.join(_js_dir, "visuals.js")
        self.blocklist = blocklist
        self._profile_dir = tempfile.TemporaryDirectory(prefix="automatiq_chrome_")

        # New Disk-Streaming Setup
        self._data_dir = tempfile.TemporaryDirectory(prefix="automatiq_stream_")
        self._bodies_dir = os.path.join(self._data_dir.name, "bodies")
        os.makedirs(self._bodies_dir, exist_ok=True)
        self._actions_file = open(os.path.join(self._data_dir.name, "actions.jsonl"), "a", encoding="utf-8")
        self._requests_file = open(os.path.join(self._data_dir.name, "requests.jsonl"), "a", encoding="utf-8")
        self._ws_connections_file = open(
            os.path.join(self._data_dir.name, "ws_connections.jsonl"), "a", encoding="utf-8"
        )
        self._ws_frames_file = open(os.path.join(self._data_dir.name, "ws_frames.jsonl"), "a", encoding="utf-8")
        self._actions_count = 0

        self.browser = None
        self.tab = None
        self.recording_start = None

        # Crash tracking state
        self.session_crashed = False
        self.crash_timestamp = None
        self.crash_error = None

        self.ts_converter = TimestampConverter()

        self.active_map = {}
        self.orphan_extra_info = {}

        # WebSocket tracking state
        self.active_websockets = {}  # str(request_id) -> {"start_time", "sequence", "url"}

        # FIX: Central Tab Registry
        self.tabs = {}  # session_id -> {"tab": tab_session, "type": "page"|"iframe"}

        self.stats = {
            "total_requests": 0,
            "completed": 0,
            "failed": 0,
            "incomplete": 0,
            "body_success": 0,
            "body_failed": 0,
            "body_skip_no_content": 0,
            "body_skip_redirect": 0,
            "body_skip_cached": 0,
            "body_from_stream": 0,
            "blocked_by_blocklist": 0,
            "ws_connections": 0,
            "ws_frames_sent": 0,
            "ws_frames_received": 0,
            "ws_frames_skipped": 0,
            "ws_blocked_by_blocklist": 0,
        }

        self.telemetry_script = ""
        self.visuals_script = ""

    def _load_scripts(self) -> bool:
        try:
            with open(self.telemetry_js_path, encoding="utf-8") as f:
                self.telemetry_script = f.read()
            with open(self.visuals_js_path, encoding="utf-8") as f:
                self.visuals_script = f.read()
            return True
        except FileNotFoundError as e:
            events.log_error.send("recorder", text=f"Missing JS dependencies: {e}")
            return False

    @staticmethod
    def merge_headers(req, extra_headers):
        if not extra_headers:
            return
        if not req["response_data"]:
            req["response_data"] = {
                "status": 0,
                "headers": {},
                "body": None,
                "base64_encoded": False,
                "charset": "utf-8",
            }
        current = req["response_data"]["headers"]
        for k, v in extra_headers.items():
            current[k] = v

    # -------------------------------------------------------------------------
    # Handlers explicitly bound to session_id
    # -------------------------------------------------------------------------

    async def binding_handler_for_tab(self, event: cdp.runtime.BindingCalled, session_id: str):
        if event.name == "sendActionToPython":
            try:
                payload = json.loads(event.payload)
                action_type = payload.get("type")
                is_iframe = payload.get("is_iframe", False)

                payload["timestamp_iso"] = self.ts_converter.current_iso8601()
                payload["timestamp_unix"] = time.time()
                payload["execution_context_id"] = event.execution_context_id
                payload["_session_id"] = session_id

                # We drop script_loaded logs to reduce console spam, but we DO NOT drop
                # iframe actions like 'click' or 'keypress'. We keep them.
                if action_type == "script_loaded":
                    # Only log the main tab init once, or optionally drop it entirely.
                    if not is_iframe:
                        events.log_info.send(
                            "recorder", text="[ACTION] script_loaded: Telemetry script initialized (Main Tab)"
                        )
                    return

                # Stream to disk instead of memory
                self._actions_file.write(json.dumps(payload) + "\n")
                self._actions_file.flush()
                self._actions_count += 1

                tag = " (IFRAME)" if is_iframe else ""

                if action_type == "keypress":
                    events.log_info.send("recorder", text=f"[ACTION] keypress{tag}: {payload.get('key')}")
                elif action_type == "click":
                    events.log_info.send("recorder", text=f"[ACTION] click{tag}: {payload.get('text', '')[:50]}")
                else:
                    fallback_val = payload.get("value", payload.get("newUrl", payload.get("text", "")))
                    events.log_info.send("recorder", text=f"[ACTION] {action_type}{tag}: {fallback_val[:50]}")
            except Exception as e:
                events.log_error.send("recorder", text=f"Binding handler failed: {e}")
                events.log_traceback.send("recorder")

    async def request_handler_for_tab(self, event: cdp.network.RequestWillBeSent, session_id: str):
        if event.wall_time:
            self.ts_converter.calibrate(event.timestamp, event.wall_time)

        if event.type_ in (
            cdp.network.ResourceType.DOCUMENT,
            cdp.network.ResourceType.XHR,
            cdp.network.ResourceType.FETCH,
            cdp.network.ResourceType.SCRIPT,
            cdp.network.ResourceType.WEB_SOCKET,
        ):
            self.stats["total_requests"] += 1

        if event.request_id in self.active_map:
            old_req = self.active_map[event.request_id]
            if event.redirect_response and not old_req["response_data"]:
                rd = event.redirect_response.to_json()
                old_req["response_data"] = {"status": rd["status"], "headers": rd.get("headers", {}), "body": None}
            old_req["request_state"] = "redirected"
            old_req.pop("_meta", None)

        unique_id = f"{event.request_id}_{uuid.uuid4().hex[:8]}"

        request_obj = {
            "unique_id": unique_id,
            "request_id": event.request_id,
            "timestamp_iso": self.ts_converter.to_iso8601(event.timestamp),
            "timestamp_unix": self.ts_converter.to_unix_timestamp(event.timestamp),
            "timestamp_monotonic": event.timestamp,
            "url": event.request.url,
            "method": event.request.method,
            "resource_type": str(event.type_),
            "headers": dict(event.request.headers),
            "post_data": event.request.post_data,
            "cookies_sent_details": [],
            "cookies_received_details": {},
            "response_data": None,
            "response_timing": {},
            "request_state": "pending",
            "body_fetch_error": None,
            "_session_id": session_id,
        }

        if event.request_id in self.orphan_extra_info:
            data = self.orphan_extra_info.pop(event.request_id)
            if "sent" in data:
                request_obj["cookies_sent_details"] = data["sent"]
            if "received" in data:
                request_obj["cookies_received_details"] = data["received"]
            if "raw_headers" in data:
                self.merge_headers(request_obj, data["raw_headers"])

        if event.request.url.startswith("data:"):
            return

        if self.blocklist and self.blocklist.is_blocked_url(event.request.url):
            self.stats["blocked_by_blocklist"] += 1
            return

        self.active_map[event.request_id] = request_obj

    async def data_received_handler_for_tab(self, event: cdp.network.DataReceived, session_id: str):
        rid = str(event.request_id)
        # We write raw chunks directly to the body file
        if event.data:
            try:
                body_path = os.path.join(self._bodies_dir, f"{rid}.bin")
                with open(body_path, "ab") as f:
                    f.write(base64.b64decode(event.data))
            except Exception as exc:
                events.log_error.send("recorder", text=f"Failed to decode/write chunk {rid}: {exc}")
                events.log_traceback.send("recorder")

    async def response_handler_for_tab(self, event: cdp.network.ResponseReceived, session_id: str):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "received"
            req["response_timing"]["received_iso"] = self.ts_converter.to_iso8601(event.timestamp)
            req["response_timing"]["received_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

            if "timestamp_unix" in req:
                req["response_timing"]["duration_ms"] = round(
                    (req["response_timing"]["received_unix"] - req["timestamp_unix"]) * 1000, 2
                )

            resp = event.response.to_json()
            if not req["response_data"]:
                req["response_data"] = {
                    "status": resp["status"],
                    "headers": {},
                    "body": None,
                    "base64_encoded": False,
                    "charset": resp.get("charset", "utf-8") or "utf-8",
                    "mime_type": resp.get("mimeType", "unknown"),
                    "from_disk_cache": resp.get("fromDiskCache", False),
                    "from_service_worker": resp.get("fromServiceWorker", False),
                    "from_prefetch_cache": resp.get("fromPrefetchCache", False),
                }
            else:
                req["response_data"]["status"] = resp["status"]
                req["response_data"]["mime_type"] = resp.get("mimeType", "unknown")
            self.merge_headers(req, resp.get("headers", {}))

            rid = str(event.request_id)
            body_path = os.path.join(self._bodies_dir, f"{rid}.bin")
            # Ensure an empty file exists so we know we tracked it
            if not os.path.exists(body_path):
                open(body_path, "a").close()

            tab_session = self.tabs.get(session_id, {}).get("tab")
            if tab_session:
                try:
                    buffered = await tab_session.send(cdp.network.stream_resource_content(request_id=event.request_id))
                    if buffered:
                        with open(body_path, "ab") as f:
                            f.write(base64.b64decode(buffered))
                except Exception as e:
                    error_str = str(e)
                    if "already finished loading" not in error_str and "does not exist" not in error_str:
                        events.log_warn.send("recorder", text=f"Stream resource failed: {e}")

    async def loading_finished_handler_for_tab(self, event: cdp.network.LoadingFinished, session_id: str):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "finished"
            self.stats["completed"] += 1
            req["response_timing"]["finished_iso"] = self.ts_converter.to_iso8601(event.timestamp)
            req["response_timing"]["finished_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

            status = req["response_data"].get("status", 0) if req["response_data"] else 0
            if 300 <= status < 400:
                self.stats["body_skip_redirect"] += 1
                req["body_fetch_error"] = f"Redirect status {status}"
            elif status in (204, 205, 304):
                self.stats["body_skip_no_content"] += 1
                req["body_fetch_error"] = f"No content status {status}"
            else:
                body_captured = False
                tab_session = self.tabs.get(session_id, {}).get("tab")
                rid = str(event.request_id)
                body_path = os.path.join(self._bodies_dir, f"{rid}.bin")

                if tab_session:
                    try:
                        result = await tab_session.send(cdp.network.get_response_body(request_id=event.request_id))
                        raw_bytes = None
                        if isinstance(result, tuple):
                            body_str, is_base64 = result
                        else:
                            body_str = result.body
                            is_base64 = result.base64_encoded

                        if is_base64:
                            raw_bytes = base64.b64decode(body_str)
                        else:
                            raw_bytes = body_str.encode("utf-8")

                        with open(body_path, "wb") as f:
                            f.write(raw_bytes)

                        req["response_data"]["body_file"] = f"bodies/{rid}.bin"
                        self.stats["body_success"] += 1
                        body_captured = True
                    except Exception as e:
                        req["body_fetch_error"] = str(e)
                        # We won't log traceback for this one as get_response_body
                        # often fails for valid reasons (cache eviction)

                if not body_captured and os.path.exists(body_path) and os.path.getsize(body_path) > 0:
                    req["response_data"]["body_file"] = f"bodies/{rid}.bin"
                    req["body_fetch_error"] = None
                    self.stats["body_from_stream"] += 1
                    body_captured = True

                if not body_captured:
                    self.stats["body_failed"] += 1

            # Stream request to disk and remove from memory
            self._requests_file.write(json.dumps(req) + "\n")
            self._requests_file.flush()
            self.active_map.pop(event.request_id, None)

    async def loading_failed_handler_for_tab(self, event: cdp.network.LoadingFailed, session_id: str):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "failed"
            req["error_text"] = event.error_text
            self.stats["failed"] += 1

            # Stream request to disk and remove from memory
            self._requests_file.write(json.dumps(req) + "\n")
            self._requests_file.flush()
            self.active_map.pop(event.request_id, None)

    async def req_extra_info_for_tab(self, event: cdp.network.RequestWillBeSentExtraInfo, session_id: str):
        cookies = [ac.to_json() for ac in event.associated_cookies]
        if event.request_id in self.active_map:
            self.active_map[event.request_id]["cookies_sent_details"] = cookies
        else:
            if event.request_id not in self.orphan_extra_info:
                self.orphan_extra_info[event.request_id] = {}
            self.orphan_extra_info[event.request_id]["sent"] = cookies

    async def res_extra_info_for_tab(self, event: cdp.network.ResponseReceivedExtraInfo, session_id: str):
        cookie_data = {
            "blocked": [c.to_json() for c in event.blocked_cookies],
            "exempted": [c.to_json() for c in (event.exempted_cookies or [])],
        }
        headers = dict(event.headers)
        if event.request_id in self.active_map:
            self.active_map[event.request_id]["cookies_received_details"] = cookie_data
            self.merge_headers(self.active_map[event.request_id], headers)
        else:
            if event.request_id not in self.orphan_extra_info:
                self.orphan_extra_info[event.request_id] = {}
            self.orphan_extra_info[event.request_id]["received"] = cookie_data
            self.orphan_extra_info[event.request_id]["raw_headers"] = headers

    # -------------------------------------------------------------------------
    # WebSocket Handlers
    #
    # Each lifecycle event is streamed separately to ws_connections.jsonl.
    # process_websocket_streams in data_compressor.py merges them at compile
    # time. This avoids race conditions where WebSocketCreated fires before
    # the handshake events have populated a shared stash.
    #
    # CDP event ordering:
    #   WebSocketCreated
    #   -> WebSocketWillSendHandshakeRequest
    #   -> WebSocketHandshakeResponseReceived
    #   -> WebSocketFrameSent / WebSocketFrameReceived
    #   -> WebSocketClosed
    # -------------------------------------------------------------------------

    async def websocket_created_handler_for_tab(self, event: cdp.network.WebSocketCreated, session_id: str):
        try:
            rid = str(event.request_id)
            url = event.url

            if self.blocklist and self.blocklist.is_blocked_url(url):
                self.stats["ws_blocked_by_blocklist"] += 1
                return

            # Set active_websockets BEFORE file I/O so frames are captured even if write fails.
            # start_time will be updated by the handshake_request handler; if that event is
            # missed, the first frame's timestamp becomes the baseline (delta = 0ms).
            self.active_websockets[rid] = {
                "start_time": None,
                "sequence": 1,
                "url": url,
            }
            self.stats["ws_connections"] += 1

            record = {
                "event": "created",
                "request_id": rid,
                "url": url,
                "created_iso": self.ts_converter.current_iso8601(),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketCreated handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_handshake_request_handler_for_tab(
        self, event: cdp.network.WebSocketWillSendHandshakeRequest, session_id: str
    ):
        try:
            if event.wall_time:
                self.ts_converter.calibrate(event.timestamp, event.wall_time)

            rid = str(event.request_id)

            # Fallback: if WebSocketCreated was missed (Network.enable not retroactive),
            # create the active_websockets stub here so frames are still captured.
            if rid not in self.active_websockets:
                self.active_websockets[rid] = {
                    "start_time": event.timestamp,
                    "sequence": 1,
                    "url": "",
                }
                self.stats["ws_connections"] += 1
            else:
                # Update start_time on the active connection so frame deltas are correct
                self.active_websockets[rid]["start_time"] = event.timestamp

            record = {
                "event": "handshake_request",
                "request_id": rid,
                "request_headers": dict(event.request.headers),
                "start_time": event.timestamp,
                "created_iso": self.ts_converter.to_iso8601(event.timestamp),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketWillSendHandshakeRequest handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_handshake_response_handler_for_tab(
        self, event: cdp.network.WebSocketHandshakeResponseReceived, session_id: str
    ):
        try:
            rid = str(event.request_id)
            resp = event.response

            # Fallback: if both WebSocketCreated and handshake_request were missed,
            # create the active_websockets stub here so frames are still captured.
            if rid not in self.active_websockets:
                self.active_websockets[rid] = {
                    "start_time": event.timestamp,
                    "sequence": 1,
                    "url": "",
                }
                self.stats["ws_connections"] += 1

            record = {
                "event": "handshake_response",
                "request_id": rid,
                "response_headers": dict(resp.headers),
                "response_status": resp.status,
                "created_iso": self.ts_converter.to_iso8601(event.timestamp),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketHandshakeResponseReceived handler failed: {e}")
            events.log_traceback.send("recorder")

    async def _process_ws_frame(self, request_id, direction: str, timestamp: float, opcode: int, payload_data: str):
        rid = str(request_id)
        if rid not in self.active_websockets:
            self.stats["ws_frames_skipped"] += 1
            return

        ws_state = self.active_websockets[rid]

        # Fallback: if start_time was never set (missed handshake), use first frame as baseline
        if ws_state["start_time"] is None:
            ws_state["start_time"] = timestamp

        delta_ms = int((timestamp - ws_state["start_time"]) * 1000)
        seq = ws_state["sequence"]
        is_base64 = opcode == 2

        frame_record = {
            "request_id": rid,
            "seq": seq,
            "direction": direction,
            "delta_ms": delta_ms,
            "opcode": opcode,
            "payload": payload_data,
            "is_base64": is_base64,
        }
        self._ws_frames_file.write(json.dumps(frame_record) + "\n")
        self._ws_frames_file.flush()

        ws_state["sequence"] += 1

        if direction == "client":
            self.stats["ws_frames_sent"] += 1
        else:
            self.stats["ws_frames_received"] += 1

    async def websocket_frame_sent_handler_for_tab(self, event: cdp.network.WebSocketFrameSent, session_id: str):
        try:
            await self._process_ws_frame(
                event.request_id, "client", event.timestamp, event.response.opcode, event.response.payload_data
            )
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketFrameSent handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_frame_received_handler_for_tab(self, event: cdp.network.WebSocketFrameReceived, session_id: str):
        try:
            await self._process_ws_frame(
                event.request_id, "server", event.timestamp, event.response.opcode, event.response.payload_data
            )
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketFrameReceived handler failed: {e}")
            events.log_traceback.send("recorder")

    async def websocket_closed_handler_for_tab(self, event: cdp.network.WebSocketClosed, session_id: str):
        try:
            rid = str(event.request_id)

            # Don't pop from active_websockets here — late-arriving frames after close
            # are still captured with a valid sequence number. _cleanup_and_build_report
            # clears active_websockets at session end.
            record = {
                "event": "closed",
                "request_id": rid,
                "closed_iso": self.ts_converter.to_iso8601(event.timestamp),
            }
            self._ws_connections_file.write(json.dumps(record) + "\n")
            self._ws_connections_file.flush()
        except Exception as e:
            events.log_error.send("recorder", text=f"WebSocketClosed handler failed: {e}")
            events.log_traceback.send("recorder")

    # -------------------------------------------------------------------------
    # Target Attach and Session Run
    # -------------------------------------------------------------------------

    def _attach_handlers_to_tab(self, tab_session, session_id, is_iframe=False):
        async def on_binding(e):
            await self.binding_handler_for_tab(e, session_id)

        tab_session.add_handler(cdp.runtime.BindingCalled, on_binding)

        if not is_iframe:

            async def on_request(e):
                await self.request_handler_for_tab(e, session_id)

            async def on_data(e):
                await self.data_received_handler_for_tab(e, session_id)

            async def on_response(e):
                await self.response_handler_for_tab(e, session_id)

            async def on_finished(e):
                await self.loading_finished_handler_for_tab(e, session_id)

            async def on_failed(e):
                await self.loading_failed_handler_for_tab(e, session_id)

            async def on_req_extra(e):
                await self.req_extra_info_for_tab(e, session_id)

            async def on_res_extra(e):
                await self.res_extra_info_for_tab(e, session_id)

            tab_session.add_handler(cdp.network.RequestWillBeSent, on_request)
            tab_session.add_handler(cdp.network.DataReceived, on_data)
            tab_session.add_handler(cdp.network.ResponseReceived, on_response)
            tab_session.add_handler(cdp.network.LoadingFinished, on_finished)
            tab_session.add_handler(cdp.network.LoadingFailed, on_failed)
            tab_session.add_handler(cdp.network.RequestWillBeSentExtraInfo, on_req_extra)
            tab_session.add_handler(cdp.network.ResponseReceivedExtraInfo, on_res_extra)

            async def on_ws_created(e):
                await self.websocket_created_handler_for_tab(e, session_id)

            async def on_ws_sent(e):
                await self.websocket_frame_sent_handler_for_tab(e, session_id)

            async def on_ws_received(e):
                await self.websocket_frame_received_handler_for_tab(e, session_id)

            async def on_ws_closed(e):
                await self.websocket_closed_handler_for_tab(e, session_id)

            tab_session.add_handler(cdp.network.WebSocketCreated, on_ws_created)
            tab_session.add_handler(cdp.network.WebSocketFrameSent, on_ws_sent)
            tab_session.add_handler(cdp.network.WebSocketFrameReceived, on_ws_received)
            tab_session.add_handler(cdp.network.WebSocketClosed, on_ws_closed)

            async def on_ws_handshake_req(e):
                await self.websocket_handshake_request_handler_for_tab(e, session_id)

            async def on_ws_handshake_res(e):
                await self.websocket_handshake_response_handler_for_tab(e, session_id)

            tab_session.add_handler(cdp.network.WebSocketWillSendHandshakeRequest, on_ws_handshake_req)
            tab_session.add_handler(cdp.network.WebSocketHandshakeResponseReceived, on_ws_handshake_res)

    async def target_created_handler(self, event: cdp.target.AttachedToTarget):
        target_info = event.target_info

        if target_info.type_ == "page":
            events.log_info.send("recorder", text=f"New Tab/Window Opened: {target_info.url}")

            # Low-latency rapid polling for the newly created tab session object.
            # Avoids a fixed 500ms blind window where critical network events might be lost.
            tab_session = None
            for _ in range(100):  # max 1.0s wait
                for t in self.browser.targets:
                    if (
                        getattr(t, "session_id", None) == event.session_id
                        or getattr(t, "target_id", None) == target_info.target_id
                    ):
                        tab_session = t
                        break
                if tab_session:
                    break
                await asyncio.sleep(0.01)

            if not tab_session:
                events.log_warn.send("recorder", text=f"Could not resolve Tab object for session {event.session_id}")
                return

            self.tabs[event.session_id] = {"tab": tab_session, "type": "page", "url": target_info.url}
            events.log_info.send("recorder", text=f"Successfully bound CDP to new tab: {target_info.target_id}")

            try:
                # Prioritize network domain to catch immediate websocket handshakes and HTTP requests
                await tab_session.send(
                    cdp.network.enable(
                        max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024
                    )
                )
                await tab_session.send(cdp.page.enable())
                await tab_session.send(cdp.page.set_bypass_csp(enabled=True))

                await tab_session.send(cdp.runtime.enable())
                await tab_session.send(cdp.runtime.add_binding(name="sendActionToPython"))

                self._attach_handlers_to_tab(tab_session, event.session_id, is_iframe=False)

                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
                )
                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.visuals_script, run_immediately=True)
                )

                await tab_session.send(cdp.runtime.evaluate(expression=self.telemetry_script))
                await tab_session.send(cdp.runtime.evaluate(expression=self.visuals_script))

            except Exception as exc:
                events.log_error.send("recorder", text=f"Failed to init CDP on new tab {target_info.target_id}: {exc}")
                events.log_traceback.send("recorder")

        elif target_info.type_ == "iframe":
            # We ONLY want JS actions from iframes (clicks inside Stripe gateways, etc.)
            # We explicitly do NOT track network requests for iframes because they are
            # already captured by the main 'page' network domain.

            if self.blocklist and self.blocklist.is_blocked_url(target_info.url):
                return

            tab_session = None
            for _ in range(100):  # max 1.0s wait
                for t in self.browser.targets:
                    if (
                        getattr(t, "session_id", None) == event.session_id
                        or getattr(t, "target_id", None) == target_info.target_id
                    ):
                        tab_session = t
                        break
                if tab_session:
                    break
                await asyncio.sleep(0.01)

            if not tab_session:
                return

            self.tabs[event.session_id] = {"tab": tab_session, "type": "iframe", "url": target_info.url}

            try:
                # Enable Runtime so we can add the binding to receive JS messages
                await tab_session.send(cdp.runtime.enable())
                await tab_session.send(cdp.runtime.add_binding(name="sendActionToPython"))

                self._attach_handlers_to_tab(tab_session, event.session_id, is_iframe=True)

                # Enable Page domain just enough to inject the script
                await tab_session.send(cdp.page.enable())
                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
                )

                # Evaluate immediately in case the iframe is already loaded
                await tab_session.send(cdp.runtime.evaluate(expression=self.telemetry_script))
            except Exception as e:
                # Iframes get destroyed quickly. We log to see if it's the real crash source.
                events.log_error.send("recorder", text=f"IFrame CDP initialization failed: {e}")
                events.log_traceback.send("recorder")

    async def run_session(self, url: str, stop_token=None) -> dict:
        if not self._load_scripts():
            return {}

        try:
            events.log_info.send("recorder", text="Starting Zendriver Browser...")
            self.browser = await zd.start(
                headless=False,
                browser_args=["--incognito", "--disable-popup-blocking", f"--user-data-dir={self._profile_dir.name}"],
            )
            self.recording_start = datetime.now(UTC)

            # 1. Get the primary tab
            self.tab = await self.browser.get("about:blank")

            # Register it in our central tabs registry
            main_session_id = getattr(self.tab, "session_id", "main")
            self.tabs[main_session_id] = {"tab": self.tab, "type": "page", "url": "about:blank"}

            events.log_info.send("recorder", text="Enabling CDP domains and binding handlers...")

            # Prioritize network domain
            await self.tab.send(
                cdp.network.enable(max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024)
            )
            await self.tab.send(cdp.page.enable())
            await self.tab.send(cdp.page.set_bypass_csp(enabled=True))
            await self.tab.send(cdp.runtime.enable())
            await self.tab.send(cdp.runtime.add_binding(name="sendActionToPython"))

            # 2. Bind the main tab handlers EXPLICITLY using our new registry functions
            self._attach_handlers_to_tab(self.tab, main_session_id, is_iframe=False)

            # Inject scripts into the main tab
            await self.tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
            )
            await self.tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=self.visuals_script, run_immediately=True)
            )

            await self.tab.send(cdp.runtime.evaluate(expression=self.telemetry_script))
            await self.tab.send(cdp.runtime.evaluate(expression=self.visuals_script))

            # 3. Enable auto-attach for ANY FUTURE tabs/windows/iframes
            await self.browser.connection.send(
                cdp.target.set_auto_attach(auto_attach=True, wait_for_debugger_on_start=False, flatten=True)
            )
            self.browser.connection.add_handler(cdp.target.AttachedToTarget, self.target_created_handler)
            await self.browser.connection.send(cdp.target.set_discover_targets(discover=True))

            events.log_info.send("recorder", text=f"Navigating to {url}")
            await self.tab.send(cdp.page.navigate(url=url))

            while not (stop_token and stop_token.is_stopped()):
                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            events.log_info.send("recorder", text="Session asyncio loop cancelled.")
            if stop_token:
                stop_token.stop()
        except KeyboardInterrupt:
            events.log_warn.send("recorder", text="Session encountered KeyboardInterrupt.")
            if stop_token:
                stop_token.stop()
        except Exception as e:
            self.session_crashed = True
            self.crash_timestamp = datetime.now(UTC).isoformat()
            self.crash_error = str(e)
            events.log_error.send("recorder", text=f"Session error: {e}")
            events.log_traceback.send("recorder")

        return await self._cleanup_and_build_report()

    async def _wait_for_pending_requests(self, timeout: float = 10.0, idle_time: float = 1.0) -> None:
        if not self.active_map:
            return
        pending = len(self.active_map)
        events.log_info.send("recorder", text=f"Waiting for {pending} pending request(s)...")

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        last_change = loop.time()
        prev_count = pending

        while self.active_map and loop.time() < deadline:
            current_count = len(self.active_map)
            if current_count != prev_count:
                last_change = loop.time()
                prev_count = current_count
            if loop.time() - last_change >= idle_time:
                break
            await asyncio.sleep(0.1)

    async def _cleanup_and_build_report(self) -> dict:
        try:
            await asyncio.wait_for(self._wait_for_pending_requests(), timeout=10.0)
        except TimeoutError:
            pass

        incomplete_count = len(self.active_map)
        if incomplete_count > 0:
            for req in self.active_map.values():
                req["request_state"] = "incomplete"
                self.stats["incomplete"] += 1
                self._requests_file.write(json.dumps(req) + "\n")
            self._requests_file.flush()
            self.active_map.clear()

        # Write synthetic "closed" events for WebSocket connections that didn't close cleanly
        if self.active_websockets:
            closed_iso = self.ts_converter.current_iso8601()
            for ws_request_id in self.active_websockets:
                closed_record = {
                    "event": "closed",
                    "request_id": str(ws_request_id),
                    "closed_iso": closed_iso,
                }
                self._ws_connections_file.write(json.dumps(closed_record) + "\n")
            self._ws_connections_file.flush()
            self.active_websockets.clear()

        # Close stream files
        try:
            self._actions_file.close()
            self._requests_file.close()
            self._ws_connections_file.close()
            self._ws_frames_file.close()
        except Exception as e:
            events.log_error.send("recorder", text=f"Failed to close stream files: {e}")
            events.log_traceback.send("recorder")

        if self.tab:
            self.tab.remove_handlers()

        for session_data in self.tabs.values():
            try:
                session_data["tab"].remove_handlers()
            except Exception as e:
                events.log_error.send("recorder", text=f"Cleanup error: {e}")
                events.log_traceback.send("recorder")

        try:
            if self.browser:
                await asyncio.wait_for(self.browser.stop(), timeout=5.0)
        except Exception as e:
            events.log_error.send("recorder", text=f"Browser stop cleanup failed: {e}")
            events.log_traceback.send("recorder")

        try:
            self._profile_dir.cleanup()
        except Exception as e:
            events.log_error.send("recorder", text=f"Profile dir cleanup failed: {e}")
            events.log_traceback.send("recorder")

        duration = (datetime.now(UTC) - self.recording_start).total_seconds() if self.recording_start else 0.0

        metadata = {
            "recording_started": self.recording_start.isoformat(timespec="milliseconds")
            if self.recording_start
            else None,
            "recording_ended": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "duration_seconds": round(duration, 2),
            "total_requests": self.stats["total_requests"],
            "completed_requests": self.stats["completed"],
            "failed_requests": self.stats["failed"],
            "incomplete_requests": self.stats["incomplete"],
            "total_actions": self._actions_count,
            "blocked_by_blocklist": self.stats["blocked_by_blocklist"],
            "timestamp_format": "ISO 8601 (YYYY-MM-DDTHH:MM:SS.sssZ)",
            "timezone": "UTC",
            "body_capture_stats": {
                "success": self.stats["body_success"],
                "from_stream": self.stats["body_from_stream"],
                "failed": self.stats["body_failed"],
                "skip_redirect": self.stats["body_skip_redirect"],
                "skip_no_content": self.stats["body_skip_no_content"],
                "skip_cached": self.stats["body_skip_cached"],
            },
            "websocket_stats": {
                "connections": self.stats["ws_connections"],
                "frames_sent": self.stats["ws_frames_sent"],
                "frames_received": self.stats["ws_frames_received"],
                "frames_skipped": self.stats["ws_frames_skipped"],
                "blocked_by_blocklist": self.stats["ws_blocked_by_blocklist"],
            },
            "session_crashed": self.session_crashed,
            "crash_timestamp": self.crash_timestamp,
            "crash_error": self.crash_error,
        }

        # Write metadata to the temp directory
        with open(os.path.join(self._data_dir.name, "metadata.json"), "w") as f:
            json.dump(metadata, f, indent=2)

        return self._data_dir.name
