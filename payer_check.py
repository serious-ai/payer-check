#!/usr/bin/env python3
"""Payer check v0.2: what each payer costs a practice, read from its own 835 remits and fee schedule.

Runs on this computer only. Standard library only, no network code, no third-party packages.
payer-check.html runs the same steps in the browser, and tests/test_page.py checks that both write
byte-identical CSVs from the same files.

Adapted from the October 28, 2026 public demo (downcoding_check.py and payer_cost.py, which read
835s with pyx12). The 835 reader here is plain code so the page and this CLI share one logic, and
tests/test_cli.py checks it line for line against the pyx12 reader on the sample remits.

Usage:
    python3 payer_check.py REMIT_FOLDER FEE_SCHEDULE.csv [--base-units BASE_UNITS.csv] [--out DIR]

REMIT_FOLDER   every file in it (and its subfolders) that starts with an ISA segment is read as an 835
FEE_SCHEDULE   CSV with columns payer, cpt, contracted_allowed and an optional effective date
               (YYYY-MM-DD). The payer name must match the 835's N1*PR name, case aside.
               Anesthesia mode: a row with a conversion_factor prices that payer's anesthesia codes
               (00100 to 01999) as (base units + time units + physical status units) x conversion factor
               x the modifier percentage. See the README for the optional columns.
BASE_UNITS     CSV of base units by anesthesia code (a code column and a base units column), such as
               the CMS anesthesia base units file saved as CSV. Needed for anesthesia mode.

Writes three files to DIR (default payer-check-output):
    summary-by-payer.csv            one row per payer, no patient-level fields
    summary-by-payer-and-code.csv   one row per payer and billed code, no patient-level fields
    chart-pull-list.csv             every flagged line with its patient account number. Keep it on this computer.
and, when the remits hold anesthesia lines, two more:
    anesthesia-by-payer.csv         one row per payer: units, paid per unit, and the gap by reason. No patient-level fields
    anesthesia-pull-list.csv        every anesthesia line paid short, with the reason. Keep it on this computer.

The patient's name (NM1*QC) is never read.
"""
import argparse, csv, io, re, sys
from datetime import date
from pathlib import Path

VERSION = '0.2.0'
EM_FAMILIES = {'9920': 'New patient', '9921': 'Established', '9924': 'Office consult'}
TOUCHES = ('denial', 'downcode', 'short_pay', 'anesthesia', 'reversal', 'slow')
# Illustrative staff and write-off inputs, from the public demo's engine.py. Each one is a flag below.
DEFAULTS = dict(minutes=dict(denial=20, downcode=15, short_pay=5, anesthesia=5, reversal=10, slow=6), hourly='30.00',
                writeoff_pct='46.0', slow_days=30, tolerance='0.50')
EXTENSIONS = ('.835', '.txt', '.edi', '.x12', '.era', '.rmt')
# Anesthesia payment modifiers and the share of the full allowance each one pays, per CMS (Medicare Claims
# Processing Manual, chapter 12, section 50: QK, QX and QY at the medically directed rate of 50 percent).
# A payer's own percentages go in the fee schedule's modifier_pct column (UnitedHealthcare's commercial policy
# 2026R0032A, for one, pays QZ at 85%). AD pays 3 base units and no time (section 50.D).
ANES_PCT = dict(AA=100, AD=100, QK=50, QX=50, QY=50, QZ=100)
ANES_REASONS = ('md_cut', 'time_cut', 'base_short', 'cf_short')
ROUNDINGS = ('tenth', 'up', 'down')
DIRECTED = ('QK', 'QX', 'QY')       # medically directed, counted in anesthesia-by-payer.csv
MATCH_CENTS = 2      # a gap within 2 cents of whole missing units (rounding on both sides) counts as those units


# ---------- money and dates, all in integers ----------
def div_half_even(n, d):
    """round(n / d) to the nearest integer, ties to even, for integers n >= 0 and d > 0."""
    q, r = divmod(n, d)
    if 2 * r > d or (2 * r == d and q % 2 == 1):
        q += 1
    return q


