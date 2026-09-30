"""Incremental parsing is exactly a full parse of the same bytes.

The differential property: after any sequence of appends (cut anywhere,
including mid-line, with or without a trailing newline), resuming the
persisted parse state from the previous checkpoint yields the same
SessionInfo as parsing the whole current file from scratch.
"""

import json
import os
import tempfile
import unittest
from dataclasses import asdict, fields
from pathlib import Path

import legacy_parsers
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from logpile.parsers import (
    PARSE_STATE_VERSION,
    ParseCheckpoint,
    PrivateSessionMarker,
    SessionInfo,
    parse_claudecode_session,
    parse_codex_session,
    parse_transcript,
    remove_parse_state,
)
from logpile.transcript_io import scan_transcript

FULL_PARSERS = {"claudecode": parse_claudecode_session, "codex": parse_codex_session}

TIMESTAMPS = st.sampled_from(
    [
        "2026-09-28T23:59:58Z",
        "2026-09-29T00:00:01Z",
        "2026-09-29T10:15:00.123Z",
        "2026-09-30T08:00:00+02:00",
        "not-a-time",
        "",
        None,
    ]
)
TEXT = st.sampled_from(
    [
        "fix the sync",
        "why is it slow?",
        "  ",
        "<system-reminder>x</system-reminder> real ask",
        # An inline privacy marker, spelled so this file does not contain it.
        "run " + "logpile" + ":private please",
        "ok",
        "é unicode ✓",
    ]
)
IDS = st.sampled_from(["a", "b", "c", "d"])


def _maybe(key, strategy):
    return st.one_of(st.just({}), strategy.map(lambda value: {key: value}))


def _merge(*parts):
    merged = {}
    for part in parts:
        merged.update(part)
    return merged


claude_user = st.builds(
    lambda text, blocks, ts, meta, cwd, sid: _merge(
        {
            "type": "user",
            "message": {
                "content": [{"type": "text", "text": text}] if blocks else text
            },
        },
        ts,
        meta,
        cwd,
        sid,
    ),
    TEXT,
    st.booleans(),
    _maybe("timestamp", TIMESTAMPS),
    _maybe("isMeta", st.booleans()),
    _maybe("cwd", st.sampled_from(["/Users/me/repo", "/Users/me/other"])),
    _maybe("sessionId", st.sampled_from(["s-1", "s-2"])),
)
claude_tool_result = st.builds(
    lambda tool_id, is_error, ts: _merge(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "is_error": is_error,
                    }
                ]
            },
        },
        ts,
    ),
    IDS.map(lambda value: f"toolu_{value}"),
    st.booleans(),
    _maybe("timestamp", TIMESTAMPS),
)
claude_assistant = st.builds(
    lambda mid, rid, uuid, usage, model, tool, ts: _merge(
        {
            "type": "assistant",
            "message": _merge(
                {"id": f"msg_{mid}", "usage": usage},
                model,
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"toolu_{tool[0]}",
                            "name": tool[1],
                            "input": {"command": tool[2], "file_path": "src/app.py"},
                        }
                    ]
                    if tool
                    else [{"type": "text", "text": "done"}]
                },
            ),
        },
        rid,
        uuid,
        ts,
    ),
    IDS,
    _maybe("requestId", IDS.map(lambda value: f"req_{value}")),
    _maybe("uuid", IDS.map(lambda value: f"uuid-{value}")),
    st.fixed_dictionaries(
        {
            "input_tokens": st.integers(0, 50),
            "output_tokens": st.integers(0, 50),
            "cache_read_input_tokens": st.integers(0, 50),
            "cache_creation_input_tokens": st.integers(0, 50),
        },
        optional={
            "cache_creation": st.fixed_dictionaries(
                {
                    "ephemeral_5m_input_tokens": st.integers(0, 30),
                    "ephemeral_1h_input_tokens": st.integers(0, 30),
                }
            )
        },
    ),
    _maybe("model", st.sampled_from(["claude-opus-5-5", "claude-sonnet-5-5"])),
    st.one_of(
        st.none(),
        st.tuples(
            IDS,
            st.sampled_from(["Bash", "Read", "Edit"]),
            st.sampled_from(["pytest -q", "cat README.md", "git commit -m x"]),
        ),
    ),
    _maybe("timestamp", TIMESTAMPS),
)
claude_other = st.sampled_from(
    [
        {"type": "summary", "summary": "x"},
        {"type": "system", "content": "compacted"},
        {"type": "assistant", "isSidechain": True, "agentId": "a1", "message": {}},
    ]
)
claude_records = st.lists(
    st.one_of(claude_user, claude_tool_result, claude_assistant, claude_other),
    min_size=0,
    max_size=14,
)


