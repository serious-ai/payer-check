# P&L normalizer sample data

Everything here is made up: the practice, its owner ("Samplefam"), staff, patients, vendors and every dollar.
Names carry SAMPLE so nothing reads as a real person or business. Run it with
`python3 pnl_normalizer.py sample-data/pnl --physicians 1 --apps 5.5 --family Samplefam`.

- `Profit and Loss by Month.csv` and `.xlsx`: a QuickBooks Online P&L by month, November 2024 to December 2025.
  The Excel copy uses indent styles and stores some numbers as formulas with no saved value, as QuickBooks does.
- `Transaction Detail by Account.csv`: the matching line detail, with patient refunds that name patients, income
  lines, a payment to a family member and the same card charge listed twice (under the expense and the card account).
- `amex-practice-card.csv` and `chase-owner-card.csv`: card statements in each issuer's own download layout.
- `Expenses by Vendor Summary.csv`: the vendor summary, with patient-name vendors and a blank vendor.
- `answer-key.json`: what was planted (owner add-backs by confidence), the decoys a correct run leaves alone, and the
  strings that must never reach a shareable file.

Rebuild it and compare: `python3 tools/make_pnl_sample.py /some/new/folder` writes identical files.
