#!/usr/bin/env python3
"""Silent downcoding check (copied from the contract check demo; read_835 also keeps DTM*050 and REF*6R): billed versus paid E/M level on a folder of X12 835 remits.

Deterministic. Reads every 835 in a folder with pyx12, compares the code the practice
billed (SVC06, present when the payer changed it) with the code the payer paid (SVC01),
and prices each gap against the practice's fee schedule. No model touches the numbers.

Line classes
  downcoded   both codes are E/M and the paid level is below the billed level
  underpaid   same code, allowed amount below the contracted rate (a different problem)
  denied      paid nothing on the line
  clean       everything else, including modifier-only recodes

Usage: downcoding_check.py REMIT_DIR FEE_SCHEDULE.csv [--out DIR] [--days N] [--tolerance 0.50]
Writes DIR/result.json and DIR/chart-pull-list.csv, prints a short summary.
Fee schedule columns: payer, cpt, contracted_allowed (payer must match the 835's N1*PR name).
"""
import argparse, csv, json, sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

try:
    from pyx12.x12file import X12Reader
except ImportError:
    sys.exit('pyx12 is missing: pip install pyx12')

EM_FAMILIES = {'9920': 'New patient', '9921': 'Established', '9924': 'Office consult'}


def em_level(code):
    """('Established', 4) for 99214, None for anything that is not an office E/M code."""
    if code and len(code) == 5 and code[:4] in EM_FAMILIES and code[4] in '12345':
        return EM_FAMILIES[code[:4]], int(code[4])
    return None


def money(x):
    return D(x or '0').quantize(D('0.01'))


def read_fee_schedule(path):
    fees = {}
    with open(path, newline='') as fh:
        for row in csv.DictReader(fh):
            fees[(row['payer'].strip().upper(), row['cpt'].strip())] = money(row['contracted_allowed'])
    return fees


def read_835(path):
    """Yield one dict per service line."""
    payer = check = pay_date = None
    claim = line = None
    reader = X12Reader(str(path))
    for seg in reader:
        sid = seg.get_seg_id()
        if sid == 'N1' and seg.get_value('01') == 'PR':
            payer = (seg.get_value('02') or '').strip().upper()
        elif sid == 'TRN':
            check = seg.get_value('02')
        elif sid == 'BPR':
            pay_date = seg.get_value('16')
        elif sid == 'CLP':
            if line: yield line; line = None
            claim = dict(acct=seg.get_value('01'), status=seg.get_value('02'), provider='', provider_npi='', received='')
        elif sid == 'NM1' and claim is not None and seg.get_value('01') == '82':
            claim['provider'] = f"{seg.get_value('04') or ''} {seg.get_value('03') or ''}".strip().title()
            claim['provider_npi'] = seg.get_value('09') or ''
        elif sid == 'SVC':
            if line: yield line
            paid_code = seg.get_value('01-2')
            line = dict(file=path.name, payer=payer, check=check, pay_date=pay_date, **claim,
                        paid_code=paid_code, billed_code=seg.get_value('06-2') or paid_code,
                        billed_mod=seg.get_value('06-3') or '', charge=money(seg.get_value('02')),
                        paid=money(seg.get_value('03')), dos='', co={}, pr=D('0'), allowed=None, rarc=[])
        elif line is None and claim is not None and sid == 'DTM' and seg.get_value('01') == '050':
            claim['received'] = seg.get_value('02')      # claim received by the payer (added for the payer cost view)
        elif line is not None and sid == 'REF' and seg.get_value('01') == '6R':
            line['ref'] = seg.get_value('02')            # line item control number, ties a reversal to its original
        elif line is not None and sid == 'DTM' and seg.get_value('01') == '472':
            line['dos'] = seg.get_value('02')
        elif line is not None and sid == 'CAS':
            grp = seg.get_value('01')
            for i in range(2, 19, 3):
                reason, amt = seg.get_value(f'{i:02d}'), seg.get_value(f'{i + 1:02d}')
                if not reason:
                    break
                if grp == 'PR':
                    line['pr'] += money(amt)
                else:
                    line['co'][f'{grp}-{reason}'] = line['co'].get(f'{grp}-{reason}', D('0')) + money(amt)
        elif line is not None and sid == 'AMT' and seg.get_value('01') == 'B6':
            line['allowed'] = money(seg.get_value('02'))
        elif line is not None and sid == 'LQ' and seg.get_value('01') == 'HE':
            line['rarc'].append(seg.get_value('02'))
        elif sid in ('SE', 'LX') and line:
            yield line; line = None
    if line:
        yield line
    errors = reader.pop_errors()
    if errors:
        print(f'{path.name}: {len(errors)} read warnings', file=sys.stderr)


