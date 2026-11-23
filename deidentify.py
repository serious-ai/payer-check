#!/usr/bin/env python3
"""De-identifier: turns a practice's billing export into a HIPAA Safe Harbor copy (45 CFR 164.514(b)(2)),
on the practice's own computer.

    python3 deidentify.py "CPT Payment Report.xlsx" --out deidentified

Reads a CSV or an Excel (.xlsx) export and writes three files:
  <name>-deidentified.csv     the copy that can be shared
  <name>-deid-record.txt      what was done to every column, and the 18 Safe Harbor identifiers checked
  <name>-visit-code-key.csv   original numbers and their visit codes. Keep it at the practice, never send it.

Each column is handled by its header, then checked by its contents:
  - Account, encounter, claim, member and similar numbers become random visit codes (164.514(c)): a code is not
    made from anything about the patient, and the key never leaves the practice.
  - Every date becomes its year. When a service date and a payment date sit side by side, the days between them
    are worked out first and added as a column.
  - Names, addresses, phone numbers, birth dates and other direct identifiers are dropped.
  - Amounts, codes, payer and provider names are kept. Any dates, phone numbers, emails or long numbers typed
    inside them are replaced.
  - Free-text notes and columns it doesn't recognize are dropped, and the record names them. Use --keep to keep one.

Standard library only, Python 3.9 or newer. deidentify.html does the same thing in a browser, line for line,
and tests/test_deidentify.py requires both to write byte-identical files from the same input.
"""
import argparse, csv, datetime, io, math, re, secrets, sys, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

VERSION = '0.1.0'

# ---------- header words ----------
W = lambda s: set(s.split())
DROP = W('ssn social dob birth birthdate bday address addr street city zip zipcode postal phone tel telephone fax '
         'email mail mailing mobile cell url website ip license lic vehicle vin plate device serial photo picture '
         'image biometric fingerprint face')
AGE = W('age')
DATE = W('date dt dte dos from thru through admit admission discharge death dod expiration')
PROVIDER = W('provider prov rendering referring attending ordering supervising doctor physician dr npi clinician '
             'practitioner md staff user operator')
ID_SUFFIX = W('id num nbr number no key identifier')
ID_SUFFIX_EXCEPT = W('amt amount total balance bal charge charges units unit qty count cnt days cpt hcpcs code mod '
                     'modifier icd dx pos npi carc rarc')
ID = W('enc encounter visit acct account claim clm mrn chart member subscriber policy group grp auth authorization '
       'ticket invoice inv icn tcn dcn control trace check chk eft ref reference person case episode accession '
       'specimen rx prescription batch transaction trans')
ID_EXCEPT = W('status type reason desc description category source class level count cnt')
AMOUNT = W('amt amount amounts charge charges chg billed allowed alwd allow approved aprv appr paid pay payment '
           'payments pmt balance bal adj adjustment adjustments price fee fees copay coins coinsurance deductible ded '
           'writeoff wo total units unit qty quantity resp responsibility days count cnt rvu rate expected variance')
PAYER = W('payer payor insurance ins carrier plan fin financial class product network contract lob')
NAME = W('name patient pt guarantor insured responsible party first last middle mi fname lname relative spouse '
         'mother father employer emergency contact kin')
FREETEXT = W('note notes comment comments memo message text narrative')
DATEPART = W('month mo week wk day quarter qtr period')
ATTR = W('cpt hcpcs code codes proc procedure mod modifier modifiers icd dx diagnosis diag pos place location loc '
         'facility site clinic department dept specialty desc description descr status reason carc rarc denial '
         'denied remark remarks source type category item service svc sv it tos revenue rev drg ndc method mode flag '
         'level tier year')

PREFIXES = 'VKLMNPQRST'
DATE_FMTS = (re.compile(r'^([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{4}|[0-9]{2})(?:[ T].*)?$'),     # month/day/year
             re.compile(r'^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?:[ T].*)?$'))                     # year-month-day
