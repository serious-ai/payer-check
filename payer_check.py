#!/usr/bin/env python3
"""Payer check v0.1: what each payer costs a practice, read from its own 835 remits and fee schedule.

Runs on this computer only. Standard library only, no network code, no third-party packages.
payer-check.html runs the same steps in the browser, and tests/test_page.py checks that both write
byte-identical CSVs from the same files.

Adapted from the October 28, 2026 public demo (downcoding_check.py and payer_cost.py, which read
835s with pyx12). The 835 reader here is plain code so the page and this CLI share one logic, and
tests/test_cli.py checks it line for line against the pyx12 reader on the sample remits.

Usage:
    python3 payer_check.py REMIT_FOLDER FEE_SCHEDULE.csv [--out DIR]

REMIT_FOLDER   every file in it (and its subfolders) that starts with an ISA segment is read as an 835
FEE_SCHEDULE   CSV with columns payer, cpt, contracted_allowed and an optional effective date
               (YYYY-MM-DD). The payer name must match the 835's N1*PR name, case aside.

Writes three files to DIR (default payer-check-output):
    summary-by-payer.csv            one row per payer, no patient-level fields
    summary-by-payer-and-code.csv   one row per payer and billed code, no patient-level fields
    chart-pull-list.csv             every flagged line with its patient account number. Keep it on this computer.

The patient's name (NM1*QC) is never read.
"""
import argparse, csv, io, sys
from datetime import date
from pathlib import Path

VERSION = '0.1.0'
EM_FAMILIES = {'9920': 'New patient', '9921': 'Established', '9924': 'Office consult'}
TOUCHES = ('denial', 'downcode', 'short_pay', 'reversal', 'slow')
# Illustrative staff and write-off inputs, from the public demo's engine.py. Each one is a flag below.
DEFAULTS = dict(minutes=dict(denial=20, downcode=15, short_pay=5, reversal=10, slow=6), hourly='30.00',
                writeoff_pct='46.0', slow_days=30, tolerance='0.50')
EXTENSIONS = ('.835', '.txt', '.edi', '.x12', '.era', '.rmt')


# ---------- money and dates, all in integers ----------
def div_half_even(n, d):
    """round(n / d) to the nearest integer, ties to even, for integers n >= 0 and d > 0."""
    q, r = divmod(n, d)
    if 2 * r > d or (2 * r == d and q % 2 == 1):
        q += 1
    return q


def to_cents(s):
    """'153.19' to 15319, exactly, from the text. Ties past two decimals round to even. Blank is 0."""
    s = (s or '').strip().replace('$', '').replace(',', '')
    if not s:
        return 0
    neg = s.startswith('-')
    s = s.lstrip('+-')
    whole, _, frac = s.partition('.')
    if not (whole or frac) or not (whole + frac).isdigit():
        raise ValueError(f'not an amount: {s!r}')
    n = int(whole or '0') * 100 + int((frac + '00')[:2])
    rest = frac[2:]
    if rest and (rest[0] > '5' or (rest[0] == '5' and (rest[1:].strip('0') or n % 2 == 1))):
        n += 1
    return -n if neg else n


def money(c):
    """15319 to '153.19'."""
    sign = '-' if c < 0 else ''
    c = abs(c)
    return f'{sign}{c // 100}.{c % 100:02d}'


