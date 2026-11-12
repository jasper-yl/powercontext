# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Turn an uploaded file into chunks: extract text by media type, then split it with the chosen parser.

A parser is a name plus integer parameters. Its profile identifies the exact chunking, so
chunks parsed with different settings never mix. PDF text extraction uses ``pypdf``, which
the deployer installs into the Server environment (see ``requirements.txt``).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO

from pydantic import BaseModel, ConfigDict, Field

MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}


class DocumentError(Exception):
    """The file cannot be turned into text; the message is shown to the user."""


@dataclass(frozen=True)
class Piece:
    text: str
    page: int | None = None


@dataclass(frozen=True)
class Parameter:
    name: str
    default: int
    minimum: int
    maximum: int


@dataclass(frozen=True)
class Parser:
    name: str
    title: dict[str, str]
    description: dict[str, str]
    parameters: tuple[Parameter, ...]
    split: Callable[..., list[str]]


class ParserChoice(BaseModel):
    """A parser and its parameters, as a user chose them; ``resolve`` fills in defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    params: dict[str, int] = Field(default_factory=dict)

    @property
    def profile(self) -> str:
        digest = hashlib.sha256(json.dumps(self.params, sort_keys=True).encode()).hexdigest()[:12]
        return f"{self.name}:{digest}"


def media_type_for(name: str) -> str | None:
    for suffix, media_type in MEDIA_TYPES.items():
        if name.lower().endswith(suffix):
            return media_type
    return None


def pdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


# Text extraction


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8").replace("\r\n", "\n").strip()
    except UnicodeDecodeError:
        raise DocumentError("text files must be UTF-8") from None  # noqa: TRY003


def extract(content: bytes, media_type: str) -> list[Piece]:
    """One piece per page for PDFs, one piece for text files."""

    if media_type != "application/pdf":
        return [Piece(_decode(content))]
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise DocumentError("PDF support needs the pypdf package on the Server") from None  # noqa: TRY003
    try:
        reader = PdfReader(BytesIO(content))
        locked = reader.is_encrypted and not reader.decrypt("")
        pages = (
            []
            if locked
            else [Piece(_clean(page.extract_text() or ""), number) for number, page in enumerate(reader.pages, 1)]
        )
    except (PdfReadError, ValueError, KeyError, TypeError) as error:
        raise DocumentError(f"the PDF cannot be read ({type(error).__name__})") from None  # noqa: TRY003
    if locked:
        raise DocumentError("the PDF is password protected")  # noqa: TRY003
    pages = [page for page in pages if page.text]
    if not pages:
        # Scanned PDFs are images; they need OCR, which this parser does not do.
        raise DocumentError("the PDF has no text layer; scanned documents need OCR")  # noqa: TRY003
    return pages


_CJK = "\u3000-\u303f\u3400-\u9fff\uff00-\uffef"
_SPACED_CJK = re.compile(rf"(?<=[{_CJK}]) (?=[{_CJK}])")
# NFKC has already turned full-width punctuation other than "。" into ASCII.
_SENTENCE_END = re.compile(r"[.!?:;。]$")


def _clean(text: str) -> str:
    """Rebuild paragraphs from PDF lines, which break at the layout width with no blank lines between.

    A line that ends a sentence, or is much shorter than the page's longest line (a heading
    or the last line of a paragraph), ends a paragraph. This is a heuristic: PDFs carry no
    paragraph structure, and a layout-aware parser would do better.
    """

    # Ligatures such as "ﬁ" would split words; spaced-out CJK headings would split bigrams.
    text = unicodedata.normalize("NFKC", text)
    lines = [_SPACED_CJK.sub("", line.strip()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    widest = max(len(line) for line in lines)
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        paragraphs[-1].append(line)
        if _SENTENCE_END.search(line) or len(line) < widest * 0.6:
            paragraphs.append([])
    joined = []
    for parts in paragraphs:
        if parts:
            # Join Chinese lines without a space and other lines with one.
            text = parts[0]
            for part in parts[1:]:
                text += part if re.match(rf"[{_CJK}]", part) and re.search(rf"[{_CJK}]$", text) else f" {part}"
            joined.append(text)
    return "\n\n".join(joined)


# Splitters


def _windows(text: str, size: int, overlap: int) -> list[str]:
    step = max(size - overlap, 1)
    return [text[start : start + size] for start in range(0, max(len(text) - overlap, 1), step)]


def split_paragraphs(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Pack whole paragraphs into chunks, carrying a short tail of the previous chunk forward."""

    chunks: list[str] = []
    current = ""
    for paragraph in (part.strip() for part in re.split(r"\n\s*\n", text)):
        if not paragraph:
            continue
        if len(paragraph) > chunk_size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_windows(paragraph, chunk_size, overlap))
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) > chunk_size:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{paragraph}" if tail else paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def split_fixed(text: str, chunk_size: int, overlap: int) -> list[str]:
    return [window for window in _windows(text, chunk_size, overlap) if window.strip()]


