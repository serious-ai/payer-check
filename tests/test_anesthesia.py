"""Anesthesia mode against the planted answer key in sample-data/anesthesia (Sample data, made up end to end)."""
import json, subprocess, sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANES = ROOT / 'sample-data' / 'anesthesia'
sys.path.insert(0, str(ROOT))
import payer_check as pc  # noqa: E402

KEY = json.loads((ANES / 'answer-key.json').read_text())


@pytest.fixture(scope='module')
def result():
    return pc.run(pc.find_835s(ANES / '835'), (ANES / 'fee-schedule.csv').read_text(), {}, (ANES / 'base-units.csv').read_text())


def test_unit_math():
    assert pc.to_fixed('20.4976', 4) == 204976 and pc.fixed(204976, 4) == '20.4976' and pc.to_fixed('37.5', 1) == 375
    # 52 minutes in 15-minute units: 3.5 to one decimal (CMS), 4 rounding up, 3 rounding down
    assert [pc.time_units(520, 15, r) for r in pc.ROUNDINGS] == [35, 40, 30]
    assert pc.time_units(70, 15, 'tenth') == 5 and pc.time_units(80, 15, 'tenth') == 5      # 7 min is 0.47, 8 min is 0.53
    # (5 base + 3.5 time) x $20.4976 = $174.2296, to $174.23. Half of it at the directed rate: $87.11
    assert pc.anes_cents(85, 204976, 100) == 17423 and pc.anes_cents(85, 204976, 50) == 8711


def test_answer_key(result):
    """Every planted short line is found, for the planted reason, with the planted gap to the cent."""
    short = {l['acct']: l for l in result['lines'] if l['class'] == 'anesthesia_short'}
    planted = {x['acct']: x for x in KEY['planted']}
    assert set(short) == set(planted)
    assert len(short) == KEY['planted_lines'] == 459
    for acct, x in planted.items():
        l = short[acct]
        assert l['anes']['reasons'] == [x['reason']], acct
        assert l['gap'] == pc.to_cents(x['gap']), acct
    assert sum(l['gap'] for l in short.values()) == pc.to_cents(KEY['planted_gap'])
    rows = {p['payer']: p for p in result['anes_by_payer']}
    for k, v in KEY['by_payer_and_reason'].items():
        payer, reason = k.split('|')
        assert rows[payer][f'{reason}_lines'] == v['lines'] and rows[payer][f'{reason}_gap'] == pc.money(pc.to_cents(v['gap'])), k


def test_decoys_left_alone(result):
    flagged = {l['acct'] for l in result['lines'] if l['class'] == 'anesthesia_short'}
    nf = KEY['not_findings']
    assert nf['rounding_acct'] not in flagged and nf['decoy_reversal_acct'] not in flagged
    for k in ('directed_at_50_accts', 'plan_h_qz_at_85_accts', 'plan_h_capped_epidural_accts', 'plan_g_physical_status_accts'):
        assert nf[k] and not set(nf[k]) & flagged, k
    k = next(p for p in result['anes_by_payer'] if p['payer'] == nf['no_contract_payer'])
    assert k['conversion_factor_on_file'] == 'no' and k['gap'] == '' and k['lines_checked'] == 0
    e = next(p for p in result['by_payer'] if p['payer'] == nf['no_contract_payer'])
    assert e['contract_on_file'] == 'no' and e['cost_per_100_paid'] == ''


def test_denials(result):
    den = Counter(f"{l['payer']}|{l['carc']}" for l in result['lines'] if l['class'] == 'denied')
    assert dict(den) == KEY['denied_claims_by_payer_carc']
    priced = [l for l in result['lines'] if l['class'] == 'denied' and l['payer'] != KEY['not_findings']['no_contract_payer']]
    assert priced and all(l['contract_billed'] > 0 for l in priced), 'a denied anesthesia line is valued at its contract price'


def test_cost_includes_anesthesia_gap(result):
    for p in result['by_payer']:
        if p['contract_on_file'] == 'yes':
            parts = ('downcoded_gap', 'short_pay_gap', 'anesthesia_gap', 'staff_cost', 'denied_dollars_lost')
            assert sum(pc.to_cents(p[k]) for k in parts) == pc.to_cents(p['total_cost']), p['payer']


def test_summaries_have_no_patient_fields(result):
    for key in ('by_payer', 'by_code', 'anes_by_payer'):
        assert not set(result[key][0]) & {'patient_account', 'acct', 'provider', 'date_of_service', 'file'}, key
        text = pc.to_csv(result[key], result['sample'])
        assert not any(l['acct'] in text for l in result['lines']), key


def test_sample_label_on_every_row(result):
    for key in ('by_payer', 'by_code', 'pull', 'anes_by_payer', 'anes_pull'):
        rows = pc.to_csv(result[key], result['sample']).splitlines()
        assert rows[0].startswith('source,') and all(r.startswith('Sample data,') for r in rows[1:]), key


def test_sample_regenerates_identically(tmp_path):
    subprocess.run([sys.executable, str(ROOT / 'tools' / 'make_anesthesia_sample.py'), str(tmp_path)], check=True, capture_output=True)
    names = sorted(p.name for p in (ANES / '835').glob('*.835'))
    assert names == sorted(p.name for p in (tmp_path / '835').glob('*.835'))
    for name in names:
        assert (tmp_path / '835' / name).read_bytes() == (ANES / '835' / name).read_bytes(), name
    for name in ('fee-schedule.csv', 'base-units.csv', 'answer-key.json'):
        assert (tmp_path / name).read_bytes() == (ANES / name).read_bytes(), name


def test_fee_schedule_errors():
    with pytest.raises(ValueError, match='time_rounding'):
        pc.read_fees('payer,cpt,conversion_factor,time_rounding\nSAMPLE X,,50,sometimes\n')
    with pytest.raises(ValueError, match='modifier_pct'):
        pc.read_fees('payer,cpt,conversion_factor,modifier_pct\nSAMPLE X,,50,QZ85\n')
    with pytest.raises(ValueError, match='contracted_allowed'):
        pc.read_fees('payer,cpt,rate\nSAMPLE X,99213,50\n')
    fees, anes = pc.read_fees('payer,cpt,conversion_factor\nSample X,,50.5\n')
    assert fees == {} and anes['cf'][('SAMPLE X', '')][0][1]['cf4'] == 505000


def test_cli_runs(tmp_path):
    out = subprocess.run([sys.executable, str(ROOT / 'payer_check.py'), str(ANES / '835'), str(ANES / 'fee-schedule.csv'),
                          '--base-units', str(ANES / 'base-units.csv'), '--out', str(tmp_path)], check=True, capture_output=True, text=True).stdout
    assert out.startswith('Sample data:') and 'anesthesia-pull-list.csv' in out
    assert {p.name for p in tmp_path.iterdir()} == {name for name, _ in pc.OUTPUTS + pc.ANES_OUTPUTS}
