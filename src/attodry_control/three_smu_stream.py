"""Loopback-only publication of formal Three-SMU samples.

The acquisition CLI remains the only owner of SMU hardware.  This small server
only republishes samples that the session has already recorded and emitted via
``on_sample``.  It deliberately binds to 127.0.0.1, never receives commands,
and carries no hardware addresses or configuration secrets.
"""

from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Empty, Queue
from threading import RLock, Thread
from typing import Any

from .three_smu import ThreeSmuSample
from .three_smu_config import ThreeSmuScanPlan, active_smu_roles


LIVE_STREAM_HOST = "127.0.0.1"
LIVE_STREAM_PORT = 8765
LIVE_STREAM_PATH = "/events"
LIVE_STREAM_URL = f"http://{LIVE_STREAM_HOST}:{LIVE_STREAM_PORT}{LIVE_STREAM_PATH}"


class ThreeSmuLivePublisher:
    """Publish the current run's in-memory events to local notebook clients."""

    def __init__(
        self,
        *,
        host: str = LIVE_STREAM_HOST,
        port: int = LIVE_STREAM_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self._lock = RLock()
        self._history: list[tuple[str, dict[str, Any]]] = []
        self._subscribers: set[Queue[tuple[str, dict[str, Any]]]] = set()
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None
        self._closed = False

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}{LIVE_STREAM_PATH}"

    def start(self, plan: ThreeSmuScanPlan, *, total_samples: int) -> None:
        """Bind before hardware is opened, so a bind failure causes zero SMU I/O."""

        if self._server is not None:
            raise RuntimeError("Three-SMU live publisher is already running")
        publisher = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - required HTTP handler name
                if self.path != LIVE_STREAM_PATH:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                subscriber = publisher._subscribe()
                try:
                    while True:
                        try:
                            event, payload = subscriber.get(timeout=1.0)
                        except Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        wire = json.dumps(payload, separators=(",", ":"))
                        self.wfile.write(
                            f"event: {event}\ndata: {wire}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                        if event in {"run_finished", "run_failed"}:
                            return
                except (BrokenPipeError, ConnectionResetError):
                    return
                finally:
                    publisher._unsubscribe(subscriber)

            def log_message(self, _format: str, *_args: object) -> None:
                """Keep the acquisition terminal free of HTTP access logs."""

        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.daemon_threads = True
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        self._publish(
            "run_started",
            {
                "mode": plan.mode.value,
                "active_roles": list(active_smu_roles(plan)),
                "total_samples": total_samples,
                "samples_per_point": plan.samples_per_point,
            },
        )

    def publish_sample(self, sample: ThreeSmuSample) -> None:
        self._publish("sample", _sample_payload(sample))

    def finish(self, *, status: str, error: str | None = None) -> None:
        event = "run_finished" if status == "completed" else "run_failed"
        self._publish(event, {"status": status, "error": error})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _publish(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            item = (event, payload)
            self._history.append(item)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(item)

    def _subscribe(self) -> Queue[tuple[str, dict[str, Any]]]:
        subscriber: Queue[tuple[str, dict[str, Any]]] = Queue()
        with self._lock:
            for item in self._history:
                subscriber.put(item)
            self._subscribers.add(subscriber)
        return subscriber

    def _unsubscribe(self, subscriber: Queue[tuple[str, dict[str, Any]]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)


def _sample_payload(sample: ThreeSmuSample) -> dict[str, Any]:
    return {
        "point_index": sample.point_index,
        "repeat_index": sample.repeat_index,
        "segment": sample.segment,
        "elapsed_s": sample.elapsed_s,
        "coordinates": dict(sample.coordinates),
        "readings": {
            role: {
                "timestamp": timed.timestamp,
                **asdict(timed.reading),
                "resistance_ohm": timed.reading.resistance_ohm,
            }
            for role, timed in sample.readings.items()
        },
        "clean": sample.clean,
        "problems": list(sample.problems),
    }