def _codex(record_type, payload, ts=None):
    record = {"type": record_type, "payload": payload}
    if ts is not None:
        record["timestamp"] = ts
    return record


FORK_TS = "2026-09-29T10:00:00Z"
codex_body = st.one_of(
    st.builds(
        lambda cwd, model, ts: _codex("turn_context", {"cwd": cwd, "model": model}, ts),
        st.sampled_from(["/Users/me/repo", "/Users/me/other"]),
        st.sampled_from(["gpt-6-luna", "gpt-6.1-sol"]),
        TIMESTAMPS,
    ),
    st.builds(
        lambda started, ts: _codex(
            "event_msg", {"type": "task_started", "started_at": started}, ts
        ),
        st.sampled_from([1790676000, 1790676001, 1790676000123]),
        st.sampled_from([FORK_TS, "2026-09-29T10:00:01Z", None]),
    ),
    st.builds(
        lambda counts, ts: _codex(
            "event_msg",
            {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": counts[0],
                        "cached_input_tokens": counts[1],
                        "output_tokens": counts[2],
                        "reasoning_output_tokens": counts[3],
                    }
                },
            },
            ts,
        ),
        st.one_of(
            st.just((0, 0, 0, 0)),
            st.tuples(
                st.integers(0, 400),
                st.integers(0, 100),
                st.integers(0, 200),
                st.integers(0, 50),
            ),
        ),
        TIMESTAMPS,
    ),
    st.builds(
        lambda role, text, ts: _codex(
            "response_item",
            {
                "type": "message",
                "role": role,
                "content": [
                    {
                        "type": "input_text" if role == "user" else "output_text",
                        "text": text,
                    }
                ],
            },
            ts,
        ),
        st.sampled_from(["user", "assistant", "developer"]),
        TEXT,
        TIMESTAMPS,
    ),
    st.builds(
        lambda call, cmd, ts: _codex(
            "response_item",
            {
                "type": "function_call",
                "name": "shell",
                "call_id": f"call_{call}",
                "arguments": json.dumps({"command": ["bash", "-lc", cmd]}),
            },
            ts,
        ),
        IDS,
        st.sampled_from(["pytest -q", "cat src/app.py", "ls"]),
        TIMESTAMPS,
    ),
    st.builds(
        lambda call, code, ts: _codex(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": f"call_{call}",
                "output": json.dumps({"output": "x", "metadata": {"exit_code": code}}),
            },
            ts,
        ),
        IDS,
        st.sampled_from([0, 1]),
        TIMESTAMPS,
    ),
)
codex_head = st.sampled_from(
    [
        [],
        [
            _codex(
                "session_meta",
                {"id": "leaf", "timestamp": FORK_TS, "cwd": "/Users/me/repo"},
                FORK_TS,
            )
        ],
        [
            _codex(
                "session_meta",
                {"id": "leaf", "forked_from_id": "parent", "timestamp": FORK_TS},
                FORK_TS,
            ),
            _codex("session_meta", {"id": "parent", "cwd": "/Users/me/other"}, FORK_TS),
        ],
    ]
)
codex_records = st.tuples(codex_head, st.lists(codex_body, max_size=14)).map(
    lambda parts: parts[0] + parts[1]
)


def _serialize(records, trailing_newline, junk_line, newline="\n"):
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    if junk_line is not None and lines:
        lines.insert(junk_line % (len(lines) + 1), '{"truncated": ')
    data = newline.join(lines)
    if lines and trailing_newline:
        data += newline
    return data.encode("utf-8")


NEWLINES = st.sampled_from(["\n", "\r\n", "\r"])


def _plain(info):
    """Compare parse results across the new and the frozen legacy module."""
    if info is None:
        return None
    if hasattr(info, "marker"):
        return ("marker", info.session_id, info.source, info.marker)
    data = {}
    for item in fields(info):
        value = getattr(info, item.name)
        if item.name in {"tool_calls", "session_paths", "daily_usage", "message_usage"}:
            value = [asdict(entry) for entry in value]
        data[item.name] = value
    return data


def _materialize(info):
    if info is None or isinstance(info, PrivateSessionMarker):
        return info
    data = asdict(
        SessionInfo(
            **{
                **{name: getattr(info, name) for name in info.__dataclass_fields__},
                "tool_calls": list(info.tool_calls),
                "session_paths": list(info.session_paths),
                "daily_usage": list(info.daily_usage),
                "message_usage": list(info.message_usage),
            }
        )
    )
    return data


class IncrementalParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_appends(self, source, data, cuts):
        """Grow one file through ``cuts`` and compare every step."""
        path = self.root / f"{source}-session.jsonl"
        state = self.root / f"{source}.state.sqlite"
        path.unlink(missing_ok=True)
        remove_parse_state(state)
        path.touch()
        checkpoint = None
        written = 0
        resumed_any = False
        for cut in [*sorted(set(cuts)), len(data)]:
            cut = min(cut, len(data))
            if cut < written:
                continue
            with path.open("ab") as handle:
                handle.write(data[written:cut])
            written = cut
            scan = scan_transcript(
                path,
                prefix_offsets=[checkpoint.offset] if checkpoint else [],
            )
            parsed = parse_transcript(
                source, path, scan, state_path=state, checkpoint=checkpoint
            )
            try:
                incremental = _materialize(parsed.info)
                resumed_any = resumed_any or parsed.resumed_from > 0
                if parsed.checkpoint is not None:
                    checkpoint = parsed.checkpoint
            finally:
                parsed.close()
            full = _materialize(FULL_PARSERS[source](path))
            self.assertEqual(incremental, full, f"diverged after {cut} bytes")
        return resumed_any

    @settings(
        max_examples=150,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        records=claude_records,
        trailing_newline=st.booleans(),
        junk_line=st.one_of(st.none(), st.integers(0, 20)),
        cuts=st.lists(st.integers(0, 6000), max_size=5),
        newline=NEWLINES,
    )
    def test_claude_incremental_equals_full_parse(
        self, records, trailing_newline, junk_line, cuts, newline
    ):
        data = _serialize(records, trailing_newline, junk_line, newline)
        event(f"resumed={self._run_appends('claudecode', data, cuts)}")

    @settings(
        max_examples=150,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        records=codex_records,
        trailing_newline=st.booleans(),
        junk_line=st.one_of(st.none(), st.integers(0, 20)),
        cuts=st.lists(st.integers(0, 6000), max_size=5),
        newline=NEWLINES,
    )
    def test_codex_incremental_equals_full_parse(
        self, records, trailing_newline, junk_line, cuts, newline
    ):
        data = _serialize(records, trailing_newline, junk_line, newline)
        event(f"resumed={self._run_appends('codex', data, cuts)}")

    @settings(
        max_examples=150,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    @given(
        source=st.sampled_from(["claudecode", "codex"]),
        claude=claude_records,
        codex=codex_records,
        trailing_newline=st.booleans(),
        junk_line=st.one_of(st.none(), st.integers(0, 20)),
        newline=NEWLINES,
    )
    def test_streaming_parsers_match_the_legacy_two_pass_parsers(
        self, source, claude, codex, trailing_newline, junk_line, newline
    ):
        records = claude if source == "claudecode" else codex
        path = self.root / f"{source}-legacy.jsonl"
        path.write_bytes(_serialize(records, trailing_newline, junk_line, newline))
        legacy = (
            legacy_parsers.parse_claudecode_session
            if source == "claudecode"
            else legacy_parsers.parse_codex_session
        )(path)
        self.assertEqual(_plain(FULL_PARSERS[source](path)), _plain(legacy))

    def test_codex_empty_timestamps_match_the_legacy_parser(self):
        path = self._write(
            "empty-ts.jsonl",
            [
                {"type": "session_meta", "timestamp": "", "payload": {"id": "x"}},
                {
                    "type": "response_item",
                    "timestamp": "",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello there"}],
                    },
                },
            ],
        )
        info = parse_codex_session(path)
        self.assertEqual(info.first_timestamp, "")
        self.assertEqual(_plain(info), _plain(legacy_parsers.parse_codex_session(path)))

    def _write(self, name, lines):
        path = self.root / name
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def _claude_lines(self, count, start=0):
        return [
            {
                "type": "assistant",
                "timestamp": "2026-09-29T10:00:00Z",
                "message": {
                    "id": f"msg_{index}",
                    "usage": {"input_tokens": 1, "output_tokens": index},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"toolu_{index}",
                            "name": "Bash",
                            "input": {"command": "pytest -q"},
                        }
                    ],
                },
            }
            for index in range(start, start + count)
        ]

    def test_resume_parses_only_appended_bytes(self):
        path = self._write("s.jsonl", self._claude_lines(50))
        state = self.root / "s.state"
        first = parse_transcript(
            "claudecode", path, scan_transcript(path), state_path=state
        )
        checkpoint = first.checkpoint
        first.close()
        self.assertEqual(checkpoint.offset, path.stat().st_size)
        with path.open("a") as handle:
            for line in self._claude_lines(3, start=50):
                handle.write(json.dumps(line) + "\n")
        scan = scan_transcript(path, prefix_offsets=[checkpoint.offset])
        second = parse_transcript(
            "claudecode", path, scan, state_path=state, checkpoint=checkpoint
        )
        try:
            self.assertEqual(second.resumed_from, checkpoint.offset)
            self.assertEqual(second.info.tool_call_count, 53)
            self.assertEqual(second.info.total_output_tokens, sum(range(53)))
        finally:
            second.close()

    def test_rewritten_prefix_forces_full_parse(self):
        path = self._write("s.jsonl", self._claude_lines(5))
        state = self.root / "s.state"
        first = parse_transcript(
            "claudecode", path, scan_transcript(path), state_path=state
        )
        checkpoint = first.checkpoint
        first.close()
        # Same size and inode, different bytes before the checkpoint.
        data = bytearray(path.read_bytes())
        data[data.index(b"msg_0")] = ord("M")
        with path.open("r+b") as handle:
            handle.write(bytes(data))
        scan = scan_transcript(path, prefix_offsets=[checkpoint.offset])
        second = parse_transcript(
            "claudecode", path, scan, state_path=state, checkpoint=checkpoint
        )
        try:
            self.assertEqual(second.resumed_from, 0)
            self.assertEqual(
                _materialize(second.info), _materialize(parse_claudecode_session(path))
            )
        finally:
            second.close()

    def test_replaced_file_and_stale_generation_force_full_parse(self):
        path = self._write("s.jsonl", self._claude_lines(4))
        state = self.root / "s.state"
        first = parse_transcript(
            "claudecode", path, scan_transcript(path), state_path=state
        )
        checkpoint = first.checkpoint
        first.close()
        stale = ParseCheckpoint(
            offset=checkpoint.offset,
            prefix_sha256=checkpoint.prefix_sha256,
            generation="0" * 32,
            dev=checkpoint.dev,
            ino=checkpoint.ino,
        )
        scan = scan_transcript(path, prefix_offsets=[checkpoint.offset])
        parsed = parse_transcript(
            "claudecode", path, scan, state_path=state, checkpoint=stale
        )
        self.assertEqual(parsed.resumed_from, 0)
        checkpoint = parsed.checkpoint
        parsed.close()

        replacement = self.root / "replacement.jsonl"
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        scan = scan_transcript(path, prefix_offsets=[checkpoint.offset])
        parsed = parse_transcript(
            "claudecode", path, scan, state_path=state, checkpoint=checkpoint
        )
        try:
            self.assertEqual(parsed.resumed_from, 0)
        finally:
            parsed.close()

    def test_other_version_state_is_not_resumed(self):
        path = self._write("s.jsonl", self._claude_lines(4))
        state = self.root / "s.state"
        first = parse_transcript(
            "claudecode", path, scan_transcript(path), state_path=state
        )
        checkpoint = first.checkpoint
        first.close()
        old = ParseCheckpoint(
            offset=checkpoint.offset,
            prefix_sha256=checkpoint.prefix_sha256,
            generation=checkpoint.generation,
            dev=checkpoint.dev,
            ino=checkpoint.ino,
            version=PARSE_STATE_VERSION - 1,
        )
        scan = scan_transcript(path, prefix_offsets=[checkpoint.offset])
        parsed = parse_transcript(
            "claudecode", path, scan, state_path=state, checkpoint=old
        )
        try:
            self.assertEqual(parsed.resumed_from, 0)
        finally:
            parsed.close()

    def test_unterminated_tail_is_provisional(self):
        path = self._write("s.jsonl", self._claude_lines(2))
        with path.open("a") as handle:
            handle.write(json.dumps(self._claude_lines(1, start=2)[0]))  # no newline
        state = self.root / "s.state"
        parsed = parse_transcript(
            "claudecode", path, scan_transcript(path), state_path=state
        )
        checkpoint = parsed.checkpoint
        try:
            # A full parse includes the complete-but-unterminated record...
            self.assertEqual(parsed.info.tool_call_count, 3)
        finally:
            parsed.close()
        # ...but durable state stops at the last terminator, so completing the
        # line later is not double counted.
        self.assertLess(checkpoint.offset, path.stat().st_size)
        with path.open("a") as handle:
            handle.write("\n")
        scan = scan_transcript(path, prefix_offsets=[checkpoint.offset])
        parsed = parse_transcript(
            "claudecode", path, scan, state_path=state, checkpoint=checkpoint
        )
        try:
            self.assertGreater(parsed.resumed_from, 0)
            self.assertEqual(parsed.info.tool_call_count, 3)
        finally:
            parsed.close()

    def test_file_changed_after_scan_raises(self):
        path = self._write("s.jsonl", self._claude_lines(2))
        scan = scan_transcript(path)
        replacement = self.root / "r.jsonl"
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
        with self.assertRaises(FileNotFoundError):
            parse_transcript("claudecode", path, scan)


if __name__ == "__main__":
    unittest.main()
