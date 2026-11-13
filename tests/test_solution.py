"""Behaviour tests on a tiny made-up credit-union site (no snapshot data needed).

Run from the repo root:  python -m unittest discover -s tests
Standard library only; the optional LLM layer stays off unless a test turns it on on purpose.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "harness"), str(ROOT / "solution")]
os.environ.pop("KNOWLEDGE_LLM", None)

from contract import Page, Snapshot  # noqa: E402

import llm  # noqa: E402
import solution  # noqa: E402
from store import canon_url, page_kind  # noqa: E402

SITE = "https://www.example.org"

RATES = """# Rates
## Auto & Motorcycle Loans
| Model Year | Term | APR |
| --- | --- | --- |
| 2024-27 Model Year | 48 Months | {a48} |
| 2024-27 Model Year | 60 Months | {a60} |

Rates are effective as of {eff}.

## Share Savings
| Balance | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- |
| $0.00 - $99.99 | 0.00% | 0.00% |
| $25,000+ | 0.20% | 0.20% |

Rates are effective as of {eff}.
"""

# a calculator nobody updated: older effective date, older model-year labels
CALCULATOR = """# Vehicle affordability calculator
## Auto Loan Rates
| Model Year | Term | APR |
| --- | --- | --- |
| 2023-26 Model Year | 60 Months | 5.34% |

Rates are effective as of 01/01/2026.
"""

# the same business savings table on two pages; one row of the rate sheet has rate > APY (a typo)
BUSINESS_SAVINGS = """# Business savings
## Business Share Savings
| Balance | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- |
| $10,000 - $24,999.99 | {rate} | 0.15% |
| $25,000+ | 0.20% | 0.20% |

Rates are effective as of 01/06/2026.
"""

BRANCH_NEWS = """# Dupaco opens a new branch in {town}
{date}

The new {town} branch is now open. Branch hours are:

