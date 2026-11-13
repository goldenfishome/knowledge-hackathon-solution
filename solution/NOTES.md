# Notes

## API keys and requirements

**No API key is required.** The core system makes no network calls and uses only the Python standard
library. Tested on Python 3.11 and 3.13.

```bash
python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
```

Public eval without a key: **0.864** accuracy, consistency 1.0, speed 1.0 (p95 ≈ 0.01 s), ingest < 1 s.

**Optional, off by default:** `KNOWLEDGE_LLM=on` plus `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` (and
`pip install -r solution/requirements.txt`) turns on the LLM sentence picker described below. Defaults:
`claude-opus-5-5` at low effort, or `gemini-4-hydrogen`. `KNOWLEDGE_LLM_PROVIDER=anthropic|gemini` picks
one when both keys are set; `KNOWLEDGE_LLM_MODEL` overrides the model. A key alone does not turn it on.

```bash
pip install -r solution/requirements.txt          # SDKs for the optional layer only
export GEMINI_API_KEY=...                         # or ANTHROPIC_API_KEY=...
KNOWLEDGE_LLM=on python harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
```

If the layer is really on, the report's p95 latency is several seconds; at ≈ 0.01 s it fell back to rules.

## How it works

| File | Job |
|---|---|
| `store.py` | Keeps every page version per snapshot, cleans boilerplate, canonicalises URLs, labels each page's trust tier |
| `facts.py` | Turns rate tables into facts: product, segment (personal/business), tier, metric (rate/APY/APR), value, effective date, page |
| `rates.py` | Answers rate questions from those facts |
| `search.py` | Answers everything else: BM25 passage search plus answer-type and name checks |
| `conflicts.py` | Finds pages that disagree about the same fact and resolves them |
| `llm.py` | Optional: lets an LLM (Claude or Gemini) choose which retrieved sentences answer a text question |

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
   A generic word alone ("savings", "loan") gets the menu of products the **rate sheet** lists
   now, each with its current range; other pages may keep retired product names with old rates.
   "Does/Do any … pay X%?" is answered Yes/No first, then with the rows it was checked against.
4. **Text answers.** BM25 relevance × page trust, with recency as a small tie-breaker. For facts that change
   over time (opening hours, counts such as the number of branches), the newest relevant source wins; a
   count must also be stated on at least 3 pages, which filters out one-off numbers. "Do all branches keep
   the same hours?" compares instead: if pages state different schedules, it says so and lists them.
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

## Optional LLM layer: where it is allowed, and where it isn't

- **Never for rates.** Numbers come only from the rate tables and the "current" rule above.
- **Only for open text questions** (no expected answer type such as a time, address or number). BM25
  retrieves the top passages; the model returns only the **numbers of the sentences** that answer the
  question (structured JSON output). The answer is the site's own sentences, so it can't invent a
  value or a stale number that isn't already in the retrieved text.
- **Fails safe.** No SDK, no key, a timeout, a refusal or malformed output all fall back to the
  rule-based answer. Picks are cached per question, so asking twice gives the same answer.
- Built for the misses where the right page was found but the wrong sentence was chosen
  (e.g. "This was the fifth time Forbes recognized Dupaco."). It does not fix retrieval misses.
- **Measured, then left off.** Public eval with Gemini (`gemini-4-hydrogen`) vs without (measured before
  the hours-comparison rule, at 0.849):

  | | Accuracy | Consistency | p95 latency | Speed score |
  |---|---|---|---|---|
  | Rules only (default) | 0.849 | 1.0 | 0.01 s | 1.0 |
  | Rules + Gemini picker | 0.849 | 1.0 | 5.5 s | 0.805 |

  It was called on 9 of 40 questions; it reworded 6 answers without changing any score. On the Forbes
  question it chose a different, also-true Forbes sentence ("ranking third in Iowa") instead of "fifth":
  the question has several valid answers and nothing tells the model that the newest count is wanted.
  No gain plus a 5-second wait, so it stays opt-in.

## Known limitations

- Rules and thresholds were tuned on the public eval; reworded questions were tested by hand, but the hidden
  set may phrase things differently.
- No synonyms beyond a small hand-made list (e.g. CD → certificate, car → auto).
- Some answers need sentence-level reasoning BM25 can't do (e.g. "fifth" in an award article, eligibility
  spread across a page).
- One checking table loses its product name, so its conflict topic reads "Balance".
