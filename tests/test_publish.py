import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import tracemalloc
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

import logpile.db as db_module
import logpile.publish as publish_module
from logpile.cli import cli
from logpile.db import ensure_user, init_db, set_session_visibility, update_user
from logpile.publish import (
    preserve_reviewed_artifact,
    review_publish_session,
    review_staging_dir,
    serialize_publish_review,
)
from logpile.sync import sync_sessions


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


def open_sqlite(path: Path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return closing(conn)


class PublishTests(unittest.TestCase):
    def _write_session(
        self,
        home: Path,
        *,
        session_id: str = "session-1",
        body: str = "hello world",
    ) -> Path:
        session_path = (
            home
            / ".claude"
            / "projects"
            / "-Users-alice-demo"
            / f"{session_id}.jsonl"
        )
        write_jsonl(
            session_path,
            [
                {
                    "timestamp": "2026-04-10T10:00:00Z",
                    "type": "user",
                    "cwd": "/tmp/demo",
                    "message": {"content": body},
                },
                {
                    "timestamp": "2026-04-10T10:00:05Z",
                    "type": "assistant",
                    "message": {
                        "id": "msg-1",
                        "model": "claude-3.7",
                        "usage": {"input_tokens": 1, "output_tokens": 2},
                        "content": [{"type": "text", "text": "ack"}],
                    },
                },
            ],
        )
        return session_path

    def _prepare_db(self, db_path: Path) -> None:
        init_db(db_path)
        with open_sqlite(db_path) as conn:
            ensure_user(conn, "alice", display_name="Alice")
            update_user(conn, "alice", default_session_visibility="private")
            conn.commit()

    def test_review_reports_risks_and_recommends_private(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(
                home,
                body=(
                    "Please contact alice@example.com. "
                    "Token sk-ant-abcdefghijklmnopqrstuvwxyz1234567890. "
                    "-----BEGIN OPENSSH PRIVATE KEY-----"
                ),
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: private", result.output)
            self.assertIn("Email address", result.output)
            self.assertIn("Token or credential", result.output)
            self.assertIn("Private key material", result.output)
            self.assertIn("Inspected file:", result.output)

    def test_scanner_covers_credentials_pii_metadata_and_masks_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)

            def jwt_part(value: dict) -> str:
                return base64.urlsafe_b64encode(
                    json.dumps(value, separators=(",", ":")).encode()
                ).decode().rstrip("=")

            jwt = (
                f"{jwt_part({'alg': 'HS256', 'typ': 'JWT'})}."
                f"{jwt_part({'sub': '123', 'role': 'admin'})}."
                "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
            )
            slack_one = "xoxb-" + "123456789012-abcdefghijklmnopqrstuvwxyz"
            slack_two = "xoxp-" + "987654321098-zyxwvutsrqponmlkjihgfedcba"
            github_pat = "github_pat_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
            google_key = "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7R"[:35]
            stripe_key = "sk_live_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
            connection_password = "db-Sup3r-Secret"
            connection_uri = (
                f"postgresql://app:{connection_password}@db.example.com/prod"
            )
            basic_value = base64.b64encode(b"admin:basic-secret-123").decode()
            npm_token = "npm_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
            card = "4111 1111 1111 1111"
            ssn = "123-45-6789"
            phone = "+1 (415) 555-2671"
            compact_phone = "4155552671"
            compact_country_phone = "+14155552671"
            compact_parenthesized_phone = "(415)555-2671"
            international_phone = "+44 20 7946 0958"
            windows_path = r"C:\Users\alice\work\private.txt"
            entropy_token = "Q7mK2vN9xR4pL8sT1wY6aB3cD5eF0hJ2"
            pem_payload = "MIIE" + "A" * 92
            pem_end = "-----END PRIVATE KEY-----"
            pem_block = (
                "-----BEGIN PRIVATE KEY-----\n"
                f"{pem_payload}\n"
                f"{pem_end}"
            )
            prefix = "ordinary context " * 40
            body = " | ".join(
                (
                    prefix + slack_one,
                    slack_two,
                    github_pat,
                    jwt,
                    connection_uri,
                    f"Authorization: Basic {basic_value}",
                    npm_token,
                    ssn,
                    card,
                    phone,
                    compact_phone,
                    compact_country_phone,
                    compact_parenthesized_phone,
                    international_phone,
                    windows_path,
                    entropy_token,
                    "alice@example.com",
                    pem_block,
                )
            )
            self._write_session(home, body=body)
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                update_user(
                    conn,
                    "alice",
                    bio=f"release contact carries {slack_one}",
                    avatar_url=connection_uri,
                )
                conn.execute(
                    """
                    UPDATE sessions
                    SET git_branch = ?, session_goal = ?, objective_label = ?,
                        session_summary = ?
                    WHERE session_id = 'session-1'
                    """,
                    (stripe_key, google_key, f"Review {google_key}", pem_block),
                )
                conn.commit()
                review = review_publish_session(conn, "session-1")

            self.assertIsNotNone(review)
            assert review is not None
            self.assertEqual(review.recommendation, "private")
            titles = {finding.title for finding in review.findings}
            self.assertTrue(
                {
                    "Slack token",
                    "JSON Web Token",
                    "GitHub fine-grained token",
                    "Google API key",
                    "Stripe live key",
                    "Credentialed connection URI",
                    "Basic authorization credential",
                    "Package registry token",
                    "US Social Security number",
                    "Payment card number",
                    "Phone number",
                    "Windows home path",
                    "High-entropy token",
                    "Email address",
                    "Private key material",
                }.issubset(titles),
                titles,
            )

            slack_findings = [
                finding
                for finding in review.findings
                if finding.title == "Slack token"
                and not finding.source.startswith("metadata.")
            ]
            self.assertEqual(len(slack_findings), 2)
            self.assertEqual({finding.match_count for finding in slack_findings}, {2})
            self.assertEqual({finding.match_index for finding in slack_findings}, {1, 2})
            self.assertGreater(min(finding.match_start or 0 for finding in slack_findings), 500)
            self.assertTrue(all("[MASKED]" in finding.evidence for finding in review.findings))
            self.assertTrue(
                any(finding.source == "metadata.bio" for finding in review.findings)
            )
            self.assertTrue(
                any(
                    finding.source == "metadata.avatar_url"
                    for finding in review.findings
                )
            )

            serialized = json.dumps(serialize_publish_review(review))
            self.assertEqual(review.metadata["session_summary"], "[MASKED]")
            self.assertNotIn(slack_one, str(review.metadata["bio"]))
            self.assertNotIn(connection_password, str(review.metadata["avatar_url"]))
            for secret in (
                slack_one,
                slack_two,
                github_pat,
                jwt,
                google_key,
                stripe_key,
                connection_password,
                basic_value,
                npm_token,
                ssn,
                card,
                phone,
                compact_phone,
                compact_country_phone,
                compact_parenthesized_phone,
                international_phone,
                windows_path,
                entropy_token,
                "alice@example.com",
                pem_payload,
                pem_end,
            ):
                self.assertNotIn(secret, serialized)

    def test_scanner_detects_compact_us_phone_forms_and_masks_values(self) -> None:
        phones = ("4155552671", "+14155552671", "(415)555-2671")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            source_path = self._write_session(home, body=" | ".join(phones))
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                review = review_publish_session(conn, "session-1")

            self.assertIsNotNone(review)
            assert review is not None
            phone_findings = [
                finding
                for finding in review.findings
                if finding.title == "Phone number"
                and not finding.source.startswith("metadata.")
            ]
            self.assertEqual(len(phone_findings), len(phones))
            self.assertEqual(
                {finding.match_count for finding in phone_findings},
                {len(phones)},
            )
            source_text = source_path.read_text(encoding="utf-8")
            detected_values = {
                source_text[finding.match_start : finding.match_end]
                for finding in phone_findings
                if finding.match_start is not None and finding.match_end is not None
            }
            self.assertEqual(detected_values, set(phones))
            self.assertTrue(
                all("[MASKED]" in finding.evidence for finding in phone_findings)
            )
            for finding in phone_findings:
                self.assertTrue(
                    all(phone not in finding.evidence for phone in phones),
                    finding.evidence,
                )
            serialized = json.dumps(serialize_publish_review(review))
            for phone in phones:
                self.assertNotIn(phone, serialized)

    def test_scanner_adversarial_negatives_remain_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            body = " | ".join(
                (
                    "xoxb-short",
                    "github_pat_short",
                    "abc.def.ghi",
                    "AIza" + "A" * 34,
                    "sk_test_A1b2C3d4E5f6G7h8I9j0",
                    "postgresql://db.example.com/prod",
                    "Authorization: Basic bm8tY29sb24=",
                    "npm_short",
                    "000-00-0000",
                    "4111 1111 1111 1112",
                    "123-456-7890",
                    "1155552671",
                    "4151552671",
                    "4151112671",
                    "4155550000",
                    "2222222222",
                    "2345678901",
                    "9876543210",
                    "+24155552671",
                    "order 41555526710",
                    r"C:\Program Files\Example\app.exe",
                    "a" * 80,
                    "sha256=" + "a" * 64,
                    "release 2026-07-11 version 1.2.3 from 192.168.1.1",
                )
            )
            self._write_session(home, body=body)
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                review = review_publish_session(conn, "session-1")

            self.assertIsNotNone(review)
            assert review is not None
            self.assertEqual(review.recommendation, "public")
            self.assertEqual(review.findings, [])

    def test_review_streams_hash_and_staged_bytes_without_read_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            stage_dir = root / "staging"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Clean staged review.")
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn, mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("whole-file read used"),
            ):
                review = review_publish_session(
                    conn,
                    "session-1",
                    stage_dir=stage_dir,
                )

            self.assertIsNotNone(review)
            assert review is not None and review.staged_path is not None
            staged_path = Path(review.staged_path)
            staged_bytes = staged_path.read_bytes()
            self.assertEqual(review.inspected_size, len(staged_bytes))
            self.assertEqual(
                review.inspected_sha256,
                hashlib.sha256(staged_bytes).hexdigest(),
            )
            self.assertEqual(os.stat(staged_path).st_mode & 0o777, 0o600)

    def test_scanner_bounds_evidence_but_counts_many_matches(self) -> None:
        email_count = 25_000
        body = " ".join(
            f"person{index:05d}@example.com" for index in range(email_count)
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body=body)
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                tracemalloc.start()
                try:
                    review = review_publish_session(conn, "session-1")
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

            self.assertIsNotNone(review)
            assert review is not None
            email_findings = [
                finding
                for finding in review.findings
                if finding.title == "Email address"
                and not finding.source.startswith("metadata.")
            ]
            self.assertEqual(len(email_findings), 5)
            self.assertEqual(
                {finding.match_count for finding in email_findings},
                {email_count},
            )
            self.assertEqual(
                {finding.omitted_count for finding in email_findings},
                {email_count - len(email_findings)},
            )
            self.assertEqual(
                [finding.match_index for finding in email_findings],
                [1, 2, 3, 4, 5],
            )
            self.assertLess(peak, 32 * 1024 * 1024)

    def test_scanner_detects_and_masks_long_home_path_across_chunk_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Clean session before boundary fixture.")
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            long_home_path = (
                "/Users/"
                + "u" * 255
                + "".join(f"/{chr(97 + index % 26) * 255}" for index in range(32))
            )
            distance_before_boundary = 5_000
            match_start = publish_module._SCAN_CHUNK_BYTES - distance_before_boundary
            self.assertGreater(distance_before_boundary, 4_096)
            self.assertLess(distance_before_boundary, publish_module._SCAN_OVERLAP_CHARS)
            self.assertGreater(
                match_start + len(long_home_path),
                publish_module._SCAN_CHUNK_BYTES,
            )

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()
                assert row is not None
                artifact_path = Path(row["shared_path"])
                artifact_path.write_text(
                    " " * match_start + long_home_path + "\n",
                    encoding="utf-8",
                )
                review = review_publish_session(conn, "session-1")

            self.assertIsNotNone(review)
            assert review is not None
            path_findings = [
                finding
                for finding in review.findings
                if finding.title == "Absolute home path"
                and not finding.source.startswith("metadata.")
            ]
            self.assertEqual(len(path_findings), 1)
            self.assertEqual(path_findings[0].match_start, match_start)
            self.assertEqual(
                path_findings[0].match_end,
                match_start + len(long_home_path),
            )
            self.assertIn("[MASKED]", path_findings[0].evidence)
            self.assertNotIn(
                long_home_path,
                json.dumps(serialize_publish_review(review)),
            )

    def test_streaming_scanner_emits_exact_cutoff_straddles_once(self) -> None:
        cases = (
            (
                "Google API key",
                "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7R"[:35],
            ),
            (
                "Token or credential",
                "sk-ant-" + "A1b2C3d4" * 12,
            ),
        )
        cutoff = 137
        match_start = cutoff - 1

        for title, secret in cases:
            with self.subTest(title=title):
                suffix_length = (
                    publish_module._SCAN_OVERLAP_CHARS - len(secret) + 1
                )
                first_chunk = " " * match_start + secret + " " * suffix_length
                self.assertEqual(
                    len(first_chunk) - publish_module._SCAN_OVERLAP_CHARS,
                    cutoff,
                )
                self.assertLess(match_start, cutoff)
                self.assertGreater(match_start + len(secret), cutoff)

                accumulator = publish_module._FindingAccumulator()
                scanner = publish_module._StreamingTextScanner(
                    "cutoff fixture",
                    accumulator,
                )
                scanner.feed(first_chunk)
                scanner.feed(" ordinary continuation")
                scanner.feed("", final=True)
                matches = [
                    finding
                    for finding in accumulator.finalize()
                    if finding.title == title
                ]

                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0].match_start, match_start)
                self.assertEqual(matches[0].match_end, match_start + len(secret))
                self.assertEqual(matches[0].match_index, 1)
                self.assertEqual(matches[0].match_count, 1)
                self.assertIn("[MASKED]", matches[0].evidence)
                self.assertNotIn(secret, matches[0].evidence)

    def test_streaming_scanner_retains_left_boundary_context_at_cutoff(self) -> None:
        secret = "AIza" + "A" * 35
        cutoff = 137
        first_chunk = (
            " " * (cutoff - 1)
            + "x"
            + secret
            + " " * (publish_module._SCAN_OVERLAP_CHARS - len(secret))
        )
        self.assertEqual(
            len(first_chunk) - publish_module._SCAN_OVERLAP_CHARS,
            cutoff,
        )

        accumulator = publish_module._FindingAccumulator()
        scanner = publish_module._StreamingTextScanner("boundary fixture", accumulator)
        scanner.feed(first_chunk)
        scanner.feed("", final=True)

        self.assertFalse(
            any(
                finding.title == "Google API key"
                for finding in accumulator.finalize()
            )
        )

    def test_scanner_patterns_have_finite_width_within_overlap(self) -> None:
        widths = [
            re._parser.parse(rule.regex.pattern, rule.regex.flags).getwidth()[1]
            for rule in publish_module._PATTERN_RULES
        ]
        self.assertTrue(all(width < re._parser.MAXWIDTH for width in widths))
        self.assertLessEqual(max(widths), publish_module._SCAN_OVERLAP_CHARS)

    def test_publish_queue_lists_pending_sessions_with_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Polish the session index.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "queue", "--db", str(db_path), "--limit", "10"],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("session-1", result.output)
            self.assertIn("summary:", result.output)
            self.assertIn("outcome:", result.output)
            self.assertIn("review: public", result.output)

    def test_new_users_default_sessions_to_unlisted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            init_db(db_path)
            self._write_session(home, body="Polish the session index.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                user = conn.execute(
                    "SELECT default_session_visibility FROM users WHERE username = 'alice'"
                ).fetchone()
                session = conn.execute(
                    "SELECT visibility, visibility_source FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(user["default_session_visibility"], "unlisted")
            self.assertEqual(session["visibility"], "unlisted")
            self.assertEqual(session["visibility_source"], "default")

    def test_publish_queue_json_outputs_structured_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Polish the session index.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "queue",
                    "--db",
                    str(db_path),
                    "--limit",
                    "10",
                    "--json",
                    "--reviews",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["visibility"], "pending")
            self.assertTrue(payload["reviews"])
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["candidates"][0]["session_id"], "session-1")
            self.assertEqual(payload["candidates"][0]["review_recommendation"], "public")

    def test_publish_queue_json_can_filter_by_origin(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, session_id="direct-1", body="make more progress on logpile")
            self._write_session(
                home,
                session_id="eval-1",
                body=(
                    "You are a senior statutory-fidelity reviewer for RAC (Rules as Code) encodings.\n\n"
                    "Review the file holistically for citation fidelity."
                ),
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "queue",
                    "--db",
                    str(db_path),
                    "--json",
                    "--origin",
                    "pipeline_eval",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["origin"], "pipeline_eval")
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["candidates"][0]["session_id"], "eval-1")
            self.assertEqual(payload["candidates"][0]["session_origin"], "pipeline_eval")

    def test_publish_queue_needs_changes_surfaces_public_sessions_with_tighter_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Please follow up with alice@example.com about the release.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            approval = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                    "--force",
                ],
            )
            self.assertEqual(approval.exit_code, 0, approval.output)

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "queue",
                    "--db",
                    str(db_path),
                    "--visibility",
                    "needs_changes",
                    "--json",
                    "--reviews",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["visibility"], "needs_changes")
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["candidates"][0]["session_id"], "session-1")
            self.assertEqual(payload["candidates"][0]["visibility"], "public")
            self.assertEqual(payload["candidates"][0]["review_recommendation"], "unlisted")
            self.assertTrue(payload["candidates"][0]["needs_visibility_change"])

    def test_publish_queue_json_total_is_not_truncated_by_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, session_id="session-1", body="First candidate.")
            self._write_session(home, session_id="session-2", body="Second candidate.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "queue",
                    "--db",
                    str(db_path),
                    "--limit",
                    "1",
                    "--json",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["total"], 2)
            self.assertEqual(len(payload["candidates"]), 1)

    def test_review_json_outputs_structured_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Discuss the change with the team.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path), "--json"],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["session_id"], "session-1")
            self.assertEqual(payload["recommendation"], "public")
            self.assertEqual(payload["current_visibility"], "private")
            self.assertIsInstance(payload["metadata"], dict)
            self.assertIsInstance(payload["findings"], list)

    def test_review_json_reports_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            self._prepare_db(db_path)

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "missing-session", "--db", str(db_path), "--json"],
            )

            self.assertEqual(result.exit_code, 1)
            payload = json.loads(result.output)
            self.assertEqual(payload["error"], "not found")
            self.assertEqual(payload["code"], "not_found")

    def test_approve_sets_public_visibility_for_clean_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Discuss the change with the team.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Updated 1 session(s) to visibility=public", result.output)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, visibility_source, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "public")
            self.assertEqual(row["visibility_source"], "manual")
            self.assertTrue(row["shared_path"])
            self.assertTrue(Path(row["shared_path"]).exists())

    def test_apply_blocks_public_publish_when_review_is_risky(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(
                home,
                body=(
                    "Send to alice@example.com and use "
                    "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890."
                ),
            )

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "apply",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )

            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: private", result.output)
            self.assertIn("refusing to set public", result.output)

            with open_sqlite(db_path) as conn:
                row = conn.execute(
                    "SELECT visibility, visibility_source, shared_path FROM sessions WHERE session_id = 'session-1'"
                ).fetchone()

            self.assertEqual(row["visibility"], "private")
            self.assertEqual(row["visibility_source"], "default")
            private_archive = Path(row["shared_path"])
            self.assertTrue(private_archive.exists())
            self.assertFalse(private_archive.is_relative_to(shared))
            self.assertEqual(private_archive.stat().st_mode & 0o777, 0o600)
            self.assertFalse((shared / "alice" / "claudecode" / "demo" / "session-1.jsonl").exists())

    def test_review_scans_rendered_home_path_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, body="Clean session.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                conn.execute(
                    """
                    UPDATE sessions
                    SET workspace_root = ?, worktree_root = ?, repo_root = ?
                    WHERE session_id = 'session-1'
                    """,
                    (
                        "/Users/alice/work/logpile",
                        "/Users/alice/work/logpile",
                        "/Users/alice/work/logpile",
                    ),
                )
                conn.commit()

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: unlisted", result.output)
            self.assertIn("Absolute home path", result.output)
            self.assertNotIn("/Users/alice/work/logpile", result.output)

    def test_review_prefers_shared_publish_artifact_over_private_source_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            session_path = self._write_session(home, body="Publishable shared artifact.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                with self.assertWarnsRegex(
                    RuntimeWarning, "no publish review was required"
                ):
                    set_session_visibility(
                        conn, "session-1", "unlisted", shared_dir=shared
                    )
                conn.commit()

            session_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-10T10:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {
                            "content": "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-1", "--db", str(db_path)],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Recommendation: public", result.output)
            self.assertNotIn("Token or credential", result.output)

    def test_approve_keeps_reviewed_shared_artifact_when_promoting_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            session_path = self._write_session(home, body="Publishable shared artifact.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            with open_sqlite(db_path) as conn:
                with self.assertWarnsRegex(
                    RuntimeWarning, "no publish review was required"
                ):
                    set_session_visibility(
                        conn, "session-1", "unlisted", shared_dir=shared
                    )
                conn.commit()

            session_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-10T10:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {
                            "content": "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "session-1",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            shared_path = shared / "alice" / "claudecode" / "demo" / "session-1.jsonl"
            self.assertTrue(shared_path.exists())
            self.assertNotIn("sk-ant-", shared_path.read_text(encoding="utf-8"))

    def test_preserve_reviewed_artifact_rejects_shared_symlink_without_touching_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", default_session_visibility="unlisted")
                conn.commit()
            source = self._write_session(home, body="reviewed bytes")
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                review = review_publish_session(
                    conn,
                    "session-1",
                    stage_dir=review_staging_dir(shared),
                )
                assert review is not None
                artifact = (
                    shared
                    / ".published"
                    / "session-1"
                    / f"{review.inspected_sha256}.jsonl"
                )
                artifact.parent.mkdir(mode=0o700, parents=True)
                artifact.symlink_to(source)

                with self.assertRaisesRegex(ValueError, "not a regular file"):
                    preserve_reviewed_artifact(
                        conn,
                        review,
                        shared_dir=shared,
                        approved_visibility="public",
                    )

            self.assertIn("reviewed bytes", source.read_text(encoding="utf-8"))
            self.assertTrue(artifact.is_symlink())

    def test_preserve_reviewed_artifact_rejects_non_directory_shared_root_without_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            with open_sqlite(db_path) as conn:
                update_user(conn, "alice", default_session_visibility="unlisted")
                conn.commit()
            self._write_session(home, body="reviewed bytes")
            sync_sessions(shared, db_path, "alice", "machine-1", home)

            with open_sqlite(db_path) as conn:
                review = review_publish_session(
                    conn,
                    "session-1",
                    stage_dir=review_staging_dir(shared),
                )
                assert review is not None
                moved = root / "original-shared"
                shared.rename(moved)
                shared.write_text("not a directory", encoding="utf-8")
                shared.chmod(0o600)

                with self.assertRaisesRegex(ValueError, "unsafe publish directory"):
                    preserve_reviewed_artifact(
                        conn,
                        review,
                        shared_dir=shared,
                        approved_visibility="public",
                    )

            self.assertEqual(shared.stat().st_mode & 0o777, 0o600)
            self.assertEqual(shared.read_text(encoding="utf-8"), "not a directory")

    def test_approve_keeps_reviewed_source_artifact_on_first_publish(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            session_path = self._write_session(home, body="Publishable source artifact.")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            original_set_visibility = db_module.set_session_visibility

            def race_set_visibility(conn, session_id, visibility, *, shared_dir):
                session_path.write_text(
                    json.dumps(
                        {
                            "timestamp": "2026-04-10T10:00:00Z",
                            "type": "user",
                            "cwd": "/tmp/demo",
                            "message": {
                                "content": "sk-ant-abcdefghijklmnopqrstuvwxyz1234567890"
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return original_set_visibility(
                    conn,
                    session_id,
                    visibility,
                    shared_dir=shared_dir,
                )

            with mock.patch("logpile.db.set_session_visibility", side_effect=race_set_visibility):
                result = CliRunner().invoke(
                    cli,
                    [
                        "publish",
                        "approve",
                        "session-1",
                        "--db",
                        str(db_path),
                        "--shared",
                        str(shared),
                        "--visibility",
                        "public",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            shared_path = shared / "alice" / "claudecode" / "demo" / "session-1.jsonl"
            self.assertTrue(shared_path.exists())
            self.assertNotIn("sk-ant-", shared_path.read_text(encoding="utf-8"))

    def test_review_rejects_ambiguous_session_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, session_id="session-alpha", body="one")
            self._write_session(home, session_id="session-alpine", body="two")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                ["publish", "review", "session-al", "--db", str(db_path)],
            )

            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("Ambiguous session id prefix", result.output)

    def test_visibility_command_rejects_ambiguous_session_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            self._prepare_db(db_path)
            self._write_session(home, session_id="session-alpha", body="one")
            self._write_session(home, session_id="session-alpine", body="two")

            sync_sessions(
                shared_dir=shared,
                db_path=db_path,
                username="alice",
                machine="machine-1",
                home=home,
            )

            result = CliRunner().invoke(
                cli,
                [
                    "visibility",
                    "session-al",
                    "public",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                ],
            )

            self.assertNotEqual(result.exit_code, 0, result.output)
            self.assertIn("Ambiguous session id prefix", result.output)
