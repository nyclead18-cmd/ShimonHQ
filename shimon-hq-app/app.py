import os
import re
import json
import uuid
import hmac
import sqlite3
from datetime import datetime, timezone, timedelta, date
from functools import wraps

from flask import (Flask, g, render_template, request, redirect,
                   url_for, session, jsonify, send_from_directory, abort)
from markupsafe import Markup, escape

import maps
import pulse
import wa

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
    for name in ("style.css", "board.js", "gestures.js", "geo.js", "icon-512.png"):
        try:
            v = max(v, int(os.path.getmtime(os.path.join(BASE, "static", name))))
        except OSError:
            pass
    return {"css_v": v}


@app.template_filter("fmt12")
def fmt12(hhmm):
    """24h stored -> 1:30 PM for display (all times are New York time)."""
    t = (hhmm or "").strip()
    if len(t) < 4 or ":" not in t:
        return t
    try:
        h = int(t[:2]); m = t[3:5]
    except ValueError:
        return t
    return "%d:%s %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


@app.template_filter("maplink")
def f_maplink(loc):
    return maps.link(loc)


@app.template_filter("mapdir")
def f_mapdir(loc):
    """Directions with no origin - Maps starts from where the phone actually is."""
    return maps.directions(loc)


@app.template_filter("mapembed")
def f_mapembed(loc):
    return maps.embed(loc)


@app.template_filter("isplace")
def f_isplace(loc):
    return maps.is_place(loc)


@app.template_filter("stamp")
def f_stamp(iso):
    """8/26 - 9:34 AM, in New York time, from whatever we stored."""
    t = (iso or "").strip()
    if len(t) < 16:
        return t[:10]
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return t[:10]
    if d.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo
            d = d.astimezone(ZoneInfo(TZ_NAME))
        except Exception:
            pass
    return "%d/%d \u00b7 %s" % (d.month, d.day, fmt12(d.strftime("%H:%M")))


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

@app.template_filter("weekspan")
def _weekspan(a, b):
    """Mon 18 Aug - Sun 24 Aug, or the short form when the month is shared."""
    try:
        d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    except ValueError:
        return "%s to %s" % (a, b)
    if d0.month == d1.month:
        return "%s %d\u2013%d %s" % (d0.strftime("%b"), d0.day, d1.day, d0.strftime("%Y"))
    return "%s \u2013 %s %s" % (d0.strftime("%b %-d"), d1.strftime("%b %-d"), d1.strftime("%Y"))


@app.template_filter("shortday")
def _shortday(iso):
    try:
        return date.fromisoformat(iso).strftime("%-m/%-d")
    except ValueError:
        return iso


@app.template_filter("weekday")
def _weekday(iso):
    try:
        return date.fromisoformat(iso).strftime("%a %-d")
    except ValueError:
        return iso


@app.template_filter("hrs")
def _hrs(mins):
    import pulse as _p
    return _p.hours(mins)


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


def commit_retry(con, tries=5):
    """SQLite locks briefly when the reminder thread writes. A dropped WhatsApp
    webhook is a lost message, so wait and try again rather than give up."""
    import time as _t
    for i in range(tries):
        try:
            con.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            _t.sleep(0.15 * (i + 1))
    try:
        con.commit()
        return True
    except sqlite3.OperationalError:
        app.logger.warning("commit still blocked after %d tries", tries)
        return False


@app.teardown_appcontext
def close_db(e=None):
    d = g.pop("db", None)
    if d is not None:
        d.close()


def _stamp_responses_in_new_york(con):
    """Responses used to be stamped in the server's UTC clock, which reads four
    hours wrong once you show the time as well as the date. Convert the old rows
    once; anything already carrying a UTC offset is left alone."""
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        ny = ZoneInfo(TZ_NAME)
    except Exception:
        return
    for table in ("item_notes", "item_files"):
        try:
            rows = con.execute(
                "SELECT id, created_at FROM %s"
                " WHERE created_at IS NOT NULL AND created_at != ''" % table).fetchall()
        except sqlite3.Error:
            continue
        for rid, raw in rows:
            try:
                d = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if d.tzinfo is not None:
                continue          # already carries an offset - never touch it twice
            con.execute("UPDATE %s SET created_at=? WHERE id=?" % table,
                        (d.replace(tzinfo=timezone.utc).astimezone(ny)
                         .isoformat(timespec="seconds"), rid))


def _seed_history(con):
    """Give the history table a floor to measure from.

    Nothing before today was ever recorded, so a task that has been waiting on
    someone since July would otherwise read as "0 days" - worse than saying
    nothing. For every task that is already waiting, write one snapshot event
    dated when the task was last touched, and mark it approximate so the page
    can say so rather than pretend it is exact.
    """
    try:
        already = con.execute(
            "SELECT COUNT(*) FROM item_events WHERE kind='snapshot'").fetchone()[0]
    except sqlite3.Error:
        return
    if already:
        return
    rows = con.execute(
        "SELECT id, title, status, waiting_on, updated_at FROM items").fetchall()
    if not rows:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for iid, title, status, waiting_on, updated in rows:
        at = now
        if updated:
            try:
                d = datetime.fromisoformat(updated)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                at = d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
        con.execute(
            "INSERT INTO item_events(item_id, at, kind, old, new, title)"
            " VALUES(?,?,'snapshot',?,?,?)",
            (iid, at, status or "", waiting_on or "", title))


def _pulse_notes_per_person(con):
    """A weekly read belongs to whoever it is about, so the week alone is no
    longer a unique key. SQLite cannot change a primary key in place."""
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(pulse_notes)")]
    except sqlite3.Error:
        return
    if not cols or "user_id" in cols:
        return
    con.executescript(
        "CREATE TABLE pulse_notes_new ("
        " week TEXT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,"
        " body TEXT NOT NULL, created_at TEXT, PRIMARY KEY(week, user_id));"
        "INSERT INTO pulse_notes_new(week, user_id, body, created_at)"
        " SELECT week, 0, body, created_at FROM pulse_notes;"
        "DROP TABLE pulse_notes;"
        "ALTER TABLE pulse_notes_new RENAME TO pulse_notes;")


INDEXES = (
    # Every one of these was a full table scan. Invisible at fifty tasks, not at
    # five hundred - and Render's disk is a network disk, where a scan costs far
    # more than it does on a laptop. They are created here rather than in
    # schema.sql because several of the columns are added by the migrations above.
    ("items_section",  "items(section_id)"),
    ("items_status",   "items(status)"),
    ("items_due",      "items(due_date)"),
    ("items_project",  "items(project_id)"),
    ("items_thread",   "items(thread_key)"),
    ("items_today",    "items(today)"),
    ("notes_item",     "item_notes(item_id)"),
    ("files_item",     "item_files(item_id)"),
    ("projects_sec",   "projects(section_id)"),
    ("sections_owner", "sections(owner_id, visibility)"),
    ("events_day",     "events(day, owner_id)"),
    ("events_owner",   "events(owner_id)"),
    ("brief_day",      "brief_items(owner_id, day)"),
    ("brief_unread",   "brief_items(owner_id, is_read)"),
    ("push_user",      "push_subs(user_id)"),
)


def _make_indexes(con):
    for name, target in INDEXES:
        try:
            con.execute("CREATE INDEX IF NOT EXISTS %s ON %s" % (name, target))
        except sqlite3.Error as e:
            app.logger.warning("index %s skipped: %s", name, e)


def _events_unique_per_person(con):
    """An Outlook event id is unique inside one mailbox, not across two.

    ext_key was globally unique, which was right while one person synced. With a
    second calendar arriving, a collision would not duplicate a meeting - the
    upsert would quietly move it from one person's board to the other's. Make the
    key unique per owner instead.
    """
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
    except sqlite3.Error:
        return
    if not cols or "owner_id" not in cols:
        return
    idx = con.execute("SELECT name FROM sqlite_master WHERE type='index'"
                      " AND tbl_name='events' AND name='events_key_owner'").fetchone()
    if idx:
        return
    con.executescript(
        "CREATE TABLE events_new ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, ext_key TEXT, subject TEXT NOT NULL,"
        " day TEXT NOT NULL, start_time TEXT, location TEXT DEFAULT '',"
        " source TEXT DEFAULT 'outlook', note TEXT DEFAULT '', dur_min INTEGER,"
        " owner_id INTEGER, synced_at TEXT);"
        "INSERT INTO events_new(id, ext_key, subject, day, start_time, location,"
        " source, note, dur_min, owner_id, synced_at)"
        " SELECT id, ext_key, subject, day, start_time, location,"
        " source, note, dur_min, owner_id, synced_at FROM events;"
        "DROP TABLE events;"
        "ALTER TABLE events_new RENAME TO events;"
        "CREATE UNIQUE INDEX events_key_owner ON events(ext_key, owner_id);")


def _shared_becomes_people(con):
    """'Shared' used to mean the whole team. From now on a section is shared with
    PEOPLE, and 'everyone' is one of the choices rather than the only one. What
    was already shared becomes explicitly shared with everyone who existed at the
    time - so nothing anyone could see disappears, and nobody new inherits it.
    """
    if con.execute("SELECT 1 FROM settings WHERE k='mig:shares'").fetchone():
        return
    users = [r[0] for r in con.execute("SELECT id FROM users")]
    for (sid, owner) in con.execute(
            "SELECT id, owner_id FROM sections WHERE visibility='shared'").fetchall():
        for uid in users:
            if uid != owner:
                con.execute("INSERT OR IGNORE INTO section_shares(section_id, user_id)"
                            " VALUES(?,?)", (sid, uid))
        con.execute("UPDATE sections SET visibility='some' WHERE id=?", (sid,))
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:shares','1')")


BOARDS = ["Personal/Family", "Pinta", "Community/Charity"]

_BOARD_ALIASES = {
    "personal": "Personal/Family", "family": "Personal/Family",
    "personal/family": "Personal/Family", "personal / family": "Personal/Family",
    "pinta": "Pinta",
    "community": "Community/Charity", "charity": "Community/Charity",
    "community/charity": "Community/Charity", "community/religious": "Community/Charity",
    "religious": "Community/Charity", "ohr chaim": "Community/Charity",
}


def canonical_board(raw):
    """Three boards, spelled the same way for everyone.

    Anything that plainly means one of them - 'personal', 'community',
    'charity', old names, API calls that cannot carry a slash - lands on the
    canonical spelling. A genuinely new name passes through untouched, so a
    board can still be invented when one is needed.
    """
    b = (raw or "").strip()[:30]
    return _BOARD_ALIASES.get(b.lower(), b)


def _three_boards(con):
    """One-time rename of every filed section onto the canonical board names."""
    if con.execute("SELECT 1 FROM settings WHERE k='mig:boards3'").fetchone():
        return
    for (sid, b) in con.execute(
            "SELECT id, board FROM sections WHERE board IS NOT NULL AND board!=''").fetchall():
        nb = canonical_board(b)
        if nb != b:
            con.execute("UPDATE sections SET board=? WHERE id=?", (nb, sid))
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:boards3','1')")


def _everything_into_buckets(con):
    """Three boxes per person - Personal/Family, Pinta, Community/Charity - and
    everything else becomes a project inside one of them.

    A section that was shared stays shared in the new shape: the project it
    becomes is tagged to the same people, so they keep seeing and working
    exactly what they saw before - on the List, item by item - and nobody
    gains sight of the rest of the bucket it moved into.
    """
    if con.execute("SELECT 1 FROM settings WHERE k='mig:buckets'").fetchone():
        return
    all_users = [r[0] for r in con.execute("SELECT id FROM users")]

    def _canon_bucket(title):
        t = " ".join((title or "").strip().lower().split())
        t = t.replace(" / ", "/").replace("/ ", "/").replace(" /", "/")
        if t in ("personal/family", "personal", "family"):
            return "Personal/Family"
        if t == "pinta":
            return "Pinta"
        if t in ("community/charity", "community", "charity"):
            return "Community/Charity"
        return None

    for uid in all_users:
        buckets = {}
        for (sid, title) in con.execute(
                "SELECT id, title FROM sections WHERE owner_id=?", (uid,)).fetchall():
            b = _canon_bucket(title)
            if b and b not in buckets:
                con.execute("UPDATE sections SET title=?, board=?, kind='tasks',"
                            " visibility='private', pos=? WHERE id=?",
                            (b, b, BUCKET_POS[b], sid))
                con.execute("DELETE FROM section_shares WHERE section_id=?", (sid,))
                buckets[b] = sid
        for b in BOARDS:
            if b not in buckets:
                buckets[b] = con.execute(
                    "INSERT INTO sections(title, pos, owner_id, visibility, board)"
                    " VALUES(?,?,?,'private',?)", (b, BUCKET_POS[b], uid, b)).lastrowid
        bucket_ids = set(buckets.values())
        for (sid, title, board, vis) in con.execute(
                "SELECT id, title, board, visibility FROM sections WHERE owner_id=?",
                (uid,)).fetchall():
            if sid in bucket_ids:
                continue
            dest = buckets.get(canonical_board(board) or "", buckets["Pinta"])
            shared_with = [r[0] for r in con.execute(
                "SELECT user_id FROM section_shares WHERE section_id=?", (sid,))]
            if vis == "shared":
                shared_with = [u for u in all_users if u != uid]
            ppos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM projects"
                               " WHERE section_id=?", (dest,)).fetchone()[0]
            # the section's own projects move over first, keeping their names
            for (opid,) in con.execute("SELECT id FROM projects WHERE section_id=?",
                                       (sid,)).fetchall():
                con.execute("UPDATE projects SET section_id=? WHERE id=?", (dest, opid))
                for u in shared_with:
                    con.execute("INSERT OR IGNORE INTO project_tags(project_id, user_id)"
                                " VALUES(?,?)", (opid, u))
            p = con.execute("INSERT INTO projects(section_id, title, pos) VALUES(?,?,?)",
                            (dest, title.strip(), ppos)).lastrowid
            for u in shared_with:
                con.execute("INSERT OR IGNORE INTO project_tags(project_id, user_id)"
                            " VALUES(?,?)", (p, u))
            con.execute("UPDATE items SET project_id=COALESCE(project_id, ?), section_id=?"
                        " WHERE section_id=?", (p, dest, sid))
            con.execute("DELETE FROM section_shares WHERE section_id=?", (sid,))
            con.execute("DELETE FROM sections WHERE id=?", (sid,))
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:buckets','1')")


def _joel_fresh_start(con):
    """Joel asked for a clean slate. Everything currently on his board is
    written to a snapshot file beside the database first - nothing is lost,
    it is just no longer in the way. UK Prospects and US carry over; the
    buckets, his login, his key, and the Joel-Shimon List are untouched
    (the List lives in other people's buckets, not on his board).
    """
    if con.execute("SELECT 1 FROM settings WHERE k='mig:joelfresh'").fetchone():
        return
    row = con.execute("SELECT id FROM users WHERE username='jlandau'").fetchone()
    if not row:
        con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:joelfresh','1')")
        return
    jid = row[0]
    secs = [r[0] for r in con.execute("SELECT id FROM sections WHERE owner_id=?", (jid,))]
    if secs:
        q = ",".join("?" * len(secs))
        snap = {"taken": datetime.now().isoformat(timespec="seconds"),
                "sections": [], "projects": [], "items": [], "notes": []}
        for t, sql in (("sections", "SELECT * FROM sections WHERE id IN (%s)" % q),
                       ("projects", "SELECT * FROM projects WHERE section_id IN (%s)" % q),
                       ("items", "SELECT * FROM items WHERE section_id IN (%s)" % q)):
            cur = con.execute(sql, secs)
            cols = [c[0] for c in cur.description]
            snap[t] = [dict(zip(cols, r)) for r in cur.fetchall()]
        iids = [it["id"] for it in snap["items"]]
        if iids:
            qi = ",".join("?" * len(iids))
            cur = con.execute("SELECT * FROM item_notes WHERE item_id IN (%s)" % qi, iids)
            cols = [c[0] for c in cur.description]
            snap["notes"] = [dict(zip(cols, r)) for r in cur.fetchall()]
        try:
            path = os.path.join(os.path.dirname(DB_PATH) or BASE,
                                "joel-board-before-reset.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=1, default=str)
        except OSError:
            pass    # a failed snapshot must not block startup; data below is only trimmed
        keep_projects = [r[0] for r in con.execute(
            "SELECT id FROM projects WHERE section_id IN (%s)" % q
            + " AND lower(title) IN ('uk prospects','us')", secs)]
        kp = ",".join("?" * len(keep_projects)) if keep_projects else "NULL"
        doomed_items = [r[0] for r in con.execute(
            "SELECT id FROM items WHERE section_id IN (%s)" % q
            + " AND (project_id IS NULL OR project_id NOT IN (%s))" % kp,
            secs + keep_projects)]
        if doomed_items:
            qd = ",".join("?" * len(doomed_items))
            con.execute("DELETE FROM item_notes WHERE item_id IN (%s)" % qd, doomed_items)
            con.execute("DELETE FROM item_files WHERE item_id IN (%s)" % qd, doomed_items)
            con.execute("DELETE FROM list_tags WHERE item_id IN (%s)" % qd, doomed_items)
            con.execute("DELETE FROM items WHERE id IN (%s)" % qd, doomed_items)
        doomed_projects = [r[0] for r in con.execute(
            "SELECT id FROM projects WHERE section_id IN (%s)" % q
            + " AND id NOT IN (%s)" % kp, secs + keep_projects)]
        if doomed_projects:
            qp = ",".join("?" * len(doomed_projects))
            con.execute("DELETE FROM project_tags WHERE project_id IN (%s)" % qp,
                        doomed_projects)
            con.execute("DELETE FROM projects WHERE id IN (%s)" % qp, doomed_projects)
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:joelfresh','1')")


def _keep_seed(con):
    """One-time import of a Google Keep takeout, staged as keep_seed.json beside
    the app. Each note became a project, its checkboxes became tasks, checked
    means done, and archived notes go straight to the Archive. The file only
    exists on the instance it is meant for; everywhere else this is a no-op."""
    path = os.path.join(BASE, "keep_seed.json")
    if not os.path.exists(path):
        return
    if con.execute("SELECT 1 FROM settings WHERE k='mig:keepseed'").fetchone():
        return
    row = con.execute("SELECT id FROM users WHERE username=?", (HQ_USER.lower(),)).fetchone()
    if not row:
        return    # the account is not born yet; no marker, so next boot retries
    uid = row[0]
    buckets = ensure_buckets(con, uid)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    now = datetime.now().isoformat(timespec="seconds")
    for bname, projects in data.items():
        sec = buckets.get(bname, buckets["Pinta"])
        for p in projects:
            ppos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM projects"
                               " WHERE section_id=?", (sec,)).fetchone()[0]
            pid = con.execute(
                "INSERT INTO projects(section_id, title, pos, archived)"
                " VALUES(?,?,?,?)",
                (sec, p["title"][:80], ppos, 1 if p.get("archived") else 0)).lastrowid
            ipos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items"
                               " WHERE section_id=?", (sec,)).fetchone()[0]
            for i, t in enumerate(p.get("tasks", [])):
                con.execute(
                    "INSERT INTO items(section_id, project_id, title, note, status, pos,"
                    " today, today_at, archived, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (sec, pid, t["t"], t.get("n", ""),
                     "done" if t.get("done") else "open", ipos + i,
                     1 if t.get("today") else 0, now if t.get("today") else None,
                     1 if p.get("archived") else 0, now, now))
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:keepseed','1')")


