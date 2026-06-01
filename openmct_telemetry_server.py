#!/usr/bin/env python3
import argparse
import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081
DEFAULT_HISTORY_LIMIT = 2000
DEFAULT_LOG_PATH = "pc_receiver.log"

OK_PREFIX = "[OK]"

FLOAT_FIELDS = {
    "roll",
    "pitch",
    "yaw",
    "rollspeed",
    "pitchspeed",
    "yawspeed",
    "x",
    "y",
    "z",
    "vx",
    "vy",
    "vz",
    "lat",
    "lon",
    "alt",
}

INT_FIELDS = {
    "seq",
    "boot_ms",
    "fix",
    "sats",
    "flags",
}

LINE_KV_PATTERN = re.compile(r"([a-zA-Z_]+)=([^\s]+)")


@dataclass
class Sample:
    timestamp_ms: int
    value: float
    source_line: str


class TelemetryStore:
    def __init__(self, history_limit: int) -> None:
        self.history_limit = history_limit
        self._series: Dict[str, Deque[Sample]] = defaultdict(lambda: deque(maxlen=history_limit))
        self._latest_fields: Dict[str, float] = {}
        self._latest_timestamp_ms = 0
        self._listeners: List["SSEClient"] = []
        self._lock = threading.Lock()

    def ingest_fields(self, timestamp_ms: int, fields: Dict[str, float], source_line: str) -> None:
        with self._lock:
            self._latest_timestamp_ms = max(self._latest_timestamp_ms, timestamp_ms)
            for key, value in fields.items():
                self._series[key].append(Sample(timestamp_ms=timestamp_ms, value=value, source_line=source_line))
                self._latest_fields[key] = value

            payload = {
                "timestamp_ms": timestamp_ms,
                "fields": fields,
                "source_line": source_line,
            }
            self._broadcast(payload)

    def series_names(self) -> List[str]:
        with self._lock:
            return sorted(self._series.keys())

    def latest_snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "timestamp_ms": self._latest_timestamp_ms,
                "fields": dict(self._latest_fields),
                "series": self.series_names(),
            }

    def history(self, series: str, since_ms: Optional[int] = None) -> List[Dict[str, object]]:
        with self._lock:
            values = list(self._series.get(series, []))

        if since_ms is not None:
            values = [sample for sample in values if sample.timestamp_ms >= since_ms]

        return [
            {
                "timestamp_ms": sample.timestamp_ms,
                "value": sample.value,
            }
            for sample in values
        ]

    def add_listener(self, client: "SSEClient") -> None:
        with self._lock:
            self._listeners.append(client)

    def remove_listener(self, client: "SSEClient") -> None:
        with self._lock:
            if client in self._listeners:
                self._listeners.remove(client)

    def _broadcast(self, payload: Dict[str, object]) -> None:
        dead_clients: List["SSEClient"] = []
        for client in self._listeners:
            if not client.send(payload):
                dead_clients.append(client)

        for client in dead_clients:
            if client in self._listeners:
                self._listeners.remove(client)


class SSEClient:
    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self.handler = handler

    def send(self, payload: Dict[str, object]) -> bool:
        try:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.handler.wfile.write(b"data: " + encoded + b"\n\n")
            self.handler.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


def normalize_value(key: str, raw_value: str) -> Optional[float]:
    cleaned = raw_value.rstrip(",")

    if key in INT_FIELDS:
        try:
            return float(int(cleaned, 0))
        except ValueError:
            return None

    if key in FLOAT_FIELDS:
        try:
            return float(cleaned)
        except ValueError:
            return None

    return None


def parse_ok_line(line: str) -> Optional[Dict[str, float]]:
    if OK_PREFIX not in line:
        return None

    parsed: Dict[str, float] = {}
    for key, raw_value in LINE_KV_PATTERN.findall(line):
        normalized = normalize_value(key, raw_value)
        if normalized is not None:
            parsed[key] = normalized

    return parsed or None


def choose_timestamp_ms(fields: Dict[str, float]) -> int:
    if "boot_ms" in fields:
        return int(fields["boot_ms"])

    return int(time.time() * 1000)


def follow_log_file(log_path: Path, store: TelemetryStore, poll_interval_s: float = 0.2) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)

    with log_path.open("r", encoding="utf-8", errors="replace") as log_file:
        log_file.seek(0, os.SEEK_END)

        while True:
            line = log_file.readline()
            if not line:
                time.sleep(poll_interval_s)
                continue

            fields = parse_ok_line(line)
            if not fields:
                continue

            timestamp_ms = choose_timestamp_ms(fields)
            store.ingest_fields(timestamp_ms=timestamp_ms, fields=fields, source_line=line.strip())


class OpenMCTRequestHandler(BaseHTTPRequestHandler):
    store: TelemetryStore = None  # type: ignore[assignment]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._write_json({"ok": True, "service": "openmct-telemetry-server"})
            return

        if path == "/schema":
            self._write_json(
                {
                    "series": self.store.series_names(),
                    "latest": self.store.latest_snapshot(),
                    "endpoints": {
                        "latest": "/latest",
                        "history": "/history?series=<name>&since_ms=<optional>",
                        "events": "/events",
                    },
                }
            )
            return

        if path == "/latest":
            self._write_json(self.store.latest_snapshot())
            return

        if path == "/history":
            params = parse_qs(parsed.query)
            series = params.get("series", [None])[0]
            if not series:
                self._write_json({"error": "missing required query param: series"}, status=HTTPStatus.BAD_REQUEST)
                return

            since_ms_raw = params.get("since_ms", [None])[0]
            since_ms = int(since_ms_raw) if since_ms_raw else None
            self._write_json(
                {
                    "series": series,
                    "points": self.store.history(series=series, since_ms=since_ms),
                }
            )
            return

        if path == "/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            client = SSEClient(self)
            self.store.add_listener(client)

            try:
                initial_payload = {
                    "timestamp_ms": int(time.time() * 1000),
                    "fields": self.store.latest_snapshot().get("fields", {}),
                    "source_line": "initial_snapshot",
                }
                client.send(initial_payload)
                while True:
                    time.sleep(1.0)
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.store.remove_listener(client)
            return

        self._write_json({"error": f"unknown path: {path}"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _write_json(self, payload: Dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(encoded)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tail PC receiver [OK] logs and expose them to Open MCT as HTTP/SSE telemetry."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument("--log-path", default=DEFAULT_LOG_PATH, help="path to the PC receiver log file")
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT, help="max samples to retain per series")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = TelemetryStore(history_limit=args.history_limit)
    OpenMCTRequestHandler.store = store

    log_path = Path(args.log_path).resolve()
    tail_thread = threading.Thread(target=follow_log_file, args=(log_path, store), daemon=True)
    tail_thread.start()

    server = ThreadingHTTPServer((args.host, args.port), OpenMCTRequestHandler)
    print(
        f"openmct telemetry server listening on http://{args.host}:{args.port} "
        f"(tailing {log_path})",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
