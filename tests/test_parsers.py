from datetime import datetime, timezone
import json
import os
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

from logpile.db import (
    apply_message_claims,
    get_db,
    init_db,
    insert_session_paths,
    insert_tool_calls,
)
from logpile.parsers import (
    JsonlLoadStats,
    PrivateSessionMarker,
    _day_of,
    _load_jsonl,
    _normalize_session_path,
    file_hash,
    parse_claudecode_session,
    parse_codex_session,
    render_codex_transcript,
)
from logpile.sync import _annotate_session_paths, _derive_session_activity


FIXTURES = Path(__file__).parent / "fixtures"


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


def assert_daily_matches_session(testcase: unittest.TestCase, info) -> None:
    fields = (
        "total_input_tokens",
        "total_output_tokens",
        "fresh_input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation_5m_input_tokens",
        "cache_creation_1h_input_tokens",
        "cache_creation_unknown_input_tokens",
        "reasoning_output_tokens",
        "user_message_count",
        "assistant_message_count",
        "tool_call_count",
    )
    for field_name in fields:
        testcase.assertEqual(
            getattr(info, field_name),
            sum(getattr(day, field_name) for day in info.daily_usage),
            field_name,
        )
    for day in info.daily_usage:
        testcase.assertEqual(
            day.cache_creation_input_tokens,
            day.cache_creation_5m_input_tokens
            + day.cache_creation_1h_input_tokens
            + day.cache_creation_unknown_input_tokens,
        )


