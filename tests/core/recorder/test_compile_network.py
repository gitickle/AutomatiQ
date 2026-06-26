"""Tests for process_network_requests — the network compile phase.

Synthetic JSONL records (matching the schema the CDP handlers write) are fed
through process_network_requests. We assert the output folder structure,
transaction.json content, body file copying, timeline events, and stats.
"""

import json
import os

from automatiq.core.recorder.compile.network import process_network_requests
from automatiq.core.recorder.compile.serializers import sanitize_filename

from .conftest import write_jsonl


def make_request_record(
    request_id="REQ_001",
    url="https://example.com/api/data",
    method="GET",
    status=200,
    headers=None,
    post_data=None,
    body_file=None,
    timestamp_unix=1700000000.0,
    timestamp_iso="2024-01-01T00:00:00.000+00:00",
    cookies_sent_details=None,
    response_headers=None,
    auth_header=None,
):
    """Build a synthetic request record matching the CDP handler JSONL schema."""
    if headers is None:
        headers = {"Accept": "application/json"}
    if cookies_sent_details is None:
        cookies_sent_details = []
    if response_headers is None:
        response_headers = {"Content-Type": "application/json"}
    if auth_header is not None:
        headers["Authorization"] = auth_header

    return {
        "unique_id": f"{request_id}_abc12345",
        "request_id": request_id,
        "timestamp_iso": timestamp_iso,
        "timestamp_unix": timestamp_unix,
        "url": url,
        "method": method,
        "headers": headers,
        "post_data": post_data,
        "cookies_sent_details": cookies_sent_details,
        "cookies_received_details": {},
        "response_data": {
            "status": status,
            "headers": response_headers,
            "body": None,
            "mime_type": "application/json",
            "body_file": body_file,
        },
        "response_timing": {
            "received_unix": timestamp_unix + 1,
            "finished_unix": timestamp_unix + 2,
            "total_duration_ms": 2000.0,
        },
        "request_state": "finished",
    }


