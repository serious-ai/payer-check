# Payer check

See what each insurance company really costs your practice, from your own payment files and your contracted rates. Free to use.

**[Open the payer check](https://check.seriousai.partners/payer-check.html)** in any web browser. There's nothing to install and no sign-up.

**Your files never leave your computer.** The page works like a calculator inside your browser. Nothing is uploaded, not to us and not to anyone else, and it works with your internet turned off. Your IT team can verify that in a few minutes (steps below).

You'll need two things your billing team already has: the insurance payment files (835s, also called ERAs) from the last 90 days or so, and a spreadsheet of your contracted rates. The page has a template for the rates.

The rest of this page is for whoever checks the tool on your behalf.

For each payer: claims, dollars paid, denied claims (valued at your contract rate), visits paid at a lower code than billed, lines paid short of contract, reversals, days to pay, the staff time all of that takes, and one number that pulls it together, the cost for every $100 the payer paid you.

## Your files never leave your computer

The check runs on your machine, in a browser tab or in a Python script. Nothing is uploaded, and neither one has any code that talks to the internet.

How a practice's IT person can verify it:

1. **Run it offline.** Download `payer-check.html`, turn off Wi-Fi (or unplug the network cable), open the file and run it. It works the same.
2. **Watch the network.** Open the browser's developer tools, go to the Network panel, and run a check. The list stays empty.
3. **Read the policy.** The page's first lines carry a Content Security Policy (`default-src 'none'; connect-src 'none'`) that tells the browser to block every fetch, upload and outside file the page could ask for. The policy doesn't cover the page sending the tab somewhere else, so the page has no links or code that open another site, and the test below watches for that too.
4. **Read the code.** The page is one file, about 450 lines, with nothing loaded from anywhere. The Python script uses only the standard library.
5. **Check the test.** `tests/test_page.py` opens the page, runs the sample remits through it, saves every output, waits a few seconds, and records every request the browser makes and every window it opens. Any request or new window fails the build, and it runs on every push.

The patient's name in the 835 (the NM1\*QC segment) is never read.

## Use it in a browser

1. Open `payer-check.html` (double-click it, no install).
2. Choose the folder your clearinghouse saves remits to, or pick the files.
3. Choose your fee schedule CSV.
4. Click Run the check, then save the three results.

## Use it from the command line

Python 3.9 or newer, nothing to install.

```bash
python3 payer_check.py path/to/835-folder path/to/fee-schedule.csv --out payer-check-output
```

Options: `--hourly 30.00` (staff cost an hour), `--writeoff-pct 46.0` (share of denied dollars never recovered), `--slow-days 30`.

The page and the script share one logic, and the tests require them to write byte-identical files from the same input.

## What goes in

- **835 files.** Every file under the folder (subfolders included) with an `.835`, `.txt`, `.edi`, `.x12`, `.era` or `.rmt` extension that starts with an ISA segment.
- **A fee schedule CSV** with the columns `payer`, `cpt` and `contracted_allowed`, plus an optional `effective` date (YYYY-MM-DD) for rates that change partway through. Extra columns are ignored. The payer name has to match the payer name in the 835 (the N1\*PR segment), case aside. A payer with no rows is named in the results and left unpriced.

## What comes out

| File | What's in it | Patient-level fields |
|---|---|---|
| `summary-by-payer.csv` | One row per payer | None |
| `summary-by-payer-and-code.csv` | One row per payer and billed code | None |
| `chart-pull-list.csv` | Every flagged line (paid at a lower code, paid short, denied) with the patient account number, so your team can pull the chart | Yes. Keep it on your computer. |

Every row starts with a `source` column. Files built from the sample remits say "Sample data" on every row.

## How it decides

- **Denied:** the line paid nothing and allowed nothing.
- **Paid at a lower code:** an office visit (99201 to 99205, 99211 to 99215, 99241 to 99245) paid at a lower level than billed. The gap is your contract rate for the billed code minus what was allowed.
- **Paid short:** paid at the code billed, with the allowed amount more than 50 cents under your contract rate for that date of service.
- **Reversal:** a claim the payer took back (claim status 22). The line counts once, at its last version.
- **Days to pay:** from the date the payer received the claim (DTM\*050) to its first payment. Claims without a received date are left out of the days, and the summary says how many were counted.

**Cost per $100 paid** adds the dollars paid short (lower codes and short pays), the share of denied dollars never recovered, and staff time, then sets it against what the payer paid. The inputs are assumptions you can change:

- Staff minutes each time a claim needs handling again: 20 to work a denial, 15 to review a visit paid at a lower code, 5 for a short pay, 10 to re-post a reversal, 6 for a status check on a claim unpaid after 30 days. These and the $30 hourly cost are illustrative.
- 46% of denied dollars never recovered. That's the rest of Premier's figure from its survey of hospitals and health systems on 2022 claims (March 21, 2024: 54.3% of claims private payers first denied were eventually paid). It's a hospital figure, and the 54.3% counts claims that were worked, so put in your own rate if you track it.

What a remit can't show stays out: prior authorization hours, records requests, portal and phone time, credentialing.

## Try it with the sample

`sample-data/` holds 90 days of made-up remits for a made-up practice, plus its fee schedule. See [sample-data/README.md](sample-data/README.md). It is Sample data, labeled that way on every surface, and the numbers it produces are not results.

```bash
python3 payer_check.py sample-data/835 sample-data/fee-schedule.csv
```

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m playwright install chromium
```

```bash
.venv/bin/python -m pytest -v
```

- The page makes no network request and opens no other window during a full run and for 3 seconds after the last save. Planted requests in copies of the page (at load, and half a second after a save) get caught, so the recorder is proven to work.
- The page and the script write byte-identical CSVs, on the sample and on a set of edge cases.
- Every planted problem in the sample is found and every decoy is left alone.
- The 835 reader matches the pyx12 reader line for line, and the per-payer numbers match the October 28, 2026 demo's own check (`tests/reference/`).

## De-identifier

**[Open the de-identifier](https://check.seriousai.partners/deidentify.html)** to turn a billing export into a copy that's safe to share under HIPAA's Safe Harbor method (45 CFR 164.514(b)(2)). It runs the same way as the payer check: one file, in the browser, nothing uploaded, with the same Content Security Policy and the same no-network test (`tests/test_deidentify.py`).

Pick a CSV or Excel (.xlsx) export as is (title rows above the headers are fine), check the columns, and save three files:

| File | What's in it | Share it? |
|---|---|---|
| `<name>-deidentified.csv` | The copy | Yes |
| `<name>-deid-record.txt` | What happened to every column, and all 18 Safe Harbor identifiers checked | Yes, alongside the copy |
| `<name>-visit-code-key.csv` | Each code and the original number it replaced | No. It stays at the practice. |

What it does, column by column:

- Account, encounter, claim, member and similar numbers become random codes (164.514(c)). A code isn't made from anything about the patient, and the same number always gets the same code.
- Every date becomes its year. When a service date and a payment date sit side by side, the days between them are worked out first and added as a column.
- Names, addresses, phone numbers, birth dates and other direct identifiers are dropped. Ages over 89 read 90+.
- Amounts, codes, payer and provider names stay. Dates, phone numbers, emails, links and long numbers typed inside them are replaced.
- Free-text notes and unrecognized columns are dropped by default, and the record names them so someone can keep one on purpose.
- Rows come out grouped by the random code, not in their original order.

What software can't do is Safe Harbor's second condition: the practice confirms it has no actual knowledge that what's left could identify someone (164.514(b)(2)(ii)). The record says so.

From the command line (Python 3.9+, nothing to install):

```bash
python3 deidentify.py "CPT Payment Report.xlsx" --out deidentified
```

`--keep`, `--drop`, `--code` and `--year` take a column name and override its default. The page and the script share one logic, and the tests require byte-identical files from the same input. `sample-data/deid/` holds a made-up payment report, as CSV and Excel (`tools/make_deid_sample.py`).

## License

Code under the [Apache License 2.0](LICENSE). Documentation under [Creative Commons Attribution 4.0](LICENSE-docs). From Serious AI Partners.
