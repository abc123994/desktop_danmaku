"""匿名 Firebase 投稿 Client。"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from firebase_auth import FIREBASE_API_KEY, FirebaseAuth, FirebaseAuthError, firebase_database_url

ROOM_ID = "main_v3"
MAX_SUBMISSION_TEXT_LENGTH = 2000


class SubmissionError(RuntimeError):
    """Firebase 投稿失敗。"""


@dataclass(frozen=True)
class SubmissionResult:
    request_id: str
    duplicate: bool


def new_request_id() -> str:
    import uuid
    return f"client-{uuid.uuid4()}"


def build_payload(text: str, *, request_id: str, auth_uid: str) -> dict[str, object]:
    normalized = text.strip()
    if not normalized:
        raise SubmissionError("投稿文字不可為空")
    if len(normalized) > MAX_SUBMISSION_TEXT_LENGTH:
        raise SubmissionError("投稿文字超過 2000 字元")
    if not request_id.strip():
        raise SubmissionError("request_id 不可為空")
    if not auth_uid.strip():
        raise SubmissionError("auth_uid 不可為空")
    return {
        "schema_version": 1,
        "request_id": request_id,
        "text": normalized,
        "created_at_ms": {".sv": "timestamp"},
        "auth_uid": auth_uid,
    }


def submit_text(
    text: str,
    *,
    auth: FirebaseAuth | None = None,
    database_url: str | None = None,
    room_id: str = ROOM_ID,
    request_id: str | None = None,
) -> SubmissionResult:
    auth_client = auth or FirebaseAuth(api_key=FIREBASE_API_KEY)
    stable_request_id = request_id or new_request_id()
    timestamp_ms = int(time.time() * 1000)
    endpoint = _submission_url(database_url or firebase_database_url(), room_id, timestamp_ms)
    try:
        payload = build_payload(text, request_id=stable_request_id, auth_uid=auth_client.local_id)
        try:
            status_code, raw = auth_client.request("PUT", endpoint, payload=payload, timeout=15)
        except FirebaseAuthError:
            existing = _read_existing(auth_client, endpoint)
            if existing is not None and str(existing.get("text", "")) == payload["text"]:
                return SubmissionResult(stable_request_id, True)
            raise
    except FirebaseAuthError as error:
        raise SubmissionError(str(error)) from error
    if status_code < 200 or status_code >= 300:
        raise SubmissionError(f"Firebase 投稿 HTTP {status_code}")
    return SubmissionResult(stable_request_id, False)


def _read_existing(auth: FirebaseAuth, endpoint: str) -> dict[str, object] | None:
    try:
        status, raw = auth.request("GET", endpoint, timeout=10)
    except FirebaseAuthError:
        return None
    if status < 200 or status >= 300 or raw.strip() in {b"", b"null"}:
        return None
    value = _json_object(raw)
    return value or None


def _submission_url(database_url: str, room_id: str, timestamp_ms: int) -> str:
    return (
        database_url.rstrip("/")
        + "/rooms/"
        + urllib.parse.quote(room_id, safe="")
        + "/danmaku_submissions/"
        + urllib.parse.quote(str(timestamp_ms), safe="")
        + ".json"
    )


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value: Any = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionError("Firebase 回應不是 JSON") from error
    return value if isinstance(value, dict) else {}
