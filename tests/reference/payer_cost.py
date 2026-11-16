#!/usr/bin/env python3
"""Payer cost check: what each contract costs the practice, priced from its own 835 remits.

Built on the contract check demo (contract_check.py there, its paid-versus-contracted section kept
here) and the downcoding check (downcoding_check.py, copied with read_835 also keeping the claim
received date and the line control number). The denial grouping by reason code follows the
"Across your locations" demo. Deterministic: no model touches a number.

One row per payer, 90 days scaled to a year:
  paid, denials by department (from the CARC), downcodes (billed versus paid code, priced at contract),
  short pays at the same code, days from claim received to payment, reversals, and staff time
  (touches x minutes x an hourly rate, all illustrative and editable on the page).
What a remit cannot show: prior auth hours, records requests, portal and phone time, credentialing.

Usage: payer_cost.py SAMPLE_DIR --out DIR [--days 90]
SAMPLE_DIR holds 835/, fee-schedule.csv (payer, cpt, contracted_allowed, effective), contracts.csv,
carc-departments.csv.
"""
import argparse, csv, json, statistics
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path

from downcoding_check import classify, read_835

# Illustrative staff inputs (no published figure). The page shows them beside a blank "Your number" column.
MINUTES = dict(denial=20, downcode=15, short_pay=5, reversal=10, slow=6)
HOURLY = 30              # blended hourly cost of a billing staff member, loaded (illustrative)
FTE_HOURS = 2080         # paid hours in a full-time year
SLOW_DAYS = 30           # a claim still unpaid this many days after receipt gets a status check
TOUCH_NAMES = dict(denial='Work a denied claim', downcode='Review a downcoded visit', short_pay='Post and flag a short pay',
                   reversal='Re-post a reversal', slow='Status check on a claim unpaid at 30 days')

ymd = lambda s: datetime.strptime(s, '%Y%m%d').date()


class Fees:
    """Contract rate in force on the date of service: fees.on(dos).get((payer, cpt))."""
    def __init__(self, path):
        self.rates = defaultdict(list)
        for r in csv.DictReader(open(path)):
            self.rates[(r['payer'].strip().upper(), r['cpt'].strip())].append((r['effective'], D(r['contracted_allowed'])))
        for v in self.rates.values():
            v.sort()

    def on(self, dos):
        iso = f'{dos[:4]}-{dos[4:6]}-{dos[6:]}'
        rates = self.rates

        class View:
            def get(self, k, default=None):
                hits = [r for eff, r in rates.get(k, []) if eff <= iso]
                return hits[-1] if hits else default
        return View()


