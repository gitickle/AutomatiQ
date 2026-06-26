"""Tests for process_websocket_streams — the WebSocket compile phase.

Synthetic WS connections and frames JSONL (matching the schema the CDP handlers
write) are fed through process_websocket_streams. We assert per-connection
folders, transaction.json, frame file naming with opcode suffixes, timeline
events, and stats.
"""

import json
import os

from automatiq.core.recorder.compile.serializers import sanitize_filename
from automatiq.core.recorder.compile.websockets import process_websocket_streams

from .conftest import write_jsonl


def make_ws_connection_records(
    request_id="WS_001",
    url="wss://example.com/socket",
    created_iso="2024-01-01T00:00:00.000+00:00",
    closed_iso="2024-01-01T00:00:10.000+00:00",
    request_headers=None,
    response_headers=None,
    response_status=101,
    include_created=True,
    include_closed=True,
):
    """Build WS connection lifecycle records matching the CDP handler schema."""
    if request_headers is None:
        request_headers = {"Upgrade": "websocket", "Host": "example.com"}
    if response_headers is None:
        response_headers = {"Upgrade": "websocket", "Connection": "Upgrade"}

    records = []
    if include_created:
        records.append({"event": "created", "request_id": request_id, "url": url, "created_iso": created_iso})
    records.append(
        {
            "event": "handshake_request",
            "request_id": request_id,
            "request_headers": request_headers,
            "start_time": 1000.0,
            "created_iso": created_iso,
        }
    )
    records.append(
        {
            "event": "handshake_response",
            "request_id": request_id,
            "response_headers": response_headers,
            "response_status": response_status,
            "created_iso": created_iso,
        }
    )
    if include_closed:
        records.append({"event": "closed", "request_id": request_id, "closed_iso": closed_iso})
    return records


def make_ws_frame_record(
    request_id="WS_001",
    seq=1,
    direction="client",
    delta_ms=0,
    opcode=1.0,
    payload="hello",
    is_base64=False,
):
    """Build a WS frame record matching the CDP handler schema."""
    return {
        "request_id": request_id,
        "seq": seq,
        "direction": direction,
        "delta_ms": delta_ms,
        "opcode": opcode,
        "payload": payload,
        "is_base64": is_base64,
    }


