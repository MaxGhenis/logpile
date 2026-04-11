import json
import tempfile
import unittest
from pathlib import Path

from logpile.parsers import (
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
            self.assertEqual(info.last_timestamp, "2026-04-10T10:00:07Z")
            self.assertEqual(info.user_message_count, 1)
            self.assertEqual(info.assistant_message_count, 1)
            self.assertEqual(info.tool_call_count, 1)
            self.assertEqual(info.error_count, 1)
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


if __name__ == "__main__":
    unittest.main()
