#!/usr/bin/env python3
"""Builds sample-data/pnl/: made-up QuickBooks exports and card statements for a made-up practice, in the layouts
QuickBooks Online and the card issuers use (late 2026). Every name is invented and carries SAMPLE. Sample data.

    python3 tools/make_pnl_sample.py

Planted for the tests (sample-data/pnl/answer-key.json lists them): personal charges on the practice card, a
family member on payroll, weekend and out-of-state dining, whole accounts with no detail, patient refunds that
name a patient, and decoys that must stay unflagged.
"""
import datetime, io, json, sys, zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'sample-data' / 'pnl'
MONTHS = [(2024, 11), (2024, 12)] + [(2025, m) for m in range(1, 13)]
MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
               'November', 'December']


def d(y, m, day):
    return datetime.date(y, m, day)


def first_weekday(y, m, wd, nth=1):
    """The nth weekday wd (Mon=0) of a month."""
    x = d(y, m, 1)
    while x.weekday() != wd:
        x += datetime.timedelta(days=1)
    return x + datetime.timedelta(days=7 * (nth - 1))


def qb_date(x):
    return x.strftime('%m/%d/%Y')


def amt(c):
    s = '{:,.2f}'.format(abs(c) / 100)
    return ('-' if c < 0 else '') + s


# ---------- line detail: (account, date, type, name, memo, cents) ----------
detail = []
card_amex, card_chase = [], []


def add(account, day, kind, name, memo, c, amex=None, chase=None):
    detail.append((account, day, kind, name, memo, c))
    if amex:
        card_amex.append((day, amex, c, 'Restaurant-Restaurant' if 'GRILL' in amex or 'STEAK' in amex else 'Merchandise & Supplies-Internet Purchase'))
    if chase:
        card_chase.append((day, chase, -c, 'Food & Drink', 'Sale'))


for y, m in MONTHS:
    # dues and subscriptions: Netflix on the practice card (planted), a medical society (business)
    add('6050 Dues & Subscriptions', d(y, m, 3), 'Expense', 'Netflix', 'NETFLIX.COM LOS GATOS CA', 2299,
        amex='NETFLIX.COM LOS GATOS CA')
    if m == 1:
        add('6050 Dues & Subscriptions', d(y, m, 9), 'Bill', 'Sample County Medical Association', 'Annual dues', 65000)
    # meals: weekday staff lunches (business), weekend steakhouse (planted, raised by weekends)
    add('6080 Meals & Entertainment', first_weekday(y, m, 2), 'Expense', '', 'PANERA BREAD #4411 SIMI VALLEY CA', 18450)
    add('6080 Meals & Entertainment', first_weekday(y, m, 5, 2), 'Expense', 'Sample Steakhouse',
        'SAMPLE STEAKHOUSE THOUSAND OAKS CA', 31200 + m * 100, amex='SAMPLE STEAKHOUSE THOUSAND OAKS CA')
    # medical supplies (business)
    add('6090 Medical Supplies', d(y, m, 12), 'Bill', 'McKesson Medical-Surgical', 'Invoice', 420000)
    add('6090 Medical Supplies', d(y, m, 20), 'Bill', 'Henry Schein', 'Invoice', 135000)
    # office supplies: Staples (business), Costco and Amazon (mixed use, Low)
    add('6100 Office Supplies', d(y, m, 6), 'Expense', 'Staples', 'STAPLES 00123 SIMI VALLEY CA', 9800)
    add('6100 Office Supplies', d(y, m, 14), 'Expense', '', 'COSTCO WHSE #0479 SIMI VALLEY CA', 21000)
    add('6100 Office Supplies', d(y, m, 22), 'Expense', '', 'AMAZON MKTPL*2K4HX1 AMZN.COM/BILL WA', 6400)
    # salaries: staff (names coded), and a family member on payroll (planted)
    for who, c in (('Sample Staffer Alpha', 420000), ('Sample Staffer Bravo', 390000), ('Sample Staffer Charlie', 380000),
                   ('Pat Samplefam', 400000)):
        add('6140 Salaries & Wages', d(y, m, 15), 'Payroll Check', who, '', c)
    # patient refunds and insurance income: names on these rows are patients
    add('4900 Patient Refunds', d(y, m, 18), 'Check', 'Sample Patient Alpha', 'Refund of overpayment', -8500)
    add('4900 Patient Refunds', d(y, m, 25), 'Check', 'Sample Patient Bravo', 'Pt credit balance refund', -6500)
    add('4000 Insurance Payments', d(y, m, 10), 'Deposit', 'Sample Patient Charlie', 'Copay', 4000)

