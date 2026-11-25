#!/usr/bin/env python3
"""Hands-off scrub for a Business Associate's own laptop: a watched folder that turns each raw billing export into
a Safe Harbor copy with deidentify.py, checks that copy again with an independent leak check, and releases it only
if every check passes. No AI and no network: standard library only, and nothing here reads the network.

    python3 deid_watch.py run-once            # what launchd runs when a file lands in the inbox
    python3 deid_watch.py status              # folders, gate, last runs (counts only)

Folders (override with --root / --outbox):
  ~/PHI-Work/inbox                drop the raw export here (or point the PHI browser profile's downloads here)
  ~/PHI-Work/keys-for-practice    the visit-code key, to hand back to the practice (never to an AI tool)
  ~/PHI-Work/quarantine           raw file + outputs when any check fails, for a person to look at
  ~/Serious-AI/deid-outbox        ONLY passing -deidentified.csv, -deid-record.txt and -leak-check.txt land here

Gate: nothing runs until ~/PHI-Work/ARMED exists (one line: the date the Readiness Notice went out). Without it a
raw file is left untouched in the inbox. That keeps BAA section 4.4 in the tool, not in anyone's memory.

What leaves ~/PHI-Work: the de-identified copy, its record, and a leak-check report that holds counts and column
headers only, never a cell value. Output files are named by run time and the report's leading words, so a raw file
name can't carry a patient name into the outbox.
"""
import argparse, csv, hashlib, io, os, re, shutil, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import deidentify as di  # noqa: E402

WATCH_VERSION = '0.1.0'
SETTLE_SECONDS = 20                 # a file must sit unchanged this long (a download still in progress is skipped)
PARTIAL = re.compile(r'\.(crdownload|download|part|partial|tmp)$|^~\$|^\.', re.I)
OK_EXT = re.compile(r'\.(csv|xlsx|txt)$', re.I)

# ---------- the independent leak check ----------
# Written separately from deidentify.py's own rules on purpose: it doesn't ask how a column was classified, it
# compares the copy against the raw file and against patterns, so a mistake in one is caught by the other.
DATE_IN_TEXT = re.compile(
    r'\b[0-9]{1,2}[/-][0-9]{1,2}[/-](?:[0-9]{4}|[0-9]{2})\b|\b(?:19|20)[0-9]{2}[/-][0-9]{1,2}[/-][0-9]{1,2}\b'
    r'|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? [0-9]{1,2}(?:st|nd|rd|th)?\b'
    r'|\b[0-9]{1,2}[- ](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b(?:19|20)[0-9]{6}\b', re.I)
PATTERNS = {
    'email address': re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    'web link': re.compile(r'https?://|www\.', re.I),
    'social security number': re.compile(r'\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b'),
    'phone number': re.compile(r'(?:\([0-9]{3}\) ?|\b[0-9]{3}[-. ])[0-9]{3}[-. ][0-9]{4}\b'),
    'IP address': re.compile(r'\b[0-9]{1,3}(?:\.[0-9]{1,3}){3}\b'),
    'full or partial date': DATE_IN_TEXT,
}
AMOUNT_HDR = re.compile(r'amt|amount|billed|allowed|alwd|aprv|paid|pay|charge|price|fee|total|balance|bal|units?\b|qty|'
                        r'days|count|rvu|rate|adj|copay|coins|deduct', re.I)
MONEY = re.compile(r'^-?\(?\$?[0-9,]*\.[0-9]{1,2}\)?$')
LONG_INT = re.compile(r'(?<![0-9.])[0-9]{6,}(?![0-9]*\.[0-9])')
TOKEN = re.compile(r'[a-z0-9]{5,}')
CODE_VALUE = re.compile(r'^[A-Z][0-9]*[0-9]{4,}$')
PERSON_HDR = re.compile(r'patient|\bpt\b|dob|birth|ssn|social|phone|fax|e-?mail|address|street|city|zip|postal|'
                        r'member|subscriber|insured|guarantor|mrn|chart|policy|first|last|middle', re.I)
ALLOWED_HDR = re.compile(r'payer|payor|provider|referring|rendering|facility|location|clinic|plan|insurance|ins\b', re.I)


def raw_table(name, data):
    rows = di.read_xlsx(data)[0] if data[:2] == b'PK' else di.read_csv(data)
    h = di.find_header(rows)
    width = max(len(r) for r in rows)
    headers, seen = [], {}
    for i in range(width):
        n = rows[h][i][0].strip() if i < len(rows[h]) else ''
        n = n or 'Column %d' % (i + 1)
        seen[n] = seen.get(n, 0) + 1
        headers.append(n if seen[n] == 1 else '%s (%d)' % (n, seen[n]))
    body = [r + [('', None)] * (width - len(r)) for r in rows[h + 1:]]
    body = [r for r in body if not all(di.is_blank(c) for c in r)]
    return headers, body