class TestProcessWebsocketStreams:
    def test_full_ws_lifecycle(self, tmp_path):
        """Complete WS connection + frames produces folder, transaction.json, frame files, timeline."""
        ws_output_dir = str(tmp_path / "websockets")
        os.makedirs(ws_output_dir, exist_ok=True)

        connections = make_ws_connection_records()
        frames = [
            make_ws_frame_record(seq=1, direction="client", delta_ms=0, payload="hello"),
            make_ws_frame_record(seq=2, direction="server", delta_ms=100, payload="world"),
        ]

        connections_path = str(tmp_path / "ws_connections.jsonl")
        frames_path = str(tmp_path / "ws_frames.jsonl")
        write_jsonl(connections_path, connections)
        write_jsonl(frames_path, frames)

        timeline, ws_stats = process_websocket_streams(connections_path, frames_path, ws_output_dir)

        # Folder: ws_{sanitize_filename(domain)}_{rid}
        expected_folder = f"ws_{sanitize_filename('example.com')}_WS_001"
        ws_root = os.path.join(ws_output_dir, expected_folder)
        assert os.path.isdir(ws_root)

        # transaction.json
        transaction_path = os.path.join(ws_root, "transaction.json")
        assert os.path.exists(transaction_path)
        with open(transaction_path) as f:
            transaction = json.load(f)

        assert transaction["url"] == "wss://example.com/socket"
        assert transaction["response_status"] == 101
        assert transaction["created_iso"] == "2024-01-01T00:00:00.000+00:00"
        assert transaction["closed_iso"] == "2024-01-01T00:00:10.000+00:00"
        assert transaction["request_headers"]["Host"] == "example.com"

        # Frame files: {seq:05d}_{direction}_{delta_ms}ms{suffix}.{ext}
        frame_files = sorted(f for f in os.listdir(ws_root) if not f.endswith("transaction.json"))
        assert len(frame_files) == 2

        # First frame: 00001_client_0ms*.txt (or detected ext)
        assert frame_files[0].startswith("00001_client_0ms")
        # Second frame: 00002_server_100ms*.txt
        assert frame_files[1].startswith("00002_server_100ms")

        # Timeline events
        assert len(timeline) == 2
        created_event = next(e for e in timeline if e["event_type"] == "websocket_created")
        closed_event = next(e for e in timeline if e["event_type"] == "websocket_closed")
        assert created_event["url"] == "wss://example.com/socket"
        assert created_event["folder"] == f"websockets/{expected_folder}"
        assert closed_event["folder"] == f"websockets/{expected_folder}"

        # Stats
        assert ws_stats["connections"] == 1
        assert ws_stats["frames"] == 2
        assert ws_stats["skipped"] == 0

    def test_url_reconstruction_from_host_header(self, tmp_path):
        """When WebSocketCreated is missed, URL is reconstructed from Host header as wss://."""
        ws_output_dir = str(tmp_path / "websockets")
        os.makedirs(ws_output_dir, exist_ok=True)

        # No "created" record — only handshake_request with Host header
        connections = make_ws_connection_records(include_created=False)
        write_jsonl(str(tmp_path / "ws_connections.jsonl"), connections)
        write_jsonl(str(tmp_path / "ws_frames.jsonl"), [])

        timeline, ws_stats = process_websocket_streams(
            str(tmp_path / "ws_connections.jsonl"), str(tmp_path / "ws_frames.jsonl"), ws_output_dir
        )

        # URL should be reconstructed as wss://example.com
        transaction_path = os.path.join(ws_output_dir, "ws_example.com_WS_001", "transaction.json")
        with open(transaction_path) as f:
            transaction = json.load(f)
        assert transaction["url"] == "wss://example.com"

    def test_unknown_rid_frames_skipped(self, tmp_path):
        """Frames for unknown request_id are skipped and counted."""
        ws_output_dir = str(tmp_path / "websockets")
        os.makedirs(ws_output_dir, exist_ok=True)

        connections = make_ws_connection_records()
        frames = [
            make_ws_frame_record(request_id="WS_001", seq=1, payload="hello"),
            make_ws_frame_record(request_id="UNKNOWN", seq=1, payload="orphan"),
        ]

        write_jsonl(str(tmp_path / "ws_connections.jsonl"), connections)
        write_jsonl(str(tmp_path / "ws_frames.jsonl"), frames)

        _, ws_stats = process_websocket_streams(
            str(tmp_path / "ws_connections.jsonl"), str(tmp_path / "ws_frames.jsonl"), ws_output_dir
        )

        assert ws_stats["frames"] == 1
        assert ws_stats["skipped"] == 1

    def test_close_opcode_suffix(self, tmp_path):
        """Close frames (opcode=8) get _close suffix in filename."""
        ws_output_dir = str(tmp_path / "websockets")
        os.makedirs(ws_output_dir, exist_ok=True)

        connections = make_ws_connection_records()
        frames = [
            make_ws_frame_record(opcode=8.0, payload="close frame", is_base64=False),
        ]

        write_jsonl(str(tmp_path / "ws_connections.jsonl"), connections)
        write_jsonl(str(tmp_path / "ws_frames.jsonl"), frames)

        process_websocket_streams(
            str(tmp_path / "ws_connections.jsonl"), str(tmp_path / "ws_frames.jsonl"), ws_output_dir
        )

        ws_root = os.path.join(ws_output_dir, "ws_example.com_WS_001")
        frame_files = [f for f in os.listdir(ws_root) if not f.endswith("transaction.json")]
        assert len(frame_files) == 1
        assert "_close" in frame_files[0]

    def test_ping_pong_suffixes(self, tmp_path):
        """Ping (opcode=9) and pong (opcode=10) frames get _ping/_pong suffixes."""
        ws_output_dir = str(tmp_path / "websockets")
        os.makedirs(ws_output_dir, exist_ok=True)

        connections = make_ws_connection_records()
        frames = [
            make_ws_frame_record(seq=1, opcode=9.0, payload="ping"),
            make_ws_frame_record(seq=2, opcode=10.0, payload="pong"),
        ]

        write_jsonl(str(tmp_path / "ws_connections.jsonl"), connections)
        write_jsonl(str(tmp_path / "ws_frames.jsonl"), frames)

        process_websocket_streams(
            str(tmp_path / "ws_connections.jsonl"), str(tmp_path / "ws_frames.jsonl"), ws_output_dir
        )

        ws_root = os.path.join(ws_output_dir, "ws_example.com_WS_001")
        frame_files = sorted(f for f in os.listdir(ws_root) if not f.endswith("transaction.json"))
        assert len(frame_files) == 2
        assert "_ping" in frame_files[0]
        assert "_pong" in frame_files[1]

    def test_missing_connections_file(self, tmp_path):
        """Missing connections file returns empty timeline and zeroed stats."""
        timeline, ws_stats = process_websocket_streams(
            str(tmp_path / "nonexistent.jsonl"), str(tmp_path / "frames.jsonl"), str(tmp_path / "ws")
        )

        assert timeline == []
        assert ws_stats["connections"] == 0
        assert ws_stats["frames"] == 0

    def test_missing_frames_file(self, tmp_path):
        """Missing frames file still processes connections — just no frame files."""
        ws_output_dir = str(tmp_path / "websockets")
        os.makedirs(ws_output_dir, exist_ok=True)

        connections = make_ws_connection_records()
        write_jsonl(str(tmp_path / "ws_connections.jsonl"), connections)

        timeline, ws_stats = process_websocket_streams(
            str(tmp_path / "ws_connections.jsonl"), str(tmp_path / "nonexistent.jsonl"), ws_output_dir
        )

        # Connection still processed
        assert ws_stats["connections"] == 1
        assert ws_stats["frames"] == 0
        # Timeline has created + closed
        assert len(timeline) == 2

    def test_multiple_connections(self, tmp_path):
        """Multiple WS connections each get their own folder."""
        ws_output_dir = str(tmp_path / "websockets")
        os.makedirs(ws_output_dir, exist_ok=True)

        connections = [
            *make_ws_connection_records(request_id="WS_001", url="wss://a.com/socket"),
            *make_ws_connection_records(request_id="WS_002", url="wss://b.com/socket"),
        ]
        write_jsonl(str(tmp_path / "ws_connections.jsonl"), connections)

        timeline, ws_stats = process_websocket_streams(
            str(tmp_path / "ws_connections.jsonl"), str(tmp_path / "nonexistent.jsonl"), ws_output_dir
        )

        assert ws_stats["connections"] == 2
        assert os.path.isdir(os.path.join(ws_output_dir, "ws_a.com_WS_001"))
        assert os.path.isdir(os.path.join(ws_output_dir, "ws_b.com_WS_002"))
        assert len(timeline) == 4  # 2 created + 2 closed
