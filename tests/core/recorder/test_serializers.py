"""Pure unit tests for serialization helpers and WebSocket opcode helpers.

These test the leaf-level utilities that all other compile functions depend on.
No fixtures, no async, no file I/O (except save_content which writes to tmp_path).
"""

import base64
import os

from automatiq.core.recorder.compile.serializers import (
    extract_cookies_sent,
    extract_cookies_set,
    get_header_val,
    make_serializable,
    sanitize_filename,
    save_content,
)
from automatiq.core.recorder.compile.websockets import _opcode_fallback_ext, _opcode_suffix

# ── sanitize_filename ─────────────────────────────────────────────────────────


class TestSanitizeFilename:
    def test_strips_https_scheme(self):
        assert sanitize_filename("https://example.com/path") == "example.com_path"

    def test_strips_http_scheme(self):
        assert sanitize_filename("http://example.com/path") == "example.com_path"

    def test_replaces_special_chars_with_underscore(self):
        result = sanitize_filename("https://example.com/path?query=1&sort=desc")
        assert "?" not in result
        assert "&" not in result
        assert "=" not in result
        # Each special char replaced with _
        assert result == "example.com_path_query_1_sort_desc"

    def test_truncates_to_100_chars(self):
        long_name = "https://example.com/" + "a" * 200
        result = sanitize_filename(long_name)
        assert len(result) <= 100

    def test_preserves_alphanumerics_dashes_dots(self):
        result = sanitize_filename("https://sub.example.com/file-name.v2.json")
        assert result == "sub.example.com_file-name.v2.json"

    def test_empty_string(self):
        assert sanitize_filename("") == ""


# ── get_header_val ────────────────────────────────────────────────────────────


class TestGetHeaderVal:
    def test_case_insensitive_lookup(self):
        headers = {"Content-Type": "application/json"}
        assert get_header_val(headers, "content-type") == "application/json"

    def test_mixed_case_headers(self):
        headers = {"X-Custom-HEADER": "value"}
        assert get_header_val(headers, "x-custom-header") == "value"

    def test_returns_none_for_missing_key(self):
        assert get_header_val({"Accept": "*/*"}, "authorization") is None

    def test_none_headers(self):
        assert get_header_val(None, "authorization") is None

    def test_empty_headers(self):
        assert get_header_val({}, "authorization") is None

    def test_exact_match(self):
        headers = {"Authorization": "Bearer token123"}
        assert get_header_val(headers, "Authorization") == "Bearer token123"


# ── extract_cookies_sent ──────────────────────────────────────────────────────


class TestExtractCookiesSent:
    def test_from_cookies_sent_details(self):
        item = {
            "cookies_sent_details": [
                {"cookie": {"name": "sid"}, "blockedReasons": []},
                {"cookie": {"name": "token"}, "blockedReasons": []},
            ],
            "headers": {},
        }
        assert extract_cookies_sent(item) == ["sid", "token"]

    def test_filters_blocked_cookies(self):
        item = {
            "cookies_sent_details": [
                {"cookie": {"name": "sid"}, "blockedReasons": []},
                {"cookie": {"name": "blocked"}, "blockedReasons": ["NotAllowlisted"]},
            ],
            "headers": {},
        }
        assert extract_cookies_sent(item) == ["sid"]

    def test_fallback_to_raw_cookie_header(self):
        item = {
            "cookies_sent_details": [],
            "headers": {"Cookie": "sid=abc; token=xyz; theme=dark"},
        }
        assert extract_cookies_sent(item) == ["sid", "theme", "token"]

    def test_empty_when_no_cookies(self):
        assert extract_cookies_sent({"cookies_sent_details": [], "headers": {}}) == []

    def test_sorted_output(self):
        item = {
            "cookies_sent_details": [
                {"cookie": {"name": "zebra"}, "blockedReasons": []},
                {"cookie": {"name": "apple"}, "blockedReasons": []},
            ],
            "headers": {},
        }
        assert extract_cookies_sent(item) == ["apple", "zebra"]


# ── extract_cookies_set ───────────────────────────────────────────────────────