class JsonlLoadingTests(unittest.TestCase):
    def test_loader_keeps_only_objects_and_counts_malformed_types(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mixed.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "valid", "id": 1}),
                        "null",
                        json.dumps(["not", "an", "object"]),
                        json.dumps("text"),
                        json.dumps({"type": "assistant", "message": []}),
                        "{invalid json",
                        json.dumps({"type": "valid", "id": 2}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stats = JsonlLoadStats()

            records = _load_jsonl(path, stats=stats)

        self.assertEqual([record["id"] for record in records], [1, 2])
        self.assertEqual(stats.invalid_json_lines, 1)
        self.assertEqual(
            stats.malformed_record_types,
            {"null": 1, "array": 1, "string": 1},
        )
        self.assertEqual(stats.malformed_fields, {"message:array": 1})
        self.assertEqual(stats.malformed_record_count, 5)

    def test_loader_contains_unexpected_exception_to_one_record(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "exception.jsonl"
            path.write_text(
                '{"id": 1}\n{"explode": true}\n{"id": 2}\n',
                encoding="utf-8",
            )
            stats = JsonlLoadStats()
            real_loads = json.loads

            def flaky_loads(line: str):
                if "explode" in line:
                    raise RuntimeError("one bad record")
                return real_loads(line)

            with mock.patch("logpile.parsers.json.loads", side_effect=flaky_loads):
                records = _load_jsonl(path, stats=stats)

        self.assertEqual([record["id"] for record in records], [1, 2])
        self.assertEqual(stats.record_exceptions, {"RuntimeError": 1})

    def test_loader_discards_partial_records_after_midstream_io_error(self) -> None:
        class FailingReader:
            def __init__(self) -> None:
                self._lines = iter(['{"type": "user", "message": {"content": "partial"}}\n'])

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                return self

            def __next__(self):
                try:
                    return next(self._lines)
                except StopIteration:
                    raise OSError("transcript rotated during read")

        stats = JsonlLoadStats()
        with mock.patch("builtins.open", return_value=FailingReader()):
            records = _load_jsonl(Path("rotating.jsonl"), stats=stats)

        self.assertEqual(records, [])
        self.assertEqual(stats.io_errors, 1)

        with mock.patch("builtins.open", return_value=FailingReader()):
            self.assertIsNone(parse_claudecode_session(Path("rotating.jsonl")))

    def test_claude_parser_skips_non_string_tool_name_and_continues(self) -> None:
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:00Z",
                "message": {
                    "id": "malformed-tool",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": ["Edit"],
                            "input": {"file_path": "bad.py"},
                        }
                    ],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:01Z",
                "message": {
                    "id": "valid-tool",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "id": "call-1",
                            "input": {"file_path": "good.py"},
                        }
                    ],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-tool-name.jsonl"
            write_jsonl(path, records)
            stats = JsonlLoadStats()

            loaded = _load_jsonl(path, stats=stats)
            info = parse_claudecode_session(path)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(stats.malformed_fields, {"message.content.name:array": 1})
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual([call.tool_name for call in info.tool_calls], ["Edit"])

    def test_codex_parser_skips_non_string_tool_name_and_continues(self) -> None:
        records = [
            {
                "timestamp": "2026-07-01T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "codex-tool-name", "cwd": "/tmp/project"},
            },
            {
                "timestamp": "2026-07-01T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": {"bad": "exec_command"},
                    "arguments": "{}",
                },
            },
            {
                "timestamp": "2026-07-01T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "ls"}),
                },
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "codex-tool-name.jsonl"
            write_jsonl(path, records)
            stats = JsonlLoadStats()

            loaded = _load_jsonl(path, stats=stats)
            info = parse_codex_session(path)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(stats.malformed_fields, {"payload.name:object": 1})
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual([call.tool_name for call in info.tool_calls], ["exec_command"])

    def test_loader_rejects_downstream_bound_non_scalar_fields(self) -> None:
        records = [
            {
                "type": "user",
                "timestamp": "2026-07-01T10:00:00Z",
                "cwd": ["bad"],
                "message": {"content": "unsafe workspace"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:01Z",
                "message": {
                    "id": "bad-model",
                    "model": {"bad": 1},
                    "content": [],
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-07-01T10:00:02Z",
                "payload": {"model": ["bad"]},
            },
            {
                "type": "session_meta",
                "timestamp": "2026-07-01T10:00:03Z",
                "payload": {
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": {"bad": 1}}
                        }
                    }
                },
            },
            {
                "type": "user",
                "timestamp": "2026-07-01T10:00:04Z",
                "cwd": "/tmp/good",
                "message": {"content": "valid later record"},
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bound-fields.jsonl"
            write_jsonl(path, records)
            stats = JsonlLoadStats()

            loaded = _load_jsonl(path, stats=stats)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(
            stats.malformed_fields,
            {
                "cwd:array": 1,
                "message.model:object": 1,
                "payload.model:array": 1,
                "payload.source.subagent.thread_spawn.parent_thread_id:object": 1,
            },
        )

    def test_parser_skips_malformed_dict_fields_and_continues(self) -> None:
        records = [
            {
                "type": "user",
                "timestamp": "2026-07-01T10:00:00Z",
                "message": {"content": "valid question"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:01Z",
                "message": [],
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:02Z",
                "message": {
                    "id": "valid-answer",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "content": [{"type": "text", "text": "valid answer"}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "malformed-field.jsonl"
            write_jsonl(path, records)

            info = parse_claudecode_session(path)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.user_message_count, 1)
        self.assertEqual(info.assistant_message_count, 1)
        self.assertEqual(info.total_output_tokens, 5)


class UtcDayTests(unittest.TestCase):
    def test_offset_timestamp_is_bucketed_by_utc_day(self) -> None:
        self.assertEqual(_day_of("2026-07-01T00:30:00+02:00"), "2026-06-30")
        self.assertEqual(_day_of("2026-06-30T21:30:00-03:00"), "2026-07-01")

    def test_malformed_timestamp_is_rejected(self) -> None:
        self.assertIsNone(_day_of("2026-02-30T12:00:00Z"))
        self.assertIsNone(_day_of("not-a-timestamp"))
        self.assertIsNone(_day_of(None))


class CodexParserTests(unittest.TestCase):
    def test_normalize_session_path_does_not_require_home_directory(self) -> None:
        with mock.patch("pathlib.Path.expanduser", side_effect=RuntimeError("no home")):
            normalized = _normalize_session_path("~/demo.py", None)

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertEqual(normalized[0], "~/demo.py")
        self.assertIsNone(normalized[1])
        self.assertEqual(normalized[2], "~/demo.py")

    def test_parse_modern_codex_session(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "sess-1",
                    "timestamp": "2026-04-10T10:00:00Z",
                    "cwd": "/tmp/project",
                },
            },
            {
                "timestamp": "2026-04-10T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>\n  <cwd>/tmp/project</cwd>\n</environment_context>",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-04-10T10:00:02Z",
                "type": "turn_context",
                "payload": {
                    "cwd": "/tmp/project",
                    "model": "gpt-5.4",
                },
            },
            {
                "timestamp": "2026-04-10T10:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "# Context from my IDE setup:\nfoo\n## My request for Codex:\nFix the parser",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-04-10T10:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Inspecting the parser now.",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-04-10T10:00:05Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {"command": ["bash", "-lc", "python scripts/fix.py src/parser.py"]}
                    ),
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-04-10T10:00:06Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": "Command...\nProcess exited with code 1\nOutput:\nboom",
                },
            },
            {
                "timestamp": "2026-04-10T10:00:07Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1200,
                            "cached_input_tokens": 300,
                            "output_tokens": 45,
                            "total_tokens": 1245,
                        }
                    },
                },
            },
            {
                "timestamp": "2026-04-10T10:00:08Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "Need to inspect parser state."}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "modern.jsonl"
            write_jsonl(path, records)

            info = parse_codex_session(path)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.session_id, "modern")
            self.assertEqual(info.thread_id, "sess-1")
            self.assertEqual(info.project, "/tmp/project")
            self.assertEqual(info.first_timestamp, "2026-04-10T10:00:00Z")
            self.assertEqual(info.last_timestamp, "2026-04-10T10:00:08Z")
            self.assertEqual(info.user_message_count, 1)
            self.assertEqual(info.assistant_message_count, 1)
            self.assertEqual(info.tool_call_count, 1)
            self.assertEqual(info.error_count, 1)
            # input_tokens (1200) already includes the 300 cached; total input
            # is 1200 and fresh (uncached) is 1200 - 300 = 900.
            self.assertEqual(info.total_input_tokens, 1200)
            self.assertEqual(info.total_output_tokens, 45)
            self.assertEqual(info.fresh_input_tokens, 900)
            self.assertEqual(info.cached_input_tokens, 300)
            self.assertEqual(info.first_user_message, "Fix the parser")
            self.assertEqual(info.model, "gpt-5.4")
            self.assertEqual(info.tool_calls[0].command, "python scripts/fix.py src/parser.py")
            self.assertTrue(info.tool_calls[0].is_error)
            self.assertEqual(info.workspace_root, "/tmp/project")
            self.assertEqual(
                sorted(path.display_path for path in info.session_paths),
                ["scripts/fix.py", "src/parser.py"],
            )

            turns = render_codex_transcript(path)
            self.assertEqual([turn["type"] for turn in turns], [
                "user",
                "assistant",
                "tool_use",
                "tool_result",
                "thinking",
            ])
            self.assertEqual(turns[0]["content"], "Fix the parser")
            self.assertEqual(
                turns[2]["input"]["command"],
                ["bash", "-lc", "python scripts/fix.py src/parser.py"],
            )
            self.assertTrue(turns[3]["is_error"])

    def test_parse_codex_session_uses_latest_running_token_total(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "sess-token", "timestamp": "2026-04-10T10:00:00Z", "cwd": "/tmp/project"},
            },
            {
                "timestamp": "2026-04-10T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Count tokens"}],
                },
            },
            {
                "timestamp": "2026-04-10T10:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 20,
                            "output_tokens": 10,
                            "total_tokens": 110,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-04-10T10:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 250,
                            "cached_input_tokens": 70,
                            "output_tokens": 25,
                            "total_tokens": 275,
                        },
                        "last_token_usage": {
                            "input_tokens": 150,
                            "cached_input_tokens": 50,
                            "output_tokens": 15,
                            "total_tokens": 165,
                        },
                    },
                },
            },
            {
                "timestamp": "2026-04-10T10:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 250,
                            "cached_input_tokens": 70,
                            "output_tokens": 25,
                            "total_tokens": 275,
                        }
                    },
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tokens.jsonl"
            write_jsonl(path, records)

            info = parse_codex_session(path)
            self.assertIsNotNone(info)
            assert info is not None
            # Latest running total has input_tokens=250 (cached 70 included),
            # so total input is 250 and fresh is 250 - 70 = 180.
            self.assertEqual(info.total_input_tokens, 250)
            self.assertEqual(info.total_output_tokens, 25)
            self.assertEqual(info.fresh_input_tokens, 180)
            self.assertEqual(info.cached_input_tokens, 70)

    def test_parse_codex_session_extracts_parent_thread_lineage(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "sess-lineage",
                    "timestamp": "2026-04-10T10:00:00Z",
                    "cwd": "/tmp/project",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "parent-123",
                                "depth": 2,
                            }
                        }
                    },
                },
            },
            {
                "timestamp": "2026-04-10T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Investigate"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lineage.jsonl"
            write_jsonl(path, records)

            info = parse_codex_session(path)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.thread_id, "sess-lineage")
            self.assertEqual(info.parent_thread_id, "parent-123")
            self.assertEqual(info.parent_session_id, "parent-123")
            self.assertEqual(info.spawn_depth, 2)

    def test_codex_parent_precedence_uses_first_leaf_top_level_parent(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "leaf-thread",
                    "timestamp": "2026-04-10T10:00:00Z",
                    "cwd": "/tmp/project",
                    "parent_thread_id": "top-parent",
                    "forked_from_id": "fork-parent",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "nested-parent",
                                "depth": 3,
                            }
                        }
                    },
                },
            },
            {
                "timestamp": "2026-04-10T10:00:01Z",
                "type": "session_meta",
                "payload": {
                    "id": "ancestor-thread",
                    "timestamp": "2020-01-01T00:00:00Z",
                    "parent_thread_id": "ancestor-parent",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "ancestor-nested",
                                "depth": 9,
                            }
                        }
                    },
                },
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "leaf-rollout.jsonl"
            write_jsonl(path, records)
            info = parse_codex_session(path)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.session_id, "leaf-rollout")
        self.assertEqual(info.thread_id, "leaf-thread")
        self.assertEqual(info.parent_thread_id, "top-parent")
        self.assertEqual(info.first_timestamp, "2026-04-10T10:00:00Z")
        self.assertEqual(info.spawn_depth, 3)

    def test_parse_legacy_codex_session(self) -> None:
        records = [
            {
                "id": "legacy-1",
                "timestamp": "2026-04-10T10:00:00Z",
                "instructions": "legacy",
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "text": "# Context from my IDE setup:\nfoo\n## My request for Codex:\nLegacy request",
                    }
                ],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"text": "Legacy reply"}],
            },
            {
                "type": "function_call",
                "name": "exec_command",
                "arguments": {"command": "ls"},
                "call_id": "call-legacy",
            },
            {
                "type": "function_call_output",
                "call_id": "call-legacy",
                "output": json.dumps({"metadata": {"exit_code": 0}, "output": "ok"}),
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.jsonl"
            write_jsonl(path, records)

            info = parse_codex_session(path)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.session_id, "legacy")
            self.assertEqual(info.thread_id, "legacy-1")
            self.assertEqual(info.user_message_count, 1)
            self.assertEqual(info.assistant_message_count, 1)
            self.assertEqual(info.tool_call_count, 1)
            self.assertEqual(info.error_count, 0)
            self.assertEqual(info.tool_calls[0].command, "ls")
            self.assertFalse(info.tool_calls[0].is_error)

    def test_private_marker_returns_structured_modern_codex_result(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "private-1", "timestamp": "2026-04-10T10:00:00Z"},
            },
            {
                "timestamp": "2026-04-10T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                            "content": [{"type": "input_text", "text": "# logpile:private"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "private.jsonl"
            write_jsonl(path, records)
            result = parse_codex_session(path)

            self.assertIsInstance(result, PrivateSessionMarker)
            assert isinstance(result, PrivateSessionMarker)
            self.assertEqual(result.session_id, "private")
            self.assertEqual(result.source, "codex")
            self.assertEqual(result.marker, "logpile:private")

    def test_file_hash_reads_past_old_prefix_limit(self) -> None:
        prefix = b"a" * (600 * 1024)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.jsonl"
            path.write_bytes(prefix + b"old")
            old_hash = file_hash(path)

            path.write_bytes(prefix + b"new")
            new_hash = file_hash(path)

            self.assertNotEqual(old_hash, new_hash)
            self.assertEqual(len(new_hash), 64)

    def test_large_synthetic_transcript_is_parsed_with_bounded_memory(self) -> None:
        """A large file must not be materialized as a record list."""
        padding = "x" * (256 * 1024)
        line = json.dumps({"type": "progress", "padding": padding}) + "\n"

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "large.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for _ in range(128):
                    handle.write(line)
            self.assertGreater(path.stat().st_size, 32 * 1024 * 1024)

            with mock.patch(
                "logpile.parsers._load_jsonl",
                side_effect=AssertionError("whole-file loader used"),
            ):
                tracemalloc.start()
                try:
                    info = parse_claudecode_session(path)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()

        self.assertIsNotNone(info)
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_high_cardinality_outputs_spill_to_disk_without_dropping_rows(self) -> None:
        """Message/tool cardinality must not become parser heap cardinality."""
        count = 20_000

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "high-cardinality.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for index in range(count):
                    handle.write(json.dumps({
                        "type": "assistant",
                        "timestamp": "2026-07-11T12:00:00Z",
                        "requestId": f"req-{index}",
                        "message": {
                            "id": f"msg-{index}",
                            "model": "claude-test",
                            "usage": {
                                "input_tokens": 1,
                                "output_tokens": index + 1,
                            },
                            "content": [{
                                "type": "tool_use",
                                "id": f"tool-{index}",
                                "name": "Bash",
                                "input": {
                                    "command": f"cat src/row-{index}.py",
                                    "file_path": f"src/row-{index}.py",
                                },
                            }],
                        },
                    }))
                    handle.write("\n")
                    handle.write(json.dumps({
                        "type": "user",
                        "timestamp": "2026-07-11T12:00:01Z",
                        "message": {"content": [{
                            "type": "tool_result",
                            "tool_use_id": f"tool-{index}",
                            "is_error": index % 7 == 0,
                        }]},
                    }))
                    handle.write("\n")

            tracemalloc.start()
            try:
                info = parse_claudecode_session(path)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertIsNotNone(info)
            assert info is not None
            self.assertLess(peak, 12 * 1024 * 1024)
            self.assertEqual(len(info.tool_calls), count)
            self.assertEqual(len(info.message_usage), count)
            self.assertEqual(len(info.session_paths), count * 2)
            self.assertEqual(info.tool_calls[0].call_id, "tool-0")
            self.assertEqual(info.tool_calls[-1].call_id, f"tool-{count - 1}")
            self.assertTrue(info.tool_calls[0].is_error)
            self.assertEqual(info.tool_calls[-1].is_error, (count - 1) % 7 == 0)
            self.assertEqual(
                info.message_usage[-1].claim_key,
                f"msg-{count - 1}:req-{count - 1}",
            )

            # Exercise the actual sync sinks: both iterables must stream all
            # rows into the ledger without re-materializing or capping them.
            db_path = root / "logpile.db"
            init_db(db_path)
            annotated_paths = _annotate_session_paths(
                info.session_paths,
                repo_root=None,
                worktree_root=None,
                workspace_root=None,
            )
            tracemalloc.start()
            with get_db(db_path) as conn:
                try:
                    activity = _derive_session_activity(
                        info.tool_calls,
                        annotated_paths,
                    )
                    insert_tool_calls(conn, info.session_id, info.tool_calls)
                    insert_session_paths(conn, info.session_id, annotated_paths)
                    apply_message_claims(conn, info.session_id, info.message_usage)
                    self.assertEqual(activity["read_path_count"], count)
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0],
                        count,
                    )
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM message_claims").fetchone()[0],
                        count,
                    )
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM session_paths").fetchone()[0],
                        count * 2,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM tool_calls WHERE is_error = 1"
                        ).fetchone()[0],
                        (count + 6) // 7,
                    )
                finally:
                    _, sync_peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
            self.assertLess(sync_peak, 12 * 1024 * 1024)

    def test_codex_high_cardinality_tool_state_spills_to_disk(self) -> None:
        """Codex's tool-call/result index must be bounded just like Claude's."""
        count = 15_000

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "codex-high-cardinality.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": "session_meta",
                    "timestamp": "2026-07-11T12:00:00Z",
                    "payload": {
                        "id": "codex-high-cardinality",
                        "cwd": "/tmp/project",
                    },
                }))
                handle.write("\n")
                for index in range(count):
                    handle.write(json.dumps({
                        "type": "response_item",
                        "timestamp": "2026-07-11T12:00:01Z",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": f"call-{index}",
                            "arguments": json.dumps({
                                "cmd": f"cat src/codex-{index}.py",
                                "file_path": f"src/codex-{index}.py",
                            }),
                        },
                    }))
                    handle.write("\n")
                    handle.write(json.dumps({
                        "type": "response_item",
                        "timestamp": "2026-07-11T12:00:02Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": f"call-{index}",
                            "output": json.dumps({
                                "output": "",
                                "metadata": {
                                    "exit_code": 1 if index % 11 == 0 else 0,
                                },
                            }),
                        },
                    }))
                    handle.write("\n")

            tracemalloc.start()
            try:
                info = parse_codex_session(path)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertIsNotNone(info)
        assert info is not None
        self.assertLess(peak, 12 * 1024 * 1024)
        self.assertEqual(len(info.tool_calls), count)
        self.assertEqual(len(info.session_paths), count * 2)
        self.assertEqual(info.tool_calls[0].call_id, "call-0")
        self.assertEqual(info.tool_calls[-1].call_id, f"call-{count - 1}")
        self.assertTrue(info.tool_calls[0].is_error)
        self.assertEqual(info.error_count, (count + 10) // 11)

    def test_parse_claudecode_session_extracts_input_and_command_paths(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/demo",
                "message": {"content": "Fix the file path layer"},
            },
            {
                "timestamp": "2026-04-10T10:00:05Z",
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "model": "claude-3.7",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "id": "tool-1",
                            "input": {
                                "file_path": "src/app.py",
                                "old_string": "before",
                                "new_string": "after",
                            },
                        },
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "id": "tool-2",
                            "input": {
                                "command": "rg -n session_paths src/app.py tests/test_sync.py",
                            },
                        },
                    ],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(path, records)

            info = parse_claudecode_session(path)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.workspace_root, "/tmp/demo")
            self.assertEqual(info.tool_call_count, 2)
            self.assertEqual(info.total_input_tokens, 1)
            self.assertEqual(info.fresh_input_tokens, 1)
            self.assertEqual(info.cached_input_tokens, 0)
            self.assertFalse(info.tool_calls[0].is_error)
            self.assertFalse(info.tool_calls[1].is_error)
            self.assertEqual(
                sorted((row.display_path, row.source, row.operation) for row in info.session_paths),
                [
                    ("src/app.py", "command", "search"),
                    ("src/app.py", "tool_input", "write"),
                    ("tests/test_sync.py", "command", "search"),
                ],
            )

    def test_parse_claudecode_session_tracks_cached_input_tokens(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/demo",
                "message": {"content": "Inspect cache-heavy run"},
            },
            {
                "timestamp": "2026-04-10T10:00:05Z",
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "model": "claude-3.7",
                    "usage": {
                        "input_tokens": 12,
                        "cache_read_input_tokens": 88,
                        "output_tokens": 7,
                    },
                    "content": [{"type": "text", "text": "hi"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-cache.jsonl"
            write_jsonl(path, records)

            info = parse_claudecode_session(path)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.total_input_tokens, 100)
            self.assertEqual(info.fresh_input_tokens, 12)
            self.assertEqual(info.cached_input_tokens, 88)
            self.assertEqual(info.total_output_tokens, 7)

    def test_parse_claudecode_session_marks_tool_errors_from_tool_results(self) -> None:
        records = [
            {
                "timestamp": "2026-04-10T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/demo",
                "message": {"content": "Run the tests"},
            },
            {
                "timestamp": "2026-04-10T10:00:05Z",
                "type": "assistant",
                "message": {
                    "id": "msg-1",
                    "model": "claude-3.7",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "id": "tool-1",
                            "input": {"command": "pytest -q"},
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-04-10T10:00:06Z",
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "is_error": True,
                            "content": "1 failed",
                        }
                    ]
                },
            },
        ]

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-error.jsonl"
            write_jsonl(path, records)

            info = parse_claudecode_session(path)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.error_count, 1)
            self.assertEqual(info.tool_call_count, 1)
            self.assertTrue(info.tool_calls[0].is_error)


