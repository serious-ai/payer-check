# Payer check

See what each payer costs your practice, read from your own 835 remits and your fee schedule.

For each payer: claims, dollars paid, denied claims (valued at your contract rate), visits paid at a lower code than billed, lines paid short of contract, reversals, days to pay, the staff time all of that takes, and one number that pulls it together, the cost for every $100 the payer paid you.

Anesthesia groups get the same check priced the way anesthesia is paid: base units plus time units, times each payer's conversion factor, times the share the modifier pays. Every line paid short is put down to its reason: a conversion factor below contract, base units short, time cut, or a medical direction cut.

## Your files never leave your computer

The check runs on your machine, in a browser tab or in a Python script. Nothing is uploaded, and neither one has any code that talks to the internet.

How a practice's IT person can verify it:

1. **Run it offline.** Download `payer-check.html`, turn off Wi-Fi (or unplug the network cable), open the file and run it. It works the same.
2. **Watch the network.** Open the browser's developer tools, go to the Network panel, and run a check. The list stays empty.
3. **Read the policy.** The page's first lines carry a Content Security Policy (`default-src 'none'; connect-src 'none'`) that tells the browser to block every fetch, upload and outside file the page could ask for. The policy doesn't cover the page sending the tab somewhere else, so the page has no links or code that open another site, and the test below watches for that too.
4. **Read the code.** The page is one file, about 650 lines, with nothing loaded from anywhere. The Python script uses only the standard library.
5. **Check the test.** `tests/test_page.py` opens the page, runs the sample remits (the GI sample and the anesthesia sample) through it, saves every output, waits a few seconds, and records every request the browser makes and every window it opens. Any request or new window fails the build, and it runs on every push.

The patient's name in the 835 (the NM1\*QC segment) is never read.

## Use it in a browser

1. Open `payer-check.html` (double-click it, no install).
2. Choose the folder your clearinghouse saves remits to, or pick the files.
3. Choose your fee schedule CSV. For anesthesia, also choose a base units CSV.
4. Click Run the check, then save the results.

## Use it from the command line

Python 3.9 or newer, nothing to install.

```bash
python3 payer_check.py path/to/835-folder path/to/fee-schedule.csv --out payer-check-output
```

For anesthesia, add `--base-units path/to/base-units.csv`.

Options: `--hourly 30.00` (staff cost an hour), `--writeoff-pct 46.0` (share of denied dollars never recovered), `--slow-days 30`.

The page and the script share one logic, and the tests require them to write byte-identical files from the same input.

## What goes in