def _bring_back_archive(con):
    """The Keep import tucked whole projects into the Archive, and a stray tap
    on the put-away arrow is easy to make - so once, everything archived comes
    back to the board. The Archive stays; anything put away after this stays
    put away until its own Bring back tap. Runs only where a Keep import ran."""
    if not os.path.exists(os.path.join(BASE, "keep_seed.json")):
        return
    if not con.execute("SELECT 1 FROM settings WHERE k='mig:keepseed'").fetchone():
        return
    if con.execute("SELECT 1 FROM settings WHERE k='mig:bringback1'").fetchone():
        return
    con.execute("UPDATE projects SET archived=0 WHERE archived=1")
    con.execute("UPDATE items SET archived=0 WHERE archived=1")
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:bringback1','1')")


def _pipeline_becomes_plain(con):
    """The pipeline confused the people it was built for, so it goes.

    Nothing anyone typed is lost: every deal figure a task carried is folded
    into its note as plain words ("£220m · EBITDA £30m") before the section
    becomes an ordinary list. The numbers also stay in their columns, unread,
    in case the idea ever earns its way back.
    """
    if con.execute("SELECT 1 FROM settings WHERE k='mig:nopipe'").fetchone():
        return
    for (sid, cur) in con.execute(
            "SELECT id, cur FROM sections WHERE kind='pipeline'").fetchall():
        cur = cur or "£"
        for (iid, note, amount, ebitda, units, tenure, stage) in con.execute(
                "SELECT id, note, amount, ebitda, units, tenure, stage FROM items"
                " WHERE section_id=?", (sid,)).fetchall():
            bits = []
            if amount:
                bits.append(_money(amount, cur))
            if ebitda:
                bits.append("EBITDA " + _money(ebitda, cur))
            if units:
                bits.append("%s units" % units)
            if tenure:
                bits.append(str(tenure))
            if stage:
                bits.append(str(stage))
            if not bits:
                continue
            extra = " · ".join(bits)
            note = (note or "").strip()
            if extra in note:
                continue
            note = (note + " · " + extra) if note else extra
            con.execute("UPDATE items SET note=? WHERE id=?", (note, iid))
        con.execute("UPDATE sections SET kind='tasks' WHERE id=?", (sid,))
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('mig:nopipe','1')")


def _make_people(con):
    """Turn the single hard-coded login into a real account, once.

    Everything that already exists becomes Shimon's and PRIVATE - not shared.
    Opening the board to a second person must never be the thing that exposes
    what was on it, so sharing is a decision he makes afterwards, one section at
    a time. The only exception is the tracker that carries his name and Joel's;
    that one was always joint work.
    """
    from werkzeug.security import generate_password_hash
    n = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if n:
        return
    now = datetime.now().isoformat(timespec="seconds")
    cur = con.execute(
        "INSERT INTO users(username, display_name, pw_hash, is_admin, created_at)"
        " VALUES(?,?,?,1,?)",
        (HQ_USER.lower(), os.environ.get("HQ_NAME", "Shimon"),
         generate_password_hash(HQ_PASSWORD), now))
    me = cur.lastrowid
    con.execute("UPDATE sections SET owner_id=? WHERE owner_id IS NULL", (me,))
    con.execute("UPDATE sections SET visibility='private'")
    con.execute("UPDATE events SET owner_id=? WHERE owner_id IS NULL", (me,))
    con.execute("UPDATE brief_items SET owner_id=? WHERE owner_id IS NULL", (me,))
    con.execute("UPDATE push_subs SET user_id=? WHERE user_id IS NULL", (me,))
    con.execute("UPDATE pulse_notes SET user_id=? WHERE user_id=0", (me,))
    # settings that describe a person rather than the board move under his key
    for k in ("origin_home", "origin_work", "origin_address", "last_pos", "last_pos_at",
              "feed_token"):
        row = con.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        if row:
            con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?,?)",
                        ("u%d:%s" % (me, k), row[0]))
    if HQ_USER.lower() == "shimon":
        con.execute("UPDATE sections SET visibility='shared' WHERE title LIKE '%Joel%'")
    con.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?,?)",
                ("u%d:tagline" % me,
                 os.environ.get("HQ_TAGLINE", "Pinta \u00b7 Ohr Chaim \u00b7 Personal")))


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    con = sqlite3.connect(DB_PATH, timeout=10)
    # An earlier build switched this database into WAL mode, which Render's disk
    # does not handle - every connection then hung. WAL is a property of the file,
    # so reverting the code was not enough; put it back on the way in.
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            con.execute("PRAGMA journal_mode = DELETE")
    except Exception:
        pass
    with open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
    if "due_date" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN due_date TEXT")
    if "project_id" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN project_id INTEGER REFERENCES projects(id)")
    if "remind_at" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN remind_at TEXT")
    if "thread_key" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN thread_key TEXT")
    if "wa_chat_id" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN wa_chat_id TEXT")
    ncols = [r[1] for r in con.execute("PRAGMA table_info(item_notes)")]
    if ncols and "source" not in ncols:
        con.execute("ALTER TABLE item_notes ADD COLUMN source TEXT DEFAULT ''")
    if ncols and "ext_id" not in ncols:
        con.execute("ALTER TABLE item_notes ADD COLUMN ext_id TEXT")
    bcols = [r[1] for r in con.execute("PRAGMA table_info(brief_items)")]
    if bcols and "target_item_id" not in bcols:
        con.execute("ALTER TABLE brief_items ADD COLUMN target_item_id INTEGER")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS wa_inbox ("
        " uid TEXT PRIMARY KEY, chat_id TEXT, chat_name TEXT, sender TEXT,"
        " from_me INTEGER NOT NULL DEFAULT 0, text TEXT, ts TEXT, received_at TEXT,"
        " handled INTEGER NOT NULL DEFAULT 0);"
        "CREATE INDEX IF NOT EXISTS wa_inbox_open ON wa_inbox(handled, ts);")
    if "created_at" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN created_at TEXT")
    # Today is a flag, not a list. Keeping "today" and "this week" as two lists
    # means keeping the same task twice and ticking it off twice.
    if "today" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN today INTEGER NOT NULL DEFAULT 0")
    if "today_at" not in cols:
        con.execute("ALTER TABLE items ADD COLUMN today_at TEXT")
    # A pipeline entry is still a task - it has responses, someone it waits on and
    # an age - but it also has numbers, and numbers buried in a title cannot be
    # sorted, compared or totalled.
    for col, decl in (("amount", "INTEGER"), ("ebitda", "INTEGER"),
                      ("units", "INTEGER"), ("tenure", "TEXT"), ("stage", "TEXT"),
                      ("pinned", "INTEGER NOT NULL DEFAULT 0"),
                      ("archived", "INTEGER NOT NULL DEFAULT 0")):
        if col not in cols:
            con.execute("ALTER TABLE items ADD COLUMN %s %s" % (col, decl))
    prcols = [r[1] for r in con.execute("PRAGMA table_info(projects)")]
    if prcols and "archived" not in prcols:
        con.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS checks ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,"
        " body TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, pos INTEGER NOT NULL DEFAULT 0);")
    _seed_history(con)
    ecols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
    if ecols and "source" not in ecols:
        con.execute("ALTER TABLE events ADD COLUMN source TEXT DEFAULT 'outlook'")
    if ecols and "note" not in ecols:
        con.execute("ALTER TABLE events ADD COLUMN note TEXT DEFAULT ''")
    if ecols and "dur_min" not in ecols:
        con.execute("ALTER TABLE events ADD COLUMN dur_min INTEGER")
    scols = [r[1] for r in con.execute("PRAGMA table_info(sections)")]
    if "owner_id" not in scols:
        con.execute("ALTER TABLE sections ADD COLUMN owner_id INTEGER")
    if "visibility" not in scols:
        con.execute("ALTER TABLE sections ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private'")
    if "kind" not in scols:
        con.execute("ALTER TABLE sections ADD COLUMN kind TEXT NOT NULL DEFAULT 'tasks'")
    if "board" not in scols:
        con.execute("ALTER TABLE sections ADD COLUMN board TEXT NOT NULL DEFAULT ''")
    if "color" not in scols:
        con.execute("ALTER TABLE sections ADD COLUMN color TEXT")
    if "cur" not in scols:
        con.execute("ALTER TABLE sections ADD COLUMN cur TEXT NOT NULL DEFAULT '\u00a3'")
    if ecols and "owner_id" not in ecols:
        con.execute("ALTER TABLE events ADD COLUMN owner_id INTEGER")
    if bcols and "owner_id" not in bcols:
        con.execute("ALTER TABLE brief_items ADD COLUMN owner_id INTEGER")
    pcols = [r[1] for r in con.execute("PRAGMA table_info(push_subs)")]
    if pcols and "user_id" not in pcols:
        con.execute("ALTER TABLE push_subs ADD COLUMN user_id INTEGER")
    con.executescript(
        "CREATE TABLE IF NOT EXISTS section_shares ("
        " section_id INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        " PRIMARY KEY(section_id, user_id));"
        "CREATE TABLE IF NOT EXISTS list_tags ("
        " item_id INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        " PRIMARY KEY(item_id, user_id));"
        "CREATE TABLE IF NOT EXISTS project_tags ("
        " project_id INTEGER NOT NULL, user_id INTEGER NOT NULL,"
        " PRIMARY KEY(project_id, user_id));")
    _pulse_notes_per_person(con)
    _make_people(con)
    _shared_becomes_people(con)
    _pipeline_becomes_plain(con)
    _three_boards(con)
    _events_unique_per_person(con)
    _make_indexes(con)          # after every ALTER, so the columns exist
    os.makedirs(FILES_DIR, exist_ok=True)
    _stamp_responses_in_new_york(con)
    n = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    if n == 0:
        with open(os.path.join(BASE, "seed_data.json"), encoding="utf-8") as f:
            data = json.load(f)
        now = datetime.now().isoformat(timespec="seconds")
        owner = con.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        owner = owner[0] if owner else None
        for si, sec in enumerate(data["sections"]):
            cur = con.execute(
                "INSERT INTO sections(title, pos, owner_id, visibility) VALUES(?,?,?,'private')",
                (sec["title"], si, owner))
            sid = cur.lastrowid
            for pi, it in enumerate(sec["items"]):
                con.execute(
                    "INSERT INTO items(section_id, title, note, waiting_on, status, pos, updated_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (sid, it["t"], it.get("n", ""), it.get("w", ""),
                     it.get("s", "open"), pi, now))
    _everything_into_buckets(con)   # after seeding, so a brand-new board is born bucketed
    _joel_fresh_start(con)          # after bucketing, so it trims projects, not sections
    _keep_seed(con)                 # after the buckets exist to receive it
    _bring_back_archive(con)        # after the import, so it can undo the put-away
    commit_retry(con)
    con.close()


# ---------- who is looking ----------
# One rule, in one place. A section is either yours or shared with the board;
# anything else is invisible. Every list narrows to these ids and every single
# row lookup is checked against them, so adding a route cannot quietly widen
# what somebody can see - a route that forgets to ask simply sees nothing.

def me():
    """Whoever this request is acting as - a signed-in person, or the account an
    API token stands for. Zero means nobody, and nobody sees nothing."""
    return session.get("uid") or getattr(g, "api_uid", 0) or 0


def user_row(con, uid=None):
    return con.execute("SELECT * FROM users WHERE id=?", (uid or me(),)).fetchone()


def people_list(con):
    return con.execute("SELECT id, username, display_name, is_admin FROM users"
                       " ORDER BY id").fetchall()


VISIBLE_SQL = ("owner_id=? OR visibility='shared'"
               " OR id IN (SELECT section_id FROM section_shares WHERE user_id=?)")


def visible_ids(con, uid=None):
    """Section ids this person may see: their own, shared-with-everyone, or
    shared with them by name. An empty list is a real answer."""
    uid = uid if uid is not None else me()
    return [r[0] for r in con.execute(
        "SELECT id FROM sections WHERE " + VISIBLE_SQL, (uid, uid))]


def sec_clause(con, col="section_id", uid=None, first=False):
    """A fragment to bolt onto any query that reaches items or sections.

    Returns a clause that matches nothing when the person can see nothing -
    never one that matches everything. That asymmetry is the whole point.
    """
    ids = visible_ids(con, uid)
    kw = " WHERE " if first else " AND "
    if not ids:
        return kw + "0=1", []
    return kw + "%s IN (%s)" % (col, ",".join("?" * len(ids))), ids


def tag_pool(con, item_id):
    """Everyone this task is on a list with - tagged itself, or through its project."""
    return [r[0] for r in con.execute(
        "SELECT user_id FROM list_tags WHERE item_id=?"
        " UNION SELECT pt.user_id FROM project_tags pt"
        " JOIN items i ON i.project_id = pt.project_id WHERE i.id=?",
        (item_id, item_id))]