def _codex_meta(ts: str, session_id: str = "sess-replay") -> dict:
    return {
        "timestamp": ts,
        "type": "session_meta",
        "payload": {"id": session_id, "timestamp": ts, "cwd": "/tmp/project"},
    }


def _codex_token_count(
    ts: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int = 0,
) -> dict:
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": reasoning_output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
            },
        },
    }


def _codex_user_message(ts: str, text: str) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _codex_assistant_message(ts: str, text: str) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _codex_function_call(ts: str, call_id: str, command: str) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "shell",
            "call_id": call_id,
            "arguments": json.dumps({"command": ["bash", "-lc", command]}),
        },
    }


class CodexReplayAccountingTests(unittest.TestCase):
    """Only structurally copied Codex history is inherited usage."""

    def _parse(self, records: list[dict]):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout-replay.jsonl"
            write_jsonl(path, records)
            info = parse_codex_session(path)
        self.assertIsNotNone(info)
        assert info is not None
        return info

    def _parse_fixture(self, name: str):
        info = parse_codex_session(FIXTURES / "codex" / name)
        self.assertIsNotNone(info)
        assert info is not None
        return info

    def test_duplicate_snapshot_uses_leaf_metadata_and_live_deltas(self) -> None:
        info = self._parse_fixture(
            "rollout-2026-06-08T11-39-04-leaf-thread.jsonl"
        )

        # The filename stem is canonical; raw graph identity and immediate
        # lineage come only from the first leaf meta, never the copied parent.
        self.assertEqual(
            info.session_id,
            "rollout-2026-06-08T11-39-04-leaf-thread",
        )
        self.assertEqual(info.thread_id, "leaf-thread")
        self.assertEqual(info.parent_thread_id, "parent-thread")
        self.assertEqual(info.parent_session_id, "parent-thread")
        self.assertEqual(info.first_timestamp, "2026-06-08T11:39:04.000Z")
        self.assertEqual(info.spawn_depth, 1)

        self.assertEqual(info.total_input_tokens, 5_000)
        self.assertEqual(info.fresh_input_tokens, 3_000)
        self.assertEqual(info.cached_input_tokens, 2_000)
        self.assertEqual(info.total_output_tokens, 200)
        self.assertEqual(info.reasoning_output_tokens, 50)
        self.assertEqual(info.user_message_count, 1)
        self.assertEqual(info.tool_call_count, 1)
        self.assertEqual(info.tool_calls[0].call_id, "call-live")
        self.assertEqual(info.first_user_message, "Inherited task")
        assert_daily_matches_session(self, info)

    def test_multisecond_replay_does_not_count_inherited_tail(self) -> None:
        info = self._parse_fixture("replay-multisecond.jsonl")

        self.assertEqual(info.total_input_tokens, 5_000)
        self.assertEqual(info.fresh_input_tokens, 2_000)
        self.assertEqual(info.cached_input_tokens, 3_000)
        self.assertEqual(info.total_output_tokens, 500)
        self.assertEqual(info.reasoning_output_tokens, 50)
        self.assertEqual(info.user_message_count, 1)
        self.assertEqual(info.tool_call_count, 0)
        assert_daily_matches_session(self, info)

    def test_fresh_same_second_counters_are_not_replay(self) -> None:
        info = self._parse_fixture("fresh-same-second.jsonl")

        self.assertEqual(info.user_message_count, 1)
        self.assertEqual(info.tool_call_count, 1)
        self.assertEqual(info.tool_calls[0].call_id, "call-fresh")
        self.assertEqual(info.total_input_tokens, 3_000)
        self.assertEqual(info.fresh_input_tokens, 600)
        self.assertEqual(info.cached_input_tokens, 2_400)
        self.assertEqual(info.total_output_tokens, 120)
        self.assertEqual(info.reasoning_output_tokens, 30)
        assert_daily_matches_session(self, info)

    def test_nonmatching_second_meta_is_not_copied_prefix_evidence(self) -> None:
        leaf = _codex_meta("2026-06-08T11:39:04.000Z", "leaf-thread")
        leaf["payload"]["forked_from_id"] = "expected-parent"
        info = self._parse([
            leaf,
            _codex_meta("2026-06-08T11:39:04.001Z", "other-thread"),
            _codex_token_count(
                "2026-06-08T11:39:04.200Z", 1_000, 600, 50, 10
            ),
            _codex_token_count(
                "2026-06-08T11:39:04.900Z", 2_000, 1_400, 90, 20
            ),
        ])

        self.assertEqual(info.total_input_tokens, 2_000)
        self.assertEqual(info.total_output_tokens, 90)
        self.assertEqual(info.reasoning_output_tokens, 20)

    def test_explicit_zero_starts_a_new_billing_epoch(self) -> None:
        info = self._parse_fixture("counter-reset-epochs.jsonl")

        self.assertEqual(info.total_input_tokens, 1_900)
        self.assertEqual(info.fresh_input_tokens, 400)
        self.assertEqual(info.cached_input_tokens, 1_500)
        self.assertEqual(info.total_output_tokens, 160)
        self.assertEqual(info.reasoning_output_tokens, 60)
        assert_daily_matches_session(self, info)

    def test_replay_reset_keeps_only_terminal_inherited_epoch_baseline(self) -> None:
        leaf = _codex_meta("2026-06-08T12:00:00.000Z", "leaf-thread")
        leaf["payload"]["forked_from_id"] = "parent-thread"
        parent = _codex_meta("2026-06-08T12:00:00.001Z", "parent-thread")
        live_timestamp = "2026-06-08T12:00:10.000Z"
        live_started_at = int(
            datetime.fromisoformat(live_timestamp.replace("Z", "+00:00")).timestamp()
        )
        info = self._parse([
            leaf,
            parent,
            _codex_token_count(
                "2026-06-08T12:00:00.100Z", 5_000, 4_000, 500, 100
            ),
            _codex_token_count(
                "2026-06-08T12:00:00.200Z", 0, 0, 0, 0
            ),
            _codex_token_count(
                "2026-06-08T12:00:00.300Z", 1_000, 800, 100, 20
            ),
            {
                "timestamp": live_timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "turn_id": "live-turn",
                    "started_at": live_started_at,
                },
            },
            _codex_token_count(
                "2026-06-08T12:01:00.000Z", 1_200, 900, 130, 30
            ),
        ])

        self.assertEqual(info.total_input_tokens, 200)
        self.assertEqual(info.fresh_input_tokens, 100)
        self.assertEqual(info.cached_input_tokens, 100)
        self.assertEqual(info.total_output_tokens, 30)
        self.assertEqual(info.reasoning_output_tokens, 10)
        assert_daily_matches_session(self, info)

    def test_small_all_component_wobble_does_not_start_an_epoch(self) -> None:
        info = self._parse([
            _codex_meta("2026-05-01T00:00:00.000Z"),
            _codex_token_count(
                "2026-05-01T01:00:00.000Z", 1_000, 800, 100, 40
            ),
            _codex_token_count(
                "2026-05-01T02:00:00.000Z", 999, 799, 99, 39
            ),
            _codex_token_count(
                "2026-05-01T03:00:00.000Z", 1_100, 900, 120, 50
            ),
        ])
        self.assertEqual(info.total_input_tokens, 1_100)
        self.assertEqual(info.cached_input_tokens, 900)
        self.assertEqual(info.total_output_tokens, 120)
        self.assertEqual(info.reasoning_output_tokens, 50)

    def test_rate_limit_only_token_event_is_not_a_counter_reset(self) -> None:
        info = self._parse([
            _codex_meta("2026-05-01T00:00:00.000Z"),
            _codex_token_count(
                "2026-05-01T01:00:00.000Z", 1_000, 800, 100, 40
            ),
            {
                "timestamp": "2026-05-01T02:00:00.000Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": {}},
            },
            _codex_token_count(
                "2026-05-01T03:00:00.000Z", 1_100, 900, 120, 50
            ),
        ])
        self.assertEqual(info.total_input_tokens, 1_100)
        self.assertEqual(info.cached_input_tokens, 900)
        self.assertEqual(info.total_output_tokens, 120)
        self.assertEqual(info.reasoning_output_tokens, 50)

    def test_reasoning_tokens_accumulate_as_deltas(self) -> None:
        info = self._parse([
            _codex_meta("2026-05-01T00:00:00.000Z"),
            _codex_token_count("2026-05-01T01:00:00.000Z", 1_000, 0, 100, reasoning_output_tokens=40),
            _codex_token_count("2026-05-01T02:00:00.000Z", 2_000, 0, 300, reasoning_output_tokens=90),
        ])
        self.assertEqual(info.reasoning_output_tokens, 90)
        self.assertEqual(info.total_output_tokens, 300)

    def test_daily_usage_buckets_by_event_day(self) -> None:
        info = self._parse([
            _codex_meta("2026-06-30T23:00:00.000Z"),
            _codex_user_message("2026-06-30T23:00:30.000Z", "Cross-midnight task"),
            _codex_token_count("2026-06-30T23:01:00.000Z", 1_000, 600, 50),
            _codex_token_count("2026-07-01T01:00:00.000Z", 3_000, 2_400, 120),
        ])
        days = {d.day: d for d in info.daily_usage}
        self.assertEqual(sorted(days), ["2026-06-30", "2026-07-01"])
        june = days["2026-06-30"]
        self.assertEqual(june.fresh_input_tokens, 400)
        self.assertEqual(june.cached_input_tokens, 600)
        self.assertEqual(june.total_input_tokens, 1_000)
        self.assertEqual(june.total_output_tokens, 50)
        self.assertEqual(june.user_message_count, 1)
        july = days["2026-07-01"]
        self.assertEqual(july.fresh_input_tokens, 200)
        self.assertEqual(july.cached_input_tokens, 1_800)
        self.assertEqual(july.total_output_tokens, 70)
        # Session totals equal the sum of the daily slices.
        self.assertEqual(
            info.total_input_tokens,
            sum(d.total_input_tokens for d in info.daily_usage),
        )
        self.assertEqual(
            info.total_output_tokens,
            sum(d.total_output_tokens for d in info.daily_usage),
        )


