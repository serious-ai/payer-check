#!/usr/bin/env python3
"""Write a synthetic 90-day set of X12 835 remits for a sample anesthesia group, plus its contract terms.

Everything here is made up: the group, the anesthesiologists and nurse anesthetists, the patients, the payers,
the conversion factors and every dollar. The ten base units match common published values so the math looks
real, but base-units.csv is not a reference table, and "Sample Plan F" is not Medicare. Seeded, so anyone can rebuild the files and confirm they are made up.
Adapted from tools/make_sample_data.py (same claim builder, 835 writer and answer key pattern).

Planted on purpose (answer-key.json lists each line and its gap):
  Sample Plan G   its conversion factor rose from 62.00 to 64.50 on July 15, 2026, and it kept paying 62.00.
  Sample Plan H   from August 1, 2026 it cuts every case's time to whole 15-minute units, where its contract
                  counts time to one decimal place.
  Sample Plan J   pays 00813 at 4 base units instead of 5, pays about a third of long cases at 120 minutes
                  whatever the record shows, and pays some nurse anesthetist cases billed QZ (on their own,
                  100%) as QX (medically directed, 50%).
Decoys the check has to leave alone: medically directed cases paid at 50% (QK, QY, QX) on every plan, Plan H's
QZ cases paid at the 85% its contract sets, Plan H's labor epidurals capped at the 435 minutes its contract sets,
Plan G's physical status units (P3 and P4) paid as agreed, coinsurance and deductibles, a 30-cent rounding
difference on Plan F, a reversal and same-amount repayment on Plan G, and Sample Plan K, which has no contract
on file.

Usage: make_anesthesia_sample.py OUT_DIR
Writes OUT_DIR/835/*.835, fee-schedule.csv, base-units.csv and answer-key.json.
"""
import csv, json, random, sys
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_FLOOR
from pathlib import Path

SEED = 20261116
START, DAYS = date(2026, 6, 1), 90              # dates of service June 1 to August 29, 2026
G_RAISE, H_TRUNC = date(2026, 7, 15), date(2026, 8, 1)
CASES = 1500

GROUP = dict(name='SAMPLE ANESTHESIA GROUP', npi='9100000041', tin='990000004')
DOCTORS = [(f'92000004{n:02d}', f'DOCTOR {n:02d}', 'SAMPLE') for n in range(1, 7)]
NURSES = [(f'92000005{n:02d}', f'NURSE ANESTHETIST {n:02d}', 'SAMPLE') for n in range(1, 11)]
# key: (payer name, payer id, share of cases, coinsurance share, pay lag range in days after receipt)
PAYERS = {
    'F': ('SAMPLE PLAN F', '99160', 0.30, D('0.20'), (10, 18)),
    'G': ('SAMPLE PLAN G', '99170', 0.24, D('0.10'), (14, 25)),
    'H': ('SAMPLE PLAN H', '99180', 0.20, D('0.10'), (15, 30)),
    'J': ('SAMPLE PLAN J', '99190', 0.20, D('0.10'), (20, 40)),
    'K': ('SAMPLE PLAN K', '99200', 0.06, D('0.10'), (15, 30)),
}
# code: (plain name, base units, minutes range, share of cases). Common published base units, not a reference table.
CODES = {
    '00731': ('Anesthesia for upper GI endoscopy', 5, (15, 40), .14),
    '00811': ('Anesthesia for lower GI endoscopy', 4, (20, 45), .16),
    '00813': ('Anesthesia for upper and lower GI endoscopy together', 5, (30, 60), .10),
    '00790': ('Anesthesia for upper abdominal surgery', 7, (75, 210), .10),
    '00840': ('Anesthesia for lower abdominal surgery', 6, (60, 160), .08),
    '01402': ('Anesthesia for knee replacement', 7, (90, 170), .08),
    '01630': ('Anesthesia for shoulder surgery', 5, (60, 150), .08),
    '00142': ('Anesthesia for eye lens surgery', 4, (12, 35), .12),
    '00400': ('Anesthesia for skin procedures', 3, (20, 70), .06),
    '01967': ('Labor epidural', 5, (150, 620), .08),
}
# contract terms by payer, as they appear in the fee schedule
TERMS = {
    'F': [dict(cf='20.80', effective='2025-01-01')],
    'G': [dict(cf='62.00', effective='2025-01-01', ps='P3=1 P4=2 P5=3'),
          dict(cf='64.50', effective=G_RAISE.isoformat(), ps='P3=1 P4=2 P5=3')],
    'H': [dict(cf='58.00', effective='2025-01-01', pct='QZ=85', caps='01967=435')],
    'J': [dict(cf='55.00', effective='2025-01-01')],
}
PCT = dict(AA=100, QZ=100, QK=50, QY=50, QX=50)
PS_UNITS = {'G': dict(P3=1, P4=2, P5=3)}
CHARGE_PER_UNIT = D('90')
q = lambda x: D(x).quantize(D('0.01'), ROUND_HALF_EVEN)
DENY = ['197', '16', '27', '11']


