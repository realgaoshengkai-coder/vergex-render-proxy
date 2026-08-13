#!/usr/bin/env python3
"""Expose CLIProxyAPI while repairing VergeX Responses compatibility."""

from __future__ import annotations

import http.client
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN = ("0.0.0.0", int(os.environ.get("PORT", "10000")))
UPSTREAM = ("127.0.0.1", 8317)
MAX_REQUEST_BYTES = 20 * 1024 * 1024
HOP_BY_HOP = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def normalize_sse(body: bytes) -> bytes:
    normalized = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    normalized = re.sub(br"\n{3,}", b"\n\n", normalized)
    return normalized.rstrip(b"\n") + b"\n\n"


def normalize_request(body: bytes) -> tuple[bytes, bool]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body, False

    items = payload.get("input")
    changed = False
    if isinstance(items, list):
        completed = {
            item.get("call_id")
            for item in items
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        }
        normalized = [
            item
            for item in items
            if not (
                isinstance(item, dict)
                and item.get("type") == "function_call"
                and item.get("call_id") not in completed
            )
        ]
        if len(normalized) != len(items):
            payload["input"] = normalized
            changed = True

    requested_stream = payload.get("stream") is True
    if requested_stream:
        payload["stream"] = False
        changed = True
    if not changed:
        return body, requested_stream
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), requested_stream


def synthesize_sse(body: bytes) -> bytes:
    response = json.loads(body)
    event_type = {
        "completed": "response.completed",
        "failed": "response.failed",
        "incomplete": "response.incomplete",
    }.get(response.get("status"))
    if event_type is None:
        raise ValueError(f"unexpected response status: {response.get('status')!r}")
    event = {"type": event_type, "response": response, "sequence_number": 0}
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    return b"event: " + event_type.encode() + b"\ndata: " + data + b"\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._proxy(b"", False)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "invalid Content-Length")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        is_responses = self.path.split("?", 1)[0] == "/v1/responses"
        requested_stream = False
        if is_responses:
            body, requested_stream = normalize_request(body)
        self._proxy(body, requested_stream)

    def _proxy(self, body: bytes, requested_stream: bool) -> None:
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != "accept-encoding"
        }
        headers["Accept-Encoding"] = "identity"
        upstream = http.client.HTTPConnection(*UPSTREAM, timeout=310)
        try:
            upstream.request(self.command, self.path, body=body or None, headers=headers)
            response = upstream.getresponse()
            response_body = response.read()
            content_type = response.getheader("Content-Type", "application/octet-stream")
            if response.status == 200 and requested_stream:
                response_body = synthesize_sse(response_body)
                content_type = "text/event-stream"
            elif response.status == 200 and content_type.split(";", 1)[0].strip() == "text/event-stream":
                response_body = normalize_sse(response_body)

            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            for key, value in response.getheaders():
                if key.lower().startswith("x-"):
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_body)
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException) as exc:
            try:
                self.send_error(502, type(exc).__name__)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
        finally:
            upstream.close()

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def self_test() -> None:
    assert normalize_sse(b"event: done\r\ndata: {}\r\n\r\n\r\n") == b"event: done\ndata: {}\n\n"
    orphan = b'{"input":[{"role":"user","content":"a"},{"type":"function_call","call_id":"plan"}]}'
    assert json.loads(normalize_request(orphan)[0])["input"] == [{"role": "user", "content": "a"}]
    paired = b'{"input":[{"type":"function_call","call_id":"call_1"},{"type":"function_call_output","call_id":"call_1","output":"ok"}]}'
    assert normalize_request(paired) == (paired, False)
    streamed, requested_stream = normalize_request(b'{"input":"x","stream":true}')
    assert requested_stream and json.loads(streamed)["stream"] is False
    completed = b'{"id":"resp_1","status":"completed","output":[]}'
    assert b"response.completed" in synthesize_sse(completed)


if __name__ == "__main__":
    if os.environ.get("SELF_TEST") == "1":
        self_test()
        print("PASS")
    else:
        ThreadingHTTPServer(LISTEN, Handler).serve_forever()
