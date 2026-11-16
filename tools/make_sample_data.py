#!/usr/bin/env python3
"""Write a synthetic 90-day set of X12 835 remits for a sample GI group, plus its contract rates.

Adapted from the contract check demo's make_sample_data.py (seeded RNG, claim builder, adjudicate,
write_835 and the answer key). Everything here is made up: the gastroenterology group, the clinicians,
the patients, the payers, the contracts and every dollar. The "Medicare" rates are rounded sample
figures, not the published schedule.

Planted on purpose (the answer key lists each one):
  Sample Plan A   the cheap contract: pays in about two weeks, few denials, no downcodes.
  Sample Plan B   denies little, but pays about 20% of billed level 4 and 5 office visits one level
                  down, at the full contracted rate for the lower code. A rate check passes every one.
  Sample Plan C   slow: a median of about 46 days from claim received to payment. It reverses and
                  repays a share of claims (CLP02 = 22), and its year-two rates (5% higher from
                  June 1, 2026) were never turned on, so every line paid on or after that date short-pays.
  Sample Plan D   denials cluster in authorization (CARC 197) and eligibility (CARC 27).
Decoys the check has to leave alone: copays, deductibles and coinsurance, a 30-cent rounding difference,
a reversal and same-amount repayment on Plan A, a modifier-only recode on Plan B, and Sample Plan E,
which has no contract on file.

Usage: make_sample_data.py OUT_DIR
Writes OUT_DIR/835/*.835 (one file per payer per weekly check run), fee-schedule.csv (contract rates in
force on each date of service), contracts.csv and answer-key.json.
"""
import csv, json, random, sys
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

SEED = 20261013
START, DAYS = date(2026, 4, 27), 90          # dates of service April 27 to July 25, 2026
LAST_FILE = date(2026, 10, 9)                # the newest remit in the set (a Friday check run)
VISITS = (330, 400)                          # office visits per clinician in the 90 days
SCOPES = (300, 360)                          # endoscopy cases per clinician in the 90 days
YEAR2 = date(2026, 6, 1)                     # Sample Plan C's year-two rates start here

GROUP = dict(name='SAMPLE GI GROUP', npi='9100000031', tin='990000003')
# Rendering clinicians are named SAMPLE DOCTOR 01 to 24 so no row can read as being about a real person
# (changed November 16, 2026 for the payer check repo, the only change from the demo's generator).
PROVIDERS = [(f'92000003{n:02d}', f'DOCTOR {n:02d}', 'SAMPLE') for n in range(1, 25)]
# key: (payer name, payer id, share of claims, copay on office visits, pay lag range in days after receipt)
PAYERS = {
    'A': ('SAMPLE PLAN A', '99110', 0.27, D('30'), (5, 16)),
    'B': ('SAMPLE PLAN B', '99120', 0.22, D('35'), (12, 24)),
    'C': ('SAMPLE PLAN C', '99130', 0.21, D('30'), (28, 66)),
    'D': ('SAMPLE PLAN D', '99140', 0.25, D('20'), (14, 26)),
    'E': ('SAMPLE PLAN E', '99150', 0.05, D('40'), (12, 30)),
}
# code: (plain name, category, sample Medicare rate). Rounded sample figures, not the published schedule.
CODES = {
    '99202': ('New patient visit, level 2', 'em', '73.10'), '99203': ('New patient visit, level 3', 'em', '112.95'),
    '99204': ('New patient visit, level 4', 'em', '168.70'), '99205': ('New patient visit, level 5', 'em', '222.60'),
    '99212': ('Established visit, level 2', 'em', '58.40'), '99213': ('Established visit, level 3', 'em', '92.35'),
    '99214': ('Established visit, level 4', 'em', '130.85'), '99215': ('Established visit, level 5', 'em', '184.20'),
    '45378': ('Colonoscopy, diagnostic', 'scope', '196.40'), '45380': ('Colonoscopy with biopsy', 'scope', '231.75'),
    '45385': ('Colonoscopy with polyp removal by snare', 'scope', '268.30'), '43235': ('Upper endoscopy (EGD), diagnostic', 'scope', '148.60'),
    '43239': ('Upper endoscopy (EGD) with biopsy', 'scope', '166.20'), '43249': ('Upper endoscopy (EGD) with balloon dilation', 'scope', '187.90'),
    '91200': ('Liver elastography', 'other', '28.15'),
}
MEDICARE = {c: D(v[2]) for c, v in CODES.items()}
MULT = {'A': D('1.45'), 'B': D('1.38'), 'C': D('1.40'), 'D': D('1.30')}   # contract as a multiple of sample Medicare
C_YEAR2 = D('1.05')
q = lambda x: D(x).quantize(D('0.01'))