def pick(rng, weights):
    r, acc = rng.random(), 0
    for k, w in weights:
        acc += w
        if r < acc:
            return k
    return weights[-1][0]


def contract(pk, dos):
    rows = [t for t in TERMS.get(pk, []) if t['effective'] <= dos.isoformat()]
    return rows[-1] if rows else None


def units(pk, code, ps, minutes, time_rule='tenth', base=None, cap=True):
    """Base + time + physical status units, as the contract (or a planted error) counts them."""
    b = CODES[code][1] if base is None else base
    m = D(minutes)
    if cap and pk == 'H' and code == '01967':
        m = min(m, D(435))
    t = (m / 15).quantize(D('0.1'), ROUND_HALF_UP) if time_rule == 'tenth' else (m / 15).quantize(D('1'), ROUND_FLOOR)
    return b + t + PS_UNITS.get(pk, {}).get(ps, 0)


def pct(pk, mod):
    return 85 if pk == 'H' and mod == 'QZ' else PCT[mod]


def expected(pk, code, mod, ps, minutes, dos):
    t = contract(pk, dos)
    if t is None:
        return None
    return q(units(pk, code, ps, minutes) * D(t['cf']) * pct(pk, mod) / 100)


def build(rng):
    """Cases, then the claim lines: one per anesthesia provider (a directed case bills twice)."""
    claims, n = [], 0
    payers = [(k, v[2]) for k, v in PAYERS.items()]
    codes = [(k, v[3]) for k, v in CODES.items()]
    for case in range(CASES):
        code = pick(rng, codes)
        lo, hi = CODES[code][2]
        minutes = rng.randint(lo, hi)
        ps = pick(rng, [('P1', .20), ('P2', .50), ('P3', .25), ('P4', .05)])
        payer = pick(rng, payers)
        dos = START + timedelta(days=rng.randrange(DAYS))
        how = 'AA' if code == '01967' and rng.random() < .5 else pick(rng, [('AA', .25), ('QZ', .25), ('directed', .50)])
        if how == 'directed':
            md = 'QY' if rng.random() < .2 else 'QK'
            parts = [(md, rng.choice(DOCTORS)), ('QX', rng.choice(NURSES))]
        else:
            parts = [(how, rng.choice(DOCTORS) if how == 'AA' else rng.choice(NURSES))]
        for mod, prov in parts:
            n += 1
            claims.append(dict(acct=f'N{60000 + n}', pid=f'NM{800000 + n}', case=case, prov=prov, payer=payer, dos=dos,
                               code=code, mod=mod, ps=ps, minutes=minutes, plan={}))
    return claims


def plan(rng, claims):
    for c in claims:
        c['plan'] = {}
    free = lambda pk: [c for c in claims if c['payer'] == pk and not c['plan']]
    for pk in PAYERS:
        pool = free(pk)
        for c in rng.sample(pool, round(len(pool) * 0.025)):
            c['plan'] = {'deny': rng.choice(DENY)}
    # Plan J: QZ recoded to QX, long cases cut to 120 minutes, 00813 at 4 base units (kept apart for a clean key)
    pool = [c for c in free('J') if c['mod'] == 'QZ']
    for c in rng.sample(pool, round(len(pool) * 0.12)):
        c['plan'] = {'recode': 'QX'}
    pool = [c for c in free('J') if c['minutes'] >= 150]
    for c in rng.sample(pool, round(len(pool) * 0.33)):
        c['plan'] = {'cap': 120}
    for c in free('J'):
        if c['code'] == '00813':
            c['plan'] = {'base': 4}
    # decoys
    rng.choice(free('F'))['plan'] = {'short': D('0.30'), 'rounding': True}
    rng.choice([c for c in free('G') if c['dos'] < G_RAISE])['plan'] = {'reverse': True, 'decoy': True}
    for pk in ('G', 'H', 'J', 'K'):
        pool = free(pk)
        for c in rng.sample(pool, round(len(pool) * 0.08)):
            c['plan'] = {'deductible': True}


