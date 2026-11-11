"""Answering rate questions from extracted table facts.

    1. read the question  -> product words, tier details (months, years, model year, $ amount,
                             words like jumbo/special/refinance), which rate (APY / APR / dividend)
    2. find the product   -> score each product name by the words it shares with the question;
                             rare words count more ("share" says more than "savings")
    3. pick the table     -> among that product's tables with a row that fits the question:
                             newest effective date first, then the more trusted page
    4. read the row(s)    -> one fitting row = one answer; several = a short list (or the
                             highest / lowest if asked); none = "not listed, please contact us"

Nothing here knows any particular answer: it only knows how the site's tables are laid out.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------- question reading

# how members say it -> how the site says it
SYNONYMS = [
    (r"\bcds?\b|\bcertificates? of deposit\b|\bshare certificates?\b", " certificate "),
    (r"\bcars?\b|\bvehicles?\b|\btrucks?\b|\bsuvs?\b", " auto "),
    (r"\bmortgages?\b|\bhome purchase loans?\b", " home loan "),
    (r"\bcredit cards?\b", " visa "),
    (r"\bretirement accounts?\b|\biras?\b", " ira "),
    (r"\bboats?\b|\brvs?\b", " boat rv "),
    (r"\bhome equity line of credit\b", " heloc "),
]
STOP = set("""a an the of for on in at to and or is are was were be do does did you your i me my we our
us it its can could would will tell give get show what whats what's which who how much many any
there this that with about dupaco current currently now right today please know like want need
offer offered have has get rate rates apr apy dividend dividends interest yield annual percentage
account accounts""".split())
GENERIC = {"loan", "saving", "program", "standard", "regular"}      # matches alone don't identify a product
# words that change WHICH product/tier is meant; a row must contain every one the question uses
DESCRIPTORS = {"adjustable", "arm", "construction", "jumbo", "special", "roth", "traditional",
               "refinance", "variable", "fixed", "student", "introductory", "intro", "rewards",
               "platinum", "business"}
EXTRA_DESCRIPTORS = {"jumbo", "special", "refinance", "roth", "traditional", "introductory", "intro"}
RATE_INTENT = re.compile(r"\b(rates?|aprs?|apys?|dividends?|interest|yield|earn)\b|%", re.I)


def tokens(s: str) -> list[str]:
    s = s.lower().replace("’", "'")
    out = []
    for t in re.findall(r"\d+/\d+|[a-z0-9$']+", s):
        t = t.strip("'")
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]                                          # loans -> loan, iras -> ira
        out.append(t)
    return out


@dataclass
class Query:
    text: str
    words: set[str]
    segment: str                       # personal | business
    metric: Optional[str]              # apy | apr | rate | None (= show what the row has)
    months: set[int]
    years: set[int]                    # "30-year"
    model_year: Optional[int]
    amounts: list[float]
    descriptors: set[str]
    pick: Optional[str]                # "max" | "min" | None


def read_question(q: str) -> Query:
    low = q.lower()
    syn = low
    for pat, rep in SYNONYMS:
        syn = re.sub(pat, rep, syn)
    words = set(tokens(syn))
    metric = None
    if re.search(r"\bapys?\b|annual percentage yield|\byield\b", low):
        metric = "apy"
    elif re.search(r"\baprs?\b|annual percentage rate", low):
        metric = "apr"
    elif re.search(r"\bdividend rate\b", low):
        metric = "rate"
    months = {int(m) for m in re.findall(r"\b(\d{1,3})[- ]?(?:months?|mo)\b", low)}
    years = {int(y) for y in re.findall(r"\b(\d{1,2})[- ]?years?\b", low)}
    my = re.search(r"\b(19[89]\d|20[0-4]\d)\b(?![,\d])", low)
    model_year = int(my.group(1)) if my and re.search(r"model|auto|vehicle|car|truck|motorcycle", low) else None
    amounts = [float(a.replace(",", "")) * (1000 if k else 1)
               for a, k in re.findall(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(k\b)?", low)]
    desc = {d for d in DESCRIPTORS if re.search(rf"\b{d}\b", low)}
    desc |= set(re.findall(r"\b\d+/\d+\b", low))                  # "7/1" ARM
    pick = "max" if re.search(r"\b(highest|best|top|most|maximum)\b", low) else \
           "min" if re.search(r"\b(lowest|cheapest|minimum)\b", low) else None
    return Query(q, words, "business" if "business" in desc else "personal", metric, months,
                 years, model_year, amounts, desc, pick)


# ---------------------------------------------------------------- row matching

def _row_text(f) -> str:
    return f"{f.product} {' '.join(f.quals)} {f.label}".lower()


def _bracket(f) -> Optional[tuple[float, float]]:
    """Balance / loan-amount bracket of a row: '$10000-$24999.99' '$25000+' '$100000 or less'."""
    for q in f.quals:
        nums = [float(n.replace(",", "")) for n in re.findall(r"\$?(\d[\d,]*(?:\.\d+)?)", q)]
        if "$" not in q or not nums:
            continue
        if "or less" in q:
            return 0.0, nums[0]
        if q.rstrip().endswith("+"):
            return nums[0], float("inf")
        if len(nums) >= 2:
            return nums[0], nums[1]
    return None


def _model_years(f) -> Optional[tuple[int, int]]:
    for q in f.quals:
        if "model year" not in q:
            continue
        m = re.search(r"(\d{4})-(\d{2,4})", q)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if len(m.group(2)) == 4 else int(m.group(1)[:2] + m.group(2))
            return min(a, b), max(a, b)                     # "2024-27" and "2026-23" both work
        m = re.search(r"(\d{4}).*older", q)
        if m:
            return 0, int(m.group(1))
    return None


def row_fits(f, qy: Query) -> bool:
    text = _row_text(f)
    row_months = {int(m) for m in re.findall(r"(\d{1,3}) ?(?:month|mo\b)", text)}
    if qy.months and not (qy.months & row_months):
        return False
    row_years = {int(y) for y in re.findall(r"(\d{1,2})-year", text)}
    if qy.years and not (qy.years & row_years):
        return False
    if qy.model_year is not None:
        rng = _model_years(f)
        if rng and not (rng[0] <= qy.model_year <= rng[1]):
            return False
    if qy.amounts:
        br = _bracket(f)
        if br and not (br[0] <= min(qy.amounts) <= br[1]):
            return False
    words = set(tokens(text))
    for d in qy.descriptors - {"business"}:
        if d not in words and d not in text:
            return False
    return True


def _extras(f, qy: Query) -> int:
    """Tier words on the row the question didn't ask for (a 'jumbo' row for a plain question)."""
    return len({d for d in EXTRA_DESCRIPTORS if re.search(rf"\b{d}\b", _row_text(f))} - qy.descriptors)


