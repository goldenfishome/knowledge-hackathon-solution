"""Page history: cleaning, URL canonicalisation, authority tiers and per-date views.

Every snapshot is the FULL website on that day, so the "view" for a date is simply the newest
snapshot taken on or before it. Page text is stored once per content hash.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------- URLs and tiers

def canon_url(url: str) -> str:
    """'https://www.dupaco.com/Rates/#x' -> 'dupaco.com/rates'. Same page = same key."""
    p = urlparse(url if "//" in url else "https://" + url)
    host = p.netloc.lower().removeprefix("www.")
    path = re.sub(r"/+$", "", p.path.lower()) or "/"
    return host + path


# (kind, tier) by path. tier: 2 = rate sheet, 1 = other official page, 0 = low (may be stale)
_KIND_RULES = [
    (r"^/(rates|business-rates)$", "rate_sheet", 2),
    (r"^/(calculators|business-calculators)(/|$)", "calculator", 0),
    (r"^/hubfs/|\.pdf$", "pdf", 0),
    (r"^/(learn|newsroom|resources)(/|$)", "article", 0),
    (r"^/business/resources(/|$)", "article", 0),
    (r"^/20\d\d/", "article", 0),
]


def page_kind(url: str) -> tuple[str, int]:
    path = canon_url(url).split("/", 1)[1] if "/" in canon_url(url) else ""
    path = "/" + path
    for pat, kind, tier in _KIND_RULES:
        if re.search(pat, path):
            return kind, tier
    return "official", 1


# ---------------------------------------------------------------- cleaning

_DROP_LINE = re.compile(
    r"^(✕|load more|sort by:.*|#### filter|\* \* \*|#+|\d+ min read"
    r"|\[continue\]\(.*\)\s*cancel|- \S+ .*\+\d{1,5})$",
    re.I,
)
_CUT_AT = re.compile(r"^## handpicked for you\s*$", re.I)   # related-article teasers (other pages' dates/text)
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_PUB_DATE = re.compile(rf"^({MONTHS}) (\d{{1,2}}), (20\d\d)$")


def clean_markdown(md: str) -> str:
    md = md.replace("\\r\\n", "\n").replace("\r\n", "\n")
    out = []
    for line in md.split("\n"):
        s = line.strip()
        if _CUT_AT.match(s):
            break
        if s and _DROP_LINE.match(s):
            continue
        out.append(line.rstrip())
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def published_date(clean_md: str) -> Optional[str]:
    """Article byline date ('April 06, 2026' within the first lines) as ISO, else None."""
    for line in clean_md.split("\n")[:12]:
        m = _PUB_DATE.match(line.strip())
        if m:
            return datetime.strptime(" ".join(m.groups()), "%B %d %Y").date().isoformat()
    return None


# ---------------------------------------------------------------- storage

@dataclass
class PageVersion:
    url: str            # original url (as crawled)
    key: str            # canonical url
    title: str
    fetched_at: str
    snapshot_id: str
    taken_on: str
    hash: str
    kind: str
    tier: int
    published: Optional[str]


@dataclass
class SnapshotView:
    snapshot_id: str
    taken_on: str
    pages: dict[str, PageVersion] = field(default_factory=dict)   # canonical url -> version


class Store:
    def __init__(self):
        self.texts: dict[str, str] = {}               # hash -> cleaned markdown
        self.snapshots: list[SnapshotView] = []        # oldest first
        self.history: dict[str, list[PageVersion]] = {}  # canonical url -> versions (oldest first)

    def add_snapshot(self, snapshot) -> SnapshotView:
        view = SnapshotView(snapshot.snapshot_id, snapshot.taken_on)
        for p in snapshot.pages:
            text = clean_markdown(p.markdown)
            h = hashlib.sha1(text.encode("utf-8")).hexdigest()
            self.texts.setdefault(h, text)
            key = canon_url(p.url)
            kind, tier = page_kind(p.url)
            pv = PageVersion(p.url, key, p.title, p.fetched_at, snapshot.snapshot_id, snapshot.taken_on,
                             h, kind, tier, published_date(text) if kind == "article" else None)
            old = view.pages.get(key)          # '/x' and '/x/#frag' in one pull: keep the newer fetch
            if old is None or pv.fetched_at > old.fetched_at:
                view.pages[key] = pv
        for key, pv in view.pages.items():
            hist = self.history.setdefault(key, [])
            if not hist or hist[-1].hash != pv.hash:
                hist.append(pv)
        self.snapshots.append(view)
        self.snapshots.sort(key=lambda v: v.taken_on)
        return view

    def text(self, pv: PageVersion) -> str:
        return self.texts[pv.hash]

    def view(self, as_of: Optional[str] = None) -> SnapshotView:
        """Newest snapshot taken on/before as_of (default: newest). Before the first pull -> first."""
        if not self.snapshots:
            raise RuntimeError("nothing ingested")
        if not as_of:
            return self.snapshots[-1]
        d = to_iso(as_of) or as_of
        eligible = [v for v in self.snapshots if v.taken_on <= d]
        return eligible[-1] if eligible else self.snapshots[0]


def to_iso(s: str) -> Optional[str]:
    """'06/15/2026', '6/1/26', '2026-06-15' -> '2026-06-15'."""
    s = (s or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
    if m:
        mo, d, y = (int(x) for x in m.groups())
        y += 2000 if y < 100 else 0
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    return None
