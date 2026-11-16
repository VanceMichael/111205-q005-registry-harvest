"""流式接收与跨块多字节字符测试。"""

import unittest
from base64 import b64decode

from harvest.stream import (
    ByteStream,
    StreamDecoder,
    char_span_to_byte_span,
    decode_stream,
    decode_with_origins,
    split_frames,
)
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class StreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        samples = json.loads((FIXTURES / "responses.json").read_text())["samples"]
        self.samples = {
            sample["source"]: {
                "raw": b64decode(sample["body_base64"], validate=True),
                "chunks": sample["chunks"],
                "http_charset": sample["http_charset"],
            }
            for sample in samples
        }

    def test_frames_reassemble_to_original_bytes(self) -> None:
        for sample in self.samples.values():
            frames = split_frames(sample["raw"], sample["chunks"])
            self.assertEqual(b"".join(frames), sample["raw"])
            # 末帧确实由剩余字节组成。
            self.assertEqual(frames[-1], sample["raw"][sample["chunks"][-1]:])

    def test_incomplete_multibyte_at_boundary_is_not_a_failure(self) -> None:
        # GB18030 双字节字符 企 = C6 F3，边界 60/61 把它切成 [C6][F3]。
        sample_a = self.samples["a"]
        frames = split_frames(sample_a["raw"], sample_a["chunks"])
        decoder = StreamDecoder("gb18030")
        # 只收到首字节且尚未结束：不能抛错，也不能产出替换字符。
        partial = decoder.feed(frames[0], final=False)
        self.assertNotIn("�", partial)
        # 继续接收剩余帧（只有最后一帧才 final=True），全程不抛错。
        for frame in frames[1:]:
            decoder.feed(frame, final=False)
        decoder.feed(b"", final=True)
        self.assertEqual(decoder.text, sample_a["raw"].decode("gb18030"))

    def test_utf8_three_byte_character_split_across_frames(self) -> None:
        sample_b = self.samples["b"]
        frames = split_frames(sample_b["raw"], sample_b["chunks"])
        # 边界 36/37 落在“日”（E6 97 A5）内部。
        self.assertEqual(frames[0][-1:], b"\xe6")
        self.assertEqual(frames[1], b"\x97")
        decoded = decode_stream(frames, "utf-8")
        self.assertEqual(decoded, sample_b["raw"].decode("utf-8"))

    def test_truly_truncated_stream_fails_only_at_final(self) -> None:
        decoder = StreamDecoder("utf-8")
        decoder.feed(b"<x>\xe6\x97", final=False)  # 暂不完整，不报错
        with self.assertRaises(UnicodeDecodeError):
            decoder.feed(b"", final=True)

    def test_byte_stream_tracks_frame_bounds(self) -> None:
        stream = ByteStream()
        stream.append(b"ab")
        stream.append(b"cde")
        self.assertEqual(stream.length, 5)
        self.assertEqual(stream.finish(), b"abcde")
        self.assertEqual(stream.frame_index_at(0), 0)
        self.assertEqual(stream.frame_index_at(2), 1)
        self.assertTrue(stream.spans_frame_boundary(1, 3))
        self.assertFalse(stream.spans_frame_boundary(3, 4))

    def test_origin_mapping_points_back_to_bytes(self) -> None:
        sample_b = self.samples["b"]
        text, origins = decode_with_origins(sample_b["raw"], "utf-8")
        # 字符“日”起始于字节 35，正是被边界 36/37 切开的三字节序列。
        day_offset = text.index("日")
        self.assertEqual(origins[day_offset], 35)
        start, end = char_span_to_byte_span(origins, len(sample_b["raw"]),
                                             day_offset, day_offset + 1)
        self.assertEqual(sample_b["raw"][start:end].decode("utf-8"), "日")

    def test_gb18030_origin_mapping(self) -> None:
        sample_a = self.samples["a"]
        text, origins = decode_with_origins(sample_a["raw"], "gb18030")
        qi_offset = text.index("企")
        self.assertEqual(sample_a["raw"][origins[qi_offset]:origins[qi_offset] + 2],
                         b"\xc6\xf3")


if __name__ == "__main__":
    unittest.main()
