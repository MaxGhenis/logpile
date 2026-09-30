import hashlib
import tempfile
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from logpile.parsers import _iter_jsonl, file_hash
from logpile.transcript_io import open_text_range, scan_transcript

# Bytes that exercise every newline style, multibyte UTF-8 and invalid UTF-8.
ALPHABET = [b"\n", b"\r", b"\r\n", b"a", b"{", b"}", b'"', b"\xc3\xa9", b"\xff", b" "]
blobs = st.lists(st.sampled_from(ALPHABET), max_size=200).map(b"".join)


def last_line_end(data: bytes) -> int:
    return max(data.rfind(b"\n"), data.rfind(b"\r")) + 1


class TranscriptScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(data=blobs, cut=st.integers(min_value=0, max_value=250))
    def test_scan_hashes_match_independent_digests(self, data, cut):
        self.path.write_bytes(data)
        scan = scan_transcript(self.path, prefix_offsets=[cut, 0, None])
        self.assertEqual(scan.size, len(data))
        self.assertEqual(scan.sha256, file_hash(self.path))
        self.assertEqual(scan.prefix_sha256s[0], hashlib.sha256(b"").hexdigest())
        if cut <= len(data):
            self.assertEqual(
                scan.prefix_sha256s[cut], hashlib.sha256(data[:cut]).hexdigest()
            )
        else:
            self.assertNotIn(cut, scan.prefix_sha256s)
        end = last_line_end(data)
        self.assertEqual(scan.line_end, end)
        self.assertEqual(scan.line_end_sha256, hashlib.sha256(data[:end]).hexdigest())

    def test_scan_spans_chunk_boundaries(self):
        data = (b"x" * (1024 * 1024 - 1)) + b"\n" + (b"y" * 10) + b"\r\n" + b"tail"
        self.path.write_bytes(data)
        offsets = [1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 3]
        scan = scan_transcript(self.path, prefix_offsets=offsets)
        self.assertEqual(scan.line_end, last_line_end(data))
        for offset in offsets:
            self.assertEqual(
                scan.prefix_sha256s[offset], hashlib.sha256(data[:offset]).hexdigest()
            )
        self.assertEqual(scan.sha256, hashlib.sha256(data).hexdigest())

    def test_prefix_matches_requires_same_offset_and_digest(self):
        self.path.write_bytes(b'{"a":1}\n{"b":2}\n')
        scan = scan_transcript(self.path, prefix_offsets=[8])
        digest = hashlib.sha256(b'{"a":1}\n').hexdigest()
        self.assertTrue(scan.prefix_matches(8, digest))
        self.assertFalse(scan.prefix_matches(9, digest))
        self.assertFalse(scan.prefix_matches(8, None))
        self.assertFalse(scan.prefix_matches(8, "0" * 64))
        self.assertFalse(scan.prefix_matches(None, digest))

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(
        records=st.lists(
            st.one_of(
                st.fixed_dictionaries({"k": st.text(max_size=8)}),
                st.just("not json"),
                st.just(""),
            ),
            max_size=12,
        ),
        newline=st.sampled_from(["\n", "\r\n", "\r"]),
        splits=st.lists(st.integers(min_value=0, max_value=10_000), max_size=4),
    )
    def test_ranges_split_on_line_boundaries_parse_like_the_whole_file(
        self, records, newline, splits
    ):
        import json

        lines = [json.dumps(r) if isinstance(r, dict) else r for r in records]
        data = newline.join(lines).encode("utf-8")
        self.path.write_bytes(data)
        whole = list(_iter_jsonl(self.path, report_malformed=False))

        # Cut only at line boundaries (just past a terminator), as sync does.
        boundaries = sorted(
            {0, len(data)}
            | {last_line_end(data[: min(split, len(data))]) for split in splits}
        )
        pieces = []
        with open(self.path, "rb") as handle:
            for start, end in zip(boundaries, boundaries[1:]):
                with open_text_range(handle, start, end) as stream:
                    pieces.extend(_iter_jsonl(stream, report_malformed=False))
        self.assertEqual(pieces, whole)

    def test_text_range_decodes_like_open(self):
        data = b'{"a":"\xc3\xa9"}\n{"b":"\xff"}\r\n{"c":1}'
        self.path.write_bytes(data)
        with (
            open(self.path, "rb") as handle,
            open_text_range(handle, 0, len(data)) as s,
        ):
            ranged = s.read()
        with open(self.path, encoding="utf-8", errors="replace") as f:
            self.assertEqual(ranged, f.read())

    def test_text_range_stops_at_end_even_if_file_grew(self):
        self.path.write_bytes(b'{"a":1}\n')
        with open(self.path, "rb") as handle:
            with open(self.path, "ab") as writer:
                writer.write(b'{"b":2}\n')
            with open_text_range(handle, 0, 8) as stream:
                self.assertEqual(stream.read(), '{"a":1}\n')


if __name__ == "__main__":
    unittest.main()
