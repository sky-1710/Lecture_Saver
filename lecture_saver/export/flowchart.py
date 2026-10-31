"""A Mermaid flowchart, drawn into a PDF with no browser and no service.

The preview's concept maps arrive as Mermaid source. Printing that source would
be a page of arrows in text, and Mermaid itself needs a browser, so the part of
its language the assistant is told to use is read and drawn here.

:func:`parse_flowchart` reads the source, or returns ``None`` for what it cannot
read, and the caller prints that source as it came rather than guess at it.
:func:`layout_chart` places the boxes in layers, the usual way to draw a
directed graph: each box a layer below whatever points at it, edges that skip
a layer routed through it, and the boxes of a layer ordered to keep lines from
crossing. Pure: it is told how big each label is and knows nothing of PDFs.
:class:`FlowchartImage` draws the result; a picture too large for the page is
scaled down, and one that would be unreadable is turned on its side. The lecture
draws the same layout on screen, in :mod:`lecture_saver.ui.flowchart_picture`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import product

from .pdf import FONT_REGULAR, printable

__all__ = [
    "Edge",
    "Flowchart",
    "FlowchartImage",
    "LABEL_WIDTH",
    "Layout",
    "PAD_X",
    "PAD_Y",
    "Route",
    "TEXT_WIDTH",
    "best_fit",
    "is_readable",
    "layout_chart",
    "parse_flowchart",
]

_HEADER = re.compile(r"^(?:flowchart|graph)(?:\s+(TD|TB|BT|LR|RL))?\s*;?$", re.IGNORECASE)
#: Lines that change how a chart looks, not what it says.
_COSMETIC = re.compile(r"^(?:subgraph\b|end\b|direction\b|style\b|classDef\b|class\b|linkStyle\b)")
_ID = re.compile(r"\w+")
#: Longest first: ``([`` must not be read as ``(``.
_SHAPES = (
    ("([", "])"), ("[[", "]]"), ("((", "))"), ("{{", "}}"), ("[/", "/]"),
    ("[", "]"), ("(", ")"), ("{", "}"),
)
#: ``-- text -->`` first, or its ``--`` would be read as a link to a box "text".
_LINK = re.compile(
    r"\s*(?:"
    r"(?P<open>--|==|-\.)\s+(?P<inner>.*?)\s+(?P<close>-->|==>|\.->|---|===)"
    r"|<?(?P<plain>-{2,}|={2,}|-\.+-)(?P<head>>|x\b|o\b)?\s*(?:\|(?P<piped>[^|]*)\|)?"
    r")\s*"
)
_LINE_BREAK = re.compile(r"<br\s*/?>|\\n", re.IGNORECASE)


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    label: str = ""
    #: ``---`` joins two boxes without saying which way.
    arrow: bool = True


@dataclass(frozen=True)
class Flowchart:
    #: Reads left to right rather than top to bottom.
    across: bool
    #: Box id -> its words, in the order the boxes first appear.
    nodes: dict[str, str]
    edges: tuple[Edge, ...]


def _clean(label: str) -> str:
    text = _LINE_BREAK.sub("\n", label).strip().strip('"').strip()
    return "\n".join(line.strip() for line in text.splitlines())


def _shape(line: str, position: int) -> tuple[str, int] | None:
    """The words in the brackets at ``position``; ``None`` if they never close."""
    for opener, closer in _SHAPES:
        if not line.startswith(opener, position):
            continue
        start = position + len(opener)
        quoted = line.startswith('"', start)
        end = line.find(f'"{closer}' if quoted else closer, start + (1 if quoted else 0))
        if end < 0:
            return None
        return line[start:end + (1 if quoted else 0)], end + len(closer) + (1 if quoted else 0)
    return "", position


def _nodes(line: str, position: int, known: dict[str, str]) -> tuple[list[str], int] | None:
    """``A``, ``A["words"]`` or ``A & B``: the ids, and where they end."""
    found: list[str] = []
    while True:
        position += len(line[position:]) - len(line[position:].lstrip())
        name = _ID.match(line, position)
        if name is None:
            return None
        shaped = _shape(line, name.end())
        if shaped is None:
            return None
        label, position = shaped
        known.setdefault(name.group(), name.group())
        if _clean(label):
            known[name.group()] = _clean(label)
        found.append(name.group())
        ahead = line[position:].lstrip()
        if not ahead.startswith("&"):
            return found, position
        position = len(line) - len(ahead) + 1


def _statement(line: str, known: dict[str, str], edges: list[Edge]) -> bool:
    """Read ``A --> B --> C``. ``False`` if any of it could not be read."""
    read = _nodes(line, 0, known)
    if read is None:
        return False
    sources, position = read
    while True:
        rest = line[position:].strip()
        if not rest:
            return True
        if rest.startswith(";"):
            return _statement(rest[1:], known, edges) if rest[1:].strip() else True
        link = _LINK.match(line, position)
        if link is None:
            return False
        read = _nodes(line, link.end(), known)
        if read is None:
            return False
        targets, position = read
        words = _clean(link["inner"] or link["piped"] or "")
        arrow = bool(link["head"]) or (link["close"] or "").endswith(">")
        edges += [Edge(a, b, words, arrow) for a, b in product(sources, targets) if a != b]
        sources = targets


def parse_flowchart(source: str) -> Flowchart | None:
    """Read Mermaid source, or ``None`` if it is not a flowchart this can draw."""
    lines = [line.strip() for line in source.splitlines()]
    lines = [line for line in lines if line and not line.startswith("%%")]
    if not lines:
        return None
    header = _HEADER.match(lines[0])
    if header is None:
        return None
    known: dict[str, str] = {}
    edges: list[Edge] = []
    for line in lines[1:]:
        if not _COSMETIC.match(line) and not _statement(line, known, edges):
            return None
    if not known:
        return None
    direction = (header.group(1) or "TD").upper()
    seen: dict[tuple[str, str], Edge] = {}
    for edge in edges:
        seen.setdefault((edge.source, edge.target), edge)
    return Flowchart(direction in ("LR", "RL"), known, tuple(seen.values()))


# -- placing the boxes -------------------------------------------------------------------

#: Width of a box's words before they wrap, in points.
TEXT_WIDTH = 116.0
PAD_X = 8.0
PAD_Y = 6.0
#: Width of the words on a line before they wrap.
LABEL_WIDTH = 84.0
_FONT_SIZE = 9.0
_LEADING = 12.5
_LABEL_SIZE = 8.0
#: Between boxes of one layer, and between layers.
_GAP_ACROSS = 18.0
_GAP_ALONG = 32.0
#: Added between layers when a line between them has words on it.
_GAP_FOR_LABEL = 16.0
#: Room a line skipping a layer takes up in it.
_THROUGH = 6.0
_SWEEPS = 6
#: Smaller than this and the words are too small to read.
_SMALLEST_SCALE = 0.6


@dataclass(frozen=True)
class Box:
    """A box's centre and size, y growing downwards."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class Route:
    points: tuple[tuple[float, float], ...]
    label: str = ""
    arrow: bool = True


