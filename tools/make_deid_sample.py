#!/usr/bin/env python3
"""Writes the de-identifier's sample: a made-up CPT payment report for a made-up practice, as a CSV and as an
Excel file with the same rows. Shaped like a practice-management payment export (title rows above the header,
an encounter number, dates of service and payment), plus the columns a messier export might add (a patient name,
a member ID, an age, a phone number, free-text notes) so every rule gets exercised. Sample data, every row.

    python3 tools/make_deid_sample.py
"""
import csv, io, random, zipfile
from datetime import date, timedelta
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / 'sample-data' / 'deid'
HEADER = ['Enc#', 'Patient Name', 'Payer Name', 'DOS', 'CPT', 'Billed Amt', 'Aprv Amt', 'Alwd Amt', 'Tran Dt',
          'Pay Amt', 'Source', 'Sv It Desc', 'Unit Price', 'Fin Class', 'Enc Referring', 'Member ID', 'Age',
          'Remark', 'Notes', 'Pt Phone']
TITLE = [['Sample Practice: CPT Payment Report 11/21/2025 to 11/20/2026'],
         ['Sample data. Made-up rows for a made-up practice.'], []]
CODES = [('99213', 'OFFICE O/P EST LOW 20 MIN', 165.00, 92.50), ('99214', 'OFFICE O/P EST MOD 30 MIN', 225.00, 130.10),
         ('99204', 'OFFICE O/P NEW MOD 45 MIN', 310.00, 168.40), ('36415', 'ROUTINE VENIPUNCTURE', 25.00, 3.00),
         ('G2211', 'COMPLEX E/M VISIT ADD ON', 40.00, 16.05), ('81003', 'URINALYSIS AUTO W/O SCOPE', 18.00, 2.25)]
PAYERS = [('SAMPLE PLAN A', 'Commercial'), ('SAMPLE PLAN B', 'Medicare'), ('SAMPLE PLAN C', 'HMO'), ('SAMPLE PLAN D', 'Medi-Cal')]
PROVIDERS = ['SAMPLE PROVIDER ONE MD', 'SAMPLE PROVIDER TWO PA', 'SAMPLE PROVIDER THREE NP']
REMARKS = ['Paid as billed', 'Paid as billed', 'Paid as billed', 'Reduced to fee schedule', 'Denied, no auth',
           'Resubmitted 03/04/2026, called 805-555-0100', 'Applied to deductible']


def rows():
    rnd = random.Random(20261123)
    out = []
    for e in range(1, 31):
        enc = str(4410000 + e * 37)
        payer, fin = PAYERS[e % 4]
        dos = date(2025, 11, 21) + timedelta(days=rnd.randrange(0, 360))
        pay = dos + timedelta(days=rnd.randrange(9, 75))
        age = 92 if e == 7 else rnd.randrange(19, 88)
        for code, desc, billed, allowed in rnd.sample(CODES, rnd.randrange(1, 4)):
            paid = 0.0 if e % 9 == 0 else round(allowed * rnd.choice([1, 1, 1, 0.8]), 2)
            out.append([enc, 'SAMPLE PATIENT %02d' % e, payer, dos, code, '%.2f' % billed, '%.2f' % allowed, '%.2f' % allowed,
                        pay, '%.2f' % paid, 'ERA' if e % 5 else 'Paper', desc, '%.2f' % billed, fin,
                        PROVIDERS[e % 3], 'SMP%07d' % (e * 7919), str(age), rnd.choice(REMARKS),
                        'Sample note %d: pt called on %s about the bill' % (e, pay.strftime('%m/%d/%Y')),
                        '(805) 555-%04d' % (100 + e)])
    return out


def write_csv(data):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator='\r\n')
    w.writerows(TITLE + [HEADER])
    for r in data:
        w.writerow([v.strftime('%m/%d/%Y') if isinstance(v, date) else v for v in r])
    (OUT / 'sample-cpt-payment-report.csv').write_text(buf.getvalue(), newline='')


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def col(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(data):
    """A minimal workbook: shared strings for the header, inline strings elsewhere, dates as serials in a date style."""
    shared = HEADER
    sheet = []
    allrows = TITLE + [HEADER] + data
    for ri, r in enumerate(allrows, 1):
        cells = []
        for ci, v in enumerate(r):
            ref = '%s%d' % (col(ci), ri)
            if ri == len(TITLE) + 1:
                cells.append('<c r="%s" t="s"><v>%d</v></c>' % (ref, shared.index(v)))
            elif isinstance(v, date):
                cells.append('<c r="%s" s="1"><v>%d</v></c>' % (ref, (v - date(1899, 12, 30)).days))
            elif ri > len(TITLE) + 1 and HEADER[ci] in ('Billed Amt', 'Aprv Amt', 'Alwd Amt', 'Pay Amt', 'Unit Price', 'Age'):
                cells.append('<c r="%s"><v>%s</v></c>' % (ref, v))
            else:
                cells.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, esc(v)))
        sheet.append('<row r="%d">%s</row>' % (ri, ''.join(cells)))
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    files = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="%s" xmlns:r="%s"><sheets>'
            '<sheet name="CPT Payment Report" sheetId="1" r:id="rId1"/></sheets></workbook>' % (ns, rns),
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>',
        'xl/styles.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="%s"><fonts count="1"><font/></fonts>'
            '<fills count="1"><fill/></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/></cellXfs></styleSheet>' % ns,
        'xl/sharedStrings.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>'
            % (ns, len(shared), len(shared), ''.join('<si><t>%s</t></si>' % esc(s) for s in shared)),
        'xl/worksheets/sheet1.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
            % (ns, ''.join(sheet)),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text)
    (OUT / 'sample-cpt-payment-report.xlsx').write_bytes(buf.getvalue())


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    data = rows()
    write_csv(data)
    write_xlsx(data)
    print('Wrote %d sample rows to %s' % (len(data), OUT))
