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

        self._streamed_bodies: dict[str, list[bytes]] = {}

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

                self.captured_actions.append(payload)

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

        self.captured_requests.append(request_obj)
        self.active_map[event.request_id] = request_obj

    async def data_received_handler_for_tab(self, event: cdp.network.DataReceived, session_id: str):
        rid = str(event.request_id)
        if rid in self._streamed_bodies and event.data:
            try:
                self._streamed_bodies[rid].append(base64.b64decode(event.data))
            except Exception as exc:
                events.log_error.send("recorder", text=f"Failed to decode chunk {rid}: {exc}")
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
            self._streamed_bodies[rid] = []

            tab_session = self.tabs.get(session_id, {}).get("tab")
            if tab_session:
                try:
                    buffered = await tab_session.send(cdp.network.stream_resource_content(request_id=event.request_id))
                    if buffered:
                        self._streamed_bodies[rid].append(base64.b64decode(buffered))
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

                if tab_session:
                    try:
                        result = await tab_session.send(cdp.network.get_response_body(request_id=event.request_id))
                        if isinstance(result, tuple):
                            req["response_data"]["body"], req["response_data"]["base64_encoded"] = result
                        else:
                            req["response_data"]["body"] = result.body
                            req["response_data"]["base64_encoded"] = result.base64_encoded
                        self.stats["body_success"] += 1
                        body_captured = True
                    except Exception as e:
                        req["body_fetch_error"] = str(e)
                        # We won't log traceback for this one as get_response_body
                        # often fails for valid reasons (cache eviction)

                rid = str(event.request_id)
                if not body_captured and rid in self._streamed_bodies and self._streamed_bodies[rid]:
                    raw = b"".join(self._streamed_bodies[rid])
                    req["response_data"]["body"] = base64.b64encode(raw).decode("ascii")
                    req["response_data"]["base64_encoded"] = True
                    req["body_fetch_error"] = None
                    self.stats["body_from_stream"] += 1
                    body_captured = True

                if not body_captured:
                    self.stats["body_failed"] += 1

            self._streamed_bodies.pop(str(event.request_id), None)
            self.active_map.pop(event.request_id, None)

    async def loading_failed_handler_for_tab(self, event: cdp.network.LoadingFailed, session_id: str):
        if event.request_id in self.active_map:
            req = self.active_map[event.request_id]
            req["request_state"] = "failed"
            req["error_text"] = event.error_text
            self.stats["failed"] += 1
            self._streamed_bodies.pop(str(event.request_id), None)
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

    async def target_created_handler(self, event: cdp.target.AttachedToTarget):
        target_info = event.target_info

        if target_info.type_ == "page":
            events.log_info.send("recorder", text=f"New Tab/Window Opened: {target_info.url}")
            await asyncio.sleep(0.5)

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

            self.tabs[event.session_id] = {"tab": tab_session, "type": "page", "url": target_info.url}
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

            await asyncio.sleep(0.1)
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
            await self.tab.send(cdp.page.enable())
            await self.tab.send(cdp.page.set_bypass_csp(enabled=True))
            await self.tab.send(
                cdp.network.enable(max_resource_buffer_size=100 * 1024 * 1024, max_total_buffer_size=1000 * 1024 * 1024)
            )
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

        return {
            "metadata": {
                "recording_started": self.recording_start.isoformat(timespec="milliseconds")
                if self.recording_start
                else None,
                "recording_ended": datetime.now(UTC).isoformat(timespec="milliseconds"),
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