@dataclass(frozen=True)
class Layout:
    width: float
    height: float
    boxes: dict[str, Box]
    routes: tuple[Route, ...]


def _ranks(chart: Flowchart) -> tuple[dict[str, int], set[Edge]]:
    """Each box's layer, and the edges that had to be turned to break a cycle."""
    following: dict[str, list[Edge]] = {node: [] for node in chart.nodes}
    for edge in chart.edges:
        following[edge.source].append(edge)
    turned: set[Edge] = set()
    open_nodes: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        open_nodes.add(node)
        for edge in following[node]:
            if edge.target in open_nodes:
                turned.add(edge)
            elif edge.target not in done:
                visit(edge.target)
        open_nodes.discard(node)
        done.add(node)

    for node in chart.nodes:
        if node not in done:
            visit(node)

    forward = [
        (e.target, e.source) if e in turned else (e.source, e.target) for e in chart.edges
    ]
    rank = dict.fromkeys(chart.nodes, 0)
    for _ in chart.nodes:
        for above, below in forward:
            rank[below] = max(rank[below], rank[above] + 1)
    return rank, turned


def _mean(values: list[float], default: float) -> float:
    return sum(values) / len(values) if values else default


def _settle(
    layer: list[str], wanted: dict[str, float], size: Callable[[str], float]
) -> dict[str, float]:
    """Keep the order and the gaps, and otherwise be as near ``wanted`` as can be."""
    placed: list[float] = []
    for index, key in enumerate(layer):
        spot = wanted[key]
        if placed:
            nearest = placed[-1] + (size(layer[index - 1]) + size(key)) / 2 + _GAP_ACROSS
            spot = max(spot, nearest)
        placed.append(spot)
    drift = _mean([spot - wanted[key] for key, spot in zip(layer, placed, strict=True)], 0.0)
    return {key: spot - drift for key, spot in zip(layer, placed, strict=True)}