def classify(l, fees, tol):
    if l['allowed'] is None:
        l['allowed'] = l['paid'] + l['pr']
    billed_lv, paid_lv = em_level(l['billed_code']), em_level(l['paid_code'])
    contract_billed = fees.get((l['payer'], l['billed_code']))
    contract_paid = fees.get((l['payer'], l['paid_code']))
    l['contract_billed'], l['contract_paid'] = contract_billed, contract_paid
    l['gap'] = None
    if l['paid'] == 0 and l['allowed'] == 0:
        l['class'] = 'denied'
    elif billed_lv and paid_lv and paid_lv[1] < billed_lv[1]:
        l['class'] = 'downcoded'
        if contract_billed is not None:
            l['gap'] = contract_billed - l['allowed']
        l['allowed_matches_paid_code'] = contract_paid is not None and abs(contract_paid - l['allowed']) <= tol
    elif l['billed_code'] == l['paid_code'] and contract_billed is not None and contract_billed - l['allowed'] > tol:
        l['class'] = 'underpaid'
        l['gap'] = contract_billed - l['allowed']
    else:
        l['class'] = 'clean'
    return l


def summarize(lines, days):
    em = [l for l in lines if em_level(l['billed_code'])]
    down = [l for l in em if l['class'] == 'downcoded']
    under = [l for l in lines if l['class'] == 'underpaid']
    total = sum((l['gap'] or D('0')) for l in down)
    factor = D(365) / D(days)

    def table(key, rows, base):
        out = defaultdict(lambda: dict(em_lines=0, high_level_lines=0, downcoded=0, gap=D('0')))
        for l in base:
            t = out[key(l)]
            t['em_lines'] += 1
            t['high_level_lines'] += em_level(l['billed_code'])[1] >= 4
        for l in rows:
            t = out[key(l)]
            t['downcoded'] += 1
            t['gap'] += l['gap'] or D('0')
        for t in out.values():
            t['share_of_high_level'] = round(t['downcoded'] / t['high_level_lines'], 3) if t['high_level_lines'] else 0
        return dict(sorted(out.items(), key=lambda kv: -kv[1]['gap']))

    by_pair = defaultdict(lambda: dict(lines=0, gap=D('0')))
    for l in down:
        p = by_pair[f"{l['billed_code']} paid as {l['paid_code']}"]
        p['lines'] += 1
        p['gap'] += l['gap'] or D('0')
    by_payer = table(lambda l: l['payer'], down, em)
    top = next(iter(by_payer.items())) if down else None
    top_em = [l for l in em if top and l['payer'] == top[0]]
    return dict(
        period_days=days, files=sorted({l['file'] for l in lines}), service_lines=len(lines), em_lines=len(em),
        downcoded_lines=len(down), downcoded_gap=total, downcoded_gap_annualized=(total * factor).quantize(D('1')),
        top_payer=top[0] if top else None, top_payer_share_of_gap=round(top[1]['gap'] / total, 3) if top and total else 0,
        by_payer=by_payer, by_provider=table(lambda l: l['provider'], down, em),
        top_payer_by_provider=table(lambda l: l['provider'], [l for l in down if top and l['payer'] == top[0]], top_em),
        by_code=dict(sorted(by_pair.items(), key=lambda kv: -kv[1]['gap'])),
        carc_on_downcodes={c: sum(1 for l in down if c in l['co']) for c in sorted({c for l in down for c in l['co']})},
        rarc_on_downcodes={r: sum(1 for l in down if r in l['rarc']) for r in sorted({r for l in down for r in l['rarc']})},
        downcodes_with_no_150=sum(1 for l in down if 'CO-150' not in l['co']),
        downcodes_allowed_matches_paid_code=sum(1 for l in down if l.get('allowed_matches_paid_code')),
        downcodes_missing_fee=sum(1 for l in down if l['gap'] is None),
        other_underpaid_lines=len(under), other_underpaid_gap=sum((l['gap'] for l in under), D('0')),
        other_underpaid_by_payer_code={f"{p} {c}": dict(lines=n, gap=g) for (p, c), (n, g) in _group(under).items()},
        denied_lines=sum(1 for l in lines if l['class'] == 'denied'),
        recoded_same_level=sum(1 for l in em if l['class'] == 'clean' and l['billed_mod']),
    ), down, under