def leak_check(name, data, deid_csv):
    """Returns (passed, report lines). Report lines hold counts and column headers only."""
    rh, body = raw_table(name, data)
    out = list(csv.reader(io.StringIO(deid_csv, newline='')))
    oh, orows = out[0], out[1:]
    checks = []

    def check(label, failures, detail=''):
        checks.append((label, failures, detail))

    # 1. rows reconcile
    check('rows: every raw row has one output row (raw %d, output %d)' % (len(body), len(orows)),
          0 if len(body) == len(orows) else abs(len(body) - len(orows)))

    # 2. headers: nothing that names a person-level field
    def all_codes(j):
        vals = [o[j].strip() for o in orows if j < len(o) and o[j].strip()]
        return all(CODE_VALUE.match(v) or re.fullmatch(r'(19|20)[0-9]{2}|90\+|[0-9]{1,2}', v) for v in vals)
    bad_h = [h for j, h in enumerate(oh) if PERSON_HDR.search(h) and not ALLOWED_HDR.search(h) and not all_codes(j)]
    check('headers: no patient-level field kept in the clear (coded or year-only columns are fine)', len(bad_h), ', '.join(bad_h))

    # 3. raw values from every column that was NOT passed through verbatim must not reappear anywhere. Output rows
    # are reordered, so "passed through verbatim" compares the column's values as a bag, not row by row.
    def transformed(j):
        vals = [o[j].strip() for o in orows if j < len(o) and o[j].strip()]
        return bool(vals) and (sum(1 for v in vals if CODE_VALUE.match(v)) >= 0.9 * len(vals) or
                               all(re.fullmatch(r'(19|20)[0-9]{2}|90\+|[0-9]{1,2}', v) for v in vals))
    kept_same = {h for h in rh if h in oh and not transformed(oh.index(h))}
    kept_vals = {r[i][0].strip().lower() for i, h in enumerate(rh) if h in kept_same for r in body}
    kept_tokens = {t for v in kept_vals for t in TOKEN.findall(v)}
    secret, secret_tokens = set(), set()
    for i, h in enumerate(rh):
        if h in kept_same:
            continue
        for r in body:
            v = r[i][0].strip().lower()
            if len(v) >= 3 and v not in kept_vals and not MONEY.match(v) and not re.fullmatch(r'-?[0-9]{1,4}', v):
                secret.add(v)
                secret_tokens |= {t for t in TOKEN.findall(v) if t not in kept_tokens and not re.fullmatch(r'(19|20)[0-9]{2}', t)}
    hits = 0
    cols_hit = set()
    for o in orows:
        for j, cell in enumerate(o):
            c = cell.strip().lower()
            if c and (c in secret or any(t in secret_tokens for t in TOKEN.findall(c))):
                hits += 1
                cols_hit.add(oh[j] if j < len(oh) else '?')
    check('raw values from removed, coded or date columns: none reappear (%s values, %s word and number pieces checked)'
          % (format(len(secret), ','), format(len(secret_tokens), ',')), hits, ', '.join(sorted(cols_hit)))

    # 4. patterns in every output cell (amount columns may hold plain money)
    for label, pat in PATTERNS.items():
        n, cols = 0, set()
        for o in orows:
            for j, cell in enumerate(o):
                if cell and not MONEY.match(cell.strip()) and pat.search(cell):
                    n += 1
                    cols.add(oh[j] if j < len(oh) else '?')
        check('patterns: no %s in any cell' % label, n, ', '.join(sorted(cols)))
    n, cols = 0, set()
    for o in orows:
        for j, cell in enumerate(o):
            h = oh[j] if j < len(oh) else ''
            if cell and not AMOUNT_HDR.search(h) and LONG_INT.search(cell):
                n += 1
                cols.add(h)
    check('patterns: no number of 6 or more digits outside amount columns', n, ', '.join(sorted(cols)))

    # 5. columns that held dates in the raw file now hold a 4-digit year or nothing
    n, cols = 0, set()
    for i, h in enumerate(rh):
        filled = [r[i] for r in body if not di.is_blank(r[i])]
        if h not in oh or not filled:
            continue
        if sum(1 for c in filled if di.parse_date(c) is not None) < 0.8 * len(filled):
            continue
        j = oh.index(h)
        for o in orows:
            v = o[j].strip() if j < len(o) else ''
            if v and not (re.fullmatch(r'(19|20)[0-9]{2}', v)):
                n += 1
                cols.add(h)
    check('dates: every date column holds a year only', n, ', '.join(sorted(cols)))

    # 6. a kept column must not look like an identifier (many distinct long numbers)
    n, cols = 0, []
    for j, h in enumerate(oh):
        vals = [o[j].strip() for o in orows if j < len(o) and o[j].strip()]
        if len(vals) < 20 or AMOUNT_HDR.search(h):
            continue
        idlike = [v for v in vals if re.fullmatch(r'[A-Za-z]{0,3}-?[0-9]{5,}', v) and not CODE_VALUE.match(v)]
        if len(idlike) >= 0.5 * len(vals) and len(set(idlike)) >= 0.3 * len(idlike):
            n += 1
            cols.append(h)
    check('identifiers: no kept column looks like a list of account, encounter or member numbers', n, ', '.join(cols))

    # 7. ages over 89
    n = 0
    for j, h in enumerate(oh):
        if re.search(r'\bage\b', h, re.I):
            n += sum(1 for o in orows if j < len(o) and o[j].strip().isdigit() and int(o[j]) > 89)
    check('ages: none over 89 (shown as 90+)', n)

    # 8. person-name words from removed name-like columns don't appear in kept text
    name_cols = [i for i, h in enumerate(rh) if re.search(r'name|patient|guarantor|insured|subscriber', h, re.I)
                 and not ALLOWED_HDR.search(h) and h not in kept_same]
    words = set()
    for i in name_cols:
        for r in body:
            words |= {w for w in re.findall(r'[a-z]{3,}', r[i][0].lower())}
    kept_words = {w for v in kept_vals for w in re.findall(r'[a-z]{3,}', v)}
    words -= kept_words
    n = 0
    if words:
        for o in orows:
            for cell in o:
                if any(w in words for w in re.findall(r'[a-z]{3,}', cell.lower())):
                    n += 1
    check('names: no word from a removed name column appears (%s words checked)' % format(len(words), ','), n)

    passed = all(f == 0 for _, f, _ in checks)
    lines = ['%s  %s%s' % ('PASS' if f == 0 else 'FAIL (%s)' % format(f, ','), label,
                           (' [columns: %s]' % d) if f and d else '') for label, f, d in checks]
    return passed, lines


