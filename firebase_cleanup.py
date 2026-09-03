"""清理 Firebase RTDB 中過期的 main_v3 投稿。"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
from collections.abc import Mapping

from firebase_auth import FirebaseAuth, FirebaseAuthError, firebase_database_url

ROOM_ID = "main_v3"
DEFAULT_KEEP_HOURS = 24.0
MILLISECONDS_PER_HOUR = 60 * 60 * 1000


class CleanupError(RuntimeError):
    """清理失敗。"""


def submissions_url(database_url: str | None = None, room_id: str = ROOM_ID) -> str:
    return (
        (database_url or firebase_database_url()).rstrip("/")
        + "/rooms/"
        + urllib.parse.quote(room_id, safe="")
        + "/danmaku_submissions.json"
    )


def expired_keys(
    submissions: object,
    *,
    now_ms: int,
    keep_hours: float,
) -> list[str]:
    if keep_hours <= 0:
        raise CleanupError("保留時間必須大於 0 小時")
    if not isinstance(submissions, Mapping):
        return []
    cutoff_ms = now_ms - int(keep_hours * MILLISECONDS_PER_HOUR)
    keys: list[str] = []
    for key in submissions:
        key_text = str(key)
        if key_text.isdigit() and int(key_text) < cutoff_ms:
            keys.append(key_text)
    return sorted(keys, key=int)


def cleanup_expired(
    *,
    auth: FirebaseAuth,
    database_url: str | None = None,
    room_id: str = ROOM_ID,
    keep_hours: float = DEFAULT_KEEP_HOURS,
    now_ms: int | None = None,
    dry_run: bool = False,
) -> list[str]:
    url = submissions_url(database_url, room_id)
    try:
        status, raw = auth.request("GET", url, timeout=15)
    except FirebaseAuthError as error:
        raise CleanupError(str(error)) from error
    if status < 200 or status >= 300:
        raise CleanupError(f"Firebase 讀取 HTTP {status}")
    try:
        submissions = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError) as error:
        raise CleanupError("Firebase 清理資料不是 JSON") from error

    keys = expired_keys(
        submissions,
        now_ms=now_ms if now_ms is not None else int(time.time() * 1000),
        keep_hours=keep_hours,
    )
    if dry_run:
        return keys
    for key in keys:
        try:
            delete_status, _ = auth.request("DELETE", _child_url(url, key), timeout=15)
        except FirebaseAuthError as error:
            raise CleanupError(f"刪除 {key} 失敗：{error}") from error
        if delete_status < 200 or delete_status >= 300:
            raise CleanupError(f"刪除 {key} HTTP {delete_status}")
    return keys


def _child_url(collection_url: str, key: str) -> str:
    return collection_url.removesuffix(".json") + "/" + urllib.parse.quote(key, safe="") + ".json"


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 Firebase main_v3 過期投稿")
    parser.add_argument("--keep-hours", type=float, default=DEFAULT_KEEP_HOURS)
    parser.add_argument("--dry-run", action="store_true", help="只列出，不刪除")
    args = parser.parse_args()
    try:
        keys = cleanup_expired(
            auth=FirebaseAuth(),
            keep_hours=args.keep_hours,
            dry_run=args.dry_run,
        )
    except CleanupError as error:
        print(f"清理失敗：{error}")
        return 1
    action = "預計刪除" if args.dry_run else "已刪除"
    print(f"{action} {len(keys)} 筆過期資料（保留 {args.keep_hours:g} 小時）")
    for key in keys:
        print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