# ---------------------------------------------------------------- answering

@dataclass
class RateAnswer:
    text: str
    sources: list = field(default_factory=list)     # facts whose pages back the answer
    note: Optional[str] = None
    confident: bool = True


def _pct(v: float) -> str:
    s = f"{v:.3f}".rstrip("0")
    return s + "0" * (2 - len(s.split(".")[1])) + "%"


def _value(f, deposit: bool) -> str:
    """'0.15% dividend rate' for savings, '7.13% interest rate' for loans, '5.39% APR'."""
    v = _pct(f.value) if f.value_hi is None else f"{_pct(f.value)}–{_pct(f.value_hi)}"
    name = {"apy": "APY", "apr": "APR"}.get(f.metric) or ("dividend rate" if deposit else "interest rate")
    return f"{v} {name}"


def answer_rate(question: str, facts: list) -> Optional[RateAnswer]:
    """None = not a rate question (let the text search try). Otherwise an answer or a refusal."""
    if not RATE_INTENT.search(question):
        return None
    qy = read_question(question)
    pool = [f for f in facts if f.segment == qy.segment] or facts

    # 2. product: shared words, rare words weigh more
    vocab = defaultdict(set)
    for f in pool:
        for w in set(tokens(f.product)) - STOP:
            vocab[w].add(f.product)
    qwords = qy.words - STOP
    def score(product: str) -> float:
        hit = (set(tokens(product)) - STOP) & qwords
        if not hit - GENERIC:
            return 0.0
        return sum(1.0 / len(vocab[w]) for w in hit)
    scores = {p: score(p) for p in {f.product for f in pool}}
    best = max(scores.values(), default=0.0)
    if best <= 0:
        return refusal()
    products = {p for p, s in scores.items() if s >= best - 1e-9}
    # a product named for a tier the question didn't ask for ("home loans - refinance") steps aside
    plain = {p for p in products if not ({d for d in EXTRA_DESCRIPTORS if d in p} - qy.descriptors)}
    products = plain or products

    # 3. rows that fit, grouped by table (page + product + effective date)
    fits = [f for f in pool if f.product in products and row_fits(f, qy)]
    if qy.metric:
        fits = [f for f in fits if f.metric == qy.metric] or fits
    if not fits:
        return refusal()
    tables = defaultdict(list)
    for f in fits:
        tables[(f.key_url, f.product, f.effective or "")].append(f)
    ranked = sorted(tables, key=lambda k: (k[2], max(f.tier for f in tables[k]), k[0], k[1]), reverse=True)
    top_url, _, top_eff = ranked[0]
    chosen = [k for k in ranked if k[0] == top_url and k[2] == top_eff]    # same page, same date

    # 4. rows -> lines
    rows = defaultdict(dict)                                  # (product, label) -> metric -> fact
    for k in chosen:
        for f in tables[k]:
            rows[(f.product, f.label)].setdefault(f.metric, f)
    if any(_extras(next(iter(m.values())), qy) == 0 for m in rows.values()):
        rows = {k: m for k, m in rows.items() if _extras(next(iter(m.values())), qy) == 0}
    order = sorted(rows, key=lambda k: (k[0], -_first_num(k[1]), k[1]))
    if qy.pick and len(order) > 1:
        main = lambda k: _main_fact(rows[k]).value
        order = [max(order, key=main) if qy.pick == "max" else min(order, key=main)]
    order = order[:14]

    lines = []
    for key in order:
        m = rows[key]
        shown = [m[qy.metric]] if qy.metric in m else [m[x] for x in ("rate", "apy", "apr") if x in m]
        label = f" ({key[1]})" if key[1] else ""
        deposit = "apy" in m or "apr" not in m
        lines.append(f"{_title(key[0], qy.segment)}{label}: " + ", ".join(_value(f, deposit) for f in shown))
    text = "\n".join(lines)

    used = [f for key in order for f in rows[key].values()]
    sources = _backing_pages(used, facts)
    stale = sorted({(f.key_url, _pct(f.value), f.effective or "no date") for k in ranked[1:] for f in tables[k]
                    if (f.product, f.label, f.metric) in {(u.product, u.label, u.metric) for u in used}
                    and f.value not in {u.value for u in used if u.metric == f.metric}})
    note = None
    if stale:
        note = "Ignored older/less authoritative values: " + "; ".join(f"{v} on {u} (effective {e})" for u, v, e in stale[:4])
    return RateAnswer(text, sources, note)


def refusal() -> RateAnswer:
    return RateAnswer("That isn't listed on our website; please contact us.", [], None, False)


def _main_fact(m: dict):
    return m.get("apy") or m.get("apr") or m.get("rate")


def _first_num(label: str) -> float:
    n = re.search(r"\d[\d,]*(?:\.\d+)?", label or "")
    return float(n.group().replace(",", "")) if n else 0.0


def _title(product: str, segment: str) -> str:
    t = " ".join(w if w in ("&", "-") else (w.upper() if w in ("ira", "iras", "heloc", "rv") else w.capitalize())
                 for w in product.split())
    return ("Business " + t) if segment == "business" and not t.lower().startswith("business") else t


def _backing_pages(used: list, facts: list) -> list:
    """Every page that shows the same values for the same rows and date, most trusted first (max 3)."""
    want = {(f.segment, f.product, f.label, f.metric, f.value, f.effective) for f in used}
    pages = {}
    for f in facts:
        if (f.segment, f.product, f.label, f.metric, f.value, f.effective) in want:
            pages.setdefault(f.key_url, f)
    ranked = sorted(pages.values(), key=lambda f: (-f.tier, f.key_url))
    return ranked[:3]