- **835 files.** Every file under the folder (subfolders included) with an `.835`, `.txt`, `.edi`, `.x12`, `.era` or `.rmt` extension that starts with an ISA segment.
- **A fee schedule CSV** with the columns `payer`, `cpt` and `contracted_allowed`, plus an optional `effective` date (YYYY-MM-DD) for rates that change partway through. Extra columns are ignored. The payer name has to match the payer name in the 835 (the N1\*PR segment), case aside. A payer with no rows is named in the results and left unpriced. Anesthesia contracts use the extra columns under [Anesthesia](#anesthesia).
- **A base units CSV** (anesthesia only) with a code column (`cpt`, `code` or `hcpcs`) and a column with "base" in its name. See [Anesthesia](#anesthesia) for where the numbers come from.

## What comes out

| File | What's in it | Patient-level fields |
|---|---|---|
| `summary-by-payer.csv` | One row per payer | None |
| `summary-by-payer-and-code.csv` | One row per payer and billed code | None |
| `chart-pull-list.csv` | Every flagged line (paid at a lower code, paid short, denied) with the patient account number, so your team can pull the chart | Yes. Keep it on your computer. |
| `anesthesia-by-payer.csv` | Written when the remits hold anesthesia codes. One row per payer: lines checked, contract and paid dollars per unit, and the gap by reason | None |
| `anesthesia-pull-list.csv` | Written when the remits hold anesthesia codes. Every anesthesia line paid short, with its minutes, units, modifiers, conversion factors and the gap by reason | Yes. Keep it on your computer. |

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

## Anesthesia

Codes 00100 to 01999 from a payer with a `conversion_factor` row are priced as anesthesia:

(base units + time units + physical status units) × conversion factor × the modifier's share

The rules come from Medicare's Claims Processing Manual, chapter 12, section 50 (the copy issued May 7, 2026). Time units are anesthesia minutes divided by 15, rounded to one decimal place. QK, QX and QY pay the medically directed rate of 50%. AD pays 3 base units, and 01996 gets no time units. Commercial contracts differ, so each of these is a column you can set per payer.

| Column | What it holds | When blank |
|---|---|---|
| `conversion_factor` | Dollars per unit, up to four decimals (for example 62.00). This is what makes a row an anesthesia row. Leave `cpt` blank for every anesthesia code, or name one code for a different rate. | Not an anesthesia row |
| `time_unit_minutes` | Minutes in one time unit | 15 |
| `time_rounding` | `tenth` (one decimal place), `up` (every started unit counts) or `down` (whole units only) | `tenth` |
| `modifier_pct` | The share each modifier pays, such as `QZ=85` | AA, AD and QZ 100, QK, QX and QY 50 |
| `physical_status_units` | Units the contract adds for physical status, such as `P3=1 P4=2 P5=3` | None, as Medicare pays |
| `max_minutes` | A cap on time for a code, such as `01967=435` | No cap |
| `base_units` | This payer's base units for the code on this row, when they differ from the base units file | The base units file |

A row for one code carries its own terms, so fill in every column that applies there too. UnitedHealthcare's commercial anesthesia policy (2026R0032A, February 15, 2026 version) is one example of why the columns exist: it pays QZ at 85%, uses the ASA's base values, and caps labor epidurals (01967) at 435 minutes.

**Base units.** CMS posts its base units on the [Anesthesiologists Center](https://www.cms.gov/medicare/payment/fee-schedules/physician/anesthesiologists-center) page. As of November 2026 the newest file there is the 2022 one, and the page says the base units are unchanged for 2026 and 2027. Save it as a CSV with a code column and a base units column. A payer that uses the ASA's values instead needs those in its `base_units` rows. This repo ships no base units table. The sample's file covers only its ten codes and isn't meant as a reference.

**Medicare's conversion factor.** For dates of service from January 1, 2027, CMS's final rule puts the national anesthesia conversion factor at $20.1056 for clinicians who aren't qualifying APM participants and $20.3068 for those who are (CMS-1848-F, 91 FR 70992, November 9, 2026, page 71653). For 2026 dates of service it's $20.4976 and $20.5998 (CMS-1832-F, 90 FR 49266, November 5, 2025, page 49961). Medicare pays by locality, so a Medicare row should carry the factor your Medicare contractor lists for your locality, not the national one, with a new row effective 2027-01-01.

**What it reads from the 835.** The procedure code and modifiers (SVC01, and SVC06 when the payer changed them), the minutes paid (SVC05) and billed (SVC07, or SVC05 when the payer paid what was billed). Medicare and UnitedHealthcare both require anesthesia time on the claim in minutes, and the check assumes the remit's units are those minutes. A remit with no units is left unchecked and counted.

**How it decides.** A line is short when the contract price is more than 50 cents above what the payer allowed. The gap is split in this order, and the parts add up to the gap to the cent:

1. **Medical direction cut:** the payer paid at a lower modifier share than billed (QZ billed, QX paid).
2. **Time cut:** fewer minutes paid than billed, past any cap in the contract, or time cut to whole units where the contract counts tenths.
3. **Base units short:** physical status units missing, or what's left equals whole base units at the contract rate, to within 2 cents.
4. **Conversion factor short:** whatever is left.

One line can't always tell a base unit cut from a lower conversion factor, because both can come to the same dollars. The pull list shows the conversion factor each line was paid at, so whoever works the list can see which it is across a payer's lines.

The anesthesia gap counts toward cost per $100 paid, with 5 staff minutes for each claim paid short. A denied anesthesia line is valued at its contract price.

## Try it with the sample

`sample-data/` holds 90 days of made-up remits for a made-up practice, plus its fee schedule, and `sample-data/anesthesia/` does the same for a made-up anesthesia group. See [sample-data/README.md](sample-data/README.md). It is Sample data, labeled that way on every surface, and the numbers it produces are not results.

```bash
python3 payer_check.py sample-data/835 sample-data/fee-schedule.csv
```

```bash
python3 payer_check.py sample-data/anesthesia/835 sample-data/anesthesia/fee-schedule.csv --base-units sample-data/anesthesia/base-units.csv
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
- Every planted problem in both samples is found, with the planted reason and gap to the cent, and every decoy is left alone.
- The 835 reader matches the pyx12 reader line for line, and the per-payer numbers match the October 28, 2026 demo's own check (`tests/reference/`).

## License

Code under the [Apache License 2.0](LICENSE). Documentation under [Creative Commons Attribution 4.0](LICENSE-docs). From Serious AI Partners.