# ---------- the run ----------
def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


LABEL_WORDS = set('cpt payment payments report charge charges remit remits era eras ar aging transaction transactions detail '
                  'export billing claims claim denial denials collections production procedure summary ledger'.split())


def safe_label(stem):
    """Only words from a short fixed list survive, so a raw file name can't carry a person's name out."""
    words = [w.lower() for w in re.findall(r'[A-Za-z]+', stem) if w.lower() in LABEL_WORDS][:3]
    return '-'.join(words) or 'export'


def period_months(stem):
    ds = re.findall(r'([0-9]{1,2})[-/.]([0-9]{1,2})[-/.]([0-9]{4})', stem)
    if len(ds) >= 2:
        a, b = [datetime(int(y), int(m), int(d)) for m, d, y in ds[:2]]
        return round(((b - a).days + 1) / 30.4375, 1)
    return None


def notify(msg):
    if sys.platform == 'darwin':
        subprocess.run(['osascript', '-e', 'display notification "%s" with title "De-identifier"' % msg.replace('"', "'")],
                       capture_output=True)


def log(paths, line):
    with open(paths['log'], 'a') as f:
        f.write('%s  %s\n' % (now(), line))


def folders(root, outbox):
    p = dict(root=root, inbox=root / 'inbox', keys=root / 'keys-for-practice', quarantine=root / 'quarantine',
             gate=root / 'ARMED', log=root / 'deid-watch.log', outbox=outbox)
    for k in ('root', 'inbox', 'keys', 'quarantine'):
        p[k].mkdir(parents=True, exist_ok=True)
        os.chmod(p[k], 0o700)
    outbox.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write(path, text):
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