def may_touch(con, item_id, uid=None):
    row = con.execute("SELECT section_id FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return False
    if row["section_id"] in visible_ids(con, uid):
        return True
    # a task lives in one person's bucket but can sit on a list with somebody
    # else - that somebody may read it and work it, and nobody else may
    uid = uid if uid is not None else me()
    return uid in tag_pool(con, item_id)


def require_item(con, item_id, uid=None):
    """404, not 403: refusing by name would confirm the task exists."""
    if not may_touch(con, item_id, uid):
        abort(404)


def require_section(con, sec_id, uid=None):
    if sec_id not in visible_ids(con, uid):
        abort(404)


BUCKET_POS = {"Personal/Family": 1, "Pinta": 2, "Community/Charity": 3}


def ensure_buckets(con, uid):
    """The three fixed boxes everyone works out of. Returns {title: section_id}."""
    out = {}
    for t in BOARDS:
        row = con.execute("SELECT id FROM sections WHERE owner_id=? AND lower(title)=lower(?)",
                          (uid, t)).fetchone()
        out[t] = row[0] if row else con.execute(
            "INSERT INTO sections(title, pos, owner_id, visibility, board)"
            " VALUES(?,?,?,'private',?)", (t, BUCKET_POS[t], uid, t)).lastrowid
    return out


def my_inbox(con, uid=None):
    """Captures land in the Inbox project inside Pinta, to be sorted later.
    Returns (section_id, project_id)."""
    uid = uid if uid is not None else me()
    sec = ensure_buckets(con, uid)["Pinta"]
    row = con.execute("SELECT id FROM projects WHERE section_id=? AND lower(title)='inbox'",
                      (sec,)).fetchone()
    proj = row[0] if row else con.execute(
        "INSERT INTO projects(section_id, title, pos) VALUES(?, 'Inbox', -1)",
        (sec,)).lastrowid
    return sec, proj


def uset(con, key, uid=None, default=""):
    """A setting that describes a person, not the board."""
    uid = uid if uid is not None else me()
    row = con.execute("SELECT v FROM settings WHERE k=?", ("u%d:%s" % (uid, key),)).fetchone()
    return row[0] if row else default


def uset_put(con, key, value, uid=None):
    uid = uid if uid is not None else me()
    con.execute("INSERT INTO settings(k, v) VALUES(?,?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                ("u%d:%s" % (uid, key), value))


def uset_del(con, keys, uid=None):
    uid = uid if uid is not None else me()
    for k in keys:
        con.execute("DELETE FROM settings WHERE k=?", ("u%d:%s" % (uid, k),))


# ---------- auth ----------

def login_required(f):
    @wraps(f)
    def wrapped(*a, **k):
        if session.get("user") and not session.get("uid"):
            # signed in before accounts existed - the cookie has a name but no
            # person behind it, which would scope every query to nobody
            session.clear()
        if not session.get("user"):
            if request.method == "POST" or request.path.startswith("/api/"):
                return jsonify(error="login required"), 401
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    from werkzeug.security import check_password_hash
    error = None
    if request.method == "POST":
        name = request.form.get("username", "").strip().lower()
        con = db()
        row = con.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        if row and check_password_hash(row["pw_hash"], request.form.get("password", "")):
            session.clear()
            session["user"] = row["username"]
            session["uid"] = row["id"]
            session["name"] = row["display_name"]
            session["admin"] = bool(row["is_admin"])
            session.permanent = True
            g.api_uid = 0
            return redirect(url_for(
                "today_view" if display_mode(con, row["id"]) == "simple" else "board"))
        # one message for both cases - never reveal which usernames exist
        error = "Wrong username or password."
    return render_template("login.html", error=error,
                           login_sub=os.environ.get("HQ_TAGLINE",
                                                    "Pinta · Ohr Chaim · Personal"))


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
    where, args = sec_clause(con, "id", first=True)
    sections = con.execute("SELECT * FROM sections" + where + " ORDER BY pos, id",
                           args).fetchall()
    order = [x for x in (uset(con, "secorder") or "").split(",") if x.strip().isdigit()]
    if order:
        rank = {int(x): i for i, x in enumerate(order)}
        sections = sorted(sections, key=lambda r: (rank.get(r["id"], 10**6), r["pos"], r["id"]))
    cur_board = canonical_board(request.args.get("b"))
    if cur_board:
        sections = [r for r in sections if (r["board"] or "").strip() == cur_board]
    shares = {}
    for r in con.execute("SELECT ss.section_id, u.display_name FROM section_shares ss"
                         " JOIN users u ON u.id = ss.user_id ORDER BY u.display_name"):
        shares.setdefault(r["section_id"], []).append(r["display_name"])
    collapsed = {int(x) for x in (uset(con, "collapsed") or "").split(",")
                 if x.strip().isdigit()}
    pcollapsed = {int(x) for x in (uset(con, "pcollapsed") or "").split(",")
                  if x.strip().isdigit()}
    where, args = sec_clause(con, "section_id", first=True)
    items = con.execute("SELECT * FROM items" + where + " AND archived=0"
                        " ORDER BY pinned DESC, pos, id", args).fetchall()
    if cur_board:
        keep_secs = {r["id"] for r in sections}
        items = [it for it in items if it["section_id"] in keep_secs]
    keep = set(it["id"] for it in items)
    notes = [n for n in con.execute("SELECT * FROM item_notes ORDER BY id")
             if n["item_id"] in keep]
    notes_by_item = {}
    for n in notes:
        notes_by_item.setdefault(n["item_id"], []).append(n)
    files = [f for f in con.execute("SELECT * FROM item_files ORDER BY id")
             if f["item_id"] in keep]
    files_by_item = {}
    for f in files:
        files_by_item.setdefault(f["item_id"], []).append(f)
    where, args = sec_clause(con, "section_id", first=True)
    projects = con.execute("SELECT * FROM projects" + where + " AND archived=0"
                           " ORDER BY pos, id", args).fetchall()
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
    checks_by_item = {}
    for c in con.execute("SELECT * FROM checks ORDER BY pos, id"):
        if c["item_id"] in keep:
            checks_by_item.setdefault(c["item_id"], []).append(c)
    ltags = {}
    for r in con.execute("SELECT item_id, user_id FROM list_tags"):
        ltags.setdefault(r["item_id"], []).append(r["user_id"])
    ltags = {k: ",".join(str(u) for u in sorted(v)) for k, v in ltags.items()}
    ptags = {}
    for r in con.execute("SELECT project_id, user_id FROM project_tags"):
        ptags.setdefault(r["project_id"], set()).add(r["user_id"])
    return render_template("board.html", sections=sections, by_sec=by_sec,
                           cur_board=cur_board, shares=shares, collapsed=collapsed,
                           pcollapsed=pcollapsed,
                           projects_by_sec=projects_by_sec, ltags=ltags, ptags=ptags,
                           checks_by_item=checks_by_item,
                           people={r["id"]: r["display_name"] for r in people_list(con)},
                           names=known_names(con),
                           sec_kind={r["id"]: r["kind"] for r in sections},
                           tenures=TENURES, stages=STAGES,
                           notes_by_item=notes_by_item, files_by_item=files_by_item,
                           today_iso=today_iso, soon_iso=soon_iso,
                           total_active=total_active, stats=stats,
                           today=datetime.now().strftime("%b %-d, %Y")
                           if os.name != "nt" else datetime.now().strftime("%b %d, %Y"))


# ---------- money ----------
# He writes "30m", not 30000000. Anything that makes him type a number in full is
# a thing he will stop doing by Thursday.

_MONEY = re.compile(r"^\s*[\u00a3$\u20ac]?\s*([\d,]*\.?\d+)\s*([kmb]?)\s*$", re.I)
_MULT = {"": 1, "k": 1000, "m": 1000000, "b": 1000000000}


def parse_money(raw):
    """'£30m' -> 30000000. None when it is not a number at all."""
    m = _MONEY.match(str(raw or ""))
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(round(n * _MULT[m.group(2).lower()]))


@app.template_filter("money")
def _money(n, cur="\u00a3"):
    """Back out the way he wrote it, so a column of these can be read at a glance."""
    if n in (None, ""):
        return ""
    n = int(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1000000000:
        body = ("%.2f" % (n / 1e9)).rstrip("0").rstrip(".") + "b"
    elif n >= 1000000:
        body = ("%.2f" % (n / 1e6)).rstrip("0").rstrip(".") + "m"
    elif n >= 1000:
        body = ("%.1f" % (n / 1e3)).rstrip("0").rstrip(".") + "k"
    else:
        body = str(n)
    return "%s%s%s" % (sign, cur or "", body)


# Section colors: distinct at a glance on cream and on dark, and stable - a
# section keeps its color whatever position it sits in. Recognition beats
# reading, which matters most for the people this board is for.
def team_list(con):
    """The internal people work waits on. One tap instead of typing a name -
    typing is why the field goes empty, and an empty field hides work."""
    row = con.execute("SELECT v FROM settings WHERE k='org:team'").fetchone()
    raw = row["v"] if row else "Joel,Shimon,Yanky,Varun,Polina,Simon"
    return [t.strip() for t in raw.split(",") if t.strip()]


SECTION_PALETTE = ("#C24E2A", "#1F3A5F", "#3A6B3E", "#B8892E",
                   "#6B4A8A", "#2A6B6B", "#A33B63", "#41586E")


def sec_color(row):
    c = (row["color"] or "").strip() if "color" in row.keys() else ""
    return c or SECTION_PALETTE[row["id"] % len(SECTION_PALETTE)]


TENURES = ("", "freehold", "leasehold", "mixed")
STAGES = ("", "lead", "looking", "diligence", "offer", "agreed", "dead")


# ---------- today ----------

@app.route("/items/<int:item_id>/today", methods=["POST"])
@login_required
def toggle_today(item_id):
    """Star it for today, or take the star off.

    The date it was starred is kept, because the useful question is never "is
    this on today" - it is "how long has this been on today". A task carrying
    a nine-day-old star is the single most informative thing on the board.
    """
    con = db()
    require_item(con, item_id)
    row = con.execute("SELECT today FROM items WHERE id=?", (item_id,)).fetchone()
    now = 0 if row["today"] else 1
    con.execute("UPDATE items SET today=?, today_at=? WHERE id=?",
                (now, _now_local().isoformat(timespec="seconds") if now else None, item_id))
    commit_retry(con)
    return jsonify(today=now)


@app.route("/pipeline")
@login_required
def pipeline_view():
    """Sections he has marked as a pipeline, as numbers rather than prose.

    They are still tasks underneath - each one keeps its responses, who it is
    waiting on and how long it has sat - but a pipeline you cannot sort or total
    is just a list you have to read every time.
    """
    con = db()
    where, args = sec_clause(con, "id", first=True)
    secs = con.execute("SELECT * FROM sections" + where + " AND kind='pipeline'"
                       " ORDER BY pos, id", args).fetchall()
    lanes = []
    for sec in secs:
        rows = con.execute(
            "SELECT * FROM items WHERE section_id=? AND status!='done'"
            " ORDER BY COALESCE(amount,0) DESC, id", (sec["id"],)).fetchall()
        done = con.execute(
            "SELECT COUNT(*) c FROM items WHERE section_id=? AND status='done'",
            (sec["id"],)).fetchone()["c"]
        lanes.append({
            "sec": sec, "rows": rows, "done": done,
            "amount": sum(r["amount"] or 0 for r in rows),
            "ebitda": sum(r["ebitda"] or 0 for r in rows),
            "units": sum(r["units"] or 0 for r in rows),
        })
    return render_template("pipeline.html", lanes=lanes, stages=STAGES)


@app.route("/sections/<int:sec_id>/kind", methods=["POST"])
@login_required
def set_section_kind(sec_id):
    con = db()
    if not con.execute("SELECT 1 FROM sections WHERE id=? AND owner_id=?",
                       (sec_id, me())).fetchone():
        abort(404)
    kind = "pipeline" if request.form.get("kind") == "pipeline" else "tasks"
    cur = (request.form.get("cur") or "\u00a3").strip()[:3]
    con.execute("UPDATE sections SET kind=?, cur=? WHERE id=?", (kind, cur, sec_id))
    commit_retry(con)
    return redirect(url_for("pipeline_view") if kind == "pipeline"
                    else url_for("board", _anchor="sec-%d" % sec_id))


@app.route("/items/<int:item_id>/deal", methods=["POST"])
@login_required
def set_deal(item_id):
    """The numbers on one pipeline entry."""
    con = db()
    require_item(con, item_id)
    tenure = (request.form.get("tenure") or "").strip().lower()
    stage = (request.form.get("stage") or "").strip().lower()
    units = request.form.get("units", type=int)
    con.execute("UPDATE items SET amount=?, ebitda=?, units=?, tenure=?, stage=?,"
                " updated_at=? WHERE id=?",
                (parse_money(request.form.get("amount")),
                 parse_money(request.form.get("ebitda")),
                 units if units and units > 0 else None,
                 tenure if tenure in TENURES else "",
                 stage if stage in STAGES else "",
                 datetime.now().isoformat(timespec="seconds"), item_id))
    commit_retry(con)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("pipeline_view"))


@app.route("/today")
@login_required
def today_view():
    """Everything starred, plus anything overdue or due today whether starred or not.

    Overdue work belongs on today's list by definition; making him star it as
    well would just be a second place to forget.
    """
    con = db()
    iso = _now_local().date().isoformat()
    where, args = sec_clause(con, "items.section_id")
    rows = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.status != 'done'"
        "   AND (items.today = 1 OR (COALESCE(items.due_date,'') != ''"
        "        AND items.due_date <= ?))" + where +
        " AND items.archived=0"
        " ORDER BY items.today DESC, items.due_date IS NULL, items.due_date,"
        "          items.today_at, items.id", [iso] + args).fetchall()
    keep = set(r["id"] for r in rows)
    notes_by_item, files_by_item = {}, {}
    for n in con.execute("SELECT * FROM item_notes ORDER BY id"):
        if n["item_id"] in keep:
            notes_by_item.setdefault(n["item_id"], []).append(n)
    for f in con.execute("SELECT * FROM item_files ORDER BY id"):
        if f["item_id"] in keep:
            files_by_item.setdefault(f["item_id"], []).append(f)
    where, args = sec_clause(con, "id", first=True)
    sections = con.execute("SELECT * FROM sections" + where + " ORDER BY pos, id",
                           args).fetchall()
    where, args = sec_clause(con, "section_id", first=True)
    projects = con.execute("SELECT * FROM projects" + where + " AND archived=0"
                           " ORDER BY pos, id", args).fetchall()
    projects_by_sec = {}
    for p in projects:
        projects_by_sec.setdefault(p["section_id"], []).append(p)
    on_me = [r for r in rows if r["status"] == "open"]
    waiting = [r for r in rows if r["status"] == "waiting"]
    evs = con.execute("SELECT * FROM events WHERE day=? AND owner_id=?"
                      " ORDER BY COALESCE(start_time,'99:99'), id", (iso, me())).fetchall()
    where2, args2 = sec_clause(con, "section_id")
    timed = con.execute(
        "SELECT * FROM items WHERE status != 'done' AND archived=0"
        " AND COALESCE(remind_at,'') != '' AND substr(remind_at,1,10) = ?"
        + where2 + " ORDER BY remind_at", [iso] + args2).fetchall()
    ltags = {}
    for r in con.execute("SELECT item_id, user_id FROM list_tags"):
        ltags.setdefault(r["item_id"], []).append(r["user_id"])
    ltags = {k: ",".join(str(u) for u in sorted(v)) for k, v in ltags.items()}
    tkeep = {r["id"] for r in rows} | {r["id"] for r in timed}
    checks_by_item = {}
    for c in con.execute("SELECT * FROM checks ORDER BY pos, id"):
        if c["item_id"] in tkeep:
            checks_by_item.setdefault(c["item_id"], []).append(c)
    return render_template("today.html", rows=rows, on_me=on_me, waiting=waiting,
                           evs=evs, timed=timed, ltags=ltags,
                           checks_by_item=checks_by_item,
                           people={r["id"]: r["display_name"] for r in people_list(con)},
                           sections=sections, projects_by_sec=projects_by_sec,
                           sec_kind={r["id"]: r["kind"] for r in sections},
                           tenures=TENURES, stages=STAGES,
                           notes_by_item=notes_by_item, files_by_item=files_by_item,
                           names=known_names(con),
                           today_iso=iso, soon_iso=iso,
                           pretty=_now_local().strftime("%A, %B %-d")
                           if os.name != "nt" else _now_local().strftime("%A, %B %d"))


def known_names(con):
    """Every person already being waited on, commonest first.

    Typing a name is the reason the waiting-on field goes unfilled, and an empty
    waiting-on field is what makes the People view useless."""
    where, args = sec_clause(con, "section_id", first=True)
    rows = con.execute(
        "SELECT TRIM(waiting_on) w, COUNT(*) c FROM items" + where +
        " AND TRIM(COALESCE(waiting_on,'')) != ''"
        " GROUP BY lower(TRIM(waiting_on)) ORDER BY c DESC, w", args).fetchall()
    return [r["w"] for r in rows]


def days_since(raw):
    d = pulse.parse(raw)
    if not d:
        return None
    return (pulse.now_utc() - d).days


app.jinja_env.globals["days_since"] = days_since


# ---------- item actions ----------

@app.route("/items/add", methods=["POST"])
@login_required
def add_item():
    sid = request.form.get("section_id", type=int)
    title = (request.form.get("title") or "").strip()
    if not sid or not title:
        return redirect(url_for("board"))
    con = db()
    require_section(con, sid)
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
    commit_retry(con)
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
    require_item(con, item_id)
    cur_row = con.execute("SELECT section_id FROM items WHERE id=?", (item_id,)).fetchone()
    # "+ New section..." / "+ New project..." straight from the dropdown
    new_sec = (request.form.get("new_section") or "").strip()[:60]
    new_proj = (request.form.get("new_project") or "").strip()[:80]
    if (request.form.get("section_id") or "") == "__new__" and new_sec:
        pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM sections").fetchone()[0]
        sid = con.execute(
            "INSERT INTO sections(title, pos, owner_id, visibility, board)"
            " VALUES(?,?,?,'private','')", (new_sec, pos, me())).lastrowid
    else:
        sid = request.form.get("section_id", type=int) or cur_row["section_id"]
        require_section(con, sid)
    if (request.form.get("project_id") or "") == "__new__" and new_proj:
        ppos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM projects WHERE section_id=?",
                           (sid,)).fetchone()[0]
        pid = con.execute("INSERT INTO projects(section_id, title, pos) VALUES(?,?,?)",
                          (sid, new_proj, ppos)).lastrowid
    else:
        pid = request.form.get("project_id", type=int) or None
        if pid:
            # a project decides the section it lives in
            prow = con.execute("SELECT section_id FROM projects WHERE id=?", (pid,)).fetchone()
            if prow:
                require_section(con, prow["section_id"])
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
    if "amount" in request.form or "stage" in request.form:
        tenure = (request.form.get("tenure") or "").strip().lower()
        stage = (request.form.get("stage") or "").strip().lower()
        units = request.form.get("units", type=int)
        con.execute("UPDATE items SET amount=?, ebitda=?, units=?, tenure=?, stage=?"
                    " WHERE id=?",
                    (parse_money(request.form.get("amount")),
                     parse_money(request.form.get("ebitda")),
                     units if units and units > 0 else None,
                     tenure if tenure in TENURES else "",
                     stage if stage in STAGES else "", item_id))
    if request.form.get("ltags") is not None:
        # only the person whose bucket this is decides who it is on a list with
        own = con.execute("SELECT s.owner_id FROM items i JOIN sections s"
                          " ON s.id=i.section_id WHERE i.id=?", (item_id,)).fetchone()
        if own and own["owner_id"] == me():
            uids = {int(x) for x in (request.form.get("ltags") or "").split(",")
                    if x.strip().isdigit()}
            known = {r["id"] for r in people_list(con)}
            con.execute("DELETE FROM list_tags WHERE item_id=?", (item_id,))
            for u in uids & (known - {me()}):
                con.execute("INSERT OR IGNORE INTO list_tags(item_id, user_id)"
                            " VALUES(?,?)", (item_id, u))
    commit_retry(con)
    return redirect(url_for("board"))


@app.route("/items/<int:item_id>/notes", methods=["POST"])
@login_required
def add_note(item_id):
    body = (request.form.get("body") or "").strip()
    note_id, created = None, None
    if body:
        con = db()
        require_item(con, item_id)
        created = _now_local().isoformat(timespec="seconds")
        cur = con.execute("INSERT INTO item_notes(item_id, body, created_at) VALUES(?,?,?)",
                          (item_id, body, created))
        note_id = cur.lastrowid
        con.execute("UPDATE items SET updated_at=? WHERE id=?", (created, item_id))
        if not commit_retry(con):
            # never fail silently - the response is still sitting in his box
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify(error="busy"), 503
            abort(503)
        it = con.execute("SELECT title FROM items WHERE id=?", (item_id,)).fetchone()
        tell_others(con, item_id, "notes", "%s replied" % _actor_name(con),
                    "%s  -  %s" % (_short(it["title"] if it else "", 40), _short(body)),
                    "/#item-%d" % item_id)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(id=note_id, body=body, created_at=created)
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("board"))


@app.route("/notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_note(note_id):
    con = db()
    owner = con.execute("SELECT item_id FROM item_notes WHERE id=?", (note_id,)).fetchone()
    if owner:
        require_item(con, owner["item_id"])
    con.execute("DELETE FROM item_notes WHERE id=?", (note_id,))
    commit_retry(con)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("board"))


@app.route("/items/<int:item_id>/cycle", methods=["POST"])
@login_required
def cycle_item(item_id):
    con = db()
    require_item(con, item_id)
    row = con.execute("SELECT status, title FROM items WHERE id=?", (item_id,)).fetchone()
    nxt = STATUSES[(STATUSES.index(row["status"]) + 1) % 3] \
        if row["status"] in STATUSES else "open"
    con.execute("UPDATE items SET status=?, updated_at=? WHERE id=?",
                (nxt, datetime.now().isoformat(timespec="seconds"), item_id))
    commit_retry(con)
    _tell_status(con, item_id, row, nxt)
    return jsonify(status=nxt)


@app.route("/items/<int:item_id>/status", methods=["POST"])
@login_required
def set_item_status(item_id):
    """Set a status outright - what a swipe uses, instead of cycling through all three."""
    st = (request.form.get("status") or "").strip()
    if st not in STATUSES:
        abort(400)
    con = db()
    require_item(con, item_id)
    was = con.execute("SELECT status, title FROM items WHERE id=?", (item_id,)).fetchone()
    con.execute("UPDATE items SET status=?, updated_at=? WHERE id=?",
                (st, datetime.now().isoformat(timespec="seconds"), item_id))
    commit_retry(con)
    _tell_status(con, item_id, was, st)
    return jsonify(status=st)


def _tell_status(con, item_id, was, now_status):
    """Only the transitions worth a buzz: finished, or handed back."""
    if not was or was["status"] == now_status:
        return
    who = _actor_name(con)
    if now_status == "done":
        tell_others(con, item_id, "done", "%s closed a task" % who,
                    _short(was["title"], 70), "/#item-%d" % item_id)
    elif was["status"] == "done":
        tell_others(con, item_id, "done", "%s reopened a task" % who,
                    _short(was["title"], 70), "/#item-%d" % item_id)


