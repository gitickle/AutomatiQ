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
        self.browser = None
        self.tab = None
        self.recording_start = None

        self.ts_converter = TimestampConverter()

        self.captured_requests = []
        self.captured_actions = []
        self.active_map = {}
        self.orphan_extra_info = {}
        # request_id (str) -> list[bytes]: chunks accumulated via streamResourceContent + DataReceived
        self._streamed_bodies: dict[str, list[bytes]] = {}
        # request_id (str) -> asyncio.Task: tracks in-flight _start_streaming tasks
        self._streaming_tasks: dict[str, asyncio.Task] = {}
        # All tab_session objects opened via target_created_handler, for cleanup
        self._extra_tabs: list = []

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
        }

        self.telemetry_script = ""
        self.visuals_script = ""

    def _load_scripts(self) -> bool:
        """Loads the injected JavaScript files from disk."""
        try:
            with open(self.telemetry_js_path, encoding="utf-8") as f:
                self.telemetry_script = f.read()
            with open(self.visuals_js_path, encoding="utf-8") as f:
                self.visuals_script = f.read()
            return True
        except FileNotFoundError as e:
            events.log_error.send("recorder", text=f"Missing JS dependencies: {e}")
            events.log_traceback.send("recorder")
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

    def _make_handlers(self, tab):
        """
        Return a set of CDP event handler coroutines bound to a specific tab.

        Every handler that issues CDP commands (response_handler via
        _start_streaming, and loading_finished_handler) needs to know which
        tab the request originated from so commands go to the right session.
        We close over `tab` here instead of using self.tab, which always
        points to the main tab.
        """

        async def binding_handler(event: cdp.runtime.BindingCalled):
            await self.binding_handler(event)

        async def request_handler(event: cdp.network.RequestWillBeSent):
            await self._request_handler(event, tab)

        async def data_received_handler(event: cdp.network.DataReceived):
            await self.data_received_handler(event)

        async def response_handler(event: cdp.network.ResponseReceived):
            await self._response_handler(event, tab)

        async def loading_finished_handler(event: cdp.network.LoadingFinished):
            await self._loading_finished_handler(event, tab)

        async def loading_failed_handler(event: cdp.network.LoadingFailed):
            await self._loading_failed_handler(event)

        async def req_extra_info(event: cdp.network.RequestWillBeSentExtraInfo):
            await self.req_extra_info(event)

        async def res_extra_info(event: cdp.network.ResponseReceivedExtraInfo):
            await self.res_extra_info(event)

        return (
            binding_handler,
            request_handler,
            data_received_handler,
            response_handler,
            loading_finished_handler,
            loading_failed_handler,
            req_extra_info,
            res_extra_info,
        )

    def _register_network_handlers(self, tab_obj):
        """Register all CDP network + runtime handlers on tab_obj, bound to that tab."""
        (
            binding_handler,
            request_handler,
            data_received_handler,
            response_handler,
            loading_finished_handler,
            loading_failed_handler,
            req_extra_info,
            res_extra_info,
        ) = self._make_handlers(tab_obj)

        tab_obj.add_handler(cdp.runtime.BindingCalled, binding_handler)
        tab_obj.add_handler(cdp.network.RequestWillBeSent, request_handler)
        tab_obj.add_handler(cdp.network.DataReceived, data_received_handler)
        tab_obj.add_handler(cdp.network.ResponseReceived, response_handler)
        tab_obj.add_handler(cdp.network.LoadingFinished, loading_finished_handler)
        tab_obj.add_handler(cdp.network.LoadingFailed, loading_failed_handler)
        tab_obj.add_handler(cdp.network.RequestWillBeSentExtraInfo, req_extra_info)
        tab_obj.add_handler(cdp.network.ResponseReceivedExtraInfo, res_extra_info)

    # -------------------------------------------------------------------------
    # Tab-independent handlers (no CDP commands issued)
    # -------------------------------------------------------------------------

    async def binding_handler(self, event: cdp.runtime.BindingCalled):
        if event.name == "sendActionToPython":
            try:
                payload = json.loads(event.payload)
                payload["timestamp_iso"] = self.ts_converter.current_iso8601()
                payload["timestamp_unix"] = time.time()
                payload["execution_context_id"] = event.execution_context_id

                self.captured_actions.append(payload)

                action_type = payload.get("type")
                if action_type == "keypress":
                    events.log_info.send("recorder", text=f"[ACTION] keypress: {payload.get('key')}")
                elif action_type == "click":
                    events.log_info.send("recorder", text=f"[ACTION] click: {payload.get('text', '')[:50]}")
                else:
                    fallback_val = payload.get("value", payload.get("newUrl", payload.get("text", "")))
                    events.log_info.send(
                        "recorder",
                        text=f"[ACTION] {action_type}: {fallback_val[:50]}",
                    )
            except Exception as e:
                events.log_error.send("recorder", text=f"Binding handler failed: {e}")
                events.log_traceback.send("recorder")

    async def _request_handler(self, event: cdp.network.RequestWillBeSent, tab):
        """request_handler closed over the originating tab so it can be stored per-request."""
        if event.wall_time:
            self.ts_converter.calibrate(event.timestamp, event.wall_time)

        if event.type_ in (
            cdp.network.ResourceType.DOCUMENT,
            cdp.network.ResourceType.XHR,
            cdp.network.ResourceType.FETCH,
            cdp.network.ResourceType.SCRIPT,
        ):
            self.stats["total_requests"] += 1

        if event.request_id in self.active_map:
            old_req = self.active_map[event.request_id]
            if event.redirect_response and not old_req["response_data"]:
                rd = event.redirect_response.to_json()
                old_req["response_data"] = {"status": rd["status"], "headers": rd.get("headers", {}), "body": None}
            # Mark the old redirect entry as redirected so it doesn't linger as "pending"
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
            # FIX (Bug 1): store originating tab so response/body handlers
            # send CDP commands to the correct session, not always self.tab.
            "_tab": tab,
        }

        if event.request_id in self.orphan_extra_info:
            data = self.orphan_extra_info.pop(event.request_id)
            if "sent" in data:
                request_obj["cookies_sent_details"] = data["sent"]
            if "received" in data:
                request_obj["cookies_received_details"] = data["received"]
            if "raw_headers" in data:
                self.merge_headers(request_obj, data["raw_headers"])

        # Skip data: URIs (base64-encoded inline resources) — they add noise, not useful context
        if event.request.url.startswith("data:"):
            return

        # Skip domains on the blocklist (ads, trackers, telemetry)
        if self.blocklist and self.blocklist.is_blocked_url(event.request.url):
            self.stats["blocked_by_blocklist"] += 1
            return

        self.captured_requests.append(request_obj)
        self.active_map[event.request_id] = request_obj

    async def data_received_handler(self, event: cdp.network.DataReceived):
        """Accumulate streamed response body chunks.

        DataReceived.data is Optional[str] and base64-encoded (CDP wire spec).
        It is only populated when streamResourceContent has been called for this
        request — without it Chrome fires DataReceived with data=None.
        """
        rid = str(event.request_id)
        if rid in self._streamed_bodies and event.data:
            try:
                self._streamed_bodies[rid].append(base64.b64decode(event.data))
            except Exception as exc:
                events.log_warn.send("recorder", text=f"Failed to decode streamed body chunk for request {rid}: {exc}")
                events.log_traceback.send("recorder")

    async def _response_handler(self, event: cdp.network.ResponseReceived, tab):
        if event.request_id not in self.active_map:
            return

        req = self.active_map[event.request_id]
        req["request_state"] = "received"
        req["response_timing"]["received_iso"] = self.ts_converter.to_iso8601(event.timestamp)
        req["response_timing"]["received_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

        if "timestamp_unix" in req:
            duration_ms = (req["response_timing"]["received_unix"] - req["timestamp_unix"]) * 1000
            req["response_timing"]["duration_ms"] = round(duration_ms, 2)

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
            req["response_data"]["from_disk_cache"] = resp.get("fromDiskCache", False)
            req["response_data"]["from_service_worker"] = resp.get("fromServiceWorker", False)
            req["response_data"]["from_prefetch_cache"] = resp.get("fromPrefetchCache", False)

        self.merge_headers(req, resp.get("headers", {}))

        # FIX (Bug 1): use the tab this response came from, not self.tab
        # FIX (v3 race): register slot SYNCHRONOUSLY before any await so
        # DataReceived events accumulate immediately and LoadingFinished always
        # finds the slot even if it fires before _start_streaming returns.
        rid = str(event.request_id)
        self._streamed_bodies[rid] = []

        task = asyncio.ensure_future(self._start_streaming(event.request_id, rid, tab))
        # FIX (Bug 2): store task so loading_finished_handler can await it
        # before checking the fallback chunks, preventing the race where
        # LoadingFinished runs and pops an empty list before the pre-buffered
        # chunk has been inserted by _start_streaming.
        self._streaming_tasks[rid] = task

    async def _start_streaming(self, request_id, rid: str, tab) -> None:
        """Background task: enable streamResourceContent and store pre-buffered data.

        FIX (Bug 1): accepts `tab` so the CDP command goes to the correct
        session, not always the main tab.
        """
        try:
            # stream_resource_content returns a base64-encoded str (bufferedData)
            # representing data Chrome already received before we subscribed.
            buffered: str = await tab.send(cdp.network.stream_resource_content(request_id=request_id))
            # Only store if the slot still exists (request hasn't finished/failed yet)
            if rid in self._streamed_bodies and buffered:
                # Insert at position 0 — pre-buffered data comes before DataReceived chunks
                self._streamed_bodies[rid].insert(0, base64.b64decode(buffered))
        except Exception as e:
            error_str = str(e)
            if not any(
                x in error_str
                for x in (
                    "already finished loading",
                    "Request with the provided ID",
                    "No resource with given identifier",
                )
            ):
                events.log_warn.send("recorder", text=f"stream_resource_content failed: {e}")
                events.log_traceback.send("recorder")

    async def _loading_finished_handler(self, event: cdp.network.LoadingFinished, tab):
        if event.request_id not in self.active_map:
            return

        req = self.active_map[event.request_id]
        req["request_state"] = "finished"
        self.stats["completed"] += 1

        req["response_timing"]["finished_iso"] = self.ts_converter.to_iso8601(event.timestamp)
        req["response_timing"]["finished_unix"] = self.ts_converter.to_unix_timestamp(event.timestamp)

        if "timestamp_unix" in req:
            total_ms = (req["response_timing"]["finished_unix"] - req["timestamp_unix"]) * 1000
            req["response_timing"]["total_duration_ms"] = round(total_ms, 2)

        if req["response_data"]:
            status = req["response_data"].get("status", 0)
            from_cache = (
                req["response_data"].get("from_disk_cache", False)
                or req["response_data"].get("from_service_worker", False)
                or req["response_data"].get("from_prefetch_cache", False)
            )

            should_skip = False
            skip_reason = None

            if 300 <= status < 400:
                should_skip = True
                skip_reason = f"Redirect status {status}"
                self.stats["body_skip_redirect"] += 1
            elif status in (204, 205, 304):
                should_skip = True
                skip_reason = f"No content status {status}"
                self.stats["body_skip_no_content"] += 1

            if should_skip:
                req["body_fetch_error"] = skip_reason
            else:
                body_captured = False
                rid = str(event.request_id)

                # FIX (Bug 2): await the streaming task before checking fallback
                # chunks. For fast responses, _start_streaming may not have run yet
                # (pre-buffered chunk not inserted), so we wait up to 2s.
                task = self._streaming_tasks.pop(rid, None)
                if task and not task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                    except (TimeoutError, Exception):
                        pass

                # Primary: getResponseBody — works for small/buffered responses.
                # FIX (Bug 1): send to the correct tab, not always self.tab.
                try:
                    result = await tab.send(cdp.network.get_response_body(request_id=event.request_id))
                    if isinstance(result, tuple):
                        body, is_base64 = result
                        req["response_data"]["body"] = body
                        req["response_data"]["base64_encoded"] = is_base64
                    else:
                        req["response_data"]["body"] = result.body
                        req["response_data"]["base64_encoded"] = result.base64_encoded
                    self.stats["body_success"] += 1
                    body_captured = True
                except Exception as e:
                    req["body_fetch_error"] = str(e)

                # Fallback: reassemble from chunks collected via DataReceived.
                # Chunks are raw bytes (already base64-decoded as they arrived).
                if not body_captured and rid in self._streamed_bodies:
                    chunks = self._streamed_bodies[rid]
                    if chunks:
                        raw = b"".join(chunks)
                        req["response_data"]["body"] = base64.b64encode(raw).decode("ascii")
                        req["response_data"]["base64_encoded"] = True
                        req["body_fetch_error"] = None
                        self.stats["body_from_stream"] += 1
                        body_captured = True

                if not body_captured:
                    if "No resource with given identifier" in (req.get("body_fetch_error") or ""):
                        if from_cache:
                            self.stats["body_skip_cached"] += 1
                        else:
                            self.stats["body_failed"] += 1
                    else:
                        self.stats["body_failed"] += 1

        self._streamed_bodies.pop(str(event.request_id), None)
        self.active_map.pop(event.request_id, None)

    async def _loading_failed_handler(self, event: cdp.network.LoadingFailed):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "failed"
            req["loading_failed"] = True
            req["error_text"] = event.error_text
            req["canceled"] = event.canceled
            req["blocked_reason"] = str(event.blocked_reason) if event.blocked_reason else None
            self.stats["failed"] += 1
            rid = str(event.request_id)
            self._streamed_bodies.pop(rid, None)
            # FIX (Bug 2): cancel and discard any in-flight streaming task
            task = self._streaming_tasks.pop(rid, None)
            if task and not task.done():
                task.cancel()
            self.active_map.pop(event.request_id, None)
            # Suppress log noise for user-initiated navigations (cancel is expected)
            if not event.canceled:
                events.log_warn.send("recorder", text=f"Request failed: {req['url'][:60]} - {event.error_text}")

    async def req_extra_info(self, event: cdp.network.RequestWillBeSentExtraInfo):
        cookies = [ac.to_json() for ac in event.associated_cookies]
        if event.request_id in self.active_map:
            self.active_map[event.request_id]["cookies_sent_details"] = cookies
        else:
            if event.request_id not in self.orphan_extra_info:
                self.orphan_extra_info[event.request_id] = {}
            self.orphan_extra_info[event.request_id]["sent"] = cookies

    async def res_extra_info(self, event: cdp.network.ResponseReceivedExtraInfo):
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

    async def target_created_handler(self, event: cdp.target.AttachedToTarget):
        target_info = event.target_info

        # We only care about full pages (not service workers or iframes)
        if target_info.type_ == "page":
            events.log_info.send("recorder", text=f"New Tab/Window Opened: {target_info.url}")

            # Wait a tiny moment for zendriver to internally register the new tab
            await asyncio.sleep(0.5)

            # Find the actual Tab object zendriver created for this session
            tab_session = None
            for t in self.browser.targets:
                if (
                    getattr(t, "session_id", None) == event.session_id
                    or getattr(t, "target_id", None) == target_info.target_id
                ):
                    tab_session = t
                    break

            if not tab_session:
                events.log_warn.send("recorder", text=f"Could not resolve Tab object for session {event.session_id}")
                return

            events.log_info.send("recorder", text=f"Successfully bound CDP to new tab: {target_info.target_id}")

            try:
                await tab_session.send(cdp.page.enable())
                await tab_session.send(cdp.page.set_bypass_csp(enabled=True))
                await tab_session.send(
                    cdp.network.enable(
                        max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024
                    )
                )
                await tab_session.send(cdp.runtime.enable())
                await tab_session.send(cdp.runtime.add_binding(name="sendActionToPython"))

                # FIX (Bug 1): use _register_network_handlers which closes over
                # tab_session so all CDP commands go to this tab, not self.tab.
                self._register_network_handlers(tab_session)

                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
                )
                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.visuals_script, run_immediately=True)
                )

                # FIX (Bug 3): track extra tabs so _cleanup can remove their handlers
                self._extra_tabs.append(tab_session)

            except Exception as exc:
                events.log_warn.send(
                    "recorder", text=f"Failed to initialise CDP on new tab {target_info.target_id}: {exc}"
                )
                events.log_traceback.send("recorder")

        elif target_info.type_ == "iframe":
            # Cross-origin iframe: only need binding + telemetry script.
            # Network events are already captured by the parent tab session.
            await asyncio.sleep(0.2)
            tab_session = None
            for t in self.browser.targets:
                if (
                    getattr(t, "session_id", None) == event.session_id
                    or getattr(t, "target_id", None) == target_info.target_id
                ):
                    tab_session = t
                    break

            if not tab_session:
                return

            try:
                await tab_session.send(cdp.runtime.enable())
                await tab_session.send(cdp.runtime.add_binding(name="sendActionToPython"))

                async def _iframe_binding_handler(evt: cdp.runtime.BindingCalled):
                    await self.binding_handler(evt)

                tab_session.add_handler(cdp.runtime.BindingCalled, _iframe_binding_handler)

                await tab_session.send(
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
                )
                self._extra_tabs.append(tab_session)
            except Exception as exc:
                events.log_warn.send("recorder", text=f"Failed to init CDP on iframe {target_info.target_id}: {exc}")

    async def run_session(self, url: str, stop_token=None) -> dict:
        if not self._load_scripts():
            return {}

        try:
            events.log_info.send("recorder", text="Starting Zendriver Browser...")
            self.browser = await zd.start(
                headless=False,
                browser_args=["--disable-popup-blocking", f"--user-data-dir={self._profile_dir.name}"],
            )
            self.recording_start = datetime.now(UTC)
            self.tab = await self.browser.get("about:blank")

            events.log_info.send("recorder", text="Enabling CDP domains and binding handlers...")
            await self.tab.send(cdp.page.enable())
            await self.tab.send(cdp.page.set_bypass_csp(enabled=True))
            await self.tab.send(
                cdp.network.enable(max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024)
            )
            await self.tab.send(cdp.runtime.enable())
            await self.tab.send(cdp.runtime.add_binding(name="sendActionToPython"))

            # FIX (Bug 1): use _register_network_handlers for the main tab too,
            # so all handlers close over self.tab consistently.
            self._register_network_handlers(self.tab)

            await self.tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=self.telemetry_script, run_immediately=True)
            )
            await self.tab.send(
                cdp.page.add_script_to_evaluate_on_new_document(source=self.visuals_script, run_immediately=True)
            )

            await self.tab.send(cdp.runtime.evaluate(expression=self.telemetry_script))
            await self.tab.send(cdp.runtime.evaluate(expression=self.visuals_script))

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
            events.log_error.send("recorder", text=f"Session encountered an error: {e}")
            events.log_traceback.send("recorder")

        return await self._cleanup_and_build_report()

    async def _wait_for_pending_requests(self, timeout: float = 10.0, idle_time: float = 1.0) -> None:
        """Wait until all tracked requests in active_map have resolved
        (LoadingFinished/LoadingFailed), or until we've been network-idle
        for `idle_time` seconds, whichever comes first.
        Gives up entirely after `timeout` seconds."""
        if not self.active_map:
            return

        pending = len(self.active_map)
        events.log_info.send(
            "recorder",
            text=f"Waiting for {pending} pending request(s) to complete (timeout={timeout}s, idle={idle_time}s)...",
        )

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
                events.log_info.send(
                    "recorder",
                    text=f"Network idle for {idle_time}s with {current_count} request(s) still pending — moving on.",
                )
                break

            await asyncio.sleep(0.1)

        remaining = len(self.active_map)
        if remaining:
            events.log_warn.send(
                "recorder", text=f"Drain finished with {remaining} request(s) still pending after {timeout}s."
            )
        else:
            events.log_info.send("recorder", text="All pending requests resolved.")

    async def _cleanup_and_build_report(self) -> dict:
        # Let in-flight requests settle before tearing down
        try:
            await asyncio.wait_for(self._wait_for_pending_requests(), timeout=10.0)
        except TimeoutError:
            events.log_warn.send("recorder", text="Timeout waiting for pending requests. Moving on.")

        events.log_info.send("recorder", text="Processing incomplete network requests...")

        incomplete_count = len(self.active_map)
        if incomplete_count > 0:
            events.log_warn.send("recorder", text=f"Found {incomplete_count} incomplete requests.")
            for _request_id, req in self.active_map.items():
                current_state = req.get("request_state", "unknown")
                if current_state == "pending":
                    req["request_state"] = "incomplete_no_response"
                    req["incomplete_reason"] = "Recording stopped before response received"
                elif current_state == "received":
                    req["request_state"] = "incomplete_loading"
                    req["incomplete_reason"] = "Recording stopped during response loading"
                else:
                    req["request_state"] = "incomplete_unknown"
                    req["incomplete_reason"] = f"Recording stopped while in state: {current_state}"

                self.stats["incomplete"] += 1

        # FIX (Bug 3): remove handlers from the main tab AND all extra tabs
        if self.tab:
            self.tab.remove_handlers()
        for extra_tab in self._extra_tabs:
            try:
                extra_tab.remove_handlers()
            except Exception:
                pass
        self._extra_tabs.clear()

        try:
            if self.browser:
                await asyncio.wait_for(self.browser.stop(), timeout=5.0)
        except Exception as exc:
            events.log_warn.send("recorder", text=f"Failed to stop browser cleanly, ignoring: {exc}")
            events.log_traceback.send("recorder")

        try:
            self._profile_dir.cleanup()
        except Exception as exc:
            events.log_warn.send("recorder", text=f"Could not clean up temporary Chrome profile: {exc}")
            events.log_traceback.send("recorder")

        recording_end = datetime.now(UTC)
        duration = (recording_end - self.recording_start).total_seconds() if self.recording_start else 0.0

        events.log_info.send(
            "recorder",
            text=f"Collection Complete. Captured {len(self.captured_requests)} requests "
            f"and {len(self.captured_actions)} actions over {duration:.2f}s.",
        )

        return {
            "metadata": {
                "recording_started": self.recording_start.isoformat(timespec="milliseconds")
                if self.recording_start
                else None,
                "recording_ended": recording_end.isoformat(timespec="milliseconds"),
                "duration_seconds": round(duration, 2),
                "total_requests": self.stats["total_requests"],
                "completed_requests": self.stats["completed"],
                "failed_requests": self.stats["failed"],
                "incomplete_requests": self.stats["incomplete"],
                "total_actions": len(self.captured_actions),
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
            },
            "requests": self.captured_requests,
            "actions": self.captured_actions,
        }
