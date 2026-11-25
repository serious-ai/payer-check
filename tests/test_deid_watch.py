"""The hands-off scrub's promises.

1. The independent leak check passes a correct de-identified copy and FAILS every planted leak: an encounter
   number, a full date, a patient name, a phone number, a member ID, a note, an age over 89, a dropped row, and a
   raw value pasted into a kept column.
2. The watched-folder run: nothing happens until the folder is armed; a file still downloading is left alone; a
   passing file releases only the copy, its record and the leak report to the outbox, the key goes to
   keys-for-practice and the raw file is deleted; a failing file releases nothing and moves to quarantine.
3. Nothing that leaves PHI-Work (outbox files, notifications aside) carries a raw value or the raw file name.
"""
import csv, io, os, re, sys, time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import deidentify as di  # noqa: E402
import deid_watch as dw  # noqa: E402

SAMPLE = ROOT / 'sample-data' / 'deid' / 'sample-cpt-payment-report.csv'


def copy_of(path, overrides=None, broken=False, monkeypatch=None):
    """broken=True stands in for a de-identifier with a bug: it keeps the column as asked AND skips its own
    inline scrub, so the raw values really do land in the copy. The leak check must catch that on its own."""
    data = path.read_bytes()
    if broken:
        monkeypatch.setattr(di, 'scrub', lambda cell, counts, amounts=False: cell[0])
    files = di.run(path.name, data, overrides, 7)
    return data, next(v for k, v in files.items() if k.endswith('-deidentified.csv'))


def failed(lines):
    return [l for l in lines if l.startswith('FAIL')]


def test_a_correct_copy_passes():
    for p in (SAMPLE, SAMPLE.with_suffix('.xlsx')):
        data, deid = copy_of(p)
        ok, lines = dw.leak_check(p.name, data, deid)
        assert ok, failed(lines)


@pytest.mark.parametrize('column,action,expect', [
    ('Enc#', 'keep', 'identifiers'),
    ('DOS', 'keep', 'date'),
    ('Patient Name', 'keep', 'headers'),
    ('Pt Phone', 'keep', 'phone'),
    ('Member ID', 'keep', 'identifiers'),
    ('Notes', 'keep', 'date'),
    ('Age', 'keep', 'ages'),
])
def test_each_planted_leak_is_caught(column, action, expect, monkeypatch):
    data, deid = copy_of(SAMPLE, {column: action}, broken=True, monkeypatch=monkeypatch)
    ok, lines = dw.leak_check(SAMPLE.name, data, deid)
    assert not ok
    assert any(expect in l for l in failed(lines)), (column, failed(lines))


def test_a_raw_value_pasted_into_a_kept_column_is_caught():
    data, deid = copy_of(SAMPLE)
    rows = list(csv.reader(io.StringIO(deid, newline='')))
    raw_enc = re.search(r'^([0-9]{7}),', SAMPLE.read_text(), re.M).group(1)
    rows[5][rows[0].index('Sv It Desc')] += ' ' + raw_enc
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\r\n').writerows(rows)
    ok, lines = dw.leak_check(SAMPLE.name, data, buf.getvalue())
    assert not ok and any('reappear' in l for l in failed(lines))


def test_a_dropped_row_is_caught():
    data, deid = copy_of(SAMPLE)
    rows = deid.split('\r\n')
    ok, lines = dw.leak_check(SAMPLE.name, data, '\r\n'.join(rows[:3] + rows[4:]))
    assert not ok and any('rows' in l for l in failed(lines))


# ---------- the watched folder ----------
@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(dw, 'notify', lambda msg: None)
    return dw.folders(tmp_path / 'PHI-Work', tmp_path / 'outbox')


def drop(box, name='SMITH JOHN cpt payment report 11-21-2025 to 11-20-2026.csv', data=None, age=60):
    f = box['inbox'] / name
    f.write_bytes(data if data is not None else SAMPLE.read_bytes())
    t = time.time() - age
    os.utime(f, (t, t))
    return f


def test_nothing_runs_until_armed(box):
    f = drop(box)
    assert dw.run_once(box, seed=3) == []
    assert f.exists() and not any(box['outbox'].iterdir())
    assert 'NOT ARMED' in box['log'].read_text()


def test_a_download_in_progress_is_left_alone(box):
    box['gate'].write_text('Readiness Notice sent 2026-11-30\n')
    drop(box, name='report.csv.crdownload')
    f = drop(box, name='report.csv', age=0)
    assert dw.run_once(box, seed=3) == []
    assert f.exists()


def test_a_passing_file_releases_only_the_copy(box):
    box['gate'].write_text('Readiness Notice sent 2026-11-30\n')
    f = drop(box)
    (res,) = dw.run_once(box, seed=3)
    assert res[0] is True and not f.exists()
    out = sorted(p.name for p in box['outbox'].iterdir())
    assert len(out) == 3 and all(n.startswith('deid-') for n in out)
    assert all('-cpt-payment-report-' in n and 'SMITH' not in n.upper() and 'JOHN' not in n.upper() for n in out), out
    keys = list(box['keys'].iterdir())
    assert len(keys) == 1 and keys[0].name.endswith('-visit-code-key.csv') and oct(keys[0].stat().st_mode)[-3:] == '600'
    released = ''.join(p.read_text() for p in box['outbox'].iterdir())
    raw = SAMPLE.read_text()
    for v in set(re.findall(r'^([0-9]{7}),', raw, re.M)) | set(re.findall(r'SAMPLE PATIENT [0-9]+', raw)) | {'SMITH', 'JOHN', '(805) 555'}:
        assert v not in released, v
    report = next(p for p in box['outbox'].iterdir() if p.name.endswith('-leak-check.txt')).read_text()
    assert report.startswith('Leak check: PASSED') and 'Report period: 12.0 months' in report
    assert 'PASS' in box['log'].read_text() and 'SMITH' not in box['log'].read_text()


def test_a_failing_file_releases_nothing(box, monkeypatch):
    box['gate'].write_text('Readiness Notice sent 2026-11-30\n')
    real = di.run
    monkeypatch.setattr(dw.di, 'scrub', lambda cell, counts, amounts=False: cell[0])
    monkeypatch.setattr(dw.di, 'run', lambda n, d, o=None, s=None: real(n, d, {'Enc#': 'keep'}, s))
    f = drop(box)
    (res,) = dw.run_once(box, seed=3)
    assert res[0] is False and not f.exists()
    out = [p.name for p in box['outbox'].iterdir()]
    assert len(out) == 1 and out[0].endswith('-BLOCKED.txt')
    text = (box['outbox'] / out[0]).read_text()
    assert 'BLOCKED' in text and 'identifiers' in text
    assert not any(v in text for v in re.findall(r'^([0-9]{7}),', SAMPLE.read_text(), re.M))
    q = [p for p in box['quarantine'].iterdir()]
    assert len(q) == 1 and any(p.name == f.name for p in q[0].iterdir())
    assert not any(box['keys'].iterdir())


def test_an_unreadable_file_is_blocked_without_quoting_it(box):
    box['gate'].write_text('armed\n')
    drop(box, name='bad.csv', data=b'\x00\x01 nothing like a report 4410037')
    (res,) = dw.run_once(box, seed=3)
    assert res[0] is False
    text = next(box['outbox'].iterdir()).read_text()
    assert '4410037' not in text and 'stopped' in text