# travel: a Maui resort (planted, leisure), a conference flight and hotel (Medium)
add('6160 Travel', d(2025, 7, 12), 'Expense', '', 'SAMPLE BEACH RESORT LAHAINA HI', 412500, amex='SAMPLE BEACH RESORT LAHAINA HI')
add('6160 Travel', d(2025, 7, 13), 'Expense', '', 'SAMPLE LUAU GRILL KAILUA HI', 28800, amex='SAMPLE LUAU GRILL KAILUA HI')
add('6160 Travel', d(2025, 7, 14), 'Expense', '', 'SAMPLE SURF GRILL LAHAINA HI', 19900, amex='SAMPLE SURF GRILL LAHAINA HI')
add('6160 Travel', d(2025, 4, 2), 'Expense', '', 'SOUTHWEST AIRLINES DALLAS TX', 38900)
add('6160 Travel', d(2025, 4, 3), 'Expense', '', 'MARRIOTT SAMPLE CONFERENCE CTR AUSTIN TX', 61200)

# card-only charges: the practice Amex and the owner's Chase card, never booked line by line in QuickBooks
for y, m in [(2025, mm) for mm in range(1, 13)]:
    card_amex.append((first_weekday(y, m, 6), 'SAMPLE COUNTRY CLUB SIMI VALLEY CA', 85000, 'Entertainment-Other'))
    card_chase.append((first_weekday(y, m, 5), 'WHOLEFDS SMP 10234 WESTLAKE VLG CA', -14200 - m * 50, 'Groceries', 'Sale'))
    card_chase.append((d(y, m, 8), 'SHELL OIL 57444 SIMI VALLEY CA', -7100, 'Gas', 'Sale'))
    card_chase.append((d(y, m, 27), 'Payment Thank You-Mobile', 250000, '', 'Payment'))
    card_amex.append((d(y, m, 28), 'AUTOPAY PAYMENT - THANK YOU', -300000, ''))
card_amex.append((d(2025, 3, 15), 'NORDSTROM #0712 CANOGA PARK CA', 64900, 'Merchandise & Supplies-Department Stores'))
card_amex.append((d(2025, 9, 20), 'NORDSTROM #0712 CANOGA PARK CA', 38800, 'Merchandise & Supplies-Department Stores'))
card_amex.append((d(2025, 8, 18), 'SAMPLE PREP ACADEMY TUITION OXNARD CA', 950000, 'Business Services-Other'))
card_amex.append((d(2025, 5, 4), 'TARGET 00012345 SIMI VALLEY CA', 12900, 'Merchandise & Supplies-General Retail'))
card_chase.append((d(2025, 6, 7), 'Zelle payment to SAMPLE SITTER JPM99A1B2C', -60000, '', 'Sale'))
card_chase.append((d(2025, 10, 4), 'Zelle payment to SAMPLE SITTER JPM99D3E4F', -60000, '', 'Sale'))
card_chase.append((d(2025, 2, 14), 'REGAL CINEMAS SAMPLE 12 SIMI VALLEY CA', -4800, 'Entertainment', 'Sale'))
# a mirror: the same practice-card charge shows under the card account in the detail, and must count once
add('2100 Sample Practice Amex', d(2025, 5, 4), 'Expense', '', 'TARGET 00012345 SIMI VALLEY CA', -12900)


