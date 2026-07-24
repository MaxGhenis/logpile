import base64
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from logpile.cli import cli
from logpile.db import (
    SEARCH_INDEX_VERSION,
    ensure_user,
    get_db,
    get_meta,
    init_db,
    set_meta,
    update_user,
)
from logpile.parsers import (
    SearchTranscriptReadError,
    clean_search_text,
    iter_session_search_text,
    parse_claudecode_session,
    parse_codex_session,
    strip_harness_preamble,
)
from logpile.search import (
    backfill_search_index,
    replace_session_search_index,
    search_sessions,
)
from logpile.sync import SESSION_TOKEN_VERSION, sync_sessions


class _SnippetCountingConnection(sqlite3.Connection):
    """Counts snippet queries so chunking tests can discriminate."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.snippet_queries = 0

    def execute(self, sql, *args):
        if "snippet(" in str(sql):
            self.snippet_queries += 1
        return super().execute(sql, *args)


class _RacingConnection(sqlite3.Connection):
    """Fires a callback just before the replacement savepoint opens.

    That is the real race window: the sessions row snapshot is already read,
    no write lock is held yet, so another connection can commit a mutation.
    """

    race_hook = None

    def execute(self, sql, *args):
        if self.race_hook is not None and str(sql).strip().startswith(
            "SAVEPOINT replace_session_search_index"
        ):
            hook, self.race_hook = self.race_hook, None
            hook()
        return super().execute(sql, *args)


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record))
            stream.write("\n")


def insert_session(
    conn,
    *,
    session_id: str,
    source: str = "claudecode",
    path: Path | None = None,
    timestamp: str = "2026-07-01T00:00:00Z",
    goal: str = "",
    summary: str = "",
    first_user_message: str = "",
    repo_name: str = "demo-repo",
    project: str = "demo-project",
    visibility: str = "private",
) -> None:
    raw_path = str(path) if path is not None else ""
    conn.execute(
        """
        INSERT INTO sessions (
            session_id, source, username, source_path, shared_path,
            first_timestamp, session_goal, session_summary,
            first_user_message, repo_name, project, visibility, is_private
        ) VALUES (?, ?, 'alice', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            source,
            raw_path,
            raw_path,
            timestamp,
            goal,
            summary,
            first_user_message,
            repo_name,
            project,
            visibility,
            1 if visibility == "private" else 0,
        ),
    )


