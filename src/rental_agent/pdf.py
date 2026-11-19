"""A plain PDF document on US Letter: a title, then headings, paragraphs, lists of facts,
tables and numbered links, with the page number on every page.

It prints in the PDF's built-in Helvetica, so no font file is needed. That font has Western
European characters only (Windows-1252); any other character prints as "?".
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

MARGIN = 18             # millimetres, on every side
LINE = 5                # line height of body text
GREY = 110
ENCODING = "windows-1252"


@dataclass(frozen=True)
class Heading:
    text: str


@dataclass(frozen=True)
class Facts:
    """Label and value pairs, one per line."""
    rows: Sequence[tuple[str, str]]


@dataclass(frozen=True)
class Table:
    headings: Sequence[str]
    rows: Sequence[Sequence[str]]
    widths: Sequence[float]                   # relative column widths
    right: frozenset[int] = frozenset()       # columns of numbers, printed right-aligned
    bold: frozenset[int] = frozenset()        # rows printed in bold


@dataclass(frozen=True)
class Links:
    """Web addresses, numbered from 1, each one clickable and printed in full."""
    urls: Sequence[str]


# A plain string is a paragraph; **bold** is the only markup in it. Web addresses go in Links,
# because a paragraph would read "--" or "__" inside one as markup.
Block = str | Heading | Facts | Table | Links


class _Document(FPDF):
    def __init__(self, footer_text: str) -> None:
        super().__init__(format="letter")
        self.core_fonts_encoding = ENCODING
        self.footer_text = footer_text
        self.set_margins(MARGIN, MARGIN)
        self.set_auto_page_break(True, margin=MARGIN)

    def footer(self) -> None:
        self.set_y(-MARGIN + 4)
        self.set_font("Helvetica", size=8)
        self.set_text_color(GREY)
        self.cell(0, LINE, self.footer_text)
        self.set_x(self.l_margin)
        self.cell(0, LINE, f"Page {self.page_no()} of {{nb}}", align="R")
        self.set_text_color(0)


def _clean(text: str) -> str:
    return text.encode(ENCODING, errors="replace").decode(ENCODING)


def _paragraph(doc: _Document, text: str) -> None:
    doc.set_font("Helvetica", size=10)
    doc.multi_cell(0, LINE, _clean(text), markdown=True, align="L", new_x="LMARGIN",
                   new_y="NEXT")
    doc.ln(2)


def _heading(doc: _Document, block: Heading) -> None:
    doc.ln(3)
    if doc.will_page_break(6 * LINE):       # keep a heading with the text under it
        doc.add_page()
    doc.set_font("Helvetica", style="B", size=13)
    doc.multi_cell(0, 7, _clean(block.text), align="L", new_x="LMARGIN", new_y="NEXT")
    doc.ln(1)


def _facts(doc: _Document, block: Facts) -> None:
    doc.set_font("Helvetica", style="B", size=10)
    widest = max(doc.get_string_width(_clean(label)) for label, _ in block.rows)
    label_width = min(widest + 6, doc.epw / 2)      # a longer label wraps
    for label, value in block.rows:
        top = doc.get_y()
        doc.set_font("Helvetica", style="B", size=10)
        doc.multi_cell(label_width, LINE, _clean(label), align="L", new_x="LMARGIN",
                       new_y="NEXT")
        bottom = doc.get_y()
        doc.set_xy(doc.l_margin + label_width, top)
        doc.set_font("Helvetica", size=10)
        doc.multi_cell(0, LINE, _clean(value), align="L", new_x="LMARGIN", new_y="NEXT")
        doc.set_y(max(bottom, doc.get_y()))
    doc.ln(2)


def _table(doc: _Document, block: Table) -> None:
    doc.set_font("Helvetica", size=8.5)
    doc.set_draw_color(190)
    aligns = tuple("RIGHT" if i in block.right else "LEFT" for i in range(len(block.headings)))
    bold = FontFace(emphasis="BOLD")
    with doc.table(col_widths=tuple(block.widths), text_align=aligns, line_height=4.2,
                   padding=(1, 2), borders_layout="HORIZONTAL_LINES",
                   headings_style=FontFace(emphasis="BOLD", fill_color=235)) as table:
        table.row([_clean(h) for h in block.headings])
        for index, cells in enumerate(block.rows):
            row = table.row()
            for cell in cells:
                row.cell(_clean(cell), style=bold if index in block.bold else None)
    doc.ln(3)


def _links(doc: _Document, block: Links) -> None:
    doc.set_font("Helvetica", size=8.5)
    for number, url in enumerate(block.urls, start=1):
        doc.set_text_color(0)
        doc.write(4.2, f"{number}.  ")
        doc.set_text_color(0, 0, 200)
        doc.write(4.2, _clean(url), link=url)
        doc.ln(4.6)
    doc.set_text_color(0)
    doc.ln(2)


def write(out: Path, *, title: str, subtitle: str, footer: str, blocks: Sequence[Block]) -> None:
    doc = _Document(_clean(footer))
    doc.set_title(_clean(title))
    doc.add_page()
    doc.set_font("Helvetica", style="B", size=17)
    doc.multi_cell(0, 8, _clean(title), align="L", new_x="LMARGIN", new_y="NEXT")
    doc.set_font("Helvetica", size=10)
    doc.set_text_color(GREY)
    doc.multi_cell(0, LINE, _clean(subtitle), align="L", new_x="LMARGIN", new_y="NEXT")
    doc.set_text_color(0)
    doc.ln(4)
    for block in blocks:
        if isinstance(block, str):
            _paragraph(doc, block)
        elif isinstance(block, Heading):
            _heading(doc, block)
        elif isinstance(block, Facts):
            _facts(doc, block)
        elif isinstance(block, Table):
            _table(doc, block)
        else:
            _links(doc, block)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.output(str(out))
