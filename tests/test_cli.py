"""The CLI against the planted answer key, the demo's pyx12 reader and the demo's payer_cost.py.

Everything runs on the synthetic sample in sample-data/ (Sample data, made up end to end).
"""
import json, subprocess, sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / 'sample-data'
REF = Path(__file__).resolve().parent / 'reference'
sys.path.insert(0, str(ROOT))
import payer_check as pc  # noqa: E402

KEY = json.loads((SAMPLE / 'answer-key.json').read_text())


@pytest.fixture(scope='module')
def result():
    return pc.run(pc.find_835s(SAMPLE / '835'), (SAMPLE / 'fee-schedule.csv').read_text())


def test_money_is_exact():
    assert [pc.to_cents(x) for x in ('153.19', '30', '-0.5', '.07', '$1,234.5', '', '0.125', '0.135', '0.1251')] == \
        [15319, 3000, -50, 7, 123450, 0, 12, 14, 13]
    assert pc.money(-5) == '-0.05' and pc.money(123456) == '1234.56'
    assert pc.div_half_even(5, 2) == 2 and pc.div_half_even(7, 2) == 4 and pc.div_half_even(10, 3) == 3


def test_answer_key(result):
    """Every planted downcode, short pay, denial and reversal is found, and every decoy is left alone."""
    lines = result['lines']
    down = [l for l in lines if l['class'] == 'downcoded']
    short = [l for l in lines if l['class'] == 'short_pay']
    assert {l['acct'] for l in down} == {x['acct'] for x in KEY['downcoded']}
    assert len(down) == KEY['downcoded_lines'] == 198
    assert sum(l['gap'] for l in down) == pc.to_cents(KEY['downcoded_gap'])
    assert {(l['acct'], l['billed_code']) for l in short} == {(x['acct'], x['code']) for x in KEY['short_pays']}
    assert len(short) == KEY['short_pay_lines'] == 2127
    assert sum(l['gap'] for l in short) == pc.to_cents(KEY['short_pay_gap'])
    den = {}
    for l in lines:
        if l['class'] == 'denied':
            den.setdefault(f"{l['payer']}|{l['carc']}", set()).add(l['acct'])
    assert {k: len(v) for k, v in den.items()} == KEY['denied_claims_by_payer_carc']
    rev = {p['payer']: p['reversed_claims'] for p in result['by_payer']}
    assert rev == KEY['reversed_claims']
    nf = KEY['not_findings']
    flagged = {l['acct'] for l in down + short}
    assert nf['rounding_acct'] not in flagged
    assert not set(nf['modifier_recode_accts']) & flagged
    assert nf['decoy_reversal_acct'] not in flagged
    e = next(p for p in result['by_payer'] if p['payer'] == nf['no_contract_payer'])
    assert e['contract_on_file'] == 'no' and e['cost_per_100_paid'] == '' and e['denied_value_at_contract'] == ''


def test_reader_matches_pyx12(result):
    """The plain-code 835 reader gives the same service lines as the demo's pyx12 reader, field for field."""
    pytest.importorskip('pyx12')
    sys.path.insert(0, str(REF))
    import downcoding_check as dc
    files = sorted((SAMPLE / '835').glob('*.835'))
    n = 0
    for f in files:
        ref = list(dc.read_835(f))
        ours = pc.read_835(f.name, f.read_text())
        assert len(ref) == len(ours), f.name
        for a, b in zip(ref, ours):
            n += 1
            assert (a['payer'], a['acct'], a['status'], a['received'], a['pay_date'], a['dos'], a['billed_code'], a['paid_code'], a.get('ref', '')) == \
                   (b['payer'], b['acct'], b['status'], b['received'], b['pay_date'], b['dos'], b['billed_code'], b['paid_code'], b['ref']), f.name
            assert (pc.to_cents(str(a['charge'])), pc.to_cents(str(a['paid'])), pc.to_cents(str(a['pr']))) == (b['charge'], b['paid'], b['pr'])
            assert {k: pc.to_cents(str(v)) for k, v in a['co'].items()} == b['co'] and list(a['co']) == list(b['co'])
            assert (None if a['allowed'] is None else pc.to_cents(str(a['allowed']))) == b['allowed']
            assert a['rarc'] == b['rarc']
    assert n == result['raw_lines'] == 17788