def to_fixed(s, places):
    """'153.19' to 15319 at 2 places, exactly, from the text. Ties past the last place round to even. Blank is 0."""
    s = (s or '').strip().replace('$', '').replace(',', '')
    if not s:
        return 0
    neg = s.startswith('-')
    s = s.lstrip('+-')
    whole, _, frac = s.partition('.')
    if not (whole or frac) or not (whole + frac).isdigit():
        raise ValueError(f'not an amount: {s!r}')
    n = int(whole or '0') * 10 ** places + int((frac + '0' * places)[:places] or '0')
    rest = frac[places:]
    if rest and (rest[0] > '5' or (rest[0] == '5' and (rest[1:].strip('0') or n % 2 == 1))):
        n += 1
    return -n if neg else n


def to_cents(s):
    return to_fixed(s, 2)


def fixed(n, places):
    """15319 at 2 places to '153.19'."""
    sign = '-' if n < 0 else ''
    n, d = abs(n), 10 ** places
    return f'{sign}{n // d}.{n % d:0{places}d}'


def money(c):
    """15319 to '153.19'."""
    return fixed(c, 2)


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
            paid_mods = [m for m in (comp(seg, 1, j) for j in range(3, 7)) if m]
            billed_mods = [m for m in (comp(seg, 6, j) for j in range(3, 7)) if m] if comp(seg, 6, 2) else paid_mods
            line = dict(file=name, payer=payer, check=check, pay_date=pay_date or prod_date, acct=claim['acct'],
                        status=claim['status'], provider=claim['provider'], received=claim['received'],
                        paid_code=paid_code, billed_code=billed_code, charge=to_cents(val(seg, 2)),
                        paid=to_cents(val(seg, 3)), dos=claim['claim_dos'], co={}, pr=0, allowed=None, rarc=[],
                        ref='', n=claim['n'], paid_mods=paid_mods, billed_mods=billed_mods,
                        units_paid=val(seg, 5).strip(), units_billed=val(seg, 7).strip() or val(seg, 5).strip())
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


def pairs(s, what):
    """'QZ=85 QK=50' to {'QZ': 85, 'QK': 50}. Pairs split on spaces or semicolons. Values are whole numbers."""
    out = {}
    for part in re.split(r'[\s;]+', (s or '').strip()):
        if not part:
            continue
        k, eq, v = part.partition('=')
        if not eq or not k.strip() or not v.strip().isdigit():
            raise ValueError(f'{what} {part!r} is not CODE=NUMBER')
        out[k.strip().upper()] = int(v.strip())
    return out