CONTRACTS = [  # payer key, stated basis, effective, term end, on file
    ('A', '145% of Medicare', '2025-01-01', '2027-12-31', 'yes'),
    ('B', '138% of Medicare', '2025-04-01', 'evergreen', 'yes'),
    ('C', '140% of Medicare, 5% escalator June 1, 2026', '2025-06-01', '2028-05-31', 'yes'),
    ('D', '130% of Medicare (Medicare Advantage)', '2026-01-01', '2027-12-31', 'yes'),
    ('E', 'Unknown', '', '', 'no'),
]
# CARC: (reason in plain words, department). Standard X12 claim adjustment reason codes.
CARC = {
    '27': ('Coverage ended before the visit', 'Front desk and eligibility'),
    '31': ('Patient not identified as insured', 'Front desk and eligibility'),
    '197': ('Authorization or precertification absent', 'Authorization'),
    '15': ('Authorization number missing or invalid', 'Authorization'),
    '11': ('Diagnosis inconsistent with the procedure', 'Coding'),
    '97': ('Bundled into another service', 'Coding'),
    '16': ('Claim missing information', 'Documentation'),
    '50': ('Not deemed medically necessary', 'Documentation'),
}


def contract_rate(pk, code, dos):
    """The rate in the contract library on this date of service (what the payer owes)."""
    if pk not in MULT:
        return None
    r = q(MEDICARE[code] * MULT[pk])
    if pk == 'C' and dos >= YEAR2:
        r = q(r * C_YEAR2)
    return r


def paid_rate(pk, code):
    """The rate the payer actually allows before any planted error."""
    if pk == 'E': return q(MEDICARE[code] * D('1.25'))
    return q(MEDICARE[code] * MULT[pk])                   # Plan C never turned on year two


def charge(code):
    return q((MEDICARE[code] * D('2.8')).quantize(D('1')))


def pick(rng, weights):
    r, acc = rng.random(), 0
    for k, w in weights:
        acc += w
        if r < acc:
            return k
    return weights[-1][0]


SCOPE = [('45378', .16), ('45380', .22), ('45385', .26), ('43235', .08), ('43239', .20), ('43249', .08)]
DOWN = {'99214': '99213', '99215': '99214', '99204': '99203', '99205': '99204'}


def build_claims(rng):
    claims, n = [], 0
    payers = [(k, v[2]) for k, v in PAYERS.items()]
    for prov in PROVIDERS:
        for _ in range(rng.randint(*VISITS)):
            n += 1
            new = rng.random() < 0.30
            em = pick(rng, [('99202', .04), ('99203', .38), ('99204', .48), ('99205', .10)] if new else
                      [('99212', .06), ('99213', .44), ('99214', .42), ('99215', .08)])
            lines = [[em, '']]
            if rng.random() < 0.06: lines.append(['91200', ''])
            claims.append(dict(acct=f'G{50000 + n}', pid=f'GM{700000 + n}', prov=prov, payer=pick(rng, payers),
                               dos=START + timedelta(days=rng.randrange(DAYS)), lines=lines, kind='visit'))
        for _ in range(rng.randint(*SCOPES)):
            n += 1
            claims.append(dict(acct=f'G{50000 + n}', pid=f'GM{700000 + n}', prov=prov, payer=pick(rng, payers),
                               dos=START + timedelta(days=rng.randrange(DAYS)), lines=[[pick(rng, SCOPE), '']], kind='scope'))
    return claims


