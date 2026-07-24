"""Cross-session dedup: message_claims ownership and native_* aggregates.

Resumed Claude Code sessions copy prior history into a new transcript file
re-stamped with the new sessionId, preserving message.id, requestId, uuid,
and timestamps. These tests cover the claims ledger that dedupes that
inherited history: ownership (earliest-ending transcript wins), order
independence, re-parse reconciliation, and the native_* columns fed by it.
"""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from logpile.db import (
    CLAIMS_TOKEN_VERSION,
    apply_message_claims,
    get_db,
    get_meta,
    init_db,
    refresh_native_usage,
)
from logpile.parsers import parse_claudecode_session
from logpile.sync import SESSION_TOKEN_VERSION, sync_sessions

FIXTURES = Path(__file__).parent / "fixtures" / "claudecode"


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


def _user(ts: str, text: str = "hello world", cwd: str = "/tmp/demo") -> dict:
    return {"timestamp": ts, "type": "user", "cwd": cwd, "message": {"content": text}}


def _assistant(
    ts: str,
    mid: str,
    *,
    rid: str | None = None,
    uid: str | None = None,
    fresh: int = 0,
    cached: int = 0,
    cache_creation: int = 0,
    out: int = 0,
    model: str = "claude-fable-5",
) -> dict:
    record = {
        "timestamp": ts,
        "type": "assistant",
        "message": {
            "id": mid,
            "model": model,
            "usage": {
                "input_tokens": fresh,
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": out,
            },
            "content": [{"type": "text", "text": "ok"}],
        },
    }
    if rid is not None:
        record["requestId"] = rid
    if uid is not None:
        record["uuid"] = uid
    return record


# The canonical resume-chain fixture: the child transcript contains a
# verbatim copy of every parent record (only sessionId is re-stamped in real
# files, which the parser never reads — identity comes from the file stem)
# followed by its live continuation.
PARENT_RECORDS = [
    _user("2026-04-10T10:00:00Z"),
    _assistant(
        "2026-04-10T10:00:05Z", "msg-1", rid="req-1", uid="uuid-1",
        fresh=100, cached=50, cache_creation=10, out=20,
    ),
    _assistant(
        "2026-04-10T10:00:10Z", "msg-2", rid="req-2", uid="uuid-2",
        fresh=200, out=30,
    ),
]
CHILD_RECORDS = PARENT_RECORDS + [
    _user("2026-04-11T11:00:00Z", text="continue please"),
    _assistant(
        "2026-04-11T11:00:05Z", "msg-3", rid="req-3", uid="uuid-3",
        fresh=300, out=40,
    ),
]


def _insert_session_row(
    conn,
    session_id: str,
    *,
    first_timestamp: str | None,
    last_timestamp: str | None,
    source: str = "claudecode",
    token_version: int = CLAIMS_TOKEN_VERSION,
    total_input: int = 0,
    total_output: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, source, username, source_path, shared_path,
            first_timestamp, last_timestamp, token_version,
            total_input_tokens, total_output_tokens,
            fresh_input_tokens, assistant_message_count
        ) VALUES (?, ?, 'alice', ?, '', ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            session_id,
            source,
            f"/tmp/{session_id}.jsonl",
            first_timestamp,
            last_timestamp,
            token_version,
            total_input,
            total_output,
            total_input,
        ),
    )