def process(p, f, seed=None):
    data = f.read_bytes()
    stem = re.sub(r'\.(csv|xlsx|txt)$', '', f.name, flags=re.I)
    run_id = datetime.now().strftime('%Y%m%d-%H%M%S')
    base = 'deid-%s-%s' % (run_id, safe_label(stem))
    sha = hashlib.sha256(data).hexdigest()
    tool_sha = hashlib.sha256((HERE / 'deidentify.py').read_bytes()).hexdigest()
    try:
        files = di.run(f.name, data, None, seed)
        deid = next(v for k, v in files.items() if k.endswith('-deidentified.csv'))
        rec = next(v for k, v in files.items() if k.endswith('-deid-record.txt'))
        key = next(v for k, v in files.items() if k.endswith('-visit-code-key.csv'))
        passed, lines = leak_check(f.name, data, deid)
        err = None
    except Exception as e:          # never print the exception text: a parser error can quote a cell
        passed, lines, err = False, ['FAIL  the de-identifier or the leak check stopped (%s)' % type(e).__name__], e
    months = period_months(stem)
    report = ['Leak check: %s' % ('PASSED, copy released' if passed else 'BLOCKED, nothing released'),
              'Run: %s, %s' % (run_id, base),
              'Tools: deid_watch %s, deidentify %s (sha256 %s)' % (WATCH_VERSION, di.VERSION, tool_sha[:16]),
              'Raw file sha256 (for the configuration record, not reversible): %s' % sha[:16],
              'Report period: %s months' % (months if months else 'unknown'),
              ''] + lines + ['', 'This report holds counts and column headers only, never a cell value.']
    text = '\n'.join(report) + '\n'
    if passed:
        atomic_write(p['keys'] / (base + '-visit-code-key.csv'), key)
        os.chmod(p['keys'] / (base + '-visit-code-key.csv'), 0o600)
        atomic_write(p['outbox'] / (base + '-deidentified.csv'), deid)
        atomic_write(p['outbox'] / (base + '-deid-record.txt'), rec.replace(stem, base))
        atomic_write(p['outbox'] / (base + '-leak-check.txt'), text)
        f.unlink()
        log(p, 'PASS %s: copy released, key kept for the practice, raw file deleted' % base)
        notify('Scrub passed. De-identified copy is in the outbox; key is in keys-for-practice.')
    else:
        q = p['quarantine'] / base
        q.mkdir(parents=True, exist_ok=True)
        os.chmod(q, 0o700)
        shutil.move(str(f), str(q / f.name))
        if err is None:
            atomic_write(q / (base + '-deidentified.csv'), deid)
            atomic_write(q / (base + '-visit-code-key.csv'), key)
        atomic_write(q / (base + '-leak-check.txt'), text)
        atomic_write(p['outbox'] / (base + '-BLOCKED.txt'), text)
        log(p, 'BLOCKED %s: raw file and outputs moved to quarantine' % base)
        notify('Scrub BLOCKED. Nothing was released. Open quarantine in PHI-Work.')
    return passed, base


def run_once(p, seed=None, settle=SETTLE_SECONDS):
    if not p['gate'].exists():
        waiting = [f for f in p['inbox'].iterdir() if f.is_file() and not PARTIAL.search(f.name)]
        if waiting:
            log(p, 'NOT ARMED: %d file(s) left untouched in the inbox (no Readiness Notice on file)' % len(waiting))
            notify('Not armed: no Readiness Notice on file, so the file was left untouched.')
        return []
    done = []
    for f in sorted(p['inbox'].iterdir()):
        if not f.is_file() or PARTIAL.search(f.name):
            continue
        if not OK_EXT.search(f.name):
            log(p, 'SKIPPED a file that is not .csv, .xlsx or .txt')
            continue
        if time.time() - f.stat().st_mtime < settle:
            continue                                    # still downloading; launchd runs again
        done.append(process(p, f, seed))
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['run-once', 'status'])
    ap.add_argument('--root', default=str(Path.home() / 'PHI-Work'))
    ap.add_argument('--outbox', default=str(Path.home() / 'Serious-AI' / 'deid-outbox'))
    ap.add_argument('--settle', type=int, default=SETTLE_SECONDS)
    a = ap.parse_args(argv)
    p = folders(Path(a.root).expanduser(), Path(a.outbox).expanduser())
    if a.command == 'status':
        print('armed: %s' % (p['gate'].read_text().strip() if p['gate'].exists() else 'NO (create %s)' % p['gate']))
        print('inbox files waiting: %d' % sum(1 for f in p['inbox'].iterdir() if f.is_file()))
        print('keys waiting to go back to the practice: %d' % sum(1 for f in p['keys'].iterdir() if f.is_file()))
        print('quarantined runs: %d' % sum(1 for f in p['quarantine'].iterdir() if f.is_dir()))
        if p['log'].exists():
            print('last log lines:\n' + ''.join(p['log'].read_text().splitlines(True)[-5:]))
        return
    for passed, base in run_once(p, settle=a.settle):
        print('%s %s' % ('PASS' if passed else 'BLOCKED', base))


if __name__ == '__main__':
    main()