@app.route("/items/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_item(item_id):
    con = db()
    require_item(con, item_id)
    con.execute("DELETE FROM items WHERE id=?", (item_id,))
    commit_retry(con)
    return redirect(url_for("board"))


@app.route("/capture", methods=["POST"])
@login_required
def capture():
    """Quick capture — one line into the Inbox section."""
    title = (request.form.get("title") or "").strip()
    if not title:
        return redirect(url_for("board"))
    con = db()
    sid, pid = my_inbox(con)
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    con.execute(
        "INSERT INTO items(section_id, project_id, title, status, pos, updated_at)"
        " VALUES(?,?,?,?,?,?)",
        (sid, pid, title, "open", pos, datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)
    return redirect(url_for("board"))


@app.route("/items/<int:item_id>/files", methods=["POST"])
@login_required
def upload_file(item_id):
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no file"), 400
    require_item(db(), item_id)
    ext = os.path.splitext(f.filename)[1][:12]
    stored = uuid.uuid4().hex + ext
    f.save(os.path.join(FILES_DIR, stored))
    size = os.path.getsize(os.path.join(FILES_DIR, stored))
    con = db()
    cur = con.execute(
        "INSERT INTO item_files(item_id, filename, stored_name, size, created_at) VALUES(?,?,?,?,?)",
        (item_id, f.filename, stored, size,
         datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)
    it = con.execute("SELECT title FROM items WHERE id=?", (item_id,)).fetchone()
    tell_others(con, item_id, "notes", "%s attached a file" % _actor_name(con),
                "%s  -  %s" % (_short(it["title"] if it else "", 40), _short(f.filename, 40)),
                "/#item-%d" % item_id)
    return jsonify(id=cur.lastrowid, filename=f.filename, size=size)


@app.route("/files/<int:file_id>")
@login_required
def get_file(file_id):
    con = db()
    row = con.execute("SELECT * FROM item_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        abort(404)
    require_item(con, row["item_id"])
    return send_from_directory(FILES_DIR, row["stored_name"],
                               download_name=row["filename"], as_attachment=False)


@app.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
def delete_file(file_id):
    con = db()
    row = con.execute("SELECT * FROM item_files WHERE id=?", (file_id,)).fetchone()
    if row:
        require_item(con, row["item_id"])
        try:
            os.remove(os.path.join(FILES_DIR, row["stored_name"]))
        except OSError:
            pass
        con.execute("DELETE FROM item_files WHERE id=?", (file_id,))
        commit_retry(con)
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
        require_section(con, sid)
        pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM projects WHERE section_id=?",
                          (sid,)).fetchone()[0]
        con.execute("INSERT INTO projects(section_id, title, pos, created_at) VALUES(?,?,?,?)",
                    (sid, title, pos, datetime.now().isoformat(timespec="seconds")))
        commit_retry(con)
    return redirect(url_for("board", _anchor="sec-%d" % (sid or 0)))


@app.route("/projects/<int:proj_id>/delete", methods=["POST"])
@login_required
def delete_project(proj_id):
    con = db()
    prow = con.execute("SELECT section_id FROM projects WHERE id=?", (proj_id,)).fetchone()
    if prow:
        require_section(con, prow["section_id"])
    con.execute("UPDATE items SET project_id=NULL WHERE project_id=?", (proj_id,))
    con.execute("DELETE FROM projects WHERE id=?", (proj_id,))
    commit_retry(con)
    return redirect(url_for("board"))


# ---------- people (chase view) ----------

@app.route("/people")
@login_required
def people_view():
    con = db()
    where, args = sec_clause(con, "items.section_id")
    rows = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.status != 'done' AND items.archived=0 AND COALESCE(items.waiting_on,'') != ''"
        + where +
        " ORDER BY items.due_date IS NULL, items.due_date, items.id", args).fetchall()
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
    """One printable page of everything open on YOUR board, bucket by bucket,
    project by project, with the latest word on each. What used to be the
    Joel/Shimon review page grew up: whoever is signed in gets their own."""
    con = db()
    uid = me()
    sections = con.execute(
        "SELECT * FROM sections WHERE owner_id=? ORDER BY pos, id", (uid,)).fetchall()
    sec_ids = [s["id"] for s in sections]
    q = ",".join("?" * len(sec_ids)) if sec_ids else "NULL"
    projects = con.execute(
        "SELECT * FROM projects WHERE section_id IN (%s) AND archived=0"
        " ORDER BY pos, id" % q, sec_ids).fetchall() if sec_ids else []
    projects_by_sec = {}
    for p in projects:
        projects_by_sec.setdefault(p["section_id"], []).append(p)
    items = con.execute(
        "SELECT * FROM items WHERE section_id IN (%s) AND archived=0"
        " ORDER BY status='done', pinned DESC, pos, id" % q,
        sec_ids).fetchall() if sec_ids else []
    keep = {it["id"] for it in items}
    latest, done_count = {}, sum(1 for it in items if it["status"] == "done")
    for n in con.execute("SELECT item_id, body, created_at FROM item_notes ORDER BY id"):
        if n["item_id"] in keep:
            latest[n["item_id"]] = n
    by_sec = {}
    for it in items:
        by_sec.setdefault(it["section_id"], []).append(it)
    checks_by_item = {}
    for c in con.execute("SELECT * FROM checks WHERE done=0 ORDER BY pos, id"):
        if c["item_id"] in keep:
            checks_by_item.setdefault(c["item_id"], []).append(c)
    return render_template("joel.html", sections=sections, by_sec=by_sec,
                           checks_by_item=checks_by_item,
                           projects_by_sec=projects_by_sec, latest=latest,
                           done_count=done_count,
                           today=_now_local().strftime("%B %-d, %Y")
                           if os.name != "nt" else _now_local().strftime("%B %d, %Y"))


# ---------- an agenda for sitting down with anyone ----------

@app.route("/agenda/<who>")
@login_required
def agenda_view(who):
    """Everything waiting on one person, ready to walk through and to print.

    The Joel sheet stopped being special: an agenda is assembled from the tag,
    for whoever you are about to sit with."""
    who = (who or "").strip()[:60]
    con = db()
    where, args = sec_clause(con, "items.section_id")
    rows = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.status != 'done' AND items.archived=0 AND lower(trim(items.waiting_on)) = lower(?)"
        + where + " ORDER BY items.due_date IS NULL, items.due_date, items.id",
        [who] + args).fetchall()
    if not rows:
        rows = con.execute(
            "SELECT items.*, sections.title AS sec_title FROM items"
            " JOIN sections ON items.section_id = sections.id"
            " WHERE items.status != 'done' AND items.archived=0"
            " AND lower(items.waiting_on) LIKE lower(?)"
            + where + " ORDER BY items.due_date IS NULL, items.due_date, items.id",
            ["%" + who + "%"] + args).fetchall()
    keep = {r["id"] for r in rows}
    latest = {}
    for n in con.execute("SELECT item_id, body, created_at FROM item_notes ORDER BY id"):
        if n["item_id"] in keep:
            latest[n["item_id"]] = n           # ordered ascending - last one wins
    return render_template("agenda.html", who=who, rows=rows, latest=latest,
                           today=_now_local().strftime("%B %-d, %Y")
                           if os.name != "nt" else _now_local().strftime("%B %d, %Y"))


# ---------- the list: two people, one page ----------

def _tagged_between(con, a, b):
    """Items owned by a (their bucket) that sit on a list with b."""
    return con.execute(
        "SELECT items.*, COALESCE(p.title, '') AS proj_title FROM items"
        " JOIN sections s ON s.id = items.section_id"
        " LEFT JOIN projects p ON p.id = items.project_id"
        " WHERE s.owner_id=? AND items.archived=0 AND items.id IN ("
        "   SELECT item_id FROM list_tags WHERE user_id=?"
        "   UNION SELECT i2.id FROM items i2"
        "     JOIN project_tags pt ON pt.project_id = i2.project_id WHERE pt.user_id=?)"
        " ORDER BY items.status='done', items.due_date IS NULL, items.due_date,"
        " items.pos, items.id", (a, b, b)).fetchall()


@app.route("/list")
@app.route("/list/<username>")
@login_required
def list_view(username=None):
    """One list for the two of you: what each side has tagged for the other,
    living in its own bucket, reviewable here and printable as one sheet."""
    con = db()
    uid = me()
    folk = {r["id"]: r for r in people_list(con)}
    other = None
    if username:
        for pid, r in folk.items():
            if r["username"] == username and pid != uid:
                other = pid
    if other is None:
        # whoever shares the most with you comes first; default to them
        counts = {}
        for pid in folk:
            if pid == uid:
                continue
            counts[pid] = len(_tagged_between(con, uid, pid)) + \
                len(_tagged_between(con, pid, uid))
        others = sorted(counts, key=lambda p: (-counts[p], p))
        if username or not others:
            other = others[0] if others else None
        else:
            other = others[0]
    if other is None:
        return render_template("list.html", folk=folk, other=None, mine=[], theirs=[],
                               latest={}, me_id=uid, today="")
    mine = _tagged_between(con, uid, other)
    theirs = _tagged_between(con, other, uid)
    keep = {r["id"] for r in mine} | {r["id"] for r in theirs}
    latest = {}
    for n in con.execute("SELECT item_id, body, created_at FROM item_notes ORDER BY id"):
        if n["item_id"] in keep:
            latest[n["item_id"]] = n
    return render_template("list.html", folk=folk, other=other, me_id=uid,
                           mine=mine, theirs=theirs, latest=latest,
                           today=_now_local().strftime("%B %-d, %Y")
                           if os.name != "nt" else _now_local().strftime("%B %d, %Y"))


@app.route("/items/<int:item_id>/tags", methods=["POST"])
@login_required
def set_item_tags(item_id):
    """Put a task on (or off) the list with the named people. Only the person
    whose bucket it lives in decides who it is shared with."""
    con = db()
    row = con.execute(
        "SELECT s.owner_id FROM items i JOIN sections s ON s.id=i.section_id"
        " WHERE i.id=?", (item_id,)).fetchone()
    if not row or row["owner_id"] != me():
        abort(404)
    uids = {int(u) for u in request.form.getlist("uids") if str(u).isdigit()}
    known = {r["id"] for r in people_list(con)}
    con.execute("DELETE FROM list_tags WHERE item_id=?", (item_id,))
    for u in uids & known - {me()}:
        con.execute("INSERT OR IGNORE INTO list_tags(item_id, user_id) VALUES(?,?)",
                    (item_id, u))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/projects/<int:proj_id>/tags", methods=["POST"])
@login_required
def set_project_tags(proj_id):
    """Tag a whole project onto the list - every task in it, present and future."""
    con = db()
    row = con.execute(
        "SELECT s.owner_id FROM projects p JOIN sections s ON s.id=p.section_id"
        " WHERE p.id=?", (proj_id,)).fetchone()
    if not row or row["owner_id"] != me():
        abort(404)
    uids = {int(u) for u in request.form.getlist("uids") if str(u).isdigit()}
    known = {r["id"] for r in people_list(con)}
    con.execute("DELETE FROM project_tags WHERE project_id=?", (proj_id,))
    for u in uids & known - {me()}:
        con.execute("INSERT OR IGNORE INTO project_tags(project_id, user_id) VALUES(?,?)",
                    (proj_id, u))
    commit_retry(con)
    return redirect(url_for("board"))


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
    where, args = sec_clause(con, "items.section_id")
    rows = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE due_date IS NOT NULL AND due_date != ''" + where +
        " ORDER BY due_date", args).fetchall()
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
    evs = con.execute("SELECT * FROM events WHERE owner_id=?"
                      " ORDER BY day, COALESCE(start_time,'')", (me(),)).fetchall()
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
                           home=uset(con, "origin_home"), work=uset(con, "origin_work"),
                           maps_key=bool(maps.KEY),
                           today_iso=t_iso, cur_month=mo)


# ---------- daily briefing ----------

def _unread_count(con):
    row = con.execute("SELECT COUNT(*) c FROM brief_items WHERE is_read=0 AND owner_id=?",
                      (me(),)).fetchone()
    return row["c"] if row else 0


def board_title(con, uid=None):
    """What this person's board calls itself. Theirs to name."""
    uid = uid if uid is not None else me()
    t = uset(con, "board_title", uid)
    if t:
        return t
    row = con.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone()
    name = (row["display_name"] if row else "").strip()
    if not name:
        return "HQ"
    name = name.split()[0]   # "Joel Landau" signs his board "Joel's HQ"
    return "%s' HQ" % name if name.endswith("s") else "%s's HQ" % name


def display_mode(con, uid=None):
    """simple = one line per task, tap for the rest. The default for everyone but
    the admin, because the person who built the board can stand its density and
    nobody else should have to."""
    uid = uid if uid is not None else me()
    v = uset(con, "display", uid)
    if v in ("simple", "full"):
        return v
    row = con.execute("SELECT is_admin FROM users WHERE id=?", (uid,)).fetchone()
    return "full" if (row and row["is_admin"]) else "simple"


@app.context_processor
def inject_identity():
    """The board is named for the person looking at it, not for the person who
    happens to have built it - and dressed for them too: a tab with nothing
    behind it is furniture, so it is not shown."""
    blank = {"board_name": "Pinta HQ", "tagline": "", "display_mode": "full",
             "has_brief": False, "has_cal": False, "has_pipe": False,
             "boards": [], "team": []}
    try:
        if not me():
            return blank
        con = db()
        ids = visible_ids(con)
        q = ",".join("?" * len(ids)) if ids else "NULL"
        secrows = con.execute(
            "SELECT id, color, board FROM sections WHERE id IN (%s)" % q,
            ids).fetchall() if ids else []
        # everyone sees the same three boards, in the same order, plus any
        # extra name somebody has invented for themselves
        boards = list(BOARDS)
        for r in secrows:
            b = (r["board"] or "").strip()
            if b and b not in boards:
                boards.append(b)
        return {
            "board_name": board_title(con),
            "tagline": uset(con, "tagline"),
            "display_mode": display_mode(con),
            "sec_colors": {r["id"]: sec_color(r) for r in secrows},
            "boards": boards,
            "team": team_list(con),
            "has_brief": bool(con.execute(
                "SELECT 1 FROM brief_items WHERE owner_id=? LIMIT 1", (me(),)).fetchone()),
            "has_cal": bool(con.execute(
                "SELECT 1 FROM events WHERE owner_id=? LIMIT 1", (me(),)).fetchone()),
            "has_pipe": bool(ids) and bool(con.execute(
                "SELECT 1 FROM sections WHERE kind='pipeline' AND id IN (%s) LIMIT 1"
                % q, ids).fetchone()),
        }
    except Exception:
        return blank


@app.context_processor
def inject_unread():
    try:
        return {"brief_unread": _unread_count(db())}
    except Exception:
        return {"brief_unread": 0}


# ---------- when something snaps ----------
# A bare "Internal Server Error" tells nobody anything. The traceback goes to
# the server log AND to one row in settings, so whoever is signed in can open
# /oops and read exactly what broke - a two-person board can afford that.

@app.errorhandler(Exception)
def _unhandled(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    import traceback
    tb = traceback.format_exc()
    print("HQ-ERROR %s %s\n%s" % (request.method, request.path, tb), flush=True)
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.execute(
            "INSERT OR REPLACE INTO settings(k, v) VALUES('last_error', ?)",
            ("%s · %s %s\n%s" % (datetime.now().isoformat(timespec="seconds"),
                                 request.method, request.path, tb),))
        con.commit()
        con.close()
    except Exception:
        pass
    try:
        return render_template("oops.html", detail=None), 500
    except Exception:
        return "Something snapped. The details are saved at /oops.", 500


@app.route("/oops")
@login_required
def oops_view():
    row = db().execute("SELECT v FROM settings WHERE k='last_error'").fetchone()
    return render_template("oops.html", detail=row[0] if row else None)


@app.route("/briefing")
@app.route("/briefing/<day>")
@login_required
def briefing_view(day=None):
    from datetime import date, timedelta
    today = _now_local().date()
    try:
        d = date.fromisoformat(day) if day else today
    except ValueError:
        d = today
    iso = d.isoformat()
    con = db()
    lines = con.execute(
        "SELECT * FROM brief_items WHERE day=? AND owner_id=? ORDER BY"
        " CASE kind WHEN 'due' THEN 0 WHEN 'meeting' THEN 1 WHEN 'email' THEN 2"
        " WHEN 'added' THEN 3 ELSE 4 END, id", (iso, me())).fetchall()
    days = [r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM brief_items WHERE owner_id=?"
        " ORDER BY day DESC LIMIT 14", (me(),))]
    where, args = sec_clause(con, "id", first=True)
    sections = con.execute("SELECT * FROM sections" + where + " ORDER BY pos, id",
                           args).fetchall()
    where, args = sec_clause(con, "section_id", first=True)
    titles = {r["id"]: r["title"]
              for r in con.execute("SELECT id, title FROM items" + where, args)}
    return render_template("briefing.html", d=d, day=iso, lines=lines, days=days,
                           sections=sections, titles=titles,
                           is_today=(iso == today.isoformat()),
                           pretty=d.strftime("%A, %B %d"),
                           prev_day=(d - timedelta(days=1)).isoformat(),
                           next_day=(d + timedelta(days=1)).isoformat())


@app.route("/brief/<int:bid>/read", methods=["POST"])
@login_required
def brief_read(bid):
    con = db()
    row = con.execute("SELECT is_read, day FROM brief_items WHERE id=? AND owner_id=?",
                      (bid, me())).fetchone()
    if not row:
        abort(404)
    new = 0 if row["is_read"] else 1
    con.execute("UPDATE brief_items SET is_read=? WHERE id=?", (new, bid))
    commit_retry(con)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(is_read=new)
    return redirect(url_for("briefing_view", day=row["day"]))


@app.route("/brief/readall/<day>", methods=["POST"])
@login_required
def brief_read_all(day):
    con = db()
    con.execute("UPDATE brief_items SET is_read=1 WHERE day=? AND owner_id=?",
                (day, me()))
    commit_retry(con)
    return redirect(url_for("briefing_view", day=day))


