# Reference implementation

Copied unchanged from the October 28, 2026 public demo ("What each payer costs") so the tests can check
`payer_check.py` against it. These files read 835s with pyx12 and are used only by `tests/test_cli.py`.

| File | SHA-256 |
|---|---|
| downcoding_check.py | 781900ce4c5b3d81ab456ca3b695dfafaa7f0782e712fb57587fd8c956a8ca4c |
| payer_cost.py | ade2c1e036ef5982c1684d5b67c969f2859cbbda8c0061c5f86e791cd324611c |

`contracts.csv` and `carc-departments.csv` are the demo's sample inputs that `payer_cost.py` needs (Sample data).
`tools/make_sample_data.py` is the demo's seeded generator (SHA-256 1697b4463fdef392888e48d2ea5b042c6ce805bf3651b50cc8142b8cac4c593c)
with one change: the rendering clinicians are named SAMPLE DOCTOR 01 to 24 instead of realistic names, so the sample
835s differ from the demo's only in the NM1*82 segments.
