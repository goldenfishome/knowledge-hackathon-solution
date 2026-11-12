"""Finding places where the website contradicts itself, and deciding which value is right.

    1. line up    in each snapshot, put table rows from different pages side by side when they
                  describe the SAME fact: same segment, same metric (rate / APY / APR), same tier,
                  and product names where one is contained in the other ("auto loans" ~ "auto &
                  motorcycle loans"). Model-year buckets are compared by position (newest bucket vs
                  newest bucket), because the site shifts the years every model year.
    2. compare    a contradiction = two pages showing different values for that fact on the same
                  day. Different tiers, rate vs APY, personal vs business are never compared.
    3. resolve    the same rule answer() uses: newest effective date first, then page trust
                  (rate sheet > other official page > calculator / article / PDF).
    4. report     one conflict per product and term ("auto loan, 60 months"), covering its tiers;
                  values and resolution come from the newest snapshot where it shows.

A rate that simply changed between snapshots is an update, not a contradiction, and is not listed.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from rates import GENERIC, _pct, _title, tokens

# words that don't tell products apart ("auto loan rates" ~ "auto loans")
PRODUCT_FILLER = {"rate", "rates", "loan", "loans", "account", "accounts", "&", "and", "-", "the",
                  "personal", "new", "used"}
KIND_NAME = {"rate_sheet": "official rate sheet", "official": "official product page",
             "calculator": "calculator", "article": "article", "pdf": "PDF"}
TERM = re.compile(r"\b\d{1,3} ?months?\b|\b\d{1,2}-year\b")


def product_words(product: str) -> frozenset:
    return frozenset(t for t in tokens(product) if t not in PRODUCT_FILLER)


def same_product(a: frozenset, b: frozenset) -> bool:
    small = min(a, b, key=len)
    return bool(small - GENERIC) and (a <= b or b <= a)   # "savings" alone names no product


def _model_year(q: str):
    m = re.search(r"(\d{4})-(\d{2,4})", q)
    if "model year" not in q or not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2)) if len(m.group(2)) == 4 else int(m.group(1)[:2] + m.group(2))
    return max(a, b)


def tier_signature(table: list) -> dict:
    """fact -> tier; model-year quals become their position in the table (0 = newest)."""
    years = sorted({y for f in table for q in f.quals if (y := _model_year(q))}, reverse=True)
    return {f: tuple(f"model year bucket #{years.index(y)}" if (y := _model_year(q)) else q for q in f.quals)
            for f in table}


def _rank(f):
    return (f.effective or "", f.tier)


def _vtext(f) -> str:
    v = _pct(f.value) if f.value_hi is None else f"{_pct(f.value)}-{_pct(f.value_hi)}"
    return f"{v} {f.metric.upper() if f.metric in ('apy', 'apr') else 'rate'}"


@dataclass
class Found:
    segment: str
    words: frozenset
    term: str
    snapshot_id: str = ""
    products: set = field(default_factory=set)
    rows: dict = field(default_factory=dict)       # (product, metric, tier) -> (winner, [losers])
    broken: set = field(default_factory=set)       # pages whose row contradicts itself (rate > APY)


def _pairs(facts: list):
    """Yield (winner, loser) for every two pages that disagree on one fact in this snapshot."""
    tables = defaultdict(list)
    for f in facts:
        tables[(f.key_url, f.product, f.effective)].append(f)
    groups = defaultdict(list)                            # (segment, metric, tier) -> rows
    for table in tables.values():
        for f, sig in tier_signature(table).items():
            if sig:
                groups[(f.segment, f.metric, sig)].append(f)
    # a deposit row whose dividend rate is above its own APY can't be right (APY >= rate)
    shown = defaultdict(dict)
    for (seg, metric, sig), rows in groups.items():
        for f in rows:
            shown[(f.key_url, f.product, sig)][metric] = f.value
    broken = {k for k, m in shown.items() if "rate" in m and "apy" in m and m["rate"] > m["apy"] + 1e-9}
    for (_, _, sig), rows in groups.items():
        # a page showing two values for one tier and product is a promo next to the regular
        # table (or a misread header), not a contradiction between pages: leave it out
        per = defaultdict(set)
        for f in rows:
            per[(f.key_url, f.product)].add((f.value, f.value_hi))
        rows = [f for f in rows if len(per[(f.key_url, f.product)]) == 1]
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if a.key_url == b.key_url or (a.value, a.value_hi) == (b.value, b.value_hi):
                    continue
                if not same_product(product_words(a.product), product_words(b.product)):
                    continue
                ba, bb = (a.key_url, a.product, sig) in broken, (b.key_url, b.product, sig) in broken
                if ba != bb:
                    yield ((b, a) if ba else (a, b)) + (sig, True)
                elif _rank(a) != _rank(b):                # equal rank: nothing says which is right
                    yield ((a, b) if _rank(a) > _rank(b) else (b, a)) + (sig, False)


def find_conflicts(views: list[tuple[str, list]]) -> list[Found]:
    """views: [(snapshot id, facts visible in it)], oldest first."""
    pairs = [(sid, win, lose, sig, bad) for sid, facts in views for win, lose, sig, bad in _pairs(facts)]

    # product names that were compared with each other describe one product family
    # ("auto loans" on a calculator ~ "auto & motorcycle loans" on the rate sheet)
    parent: dict = {}
    def root(p):
        while parent.setdefault(p, p) != p:
            p = parent[p]
        return p
    for _, win, lose, _, _ in pairs:
        parent[root((lose.segment, lose.product))] = root((win.segment, win.product))

    found: dict[tuple, Found] = {}
    for sid, win, lose, sig, bad in pairs:               # oldest snapshot first
        term = next((m.group(0).replace(" ", "-") for q in win.quals if (m := TERM.search(q))), "")
        fam = root((win.segment, win.product))
        c = found.setdefault((fam, term), Found(win.segment, product_words(fam[1]), term))
        if c.snapshot_id != sid:                         # newer snapshot: report what it shows
            c.snapshot_id, c.rows, c.products, c.broken = sid, {}, set(), set()
        if bad:
            c.broken.add(lose.key_url)
        c.products |= {win.product, lose.product}
        row = (win.product, win.metric, sig)
        best, losers = c.rows.get(row, (win, []))
        c.rows[row] = (win if _rank(win) > _rank(best) else best, losers + [lose])
    return list(found.values())


def describe(c: Found) -> dict:
    """topic / values / resolution / reason as text, plus the facts to cite."""
    main = max(c.products, key=lambda p: (len(product_words(p)), p))
    metrics = sorted({m for _, m, _ in c.rows})
    topic = " ".join(x for x in (_title(main, c.segment), c.term,
                                 "/".join({"apy": "APY", "apr": "APR"}.get(m, "rate") for m in metrics)) if x)
    winners = [w for w, _ in c.rows.values()]
    losers = [l for _, ls in c.rows.values() for l in ls]
    values = list(dict.fromkeys(_vtext(f) for f in winners + losers))
    # the winning value of every tier, labelled: "5.39% APR (2024-27 model year, 60 month)"
    resolution = "; ".join(dict.fromkeys(f"{_vtext(w)} ({w.label or ', '.join(w.quals)})"
                                         for w in sorted(winners, key=lambda f: (f.metric, f.quals))))
    w = max(winners, key=_rank)
    l = min(losers, key=_rank)
    if c.broken:
        reason = (f"{', '.join(sorted(c.broken))} shows a dividend rate above its own APY, which can't be "
                  f"right (APY is never below the rate); {w.key_url} is consistent")
    elif (w.effective or "") > (l.effective or ""):
        reason = (f"{w.key_url} is effective {w.effective}; {l.key_url} still shows older values "
                  f"(effective {l.effective or 'undated'})")
    else:
        reason = (f"same effective date; the {KIND_NAME.get(w.kind, w.kind)} ({w.key_url}) outranks "
                  f"the {KIND_NAME.get(l.kind, l.kind)} ({l.key_url})")
    cite = list({f.key_url: f for f in sorted(winners, key=_rank, reverse=True) + losers}.values())
    return {"topic": topic, "values": values, "resolution": resolution, "reason": reason, "cite": cite}


# ---------------------------------------------------------------- counts stated in text

COUNT_TOPICS = [("branch", "Number of Dupaco branch offices", "branches")]


def count_conflicts(index) -> list[dict]:
    """Articles keep old counts ("19 branch offices" in 2022, "23" in 2025). A number counts as
    a claim when at least 2 pages state it; the current one is the newest page among numbers
    stated on 3+ pages (the same rule answer() uses for "how many" questions)."""
    from search import count_pattern
    out = []
    for noun, topic, plural in COUNT_TOPICS:
        pat = re.compile(count_pattern(noun), re.I)
        pages = defaultdict(dict)                         # number -> page key -> chunk
        for c in index.chunks:
            for m in pat.finditer(c.text):
                n = m.group(1)
                if "," not in n and n != "0":             # "5,600 shared branches" is another fact
                    pages[n].setdefault(c.pv.key, c)
        claims = {n: v for n, v in pages.items() if len(v) >= 2}
        if len(claims) < 2:
            continue
        newest = lambda n: max(index.recency(c.pv) for c in claims[n].values())
        strong = [n for n in claims if len(claims[n]) >= 3] or list(claims)
        win = max(strong, key=lambda n: (newest(n), len(claims[n])))
        wpage = max(claims[win].values(), key=lambda c: index.recency(c.pv))
        others = sorted((n for n in claims if n != win), key=lambda n: -len(claims[n]))
        last_other = max((c.pv for n in others for c in claims[n].values()),
                         key=lambda pv: pv.published or "")
        out.append({
            "topic": topic,
            "values": [f"{n} {plural}" for n in [win] + others],
            "resolution": f"{win} {plural}",
            "reason": (f"{win} is stated on the newest page that gives a count ({wpage.pv.key}, "
                       f"{wpage.pv.published or 'official page'}); the other numbers come from older "
                       f"articles (latest {last_other.published or 'undated'})"),
            "cite": [wpage.pv] + [next(iter(claims[n].values())).pv for n in others],
        })
    return out