class ParserMessageUsageTests(unittest.TestCase):
    def _parse(self, records: list[dict], stem: str = "session-a"):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / f"{stem}.jsonl"
            write_jsonl(path, records)
            info = parse_claudecode_session(path)
        self.assertIsNotNone(info)
        assert info is not None
        return info

    def test_emits_claims_keyed_by_message_id_and_request_id(self) -> None:
        info = self._parse(PARENT_RECORDS)
        keys = {m.claim_key: m for m in info.message_usage}
        self.assertEqual(set(keys), {"msg-1:req-1", "msg-2:req-2"})
        m1 = keys["msg-1:req-1"]
        self.assertEqual(m1.day, "2026-04-10")
        self.assertEqual(m1.model, "claude-fable-5")
        self.assertEqual(m1.fresh_input_tokens, 100)
        self.assertEqual(m1.cached_input_tokens, 50)
        self.assertEqual(m1.cache_creation_input_tokens, 10)
        self.assertEqual(m1.cache_creation_5m_input_tokens, 0)
        self.assertEqual(m1.cache_creation_unknown_input_tokens, 10)
        self.assertEqual(m1.output_tokens, 20)

    def test_claim_key_falls_back_to_uuid_then_message_id(self) -> None:
        records = [
            _user("2026-04-10T10:00:00Z"),
            _assistant("2026-04-10T10:00:05Z", "msg-a", uid="uuid-a", fresh=1, out=1),
            _assistant("2026-04-10T10:00:06Z", "msg-b", fresh=2, out=2),
        ]
        info = self._parse(records)
        self.assertEqual(
            {m.claim_key for m in info.message_usage},
            {"uuid:uuid-a", "mid:msg-b"},
        )

    def test_kept_retry_copy_defines_the_claim_key(self) -> None:
        # Same message.id with two requestIds (API retry): per-file accounting
        # keeps the highest-output copy, and the claim key follows it.
        records = [
            _user("2026-04-10T10:00:00Z"),
            _assistant("2026-04-10T10:00:05Z", "msg-r", rid="req-a", uid="u-a", fresh=10, out=5),
            _assistant("2026-04-10T10:00:07Z", "msg-r", rid="req-b", uid="u-b", fresh=10, out=9),
        ]
        info = self._parse(records)
        self.assertEqual([m.claim_key for m in info.message_usage], ["msg-r:req-b"])
        self.assertEqual(info.message_usage[0].output_tokens, 9)


class ApplyMessageClaimsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        base = Path(self._td.name)
        self.db_path = base / "logpile.db"
        init_db(self.db_path)
        self.parent_path = base / "zzz-parent.jsonl"
        self.child_path = base / "aaa-child.jsonl"
        write_jsonl(self.parent_path, PARENT_RECORDS)
        write_jsonl(self.child_path, CHILD_RECORDS)
        self.parent = parse_claudecode_session(self.parent_path)
        self.child = parse_claudecode_session(self.child_path)
        assert self.parent is not None and self.child is not None

    def _register(self, conn, info) -> None:
        _insert_session_row(
            conn,
            info.session_id,
            first_timestamp=info.first_timestamp,
            last_timestamp=info.last_timestamp,
            total_input=info.total_input_tokens,
            total_output=info.total_output_tokens,
        )

    def _owners(self, conn) -> dict[str, str]:
        return {
            row["claim_key"]: row["owner_session_id"]
            for row in conn.execute(
                "SELECT claim_key, owner_session_id FROM message_claim_owners"
            )
        }

    def _occurrences(self, conn) -> set[tuple[str, str]]:
        return {
            (row["claim_key"], row["session_id"])
            for row in conn.execute(
                "SELECT claim_key, session_id FROM message_claims"
            )
        }

    EXPECTED_OWNERS: ClassVar[dict[str, str]] = {
        "msg-1:req-1": "zzz-parent",
        "msg-2:req-2": "zzz-parent",
        "msg-3:req-3": "aaa-child",
    }

    def test_real_format_resume_fixtures_retain_both_occurrences(self) -> None:
        parent = parse_claudecode_session(FIXTURES / "claims" / "claim-parent.jsonl")
        child = parse_claudecode_session(FIXTURES / "claims" / "claim-child.jsonl")
        assert parent is not None and child is not None
        with get_db(self.db_path) as conn:
            self._register(conn, parent)
            self._register(conn, child)
            apply_message_claims(conn, child.session_id, child.message_usage)
            apply_message_claims(conn, parent.session_id, parent.message_usage)

            self.assertEqual(
                self._occurrences(conn),
                {
                    ("claim-message:claim-request", "claim-parent"),
                    ("claim-message:claim-request", "claim-child"),
                },
            )
            self.assertEqual(
                self._owners(conn)["claim-message:claim-request"], "claim-child"
            )

    def test_ownership_is_order_independent(self) -> None:
        for order in ((self.parent, self.child), (self.child, self.parent)):
            with (
                self.subTest(first=order[0].session_id),
                get_db(self.db_path) as conn,
            ):
                conn.execute("DELETE FROM message_claims")
                conn.execute("DELETE FROM sessions")
                for info in order:
                    self._register(conn, info)
                for info in order:
                    apply_message_claims(conn, info.session_id, info.message_usage)
                self.assertEqual(self._owners(conn), self.EXPECTED_OWNERS)
                self.assertEqual(len(self._occurrences(conn)), 5)

    def test_steal_reports_previous_owner_as_affected(self) -> None:
        with get_db(self.db_path) as conn:
            self._register(conn, self.child)
            self._register(conn, self.parent)
            apply_message_claims(conn, "aaa-child", self.child.message_usage)
            affected = apply_message_claims(conn, "zzz-parent", self.parent.message_usage)
            self.assertEqual(affected, {"zzz-parent", "aaa-child"})

    def test_reparse_is_stable_and_marks_current_claimants_for_rank_refresh(self) -> None:
        with get_db(self.db_path) as conn:
            self._register(conn, self.parent)
            apply_message_claims(conn, "zzz-parent", self.parent.message_usage)
            affected = apply_message_claims(conn, "zzz-parent", self.parent.message_usage)
            self.assertEqual(affected, {"zzz-parent"})

    def test_reparse_drops_stale_keys_after_retry_flip(self) -> None:
        records = [
            _user("2026-04-10T10:00:00Z"),
            _assistant("2026-04-10T10:00:05Z", "msg-r", rid="req-a", uid="u-a", fresh=10, out=5),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "retry.jsonl"
            write_jsonl(path, records)
            before = parse_claudecode_session(path)
            write_jsonl(path, records + [
                _assistant("2026-04-10T10:00:07Z", "msg-r", rid="req-b", uid="u-b", fresh=10, out=9),
            ])
            after = parse_claudecode_session(path)
        assert before is not None and after is not None

        with get_db(self.db_path) as conn:
            _insert_session_row(
                conn, "retry",
                first_timestamp=before.first_timestamp,
                last_timestamp=before.last_timestamp,
            )
            apply_message_claims(conn, "retry", before.message_usage)
            self.assertEqual(set(self._owners(conn)), {"msg-r:req-a"})
            apply_message_claims(conn, "retry", after.message_usage)
            self.assertEqual(set(self._owners(conn)), {"msg-r:req-b"})

    def test_vanished_owner_loses_to_any_live_claimant(self) -> None:
        with get_db(self.db_path) as conn:
            self._register(conn, self.parent)
            apply_message_claims(conn, "zzz-parent", self.parent.message_usage)
            conn.execute("DELETE FROM sessions WHERE session_id = 'zzz-parent'")
            self._register(conn, self.child)
            apply_message_claims(conn, "aaa-child", self.child.message_usage)
            self.assertEqual(
                set(self._owners(conn).values()), {"aaa-child"}
            )

    def test_identical_transcripts_tie_break_on_session_id(self) -> None:
        # A resume that added nothing produces a byte-identical history; the
        # lexically smaller session id wins, deterministically.
        with get_db(self.db_path) as conn:
            for sid in ("twin-b", "twin-a"):
                _insert_session_row(
                    conn, sid,
                    first_timestamp=self.parent.first_timestamp,
                    last_timestamp=self.parent.last_timestamp,
                )
                apply_message_claims(conn, sid, self.parent.message_usage)
            self.assertEqual(set(self._owners(conn).values()), {"twin-a"})
            self.assertEqual(len(self._occurrences(conn)), 4)

    def test_owner_dropping_key_promotes_unchanged_loser_occurrence(self) -> None:
        with get_db(self.db_path) as conn:
            self._register(conn, self.parent)
            self._register(conn, self.child)
            child_usage = [
                replace(message, output_tokens=777)
                if message.claim_key == "msg-1:req-1"
                else message
                for message in self.child.message_usage
            ]
            apply_message_claims(conn, self.child.session_id, child_usage)
            apply_message_claims(conn, self.parent.session_id, self.parent.message_usage)
            self.assertEqual(self._owners(conn)["msg-1:req-1"], "zzz-parent")

            remaining_parent = [
                message
                for message in self.parent.message_usage
                if message.claim_key != "msg-1:req-1"
            ]
            affected = apply_message_claims(
                conn, self.parent.session_id, remaining_parent
            )

            self.assertEqual(affected, {"zzz-parent", "aaa-child"})
            self.assertEqual(self._owners(conn)["msg-1:req-1"], "aaa-child")
            winner_value = conn.execute(
                """
                SELECT c.output_tokens
                FROM message_claim_owners o
                JOIN message_claims c
                  ON c.claim_key = o.claim_key
                 AND c.session_id = o.owner_session_id
                WHERE o.claim_key = 'msg-1:req-1'
                """
            ).fetchone()[0]
            self.assertEqual(winner_value, 777)

    def test_rank_change_recomputes_owner_without_loser_reparse(self) -> None:
        with get_db(self.db_path) as conn:
            self._register(conn, self.parent)
            self._register(conn, self.child)
            apply_message_claims(conn, self.parent.session_id, self.parent.message_usage)
            apply_message_claims(conn, self.child.session_id, self.child.message_usage)
            self.assertEqual(self._owners(conn)["msg-1:req-1"], "zzz-parent")

            conn.execute(
                "UPDATE sessions SET last_timestamp = '2027-01-01T00:00:00Z' "
                "WHERE session_id = 'zzz-parent'"
            )
            affected = apply_message_claims(
                conn, self.parent.session_id, self.parent.message_usage
            )
            self.assertEqual(affected, {"zzz-parent", "aaa-child"})
            self.assertEqual(self._owners(conn)["msg-1:req-1"], "aaa-child")

            conn.execute(
                "UPDATE sessions SET last_timestamp = '2026-04-10T10:00:10Z' "
                "WHERE session_id = 'zzz-parent'"
            )
            apply_message_claims(conn, self.parent.session_id, self.parent.message_usage)
            self.assertEqual(self._owners(conn)["msg-1:req-1"], "zzz-parent")

    def test_stale_losing_occurrence_does_not_delete_winner(self) -> None:
        with get_db(self.db_path) as conn:
            self._register(conn, self.parent)
            self._register(conn, self.child)
            apply_message_claims(conn, self.parent.session_id, self.parent.message_usage)
            apply_message_claims(conn, self.child.session_id, self.child.message_usage)

            child_unique = [
                message
                for message in self.child.message_usage
                if message.claim_key == "msg-3:req-3"
            ]
            apply_message_claims(conn, self.child.session_id, child_unique)

            owners = self._owners(conn)
            self.assertEqual(owners["msg-1:req-1"], "zzz-parent")
            self.assertEqual(owners["msg-2:req-2"], "zzz-parent")
            self.assertEqual(owners["msg-3:req-3"], "aaa-child")


class NativeRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.db_path = Path(self._td.name) / "logpile.db"
        init_db(self.db_path)

    def test_codex_native_mirrors_transcript_totals(self) -> None:
        with get_db(self.db_path) as conn:
            _insert_session_row(
                conn, "codex-1", source="codex",
                first_timestamp="2026-04-10T10:00:00Z",
                last_timestamp="2026-04-10T11:00:00Z",
                total_input=500, total_output=60,
            )
            refresh_native_usage(conn, {"codex-1"})
            row = conn.execute(
                "SELECT native_total_input_tokens, native_total_output_tokens,"
                " native_fresh_input_tokens FROM sessions WHERE session_id = 'codex-1'"
            ).fetchone()
            self.assertEqual(
                (row[0], row[1], row[2]), (500, 60, 500)
            )

    def test_pre_claims_claudecode_rows_mirror_transcript_totals(self) -> None:
        with get_db(self.db_path) as conn:
            _insert_session_row(
                conn, "old-cc", token_version=CLAIMS_TOKEN_VERSION - 1,
                first_timestamp="2026-03-01T10:00:00Z",
                last_timestamp="2026-03-01T11:00:00Z",
                total_input=900, total_output=90,
            )
            refresh_native_usage(conn, {"old-cc"})
            row = conn.execute(
                "SELECT native_total_input_tokens, native_total_output_tokens"
                " FROM sessions WHERE session_id = 'old-cc'"
            ).fetchone()
            self.assertEqual((row[0], row[1]), (900, 90))


class SyncClaimsIntegrationTests(unittest.TestCase):
    """End-to-end through sync_sessions with an adversarial parse order:
    the resume child sorts before its parent, so the child claims first and
    the parent must steal the inherited history back."""

    def _setup_chain(self, base: Path) -> tuple[Path, Path, Path]:
        home = base / "home"
        shared = base / "shared"
        db_path = base / "logpile.db"
        project_dir = home / ".claude" / "projects" / "-tmp-demo"
        write_jsonl(project_dir / "aaa-child.jsonl", CHILD_RECORDS)
        write_jsonl(project_dir / "zzz-parent.jsonl", PARENT_RECORDS)
        return home, shared, db_path

    def _sync(self, home: Path, shared: Path, db_path: Path):
        return sync_sessions(shared, db_path, "alice", "machine-1", home)

    def _session_tokens(self, conn, session_id: str) -> dict:
        return dict(
            conn.execute(
                """
                SELECT total_input_tokens, total_output_tokens,
                       native_total_input_tokens, native_total_output_tokens,
                       native_fresh_input_tokens, native_cached_input_tokens,
                       native_cache_creation_input_tokens,
                       native_assistant_message_count, assistant_message_count
                FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        )

    def test_resume_chain_native_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = self._setup_chain(Path(td))
            self._sync(home, shared, db_path)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                parent = self._session_tokens(conn, "zzz-parent")
                child = self._session_tokens(conn, "aaa-child")

                # Transcript semantics unchanged: child contains the copies.
                self.assertEqual(parent["total_input_tokens"], 360)
                self.assertEqual(parent["total_output_tokens"], 50)
                self.assertEqual(child["total_input_tokens"], 660)
                self.assertEqual(child["total_output_tokens"], 90)
                self.assertEqual(child["assistant_message_count"], 3)

                # Native semantics: inherited history stays with the parent.
                self.assertEqual(parent["native_total_input_tokens"], 360)
                self.assertEqual(parent["native_total_output_tokens"], 50)
                self.assertEqual(parent["native_assistant_message_count"], 2)
                self.assertEqual(child["native_total_input_tokens"], 300)
                self.assertEqual(child["native_total_output_tokens"], 40)
                self.assertEqual(child["native_fresh_input_tokens"], 300)
                self.assertEqual(child["native_assistant_message_count"], 1)

                # The chain's native sum equals the true union.
                total = conn.execute(
                    "SELECT SUM(native_total_input_tokens), SUM(native_total_output_tokens)"
                    " FROM sessions"
                ).fetchone()
                self.assertEqual((total[0], total[1]), (660, 90))

    def test_daily_native_buckets_by_owned_message_day(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = self._setup_chain(Path(td))
            self._sync(home, shared, db_path)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = {
                    (row["session_id"], row["day"]): row
                    for row in conn.execute(
                        """
                        SELECT session_id, day, total_input_tokens,
                               native_total_input_tokens, native_assistant_message_count
                        FROM session_daily_usage
                        """
                    )
                }
                # Child's transcript shows the replayed 4/10 history, but
                # natively it owns nothing that day.
                child_apr10 = rows[("aaa-child", "2026-04-10")]
                self.assertEqual(child_apr10["total_input_tokens"], 360)
                self.assertEqual(child_apr10["native_total_input_tokens"], 0)
                self.assertEqual(child_apr10["native_assistant_message_count"], 0)
                child_apr11 = rows[("aaa-child", "2026-04-11")]
                self.assertEqual(child_apr11["native_total_input_tokens"], 300)
                parent_apr10 = rows[("zzz-parent", "2026-04-10")]
                self.assertEqual(parent_apr10["native_total_input_tokens"], 360)

                effective = conn.execute(
                    """
                    SELECT day,
                           SUM(total_input_tokens) AS transcript_in,
                           SUM(native_total_input_tokens) AS native_in
                    FROM session_daily_effective
                    GROUP BY day ORDER BY day
                    """
                ).fetchall()
                self.assertEqual(
                    [(r["day"], r["transcript_in"], r["native_in"]) for r in effective],
                    [("2026-04-10", 720, 360), ("2026-04-11", 300, 300)],
                )

    def test_resync_without_changes_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = self._setup_chain(Path(td))
            self._sync(home, shared, db_path)
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                before = conn.execute(
                    "SELECT session_id, native_total_input_tokens FROM sessions ORDER BY session_id"
                ).fetchall()
            self._sync(home, shared, db_path)
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                after = conn.execute(
                    "SELECT session_id, native_total_input_tokens FROM sessions ORDER BY session_id"
                ).fetchall()
            self.assertEqual(
                [tuple(r) for r in before], [tuple(r) for r in after]
            )

    def test_live_continuation_extends_native_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = self._setup_chain(Path(td))
            self._sync(home, shared, db_path)

            project_dir = home / ".claude" / "projects" / "-tmp-demo"
            write_jsonl(project_dir / "aaa-child.jsonl", CHILD_RECORDS + [
                _assistant(
                    "2026-04-11T12:00:00Z", "msg-4", rid="req-4", uid="uuid-4",
                    fresh=1000, out=100,
                ),
            ])
            self._sync(home, shared, db_path)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                child = conn.execute(
                    "SELECT native_total_input_tokens, native_total_output_tokens"
                    " FROM sessions WHERE session_id = 'aaa-child'"
                ).fetchone()
                self.assertEqual(child["native_total_input_tokens"], 1300)
                self.assertEqual(child["native_total_output_tokens"], 140)
                parent = conn.execute(
                    "SELECT native_total_input_tokens FROM sessions WHERE session_id = 'zzz-parent'"
                ).fetchone()
                self.assertEqual(parent["native_total_input_tokens"], 360)

    def test_backfill_from_shared_copy_emits_claims(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = self._setup_chain(Path(td))
            self._sync(home, shared, db_path)

            # Rotate the parent transcript away and force a token re-parse:
            # the shared copy must reproduce its claims.
            (home / ".claude" / "projects" / "-tmp-demo" / "zzz-parent.jsonl").unlink()
            with sqlite3.connect(db_path) as conn:
                conn.execute("DELETE FROM message_claims")
                conn.execute(
                    "UPDATE sessions SET token_version = ? WHERE session_id = 'zzz-parent'",
                    (CLAIMS_TOKEN_VERSION - 1,),
                )
                conn.commit()
            self._sync(home, shared, db_path)

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                owners = {
                    row["claim_key"]: row["owner_session_id"]
                    for row in conn.execute(
                        "SELECT claim_key, owner_session_id FROM message_claim_owners"
                    )
                }
                self.assertEqual(owners["msg-1:req-1"], "zzz-parent")
                self.assertEqual(owners["msg-2:req-2"], "zzz-parent")
                parent = conn.execute(
                    "SELECT native_total_input_tokens, token_version"
                    " FROM sessions WHERE session_id = 'zzz-parent'"
                ).fetchone()
                self.assertEqual(parent["native_total_input_tokens"], 360)
                self.assertEqual(parent["token_version"], SESSION_TOKEN_VERSION)

    def test_interrupted_sync_flag_forces_full_heal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home, shared, db_path = self._setup_chain(Path(td))
            self._sync(home, shared, db_path)

            with sqlite3.connect(db_path) as conn:
                # Simulate a sync that died after committing rows but before
                # its native refresh.
                conn.execute(
                    "UPDATE sessions SET native_total_input_tokens = 1"
                    " WHERE session_id = 'zzz-parent'"
                )
                conn.execute(
                    "UPDATE logpile_meta SET value = '1' WHERE key = 'native_refresh_pending'"
                )
                conn.commit()

            self._sync(home, shared, db_path)  # no file changes

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                parent = conn.execute(
                    "SELECT native_total_input_tokens FROM sessions"
                    " WHERE session_id = 'zzz-parent'"
                ).fetchone()
                self.assertEqual(parent["native_total_input_tokens"], 360)
                with get_db(db_path) as check:
                    self.assertEqual(get_meta(check, "native_refresh_pending"), "0")


class MigrationTests(unittest.TestCase):
    def test_existing_rows_get_native_mirror_on_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                _insert_session_row(
                    conn, "legacy", token_version=4,
                    first_timestamp="2026-03-01T10:00:00Z",
                    last_timestamp="2026-03-01T11:00:00Z",
                    total_input=1234, total_output=56,
                )
                conn.execute(
                    """
                    INSERT INTO session_daily_usage (
                        session_id, day, total_input_tokens, total_output_tokens
                    ) VALUES ('legacy', '2026-03-01', 1234, 56)
                    """
                )
            init_db(db_path)  # re-run migration
            with get_db(db_path) as conn:
                row = conn.execute(
                    "SELECT native_total_input_tokens, native_total_output_tokens"
                    " FROM sessions WHERE session_id = 'legacy'"
                ).fetchone()
                self.assertEqual((row[0], row[1]), (1234, 56))
                daily = conn.execute(
                    "SELECT native_total_input_tokens FROM session_daily_usage"
                    " WHERE session_id = 'legacy'"
                ).fetchone()
                self.assertEqual(daily[0], 1234)

    def test_orphaned_claims_are_dropped_on_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                conn.execute(
                    "INSERT INTO message_claims (claim_key, session_id)"
                    " VALUES ('msg-x:req-x', 'ghost-session')"
                )
            init_db(db_path)
            with get_db(db_path) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM message_claims").fetchone()[0],
                    0,
                )

    def test_orphan_cleanup_promotes_survivor_and_marks_native_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                _insert_session_row(
                    conn,
                    "owner-a",
                    first_timestamp="2026-03-01T10:00:00Z",
                    last_timestamp="2026-03-01T11:00:00Z",
                )
                _insert_session_row(
                    conn,
                    "loser-b",
                    first_timestamp="2026-03-02T10:00:00Z",
                    last_timestamp="2026-03-02T11:00:00Z",
                )
                conn.executemany(
                    "INSERT INTO message_claims (claim_key, session_id, output_tokens) "
                    "VALUES ('shared:key', ?, ?)",
                    [("owner-a", 10), ("loser-b", 20)],
                )
                conn.execute("DELETE FROM sessions WHERE session_id = 'owner-a'")
                conn.execute(
                    "INSERT OR REPLACE INTO logpile_meta (key, value) "
                    "VALUES ('native_refresh_pending', '0')"
                )

            init_db(db_path)
            with get_db(db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT owner_session_id FROM message_claim_owners "
                        "WHERE claim_key = 'shared:key'"
                    ).fetchone()[0],
                    "loser-b",
                )
                self.assertEqual(get_meta(conn, "native_refresh_pending"), "1")

    def test_winner_only_ledger_migrates_to_occurrence_and_marks_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                _insert_session_row(
                    conn,
                    "legacy-owner",
                    first_timestamp="2026-03-01T10:00:00Z",
                    last_timestamp="2026-03-01T11:00:00Z",
                )
                conn.execute(
                    """
                    UPDATE sessions
                    SET token_version = ?,
                        native_cache_creation_input_tokens = 10,
                        native_cache_creation_5m_input_tokens = 8,
                        native_cache_creation_1h_input_tokens = 8
                    WHERE session_id = 'legacy-owner'
                    """,
                    (CLAIMS_TOKEN_VERSION,),
                )
                conn.execute("DROP VIEW message_claim_owners")
                conn.execute("DROP TABLE message_claims")
                conn.execute(
                    """
                    CREATE TABLE message_claims (
                        claim_key TEXT PRIMARY KEY,
                        owner_session_id TEXT NOT NULL,
                        day TEXT,
                        model TEXT,
                        fresh_input_tokens INTEGER NOT NULL DEFAULT 0,
                        cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_creation_5m_input_tokens INTEGER NOT NULL DEFAULT 0,
                        cache_creation_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0
                    ) WITHOUT ROWID
                    """
                )
                conn.execute(
                    """
                    INSERT INTO message_claims (
                        claim_key, owner_session_id, day,
                        cache_creation_input_tokens,
                        cache_creation_5m_input_tokens,
                        cache_creation_1h_input_tokens,
                        output_tokens
                    ) VALUES ('legacy:key', 'legacy-owner', '2026-03-01', 10, 2, 3, 7)
                    """
                )

            init_db(db_path)
            with get_db(db_path) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(message_claims)")
                }
                self.assertIn("session_id", columns)
                self.assertNotIn("owner_session_id", columns)
                occurrence = conn.execute(
                    """
                    SELECT session_id, cache_creation_5m_input_tokens,
                           cache_creation_1h_input_tokens,
                           cache_creation_unknown_input_tokens
                    FROM message_claims WHERE claim_key = 'legacy:key'
                    """
                ).fetchone()
                self.assertEqual(tuple(occurrence), ("legacy-owner", 2, 3, 5))
                owner = conn.execute(
                    "SELECT owner_session_id FROM message_claim_owners "
                    "WHERE claim_key = 'legacy:key'"
                ).fetchone()[0]
                self.assertEqual(owner, "legacy-owner")
                self.assertEqual(get_meta(conn, "native_refresh_pending"), "1")
                native_split = conn.execute(
                    """
                    SELECT native_cache_creation_input_tokens,
                           native_cache_creation_5m_input_tokens,
                           native_cache_creation_1h_input_tokens,
                           native_cache_creation_unknown_input_tokens
                    FROM sessions WHERE session_id = 'legacy-owner'
                    """
                ).fetchone()
                self.assertEqual(tuple(native_split), (10, 0, 0, 10))

    def test_legacy_codex_raw_parent_is_preserved_in_parent_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                _insert_session_row(
                    conn,
                    "legacy-codex-child",
                    source="codex",
                    first_timestamp="2026-03-01T10:00:00Z",
                    last_timestamp="2026-03-01T11:00:00Z",
                )
                conn.execute(
                    "UPDATE sessions SET parent_session_id = 'raw-parent-thread', "
                    "parent_thread_id = NULL, identity_version = 0 "
                    "WHERE session_id = 'legacy-codex-child'"
                )

            init_db(db_path)
            with get_db(db_path) as conn:
                row = conn.execute(
                    "SELECT parent_session_id, parent_thread_id FROM sessions "
                    "WHERE session_id = 'legacy-codex-child'"
                ).fetchone()
                self.assertEqual(tuple(row), ("raw-parent-thread", "raw-parent-thread"))


if __name__ == "__main__":
    unittest.main()
