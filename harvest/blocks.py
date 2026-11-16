"""按页面区块（section）解析字段片段。

两种页面结构均来自仓库内合成材料：

``labeled_text``  ``<b>标签</b>值``，值是标签元素结束后的文本（fixtures 响应）；
``dl``             ``<dt>标签</dt><dd>值</dd>`` 成对描述列表（fixtures/pages）。

解析器只负责抽取 *带位置的片段*，不做字段合并、不做日期猜测。同名标签在不同
区块里各自独立；同一区块内的重复标签由上层按冲突处理，绝不静默取第一个值。
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

from harvest.stream import char_span_to_byte_span

DEFAULT_SECTION_ATTR = "data-section"
DEFAULT_SECTION_ID = "_unsectioned"
MODE_LABELED_TEXT = "labeled_text"
MODE_DL = "dl"

# HTML 空元素没有闭合标签，入栈后无法弹出，直接跳过栈维护。
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)


@dataclass(frozen=True)
class PageSection:
    section_id: str
    kind: str
    element_path: str
    char_start: int
    char_end: int
    byte_start: int = -1
    byte_end: int = -1

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "kind": self.kind,
            "element_path": self.element_path,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }


@dataclass(frozen=True)
class Fragment:
    label: str
    value: str
    section: PageSection
    element_path: str
    label_char_start: int
    label_char_end: int
    value_char_start: int
    value_char_end: int
    byte_start: int = -1
    byte_end: int = -1
    crossed_chunk: bool = False
    label_byte_start: int = -1
    label_byte_end: int = -1
    label_crossed_chunk: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "value": self.value,
            "section": self.section.to_dict(),
            "element_path": self.element_path,
            "label_char_span": [self.label_char_start, self.label_char_end],
            "value_char_span": [self.value_char_start, self.value_char_end],
            "byte_span": [self.byte_start, self.byte_end],
            "crossed_chunk": self.crossed_chunk,
            "label_byte_span": [self.label_byte_start, self.label_byte_end],
            "label_crossed_chunk": self.label_crossed_chunk,
        }


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


class _BlockHTMLParser(HTMLParser):
    def __init__(
        self,
        text: str,
        *,
        mode: str,
        section_attr: str,
        section_kinds: dict[str, str],
        label_tags: frozenset[str],
        default_section: str,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._text = text
        self._line_starts = _line_starts(text)
        self._mode = mode
        self._section_attr = section_attr
        self._section_kinds = section_kinds
        self._label_tags = label_tags
        self._default_section = default_section

        self._stack: list[dict] = []
        self._sections: list[PageSection] = []
        self.fragments: list[Fragment] = []

        # labeled_text 模式状态
        self._pending_label: Optional[dict] = None
        self._in_label_depth = 0
        # dl 模式状态
        self._dt_parts: list[tuple[str, int]] = []
        self._dd_parts: list[tuple[str, int]] = []
        self._in_dt = False
        self._in_dd = False
        self._dt_span: Optional[tuple[int, int]] = None
        self._dd_span: Optional[tuple[int, int]] = None

    # -- 位置与路径辅助 -----------------------------------------------------

    def _abs_offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _current_path(self, tag: str, attrs: dict[str, str]) -> str:
        selector = tag
        for name, value in attrs.items():
            if name == self._section_attr and value is not None:
                selector = f"{tag}[{name}={value}]"
                break
        ancestors = [element["path_selector"] for element in self._stack]
        return "/".join(ancestors + [selector])

    def _current_section(self) -> PageSection:
        for element in reversed(self._stack):
            if element["section"] is not None:
                return element["section"]
        return PageSection(
            section_id=self._default_section,
            kind="unsectioned",
            element_path="",
            char_start=0,
            char_end=len(self._text),
        )

    # -- HTMLParser 钩子 ----------------------------------------------------

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, Optional[str]]]) -> None:
        attrs = {name: (value or "") for name, value in attrs_list}
        path = self._current_path(tag, attrs)
        section = None
        if self._section_attr in attrs:
            section_id = attrs[self._section_attr] or self._default_section
            section = PageSection(
                section_id=section_id,
                kind=self._section_kinds.get(section_id, section_id),
                element_path=path,
                char_start=self._abs_offset(),
                char_end=-1,
            )
            self._sections.append(section)
        if tag not in _VOID_TAGS:
            self._stack.append(
                {"tag": tag, "attrs": attrs, "path_selector": path, "section": section}
            )

        if self._mode == MODE_LABELED_TEXT and tag in self._label_tags:
            # 新标签开始：把上一个尚未落盘的标签先落盘，再开新标签。
            self._flush_pending_label()
            self._pending_label = {
                "label_parts": [],
                "label_start": self._abs_offset(),
                "label_end": -1,
                "value_parts": [],
                "value_start": None,
                "value_end": None,
                "path": path,
            }
            self._in_label_depth = 1
        elif self._mode == MODE_DL:
            if tag == "dt":
                self._dt_parts = []
                self._dt_span = (self._abs_offset(), -1)
                self._in_dt = True
            elif tag == "dd":
                self._dd_parts = []
                self._dd_span = (self._abs_offset(), -1)
                self._in_dd = True

    def handle_startendtag(
        self, tag: str, attrs_list: list[tuple[str, Optional[str]]]
    ) -> None:
        # 自闭合元素不承载标签文本，按入栈再出栈处理以维护路径。
        self.handle_starttag(tag, attrs_list)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        offset = self._abs_offset()
        if self._mode == MODE_LABELED_TEXT and tag in self._label_tags and self._pending_label:
            self._pending_label["label_end"] = offset
            self._in_label_depth = 0
        elif self._mode == MODE_DL:
            if tag == "dt" and self._dt_span:
                self._dt_span = (self._dt_span[0], offset)
                self._in_dt = False
            elif tag == "dd" and self._dd_span:
                self._dd_span = (self._dd_span[0], offset)
                self._in_dd = False
                self._emit_dl_pair(offset)

        # LIFO 弹出：只有栈顶标签匹配时才出栈，避免闭合内层标签时把
        # 承载区块身份的外层 article 一起误删。
        top = self._stack[-1] if self._stack else None
        is_section_close = (
            self._mode == MODE_LABELED_TEXT
            and top is not None
            and top["tag"] == tag
            and top["section"] is not None
        )
        # 区块元素自身闭合：先在仍处于区块内时落片段，保住区块身份。
        if is_section_close:
            self._flush_pending_label()

        closed_section = None
        if self._stack and self._stack[-1]["tag"] == tag:
            closing = self._stack.pop()
            if closing["section"] is not None:
                closed_section = closing["section"]

        # 普通元素闭合：出栈后再落片段，此时外层区块仍在栈上，
        # _current_section 才能找到正确的区块身份。标签自身闭合时值文本
        # 还没到，保持 pending，等值收齐或下一个标签/区块结束再落盘。
        if (
            self._mode == MODE_LABELED_TEXT
            and closed_section is None
            and tag not in self._label_tags
        ):
            self._flush_pending_label()

        if closed_section is not None:
            self._close_section(closed_section, offset)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        start = self._abs_offset()
        end = start + len(data)
        if self._mode == MODE_LABELED_TEXT and self._pending_label is not None:
            if self._in_label_depth > 0:
                self._pending_label["label_parts"].append(data)
            else:
                if self._pending_label["value_start"] is None:
                    self._pending_label["value_start"] = start
                self._pending_label["value_parts"].append(data)
                self._pending_label["value_end"] = end
        elif self._mode == MODE_DL:
            if self._in_dt:
                self._dt_parts.append((data, start, end))
            elif self._in_dd:
                self._dd_parts.append((data, start, end))

    def _close_section(self, section: PageSection, offset: int) -> None:
        if self._mode == MODE_LABELED_TEXT:
            self._flush_pending_label()
        for index, candidate in enumerate(self._sections):
            if candidate is section:
                self._sections[index] = PageSection(
                    section_id=candidate.section_id,
                    kind=candidate.kind,
                    element_path=candidate.element_path,
                    char_start=candidate.char_start,
                    char_end=offset,
                )
                break

    # -- 片段产出 -----------------------------------------------------------

    def _emit_fragment(
        self,
        label: str,
        value: str,
        section: PageSection,
        path: str,
        label_span: tuple[int, int],
        value_span: tuple[int, int],
    ) -> None:
        label = label.strip()
        if not label:
            return
        self.fragments.append(
            Fragment(
                label=label,
                value=value.strip(),
                section=section,
                element_path=path,
                label_char_start=label_span[0],
                label_char_end=label_span[1],
                value_char_start=value_span[0],
                value_char_end=value_span[1],
            )
        )

    def _flush_pending_label(self) -> None:
        pending = self._pending_label
        if pending is None:
            return
        self._pending_label = None
        self._in_label_depth = 0
        label = "".join(pending["label_parts"]).strip()
        value = "".join(pending["value_parts"]).strip()
        if not label or not value:
            return
        section = self._current_section()
        self._emit_fragment(
            label,
            value,
            section,
            pending["path"],
            (pending["label_start"], pending["label_end"]),
            (
                pending["value_start"] if pending["value_start"] is not None else 0,
                pending["value_end"] if pending["value_end"] is not None else 0,
            ),
        )

    def _emit_dl_pair(self, offset: int) -> None:
        if not self._dt_parts or not self._dd_parts:
            self._dt_parts, self._dd_parts = [], []
            self._dt_span, self._dd_span = None, None
            return
        label = "".join(part[0] for part in self._dt_parts)
        value = "".join(part[0] for part in self._dd_parts)
        label_span = (self._dt_parts[0][1], self._dt_parts[-1][2])
        value_span = (self._dd_parts[0][1], self._dd_parts[-1][2])
        path = self._current_path("dd", {})
        self._emit_fragment(
            label, value, self._current_section(), path, label_span, value_span
        )
        self._dt_parts, self._dd_parts = [], []
        self._dt_span, self._dd_span = None, None

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._flush_pending_label()


def extract_fragments(
    text: str,
    origins: list[int],
    raw_len: int,
    rule: dict,
    stream: Optional["object"] = None,
) -> tuple[list[Fragment], list[PageSection]]:
    """按来源区块规则抽取片段，并把字符位置换算成原始字节位置。

    ``stream`` 提供分块边界时，会同时标注片段值是否跨越分块边界。
    """

    parser = _BlockHTMLParser(
        text,
        mode=rule.get("block_mode", MODE_LABELED_TEXT),
        section_attr=rule.get("section_attr", DEFAULT_SECTION_ATTR),
        section_kinds=dict(rule.get("section_kinds", {})),
        label_tags=frozenset(rule.get("label_tags", ["b"])),
        default_section=rule.get("default_section", DEFAULT_SECTION_ID),
    )
    parser.feed(text)
    parser.close()

    fragments: list[Fragment] = []
    for fragment in parser.fragments:
        section = fragment.section
        label_byte_start, label_byte_end = char_span_to_byte_span(
            origins,
            raw_len,
            fragment.label_char_start,
            fragment.label_char_end,
        )
        byte_start, byte_end = char_span_to_byte_span(
            origins,
            raw_len,
            fragment.value_char_start,
            fragment.value_char_end,
        )
        if section.section_id != DEFAULT_SECTION_ID and section.char_end >= 0:
            sec_start, sec_end = char_span_to_byte_span(
                origins, raw_len, section.char_start, section.char_end
            )
            section = PageSection(
                section_id=section.section_id,
                kind=section.kind,
                element_path=section.element_path,
                char_start=section.char_start,
                char_end=section.char_end,
                byte_start=sec_start,
                byte_end=sec_end,
            )
        crossed = False
        label_crossed = False
        if stream is not None:
            crossed = stream.spans_frame_boundary(byte_start, byte_end)
            label_crossed = stream.spans_frame_boundary(
                label_byte_start, label_byte_end
            )
        fragments.append(
            Fragment(
                label=fragment.label,
                value=fragment.value,
                section=section,
                element_path=fragment.element_path,
                label_char_start=fragment.label_char_start,
                label_char_end=fragment.label_char_end,
                value_char_start=fragment.value_char_start,
                value_char_end=fragment.value_char_end,
                byte_start=byte_start,
                byte_end=byte_end,
                crossed_chunk=crossed,
                label_byte_start=label_byte_start,
                label_byte_end=label_byte_end,
                label_crossed_chunk=label_crossed,
            )
        )

    sections = list(parser._sections)
    for index, section in enumerate(sections):
        if section.byte_start >= 0:
            continue
        if section.char_end < 0:
            sec_end = len(text)
        else:
            sec_end = section.char_end
        byte_start, byte_end = char_span_to_byte_span(
            origins, raw_len, section.char_start, sec_end
        )
        sections[index] = PageSection(
            section_id=section.section_id,
            kind=section.kind,
            element_path=section.element_path,
            char_start=section.char_start,
            char_end=sec_end,
            byte_start=byte_start,
            byte_end=byte_end,
        )
    return fragments, sections