@app.route("/brief/<int:bid>/task", methods=["POST"])
@login_required
def brief_to_task(bid):
    con = db()
    row = con.execute("SELECT * FROM brief_items WHERE id=? AND owner_id=?",
                      (bid, me())).fetchone()
    if not row:
        abort(404)
    title = (request.form.get("title") or row["text"]).strip()[:120]
    sid = request.form.get("section_id", type=int)
    pid = None
    if sid:
        require_section(con, sid)
    else:
        sid, pid = my_inbox(con)
    note = (request.form.get("note") or row["detail"] or "").strip()[:200]
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    iid = con.execute(
        "INSERT INTO items(section_id, project_id, title, note, waiting_on, status, pos,"
        " due_date, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, pid, title, note, (request.form.get("waiting_on") or "").strip(), "open", pos,
         (request.form.get("due_date") or "").strip() or None,
         datetime.now().isoformat(timespec="seconds"))).lastrowid
    con.execute("UPDATE brief_items SET item_id=?, is_read=1 WHERE id=?", (iid, bid))
    commit_retry(con)
    return redirect(url_for("briefing_view", day=row["day"]))


@app.route("/brief/<int:bid>/file", methods=["POST"])
@login_required
def brief_file(bid):
    """Accept a suggested update: it becomes a response on the task it points at."""
    con = db()
    b = con.execute("SELECT * FROM brief_items WHERE id=? AND owner_id=?",
                    (bid, me())).fetchone()
    if not b:
        abort(404)
    target = b["target_item_id"]
    if not target or not may_touch(con, target):
        abort(400)
    body = b["text"]
    if b["detail"]:
        body += "  " + b["detail"]
    now = _now_local().isoformat(timespec="seconds")
    con.execute("INSERT INTO item_notes(item_id, body, source, created_at)"
                " VALUES(?,?,?,?)", (target, body, "email", now))
    con.execute("UPDATE items SET updated_at=? WHERE id=?", (now, target))
    con.execute("UPDATE brief_items SET item_id=?, is_read=1 WHERE id=?", (target, bid))
    commit_retry(con)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, item_id=target)
    return redirect(url_for("board", _anchor="item-%d" % target))


@app.route("/brief/<int:bid>/dismiss", methods=["POST"])
@login_required
def brief_dismiss(bid):
    con = db()
    row = con.execute("SELECT day FROM brief_items WHERE id=? AND owner_id=?",
                      (bid, me())).fetchone()
    if not row:
        abort(404)
    con.execute("DELETE FROM brief_items WHERE id=?", (bid,))
    commit_retry(con)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("briefing_view", day=row["day"] if row else None))


# ---------- maps: where you are leaving from, and how long it takes ----------

BUFFER_MIN = 10          # padding either side of a drive so you are not sprinting


FIX_MAX_AGE_MIN = 25     # a location fix older than this is not where you are any more


def _origins(con, uid=None):
    home = uset(con, "origin_home", uid) or uset(con, "origin_address", uid) or ""
    work = uset(con, "origin_work", uid) or ""
    return home, work


def live_fix(con, max_age_min=FIX_MAX_AGE_MIN, uid=None):
    """Where the phone last said you were, if that was recent enough to trust."""
    pos = uset(con, "last_pos", uid) or ""
    at = uset(con, "last_pos_at", uid) or ""
    if not (pos and at):
        return ""
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        return ""
    now = _now_local()
    if when.tzinfo is None:
        now = now.replace(tzinfo=None)
    from datetime import timedelta as _td2
    if now - when > _td2(minutes=max_age_min):
        return ""
    return pos


FIX_HORIZON_MIN = 180    # a fix says where you are now, not where you will be tonight


def origin_for(evs, idx, home, work, fix="", now_hhmm=""):
    """Where you will actually be coming from for evs[idx].

    Best answer first: where your phone says you are right now - but only for
    something starting in the next few hours, since by tonight you will have moved.
    Then the last real place you were today. Then home early / the office later.
    Returns (address, label) - the label is shown so it is never a mystery."""
    ev = evs[idx]
    if fix:
        near = True
        start = (ev["start_time"] or "")[:5]
        if now_hhmm and start:
            try:
                a = int(now_hhmm[:2]) * 60 + int(now_hhmm[3:5])
                b = int(start[:2]) * 60 + int(start[3:5])
                near = 0 <= (b - a) <= FIX_HORIZON_MIN
            except ValueError:
                near = True
        if near:
            return fix, "from where you are"
    for j in range(idx - 1, -1, -1):
        if evs[j]["day"] == ev["day"] and maps.is_place(evs[j]["location"]):
            subj = (evs[j]["subject"] or "your last stop").strip()
            if len(subj) > 26:
                subj = subj[:26].rsplit(" ", 1)[0] + "\u2026"
            return evs[j]["location"], "after " + subj
    hhmm = (ev["start_time"] or "")[:5]
    if hhmm and hhmm >= "11:00" and work:
        return work, "from the office"
    if home:
        return home, "from home"
    return (work, "from the office") if work else ("", "")


def _depart_utc(day, hhmm):
    """The event start as a UTC datetime, for a traffic-aware estimate."""
    if not hhmm:
        return None
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        local = datetime.strptime(day + " " + hhmm[:5], "%Y-%m-%d %H:%M")
        return local.replace(tzinfo=ZoneInfo(TZ_NAME)).astimezone(timezone.utc)
    except Exception:
        return None


def _leave_by(hhmm, secs):
    """'1:05 PM' - when to walk out the door to make a start time."""
    if not (hhmm and secs):
        return ""
    try:
        from datetime import timedelta as _td
        t = datetime.strptime(hhmm[:5], "%H:%M") - _td(seconds=secs) - _td(minutes=BUFFER_MIN)
        return fmt12(t.strftime("%H:%M"))
    except Exception:
        return ""


@app.route("/settings/origin", methods=["POST"])
@login_required
def set_origin():
    """The two addresses travel times are measured from."""
    con = db()
    for field in ("origin_home", "origin_work"):
        if field in request.form:
            uset_put(con, field, (request.form.get(field) or "").strip())
    commit_retry(con)
    return redirect(request.form.get("back") or url_for("calendar_view"))


@app.route("/where", methods=["POST"])
@login_required
def set_where():
    """The phone telling us where it is, so travel times start from you, not an address.

    Only the latest fix is kept - this is not a location history."""
    try:
        lat = float(request.form.get("lat"))
        lng = float(request.form.get("lng"))
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(ok=False), 400
    con = db()
    uset_put(con, "last_pos", "%.5f,%.5f" % (lat, lng))
    uset_put(con, "last_pos_at", _now_local().isoformat(timespec="seconds"))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/where/forget", methods=["POST"])
@login_required
def forget_where():
    con = db()
    uset_del(con, ("last_pos", "last_pos_at"))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/api/origin")