class ClaudeSidechainIdentityTests(unittest.TestCase):
    def _parse_fixture(self, relative_path: str):
        info = parse_claudecode_session(
            FIXTURES / "claudecode" / "-tmp-demo" / relative_path
        )
        self.assertIsNotNone(info)
        assert info is not None
        return info

    def test_root_session_keeps_root_identity(self) -> None:
        info = self._parse_fixture("root-session.jsonl")

        self.assertEqual(info.session_id, "root-session")
        self.assertEqual(info.thread_id, "root-session")
        self.assertIsNone(info.parent_thread_id)
        self.assertIsNone(info.parent_session_id)
        self.assertEqual(info.spawn_depth, 0)

    def test_subagent_uses_agent_identity_and_root_parent(self) -> None:
        info = self._parse_fixture("root-session/subagents/agent-worker.jsonl")

        self.assertEqual(info.session_id, "agent-worker")
        self.assertEqual(info.thread_id, "worker")
        self.assertEqual(info.parent_thread_id, "root-session")
        self.assertEqual(info.parent_session_id, "root-session")
        self.assertGreaterEqual(info.spawn_depth, 1)
        self.assertEqual(info.total_input_tokens, 600)
        self.assertEqual(info.cache_creation_5m_input_tokens, 50)
        self.assertEqual(info.cache_creation_1h_input_tokens, 150)
        self.assertEqual(info.cache_creation_unknown_input_tokens, 0)
        assert_daily_matches_session(self, info)

    def test_workflow_journal_path_is_a_nonroot_agent(self) -> None:
        info = self._parse_fixture(
            "root-session/subagents/workflows/wf-fixture/journal.jsonl"
        )

        self.assertEqual(info.session_id, "agent-worker")
        self.assertEqual(info.thread_id, "worker")
        self.assertEqual(info.parent_thread_id, "root-session")
        self.assertEqual(info.parent_session_id, "root-session")
        self.assertGreaterEqual(info.spawn_depth, 1)


