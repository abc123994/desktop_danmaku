from __future__ import annotations

import io
import json
import os
import unittest
from unittest.mock import patch

from firebase_auth import firebase_database_url
from firebase_cleanup import CleanupError, cleanup_expired, expired_keys, submissions_url
from firebase_feed import FirebaseFeedClient, feed_url, iter_sse_events
from firebase_submit import SubmissionError, _submission_url, build_payload


class FirebaseTests(unittest.TestCase):
    def test_expired_keys_use_timestamp_key_and_keep_hours(self) -> None:
        submissions = {"100": {}, "200": {}, "300": {}}
        self.assertEqual(expired_keys(submissions, now_ms=300, keep_hours=0.0000278), ["100"])
        with self.assertRaises(CleanupError):
            expired_keys(submissions, now_ms=300, keep_hours=0)

    def test_cleanup_dry_run_does_not_delete(self) -> None:
        class FakeAuth:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method: str, url: str, **_kwargs: object) -> tuple[int, bytes]:
                self.calls.append((method, url))
                return 200, b'{"100":{"text":"old"},"300":{"text":"new"}}'

        auth = FakeAuth()
        keys = cleanup_expired(
            auth=auth,
            database_url="https://example.test",
            now_ms=300,
            keep_hours=0.0000278,
            dry_run=True,
        )
        self.assertEqual(keys, ["100"])
        self.assertEqual([method for method, _url in auth.calls], ["GET"])

    def test_cleanup_deletes_only_expired_timestamp_keys(self) -> None:
        class FakeAuth:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method: str, url: str, **_kwargs: object) -> tuple[int, bytes]:
                self.calls.append((method, url))
                if method == "GET":
                    return 200, b'{"100":{"text":"old"},"300":{"text":"new"}}'
                return 200, b"null"

        auth = FakeAuth()
        cleanup_expired(auth=auth, database_url="https://example.test", now_ms=300, keep_hours=0.0000278)
        self.assertEqual(
            auth.calls[1],
            ("DELETE", "https://example.test/rooms/main_v3/danmaku_submissions/100.json"),
        )

    def test_cleanup_url_targets_submissions(self) -> None:
        self.assertEqual(
            submissions_url("https://example.test", "room"),
            "https://example.test/rooms/room/danmaku_submissions.json",
        )

    def test_database_url_defaults_when_environment_is_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DANMAKU_FIREBASE_DATABASE_URL", None)
            self.assertEqual(
                firebase_database_url(),
                "https://dc-danmaku-default-rtdb.asia-southeast1.firebasedatabase.app",
            )

    def test_database_url_can_be_overridden_by_environment(self) -> None:
        with patch.dict(os.environ, {"DANMAKU_FIREBASE_DATABASE_URL": "https://firebase.example/"}):
            self.assertEqual(
                feed_url(room_id="room"),
                "https://firebase.example/rooms/room/danmaku_submissions.json",
            )

    def test_sse_parser_reads_put(self) -> None:
        stream = io.BytesIO(b'event: put\ndata: {"path":"/","data":{}}\n\n')
        self.assertEqual(list(iter_sse_events(stream)), [("put", '{"path":"/","data":{}}')])

    def test_feed_url_targets_main_v3_submissions(self) -> None:
        self.assertEqual(
            feed_url("https://example.test", "room"),
            "https://example.test/rooms/room/danmaku_submissions.json",
        )

    def test_initial_snapshot_is_not_replayed_and_new_data_is_sorted(self) -> None:
        received: list[dict[str, object]] = []
        client = FirebaseFeedClient(on_message=received.append)
        client._handle_event("put", json.dumps({
            "path": "/",
            "data": {
                "100": {"request_id": "a", "text": "old", "created_at_ms": 100},
                "200": {"request_id": "b", "text": "old2", "created_at_ms": 200},
            },
        }))
        self.assertEqual(received, [])
        client._handle_event("put", json.dumps({
            "path": "/300",
            "data": {"request_id": "c", "text": "new", "created_at_ms": 300},
        }))
        self.assertEqual([item["request_id"] for item in received], ["c"])

    def test_reconnect_snapshot_emits_only_new_request_ids(self) -> None:
        received: list[dict[str, object]] = []
        client = FirebaseFeedClient(on_message=received.append)
        client._handle_event("put", json.dumps({
                "path": "/",
                "data": {"100": {"request_id": "a", "text": "old", "created_at_ms": 100}},
        }))
        client._handle_event("put", json.dumps({
            "path": "/",
            "data": {
                "100": {"request_id": "a", "text": "old", "created_at_ms": 100},
                "200": {"request_id": "b", "text": "new", "created_at_ms": 200},
            },
        }))
        self.assertEqual([item["request_id"] for item in received], ["b"])

    def test_submission_payload_uses_server_epoch_milliseconds(self) -> None:
        payload = build_payload(" 投稿 🎉 ", request_id="client-fixed", auth_uid="anonymous-uid")
        self.assertEqual(payload["created_at_ms"], {".sv": "timestamp"})
        self.assertNotIn("status", payload)
        with self.assertRaises(SubmissionError):
            build_payload("", request_id="x", auth_uid="u")

    def test_submission_url_targets_main_v3_submissions(self) -> None:
        self.assertEqual(
            _submission_url("https://example.test", "room", 1788410612101),
            "https://example.test/rooms/room/danmaku_submissions/1788410612101.json",
        )


if __name__ == "__main__":
    unittest.main()