def adjudicate(c):
    """What the payer allowed and paid on the line, and the minutes and modifier it paid at."""
    pk, p = c['payer'], c['plan']
    plain_units = CODES[c['code']][1] + (D(c['minutes']) / 15).quantize(D('0.1'), ROUND_HALF_UP)
    ch = q(plain_units * CHARGE_PER_UNIT)
    a = dict(charge=ch, paid_mod=c['mod'], minutes_paid=c['minutes'], cas=[])
    if p.get('deny'):
        a.update(allowed=D('0'), paid=D('0'), cas=[('CO', p['deny'], ch)])
        return a
    if pk == 'K':          # no contract on file: it pays its own way
        allowed = q(plain_units * D('45.00') * PCT[c['mod']] / 100)
    else:
        t = contract(pk, c['dos'])
        cf = D('62.00') if pk == 'G' else D(t['cf'])                    # Plan G never turned on its raise
        rule = 'down' if pk == 'H' and c['dos'] >= H_TRUNC else 'tenth'
        minutes, mod, base = c['minutes'], c['mod'], None
        if p.get('cap'):
            minutes = a['minutes_paid'] = p['cap']
        if p.get('recode'):
            mod = a['paid_mod'] = p['recode']
        if p.get('base'):
            base = p['base']
        allowed = q(units(pk, c['code'], c['ps'], minutes, rule, base) * cf * pct(pk, mod) / 100)
        if p.get('short'):
            allowed -= p['short']
        if pk == 'H' and c['code'] == '01967':
            a['minutes_paid'] = min(c['minutes'], 435)
    pr = []
    if p.get('deductible'):
        pr.append(('PR', '1', q(min(allowed, D('250')))))
    co_ins = q((allowed - sum(x[2] for x in pr)) * PAYERS[pk][3])
    if co_ins:
        pr.append(('PR', '2', co_ins))
    a['cas'] = [('CO', '45', q(ch - allowed))] + [x for x in pr if x[2]]
    a.update(allowed=allowed, paid=q(allowed - sum(x[2] for x in pr)))
    return a


def seg(*els):
    return '*'.join(str(e) for e in els).rstrip('*') + '~'


def claim_segs(c, status, sign=1):
    a, pid = c['adj'], PAYERS[c['payer']][1]
    m = (lambda x: x) if sign == 1 else (lambda x: q(-x))
    pr = sum(x[2] for x in a['cas'] if x[0] == 'PR')
    s = [seg('CLP', c['acct'], status, m(a['charge']), m(a['paid']), m(pr) if pr else '', '12', f"{pid}{c['acct'][1:]}0", '22', '1'),
         seg('NM1', 'QC', '1', 'SAMPLE', 'PATIENT', '', '', '', 'MI', c['pid']),
         seg('NM1', '82', '1', c['prov'][1], c['prov'][2], '', '', '', 'XX', c['prov'][0]),
         seg('DTM', '050', c['received'].strftime('%Y%m%d'))]
    billed = f"HC:{c['code']}:{c['mod']}:{c['ps']}"
    paid = f"HC:{c['code']}:{a['paid_mod']}:{c['ps']}"
    els = ['SVC', paid, m(a['charge']), m(a['paid']), '', a['minutes_paid']]
    if paid != billed or a['minutes_paid'] != c['minutes']:          # SVC06 when recoded, SVC07 when units differ
        els += [billed if paid != billed else '', c['minutes'] if a['minutes_paid'] != c['minutes'] else '']
    s.append(seg(*els))
    s.append(seg('DTM', '472', c['dos'].strftime('%Y%m%d')))
    for grp in ('CO', 'PR'):
        adj = [x for x in a['cas'] if x[0] == grp and x[2]]
        if adj:
            s.append(seg('CAS', grp, *[e for x in adj for e in (x[1], m(x[2]), '')]))
    s.append(seg('REF', '6R', f"{c['acct']}-1"))
    if a['allowed']:
        s.append(seg('AMT', 'B6', m(a['allowed'])))
    return s, (a['paid'] if sign == 1 else -a['paid'])