def plan(rng, claims):
    """Decide each claim's adjudication: the planted findings plus the decoys."""
    for c in claims:
        c['plan'] = {}
    by = lambda pk, kind=None: [c for c in claims if c['payer'] == pk and (kind is None or c['kind'] == kind) and not c['plan']]
    # denials: a light background on every payer, and Plan D's front-end cluster
    background = {'A': 0.012, 'B': 0.014, 'C': 0.022, 'D': 0.012, 'E': 0.02}
    mix = ['11', '97', '16', '50', '31', '15']
    for pk, share in background.items():
        pool = by(pk)
        for c in rng.sample(pool, round(len(pool) * share)):
            c['plan'] = {'deny': rng.choice(mix)}
    pool = by('D', 'scope')
    for c in rng.sample(pool, round(len(pool) * 0.075)):
        c['plan'] = {'deny': '197'}
    pool = by('D')
    for c in rng.sample(pool, round(len(pool) * 0.025)):
        c['plan'] = {'deny': '27'}
    # Plan B: about 20% of billed level 4 and 5 visits paid one level down, at the lower code's full rate
    pool = [c for c in by('B', 'visit') if c['lines'][0][0] in DOWN]
    for c in rng.sample(pool, round(len(pool) * 0.20)):
        c['plan'] = {'down': DOWN[c['lines'][0][0]]}
    # Plan B decoy: a modifier-only recode (billed 99214-25, paid 99214), same level and the full rate
    pool = [c for c in by('B', 'visit') if c['lines'][0][0] == '99214']
    for c in rng.sample(pool, 6):
        c['plan'] = {'modifier_recode': True}
    # Plan C: reversed and repaid at the same amount, a staff touch each
    pool = by('C')
    for c in rng.sample(pool, round(len(pool) * 0.06)):
        c['plan'] = {'reverse': True}
    # Plan A decoys: one reversal and same-amount repayment, one 30-cent rounding difference
    a = by('A')
    picks = rng.sample(a, 2)
    picks[0]['plan'] = {'reverse': True, 'decoy': True}
    picks[1]['plan'] = {'short': D('0.30'), 'rounding': True}
    commercial = [c for c in claims if not c['plan']]
    for c in rng.sample(commercial, round(len(commercial) * 0.10)):
        c['plan'] = {'deductible': True}


def adjudicate(c):
    out, pk = [], c['payer']
    for i, (code, mod) in enumerate(c['lines']):
        ch = charge(code)
        line = dict(code=code, paid_code=code, mod=mod, charge=ch, cas=[], rarc=[])
        if c['plan'].get('modifier_recode') and i == 0:
            line['mod'] = '25'
        if c['plan'].get('deny'):
            line.update(allowed=D('0'), paid=D('0'), cas=[('CO', c['plan']['deny'], ch)])
            out.append(line); continue
        paid_code = c['plan']['down'] if c['plan'].get('down') and i == 0 else code
        line['paid_code'] = paid_code
        allowed = paid_rate(pk, paid_code)
        if c['plan'].get('short') and i == 0:
            allowed -= c['plan']['short']
        pr = []
        if i == 0 and CODES[code][1] == 'em':
            pr.append(('PR', '3', min(PAYERS[pk][3], allowed)))
        if c['plan'].get('deductible') and i == len(c['lines']) - 1:
            left = allowed - sum(x[2] for x in pr)
            pr.append(('PR', '1', q(min(left, D('250')))))
        if c['kind'] == 'scope' and pk == 'D':
            pr.append(('PR', '2', q((allowed - sum(x[2] for x in pr)) * D('0.10'))))
        line['cas'] = [('CO', '45', q(ch - allowed))] + [x for x in pr if x[2]]
        line.update(allowed=q(allowed), paid=q(allowed - sum(x[2] for x in pr)))
        out.append(line)
    return out


def seg(*els):
    return '*'.join(str(e) for e in els).rstrip('*') + '~'


def neg(x):
    return q(-x)