def ymd(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def days_between(a, b):
    return (ymd(b) - ymd(a)).days


def half(m2):
    """A median held as twice its value, written out: 94 is '47', 93 is '46.5'."""
    return f'{m2 // 2}.5' if m2 % 2 else str(m2 // 2)


def em_level(code):
    if code and len(code) == 5 and code[:4] in EM_FAMILIES and code[4] in '12345':
        return EM_FAMILIES[code[:4]], int(code[4])
    return None


# ---------- 835 reader ----------
def segments(text):
    """Split one 835 file into lists of elements. Separators come from the ISA segment."""
    text = text.lstrip('﻿ \t\r\n')
    if not text.startswith('ISA') or len(text) < 106:
        return []
    el, cm, term = text[3], text[104], text[105]
    out = []
    for raw in text.split(term):
        raw = raw.strip('\r\n\t ')
        if raw:
            out.append([e.split(cm) if cm in e else e for e in raw.split(el)])
    return out


def val(seg, i):
    """Element i (1-based, as in the X12 guides) as text, or '' when absent. A composite gives its first part."""
    if i >= len(seg):
        return ''
    v = seg[i]
    return v[0] if isinstance(v, list) else v


def comp(seg, i, j):
    """Component j (1-based) of composite element i, or ''."""
    if i >= len(seg):
        return ''
    v = seg[i]
    if not isinstance(v, list):
        return v if j == 1 else ''
    return v[j - 1] if j - 1 < len(v) else ''


def read_835(name, text):
    """One dict per service line, in file order. Mirrors downcoding_check.read_835 from the demo."""
    lines = []
    payer = check = pay_date = prod_date = ''
    claim = line = None

    def close():
        nonlocal line
        if line is not None:
            lines.append(line)
            line = None

    for seg in segments(text):
        sid = seg[0]
        if sid == 'ST':
            close()
            payer = check = pay_date = prod_date = ''
            claim = None
        elif sid == 'N1' and val(seg, 1) == 'PR':
            payer = val(seg, 2).strip().upper()
        elif sid == 'TRN':
            check = val(seg, 2)
        elif sid == 'BPR':
            pay_date = val(seg, 16)
        elif sid == 'DTM' and claim is None and val(seg, 1) == '405':
            prod_date = val(seg, 2)
        elif sid == 'CLP':
            close()
            claim = dict(acct=val(seg, 1), status=val(seg, 2), provider='', received='', claim_dos='', n=0)
        elif sid == 'NM1' and claim is not None and val(seg, 1) == '82':
            claim['provider'] = f"{val(seg, 4)} {val(seg, 3)}".strip()
        elif sid == 'SVC' and claim is not None:
            close()
            claim['n'] += 1
            paid_code = comp(seg, 1, 2)
            billed_code = comp(seg, 6, 2) or paid_code
            line = dict(file=name, payer=payer, check=check, pay_date=pay_date or prod_date, acct=claim['acct'],
                        status=claim['status'], provider=claim['provider'], received=claim['received'],
                        paid_code=paid_code, billed_code=billed_code, charge=to_cents(val(seg, 2)),
                        paid=to_cents(val(seg, 3)), dos=claim['claim_dos'], co={}, pr=0, allowed=None, rarc=[],
                        ref='', n=claim['n'])
        elif sid == 'DTM' and claim is not None and line is None:
            if val(seg, 1) == '050':
                claim['received'] = val(seg, 2)
            elif val(seg, 1) == '232':
                claim['claim_dos'] = val(seg, 2)
        elif line is None:
            continue
        elif sid == 'DTM' and val(seg, 1) in ('472', '150'):
            line['dos'] = val(seg, 2) or val(seg, 6)[:8]
        elif sid == 'REF' and val(seg, 1) == '6R':
            line['ref'] = val(seg, 2)
        elif sid == 'CAS':
            grp = val(seg, 1)
            for i in range(2, 19, 3):
                reason, amt = val(seg, i), val(seg, i + 1)
                if not reason:
                    break
                if grp == 'PR':
                    line['pr'] += to_cents(amt)
                else:
                    key = f'{grp}-{reason}'
                    line['co'][key] = line['co'].get(key, 0) + to_cents(amt)
        elif sid == 'AMT' and val(seg, 1) == 'B6':
            line['allowed'] = to_cents(val(seg, 2))
        elif sid == 'LQ' and val(seg, 1) == 'HE':
            line['rarc'].append(val(seg, 2))
        elif sid in ('LX', 'SE', 'PLB'):
            close()
    close()
    return lines


def find_835s(folder):
    """(relative path, text) for every remit file under folder, sorted by path."""
    root = Path(folder)
    out = []
    for p in sorted(root.rglob('*'), key=lambda p: p.relative_to(root).as_posix()):
        if p.is_file() and p.suffix.lower() in EXTENSIONS:
            text = p.read_text(encoding='utf-8', errors='replace')
            if text.lstrip('﻿ \t\r\n').startswith('ISA'):
                out.append((p.relative_to(root).as_posix(), text))
    return out


# ---------- fee schedule ----------
def norm_date(s):
    s = (s or '').strip()
    if not s:
        return ''
    t = s.replace('-', '')
    if len(t) == 8 and t.isdigit():
        return t
    raise ValueError(f'effective date {s!r} is not YYYY-MM-DD')


def read_fees(text):
    """{(PAYER, cpt): [(effective YYYYMMDD or '', cents), ...]} sorted by date."""
    rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    if not rows:
        raise ValueError('the fee schedule is empty')
    head = [h.strip().lower() for h in rows[0]]
    for need in ('payer', 'cpt', 'contracted_allowed'):
        if need not in head:
            raise ValueError(f'the fee schedule needs a {need} column (it has {", ".join(head)})')
    ip, ic, ia = head.index('payer'), head.index('cpt'), head.index('contracted_allowed')
    ie = head.index('effective') if 'effective' in head else None
    fees = {}
    for n, r in enumerate(rows[1:], start=2):
        if not any(x.strip() for x in r):
            continue
        cell = lambda i: r[i] if i is not None and i < len(r) else ''
        key = (cell(ip).strip().upper(), cell(ic).strip())
        try:
            fees.setdefault(key, []).append((norm_date(cell(ie)), to_cents(cell(ia))))
        except ValueError as e:
            raise ValueError(f'fee schedule row {n}: {e}')
    for v in fees.values():
        v.sort()
    return fees


def rate_on(fees, payer, code, dos):
    hits = [c for eff, c in fees.get((payer, code), []) if eff <= dos]
    return hits[-1] if hits else None


# ---------- the check ----------
def classify(l, fees, tol):
    if l['allowed'] is None:
        l['allowed'] = l['paid'] + l['pr']
    billed_lv, paid_lv = em_level(l['billed_code']), em_level(l['paid_code'])
    l['contract_billed'] = rate_on(fees, l['payer'], l['billed_code'], l['dos'])
    l['gap'] = None
    if l['paid'] == 0 and l['allowed'] == 0:
        l['class'] = 'denied'
        l['carc'] = next((k.split('-', 1)[1] for k in l['co'] if k[:3] in ('CO-', 'OA-', 'PI-') and k != 'CO-45'), 'other')
    elif billed_lv and paid_lv and paid_lv[1] < billed_lv[1]:
        l['class'] = 'downcoded'
        if l['contract_billed'] is not None:
            l['gap'] = l['contract_billed'] - l['allowed']
    elif l['billed_code'] == l['paid_code'] and l['contract_billed'] is not None and l['contract_billed'] - l['allowed'] > tol:
        l['class'] = 'short_pay'
        l['gap'] = l['contract_billed'] - l['allowed']
    else:
        l['class'] = 'clean'
    return l


def settle(raw):
    """Fold reversals (CLP02 = 22) into one line per service: its last version that is not a reversal.
    Returns (lines, reversed claims by payer). Mirrors payer_cost.py from the demo."""
    occ = {}
    for l in raw:
        ref = l['ref'] or f"{l['acct']}|{l['n']}"
        occ.setdefault((l['payer'], ref), []).append(l)
    lines, reversed_ = [], {}
    for (payer, _), ls in occ.items():
        ls.sort(key=lambda l: (l['pay_date'], l['status'] == '22'))
        keep = [l for l in ls if l['status'] != '22']
        if any(l['status'] == '22' for l in ls):
            reversed_.setdefault(payer, set()).add(ls[0]['acct'])
        if not keep:            # a reversal of a payment from before these files: a touch, no line
            continue
        eff = dict(keep[-1])
        eff['first_pay_date'] = keep[0]['pay_date']
        lines.append(eff)
    return lines, reversed_


def p90(v):
    v = sorted(v)
    return v[min(len(v) - 1, div_half_even(9 * (len(v) - 1), 10))]


def median2(v):
    v = sorted(v)
    n = len(v)
    return 2 * v[n // 2] if n % 2 else v[n // 2 - 1] + v[n // 2]


def run(files, fee_text, opts=None):
    """files: [(name, text)]. Returns the lines and the three output tables."""
    opts = opts or {}
    o = {**DEFAULTS, **opts, 'minutes': {**DEFAULTS['minutes'], **opts.get('minutes', {})}}
    tol, hourly_c, wo_bp = to_cents(o['tolerance']), to_cents(o['hourly']), to_cents(o['writeoff_pct'])   # bp: 46.0% is 4600
    fees = read_fees(fee_text)
    on_file = {p for p, _ in fees}
    raw = [l for name, text in files for l in read_835(name, text)]
    lines, reversed_ = settle(raw)
    for l in lines:
        classify(l, fees, tol)

    payers = sorted({l['payer'] for l in lines} | set(reversed_))
    by_payer = []
    for payer in payers:
        pl = [l for l in lines if l['payer'] == payer]
        priced = payer in on_file
        claims = {}
        for l in pl:
            claims.setdefault(l['acct'], []).append(l)
        days = [days_between(v[0]['received'], v[0]['first_pay_date']) for v in claims.values()
                if any(x['paid'] > 0 for x in v) and len(v[0]['received']) == 8 and len(v[0]['first_pay_date']) == 8]
        den = [l for l in pl if l['class'] == 'denied']
        down = [l for l in pl if l['class'] == 'downcoded']
        short = [l for l in pl if l['class'] == 'short_pay']
        touches = dict(denial=len({l['acct'] for l in den}), downcode=len(down), short_pay=len({l['acct'] for l in short}),
                       reversal=len(reversed_.get(payer, ())), slow=sum(1 for d in days if d > o['slow_days']))
        minutes = sum(touches[k] * o['minutes'][k] for k in TOUCHES)
        staff = div_half_even(minutes * hourly_c, 60)
        paid = sum(l['paid'] for l in pl)
        down_gap = sum(l['gap'] or 0 for l in down)
        short_gap = sum(l['gap'] for l in short)
        denied_value = sum(l['contract_billed'] or 0 for l in den)
        writeoff = div_half_even(denied_value * wo_bp, 10000)
        cost = down_gap + short_gap + staff + writeoff
        dos = sorted(l['dos'] for l in pl if l['dos'])
        by_payer.append(dict(
            payer=payer, contract_on_file='yes' if priced else 'no',
            first_date_of_service=dos[0] if dos else '', last_date_of_service=dos[-1] if dos else '',
            claims=len(claims), service_lines=len(pl), paid=money(paid),
            denied_claims=touches['denial'], denied_lines=len(den), denied_value_at_contract=money(denied_value) if priced else '',
            downcoded_lines=len(down), downcoded_gap=money(down_gap) if priced else '',
            short_pay_lines=len(short), short_pay_claims=touches['short_pay'], short_pay_gap=money(short_gap) if priced else '',
            reversed_claims=touches['reversal'], claims_with_days_to_pay=len(days),
            days_to_pay_median=half(median2(days)) if days else '', days_to_pay_p90=p90(days) if days else '',
            slow_claims=touches['slow'], staff_minutes=minutes, staff_cost=money(staff),
            denied_dollars_lost=money(writeoff) if priced else '', total_cost=money(cost) if priced else '',
            cost_per_100_paid=money(div_half_even(cost * 10000, paid)) if priced and paid > 0 else ''))

    groups = {}
    for l in lines:
        groups.setdefault((l['payer'], l['billed_code']), []).append(l)
    by_code = []
    for (payer, code), gl in sorted(groups.items()):
        priced = payer in on_file
        pick = lambda c: [l for l in gl if l['class'] == c]
        by_code.append(dict(
            payer=payer, billed_code=code, service_lines=len(gl), charge=money(sum(l['charge'] for l in gl)),
            allowed=money(sum(l['allowed'] for l in gl)), paid=money(sum(l['paid'] for l in gl)),
            denied_lines=len(pick('denied')),
            denied_value_at_contract=money(sum(l['contract_billed'] or 0 for l in pick('denied'))) if priced else '',
            downcoded_lines=len(pick('downcoded')),
            downcoded_gap=money(sum(l['gap'] or 0 for l in pick('downcoded'))) if priced else '',
            short_pay_lines=len(pick('short_pay')), short_pay_gap=money(sum(l['gap'] for l in pick('short_pay'))) if priced else ''))

    order = dict(downcoded=0, short_pay=1, denied=2)
    flagged = sorted((l for l in lines if l['class'] in order),
                     key=lambda l: (order[l['class']], l['payer'], l['provider'], l['dos'], l['acct'], l['n']))
    opt = lambda c: money(c) if c is not None else ''
    pull = [dict(flag=l['class'], payer=l['payer'], provider=l['provider'], patient_account=l['acct'], date_of_service=l['dos'],
                 billed_code=l['billed_code'], paid_code=l['paid_code'], charge=money(l['charge']), allowed=money(l['allowed']),
                 paid=money(l['paid']), contract_rate=opt(l['contract_billed']), dollar_gap=opt(l['gap']),
                 adjustment_codes=' '.join(l['co']), remark_codes=' '.join(l['rarc']), file=l['file']) for l in flagged]

    sample = bool(payers) and all(p.startswith('SAMPLE ') for p in payers)
    dos = sorted(l['dos'] for l in lines if l['dos'])
    return dict(files=[n for n, _ in files], raw_lines=len(raw), lines=lines, by_payer=by_payer, by_code=by_code, pull=pull,
                sample=sample, period=(dos[0], dos[-1]) if dos else ('', ''), payers_without_fees=[p for p in payers if p not in on_file])


def to_csv(rows, sample):
    """CSV text with '\\n' line ends and a first column that says where the numbers came from."""
    if not rows:
        return ''
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\n')
    w.writerow(['source'] + list(rows[0]))
    for r in rows:
        w.writerow(['Sample data' if sample else "Practice's 835 files"] + [r[k] for k in r])
    return buf.getvalue()


OUTPUTS = (('summary-by-payer.csv', 'by_payer'), ('summary-by-payer-and-code.csv', 'by_code'), ('chart-pull-list.csv', 'pull'))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('remit_folder')
    ap.add_argument('fee_schedule')
    ap.add_argument('--out', default='payer-check-output')
    ap.add_argument('--hourly', default=DEFAULTS['hourly'], help='staff cost per hour in dollars (default 30.00, illustrative)')
    ap.add_argument('--writeoff-pct', default=DEFAULTS['writeoff_pct'],
                    help='share of denied dollars never recovered, in percent (default 46.0, from Premier, March 2024, hospitals on 2022 claims)')
    ap.add_argument('--slow-days', type=int, default=DEFAULTS['slow_days'], help='days after receipt that count as slow (default 30)')
    ap.add_argument('--version', action='version', version=VERSION)
    a = ap.parse_args(argv)
    files = find_835s(a.remit_folder)
    if not files:
        sys.exit(f'No 835 files in {a.remit_folder} (looked for {", ".join(EXTENSIONS)} files that start with ISA)')
    r = run(files, Path(a.fee_schedule).read_text(encoding='utf-8'),
            dict(hourly=a.hourly, writeoff_pct=a.writeoff_pct, slow_days=a.slow_days))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, key in OUTPUTS:
        (out / name).write_text(to_csv(r[key], r['sample']), encoding='utf-8')
    if r['sample']:
        print('Sample data: made-up remits from a made-up practice. These numbers are not results.')
    print(f"{len(files)} files, {r['raw_lines']} remit lines, {len(r['lines'])} service lines after reversals, "
          f"dates of service {r['period'][0]} to {r['period'][1]}")
    for p in r['by_payer']:
        tail = f", cost per $100 paid {p['cost_per_100_paid']}" if p['cost_per_100_paid'] else ', no fee schedule rows, not priced'
        print(f"  {p['payer']}: {p['claims']} claims, paid {p['paid']}, denied {p['denied_claims']} claims, "
              f"downcoded {p['downcoded_lines']}, short {p['short_pay_lines']}, reversed {p['reversed_claims']}, "
              f"median {p['days_to_pay_median'] or 'unknown'} days to pay{tail}")
    print(f"Wrote {', '.join(n for n, _ in OUTPUTS)} to {out}/. chart-pull-list.csv has patient account numbers: keep it on this computer.")


if __name__ == '__main__':
    main()