class TestExtractCookiesSet:
    def test_from_set_cookie_header(self):
        item = {
            "response_data": {
                "headers": {"Set-Cookie": "sid=abc; Path=/\ntoken=xyz; Path=/"},
            },
        }
        assert extract_cookies_set(item) == ["sid", "token"]

    def test_empty_when_no_set_cookie(self):
        assert extract_cookies_set({"response_data": {"headers": {}}}) == []

    def test_none_response_data(self):
        assert extract_cookies_set({"response_data": None}) == []

    def test_single_cookie(self):
        item = {
            "response_data": {
                "headers": {"Set-Cookie": "session=abc123; HttpOnly; Path=/"},
            },
        }
        assert extract_cookies_set(item) == ["session"]


# ── make_serializable ─────────────────────────────────────────────────────────


class TestMakeSerializable:
    def test_primitives_pass_through(self):
        assert make_serializable("hello") == "hello"
        assert make_serializable(42) == 42
        assert make_serializable(3.14) == 3.14
        assert make_serializable(True) is True
        assert make_serializable(None) is None

    def test_bytes_become_empty_string(self):
        assert make_serializable(b"binary data") == ""

    def test_list_recursion(self):
        result = make_serializable([1, "two", b"bytes", None])
        assert result == [1, "two", "", None]

    def test_dict_recursion(self):
        result = make_serializable({"a": 1, "b": b"bytes", "c": {"d": b"x"}})
        assert result == {"a": 1, "b": "", "c": {"d": ""}}

    def test_non_serializable_becomes_string(self):
        class Custom:
            def __str__(self):
                return "custom_instance"

        result = make_serializable(Custom())
        assert result == "custom_instance"


# ── save_content ──────────────────────────────────────────────────────────────


class TestSaveContent:
    def test_string_content(self, tmp_path):
        path = str(tmp_path / "test.txt")
        save_content(path, "hello world")
        with open(path, "rb") as f:
            assert f.read() == b"hello world"

    def test_base64_content(self, tmp_path):
        path = str(tmp_path / "test.bin")
        encoded = base64.b64encode(b"binary data").decode("ascii")
        save_content(path, encoded, is_base64=True)
        with open(path, "rb") as f:
            assert f.read() == b"binary data"

    def test_none_content_is_noop(self, tmp_path):
        path = str(tmp_path / "noop.txt")
        save_content(path, None)
        assert not os.path.exists(path)

    def test_overwrites_existing_file(self, tmp_path):
        path = str(tmp_path / "overwrite.txt")
        save_content(path, "first")
        save_content(path, "second")
        with open(path, "rb") as f:
            assert f.read() == b"second"


# ── _opcode_suffix ────────────────────────────────────────────────────────────


class TestOpcodeSuffix:
    def test_text_frame_no_suffix(self):
        assert _opcode_suffix(1) == ""

    def test_binary_frame_no_suffix(self):
        assert _opcode_suffix(2) == ""

    def test_close_suffix(self):
        assert _opcode_suffix(8) == "_close"

    def test_ping_suffix(self):
        assert _opcode_suffix(9) == "_ping"

    def test_pong_suffix(self):
        assert _opcode_suffix(10) == "_pong"

    def test_continuation_suffix(self):
        assert _opcode_suffix(0) == "_continuation"

    def test_unknown_opcode(self):
        assert _opcode_suffix(99) == "_opcode99"


# ── _opcode_fallback_ext ──────────────────────────────────────────────────────


class TestOpcodeFallbackExt:
    def test_text_frame_returns_txt(self):
        assert _opcode_fallback_ext(1) == "txt"

    def test_close_returns_txt(self):
        assert _opcode_fallback_ext(8) == "txt"

    def test_binary_returns_bin(self):
        assert _opcode_fallback_ext(2) == "bin"

    def test_ping_returns_bin(self):
        assert _opcode_fallback_ext(9) == "bin"

    def test_pong_returns_bin(self):
        assert _opcode_fallback_ext(10) == "bin"

    def test_unknown_returns_bin(self):
        assert _opcode_fallback_ext(99) == "bin"