def claim_segs(c, lines, status, sign=1):
    pid = PAYERS[c['payer']][1]
    m = (lambda x: x) if sign == 1 else neg
    ch, paid = sum(l['charge'] for l in lines), sum(l['paid'] for l in lines)
    pr = sum(x[2] for l in lines for x in l['cas'] if x[0] == 'PR')
    s = [seg('CLP', c['acct'], status, m(ch), m(paid), m(pr) if pr else '', '12', f"{pid}{c['acct'][1:]}0", '11' if c['kind'] == 'visit' else '22', '1'),
         seg('NM1', 'QC', '1', 'SAMPLE', 'PATIENT', '', '', '', 'MI', c['pid']),
         seg('NM1', '82', '1', c['prov'][1], c['prov'][2], '', '', '', 'XX', c['prov'][0]),
         seg('DTM', '050', c['received'].strftime('%Y%m%d'))]
    for n, l in enumerate(lines, 1):
        billed = 'HC:' + l['code'] + (':' + l['mod'] if l['mod'] else '')
        paidp = 'HC:' + l['paid_code']
        if billed != paidp:
            s.append(seg('SVC', paidp, m(l['charge']), m(l['paid']), '', '1', billed))
        else:
            s.append(seg('SVC', paidp, m(l['charge']), m(l['paid']), '', '1'))
        s.append(seg('DTM', '472', c['dos'].strftime('%Y%m%d')))
        for grp in ('CO', 'PR'):
            adj = [x for x in l['cas'] if x[0] == grp and x[2]]
            if adj:
                s.append(seg('CAS', grp, *[e for x in adj for e in (x[1], m(x[2]), '')]))
        s.append(seg('REF', '6R', f"{c['acct']}-{n}"))
        if l['allowed']:
            s.append(seg('AMT', 'B6', m(l['allowed'])))
    return s, (paid if sign == 1 else -paid)


def write_835(path, pk, check_no, pay_date, entries, ctrl):
    """entries: list of (claim, kind) where kind is 'pay', 'reverse' or 'repay'."""
    name, pid = PAYERS[pk][:2]
    out, total = [], D('0')
    for c, kind in sorted(entries, key=lambda e: (e[0]['dos'], e[0]['acct'], e[1])):
        lines = c['adj']
        denied = all(l['paid'] == 0 and l['allowed'] == 0 for l in lines)
        if kind == 'reverse':
            s, amt = claim_segs(c, lines, '22', sign=-1)
        else:
            s, amt = claim_segs(c, lines, '4' if denied else '1')
        total += amt
        out.extend(s)
    head = [seg('ST', '835', '0001'),
            seg('BPR', 'I', q(total), 'C', 'ACH', 'CCP', '01', '999999999', 'DA', '000111222', '1' + pid.zfill(9), '', '01', '999999998', 'DA', '000333444', pay_date.strftime('%Y%m%d')),
            seg('TRN', '1', check_no, '1' + pid.zfill(9)), seg('DTM', '405', pay_date.strftime('%Y%m%d')),
            seg('N1', 'PR', name), seg('N3', '100 SAMPLE PLAZA'), seg('N4', 'SAMPLETOWN', 'NC', '28801'),
            seg('PER', 'BL', 'PROVIDER SERVICES', 'TE', '8005550100'),
            seg('N1', 'PE', GROUP['name'], 'XX', GROUP['npi']), seg('N3', '200 EXAMPLE AVE'), seg('N4', 'SAMPLETOWN', 'NC', '28801'),
            seg('REF', 'TJ', GROUP['tin']), seg('LX', '1')]
    st = head + out
    st.append(seg('SE', len(st) + 1, '0001'))
    stamp = pay_date.strftime('%y%m%d')
    isa = f"ISA*00*          *00*          *ZZ*{pid:<15}*ZZ*{'SAMPLEGRP':<15}*{stamp}*1200*^*00501*{ctrl:09d}*0*T*:~"
    path.write_text('\n'.join([isa, seg('GS', 'HP', pid, 'SAMPLEGRP', pay_date.strftime('%Y%m%d'), '1200', ctrl, 'X', '005010X221A1')] + st
                              + [seg('GE', '1', ctrl), seg('IEA', '1', f'{ctrl:09d}')]) + '\n')


def friday(d):
    """Each payer cuts checks on Fridays: snap a target date forward to the next Friday."""
    return d + timedelta(days=(4 - d.weekday()) % 7)


