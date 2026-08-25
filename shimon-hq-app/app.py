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
        "INSERT INTO items(section_id, title, note, waiting_on, status, pos, due_date, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (sid, title, (request.form.get("note") or "").strip(),
         (request.form.get("waiting_on") or "").strip(), "open", pos,
         (request.form.get("due_date") or "").strip() or None,
         datetime.now().isoformat(timespec="seconds")))
    con.commit()
    return redirect(url_for("board", _anchor="sec-%d" % sid))


@app.route("/items/<int:item_id>/edit", methods=["POST"])
@login_required
def edit_item(item_id):
    title = (request.form.get("title") or "").strip()
    if not title:
        return redirect(url_for("board"))
    con = db()
    con.execute(
        "UPDATE items SET title=?, note=?, waiting_on=?, due_date=?, updated_at=? WHERE id=?",
        (title, (request.form.get("note") or "").strip(),
         (request.form.get("waiting_on") or "").strip(),
         (request.form.get("due_date") or "").strip() or None,
         datetime.now().isoformat(timespec="seconds"), item_id))
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


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