def api_origin():
    """Read or set the two addresses travel times are measured from."""
    if not _api_auth():
        abort(401)
    con = db()
    changed = []
    for arg, key in (("home", "origin_home"), ("work", "origin_work")):
        if arg in request.args:
            con.execute("INSERT INTO settings(k, v) VALUES(?, ?)"
                        " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (key, (request.args.get(arg) or "").strip()))
            changed.append(arg)
    if changed:
        commit_retry(con)
    home, work = _origins(con)
    return ("HOME: %s\nWORK: %s\n%s" %
            (home or "(not set)", work or "(not set)",
             ("SAVED: " + ", ".join(changed)) if changed else "unchanged")), 200, \
        {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/maps/check")
def maps_check():
    """Is the Google key in place and does it actually answer? Signed in, or with the API token."""
    if not (session.get("user") or _api_auth()):
        abort(401)
    st = maps.status()
    con = db()
    home, work = _origins(con)
    st["home"], st["work"] = home, work
    # prove the real path end to end, not just that the key answers
    if maps.KEY and home and work:
        secs = maps.drive_seconds(home, work)
        st["sample"] = ("home to the office: %s, so for a 9:00 AM you leave at %s"
                        % (maps.pretty_minutes(secs), _leave_by("09:00", secs))) \
            if secs else "home to the office: no route came back"
    return jsonify(st)


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
        "SELECT * FROM events WHERE day=? AND owner_id=?"
        " ORDER BY COALESCE(start_time,'99:99'), id", (day, me())).fetchall()
    where, args = sec_clause(con, "items.section_id")
    tasks = con.execute(
        "SELECT items.*, sections.title AS sec_title FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.due_date = ?" + where +
        " ORDER BY items.status='done', items.id", [day] + args).fetchall()
    where, args = sec_clause(con, "id", first=True)
    sections = con.execute("SELECT * FROM sections" + where + " ORDER BY pos, id",
                           args).fetchall()
    home, work = _origins(con)
    fix = live_fix(con) if day == _now_local().strftime("%Y-%m-%d") else ""
    travel = {}
    if (home or work) and maps.KEY and day >= _now_local().strftime("%Y-%m-%d"):
        for i, e in enumerate(evs):
            if not maps.is_place(e["location"]):
                continue
            src, label = origin_for(evs, i, home, work, fix, _now_local().strftime("%H:%M"))
            if not src:
                continue
            secs = maps.drive_seconds(src, e["location"],
                                      _depart_utc(e["day"], e["start_time"]))
            if secs:
                travel[e["id"]] = {"pretty": maps.pretty_minutes(secs),
                                   "leave": _leave_by(e["start_time"], secs),
                                   "from": label}
    return render_template("day.html", d=d, day=day, evs=evs, tasks=tasks,
                           sections=sections, home=home, work=work, travel=travel,
                           maps_key=bool(maps.KEY),
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
        "INSERT INTO events(ext_key, subject, day, start_time, location, note, source,"
        " owner_id, synced_at) VALUES(?,?,?,?,?,?,'manual',?,?)",
        ("m-" + uuid.uuid4().hex[:12], subj, day,
         (request.form.get("start_time") or "").strip() or None,
         (request.form.get("location") or "").strip(),
         (request.form.get("note") or "").strip(), me(),
         datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)
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
        " WHERE id=? AND owner_id=?",
        (subj, day, (request.form.get("start_time") or "").strip() or None,
         (request.form.get("location") or "").strip(),
         (request.form.get("note") or "").strip(), ev_id, me()))
    commit_retry(con)
    return redirect(url_for("day_view", day=day))


@app.route("/events/<int:ev_id>/delete", methods=["POST"])
@login_required
def delete_event(ev_id):
    con = db()
    row = con.execute("SELECT * FROM events WHERE id=? AND owner_id=?",
                      (ev_id, me())).fetchone()
    if not row:
        return redirect(url_for("calendar_view"))
    if (row["source"] or "outlook") == "outlook" and row["ext_key"]:
        # remember it so tomorrow's sync does not bring it back
        con.execute("INSERT OR IGNORE INTO hidden_events(ext_key) VALUES(?)", (row["ext_key"],))
    con.execute("DELETE FROM events WHERE id=?", (ev_id,))
    commit_retry(con)
    return redirect(url_for("day_view", day=row["day"]))


# ---------- outbound: publish HQ to Outlook (ICS) ----------

def _feed_token(con, uid=None):
    """One private URL each - a shared one would hand over the other person's diary."""
    uid = uid if uid is not None else me()
    tok = uset(con, "feed_token", uid) or ""
    if not tok:
        tok = uuid.uuid4().hex
        uset_put(con, "feed_token", tok, uid)
        commit_retry(con)
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


def build_ics(con, feed_uid):
    from datetime import timedelta as _td
    now = datetime.now()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    horizon = (now.date() - _td(days=60)).isoformat()
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Pinta HQ//EN",
         "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         "X-WR-CALNAME:HQ", "X-WR-TIMEZONE:" + TZ_NAME,
         "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]

    # your own events (Outlook ones are skipped - they already live in Outlook)
    for e in con.execute("SELECT * FROM events WHERE COALESCE(source,'outlook')='manual'"
                         " AND owner_id=? AND day >= ?", (feed_uid, horizon)):
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
    where, args = sec_clause(con, "items.section_id", uid=feed_uid)
    for it in con.execute(
            "SELECT items.*, sections.title AS sec FROM items"
            " JOIN sections ON items.section_id = sections.id"
            " WHERE COALESCE(items.due_date,'') != '' AND items.due_date >= ?"
            " AND items.status != 'done'" + where, [horizon] + args):
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
    if not token:
        abort(404)
    row = con.execute("SELECT k FROM settings WHERE v=? AND k LIKE 'u%:feed_token'",
                      (token,)).fetchone()
    if not row:
        abort(404)
    try:
        feed_uid = int(row["k"].split(":")[0][1:])
    except (ValueError, IndexError):
        abort(404)
    return build_ics(con, feed_uid), 200, {"Content-Type": "text/calendar; charset=utf-8"}


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
    e = con.execute("SELECT * FROM events WHERE id=? AND owner_id=?",
                    (ev_id, me())).fetchone()
    if not e:
        abort(404)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Pinta HQ//EN", "METHOD:PUBLISH",
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


def _int_or_none(v):
    """Length of a meeting, when the sweep bothered to send one."""
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 24 * 60 else None


# ---------- telling the other person ----------
# A notification carries the task's title in its body, so who may be told is
# exactly who may see the task. That is derived from the section every time
# rather than passed in, because the one way this goes badly wrong is a push
# describing work somebody was not allowed to read.

NOTIFY_KINDS = {"notes": "Someone responds on a shared task",
                "done": "Someone closes a shared task"}


def wants(con, uid, kind):
    v = uset(con, "notify_" + kind, uid, default="1")
    return v != "0"


def watchers(con, item_id, kind, exclude=None):
    """Everyone who can see this task and wants this kind of nudge, minus the
    person who just did the thing. Empty for a private section, by construction."""
    row = con.execute("SELECT section_id FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        return []
    sec = con.execute("SELECT id, owner_id, visibility FROM sections WHERE id=?",
                      (row["section_id"],)).fetchone()
    if not sec:
        return []
    exclude = exclude if exclude is not None else me()
    if sec["visibility"] == "shared":
        pool = [r["id"] for r in con.execute("SELECT id FROM users WHERE id<>?", (exclude,))]
    elif sec["visibility"] == "some":
        pool = [r[0] for r in con.execute(
            "SELECT user_id FROM section_shares WHERE section_id=?", (sec["id"],))]
        pool.append(sec["owner_id"])
        pool = [u for u in set(pool) if u != exclude]
    else:
        pool = []
    # a task on a list is watched by everyone on that list, and by its owner
    tags = tag_pool(con, item_id)
    if tags:
        pool = [u for u in set(pool) | set(tags) | {sec["owner_id"]} if u != exclude]
    if not pool:
        return []
    return [u for u in pool if wants(con, u, kind)]


def tell_others(con, item_id, kind, title, body, url):
    for uid in watchers(con, item_id, kind):
        try:
            send_push(title, body, url, uid=uid)
        except Exception as e:                      # a push must never lose the write
            app.logger.warning("push to %s failed: %s", uid, e)


def _short(txt, n=90):
    t = " ".join((txt or "").split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "\u2026"


def _actor_name(con, uid=None):
    row = con.execute("SELECT display_name FROM users WHERE id=?",
                      (uid if uid is not None else me(),)).fetchone()
    return row["display_name"] if row else "Someone"


# ---------- account ----------

@app.route("/account")
@login_required
def account_view():
    con = db()
    folk = people_list(con)
    home, work = _origins(con)
    return render_template("account.html",
                           home=home, work=work, maps_key=bool(maps.KEY),
                           who=user_row(con), folk=folk,
                           titles={r["id"]: board_title(con, r["id"]) for r in folk},
                           taglines={r["id"]: uset(con, "tagline", r["id"]) for r in folk},
                           my_title=uset(con, "board_title"),
                           api_token=api_token_for(con),
                           notify_kinds=NOTIFY_KINDS,
                           notify_on={k: wants(con, me(), k) for k in NOTIFY_KINDS},
                           feed_url=request.url_root.rstrip("/")
                           + url_for("ics_feed", token=_feed_token(con)))


@app.route("/account/password", methods=["POST"])
@login_required
def change_password():
    from werkzeug.security import check_password_hash, generate_password_hash
    con = db()
    row = user_row(con)
    old = request.form.get("current", "")
    new = request.form.get("new", "")
    if not row or not check_password_hash(row["pw_hash"], old):
        return render_template("account.html", who=row, folk=people_list(con),
                               feed_url="", api_token="", error="That is not your current password.")
    if len(new) < 8:
        return render_template("account.html", who=row, folk=people_list(con),
                               feed_url="", api_token="", error="Use at least 8 characters.")
    con.execute("UPDATE users SET pw_hash=? WHERE id=?",
                (generate_password_hash(new), row["id"]))
    commit_retry(con)
    return render_template("account.html", who=row, folk=people_list(con),
                           feed_url="", api_token="", ok="Password changed.")


@app.route("/account/identity", methods=["POST"])
@login_required
def set_identity():
    """Rename yourself and your board. An admin may do it for anyone - the person
    who sets someone up is usually the person who knows what to call them."""
    con = db()
    target = request.form.get("uid", type=int) or me()
    if target != me() and not session.get("admin"):
        abort(403)
    if not con.execute("SELECT 1 FROM users WHERE id=?", (target,)).fetchone():
        abort(404)
    name = (request.form.get("display_name") or "").strip()[:40]
    if name:
        con.execute("UPDATE users SET display_name=? WHERE id=?", (name, target))
        if target == me():
            session["name"] = name
    title = (request.form.get("board_title") or "").strip()[:40]
    uset_put(con, "board_title", title, target)      # empty falls back to "<Name>'s HQ"
    if "tagline" in request.form:
        uset_put(con, "tagline", (request.form.get("tagline") or "").strip()[:60], target)
    d = (request.form.get("display") or "").strip()
    if d in ("simple", "full"):
        uset_put(con, "display", d, target)
    commit_retry(con)
    return redirect(url_for("account_view"))


@app.route("/account/notify", methods=["POST"])
@login_required
def set_notify():
    con = db()
    for kind in NOTIFY_KINDS:
        uset_put(con, "notify_" + kind, "1" if request.form.get(kind) else "0")
    commit_retry(con)
    return redirect(url_for("account_view"))


@app.route("/account/newkey", methods=["POST"])
@login_required
def new_api_key():
    con = db()
    uset_del(con, ("api_token",))
    api_token_for(con)
    commit_retry(con)
    return redirect(url_for("account_view"))


@app.route("/account/add", methods=["POST"])
@login_required
def add_person():
    """Only an admin adds people, and a new person starts with nothing visible."""
    from werkzeug.security import generate_password_hash
    con = db()
    if not session.get("admin"):
        abort(403)
    username = (request.form.get("username") or "").strip().lower()
    display = (request.form.get("display_name") or "").strip() or username.title()
    pw = request.form.get("password") or ""
    err = None
    if not re.match(r"^[a-z0-9_.-]{2,32}$", username):
        err = "Username: letters, digits, dot, dash or underscore."
    elif len(pw) < 8:
        err = "Give them a password of at least 8 characters."
    elif con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        err = "That username is taken."
    if err:
        return render_template("account.html", who=user_row(con), folk=people_list(con),
                               feed_url="", api_token="", error=err)
    uid = con.execute(
        "INSERT INTO users(username, display_name, pw_hash, is_admin, created_at)"
        " VALUES(?,?,?,0,?)",
        (username, display, generate_password_hash(pw),
         datetime.now().isoformat(timespec="seconds"))).lastrowid
    # everyone starts with the same three boxes, private until they share
    ensure_buckets(con, uid)
    my_inbox(con, uid)
    commit_retry(con)
    return render_template("account.html", who=user_row(con), folk=people_list(con),
                           feed_url="", api_token="",
                           ok="%s can sign in now. Their own board key is on their Account page."
                              % display)


# ---------- pulse ----------

@app.route("/pulse")
@app.route("/pulse/<day>")
@login_required
def pulse_view(day=None):
    """The week, measured. Nothing here asks him to record anything."""
    try:
        d = date.fromisoformat(day) if day else None
    except ValueError:
        d = None
    con = db()
    data = pulse.week(con, d, me())
    mon = date.fromisoformat(data["from"])
    pending = uset(con, "read_wanted") == data["from"]
    return render_template("pulse.html",
                           p=data, pending=pending,
                           prev_week=(mon - timedelta(days=7)).isoformat(),
                           next_week=(mon + timedelta(days=7)).isoformat(),
                           this_week=pulse.week_of()[0].isoformat())


@app.route("/api/pulse")
def api_pulse():
    """The week as plain text, for whatever writes the read on Friday.

    Deliberately not JSON: the fetch proxy mangles long encoded payloads, and a
    tab-separated page survives it.
    """
    if not _api_auth():
        abort(401)
    try:
        d = date.fromisoformat((request.args.get("day") or "").strip())
    except ValueError:
        d = None
    con = db()
    p = pulse.week(con, d, me())
    m, prev = p["movement"], p["previous"]
    L = ["WEEK\t%s to %s" % (p["from"], p["to"]),
         "MOVED\topened %d\tclosed %d\treopened %d\t(last week opened %d closed %d)"
         % (m["opened"], m["closed"], m["reopened"], prev["opened"], prev["closed"])]
    mt = p["meetings"]
    L.append("MEETINGS\t%d\t%s\tbusiest %s (%d)\tunmeasured %d"
             % (mt["count"], pulse.hours(mt["minutes_known"]) or "length unknown",
                mt["busiest_day"] or "-", mt["busiest_count"], mt["unmeasured"]))
    for r in p["closed"]:
        L.append("CLOSED\t%s\t%s\t%s" % (r["title"], r["section"],
                 ("%d days old" % r["age_days"]) if r["age_days"] is not None else "age unknown"))
    for r in p["opened"]:
        L.append("OPENED\t%s\t%s\t%s" % (r["title"], r["section"], r["status"]))
    for r in p["waiting"][:15]:
        L.append("WAITING\t%s\t%s\t%s" % (
            r["who"], r["title"],
            ("at least %d day%s" % (r["days"], "" if r["days"] == 1 else "s"))
            if (r["days"] and r["approx"])
            else ("%d day%s" % (r["days"], "" if r["days"] == 1 else "s"))
            if r["days"] is not None
            else "unknown (was waiting before tracking began)"))
    for r in p["stalled"][:15]:
        L.append("STALLED\t%s\t%s\t%d days quiet" % (r["title"], r["section"], r["quiet_days"]))
    for day_key, w in p["active"].items():
        L.append("ACTIVE\t%s\t%s to %s" % (day_key, w["first"], w["last"]))
    if not p["tracking_since"]:
        L.append("NOTE\tno history recorded yet")
    return "\n".join(L) or "(nothing)", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/pulse/refresh", methods=["POST"])
@login_required
def pulse_refresh():
    """Update now.

    Two things happen. The board writes a read from the numbers immediately, so
    the button does something the moment it is pressed. It also leaves a request
    for the sweep, which replaces it with the considered version - the app cannot
    summon that itself, and pretending otherwise would make the button a lie.
    """
    con = db()
    try:
        d = date.fromisoformat((request.form.get("week") or "").strip())
    except ValueError:
        d = None
    mon = pulse.week_of(d)[0]
    body = pulse.auto_read(pulse.week(con, d, me()))
    con.execute("INSERT INTO pulse_notes(week, user_id, body, created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(week, user_id) DO UPDATE SET body=excluded.body,"
                " created_at=excluded.created_at",
                (mon.isoformat(), me(), body,
                 datetime.now().isoformat(timespec="seconds")))
    uset_put(con, "read_wanted", mon.isoformat())
    commit_retry(con)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(body=body, pending=True)
    return redirect(url_for("pulse_view", day=mon.isoformat()))


@app.route("/api/seckind")
def api_section_kind():
    """Mark one of the acting person's own sections as a pipeline, or back."""
    if not _api_auth():
        abort(401)
    title = (request.args.get("title") or "").strip()
    if not title:
        return "ERROR: title required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    row = con.execute("SELECT id FROM sections WHERE lower(title)=lower(?) AND owner_id=?",
                      (title, me())).fetchone()
    if not row:
        return "NOT FOUND: " + title, 404, {"Content-Type": "text/plain; charset=utf-8"}
    kind = "pipeline" if (request.args.get("kind") or "").strip() == "pipeline" else "tasks"
    con.execute("UPDATE sections SET kind=? WHERE id=?", (kind, row["id"]))
    commit_retry(con)
    return "SET %s: %s" % (kind, title), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/secboard")
def api_section_board():
    """Put one of the acting person's sections on a board (or off, with board empty)."""
    if not _api_auth():
        abort(401)
    title = (request.args.get("title") or "").strip()
    if not title:
        return "ERROR: title required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    row = con.execute("SELECT id FROM sections WHERE lower(title)=lower(?) AND owner_id=?",
                      (title, me())).fetchone()
    b = canonical_board(request.args.get("board"))
    if not row:
        if (request.args.get("create") or "") != "1":
            return "NOT FOUND: " + title, 404, {"Content-Type": "text/plain; charset=utf-8"}
        # scaffolding: the block exists before its first task, private like any new section
        pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM sections").fetchone()[0]
        con.execute("INSERT INTO sections(title, pos, owner_id, visibility, board)"
                    " VALUES(?,?,?,'private',?)", (title, pos, me(), b))
        commit_retry(con)
        return "CREATED on %s: %s" % (b or "(none)", title), 200, \
            {"Content-Type": "text/plain; charset=utf-8"}
    con.execute("UPDATE sections SET board=? WHERE id=?", (b, row["id"]))
    commit_retry(con)
    return "BOARD %s: %s" % (b or "(none)", title), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/pulsereq")
def api_pulse_requests():
    """Who has asked for a written read, for the sweep to pick up.

    One line per person: USERNAME <tab> WEEK. NONE when nobody is waiting.
    """
    if not _api_auth():
        abort(401)
    con = db()
    out = []
    for u in people_list(con):
        week = uset(con, "read_wanted", u["id"])
        if week:
            out.append("%s\t%s" % (u["username"], week))
    return ("\n".join(out) or "NONE"), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/read")
def api_read():
    """Store the written half of the week - the part the numbers cannot say."""
    if not _api_auth():
        abort(401)
    body = (request.args.get("text") or "").strip()
    if not body:
        return "ERROR: text required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    try:
        d = date.fromisoformat((request.args.get("week") or "").strip())
    except ValueError:
        d = None
    mon = pulse.week_of(d)[0].isoformat()
    con = db()
    con.execute("INSERT INTO pulse_notes(week, user_id, body, created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(week, user_id) DO UPDATE SET body=excluded.body,"
                " created_at=excluded.created_at",
                (mon, me(), body, datetime.now().isoformat(timespec="seconds")))
    uset_del(con, ("read_wanted",))
    commit_retry(con)
    return "SAVED for week of " + mon, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ---------- api (for CRM integration) ----------

def _api_auth():
    tok = os.environ.get("API_TOKEN")
    if not tok:
        return False
    auth = request.headers.get("Authorization", "")
    supplied = auth[7:] if auth.startswith("Bearer ") else request.args.get("token", "")
    if not supplied:
        return False
    con = db()

    # A personal token IS the identity - it cannot be talked into being someone
    # else. This is what lets Joel's own sweep hold a token without that token
    # also being able to read Shimon's private board.
    row = con.execute("SELECT k FROM settings WHERE k LIKE 'u%:api_token' AND v=?",
                      (supplied,)).fetchone()
    if row:
        try:
            g.api_uid = int(row["k"].split(":")[0][1:])
        except (ValueError, IndexError):
            return False
        return True

    # The board-wide token is the admin's. Only it may act as somebody else, which
    # is how one task can serve several people. The parameter is "acting", NOT "who" -
    # "who" already means "who said this" on /api/note, and reusing it would turn
    # every note the sweep files into a 401.
    if not (tok and hmac.compare_digest(supplied, tok)):
        return False
    who = (request.args.get("acting") or "").strip().lower()
    if who:
        row = con.execute("SELECT id FROM users WHERE lower(username)=?", (who,)).fetchone()
        if not row:
            return False
    else:
        row = (con.execute("SELECT id FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
               or con.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone())
    g.api_uid = row["id"] if row else 0
    return True


def api_token_for(con, uid=None):
    """Each person's own key to their own board. Made on first sight."""
    uid = uid if uid is not None else me()
    tok = uset(con, "api_token", uid) or ""
    if not tok:
        tok = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        uset_put(con, "api_token", tok, uid)
        commit_retry(con)
    return tok


@app.route("/api/titles")
def api_titles():
    """Plain-text task list for the morning sweep: STATUS <tab> TITLE per line."""
    if not _api_auth():
        abort(401)
    con = db()
    where, args = sec_clause(con, "section_id", first=True)
    rows = con.execute("SELECT status, title FROM items" + where + " ORDER BY id",
                       args).fetchall()
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
    where, args = sec_clause(con, "section_id")
    cur = con.execute("DELETE FROM items WHERE lower(title)=lower(?)" + where,
                      [title] + args)
    con.execute("DELETE FROM sections WHERE id NOT IN (SELECT DISTINCT section_id FROM items)"
                " AND owner_id=?"
                " AND title NOT IN ('Joel / Shimon Tracker','Pinta / Office','Shul + Tzedaka',"
                "'Personal / Family','Inbox')", (me(),))
    commit_retry(con)
    return "REMOVED %d: %s" % (cur.rowcount, title), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/ev")
def api_event():
    """Upsert one Outlook calendar event (short params, proxy friendly)."""
    if not _api_auth():
        abort(401)
    key = (request.args.get("key") or request.args.get("k") or "").strip()
    subj = (request.args.get("subject") or request.args.get("s") or "").strip()
    day = (request.args.get("day") or request.args.get("d") or "").strip()
    if not (key and subj and day):
        return "ERROR: k, s and d required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    if con.execute("SELECT 1 FROM hidden_events WHERE ext_key=?", (key,)).fetchone() \
            and con.execute("SELECT 1 FROM events WHERE ext_key=? AND owner_id<>?",
                            (key, me())).fetchone() is None:
        return "SKIPPED (deleted here): " + subj, 200, {"Content-Type": "text/plain; charset=utf-8"}
    if con.execute("SELECT 1 FROM events WHERE ext_key=? AND source='manual'", (key,)).fetchone():
        return "SKIPPED (edited here): " + subj, 200, {"Content-Type": "text/plain; charset=utf-8"}
    con.execute(
        "INSERT INTO events(ext_key, subject, day, start_time, location, dur_min,"
        " owner_id, synced_at) VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(ext_key, owner_id) DO UPDATE SET"
        " subject=excluded.subject, day=excluded.day,"
        " start_time=excluded.start_time, location=excluded.location,"
        " dur_min=excluded.dur_min, owner_id=excluded.owner_id,"
        " synced_at=excluded.synced_at",
        (key, subj, day,
         (request.args.get("time") or request.args.get("t") or "").strip() or None,
         (request.args.get("loc") or request.args.get("l") or "").strip(),
         _int_or_none(request.args.get("mins")), me(),
         datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)
    return "SYNCED: " + subj, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/evclear")
def api_events_clear():
    """Drop synced events from a date forward, so a re-sync never duplicates or keeps cancellations."""
    if not _api_auth():
        abort(401)
    frm = (request.args.get("from") or datetime.now().date().isoformat()).strip()
    con = db()
    cur = con.execute("DELETE FROM events WHERE day >= ? AND owner_id=?"
                      " AND COALESCE(source,'outlook')='outlook'", (frm, me()))
    commit_retry(con)
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
    where, args = sec_clause(con, "section_id")
    cur = con.execute("UPDATE items SET waiting_on=? WHERE lower(trim(waiting_on))=lower(?)"
                      + where, [to, frm] + args)
    commit_retry(con)
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
    where, args = sec_clause(con, "section_id")
    row = con.execute("SELECT * FROM items WHERE lower(title)=lower(?)" + where,
                      [find] + args).fetchone()
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
    commit_retry(con)
    return "UPDATED: " + (title or row["title"]), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/notify")
def api_notify():
    """Send a push to Shimon's devices (used when email capture lands something new)."""
    if not _api_auth():
        abort(401)
    title = (request.args.get("title") or "Pinta HQ").strip()
    body = (request.args.get("body") or "").strip()
    if not body:
        return "ERROR: body required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    n = send_push(title, body, request.args.get("url") or "/", uid=me())
    return "PUSHED to %d device(s)" % n, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/brief")
def api_brief():
    """Post one line into the day's briefing (the sweep calls this per line)."""
    if not _api_auth():
        abort(401)
    text = (request.args.get("text") or "").strip()
    if not text:
        return "ERROR: text required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    day = (request.args.get("day") or _now_local().strftime("%Y-%m-%d")).strip()
    kind = (request.args.get("kind") or "note").strip().lower()
    if kind not in ("meeting", "email", "due", "added", "note", "update", "rec"):
        kind = "note"
    con = db()
    target = None
    tgt = (request.args.get("target") or "").strip()
    if tgt:
        row = _find_item(con, tgt)
        if not row:
            return "ERROR: no task matches " + tgt, 404, \
                {"Content-Type": "text/plain; charset=utf-8"}
        target = row["id"]
    dup = con.execute("SELECT 1 FROM brief_items WHERE day=? AND owner_id=?"
                      " AND lower(text)=lower(?)", (day, me(), text)).fetchone()
    if dup:
        return "SKIPPED (duplicate): " + text, 200, {"Content-Type": "text/plain; charset=utf-8"}
    con.execute("INSERT INTO brief_items(day, kind, text, detail, link, target_item_id,"
                " owner_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (day, kind, text, (request.args.get("detail") or "").strip(),
                 (request.args.get("link") or "").strip(), target, me(),
                 datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)
    return "BRIEFED: " + text, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/briefclear")
def api_brief_clear():
    """Wipe a day's briefing before re-posting it."""
    if not _api_auth():
        abort(401)
    day = (request.args.get("day") or _now_local().strftime("%Y-%m-%d")).strip()
    con = db()
    cur = con.execute("DELETE FROM brief_items WHERE day=? AND owner_id=? AND item_id IS NULL",
                      (day, me()))
    commit_retry(con)
    return "CLEARED %d from %s" % (cur.rowcount, day), 200, \
        {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/digest")
def api_digest():
    """Plain-text overdue / due-today digest for the morning sweep."""
    if not _api_auth():
        abort(401)
    today = datetime.now().date().isoformat()
    con = db()
    where, args = sec_clause(con, "items.section_id")
    rows = con.execute(
        "SELECT items.*, sections.title AS sec FROM items"
        " JOIN sections ON items.section_id = sections.id"
        " WHERE items.status != 'done' AND COALESCE(items.due_date,'') != ''" + where +
        " ORDER BY items.due_date", args).fetchall()
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
    # exact match first, then a forgiving prefix/substring match ("Joel" -> "Joel / Shimon Tracker"),
    # and only ever among the sections this account may write to
    where, args = sec_clause(con, "id")
    sec = con.execute("SELECT id FROM sections WHERE title=?" + where,
                      [sec_title] + args).fetchone()
    preset_pid = None
    if not sec and sec_title:
        # the old boxes live on as projects inside the three buckets - a sweep
        # still saying "Joel / Shimon Tracker" lands in that project
        prow = con.execute(
            "SELECT id, section_id FROM projects WHERE (title=? OR title LIKE ?)"
            + where.replace(" AND id", " AND section_id")
            + " ORDER BY (title=?) DESC, id LIMIT 1",
            [sec_title, "%" + sec_title + "%"] + args + [sec_title]).fetchone()
        if prow:
            sec = {"id": prow["section_id"]}
            preset_pid = prow["id"]
    if not sec and sec_title:
        sec = con.execute("SELECT id FROM sections WHERE title LIKE ?" + where
                          + " ORDER BY id LIMIT 1",
                          ["%" + sec_title + "%"] + args).fetchone()
    sid = sec["id"] if sec else con.execute(
        "INSERT INTO sections(title, pos, owner_id, visibility, board)"
        " VALUES(?,99,?,'private',?)",
        (sec_title, me(), canonical_board(request.args.get("board")))).lastrowid
    where, args = sec_clause(con, "section_id")
    dup = con.execute("SELECT 1 FROM items WHERE lower(title)=lower(?)" + where,
                      [title] + args).fetchone()
    if dup:
        return "SKIPPED (duplicate): " + title, 200, {"Content-Type": "text/plain; charset=utf-8"}
    pid = preset_pid
    proj = (request.args.get("project") or "").strip()
    if proj:
        prow = con.execute("SELECT id FROM projects WHERE section_id=? AND lower(title)=lower(?)",
                           (sid, proj)).fetchone()
        if prow:
            pid = prow["id"]
        else:
            ppos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM projects WHERE section_id=?",
                               (sid,)).fetchone()[0]
            pid = con.execute("INSERT INTO projects(section_id, title, pos, created_at)"
                              " VALUES(?,?,?,?)",
                              (sid, proj, ppos,
                               datetime.now().isoformat(timespec="seconds"))).lastrowid
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    star = 1 if (request.args.get("today") or "").strip() in ("1", "yes") else 0
    tenure = (request.args.get("tenure") or "").strip().lower()
    con.execute(
        "INSERT INTO items(section_id, project_id, title, note, waiting_on, status, pos,"
        " due_date, today, today_at, amount, ebitda, units, tenure, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, pid, title, request.args.get("note", ""), request.args.get("waiting_on", ""),
         "open", pos, request.args.get("due") or None,
         star, _now_local().isoformat(timespec="seconds") if star else None,
         parse_money(request.args.get("amount")), parse_money(request.args.get("ebitda")),
         request.args.get("units", type=int) or None,
         tenure if tenure in TENURES else "",
         datetime.now().isoformat(timespec="seconds")))
    new_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    key = thread_key(request.args.get("subject"))
    if key:
        con.execute("UPDATE items SET thread_key=? WHERE id=?", (key, new_id))
    chat = (request.args.get("wachat") or "").strip()
    if chat:
        con.execute("UPDATE items SET wa_chat_id=? WHERE id=?", (chat, new_id))
    commit_retry(con)
    return "ADDED: " + title + (" [%s]" % proj if proj else ""), 200, \
        {"Content-Type": "text/plain; charset=utf-8"}


# ---------- filing email traffic onto the task it belongs to ----------

_RE_PREFIX = re.compile(r"^\s*(?:re|fw|fwd|aw|antw|tr|rv|sv|vs)\s*(?:\[\d+\])?\s*:\s*", re.I)
# subjects too generic to identify anything
_WEAK_SUBJECTS = {"", "re", "fw", "fwd", "hi", "hello", "question", "update", "balance",
                  "invoice", "check", "checks", "payment", "thanks", "thank you", "info",
                  "follow up", "quick question", "documents", "docs", "please review"}


def thread_key(subject):
    """A stable id for an email conversation: the subject with every Re:/Fw: peeled off.

    Returns '' when the subject is too generic to identify anything - better to file
    nothing than to file it on the wrong task."""
    t = (subject or "").strip()
    for _ in range(8):
        t2 = _RE_PREFIX.sub("", t)
        if t2 == t:
            break
        t = t2
    t = re.sub(r"\s+", " ", t).strip().lower()
    if len(t) < 8 or t in _WEAK_SUBJECTS:
        return ""
    return t[:200]


def _find_item(con, needle):
    """Resolve a task by id, exact title, or a distinctive substring.

    Only ever within what the caller may see - otherwise a lucky guess at a title
    would file a note onto somebody else's private work."""
    n = (needle or "").strip()
    if not n:
        return None
    where, args = sec_clause(con, "section_id")
    if n.isdigit():
        return con.execute("SELECT * FROM items WHERE id=?" + where,
                           [int(n)] + args).fetchone()
    row = con.execute("SELECT * FROM items WHERE lower(title)=lower(?)" + where,
                      [n] + args).fetchone()
    if row:
        return row
    hits = con.execute("SELECT * FROM items WHERE lower(title) LIKE lower(?)" + where
                       + " ORDER BY status='done', id", ["%" + n + "%"] + args).fetchall()
    return hits[0] if hits else None


@app.route("/api/threads")
def api_threads():
    """Every task that is anchored to an email conversation, for the sweep to match against."""
    if not _api_auth():
        abort(401)
    con = db()
    where, args = sec_clause(con, "section_id")
    rows = con.execute(
        "SELECT id, thread_key, wa_chat_id, title FROM items"
        " WHERE status != 'done' AND ((thread_key IS NOT NULL AND thread_key != '')"
        "   OR (wa_chat_id IS NOT NULL AND wa_chat_id != ''))" + where
        + " ORDER BY id", args).fetchall()
    out = ["%d\t%s\t%s\t%s" % (r["id"], r["thread_key"] or "-",
                                 ("wa:" + r["wa_chat_id"]) if r["wa_chat_id"] else "-",
                                 r["title"]) for r in rows]
    return ("\n".join(out) or "NONE"), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/link")
def api_link():
    """Tie a task to an email conversation. Every later reply on it files itself."""
    if not _api_auth():
        abort(401)
    con = db()
    it = _find_item(con, request.args.get("find") or request.args.get("id"))
    if not it:
        return "ERROR: no such task", 404, {"Content-Type": "text/plain; charset=utf-8"}
    key = thread_key(request.args.get("subject"))
    if not key:
        return "SKIPPED (subject too generic): " + it["title"], 200, \
            {"Content-Type": "text/plain; charset=utf-8"}
    con.execute("UPDATE items SET thread_key=? WHERE id=?", (key, it["id"]))
    commit_retry(con)
    return "LINKED: %s <- %s" % (it["title"], key), 200, \
        {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/note")
def api_note():
    """File one line of email traffic onto a task as a response."""
    if not _api_auth():
        abort(401)
    text = (request.args.get("text") or "").strip()
    if not text:
        return "ERROR: text required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con = db()
    it = _find_item(con, request.args.get("find") or request.args.get("id"))
    if not it:
        return "ERROR: no such task", 404, {"Content-Type": "text/plain; charset=utf-8"}
    msg = (request.args.get("msg") or "").strip()
    if msg and con.execute("SELECT 1 FROM item_notes WHERE item_id=? AND ext_id=?",
                           (it["id"], msg)).fetchone():
        return "SKIPPED (already filed): " + it["title"], 200, \
            {"Content-Type": "text/plain; charset=utf-8"}
    who = (request.args.get("who") or "").strip()
    body = ("%s: %s" % (who, text)) if who else text
    src = (request.args.get("src") or "").strip().lower()
    if src not in ("email", "wa"):
        src = "wa" if request.args.get("wachat") else "email"
    now = _now_local().isoformat(timespec="seconds")
    con.execute("INSERT INTO item_notes(item_id, body, source, ext_id, created_at)"
                " VALUES(?,?,?,?,?)", (it["id"], body, src, msg or None, now))
    con.execute("UPDATE items SET updated_at=? WHERE id=?", (now, it["id"]))
    key = thread_key(request.args.get("subject"))
    if key and not it["thread_key"]:
        con.execute("UPDATE items SET thread_key=? WHERE id=?", (key, it["id"]))
    chat = (request.args.get("wachat") or "").strip()
    if chat and not it["wa_chat_id"]:
        con.execute("UPDATE items SET wa_chat_id=? WHERE id=?", (chat, it["id"]))
    commit_retry(con)
    # a sweep filing onto shared work should nudge the other person too - that is
    # the case where somebody genuinely wants to know without opening the app
    tell_others(con, it["id"], "notes", "New on %s" % _short(it["title"], 40),
                _short(body), "/#item-%d" % it["id"])
    return "FILED on %s: %s" % (it["title"], body[:60]), 200, \
        {"Content-Type": "text/plain; charset=utf-8"}


# ---------- WhatsApp, read-only, through TimelinesAI ----------

WA_LOOKBACK_HOURS = 26      # first ever run, and the safety net after an outage


@app.route("/wa/check")
def wa_check():
    """Is WhatsApp connected? Signed in, or with the API token."""
    if not (session.get("user") or _api_auth()):
        abort(401)
    con = db()
    st = {"token": bool(wa.TOKEN)}
    try:
        st["tables"] = sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'wa%'"))
    except Exception as e:
        st["tables_error"] = str(e)
    if wa.TOKEN:
        try:
            rows, more = wa.chats(page=1, ua=request.args.get("ua") or None)
            st["works"] = True
            st["detail"] = "Token accepted (%d chats on page 1%s)." % (
                len(rows), ", more pages" if more else "")
        except Exception as e:
            st["works"] = False
            st["detail"] = "TimelinesAI call failed: %s" % e
    else:
        st["works"] = False
        st["detail"] = "No TIMELINES_TOKEN set."
    for label, fn in (
        ("hook_url", lambda: url_for("wa_hook", secret=_wa_secret(con), _external=True)),
        ("inbox_waiting", lambda: con.execute(
            "SELECT COUNT(*) c FROM wa_inbox WHERE handled=0").fetchone()["c"]),
        ("inbox_total", lambda: con.execute(
            "SELECT COUNT(*) c FROM wa_inbox").fetchone()["c"]),
        ("last_hook_at", lambda: _setting_ro(con, "wa_last_hook", "")
            or "(no webhook has arrived yet)"),
        ("unreadable_payload_shape", lambda: _setting_ro(con, "wa_last_shape", "") or "-"),
        ("anchored_chats", lambda: con.execute(
            "SELECT COUNT(*) c FROM items WHERE wa_chat_id IS NOT NULL"
            " AND wa_chat_id != ''").fetchone()["c"]),
    ):
        try:
            st[label] = fn()
        except Exception as e:
            st[label] = "ERROR %s: %s" % (type(e).__name__, e)
    return jsonify(st)


@app.route("/api/walink")
def api_walink():
    """Tie a task to a WhatsApp chat. Everything later in that chat files itself."""
    if not _api_auth():
        abort(401)
    con = db()
    it = _find_item(con, request.args.get("find") or request.args.get("id"))
    if not it:
        return "ERROR: no such task", 404, {"Content-Type": "text/plain; charset=utf-8"}
    chat = (request.args.get("wachat") or "").strip()
    if not chat:
        return "ERROR: wachat required", 400, {"Content-Type": "text/plain; charset=utf-8"}
    con.execute("UPDATE items SET wa_chat_id=? WHERE id=?", (chat, it["id"]))
    commit_retry(con)
    return "LINKED: %s <- chat %s" % (it["title"], chat), 200, \
        {"Content-Type": "text/plain; charset=utf-8"}


def _shape(v, depth=0):
    """Structure only - key names and types, never a single value."""
    if depth > 3:
        return "..."
    if isinstance(v, dict):
        return dict((k, _shape(val, depth + 1)) for k, val in list(v.items())[:25])
    if isinstance(v, list):
        return ["list[%d]" % len(v)] + ([_shape(v[0], depth + 1)] if v else [])
    return type(v).__name__


@app.route("/api/washape")
def api_washape():
    """Field NAMES only, never values - so the payload shape can be checked
    without any of Shimon's WhatsApp content leaving the server."""
    if not (session.get("user") or _api_auth()):
        abort(401)
    out = {}
    try:
        rows = wa.all_chats()
        out["chat_count"] = len(rows)
        out["chat_keys"] = sorted(rows[0].keys()) if rows else []
        stamped = [c for c in rows if c.get("last_message_timestamp")]
        out["chats_with_a_timestamp"] = len(stamped)
        out["newest_timestamp"] = max(
            [c["last_message_timestamp"] for c in stamped], default=None)
        # probe the most recently active chat, not whichever happens to be first
        pick = max(stamped, key=lambda c: c["last_message_timestamp"]) if stamped else \
            (rows[0] if rows else None)
        cid = pick.get("id") if pick else None
        out["id_field"] = "id" if cid is not None else None
        if cid is None:
            return jsonify(out)
        try:
            raw = wa._get("/chats/%s/messages" % cid, {"sorting_order": "desc"})
            out["msg_envelope"] = _shape(raw)
            msgs, _m = wa._rows(raw)
            out["message_count"] = len(msgs)
            out["message_keys"] = sorted(msgs[0].keys()) if msgs else []
        except Exception as e:
            out["messages_error"] = "%s: %s" % (type(e).__name__, e)
        try:
            out["chat_envelope"] = _shape(wa._get("/chats", {"page": 1}))
        except Exception as e:
            out["chat_envelope_error"] = str(e)
    except Exception as e:
        out["chats_error"] = "%s: %s" % (type(e).__name__, e)
    return jsonify(out)


def _wa_secret(con):
    """The unguessable bit of the webhook URL, made once and kept."""
    v = _setting_ro(con, "wa_hook_secret", "")
    if not v:
        v = uuid.uuid4().hex
        con.execute("INSERT INTO settings(k, v) VALUES('wa_hook_secret', ?)"
                    " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (v,))
        commit_retry(con)
    return v


@app.route("/hooks/wa/<secret>", methods=["POST", "GET"])
def wa_hook(secret):
    """TimelinesAI posts every new message here the moment it lands.

    No polling, no paging a contact list of thousands - and the secret in the path
    is the only thing that lets anyone write to it."""
    con = db()
    if secret != _wa_secret(con):
        abort(404)
    if request.method == "GET":
        return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}
    payload = request.get_json(silent=True)
    if payload is None:
        try:
            payload = json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            payload = {}
    now = _now_local().isoformat(timespec="seconds")
    con.execute("INSERT INTO settings(k, v) VALUES('wa_last_hook', ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (now,))
    row = wa.parse_hook(payload)
    if not row:
        # remember the SHAPE of anything we could not read - names and types only,
        # never values - so the parser can be taught the real payload
        try:
            con.execute("INSERT INTO settings(k, v) VALUES('wa_last_shape', ?)"
                        " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (json.dumps(_shape(payload))[:4000],))
        except Exception:
            pass
        commit_retry(con)
        return jsonify(ok=True, stored=False)
    con.execute(
        "INSERT INTO wa_inbox(uid, chat_id, chat_name, sender, from_me, text, ts,"
        " received_at, handled) VALUES(?,?,?,?,?,?,?,?,0)"
        " ON CONFLICT(uid) DO NOTHING",
        (row["uid"], row["chat_id"], row["chat_name"], row["sender"],
         row["from_me"], row["text"], row["ts"] or now, now))
    con.execute("DELETE FROM wa_inbox WHERE handled=1 AND received_at < ?",
                ((_now_local() - __import__("datetime").timedelta(days=30))
                 .isoformat(timespec="seconds"),))
    ok = commit_retry(con)
    return jsonify(ok=ok, stored=ok), (200 if ok else 503)


@app.route("/api/wafeed")
def api_wafeed():
    """Everything WhatsApp has delivered and nobody has dealt with yet.

    Reads the webhook inbox, not the API, so it is instant and costs nothing.
    Each chat is labelled with the task it is already tied to, so the sweep knows
    which lines file themselves and which are new ground."""
    if not _api_auth():
        abort(401)
    con = db()
    try:
        limit = max(1, min(int(request.args.get("max") or 120), 400))
    except ValueError:
        limit = 120
    rows = con.execute(
        "SELECT * FROM wa_inbox WHERE handled=0 ORDER BY ts, rowid LIMIT ?",
        (limit,)).fetchall()
    if not rows:
        hooked = _setting_ro(con, "wa_last_hook", "")
        return ("(nothing new)\nLAST HOOK\t" + (hooked or "none yet - webhook not set up"),
                200, {"Content-Type": "text/plain; charset=utf-8"})

    linked = {}
    for r in con.execute("SELECT id, wa_chat_id, title FROM items"
                         " WHERE wa_chat_id IS NOT NULL AND wa_chat_id != ''"):
        linked[str(r["wa_chat_id"])] = (r["id"], r["title"])

    by_chat = {}
    for r in rows:
        by_chat.setdefault(str(r["chat_id"]), []).append(r)

    out = []
    for cid, msgs in by_chat.items():
        tie = linked.get(cid)
        out.append("")
        out.append("CHAT\t%s\t%s\t%s" % (
            cid, msgs[0]["chat_name"] or "?",
            ("task %d: %s" % tie) if tie else "-"))
        for m in msgs:
            who = "You" if m["from_me"] else (m["sender"] or "?")
            txt = (m["text"] or "").replace("\n", " ").strip()
            if len(txt) > 220:
                txt = txt[:220].rsplit(" ", 1)[0] + "\u2026"
            out.append("  %s\t%s  %s: %s" % (
                m["uid"], (m["ts"] or "")[:16].replace("T", " "), who, txt))
    out.append("")
    out.append("UIDS\t" + ",".join(r["uid"] for r in rows))
    return "\n".join(out), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/waseen")
def api_waseen():
    """Tick off the messages the sweep has dealt with, so they never come back."""
    if not _api_auth():
        abort(401)
    uids = [u.strip() for u in (request.args.get("uids") or "").split(",") if u.strip()]
    con = db()
    if uids:
        con.executemany("UPDATE wa_inbox SET handled=1 WHERE uid=?", [(u,) for u in uids])
        commit_retry(con)
        return "MARKED %d" % len(uids), 200, {"Content-Type": "text/plain; charset=utf-8"}
    if (request.args.get("all") or "").strip() == "1":
        cur = con.execute("UPDATE wa_inbox SET handled=1 WHERE handled=0")
        commit_retry(con)
        return "MARKED %d (all)" % cur.rowcount, 200, \
            {"Content-Type": "text/plain; charset=utf-8"}
    return "ERROR: uids or all=1 required", 400, \
        {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/board")
def api_board():
    if not _api_auth():
        abort(401)
    con = db()
    out = []
    where, args = sec_clause(con, "id", first=True)
    for s in con.execute("SELECT * FROM sections" + where + " ORDER BY pos, id", args):
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
    where, args = sec_clause(con, "id")
    sec = con.execute("SELECT id FROM sections WHERE title=?" + where,
                      [d.get("section", "Inbox")] + args).fetchone()
    sid = sec["id"] if sec else con.execute(
        "INSERT INTO sections(title, pos, owner_id, visibility) VALUES(?,99,?,'private')",
        (d.get("section", "Inbox"), me())).lastrowid
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM items WHERE section_id=?",
                      (sid,)).fetchone()[0]
    cur = con.execute(
        "INSERT INTO items(section_id, title, note, waiting_on, status, pos, due_date, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (sid, title, d.get("note", ""), d.get("waiting_on", ""), "open", pos,
         d.get("due_date"), datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)
    return jsonify(id=cur.lastrowid), 201


@app.route("/sections/add", methods=["POST"])
@login_required
def add_section():
    title = (request.form.get("title") or "").strip()
    if title:
        con = db()
        pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM sections").fetchone()[0]
        shared = 'shared' if request.form.get("shared") else 'private'
        con.execute("INSERT INTO sections(title, pos, owner_id, visibility, board)"
                    " VALUES(?,?,?,?,?)",
                    (title, pos, me(), shared,
                     canonical_board(request.form.get("board"))))
        commit_retry(con)
    return redirect(url_for("board"))


@app.route("/sections/<int:sec_id>/delete", methods=["POST"])
@login_required
def delete_section(sec_id):
    """Only the owner can remove a section - sharing it does not give it away."""
    con = db()
    own = con.execute("SELECT 1 FROM sections WHERE id=? AND owner_id=?",
                      (sec_id, me())).fetchone()
    if not own:
        abort(404)
    n = con.execute("SELECT COUNT(*) FROM items WHERE section_id=? AND status!='done'",
                    (sec_id,)).fetchone()[0]
    if n == 0:
        con.execute("DELETE FROM sections WHERE id=?", (sec_id,))
        commit_retry(con)
    return redirect(url_for("board"))


@app.route("/sections/<int:sec_id>/board", methods=["POST"])
@login_required
def set_section_board(sec_id):
    con = db()
    if not con.execute("SELECT 1 FROM sections WHERE id=? AND owner_id=?",
                       (sec_id, me())).fetchone():
        abort(404)
    b = canonical_board(request.form.get("board"))
    con.execute("UPDATE sections SET board=? WHERE id=?", (b, sec_id))
    commit_retry(con)
    return redirect(url_for("board", b=b or None))


@app.route("/sections/<int:sec_id>/color", methods=["POST"])
@login_required
def cycle_section_color(sec_id):
    """Tap the section's dot, get the next color. No picker, no dialog."""
    con = db()
    row = con.execute("SELECT id, color FROM sections WHERE id=? AND owner_id=?",
                      (sec_id, me())).fetchone()
    if not row:
        abort(404)
    cur = sec_color(row)
    i = SECTION_PALETTE.index(cur) if cur in SECTION_PALETTE else -1
    nxt = SECTION_PALETTE[(i + 1) % len(SECTION_PALETTE)]
    con.execute("UPDATE sections SET color=? WHERE id=?", (nxt, sec_id))
    commit_retry(con)
    return jsonify(color=nxt)


@app.route("/items/<int:item_id>/top", methods=["POST"])
@login_required
def bump_to_top(item_id):
    """The thing that just became urgent goes first, one tap."""
    con = db()
    require_item(con, item_id)
    row = con.execute("SELECT section_id FROM items WHERE id=?", (item_id,)).fetchone()
    low = con.execute("SELECT COALESCE(MIN(pos),0)-1 FROM items WHERE section_id=?",
                      (row["section_id"],)).fetchone()[0]
    con.execute("UPDATE items SET pos=?, updated_at=? WHERE id=?",
                (low, datetime.now().isoformat(timespec="seconds"), item_id))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/items/<int:item_id>/pin", methods=["POST"])
@login_required
def pin_item(item_id):
    """Keep-style pin: it stays at the very top of its box until unpinned."""
    con = db()
    require_item(con, item_id)
    now = con.execute("SELECT pinned FROM items WHERE id=?", (item_id,)).fetchone()[0]
    con.execute("UPDATE items SET pinned=? WHERE id=?", (0 if now else 1, item_id))
    commit_retry(con)
    return jsonify(pinned=0 if now else 1)


@app.route("/items/<int:item_id>/arch", methods=["POST"])
@login_required
def archive_item(item_id):
    """Out of sight without being gone: off every list, waiting in the Archive."""
    con = db()
    require_item(con, item_id)
    now = con.execute("SELECT archived FROM items WHERE id=?", (item_id,)).fetchone()[0]
    con.execute("UPDATE items SET archived=? WHERE id=?", (0 if now else 1, item_id))
    commit_retry(con)
    return jsonify(archived=0 if now else 1)


@app.route("/projects/<int:proj_id>/arch", methods=["POST"])
@login_required
def archive_project(proj_id):
    """Archive a whole project - the box and everything in it - or bring it back."""
    con = db()
    row = con.execute("SELECT section_id, archived FROM projects WHERE id=?",
                      (proj_id,)).fetchone()
    if not row:
        abort(404)
    require_section(con, row["section_id"])
    nxt = 0 if row["archived"] else 1
    con.execute("UPDATE projects SET archived=? WHERE id=?", (nxt, proj_id))
    con.execute("UPDATE items SET archived=? WHERE project_id=?", (nxt, proj_id))
    commit_retry(con)
    return redirect(url_for("archive_view") if nxt == 0 else url_for("board"))


@app.route("/archive")
@login_required
def archive_view():
    """Everything put away, still searchable, one tap from coming back."""
    con = db()
    where, args = sec_clause(con, "items.section_id")
    items = con.execute(
        "SELECT items.*, sections.title AS sec_title, COALESCE(p.title,'') AS proj_title"
        " FROM items JOIN sections ON sections.id = items.section_id"
        " LEFT JOIN projects p ON p.id = items.project_id"
        " WHERE items.archived=1"
        " AND (items.project_id IS NULL OR items.project_id NOT IN"
        "      (SELECT id FROM projects WHERE archived=1))" + where +
        " ORDER BY sections.pos, sections.id, items.id DESC", args).fetchall()
    where, args = sec_clause(con, "projects.section_id")
    projs = con.execute(
        "SELECT projects.*, sections.title AS sec_title,"
        " (SELECT COUNT(*) FROM items i WHERE i.project_id = projects.id"
        "  AND i.status != 'done') AS n_open,"
        " (SELECT COUNT(*) FROM items i WHERE i.project_id = projects.id) AS n_all"
        " FROM projects JOIN sections ON sections.id = projects.section_id"
        " WHERE projects.archived=1" + where +
        " ORDER BY sections.pos, sections.id, projects.id DESC",
        args).fetchall()
    # one lane per bucket, in board order, projects first and loose tasks after
    lanes = {}
    for p in projs:
        lanes.setdefault(p["sec_title"], {"projs": [], "items": []})["projs"].append(p)
    for it in items:
        lanes.setdefault(it["sec_title"], {"projs": [], "items": []})["items"].append(it)
    return render_template("archive.html", items=items, projs=projs, lanes=lanes)


@app.route("/items/<int:item_id>/checks", methods=["POST"])
@login_required
def add_check(item_id):
    """A checkbox inside a task - Keep's checklists, living on the row."""
    con = db()
    require_item(con, item_id)
    body = (request.form.get("body") or "").strip()[:200]
    if not body:
        return jsonify(error="empty"), 400
    pos = con.execute("SELECT COALESCE(MAX(pos),0)+1 FROM checks WHERE item_id=?",
                      (item_id,)).fetchone()[0]
    cid = con.execute("INSERT INTO checks(item_id, body, pos) VALUES(?,?,?)",
                      (item_id, body, pos)).lastrowid
    commit_retry(con)
    return jsonify(id=cid, body=body)


@app.route("/checks/<int:check_id>/toggle", methods=["POST"])
@login_required
def toggle_check(check_id):
    con = db()
    row = con.execute("SELECT item_id, done FROM checks WHERE id=?", (check_id,)).fetchone()
    if not row:
        abort(404)
    require_item(con, row["item_id"])
    con.execute("UPDATE checks SET done=? WHERE id=?", (0 if row["done"] else 1, check_id))
    commit_retry(con)
    return jsonify(done=0 if row["done"] else 1)


@app.route("/checks/<int:check_id>/delete", methods=["POST"])
@login_required
def delete_check(check_id):
    con = db()
    row = con.execute("SELECT item_id FROM checks WHERE id=?", (check_id,)).fetchone()
    if not row:
        abort(404)
    require_item(con, row["item_id"])
    con.execute("DELETE FROM checks WHERE id=?", (check_id,))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/items/<int:item_id>/move", methods=["POST"])
@login_required
def move_item(item_id):
    """Drag a task into another box - or another project - and it lives there now.

    Both ends are checked: the task must be one you may touch, the destination
    a section you can see. before_id says which task it should sit above;
    the whole destination list is renumbered so positions stay honest.
    """
    con = db()
    require_item(con, item_id)
    try:
        sec_id = int(request.form.get("section_id") or 0)
    except ValueError:
        abort(400)
    require_section(con, sec_id)
    proj_raw = (request.form.get("project_id") or "").strip()
    proj_id = None
    if proj_raw.isdigit():
        p = con.execute("SELECT id FROM projects WHERE id=? AND section_id=?",
                        (int(proj_raw), sec_id)).fetchone()
        if not p:
            abort(400)
        proj_id = p["id"]
    before_raw = (request.form.get("before_id") or "").strip()
    rows = [r["id"] for r in con.execute(
        "SELECT id FROM items WHERE section_id=? AND id!=? ORDER BY pos, id",
        (sec_id, item_id))]
    if before_raw.isdigit() and int(before_raw) in rows:
        rows.insert(rows.index(int(before_raw)), item_id)
    else:
        rows.append(item_id)
    con.execute("UPDATE items SET section_id=?, project_id=?, updated_at=? WHERE id=?",
                (sec_id, proj_id,
                 datetime.now().isoformat(timespec="seconds"), item_id))
    for pos, iid in enumerate(rows):
        con.execute("UPDATE items SET pos=? WHERE id=?", (pos, iid))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/sections/order", methods=["POST"])
@login_required
def order_sections():
    """The arrangement is yours alone - dragging your boxes never moves anyone
    else's view of the same shared section."""
    raw = (request.form.get("ids") or "")[:2000]
    ids = [x for x in raw.split(",") if x.strip().isdigit()]
    con = db()
    uset_put(con, "secorder", ",".join(ids[:200]))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/sections/<int:sec_id>/fold", methods=["POST"])
@login_required
def fold_section(sec_id):
    con = db()
    require_section(con, sec_id)
    cur = {x for x in (uset(con, "collapsed") or "").split(",") if x.strip().isdigit()}
    key = str(sec_id)
    folded = key not in cur
    (cur.add if folded else cur.discard)(key)
    uset_put(con, "collapsed", ",".join(sorted(cur)))
    commit_retry(con)
    return jsonify(folded=folded)


@app.route("/projects/<int:proj_id>/fold", methods=["POST"])
@login_required
def fold_project(proj_id):
    """A project folds to its title line - your view only, like section folds."""
    con = db()
    row = con.execute("SELECT section_id FROM projects WHERE id=?", (proj_id,)).fetchone()
    if not row:
        abort(404)
    require_section(con, row["section_id"])
    cur = {x for x in (uset(con, "pcollapsed") or "").split(",") if x.strip().isdigit()}
    key = str(proj_id)
    folded = key not in cur
    (cur.add if folded else cur.discard)(key)
    uset_put(con, "pcollapsed", ",".join(sorted(cur)))
    commit_retry(con)
    return jsonify(folded=folded)


@app.route("/sections/<int:sec_id>/share", methods=["POST"])
@login_required
def share_section(sec_id):
    """Who may see this section: nobody, everyone, or exactly these people."""
    con = db()
    if not con.execute("SELECT 1 FROM sections WHERE id=? AND owner_id=?",
                       (sec_id, me())).fetchone():
        abort(404)
    scope = (request.form.get("scope") or "").strip()
    con.execute("DELETE FROM section_shares WHERE section_id=?", (sec_id,))
    if scope == "all":
        con.execute("UPDATE sections SET visibility='shared' WHERE id=?", (sec_id,))
    else:
        uids = [u for u in request.form.getlist("uids", type=int) if u and u != me()]
        uids = [u for u in uids if con.execute("SELECT 1 FROM users WHERE id=?",
                                               (u,)).fetchone()]
        for u in uids:
            con.execute("INSERT OR IGNORE INTO section_shares(section_id, user_id)"
                        " VALUES(?,?)", (sec_id, u))
        con.execute("UPDATE sections SET visibility=? WHERE id=?",
                    ("some" if uids else "private", sec_id))
    commit_retry(con)
    return redirect(url_for("board", _anchor="sec-%d" % sec_id))


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
        commit_retry(con)
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
    con.execute("INSERT OR REPLACE INTO push_subs(endpoint, p256dh, auth, user_id, created_at)"
                " VALUES(?,?,?,?,?)",
                (d["endpoint"], keys["p256dh"], keys.get("auth", ""), me(),
                 datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)
    return jsonify(ok=True)


@app.route("/push/test", methods=["POST"])
@login_required
def push_test():
    n = send_push("HQ", "Notifications are on. This is what a reminder looks like.",
                  uid=me())
    return jsonify(sent=n)


def send_push(title, body, url="/", uid=None):
    """Fire one notification to a person's devices. Returns how many got it.

    uid=None means everyone, which is only ever right for a board-wide message -
    a reminder about somebody's task must name them or it goes to both phones."""
    try:
        import webpush_lite
    except Exception as e:
        app.logger.warning("push module missing: %s", e)
        return 0
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    subs = (con.execute("SELECT * FROM push_subs WHERE user_id=?", (uid,)).fetchall()
            if uid else con.execute("SELECT * FROM push_subs").fetchall())
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
                commit_retry(con)
            elif 200 <= status < 300:
                sent += 1
            else:
                app.logger.warning("push endpoint returned %s", status)
        except Exception as e:
            app.logger.warning("push failed: %s", e)
    con.close()
    return sent


def _claim(con, ref):
    """Claim the right to send this one notification, or find it already taken.

    With more than one worker process the reminder loop runs in each of them.
    Checking and then sending leaves a gap where both see nothing and both push,
    and two buzzes for the same meeting is exactly the kind of thing that makes
    an app feel broken. The insert IS the claim: whoever writes the row sends.
    """
    cur = con.execute("INSERT OR IGNORE INTO reminders_sent(ref, sent_at) VALUES(?,?)",
                      (ref, datetime.now().isoformat(timespec="seconds")))
    if not cur.rowcount:
        return False
    commit_retry(con)
    return True


def _already_sent(con, ref):
    return con.execute("SELECT 1 FROM reminders_sent WHERE ref=?", (ref,)).fetchone() is not None


def _mark_sent(con, ref):
    con.execute("INSERT OR REPLACE INTO reminders_sent(ref, sent_at) VALUES(?,?)",
                (ref, datetime.now().isoformat(timespec="seconds")))
    commit_retry(con)


def reminder_tick():
    """One pass: task reminders due, meetings starting soon, the morning digest."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    now = _now_local()
    now_s = now.strftime("%Y-%m-%dT%H:%M")
    today = now.strftime("%Y-%m-%d")

    # Everything below is per person: one shared "already sent" marker would let the
    # first phone silence the second, and a reminder must reach whose task it is.
    for _u in con.execute("SELECT id FROM users ORDER BY id"):
      uid = _u["id"]
      vis = [r[0] for r in con.execute(
          "SELECT id FROM sections WHERE " + VISIBLE_SQL, (uid, uid))]
      inq = ",".join("?" * len(vis)) if vis else "NULL"

      # 1. per-task reminders that have come due (within the last 2 hours, so a restart never misses one)
      from datetime import timedelta as _td
      back = (now - _td(hours=2)).strftime("%Y-%m-%dT%H:%M")
      for it in con.execute(
              "SELECT * FROM items WHERE remind_at IS NOT NULL AND remind_at != ''"
              " AND archived=0"
              " AND remind_at <= ? AND remind_at >= ? AND status != 'done'"
              " AND section_id IN (%s)" % inq, [now_s, back] + vis):
        ref = "u%d:item:%d:%s" % (uid, it["id"], it["remind_at"])
        if not _claim(con, ref):
            continue
        body = it["title"]
        if it["waiting_on"]:
            body += "  (waiting on %s)" % it["waiting_on"]
        send_push("Reminder", body, "/#item-%d" % it["id"], uid=uid)

      # 2. meetings starting in the next 15 minutes
      soon = (now + _td(minutes=15)).strftime("%H:%M")
      for e in con.execute(
              "SELECT * FROM events WHERE day=? AND owner_id=? AND start_time IS NOT NULL"
              " AND start_time > ? AND start_time <= ?",
              (today, uid, now.strftime("%H:%M"), soon)):
        ref = "u%d:ev:%s:%s" % (uid, e["ext_key"], e["day"])
        if not _claim(con, ref):
            continue
        body = "%s starts %s" % (e["subject"], e["start_time"])
        if e["location"]:
            body += "  ·  " + e["location"]
        send_push("Coming up", body, "/calendar", uid=uid)

      # 2b. "leave now" - only when there is a key and a from-address to measure from
      home, work = _origins(con, uid)
      if maps.KEY and (home or work):
        ahead = (now + _td(minutes=120)).strftime("%H:%M")
        day_evs = con.execute(
            "SELECT * FROM events WHERE day=? AND owner_id=?"
            " ORDER BY COALESCE(start_time,'99:99'), id", (today, uid)).fetchall()
        for i, e in enumerate(day_evs):
            hhmm = (e["start_time"] or "")[:5]
            if not hhmm or hhmm <= now.strftime("%H:%M") or hhmm > ahead:
                continue
            if not maps.is_place(e["location"]):
                continue
            ref = "u%d:leave:%s:%s" % (uid, e["ext_key"] or e["id"], e["day"])
            if not _claim(con, ref):
                continue
            src, _label = origin_for(day_evs, i, home, work, live_fix(con, uid=uid),
                                     now.strftime("%H:%M"))
            if not src:
                continue
            secs = maps.drive_seconds(src, e["location"],
                                      _depart_utc(e["day"], e["start_time"]))
            if not secs:
                continue
            leave = (datetime.strptime(e["start_time"][:5], "%H:%M")
                     - _td(seconds=secs) - _td(minutes=BUFFER_MIN)).strftime("%H:%M")
            # fire in the two minutes around leave time, or straight away if we are late
            if now.strftime("%H:%M") < leave:
                continue
            send_push("Leave now",
                      "%s at %s  -  %s drive" % (e["subject"], fmt12(e["start_time"]),
                                                 maps.pretty_minutes(secs)),
                      "/day/" + e["day"], uid=uid)

      # 3. one morning digest at 8am on weekdays
      if now.weekday() < 5 and "08:00" <= now.strftime("%H:%M") < "09:00":
        ref = "u%d:digest:%s" % (uid, today)
        if _claim(con, ref):
            due = con.execute(
                "SELECT COUNT(*) c FROM items WHERE status != 'done' AND due_date = ?"
                " AND section_id IN (%s)" % inq, [today] + vis).fetchone()["c"]
            over = con.execute(
                "SELECT COUNT(*) c FROM items WHERE status != 'done'"
                " AND COALESCE(due_date,'') != '' AND due_date < ?"
                " AND section_id IN (%s)" % inq, [today] + vis).fetchone()["c"]
            meetings = con.execute(
                "SELECT COUNT(*) c FROM events WHERE day = ? AND owner_id = ?",
                (today, uid)).fetchone()["c"]
            bits = []
            if meetings:
                bits.append("%d meeting%s" % (meetings, "" if meetings == 1 else "s"))
            if due:
                bits.append("%d due today" % due)
            if over:
                bits.append("%d overdue" % over)
            if bits:
                send_push("Today", "  ·  ".join(bits), "/calendar", uid=uid)

    con.execute("DELETE FROM reminders_sent WHERE sent_at < ?",
                ((now - _td(days=14)).isoformat(timespec="seconds"),))
    commit_retry(con)
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