def read_fees(text):
    """Returns (fees, anes). fees: {(PAYER, cpt): [(effective YYYYMMDD or '', cents), ...]} sorted by date.
    anes: {'cf': {(PAYER, cpt or ''): [(effective, terms), ...]}, 'base': {(PAYER, cpt): base units}}."""
    rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    if not rows:
        raise ValueError('the fee schedule is empty')
    head = [h.strip().lower() for h in rows[0]]
    for need in ('payer', 'cpt'):
        if need not in head:
            raise ValueError(f'the fee schedule needs a {need} column (it has {", ".join(head)})')
    if 'contracted_allowed' not in head and 'conversion_factor' not in head:
        raise ValueError(f'the fee schedule needs a contracted_allowed column (it has {", ".join(head)})')
    col = lambda h: head.index(h) if h in head else None
    ip, ic, ia, ie = col('payer'), col('cpt'), col('contracted_allowed'), col('effective')
    icf, ib, ium, ir, imp, ips, imx = (col(h) for h in ('conversion_factor', 'base_units', 'time_unit_minutes', 'time_rounding',
                                                         'modifier_pct', 'physical_status_units', 'max_minutes'))
    fees, anes = {}, dict(cf={}, base={})
    for n, r in enumerate(rows[1:], start=2):
        if not any(x.strip() for x in r):
            continue
        cell = lambda i: r[i].strip() if i is not None and i < len(r) else ''
        key = (cell(ip).upper(), cell(ic))
        try:
            eff = norm_date(cell(ie))
            anes_row = bool(cell(icf) or cell(ib))
            if ia is not None and (cell(ia) or not anes_row):
                fees.setdefault(key, []).append((eff, to_cents(cell(ia))))
            if cell(ib):
                if not cell(ib).isdigit() or not key[1]:
                    raise ValueError(f'base_units {cell(ib)!r} needs a whole number and a cpt')
                anes['base'][key] = int(cell(ib))
            if cell(icf):
                um, rnd = cell(ium) or '15', (cell(ir) or 'tenth').lower()
                if not um.isdigit() or int(um) < 1:
                    raise ValueError(f'time_unit_minutes {um!r} is not a whole number of minutes')
                if rnd not in ROUNDINGS:
                    raise ValueError(f'time_rounding {rnd!r} is not one of {", ".join(ROUNDINGS)}')
                terms = dict(cf4=to_fixed(cell(icf), 4), unit_minutes=int(um), rounding=rnd,
                             pct={**ANES_PCT, **pairs(cell(imp), 'modifier_pct')},
                             ps=pairs(cell(ips), 'physical_status_units'), caps=pairs(cell(imx), 'max_minutes'))
                anes['cf'].setdefault(key, []).append((eff, terms))
        except ValueError as e:
            raise ValueError(f'fee schedule row {n}: {e}')
    for v in fees.values():
        v.sort()
    for v in anes['cf'].values():
        v.sort(key=lambda e: e[0])
    return fees, anes


def read_base_units(text):
    """{cpt: base units} from a CSV with a code column (cpt, code or hcpcs) and a column whose name has 'base' in it."""
    if not text:
        return {}
    rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    head = [h.strip().lower() for h in rows[0]] if rows else []
    ic = next((i for i, h in enumerate(head) if h in ('cpt', 'code', 'hcpcs', 'cpt code', 'hcpcs code', 'procedure code')), None)
    ib = next((i for i, h in enumerate(head) if 'base' in h), None)
    if ic is None or ib is None:
        raise ValueError(f'the base units file needs a code column and a base units column (it has {", ".join(head)})')
    out = {}
    for n, r in enumerate(rows[1:], start=2):
        code = r[ic].strip() if ic < len(r) else ''
        units = r[ib].strip() if ib < len(r) else ''
        if not code and not units:
            continue
        if not code or not units.isdigit():
            raise ValueError(f'base units row {n}: {code!r} {units!r} is not a code and a whole number')
        out[code] = int(units)
    return out


def rate_on(fees, payer, code, dos):
    hits = [c for eff, c in fees.get((payer, code), []) if eff <= dos]
    return hits[-1] if hits else None


# ---------- anesthesia ----------
def is_anesthesia(code):
    return len(code) == 5 and code.isdigit() and 100 <= int(code) <= 1999


def anes_terms(anes, payer, code, dos):
    """The conversion factor row in force: one for this code if there is one, else the payer's row for every code."""
    for key in ((payer, code), (payer, '')):
        hits = [t for eff, t in anes['cf'].get(key, []) if eff <= dos]
        if hits:
            return hits[-1]
    return None


