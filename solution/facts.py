"""Structured facts from markdown rate tables.

One fact = one percentage cell:
    product   the section heading above the table   ("Share Savings", "Regular Certificates")
    quals     the row's non-rate cells, normalised  ("$10000-$24999.99",) or ("2024-27 model year", "60 month")
    metric    rate / apy / apr (from the column header)
    value     float, e.g. 0.15
    effective the 'Rates are effective as of …' date written right under that table (ISO)

The fact key (product, quals, metric) is what answers and conflicts are matched on, so two rows
that differ in any qualifier (term, tier, model year, business vs personal) are different facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from store import to_iso

PCT = re.compile(r"(\d{1,2}\.\d{1,3})\s*%")
EFFECTIVE = re.compile(r"effective\s+(?:as\s+of\s+)?(\d{1,2}/\d{1,2}/\d{2,4})", re.I)
SEP_ROW = re.compile(r"^\|\s*:?-{3,}")
# columns that describe the row but are not part of its identity
# first-column headers whose (digit-free) cells name the product itself: "| Share Savings | $25,000+ |…"
PRODUCT_COLS = re.compile(r"^(account type|account|certificate|product|card type|ira type|loan type)$", re.I)
IGNORED_COLS = re.compile(r"payment|example|points|min\.? to (open|earn)", re.I)


@dataclass(frozen=True)
class Fact:
    segment: str             # "personal" | "business"
    product: str
    section: str             # headings above the table, nearest first ("share savings > savings accounts")
    quals: tuple[str, ...]   # sorted, normalised row qualifiers ("2024-27 model year", "60 month")
    metric: str
    value: float
    raw: str                 # the cell text as written, e.g. "3.40%1"
    row: str                 # the whole row (for quoting)
    effective: Optional[str]
    url: str
    key_url: str
    snapshot_id: str
    taken_on: str
    kind: str
    tier: int

    @property
    def key(self):
        return (self.segment, self.product, self.quals, self.metric)


# ---------------------------------------------------------------- normalisation

def norm_text(s: str) -> str:
    s = s.replace("\\", "").replace("*", "")
    s = re.sub(r"[–—]", "-", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def norm_qual(s: str) -> str:
    s = norm_text(s)
    s = re.sub(r"\$\s*([\d,]+)(?:\.00)?\b", lambda m: "$" + m.group(1).replace(",", ""), s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = re.sub(r"\bmonths\b", "month", s)
    s = re.sub(r"(?<=[a-z%)])\d$", "", s)          # trailing footnote marker: "Traditional1"
    return s


def metric_of(header: str) -> Optional[str]:
    h = norm_text(header)
    if "apy" in h or "annual percentage yield" in h:
        return "apy"
    if "apr" in h or "annual percentage rate" in h:
        return "apr"
    if "dividend" in h or h in ("rate", "rates") or h.endswith(" rate"):
        return "rate"
    return None


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or s.startswith(("|", "[", "!", "-", ">")) or len(s) > 70:
        return False
    if EFFECTIVE.search(s) or re.match(r"^\d[A-Za-z]", s) or re.search(r"[.:?!]$", s) or len(s.split()) > 8:
        return False
    return bool(re.search(r"[A-Za-z]", s))


def _heading(s: str) -> str:
    return norm_text(re.sub(r"^#+\s*", "", s).strip("* "))


# ---------------------------------------------------------------- extraction

def extract_tables(text: str):
    """Yield (headings, header_cells, rows, effective_iso, extra_effective) for each markdown table."""
    lines = text.split("\n")
    i, prev_end = 0, 0
    while i < len(lines):
        if lines[i].startswith("|") and i + 1 < len(lines) and SEP_ROW.match(lines[i + 1]):
            start, header = i, _cells(lines[i])
            j = i + 2
            rows = []
            while j < len(lines) and lines[j].startswith("|"):
                rows.append(lines[j])
                j += 1
            # headings: up to two heading-like lines above, not crossing the previous table
            heads = []
            for k in range(start - 1, max(prev_end, start - 25) - 1, -1):
                if _is_heading(lines[k]):
                    h = _heading(lines[k])
                    if h and h not in heads:
                        heads.append(h)
                    if len(heads) == 2:
                        break
            # effective date: first statement after the table, before the next table.
            # Plain lines beat footnote lines ("1Rates are effective …").
            plain, foot = None, None
            for k in range(j, min(len(lines), j + 14)):
                if lines[k].startswith("|"):
                    break
                m = EFFECTIVE.search(lines[k])
                if m:
                    iso = to_iso(m.group(1))
                    if re.match(r"^\W*\d[A-Za-z]", lines[k].strip()):
                        foot = foot or iso
                    else:
                        plain = plain or iso
            yield heads, header, rows, plain or foot
            prev_end = j
            i = j
        else:
            i += 1


def facts_from_page(text: str, pv) -> list[Fact]:
    out = []
    for heads, header, rows, eff in extract_tables(text):
        metrics = [metric_of(h) for h in header]
        if not any(metrics):
            # 2-column "label | 2.90%" tables: header cell itself is a row
            if len(header) == 2 and PCT.search(header[1]):
                rows = ["| " + " | ".join(header) + " |"] + rows
                header, metrics = ["product", "apr"], [None, "apr"]
            else:
                continue
        section = " > ".join(heads)
        for row in rows:
            cells = _cells(row)
            if len(cells) != len(header):
                continue
            product = heads[0] if heads else norm_text(header[0])
            if PRODUCT_COLS.match(norm_text(header[0])) and cells[0] and not re.search(r"\d", cells[0]):
                product = norm_text(re.sub(r"\d+$", "", cells[0]))
            segment = "business" if (pv.key.split("/", 1)[-1].startswith(("business", "business-"))
                                     or re.search(r"\bbusiness\b", product)) else "personal"
            product = re.sub(r"^business\s+|\s*\(business\)$", "", product).strip() or product
            quals = []
            for h, c, m in zip(header, cells, metrics):
                if m or IGNORED_COLS.search(h) or PCT.search(c):
                    continue
                q = norm_qual(c)
                if q and q != product and not product.startswith(q):
                    quals.append(q)
            for h, c, m in zip(header, cells, metrics):
                if not m:
                    continue
                pm = PCT.findall(c)
                if len(pm) != 1:          # ranges like "12.50% – 19.50%" are kept out of point facts
                    continue
                m = metric_of(c) or m     # the cell's own unit wins: some tables mislabel columns
                out.append(Fact(segment, product, section, tuple(sorted(set(quals))), m, float(pm[0]), c, row.strip(), eff,
                                pv.url, pv.key, pv.snapshot_id, pv.taken_on, pv.kind, pv.tier))
    return out
