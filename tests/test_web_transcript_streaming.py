import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun is not installed")
def test_next_transcript_reader_caps_bytes_and_paginates_turns(tmp_path: Path) -> None:
    transcript = tmp_path / "large-codex.jsonl"
    filler = json.dumps({"type": "progress", "message": "ordinary filler"}) + "\n"
    with transcript.open("w", encoding="utf-8") as handle:
        while handle.tell() < 5 * 1024 * 1024:
            handle.write(filler)
        for index in range(250):
            handle.write(
                json.dumps(
                    {
                        "timestamp": f"2026-07-11T12:{index // 60:02d}:{index % 60:02d}Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": f"turn {index:03d}"}
                            ],
                        },
                    }
                )
                + "\n"
            )

    script = """
import { renderCodexTranscript } from './web/src/lib/parsers.ts';
const file = process.argv[1];
const first = await renderCodexTranscript(file);
const second = await renderCodexTranscript(file, { cursor: first.nextCursor ?? 0 });
const third = await renderCodexTranscript(file, { cursor: second.nextCursor ?? 0 });
process.stdout.write(JSON.stringify({ first, second, third }));
"""
    result = subprocess.run(
        ["bun", "--eval", script, str(transcript)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    first = payload["first"]
    second = payload["second"]
    third = payload["third"]

    assert first["byteLimitReached"] is True
    assert first["bytesRead"] <= 4 * 1024 * 1024
    assert first["turns"] == []
    assert 0 < first["nextCursor"] < transcript.stat().st_size

    assert len(second["turns"]) == 100
    assert second["bytesRead"] <= 4 * 1024 * 1024
    assert second["nextCursor"] > first["nextCursor"]
    assert len(third["turns"]) == 100
    assert third["nextCursor"] > second["nextCursor"]

    parser_source = (ROOT / "web" / "src" / "lib" / "parsers.ts").read_text()
    assert "readFileSync" not in parser_source
    review_ui = (
        ROOT / "web" / "src" / "app" / "publish" / "review" / "[id]" / "page.tsx"
    ).read_text()
    assert "No configured patterns detected; manual review required" in review_ui


@pytest.mark.skipif(shutil.which("bun") is None, reason="bun is not installed")
def test_next_transcript_reader_advances_past_oversized_record(tmp_path: Path) -> None:
    transcript = tmp_path / "oversized-record.jsonl"
    with transcript.open("w", encoding="utf-8") as handle:
        handle.write('{"type":"progress","padding":"')
        handle.write("x" * (9 * 1024 * 1024))
        handle.write('"}\n')
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-11T12:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "reachable later turn"}
                        ],
                    },
                }
            )
            + "\n"
        )

    script = """
import { renderCodexTranscript } from './web/src/lib/parsers.ts';
const file = process.argv[1];
const first = await renderCodexTranscript(file);
const second = await renderCodexTranscript(file, { cursor: first.nextCursor ?? 0 });
const third = await renderCodexTranscript(file, { cursor: second.nextCursor ?? 0 });
process.stdout.write(JSON.stringify({ first, second, third }));
"""
    result = subprocess.run(
        ["bun", "--eval", script, str(transcript)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    first = payload["first"]
    second = payload["second"]
    third = payload["third"]

    assert first["byteLimitReached"] is True
    assert second["byteLimitReached"] is True
    assert first["nextCursor"] == 4 * 1024 * 1024
    assert second["nextCursor"] == 8 * 1024 * 1024
    assert first["bytesRead"] <= 4 * 1024 * 1024
    assert second["bytesRead"] <= 4 * 1024 * 1024
    assert third["nextCursor"] is None
    assert [turn["content"] for turn in third["turns"]] == ["reachable later turn"]
