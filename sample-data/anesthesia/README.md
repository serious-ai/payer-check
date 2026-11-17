# Anesthesia sample data

Everything in this folder is made up: the group (6 anesthesiologists and 10 nurse anesthetists), the patients,
the five payers ("Sample Plan F" to "Sample Plan K"), the conversion factors and every dollar.
The ten base units match common published values for these codes, so the math looks real, but don't use
`base-units.csv` as a reference table. Sample Plan F is not Medicare. None of it is evidence of anything about a
real payer or practice.

- `835/`: 77 synthetic X12 835 remits for 1,500 cases (2,205 claim lines) over 90 days of service
  (June 1 to August 29, 2026). A medically directed case bills twice, once for the anesthesiologist and once
  for the nurse anesthetist.
- `fee-schedule.csv`: each payer's conversion factor and contract terms. Sample Plan G's conversion factor rises
  from 62.00 to 64.50 on July 15, 2026. Sample Plan H pays QZ at 85% and caps labor epidurals at 435 minutes.
  Sample Plan K has no rows, so it is named and never priced.
- `base-units.csv`: base units for the ten codes in the remits, there so the sample runs on its own.
- `answer-key.json`: what was planted on purpose (Plan G kept paying the old conversion factor, Plan H cut time
  to whole units from August 1, Plan J paid 00813 one base unit short, capped long cases at 120 minutes and
  paid some QZ cases as QX) and the decoys a correct check leaves alone.

Rebuild it yourself and compare: `python3 tools/make_anesthesia_sample.py /some/new/folder` writes identical
files (a seeded generator, checked by `tests/test_anesthesia.py`).