NUMBER = re.compile(r'^-?\$?[0-9,]*\.?[0-9]+$')
A = re.ASCII
INLINE = ((re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', A), '[email]', 'emails'),
          (re.compile(r'(?:[Hh][Tt][Tt][Pp][Ss]?://|[Ww][Ww][Ww]\.)[^ \t\r\n]+', A), '[link]', 'web links'),
          (re.compile(r'\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b', A), '[number]', 'social security numbers'),
          (re.compile(r'(?:\([0-9]{3}\) ?|\b[0-9]{3}[-. ])[0-9]{3}[-. ][0-9]{4}\b', A), '[phone]', 'phone numbers'),
          (re.compile(r'\b[0-9]{1,2}[/-][0-9]{1,2}[/-](?:[0-9]{4}|[0-9]{2})\b|\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b', A), '[date]', 'dates'),
          (re.compile(r'\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}\b', A), '[number]', 'IP addresses'),
          (re.compile(r'\b[0-9]{6,}\b', A), '[number]', 'long numbers'))


def tokens(header):
    return re.sub(r'[^a-z0-9]+', ' ', header.lower().replace('#', ' num ')).split()


def classify(header):
    """(action, reason) from the header alone. Actions: code, year, age, keep, drop."""
    t = set(tokens(header))
    if t & DROP:
        return 'drop', 'a direct identifier (' + ', '.join(sorted(t & DROP)) + ')'
    if t & AGE:
        return 'age', 'ages over 89 become 90+'
    if t & DATE:
        return 'year', 'a date'
    if t & PROVIDER:
        return 'keep', 'provider details, not the patient'
    if (t & ID_SUFFIX and not t & ID_SUFFIX_EXCEPT) or (t & ID and not t & ID_EXCEPT and not t & AMOUNT):
        return 'code', 'an identifying number'
    if t & AMOUNT:
        return 'keep', 'an amount or count'
    if t & PAYER:
        return 'keep', 'payer or plan details'
    if t & NAME:
        return 'drop', 'a name or person field'
    if t & FREETEXT:
        return 'drop', 'free text, which can hold names'
    if t & DATEPART:
        return 'drop', 'a date part finer than the year'
    if t & ATTR:
        return 'keep', 'a code or description'
    return 'drop', 'not recognized'


# ---------- dates, all in whole days ----------
def ymd(y, m, d):
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def parse_date(cell):
    """A date from a cell: an Excel date serial, or text like 11/21/2025, 11-21-25 or 2025-11-21."""
    v, serial = cell
    if serial is not None:
        return serial
    s = v.strip()
    m = DATE_FMTS[0].match(s)
    if m:
        y = int(m.group(3))
        if len(m.group(3)) == 2:
            y += 2000 if y <= 69 else 1900
        return ymd(y, int(m.group(1)), int(m.group(2)))
    m = DATE_FMTS[1].match(s)
    if m:
        return ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


# ---------- readers: rows of (text, date or None) ----------
def read_csv(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('latin-1')
    return [[(v, None) for v in r] for r in csv.reader(io.StringIO(text, newline=''))]


BUILTIN_DATE_FMTS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def is_date_format(code):
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', '', code)
    return bool(re.search(r'[dy]', code, re.I))


def local(tag):
    return tag.rsplit('}', 1)[-1]


def rich_text(el):
    """The text of a string item: its own <t>, or the <t> of each run. Phonetic guides (<rPh>) are left out."""
    out = ''
    for ch in el:
        if local(ch.tag) == 't':
            out += ch.text or ''
        elif local(ch.tag) == 'r':
            out += ''.join(t.text or '' for t in ch if local(t.tag) == 't')
    return out


def col_index(ref):
    n = 0
    for ch in re.match(r'[A-Z]*', ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    wb = ET.fromstring(z.read('xl/workbook.xml'))
    epoch = datetime.date(1899, 12, 30)
    for el in wb.iter():
        if local(el.tag) == 'workbookPr' and el.get('date1904') in ('1', 'true'):
            epoch = datetime.date(1904, 1, 1)
    sheets = [el for el in wb.iter() if local(el.tag) == 'sheet']
    rid = next(v for k, v in sheets[0].attrib.items() if local(k) == 'id')
    rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    target = next(el.get('Target') for el in rels.iter() if el.get('Id') == rid)
    path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
    strings = []
    if 'xl/sharedStrings.xml' in names:
        for si in ET.fromstring(z.read('xl/sharedStrings.xml')):
            strings.append(rich_text(si))
    date_styles = []
    if 'xl/styles.xml' in names:
        st = ET.fromstring(z.read('xl/styles.xml'))
        custom = {el.get('numFmtId'): el.get('formatCode') or '' for el in st.iter() if local(el.tag) == 'numFmt'}
        for el in st:
            if local(el.tag) == 'cellXfs':
                for xf in el:
                    fid = xf.get('numFmtId') or '0'
                    date_styles.append(int(fid) in BUILTIN_DATE_FMTS if fid not in custom else is_date_format(custom[fid]))
    rows = []
    for row in ET.fromstring(z.read(path)).iter():
        if local(row.tag) != 'row':
            continue
        out, nxt = [], 0
        for c in row:
            if local(c.tag) != 'c':
                continue
            i = col_index(c.get('r')) if c.get('r') else nxt
            nxt = i + 1
            t, s = c.get('t'), int(c.get('s') or 0)
            v = next((x.text or '' for x in c if local(x.tag) == 'v'), '')
            if t == 's':
                val = (strings[int(v)] if v != '' else '', None)
            elif t == 'inlineStr':
                val = (next((rich_text(x) for x in c if local(x.tag) == 'is'), ''), None)
            elif t in (None, 'n') and v != '' and s < len(date_styles) and date_styles[s]:
                val = (v, epoch + datetime.timedelta(days=math.floor(float(v))))
            elif t == 'b':
                val = ('TRUE' if v == '1' else 'FALSE', None)
            else:
                val = (v, None)
            while len(out) < i:
                out.append(('', None))
            out.append(val)
        rows.append(out)
    return rows, sheets[0].get('name') or 'Sheet1'


# ---------- random codes ----------
def mulberry32(seed):
    state = [seed & 0xFFFFFFFF]

    def nxt(n):
        state[0] = (state[0] + 0x6D2B79F5) & 0xFFFFFFFF
        t = state[0]
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        t = (t ^ (t >> 14)) & 0xFFFFFFFF
        return math.floor(t / 4294967296 * n)
    return nxt


def shuffled(items, rand):
    a = list(items)
    for i in range(len(a) - 1, 0, -1):
        j = rand(i + 1)
        a[i], a[j] = a[j], a[i]
    return a


# ---------- the run ----------
def is_blank(cell):
    return cell[0].strip() == '' and cell[1] is None


def find_header(rows):
    counts = [sum(1 for c in r if not is_blank(c)) for r in rows[:200]]
    most = max(counts or [0])
    for i, r in enumerate(rows[:30]):
        filled = [c for c in r if not is_blank(c)]
        if len(filled) >= max(2, math.ceil(0.6 * most)) and all(c[1] is None and not NUMBER.match(c[0].strip())
                                                               and parse_date(c) is None for c in filled):
            return i
    return 0


def scrub(text, counts):
    if NUMBER.match(text.strip()):
        return text
    for pat, repl, what in INLINE:
        text, n = pat.subn(repl, text)
        if n:
            counts[what] = counts.get(what, 0) + n
    return text


def run(filename, data, overrides=None, seed=None):
    """Returns {output file name: text}. overrides maps a column header to keep, drop, code or year."""
    overrides = overrides or {}
    stem = re.sub(r'\.(csv|xlsx|txt)$', '', Path(filename).name, flags=re.I)
    if data[:2] == b'PK':
        rows, sheet = read_xlsx(data)
    else:
        rows, sheet = read_csv(data), None
    if not rows:
        raise ValueError('the file has no rows')
    h = find_header(rows)
    width = max(len(r) for r in rows)
    headers, seen = [], {}
    for i in range(width):
        name = rows[h][i][0].strip() if i < len(rows[h]) else ''
        name = name or 'Column %d' % (i + 1)
        seen[name] = seen.get(name, 0) + 1
        headers.append(name if seen[name] == 1 else '%s (%d)' % (name, seen[name]))
    body = [r + [('', None)] * (width - len(r)) for r in rows[h + 1:]]
    body = [r for r in body if not all(is_blank(c) for c in r)]

    cols = []
    for i, name in enumerate(headers):
        action, reason = classify(name)
        filled = [r[i] for r in body if not is_blank(r[i])]
        dates = sum(1 for c in filled if parse_date(c) is not None)
        if action in ('keep', 'drop') and filled and dates >= 0.8 * len(filled) and reason != 'provider details, not the patient':
            if action == 'keep' or reason in ('not recognized', 'a date part finer than the year'):
                action, reason = 'year', 'holds dates'
        if action == 'keep' and len(filled) >= 20:
            texts = [c[0] for c in filled if not NUMBER.match(c[0].strip())]
            if texts and len(set(texts)) > 0.5 * len(texts) and sum(len(x) for x in texts) / len(texts) > 25:
                action, reason = 'drop', 'free text, which can hold names'
        if name in overrides:
            action, reason = overrides[name], 'set by hand'
        cols.append({'i': i, 'name': name, 'action': action, 'reason': reason, 'filled': len(filled)})

    rand = mulberry32(seed) if seed is not None else (lambda n: secrets.randbelow(n))
    key_rows, prefix_at = [], 0
    for c in cols:
        if c['action'] != 'code':
            continue
        distinct = []
        known = set()
        for r in body:
            v = r[c['i']][0].strip()
            if v and v not in known:
                known.add(v)
                distinct.append(v)
        width_n = max(4, len(str(len(distinct))))
        prefix = PREFIXES[prefix_at % len(PREFIXES)] + ('' if prefix_at < len(PREFIXES) else str(prefix_at // len(PREFIXES)))
        prefix_at += 1
        order = shuffled(range(len(distinct)), rand)
        c['map'] = {distinct[k]: prefix + str(n + 1).zfill(width_n) for n, k in enumerate(order)}
        c['rank'] = {distinct[k]: n for n, k in enumerate(order)}
        c['distinct'] = len(distinct)
        for v in distinct:
            key_rows.append((c['name'], v, c['map'][v]))

    # Days between a service date and a payment date, before both are cut to the year
    year_cols = [c for c in cols if c['action'] == 'year']
    svc = next((c for c in year_cols if set(tokens(c['name'])) & W('dos service svc from')), None)
    pay = next((c for c in year_cols if c is not svc and set(tokens(c['name'])) & W(
        'tran trans transaction paid pay payment pmt posted post deposit check remit era process processed')), None)
    derived = 'Days from %s to %s' % (svc['name'], pay['name']) if svc and pay else None

    inline, missing_days, unreadable = {}, 0, {}
    out_rows = []
    for idx, r in enumerate(body):
        out = []
        for c in cols:
            cell, a = r[c['i']], c['action']
            if a == 'drop':
                continue
            v = cell[0]
            if a == 'code':
                v = c['map'].get(v.strip(), '')
            elif a == 'year':
                d = parse_date(cell)
                if d is None and not is_blank(cell):
                    unreadable[c['name']] = unreadable.get(c['name'], 0) + 1
                v = str(d.year) if d else ''
            elif a == 'age':
                m = re.match(r'\s*([0-9]+)', v)
                v = ('90+' if int(m.group(1)) > 89 else m.group(1)) if m else ''
            else:
                v = scrub(v, inline)
            out.append(v)
        if derived:
            a, b = parse_date(r[svc['i']]), parse_date(r[pay['i']])
            if a and b:
                out.append(str((b - a).days))
            else:
                out.append('')
                missing_days += 1
        first = next((c for c in cols if c['action'] == 'code'), None)
        rank = first['rank'].get(r[first['i']][0].strip(), -1) if first else None
        out_rows.append((rank, idx, out))
    if any(c['action'] == 'code' for c in cols):
        out_rows.sort(key=lambda x: (x[0], x[1]))
    else:
        order = shuffled(range(len(out_rows)), rand)
        out_rows = [out_rows[k] for k in order]

    out_headers = [c['name'] for c in cols if c['action'] != 'drop'] + ([derived] if derived else [])
    files = {
        stem + '-deidentified.csv': to_csv([out_headers] + [o for _, _, o in out_rows]),
        stem + '-deid-record.txt': record(filename, sheet, h, body, cols, derived, missing_days, inline, unreadable),
        stem + '-visit-code-key.csv': to_csv([['column', 'original value', 'visit code']] +
                                             sorted([list(k) for k in key_rows], key=lambda k: (k[0], k[2]))),
    }
    return files


def to_csv(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\r\n').writerows(rows)
    return buf.getvalue()


def plural(n, one, many=None):
    return '%s %s' % (format(n, ','), one if n == 1 else (many or one + 's'))


def record(filename, sheet, h, body, cols, derived, missing_days, inline, unreadable):
    L = ['De-identification record', '',
         'Tool: Serious AI de-identifier v%s. It runs on the computer it is opened on, and nothing is uploaded.' % VERSION,
         'Method: HIPAA Safe Harbor, 45 CFR 164.514(b)(2).',
         'Input: %s%s.' % (Path(filename).name, ', first sheet "%s"' % sheet if sheet else ''),
         'Rows: %s, under the header row. %s above it left out (report titles).' % (format(len(body), ','), plural(h, 'row')),
         '', 'Column by column']
    for c in cols:
        a, n = c['action'], c['name']
        if a == 'code':
            line = 'replaced with random codes, %s. Codes are not made from anything about the patient, and the key stays at the practice.' % plural(c['distinct'], 'distinct value')
        elif a == 'year':
            line = 'cut to the year, %s' % plural(c['filled'], 'cell')
            if unreadable.get(n):
                line += ' (%s not readable as a date, left blank)' % format(unreadable[n], ',')
            line += '.'
        elif a == 'age':
            line = 'kept, with ages over 89 shown as 90+.'
        elif a == 'keep':
            line = 'kept (%s).' % c['reason']
        else:
            line = 'dropped (%s).' % c['reason']
        if c['reason'] == 'set by hand':
            line = line.replace(' (set by hand).', '.').rstrip('.') + '. Set by hand.'
        L.append('- %s: %s' % (n, line))
    if derived:
        L.append('- %s: added, worked out before the dates were cut%s.' % (
            derived, ', left blank on %s missing a date' % plural(missing_days, 'row') if missing_days else ''))
    L.append('')
    if inline:
        L.append('Inside kept columns, replaced: ' + ', '.join('%s %s' % (format(v, ','), k) for k, v in inline.items()) + '.')
    else:
        L.append('Inside kept columns: no dates, phone numbers, emails, links or long numbers found.')

    kept = [c for c in cols if c['action'] != 'drop']
    def by(words):
        return [c['name'] for c in cols if c['action'] == 'drop' and set(tokens(c['name'])) & W(words)]
    def note(dropped, none):
        return 'dropped: %s.' % ', '.join(dropped) if dropped else none
    coded = [c['name'] for c in cols if c['action'] == 'code']
    years = [c['name'] for c in cols if c['action'] == 'year']
    L += ['', 'The 18 Safe Harbor identifiers',
          '1. Names: ' + note([c['name'] for c in cols if c['action'] == 'drop' and c['reason'] == 'a name or person field'],
                              'no name column found.') + ' Kept provider and payer names are not the patient\'s.',
          '2. Addresses, cities and ZIP codes: ' + note(by('address addr street city zip zipcode postal'), 'none found.'),
          '3. Dates: ' + ('cut to the year in %s.' % ', '.join(years) if years else 'no date column found.')
          + (' Birth dates dropped: %s.' % ', '.join(by('dob birth birthdate bday')) if by('dob birth birthdate bday') else '')
          + (' Ages over 89 shown as 90+.' if any(c['action'] == 'age' for c in cols) else ''),
          '4-5. Phone and fax numbers: ' + note(by('phone tel telephone fax mobile cell'), 'no column found') + ('' if by('phone tel telephone fax mobile cell') else '; any inside kept text are replaced.'),
          '6. Email addresses: ' + note(by('email mail mailing'), 'no column found') + ('' if by('email mail mailing') else '; any inside kept text are replaced.'),
          '7. Social security numbers: ' + note(by('ssn social'), 'no column found') + ('' if by('ssn social') else '; any inside kept text are replaced.'),
          '8-11. Medical record, health plan, account, certificate and license numbers, and 18. any other unique number: '
          + ('replaced with random codes in %s.' % ', '.join(coded) if coded else 'no such column found.')
          + ' Long numbers inside kept text are replaced.',
          '12-13. Vehicle and device identifiers: ' + note(by('vehicle vin plate device serial'), 'none found.'),
          '14-15. Web links and IP addresses: ' + note(by('url website ip'), 'no column found') + ('' if by('url website ip') else '; any inside kept text are replaced.'),
          '16-17. Biometric identifiers and photos: ' + note(by('biometric fingerprint face photo picture image'), 'none in a spreadsheet export.'),
          '', 'Columns in the de-identified copy: %s.' % (', '.join(c['name'] for c in kept) or 'none'),
          'Columns dropped: %s.' % (', '.join(c['name'] for c in cols if c['action'] == 'drop') or 'none'),
          '', 'Still the practice\'s call (Safe Harbor\'s second condition, 164.514(b)(2)(ii)): software can\'t know what '
          'the practice knows. Before sending, confirm nobody at the practice knows of a way the remaining details '
          '(a rare procedure, a very small payer) could point to a particular patient. Keep the visit-code key at '
          'the practice and never send it.', '']
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', help='the CSV or .xlsx export')
    ap.add_argument('--out', default='deidentified', help='folder for the three files (default: deidentified)')
    for a in ('keep', 'drop', 'code', 'year'):
        ap.add_argument('--' + a, action='append', default=[], metavar='COLUMN', help='%s this column, whatever its header says' % a)
    ap.add_argument('--seed', type=int, help=argparse.SUPPRESS)       # tests only: repeatable codes
    args = ap.parse_args(argv)
    overrides = {c: a for a in ('keep', 'drop', 'code', 'year') for c in getattr(args, a)}
    path = Path(args.file)
    files = run(path.name, path.read_bytes(), overrides, args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_bytes(text.encode('utf-8'))
    print('\n'.join(files[next(n for n in files if n.endswith('-deid-record.txt'))].split('\n')[8:]))
    print('Wrote %s to %s. Share only the -deidentified.csv. Keep the key at the practice.' % (', '.join(files), out))


if __name__ == '__main__':
    sys.exit(main())