# ---------- monthly amounts for the P&L ----------
def month_sum(account):
    out = {}
    for a, day, _, _, _, c in detail:
        if a == account:
            out[(day.year, day.month)] = out.get((day.year, day.month), 0) + c
    return out


flat = lambda c: {ym: c for ym in MONTHS}
pl = [  # (section, label, monthly dict or None for a heading, depth)
    ('Income', '4000 Insurance Payments', {ym: 17500000 + (ym[1] % 3) * 120000 for ym in MONTHS}),
    ('Income', '4100 Capitation Income', flat(3000000)),
    ('Income', '4900 Patient Refunds', month_sum('4900 Patient Refunds')),
    ('Cost of Goods Sold', '5000 Vaccines and Drugs', flat(600000)),
    ('Expenses', '6000 Advertising', None), ('Expenses', '  Advertising', flat(80000)), ('Expenses', 'Total for 6000 Advertising', 'total'),
    ('Expenses', '6010 Auto Expense', flat(90000)),
    ('Expenses', '6020 Bank Charges', flat(30000)),
    ('Expenses', '6030 Billing Service', flat(900000)),
    ('Expenses', '6040 Charitable Contributions', flat(50000)),
    ('Expenses', '6050 Dues & Subscriptions', month_sum('6050 Dues & Subscriptions')),
    ('Expenses', '6060 Insurance', flat(120000)), ('Expenses', '  Workers Compensation', flat(40000)),
    ('Expenses', 'Total for 6060 Insurance', 'total'),
    ('Expenses', '6070 Malpractice Insurance', flat(250000)),
    ('Expenses', '6080 Meals & Entertainment', month_sum('6080 Meals & Entertainment')),
    ('Expenses', '6090 Medical Supplies', month_sum('6090 Medical Supplies')),
    ('Expenses', '6100 Office Supplies', month_sum('6100 Office Supplies')),
    ('Expenses', '6110 Officer Compensation', flat(2500000)),
    ('Expenses', '6120 Payroll Taxes', flat(500000)),
    ('Expenses', '6130 Rent', flat(1200000)),
    ('Expenses', '6140 Salaries & Wages', month_sum('6140 Salaries & Wages')),
    ('Expenses', '6141 Staff Wages - Clinical and Front Office', flat(4500000)),
    ('Expenses', '6145 Provider Salaries - NP and PA', flat(6000000)),
    ('Expenses', '6150 Software', flat(200000)),
    ('Expenses', '6160 Travel', month_sum('6160 Travel')),
    ('Expenses', '6170 Utilities', None), ('Expenses', '  6171 Electric', flat(90000)), ('Expenses', '  6172 Water', flat(15000)),
    ('Expenses', 'Total for 6170 Utilities', 'total'),
    ('Expenses', '6180 Lab Fees', flat(200000)),
    ('Expenses', '6190 Uncategorized Expense', flat(30000)),
    ('Other Income', '8000 Interest Income', flat(5000)),
    ('Other Expenses', '7000 Interest Expense', flat(40000)),
    ('Other Expenses', '7010 Depreciation', flat(150000)),
]


