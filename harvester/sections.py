"""Section-aware page extraction.

Registry pages interleave several blocks in one document, and the same visible
label (``日期``) is meaningful only relative to its block.  Taking the first
occurrence of a label is exactly the failure mode this module avoids.

Every extracted value carries the enclosing block identity, the label tag that
produced it, its character span in the decoded text, and (when the decode
round-trips exactly) a raw byte span.

Supported label/value shapes, all within a declared block:

* ``<b>label</b>value`` (and ``strong``/``th``/``h*`` labels);
* ``<b>label</b><span>value</span>`` value containers;
* ``<dt>label</dt><dd>value</dd>`` definition lists;
* ``<dl>...</dl>`` inside a labelled container.

A label outside any declared block is reported as ``unsectioned-label`` and is
never attributed to a neighbouring block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser

SECTION_ATTRS = ("data-section", "data-block", "data-scope")
BLOCK_TAGS = ("article", "section", "div", "li", "dl")
LABEL_TAGS = ("b", "strong", "dt", "th", "label", "h1", "h2", "h3", "span")
VALUE_TAGS = ("dd", "td", "p", "div", "span")
COMPANY_ATTRS = ("data-company", "data-company-id", "data-org-id")
VOID_TAGS = ("br", "img", "input", "meta", "link", "hr")


@dataclass
class Span:
    start: int
    end: int
    byte_start: int | None = None
    byte_end: int | None = None

    def with_bytes(self, text_to_byte) -> "Span":
        if text_to_byte is None:
            return self
        try:
            return Span(self.start, self.end, text_to_byte[self.start], text_to_byte[self.end])
        except (IndexError, TypeError):
            return self

    def as_dict(self) -> dict:
        return {
            "char": [self.start, self.end],
            "byte": [self.byte_start, self.byte_end] if self.byte_start is not None else None,
        }


@dataclass
class Field:
    label: str
    value: str
    section_local: str
    label_span: Span
    value_span: Span
    label_tag: str
    section: "Section | None" = None

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "value": self.value,
            "label_span": self.label_span.as_dict(),
            "value_span": self.value_span.as_dict(),
            "label_tag": self.label_tag,
        }


@dataclass
class Section:
    local_id: str
    identity: tuple[str, str] | None  # (attribute name, raw value) as declared
    attrs: dict
    start: int
    end: int | None = None
    company_raw: str | None = None
    fields: list[Field] = field(default_factory=list)
    byte_start: int | None = None
    byte_end: int | None = None

    @property
    def section_id(self) -> str:
        if self.identity is None:
            return self.local_id
        return f"{self.identity[0]}={self.identity[1]}#{self.local_id}"

    def as_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "local_id": self.local_id,
            "identity": {"attribute": self.identity[0], "value": self.identity[1]}
            if self.identity else None,
            "attrs": dict(self.attrs),
            "company_raw": self.company_raw,
            "span": {"char": [self.start, self.end],
                     "byte": [self.byte_start, self.byte_end]
                     if self.byte_start is not None else None},
            "fields": [item.as_dict() for item in self.fields],
        }


@dataclass
class Extraction:
    sections: list[Section]
    fields: list[Field]
    issues: list[dict]          # structural problems (unsectioned labels, ...)
    unclosed: list[dict]        # labelled blocks missing an end tag
    text_to_byte: list[int] | None = None

    def finalise(self) -> "Extraction":
        for section in self.sections:
            for item in section.fields:
                item.label_span = item.label_span.with_bytes(self.text_to_byte)
                item.value_span = item.value_span.with_bytes(self.text_to_byte)
            if section.end is not None:
                span = Span(section.start, section.end).with_bytes(self.text_to_byte)
                section.byte_start, section.byte_end = span.byte_start, span.byte_end
        return self


class _Parser(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.text = text
        self.sections: list[Section] = []
        self.issues: list[dict] = []
        self.unclosed: list[dict] = []
        self._open_blocks: list[Section] = []
        self._tag_stack: list[tuple[str, Section | None]] = []
        self._counter = 0
        self._label: dict | None = None
        self._pending_value: dict | None = None
        self._dt: dict | None = None
        self._line_starts = [0]
        for index, character in enumerate(text):
            if character == "\n":
                self._line_starts.append(index + 1)

    # -- positions --------------------------------------------------------
    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def _tag_end(self) -> int:
        return self._offset() + len(self.get_starttag_text() or "")

    def _current_section(self) -> Section | None:
        return self._open_blocks[-1] if self._open_blocks else None

    # -- callbacks --------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:
            return
        attrs = {key.lower(): (value if value is not None else "") for key, value in attrs}
        start = self._offset()

        if tag in BLOCK_TAGS and self._identity(attrs):
            self._counter += 1
            section = Section(
                local_id=str(self._counter),
                identity=self._identity(attrs),
                attrs=attrs,
                start=start,
                company_raw=next((attrs[key] for key in COMPANY_ATTRS if attrs.get(key)), None),
            )
            self._open_blocks.append(section)
            self.sections.append(section)
            self._tag_stack.append((tag, section))
            self._label = None
            self._pending_value = None
            return

        self._tag_stack.append((tag, None))

        if self._pending_value is not None and tag in VALUE_TAGS:
            # <b>label</b><span>value</span>: this tag is the value container.
            self._label = {
                "tag": tag,
                "start": self._pending_value["label_start"],
                "body": self._tag_end(),
                "text": "",
                "section": self._pending_value["section"],
                "label_text": self._pending_value["label"],
                "label_span": self._pending_value["label_span"],
                "label_tag": self._pending_value["label_tag"],
                "value_tag": tag,
            }
            self._pending_value = None
            return

        if tag in LABEL_TAGS:
            section = self._current_section()
            if section is None:
                # Never attribute a label to a neighbouring block by accident.
                self.issues.append({
                    "kind": "unsectioned-label",
                    "tag": tag,
                    "char": [start, self._tag_end()],
                })
                return
            self._pending_value = None
            self._label = {
                "tag": tag,
                "start": start,
                "body": self._tag_end(),
                "text": "",
                "section": section,
            }
            return

        if tag == "dd" and self._label is not None and self._label["tag"] == "dt":
            # <dt>label</dt><dd>value</dd>
            done = self._label
            self._finish_label()
            self._label = {
                "tag": tag,
                "start": done["start"],
                "body": self._tag_end(),
                "text": "",
                "section": done["section"],
                "label_text": done["text"].strip(),
                "label_span": Span(done["body"],
                                   done["body"] + len(done["text"].strip())),
                "label_tag": "dt",
                "value_tag": tag,
            }
            return

        if tag == "dd" and self._dt is not None:
            label = self._dt
            self._dt = None
            self._label = {
                "tag": tag,
                "start": label["start"],
                "body": self._tag_end(),
                "text": "",
                "section": label["section"],
                "label_text": label["text"],
                "label_span": label["label_span"],
                "label_tag": "dt",
                "value_tag": tag,
            }

    def handle_endtag(self, tag):
        if self._label is not None and tag == self._label["tag"]:
            self._finish_label()
        elif tag == "dt" and self._label is not None and self._label["tag"] == "dt":
            self._finish_label()

        # Close exactly the matching opener and everything opened inside it.
        # Only a close of the block's own tag ends a declared block; closing an
        # inner <dl> must not pop the enclosing <article>.
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index][0] == tag:
                name, owner = self._tag_stack[index]
                if owner is not None and owner.end is None and owner in self._open_blocks:
                    owner.end = self._offset()
                    self._open_blocks.remove(owner)
                del self._tag_stack[index:]
                break

    def handle_data(self, data):
        if self._label is not None:
            self._label["text"] += data
            return
        if self._pending_value is not None:
            stripped = data.strip()
            if stripped:
                lead = len(data) - len(data.lstrip())
                start = self._offset() + lead
                self._record(
                    label=self._pending_value["label"],
                    value=stripped,
                    label_span=self._pending_value["label_span"],
                    label_tag=self._pending_value["label_tag"],
                    section=self._pending_value["section"],
                    value_span=Span(start, start + len(stripped)),
                )
                self._pending_value = None

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _identity(attrs):
        for key in SECTION_ATTRS:
            if attrs.get(key):
                return (key, attrs[key].strip())
        return None

    def _record(self, *, label, value, label_span, label_tag, section, value_span):
        item = Field(label=label, value=value, section_local=section.local_id,
                     label_span=label_span, value_span=value_span,
                     label_tag=label_tag, section=section)
        section.fields.append(item)

    def _finish_label(self):
        if self._label is None:
            return
        pending = self._label
        self._label = None
        section = pending["section"]
        text = pending["text"].strip()
        if "label_text" in pending:
            body = pending["body"]
            lead = pending["text"].find(text) if text else 0
            if lead < 0:
                lead = 0
            self._record(
                label=pending["label_text"],
                value=text,
                label_span=pending["label_span"],
                label_tag="dt",
                section=section,
                value_span=Span(body + lead, body + lead + len(text)),
            )
            return
        if pending["tag"] == "dt":
            label_span = Span(pending["body"], pending["body"] + len(text))
            self._dt = {
                "text": text,
                "body": pending["body"],
                "start": pending["start"],
                "section": section,
                "label_span": label_span,
            }
            self._pending_value = {
                "label": text,
                "label_span": label_span,
                "label_tag": "dt",
                "section": section,
                "label_start": pending["start"],
            }
            return
        self._pending_value = {
            "label": text,
            "label_span": Span(pending["body"], pending["body"] + len(text)),
            "label_tag": pending["tag"],
            "section": section,
            "label_start": pending["start"],
        }

    def finish(self):
        if self._label is not None and self._label["tag"] == "dt":
            self._finish_label()
        for section in self._open_blocks:
            self.unclosed.append({
                "local_id": section.local_id,
                "section_id": section.section_id,
                "char": [section.start, len(self.text)],
            })
            section.end = len(self.text)
        self._open_blocks.clear()


def extract(text: str, text_to_byte: list[int] | None = None) -> Extraction:
    parser = _Parser(text)
    parser.feed(text)
    parser.close()
    parser.finish()
    return Extraction(
        sections=parser.sections,
        fields=[item for section in parser.sections for item in section.fields],
        issues=parser.issues,
        unclosed=parser.unclosed,
        text_to_byte=text_to_byte,
    ).finalise()