def layout_chart(
    chart: Flowchart, measure: Callable[[str], tuple[float, float]]
) -> Layout:
    """Place the boxes. ``measure`` gives a label's width and height as a box."""
    rank, turned = _ranks(chart)
    sizes = {node: measure(label) for node, label in chart.nodes.items()}
    wide = 1 if chart.across else 0  # which of width, height runs across the layers

    def across_size(key: str) -> float:
        return sizes[key][wide] if key in sizes else _THROUGH

    def along_size(key: str) -> float:
        return sizes[key][1 - wide] if key in sizes else 0.0

    layers: list[list[str]] = [[] for _ in range(max(rank.values()) + 1)]
    for node in chart.nodes:
        layers[rank[node]].append(node)

    # An edge across several layers passes through each as a box of no size,
    # which is what keeps it from running behind the boxes there.
    above: dict[str, list[str]] = {key: [] for layer in layers for key in layer}
    below: dict[str, list[str]] = {key: [] for layer in layers for key in layer}
    chains: list[list[str]] = []
    for number, edge in enumerate(chart.edges):
        top, bottom = (edge.target, edge.source) if edge in turned else (edge.source, edge.target)
        chain = [top]
        for between in range(rank[top] + 1, rank[bottom]):
            passing = f"\0{number}:{between}"
            layers[between].append(passing)
            above[passing], below[passing] = [], []
            chain.append(passing)
        chain.append(bottom)
        for upper, lower in zip(chain, chain[1:], strict=False):
            below[upper].append(lower)
            above[lower].append(upper)
        chains.append(chain)

    # Order each layer by where its neighbours are, sweeping down and up.
    index: dict[str, float] = {k: float(i) for layer in layers for i, k in enumerate(layer)}
    for sweep in range(_SWEEPS):
        downward = sweep % 2 == 0
        neighbours = above if downward else below
        for layer in layers[1:] if downward else layers[-2::-1]:
            layer.sort(key=lambda k: _mean([index[n] for n in neighbours[k]], index[k]))
            index.update({key: float(i) for i, key in enumerate(layer)})

    # Then the same sweeps for where each box goes across its layer.
    across: dict[str, float] = {}
    for layer in layers:
        across.update(_settle(layer, dict.fromkeys(layer, 0.0), across_size))
    for sweep in range(_SWEEPS):
        downward = sweep % 2 == 0
        neighbours = above if downward else below
        for layer in layers[1:] if downward else layers[-2::-1]:
            wanted = {k: _mean([across[n] for n in neighbours[k]], across[k]) for k in layer}
            across.update(_settle(layer, wanted, across_size))

    along: dict[str, float] = {}
    labelled = {
        rank[chain[0]] + position
        for chain, edge in zip(chains, chart.edges, strict=True) if edge.label
        for position in range(len(chain) - 1)
    }
    reached = 0.0
    for number, layer in enumerate(layers):
        depth = max(along_size(key) for key in layer) if layer else 0.0
        for key in layer:
            along[key] = reached + depth / 2
        reached += depth + _GAP_ALONG + (_GAP_FOR_LABEL if number in labelled else 0.0)

    left = min(across[k] - across_size(k) / 2 for k in across)
    right = max(across[k] + across_size(k) / 2 for k in across)
    deepest = max(along[k] + along_size(k) / 2 for k in along)

    def place(cross: float, main: float) -> tuple[float, float]:
        cross -= left
        return (main, cross) if chart.across else (cross, main)

    boxes = {}
    for node in chart.nodes:
        x, y = place(across[node], along[node])
        boxes[node] = Box(x, y, sizes[node][0], sizes[node][1])

    routes = []
    for chain, edge in zip(chains, chart.edges, strict=True):
        first, last = chain[0], chain[-1]
        points = [place(across[first], along[first] + along_size(first) / 2)]
        points += [place(across[key], along[key]) for key in chain[1:-1]]
        points.append(place(across[last], along[last] - along_size(last) / 2))
        if edge in turned:
            points.reverse()
        routes.append(Route(tuple(points), edge.label, edge.arrow))

    span, depth = right - left, deepest
    width, height = (depth, span) if chart.across else (span, depth)
    return Layout(width, height, boxes, tuple(routes))


# -- drawing -----------------------------------------------------------------------------


def _markup(label: str) -> str:
    return "<br/>".join(printable(line) for line in label.splitlines())


def _measure(label: str) -> tuple[float, float]:
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Paragraph

    natural = max(
        (stringWidth(line, FONT_REGULAR, _FONT_SIZE) for line in label.splitlines()), default=0.0
    )
    text_width = min(natural + 2, TEXT_WIDTH)
    _, height = Paragraph(_markup(label), _box_style()).wrap(text_width, 10000)
    return text_width + 2 * PAD_X, height + 2 * PAD_Y


def _box_style() -> object:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle(
        "flow_box", fontName=FONT_REGULAR, fontSize=_FONT_SIZE, leading=_LEADING,
        alignment=TA_CENTER, textColor=colors.HexColor("#111827"),
    )


def _label_style() -> object:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.styles import ParagraphStyle

    return ParagraphStyle(
        "flow_label", fontName=FONT_REGULAR, fontSize=_LABEL_SIZE, leading=10.5,
        alignment=TA_CENTER, textColor=colors.HexColor("#1D4ED8"),
    )