class ClaudeCacheCreationTests(unittest.TestCase):
    def _parse(self, records: list[dict]):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-cache.jsonl"
            write_jsonl(path, records)
            info = parse_claudecode_session(path)
        self.assertIsNotNone(info)
        assert info is not None
        return info

    def _parse_fixture(self, name: str):
        info = parse_claudecode_session(FIXTURES / "claudecode" / name)
        self.assertIsNotNone(info)
        assert info is not None
        return info

    @staticmethod
    def _assistant(ts: str, msg_id: str, usage: dict) -> dict:
        return {
            "timestamp": ts,
            "type": "assistant",
            "message": {
                "id": msg_id,
                "model": "claude-fable-5",
                "usage": usage,
                "content": [{"type": "text", "text": "hi"}],
            },
        }

    def test_cache_creation_with_breakdown(self) -> None:
        info = self._parse([
            {
                "timestamp": "2026-07-02T10:00:00Z",
                "type": "user",
                "cwd": "/tmp/demo",
                "message": {"content": "hello"},
            },
            self._assistant("2026-07-02T10:00:05Z", "msg-1", {
                "input_tokens": 10,
                "cache_creation_input_tokens": 9_000,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 1_000,
                    "ephemeral_1h_input_tokens": 8_000,
                },
                "cache_read_input_tokens": 20_000,
                "output_tokens": 50,
            }),
        ])
        self.assertEqual(info.cache_creation_input_tokens, 9_000)
        self.assertEqual(info.cache_creation_5m_input_tokens, 1_000)
        self.assertEqual(info.cache_creation_1h_input_tokens, 8_000)
        self.assertEqual(info.cache_creation_unknown_input_tokens, 0)
        self.assertEqual(info.fresh_input_tokens, 10)
        self.assertEqual(info.cached_input_tokens, 20_000)
        # Every prompt token arrives exactly one way: fresh, written, or read.
        self.assertEqual(info.total_input_tokens, 10 + 9_000 + 20_000)

    def test_cache_creation_without_breakdown_is_explicitly_unknown(self) -> None:
        info = self._parse([
            self._assistant("2026-07-02T10:00:05Z", "msg-1", {
                "input_tokens": 100,
                "cache_creation_input_tokens": 5_000,
                "cache_read_input_tokens": 0,
                "output_tokens": 300,
            }),
        ])
        self.assertEqual(info.cache_creation_input_tokens, 5_000)
        self.assertEqual(info.cache_creation_5m_input_tokens, 0)
        self.assertEqual(info.cache_creation_1h_input_tokens, 0)
        self.assertEqual(info.cache_creation_unknown_input_tokens, 5_000)

    def test_last_matching_usage_iteration_supplies_cache_split(self) -> None:
        info = self._parse_fixture("cache-iteration-match.jsonl")

        self.assertEqual(info.cache_creation_input_tokens, 600)
        self.assertEqual(info.cache_creation_5m_input_tokens, 100)
        self.assertEqual(info.cache_creation_1h_input_tokens, 500)
        self.assertEqual(info.cache_creation_unknown_input_tokens, 0)
        self.assertEqual(len(info.message_usage), 1)
        claim = info.message_usage[0]
        self.assertEqual(claim.cache_creation_5m_input_tokens, 100)
        self.assertEqual(claim.cache_creation_1h_input_tokens, 500)
        self.assertEqual(claim.cache_creation_unknown_input_tokens, 0)
        assert_daily_matches_session(self, info)

    def test_contradictory_split_becomes_unknown_remainder(self) -> None:
        info = self._parse_fixture("cache-unknown-remainder.jsonl")

        self.assertEqual(info.cache_creation_input_tokens, 600)
        self.assertEqual(info.cache_creation_5m_input_tokens, 0)
        self.assertEqual(info.cache_creation_1h_input_tokens, 0)
        self.assertEqual(info.cache_creation_unknown_input_tokens, 600)
        self.assertEqual(
            info.cache_creation_input_tokens,
            info.cache_creation_5m_input_tokens
            + info.cache_creation_1h_input_tokens
            + info.cache_creation_unknown_input_tokens,
        )
        assert_daily_matches_session(self, info)

    def test_daily_usage_buckets_by_event_day(self) -> None:
        info = self._parse([
            {
                "timestamp": "2026-04-30T23:00:00Z",
                "type": "user",
                "cwd": "/tmp/demo",
                "message": {"content": "start of a long session"},
            },
            self._assistant("2026-04-30T23:10:00Z", "msg-1", {
                "input_tokens": 100,
                "cache_read_input_tokens": 400,
                "output_tokens": 40,
            }),
            self._assistant("2026-05-02T08:00:00Z", "msg-2", {
                "input_tokens": 200,
                "cache_creation_input_tokens": 1_000,
                "cache_read_input_tokens": 800,
                "output_tokens": 60,
            }),
        ])
        days = {d.day: d for d in info.daily_usage}
        self.assertEqual(sorted(days), ["2026-04-30", "2026-05-02"])
        self.assertEqual(days["2026-04-30"].total_input_tokens, 500)
        self.assertEqual(days["2026-04-30"].user_message_count, 1)
        self.assertEqual(days["2026-04-30"].assistant_message_count, 1)
        self.assertEqual(days["2026-05-02"].total_input_tokens, 2_000)
        self.assertEqual(days["2026-05-02"].cache_creation_input_tokens, 1_000)
        self.assertEqual(
            info.total_input_tokens,
            sum(d.total_input_tokens for d in info.daily_usage),
        )

    def test_deduplicated_retries_keep_highest_output_copy(self) -> None:
        info = self._parse([
            self._assistant("2026-07-02T10:00:05Z", "msg-1", {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "output_tokens": 5,
            }),
            self._assistant("2026-07-02T10:00:09Z", "msg-1", {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "output_tokens": 80,
            }),
        ])
        self.assertEqual(info.assistant_message_count, 1)
        self.assertEqual(info.total_output_tokens, 80)
        self.assertEqual(len(info.daily_usage), 1)
        self.assertEqual(info.daily_usage[0].assistant_message_count, 1)


