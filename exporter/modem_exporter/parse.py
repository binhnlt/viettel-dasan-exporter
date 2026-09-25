"""Turn modem web pages (HTML, inline JS variables, JSON) into generic fields and tables."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

MAX_LABEL = 60
MAX_VALUE = 200
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "legend", "caption")
HEADING_CLASS = re.compile(r"title|header|heading|caption", re.IGNORECASE)
JS_VAR_RE = re.compile(
    r"""(?:\bvar|\blet|\bconst)?\s*\b([A-Za-z_]\w{1,40})\s*=\s*(["'])([^"'\n]{0,200})\2\s*;""",
)
JS_NUM_RE = re.compile(r"""(?:\bvar|\blet|\bconst)\s+([A-Za-z_]\w{1,40})\s*=\s*(-?\d+(?:\.\d+)?)\s*;""")


@dataclass
class Field:
    page: str
    section: str
    label: str
    value: str


@dataclass
class Table:
    page: str
    section: str
    columns: list[str]
    rows: list[list[str]] = field(default_factory=list)


@dataclass
class Extracted:
    fields: list[Field] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)


def clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.rstrip(":").strip()


def _text(el: Tag) -> str:
    """Visible text of an element; form controls contribute their value."""
    if el.name == "input":
        return clean(el.get("value", ""))
    if el.name == "select":
        opt = el.find("option", selected=True) or el.find("option")
        return clean(opt.get_text(" ")) if opt else ""
    parts = []
    for node in el.descendants:
        if isinstance(node, NavigableString):
            if node.parent and node.parent.name in ("script", "style", "option"):
                continue
            parts.append(str(node))
        elif isinstance(node, Tag) and node.name == "input" and node.get("type", "text").lower() in ("text", "hidden") and node.get("value"):
            parts.append(" " + node["value"] + " ")
        elif isinstance(node, Tag) and node.name == "select":
            opt = node.find("option", selected=True)
            if opt:
                parts.append(" " + opt.get_text(" ") + " ")
    return clean(" ".join(parts))


def _section_for(el: Tag) -> str:
    """Nearest preceding heading-ish element: used to group fields (e.g. 'Device Information')."""
    for prev in el.find_all_previous(limit=400):
        if not isinstance(prev, Tag):
            continue
        if prev.name in HEADING_TAGS or (
            prev.name in ("div", "span", "td", "th", "p", "strong", "b")
            and HEADING_CLASS.search(" ".join(prev.get("class", [])) + " " + (prev.get("id") or ""))
        ):
            txt = clean(prev.get_text(" "))
            if 0 < len(txt) <= MAX_LABEL:
                return txt
    return ""


def _is_label(text: str) -> bool:
    return 0 < len(text) <= MAX_LABEL and not re.fullmatch(r"[-\d.,:%\s]+", text) and bool(re.search(r"[A-Za-z]", text))


def _grid(table: Tag) -> list[list[tuple[str, bool]]]:
    """Expand a table into a grid honoring colspan/rowspan. Each cell is (text, is_header)."""

    def span(cell: Tag, attr: str) -> int:
        v = str(cell.get(attr, "1")).strip()
        return min(int(v), 20) if v.isdigit() and int(v) > 0 else 1

    grid: list[list[tuple[str, bool]]] = []
    pending: dict[int, tuple[tuple[str, bool], int]] = {}  # column -> (cell, rows remaining)
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        row: list[tuple[str, bool]] = []
        cells = tr.find_all(["td", "th"], recursive=False)
        col = 0

        def fill_pending(row: list, col: int) -> int:
            while col in pending:
                cell, left = pending[col]
                row.append(cell)
                if left <= 1:
                    del pending[col]
                else:
                    pending[col] = (cell, left - 1)
                col += 1
            return col

        for cell in cells:
            col = fill_pending(row, col)
            value = (_text(cell), cell.name == "th")
            rs = span(cell, "rowspan")
            for _ in range(span(cell, "colspan")):
                row.append(value)
                if rs > 1:
                    pending[col] = (value, rs - 1)
                col += 1
        fill_pending(row, col)
        if row:
            grid.append(row)
    return grid


def _parse_table(table: Tag, page: str, out: Extracted) -> None:
    grid = _grid(table)
    if not grid:
        return
    width = max(len(r) for r in grid)
    if width <= 2:
        for row in grid:
            if len(row) == 2 and _is_label(row[0][0]) and len(row[1][0]) <= MAX_VALUE:
                out.fields.append(Field(page, _section_for(table), row[0][0], row[1][0]))
        return

    # Multi-column table: header rows are rows made of <th>, or the first row.
    header_rows = []
    for row in grid:
        if all(is_h for _, is_h in row):
            header_rows.append(row)
        else:
            break
    if not header_rows:
        header_rows = [grid[0]]
    body = grid[len(header_rows):]
    columns = []
    for i in range(width):
        parts = []
        for hr in header_rows:
            if i < len(hr) and hr[i][0] and hr[i][0] not in parts:
                parts.append(hr[i][0])
        columns.append(" ".join(parts) or f"col{i}")
    rows = [[c for c, _ in r] + [""] * (width - len(r)) for r in body if len(r) >= 2]
    if rows:
        out.tables.append(Table(page, _section_for(table), columns, rows))