def test_matches_demo_payer_cost(tmp_path, result):
    """At a 0% write-off (the demo's payer_cost.py prices none) every payer's numbers match the demo's check."""
    pytest.importorskip('pyx12')
    src = tmp_path / 'sample'
    src.mkdir()
    (src / '835').symlink_to(SAMPLE / '835')
    for name in ('fee-schedule.csv',):
        (src / name).write_text((SAMPLE / name).read_text())
    for name in ('contracts.csv', 'carc-departments.csv'):
        (src / name).write_text((REF / name).read_text())
    subprocess.run([sys.executable, str(REF / 'payer_cost.py'), str(src), '--out', str(tmp_path / 'ref')], check=True,
                   cwd=REF, capture_output=True)
    ref = json.loads((tmp_path / 'ref' / 'result.json').read_text())['by_payer']
    ours = {p['payer']: p for p in pc.run(pc.find_835s(SAMPLE / '835'), (SAMPLE / 'fee-schedule.csv').read_text(),
                                          dict(writeoff_pct='0'))['by_payer']}
    assert set(ref) == set(ours)
    c = lambda x: pc.to_cents(str(x)) if x not in (None, '') else None
    for payer, r in ref.items():
        o = ours[payer]
        assert o['claims'] == r['claims'] and o['service_lines'] == r['lines'], payer
        assert c(o['paid']) == c(r['paid']), payer
        assert o['denied_claims'] == r['denied_claims'] and o['denied_lines'] == r['denied_lines'], payer
        assert c(o['denied_value_at_contract']) == c(r['denied_dollars']), payer
        assert o['downcoded_lines'] == r['downcoded_lines'] and o['short_pay_lines'] == r['short_pay_lines'], payer
        assert o['short_pay_claims'] == r['short_pay_claims'] and o['reversed_claims'] == r['reversed_claims'], payer
        if r['on_file']:
            assert c(o['downcoded_gap']) == c(r['downcoded_gap']) and c(o['short_pay_gap']) == c(r['short_pay_gap']), payer
            assert c(o['cost_per_100_paid']) == c(r['cost_per_100_paid']), payer
        assert Decimal(o['days_to_pay_median']) == Decimal(str(r['days_median'])) and o['days_to_pay_p90'] == r['days_p90'], payer
        assert o['slow_claims'] == r['slow_claims'] and o['staff_minutes'] == r['staff_minutes_90'], payer
        assert c(o['staff_cost']) == c(r['staff_cost_90']), payer


def test_default_cost_matches_demo_page(result):
    """At the defaults (46% write-off) cost per $100 paid matches the published demo page (engine.py, default inputs)."""
    got = {p['payer'][-1]: p['cost_per_100_paid'] for p in result['by_payer']}
    assert got == {'A': '0.69', 'B': '2.91', 'C': '7.15', 'D': '5.60', 'E': ''}


def test_summaries_have_no_patient_fields(result):
    for key in ('by_payer', 'by_code'):
        cols = set(result[key][0])
        assert not cols & {'patient_account', 'acct', 'provider', 'date_of_service', 'file'}, key
        text = pc.to_csv(result[key], result['sample'])
        assert not any(l['acct'] in text for l in result['lines'][:500]), key


def test_sample_label_on_every_row(result):
    for key in ('by_payer', 'by_code', 'pull'):
        rows = pc.to_csv(result[key], result['sample']).splitlines()
        assert rows[0].startswith('source,') and all(r.startswith('Sample data,') for r in rows[1:]), key


def test_sample_regenerates_identically(tmp_path):
    """The sample remits come from a seeded generator, so anyone can rebuild them and confirm they are made up."""
    subprocess.run([sys.executable, str(ROOT / 'tools' / 'make_sample_data.py'), str(tmp_path)], check=True, capture_output=True)
    for f in sorted((SAMPLE / '835').glob('*.835')):
        assert (tmp_path / '835' / f.name).read_bytes() == f.read_bytes(), f.name
    assert (tmp_path / 'fee-schedule.csv').read_bytes() == (SAMPLE / 'fee-schedule.csv').read_bytes()


def test_cli_runs(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / 'payer_check.py'), str(SAMPLE / '835'), str(SAMPLE / 'fee-schedule.csv'),
                          '--out', str(tmp_path)], check=True, capture_output=True, text=True).stdout
    assert out.startswith('Sample data:')
    assert {p.name for p in tmp_path.iterdir()} == {'summary-by-payer.csv', 'summary-by-payer-and-code.csv', 'chart-pull-list.csv'}
