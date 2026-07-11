import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from logpile.parsers import (
    _normalize_session_path,
    file_hash,
    parse_claudecode_session,
    parse_codex_session,
    render_codex_transcript,
)


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record))
            fh.write("\n")


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
            self.assertEqual(info.session_id, "sess-1")
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
            self.assertEqual(info.parent_session_id, "parent-123")
            self.assertEqual(info.spawn_depth, 2)

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
            self.assertEqual(info.session_id, "legacy-1")
            self.assertEqual(info.user_message_count, 1)
            self.assertEqual(info.assistant_message_count, 1)
            self.assertEqual(info.tool_call_count, 1)
            self.assertEqual(info.error_count, 0)
            self.assertEqual(info.tool_calls[0].command, "ls")
            self.assertFalse(info.tool_calls[0].is_error)

    def test_private_marker_skips_modern_codex_session(self) -> None:
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
            self.assertIsNone(parse_codex_session(path))

    def test_file_hash_reads_past_old_prefix_limit(self) -> None:
        prefix = b"a" * (600 * 1024)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.jsonl"
            path.write_bytes(prefix + b"old")
            old_hash = file_hash(path)

            path.write_bytes(prefix + b"new")
            new_hash = file_hash(path)

            self.assertNotEqual(old_hash, new_hash)

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
    """Resume/fork snapshots replay prior history re-stamped into one second;
    only the live continuation may count."""

    def _parse(self, records: list[dict]):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "rollout-replay.jsonl"
            write_jsonl(path, records)
            info = parse_codex_session(path)
        self.assertIsNotNone(info)
        assert info is not None
        return info

    def test_replay_burst_skipped_but_baselines_deltas(self) -> None:
        burst = "2026-06-08T11:39:04"
        info = self._parse([
            _codex_meta(burst + ".050Z"),
            _codex_user_message(burst + ".100Z", "Original task from April"),
            _codex_token_count(burst + ".200Z", 10_000, 8_000, 500),
            _codex_token_count(burst + ".300Z", 500_000, 400_000, 9_000),
            _codex_token_count(burst + ".400Z", 1_000_000, 800_000, 20_000),
            # live continuation
            _codex_token_count("2026-06-08T12:00:00.000Z", 1_050_000, 840_000, 21_000),
        ])
        # Only the post-burst delta: input 50k of which 40k cached, out 1k.
        self.assertEqual(info.total_input_tokens, 50_000)
        self.assertEqual(info.fresh_input_tokens, 10_000)
        self.assertEqual(info.cached_input_tokens, 40_000)
        self.assertEqual(info.total_output_tokens, 1_000)

    def test_replay_messages_and_tools_not_counted_but_topic_kept(self) -> None:
        burst = "2026-06-08T11:39:04"
        info = self._parse([
            _codex_meta(burst + ".050Z"),
            _codex_user_message(burst + ".100Z", "Original task from April"),
            _codex_assistant_message(burst + ".150Z", "Replayed answer"),
            _codex_function_call(burst + ".160Z", "call-old", "pytest tests/"),
            _codex_token_count(burst + ".200Z", 10_000, 8_000, 500),
            _codex_token_count(burst + ".300Z", 20_000, 16_000, 900),
            # live continuation
            _codex_user_message("2026-06-08T12:00:00.000Z", "Keep going please"),
            _codex_assistant_message("2026-06-08T12:00:30.000Z", "Live answer"),
            _codex_function_call("2026-06-08T12:01:00.000Z", "call-new", "ruff check ."),
            _codex_token_count("2026-06-08T12:02:00.000Z", 25_000, 18_000, 1_100),
        ])
        self.assertEqual(info.user_message_count, 1)
        self.assertEqual(info.assistant_message_count, 1)
        self.assertEqual(info.tool_call_count, 1)
        self.assertEqual(info.tool_calls[0].call_id, "call-new")
        # The replayed first message still names the session topic.
        self.assertEqual(info.first_user_message, "Original task from April")

    def test_fresh_session_multiline_first_second_not_treated_as_replay(self) -> None:
        # A normal session start writes several records inside one second,
        # but never two token_count events.
        start = "2026-06-08T11:39:04"
        info = self._parse([
            _codex_meta(start + ".050Z"),
            _codex_user_message(start + ".100Z", "Fresh task"),
            _codex_token_count(start + ".900Z", 1_000, 600, 50),
            _codex_token_count("2026-06-08T11:40:10.000Z", 3_000, 2_400, 120),
        ])
        self.assertEqual(info.user_message_count, 1)
        self.assertEqual(info.total_input_tokens, 3_000)
        self.assertEqual(info.fresh_input_tokens, 600)
        self.assertEqual(info.cached_input_tokens, 2_400)
        self.assertEqual(info.total_output_tokens, 120)

    def test_counter_reset_clamps_to_zero(self) -> None:
        info = self._parse([
            _codex_meta("2026-05-01T00:00:00.000Z"),
            _codex_token_count("2026-05-01T01:00:00.000Z", 1_000, 800, 100),
            _codex_token_count("2026-05-01T02:00:00.000Z", 200, 100, 10),  # reset
            _codex_token_count("2026-05-01T03:00:00.000Z", 900, 700, 60),
        ])
        # First event counts in full; the reset and the sub-baseline recovery
        # clamp to zero instead of double counting.
        self.assertEqual(info.total_input_tokens, 1_000)
        self.assertEqual(info.fresh_input_tokens, 200)
        self.assertEqual(info.cached_input_tokens, 800)
        self.assertEqual(info.total_output_tokens, 100)

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


class ClaudeCacheCreationTests(unittest.TestCase):
    def _parse(self, records: list[dict]):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude-cache.jsonl"
            write_jsonl(path, records)
            info = parse_claudecode_session(path)
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
        self.assertEqual(info.fresh_input_tokens, 10)
        self.assertEqual(info.cached_input_tokens, 20_000)
        # Every prompt token arrives exactly one way: fresh, written, or read.
        self.assertEqual(info.total_input_tokens, 10 + 9_000 + 20_000)

    def test_cache_creation_without_breakdown_assumes_5m(self) -> None:
        info = self._parse([
            self._assistant("2026-07-02T10:00:05Z", "msg-1", {
                "input_tokens": 100,
                "cache_creation_input_tokens": 5_000,
                "cache_read_input_tokens": 0,
                "output_tokens": 300,
            }),
        ])
        self.assertEqual(info.cache_creation_input_tokens, 5_000)
        self.assertEqual(info.cache_creation_5m_input_tokens, 5_000)
        self.assertEqual(info.cache_creation_1h_input_tokens, 0)

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


if __name__ == "__main__":
    unittest.main()
