# Notes

## API keys and requirements

**None.** No API keys, no network calls and no LLM. Everything runs offline on the Python standard
library (`requirements.txt` is intentionally empty). Developed on Python 3.13; the code only uses
syntax that parses on Python 3.9+.

```bash
python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
```

Public eval: **0.814** accuracy, consistency 1.0, speed 1.0 (p95 ≈ 0.01 s), ingest < 1 s.

## How it works

| File | Job |
|---|---|
| `store.py` | Keeps every page version per snapshot, cleans boilerplate, canonicalises URLs, labels each page's trust tier |
| `facts.py` | Turns rate tables into facts: product, segment (personal/business), tier, metric (rate/APY/APR), value, effective date, page |
| `rates.py` | Answers rate questions from those facts |
| `search.py` | Answers everything else: BM25 passage search plus answer-type and name checks |
| `conflicts.py` | Finds pages that disagree about the same fact and resolves them |

## How "current" is decided

1. **Which day.** `as_of` selects the newest snapshot taken on or before that date (each snapshot is the
   whole site on that day). With no `as_of`, the newest snapshot is used.
2. **Which page, for rates.** Among the tables that have a row matching the question's product and tier:
   - the **newest effective date** stated under the table wins;
   - on a tie, the **more trusted page** wins: the rate sheets (`/rates`, `/business-rates`) beat other
     official product pages, which beat calculators, articles, newsroom posts and PDFs (these often keep
     old numbers).
3. **Which tier.** Rows are matched on every qualifier in the question: term, model year, balance or loan
   amount, rate vs APY vs APR, personal vs business, and words like jumbo/special/refinance. A different
   tier is a different fact, never a conflict.
4. **Text answers.** BM25 relevance × page trust, with recency as a small tie-breaker. For facts that change
   over time (opening hours, counts such as the number of branches), the newest relevant source wins; a
   count must also be stated on at least 3 pages, which filters out one-off numbers.
5. **Honesty.** A passage must contain the kind of answer asked for (a time, an address, a date, a number)
   and every specific name in the question. If nothing qualifies, the system says it isn't listed and
   returns `confident=False` instead of guessing.

## Conflicts

`conflicts()` compares, within each snapshot, table rows from different pages that describe the same fact
(same segment, metric and tier; model-year buckets compared by position, since the site shifts the years;
product names where one contains the other). Different values on the same day are a conflict, resolved with
the same rule as above. One extra check: a deposit row whose dividend rate is above its own APY cannot be
right and loses. One conflict is reported per product and term, using the newest snapshot where it appears.
Branch counts repeated across articles are reported as a text conflict. A rate that simply changed between
snapshots is treated as an update, not a contradiction.

## Known limitations

- Rules and thresholds were tuned on the public eval; reworded questions were tested by hand, but the hidden
  set may phrase things differently.
- No synonyms beyond a small hand-made list (e.g. CD → certificate, car → auto).
- Some answers need sentence-level reasoning BM25 can't do (e.g. "fifth" in an award article, eligibility
  spread across a page).
- One checking table loses its product name, so its conflict topic reads "Balance".
