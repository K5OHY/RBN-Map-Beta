"""PDF reports for the RBN Signal Mapper, drawn with matplotlib's own PDF writer (no extra packages to install).

Everything here takes plain data (text, numbers, matplotlib figures), never Streamlit objects, so it can be tried on
its own. `build_pdf(meta, blocks)` lays the blocks out on Letter pages, starting a new page whenever one runs out:

    ("heading", text)                       a section heading
    ("verdict", {"headline", "points", "caveats", "winner"})
    ("paragraph", text) / ("note", text)    body text / small grey text
    ("bullets", [text, ...])
    ("scoreboard", names, rows, colors)     rows as built for the on-screen scoreboard
    ("bar", counts, labels, colors)         one stacked bar
    ("metrics", [(label, value), ...])      a row of big numbers
    ("figure", Figure, max_height_inches)   a chart
    ("table", columns, rows, col_widths)    a plain table; `rows` are lists of strings
"""
import io
import re
import textwrap
from datetime import datetime, timezone

import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

NL = chr(10)
PAGE_W, PAGE_H = 8.5, 11.0
MARGIN = 0.75
TEXT_W = PAGE_W - 2 * MARGIN
INK, GREY, RULE = "#1f2328", "#6b7280", "#d0d7de"
_EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍]")  # no colour-emoji font in a PDF


