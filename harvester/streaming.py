"""Strict, chunk-aware decoding with explicit provenance.

Design notes
------------
* Bytes are never mutated.  The decoder consumes the original chunks in order
  and keeps an explicit ledger of (chunk byte range, carried-over bytes).
* A chunk boundary that splits a multi-byte character is normal: an
  incremental decoder holds the partial tail back until the next chunk.
  ``final=True`` is requested only after the stream has ended, so a ``final``
  error means the *stream itself* ended mid-character.  Transient
  incompleteness in the middle of a stream is never treated as corruption.
* ``errors='strict'`` is used everywhere.  No replacement character is ever
  produced.  A sample that cannot be decoded losslessly becomes a quarantine
  record carrying the raw bytes and the exact failing offset; no partially
  decoded document is imported.
* Evidence order is BOM, response header, page declaration, source config.
  Divergences are recorded rather than hidden.  When several distinct
  lossless interpretations remain and none is authoritative, the sample is
  quarantined as ambiguous instead of silently choosing one.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Iterable, Sequence


class DecodeFailure(Exception):
    """Raised when a stream cannot be decoded losslessly."""

    def __init__(self, *, encoding: str | None, reason: str, absolute_offset: int,
                 boundary: str, evidence: Sequence[str], detail: str = ""):
        super().__init__(f"{encoding or 'unknown'}: {detail or reason}")
        self.encoding = encoding
        self.reason = reason
        self.absolute_offset = absolute_offset
        self.boundary = boundary  # inside-chunk | end-of-stream | no-candidate
        self.evidence = list(evidence)
        self.detail = detail or reason

    def as_dict(self) -> dict:
        return {
            "encoding": self.encoding,
            "reason": self.reason,
            "absolute_offset": self.absolute_offset,
            "boundary": self.boundary,
            "evidence": self.evidence,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Chunk:
    """One received chunk with its absolute byte range in the raw payload."""

    index: int
    start: int
    end: int
    data: bytes

    @property
    def byte_range(self) -> str:
        return f"{self.start}:{self.end}"


@dataclass
class Candidate:
    encoding: str          # codec used by the incremental decoder
    evidence: list[str]
    priority: int
    map_encoding: str      # stateless codec used to map chars back to bytes
    trim_bytes: int = 0    # BOM bytes that are not part of the decoded text

    def as_dict(self) -> dict:
        return {
            "encoding": self.encoding,
            "map_encoding": self.map_encoding,
            "evidence": list(self.evidence),
            "priority": self.priority,
            "trim_bytes": self.trim_bytes,
        }


@dataclass
class Decoded:
    text: str
    encoding: str
    evidence: list[str]
    map_encoding: str
    trim_bytes: int
    chunks: list[dict]
    raw_sans_bom: bytes
    byte_offsets: list[int] | None = None  # len(text)+1 offsets, None if not exact

    def as_dict(self) -> dict:
        return {
            "encoding": self.encoding,
            "evidence": list(self.evidence),
            "chunks": list(self.chunks),
            "byte_offsets_exact": self.byte_offsets is not None,
        }


# ---------------------------------------------------------------------------
# Chunk materialisation
# ---------------------------------------------------------------------------

def cumulative_chunks(raw: bytes, boundaries: Iterable[int]) -> list[Chunk]:
    """Materialise cumulative byte boundaries.

    ``boundaries`` are *cumulative* ends, matching ``fixtures/responses.json``.
    The remainder after the last boundary becomes the last chunk.  Boundaries
    that are non-integer, non-ascending or outside ``1..len(raw)`` are rejected
    loudly instead of being silently clamped.
    """

    total = len(raw)
    ends: list[int] = []
    previous = 0
    for boundary in boundaries:
        if isinstance(boundary, bool) or not isinstance(boundary, int):
            raise ValueError(f"chunk boundary must be int, got {boundary!r}")
        if boundary <= previous or boundary > total:
            raise ValueError(
                f"chunk boundary {boundary} is not ascending within 1..{total} after {previous}"
            )
        ends.append(boundary)
        previous = boundary
    if not ends or ends[-1] != total:
        ends.append(total)
    pieces: list[Chunk] = []
    start = 0
    for index, end in enumerate(ends):
        pieces.append(Chunk(index=index, start=start, end=end, data=raw[start:end]))
        start = end
    return pieces


# ---------------------------------------------------------------------------
# Encoding evidence
# ---------------------------------------------------------------------------

# ISO-8859-1 style labels are deliberately not accepted as evidence: they
# decode any byte stream, which would let an unlabelled GB18030 page sneak in
# through an HTTP default.  Every accepted label below has a strict decoder.
_ALIASES = {
    "utf-8": "utf-8", "utf8": "utf-8", "utf_8": "utf-8", "utf-8-sig": "utf-8-sig",
    "gb18030": "gb18030", "cp936": "gb18030", "gbk": "gb18030", "ms936": "gb18030",
    "gb2312": "gb18030", "euc-cn": "gb18030",
    "big5": "big5", "big-5": "big5", "cp950": "big5",
    "utf-16": "utf-16", "utf-16-le": "utf-16-le", "utf-16-be": "utf-16-be",
    "utf-32": "utf-32", "utf-32-le": "utf-32-le", "utf-32-be": "utf-32-be",
}

_CHARSET_RE = re.compile(
    r"<meta[^>]*charset\s*=\s*[\"']?([0-9A-Za-z_.:\-]+)", re.IGNORECASE
)

# BOM entries are ordered longest-first so UTF-32-LE is not mistaken for UTF-16-LE.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32", "bom:utf-32-le", "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32", "bom:utf-32-be", "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8-sig", "bom:utf-8", "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16", "bom:utf-16-le", "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16", "bom:utf-16-be", "utf-16-be"),
)

_MAP_ENCODING = {
    "utf-8-sig": "utf-8",
}


def normalise_label(label: str | None, *, allow_generic_utf: bool = False) -> str | None:
    """Map an encoding label to a supported Python codec, or None.

    Generic UTF-16/32 labels are only decodable with a BOM, so as *automatic*
    evidence they are rejected; a human confirmation may pass
    ``allow_generic_utf=True``.
    """

    if not label:
        return None
    cleaned = label.strip().lower()
    if cleaned in ("utf-16", "utf-32") and not allow_generic_utf:
        return None
    return _ALIASES.get(cleaned)


def detect_bom(raw: bytes) -> tuple[str, str, str, int] | None:
    """Return ``(decoder codec, map codec, evidence, BOM byte count)``."""

    for bom, decoding, evidence, mapping in _BOMS:
        if raw.startswith(bom):
            return decoding, mapping, evidence, len(bom)
    return None


def detect_page_declaration(raw: bytes, probe_bytes: int = 4096) -> tuple[str, str] | None:
    """Find an in-document charset declaration.

    Returns ``(codec, original label)``.  Both ``<meta charset=...>`` and the
    legacy ``http-equiv`` form are recognised.  Only a short ASCII-compatible
    prefix is scanned, so the probe itself cannot corrupt high bytes.
    """

    prefix = raw[:probe_bytes]
    ascii_view = prefix.decode("ascii", "ignore")
    match = _CHARSET_RE.search(ascii_view)
    if not match:
        return None
    codec = normalise_label(match.group(1))
    if codec is None:
        return None
    return codec, match.group(1)


def candidate_chain(*, raw: bytes, http_charset: str | None,
                    source_charset: str | None,
                    manual_charset: str | None = None) -> tuple[list[Candidate], list[str]]:
    """Build encoding candidates plus human-readable conflict notes.

    A BOM is authoritative.  A manual confirmation is also authoritative and
    is marked as such.  Otherwise every other piece of evidence is attempted;
    conflicts are reported verbatim even when one candidate wins.
    """

    conflicts: list[str] = []

    if manual_charset:
        codec = normalise_label(manual_charset, allow_generic_utf=True)
        if codec is None:
            raise ValueError(f"manual encoding {manual_charset!r} is not supported")
        candidate = Candidate(codec, [f"manual-confirm:{manual_charset.strip().lower()}"],
                              priority=-1, map_encoding=_MAP_ENCODING.get(codec, codec))
        return [candidate], conflicts

    bom = detect_bom(raw)
    if bom is not None:
        decoding, mapping, evidence, trim = bom
        conflicts.extend(_cross_check(raw, http_charset, source_charset, authoritative=evidence))
        return [Candidate(decoding, [evidence], 0, mapping, trim)], conflicts

    candidates: list[Candidate] = []

    header = normalise_label(http_charset)
    if header is not None:
        candidates.append(Candidate(header, [f"http-header:{http_charset.strip().lower()}"],
                                    1, _MAP_ENCODING.get(header, header)))
        if http_charset.strip().lower() != header and header in ("gb18030",):
            conflicts.append(
                f"header label {http_charset!r} is treated as {header!r}"
            )
    elif http_charset:
        conflicts.append(f"header charset {http_charset!r} is not a supported label")

    declaration = detect_page_declaration(raw)
    if declaration is not None:
        codec, label = declaration
        candidates.append(Candidate(codec, [f"page-declaration:{label.strip().lower()}"],
                                    2, _MAP_ENCODING.get(codec, codec)))
        if header is not None and header != codec:
            conflicts.append(
                f"header charset {http_charset.strip().lower()!r} conflicts with "
                f"page declaration {label.strip().lower()!r}"
            )

    configured = normalise_label(source_charset)
    if configured is not None:
        candidates.append(Candidate(configured, [f"source-config:{source_charset.strip().lower()}"],
                                    3, _MAP_ENCODING.get(configured, configured)))
        if header is not None and header != configured:
            conflicts.append(
                f"header charset {http_charset.strip().lower()!r} conflicts with "
                f"source charset {source_charset.strip().lower()!r}"
            )
        if declaration is not None and declaration[0] != configured:
            conflicts.append(
                f"page declaration {declaration[1].strip().lower()!r} conflicts with "
                f"source charset {source_charset.strip().lower()!r}"
            )
    elif source_charset:
        conflicts.append(f"source charset {source_charset!r} is not a supported label")

    return candidates, conflicts


def _cross_check(raw: bytes, http_charset: str | None, source_charset: str | None,
                 *, authoritative: str) -> list[str]:
    notes: list[str] = []
    header = normalise_label(http_charset)
    if http_charset and header is None:
        notes.append(f"header charset {http_charset!r} is not a supported label; {authoritative} used")
    elif header is not None and not _same_encoding(authoritative.split(":", 1)[1], header):
        notes.append(f"{authoritative} conflicts with header charset {http_charset!r}; BOM wins")
    declaration = detect_page_declaration(raw)
    if declaration is not None:
        codec, label = declaration
        if not _same_encoding(authoritative.split(":", 1)[1], codec):
            notes.append(f"{authoritative} conflicts with page declaration {label!r}; BOM wins")
    configured = normalise_label(source_charset)
    if configured is not None and not _same_encoding(authoritative.split(":", 1)[1], configured):
        notes.append(f"{authoritative} conflicts with source charset {source_charset!r}; BOM wins")
    return notes


def _same_encoding(a: str, b: str) -> bool:
    # BOM evidence names include byte order (utf-16-le) while the codec is utf-16.
    first = a.split(":", 1)[-1]
    second = b.split(":", 1)[-1]
    return first == second or normalise_label(first) == normalise_label(second)


# ---------------------------------------------------------------------------
# Streaming strict decoding
# ---------------------------------------------------------------------------

def decode_stream(raw: bytes, chunks: Sequence[Chunk], candidate: Candidate) -> Decoded:
    """Decode chunks in order with a strict incremental decoder.

    Partial multi-byte sequences at a chunk boundary are carried over by the
    codec and recorded.  Only the final flush may fail for truncation; an
    in-chunk failure is reported at its absolute raw byte position.
    """

    decoder = codecs.getincrementaldecoder(candidate.encoding)(errors="strict")
    pieces: list[str] = []
    ledger: list[dict] = []
    consumed = 0
    skip = candidate.trim_bytes

    for chunk in chunks:
        data = chunk.data
        if skip:
            drop = min(skip, len(data))
            data = data[drop:]
            skip -= drop
        pending_before = len(decoder.getstate()[0])
        try:
            out = decoder.decode(data, final=False)
        except UnicodeDecodeError as exc:
            absolute = chunk.start + (chunk.end - chunk.start - len(data)) - pending_before + exc.start
            raise DecodeFailure(
                encoding=candidate.encoding,
                reason="invalid-byte",
                absolute_offset=absolute,
                boundary="inside-chunk",
                evidence=candidate.evidence,
                detail=f"{exc.reason} at byte {absolute}",
            ) from exc
        remaining = len(decoder.getstate()[0])
        ledger.append({
            "index": chunk.index,
            "byte_range": chunk.byte_range,
            "input_bytes": len(chunk.data),
            "decoded_bytes": len(data),
            "carried_in": pending_before,
            "carried_out": remaining,
            "chars_out": len(out),
        })
        pieces.append(out)
        consumed = chunk.end

    try:
        tail = decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        partial = decoder.getstate()[0]
        absolute = consumed - len(partial) if partial else consumed
        raise DecodeFailure(
            encoding=candidate.encoding,
            reason="truncated-multibyte",
            absolute_offset=absolute,
            boundary="end-of-stream",
            evidence=candidate.evidence,
            detail=f"{exc.reason}; stream ended inside a multi-byte character at byte {absolute}",
        ) from exc
    pieces.append(tail)

    if skip:
        raise DecodeFailure(
            encoding=candidate.encoding,
            reason="truncated-bom",
            absolute_offset=len(raw),
            boundary="end-of-stream",
            evidence=candidate.evidence,
            detail="stream ended inside the byte-order mark",
        )

    text = "".join(pieces)
    raw_sans_bom = raw[candidate.trim_bytes:]
    offsets = char_to_byte_offsets(text, candidate.map_encoding, raw_sans_bom)
    if offsets is not None and candidate.trim_bytes:
        # Publish absolute offsets into the original payload, not into the
        # payload after the BOM was stripped.
        offsets = [offset + candidate.trim_bytes for offset in offsets]
    return Decoded(text=text, encoding=candidate.encoding, evidence=list(candidate.evidence),
                   map_encoding=candidate.map_encoding, trim_bytes=candidate.trim_bytes,
                   chunks=ledger, raw_sans_bom=raw_sans_bom, byte_offsets=offsets)


def char_to_byte_offsets(text: str, map_encoding: str, raw: bytes) -> list[int] | None:
    """Exact text-character -> raw-byte offsets, when re-encoding round-trips.

    Some codecs (notably GB18030) have byte sequences that are valid but not
    canonical; if a full round-trip does not reproduce the original bytes we
    return ``None`` rather than publish guessed byte offsets.  Character
    offsets in the decoded page remain exact regardless.
    """

    try:
        encoded = b"".join(character.encode(map_encoding) for character in text)
    except (UnicodeEncodeError, LookupError):
        return None
    if encoded != raw:
        return None
    offsets = [0]
    total = 0
    for character in text:
        total += len(character.encode(map_encoding))
        offsets.append(total)
    return offsets


@dataclass
class Decisions:
    decoded: Decoded | None
    failures: list[dict]
    conflicts: list[str]
    candidates: list[dict]
    ambiguity: list[dict]
    status: str  # decoded | ambiguous | failed
    chosen_by: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "chosen_by": self.chosen_by,
            "encoding": self.decoded.encoding if self.decoded else None,
            "evidence": self.decoded.evidence if self.decoded else [],
            "conflicts": list(self.conflicts),
            "candidates": list(self.candidates),
            "failures": list(self.failures),
            "ambiguity": list(self.ambiguity),
        }


def decode_with_evidence(*, raw: bytes, boundaries: Iterable[int],
                         http_charset: str | None = None,
                         source_charset: str | None = None,
                         manual_charset: str | None = None) -> Decisions:
    """Attempt every evidence-backed candidate, strictly.

    Resolution rules:

    * manual confirmation or a BOM is authoritative;
    * otherwise, if several candidates decode losslessly to *different* text,
      the sample is ambiguous and is quarantined rather than guessed;
    * if all lossless results agree on the text, the highest-priority one is
      recorded and the others are noted.
    """

    chunks = cumulative_chunks(raw, boundaries)
    candidates, conflicts = candidate_chain(
        raw=raw, http_charset=http_charset, source_charset=source_charset,
        manual_charset=manual_charset,
    )

    if not candidates:
        return Decisions(
            decoded=None,
            failures=[DecodeFailure(
                encoding=None, reason="no-candidate", absolute_offset=0,
                boundary="no-candidate",
                evidence=["no BOM, no usable header label, no page declaration, no source config"],
                detail="no trustworthy encoding evidence",
            ).as_dict()],
            conflicts=conflicts, candidates=[],
            ambiguity=[], status="failed",
        )

    successes: list[tuple[Candidate, Decoded]] = []
    failures: list[dict] = []
    for candidate in candidates:
        try:
            successes.append((candidate, decode_stream(raw, chunks, candidate)))
        except DecodeFailure as failure:
            failures.append(failure.as_dict())

    candidates_json = [candidate.as_dict() for candidate in candidates]
    if not successes:
        return Decisions(decoded=None, failures=failures, conflicts=conflicts,
                         candidates=candidates_json, ambiguity=[], status="failed")

    distinct: dict[str, list[tuple[Candidate, Decoded]]] = {}
    for candidate, decoded in successes:
        distinct.setdefault(decoded.text, []).append((candidate, decoded))

    authoritative = candidates[0].priority < 1  # manual (-1) or BOM (0)
    if len(distinct) == 1:
        chosen_candidate, chosen = successes[0]
        return Decisions(decoded=chosen, failures=failures, conflicts=conflicts,
                         candidates=candidates_json, ambiguity=[], status="decoded",
                         chosen_by=chosen_candidate.evidence[0])

    if authoritative:
        chosen_candidate, chosen = successes[0]
        notes = list(conflicts)
        notes.append(
            "other lossless interpretations exist but "
            + chosen_candidate.evidence[0] + " is authoritative"
        )
        return Decisions(decoded=chosen, failures=failures, conflicts=notes,
                         candidates=candidates_json, ambiguity=[], status="decoded",
                         chosen_by=chosen_candidate.evidence[0])

    ambiguity = []
    for text, group in distinct.items():
        ambiguity.append({
            "encoding": group[0][1].encoding,
            "evidence": group[0][0].evidence,
            "decoded_preview": text[:80],
            "decoded_chars": len(text),
        })
    return Decisions(decoded=None, failures=failures, conflicts=conflicts,
                     candidates=candidates_json, ambiguity=ambiguity, status="ambiguous")