Lobby
Monday-Friday: 8:30 a.m.-5:30 p.m.
Saturday: {saturday}
Sunday: Closed
"""


def page(path: str, title: str, markdown: str, day: str) -> Page:
    return Page(url=SITE + path, title=title, markdown=markdown, fetched_at=day + "T10:00:00Z")


def snapshot(sid: str, day: str, a48: str, a60: str, eff: str) -> Snapshot:
    return Snapshot(sid, day, [
        page("/rates", "Rates", RATES.format(a48=a48, a60=a60, eff=eff), day),
        page("/calculators/vehicle-affordability", "Calculator", CALCULATOR, day),
        page("/business-rates", "Business rates", BUSINESS_SAVINGS.format(rate="0.20%"), day),
        page("/business/savings-accounts", "Business savings", BUSINESS_SAVINGS.format(rate="0.15%"), day),
        page("/newsroom/branch-a", "Branch A opens",
             BRANCH_NEWS.format(town="Avon", date="April 26, 2024", saturday="8:30 a.m.-12:30 p.m."), day),
        page("/newsroom/branch-b", "Branch B opens",
             BRANCH_NEWS.format(town="Bly", date="July 23, 2021", saturday="9 a.m.-noon"), day),
    ])


def build_system():
    ks = solution.build()
    ks.ingest(snapshot("01_2026-05-12_baseline", "2026-05-12", "5.09%", "5.19%", "05/01/2026"))
    ks.ingest(snapshot("02_2026-09-28_latest", "2026-09-28", "5.29%", "5.39%", "08/31/2026"))
    return ks


AUTO_Q = "What's the APR for a 60-month auto loan on a 2025 model-year vehicle?"


class CurrentValue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ks = build_system()

    def test_rate_sheet_beats_stale_calculator(self):
        a = self.ks.answer(AUTO_Q)
        self.assertIn("5.39%", a.text)
        self.assertNotIn("5.34%", a.text)          # the calculator's old value must not leak in
        self.assertTrue(a.confident)
        self.assertIn("example.org/rates", a.sources[0].url)

    def test_as_of_answers_from_that_day(self):
        a = self.ks.answer(AUTO_Q, as_of="2026-06-01")
        self.assertIn("5.19%", a.text)
        self.assertNotIn("5.39%", a.text)

    def test_right_tier(self):
        a = self.ks.answer("What's the 48-month auto loan APR for a 2025 model year?")
        self.assertIn("5.29%", a.text)
        self.assertNotIn("5.39%", a.text)

    def test_unlisted_product_is_refused_without_numbers(self):
        a = self.ks.answer("What is the 60-month jumbo certificate rate?")
        self.assertFalse(a.confident)
        self.assertEqual(re.findall(r"\d", a.text), [])

    def test_same_question_same_answer(self):
        self.assertEqual(self.ks.answer(AUTO_Q).text, self.ks.answer(AUTO_Q).text)


class Conflicts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.found = build_system().conflicts()

    def find(self, word: str):
        return [c for c in self.found if word in c.topic.lower()]

    def test_stale_calculator_is_a_conflict_resolved_to_rate_sheet(self):
        auto = self.find("auto")
        self.assertEqual(len(auto), 1, [c.topic for c in self.found])
        self.assertIn("5.34% APR", auto[0].values)
        self.assertTrue(auto[0].resolution.startswith("5.39% APR"))

    def test_different_terms_are_not_conflicts(self):
        # 48-month 5.29% vs 60-month 5.39% on the same page is two facts, not a contradiction
        for c in self.found:
            self.assertFalse({"5.29% APR", "5.39% APR"} <= set(c.values), c)

    def test_row_with_rate_above_apy_loses(self):
        biz = self.find("share savings")
        self.assertEqual(len(biz), 1, [c.topic for c in self.found])
        self.assertTrue(biz[0].resolution.startswith("0.15%"))   # not the rate sheet's 0.20% typo
        self.assertIn("above its own APY", biz[0].reason)


class TextAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ks = build_system()

    def test_hours_comparison_lists_each_schedule(self):
        a = self.ks.answer("Do all branches keep the same lobby hours?")
        self.assertTrue(a.text.startswith("Hours are not the same at every branch"), a.text)
        self.assertIn("12:30", a.text)
        self.assertIn("noon", a.text)

    def test_plain_hours_question_is_not_a_comparison(self):
        a = self.ks.answer("What are the lobby hours?")
        self.assertNotIn("not the same", a.text)


class OptionalLLM(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("KNOWLEDGE_LLM", None)
        os.environ.pop("GEMINI_API_KEY", None)

    def test_off_by_default_even_with_a_key(self):
        os.environ["GEMINI_API_KEY"] = "placeholder"
        self.assertIsNone(llm.provider())

    def test_rate_questions_never_reach_the_llm(self):
        ks = build_system()
        original = llm.pick
        llm.pick = lambda *a, **k: self.fail("rate question was sent to the LLM")
        try:
            os.environ["KNOWLEDGE_LLM"] = "on"
            os.environ["GEMINI_API_KEY"] = "placeholder"
            self.assertIn("5.39%", ks.answer(AUTO_Q).text)
        finally:
            llm.pick = original


class Urls(unittest.TestCase):
    def test_tracking_params_and_fragments_are_the_same_page(self):
        self.assertEqual(canon_url("https://www.Example.org/Rates/?hsLang=en#auto"), "example.org/rates")

    def test_real_query_params_are_different_pages(self):
        self.assertNotEqual(canon_url("example.org/learn?category=Budgeting"), canon_url("example.org/learn"))

    def test_page_trust(self):
        self.assertEqual(page_kind("https://example.org/rates"), ("rate_sheet", 2))
        self.assertEqual(page_kind("https://example.org/calculators/x"), ("calculator", 0))
        self.assertEqual(page_kind("https://example.org/personal/loans/auto"), ("official", 1))


if __name__ == "__main__":
    unittest.main()