def pct(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))] if v else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sample_dir'); ap.add_argument('--out', default='check-output')
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--tolerance', type=D, default=D('0.50'))
    a = ap.parse_args()
    src = Path(a.sample_dir)
    fees = Fees(src / 'fee-schedule.csv')
    contracts = {r['payer']: r for r in csv.DictReader(open(src / 'contracts.csv'))}
    dept = {r['carc']: (r['reason'], r['department']) for r in csv.DictReader(open(src / 'carc-departments.csv'))}
    files = sorted((src / '835').glob('*.835'))
    raw = [l for f in files for l in read_835(f)]
    per_year = D(365) / D(a.days)

    # Reversals: CLP02 = 22 takes back an earlier payment. Each line keeps its last non-reversal version.
    occ = defaultdict(list)
    for l in raw:
        occ[(l['payer'], l['ref'])].append(l)
    lines, reversals = [], defaultdict(set)
    reversal_net = defaultdict(lambda: D('0'))
    for (payer, ref), ls in occ.items():
        ls.sort(key=lambda l: (l['pay_date'], l['status'] == '22'))
        keep = [l for l in ls if l['status'] != '22']
        rev = [l for l in ls if l['status'] == '22']
        eff = dict(keep[-1])
        eff['first_pay_date'] = keep[0]['pay_date']
        if rev:
            reversals[payer].add(eff['acct'])
            reversal_net[payer] += keep[0]['paid'] - (sum(l['paid'] for l in keep) + sum(l['paid'] for l in rev))   # dollars lost to the pair
            eff['reversed'] = True
        lines.append(classify(eff, fees.on(eff['dos']), a.tolerance))
    for l in lines:
        if l['class'] == 'clean' and l['contract_billed'] is None:
            l['class'] = 'no_contract'
        if l['class'] == 'denied':
            carc = next((k.split('-')[1] for k in l['co'] if k.startswith('CO-') and k != 'CO-45'), 'other')
            l['carc'] = carc
            l['reason'], l['department'] = dept.get(carc, ('Other', 'Other'))
            l['denied_value'] = l['contract_billed']          # priced at contract, unpriced with no contract on file
    dates = sorted(l['dos'] for l in lines)

    by_payer, departments = {}, sorted({d for _, d in dept.values()})
    for payer in sorted({l['payer'] for l in lines}):
        pl = [l for l in lines if l['payer'] == payer]
        c = contracts.get(payer, {})
        on_file = c.get('on_file') == 'yes'
        claims = defaultdict(list)
        for l in pl:
            claims[l['acct']].append(l)
        paid_claims = {k: v for k, v in claims.items() if any(x['paid'] > 0 for x in v)}
        days = [(ymd(v[0]['first_pay_date']) - ymd(v[0]['received'])).days for v in paid_claims.values()]
        den = [l for l in pl if l['class'] == 'denied']
        down = [l for l in pl if l['class'] == 'downcoded']
        short = [l for l in pl if l['class'] == 'underpaid']
        den_claims = {l['acct'] for l in den}
        short_claims = {l['acct'] for l in short}
        slow = sum(1 for d in days if d > SLOW_DAYS)
        touches = dict(denial=len(den_claims), downcode=len(down), short_pay=len(short_claims), reversal=len(reversals[payer]), slow=slow)
        by_dept = {d: dict(claims=len({l['acct'] for l in den if l['department'] == d}),
                           dollars=sum((l['denied_value'] or D('0') for l in den if l['department'] == d), D('0'))) for d in departments}
        by_carc = Counter(l['carc'] for l in den)
        paid = sum((l['paid'] for l in pl), D('0'))
        kept = sum((l['gap'] or D('0') for l in down), D('0')) + sum((l['gap'] for l in short), D('0'))
        minutes = sum(touches[k] * MINUTES[k] for k in touches)
        staff_90 = (D(minutes) / 60 * HOURLY).quantize(D('0.01'))
        t = dict(on_file=on_file, stated_basis=c.get('stated_basis', 'Unknown'), claims=len(claims), lines=len(pl),
                 paid=paid, paid_lines=sum(1 for l in pl if l['paid'] > 0),
                 denied_claims=len(den_claims), denied_lines=len(den),
                 denied_dollars=sum((l['denied_value'] or D('0') for l in den), D('0')) if on_file else None,
                 denials_by_department=by_dept, denials_by_carc=dict(by_carc.most_common()),
                 downcoded_lines=len(down), downcoded_gap=sum((l['gap'] or D('0') for l in down), D('0')),
                 downcodes_at_lower_code_rate=sum(1 for l in down if l.get('allowed_matches_paid_code')),
                 downcodes_with_no_150=sum(1 for l in down if 'CO-150' not in l['co']),
                 short_pay_lines=len(short), short_pay_claims=len(short_claims), short_pay_gap=sum((l['gap'] for l in short), D('0')),
                 days_median=statistics.median(days) if days else None, days_p90=pct(days, 90), slow_claims=slow,
                 reversed_claims=len(reversals[payer]), reversal_net_dollars=reversal_net[payer],
                 touches=touches, staff_minutes_90=minutes, staff_cost_90=staff_90, kept_90=kept)
        if on_file:
            yr = lambda x: (x * per_year).quantize(D('1'))
            t['year'] = dict(paid=yr(paid), kept=yr(kept), staff_cost=yr(staff_90), denied_dollars=yr(t['denied_dollars']),
                             downcoded_gap=yr(t['downcoded_gap']), short_pay_gap=yr(t['short_pay_gap']),
                             staff_hours=(D(minutes) / 60 * per_year).quantize(D('1')),
                             touches={k: int((D(v) * per_year).quantize(D('1'))) for k, v in touches.items()})
            t['year']['fte'] = (t['year']['staff_hours'] / FTE_HOURS).quantize(D('0.01'))
            t['year']['cost'] = t['year']['kept'] + t['year']['staff_cost']
            t['cost_per_100_paid'] = ((kept + staff_90) / paid * 100).quantize(D('0.01')) if paid else None
        else:
            t['note'] = 'No contract on file: named here, never priced.'
        by_payer[payer] = t

    priced = {p: t for p, t in by_payer.items() if t['on_file']}
    total = {k: sum((t['year'][k] for t in priced.values()), D('0')) for k in ('paid', 'kept', 'staff_cost', 'cost', 'denied_dollars', 'staff_hours')}
    total['fte'] = (total['staff_hours'] / FTE_HOURS).quantize(D('0.01'))
    allowed_year = (sum((l['allowed'] for l in lines if l['class'] != 'denied'), D('0')) * per_year).quantize(D('1'))

    # Piles: every flagged line for the page, newest first
    keep = ('payer', 'provider', 'acct', 'dos', 'received', 'first_pay_date', 'billed_code', 'paid_code', 'charge', 'paid',
            'allowed', 'contract_billed', 'contract_paid', 'gap', 'carc', 'reason', 'department', 'denied_value', 'file')
    pick = lambda cls: [{k: l.get(k) for k in keep} for l in sorted((x for x in lines if x['class'] == cls), key=lambda x: x['dos'], reverse=True)]
    slow_rows = []
    for l in lines:
        if l['paid'] > 0 and l['first_pay_date'] and (ymd(l['first_pay_date']) - ymd(l['received'])).days > SLOW_DAYS:
            slow_rows.append({k: l.get(k) for k in keep} | dict(days=(ymd(l['first_pay_date']) - ymd(l['received'])).days))
    slow_rows.sort(key=lambda r: -r['days'])
    rev_rows = [{k: l.get(k) for k in keep} for l in lines if l.get('reversed')]

    summary = dict(files=[f.name for f in files], service_lines=len(lines), raw_segments_lines=len(raw),
                   claims=len({(l['payer'], l['acct']) for l in lines}), providers=len({l['provider'] for l in lines}),
                   payers=len(by_payer), contracts_on_file=len(priced), first_dos=dates[0], last_dos=dates[-1],
                   last_pay_date=max(l['pay_date'] for l in raw), period_days=a.days, classes=dict(Counter(l['class'] for l in lines)),
                   allowed_year=allowed_year, total_year=total,
                   staff_inputs=dict(minutes=MINUTES, hourly=HOURLY, fte_hours=FTE_HOURS, slow_days=SLOW_DAYS, names=TOUCH_NAMES))
    result = dict(summary=summary, by_payer=by_payer, piles=dict(denied=pick('denied'), downcoded=pick('downcoded'),
                  short_pay=pick('underpaid'), slow=slow_rows, reversed=rev_rows))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / 'result.json').write_text(json.dumps(result, indent=1, default=str))
    # Every service line after reversals, for the public page's in-browser engine (added October 28, 2026)
    lk = ('payer', 'acct', 'provider', 'billed_code', 'paid_code', 'dos', 'received', 'first_pay_date', 'pay_date', 'allowed', 'paid',
          'charge', 'class', 'gap', 'carc', 'file', 'ref')
    (out / 'lines.json').write_text(json.dumps([{k: l.get(k) for k in lk} | dict(reversed=bool(l.get('reversed'))) for l in lines], default=str))
    with open(out / 'payer-cost.csv', 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['payer', 'paid_year', 'denied_claims_90', 'denied_dollars_year', 'downcoded_lines_90', 'downcoded_gap_year', 'short_pay_lines_90',
                    'short_pay_gap_year', 'days_median', 'days_p90', 'reversed_claims_90', 'staff_hours_year', 'fte', 'staff_cost_year', 'cost_per_100_paid'])
        for p, t in by_payer.items():
            y = t.get('year', {})
            w.writerow([p, y.get('paid', ''), t['denied_claims'], y.get('denied_dollars', ''), t['downcoded_lines'], y.get('downcoded_gap', ''),
                        t['short_pay_lines'], y.get('short_pay_gap', ''), t['days_median'], t['days_p90'], t['reversed_claims'],
                        y.get('staff_hours', ''), y.get('fte', ''), y.get('staff_cost', ''), t.get('cost_per_100_paid', '')])

    print(f"{len(files)} files, {len(raw)} remit lines, {len(lines)} service lines after reversals, {summary['claims']} claims, {dates[0]} to {dates[-1]}")
    for p, t in by_payer.items():
        y = t.get('year')
        print(f"  {p}: denied {t['denied_claims']} claims, downcoded {t['downcoded_lines']} (${t['downcoded_gap']:,}), short {t['short_pay_lines']} "
              f"(${t['short_pay_gap']:,}), days median {t['days_median']} p90 {t['days_p90']}, reversed {t['reversed_claims']}"
              + (f", cost/yr ${y['cost']:,} ({y['fte']} FTE), per $100 paid {t['cost_per_100_paid']}" if y else ', not priced'))
    print(f"All contracts a year: kept ${total['kept']:,}, staff ${total['staff_cost']:,} ({total['fte']} FTE), cost ${total['cost']:,}; allowed a year ${allowed_year:,}")


if __name__ == '__main__':
    main()