class SearchIndexTests(unittest.TestCase):
    def test_structured_fields_outrank_body_and_newest_breaks_ties(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            body_path = root / "body.jsonl"
            old_body_path = root / "old-body.jsonl"
            phrase = "quartz ranking phrase"
            write_jsonl(
                body_path,
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": " ".join([phrase] * 1000)}
                            ]
                        },
                    }
                ],
            )
            write_jsonl(
                old_body_path,
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": " ".join([phrase] * 1000)}
                            ]
                        },
                    }
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="goal-hit",
                    timestamp="2026-07-01T00:00:00Z",
                    goal=phrase,
                )
                insert_session(
                    conn,
                    session_id="summary-hit",
                    timestamp="2026-07-02T00:00:00Z",
                    summary=phrase,
                )
                insert_session(
                    conn,
                    session_id="body-new",
                    path=body_path,
                    timestamp="2026-07-04T00:00:00Z",
                )
                insert_session(
                    conn,
                    session_id="body-old",
                    path=old_body_path,
                    timestamp="2026-07-03T00:00:00Z",
                )
                for session_id, path in (
                    ("goal-hit", None),
                    ("summary-hit", None),
                    ("body-new", body_path),
                    ("body-old", old_body_path),
                ):
                    replace_session_search_index(
                        conn,
                        session_id,
                        transcript_path=path,
                    )

                results = search_sessions(conn, phrase, limit=10)

            ids = [row["session_id"] for row in results]
            self.assertEqual(set(ids[:2]), {"goal-hit", "summary-hit"})
            self.assertLess(ids.index("goal-hit"), ids.index("body-new"))
            self.assertLess(ids.index("summary-hit"), ids.index("body-new"))
            self.assertLess(ids.index("body-new"), ids.index("body-old"))

    def test_hyphenated_queries_match_hyphenated_and_spaced_text(self) -> None:
        # Unquoted, FTS5 parses `targeted-signed-reencode` as a column filter
        # ("no such column: signed"), so hyphenated searches used to fail.
        # _quoted_fts_phrase must keep whole-query phrase quoting so hyphenated
        # and spaced forms of the same term match each other's documents.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            hyphen_path = root / "hyphen.jsonl"
            spaced_path = root / "spaced.jsonl"
            write_jsonl(
                hyphen_path,
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "The targeted-signed-reencode job wraps "
                                        "the signed-apply workflow."
                                    ),
                                }
                            ]
                        },
                    }
                ],
            )
            write_jsonl(
                spaced_path,
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "The targeted signed reencode job wraps "
                                        "the signed apply workflow."
                                    ),
                                }
                            ]
                        },
                    }
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="hyphen-text",
                    path=hyphen_path,
                    timestamp="2026-07-02T00:00:00Z",
                )
                insert_session(
                    conn,
                    session_id="spaced-text",
                    path=spaced_path,
                    timestamp="2026-07-01T00:00:00Z",
                )
                for session_id, path in (
                    ("hyphen-text", hyphen_path),
                    ("spaced-text", spaced_path),
                ):
                    replace_session_search_index(
                        conn,
                        session_id,
                        transcript_path=path,
                    )

                for query in (
                    "targeted-signed-reencode",
                    "targeted signed reencode",
                    "signed-apply workflow",
                    "signed apply workflow",
                ):
                    with self.subTest(query=query):
                        ids = {
                            row["session_id"]
                            for row in search_sessions(conn, query, limit=10)
                        }
                        self.assertEqual(ids, {"hyphen-text", "spaced-text"})

                self.assertEqual(search_sessions(conn, "--", limit=10), [])

    def test_candidate_cap_cannot_change_result_membership(self) -> None:
        # The candidate cap is a performance floor, not a membership bound:
        # when the rank cut yields fewer distinct eligible sessions than the
        # requested limit, the tier deepens until exhaustion. A tiny explicit
        # cap must therefore return exactly what the default cap returns.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            phrase = "cap sentinel phrase"
            dense_path = root / "dense.jsonl"
            weak_path = root / "weak.jsonl"
            write_jsonl(
                dense_path,
                [
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": phrase}]},
                    }
                ],
            )
            write_jsonl(
                weak_path,
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": phrase + " filler" * 200,
                                }
                            ]
                        },
                    }
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="goal-hit",
                    goal=phrase,
                )
                insert_session(
                    conn,
                    session_id="dense-transcript",
                    path=dense_path,
                )
                insert_session(
                    conn,
                    session_id="weak-transcript",
                    path=weak_path,
                )
                for session_id, path in (
                    ("goal-hit", None),
                    ("dense-transcript", dense_path),
                    ("weak-transcript", weak_path),
                ):
                    replace_session_search_index(
                        conn,
                        session_id,
                        transcript_path=path,
                    )

                uncapped = {
                    row["session_id"] for row in search_sessions(conn, phrase, limit=10)
                }
                capped = {
                    row["session_id"]
                    for row in search_sessions(conn, phrase, limit=10, candidate_cap=1)
                }

            self.assertEqual(
                uncapped,
                {"goal-hit", "dense-transcript", "weak-transcript"},
            )
            self.assertEqual(capped, uncapped)

    def test_only_plaintext_message_blocks_are_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            claude_path = root / "claude.jsonl"
            codex_path = root / "codex.jsonl"
            base64_blob = "Q" * 96
            write_jsonl(
                claude_path,
                [
                    {
                        "type": "user",
                        "message": {"content": "visible claude user prose"},
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "visible claude assistant prose",
                                },
                                {"type": "thinking", "thinking": "claudeThinkingLeak"},
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {"command": "claudeToolLeak"},
                                },
                                {
                                    "type": "image",
                                    "source": {"data": "claudeImageLeak"},
                                },
                                {
                                    "type": "text",
                                    "text": f"readable around {base64_blob} payload",
                                },
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "content": "claudeResultLeak",
                                }
                            ]
                        },
                    },
                ],
            )
            write_jsonl(
                codex_path,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "visible codex user prose",
                                }
                            ],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "visible codex assistant prose",
                                },
                                {"type": "thinking", "text": "codexThinkingLeak"},
                                {"type": "image", "text": "codexImageLeak"},
                            ],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "reasoning",
                            "summary": [{"text": "codexReasoningLeak"}],
                            "encrypted_content": "codexEncryptedLeak",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "shell",
                            "arguments": {"command": "codexToolLeak"},
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "output": "codexResultLeak",
                        },
                    },
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="claude",
                    source="claudecode",
                    path=claude_path,
                )
                insert_session(
                    conn,
                    session_id="codex",
                    source="codex",
                    path=codex_path,
                )
                replace_session_search_index(
                    conn,
                    "claude",
                    transcript_path=claude_path,
                )
                replace_session_search_index(
                    conn,
                    "codex",
                    transcript_path=codex_path,
                )

                for visible in (
                    "visible claude user prose",
                    "visible claude assistant prose",
                    "visible codex user prose",
                    "visible codex assistant prose",
                    "readable around",
                ):
                    self.assertTrue(search_sessions(conn, visible), visible)
                for hidden in (
                    "claudeThinkingLeak",
                    "claudeToolLeak",
                    "claudeImageLeak",
                    "claudeResultLeak",
                    "codexThinkingLeak",
                    "codexImageLeak",
                    "codexReasoningLeak",
                    "codexEncryptedLeak",
                    "codexToolLeak",
                    "codexResultLeak",
                    base64_blob,
                ):
                    self.assertEqual(search_sessions(conn, hidden), [], hidden)

    def test_base64_variants_are_removed_without_losing_surrounding_text(self) -> None:
        variants = (
            "Q" * 64,
            base64.b64encode(b"x" * 32).decode(),
            base64.encodebytes(b"x" * 58).decode().strip(),
            base64.urlsafe_b64encode((b"\xfb\xff") * 24).decode(),
            "\n".join(
                (
                    base64.b64encode(b"x" * 63).decode()[:80],
                    base64.b64encode(b"x" * 63).decode()[80:],
                )
            ),
            "\n".join(
                base64.b64encode(b"x" * 48).decode()[index : index + 32]
                for index in range(0, 64, 32)
            ),
            " ".join(
                base64.b64encode(b"x" * 48).decode()[index : index + 32]
                for index in range(0, 64, 32)
            ),
            "\t".join(
                base64.b64encode(b"x" * 48).decode()[index : index + 32]
                for index in range(0, 64, 32)
            ),
            " ".join(
                base64.b64encode(b"x" * 51).decode()[index : index + 32]
                for index in range(0, 68, 32)
            ),
            " ".join(
                base64.b64encode(b"x" * 54).decode()[index : index + 32]
                for index in range(0, 72, 32)
            ),
            base64.b64encode(b"x" * 13).decode(),
        )
        for blob in variants:
            self.assertEqual(clean_search_text(f"before {blob} after"), "before after")
        spaced_blob = variants[-5]
        for suffix in ("done", "okay", "tail", "yes"):
            self.assertEqual(
                clean_search_text(f"before {spaced_blob} {suffix}"),
                f"before {suffix}",
            )

        adversarial = " ".join(
            token for _ in range(2000) for token in (("A" * 16,) * 4 + ("B" * 17,))
        )
        started = time.monotonic()
        cleaned_adversarial = clean_search_text(adversarial)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertNotIn("A" * 16, cleaned_adversarial)
        self.assertIn("B" * 17, cleaned_adversarial)

        malformed_terminal = " ".join(["A" * 16] * 8000 + [("A" * 16) + "="])
        started = time.monotonic()
        clean_search_text(malformed_terminal)
        self.assertLess(time.monotonic() - started, 2.0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "base64.jsonl"
            records = []
            for index, blob in enumerate(variants):
                records.append(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"marker{index} before {blob} after",
                                }
                            ]
                        },
                    }
                )
            write_jsonl(transcript, records)
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(conn, session_id="base64", path=transcript)
                replace_session_search_index(
                    conn,
                    "base64",
                    transcript_path=transcript,
                )
                for index, blob in enumerate(variants):
                    self.assertTrue(search_sessions(conn, f"marker{index} before"))
                    for physical_token in blob.split():
                        if physical_token:
                            self.assertEqual(
                                search_sessions(conn, physical_token),
                                [],
                                physical_token,
                            )

    def test_untyped_tool_shaped_blocks_never_become_titles_or_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            claude_path = root / "claude-untyped.jsonl"
            codex_path = root / "codex-untyped.jsonl"
            write_jsonl(
                claude_path,
                [
                    {
                        "type": "user",
                        "message": {
                            "content": {
                                "text": "CLAUDE_UNTYPED_DICT_SECRET",
                                "name": "Bash",
                                "input": {"command": "secret"},
                            }
                        },
                    },
                    {
                        "type": "user",
                        "message": {"content": "real claude operator request"},
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "text": "CLAUDE_UNTYPED_LIST_SECRET",
                                    "name": "Bash",
                                    "input": {"command": "secret"},
                                }
                            ]
                        },
                    },
                ],
            )
            write_jsonl(
                codex_path,
                [
                    {
                        "type": "message",
                        "role": "user",
                        "content": {
                            "text": "CODEX_UNTYPED_DICT_SECRET",
                            "name": "shell",
                            "input": {"command": "secret"},
                        },
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"text": "real legacy codex operator request"}],
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "text": "CODEX_UNTYPED_LIST_SECRET",
                                "name": "shell",
                                "input": {"command": "secret"},
                            }
                        ],
                    },
                ],
            )
            claude = parse_claudecode_session(claude_path)
            codex = parse_codex_session(codex_path)
            assert claude is not None and codex is not None
            self.assertEqual(claude.first_user_message, "real claude operator request")
            self.assertEqual(
                codex.first_user_message, "real legacy codex operator request"
            )

            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="claude-untyped",
                    path=claude_path,
                    first_user_message=claude.first_user_message,
                    goal=claude.first_user_message,
                )
                insert_session(
                    conn,
                    session_id="codex-untyped",
                    source="codex",
                    path=codex_path,
                    first_user_message=codex.first_user_message,
                    goal=codex.first_user_message,
                )
                replace_session_search_index(
                    conn,
                    "claude-untyped",
                    transcript_path=claude_path,
                )
                replace_session_search_index(
                    conn,
                    "codex-untyped",
                    transcript_path=codex_path,
                )
                for secret in (
                    "CLAUDE_UNTYPED_DICT_SECRET",
                    "CLAUDE_UNTYPED_LIST_SECRET",
                    "CODEX_UNTYPED_DICT_SECRET",
                    "CODEX_UNTYPED_LIST_SECRET",
                ):
                    self.assertEqual(search_sessions(conn, secret), [], secret)
                self.assertTrue(search_sessions(conn, "real claude operator request"))
                self.assertTrue(
                    search_sessions(conn, "real legacy codex operator request")
                )

    def test_harness_preamble_cleaning_matches_session_title(self) -> None:
        self.assertEqual(
            strip_harness_preamble(
                "<recommended_plugins>injected</recommended_plugins>real ask"
            ),
            "",
        )
        self.assertEqual(
            strip_harness_preamble(
                "<environment_context>hidden</environment_context> "
                "<cwd>/tmp</cwd> Actual indexed title"
            ),
            "Actual indexed title",
        )
        # Harness wrappers strip even when unclosed; unknown tags are user
        # prose and survive verbatim.
        self.assertEqual(
            strip_harness_preamble("<system-reminder>Actual title"),
            "Actual title",
        )
        self.assertEqual(
            strip_harness_preamble("<orphan>Actual title"),
            "<orphan>Actual title",
        )

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "logpile.db"
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="preamble",
                    goal="<recommended_plugins>pluginNeedle</recommended_plugins>",
                    first_user_message=(
                        "<environment_context>contextNeedle</environment_context> "
                        "Actual indexed title"
                    ),
                )
                replace_session_search_index(
                    conn,
                    "preamble",
                    transcript_path=None,
                )
                result = search_sessions(conn, "Actual indexed title")
                self.assertEqual(result[0]["matched_field"], "first user message")
                self.assertEqual(search_sessions(conn, "pluginNeedle"), [])
                self.assertEqual(search_sessions(conn, "contextNeedle"), [])

    def test_recommended_plugins_title_is_blank_but_suffix_remains_in_transcript(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "plugins.jsonl"
            raw = (
                "<recommended_plugins>PLUGIN_SENTINEL</recommended_plugins> "
                "yc paxel suffix request"
            )
            write_jsonl(
                transcript,
                [{"type": "user", "message": {"content": raw}}],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="plugin-suffix",
                    path=transcript,
                    goal=raw,
                    first_user_message=raw,
                )
                replace_session_search_index(
                    conn,
                    "plugin-suffix",
                    transcript_path=transcript,
                )
                results = search_sessions(conn, "yc paxel suffix request")
                self.assertEqual(search_sessions(conn, "PLUGIN_SENTINEL"), [])

            self.assertEqual(strip_harness_preamble(raw), "")
            self.assertEqual([row["session_id"] for row in results], ["plugin-suffix"])
            self.assertEqual(results[0]["matched_field"], "user transcript")
            self.assertIn("[yc paxel suffix request]", results[0]["excerpt"].lower())

    def test_unclosed_recommended_plugins_message_is_not_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "unclosed-plugins.jsonl"
            raw = "<recommended_plugins>PLUGIN_CATALOG_SECRET apparent operator ask"
            write_jsonl(
                transcript,
                [{"type": "user", "message": {"content": raw}}],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="unclosed-plugin",
                    path=transcript,
                    goal=raw,
                    first_user_message=raw,
                )
                replace_session_search_index(
                    conn,
                    "unclosed-plugin",
                    transcript_path=transcript,
                )
                self.assertEqual(search_sessions(conn, "PLUGIN_CATALOG_SECRET"), [])
                self.assertEqual(search_sessions(conn, "apparent operator ask"), [])
            self.assertEqual(strip_harness_preamble(raw), "")

    def test_user_authored_xml_prompts_stay_searchable(self) -> None:
        # Only harness wrapper tags are stripped. A prompt the operator
        # deliberately wrote in XML style must stay searchable in both the
        # structured and transcript tiers.
        raw = "<task>fix quarterlyRevenueReconciliation</task>"
        self.assertEqual(strip_harness_preamble(raw), raw)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "xml-prompt.jsonl"
            write_jsonl(
                transcript,
                [{"type": "user", "message": {"content": raw}}],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="xml-prompt",
                    path=transcript,
                    goal=raw,
                    first_user_message=raw,
                )
                replace_session_search_index(
                    conn,
                    "xml-prompt",
                    transcript_path=transcript,
                )
                results = search_sessions(conn, "quarterlyRevenueReconciliation")

            self.assertEqual([row["session_id"] for row in results], ["xml-prompt"])
            self.assertIn(
                results[0]["matched_field"],
                {"session goal", "first user message"},
            )

    def test_harness_injected_records_are_not_indexed(self) -> None:
        # isMeta records and text blocks riding tool-result user records are
        # harness output (caveats, system reminders), not operator prose.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "injected.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "user",
                        "isMeta": True,
                        "message": {"content": "metaCaveatSentinel injected"},
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "content": "toolResultSentinel output",
                                },
                                {
                                    "type": "text",
                                    "text": "reminderRiderSentinel instructions",
                                },
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {"content": "genuine operator ask"},
                    },
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="injected",
                    path=transcript,
                )
                replace_session_search_index(
                    conn,
                    "injected",
                    transcript_path=transcript,
                )
                for leak in (
                    "metaCaveatSentinel",
                    "toolResultSentinel",
                    "reminderRiderSentinel",
                ):
                    with self.subTest(leak=leak):
                        self.assertEqual(search_sessions(conn, leak), [])
                kept = search_sessions(conn, "genuine operator ask")

            self.assertEqual([row["session_id"] for row in kept], ["injected"])

    def test_codex_agents_instructions_payload_is_not_indexed(self) -> None:
        agents_payload = (
            "# AGENTS.md instructions for /Users/x"
            "\n<INSTRUCTIONS>\nagentsPayloadSentinel rules\n</INSTRUCTIONS>"
        )
        bare_header_payload = (
            "# AGENTS.md instructions <INSTRUCTIONS> "
            "bareHeaderSentinel rules </INSTRUCTIONS>"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "codex-agents.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": agents_payload,
                                }
                            ],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": bare_header_payload,
                                }
                            ],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "real codex operator ask",
                                }
                            ],
                        },
                    },
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                # Old parser versions stored the payload as the title/goal;
                # the structured tier must not index it either.
                insert_session(
                    conn,
                    session_id="codex-agents",
                    source="codex",
                    path=transcript,
                    goal=agents_payload,
                    first_user_message=agents_payload,
                )
                replace_session_search_index(
                    conn,
                    "codex-agents",
                    transcript_path=transcript,
                )
                self.assertEqual(search_sessions(conn, "agentsPayloadSentinel"), [])
                self.assertEqual(search_sessions(conn, "bareHeaderSentinel"), [])
                kept = search_sessions(conn, "real codex operator ask")

            self.assertEqual([row["session_id"] for row in kept], ["codex-agents"])

    def test_public_mode_cannot_search_private_session_text(self) -> None:
        # A reviewed public session and a private session share the same
        # sentinel phrase: public mode must return exactly the public one
        # (returning nothing would also pass a leak check, so the positive
        # fixture is load-bearing) and must not expose corpus-statistics
        # scores.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            public_source = (
                home / ".claude" / "projects" / "-tmp-demo" / "public-visible.jsonl"
            )
            write_jsonl(
                public_source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "visibility sentinel public prose"},
                    }
                ],
            )
            private_path = root / "private.jsonl"
            write_jsonl(
                private_path,
                [
                    {
                        "type": "user",
                        "message": {"content": "visibility sentinel private prose"},
                    }
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                ensure_user(conn, "alice", display_name="Alice")
                update_user(conn, "alice", default_session_visibility="private")
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            approval = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "public-visible",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )
            self.assertEqual(approval.exit_code, 0, approval.output)

            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="private-session",
                    path=private_path,
                    visibility="private",
                )
                replace_session_search_index(
                    conn,
                    "private-session",
                    transcript_path=private_path,
                )
                replace_session_search_index(
                    conn,
                    "public-visible",
                    transcript_path=None,
                    shared_dir=shared,
                )
                private_results = search_sessions(
                    conn,
                    "visibility sentinel",
                    public_mode=False,
                )
                public_results = search_sessions(
                    conn,
                    "visibility sentinel",
                    public_mode=True,
                )

            self.assertEqual(
                [row["session_id"] for row in public_results],
                ["public-visible"],
            )
            self.assertIsNone(public_results[0]["score"])
            self.assertEqual(
                {row["session_id"] for row in private_results},
                {"public-visible", "private-session"},
            )
            private_row = next(
                row for row in private_results if row["session_id"] == "private-session"
            )
            self.assertIsNotNone(private_row["score"])

    def test_public_index_reads_only_the_verified_review_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = home / ".claude" / "projects" / "-tmp-demo" / "public-search.jsonl"
            write_jsonl(
                source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "reviewed public search phrase"},
                    }
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                ensure_user(conn, "alice", display_name="Alice")
                update_user(conn, "alice", default_session_visibility="private")
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            approval = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "public-search",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )
            self.assertEqual(approval.exit_code, 0, approval.output)

            with get_db(db_path) as conn:
                shared_path = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions "
                        "WHERE session_id = 'public-search'"
                    ).fetchone()[0]
                )
            write_jsonl(
                source,
                [{"type": "user", "message": {"content": "SOURCE_INJECTION"}}],
            )
            write_jsonl(
                shared_path,
                [{"type": "user", "message": {"content": "SHARED_INJECTION"}}],
            )

            with get_db(db_path) as conn:
                replace_session_search_index(
                    conn,
                    "public-search",
                    transcript_path=source,
                    shared_dir=shared,
                )
                reviewed = search_sessions(
                    conn,
                    "reviewed public search phrase",
                    public_mode=True,
                )
                self.assertEqual(search_sessions(conn, "SOURCE_INJECTION"), [])
                self.assertEqual(search_sessions(conn, "SHARED_INJECTION"), [])

            self.assertEqual(reviewed[0]["session_id"], "public-search")

    def test_sync_replaces_stale_text_and_pending_state_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            session_path = (
                home / ".claude" / "projects" / "-tmp-demo" / "search-session.jsonl"
            )
            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "old incremental needle"},
                    }
                ],
            )

            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                self.assertTrue(search_sessions(conn, "old incremental needle"))

            write_jsonl(
                session_path,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "new incremental replacement needle"},
                    }
                ],
            )
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                self.assertEqual(search_sessions(conn, "old incremental needle"), [])
                self.assertTrue(
                    search_sessions(conn, "new incremental replacement needle")
                )
                conn.execute(
                    "DELETE FROM session_search_documents "
                    "WHERE session_id = 'search-session'"
                )
                conn.execute(
                    "DELETE FROM session_search_state "
                    "WHERE session_id = 'search-session'"
                )
                set_meta(conn, "search_refresh_pending", "1")

            # No transcript bytes changed. Durable pending state still forces
            # the resumable backfill to heal the missing index revision.
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                self.assertTrue(
                    search_sessions(conn, "new incremental replacement needle")
                )
                self.assertEqual(get_meta(conn, "search_refresh_pending"), "0")
                conn.execute("DELETE FROM sessions WHERE session_id = 'search-session'")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM session_search_documents "
                        "WHERE session_id = 'search-session'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM session_search_state "
                        "WHERE session_id = 'search-session'"
                    ).fetchone()[0],
                    0,
                )

    def test_incremental_search_uses_verified_copy_not_post_copy_source_bytes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = home / ".claude" / "projects" / "-tmp-demo" / "copy-race.jsonl"
            old_record = {
                "timestamp": "2026-07-01T00:00:00Z",
                "type": "user",
                "cwd": "/tmp/demo",
                "message": {"content": "verified pre-copy revision"},
            }
            new_record = {
                **old_record,
                "message": {"content": "POST_COPY_RACE_INJECTION"},
            }
            write_jsonl(source, [old_record])
            seen_paths: list[Path] = []

            def mutate_source_when_search_starts(path, source_kind):
                seen_paths.append(Path(path))
                write_jsonl(source, [new_record])
                return iter_session_search_text(path, source_kind)

            with mock.patch(
                "logpile.search.iter_session_search_text",
                side_effect=mutate_source_when_search_starts,
            ):
                sync_sessions(shared, db_path, "alice", "machine-1", home)

            with get_db(db_path) as conn:
                managed_path = Path(
                    conn.execute(
                        "SELECT shared_path FROM sessions "
                        "WHERE session_id = 'copy-race'"
                    ).fetchone()[0]
                )
                self.assertEqual(seen_paths, [managed_path])
                self.assertNotEqual(managed_path, source)
                self.assertTrue(search_sessions(conn, "verified pre-copy revision"))
                self.assertEqual(
                    search_sessions(conn, "POST_COPY_RACE_INJECTION"),
                    [],
                )
                state = conn.execute(
                    "SELECT transcript_status FROM session_search_state "
                    "WHERE session_id = 'copy-race'"
                ).fetchone()
                self.assertEqual(state["transcript_status"], "complete")

    def test_concurrent_session_mutation_defers_replacement(self) -> None:
        # A redaction COMMITTED BY ANOTHER CONNECTION between the
        # replacement's row snapshot and its first write must be detected:
        # the replacement defers, and the stale-trigger invalidation the
        # redaction produced survives instead of being clobbered by a stale
        # 'complete' revision.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "steady.jsonl"
            write_jsonl(
                transcript,
                [{"type": "user", "message": {"content": "first revision text"}}],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="racing",
                    path=transcript,
                    goal="original goal",
                )
                replace_session_search_index(
                    conn,
                    "racing",
                    transcript_path=transcript,
                )

            racing_conn = sqlite3.connect(db_path, factory=_RacingConnection)
            racing_conn.row_factory = sqlite3.Row
            try:

                def commit_redaction() -> None:
                    other = sqlite3.connect(db_path)
                    try:
                        other.execute(
                            "UPDATE sessions SET session_goal = ? WHERE session_id = ?",
                            ("redacted goal", "racing"),
                        )
                        other.commit()
                    finally:
                        other.close()

                racing_conn.race_hook = commit_redaction
                with self.assertRaises(SearchTranscriptReadError):
                    replace_session_search_index(
                        racing_conn,
                        "racing",
                        transcript_path=transcript,
                    )
            finally:
                racing_conn.close()

            with get_db(db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT session_goal FROM sessions WHERE session_id = 'racing'"
                    ).fetchone()[0],
                    "redacted goal",
                )
                # The trigger's invalidation survived the deferred replace,
                # so the session is fail-closed out of search entirely.
                self.assertEqual(
                    conn.execute(
                        "SELECT transcript_status FROM session_search_state "
                        "WHERE session_id = 'racing'"
                    ).fetchone()[0],
                    "stale",
                )
                self.assertEqual(search_sessions(conn, "first revision text"), [])
                self.assertEqual(search_sessions(conn, "redacted goal"), [])

    def test_public_membership_survives_private_shadowing(self) -> None:
        # Rank-cut-then-filter must not decide membership: many dense private
        # documents outrank the public session's one weaker document, so with
        # candidate_cap=1 the first cut contains only private candidates.
        # Deepening has to keep going until the reviewed public hit surfaces.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            public_source = (
                home / ".claude" / "projects" / "-tmp-demo" / "public-shadowed.jsonl"
            )
            write_jsonl(
                public_source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {
                            "content": (
                                "shadow sentinel surrounded by much longer "
                                "public prose that scores weaker"
                            )
                        },
                    }
                ],
            )
            private_path = root / "private-shadow.jsonl"
            write_jsonl(
                private_path,
                [
                    {"type": "user", "message": {"content": "shadow sentinel"}}
                    for _ in range(30)
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                ensure_user(conn, "alice", display_name="Alice")
                update_user(conn, "alice", default_session_visibility="private")
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            approval = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "public-shadowed",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )
            self.assertEqual(approval.exit_code, 0, approval.output)

            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="private-shadow",
                    path=private_path,
                    visibility="private",
                )
                replace_session_search_index(
                    conn,
                    "private-shadow",
                    transcript_path=private_path,
                )
                replace_session_search_index(
                    conn,
                    "public-shadowed",
                    transcript_path=None,
                    shared_dir=shared,
                )
                public_results = search_sessions(
                    conn,
                    "shadow sentinel",
                    public_mode=True,
                    candidate_cap=1,
                )

            self.assertEqual(
                [row["session_id"] for row in public_results],
                ["public-shadowed"],
            )
            # The winner's excerpt passed the same eligibility predicate as
            # the candidate query — snippets are never bound by bare rowid.
            self.assertIn("[shadow sentinel]", public_results[0]["excerpt"])

    def test_clean_search_text_keeps_ragged_multiline_prose(self) -> None:
        self.assertEqual(
            clean_search_text("first\nsecond\nthird\nfourth\nfifth\nsixth"),
            "first second third fourth fifth sixth",
        )
        # Coincidentally equal-length word pairs stay below the 24-column
        # floor real base64 encoders wrap at.
        self.assertEqual(
            clean_search_text("misunderstanding\nresponsibilities"),
            "misunderstanding responsibilities",
        )
        for tag in (
            "heartbeat",
            "turn_aborted",
            "goal_context",
            "subagent_notification",
        ):
            with self.subTest(tag=tag):
                self.assertEqual(
                    strip_harness_preamble(f"<{tag}>ping</{tag}> real ask"),
                    "real ask",
                )
        for prose_about_agents in (
            "# AGENTS.md instructions are confusing; explain precedence",
            "# AGENTS.md instructions for beginners: explain precedence",
        ):
            with self.subTest(prose=prose_about_agents):
                self.assertEqual(
                    strip_harness_preamble(prose_about_agents),
                    prose_about_agents,
                )

    def test_boundary_score_ties_deepen_until_exhausted(self) -> None:
        # Three sessions hold byte-identical single documents (equal bm25).
        # With limit=2 and cap=2, the first cut finds two distinct sessions
        # and would satisfy the session-count check — but the third identical
        # document beyond the cap ties the boundary score, so the cut must
        # deepen; otherwise which sessions win (and the newest-first
        # tiebreak) would depend on rowid insertion order at the cut.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            phrase = "boundary tie sentinel"
            init_db(db_path)
            with get_db(db_path) as conn:
                for index in range(3):
                    path = root / f"tie-{index}.jsonl"
                    write_jsonl(
                        path,
                        [
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [{"type": "text", "text": phrase}]
                                },
                            }
                        ],
                    )
                    insert_session(
                        conn,
                        session_id=f"tie-{index}",
                        path=path,
                        timestamp=f"2026-07-0{index + 1}T00:00:00Z",
                    )
                    replace_session_search_index(
                        conn,
                        f"tie-{index}",
                        transcript_path=path,
                    )

                results = search_sessions(conn, phrase, limit=2, candidate_cap=2)

            # tie-2 is the newest; the equal-score class must be fully
            # consumed so it wins regardless of which side of the original
            # cut it fell on.
            self.assertEqual(results[0]["session_id"], "tie-2")
            self.assertEqual(len(results), 2)

    def test_agents_payload_behind_harness_wrapper_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "wrapped-agents.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        "<heartbeat>ping</heartbeat> "
                                        "# AGENTS.md instructions "
                                        "<INSTRUCTIONS> wrappedAgentsSentinel "
                                        "</INSTRUCTIONS>"
                                    ),
                                }
                            ],
                        },
                    }
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="wrapped-agents",
                    source="codex",
                    path=transcript,
                )
                replace_session_search_index(
                    conn,
                    "wrapped-agents",
                    transcript_path=transcript,
                )
                self.assertEqual(search_sessions(conn, "wrappedAgentsSentinel"), [])

    def test_codex_agents_payload_never_becomes_the_title(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "codex-agents-title.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "response_item",
                        "timestamp": "2026-07-01T00:00:00Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        "# AGENTS.md instructions for /Users/x"
                                        "\n<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>"
                                    ),
                                }
                            ],
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-07-01T00:00:01Z",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "real codex topic ask",
                                }
                            ],
                        },
                    },
                ],
            )
            info = parse_codex_session(path)
            self.assertEqual(info.first_user_message, "real codex topic ask")

    def test_is_meta_record_never_becomes_the_title(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "meta-first.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "isMeta": True,
                        "timestamp": "2026-07-01T00:00:00Z",
                        "cwd": "/tmp/demo",
                        "message": {"content": "Caveat: injected preamble"},
                    },
                    {
                        "type": "user",
                        "timestamp": "2026-07-01T00:00:01Z",
                        "cwd": "/tmp/demo",
                        "message": {"content": "real first ask"},
                    },
                ],
            )
            info = parse_claudecode_session(path)
            self.assertEqual(info.first_user_message, "real first ask")

    def test_snippet_lookup_chunks_winner_ids(self) -> None:
        # Winner ids bind through the shared chunk loop like every other id
        # list; chunk size 1 forces one snippet query per winner, so an
        # unchunked regression would leave later winners without excerpts.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            phrase = "chunked snippet sentinel"
            init_db(db_path)
            with get_db(db_path) as conn:
                for index in range(3):
                    path = root / f"chunk-{index}.jsonl"
                    write_jsonl(
                        path,
                        [
                            {
                                "type": "user",
                                "message": {"content": f"{phrase} variant {index}"},
                            }
                        ],
                    )
                    insert_session(
                        conn,
                        session_id=f"chunk-{index}",
                        path=path,
                        timestamp=f"2026-07-0{index + 1}T00:00:00Z",
                    )
                    replace_session_search_index(
                        conn,
                        f"chunk-{index}",
                        transcript_path=path,
                    )

            counting_conn = sqlite3.connect(db_path, factory=_SnippetCountingConnection)
            counting_conn.row_factory = sqlite3.Row
            try:
                with mock.patch("logpile.search._SQL_IN_CHUNK", 1):
                    results = search_sessions(counting_conn, phrase, limit=3)
                snippet_queries = counting_conn.snippet_queries
            finally:
                counting_conn.close()

            self.assertEqual(len(results), 3)
            for row in results:
                self.assertIn("[chunked snippet sentinel]", row["excerpt"])
            # One snippet query per chunk: an unchunked reversion would bind
            # all three winners in a single query and fail this count.
            self.assertEqual(snippet_queries, 3)

    def test_oversized_jsonl_lines_are_skipped_without_materializing(self) -> None:
        from logpile.parsers import JsonlLoadStats, _iter_jsonl

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mixed.jsonl"
            giant = json.dumps({"type": "user", "message": {"content": "G" * 4000}})
            normal = json.dumps(
                {"type": "user", "message": {"content": "small survivor"}}
            )
            path.write_text(f"{giant}\n{normal}\n", encoding="utf-8")

            stats = JsonlLoadStats()
            records = list(_iter_jsonl(path, stats=stats, max_line_chars=1000))

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["message"]["content"], "small survivor")
        self.assertEqual(stats.malformed_fields.get("line:oversized"), 1)

    def test_v7_tool_result_metadata_is_reparsed_and_removed_from_search(self) -> None:
        self.assertGreater(SESSION_TOKEN_VERSION, 7)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = home / ".claude" / "projects" / "-tmp-demo" / "dict-tool.jsonl"
            write_jsonl(
                source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {
                            "content": {
                                "type": "tool_result",
                                "tool_use_id": "call-1",
                                "content": "DICT_TOOL_STRUCTURED_LEAK",
                            }
                        },
                    },
                    {
                        "timestamp": "2026-07-01T00:00:01Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "real operator search request"},
                    },
                ],
            )
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                row = conn.execute(
                    "SELECT first_user_message, session_goal, user_message_count "
                    "FROM sessions WHERE session_id = 'dict-tool'"
                ).fetchone()
                self.assertEqual(
                    row["first_user_message"], "real operator search request"
                )
                self.assertEqual(row["session_goal"], "real operator search request")
                self.assertEqual(row["user_message_count"], 1)

                # Reproduce an already-current v7 row made by the permissive
                # parser, including a complete old search revision.
                conn.execute(
                    """
                    UPDATE sessions
                    SET first_user_message = 'DICT_TOOL_STRUCTURED_LEAK',
                        session_goal = 'DICT_TOOL_STRUCTURED_LEAK',
                        token_version = 7
                    WHERE session_id = 'dict-tool'
                    """
                )
                replace_session_search_index(
                    conn,
                    "dict-tool",
                    transcript_path=source,
                )
                self.assertTrue(search_sessions(conn, "DICT_TOOL_STRUCTURED_LEAK"))

            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                row = conn.execute(
                    "SELECT first_user_message, session_goal, token_version "
                    "FROM sessions WHERE session_id = 'dict-tool'"
                ).fetchone()
                self.assertEqual(
                    row["first_user_message"], "real operator search request"
                )
                self.assertEqual(row["session_goal"], "real operator search request")
                self.assertEqual(row["token_version"], SESSION_TOKEN_VERSION)
                self.assertEqual(
                    search_sessions(conn, "DICT_TOOL_STRUCTURED_LEAK"),
                    [],
                )
                self.assertEqual(
                    search_sessions(conn, "real operator search request")[0][
                        "session_id"
                    ],
                    "dict-tool",
                )

    def test_failed_refresh_is_hidden_until_verified_public_artifact_is_indexed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = home / ".claude" / "projects" / "-tmp-demo" / "stale-public.jsonl"
            init_db(db_path)
            with get_db(db_path) as conn:
                ensure_user(conn, "alice", display_name="Alice")
                update_user(conn, "alice", default_session_visibility="private")
            write_jsonl(
                source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "stale private revision sentinel"},
                    }
                ],
            )
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            write_jsonl(
                source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "current reviewed revision sentinel"},
                    }
                ],
            )
            with mock.patch(
                "logpile.search.iter_session_search_text",
                side_effect=SearchTranscriptReadError("forced refresh failure"),
            ):
                sync_sessions(shared, db_path, "alice", "machine-1", home)

            with get_db(db_path) as conn:
                state = conn.execute(
                    "SELECT transcript_status FROM session_search_state "
                    "WHERE session_id = 'stale-public'"
                ).fetchone()
                self.assertEqual(state["transcript_status"], "error")
                self.assertEqual(get_meta(conn, "search_refresh_pending"), "1")

            approval = CliRunner().invoke(
                cli,
                [
                    "publish",
                    "approve",
                    "stale-public",
                    "--db",
                    str(db_path),
                    "--shared",
                    str(shared),
                    "--visibility",
                    "public",
                ],
            )
            self.assertEqual(approval.exit_code, 0, approval.output)
            with get_db(db_path) as conn:
                self.assertEqual(
                    search_sessions(
                        conn,
                        "stale private revision sentinel",
                        public_mode=True,
                    ),
                    [],
                )
                self.assertEqual(
                    search_sessions(
                        conn,
                        "current reviewed revision sentinel",
                        public_mode=True,
                    ),
                    [],
                )

            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                state = conn.execute(
                    """
                    SELECT st.transcript_status, st.artifact_hash,
                           s.reviewed_sha256
                    FROM session_search_state AS st
                    JOIN sessions AS s USING (session_id)
                    WHERE st.session_id = 'stale-public'
                    """
                ).fetchone()
                self.assertEqual(state["transcript_status"], "complete")
                self.assertEqual(state["artifact_hash"], state["reviewed_sha256"])
                self.assertEqual(get_meta(conn, "search_refresh_pending"), "0")
                self.assertEqual(
                    search_sessions(conn, "stale private revision sentinel"),
                    [],
                )
                current = search_sessions(
                    conn,
                    "current reviewed revision sentinel",
                    public_mode=True,
                )
                self.assertEqual(current[0]["session_id"], "stale-public")

    def test_missing_fts_table_is_rebuilt_and_pending_sync_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = home / ".claude" / "projects" / "-tmp-demo" / "fts-recovery.jsonl"
            write_jsonl(
                source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "fts recreation sentinel"},
                    }
                ],
            )
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                document_count = conn.execute(
                    "SELECT COUNT(*) FROM session_search_documents"
                ).fetchone()[0]
            with sqlite3.connect(db_path) as conn:
                conn.execute("DROP TABLE session_search_fts")

            init_db(db_path)
            with get_db(db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM session_search_documents"
                    ).fetchone()[0],
                    0,
                )
                self.assertGreater(document_count, 0)
                self.assertEqual(search_sessions(conn, "fts recreation sentinel"), [])
                self.assertEqual(get_meta(conn, "search_refresh_pending"), "1")

            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                self.assertTrue(search_sessions(conn, "fts recreation sentinel"))
                self.assertEqual(get_meta(conn, "search_refresh_pending"), "0")
                self.assertEqual(
                    get_meta(conn, "search_fts_generation"),
                    str(SEARCH_INDEX_VERSION),
                )

    def test_interrupted_fts_recreation_marker_forces_safe_chunked_rebuild(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            shared = root / "shared"
            db_path = root / "logpile.db"
            source = (
                home / ".claude" / "projects" / "-tmp-demo" / "interrupted-fts.jsonl"
            )
            write_jsonl(
                source,
                [
                    {
                        "timestamp": "2026-07-01T00:00:00Z",
                        "type": "user",
                        "cwd": "/tmp/demo",
                        "message": {"content": "interrupted fts sentinel"},
                    }
                ],
            )
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO logpile_meta (key, value) VALUES (?, ?)",
                    (
                        "search_fts_generation",
                        f"rebuilding:{SEARCH_INDEX_VERSION}",
                    ),
                )
                conn.execute("DROP TABLE session_search_fts")
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE session_search_fts USING fts5(
                        structured_text,
                        transcript_text,
                        content='session_search_documents',
                        content_rowid='id',
                        tokenize='unicode61 remove_diacritics 2'
                    )
                    """
                )

            # This is the dangerous interrupted shape: an empty term index
            # beside complete state. Migration must discard the derived rows
            # before any delete trigger can address the empty FTS table.
            init_db(db_path)
            with get_db(db_path) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM session_search_state"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM session_search_documents"
                    ).fetchone()[0],
                    0,
                )
            sync_sessions(shared, db_path, "alice", "machine-1", home)
            with get_db(db_path) as conn:
                self.assertTrue(search_sessions(conn, "interrupted fts sentinel"))
                self.assertEqual(get_meta(conn, "search_refresh_pending"), "0")

    def test_missing_transcript_keeps_private_structured_fields_searchable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            missing = root / "missing.jsonl"
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="missing-transcript",
                    path=missing,
                    goal="durable structured goal sentinel",
                )
                stats = backfill_search_index(conn, shared_dir=root / "shared")
                self.assertEqual(stats.missing, 1)
                result = search_sessions(conn, "durable structured goal sentinel")
                self.assertEqual(result[0]["session_id"], "missing-transcript")
                self.assertEqual(result[0]["matched_field"], "session goal")
                self.assertEqual(
                    search_sessions(
                        conn,
                        "durable structured goal sentinel",
                        public_mode=True,
                    ),
                    [],
                )

    def test_multiple_matching_fields_are_grouped_to_one_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "grouped.jsonl"
            phrase = "single grouped result"
            write_jsonl(
                transcript,
                [{"type": "user", "message": {"content": phrase}}],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="grouped",
                    path=transcript,
                    goal=phrase,
                    summary=phrase,
                )
                replace_session_search_index(
                    conn,
                    "grouped",
                    transcript_path=transcript,
                )
                results = search_sessions(conn, phrase)
            self.assertEqual([row["session_id"] for row in results], ["grouped"])
            self.assertEqual(results[0]["matched_field"], "session goal")

    def test_apostrophe_query_is_quoted_and_snippet_is_centered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "logpile.db"
            transcript = root / "paxel.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "type": "user",
                        "message": {
                            "content": (
                                ("prefixword " * 80)
                                + "then asks about yc's paxel tool for the workflow "
                                + ("suffixword " * 80)
                            )
                        },
                    }
                ],
            )
            init_db(db_path)
            with get_db(db_path) as conn:
                insert_session(
                    conn,
                    session_id="paxel-session",
                    path=transcript,
                )
                replace_session_search_index(
                    conn,
                    "paxel-session",
                    transcript_path=transcript,
                )
                results = search_sessions(conn, "yc's paxel tool")

            self.assertEqual(results[0]["session_id"], "paxel-session")
            self.assertEqual(results[0]["matched_field"], "user transcript")
            self.assertIn("[yc's paxel tool]", results[0]["excerpt"].lower())
            self.assertIn("…", results[0]["excerpt"])


if __name__ == "__main__":
    unittest.main()
