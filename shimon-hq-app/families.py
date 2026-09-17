"""Families: the almanos / yesomim directory behind the Shefa Yoel line.

The CRM (Simon's) stays the system of record. HQ keeps a copy - one row per family,
children folded in as JSON, every phone number indexed - so a voicemail can say who
is calling before anyone presses play. Loaded from the CRM's "Global Families Export"
spreadsheet; re-uploading replaces what changed and keeps HQ-side notes.
"""
import json
import re
from datetime import datetime


def ensure_schema(con):
    con.executescript(
        "CREATE TABLE IF NOT EXISTS families ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " die_id TEXT UNIQUE,"            # the CRM's family key, e.g. 690-Weiss/ Jungreisz
        " name_en TEXT, name_yi TEXT,"
        " father_en TEXT, father_yi TEXT, father_deceased INTEGER, father_yurtzeit TEXT,"
        " mother_en TEXT, mother_yi TEXT, mother_deceased INTEGER, mother_yurtzeit TEXT,"
        " street TEXT, unit TEXT, city TEXT, state TEXT, zip TEXT, area TEXT,"
        " home_phone TEXT, mobile_phone TEXT,"
        " chasidus TEXT, qbo_payee TEXT, crm_notes TEXT, mayer_comments TEXT, status_changes TEXT,"
        " children TEXT,"                 # JSON list
        " active INTEGER NOT NULL DEFAULT 1, source TEXT,"
        " hq_notes TEXT,"                 # ours, survives re-imports
        " imported_at TEXT);"
        "CREATE INDEX IF NOT EXISTS fam_name ON families(name_en);"
        "CREATE TABLE IF NOT EXISTS family_phones ("
        " digits TEXT NOT NULL, family_id INTEGER NOT NULL, kind TEXT,"
        " PRIMARY KEY(digits, family_id));"
        "CREATE INDEX IF NOT EXISTS fam_phone ON family_phones(digits);")


def digits10(v):
    """Last ten digits of anything that looks like a US number; '' if not a number."""
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def _yes(v):
    return 1 if str(v or "").strip().lower() in ("yes", "y", "true", "1") else 0


def _s(v):
    return str(v).strip() if v is not None else ""


COLS = ["Father First Name (English)", "Father First Name (Yiddish)", "Father Deceased", "Father Yurtzeit",
        "Mother First Name (English)", "Mother First Name (Yiddish)", "Mother Deceased", "Mother Yurtzeit",
        "Street Address", "Unit / Apt", "City", "State", "Zip", "Area",
        "Family Name (English)", "Family Name (Yiddish)", "Home Phone", "Mobile Phone", "Chasidus",
        "Active", "Source", "Die ID", "QBO Payee", "Notes", "Mrs Mayer Comments", "Status Changes 2025",
        "First Name (English)", "First Name (Yiddish)", "Gender", "Order", "DOB (English)", "Age",
        "DOB (Hebrew)", "School", "Grade", "Bar/Bat Mitzvah Date (Hebrew)",
        "Bar/Bat Mitzvah Date (Gregorian)", "Married", "Spouse Name"]


def parse_export(path):
    """The CRM's Global Families Export (.xlsx): one row per child, the family columns
    repeated. Returns {die_id: family dict} with children folded in."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    hdr = None
    for r in rows:
        if r and "Die ID" in [str(x).strip() for x in r if x]:
            hdr = [str(x).strip() if x else "" for x in r]
            break
    if not hdr:
        raise ValueError("Could not find the header row (no 'Die ID' column).")
    H = {h: i for i, h in enumerate(hdr) if h}

    def g(r, name):
        i = H.get(name)
        return _s(r[i]) if i is not None and i < len(r) else ""

    fams = {}
    for r in rows:
        if not r or not g(r, "Die ID"):
            continue
        k = g(r, "Die ID")
        f = fams.get(k)
        if not f:
            f = fams[k] = dict(
                die_id=k, name_en=g(r, "Family Name (English)"), name_yi=g(r, "Family Name (Yiddish)"),
                father_en=g(r, "Father First Name (English)"), father_yi=g(r, "Father First Name (Yiddish)"),
                father_deceased=_yes(g(r, "Father Deceased")), father_yurtzeit=g(r, "Father Yurtzeit"),
                mother_en=g(r, "Mother First Name (English)"), mother_yi=g(r, "Mother First Name (Yiddish)"),
                mother_deceased=_yes(g(r, "Mother Deceased")), mother_yurtzeit=g(r, "Mother Yurtzeit"),
                street=g(r, "Street Address"), unit=g(r, "Unit / Apt"), city=g(r, "City").title(),
                state=g(r, "State"), zip=g(r, "Zip"), area=g(r, "Area"),
                home_phone=g(r, "Home Phone"), mobile_phone=g(r, "Mobile Phone"),
                chasidus=g(r, "Chasidus"), qbo_payee=g(r, "QBO Payee"), crm_notes=g(r, "Notes"),
                mayer_comments=g(r, "Mrs Mayer Comments"), status_changes=g(r, "Status Changes 2025"),
                active=_yes(g(r, "Active") or "Yes"), source=g(r, "Source"), children=[])
        child = dict(name_en=g(r, "First Name (English)"), name_yi=g(r, "First Name (Yiddish)"),
                     gender=g(r, "Gender"), order=g(r, "Order"), dob=g(r, "DOB (English)"),
                     dob_heb=g(r, "DOB (Hebrew)"), age=g(r, "Age"), school=g(r, "School"),
                     grade=g(r, "Grade"), bm_heb=g(r, "Bar/Bat Mitzvah Date (Hebrew)"),
                     bm=g(r, "Bar/Bat Mitzvah Date (Gregorian)"), married=_yes(g(r, "Married")),
                     spouse=g(r, "Spouse Name"))
        if child["name_en"] or child["name_yi"]:
            f["children"].append({k2: v for k2, v in child.items() if v not in ("", 0, None)})
    return fams


def import_export(con, path):
    """Upsert every family from the spreadsheet. Returns (families, new, phones)."""
    fams = parse_export(path)
    now = datetime.now().isoformat(timespec="seconds")
    new = 0
    for k, f in fams.items():
        kids = json.dumps(f.pop("children"), ensure_ascii=False)
        row = con.execute("SELECT id FROM families WHERE die_id=?", (k,)).fetchone()
        cols = list(f.keys())
        if row:
            con.execute("UPDATE families SET %s, children=?, imported_at=? WHERE id=?"
                        % ", ".join("%s=?" % c for c in cols),
                        [f[c] for c in cols] + [kids, now, row[0]])
            fid = row[0]
        else:
            fid = con.execute("INSERT INTO families(%s, children, imported_at) VALUES(%s)"
                              % (", ".join(cols), ",".join("?" * (len(cols) + 2))),
                              [f[c] for c in cols] + [kids, now]).lastrowid
            new += 1
        con.execute("DELETE FROM family_phones WHERE family_id=?", (fid,))
        for kind in ("home_phone", "mobile_phone"):
            d = digits10(f[kind])
            if d:
                con.execute("INSERT OR IGNORE INTO family_phones(digits, family_id, kind) VALUES(?,?,?)",
                            (d, fid, kind))
    con.commit()
    phones = con.execute("SELECT COUNT(*) FROM family_phones").fetchone()[0]
    return len(fams), new, phones


def lookup_phone(con, number):
    """Families whose home or mobile matches this caller. Usually one; sometimes a
    number is shared (a mother remarried, a household line)."""
    d = digits10(number)
    if not d:
        return []
    return con.execute(
        "SELECT f.*, p.kind FROM family_phones p JOIN families f ON f.id=p.family_id"
        " WHERE p.digits=? ORDER BY f.active DESC, f.id", (d,)).fetchall()


def label(f):
    """'Weiss / Jungreisz · Monroe' - how a family reads on a voicemail row."""
    name = (f["name_en"] or f["name_yi"] or "").strip().strip("/").strip()
    bits = [name]
    if f["area"]:
        bits.append(f["area"])
    return " · ".join(b for b in bits if b)


def search(con, q, limit=60):
    q = (q or "").strip()
    if not q:
        return con.execute("SELECT * FROM families ORDER BY name_en LIMIT ?", (limit,)).fetchall()
    d = digits10(q) or re.sub(r"\D", "", q)
    if d and len(d) >= 4:
        return con.execute(
            "SELECT DISTINCT f.* FROM families f JOIN family_phones p ON p.family_id=f.id"
            " WHERE p.digits LIKE ? ORDER BY f.name_en LIMIT ?", ("%" + d + "%", limit)).fetchall()
    like = "%" + q + "%"
    return con.execute(
        "SELECT * FROM families WHERE name_en LIKE ? OR name_yi LIKE ? OR mother_en LIKE ? OR father_en LIKE ?"
        " OR street LIKE ? OR qbo_payee LIKE ? OR die_id LIKE ? OR children LIKE ?"
        " ORDER BY name_en LIMIT ?", (like,) * 8 + (limit,)).fetchall()


def children(f):
    try:
        return json.loads(f["children"] or "[]")
    except ValueError:
        return []