def time_units(mt, unit_minutes, rounding):
    """Minutes (in tenths of a minute) to time units (in tenths of a unit). tenth: to one decimal place, as CMS does.
    up: every started unit counts. down: only whole units count."""
    if rounding == 'tenth':
        return (2 * mt + unit_minutes) // (2 * unit_minutes)
    whole = 10 * unit_minutes
    return (-(-mt // whole) if rounding == 'up' else mt // whole) * 10


def anes_cents(units10, cf4, pct):
    """Tenths of a unit x conversion factor (in ten-thousandths of a dollar) x percent, to cents."""
    return div_half_even(units10 * cf4 * pct, 100000)


def first_hit(mods, table):
    return next((m for m in mods if m in table), '')


def minutes_text(mt):
    return str(mt // 10) if mt % 10 == 0 else fixed(mt, 1)


def price_anesthesia(l, anes, base_table):
    """What the contract pays for this line, from (base + time + physical status units) x CF x modifier percent.
    Sets l['anes'] (the working) and returns the expected allowed in cents, or None when it can't be priced."""
    t = anes_terms(anes, l['payer'], l['billed_code'], l['dos'])
    a = l['anes'] = dict(skip='', reasons=[], gaps=dict.fromkeys(ANES_REASONS, 0))
    if t is None:
        a['skip'] = 'no conversion factor on file for the date of service'
        return None
    code = l['billed_code']
    mod_b, mod_p = first_hit(l['billed_mods'], t['pct']), first_hit(l['paid_mods'], t['pct'])
    pct_b, pct_p = t['pct'].get(mod_b, 100), t['pct'].get(mod_p, 100)
    supervised = mod_b == 'AD'
    base = 3 if supervised else anes['base'].get((l['payer'], code), base_table.get(code))
    if base is None:
        a['skip'] = 'no base units for the code'
        return None
    try:
        mb, mp = to_fixed(l['units_billed'], 1), to_fixed(l['units_paid'], 1)
    except ValueError:
        mb = mp = 0
    if not l['units_paid'] or mb <= 0:
        a['skip'] = 'no minutes on the remit'
        return None
    no_time = supervised or code == '01996'
    cap = t['caps'].get(code)
    mb_c = min(mb, cap * 10) if cap else mb
    tu = lambda m: 0 if no_time else time_units(m, t['unit_minutes'], t['rounding'])
    ps_b = t['ps'].get(first_hit(l['billed_mods'], t['ps']), 0)
    ps_p = t['ps'].get(first_hit(l['paid_mods'], t['ps']), 0)
    a.update(terms=t, pct_b=pct_b, pct_p=pct_p, base=base, mb=mb, mp=mp, t_contract=tu(mb_c), t_paid=tu(mp),
             t_down=0 if no_time else time_units(mp, t['unit_minutes'], 'down'), ps_b=ps_b, ps_p=ps_p)
    a['u_contract'] = base * 10 + a['t_contract'] + ps_b * 10
    a['u_paid'] = base * 10 + a['t_paid'] + ps_p * 10
    return anes_cents(a['u_contract'], t['cf4'], pct_b)


def explain_anesthesia(l, gap, tol):
    """Split a short-paid anesthesia line's gap into reasons, in this order: a medical direction cut (paid at a lower
    modifier percentage than billed), time cut (fewer minutes paid than billed, past any cap in the contract, or time
    truncated to whole units), base units short (whole base or physical status units missing), and conversion factor
    short (what's left). The parts add up to the gap exactly."""
    a = l['anes']
    t, g, cf4, pct_p = a['terms'], a['gaps'], a['terms']['cf4'], a['pct_p']
    if pct_p < a['pct_b']:
        g['md_cut'] = anes_cents(a['u_contract'], cf4, a['pct_b'] - pct_p)
    if a['t_paid'] < a['t_contract']:
        g['time_cut'] = anes_cents(a['t_contract'] - a['t_paid'], cf4, pct_p)
    if a['ps_p'] < a['ps_b']:
        g['base_short'] = anes_cents((a['ps_b'] - a['ps_p']) * 10, cf4, pct_p)
    rest = gap - sum(g.values())
    if rest > tol:
        trunc = a['t_paid'] - a['t_down']
        if trunc > 0 and abs(rest - anes_cents(trunc, cf4, pct_p)) <= MATCH_CENTS:
            g['time_cut'] += rest
        elif any(abs(rest - anes_cents(k * 10, cf4, pct_p)) <= MATCH_CENTS for k in range(1, a['base'] + 1)):
            g['base_short'] += rest
        else:
            g['cf_short'] += rest
    elif rest >= 0:             # rounding left over: it goes to the largest part
        big = max(ANES_REASONS, key=lambda r: g[r])
        g[big] += rest
    else:                       # the parts explain more than the gap (the payer overpaid something else on the line):
        take = -rest            # take the excess off the parts from the last reason back, never below zero
        for r in reversed(ANES_REASONS):
            cut = min(g[r], take)
            g[r] -= cut
            take -= cut
    a['reasons'] = [r for r in ANES_REASONS if g[r] > 0]
    a['paid_cf4'] = div_half_even(l['allowed'] * 100000, a['u_paid'] * pct_p) if a['u_paid'] * pct_p else None


# ---------- the check ----------
def classify(l, fees, tol, anes=None, base_table=None):
    if l['allowed'] is None:
        l['allowed'] = l['paid'] + l['pr']
    billed_lv, paid_lv = em_level(l['billed_code']), em_level(l['paid_code'])
    anesthesia = anes is not None and is_anesthesia(l['billed_code']) and any(p == l['payer'] for p, _ in anes['cf'])
    if anesthesia:
        l['contract_billed'] = price_anesthesia(l, anes, base_table)
    else:
        l['contract_billed'] = rate_on(fees, l['payer'], l['billed_code'], l['dos'])
    l['gap'] = None
    if l['paid'] == 0 and l['allowed'] == 0:
        l['class'] = 'denied'
        l['carc'] = next((k.split('-', 1)[1] for k in l['co'] if k[:3] in ('CO-', 'OA-', 'PI-') and k != 'CO-45'), 'other')
    elif anesthesia:
        if l['contract_billed'] is not None and l['contract_billed'] - l['allowed'] > tol:
            l['class'] = 'anesthesia_short'
            l['gap'] = l['contract_billed'] - l['allowed']
            explain_anesthesia(l, l['gap'], tol)
        else:
            l['class'] = 'clean'
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


def run(files, fee_text, opts=None, base_text=''):
    """files: [(name, text)]. Returns the lines and the output tables."""
    opts = opts or {}
    o = {**DEFAULTS, **opts, 'minutes': {**DEFAULTS['minutes'], **opts.get('minutes', {})}}
    tol, hourly_c, wo_bp = to_cents(o['tolerance']), to_cents(o['hourly']), to_cents(o['writeoff_pct'])   # bp: 46.0% is 4600
    fees, anes = read_fees(fee_text)
    base_table = read_base_units(base_text)
    on_file = {p for p, _ in fees} | {p for p, _ in anes['cf']}
    raw = [l for name, text in files for l in read_835(name, text)]
    lines, reversed_ = settle(raw)
    for l in lines:
        classify(l, fees, tol, anes, base_table)

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
        anes_short = [l for l in pl if l['class'] == 'anesthesia_short']
        touches = dict(denial=len({l['acct'] for l in den}), downcode=len(down), short_pay=len({l['acct'] for l in short}),
                       anesthesia=len({l['acct'] for l in anes_short}),
                       reversal=len(reversed_.get(payer, ())), slow=sum(1 for d in days if d > o['slow_days']))
        minutes = sum(touches[k] * o['minutes'][k] for k in TOUCHES)
        staff = div_half_even(minutes * hourly_c, 60)
        paid = sum(l['paid'] for l in pl)
        down_gap = sum(l['gap'] or 0 for l in down)
        short_gap = sum(l['gap'] for l in short)
        anes_gap = sum(l['gap'] for l in anes_short)
        denied_value = sum(l['contract_billed'] or 0 for l in den)
        writeoff = div_half_even(denied_value * wo_bp, 10000)
        cost = down_gap + short_gap + anes_gap + staff + writeoff
        dos = sorted(l['dos'] for l in pl if l['dos'])
        by_payer.append(dict(
            payer=payer, contract_on_file='yes' if priced else 'no',
            first_date_of_service=dos[0] if dos else '', last_date_of_service=dos[-1] if dos else '',
            claims=len(claims), service_lines=len(pl), paid=money(paid),
            denied_claims=touches['denial'], denied_lines=len(den), denied_value_at_contract=money(denied_value) if priced else '',
            downcoded_lines=len(down), downcoded_gap=money(down_gap) if priced else '',
            short_pay_lines=len(short), short_pay_claims=touches['short_pay'], short_pay_gap=money(short_gap) if priced else '',
            anesthesia_short_lines=len(anes_short), anesthesia_gap=money(anes_gap) if priced else '',
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
            short_pay_lines=len(pick('short_pay')), short_pay_gap=money(sum(l['gap'] for l in pick('short_pay'))) if priced else '',
            anesthesia_short_lines=len(pick('anesthesia_short')),
            anesthesia_gap=money(sum(l['gap'] for l in pick('anesthesia_short'))) if priced else ''))

    order = dict(downcoded=0, short_pay=1, denied=2)
    flagged = sorted((l for l in lines if l['class'] in order),
                     key=lambda l: (order[l['class']], l['payer'], l['provider'], l['dos'], l['acct'], l['n']))
    opt = lambda c: money(c) if c is not None else ''
    pull = [dict(flag=l['class'], payer=l['payer'], provider=l['provider'], patient_account=l['acct'], date_of_service=l['dos'],
                 billed_code=l['billed_code'], paid_code=l['paid_code'], charge=money(l['charge']), allowed=money(l['allowed']),
                 paid=money(l['paid']), contract_rate=opt(l['contract_billed']), dollar_gap=opt(l['gap']),
                 adjustment_codes=' '.join(l['co']), remark_codes=' '.join(l['rarc']), file=l['file']) for l in flagged]

    anes_by_payer, anes_pull = anesthesia_tables(lines, payers, anes)
    sample = bool(payers) and all(p.startswith('SAMPLE ') for p in payers)
    dos = sorted(l['dos'] for l in lines if l['dos'])
    return dict(files=[n for n, _ in files], raw_lines=len(raw), lines=lines, by_payer=by_payer, by_code=by_code, pull=pull,
                anes_by_payer=anes_by_payer, anes_pull=anes_pull,
                sample=sample, period=(dos[0], dos[-1]) if dos else ('', ''), payers_without_fees=[p for p in payers if p not in on_file])


def anesthesia_tables(lines, payers, anes):
    """One row per payer with anesthesia lines, and one row per anesthesia line paid short."""
    by_payer = []
    for payer in payers:
        al = [l for l in lines if l['payer'] == payer and is_anesthesia(l['billed_code'])]
        if not al:
            continue
        has_cf = any(p == payer for p, _ in anes['cf'])
        checked = [l for l in al if 'anes' in l and not l['anes']['skip']]
        paid_lines = [l for l in checked if l['class'] != 'denied']
        skipped = {}
        for l in al:
            if 'anes' in l and l['anes']['skip']:
                skipped[l['anes']['skip']] = skipped.get(l['anes']['skip'], 0) + 1
        short = [l for l in checked if l['class'] == 'anesthesia_short']
        allowed = sum(l['allowed'] for l in paid_lines)
        u_paid = sum(l['anes']['u_paid'] * l['anes']['pct_p'] for l in paid_lines)
        u_contract = sum(l['anes']['u_contract'] * l['anes']['pct_b'] for l in paid_lines)
        expected = sum(l['contract_billed'] for l in paid_lines)
        latest = max(checked, key=lambda l: l['dos'])['anes']['terms']['cf4'] if checked else None
        per = lambda c, u: fixed(div_half_even(c * 100000, u), 4) if u else ''
        row = dict(payer=payer, conversion_factor_on_file='yes' if has_cf else 'no', anesthesia_lines=len(al),
                   lines_checked=len(checked), lines_not_checked=len(al) - len(checked) if has_cf else len(al),
                   not_checked_why='; '.join(f'{k}: {v}' for k, v in sorted(skipped.items())) if has_cf else 'no conversion factor on file',
                   denied_lines=sum(1 for l in checked if l['class'] == 'denied'),
                   directed_lines=sum(1 for l in paid_lines if first_hit(l['paid_mods'], DIRECTED)),
                   units_paid=fixed(sum(l['anes']['u_paid'] for l in paid_lines), 1) if has_cf else '',
                   allowed=money(allowed) if has_cf else '',
                   contract_cf_latest=fixed(latest, 4) if latest is not None else '',
                   contract_per_unit=per(expected, u_contract), paid_per_unit=per(allowed, u_paid),
                   short_lines=len(short), gap=money(sum(l['gap'] for l in short)) if has_cf else '')
        for r in ANES_REASONS:
            hit = [l for l in short if r in l['anes']['reasons']]
            row[f'{r}_lines'] = len(hit)
            row[f'{r}_gap'] = money(sum(l['anes']['gaps'][r] for l in hit)) if has_cf else ''
        by_payer.append(row)

    short = sorted((l for l in lines if l['class'] == 'anesthesia_short'),
                   key=lambda l: (l['payer'], l['provider'], l['dos'], l['acct'], l['n']))
    pull = []
    for l in short:
        a = l['anes']
        row = dict(flag=' '.join(a['reasons']), payer=l['payer'], provider=l['provider'], patient_account=l['acct'],
                   date_of_service=l['dos'], code=l['billed_code'], billed_modifiers=' '.join(l['billed_mods']),
                   paid_modifiers=' '.join(l['paid_mods']), minutes_billed=minutes_text(a['mb']), minutes_paid=minutes_text(a['mp']),
                   base_units=a['base'], time_units_contract=fixed(a['t_contract'], 1), time_units_paid=fixed(a['t_paid'], 1),
                   modifier_pct_billed=a['pct_b'], modifier_pct_paid=a['pct_p'],
                   contract_cf=fixed(a['terms']['cf4'], 4), paid_cf=fixed(a['paid_cf4'], 4) if a['paid_cf4'] is not None else '',
                   expected=money(l['contract_billed']), allowed=money(l['allowed']), paid=money(l['paid']), dollar_gap=money(l['gap']))
        for r in ANES_REASONS:
            row[f'{r}_gap'] = money(a['gaps'][r])
        row.update(adjustment_codes=' '.join(l['co']), remark_codes=' '.join(l['rarc']), file=l['file'])
        pull.append(row)
    return by_payer, pull


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
ANES_OUTPUTS = (('anesthesia-by-payer.csv', 'anes_by_payer'), ('anesthesia-pull-list.csv', 'anes_pull'))


def outputs(r):
    """The files a run writes: the three summaries, plus the two anesthesia files when there are anesthesia lines."""
    return OUTPUTS + (ANES_OUTPUTS if r['anes_by_payer'] else ())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('remit_folder')
    ap.add_argument('fee_schedule')
    ap.add_argument('--base-units', help='CSV of anesthesia base units by code (needed to price anesthesia lines)')
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
            dict(hourly=a.hourly, writeoff_pct=a.writeoff_pct, slow_days=a.slow_days),
            Path(a.base_units).read_text(encoding='utf-8') if a.base_units else '')
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    written = outputs(r)
    for name, key in written:
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
    for p in r['anes_by_payer']:
        if p['conversion_factor_on_file'] == 'no':
            print(f"  {p['payer']} anesthesia: {p['anesthesia_lines']} lines, no conversion factor on file, not priced")
            continue
        why = ', '.join(f"{x.replace('_', ' ')} {p[x + '_lines']}" for x in ANES_REASONS)
        print(f"  {p['payer']} anesthesia: {p['lines_checked']} of {p['anesthesia_lines']} lines checked, "
              f"paid {p['paid_per_unit'] or 'unknown'} a unit against {p['contract_per_unit'] or 'unknown'} contracted, "
              f"{p['short_lines']} short ({why}), gap {p['gap']}")
    private = ' and anesthesia-pull-list.csv have' if len(written) > 3 else ' has'
    print(f"Wrote {', '.join(n for n, _ in written)} to {out}/. chart-pull-list.csv{private} patient account numbers: keep {'them' if len(written) > 3 else 'it'} on this computer.")


if __name__ == '__main__':
    main()