def write_835(path, pk, check_no, pay_date, entries, ctrl):
    name, pid = PAYERS[pk][:2]
    out, total = [], D('0')
    for c, kind in sorted(entries, key=lambda e: (e[0]['dos'], e[0]['acct'], e[1])):
        denied = c['adj']['paid'] == 0 and c['adj']['allowed'] == 0
        s, amt = claim_segs(c, '22', -1) if kind == 'reverse' else claim_segs(c, '4' if denied else '1')
        total += amt
        out.extend(s)
    head = [seg('ST', '835', '0001'),
            seg('BPR', 'I', q(total), 'C', 'ACH', 'CCP', '01', '999999999', 'DA', '000111222', '1' + pid.zfill(9), '', '01', '999999998', 'DA', '000333444', pay_date.strftime('%Y%m%d')),
            seg('TRN', '1', check_no, '1' + pid.zfill(9)), seg('DTM', '405', pay_date.strftime('%Y%m%d')),
            seg('N1', 'PR', name), seg('N3', '100 SAMPLE PLAZA'), seg('N4', 'SAMPLETOWN', 'NC', '28801'),
            seg('PER', 'BL', 'PROVIDER SERVICES', 'TE', '8005550100'),
            seg('N1', 'PE', GROUP['name'], 'XX', GROUP['npi']), seg('N3', '300 EXAMPLE AVE'), seg('N4', 'SAMPLETOWN', 'NC', '28801'),
            seg('REF', 'TJ', GROUP['tin']), seg('LX', '1')]
    st = head + out
    st.append(seg('SE', len(st) + 1, '0001'))
    stamp = pay_date.strftime('%y%m%d')
    isa = f"ISA*00*          *00*          *ZZ*{pid:<15}*ZZ*{'SAMPLEANES':<15}*{stamp}*1200*^*00501*{ctrl:09d}*0*T*:~"
    path.write_text('\n'.join([isa, seg('GS', 'HP', pid, 'SAMPLEANES', pay_date.strftime('%Y%m%d'), '1200', ctrl, 'X', '005010X221A1')] + st
                              + [seg('GE', '1', ctrl), seg('IEA', '1', f'{ctrl:09d}')]) + '\n')


def friday(d):
    return d + timedelta(days=(4 - d.weekday()) % 7)