def is_readable(scale: float) -> bool:
    return scale >= _SMALLEST_SCALE


def best_fit(
    chart: Flowchart,
    width: float,
    height: float,
    measure: Callable[[str], tuple[float, float]] = _measure,
) -> tuple[Layout, float, Flowchart]:
    """The layout, scale and chart to draw: as written, unless that is unreadable."""
    fit = layout_chart(chart, measure)
    scale = min(1.0, width / fit.width, height / fit.height)
    if is_readable(scale):
        return fit, scale, chart
    turned = replace(chart, across=not chart.across)
    other = layout_chart(turned, measure)
    other_scale = min(1.0, width / other.width, height / other.height)
    if other_scale > scale:
        return other, other_scale, turned
    return fit, scale, chart


def _make_flowable() -> type:
    """The class itself: ReportLab is imported only when a PDF is being made."""
    from reportlab.lib import colors
    from reportlab.platypus import Flowable, Paragraph

    stroke = colors.HexColor("#1D4ED8")
    fill = colors.HexColor("#EFF6FF")
    line = colors.HexColor("#6B7280")

    class _Image(Flowable):
        def __init__(self, chart: Flowchart, width: float, height: float) -> None:
            super().__init__()
            self.hAlign = "CENTER"
            self._layout, self._scale, self._chart = best_fit(chart, width, height)
            self.readable = is_readable(self._scale)

        def wrap(self, _available_width: float, _available_height: float) -> tuple[float, float]:
            return self._layout.width * self._scale, self._layout.height * self._scale

        def draw(self) -> None:
            canvas, layout = self.canv, self._layout
            canvas.saveState()
            canvas.scale(self._scale, self._scale)
            for route in layout.routes:
                self._route(route)
            for node, box in layout.boxes.items():
                left, top = box.x - box.width / 2, layout.height - box.y + box.height / 2
                canvas.setStrokeColor(stroke)
                canvas.setFillColor(fill)
                canvas.setLineWidth(0.8)
                canvas.roundRect(left, top - box.height, box.width, box.height, 5, fill=1)
                words = Paragraph(_markup(self._chart.nodes[node]), _box_style())
                text_width = box.width - 2 * PAD_X
                _, text_height = words.wrap(text_width, 10000)
                words.drawOn(
                    canvas, left + PAD_X, top - box.height + (box.height - text_height) / 2
                )
            for route in layout.routes:
                if route.label:
                    self._label(route)
            canvas.restoreState()

        def _route(self, route: Route) -> None:
            canvas, height = self.canv, self._layout.height
            points = [(x, height - y) for x, y in route.points]
            canvas.setStrokeColor(line)
            canvas.setFillColor(line)
            canvas.setLineWidth(0.9)
            path = canvas.beginPath()
            path.moveTo(*points[0])
            for point in points[1:]:
                path.lineTo(*point)
            canvas.drawPath(path, stroke=1, fill=0)
            if route.arrow:
                self._arrowhead(points[-2], points[-1])

        def _arrowhead(self, start: tuple[float, float], tip: tuple[float, float]) -> None:
            canvas = self.canv
            dx, dy = tip[0] - start[0], tip[1] - start[1]
            length = (dx * dx + dy * dy) ** 0.5 or 1.0
            ux, uy = dx / length, dy / length
            back_x, back_y = tip[0] - 7 * ux, tip[1] - 7 * uy
            path = canvas.beginPath()
            path.moveTo(*tip)
            path.lineTo(back_x - 3.2 * uy, back_y + 3.2 * ux)
            path.lineTo(back_x + 3.2 * uy, back_y - 3.2 * ux)
            path.close()
            canvas.drawPath(path, stroke=0, fill=1)

        def _label(self, route: Route) -> None:
            canvas, height = self.canv, self._layout.height
            middle = (len(route.points) - 1) // 2
            (x1, y1), (x2, y2) = route.points[middle], route.points[middle + 1]
            x, y = (x1 + x2) / 2, height - (y1 + y2) / 2
            words = Paragraph(_markup(route.label), _label_style())
            width, text_height = words.wrap(LABEL_WIDTH, 1000)
            canvas.setFillColor(colors.white)
            canvas.rect(
                x - width / 2 - 1, y - text_height / 2 - 1, width + 2, text_height + 2,
                stroke=0, fill=1,
            )
            words.drawOn(canvas, x - width / 2, y - text_height / 2)

    return _Image


def FlowchartImage(chart: Flowchart, width: float, height: float) -> object:  # noqa: N802
    """A flowable drawing ``chart`` within ``width`` by ``height``.

    Its ``readable`` is ``False`` when even the best fit is too small to read.
    A factory, because a ``Flowable`` subclass needs ReportLab imported, and
    importing this module must not.
    """
    return _make_flowable()(chart, width, height)