def split_headings(text: str, max_size: int) -> list[str]:
    """One chunk per Markdown section; long sections fall back to paragraph packing."""

    sections = [part.strip() for part in re.split(r"(?m)^(?=#{1,6}\s)", text) if part.strip()]
    chunks: list[str] = []
    for section in sections:
        chunks.extend([section] if len(section) <= max_size else split_paragraphs(section, max_size, 0))
    return chunks


PARSERS = {
    parser.name: parser
    for parser in (
        Parser(
            name="paragraph",
            title={"zh": "按段落", "en": "Paragraphs"},
            description={
                "zh": "把相邻段落合并到不超过指定长度，相邻切块保留少量重叠。适合大多数文档。",  # noqa: RUF001 - Chinese text uses full-width punctuation
                "en": "Packs neighbouring paragraphs up to a size, with a small overlap. Suits most documents.",
            },
            parameters=(Parameter("chunk_size", 800, 200, 4000), Parameter("overlap", 120, 0, 1000)),
            split=split_paragraphs,
        ),
        Parser(
            name="heading",
            title={"zh": "按标题", "en": "Headings"},
            description={
                "zh": "每个 Markdown 标题下的内容作为一个切块，过长时再按段落拆分。适合结构清晰的手册。",  # noqa: RUF001 - Chinese text uses full-width punctuation
                "en": "One chunk per Markdown section, split by paragraph when too long. Suits structured manuals.",
            },
            parameters=(Parameter("max_size", 2000, 200, 8000),),
            split=split_headings,
        ),
        Parser(
            name="fixed",
            title={"zh": "固定长度", "en": "Fixed length"},
            description={
                "zh": "按固定字符数切分，不考虑段落。适合没有段落结构的文本，例如 PDF 转出的长段文字。",  # noqa: RUF001 - Chinese text uses full-width punctuation
                "en": "Cuts every N characters regardless of paragraphs. Suits text without structure.",
            },
            parameters=(Parameter("chunk_size", 600, 100, 4000), Parameter("overlap", 100, 0, 1000)),
            split=split_fixed,
        ),
    )
}


def resolve(choice: ParserChoice) -> ParserChoice:
    """Check a choice against its parser and fill in default parameters."""

    parser = PARSERS.get(choice.name)
    if parser is None:
        raise ValueError(f"unknown parser {choice.name!r}")  # noqa: TRY003
    unknown = set(choice.params) - {parameter.name for parameter in parser.parameters}
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(sorted(unknown))}")  # noqa: TRY003
    params = {}
    for parameter in parser.parameters:
        value = choice.params.get(parameter.name, parameter.default)
        if not parameter.minimum <= value <= parameter.maximum:
            raise ValueError(f"{parameter.name} must be between {parameter.minimum} and {parameter.maximum}")  # noqa: TRY003
        params[parameter.name] = value
    if params.get("overlap", 0) >= params.get("chunk_size", 1 << 30):
        raise ValueError("overlap must be smaller than chunk_size")  # noqa: TRY003
    return ParserChoice(name=choice.name, params=params)


def parse(content: bytes, media_type: str, choice: ParserChoice) -> list[Piece]:
    """Chunks never cross PDF pages, so every chunk keeps the page it came from."""

    parser = PARSERS[choice.name]
    pieces = [
        Piece(text, block.page)
        for block in extract(content, media_type)
        for text in parser.split(block.text, **choice.params)
        if text.strip()
    ]
    if not pieces:
        raise DocumentError("the document has no text")  # noqa: TRY003
    return pieces
