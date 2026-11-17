# Sample data

Everything in this folder is made up: the practice (a 24-clinician GI group), the clinicians, the patients,
the five payers ("Sample Plan A" to "Sample Plan E"), the contracts and every dollar. The "Medicare" rates
behind the fee schedule are rounded sample figures, not the published schedule. None of it is evidence of
anything about a real payer or practice.

- `835/`: 82 synthetic X12 835 remits covering 90 days of visits (April 27 to July 25, 2026).
- `fee-schedule.csv`: contract rates by payer and code. Sample Plan C's rates rise 5% from June 1, 2026
  (the `effective` column). Sample Plan E has no rows, so it is named and never priced.
- `answer-key.json`: what was planted on purpose (198 visits paid at a lower code on Plan B, 2,127 lines on
  Plan C still paid at the old rate, denials by reason, 196 reversals) and the decoys a correct check leaves alone.

Rebuild it yourself and compare: `python3 tools/make_sample_data.py /some/new/folder` writes identical files
(a seeded generator, checked by `tests/test_cli.py`).

`anesthesia/` is a second made-up sample for anesthesia mode. See [anesthesia/README.md](anesthesia/README.md).
