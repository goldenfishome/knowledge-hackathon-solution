"""Text answers for non-rate questions: BM25 search + answer-type checks + name checks.

    1. chunk     split each cleaned page into passages (short lines like an hours block stay together)
    2. search    BM25 relevance (rare words count more), times page trust and recency
    3. check     the passage must contain what the question asks for:
                   hours -> a time, address -> a street/ZIP, when -> a date, how many -> a number, ...
                 and every specific name in the question ("Galena", "Mullan Avenue")
    4. answer    the 1-3 best lines of the winning passage; nothing passes the checks -> refuse

For facts that change over time (counts, rankings), the newest source among the relevant ones wins,
the same rule rates.py uses for tables.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Optional

import llm

STOP = set("""a an the of for on in at to and or is are was were be been do does did you your i me my we our
us it its can could would will should shall may might tell give get show what whats what's which who whom
whose how when where why this that these those with about from by as any there here have has had dupaco
please know like want need still today just also than then them they their""".split())
NOT_NAMES = {"dupaco", "i", "what", "when", "where", "who", "how", "is", "are", "do", "does", "can", "the",
             "branch", "branches", "credit", "union", "cd", "cds", "apr", "apy", "ira", "visa", "october",
             "november", "december", "january", "february", "march", "april", "may", "june", "july",
             "august", "september", "monday", "friday", "saturday", "sunday"}

MONTHS = r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
TIME = r"\b\d{1,2}(?::\d{2})?\s?(?:a\.?m\.?|p\.?m\.?)|\bnoon\b"
# what kind of answer the question wants -> (question pattern, pattern the answer must contain)
ANSWER_TYPES = [
    ("hours",   r"\bhours?\b|\bwhat time\b|\bopen (?:on|at|until)\b", TIME),
    ("address", r"\baddress\b|\bwhere is\b|\blocated\b",    r"\b\d{2,6}\s+[a-z0-9.]+.*\b\d{5}\b"),
    ("routing", r"\brouting\b",                             r"\b\d{9}\b"),
    ("phone",   r"\bphone\b|\bnumber to call\b",            r"\d{3}[-.\s]\d{3}[-.\s]\d{4}"),
    ("count",   r"\bhow many\b",                            r"\b\d+\b"),
    ("date",    r"^\s*when\b|\bwhat date\b",                rf"\b{MONTHS}\s+\d{{1,2}}(?:,\s*\d{{4}})?|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}"),
    ("link",    r"\blink\b|\burl\b|\bwebsite\b|\bweb address\b", r"[a-z0-9.-]+\.(?:com|org)\b"),
]
# facts that change over time: the newest relevant source wins over the most relevant one
RECENCY_FIRST = {"count", "hours"}
# "Do all branches keep the same hours?" asks for a comparison, not one schedule
COMPARE = re.compile(r"\b(?:all|every|each|same|vary|varies|differ|different)\b")
LLM_PASSAGES = 10         # passages the optional LLM picker reads (llm.py)
REFUSAL = "That isn't listed on our website; please contact us."


def words(s: str) -> list[str]:
    out = []
    s = re.sub(r"['’]s\b", "", s.lower())                   # possessives: "Dupaco's" -> "dupaco"
    for t in re.findall(r"[a-z0-9$]+(?:[-'][a-z0-9]+)*", s):
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        if t not in STOP:
            out.append(stem(t))
    return out


def stem(t: str) -> str:
    """Crude stem so 'recognition' ~ 'recognized' and 'employees' ~ 'employee': keep the first 6 letters."""
    return t[:6] if len(t) > 6 and t.isalpha() else t


# ---------------------------------------------------------------- 1. chunking

@dataclass
class Chunk:
    text: str          # display text (links simplified)
    terms: Counter     # index terms (passage + page title)
    pv: object         # PageVersion it came from


_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _display(s: str) -> str:
    s = _IMAGE.sub("", s)
    s = _LINK.sub(lambda m: f"{m.group(1)} ({re.sub(r'[?#].*$', '', m.group(2))})" if m.group(1) else "", s)
    s = re.sub(r"^#+\s*", "", s.replace("**", "").replace("\\", ""))
    return re.sub(r"[ \t]+", " ", s).strip()


def _junk(par: str) -> bool:
    return par.count("|") > 8 and ("Col" in par or par.count("|") > len(par) / 12)


def chunk_page(text: str, pv) -> list[Chunk]:
    """Paragraphs; runs of short lines (hours, addresses, lists) are kept together, up to ~600 chars."""
    pars = [p.strip() for p in re.split(r"\n\s*\n", text)
            if p.strip() and not re.search(r"/(?:author|tag)/", p)]          # bylines, tag links
    groups, cur = [], []
    for p in pars:
        if _junk(p) or p.startswith("![") or set(p) <= set("-|: "):
            continue
        short = len(p) < 140 and "\n" not in p
        after_heading = bool(cur) and cur[-1].startswith("#")      # a heading never stands alone
        if after_heading or (cur and short and sum(len(x) for x in cur) < 600
                             and (len(cur[-1]) < 140 or cur[-1].endswith(":"))):
            cur.append(p)
        else:
            if cur:
                groups.append(cur)
            cur = [p]
    if cur:
        groups.append(cur)
    title_terms = words(pv.title or "")
    out = []
    for g in groups:
        disp = "\n".join(_display(x) for x in g).strip()
        if len(disp) < 12:
            continue
        out.append(Chunk(disp, Counter(words(disp) + title_terms), pv))
    return out


# ---------------------------------------------------------------- 2. BM25 index for one snapshot view

class Index:
    K1, B = 1.4, 0.75

    def __init__(self, chunks: list[Chunk], today: str):
        self.chunks = chunks
        self.today = today
        self.post = defaultdict(list)                # term -> [(chunk id, tf)]
        for i, c in enumerate(chunks):
            for t, tf in c.terms.items():
                self.post[t].append((i, tf))
        self.lens = [sum(c.terms.values()) for c in chunks]
        self.avg = sum(self.lens) / max(1, len(self.lens))
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.post.items()}

    def bm25(self, q_terms: list[str]) -> dict[int, float]:
        scores = defaultdict(float)
        for t in set(q_terms):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, tf in self.post[t]:
                norm = tf + self.K1 * (1 - self.B + self.B * self.lens[i] / self.avg)
                scores[i] += idf * tf * (self.K1 + 1) / norm
        return scores

    def recency(self, pv) -> float:
        """0..1 — official pages count as current; articles by publish date; undated low pages = 0."""
        if pv.tier >= 1:
            return 1.0
        if not pv.published:
            return 0.0
        days = (date.fromisoformat(self.today) - date.fromisoformat(pv.published)).days
        return max(0.0, 1.0 - days / (8 * 365))


# ---------------------------------------------------------------- 3 + 4. checks and answer

def question_names(q: str) -> list[str]:
    """Specific names in the question: capitalised words not at the start, or words like Hy-Vee / Home$tart."""
    names = []
    toks = re.findall(r"[A-Za-z0-9$][A-Za-z0-9$'’-]*", q)
    for k, t in enumerate(toks):
        bare = t.lower().strip("'’").removesuffix("'s").removesuffix("’s")
        if bare in NOT_NAMES or len(bare) < 3 or (t.isupper() and len(t) <= 5):
            continue                                       # "URL", "ATM", "FHLB" describe, they don't name
        if (k > 0 and t[0].isupper()) or "$" in t or re.search(r"[a-z]-[A-Z]", t):
            names.append(bare)
    return names


@dataclass
class TextAnswer:
    text: str
    pv: Optional[object]
    confident: bool
    also: tuple = ()          # other pages that state the same value (routing number, phone, ...)


def is_link_question(q: str) -> bool:
    """'What link/URL should I use…' wants a page, even if it mentions rates."""
    return bool(re.search(ANSWER_TYPES[-1][1], q.lower()))


def count_pattern(noun: str) -> str:
    """'22 branch office locations', '294 Dupaco employees' — not '20 outside of … branch' or a year."""
    between = r"(?:(?!of\b|in\b|at\b|for\b|outside\b|every\b|the\b)[a-z-]+\s+){0,2}"
    return rf"\b(?!(?:19|20)\d\d\b)(\d[\d,]*)\+?\s+{between}{re.escape(noun)}"


def answer_text(question: str, index: Index) -> TextAnswer:
    q_terms = words(question)
    low = question.lower()
    kind = next(((name, ans) for name, qpat, ans in ANSWER_TYPES if re.search(qpat, low)), None)
    if kind and kind[0] == "count":
        after = re.search(r"\bhow many\s+(.*)", low)
        noun = next((w for w in words(after.group(1))), None) if after else None   # skips "dupaco", "the"
        if noun:
            kind = ("count", count_pattern(noun))
    names = question_names(question)

    scores = index.bm25(q_terms)
    if not scores:
        return TextAnswer(REFUSAL, None, False)
    top = max(scores.values())
    cands = []
    for i, s in scores.items():
        if s < 0.35 * top and not (kind and kind[0] == "count"):
            continue
        c = index.chunks[i]
        blob = (c.text + " " + (c.pv.title or "")).lower()
        if any(n not in blob for n in names):
            continue                                            # must mention every specific name
        if kind and not re.search(kind[1], c.text, re.I):
            continue                                            # must contain the kind of answer asked for
        rec = index.recency(c.pv)
        trust = 1.15 if c.pv.tier >= 1 else 1.0
        only_questions = 0.6 if _only_questions(c.text) else 1.0     # an FAQ question without its answer
        # recency decides counts/hours (RECENCY_FIRST below); elsewhere it only nudges ties
        cands.append((s * trust * only_questions * (0.9 + 0.2 * rec), rec, s, c, i))
    if not cands:
        return TextAnswer(REFUSAL, None, False)

    if kind and kind[0] == "count":
        return _count_answer(cands, q_terms, kind)
    if kind and kind[0] == "hours" and COMPARE.search(low):
        compared = _compare_hours(cands, q_terms, kind, names)
        if compared:
            return compared
    if kind and kind[0] in RECENCY_FIRST:
        best_rel = max(x[2] for x in cands)
        pool = [x for x in cands if x[2] >= 0.6 * best_rel]
        pick = max(pool, key=lambda x: (x[1], x[2], x[3].pv.key))
    else:
        pick = max(cands, key=lambda x: (x[0], x[1], x[3].pv.key))
    c = pick[3]
    if not kind and llm.enabled():
        # optional: Claude picks which of the top passages' sentences answer the question
        # it reads the top BM25 passages that name every specific name, before the 35% cut-off,
        # so an answer split over two short passages ("who can join" + "Iowa, Illinois…") reaches it
        ranked = sorted(scores, key=lambda i: (-scores[i], i))
        top = [index.chunks[i] for i in ranked
               if all(n in (index.chunks[i].text + " " + (index.chunks[i].pv.title or "")).lower() for n in names)
               ][:LLM_PASSAGES]
        chosen = llm.pick(question, [t.text for t in top])
        if chosen:
            src = next((t for t in top if chosen[0] in " ".join(t.text.split("\n"))), c)
            return TextAnswer(" ".join(chosen)[:700], src.pv, True)
    if re.match(r"\s*what (?:is|are)\b", low) and names and not kind:
        # "What is X?": the first passage on that page that mentions X is usually the definition
        same = [x for x in cands if x[3].pv is c.pv]
        c = min(same, key=lambda x: x[4])[3]
        sents = re.split(r"(?<=[.!?])\s+(?=[A-Z])", " ".join(c.text.split("\n")))
        return TextAnswer(" ".join(sents[:3])[:600], c.pv, True)      # a definition = the opening sentences
    text = _best_lines(c.text, q_terms, kind, names)
    return TextAnswer(text, c.pv, True, _backing(text, kind, c.pv, scores, index))


def _backing(text: str, kind, pv, scores, index) -> tuple:
    """Pages that state the exact same value (a routing number, phone, address), most trusted and
    most relevant first, so the answer cites the official page too, not only the one it read."""
    if not kind or kind[0] not in ("routing", "phone", "address"):
        return ()
    m = re.search(kind[1], text, re.I)
    if not m:
        return ()
    value = m.group(0)
    best = {}
    for i, sc in scores.items():
        c = index.chunks[i]
        if c.pv.key != pv.key and value in c.text and sc > best.get(c.pv.key, (None, -1))[1]:
            best[c.pv.key] = (c.pv, sc)
    ranked = sorted(best.values(), key=lambda x: (-x[0].tier, -x[1], x[0].key))
    return tuple(p for p, _ in ranked[:2])


def _count_answer(cands, q_terms, kind) -> TextAnswer:
    """'How many X?': many pages state a count, often old ones. Keep numbers the site states on at
    least 3 pages (its own repeated claim, not a one-off like '5,600 shared branches nationwide'),
    then take the number from the newest page that states it."""
    pages = defaultdict(dict)                       # number -> page key -> (recency, candidate)
    for cand in cands:
        for m in re.finditer(kind[1], cand[3].text, re.I):
            n = m.group(1).replace(",", "")
            old = pages[n].get(cand[3].pv.key)
            if old is None or cand[1] > old[0]:
                pages[n][cand[3].pv.key] = (cand[1], cand)
    repeated = {n: v for n, v in pages.items() if len(v) >= 3} or pages
    def newest(n):
        rec, cand = max(repeated[n].values(), key=lambda x: (x[0], x[1][2], x[1][3].pv.key))
        return rec, cand
    n = max(repeated, key=lambda n: (newest(n)[0], len(repeated[n]), n))
    c = newest(n)[1][3]
    return TextAnswer(_best_lines(c.text, q_terms, kind), c.pv, True)


def _compare_hours(cands, q_terms, kind, names) -> Optional[TextAnswer]:
    """Different schedules stated on different pages -> 'not the same everywhere', with each one.
    One schedule everywhere -> None (answer normally)."""
    seen = {}
    for cand in sorted(cands, key=lambda x: (-x[1], -x[2], x[3].pv.key)):   # newest first
        block = _best_lines(cand[3].text, q_terms, kind, names)
        lobby = [l for l in block.split("\n") if re.search(kind[1], l, re.I)]
        if not lobby:
            continue
        key = tuple(re.sub(r"[\s.]", "", t.lower()) for t in re.findall(kind[1], "\n".join(lobby), re.I))
        seen.setdefault(key, (block, cand[3].pv))
    if len(seen) < 2:
        return None
    shown = list(seen.values())[:3]
    text = ("Hours are not the same at every branch. Schedules on the site include:\n\n"
            + "\n\n".join(b for b, _ in shown) + "\n\nAsk which branch for its exact hours.")
    return TextAnswer(text, shown[0][1], True, tuple(pv for _, pv in shown[1:]))


def _only_questions(text: str) -> bool:
    body = [l for l in text.split("\n") if l.strip() and not re.match(r"^[-*]?\s*[A-Z][^.!?]{0,60}$", l.strip())]
    return bool(body) and all(l.rstrip().rstrip(")").split("(")[0].rstrip().endswith("?") for l in body)


def _best_lines(text: str, q_terms: list[str], kind, names=()) -> str:
    """The 1-3 lines/sentences of the passage that best answer the question."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if kind and kind[0] in ("address", "phone", "routing") and names:
        # a list of branches: only the line with the asked-for name and the address/number
        hit = [l for l in lines if re.search(kind[1], l, re.I) and all(n in l.lower() for n in names)]
        if hit:
            return hit[0]
    if kind and kind[0] == "hours":                          # an hours block: keep every line with a time
        keep = [l for l in lines if re.search(kind[1], l, re.I)
                or re.search(r"closed|atm only|^(lobby|drive[- ]?up)\b", l, re.I)]
        return "\n".join(keep[:10])
    sents = []
    for l in lines:
        sents += [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", l) if s.strip()]
    qs = set(q_terms)
    def score(s):
        w = set(words(s))
        sc = len(w & qs) + (2 if kind and re.search(kind[1], s, re.I) else 0)
        if s.rstrip().endswith("?"):
            sc -= 1.5                                    # FAQ pages repeat the question first
        if re.search(r"\bdupaco\b", s, re.I):
            sc += 0.5                                    # questions are about Dupaco itself
        return sc
    ranked = sorted(range(len(sents)), key=lambda k: (-score(sents[k]), k))
    keep = sorted(ranked[:2])
    out = " ".join(sents[k] for k in keep)
    return out[:500]