def clean(text):
    """Plain text for the page: **bold** markers and emoji removed (the PDF font has no emoji)."""
    text = _EMOJI.sub("", str(text)).replace("**", "").replace("*", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def _tint(color, amount=0.85):
    """`color` mixed toward white (0 = the colour, 1 = white), as an RGB tuple."""
    from matplotlib.colors import to_rgb
    r, g, b = to_rgb(color)
    return (r + (1 - r) * amount, g + (1 - g) * amount, b + (1 - b) * amount)


class _Flow:
    """Places blocks down the page from the top, starting a new page when the next one will not fit."""

    def __init__(self, pdf, meta):
        self.pdf, self.meta, self.page, self.fig, self.y = pdf, meta, 0, None, 0.0
        self.new_page()

    # -- pages
    def _inches(self, x, y):
        return x / PAGE_W, 1 - y / PAGE_H

    def new_page(self):
        if self.fig is not None:
            self._footer()
            self.pdf.savefig(self.fig)
        self.page += 1
        self.fig = Figure(figsize=(PAGE_W, PAGE_H))
        self.fig.patch.set_facecolor("white")
        self.y = MARGIN
        if self.page == 1:
            self._title()
        else:
            self.text(MARGIN, self.y, f"{self.meta['title']}  ·  {self.meta.get('callsign', '')}", 8.5, color=GREY)
            self.y += 0.35

    def _footer(self):
        self.fig.text(0.5, 0.03, f"{self.meta.get('footer', 'RBN Signal Mapper')}  ·  page {self.page}",
                      ha="center", va="bottom", fontsize=7.5, color=GREY)

    def finish(self):
        self._footer()
        self.pdf.savefig(self.fig)

    def ensure(self, height):
        if self.y + height > PAGE_H - MARGIN - 0.2:
            self.new_page()

    # -- drawing primitives
    def text(self, x, y, s, size=10, weight="normal", color=INK, ha="left"):
        fx, fy = self._inches(x, y)
        self.fig.text(fx, fy, s, fontsize=size, fontweight=weight, color=color, ha=ha, va="top")

    def _wrap(self, s, size, width_in, bold=False):
        chars = max(int(width_in * 72 / (size * (0.64 if bold else 0.55))), 16)  # DejaVu Sans, a little over half an em
        return textwrap.wrap(clean(s), chars) or [""]

    def _lines(self, lines, x, size, color=INK, weight="normal", spacing=1.42):
        step = size * spacing / 72
        for line in lines:
            self.text(x, self.y, line, size, weight, color)
            self.y += step

    def rule(self):
        fx0, fy = self._inches(MARGIN, self.y)
        fx1, _ = self._inches(PAGE_W - MARGIN, self.y)
        self.fig.add_artist(Rectangle((fx0, fy), fx1 - fx0, 0.0006, color=RULE, transform=self.fig.transFigure))
        self.y += 0.12

    # -- blocks
    def _title(self):
        m = self.meta
        self.text(MARGIN, self.y, m["title"], 21, "bold")
        self.y += 0.42
        for line in m.get("subtitle", []):
            self.text(MARGIN, self.y, clean(line), 9.5, color=GREY)
            self.y += 0.2
        self.y += 0.05
        self.rule()
        self.y += 0.08

    def heading(self, s, need=0.7):
        self.ensure(max(need, 0.7))
        self.y += 0.12
        self.text(MARGIN, self.y, clean(s), 13.5, "bold")
        self.y += 0.32

    def paragraph(self, s, size=10, color=INK):
        lines = self._wrap(s, size, TEXT_W)
        self.ensure(len(lines) * size * 1.42 / 72 + 0.1)
        self._lines(lines, MARGIN, size, color)
        self.y += 0.08

    def note(self, s):
        self.paragraph(s, 8.5, GREY)

    def bullets(self, items, size=10):
        for item in items:
            lines = self._wrap(item, size, TEXT_W - 0.25)
            self.ensure(len(lines) * size * 1.42 / 72 + 0.08)
            self.text(MARGIN + 0.02, self.y, "•", size)
            self._lines(lines, MARGIN + 0.25, size)
            self.y += 0.05

    def verdict(self, v, colors):
        color = colors.get(v.get("winner"), colors.get(None, "#6b7280"))
        head = self._wrap(v["headline"], 13, TEXT_W - 0.55, bold=True)
        h_head = len(head) * 13 * 1.4 / 72
        points = [self._wrap(t, 9.5, TEXT_W - 0.65) for _, t in v["points"]]
        h_points = sum(len(p) * 9.5 * 1.42 / 72 + 0.05 for p in points)
        box_h = 0.2 + h_head + 0.12 + h_points + 0.15
        self.ensure(box_h + 0.1)
        fx, fy_top = self._inches(MARGIN, self.y)
        fw, fh = TEXT_W / PAGE_W, box_h / PAGE_H
        self.fig.add_artist(Rectangle((fx, fy_top - fh), fw, fh, facecolor=_tint(color, 0.88), edgecolor="none",
                                      transform=self.fig.transFigure))
        self.fig.add_artist(Rectangle((fx, fy_top - fh), 0.07 / PAGE_W, fh, facecolor=color, edgecolor="none",
                                      transform=self.fig.transFigure))
        self.y += 0.14
        self._lines(head, MARGIN + 0.25, 13, INK, "bold", 1.4)
        self.y += 0.1
        for lines in points:
            self.text(MARGIN + 0.25, self.y, "•", 9.5)
            self._lines(lines, MARGIN + 0.45, 9.5)
            self.y += 0.05
        self.y += 0.2
        if v.get("caveats"):
            self.bullets([c for c in v["caveats"]], 8.5)

    def metrics(self, items):
        self.ensure(0.9)
        n = len(items)
        cell = TEXT_W / n
        for i, (label, value) in enumerate(items):
            x = MARGIN + i * cell
            self.text(x, self.y, clean(label), 8.5, color=GREY)
            self.text(x, self.y + 0.2, clean(value), 15, "bold")
        self.y += 0.75

    def bar(self, counts, labels, colors):
        self.ensure(0.95)
        total = max(sum(counts), 1)
        ax = self.fig.add_axes([MARGIN / PAGE_W, 1 - (self.y + 0.45) / PAGE_H, TEXT_W / PAGE_W, 0.38 / PAGE_H])
        left = 0
        for c, lab, col in zip(counts, labels, colors):
            if c:
                ax.barh(0, c, left=left, color=col, height=1.0)
                ax.text(left + c / 2, 0, str(c), ha="center", va="center", color="white", fontsize=10, fontweight="bold")
            left += c
        ax.set_xlim(0, total)
        ax.axis("off")
        self.y += 0.5
        self.text(MARGIN, self.y, "   ".join(f"{lab}: {c}" for c, lab in zip(counts, labels)), 8.5, color=GREY)
        self.y += 0.3

    def scoreboard(self, names, rows, colors):
        wrap = lambda t, n: NL.join(textwrap.wrap(clean(t), n)) if t else ""  # noqa: E731
        cell_text, face, heights = [], [], []
        for label, note, (va, na), (vb, nb), winner in rows:
            first = wrap(label, 44) + NL + wrap(note, 52)
            a_txt = clean(va) + (NL + wrap(na, 24) if na else "")
            b_txt = clean(vb) + (NL + wrap(nb, 24) if nb else "")
            cell_text.append([first, a_txt, b_txt])
            heights.append(0.16 + 0.145 * max(t.count(NL) + 1 for t in (first, a_txt, b_txt)))
            face.append(["white", _tint(colors["A"], 0.82) if winner == "A" else "white",
                         _tint(colors["B"], 0.82) if winner == "B" else "white"])
        head_h = 0.32
        height = head_h + sum(heights)
        self.ensure(height)
        ax = self.fig.add_axes([MARGIN / PAGE_W, 1 - (self.y + height) / PAGE_H, TEXT_W / PAGE_W, height / PAGE_H])
        ax.axis("off")
        table = ax.table(cellText=cell_text, cellColours=face, colLabels=["", clean(names["A"]), clean(names["B"])],
                         colWidths=[0.46, 0.27, 0.27], loc="upper center", cellLoc="left")
        table.auto_set_font_size(False)
        table.set_fontsize(8.5)
        for (r, c), cell in table.get_celld().items():
            cell.set_edgecolor(RULE)
            cell.set_height((head_h if r == 0 else heights[r - 1]) / height)
            if r == 0 and c:
                cell.get_text().set_color(colors["A"] if c == 1 else colors["B"])
                cell.get_text().set_fontweight("bold")
            cell.PAD = 0.05
        self.y += height + 0.15

    def figure(self, fig, max_h=3.6, width=TEXT_W):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=200, facecolor="white", bbox_inches="tight")
        buf.seek(0)
        import matplotlib.image as mpimg
        img = mpimg.imread(buf)
        ratio = img.shape[0] / img.shape[1]
        w = min(width, max_h / ratio)
        h = w * ratio
        self.ensure(h + 0.15)
        ax = self.fig.add_axes([(MARGIN + (TEXT_W - w) / 2) / PAGE_W, 1 - (self.y + h) / PAGE_H, w / PAGE_W, h / PAGE_H])
        ax.imshow(img)
        ax.axis("off")
        self.y += h + 0.15

    def table(self, columns, rows, widths=None):
        row_h = 0.24
        n = len(rows)
        i = 0
        while i < n or i == 0:
            room = int((PAGE_H - MARGIN - 0.3 - self.y) / row_h) - 1
            if room < 4:
                self.new_page()
                continue
            chunk = rows[i:i + room]
            height = row_h * (len(chunk) + 1)
            ax = self.fig.add_axes([MARGIN / PAGE_W, 1 - (self.y + height) / PAGE_H, TEXT_W / PAGE_W, height / PAGE_H])
            ax.axis("off")
            widths = widths or [1 / len(columns)] * len(columns)
            table = ax.table(cellText=[[clean(c) for c in r] for r in chunk], colLabels=[clean(c) for c in columns],
                             colWidths=widths, loc="upper center", cellLoc="left")
            table.auto_set_font_size(False)
            table.set_fontsize(8)
            for (r, c), cell in table.get_celld().items():
                cell.set_edgecolor(RULE)
                cell.set_height(1 / (len(chunk) + 1))
                cell.set_facecolor("#f3f4f6" if r == 0 else "white")
                if r == 0:
                    cell.get_text().set_fontweight("bold")
            self.y += height + 0.15
            i += len(chunk)
            if i < n:
                self.new_page()
            if not n:
                break


def build_pdf(meta, blocks):
    """The finished PDF as bytes. `meta` = {"title", "callsign", "subtitle": [lines], "footer"}."""
    meta = dict(meta)
    meta.setdefault("footer", "Made with RBN Signal Mapper")
    meta["footer"] = f"{meta['footer']}  ·  generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"
    buf = io.BytesIO()
    with PdfPages(buf, metadata={"Title": clean(meta["title"]), "Author": "RBN Signal Mapper"}) as pdf:
        flow = _Flow(pdf, meta)
        for i, block in enumerate(blocks):
            kind, args = block[0], block[1:]
            if kind == "heading":  # keep the heading on the same page as the start of what it introduces
                nxt = blocks[i + 1] if i + 1 < len(blocks) else None
                need = 0.4 + {"figure": min(nxt[2], 4.0) if nxt and nxt[0] == "figure" else 1.0}.get(nxt[0] if nxt else "", 1.0)
                args = (args[0], need)
            getattr(flow, kind)(*args)
        flow.finish()
    return buf.getvalue()
