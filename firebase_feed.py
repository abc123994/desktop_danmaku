"""Firebase RTDB SSE 即時 Feed Client。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from firebase_auth import FirebaseAuth, FirebaseAuthError, firebase_database_url

ROOM_ID = "main_v3"
POLL_RECONNECT_SECONDS = 2.0
MAX_SSE_LINE_BYTES = 2 * 1024 * 1024


class FirebaseFeedError(RuntimeError):
    """Firebase feed 格式或連線錯誤。"""


def feed_url(database_url: str | None = None, room_id: str = ROOM_ID) -> str:
    database_url = database_url or firebase_database_url()
    return (
        database_url.rstrip("/")
        + "/rooms/"
        + urllib.parse.quote(room_id, safe="")
        + "/danmaku_submissions.json"
    )


def iter_sse_events(response: Any) -> Iterator[tuple[str, str]]:
    event_name = ""
    data_lines: list[str] = []
    while True:
        raw_line = response.readline()
        if not raw_line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            return
        if isinstance(raw_line, bytes):
            if len(raw_line) > MAX_SSE_LINE_BYTES:
                raise FirebaseFeedError("Firebase SSE 單行過大")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        else:
            line = str(raw_line).rstrip("\r\n")
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name, data_lines = "", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


class FirebaseFeedClient:
    def __init__(
        self,
        *,
        auth: FirebaseAuth | None = None,
        database_url: str | None = None,
        room_id: str = ROOM_ID,
        opener: Callable[..., Any] | None = None,
        on_message: Callable[[dict[str, object]], None] | None = None,
        on_status: Callable[[str, bool], None] | None = None,
    ) -> None:
        self.auth = auth or FirebaseAuth()
        self.url = feed_url(database_url, room_id)
        self._opener = opener or urllib.request.urlopen
        self._on_message = on_message or (lambda _payload: None)
        self._on_status = on_status or (lambda _message, _connected: None)
        self._messages: dict[str, dict[str, object]] = {}
        self._seen: set[str] = set()
        self._seen_order: list[str] = []
        self._max_seen = 5000
        self.stop_requested = threading.Event()
        self.thread: threading.Thread | None = None
        self._started = False
        self._startup_snapshot_pending = True

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.thread = threading.Thread(target=self._run, name="firebase-feed-sse", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop_requested.is_set():
            try:
                self._stream_once()
                self._on_status("Firebase SSE 已連線", True)
            except FirebaseAuthError as error:
                self._on_status(f"Firebase 認證失敗：{error}", False)
            except (FirebaseFeedError, OSError, TimeoutError, urllib.error.URLError) as error:
                self._on_status(f"Firebase SSE 斷線：{_short_error(error)}", False)
            if self.stop_requested.wait(POLL_RECONNECT_SECONDS):
                break
        self._on_status("Firebase SSE 已停止", False)

    def _stream_once(self) -> None:
        for attempt in range(2):
            token = self.auth.id_token()
            request = urllib.request.Request(
                _with_query(self.url, "auth", token),
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
                method="GET",
            )
            try:
                with self._opener(request, timeout=None) as response:  # type: ignore[arg-type]
                    status = int(getattr(response, "status", 200))
                    if status < 200 or status >= 300:
                        raise FirebaseFeedError(f"Firebase SSE HTTP {status}")
                    for event_name, raw in iter_sse_events(response):
                        if self.stop_requested.is_set():
                            return
                        self._handle_event(event_name, raw)
                return
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 0:
                    self.auth.force_refresh()
                    continue
                raise FirebaseFeedError(f"Firebase SSE HTTP {error.code}") from error
        raise FirebaseFeedError("Firebase SSE 認證重試失敗")

    def _handle_event(self, event_name: str, raw: str) -> None:
        if event_name in {"keep-alive", ""} and not raw.strip():
            return
        if event_name in {"cancel", "auth_revoked"}:
            raise FirebaseFeedError(f"Firebase SSE event {event_name}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise FirebaseFeedError("Firebase SSE data 不是 JSON") from error
        if not isinstance(payload, dict):
            raise FirebaseFeedError("Firebase SSE payload 格式錯誤")
        self._apply(str(payload.get("path", "/")), payload.get("data"), event_name)
        self._on_status("Firebase 即時連線", True)

    def _apply(self, path: str, data: object, event_name: str = "put") -> None:
        clean = path.strip("/")
        if clean == "":
            if isinstance(data, dict):
                if event_name != "patch":
                    self._messages.clear()
                for key, value in data.items():
                    key = str(key)
                    if value is None:
                        self._messages.pop(key, None)
                    elif isinstance(value, dict):
                        if event_name == "patch" and key in self._messages:
                            self._messages[key].update(value)
                        else:
                            self._messages[key] = dict(value)
            if self._startup_snapshot_pending:
                self._mark_current_as_seen()
                self._startup_snapshot_pending = False
                return
            self._emit_ready()
            return

        parts = clean.split("/")
        key = parts[0]
        if len(parts) == 1:
            if data is None:
                self._messages.pop(key, None)
            elif isinstance(data, dict):
                if event_name == "patch" and key in self._messages:
                    self._messages[key].update(data)
                else:
                    self._messages[key] = dict(data)
        else:
            current = self._messages.setdefault(key, {})
            if len(parts) == 2:
                if data is None:
                    current.pop(parts[1], None)
                else:
                    current[parts[1]] = data
            elif isinstance(data, dict):
                current.update(data)
        self._emit_ready()

    def _mark_current_as_seen(self) -> None:
        for key, value in self._messages.items():
            self._remember_id(_message_id(key, value))

    def _emit_ready(self) -> None:
        ready: list[dict[str, object]] = []
        for key, value in self._messages.items():
            message = dict(value)
            request_id = _message_id(key, message)
            text = str(message.get("text", "")).strip()
            created_at_ms = message.get("created_at_ms")
            if (
                not request_id
                or request_id in self._seen
                or not text
                or isinstance(created_at_ms, bool)
                or not isinstance(created_at_ms, (int, float))
            ):
                continue
            message["request_id"] = request_id
            message["created_at_ms"] = int(created_at_ms)
            ready.append(message)
        ready.sort(key=lambda item: (int(item["created_at_ms"]), str(item["request_id"])))
        for message in ready:
            self._remember_id(str(message["request_id"]))
            self._on_message(message)

    def _remember_id(self, request_id: str) -> None:
        if not request_id or request_id in self._seen:
            return
        self._seen.add(request_id)
        self._seen_order.append(request_id)
        while len(self._seen_order) > self._max_seen:
            self._seen.discard(self._seen_order.pop(0))

    def stop(self) -> None:
        if self.stop_requested.is_set():
            return
        self.stop_requested.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.5)


def _message_id(key: str, value: dict[str, object]) -> str:
    return str(value.get("request_id", key)).strip()


def _short_error(error: BaseException) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:180]


def _with_query(url: str, key: str, value: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [(name, item) for name, item in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if name != key]
    query.append((key, value))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )
