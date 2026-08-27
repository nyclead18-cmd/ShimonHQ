"""What actually moved, and what did not.

Every number here comes from history the board records for itself - no stopwatch,
no timesheet, nothing to remember to press. The cost of that is honesty about
what cannot be known: hours at a desk are not measurable this way, so this module
never invents them. It reports meetings, which are real, and movement, which is
real, and is explicit everywhere else about what it is guessing.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None

TZ_NAME = "America/New_York"
STALE_DAYS = 14           # open, untouched this long, and it is not really open


def _ny():
    return ZoneInfo(TZ_NAME) if ZoneInfo else timezone.utc


def now_utc():
    return datetime.now(timezone.utc)


def stamp(dt):
    """The exact format the SQLite triggers write, so comparisons are plain strings."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(raw):
    """Any timestamp this database holds -> an aware UTC datetime, or None.

    Three formats are in circulation: trigger rows ending in Z, responses
    carrying a New York offset, and older naive rows written in the server's
    UTC clock. A naive value is treated as UTC, which is what it always was.
    """
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def today_ny():
    return datetime.now(_ny()).date()


def week_of(d=None):
    """The Monday-to-Sunday week containing `d`, as New York dates."""
    d = d or today_ny()
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _bounds(day_from, day_to):
    """New York dates -> the UTC string window covering them, end exclusive."""
    ny = _ny()
    a = datetime.combine(day_from, datetime.min.time(), tzinfo=ny)
    b = datetime.combine(day_to + timedelta(days=1), datetime.min.time(), tzinfo=ny)
    return stamp(a), stamp(b)


def _q(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []


# ---------- whose board ----------
# Movement is counted from item_events, which has no section of its own - it
# records the task id and nothing else. So every query here joins back through
# items to sections. A task that has since been deleted has no section any more;
# it is left out rather than counted for everybody.

def scope(con, uid):
    """(sql fragment, params) limiting item_events rows to this person's board."""
    ids = [r[0] for r in _q(con, "SELECT id FROM sections"
                                 " WHERE owner_id=? OR visibility='shared'", (uid,))]
    if not ids:
        return " AND 0=1", []
    return (" AND e.item_id IN (SELECT id FROM items WHERE section_id IN (%s))"
            % ",".join("?" * len(ids))), ids


def _ids(con, uid):
    return [r[0] for r in _q(con, "SELECT id FROM sections"
                                  " WHERE owner_id=? OR visibility='shared'", (uid,))]


def _inq(con, uid, col="i.section_id"):
    ids = _ids(con, uid)
    if not ids:
        return " AND 0=1", []
    return " AND %s IN (%s)" % (col, ",".join("?" * len(ids))), ids


# ---------- movement ----------

def movement(con, day_from, day_to, uid=0):
    """Opened, closed and reopened inside the window.

    Counted per event, not per task: a task closed on Tuesday and reopened on
    Thursday shows in both columns, because both things happened.
    """
    a, b = _bounds(day_from, day_to)
    sc, sa = scope(con, uid)
    opened = _q(con, "SELECT COUNT(*) FROM item_events e"
                     " WHERE kind='created' AND at>=? AND at<?" + sc, [a, b] + sa)
    closed = _q(con, "SELECT COUNT(*) FROM item_events e"
                     " WHERE kind='status' AND new='done' AND at>=? AND at<?" + sc,
                [a, b] + sa)
    reopened = _q(con, "SELECT COUNT(*) FROM item_events e"
                       " WHERE kind='status' AND old='done' AND new<>'done'"
                       " AND at>=? AND at<?" + sc, [a, b] + sa)
    return {"opened": opened[0][0] if opened else 0,
            "closed": closed[0][0] if closed else 0,
            "reopened": reopened[0][0] if reopened else 0}


def closed_items(con, day_from, day_to, uid=0):
    """What was finished, newest first, with how long each had been on the board.

    `age_days` is None for anything that predates the history table - guessing
    would make old work look freshly done.
    """
    a, b = _bounds(day_from, day_to)
    sc, sa = scope(con, uid)
    rows = _q(con,
              "SELECT e.item_id, e.title, e.at, s.title"
              " FROM item_events e"
              " LEFT JOIN items i ON i.id = e.item_id"
              " LEFT JOIN sections s ON s.id = i.section_id"
              " WHERE e.kind='status' AND e.new='done' AND e.at>=? AND e.at<?" + sc +
              " ORDER BY e.at DESC", [a, b] + sa)
    ids = list({r[0] for r in rows})
    born = {}
    if ids:
        q = ",".join("?" * len(ids))
        for row in _q(con, "SELECT item_id, MIN(at) FROM item_events"
                           " WHERE item_id IN (%s) AND kind='created'"
                           " GROUP BY item_id" % q, ids):
            born[row[0]] = row[1]
    out = []
    seen = set()
    for iid, title, at, section in rows:
        if iid in seen:
            continue
        seen.add(iid)
        age = None
        d0, d1 = parse(born.get(iid)), parse(at)
        if d0 and d1:
            age = max(0, (d1 - d0).days)
        out.append({"id": iid, "title": title, "section": section or "",
                    "at": at, "age_days": age})
    return out


def opened_items(con, day_from, day_to, uid=0):
    a, b = _bounds(day_from, day_to)
    sc, sa = scope(con, uid)
    rows = _q(con,
              "SELECT e.item_id, e.title, e.at, s.title, i.status"
              " FROM item_events e"
              " LEFT JOIN items i ON i.id = e.item_id"
              " LEFT JOIN sections s ON s.id = i.section_id"
              " WHERE e.kind='created' AND e.at>=? AND e.at<?" + sc +
              " ORDER BY e.at DESC", [a, b] + sa)
    return [{"id": r[0], "title": r[1], "at": r[2],
             "section": r[3] or "", "status": r[4] or "gone"} for r in rows]


def history(con, weeks=8, day=None, uid=0):
    """Opened against closed, one row a week, oldest first.

    Counted in one pass over the window rather than three queries a week - the
    chart is eight bars and used to cost twenty-four round trips."""
    mon, _sun = week_of(day)
    first = mon - timedelta(days=7 * (weeks - 1))
    a, b = _bounds(first, mon + timedelta(days=6))
    sc, sa = scope(con, uid)
    weeks_out = []
    for back in range(weeks - 1, -1, -1):
        w = mon - timedelta(days=7 * back)
        weeks_out.append({"week": w.isoformat(), "label": w.strftime("%-m/%-d"),
                          "opened": 0, "closed": 0, "reopened": 0})
    index = {w["week"]: w for w in weeks_out}
    ny = _ny()
    for at, kind, old, new in _q(con,
            "SELECT at, kind, old, new FROM item_events e"
            " WHERE at>=? AND at<?" + sc, [a, b] + sa):
        d = parse(at)
        if not d:
            continue
        local = d.astimezone(ny).date()
        key = (local - timedelta(days=local.weekday())).isoformat()
        bucket = index.get(key)
        if not bucket:
            continue
        if kind == "created":
            bucket["opened"] += 1
        elif kind == "status" and new == "done":
            bucket["closed"] += 1
        elif kind == "status" and old == "done" and new != "done":
            bucket["reopened"] += 1
    return weeks_out


# ---------- other people ----------

def waiting_on(con, uid=0):
    """Everything sitting with someone else, longest first.

    `approx` means the clock started when history did, not when he actually
    handed it over - so the real wait is at least this long, never less.
    """
    inq, ia = _inq(con, uid)
    rows = _q(con,
              "SELECT i.id, i.title, i.waiting_on, s.title"
              " FROM items i LEFT JOIN sections s ON s.id = i.section_id"
              " WHERE i.status<>'done' AND ifnull(i.waiting_on,'')<>''" + inq, ia)
    now = now_utc()
    ids = [r[0] for r in rows]
    handed = {}
    if ids:
        q = ",".join("?" * len(ids))
        for row in _q(con, "SELECT item_id, at, kind FROM item_events"
                           " WHERE item_id IN (%s) AND kind IN ('waiting','snapshot')"
                           " AND ifnull(new,'')<>'' ORDER BY at" % q, ids):
            handed[row[0]] = (row[1], row[2])      # ordered ascending, so last wins
    out = []
    for iid, title, who, section in rows:
        ev = handed.get(iid)
        since, approx = None, True
        if ev:
            since = parse(ev[0])
            approx = (ev[1] == "snapshot")
        days = (now - since).days if since else None
        if approx and not days:
            # The snapshot was written when tracking began, so "0 days" would be a
            # lie about a task that may have been sitting with someone since July.
            # Say nothing rather than say something wrong.
            days = None
        out.append({"id": iid, "title": title, "who": who, "section": section or "",
                    "days": days, "approx": approx})
    out.sort(key=lambda r: (r["days"] is None, -(r["days"] or 0)))
    return out


# ---------- things going quiet ----------

def _last_touch_all(con, ids):
    """When something last actually happened to each of these tasks.

    Four queries per task became a thousand queries to draw one page. This asks
    four times in total and joins the answers in Python.

    Snapshot rows are excluded on purpose: they were all written the moment
    tracking began, and counting them would make every old task look freshly
    handled, so nothing would ever show as stalled.
    """
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    best = {}
    def offer(iid, raw):
        d = parse(raw)
        if d and (iid not in best or d > best[iid]):
            best[iid] = d
    for row in _q(con, "SELECT item_id, MAX(at) FROM item_events"
                       " WHERE item_id IN (%s) AND kind<>'snapshot'"
                       " GROUP BY item_id" % q, ids):
        offer(row[0], row[1])
    for table in ("item_notes", "item_files"):
        for row in _q(con, "SELECT item_id, MAX(created_at) FROM %s"
                           " WHERE item_id IN (%s) GROUP BY item_id" % (table, q), ids):
            offer(row[0], row[1])
    for row in _q(con, "SELECT id, updated_at FROM items WHERE id IN (%s)" % q, ids):
        offer(row[0], row[1])
    return best


def stalled(con, days=STALE_DAYS, uid=0):
    """Open, and nothing has happened to it in a fortnight."""
    inq, ia = _inq(con, uid)
    rows = _q(con,
              "SELECT i.id, i.title, s.title, i.status, i.due_date"
              " FROM items i LEFT JOIN sections s ON s.id = i.section_id"
              " WHERE i.status<>'done'" + inq, ia)
    now = now_utc()
    touched = _last_touch_all(con, [r[0] for r in rows])
    out = []
    for iid, title, section, status, due in rows:
        t = touched.get(iid)
        if not t:
            continue
        quiet = (now - t).days
        if quiet >= days:
            out.append({"id": iid, "title": title, "section": section or "",
                        "status": status, "due": due or "", "quiet_days": quiet})
    out.sort(key=lambda r: -r["quiet_days"])
    return out


# ---------- the calendar ----------

def meetings(con, day_from, day_to, uid=0):
    """Meetings are the one part of the day that is genuinely measured.

    Duration is only known for events synced after the column existed, so hours
    are reported as a floor with a count of how many are still unmeasured.
    """
    rows = _q(con, "SELECT day, subject, start_time, dur_min FROM events"
                   " WHERE day>=? AND day<=? AND owner_id=? ORDER BY day, start_time",
              (day_from.isoformat(), day_to.isoformat(), uid))
    per_day, known, unknown = {}, 0, 0
    for day, _subj, _st, dur in rows:
        per_day[day] = per_day.get(day, 0) + 1
        if dur:
            known += int(dur)
        else:
            unknown += 1
    busiest = max(per_day.items(), key=lambda kv: kv[1]) if per_day else None
    return {"count": len(rows), "minutes_known": known, "unmeasured": unknown,
            "per_day": per_day,
            "busiest_day": busiest[0] if busiest else "",
            "busiest_count": busiest[1] if busiest else 0}


def hours(minutes):
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    if not h:
        return "%d min" % m
    return "%dh %02dm" % (h, m) if m else "%dh" % h


# ---------- when the day starts and stops ----------

def active_window(con, day_from, day_to, uid=0):
    """The first and last time he touched the board each day.

    This is not hours worked and must never be presented as such - it is the
    span between the first and last thing he did in here, which is the only
    honest thing the board can say about a day without being told.
    """
    a, b = _bounds(day_from, day_to)
    sc, sa = scope(con, uid)
    ny = _ny()
    marks = {}
    ids = _ids(con, uid)
    nq = ",".join("?" * len(ids)) if ids else "NULL"
    for sql, extra in (
            ("SELECT at FROM item_events e WHERE at>=? AND at<?" + sc, sa),
            ("SELECT created_at FROM item_notes WHERE created_at>=? AND created_at<?"
             " AND item_id IN (SELECT id FROM items WHERE section_id IN (%s))" % nq, ids)):
        for (raw,) in _q(con, sql, [a, b] + list(extra)):
            d = parse(raw)
            if not d:
                continue
            local = d.astimezone(ny)
            key = local.date().isoformat()
            lo, hi = marks.get(key, (None, None))
            marks[key] = (local if lo is None or local < lo else lo,
                          local if hi is None or local > hi else hi)
    return dict((k, {"first": v[0].strftime("%H:%M"), "last": v[1].strftime("%H:%M"),
                     "span_min": int((v[1] - v[0]).total_seconds() // 60)})
                for k, v in sorted(marks.items()))


# ---------- everything, for one week ----------

def week(con, day=None, uid=0):
    mon, sun = week_of(day)
    prev = movement(con, mon - timedelta(days=7), mon - timedelta(days=1), uid)
    m = movement(con, mon, sun, uid)
    return {
        "from": mon.isoformat(), "to": sun.isoformat(),
        "movement": m, "previous": prev,
        "closed": closed_items(con, mon, sun, uid),
        "opened": opened_items(con, mon, sun, uid),
        "waiting": waiting_on(con, uid),
        "stalled": stalled(con, uid=uid),
        "meetings": meetings(con, mon, sun, uid),
        "active": active_window(con, mon, sun, uid),
        "history": history(con, 8, day, uid),
        "note": note(con, mon, uid),
        "tracking_since": tracking_since(con),
    }


def tracking_since(con):
    r = _q(con, "SELECT MIN(at) FROM item_events")
    return (r[0][0] or "") if r else ""


def note(con, monday, uid=0):
    r = _q(con, "SELECT body FROM pulse_notes WHERE week=? AND user_id=?",
           (monday.isoformat(), uid))
    return r[0][0] if r else ""