class DailyResidualUsageTests(unittest.TestCase):
    def test_untimestamped_usage_uses_first_valid_event_day(self) -> None:
        info = parse_claudecode_session(
            FIXTURES / "claudecode" / "residual-day.jsonl"
        )
        self.assertIsNotNone(info)
        assert info is not None

        self.assertEqual(len(info.daily_usage), 1)
        residual = info.daily_usage[0]
        self.assertEqual(residual.day, "2026-07-03")
        self.assertTrue(residual.approximated)
        self.assertEqual(residual.total_input_tokens, 60)
        self.assertEqual(residual.total_output_tokens, 40)
        self.assertEqual(residual.user_message_count, 1)
        self.assertEqual(residual.assistant_message_count, 1)
        self.assertEqual(residual.cache_creation_unknown_input_tokens, 20)
        self.assertEqual(info.message_usage[0].day, "2026-07-03")
        assert_daily_matches_session(self, info)

    def test_all_untimestamped_usage_uses_file_mtime_day(self) -> None:
        records = [
            {
                "type": "assistant",
                "sessionId": "mtime-session",
                "uuid": "mtime-uuid",
                "requestId": "mtime-request",
                "message": {
                    "id": "mtime-message",
                    "model": "claude-fable-5",
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "cache_read_input_tokens": 4,
                        "output_tokens": 5,
                    },
                    "content": [{"type": "text", "text": "done"}],
                },
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mtime-session.jsonl"
            write_jsonl(path, records)
            mtime = datetime(2026, 1, 2, 12, tzinfo=timezone.utc).timestamp()
            os.utime(path, (mtime, mtime))
            info = parse_claudecode_session(path)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(len(info.daily_usage), 1)
        self.assertEqual(info.daily_usage[0].day, "2026-01-02")
        self.assertTrue(info.daily_usage[0].approximated)
        self.assertEqual(info.message_usage[0].day, "2026-01-02")
        assert_daily_matches_session(self, info)


if __name__ == "__main__":
    unittest.main()