def main(out_dir):
    rng = random.Random(SEED)
    out = Path(out_dir)
    (out / '835').mkdir(parents=True, exist_ok=True)
    for old in (out / '835').glob('*.835'):
        old.unlink()
    claims = build(rng)
    plan(rng, claims)
    events = []
    for c in claims:
        c['adj'] = adjudicate(c)
        c['received'] = c['dos'] + timedelta(days=rng.randint(2, 6))
        lo, hi = PAYERS[c['payer']][4]
        c['paid_on'] = friday(c['received'] + timedelta(days=rng.randint(lo, hi)))
        events.append((c['payer'], c['paid_on'], c, 'pay'))
        if c['plan'].get('reverse'):
            when = friday(c['paid_on'] + timedelta(days=21))
            events += [(c['payer'], when, c, 'reverse'), (c['payer'], when, c, 'repay')]
    ctrl = 5001
    for pk in PAYERS:
        for d in sorted({e[1] for e in events if e[0] == pk}):
            batch = [(e[2], e[3]) for e in events if e[0] == pk and e[1] == d]
            write_835(out / '835' / f"sample-anes-{pk.lower()}-{d.isoformat()}.835", pk, f'SAMPLE{ctrl}', d, batch, ctrl)
            ctrl += 1

    with open(out / 'fee-schedule.csv', 'w', newline='') as fh:
        w = csv.writer(fh, lineterminator='\n')
        w.writerow(['payer', 'cpt', 'description', 'contracted_allowed', 'conversion_factor', 'modifier_pct',
                    'physical_status_units', 'max_minutes', 'effective'])
        for pk, rows in TERMS.items():
            for t in rows:
                w.writerow([PAYERS[pk][0], '', 'Anesthesia, every code', '', t['cf'], t.get('pct', ''), t.get('ps', ''),
                            t.get('caps', ''), t['effective']])
    with open(out / 'base-units.csv', 'w', newline='') as fh:
        w = csv.writer(fh, lineterminator='\n')
        w.writerow(['source', 'code', 'description', 'base_units'])
        for code, v in CODES.items():
            w.writerow(['Sample data', code, v[0], v[1]])

    # Answer key: every line short of its contract on purpose, with the reason and the gap
    nm = lambda pk: PAYERS[pk][0]
    planted = []
    for c in claims:
        p, a = c['plan'], c['adj']
        if p.get('deny') or c['payer'] == 'K' or p.get('rounding'):
            continue
        e = expected(c['payer'], c['code'], c['mod'], c['ps'], c['minutes'], c['dos'])
        gap = e - a['allowed']
        reason = ('md_cut' if p.get('recode') else 'time_cut' if p.get('cap') else 'base_short' if p.get('base')
                  else 'cf_short' if c['payer'] == 'G' and c['dos'] >= G_RAISE
                  else 'time_cut' if c['payer'] == 'H' and c['dos'] >= H_TRUNC else None)
        if reason and gap > D('0.50'):
            planted.append(dict(acct=c['acct'], payer=nm(c['payer']), code=c['code'], reason=reason, expected=str(e),
                                allowed=str(a['allowed']), gap=str(gap)))
        else:
            assert abs(gap) <= D('0.02'), (c['acct'], c['plan'], e, a['allowed'])
    by_reason = {}
    for x in planted:
        r = by_reason.setdefault(f"{x['payer']}|{x['reason']}", dict(lines=0, gap='0'))
        r['lines'] += 1
        r['gap'] = str(D(r['gap']) + D(x['gap']))
    denials = {}
    for c in claims:
        if c['plan'].get('deny'):
            k = f"{nm(c['payer'])}|{c['plan']['deny']}"
            denials[k] = denials.get(k, 0) + 1
    clean = lambda c: not c['plan'] and not (c['payer'] == 'G' and c['dos'] >= G_RAISE) and not (c['payer'] == 'H' and c['dos'] >= H_TRUNC)
    key = dict(planted_lines=len(planted), planted_gap=str(sum(D(x['gap']) for x in planted)), by_payer_and_reason=by_reason,
               planted=planted, denied_claims_by_payer_carc=denials,
               not_findings=dict(rounding_acct=[c['acct'] for c in claims if c['plan'].get('rounding')][0],
                                 decoy_reversal_acct=[c['acct'] for c in claims if c['plan'].get('decoy')][0],
                                 directed_at_50_accts=[c['acct'] for c in claims if c['mod'] in ('QK', 'QY', 'QX')
                                                       and c['payer'] != 'K' and clean(c)],
                                 plan_h_qz_at_85_accts=[c['acct'] for c in claims if c['payer'] == 'H' and c['mod'] == 'QZ' and clean(c)],
                                 plan_h_capped_epidural_accts=[c['acct'] for c in claims if c['payer'] == 'H' and c['code'] == '01967'
                                                               and c['minutes'] > 435 and clean(c)],
                                 plan_g_physical_status_accts=[c['acct'] for c in claims if c['payer'] == 'G' and c['ps'] in ('P3', 'P4')
                                                               and clean(c)],
                                 no_contract_payer=nm('K')),
               note='Answer key for testing the check. Synthetic sample data.')
    (out / 'answer-key.json').write_text(json.dumps(key, indent=1) + '\n')
    print(f"{len({c['case'] for c in claims})} cases, {len(claims)} claim lines, {len(planted)} planted short lines, "
          f"{sum(denials.values())} denied, {len(list((out / '835').glob('*.835')))} files in {out / '835'}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'sample-data/anesthesia')