def pnl_rows():
    """Rows of cells for the QuickBooks Online Profit and Loss by month layout; cells are (text, indent)."""
    rows = [['Profit and Loss'], ['Sample Family Medicine (Sample data)'], ['November 2024-December, 2025'], []]
    rows.append(['', ] + ['%s %d' % (MONTH_NAMES[m - 1], y) for y, m in MONTHS] + ['Total'])
    totals = {}
    sec = None

    def money_cells(vals, dollar=False):
        cells = [amt(vals.get(ym, 0)) if vals.get(ym, 0) else '' for ym in MONTHS]
        t = sum(vals.get(ym, 0) for ym in MONTHS)
        cells.append(('$' if dollar else '') + amt(t) if not dollar or t >= 0 else '-$' + amt(-t))
        return cells

    def close(section):
        rows.append(['Total for ' + section] + money_cells(totals[section], True))

    group = {}
    for section, label, vals in pl:
        if section != sec:
            if sec:
                close(sec)
            sec = section
            rows.append([section])
            totals[section] = {}
        if vals is None:
            rows.append([label])
            group = {}
        elif vals == 'total':
            rows.append([label] + money_cells(group, True))
        else:
            rows.append([label] + money_cells(vals))
            for ym in MONTHS:
                totals[section][ym] = totals[section].get(ym, 0) + vals.get(ym, 0)
                if label.startswith('  ') or label in ('6060 Insurance',):
                    group[ym] = group.get(ym, 0) + vals.get(ym, 0)
        if label == '6060 Insurance':
            group = dict((ym, vals.get(ym, 0)) for ym in MONTHS)
            rows[-1] = [label] + money_cells(vals)
    close(sec)
    net = {}
    for ym in MONTHS:
        net[ym] = (totals['Income'][ym] + totals['Other Income'][ym] - totals['Cost of Goods Sold'][ym]
                   - totals['Expenses'][ym] - totals['Other Expenses'][ym])
    rows.append(['Net Income'] + money_cells(net, True))
    rows += [[], [], ['Accrual Basis Friday, January 9, 2026 04:36 PM GMTZ']]
    return rows


def to_csv(rows):
    out = io.StringIO()
    import csv
    w = csv.writer(out, lineterminator='\r\n')
    width = max(len(r) for r in rows)
    for r in rows:
        w.writerow([c.strip() if isinstance(c, str) else c for c in r] + [''] * (width - len(r)))
    return out.getvalue()


def col_name(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def to_xlsx(rows, sheet='Profit and Loss'):
    """A minimal workbook like QuickBooks Online's export: labels indented by cell style, some amounts stored as
    formula text with no cached value."""
    xml_rows = []
    for ri, r in enumerate(rows, 1):
        cells = []
        for ci, v in enumerate(r):
            if v == '':
                continue
            ref = '%s%d' % (col_name(ci), ri)
            num = v.replace(',', '').replace('$', '')
            if ci > 0 and num.lstrip('-').replace('.', '', 1).isdigit():
                if ri % 4 == 0:
                    cells.append('<c r="%s"><f>%s</f></c>' % (ref, num))
                else:
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, num))
            else:
                style = ' s="1"' if v.startswith('  ') else ''
                cells.append('<c r="%s" t="inlineStr"%s><is><t>%s</t></is></c>' % (ref, style, escape(v.strip())))
        xml_rows.append('<row r="%d">%s</row>' % (ri, ''.join(cells)))
    files = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>' % sheet,
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        'xl/styles.xml': '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="0" applyAlignment="1"><alignment indent="1"/></xf></cellXfs></styleSheet>',
        'xl/worksheets/sheet1.xml': '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>%s</sheetData></worksheet>' % ''.join(xml_rows),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, (2026, 1, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text)
    return buf.getvalue()


def detail_rows():
    rows = [['Transaction Detail by Account'], ['Sample Family Medicine (Sample data)'], ['November 2024-December, 2025'], [],
            ['', 'Transaction date', 'Transaction type', 'Num', 'Name', 'Memo/Description', 'Account full name', 'Amount', 'Balance']]
    accounts = sorted(set(a for a, *_ in detail))
    for a in accounts:
        rows.append([a])
        bal = 0
        other = '2100 Sample Practice Amex' if a != '2100 Sample Practice Amex' else '6100 Office Supplies'
        for acc, day, kind, name, memo, c in sorted(x for x in detail if x[0] == a):
            bal += c
            rows.append(['', qb_date(day), kind, '', name, memo, other, amt(c), amt(bal)])
        rows.append(['Total for ' + a, '', '', '', '', '', '', '$' + amt(bal) if bal >= 0 else '-$' + amt(-bal), ''])
    rows.append(['', 'TOTAL', '', '', '', '', '', '', ''])
    rows += [[], [], ['Accrual Basis Friday, January 9, 2026 04:41 PM GMTZ']]
    return rows