def main(out_dir):
    rng = random.Random(SEED)
    out = Path(out_dir)
    (out / '835').mkdir(parents=True, exist_ok=True)
    for old in (out / '835').glob('*.835'):
        old.unlink()
    claims = build_claims(rng)
    plan(rng, claims)
    events = []   # (payer, pay date, claim, kind)
    for c in claims:
        c['adj'] = adjudicate(c)
        c['received'] = c['dos'] + timedelta(days=rng.randint(1, 4))
        lo, hi = PAYERS[c['payer']][4]
        lag = rng.randint(lo, hi) if c['payer'] != 'C' else round(rng.triangular(lo, hi, 40))
        c['paid_on'] = friday(c['received'] + timedelta(days=lag))
        assert c['paid_on'] <= LAST_FILE, (c['acct'], c['paid_on'])
        events.append((c['payer'], c['paid_on'], c, 'pay'))
        if c['plan'].get('reverse'):
            c['reversed_on'] = friday(c['paid_on'] + timedelta(days=rng.randint(14, 28)))
            if c['reversed_on'] > LAST_FILE:          # would land after the newest file: no reversal in this set
                c['plan'] = {}; c['adj'] = adjudicate(c); continue
            events.append((c['payer'], c['reversed_on'], c, 'reverse'))
            events.append((c['payer'], c['reversed_on'], c, 'repay'))
    ctrl = 3001
    for pk in PAYERS:
        for d in sorted({e[1] for e in events if e[0] == pk}):
            batch = [(e[2], e[3]) for e in events if e[0] == pk and e[1] == d]
            write_835(out / '835' / f"sample-{pk.lower()}-{d.isoformat()}.835", pk, f'SAMPLE{ctrl}', d, batch, ctrl)
            ctrl += 1
    with open(out / 'fee-schedule.csv', 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['payer', 'cpt', 'description', 'contracted_allowed', 'effective'])
        for pk, p in PAYERS.items():
            for code in CODES:
                r = contract_rate(pk, code, START)
                if r is not None:
                    w.writerow([p[0], code, CODES[code][0], r, '2025-01-01'])
                if pk == 'C':
                    w.writerow([p[0], code, CODES[code][0], contract_rate(pk, code, YEAR2), YEAR2.isoformat()])
    with open(out / 'contracts.csv', 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['payer', 'stated_basis', 'effective', 'term_end', 'on_file'])
        for pk, *rest in CONTRACTS:
            w.writerow([PAYERS[pk][0], *rest])
    with open(out / 'carc-departments.csv', 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['carc', 'reason', 'department'])
        for k, (reason, dept) in CARC.items():
            w.writerow([k, reason, dept])

    # Answer key
    nm = lambda pk: PAYERS[pk][0]
    downs = [dict(acct=c['acct'], billed=c['lines'][0][0], paid_as=c['plan']['down'], payer=nm(c['payer']),
                  gap=str(contract_rate(c['payer'], c['lines'][0][0], c['dos']) - paid_rate(c['payer'], c['plan']['down'])))
             for c in claims if c['plan'].get('down')]
    shorts = [dict(acct=c['acct'], code=l['code'], payer=nm('C'), gap=str(contract_rate('C', l['code'], c['dos']) - l['allowed']))
              for c in claims if c['payer'] == 'C' and c['dos'] >= YEAR2 and not c['plan'].get('deny') for l in c['adj']]
    denials = {}
    for c in claims:
        if c['plan'].get('deny'):
            k = f"{nm(c['payer'])}|{c['plan']['deny']}"
            denials[k] = denials.get(k, 0) + 1
    key = dict(downcoded_lines=len(downs), downcoded_gap=str(sum(D(x['gap']) for x in downs)), downcoded=downs,
               short_pay_lines=len(shorts), short_pay_gap=str(sum(D(x['gap']) for x in shorts)), short_pay_payer=nm('C'), short_pays=shorts,
               denied_claims_by_payer_carc=denials,
               reversed_claims={nm(pk): sum(1 for c in claims if c['payer'] == pk and c['plan'].get('reverse')) for pk in PAYERS},
               not_findings=dict(rounding_acct=[c['acct'] for c in claims if c['plan'].get('rounding')][0],
                                 decoy_reversal_acct=[c['acct'] for c in claims if c['plan'].get('decoy')][0],
                                 modifier_recode_accts=[c['acct'] for c in claims if c['plan'].get('modifier_recode')],
                                 deductible_claims=sum(1 for c in claims if c['plan'].get('deductible')),
                                 no_contract_payer=nm('E')),
               lag_days={nm(pk): list(PAYERS[pk][4]) for pk in PAYERS},
               note='Answer key for testing the check. Synthetic sample data.')
    (out / 'answer-key.json').write_text(json.dumps(key, indent=1))
    lines = sum(len(c['lines']) for c in claims)
    print(f'{len(claims)} claims, {lines} service lines, {len(downs)} planted downcodes, {len(shorts)} planted short pays, '
          f'{sum(denials.values())} denied claims, {len(list((out / "835").glob("*.835")))} files in {out / "835"}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'sample-data')