def _parse_pairs(soup: BeautifulSoup, page: str, out: Extracted, seen: set[tuple[str, str]]) -> None:
    """Label/value pairs outside tables: <dt>/<dd>, <label>+value, and 2-child div rows."""
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            _add_pair(out, seen, page, dt, _text(dt), _text(dd))

    for el in soup.find_all(["div", "li", "p", "span"]):
        if el.find_parent("table"):
            continue
        kids = [k for k in el.children if isinstance(k, Tag) and k.name not in ("script", "style", "br")]
        if len(kids) == 2 and not kids[0].find(["div", "table", "ul"]) and not kids[1].find(["div", "table", "ul"]):
            _add_pair(out, seen, page, el, _text(kids[0]), _text(kids[1]))
        elif not kids:
            # "Label: value" inside a single element.
            txt = clean(el.get_text(" "))
            m = re.match(r"^([A-Za-z][^:]{0,58}):\s*(.+)$", el.get_text(" ").strip())
            if m and len(txt) <= MAX_LABEL + MAX_VALUE:
                _add_pair(out, seen, page, el, clean(m.group(1)), clean(m.group(2)))

    for lab in soup.find_all("label"):
        target = None
        if lab.get("for"):
            target = soup.find(id=lab["for"])
        if target is None:
            target = lab.find_next_sibling()
        if target is not None:
            _add_pair(out, seen, page, lab, _text(lab), _text(target))


def _add_pair(out: Extracted, seen: set, page: str, anchor: Tag, label: str, value: str) -> None:
    if not _is_label(label) or not value or len(value) > MAX_VALUE or label == value:
        return
    section = _section_for(anchor)
    key = (section, label)
    if key in seen:
        return
    seen.add(key)
    out.fields.append(Field(page, section, label, value))


def _parse_js_vars(soup: BeautifulSoup, page: str, out: Extracted) -> None:
    """Many embedded UIs inject status values as JS variables in inline <script> blocks."""
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        code = script.string or script.get_text()
        for m in JS_VAR_RE.finditer(code):
            name, value = m.group(1), m.group(3).strip()
            if value and not value.startswith(("/", "http", "#", "<")) and not name.isupper():
                out.fields.append(Field(page, "js", name, value))
        for m in JS_NUM_RE.finditer(code):
            out.fields.append(Field(page, "js", m.group(1), m.group(2)))


def _flatten_json(obj, page: str, out: Extracted, path: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, page, out, path + [str(k)])
    elif isinstance(obj, list):
        # List of homogeneous objects => table (e.g. per-interface stats)
        if obj and all(isinstance(x, dict) for x in obj):
            cols = sorted({k for x in obj for k, v in x.items() if not isinstance(v, (dict, list))})
            key_col = next((c for c in cols if re.search(r"name|interface|port|ifname|id", c, re.IGNORECASE)), None)
            if key_col:
                cols = [key_col] + [c for c in cols if c != key_col]
            rows = [[str(x.get(c, "")) for c in cols] for x in obj]
            out.tables.append(Table(page, ".".join(path), cols, rows))
        else:
            for i, v in enumerate(obj):
                _flatten_json(v, page, out, path + [str(i)])
    elif obj is not None and path:
        value = ("1" if obj else "0") if isinstance(obj, bool) else str(obj)
        out.fields.append(Field(page, ".".join(path[:-1]), path[-1], value))


def extract(page_path: str, content_type: str, text: str) -> Extracted:
    out = Extracted()
    stripped = text.lstrip()
    if "json" in content_type or stripped[:1] in ("{", "["):
        try:
            _flatten_json(json.loads(text), page_path, out, [])
            return out
        except ValueError:
            pass
    if "javascript" in content_type or page_path.split("?")[0].endswith(".js"):
        return out  # external scripts are only used for link discovery
    soup = BeautifulSoup(text, "html.parser")
    seen: set[tuple[str, str]] = set()
    for table in soup.find_all("table"):
        _parse_table(table, page_path, out)
    for f in out.fields:
        seen.add((f.section, f.label))
    _parse_pairs(soup, page_path, out, seen)
    _parse_js_vars(soup, page_path, out)
    return out
