"""Knowledge system entry point: build() -> KnowledgeSystem (see harness/contract.py).

Status: ingest keeps full page history, cleans boilerplate, labels page authority and extracts
rate-table facts. answer() uses those facts for rate questions (rates.py) and BM25 passage
search with answer-type checks for everything else (search.py); conflicts() comes next.
"""
from __future__ import annotations

import re
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(next(d / "harness" for d in HERE.parents if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Snapshot, Source  # noqa: E402

from conflicts import count_conflicts, describe, find_conflicts  # noqa: E402
from facts import Fact, facts_from_page  # noqa: E402
from rates import answer_rate, rate_link  # noqa: E402
from search import Chunk, Index, answer_text, chunk_page, is_link_question  # noqa: E402
from store import Store  # noqa: E402

LISTING_PAGE = re.compile(r"/(tag|author|category)/")


class Knowledge:
    def __init__(self):
        self.store = Store()
        self.facts: dict[tuple, list[Fact]] = {}   # (canonical url, content hash) -> facts, parsed once
        self._facts_cache: dict[str, list[Fact]] = {}   # snapshot id -> facts visible in that view
        self._chunk_cache: dict[tuple, list[Chunk]] = {}  # (canonical url, content hash) -> passages
        self._index_cache: dict[str, Index] = {}          # snapshot id -> BM25 index

    # ------------------------------------------------------------ ingest
    def ingest(self, snapshot: Snapshot) -> None:
        view = self.store.add_snapshot(snapshot)
        for pv in view.pages.values():
            if (pv.key, pv.hash) not in self.facts:
                self.facts[(pv.key, pv.hash)] = facts_from_page(self.store.text(pv), pv)

    def view_facts(self, as_of=None) -> list[Fact]:
        """All facts visible on the given date, tagged with the page version that carried them."""
        view = self.store.view(as_of)
        out = []
        for pv in view.pages.values():
            for f in self.facts.get((pv.key, pv.hash), []):
                # facts are cached per content hash; re-tag with this view's page version
                out.append(f if f.snapshot_id == pv.snapshot_id else
                           replace(f, snapshot_id=pv.snapshot_id, taken_on=pv.taken_on))
        return out

    # ------------------------------------------------------------ answer
    def answer(self, question, as_of=None):
        view = self.store.view(as_of)

        # 1) rate questions: answer from table facts (or refuse when no table row fits)
        if view.snapshot_id not in self._facts_cache:
            self._facts_cache[view.snapshot_id] = self.view_facts(as_of)
        facts = self._facts_cache[view.snapshot_id]
        # "what link shows the X rates?" wants a page, not the numbers
        ra = rate_link(question, facts) if is_link_question(question) else answer_rate(question, facts)
        if ra is not None:
            return Answer(text=ra.text, confident=ra.confident, conflict_note=ra.note,
                          sources=[Source(url=f.url, snapshot_id=view.snapshot_id, quote=f.row[:200])
                                   for f in ra.sources])

        # 2) everything else: BM25 search with answer-type and name checks (search.py)
        ta = answer_text(question, self.index(view))
        sources = [Source(url=pv.url, snapshot_id=view.snapshot_id) for pv in ((ta.pv,) + ta.also if ta.pv else ())]
        return Answer(text=ta.text, sources=sources, confident=ta.confident)

    def index(self, view) -> Index:
        """BM25 index over the passages of one snapshot view, built on first use and cached."""
        if view.snapshot_id not in self._index_cache:
            chunks = []
            for pv in sorted(view.pages.values(), key=lambda p: p.key):
                if LISTING_PAGE.search(pv.key):
                    continue                        # tag/author listings: teasers of other pages
                ck = (pv.key, pv.hash)
                if ck not in self._chunk_cache:
                    self._chunk_cache[ck] = chunk_page(self.store.text(pv), pv)
                chunks += [c if c.pv is pv else Chunk(c.text, c.terms, pv) for c in self._chunk_cache[ck]]
            self._index_cache[view.snapshot_id] = Index(chunks, view.taken_on)
        return self._index_cache[view.snapshot_id]

    def conflicts(self) -> list[Conflict]:
        """Every place two pages disagree on the same fact, with the value we believe and why."""
        views = [(v.snapshot_id, self.view_facts(v.taken_on)) for v in self.store.snapshots]
        found = [describe(c) for c in find_conflicts(views)]
        found += count_conflicts(self.index(self.store.view()))
        out = []
        for d in found:
            sources = [Source(url=x.url, snapshot_id=x.snapshot_id, quote=getattr(x, "row", "")[:200] or None)
                       for x in d["cite"]]
            out.append(Conflict(topic=d["topic"], values=d["values"], sources=sources,
                                resolution=d["resolution"], reason=d["reason"]))
        return sorted(out, key=lambda c: c.topic)


def build():
    return Knowledge()
