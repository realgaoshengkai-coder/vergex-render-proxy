#!/usr/bin/env python3
"""Expose CLIProxyAPI while repairing VergeX Responses compatibility."""

from __future__ import annotations

import http.client
import json
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LISTEN = ("0.0.0.0", int(os.environ.get("PORT", "10000")))
UPSTREAM = ("127.0.0.1", 8317)
MAX_REQUEST_BYTES = 20 * 1024 * 1024
MAX_SNAPSHOT_AGE_SECONDS = 75
MIN_ORDER_NOTIONAL = 12.0
MIN_STOP_PCT = 0.005
MAX_STOP_PCT = 0.015
MIN_NET_RR = 1.8
ROUND_TRIP_COST_PCT = 0.002
OPEN_TOOLS = {"open_long", "open_short"}
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


def normalize_request(body: bytes) -> tuple[bytes, bool, dict | None]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body, False, None

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
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
        payload["reasoning"] = reasoning
    if reasoning.get("effort") != "low":
        reasoning["effort"] = "low"
        changed = True
    if not changed:
        return body, requested_stream, payload
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
        requested_stream,
        payload,
    )


def _request_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_request_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(_request_text(item) for item in value.values())
    return ""


def _latest_snapshot_at(text: str) -> datetime | None:
    values = re.findall(r"Timestamp\*\*:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC", text)
    if not values:
        return None
    return max(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) for value in values)


def _market_price(text: str, symbol: str) -> float | None:
    match = re.search(
        rf"###\s+{re.escape(symbol)}\b(?:(?!\n###\s).)*?Current Price:\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        re.S,
    )
    return float(match.group(1)) if match else None


def _equity(text: str) -> float | None:
    match = re.search(r"\|\s*Total Equity\s*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|", text)
    return float(match.group(1)) if match else None


def _notional(args: dict, entry: float, equity: float | None) -> float | None:
    quantity = args.get("quantity")
    leverage = args.get("leverage", 1)
    if not isinstance(quantity, (int, float)) or not isinstance(leverage, (int, float)):
        return None
    kind = str(args.get("quantity_type", "")).upper()
    if kind in {"USDT", "USD"}:
        return float(quantity) * float(leverage)
    if kind == "PERCENT" and equity is not None:
        return equity * float(quantity) / 100 * float(leverage)
    if kind in {"COINS", "COIN"}:
        return float(quantity) * entry
    return None


def _open_guard_reason(args: dict, text: str, now: datetime) -> str | None:
    tool = str(args.pop("_tool", "") or "").lower()
    symbol = str(args.get("symbol", ""))
    side = tool or str(args.get("side", "")).lower()
    snapshot_at = _latest_snapshot_at(text)
    if snapshot_at is None or (now - snapshot_at).total_seconds() > MAX_SNAPSHOT_AGE_SECONDS:
        return "行情快照已超过75秒"

    price = args.get("price")
    entry = float(price) if isinstance(price, (int, float)) and price > 0 else _market_price(text, symbol)
    stop = args.get("stop_loss")
    target = args.get("take_profit")
    if entry is None or not isinstance(stop, (int, float)) or not isinstance(target, (int, float)):
        return "缺少可校验的入场价、止损或目标"

    is_long = side in {"open_long", "long", "buy"}
    risk = entry - float(stop) if is_long else float(stop) - entry
    reward = float(target) - entry if is_long else entry - float(target)
    if risk <= 0 or reward <= 0:
        return "止损或目标方向错误"
    stop_pct = risk / entry
    if not MIN_STOP_PCT <= stop_pct <= MAX_STOP_PCT:
        return f"止损距离{stop_pct:.2%}不在0.5%至1.5%"
    net_rr = (reward - ROUND_TRIP_COST_PCT * entry) / risk
    if net_rr < MIN_NET_RR:
        return f"扣除0.20%成本后仅{net_rr:.2f}R"

    equity = _equity(text)
    notional = _notional(args, entry, equity)
    if notional is None or notional < MIN_ORDER_NOTIONAL:
        shown = "未知" if notional is None else f"{notional:.2f} USDT"
        return f"舍入前名义价值{shown}，需至少12 USDT"
    if equity is None or notional * (stop_pct + ROUND_TRIP_COST_PCT) > equity * 0.10:
        return "含成本最大损失超过权益10%或权益不可校验"
    return None


def guard_response(body: bytes, request_payload: dict | None, now: datetime | None = None) -> bytes:
    if request_payload is None:
        return body
    response = json.loads(body)
    text = _request_text(request_payload.get("input"))
    current = now or datetime.now(timezone.utc)
    changed = False
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = str(item.get("name", "")).lower()
        if name not in OPEN_TOOLS:
            continue
        try:
            args = json.loads(item.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError):
            args = {}
        args["_tool"] = name
        reason = _open_guard_reason(args, text, current)
        if reason is None:
            continue
        item["name"] = "watch"
        item["arguments"] = json.dumps(
            {
                "symbol": args.get("symbol", "UNKNOWN"),
                "reasoning": f"确定性开仓校验拦截：{reason}",
                "confidence": min(float(args.get("confidence", 0) or 0), 99),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        changed = True
    if not changed:
        return body
    return json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()


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
        request_payload = None
        if is_responses:
            body, requested_stream, request_payload = normalize_request(body)
        self._proxy(body, requested_stream, request_payload)

    def _proxy(self, body: bytes, requested_stream: bool, request_payload: dict | None = None) -> None:
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
            if response.status == 200 and request_payload is not None:
                response_body = guard_response(response_body, request_payload)
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
    normalized_paired = normalize_request(paired)
    assert normalized_paired[1] is False and normalized_paired[2]["reasoning"]["effort"] == "low"
    streamed, requested_stream, _ = normalize_request(b'{"input":"x","stream":true}')
    assert requested_stream and json.loads(streamed)["stream"] is False
    completed = b'{"id":"resp_1","status":"completed","output":[]}'
    assert b"response.completed" in synthesize_sse(completed)

    prompt = """**Timestamp**: 2026-08-16 00:00:00 UTC
| Total Equity | 20 |
### TESTUSDC
- Current Price: 100
"""
    request = {"input": prompt}
    good = {
        "type": "function_call",
        "name": "open_long",
        "arguments": json.dumps(
            {
                "symbol": "TESTUSDC",
                "price": 0,
                "stop_loss": 99.4,
                "take_profit": 101.5,
                "quantity": 10,
                "quantity_type": "USDT",
                "leverage": 2,
                "confidence": 80,
            }
        ),
    }
    response = lambda item: json.dumps({"status": "completed", "output": [item]}).encode()
    now = datetime(2026, 8, 16, 0, 0, 30, tzinfo=timezone.utc)
    assert json.loads(guard_response(response(good.copy()), request, now))["output"][0]["name"] == "open_long"
    too_tight = good.copy()
    too_tight["arguments"] = good["arguments"].replace("99.4", "99.8")
    assert json.loads(guard_response(response(too_tight), request, now))["output"][0]["name"] == "watch"
    stale = datetime(2026, 8, 16, 0, 2, tzinfo=timezone.utc)
    assert json.loads(guard_response(response(good.copy()), request, stale))["output"][0]["name"] == "watch"


if __name__ == "__main__":
    if os.environ.get("SELF_TEST") == "1":
        self_test()
        print("PASS")
    else:
        ThreadingHTTPServer(LISTEN, Handler).serve_forever()