def amex_csv():
    rows = [['Date', 'Description', 'Card Member', 'Account #', 'Amount', 'Category']]
    for day, desc, c, cat in sorted(card_amex):
        if day.year < 2025:
            continue
        rows.append([qb_date(day), desc, 'SAMPLE OWNER', '-41007', '%.2f' % (c / 100), cat])
    return to_csv(rows)


def chase_csv():
    rows = [['Transaction Date', 'Post Date', 'Description', 'Category', 'Type', 'Amount', 'Memo']]
    for day, desc, c, cat, kind in sorted(card_chase, reverse=True):
        if day.year < 2025:
            continue
        rows.append([qb_date(day), qb_date(day + datetime.timedelta(days=1)), desc, cat, kind, '%.2f' % (c / 100), ''])
    return to_csv(rows)


def vendor_rows():
    """Expenses by Vendor Summary, the way a practice might send it with no line detail. Patient refund checks
    show the patient as the vendor."""
    rows = [['Expenses by Vendor Summary'], ['Sample Family Medicine (Sample data)'], ['January 1-December 31, 2025'], [],
            ['Vendor', 'Total']]
    vend = (('', 412000), ('Sample Patient Alpha', 102000), ('Sample Patient Bravo', 78000),
                    ('McKesson Medical-Surgical', 5040000), ('Netflix', 27588), ('Nordstrom', 103700),
                    ('Sample Valley Lab Services', 2400000), ('Pat Samplefam', 4800000), ('Staples', 117600),
                    ('Costco', 252000), ('Sample Country Club', 1020000))
    for name, c in vend:
        rows.append([name, amt(c)])
    rows.append(['TOTAL', '$' + amt(sum(c for _, c in vend))])
    return rows


def main():
    global OUT
    if len(sys.argv) > 1:
        OUT = Path(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    p = pnl_rows()
    (OUT / 'Profit and Loss by Month.csv').write_text(to_csv(p), newline='')
    (OUT / 'Profit and Loss by Month.xlsx').write_bytes(to_xlsx(p))
    (OUT / 'Transaction Detail by Account.csv').write_text(to_csv(detail_rows()), newline='')
    (OUT / 'amex-practice-card.csv').write_text(amex_csv(), newline='')
    (OUT / 'chase-owner-card.csv').write_text(chase_csv(), newline='')
    (OUT / 'Expenses by Vendor Summary.csv').write_text(to_csv(vendor_rows()), newline='')
    key = {
        'high': ['Streaming and entertainment', 'Clothing and personal shopping', 'School, childcare and kids',
                 'Personal care, fitness and clubs', 'Leisure travel', '6040 Charitable Contributions',
                 'Family member', 'Sample Steakhouse', 'Groceries'],
        'medium': ['Panera', '6010 Auto Expense', '6190 Uncategorized Expense', 'Person-to-person transfer', 'Vehicle and fuel',
                   'Air travel', 'Hotels'],
        'low': ['Costco'],
        'never_flagged': ['Staples', 'McKesson Medical-Surgical', 'Henry Schein', 'Sample County Medical Association',
                          '6130 Rent', '6030 Billing Service', 'Target'],
        'never_written': ['Sample Patient Alpha', 'Sample Patient Bravo', 'Sample Patient Charlie', 'Pat Samplefam',
                          'Sample Staffer Alpha', 'SAMPLE SITTER', 'Refund of overpayment', 'credit balance refund',
                          'Copay'],
    }
    (OUT / 'answer-key.json').write_text(json.dumps(key, indent=2) + '\n')
    print('wrote', ', '.join(sorted(x.name for x in OUT.iterdir())))


if __name__ == '__main__':
    main()
