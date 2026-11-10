"""Knowledge system entry point: build() -> KnowledgeSystem (see harness/contract.py).

Status: ingest keeps full page history, cleans boilerplate, labels page authority and extracts
rate-table facts. answer() is still a simple keyword fallback over the requested date's view;
fact-based answering and conflicts() come next.
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

from facts import Fact, facts_from_page  # noqa: E402
from store import Store  # noqa: E402

WORD = re.compile(r"[a-z0-9$%.]+")


class Knowledge:
    def __init__(self):
        self.store = Store()
        self.facts: dict[tuple, list[Fact]] = {}   # (canonical url, content hash) -> facts, parsed once

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

    # ------------------------------------------------------------ answer (placeholder)
    def answer(self, question, as_of=None):
        view = self.store.view(as_of)
        q = set(WORD.findall(question.lower()))
        best, best_score = None, 0
        for pv in sorted(view.pages.values(), key=lambda p: (-p.tier, p.key)):
            for para in self.store.text(pv).split("\n\n"):
                s = len(q & set(WORD.findall(para.lower())))
                if s > best_score:
                    best, best_score = (pv, para.strip()), s
        if not best:
            return Answer(text="That isn't listed on our website; please contact us.", confident=False)
        pv, para = best
        return Answer(text=para[:600], sources=[Source(url=pv.url, snapshot_id=pv.snapshot_id)])

    def conflicts(self):
        return []


def build():
    return Knowledge()