class TestProcessNetworkRequests:
    def test_single_request_full_output(self, tmp_path):
        """A complete request record produces a transaction folder with all files."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        os.makedirs(temp_data_dir + "/bodies", exist_ok=True)
        os.makedirs(requests_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        # Create body file
        body_path = os.path.join(temp_data_dir, "bodies", "REQ_001.bin")
        with open(body_path, "wb") as f:
            f.write(b'{"key": "value"}')

        record = make_request_record(body_file="bodies/REQ_001.bin")
        write_jsonl(os.path.join(temp_data_dir, "requests.jsonl"), [record])

        timeline, detection_stats, stats = process_network_requests(
            os.path.join(temp_data_dir, "requests.jsonl"), temp_data_dir, requests_dir, output_dir
        )

        # Folder naming: {idx:03d}_{METHOD}_{sanitize_filename(domain)}
        domain = "example.com"
        expected_folder = f"000_GET_{sanitize_filename(domain)}"
        req_root = os.path.join(requests_dir, expected_folder)
        assert os.path.isdir(req_root)

        # transaction.json
        transaction_path = os.path.join(req_root, "transaction.json")
        assert os.path.exists(transaction_path)
        with open(transaction_path) as f:
            transaction = json.load(f)

        assert transaction["metadata"]["index"] == 0
        assert transaction["metadata"]["method"] == "GET"
        assert transaction["metadata"]["url"] == "https://example.com/api/data"
        assert transaction["metadata"]["status"] == 200
        assert "timing" in transaction["metadata"]
        assert "security" in transaction["metadata"]

        assert transaction["request"]["headers"]["Accept"] == "application/json"
        assert transaction["request"]["has_payload"] is False

        assert transaction["response"]["headers"]["Content-Type"] == "application/json"
        assert transaction["response"]["has_body"] is True

        # Body file copied
        res_body_files = [f for f in os.listdir(req_root) if f.startswith("res_body.")]
        assert len(res_body_files) == 1
        with open(os.path.join(req_root, res_body_files[0]), "rb") as f:
            assert f.read() == b'{"key": "value"}'

        # Timeline event
        assert len(timeline) == 1
        event = timeline[0]
        assert event["event_type"] == "network_request"
        assert event["method"] == "GET"
        assert event["url"] == "https://example.com/api/data"
        assert event["status"] == 200
        assert event["folder"] == f"requests/{expected_folder}"

        # Stats
        assert stats["methods"]["GET"] == 1
        assert stats["domains"]["example.com"] == 1
        assert stats["status_codes"]["200"] == 1
        assert stats["with_auth"] == 0
        assert stats["with_cookies"] == 0

    def test_post_data_saved(self, tmp_path):
        """Request with post_data creates a req_payload file."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        for d in (temp_data_dir, requests_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        record = make_request_record(method="POST", post_data='{"query": "hello"}')
        write_jsonl(os.path.join(temp_data_dir, "requests.jsonl"), [record])

        process_network_requests(os.path.join(temp_data_dir, "requests.jsonl"), temp_data_dir, requests_dir, output_dir)

        req_root = os.path.join(requests_dir, "000_POST_example.com")
        payload_files = [f for f in os.listdir(req_root) if f.startswith("req_payload.")]
        assert len(payload_files) == 1

    def test_auth_header_counted(self, tmp_path):
        """Requests with Authorization header increment with_auth stat."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        for d in (temp_data_dir, requests_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        record = make_request_record(auth_header="Bearer token123")
        write_jsonl(os.path.join(temp_data_dir, "requests.jsonl"), [record])

        _, _, stats = process_network_requests(
            os.path.join(temp_data_dir, "requests.jsonl"), temp_data_dir, requests_dir, output_dir
        )

        assert stats["with_auth"] == 1

    def test_cookies_counted(self, tmp_path):
        """Requests with cookies_sent_details increment with_cookies stat."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        for d in (temp_data_dir, requests_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        record = make_request_record(cookies_sent_details=[{"cookie": {"name": "sid"}, "blockedReasons": []}])
        write_jsonl(os.path.join(temp_data_dir, "requests.jsonl"), [record])

        _, _, stats = process_network_requests(
            os.path.join(temp_data_dir, "requests.jsonl"), temp_data_dir, requests_dir, output_dir
        )

        assert stats["with_cookies"] == 1

    def test_multiple_requests_indexed(self, tmp_path):
        """Multiple requests get sequential index-based folder names."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        for d in (temp_data_dir, requests_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        records = [
            make_request_record(request_id="REQ_001", url="https://a.com/api"),
            make_request_record(request_id="REQ_002", url="https://b.com/api", method="POST"),
            make_request_record(request_id="REQ_003", url="https://c.com/api", status=404),
        ]
        write_jsonl(os.path.join(temp_data_dir, "requests.jsonl"), records)

        timeline, _, stats = process_network_requests(
            os.path.join(temp_data_dir, "requests.jsonl"), temp_data_dir, requests_dir, output_dir
        )

        assert len(timeline) == 3
        assert os.path.isdir(os.path.join(requests_dir, "000_GET_a.com"))
        assert os.path.isdir(os.path.join(requests_dir, "001_POST_b.com"))
        assert os.path.isdir(os.path.join(requests_dir, "002_GET_c.com"))

        assert stats["methods"]["GET"] == 2
        assert stats["methods"]["POST"] == 1
        assert stats["status_codes"]["404"] == 1

    def test_malformed_line_writes_crash_report(self, tmp_path):
        """A malformed JSONL line produces a CRASH_REPORT file and processing continues."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        for d in (temp_data_dir, requests_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        # Write one valid + one malformed + one valid
        requests_path = os.path.join(temp_data_dir, "requests.jsonl")
        with open(requests_path, "w") as f:
            f.write(json.dumps(make_request_record(request_id="REQ_001")) + "\n")
            f.write("{INVALID JSON}\n")
            f.write(json.dumps(make_request_record(request_id="REQ_003", url="https://c.com/api")) + "\n")

        timeline, _, _ = process_network_requests(requests_path, temp_data_dir, requests_dir, output_dir)

        # Two valid requests processed
        assert len(timeline) == 2

        # Crash report written for index 1
        crash_files = [f for f in os.listdir(output_dir) if f.startswith("CRASH_REPORT_")]
        assert len(crash_files) == 1
        assert "001" in crash_files[0]

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing requests.jsonl returns empty timeline and zeroed stats."""
        timeline, detection_stats, stats = process_network_requests(
            str(tmp_path / "nonexistent.jsonl"), str(tmp_path), str(tmp_path / "requests"), str(tmp_path)
        )

        assert timeline == []
        assert detection_stats["request_detected"] == 0
        assert stats["methods"] == {}

    def test_no_body_file_skips_copy(self, tmp_path):
        """Request with body_file pointing to non-existent file doesn't crash."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        for d in (temp_data_dir, requests_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        record = make_request_record(body_file="bodies/MISSING.bin")
        write_jsonl(os.path.join(temp_data_dir, "requests.jsonl"), [record])

        # Should not crash
        process_network_requests(os.path.join(temp_data_dir, "requests.jsonl"), temp_data_dir, requests_dir, output_dir)

        req_root = os.path.join(requests_dir, "000_GET_example.com")
        # transaction.json should still exist
        assert os.path.exists(os.path.join(req_root, "transaction.json"))
        # No res_body file
        res_body_files = [f for f in os.listdir(req_root) if f.startswith("res_body.")]
        assert len(res_body_files) == 0

    def test_transaction_security_fields(self, tmp_path):
        """transaction.json security block correctly detects auth/challenge headers."""
        temp_data_dir = str(tmp_path / "data")
        requests_dir = str(tmp_path / "requests")
        output_dir = str(tmp_path / "session_dump")
        for d in (temp_data_dir, requests_dir, output_dir):
            os.makedirs(d, exist_ok=True)

        record = make_request_record(
            auth_header="Bearer token",
            response_headers={"WWW-Authenticate": 'Basic realm="Secure"'},
        )
        write_jsonl(os.path.join(temp_data_dir, "requests.jsonl"), [record])

        process_network_requests(os.path.join(temp_data_dir, "requests.jsonl"), temp_data_dir, requests_dir, output_dir)

        transaction_path = os.path.join(requests_dir, "000_GET_example.com", "transaction.json")
        with open(transaction_path) as f:
            transaction = json.load(f)

        assert transaction["metadata"]["security"]["has_authorization"] is True
        assert transaction["metadata"]["security"]["has_challenge"] is True
