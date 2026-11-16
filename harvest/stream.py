"""流式接收器与字符/字节位置映射。

合成响应按 ``chunks``（累计字节边界）切分，边界有意落在多字节字符内部。
本模块保证：

- 分块到达时只做增量解码，暂时不完整的多字节序列保留在解码器内部，
  在 ``final`` 之前绝不抛错、绝不用替换字符兜底；
- 只有整段字节接收完毕、严格解码仍失败时，才由上层判定为永久坏页；
- 解码同时产出 *字符偏移 -> 原始字节偏移* 的映射，使每条结构化字段都能
  回溯到原始片段位置。
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import Optional


def split_frames(raw: bytes, cumulative_bounds: list[int]) -> list[bytes]:
    """按累计字节边界把完整字节切成到达帧，末帧为剩余字节。"""

    frames: list[bytes] = []
    start = 0
    for bound in cumulative_bounds:
        frames.append(raw[start:bound])
        start = bound
    frames.append(raw[start:])
    return frames


@dataclass
class ByteStream:
    """累计接收分块字节，记录每帧的累计边界与总长度。"""

    frames: list[bytes]
    bounds: list[int]

    def __init__(self) -> None:
        self.frames = []
        self.bounds = []
        self._length = 0

    def append(self, chunk: bytes) -> None:
        if not isinstance(chunk, (bytes, bytearray)):
            raise TypeError("stream frames must be bytes")
        chunk = bytes(chunk)
        self._length += len(chunk)
        self.frames.append(chunk)
        self.bounds.append(self._length)

    @property
    def length(self) -> int:
        return self._length

    def finish(self) -> bytes:
        return b"".join(self.frames)

    def frame_index_at(self, byte_offset: int) -> int:
        """返回某字节偏移落在哪一帧（0 起），用于标注跨块字符。"""

        for index, bound in enumerate(self.bounds):
            if byte_offset < bound:
                return index
        return len(self.bounds) - 1

    def spans_frame_boundary(self, start: int, end: int) -> bool:
        """字节区间 [start, end) 是否跨越任意分块边界。"""

        return any(start < bound <= end for bound in self.bounds[:-1])


class StreamDecoder:
    """增量解码器：未结束时容忍跨块的不完整多字节序列。"""

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        self._buffered_text = ""

    def feed(self, chunk: bytes, final: bool = False) -> str:
        decoded = self._decoder.decode(chunk, final=final)
        self._buffered_text += decoded
        return decoded

    @property
    def text(self) -> str:
        return self._buffered_text


def decode_stream(frames: list[bytes], encoding: str) -> str:
    """逐帧增量解码；仅在最后一帧 ``final=True`` 时允许严格报错。"""

    decoder = StreamDecoder(encoding)
    last = len(frames) - 1
    for index, frame in enumerate(frames):
        decoder.feed(frame, final=index == last)
    return decoder.text


def decode_with_origins(
    raw: bytes, encoding: str
) -> tuple[str, list[int]]:
    """严格解码并返回 (文本, 每个字符的起始字节偏移)。

    逐字节喂入增量解码器：多字节序列的首字节进入时记录待定位起点，
    字符产出时把该起点记为它的原始字节位置。这样跨块字符也能准确回溯。
    """

    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    origins: list[int] = []
    text = ""
    pending_start: Optional[int] = None
    for offset, byte in enumerate(raw):
        if pending_start is None:
            pending_start = offset
        emitted = decoder.decode(bytes((byte,)), final=False)
        if emitted:
            for _ in emitted:
                origins.append(pending_start)
            text += emitted
            pending_start = None
    tail = decoder.decode(b"", final=True)
    if tail:
        if pending_start is None:
            pending_start = len(raw)
        for _ in tail:
            origins.append(pending_start)
        text += tail
    # 自检：映射长度必须与字符数一致，且重组结果等于一次性严格解码。
    if len(origins) != len(text):
        raise AssertionError("character origin mapping is misaligned")
    if text != raw.decode(encoding, errors="strict"):
        raise AssertionError("streaming decode disagrees with strict decode")
    return text, origins


def char_span_to_byte_span(
    origins: list[int], raw_len: int, char_start: int, char_end: int
) -> tuple[int, int]:
    """把字符半开区间 [char_start, char_end) 换成字节半开区间。"""

    if not origins:
        return 0, raw_len
    start = origins[max(0, min(char_start, len(origins) - 1))]
    if char_end >= len(origins):
        end = raw_len
    else:
        end = origins[char_end]
    return start, end
