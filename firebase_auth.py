"""Firebase Anonymous Auth 與 ID Token 管理。

Client 只使用 Firebase Web API Key。API Key 與 Database URL 是公開 client
設定，不是 Admin 憑證；Service Account JSON 絕不放在 Windows 包內。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable


FIREBASE_API_KEY = "AIzaSyBAL9lJbgkfiFG0xEwXITCeAQUDpNDI11U"
DEFAULT_FIREBASE_DATABASE_URL = (
    "https://dc-danmaku-default-rtdb.asia-southeast1.firebasedatabase.app"
)
FIREBASE_DATABASE_URL_ENV = "DANMAKU_FIREBASE_DATABASE_URL"


def firebase_database_url() -> str:
    """取得手動設定的 URL；未設定或空白時使用預設 Firebase URL。"""
    configured = os.environ.get(FIREBASE_DATABASE_URL_ENV, "").strip()
    return configured or DEFAULT_FIREBASE_DATABASE_URL


# 相容既有 import；實際呼叫會透過 firebase_database_url() 讀取最新設定。
FIREBASE_DATABASE_URL = DEFAULT_FIREBASE_DATABASE_URL
MAX_RESPONSE_BYTES = 512 * 1024


class FirebaseAuthError(RuntimeError):
    """Anonymous Auth 或 token 更新失敗。"""


@dataclass(frozen=True)
class AuthSession:
    id_token: str
    refresh_token: str
    local_id: str
    expires_at: float


class FirebaseAuth:
    def __init__(
        self,
        *,
        api_key: str = FIREBASE_API_KEY,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.api_key = api_key
        self._opener = opener or urllib.request.urlopen
        self._lock = threading.RLock()
        self._session: AuthSession | None = None

    @property
    def local_id(self) -> str:
        with self._lock:
            return self._ensure_session_locked().local_id

    def id_token(self) -> str:
        with self._lock:
            return self._ensure_session_locked().id_token

    def force_refresh(self) -> AuthSession:
        with self._lock:
            self._session = None
            return self._ensure_session_locked()

    def _ensure_session_locked(self) -> AuthSession:
        now = time.time()
        if self._session is not None and self._session.expires_at - now > 60:
            return self._session

        if self._session is not None and self._session.refresh_token:
            try:
                self._session = self._refresh_locked(self._session.refresh_token)
                return self._session
            except FirebaseAuthError:
                self._session = None

        self._session = self._sign_in_anonymously_locked()
        return self._session

    def _sign_in_anonymously_locked(self) -> AuthSession:
        url = (
            "https://identitytoolkit.googleapis.com/v1/accounts:signUp?"
            + urllib.parse.urlencode({"key": self.api_key})
        )
        payload = {"returnSecureToken": True}
        result = self._post_json(url, payload)
        return self._parse_session(result, refresh_token_key="refreshToken")

    def _refresh_locked(self, refresh_token: str) -> AuthSession:
        url = (
            "https://securetoken.googleapis.com/v1/token?"
            + urllib.parse.urlencode({"key": self.api_key})
        )
        body = urllib.parse.urlencode(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        result = self._open_json(request)
        try:
            return AuthSession(
                id_token=str(result["id_token"]),
                refresh_token=str(result.get("refresh_token", refresh_token)),
                local_id=str(result["user_id"]),
                expires_at=time.time() + int(result.get("expires_in", 3600)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FirebaseAuthError("Firebase token refresh 回應格式錯誤") from error

    def _parse_session(
        self,
        result: dict[str, object],
        *,
        refresh_token_key: str,
    ) -> AuthSession:
        try:
            return AuthSession(
                id_token=str(result["idToken"]),
                refresh_token=str(result[refresh_token_key]),
                local_id=str(result["localId"]),
                expires_at=time.time() + int(result.get("expiresIn", 3600)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FirebaseAuthError("Firebase anonymous auth 回應格式錯誤") from error

    def _post_json(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict[str, object]:
        try:
            with self._opener(request, timeout=15) as response:  # type: ignore[arg-type]
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
            raise FirebaseAuthError(_error_text(raw, error.code)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise FirebaseAuthError("Firebase Auth 無法連線") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FirebaseAuthError("Firebase Auth 回應過大")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FirebaseAuthError("Firebase Auth 回應不是 JSON") from error
        if not isinstance(value, dict):
            raise FirebaseAuthError("Firebase Auth 回應格式錯誤")
        return value

    def request(
        self,
        method: str,
        url: str,
        *,
        payload: object | None = None,
        timeout: float = 15,
    ) -> tuple[int, bytes]:
        """以目前 ID Token 發出請求；401 會重新登入後重試一次。"""

        for attempt in range(2):
            token = self.id_token()
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            headers = {
                "Accept": "application/json",
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(
                _with_query(url, "auth", token),
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with self._opener(request, timeout=timeout) as response:  # type: ignore[arg-type]
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    status = int(getattr(response, "status", 200))
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise FirebaseAuthError("Firebase 回應過大")
                return status, raw
            except urllib.error.HTTPError as error:
                raw = error.read(MAX_RESPONSE_BYTES + 1)
                if error.code == 401 and attempt == 0:
                    self.force_refresh()
                    continue
                raise FirebaseAuthError(_error_text(raw, error.code)) from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise FirebaseAuthError("Firebase 請求無法連線") from error
        raise FirebaseAuthError("Firebase Auth 重試失敗")


def _error_text(raw: bytes, status: int) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"Firebase HTTP {status}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return f"Firebase HTTP {status}: {message}"
    return f"Firebase HTTP {status}"


def _with_query(url: str, key: str, value: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(name, item) for name, item in query if name != key]
    query.append((key, value))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )
