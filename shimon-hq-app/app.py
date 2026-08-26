import os
import re
import json
import uuid
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (Flask, g, render_template, request, redirect,
                   url_for, session, jsonify, send_from_directory, abort)
from markupsafe import Markup, escape

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "hq.db"))
FILES_DIR = os.environ.get("FILES_DIR",
                           os.path.join(os.path.dirname(DB_PATH) or BASE, "files"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-key-change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB per upload

_URL_RE = re.compile(r"(https?://[^\s<>\"]+)")


@app.context_processor
def inject_css_version():
    v = 0
    for name in ("style.css", "board.js", "icon-512.png"):
        try:
            v = max(v, int(os.path.getmtime(os.path.join(BASE, "static", name))))
        except OSError:
            pass
    return {"css_v": v}


@app.template_filter("linkify")
def linkify(text):
    """Escape text, then turn URLs into safe links."""
    out = []
    last = 0
    s = str(text or "")
    for m in _URL_RE.finditer(s):
        out.append(escape(s[last:m.start()]))
        url = m.group(1)
        out.append(Markup('<a href="%s" target="_blank" rel="noopener">%s</a>')
                   % (url, url if len(url) <= 45 else url[:42] + "…"))
        last = m.end()
    out.append(escape(s[last:]))
    return Markup("").join(out)

HQ_USER = os.environ.get("HQ_USER", "shimon")
HQ_PASSWORD = os.environ.get("HQ_PASSWORD", "changeme")

STATUSES = ("open", "waiting", "done")


# ---------- db ----------

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    con = sqlite3.connect(DB_PATH)
    with open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
    if "due_date" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN due_date TEXT")
    if "project_id" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN project_id INTEGER REFERENCES projects(id)")
    if "remind_at" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN remind_at TEXT")
    ecols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
    if ecols and "source" not in ecols:
        con.execute("ALTER TABLE events ADD COLUMN source TEXT DEFAULT 'outlook'")
    if ecols and "note" not in ecols:
        con.execute("ALTER TABLE events ADD COLUMN note TEXT DEFAULT ''")
    os.makedirs(FILES_DIR, exist_ok=True)
    n = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    if n == 0:
        with open(os.path.join(BASE, "seed_data.json"), encoding="utf-8") as f:
            data = json.load(f)
        now = datetime.now().isoformat(timespec="seconds")
        for si, sec in enumerate(data["sections"]):
            cur = con.execute("INSERT INTO sections(title, pos) VALUES(?, ?)",
                              (sec["title"], si))
            sid = cur.lastrowid
            for pi, it in enumerate(sec["items"]):
                con.execute(
                    "INSERT INTO items(section_id, title, note, waiting_on, status, pos, updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (sid, it["t"], it.get("n", ""), it.get("w", ""),
                     it.get("s", "open"), pi, now))
    con.commit()
    con.close()


# ---------- auth ----------

def login_required(f):
    @wraps(f)
    def wrapped(*a, **k):
        if not session.get("user"):
            if request.method == "POST" or request.path.startswith("/api/"):
                return jsonify(error="login required"), 401
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (request.form.get("username", "").strip().lower() == HQ_USER.lower()
                and request.form.get("password", "") == HQ_PASSWORD):
            session["user"] = HQ_USER
            session.permanent = True
            return redirect(url_for("board"))
        error = "Wrong username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- pages ----------

@app.route("/")
@login_required
def board():
    from datetime import timedelta
    con = db()
    sections = con.execute("SELECT * FROM sections ORDER BY pos, id").fetchall()
    items = con.execute("SELECT * FROM items ORDER BY pos, id").fetchall()
    notes = con.execute("SELECT * FROM item_notes ORDER BY id").fetchall()
    notes_by_item = {}
    for n in notes:
        notes_by_item.setdefault(n["item_id"], []).append(n)
    files = con.execute("SELECT * FROM item_files ORDER BY id").fetchall()
    files_by_item = {}
    for f in files:
        files_by_item.setdefault(f["item_id"], []).append(f)
    projects = con.execute("SELECT * FROM projects ORDER BY pos, id").fetchall()
    projects_by_sec = {}
    for p in projects:
        projects_by_sec.setdefault(p["section_id"], []).append(p)
    today_iso = datetime.now().date().isoformat()
    soon_iso = (datetime.now().date() + timedelta(days=3)).isoformat()
    by_sec = {}
    for it in items:
        by_sec.setdefault(it["section_id"], []).append(it)
    total_active = sum(1 for it in items if it["status"] != "done")
    stats = {
        "open": sum(1 for it in items if it["status"] == "open"),
        "waiting": sum(1 for it in items if it["status"] == "waiting"),
        "done": sum(1 for it in items if it["status"] == "done"),
    }
    return render_template("board.html", sections=sections, by_sec=by_sec,
                           projects_by_sec=projects_by_sec,
                           notes_by_item=notes_by_item, files_by_item=files_by_item,
                           today_iso=today_iso, soon_iso=soon_iso,
                           total_active=total_active, stats=stats,
                           today=datetime.now().strftime("%b %-d, %Y")
                           if os.name != "nt" else datetime.now().strftime("%b %d, %Y"))


# ---------- item actions ----------

@app.route("/items/add", methods=["POST"])
@login_required
def add_item():
    sid = request.form.get("section_id", type=int)
    title = (request.form.get("title") or "").strip()
    if not sid or not title:
        return redirect(url_for("board"))
    con = db()
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    con.execute(
        "INSERT INTO items(section_id, title, note, waiting_on, status, pos, due_date,"
        " project_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, title, (request.form.get("note") or "").strip(),
         (request.form.get("waiting_on") or "").strip(), "open", pos,
         (request.form.get("due_date") or "").strip() or None,
         request.form.get("project_id", type=int) or None,
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    if (request.form.get("due_date") or "").strip():
        return redirect(url_for("day_view", day=request.form["due_date"].strip()))
    return redirect(url_for("board", _anchor="sec-%d" % sid))


@app.route("/items/<int:item_id>/edit", methods=["POST"])
@login_required
def edit_item(item_id):
    title = (request.form.get("title") or "").strip()
    if not title:
        return redirect(url_for("board"))
    con = db()
    cur_row = con.execute("SELECT section_id FROM items WHERE id=?", (item_id,)).fetchone()
    if not cur_row:
        abort(404)
    sid = request.form.get("section_id", type=int) or cur_row["section_id"]
    pid = request.form.get("project_id", type=int) or None
    if pid:
        # a project decides the section it lives in
        prow = con.execute("SELECT section_id FROM projects WHERE id=?", (pid,)).fetchone()
        if prow:
            sid = prow["section_id"]
        else:
            pid = None
    con.execute(
        "UPDATE items SET title=?, note=?, waiting_on=?, due_date=?, section_id=?, project_id=?,"
        " updated_at=? WHERE id=?",
        (title, (request.form.get("note") or "").strip(),
         (request.form.get("waiting_on") or "").strip(),
         (request.form.get("due_date") or "").strip() or None,
         sid, pid,
         datetime.now().isoformat(timespec="seconds"), item_id))
    con.execute("UPDATE items SET remind_at=? WHERE id=?",
                ((request.form.get("remind_at") or "").strip() or None, item_id))
    con.commit()
    return redirect(url_for("board"))


@app.route("/items/<int:item_id>/notes", methods=["POST"])
@login_required
def add_note(item_id):
    body = (request.form.get("body") or "").strip()
    note_id, created = None, None
    if body:
        con = db()
        created = datetime.now().isoformat(timespec="seconds")
        cur = con.execute("INSERT INTO item_notes(item_id, body, created_at) VALUES(?,?,?)",
                          (item_id, body, created))
        note_id = cur.lastrowid
        con.execute("UPDATE items SET updated_at=? WHERE id=?", (created, item_id))
        con.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(id=note_id, body=body, created_at=created)
    return redirect(url_for("board"))


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id):
    con = db()
    con.execute("DELETE FROM item_notes WHERE id=?", (note_id,))
    con.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("board"))


@app.route("/items/<int:item_id>/cycle", methods=["POST"])
@login_required
def cycle_item(item_id):
    con = db()
    row = con.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        abort(404)
    nxt = STATUSES[(STATUSES.index(row["status"]) + 1) % 3] \
        if row["status"] in STATUSES else "open"
    con.execute("UPDATE items SET status=?, updated_at=? WHERE id=?",
                (nxt, datetime.now().isoformat(timespec="seconds"), item_id))
    con.commit()
    return jsonify(status=nxt)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_item(item_id):
    con = db()
    con.execute("DELETE FROM items WHERE id=?", (item_id,))
    con.commit()
    return redirect(url_for("board"))


@app.route("/capture", methods=["POST"])
@login_required
def capture():
    """Quick capture — one line into the Inbox section."""
    title = (request.form.get("title") or "").strip()
    if not title:
        return redirect(url_for("board"))
    con = db()
    row = con.execute("SELECT id FROM sections WHERE title='Inbox'").fetchone()
    if row:
        sid = row["id"]
    else:
        sid = con.execute("INSERT INTO sections(title, pos) VALUES('Inbox', -1)").lastrowid
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    con.execute(
        "INSERT INTO items(section_id, title, status, pos, updated_at) VALUES(?,?,?,?,?)",
        (sid, title, "open", pos, datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return redirect(url_for("board"))


@app.route("/items/<int:item_id>/files", methods=["POST"])
@login_required
def upload_file(item_id):
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no file"), 400
    ext = os.path.splitext(f.filename)[1][:12]
    stored = uuid.uuid4().hex + ext
    f.save(os.path.join(FILES_DIR, stored))
    size = os.path.getsize(os.path.join(FILES_DIR, stored))
    con = db()
    cur = con.execute(
        "INSERT INTO item_files(item_id, filename, stored_name, size, created_at) VALUES(?,?,?,?,?)",
        (item_id, f.filename, stored, size,
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return jsonify(id=cur.lastrowid, filename=f.filename, size=size)


@app.route("/files/<int:file_id>")
@login_required
def get_file(file_id):
    row = db().execute("SELECT * FROM item_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        abort(404)
    return send_from_directory(FILES_DIR, row["stored_name"],
                               download_name=row["filename"], as_attachment=False)


@app.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
def delete_file(file_id):
    con = db()
    row = con.execute("SELECT * FROM item_files WHERE id=?", (file_id,)).fetchone()
    if row:
        try:
            os.remove(os.path.join(FILES_DIR, row["stored_name"]))
        except OSError:
            pass
        con.execute("DELETE FROM item_files WHERE id=?", (file_id,))
        con.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("board"))


# ---------- section actions ----------

@app.route("/projects/add", methods=["POST"])
@login_required
def add_project():
    sid = request.form.get("section_id", type=int)
    title = (request.form.get("title") or "").strip()
    if sid and title:
        con = db()
        pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM projects WHERE section_id=?",
                          (sid,)).fetchone()[0]
        con.execute("INSERT INTO projects(section_id, title, pos, created_at) VALUES(?,?,?,?)",
                    (sid, title, pos, datetime.now().isoformat(timespec="seconds")))
        con.commit()
    return redirect(url_for("board", _anchor="sec-%d" % (sid or 0)))


@app.route("/projects/<int:proj_id>/delete", methods=["POST"])
@login_required
def delete_project(proj_id):
    con = db()
    con.execute("UPDATE items SET project_id=NULL WHERE project_id=?", (proj_id,))
    con.execute("DELETE FROM projects WHERE id=?", (proj_id,))
    con.commit()
    return redirect(url_for("board"))


# ---------- people (chase view) ----------

@app.route("/people")
@login_required
def people_view():
    con = db()
    rows = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.status != 'done' AND COALESCE(items.waiting_on,'') != ''"
        " ORDER BY items.due_date IS NULL, items.due_date, items.id").fetchall()
    groups = {}
    for r in rows:
        key = r["waiting_on"].strip()
        groups.setdefault(key, []).append(r)
    people = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    return render_template("people.html", people=people,
                           today_iso=datetime.now().date().isoformat())


# ---------- joel meeting mode ----------

@app.route("/joel")
@login_required
def joel_view():
    """The Joel section as a live board (same controls as everywhere) + a print sheet."""
    from datetime import timedelta
    con = db()
    sections = con.execute("SELECT * FROM sections ORDER BY pos, id").fetchall()
    sec = con.execute("SELECT * FROM sections WHERE title LIKE '%Joel%' ORDER BY id LIMIT 1").fetchone()
    projects = con.execute("SELECT * FROM projects ORDER BY pos, id").fetchall()
    projects_by_sec = {}
    for p in projects:
        projects_by_sec.setdefault(p["section_id"], []).append(p)
    items, notes_by_item, files_by_item = [], {}, {}
    if sec:
        items = con.execute(
            "SELECT * FROM items WHERE section_id=? ORDER BY pos, id", (sec["id"],)).fetchall()
        ids = [str(it["id"]) for it in items]
        if ids:
            joined = ",".join(ids)
            for n in con.execute("SELECT * FROM item_notes WHERE item_id IN (%s) ORDER BY id" % joined):
                notes_by_item.setdefault(n["item_id"], []).append(n)
            for f in con.execute("SELECT * FROM item_files WHERE item_id IN (%s) ORDER BY id" % joined):
                files_by_item.setdefault(f["item_id"], []).append(f)
    return render_template("joel.html", sec=sec, items=items, sections=sections,
                           projects_by_sec=projects_by_sec,
                           notes_by_item=notes_by_item, files_by_item=files_by_item,
                           today_iso=datetime.now().date().isoformat(),
                           soon_iso=(datetime.now().date() + timedelta(days=3)).isoformat(),
                           today=datetime.now().strftime("%B %d, %Y"))


# ---------- calendar ----------

@app.route("/calendar")
@login_required
def calendar_view():
    from datetime import date, timedelta
    m = request.args.get("m", "")
    today = date.today()
    try:
        y, mo = int(m[:4]), int(m[5:7])
    except (ValueError, IndexError):
        y, mo = today.year, today.month
    first = date(y, mo, 1)
    start = first - timedelta(days=(first.weekday() + 1) % 7)  # back to Sunday
    days = [start + timedelta(days=i) for i in range(42)]
    con = db()
    rows = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE due_date IS NOT NULL AND due_date != ''"
        " ORDER BY due_date").fetchall()
    by_day, overdue, upcoming = {}, [], []
    t_iso = today.isoformat()
    horizon = (today + timedelta(days=14)).isoformat()
    for r in rows:
        by_day.setdefault(r["due_date"], []).append(r)
        if r["status"] != "done":
            if r["due_date"] < t_iso:
                overdue.append(r)
            elif r["due_date"] <= horizon:
                upcoming.append(r)
    evs = con.execute("SELECT * FROM events ORDER BY day, COALESCE(start_time,'')").fetchall()
    ev_by_day = {}
    for e in evs:
        ev_by_day.setdefault(e["day"], []).append(e)
    horizon14 = (today + timedelta(days=14)).isoformat()
    upcoming_events = [e for e in evs if t_iso <= e["day"] <= horizon14]
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = date(y + (1 if mo == 12 else 0), 1 if mo == 12 else mo + 1, 1).strftime("%Y-%m")
    return render_template("calendar.html", days=days, by_day=by_day,
                           ev_by_day=ev_by_day, upcoming_events=upcoming_events,
                           overdue=overdue, upcoming=upcoming,
                           month_label=first.strftime("%B %Y"),
                           prev_m=prev_m, next_m=next_m,
                           today_iso=t_iso, cur_month=mo)


# ---------- day view: open and edit a single day ----------

@app.route("/day/<day>")
@login_required
def day_view(day):
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return redirect(url_for("calendar_view"))
    con = db()
    evs = con.execute(
        "SELECT * FROM events WHERE day=? ORDER BY COALESCE(start_time,'99:99'), id",
        (day,)).fetchall()
    tasks = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.due_date = ? ORDER BY items.status='done', items.id", (day,)).fetchall()
    sections = con.execute("SELECT * FROM sections ORDER BY pos, id").fetchall()
    return render_template("day.html", d=d, day=day, evs=evs, tasks=tasks,
                           sections=sections,
                           pretty=d.strftime("%A, %B %d, %Y"),
                           prev_day=(d - timedelta(days=1)).isoformat(),
                           next_day=(d + timedelta(days=1)).isoformat(),
                           today_iso=datetime.now().date().isoformat())


@app.route("/events/add", methods=["POST"])
@login_required
def add_event():
    day = (request.form.get("day") or "").strip()
    subj = (request.form.get("subject") or "").strip()
    if not (day and subj):
        return redirect(url_for("calendar_view"))
    con = db()
    con.execute(
        "INSERT INTO events(ext_key, subject, day, start_time, location, note, source, synced_at)"
        " VALUES(?,?,?,?,?,?,'manual',?)",
        ("m-" + uuid.uuid4().hex[:12], subj, day,
         (request.form.get("start_time") or "").strip() or None,
         (request.form.get("location") or "").strip(),
         (request.form.get("note") or "").strip(),
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return redirect(url_for("day_view", day=day))


@app.route("/events/<int:ev_id>/edit", methods=["POST"])
@login_required
def edit_event(ev_id):
    subj = (request.form.get("subject") or "").strip()
    day = (request.form.get("day") or "").strip()
    if not (subj and day):
        return redirect(url_for("calendar_view"))
    con = db()
    # editing an Outlook event pins it: the daily sync stops overwriting your version
    con.execute(
        "UPDATE events SET subject=?, day=?, start_time=?, location=?, note=?, source='manual'"
        " WHERE id=?",
        (subj, day, (request.form.get("start_time") or "").strip() or None,
         (request.form.get("location") or "").strip(),
         (request.form.get("note") or "").strip(), ev_id))
    con.commit()
    return redirect(url_for("day_view", day=day))


@app.route("/events/<int:ev_id>/delete", methods=["POST"])
@login_required
def delete_event(ev_id):
    con = db()
    row = con.execute("SELECT * FROM events WHERE id=?", (ev_id,)).fetchone()
    if not row:
        return redirect(url_for("calendar_view"))
    if (row["source"] or "outlook") == "outlook" and row["ext_key"]:
        # remember it so tomorrow's sync does not bring it back
        con.execute("INSERT OR IGNORE INTO hidden_events(ext_key) VALUES(?)", (row["ext_key"],))
    con.execute("DELETE FROM events WHERE id=?", (ev_id,))
    con.commit()
    return redirect(url_for("day_view", day=row["day"]))


# ---------- outbound: publish HQ to Outlook (ICS) ----------

def _feed_token(con):
    tok = _setting_ro(con, "feed_token")
    if not tok:
        tok = uuid.uuid4().hex
        con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('feed_token', ?)", (tok,))
        con.commit()
    return tok


def _setting_ro(con, key, default=None):
    row = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def _ics_escape(t):
    return (str(t or "").replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line):
    """ICS lines must be <=75 octets."""
    out, cur = [], line
    while len(cur.encode("utf-8")) > 73:
        cut = 73
        while len(cur[:cut].encode("utf-8")) > 73:
            cut -= 1
        out.append(cur[:cut])
        cur = " " + cur[cut:]
    out.append(cur)
    return "\r\n".join(out)


def _to_utc_stamp(day, hhmm):
    """Local wall-clock -> UTC stamp, so Outlook never mis-shifts the time."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        local = _dt.strptime("%s %s" % (day, hhmm), "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo(TZ_NAME))
        from datetime import timezone
        return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return _dt.strptime("%s %s" % (day, hhmm), "%Y-%m-%d %H:%M").strftime("%Y%m%dT%H%M%SZ")


def build_ics(con):
    from datetime import timedelta as _td
    now = datetime.now()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    horizon = (now.date() - _td(days=60)).isoformat()
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Shimon HQ//EN",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         "X-WR-CALNAME:Shimon HQ", "X-WR-TIMEZONE:" + TZ_NAME,
         "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]

    # your own events (Outlook ones are skipped - they already live in Outlook)
    for e in con.execute("SELECT * FROM events WHERE COALESCE(source,'outlook')='manual'"
                         " AND day >= ?", (horizon,)):
        L += ["BEGIN:VEVENT", "UID:hq-ev-%s@shimonhq" % e["ext_key"], "DTSTAMP:" + stamp]
        if e["start_time"]:
            start = _to_utc_stamp(e["day"], e["start_time"])
            end_h = (int(e["start_time"][:2]) + 1) % 24
            end = _to_utc_stamp(e["day"], "%02d:%s" % (end_h, e["start_time"][3:5]))
            L += ["DTSTART:" + start, "DTEND:" + end]
        else:
            d = e["day"].replace("-", "")
            L += ["DTSTART;VALUE=DATE:" + d, "DTEND;VALUE=DATE:" + d]
        L.append(_fold("SUMMARY:" + _ics_escape(e["subject"])))
        if e["location"]:
            L.append(_fold("LOCATION:" + _ics_escape(e["location"])))
        if e["note"]:
            L.append(_fold("DESCRIPTION:" + _ics_escape(e["note"])))
        L.append("END:VEVENT")

    # task deadlines as all-day entries
    for it in con.execute(
            "SELECT items.*, sections.title AS sec FROM items"
            " JOIN sections ON items.section_id = sections.id"
            " WHERE COALESCE(items.due_date,'') != '' AND items.due_date >= ?"
            " AND items.status != 'done'", (horizon,)):
        d = it["due_date"].replace("-", "")
        desc = it["note"] or ""
        if it["waiting_on"]:
            desc = (desc + "  |  waiting on " + it["waiting_on"]).strip()
        L += ["BEGIN:VEVENT", "UID:hq-task-%d@shimonhq" % it["id"], "DTSTAMP:" + stamp,
              "DTSTART;VALUE=DATE:" + d, "DTEND;VALUE=DATE:" + d,
              _fold("SUMMARY:DUE: " + _ics_escape(it["title"])),
              _fold("DESCRIPTION:" + _ics_escape(desc + "  |  " + it["sec"])),
              "TRANSP:TRANSPARENT", "END:VEVENT"]

    L.append("END:VCALENDAR")
    return "\r\n".join(L) + "\r\n"


@app.route("/feed/<token>.ics")
def ics_feed(token):
    """Subscribe to this URL in Outlook - no login, so the token is the key."""
    con = db()
    if not token or token != _setting_ro(con, "feed_token"):
        abort(404)
    return build_ics(con), 200, {"Content-Type": "text/calendar; charset=utf-8"}


@app.route("/feed")
@login_required
def feed_info():
    con = db()
    tok = _feed_token(con)
    url = request.url_root.rstrip("/") + url_for("ics_feed", token=tok)
    return render_template("feed.html", feed_url=url)


@app.route("/events/<int:ev_id>.ics")
@login_required
def event_ics(ev_id):
    """One event as a file - open it and Outlook adds it to your real calendar."""
    con = db()
    e = con.execute("SELECT * FROM events WHERE id=?", (ev_id,)).fetchone()
    if not e:
        abort(404)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Shimon HQ//EN", "METHOD:PUBLISH",
         "BEGIN:VEVENT", "UID:hq-ev-%s@shimonhq" % e["ext_key"], "DTSTAMP:" + stamp]
    if e["start_time"]:
        end_h = (int(e["start_time"][:2]) + 1) % 24
        L += ["DTSTART:" + _to_utc_stamp(e["day"], e["start_time"]),
              "DTEND:" + _to_utc_stamp(e["day"], "%02d:%s" % (end_h, e["start_time"][3:5]))]
    else:
        d = e["day"].replace("-", "")
        L += ["DTSTART;VALUE=DATE:" + d, "DTEND;VALUE=DATE:" + d]
    L.append(_fold("SUMMARY:" + _ics_escape(e["subject"])))
    if e["location"]:
        L.append(_fold("LOCATION:" + _ics_escape(e["location"])))
    if e["note"]:
        L.append(_fold("DESCRIPTION:" + _ics_escape(e["note"])))
    L += ["END:VEVENT", "END:VCALENDAR"]
    body = "\r\n".join(L) + "\r\n"
    safe = re.sub(r"[^A-Za-z0-9]+", "_", e["subject"])[:40] or "event"
    return body, 200, {"Content-Type": "text/calendar; charset=utf-8",
                       "Content-Disposition": 'attachment; filename="%s.ics"' % safe}


# ---------- api (for CRM integration) ----------

def _api_auth():
    tok = os.environ.get("API_TOKEN")
    if not tok:
        return False
    auth = request.headers.get("Authorization", "")
    supplied = auth[7:] if auth.startswith("Bearer ") else request.args.get("token", "")
    return supplied == tok


@app.route("/api/titles")
def api_titles():
    """Plain-text task list for the morning sweep: STATUS <tab> TITLE per line."""
    if not _api_auth():
        abort(401)
    rows = db().execute("SELECT status, title FROM items ORDER BY id").fetchall()
    body = "\n".join("%s\t%s" % (r["status"], r["title"]) for r in rows)
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/rm")
def api_rm():
    """Delete a task by exact title (cleanup helper for the sweep)."""
    if not _api_auth():
        abort(401)
    title = (request.args.get("title") or "").strip()
    if not title:
        return "ERROR: title required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    cur = con.execute("DELETE FROM items WHERE lower(title)=lower(?)", (title,))
    con.execute("DELETE FROM sections WHERE id NOT IN (SELECT DISTINCT section_id FROM items)"
                " AND title NOT IN ('Joel / Shimon Tracker','Pinta / Office','Shul + Tzedaka',"
                "'Personal / Family','Inbox')")
    con.commit()
    return "REMOVED %d: %s" % (cur.rowcount, title), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/ev")
def api_event():
    """Upsert one Outlook calendar event (short params, proxy friendly)."""
    if not _api_auth():
        abort(401)
    key = (request.args.get("k") or "").strip()
    subj = (request.args.get("s") or "").strip()
    day = (request.args.get("d") or "").strip()
    if not (key and subj and day):
        return "ERROR: k, s and d required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    if con.execute("SELECT 1 FROM hidden_events WHERE ext_key=?", (key,)).fetchone():
        return "SKIPPED (deleted here): " + subj, 200, {"Content-Type": "text/plain; charset=utf-8"}
    if con.execute("SELECT 1 FROM events WHERE ext_key=? AND source='manual'", (key,)).fetchone():
        return "SKIPPED (edited here): " + subj, 200, {"Content-Type": "text/plain; charset=utf-8"}
    con.execute(
        "INSERT INTO events(ext_key, subject, day, start_time, location, synced_at)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(ext_key) DO UPDATE SET subject=excluded.subject, day=excluded.day,"
        " start_time=excluded.start_time, location=excluded.location, synced_at=excluded.synced_at",
        (key, subj, day, (request.args.get("t") or "").strip() or None,
         (request.args.get("l") or "").strip(),
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return "SYNCED: " + subj, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/evclear")
def api_events_clear():
    """Drop synced events from a date forward, so a re-sync never duplicates or keeps cancellations."""
    if not _api_auth():
        abort(401)
    frm = (request.args.get("from") or datetime.now().date().isoformat()).strip()
    con = db()
    cur = con.execute("DELETE FROM events WHERE day >= ? AND COALESCE(source,'outlook')='outlook'",
                      (frm,))
    con.commit()
    return "CLEARED %d from %s" % (cur.rowcount, frm), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/retag")
def api_retag():
    """Merge one 'waiting on' name into another (same person, two spellings)."""
    if not _api_auth():
        abort(401)
    frm = (request.args.get("from") or "").strip()
    to = (request.args.get("to") or "").strip()
    if not (frm and to):
        return "ERROR: from and to required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    cur = con.execute("UPDATE items SET waiting_on=? WHERE lower(trim(waiting_on))=lower(?)",
                      (to, frm))
    con.commit()
    return "RETAGGED %d: %s -> %s" % (cur.rowcount, frm, to), 200, \
        {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/set")
def api_set():
    """Update one task by its current title (t), setting any of: title, note, waiting_on."""
    if not _api_auth():
        abort(401)
    find = (request.args.get("find") or request.args.get("t") or "").strip()
    if not find:
        return "ERROR: t required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    row = con.execute("SELECT * FROM items WHERE lower(title)=lower(?)", (find,)).fetchone()
    if not row:
        return "NOT FOUND: " + find, 200, {"Content-Type": "text/plain; charset=utf-8"}
    title = request.args.get("title")
    note = request.args.get("note")
    wait = request.args.get("waiting_on")
    con.execute("UPDATE items SET title=?, note=?, waiting_on=?, updated_at=? WHERE id=?",
                (title if title is not None else row["title"],
                 note if note is not None else row["note"],
                 wait if wait is not None else row["waiting_on"],
                 datetime.now().isoformat(timespec="seconds"), row["id"]))
    con.commit()
    return "UPDATED: " + (title or row["title"]), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/digest")
def api_digest():
    """Plain-text overdue / due-today digest for the morning sweep."""
    if not _api_auth():
        abort(401)
    today = datetime.now().date().isoformat()
    rows = db().execute(
        "SELECT items.*, sections.title AS sec FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.status != 'done' AND COALESCE(items.due_date,'') != ''"
        " ORDER BY items.due_date").fetchall()
    over = ["%s | %s%s" % (r["due_date"], r["title"],
                           (" (waiting on %s)" % r["waiting_on"]) if r["waiting_on"] else "")
            for r in rows if r["due_date"] < today]
    due = ["%s%s" % (r["title"],
                     (" (waiting on %s)" % r["waiting_on"]) if r["waiting_on"] else "")
           for r in rows if r["due_date"] == today]
    body = "OVERDUE (%d):\n%s\n\nDUE TODAY (%d):\n%s" % (
        len(over), "\n".join(over) or "-", len(due), "\n".join(due) or "-")
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/quickadd")
def api_quickadd():
    """GET-based add for the morning sweep (params: token, title, note, waiting_on, section, due)."""
    if not _api_auth():
        abort(401)
    title = (request.args.get("title") or "").strip()
    if not title:
        return "ERROR: title required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    sec_title = request.args.get("section", "Inbox")
    # exact match first, then a forgiving prefix/substring match ("Joel" -> "Joel / Shimon Tracker")
    sec = con.execute("SELECT id FROM sections WHERE title=?", (sec_title,)).fetchone()
    if not sec and sec_title:
        sec = con.execute("SELECT id FROM sections WHERE title LIKE ? ORDER BY id LIMIT 1",
                          ("%" + sec_title + "%",)).fetchone()
    sid = sec["id"] if sec else con.execute(
        "INSERT INTO sections(title, pos) VALUES(?, 99)", (sec_title,)).lastrowid
    dup = con.execute("SELECT 1 FROM items WHERE lower(title)=lower(?)", (title,)).fetchone()
    if dup:
        return "SKIPPED (duplicate): " + title, 200, {"Content-Type": "text/plain; charset=utf-8"}
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    con.execute(
        "INSERT INTO items(section_id, title, note, waiting_on, status, pos, due_date, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (sid, title, request.args.get("note", ""), request.args.get("waiting_on", ""),
         "open", pos, request.args.get("due") or None,
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return "ADDED: " + title, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/board")
def api_board():
    if not _api_auth():
        abort(401)
    con = db()
    out = []
    for s in con.execute("SELECT * FROM sections ORDER BY pos, id"):
        sec = {"id": s["id"], "title": s["title"], "projects": [], "tasks": []}
        for p in con.execute("SELECT * FROM projects WHERE section_id=? ORDER BY pos, id",
                             (s["id"],)):
            proj = {"id": p["id"], "title": p["title"], "tasks": []}
            for it in con.execute("SELECT * FROM items WHERE project_id=? ORDER BY pos, id",
                                  (p["id"],)):
                proj["tasks"].append(dict(it))
            sec["projects"].append(proj)
        for it in con.execute(
                "SELECT * FROM items WHERE section_id=? AND project_id IS NULL ORDER BY pos, id",
                (s["id"],)):
            sec["tasks"].append(dict(it))
        out.append(sec)
    return jsonify(sections=out)


@app.route("/api/items", methods=["POST"])
def api_add_item():
    if not _api_auth():
        abort(401)
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    if not title:
        return jsonify(error="title required"), 400
    con = db()
    sec = con.execute("SELECT id FROM sections WHERE title=?",
                      (d.get("section", "Inbox"),)).fetchone()
    sid = sec["id"] if sec else con.execute(
        "INSERT INTO sections(title, pos) VALUES(?, 99)",
        (d.get("section", "Inbox"),)).lastrowid
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    cur = con.execute(
        "INSERT INTO items(section_id, title, note, waiting_on, status, pos, due_date, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (sid, title, d.get("note", ""), d.get("waiting_on", ""), "open", pos,
         d.get("due_date"), datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return jsonify(id=cur.lastrowid), 201


@app.route("/sections/add", methods=["POST"])
@login_required
def add_section():
    title = (request.form.get("title") or "").strip()
    if title:
        con = db()
        pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM sections").fetchone()[0]
        con.execute("INSERT INTO sections(title, pos) VALUES(?,?)", (title, pos))
        con.commit()
    return redirect(url_for("board"))


@app.route("/sections/<int:sec_id>/delete", methods=["POST"])
@login_required
def delete_section(sec_id):
    con = db()
    n = con.execute("SELECT COUNT(*) FROM items WHERE section_id=? AND status!='done'",
                    (sec_id,)).fetchone()[0]
    if n == 0:
        con.execute("DELETE FROM sections WHERE id=?", (sec_id,))
        con.commit()
    return redirect(url_for("board"))


# ---------- pwa ----------

@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(os.path.join(BASE, "static"), "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.route("/sw.js")
def sw():
    return send_from_directory(os.path.join(BASE, "static"), "sw.js",
                               mimetype="application/javascript")


# ---------- push notifications ----------

import base64
import threading
import time as _time

TZ_NAME = os.environ.get("TZ_NAME", "America/New_York")
VAPID_PEM = os.path.join(os.path.dirname(DB_PATH) or BASE, "vapid_private.pem")


def _now_local():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return datetime.now()


def _setting(con, key, default=None):
    row = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def ensure_vapid():
    """Create the push signing keys once; they live on the persistent disk."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    pub = _setting(con, "vapid_public")
    if pub and os.path.exists(VAPID_PEM):
        con.close()
        return pub
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        key = ec.generate_private_key(ec.SECP256R1())
        with open(VAPID_PEM, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
        raw = key.public_key().public_bytes(serialization.Encoding.X962,
                                            serialization.PublicFormat.UncompressedPoint)
        pub = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('vapid_public', ?)", (pub,))
        con.commit()
    except Exception as e:
        app.logger.warning("VAPID setup failed: %s", e)
        pub = None
    con.close()
    return pub


@app.route("/push/key")
@login_required
def push_key():
    con = db()
    return jsonify(key=_setting(con, "vapid_public"))


@app.route("/push/subscribe", methods=["POST"])
@login_required
def push_subscribe():
    d = request.get_json(silent=True) or {}
    keys = d.get("keys") or {}
    if not d.get("endpoint") or not keys.get("p256dh"):
        return jsonify(error="bad subscription"), 400
    con = db()
    con.execute("INSERT OR REPLACE INTO push_subs(endpoint, p256dh, auth, created_at)"
                " VALUES(?,?,?,?)",
                (d["endpoint"], keys["p256dh"], keys.get("auth", ""),
                 datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return jsonify(ok=True)


@app.route("/push/test", methods=["POST"])
@login_required
def push_test():
    n = send_push("Shimon HQ", "Notifications are on. This is what a reminder looks like.")
    return jsonify(sent=n)


def send_push(title, body, url="/"):
    """Fire one notification to every subscribed device. Returns how many got it."""
    try:
        import webpush_lite
    except Exception as e:
        app.logger.warning("push module missing: %s", e)
        return 0
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    subs = con.execute("SELECT * FROM push_subs").fetchall()
    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for sub in subs:
        try:
            status = webpush_lite.send(
                {"endpoint": sub["endpoint"],
                 "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                payload, VAPID_PEM, "mailto:sdeutsch@pintapartners.com")
            if status in (404, 410):           # device unsubscribed
                con.execute("DELETE FROM push_subs WHERE id=?", (sub["id"],))
                con.commit()
            elif 200 <= status < 300:
                sent += 1
            else:
                app.logger.warning("push endpoint returned %s", status)
        except Exception as e:
            app.logger.warning("push failed: %s", e)
    con.close()
    return sent


def _already_sent(con, ref):
    return con.execute("SELECT 1 FROM reminders_sent WHERE ref=?", (ref,)).fetchone() is not None


def _mark_sent(con, ref):
    con.execute("INSERT OR REPLACE INTO reminders_sent(ref, sent_at) VALUES(?,?)",
                (ref, datetime.now().isoformat(timespec="seconds")))
    con.commit()


def reminder_tick():
    """One pass: task reminders due, meetings starting soon, the morning digest."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    now = _now_local()
    now_s = now.strftime("%Y-%m-%dT%H:%M")
    today = now.strftime("%Y-%m-%d")

    # 1. per-task reminders that have come due (within the last 2 hours, so a restart never misses one)
    from datetime import timedelta as _td
    back = (now - _td(hours=2)).strftime("%Y-%m-%dT%H:%M")
    for it in con.execute(
            "SELECT * FROM items WHERE remind_at IS NOT NULL AND remind_at != ''"
            " AND remind_at <= ? AND remind_at >= ? AND status != 'done'", (now_s, back)):
        ref = "item:%d:%s" % (it["id"], it["remind_at"])
        if _already_sent(con, ref):
            continue
        body = it["title"]
        if it["waiting_on"]:
            body += "  (waiting on %s)" % it["waiting_on"]
        send_push("Reminder", body, "/#item-%d" % it["id"])
        _mark_sent(con, ref)

    # 2. meetings starting in the next 15 minutes
    soon = (now + _td(minutes=15)).strftime("%H:%M")
    for e in con.execute(
            "SELECT * FROM events WHERE day=? AND start_time IS NOT NULL"
            " AND start_time > ? AND start_time <= ?",
            (today, now.strftime("%H:%M"), soon)):
        ref = "ev:%s:%s" % (e["ext_key"], e["day"])
        if _already_sent(con, ref):
            continue
        body = "%s starts %s" % (e["subject"], e["start_time"])
        if e["location"]:
            body += "  ·  " + e["location"]
        send_push("Coming up", body, "/calendar")
        _mark_sent(con, ref)

    # 3. one morning digest at 8am on weekdays
    if now.weekday() < 5 and now.strftime("%H:%M") >= "08:00" and now.strftime("%H:%M") < "09:00":
        ref = "digest:" + today
        if not _already_sent(con, ref):
            due = con.execute(
                "SELECT COUNT(*) c FROM items WHERE status != 'done' AND due_date = ?",
                (today,)).fetchone()["c"]
            over = con.execute(
                "SELECT COUNT(*) c FROM items WHERE status != 'done'"
                " AND COALESCE(due_date,'') != '' AND due_date < ?", (today,)).fetchone()["c"]
            meetings = con.execute(
                "SELECT COUNT(*) c FROM events WHERE day = ?", (today,)).fetchone()["c"]
            bits = []
            if meetings:
                bits.append("%d meeting%s" % (meetings, "" if meetings == 1 else "s"))
            if due:
                bits.append("%d due today" % due)
            if over:
                bits.append("%d overdue" % over)
            if bits:
                send_push("Today", "  ·  ".join(bits), "/calendar")
            _mark_sent(con, ref)

    con.execute("DELETE FROM reminders_sent WHERE sent_at < ?",
                ((now - _td(days=14)).isoformat(timespec="seconds"),))
    con.commit()
    con.close()


def _reminder_loop():
    while True:
        try:
            reminder_tick()
        except Exception as e:
            app.logger.warning("reminder tick failed: %s", e)
        _time.sleep(60)


def start_reminders():
    t = threading.Thread(target=_reminder_loop, daemon=True, name="hq-reminders")
    t.start()


init_db()
ensure_vapid()
start_reminders()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
