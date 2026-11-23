#!/usr/bin/env python3
"""P&L normalizer: turns a practice's raw QuickBooks exports into a normalized overhead P&L and a short list of
likely owner add-backs, on the practice's own computer.

    python3 pnl_normalizer.py "Profit and Loss by Month.xlsx" "Transaction Detail by Account.csv" amex.csv \
        --physicians 1 --apps 5.5 --family "Smith" --out pnl-output

Reads any mix of these, CSV or Excel (.xlsx), each told apart by its own headers:
  - Profit and Loss (by month, or one Total column)            the reported numbers
  - Transaction Detail by Account / General Ledger / Transaction List   line-level detail
  - Credit card or bank statement downloads                    line-level detail for the card owners use
  - Expenses by Vendor Summary                                 vendor totals, when there's no line detail
  - The add-back review file this tool wrote, with the owner's answers filled in
  - A benchmarks CSV (category, pct_of_revenue, source) to replace the built-in references

Writes five files:
  pnl-normalized-overhead.csv   overhead by MGMA cost category, before and after add-backs, against benchmarks
  pnl-add-back-review.csv       the short list for the owner: each likely add-back, why, and a column to answer
  pnl-line-detail.csv           every account and charge with its category and flag
  pnl-record.txt                what was read, what reconciled, what was masked, and every assumption
  pnl-name-key.csv              individuals' codes and real names. Keep it at the practice, never send it.

The first four hold no patient names: patient refund and income lines are collapsed to a yearly total with no
names, every individual's name becomes a code, and memos are read for matching but never written out.

Standard library only, Python 3.9 or newer. pnl-normalizer.html does the same thing in a browser, line for line,
and tests/test_pnl_normalizer.py requires both to write byte-identical files from the same input.
"""
import argparse, csv, datetime, io, math, re, sys, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

VERSION = '0.1.0'
LEVEL_NAMES = {3: 'High', 2: 'Medium', 1: 'Low', 0: ''}
OUT = ('pnl-normalized-overhead.csv', 'pnl-add-back-review.csv', 'pnl-line-detail.csv', 'pnl-record.txt',
       'pnl-name-key.csv')


# ---------- text ----------
def norm(s):
    """Lower case, every run of other characters one space, padded so ' phrase ' matches whole words."""
    return ' ' + ' '.join(re.sub(r'[^a-z0-9]+', ' ', s.lower()).split()) + ' '


def phrases(s):
    return [norm(p).strip() for p in s.split('|') if norm(p).strip()]


def hit(text, plist):
    """The first phrase found in normalized text, or None."""
    for p in plist:
        if ' ' + p + ' ' in text:
            return p
    return None


# ---------- MGMA cost categories (DataDive Cost & Revenue glossary, 2023) ----------
# key, label, built-in reference % of total medical revenue, source of that reference
CATEGORIES = [
    ('support_staff', 'Support staff (salaries, payroll taxes, benefits)', 320,
     'MGMA 2026 APP Staffing and Access report, primary care, 2025 data: roughly 30-34% at the median'),
    ('it', 'Information technology (software, EHR, phones, internet)', 19, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('drug', 'Drug supply', 34, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('med_surg', 'Medical and surgical supply', 17, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('building', 'Building and occupancy', 68, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('equipment', 'Furniture and equipment', 3, 'MGMA via AAFP FPM 2010, family medicine, 2008 data (depreciation left out)'),
    ('admin', 'Administrative supplies and services', 17, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('prof_liability', 'Professional liability insurance', 15, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('other_insurance', 'Other insurance premiums', 2, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('prof_fees', 'Outside professional fees (legal, consulting, accounting)', 9, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('marketing', 'Promotion and marketing', 4, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('lab', 'Clinical laboratory', 33, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('imaging', 'Radiology and imaging', 9, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
    ('ancillary', 'Other ancillary services', None, ''),
    ('billing', 'Billing and collections purchased services', 50, 'MGMA Stat 2025: revenue cycle about 5% of collections when outsourced'),
    ('management', 'Management fees paid to an MSO', None, ''),
    ('misc', 'Miscellaneous operating cost', 17, 'MGMA via AAFP FPM 2010, family medicine, 2008 data'),
]
TOTAL_REF = (664, 'MGMA Better Performers report (Dec 2023), primary care, 2022 data: $443,682 of $667,995 per FTE physician')
OTHER_CATS = [('provider_comp', 'Provider pay and benefits (physicians, NPs, PAs)'),
              ('non_operating', 'Interest, depreciation, amortization, income taxes'),
              ('revenue', 'Practice revenue'), ('other_income', 'Other income (not from patient care)'),
              ('card_lump', 'Card charges with no matching QuickBooks row (booked in a lump, account not known)')]
CAT_LABEL = dict([(c[0], c[1]) for c in CATEGORIES] + OTHER_CATS)
OVERHEAD = [c[0] for c in CATEGORIES]

# ---------- account rules: first match wins ----------
# (phrases, category, add-back level 0-3, kind, why)
COMP = phrases('salary|salaries|wages|wage|compensation|comp|payroll|bonus|bonuses|pay|draw guaranteed')
PROVIDER = phrases('physician|physicians|provider|providers|doctor|doctors|md|mds|np|nps|pa|pas|nurse practitioner|'
                   'nurse practitioners|physician assistant|physician assistants|mid level|midlevel|app|apps|clinician|clinicians')
OWNER_PAY = phrases('officer|officers|owner|owners|shareholder|shareholders|member|members|partner|partners')
ACCOUNT_RULES = [(phrases(p), c, lv, k, why) for p, c, lv, k, why in [
    ('personal protective|ppe', 'med_surg', 0, '', ''),
    ('personal property tax|property tax|property taxes', 'building', 0, '', ''),
    ('owner draw|owner draws|draws|shareholder draw|member draw|distribution|distributions|personal|owner expense|'
     'owner expenses|owners expense|shareholder expense|due from shareholder|due from owner', 'misc', 3, 'Personal',
     'an owner or personal account booked as a practice expense'),
    ('charitable|charity|donation|donations', 'misc', 3, 'Personal', 'charitable giving is the owner\'s choice, not a practice cost'),
    ('political|lobbying', 'misc', 3, 'Personal', 'political giving is not a practice cost'),
    ('life insurance|officer life|key man|keyman|owner disability', 'other_insurance', 3, 'Personal',
     'insurance on the owner\'s own life or income'),
    ('interest|depreciation|amortization|income tax|income taxes|franchise tax|state tax|federal tax|corporate tax|'
     'llc fee|gain on|loss on sale', 'non_operating', 0, '', ''),
    ('capitation', 'revenue', 0, '', ''),
    ('locum|locums|locum tenens|guaranteed payment|guaranteed payments', 'provider_comp', 0, '', ''),
    ('payroll service|payroll services|payroll processing|payroll fees|payroll fee', 'admin', 0, '', ''),
    ('payroll tax|payroll taxes|employer taxes|fica|futa|suta|sui|sdi|ett|salaries|salary|wages|wage|payroll|'
     'employee benefits|benefits|health insurance|group health|medical insurance|dental insurance|vision insurance|'
     'workers comp|workers compensation|worker s comp|worker s compensation|401k|401 k|retirement|pension|'
     'profit sharing|bonus|bonuses|staffing|temp agency|temporary help|contract labor|employee|employees',
     'support_staff', 0, '', ''),
    ('settlement|lawsuit|litigation|judgment|penalty|penalties|fines|relocation|moving expense|moving expenses|'
     'non recurring|nonrecurring|one time', 'misc', 2, 'One-time', 'looks like a one-time cost, not a running one'),
    ('billing|clearinghouse|revenue cycle|rcm|claims processing|collection agency|collection agencies|'
     'collections agency|availity|waystar|office ally|trizetto|change healthcare', 'billing', 0, '', ''),
    ('management fee|management fees|mso', 'management', 0, '', ''),
    ('malpractice|professional liability|med mal|medical liability', 'prof_liability', 0, '', ''),
    ('insurance|umbrella|cyber|general liability', 'other_insurance', 0, '', ''),
    ('lab|labs|laboratory|reference lab|quest|labcorp|pathology', 'lab', 0, '', ''),
    ('radiology|imaging|x ray|xray|ultrasound|mri', 'imaging', 0, '', ''),
    ('drug|drugs|pharmaceutical|pharmaceuticals|vaccine|vaccines|injectable|injectables|medication|medications|'
     'pharmacy', 'drug', 0, '', ''),
    ('medical supplies|medical supply|clinical supplies|clinic supplies|surgical|exam supplies|med supplies|'
     'mckesson|henry schein|medline', 'med_surg', 0, '', ''),
    ('club|clubs|country club|golf', 'misc', 3, 'Personal', 'club dues and golf are personal unless the owner shows otherwise'),
    ('dues and subscriptions|dues subscriptions', 'admin', 0, '', ''),
    ('software|computer|computers|it|technology|internet|ehr|emr|nextgen|electronic health|subscription|'
     'subscriptions|hosting|telephone|telephones|phone|phones|telecom|telecommunications|answering service|fax|'
     'data processing|cloud', 'it', 0, '', ''),
    ('equipment|furniture|copier|copiers|fixtures', 'equipment', 0, '', ''),
    ('rent|lease|leases|occupancy|utilities|utility|electric|electricity|gas and electric|gas electric|water|sewer|'
     'trash|waste|refuse|janitorial|cleaning|custodial|repairs|repair|maintenance|security|alarm|landscaping|'
     'pest control|building|facility|facilities|cam|common area', 'building', 0, '', ''),
    ('legal|attorney|attorneys|lawyer|accounting|accountant|cpa|bookkeeping|consulting|consultant|consultants|'
     'professional fees|professional services|tax preparation|credentialing|outside services', 'prof_fees', 0, '', ''),
    ('advertising|marketing|promotion|promotional|website|web design|seo|google ads|yelp|social media|sponsorship|'
     'sponsorships|public relations', 'marketing', 0, '', ''),
    ('office supplies|office supply|office expense|office expenses|postage|shipping|delivery|courier|printing|'
     'bank charges|bank fees|bank service|service charges|merchant fees|merchant|credit card fees|card processing|'
     'processing fees|dues|memberships|membership|publications|books|licenses|license|permits|permit|'
     'continuing education|education|training|cme|seminars|conference|conferences|recruiting|recruitment|uniforms|'
     'laundry|shredding|stationery|supplies|taxes|tax', 'admin', 0, '', ''),
    ('physical therapy|ancillary|dme|durable medical', 'ancillary', 0, '', ''),
    ('meals|meal|entertainment|travel|airfare|lodging|hotel|hotels|auto|automobile|autos|vehicle|vehicles|car|cars|'
     'fuel|gasoline|gas|mileage|parking|tolls|gift|gifts', 'misc', 2, 'Personal',
     'an account that often mixes personal and practice spending'),
    ('ask my accountant|uncategorized|uncategorised|suspense', 'misc', 2, 'Review', 'a catch-all account nobody has sorted'),
    ('miscellaneous|misc', 'misc', 1, 'Review', 'a catch-all account'),
]]


def account_rule(text, section):
    """(category, level, kind, why, mapped) for an account's normalized text (parent and name)."""
    if section == 'income':
        return 'revenue', 0, '', '', True
    if section == 'other_income':
        return 'other_income', 0, '', '', True
    early = ACCOUNT_RULES[:7]
    for plist, cat, lv, kind, why in early:
        if hit(text, plist):
            return cat, lv, kind, why, True
    if hit(text, OWNER_PAY) and hit(text, COMP):
        return 'provider_comp', 0, 'Owner pay', '', True
    if hit(text, PROVIDER) and hit(text, COMP):
        return 'provider_comp', 0, '', '', True
    for plist, cat, lv, kind, why in ACCOUNT_RULES[7:]:
        if hit(text, plist):
            return cat, lv, kind, why, True
    if section == 'other_expense':
        return 'non_operating', 0, '', '', False
    return 'misc', 0, '', '', False


# ---------- payee rules: (label, phrases, category hint, level, kind, weekend-sensitive) ----------
VENDOR_RULES = [(lab, phrases(p), c, lv, k, ws) for lab, p, c, lv, k, ws in [
    ('Food delivery', 'doordash|uber eats|ubereats|grubhub|postmates|caviar', 'misc', 2, 'Personal', True),
    ('Streaming and entertainment', 'netflix|hulu|disney plus|disneyplus|spotify|hbo max|max com|peacock|paramount|'
     'youtube tv|youtube premium|siriusxm|sirius xm|audible|playstation|xbox|nintendo|steam games|steampowered|'
     'ticketmaster|stubhub|live nation|seatgeek|amc theatres|amc theaters|regal cinemas|regal theatre|cinemark|'
     'fandango|disneyland|disney resort|walt disney world|universal studios|six flags|knotts|legoland', 'misc', 3,
     'Personal', True),
    ('Clothing and personal shopping', 'nordstrom|neiman marcus|saks|bloomingdale s|bloomingdales|louis vuitton|gucci|'
     'prada|chanel|hermes|tiffany|cartier|rolex|sephora|ulta|lululemon|nike|adidas|macy s|macys|old navy|zara|'
     'anthropologie|j crew|banana republic|jewelry|jeweler|jewelers', 'misc', 3, 'Personal', True),
    ('School, childcare and kids', 'tuition|preschool|daycare|day care|montessori|tutoring|kumon|mathnasium|'
     'summer camp|little league|toys r us|build a bear|lego store', 'misc', 3, 'Personal', False),
    ('Personal care, fitness and clubs', 'salon|nail|nails|barber|massage envy|hand and stone|equinox|'
     '24 hour fitness|la fitness|planet fitness|orangetheory|soulcycle|peloton|crunch fitness|gold s gym|golf|'
     'country club|topgolf', 'misc', 3, 'Personal', True),
    ('Leisure travel', 'resort|cruise|carnival|royal caribbean|norwegian cruise|princess cruise|airbnb|vrbo|'
     'vail resorts|mammoth mountain|sandals|club med|ski', 'misc', 3, 'Personal', True),
    ('Charitable giving', 'donation|church|parish|synagogue|mosque|gofundme|red cross|united way|st jude|unicef|'
     'charity', 'misc', 3, 'Personal', False),
    ('Political giving', 'actblue|winred|for congress|for senate|for assembly|for mayor|campaign', 'misc', 3,
     'Personal', False),
    ('Alcohol', 'bevmo|total wine|liquor|wine com|winery|vineyard|brewery|brewing', 'misc', 3, 'Personal', True),
    ('Pets', 'petco|petsmart|chewy|veterinary|banfield|vca animal', 'misc', 3, 'Personal', False),
    ('Home furnishings and garden', 'wayfair|williams sonoma|pottery barn|restoration hardware|crate barrel|'
     'west elm|pool supply|pool service|nursery', 'misc', 2, 'Personal', True),
    ('Meals and dining', 'restaurant|grill|cafe|bistro|sushi|steakhouse|steak house|pizza|pizzeria|taqueria|ramen|'
     'bbq|tavern|starbucks|coffee|peet s|chipotle|cheesecake factory|ruth s chris|mastro s|nobu|in n out|panera|tst',
     'misc', 2, 'Personal', True),
    ('Groceries', 'whole foods|wholefds|trader joe s|safeway|vons|ralphs|albertsons|sprouts|gelson s|erewhon|kroger|'
     'smart final|aldi|grocery', 'misc', 2, 'Personal', True),
    ('Air travel', 'united airlines|united air|delta air|american airlines|american air|southwest air|southwest airlines|'
     'alaska air|jetblue|spirit airl|frontier airlines|hawaiian air|airline|airlines', 'misc', 2, 'Personal', True),
    ('Hotels', 'marriott|hilton|hyatt|sheraton|westin|ritz carlton|four seasons|hotel|motel|expedia|booking com|'
     'hotels com', 'misc', 2, 'Personal', True),
    ('Rides and rental cars', 'uber|lyft|hertz|avis|enterprise rent|national car|budget rent', 'misc', 2,
     'Personal', True),
    ('Vehicle and fuel', 'shell oil|shell service|chevron|arco|mobil|exxon|valero|costco gas|tesla supercharger|'
     'chargepoint|car wash|carwash|jiffy lube|dmv|bmw financial|mercedes benz financial|mb financial|'
     'toyota financial|lexus financial|honda financial|tesla motors|tesla inc|porsche|audi financial|ford credit|'
     'ally auto', 'misc', 2, 'Personal', False),
    ('Person-to-person transfer', 'venmo|zelle|cash app|square cash|apple cash', 'misc', 2, 'Personal', False),
    ('Cash withdrawal', 'atm|cash withdrawal', 'misc', 2, 'Personal', False),
    ('Personal insurance', 'geico|progressive|state farm|allstate|farmers insurance|mercury insurance|'
     'northwestern mutual|mass mutual|massmutual|new york life|prudential|life insurance', 'other_insurance', 2,
     'Personal', False),
    ('Gifts and flowers', 'gift|gifts|flowers|1 800 flowers|ftd|proflowers|edible arrangements|harry david',
     'misc', 2, 'Personal', False),
    ('Mixed-use retailer', 'amazon|amzn|costco|target|walmart|wal mart|sam s club|home depot|lowe s|lowes|best buy|'
     'apple store|apple com|ebay|etsy|paypal|ikea|bed bath', 'misc', 1, 'Review', True),
    ('Office supplier', 'staples|office depot|officemax|uline|usps|ups store|fedex|stamps com|pitney bowes',
     'admin', 0, '', False),
    ('Medical supplier', 'mckesson|henry schein|medline|cardinal health|moore medical|bound tree|concordance',
     'med_surg', 0, '', False),
    ('Reference lab', 'quest diagnostics|labcorp|laboratory corporation', 'lab', 0, '', False),
    ('Software and phones', 'google workspace|gsuite|microsoft|adobe|zoom|dropbox|docusign|nextgen|athenahealth|'
     'verizon|at t|t mobile|comcast|spectrum|cox communications|ringcentral', 'it', 0, '', False),
    ('Billing and clearinghouse', 'availity|waystar|office ally|trizetto|change healthcare', 'billing', 0, '', False),
    ('Utilities', 'southern california edison|socalgas|so cal gas|ladwp|waste management|republic services|pg e',
     'building', 0, '', False),
]]
P2P_LABEL = 'Person-to-person transfer'
P2P_WORDS = set(phrases('zelle|venmo|cash|app|square|apple|payment|payments|pmt|to|from|sent|send|transfer|'
                        'web|id|conf|confirmation|online|mobile|pay|p2p|debit|card|purchase|recurring|des|indn|co|'
                        'ppd|ccd|web id|ach|xfer|inst|jpm|chase|bofa|wf'))

# Card issuers' own category words, used only when nothing else places a charge: (phrases, category, level, kind, label, ws)
CARD_CATS = [(phrases(p), c, lv, k, lab, ws) for p, c, lv, k, lab, ws in [
    ('restaurant|restaurants|dining|food drink|bars', 'misc', 2, 'Personal', 'Meals and dining (card category)', True),
    ('groceries|grocery|supermarkets', 'misc', 2, 'Personal', 'Groceries (card category)', True),
    ('travel|airline|airlines|airfare|lodging|hotel|hotels', 'misc', 2, 'Personal', 'Travel (card category)', True),
    ('entertainment', 'misc', 2, 'Personal', 'Entertainment (card category)', True),
    ('gas|gasoline|automotive|fuel', 'misc', 2, 'Personal', 'Vehicle and fuel (card category)', False),
    ('personal|home', 'misc', 2, 'Personal', 'Personal or home (card category)', True),
    ('gifts donations|donations|charity|charities', 'misc', 3, 'Personal', 'Gifts and donations (card category)', False),
    ('shopping|merchandise|retail', 'misc', 1, 'Review', 'Shopping (card category)', True),
    ('professional services', 'prof_fees', 0, '', '', False),
    ('bills utilities|utilities', 'building', 0, '', '', False),
    ('fees adjustments|fees', 'admin', 0, '', '', False),
]]

BUSINESS_WORDS = set(phrases(
    'inc|llc|llp|lp|pc|pllc|corp|corporation|co|company|companies|group|services|service|medical|medicine|health|'
    'healthcare|bank|insurance|store|stores|market|supply|supplies|labs|lab|laboratory|center|centre|clinic|'
    'associates|partners|pharmacy|systems|solutions|software|tech|technologies|technology|city|county|state|'
    'department|dept|of|the|and|usa|america|american|national|financial|capital|management|consulting|cpa|law|'
    'office|offices|studio|foods|food|electric|edison|gas|water|power|energy|wireless|communications|southern|'
    'northern|california|pacific|valley|west|east|north|south|united|first|golden|sun|hospital|imaging|radiology|'
    'diagnostics|billing|collections|express|international|global|enterprises|holdings|trust|fund|foundation|'
    'university|college|school|academy|church|club|plan|care|staffing|solutions|network|depot|mart|shop|warehouse|'
    'payroll|irs|franchise|tax|board|ftb|edd|dmv|usps|ups|fedex|amazon|google|apple|microsoft|adobe|verizon|'
    'comcast|spectrum|cleaning|janitorial|landscaping|security|alarm|realty|properties|property|leasing|rentals|'
    'motors|auto|construction|plumbing|electrical|printing|marketing|media|design|agency|associates|ltd|dds|'
    'dmd|od|pt|chiropractic|dental|vision|optometry'))
PERSON = re.compile(r"[A-Za-z][A-Za-z'.-]*(?:,? +[A-Za-z][A-Za-z'.-]*){1,3}")
US_STATES = set('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY '
                'NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split())
INCOME_WORDS = phrases('income|revenue|revenues|fee for service|capitation|patient payments|insurance payments|'
                       'sales|collections|copay|copays|medicare|medi cal|medicaid')
BALANCE_WORDS = phrases('checking|savings|bank|receivable|receivables|payable|payables|loan|loans|line of credit|'
                        'equity|retained earnings|opening balance|undeposited|credit card|visa|mastercard|amex|'
                        'american express|accrued|prepaid|deposit|deposits|accumulated|fixed asset|fixed assets|'
                        'liabilities|liability|draw|draws|distribution|distributions|capital stock|petty cash')
MONEY_WORDS = phrases('checking|savings|bank|credit card|card|visa|mastercard|amex|american express|petty cash|cash|'
                      'chase|wells fargo|bofa|capital one|citi|undeposited')
REFUND_WORDS = phrases('refund|refunds|refunded|overpayment|overpayments|credit balance|credit balances')
PATIENT_WORDS = phrases('patient|patients|pt|pts|overpayment|overpayments|credit balance|credit balances')
PAYMENT_WORDS = phrases('payment thank you|autopay|auto pay|online payment|epayment|payment received|mobile payment|'
                        'thank you|payment')
STRONG_PAYMENT = phrases('payment thank you|autopay payment|auto pay|online payment thank|epayment|payment received|'
                         'internet payment')
NOT_INCOME = phrases('agency|expense|expenses|refund|refunds|tax|taxes')
PREFIX_WORDS = set(phrases('sq|tst|sp|pp|py|dd|in|paypal|pos|purchase|debit|card|recurring|ach|checkcard|chk'))
LOW_LIST_MIN = 100000
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
FULL_MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october',
               'november', 'december']


def month_of_token(t):
    if t in MONTHS:
        return MONTHS.index(t) + 1
    if t in FULL_MONTHS:
        return FULL_MONTHS.index(t) + 1
    if t == 'sept':
        return 9
    return 0


# ---------- numbers: integer cents, rounded half away from zero ----------
def div_round(n, d):
    if d <= 0:
        return 0
    q = (2 * abs(n) + d) // (2 * d)
    return -q if n < 0 else q


def cents(cell):
    """Integer cents from an amount cell, or None: $1,234.56, (1,234.56), -1234.5, 1234.56-, 1.2E3."""
    v, d = cell
    if d is not None:
        return None
    s = v.strip().replace(' ', '').replace(' ', '')
    if s == '':
        return None
    neg = False
    if s.startswith('(') and s.endswith(')'):
        neg, s = True, s[1:-1]
    s = s.replace('$', '').replace(',', '')
    if s.endswith('-'):
        neg, s = not neg, s[:-1]
    if s.startswith('-'):
        neg, s = not neg, s[1:]
    elif s.startswith('+'):
        s = s[1:]
    m = re.fullmatch(r'([0-9]*)(?:\.([0-9]*))?', s)
    if m and (m.group(1) or m.group(2)):
        frac = m.group(2) or ''
        if len(frac) <= 2:
            c = int(m.group(1) or '0') * 100 + int((frac + '00')[:2])
            return -c if neg else c
    if not re.fullmatch(r'[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?|[0-9]+\.', s):
        return None
    c = math.floor(float(s) * 100 + 0.5)
    return -c if neg else c


def money(c):
    return ('-' if c < 0 else '') + str(abs(c) // 100) + '.' + str(abs(c) % 100).rjust(2, '0')


def tenths(t):
    return ('-' if t < 0 else '') + str(abs(t) // 10) + '.' + str(abs(t) % 10)


def fnv(s):
    h = 0x811C9DC5
    for ch in s.encode('utf-8'):
        h = ((h ^ ch) * 0x01000193) & 0xFFFFFFFF
    return 'R' + ('%08X' % h)


# ---------- dates, as datetime.date ----------
DATE_FMTS = (re.compile(r'^([0-9]{1,2})[/-]([0-9]{1,2})[/-]([0-9]{4}|[0-9]{2})(?:[ T].*)?$'),
             re.compile(r'^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?:[ T].*)?$'))


def ymd(y, m, d):
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def parse_date(cell):
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


def iso(d):
    return '%04d-%02d-%02d' % (d.year, d.month, d.day)


def ym_text(ym):
    return '%04d-%02d' % (ym // 12, ym % 12 + 1)


def month_label(cell):
    """A column header that names one month, as year*12 + month-1, or None."""
    v, d = cell
    if d is not None:
        return d.year * 12 + d.month - 1
    s = v.strip()
    m = re.fullmatch(r'([0-9]{1,2})/([0-9]{4})', s)
    if m and 1 <= int(m.group(1)) <= 12:
        return int(m.group(2)) * 12 + int(m.group(1)) - 1
    m = re.fullmatch(r'([0-9]{4})-([0-9]{2})', s)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)) * 12 + int(m.group(2)) - 1
    toks = norm(s).split()
    found = [month_of_token(t) for t in toks if month_of_token(t)]
    if not found or len(set(found)) != 1 or 'total' in toks:
        return None
    year = None
    seen_month = False
    for t in toks:
        if month_of_token(t):
            seen_month = True
        elif re.fullmatch(r'(19|20)[0-9]{2}', t):
            year = int(t)
        elif seen_month and year is None and re.fullmatch(r'[0-9]{2}', t):
            year = 2000 + int(t)
    if year is None:
        return None
    return year * 12 + found[0] - 1


def title_range(text):
    """(first month, month count) from a report title like 'January - December 2025', or None."""
    toks = norm(text).split()
    marks = []
    for i, t in enumerate(toks):
        mo = month_of_token(t)
        if mo:
            yr = None
            for t2 in toks[i + 1:i + 4]:
                if month_of_token(t2):
                    break
                if re.fullmatch(r'(19|20)[0-9]{2}', t2):
                    yr = int(t2)
                    break
            marks.append([mo, yr])
    for m in re.finditer(r'([0-9]{1,2})/[0-9]{1,2}/([0-9]{4})', text):
        marks.append([int(m.group(1)), int(m.group(2))])
    if not marks:
        return None
    for i in range(len(marks) - 2, -1, -1):
        if marks[i][1] is None:
            marks[i][1] = marks[i + 1][1]
    if marks[0][1] is None or marks[-1][1] is None:
        return None
    a = marks[0][1] * 12 + marks[0][0] - 1
    b = marks[-1][1] * 12 + marks[-1][0] - 1
    if b < a:
        return None
    return a, b - a + 1


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
    """The first sheet of an .xlsx file. Indented labels (QuickBooks Online's Excel export) keep their indent as
    leading spaces, two per level, so the hierarchy reads the same as the CSV export."""
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
    date_styles, indents = [], []
    if 'xl/styles.xml' in names:
        st = ET.fromstring(z.read('xl/styles.xml'))
        custom = {el.get('numFmtId'): el.get('formatCode') or '' for el in st.iter() if local(el.tag) == 'numFmt'}
        for el in st:
            if local(el.tag) == 'cellXfs':
                for xf in el:
                    fid = xf.get('numFmtId') or '0'
                    date_styles.append(int(fid) in BUILTIN_DATE_FMTS if fid not in custom else is_date_format(custom[fid]))
                    ind = 0
                    for al in xf:
                        if local(al.tag) == 'alignment' and (al.get('indent') or '').isdigit():
                            ind = int(al.get('indent'))
                    indents.append(ind)
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
            f = next((x.text or '' for x in c if local(x.tag) == 'f'), '')
            if v == '' and re.fullmatch(r'=?-?[0-9]*\.?[0-9]+', f.strip()):
                v, t = f.strip().lstrip('='), 'n'
            pad = '  ' * indents[s] if s < len(indents) else ''
            if t == 's':
                val = (pad + (strings[int(v)] if v != '' else ''), None)
            elif t == 'inlineStr':
                val = (pad + next((rich_text(x) for x in c if local(x.tag) == 'is'), ''), None)
            elif t == 'str':
                val = (pad + v, None)
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
    return rows


def read_rows(name, data):
    if data[:2] == b'PK':
        return read_xlsx(data)
    return read_csv(data)


# ---------- telling files apart ----------
DATE_HEADS = ['transaction date', 'trans date', 'txn date', 'date', 'tran date', 'posted date', 'post date',
              'posting date', 'trans dt', 'date posted']
AMOUNT_HEADS = ['amount', 'amount usd', 'debit', 'credit', 'debit amount', 'credit amount', 'charges', 'charge amount']


def cell_norm(cell):
    return norm(cell[0]).strip()


def find_header(rows, test, limit=40):
    for i, r in enumerate(rows[:limit]):
        if test([cell_norm(c) for c in r], r):
            return i
    return -1


def detect(rows):
    """(kind, header row index)."""
    h = find_header(rows, lambda t, r: 'review id' in t and 'owner decision' in t)
    if h >= 0:
        return 'decisions', h
    h = find_header(rows, lambda t, r: 'category' in t and 'pct of revenue' in t)
    if h >= 0:
        return 'benchmarks', h
    h = find_header(rows, lambda t, r: any(x in t for x in DATE_HEADS) and any(x in t for x in AMOUNT_HEADS))
    if h >= 0:
        return 'transactions', h
    h = find_header(rows, lambda t, r: 'total' in t[1:] or sum(1 for c in r[1:] if month_label(c) is not None) >= 1)
    if h < 0:
        return 'unknown', -1
    title = norm(' '.join(c[0] for r in rows[:h] for c in r))
    first = cell_norm(rows[h][0]) if rows[h] else ''
    if first == 'vendor' or ' vendor ' in title:
        return 'vendors', h
    months = sum(1 for c in rows[h][1:] if month_label(c) is not None)
    if months or hit(title, phrases('profit and loss|profit loss|income statement|p l')) or first in ('distribution account', 'account'):
        return 'pnl', h
    return 'unknown', -1


def indent_of(v):
    return len(v) - len(v.lstrip(' '))


SECTION_NAMES = [('income', phrases('income|revenue|revenues|ordinary income')),
                 ('cogs', phrases('cost of goods sold|cogs|cost of sales')),
                 ('expense', phrases('expenses|expense|operating expenses')),
                 ('other_income', phrases('other income')),
                 ('other_expense', phrases('other expenses|other expense'))]
COMPUTED = phrases('gross profit|net operating income|net ordinary income|net other income|net income|net profit|'
                   'net loss|net revenue')


def section_of(label):
    n = norm(label).strip()
    for key, names in SECTION_NAMES:
        if n in names:
            return key
    return None


def total_target(label):
    n = norm(label).strip()
    if n.endswith(' with sub accounts'):
        n = n[:-18]
    if n.startswith('total for '):
        return n[10:]
    if n.startswith('total '):
        return n[6:]
    if n == 'total':
        return ''
    return None


def account_key(label):
    """An account's name for matching across reports: last segment after ':', no account numbers."""
    last = label.split(':')[-1]
    return ' '.join(t for t in norm(last).split() if not t.isdigit())


# ---------- the P&L ----------
def parse_pnl(rows, h):
    head = rows[h]
    mcols = [(i, month_label(c)) for i, c in enumerate(head) if i > 0 and month_label(c) is not None]
    tcol = next((i for i, c in enumerate(head) if i > 0 and 'total' in cell_norm(c).split()), None)
    first_amt = mcols[0][0] if mcols else (tcol if tcol is not None else 1)
    title = ' '.join(c[0] for r in rows[:h] for c in r)
    pnl = {'lines': [], 'months': sorted(set(m for _, m in mcols)), 'file_totals': {}, 'title': title.strip(),
           'single': not mcols, 'range': title_range(title) if not mcols else None, 'skipped': 0}
    cols = mcols if mcols else ([(tcol, None)] if tcol is not None else [])
    body = []
    for r in rows[h + 1:]:
        labels = [(i, c[0]) for i, c in enumerate(r[:first_amt]) if c[0].strip()]
        if not labels:
            continue
        col, raw = labels[0]
        amts, any_amt = {}, False
        for i, m in cols:
            c = cents(r[i]) if i < len(r) else None
            if c is not None:
                any_amt = True
            amts[m] = amts.get(m, 0) + (c or 0)
        body.append((col * 100 + indent_of(raw), raw.strip(), amts, any_amt))
    targets = set(total_target(b[1]) for b in body if total_target(b[1]))
    stack, section = [], None
    for depth, label, amts, any_amt in body:
        n = norm(label).strip()
        tgt = total_target(label)
        if tgt is not None:
            key = section_of(tgt)
            if key and any_amt:
                pnl['file_totals'][key] = sum(amts.values())
            if any(x[1] == tgt for x in stack):
                while stack and stack[-1][1] != tgt:
                    stack.pop()
                stack.pop()
            continue
        if n in COMPUTED:
            if n == 'net income' and any_amt:
                pnl['file_totals']['net'] = sum(amts.values())
            continue
        sec = section_of(label)
        if sec and not any_amt:
            section, stack = sec, []
            continue
        if n in ('ordinary income expense', 'other income expense', 'ordinary income expenses'):
            continue
        while stack and depth > 0 and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][2] if stack else ''
        if any_amt:
            if section is None:
                pnl['skipped'] += 1
            else:
                pnl['lines'].append({'label': label, 'parent': parent, 'section': section, 'amounts': amts})
        if not any_amt or n in targets:
            stack.append((depth, n, label))
    return pnl


# ---------- line detail ----------
def head_index(t, names):
    for nm in names:
        if nm in t:
            return t.index(nm)
    return None


def parse_transactions(rows, h, fname):
    t = [cell_norm(c) for c in rows[h]]
    col = {
        'date': head_index(t, DATE_HEADS),
        'type': head_index(t, ['transaction type', 'type', 'trans type']),
        'name': head_index(t, ['name', 'payee', 'vendor', 'vendor name', 'payee name']),
        'desc': head_index(t, ['memo description', 'description', 'transaction description', 'merchant',
                               'merchant name', 'details']),
        'memo': head_index(t, ['memo', 'extended details', 'appears on your statement as']),
        'account': head_index(t, ['account', 'account full name', 'account name', 'distribution account',
                                  'gl account']),
        'split': head_index(t, ['split', 'split account', 'item split account']),
        'amount': head_index(t, ['amount', 'amount usd', 'charge amount', 'charges']),
        'debit': head_index(t, ['debit', 'debit amount']),
        'credit': head_index(t, ['credit', 'credit amount']),
        'cardcat': head_index(t, ['category']),
        'status': head_index(t, ['status']),
    }
    ledger = any(x in t for x in ('split', 'num', 'transaction type', 'account full name', 'distribution account',
                                  'split account', 'account name')) or \
        ('account' in t and not any(x in t for x in ('card member', 'card no', 'card number', 'receipt')))
    out, stack, skipped = [], [], 0
    for r in rows[h + 1:]:
        get = lambda k: r[col[k]] if col[k] is not None and col[k] < len(r) else ('', None)
        d = parse_date(get('date'))
        if col['amount'] is not None:
            amt = cents(get('amount'))
        else:
            de, cr = cents(get('debit')), cents(get('credit'))
            amt = None if de is None and cr is None else abs(de or 0) - abs(cr or 0)
        filled = [(i, c[0].strip()) for i, c in enumerate(r) if c[0].strip()]
        if d is None:
            if not filled:
                continue
            label = filled[0][1]
            tgt = total_target(label)
            if tgt is not None:
                if any(x[0] == tgt for x in stack):
                    while stack and stack[-1][0] != tgt:
                        stack.pop()
                    stack.pop()
                continue
            if amt is None and len(filled) == 1 and filled[0][0] < (col['date'] or 1) + 1 and filled[0][0] != col['date']:
                ledger = True
                stack.append((norm(label).strip(), label))
            else:
                skipped += 1
            continue
        if amt is None:
            skipped += 1
            continue
        if norm(get('status')[0]).strip() == 'pending':
            skipped += 1
            continue
        acct_cell = get('account')[0].strip()
        split = get('split')[0].strip()
        if stack:
            account = stack[-1][1]
            other = split or acct_cell
        else:
            account, other = acct_cell, split
        segs = account.split(':')
        account, parent = segs[-1].strip(), (segs[-2].strip() if len(segs) > 1 else '')
        if not parent and len(stack) > 1:
            parent = stack[-2][1]
        swapped = False
        if account and other and hit(norm(account), MONEY_WORDS) and not other.lower().startswith('-split'):
            segs = other.split(':')
            account, parent, swapped = segs[-1].strip(), (segs[-2].strip() if len(segs) > 1 else ''), True
        desc, memo = get('desc')[0].strip(), get('memo')[0].strip()
        out.append({'date': d, 'name': get('name')[0].strip(), 'memo': (desc + ' ' + memo).strip(),
                    'desc': desc or get('name')[0].strip(), 'type': get('type')[0].strip(), 'account': account,
                    'parent': parent, 'amount': amt, 'swapped': swapped, 'cardcat': get('cardcat')[0].strip(),
                    'file': fname, 'card': False})
    if not ledger:
        for x in out:
            x['card'] = True
    return out, skipped, ledger


def parse_vendors(rows, h):
    head = [cell_norm(c) for c in rows[h]]
    tots = [i for i, x in enumerate(head) if x == 'total']
    mcols = [i for i, c in enumerate(rows[h]) if i > 0 and month_label(c) is not None]
    title = ' '.join(c[0] for r in rows[:h] for c in r)
    out = []
    for r in rows[h + 1:]:
        if not any(c[0].strip() for c in r):
            continue
        name = r[0][0].strip() if r else ''
        if total_target(name) is not None:
            continue
        if tots:
            c = cents(r[tots[-1]]) if tots[-1] < len(r) else None
        else:
            vals = [cents(r[i]) for i in mcols if i < len(r)]
            c = None if all(v is None for v in vals) else sum(v or 0 for v in vals)
        if c is None:
            continue
        out.append({'name': name or '(no payee)', 'amount': c})
    rng = title_range(title)
    if mcols:
        ms = sorted(set(month_label(rows[h][i]) for i in mcols))
        rng = (ms[0], ms[-1] - ms[0] + 1)
    return out, rng, title.strip()


# ---------- names ----------
def person_like(name):
    s = name.strip()
    if not PERSON.fullmatch(s):
        return False
    toks = norm(s).split()
    if len(toks) < 2 or any(t in BUSINESS_WORDS for t in toks):
        return False
    return vendor_match(norm(s)) is None


def vendor_match(text):
    for rule in VENDOR_RULES:
        p = hit(text, rule[1])
        if p:
            return rule, p
    return None


def clean_merchant(desc):
    """A card description fit to show: tokens with digits, @ or / left out, at most 60 characters."""
    keep = [tk for tk in desc.split() if not re.search(r'[0-9@/]', tk)]
    return ' '.join(keep)[:60].strip()


# ---------- the run ----------
def parse_settings(settings):
    s = settings or {}

    def h(k, dflt):
        v = str(s.get(k, dflt)).strip()
        c = cents((v, None)) if v else None
        return c if c is not None and c >= 0 else cents((dflt, None))
    fam = [t for t in norm(str(s.get('family', '') or '')).split() if len(t) >= 2]
    return {'physicians': h('physicians', '1'), 'apps': h('apps', '0'), 'family': fam}


def run(inputs, settings=None):
    """inputs: list of (file name, bytes). Returns {file name: text} for the five outputs."""
    st = parse_settings(settings)
    rec = ['P&L normalizer %s' % VERSION, '']
    pnls, txns, vendors, decisions, bench = [], [], [], {}, {}
    vendor_meta = []
    for name, data in inputs:
        base = name.split('/')[-1].split('\\')[-1]
        try:
            rows = read_rows(base, data)
        except Exception as e:  # noqa: BLE001
            rec.append('- %s: could not be read (%s). Left out.' % (base, type(e).__name__))
            continue
        kind, h = detect(rows)
        if kind == 'pnl':
            p = parse_pnl(rows, h)
            p['file'] = base
            pnls.append(p)
            span = ('%d months, %s to %s' % (len(p['months']), ym_text(min(p['months'])), ym_text(max(p['months'])))
                    if p['months'] else 'one total column')
            rec.append('- %s: a profit and loss report (%s), %d accounts with amounts.' % (base, span, len(p['lines'])))
        elif kind == 'transactions':
            got, skipped, ledger = parse_transactions(rows, h, base)
            txns.extend(got)
            rec.append('- %s: %s, %d dated rows%s.' % (
                base, 'QuickBooks line detail' if ledger else 'a card or bank statement', len(got),
                ', %d other rows left out' % skipped if skipped else ''))
        elif kind == 'vendors':
            got, rng, title = parse_vendors(rows, h)
            vendors.extend(got)
            vendor_meta.append(rng)
            rec.append('- %s: an expenses by vendor summary, %d vendors.' % (base, len(got)))
        elif kind == 'decisions':
            t = [cell_norm(c) for c in rows[h]]
            ri, di = t.index('review id'), t.index('owner decision')
            n = 0
            for r in rows[h + 1:]:
                if max(ri, di) < len(r) and r[ri][0].strip() and r[di][0].strip():
                    decisions[r[ri][0].strip()] = r[di][0].strip()
                    n += 1
            rec.append('- %s: the owner\'s answers on the review list, %d answered.' % (base, n))
        elif kind == 'benchmarks':
            t = [cell_norm(c) for c in rows[h]]
            ci, pi = t.index('category'), t.index('pct of revenue')
            si = t.index('source') if 'source' in t else None
            for r in rows[h + 1:]:
                if max(ci, pi) < len(r):
                    key = r[ci][0].strip()
                    pc = cents(r[pi])
                    if key in OVERHEAD and pc is not None:
                        bench[key] = (div_round(pc, 10), r[si][0].strip() if si is not None and si < len(r) else '')
            rec.append('- %s: benchmarks for %d categories, used in place of the built-in ones.' % (base, len(bench)))
        else:
            rec.append('- %s: not a report this tool reads. Left out.' % base)
    return build(st, rec, pnls, txns, vendors, vendor_meta, decisions, bench)


def merchant_key(desc):
    """A card merchant's group name: numbers, payment-processor prefixes and a trailing state left out, first two words."""
    toks = [t for t in norm(clean_merchant(desc)).split() if t not in PREFIX_WORDS]
    if toks and toks[-1].upper() in US_STATES and len(toks) > 2:
        toks = toks[:-1]
    if toks and toks[0] == 'the' and len(toks) > 1:
        toks = toks[1:]
    return ' '.join(toks[:3]).upper() or '(no description)'


GENERIC = set(phrases(
    'grill|cafe|bistro|sushi|steakhouse|steak house|pizza|pizzeria|taqueria|ramen|bbq|tavern|coffee|restaurant|tst|'
    'hotel|motel|resort|cruise|ski|tuition|preschool|daycare|day care|montessori|tutoring|summer camp|salon|nail|nails|'
    'barber|golf|country club|donation|church|parish|synagogue|mosque|charity|campaign|liquor|winery|vineyard|brewery|'
    'brewing|nursery|pool supply|pool service|jewelry|jeweler|jewelers|gift|gifts|flowers|atm|cash withdrawal|grocery|'
    'airline|airlines|life insurance|veterinary|for congress|for senate|for assembly|for mayor|car wash|carwash|dmv'))


def brand(vm):
    """A matched brand's name to group by ('Costco'), or None when the match was a generic word ('grill')."""
    if not vm or vm[1] in GENERIC:
        return None
    return ' '.join(w[:1].upper() + w[1:] for w in vm[1].split())


def in_set(ym, wset):
    return wset is None or ym in wset


def ym_of(d):
    return d.year * 12 + d.month - 1


def build(st, rec, pnls, txns, vendors, vendor_meta, decisions, bench):
    notes = []
    pnl = pnls[0] if pnls else None
    if len(pnls) > 1:
        notes.append('More than one profit and loss report came in. The first one (%s) is used.' % pnl['file'])

    # ----- the period: the P&L's last 12 months, else the detail's last 12 months -----
    window, nmonths = None, 0
    if pnl and pnl['months']:
        window = pnl['months'][-12:]
        nmonths = len(window)
    elif pnl and pnl['range']:
        window = list(range(pnl['range'][0], pnl['range'][0] + pnl['range'][1]))
        nmonths = len(window)
    elif pnl:
        nmonths = 12
        notes.append('The profit and loss report has one total column and no readable date range, so it is read as '
                     '12 months and every dated row is used.')
    elif txns:
        lastm = max(ym_of(x['date']) for x in txns)
        window = list(range(lastm - 11, lastm + 1))
        nmonths = len(set(ym_of(x['date']) for x in txns if ym_of(x['date']) > lastm - 12))
    wset = set(window) if window is not None else None

    pkeys = {}
    if pnl:
        for ln in pnl['lines']:
            pkeys.setdefault(account_key(ln['label']), ln['section'])
    ledger = [x for x in txns if not x['card']]
    cards = [x for x in txns if x['card']]
    # a charge shows twice in a full detail export: under the expense account, and under the card or bank account
    # with the expense account beside it. Keep the first.
    direct = set((x['file'], account_key(x['account']), x['date'], abs(x['amount'])) for x in ledger if not x['swapped'])
    mirrors = [x for x in ledger if x['swapped'] and (x['file'], account_key(x['account']), x['date'], abs(x['amount'])) in direct]
    ledger = [x for x in ledger if not any(x is m for m in mirrors)]

    # signs: card charges and money-account rows count as positive spending
    for fname in sorted(set(x['file'] for x in cards)):
        rows = [x for x in cards if x['file'] == fname]
        body = [x for x in rows if not is_payment(x)]
        if sum(1 for x in body if x['amount'] < 0) * 2 > len(body):
            for x in rows:
                x['amount'] = -x['amount']
    for fname in sorted(set(x['file'] for x in ledger if x['swapped'])):
        rows = [x for x in ledger if x['file'] == fname and x['swapped']]
        if sum(1 for x in rows if x['amount'] < 0) * 2 > len(rows):
            for x in rows:
                x['amount'] = -x['amount']

    names, name_order = {}, []

    def code(name):
        k = norm(name).strip()
        if k not in names:
            names[k] = ['Individual %02d' % (len(names) + 1), name.strip()]
            name_order.append(k)
        return names[k][0]

    collapsed = {}
    income_in_window = 0
    left = {'balance': 0, 'payments': 0, 'outside': 0, 'draws': 0}
    items = []
    for x in ledger:
        text = norm(x['parent'] + ' ' + x['account'])
        memo_n = norm(x['memo'] + ' ' + x['name'])
        akey = account_key(x['account'])
        if pkeys:
            section = pkeys.get(akey) or ('income' if hit(text, INCOME_WORDS) and not hit(text, NOT_INCOME) else 'balance')
        elif hit(text, INCOME_WORDS) and not hit(text, NOT_INCOME):
            section = 'income'
        elif x['account'] and hit(text, BALANCE_WORDS):
            section = 'balance'
        else:
            section = 'expense'
        refund = bool((hit(text, REFUND_WORDS) and (hit(text, PATIENT_WORDS) or section == 'income'))
                      or (hit(memo_n, REFUND_WORDS) and hit(memo_n, PATIENT_WORDS)))
        if refund or section in ('income', 'other_income'):
            k = ('Patient refunds' if refund else 'Income', x['date'].year, x['account'] or '(no account)')
            c = collapsed.setdefault(k, [0, 0])
            c[0] += 1
            c[1] += x['amount']
            if not refund and section == 'income' and in_set(ym_of(x['date']), wset):
                income_in_window += x['amount']
            continue
        if section == 'balance':
            if hit(text, phrases('draw|draws|distribution|distributions')):
                left['draws'] += x['amount']
            left['balance'] += 1
            continue
        if not in_set(ym_of(x['date']), wset):
            left['outside'] += 1
            continue
        items.append(dict(x, section=section, src='ledger', card_text=''))

    by_amt = {}
    for it in items:
        by_amt.setdefault(it['amount'], []).append(it)
    matched = 0
    for x in cards:
        if is_payment(x):
            left['payments'] += 1
            continue
        if not in_set(ym_of(x['date']), wset):
            left['outside'] += 1
            continue
        hitrow = next((it for it in by_amt.get(x['amount'], [])
                       if not it.get('matched') and abs((it['date'] - x['date']).days) <= 5), None)
        if hitrow is not None:
            hitrow['matched'] = True
            hitrow['card_text'] = x['desc']
            hitrow['cardcat'] = hitrow['cardcat'] or x['cardcat']
            matched += 1
            continue
        items.append(dict(x, section='expense', src='card', card_text=''))

    # home state: the most common state at the end of a description, once at least 10 show one
    states = {}
    for it in items:
        toks = (it['card_text'] or it['desc'] or it['memo']).upper().split()
        it['state'] = toks[-1] if len(toks) >= 3 and toks[-1] in US_STATES else ''
        if it['state']:
            states[it['state']] = states.get(it['state'], 0) + 1
    home = sorted(states.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if sum(states.values()) >= 10 else ''

    # ----- classify each charge -----
    covered = set()
    for it in items:
        if it['src'] == 'ledger' and it['account']:
            cat, level, kind, why, mapped = account_rule(norm(it['parent'] + ' ' + it['account']), it['section'])
            covered.add(account_key(it['account']))
        else:
            cat, level, kind, why, mapped = 'misc', 0, '', '', False
        reasons = [why] if why else []
        label, ws = '', False
        vm = vendor_match(norm(it['name'] + ' ' + it['memo'] + ' ' + it['card_text']))
        if vm:
            rule, p = vm
            label, ws = rule[0], rule[5]
            if not mapped:
                cat = rule[2]
            if rule[3] > level:
                level, kind, reasons = rule[3], rule[4], ['matched "%s"' % p] + reasons
            elif rule[3] and rule[3] == level:
                reasons = ['matched "%s"' % p] + reasons
        elif not mapped and it['cardcat']:
            for plist, ccat, clv, ckind, clab, cws in CARD_CATS:
                if hit(norm(it['cardcat']), plist):
                    cat = ccat
                    if clv > level:
                        level, kind, label, ws = clv, ckind, clab, cws
                        reasons = ['the card company filed it as "%s"' % it['cardcat']]
                    break
        nm = it['name']
        person = bool(nm) and person_like(nm)
        if label == P2P_LABEL and not person:
            src_text = it['desc'] if it['src'] == 'card' else (it['card_text'] or it['memo'])
            rest = [t for t in norm(clean_merchant(src_text)).split() if t not in P2P_WORDS]
            shown = group = P2P_LABEL + ' to ' + code(' '.join(rest).title() if rest else 'Unnamed')
        elif it['src'] == 'card':
            shown = clean_merchant(it['desc']) or '(no description)'
            group = brand(vm) or merchant_key(it['desc'])
        elif person:
            shown = group = code(nm)
        elif nm:
            shown = group = nm
        else:
            shown = group = brand(vm) or (merchant_key(it['memo']) if vm else '(no payee)')
        fam = [f for f in st['family'] if ' ' + f + ' ' in norm(nm + ' ' + (it['desc'] if it['src'] == 'card' else ''))]
        if fam:
            level, kind = 3, 'Family member'
            reasons = ['the payee shares a family name you entered'] + reasons
        if level and not vm and it['src'] == 'ledger' and not fam:
            group = 'Other charges in ' + it['account']
        it.update(cat=cat, level=level, kind=kind, reasons=reasons, label=label, ws=ws, shown=shown, group=group,
                  weekend=it['date'].weekday() >= 5, gid='')

    # ----- groups for the owner -----
    groups = {}

    def new_group(key, who, account, cat, kind, level, reasons):
        groups[key] = {'id': fnv(key), 'who': who, 'account': account, 'cat': cat, 'kind': kind, 'level': level,
                       'n': 0, 'amount': 0, 'months': set(), 'weekend': 0, 'states': {}, 'reasons': list(reasons),
                       'ws': False, 'first': None, 'last': None, 'example': ''}
        return groups[key]

    for it in items:
        if not it['level']:
            continue
        acct = it['account'] if it['src'] == 'ledger' else 'Card statement'
        key = '|'.join([it['kind'], norm(it['group']).strip(), account_key(acct)])
        cat = 'card_lump' if it['src'] == 'card' and pnl else it['cat']
        g = groups.get(key) or new_group(key, it['group'], acct, cat, it['label'] or it['kind'], 0, [])
        g['level'] = max(g['level'], it['level'])
        g['n'] += 1
        g['amount'] += it['amount']
        g['months'].add(ym_of(it['date']))
        g['weekend'] += 1 if it['weekend'] else 0
        g['ws'] = g['ws'] or it['ws']
        g['first'] = it['date'] if g['first'] is None or it['date'] < g['first'] else g['first']
        g['last'] = it['date'] if g['last'] is None or it['date'] > g['last'] else g['last']
        if not g['example']:
            g['example'] = it['shown']
        if it['state']:
            g['states'][it['state']] = g['states'].get(it['state'], 0) + 1
        for r in it['reasons']:
            if r not in g['reasons']:
                g['reasons'].append(r)
        it['gid'] = g['id']

    pnl_lines = []
    if pnl:
        for ln in pnl['lines']:
            amt = sum(v for m, v in ln['amounts'].items() if m is None or in_set(m, wset))
            text = norm(ln['parent'] + ' ' + ln['label'])
            cat, lv, kind, why, mapped = account_rule(text, ln['section'])
            if not mapped and ln['section'] == 'cogs':
                cat = 'med_surg'
            e = {'label': ln['label'], 'parent': ln['parent'], 'section': ln['section'], 'amount': amt, 'cat': cat,
                 'level': lv, 'kind': kind, 'mapped': mapped, 'gid': ''}
            pnl_lines.append(e)
            if lv and amt > 0 and account_key(ln['label']) not in covered:
                key = '|'.join([kind, 'whole account', account_key(ln['label'])])
                g = new_group(key, '(whole account)', ln['label'], cat, kind, lv,
                              [why, 'no line detail came in for this account, so all of it is listed'])
                g['amount'] = amt
                g['months'] = set(m for m, v in ln['amounts'].items() if v and m is not None and in_set(m, wset))
                e['gid'] = g['id']

    vendor_rows = []
    if vendors and not items:
        rng = next((r for r in vendor_meta if r), None)
        vmonths = rng[1] if rng else 12
        if not nmonths:
            nmonths = vmonths
        for v in vendors:
            vm = vendor_match(norm(v['name']))
            shown = code(v['name']) if person_like(v['name']) else v['name']
            cat, lv, kind = (vm[0][2], vm[0][3], vm[0][4]) if vm else ('misc', 0, '')
            reasons = ['matched "%s"' % vm[1]] if vm and lv else []
            if any(' ' + f + ' ' in norm(v['name']) for f in st['family']):
                lv, kind = 3, 'Family member'
                reasons = ['the payee shares a family name you entered'] + reasons
            amt = v['amount'] if vmonths == nmonths else div_round(v['amount'] * nmonths, vmonths)
            row = {'shown': shown, 'amount': amt, 'cat': cat, 'level': lv, 'gid': ''}
            vendor_rows.append(row)
            if lv and amt > 0:
                key = '|'.join([kind, norm(shown).strip(), 'vendor summary'])
                g = new_group(key, shown, 'Expenses by vendor', cat, kind, lv, reasons)
                g['amount'] = amt
                row['gid'] = g['id']

    # signals that move a group up one level
    for g in groups.values():
        if g['n'] >= 3 and g['ws'] and g['weekend'] * 100 >= 60 * g['n'] and g['level'] < 3:
            g['level'] += 1
            g['reasons'].append('%d of %d charges on a Saturday or Sunday' % (g['weekend'], g['n']))
        away = sum(v for k, v in g['states'].items() if k != home)
        if home and g['ws'] and g['n'] >= 2 and away * 2 >= g['n'] and g['level'] == 2:
            g['level'] += 1
            g['reasons'].append('mostly outside %s (%s)' % (home, ', '.join(sorted(k for k in g['states'] if k != home))))

    used = 0
    for g in groups.values():
        g['share'] = decision_share(decisions.get(g['id']))
        used += 1 if g['share'] is not None else 0

    # ----- totals, scaled to a year -----
    factor = nmonths or 12
    ann = lambda c: div_round(c * 12, factor)
    reported = dict((k, 0) for k in OVERHEAD + ['provider_comp', 'non_operating', 'revenue', 'other_income', 'card_lump'])
    owner_pay = 0
    if pnl:
        for e in pnl_lines:
            if e['cat'] == 'revenue' and e['section'] != 'income':
                reported['revenue'] -= e['amount']
            else:
                reported[e['cat']] += e['amount']
            if e['kind'] == 'Owner pay':
                owner_pay += e['amount']
    else:
        for it in items:
            reported[it['cat']] += it['amount']
        for r in vendor_rows:
            reported[r['cat']] += r['amount']
        reported['revenue'] += income_in_window
    likely, possible, confirmed = [dict((k, 0) for k in reported) for _ in range(3)]
    for g in groups.values():
        if g['amount'] <= 0:
            continue
        if g['share'] is not None:
            confirmed[g['cat']] += div_round(g['amount'] * g['share'], 100)
        elif g['level'] == 3:
            likely[g['cat']] += g['amount']
        elif g['level'] == 2:
            possible[g['cat']] += g['amount']

    rev = ann(reported['revenue'])
    phys, apps = st['physicians'], st['apps']
    prov = phys + apps
    pct = lambda c: tenths(div_round(c * 1000, rev)) if rev > 0 else ''
    per = lambda c, fte: money(div_round(c * 100, fte)) if fte > 0 else ''

    def versus(c, ref, band):
        if ref is None or rev <= 0:
            return ''
        gap = div_round(c * 1000, rev) - ref
        if gap > band:
            return 'above by %s points' % tenths(gap)
        if -gap > band:
            return 'below by %s points' % tenths(-gap)
        return 'in line'

    out = [['section', 'line', 'reported_annual', 'likely_add_backs', 'owner_confirmed_add_backs', 'normalized_annual',
            'possible_add_backs_not_counted', 'normalized_pct_of_revenue', 'normalized_per_fte_physician',
            'normalized_per_fte_provider', 'benchmark_pct_of_revenue', 'vs_benchmark', 'benchmark_source']]
    out.append(['Revenue', CAT_LABEL['revenue'], money(rev), '', '', money(rev), '', pct(rev), per(rev, phys),
                per(rev, prov), '', '', ''])
    tr = tl = tc = tp = 0
    for key, label, ref, src in CATEGORIES:
        if key in bench:
            ref, src = bench[key]
        r, l, c, p = ann(reported[key]), ann(likely[key]), ann(confirmed[key]), ann(possible[key])
        nrm = r - l - c
        tr, tl, tc, tp = tr + r, tl + l, tc + c, tp + p
        out.append(['Operating cost', label, money(r), money(l), money(c), money(nrm), money(p), pct(nrm),
                    per(nrm, phys), per(nrm, prov), tenths(ref) if ref is not None else '',
                    versus(nrm, ref, max(10, div_round((ref or 0) * 25, 100))), src if ref is not None else ''])
    l, c, p = ann(likely['card_lump']), ann(confirmed['card_lump']), ann(possible['card_lump'])
    if l or c or p:
        # charges only on a card statement: in the P&L somewhere (the practice pays the card), but no one category
        tl, tc, tp = tl + l, tc + c, tp + p
        out.append(['Operating cost', CAT_LABEL['card_lump'], '', money(l), money(c), '', money(p), '', '', '', '', '', ''])
    nt = tr - tl - tc
    out.append(['Operating cost', 'Total operating cost', money(tr), money(tl), money(tc), money(nt), money(tp), pct(nt),
                per(nt, phys), per(nt, prov), tenths(TOTAL_REF[0]), versus(nt, TOTAL_REF[0], 30), TOTAL_REF[1]])
    for key in ('provider_comp', 'non_operating'):
        r = ann(reported[key])
        out.append(['Not operating cost', CAT_LABEL[key], money(r), '', '', money(r), '', pct(r), per(r, phys),
                    per(r, prov), '', '', ''])
    if pnl:
        ni = ann(reported['revenue'] + reported['other_income'] - sum(reported[k] for k in OVERHEAD)
                 - reported['provider_comp'] - reported['non_operating'])
        nonop, oth = ann(reported['non_operating']), ann(reported['other_income'])
        ebitda = ni + nonop - oth
        adj = ebitda + tl + tc
        for label, v in (('Net income as reported', ni),
                         ('Add back interest, depreciation, amortization, income taxes', nonop),
                         ('Take out other income (not from patient care)', -oth),
                         ('EBITDA as reported', ebitda),
                         ('Add likely owner add-backs (High)', tl),
                         ('Add add-backs the owner confirmed', tc),
                         ('Adjusted EBITDA', adj),
                         ('Possible add-backs not counted yet (Medium)', tp),
                         ('Adjusted EBITDA if every possible add-back holds', adj + tp),
                         ('Owner pay booked in the P&L (set to market pay at the site visit)', ann(owner_pay))):
            out.append(['Earnings', label, '', '', '', money(v), '', pct(v), '', '', '', '', ''])
    normalized = csv_text(out)

    rv = [['review_id', 'confidence', 'looks_like', 'payee', 'account', 'category', 'charges', 'months_with_charges',
           'first_charge', 'last_charge', 'amount_in_period', 'annualized', 'why_flagged', 'owner_decision']]
    listed = 0
    for g in sorted(groups.values(), key=lambda g: (-g['level'], -g['amount'], g['id'])):
        if g['amount'] <= 0 or (g['level'] == 1 and ann(g['amount']) < LOW_LIST_MIN):
            continue
        listed += 1
        rv.append([g['id'], LEVEL_NAMES[g['level']], g['kind'], g['who'], g['account'], CAT_LABEL[g['cat']],
                   str(g['n']) if g['n'] else '', str(len(g['months'])) if g['months'] else '',
                   iso(g['first']) if g['first'] else '', iso(g['last']) if g['last'] else '', money(g['amount']),
                   money(ann(g['amount'])), '; '.join(r for r in g['reasons'] if r), decisions.get(g['id'], '')])
    review = csv_text(rv)

    level_of = dict((g['id'], g['level']) for g in groups.values())
    ld = [['source_file', 'row_type', 'date', 'payee', 'account', 'category', 'amount', 'confidence', 'review_id']]
    wtext = '%s to %s' % (ym_text(window[0]), ym_text(window[-1])) if window else ''
    for e in pnl_lines:
        ld.append([pnl['file'], 'P&L account, period total', wtext, '', (e['parent'] + ': ' if e['parent'] else '') + e['label'],
                   CAT_LABEL[e['cat']], money(e['amount']), LEVEL_NAMES[level_of.get(e['gid'], 0)], e['gid']])
    for it in sorted(items, key=lambda i: (i['date'], i['file'], i['shown'], i['amount'])):
        ld.append([it['file'], 'card charge' if it['src'] == 'card' else 'transaction', iso(it['date']), it['shown'],
                   it['account'] if it['src'] == 'ledger' else '', CAT_LABEL[it['cat']], money(it['amount']),
                   LEVEL_NAMES[level_of.get(it['gid'], 0)], it['gid']])
    for r in vendor_rows:
        ld.append(['', 'vendor total', '', r['shown'], '', CAT_LABEL[r['cat']], money(r['amount']),
                   LEVEL_NAMES[level_of.get(r['gid'], 0)], r['gid']])
    for k in sorted(collapsed):
        n, c = collapsed[k]
        ld.append(['', '%s, %d rows collapsed, no names' % (k[0].lower(), n), str(k[1]), '', k[2],
                   CAT_LABEL['revenue'] if k[0] == 'Income' else 'Patient refunds', money(c), '', ''])
    detail = csv_text(ld)
    key_text = csv_text([['code', 'name']] + [names[k] for k in name_order])

    # ----- the record -----
    rec.append('')
    if window:
        rec.append('Period: %s to %s, %d months with data%s.' % (ym_text(window[0]), ym_text(window[-1]), nmonths,
                   ', scaled to 12 to make a year' if nmonths != 12 else ''))
    elif nmonths:
        rec.append('Period: %d months%s.' % (nmonths, ', scaled to 12 to make a year' if nmonths != 12 else ''))
    if pnl:
        checks = []
        for key, label in (('income', 'Income'), ('cogs', 'Cost of goods sold'), ('expense', 'Expenses'),
                           ('other_income', 'Other income'), ('other_expense', 'Other expenses')):
            if key in pnl['file_totals']:
                mine = sum(sum(ln['amounts'].values()) for ln in pnl['lines'] if ln['section'] == key)
                ft = pnl['file_totals'][key]
                checks.append('- %s: the accounts add to %s; the report\'s own total says %s.%s' % (
                    label, money(mine), money(ft), '' if mine == ft else ' THEY DIFFER: check the export.'))
        if checks:
            rec.append('Reconciliation, over every month in the report:')
            rec.extend(checks)
        unmapped = [e['label'] for e in pnl_lines if not e['mapped']]
        if unmapped:
            rec.append('Accounts no rule placed (counted as miscellaneous, or non-operating under Other expenses): %s.'
                       % ', '.join(unmapped))
        if pnl['skipped']:
            rec.append('%d rows with amounts sat above the first section heading and were left out.' % pnl['skipped'])
    rec.append('')
    rec.append('Privacy, for the files shared outside the practice:')
    rec.append('- Patient refund rows: %d, collapsed to one total per year and account, with no names, dates or memos.'
               % sum(v[0] for k, v in collapsed.items() if k[0] == 'Patient refunds'))
    rec.append('- Income rows: %d, collapsed the same way, because customer names on income lines can be patients.'
               % sum(v[0] for k, v in collapsed.items() if k[0] == 'Income'))
    rec.append('- Individuals\' names replaced by codes: %d. pnl-name-key.csv holds the real names and stays at the '
               'practice.' % len(names))
    rec.append('- Memo and description text is read to match payees and never written out. Card merchant names are '
               'shown without any numbers.')
    if left['balance'] or left['payments'] or left['outside']:
        rec.append('')
        rec.append('Left out: %d balance sheet rows (bank, card, loan and equity accounts), %d card payments, %d rows '
                   'outside the period.' % (left['balance'], left['payments'], left['outside']))
    if left['draws']:
        rec.append('Owner draws or distributions in the detail (equity, not expense): %s.' % money(left['draws']))
    if mirrors:
        rec.append('Rows that appeared twice in the detail (under the expense account and under the card or bank '
                   'account) and counted once: %d.' % len(mirrors))
    if cards:
        rec.append('Card statement rows matched to a QuickBooks row (same amount, within 5 days) and counted once: %d.'
                   % matched)
        unmatched = sum(1 for it in items if it['src'] == 'card')
        if pnl and unmatched:
            rec.append('Card statement charges with no matching QuickBooks row: %d. They are taken as practice spending booked '
                       'in a lump when the card is paid, so their add-backs come off total operating cost, not one category. '
                       'Leave out the statement of any card the practice does not pay.' % unmatched)
    if home:
        rec.append('Home state, from the most common state on charges: %s.' % home)
    if vendors and items:
        rec.append('The expenses by vendor summary was not needed: line detail came in, and it is finer.')
    rec.append('')
    rec.append('Review list: %d groups. How it decides:' % listed)
    rec.append('- High (likely add-back): personal by nature, such as streaming, clothing, school, clubs, leisure '
               'travel, charity, political giving, or a payee sharing a family name. Counted in Adjusted EBITDA.')
    rec.append('- Medium (possible): spending that often mixes personal and practice use, such as meals, travel, fuel, '
               'groceries and transfers to individuals. Shown, not counted, until the owner answers.')
    rec.append('- Low: mixed-use stores such as Amazon and Costco, listed above $1,000 a year, to sample at the visit.')
    rec.append('- A group moves up one level when most of its charges fall on a weekend or outside the home state.')
    rec.append('- To answer, write personal, business, or a percent personal (like 40%%) in owner_decision, then run '
               'the tool again with the answered file added. Answers used this run: %d.' % used)
    rec.append('')
    rec.append('Benchmarks are percent of total revenue by MGMA cost category, and "in line" means within a quarter '
               'of the reference (at least 1 point). The built-in references are the best public figures and are '
               'dated: category splits are MGMA family medicine 2008 data (AAFP FPM, March 2010), support staff is '
               'MGMA primary care 2025 data, billing is an MGMA Stat 2025 rule of thumb, and total operating cost is '
               'MGMA primary care 2022 data. Current category figures sit in MGMA DataDive; add a benchmarks CSV '
               '(category, pct_of_revenue, source) to use them.')
    rec.append('Depreciation sits with interest and taxes here, outside operating cost; MGMA counts it inside, usually '
               'about 1%% of revenue. Providers: %s physician FTE and %s NP/PA FTE, counted one to one for per-provider '
               'figures as MGMA does.' % (money(phys), money(apps)))
    if notes:
        rec.append('')
        rec.extend(notes)
    rec.append('')
    return {OUT[0]: normalized, OUT[1]: review, OUT[2]: detail, OUT[3]: '\n'.join(rec), OUT[4]: key_text}


def decision_share(d):
    if d is None:
        return None
    dn = norm(d).strip()
    if dn in ('personal', 'p', 'yes', 'y', 'add back', 'addback', 'all personal'):
        return 100
    if dn in ('business', 'b', 'no', 'n', 'keep', 'practice'):
        return 0
    m = re.fullmatch(r'\s*([0-9]{1,3})\s*%?\s*(?:personal)?\s*', d.lower())
    if m and int(m.group(1)) <= 100:
        return int(m.group(1))
    return None


def is_payment(x):
    if norm(x['type']).strip() in ('payment', 'autopay', 'payment received'):
        return True
    return bool(hit(norm(x['desc'] + ' ' + x['memo']), PAYMENT_WORDS) and x['amount'] <= 0) or \
        bool(hit(norm(x['desc']), STRONG_PAYMENT))


def csv_text(rows):
    def q(v):
        v = str(v)
        if v[:1] in ('=', '+', '@', '\t', '\r') or (v[:1] == '-' and len(v) > 1 and not re.match(r'[0-9.]', v[1])):
            v = "'" + v
        if any(c in v for c in ',"\r\n'):
            v = '"' + v.replace('"', '""') + '"'
        return v
    return ''.join(','.join(q(v) for v in r) + '\r\n' for r in rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Normalize a practice P&L and list likely owner add-backs, offline.')
    ap.add_argument('files', nargs='+', help='QuickBooks exports, card statements, an answered review file')
    ap.add_argument('--physicians', default='1', help='physician FTE (default 1)')
    ap.add_argument('--apps', default='0', help='NP and PA FTE (default 0)')
    ap.add_argument('--family', default='', help='owner and family last names, comma separated')
    ap.add_argument('--out', default='pnl-output')
    a = ap.parse_args(argv)
    files = []
    for f in a.files:
        p = Path(f)
        for q in (sorted(x for x in p.rglob('*') if x.is_file()) if p.is_dir() else [p]):
            if q.suffix.lower() in ('.csv', '.xlsx', '.txt'):
                files.append((q.name, q.read_bytes()))
    res = run(files, {'physicians': a.physicians, 'apps': a.apps, 'family': a.family})
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for k, v in res.items():
        (out / k).write_text(v, encoding='utf-8', newline='')
    print('Wrote %d files to %s. Share all but pnl-name-key.csv; that one stays at the practice.' % (len(res), out))


if __name__ == '__main__':
    main()