def _group(rows):
    g = defaultdict(lambda: [0, D('0')])
    for l in rows:
        g[(l['payer'], l['billed_code'])][0] += 1
        g[(l['payer'], l['billed_code'])][1] += l['gap']
    return g


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('remit_dir'); ap.add_argument('fee_schedule')
    ap.add_argument('--out', default='downcoding-check-output')
    ap.add_argument('--days', type=int, help='days covered (default: first to last date of service)')
    ap.add_argument('--tolerance', type=D, default=D('0.50'), help='dollars of rounding allowed against the fee schedule')
    a = ap.parse_args()
    fees = read_fee_schedule(a.fee_schedule)
    files = sorted(p for p in Path(a.remit_dir).iterdir() if p.suffix.lower() in ('.835', '.txt', '.edi', '.x12'))
    if not files:
        sys.exit(f'No 835 files (.835, .txt, .edi, .x12) in {a.remit_dir}')
    lines = [classify(l, fees, a.tolerance) for f in files for l in read_835(f)]
    dates = sorted(l['dos'] for l in lines if l['dos'])
    days = a.days or (datetime.strptime(dates[-1], '%Y%m%d') - datetime.strptime(dates[0], '%Y%m%d')).days + 1
    summary, down, under = summarize(lines, days)
    summary['first_dos'], summary['last_dos'] = dates[0], dates[-1]
    payers_missing = sorted({l['payer'] for l in lines} - {p for p, _ in fees})
    summary['payers_missing_from_fee_schedule'] = payers_missing

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    keep = ('payer', 'provider', 'provider_npi', 'acct', 'dos', 'billed_code', 'paid_code', 'charge', 'paid', 'allowed',
            'contract_billed', 'contract_paid', 'gap', 'co', 'rarc', 'check', 'pay_date', 'file')
    flagged = sorted(down, key=lambda l: (l['payer'], l['provider'], l['dos']))
    result = dict(summary=summary, downcoded=[{k: l.get(k) for k in keep} for l in flagged],
                  underpaid=[{k: l.get(k) for k in keep} for l in under])
    (out / 'result.json').write_text(json.dumps(result, indent=1, default=str))
    with open(out / 'chart-pull-list.csv', 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['provider', 'patient_account', 'date_of_service', 'payer', 'billed', 'paid_as', 'dollar_gap', 'remark_codes'])
        for l in flagged:
            w.writerow([l['provider'], l['acct'], l['dos'], l['payer'], l['billed_code'], l['paid_code'], l['gap'], ' '.join(list(l['co']) + l['rarc'])])

    s = summary
    print(f"{len(files)} files, {s['service_lines']} service lines, {s['em_lines']} E/M lines, {days} days ({s['first_dos']} to {s['last_dos']})")
    print(f"Downcoded: {s['downcoded_lines']} lines, ${s['downcoded_gap']:,} gap, ${s['downcoded_gap_annualized']:,} a year at this pace")
    for p, t in s['by_payer'].items():
        if t['downcoded']:
            print(f"  {p}: {t['downcoded']} lines, ${t['gap']:,} ({t['share_of_high_level']:.0%} of its high-level lines)")
    print(f"Other underpayments (same code, below contract): {s['other_underpaid_lines']} lines, ${s['other_underpaid_gap']:,}")
    print(f"Denied lines: {s['denied_lines']}. Written: {out / 'result.json'}, {out / 'chart-pull-list.csv'}")
    if payers_missing:
        print(f"WARNING: no fee schedule rows for {', '.join(payers_missing)}. Their gaps are blank.")


if __name__ == '__main__':
    main()
