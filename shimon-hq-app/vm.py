"""RingCentral voicemail -> Yiddish transcript, for HQ.

The Shefa Yoel line takes the calls from the almanos / tzedakah families. Every new
voicemail is pulled from RingCentral, the audio kept on the HQ disk, and the
recording sent to Yiddish Labs for a transcript, so nobody has to sit with
the phone to know what came in.

Env:
  RC_CLIENT_ID / RC_CLIENT_SECRET / RC_JWT   RingCentral app (JWT auth flow)
  RC_SERVER        default https://platform.ringcentral.com
  RC_EXTENSION_ID    the extension whose voicemail box we read. "~" = the JWT user.
  RC_EXTENSION_NAME  or: resolve the box by its name/number (e.g. "Shefa yoel");
                     needs the ReadAccounts scope on the RC app.
  RC_DATE_FROM     backfill start, YYYY-MM-DD (default: 90 days back)
  YL_API_KEY       Yiddish Labs key (yl_live_... standard, or yl_flash_... flash)
  YL_MODE          standard | flash (inferred from the key prefix if unset)
  YL_CONTEXT       optional hint text for the transcriber
  VM_SHORT_SEC     voicemails this long or shorter are filed as "short" and not transcribed (default 5)
  ANTHROPIC_API_KEY  optional - English gist of each transcript
  VM_MIRROR_URL / VM_MIRROR_TOKEN
                     Mirror mode (Joel's HQ): instead of RingCentral + Yiddish Labs, copy the
                     voicemails, transcripts and recordings from another HQ (its /api/vm/export,
                     Bearer = that HQ's API_TOKEN). No second transcription bill.
  DH_URL / DH_TOKEN  Divrei HaYamim app (https://divrei-hayamim.onrender.com) + its APP_TOKEN,
                     for "send to Divrei HaYamim" on a voicemail

Pure urllib, like wa.py: no new dependencies.
"""
import json
import os
import re
import time
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/New_York"))


def to_local(ts):
    """RingCentral stamps everything in UTC ('...Z'); HQ shows New York time.
    Returns a naive local ISO string (YYYY-MM-DDTHH:MM:SS)."""
    if not ts:
        return ts
    try:
        t = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            return ts[:19]
        return d.astimezone(TZ).replace(tzinfo=None).isoformat(timespec="seconds")
    except ValueError:
        return ts

RC_SERVER = os.environ.get("RC_SERVER", "https://platform.ringcentral.com")
UA = "ShimonHQ-vm/1.0"

_tok = {"access": None, "exp": 0}


def configured():
    return all(os.environ.get(k) for k in ("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT")) or mirror_configured()


MIRROR_URL = (os.environ.get("VM_MIRROR_URL") or "").rstrip("/")


def mirror_configured():
    return bool(MIRROR_URL and os.environ.get("VM_MIRROR_TOKEN"))


def mirror_sync(con, files_dir, log=None):
    """Copy voicemails from the source HQ: rows upserted by rc_id (transcripts arrive
    later, so text fields are refreshed every pass), recordings fetched once.
    Handled / task / Divrei HaYamim state stays local to this HQ."""
    log = log or (lambda *a: None)
    h = {"Authorization": "Bearer " + os.environ["VM_MIRROR_TOKEN"]}
    rows = _req(MIRROR_URL + "/api/vm/export", headers=h, timeout=120).get("voicemails", [])
    vm_dir = os.path.join(files_dir, "vm")
    os.makedirs(vm_dir, exist_ok=True)
    have = {r[0]: r for r in con.execute("SELECT rc_id, tstatus, stored_name FROM voicemails")}
    new = fetched = 0
    for r in rows:
        if r["rc_id"] not in have:
            con.execute(
                "INSERT INTO voicemails(rc_id, ext, ts, caller_number, caller_name, duration,"
                " stored_name, rc_text, yiddish, english, tstatus, received_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rc_id) DO NOTHING",
                (r["rc_id"], r.get("ext"), r.get("ts"), r.get("caller_number"), r.get("caller_name"),
                 r.get("duration"), None, r.get("rc_text"), r.get("yiddish"), r.get("english"),
                 r.get("tstatus") or "new", datetime.now().isoformat(timespec="seconds")))
            new += 1
        else:
            con.execute("UPDATE voicemails SET ts=?, caller_number=?, caller_name=?, duration=?, rc_text=?,"
                        " yiddish=?, english=?, tstatus=CASE WHEN tstatus='failed' THEN ? ELSE ? END WHERE rc_id=?",
                        (r.get("ts"), r.get("caller_number"), r.get("caller_name"), r.get("duration"), r.get("rc_text"),
                         r.get("yiddish"), r.get("english"), r.get("tstatus") or "new", r.get("tstatus") or "new", r["rc_id"]))
        # the recording, once
        local = have.get(r["rc_id"])
        if r.get("stored_name") and not (local and local[2]):
            try:
                blob, ctype = _req("%s/api/vm/%s/audio" % (MIRROR_URL, r["rc_id"]), headers=h, raw=True, timeout=120)
                with open(os.path.join(vm_dir, r["stored_name"]), "wb") as f:
                    f.write(blob)
                con.execute("UPDATE voicemails SET stored_name=? WHERE rc_id=?", (r["stored_name"], r["rc_id"]))
                fetched += 1
            except Exception as e:
                log("vm mirror: audio %s failed: %s", r["rc_id"], e)
        con.commit()
    con.execute("INSERT INTO settings(k, v) VALUES('vm_last_sync', ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    log("vm mirror: %d new, %d recordings", new, fetched)
    return new, fetched


# ---------- http ----------

def _req(url, data=None, headers=None, method=None, timeout=60, raw=False):
    h = {"User-Agent": UA}
    h.update(headers or {})
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "")
    if raw:
        return body, ctype
    return json.loads(body.decode("utf-8") or "{}")


def _retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(int(e.headers.get("Retry-After", 15)))
                continue
            raise


# ---------- RingCentral ----------

def rc_token():
    if _tok["access"] and time.time() < _tok["exp"] - 60:
        return _tok["access"]
    import base64
    basic = base64.b64encode(("%s:%s" % (os.environ["RC_CLIENT_ID"],
                                         os.environ["RC_CLIENT_SECRET"])).encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": os.environ["RC_JWT"]}).encode()
    j = _req(RC_SERVER + "/restapi/oauth/token", data=data, timeout=30,
             headers={"Authorization": "Basic " + basic,
                      "Content-Type": "application/x-www-form-urlencoded"})
    _tok["access"] = j["access_token"]
    _tok["exp"] = time.time() + int(j.get("expires_in", 3600))
    return _tok["access"]


def _rc_get(path, params=None, raw=False):
    url = path if path.startswith("http") else RC_SERVER + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    h = {"Authorization": "Bearer " + rc_token(), "Accept": "*/*" if raw else "application/json"}
    return _retry(lambda: _req(url, headers=h, raw=raw, timeout=120))


_ext_cache = {}


def rc_extension_id():
    """RC_EXTENSION_ID wins; else RC_EXTENSION_NAME is matched against the account's
    extensions (name or extension number, case-insensitive); else the JWT user."""
    eid = (os.environ.get("RC_EXTENSION_ID") or "").strip()
    if eid:
        return eid
    want = (os.environ.get("RC_EXTENSION_NAME") or "").strip().lower()
    if not want:
        return "~"
    if want in _ext_cache:
        return _ext_cache[want]
    page = 1
    while True:
        j = _rc_get("/restapi/v1.0/account/~/extension", {"perPage": 1000, "page": page})
        for e in j.get("records", []):
            if (e.get("name") or "").strip().lower() == want or str(e.get("extensionNumber")) == want:
                _ext_cache[want] = str(e["id"])
                return _ext_cache[want]
        if page >= (j.get("paging") or {}).get("totalPages", 1):
            break
        page += 1
    # loose match as a fallback (e.g. "shefa" -> "Shefa yoel")
    j = _rc_get("/restapi/v1.0/account/~/extension", {"perPage": 1000})
    hits = [e for e in j.get("records", []) if want in (e.get("name") or "").lower()]
    if len(hits) == 1:
        _ext_cache[want] = str(hits[0]["id"])
        return _ext_cache[want]
    raise RuntimeError("RingCentral extension %r not found (%d loose matches)" % (want, len(hits)))


def rc_list_voicemails(date_from=None):
    ext = rc_extension_id()
    if not date_from:
        days = 90
        date_from = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    out, page = [], 1
    while True:
        j = _rc_get("/restapi/v1.0/account/~/extension/%s/message-store" % ext,
                    {"messageType": "VoiceMail", "dateFrom": date_from + "T00:00:00.000Z",
                     "perPage": 250, "page": page})
        out += j.get("records", [])
        if page >= (j.get("paging") or {}).get("totalPages", 1):
            break
        page += 1
    return out


def rc_audio(record):
    """(bytes, ext, duration_sec) for the voicemail's recording, or None."""
    for a in record.get("attachments", []):
        if a.get("type") == "AudioRecording":
            body, ctype = _rc_get(a["uri"], raw=True)
            ext = "mp3" if "mpeg" in (ctype or a.get("contentType", "")) else "wav"
            return body, ext, a.get("vmDuration")
    return None


def rc_transcript(record):
    """RingCentral's own (English) transcription, if it made one."""
    if record.get("vmTranscriptionStatus") != "Completed":
        return ""
    for a in record.get("attachments", []):
        if a.get("type") == "AudioTranscription":
            try:
                body, _ = _rc_get(a["uri"], raw=True)
                return body.decode("utf-8", "replace").strip()
            except Exception:
                return ""
    return ""


# ---------- Yiddish Labs ----------
#
# Two products behind one vendor:
#   standard  app.yiddishlabs.com/api/v1  X-API-KEY: yl_live_...  slower, more accurate,
#             returns a summary, stores the job; /process/text translates.
#   flash     flash.yiddishlabs.com/v1    Bearer yl_flash_...    fast, OpenAI-shaped,
#             nothing stored; /audio/translations gives English straight from audio.
# YL_MODE=standard (default) | flash.  A voicemail is short, so both run synchronously.

YL_STD = "https://app.yiddishlabs.com/api/v1"
YL_FLASH = "https://flash.yiddishlabs.com/v1"
YL_CONTEXT = os.environ.get("YL_CONTEXT",
    "Voicemail left on the Shefa Yoel tzedakah phone line by almanos and yesomim families; "
    "Chassidish Yiddish, names, addresses, phone numbers, dollar amounts.")


def yl_mode():
    m = (os.environ.get("YL_MODE") or "").lower()
    if m in ("standard", "flash"):
        return m
    return "flash" if (os.environ.get("YL_API_KEY") or "").startswith("yl_flash_") else "standard"


def yl_configured():
    return bool(os.environ.get("YL_API_KEY"))


def _multipart(fields, file_field, path):
    boundary = "----hq" + _uuid.uuid4().hex
    fname = os.path.basename(path)
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                  "Content-Type: %s\r\n\r\n" % (boundary, file_field, fname, ctype)).encode())
    with open(path, "rb") as f:
        parts.append(f.read())
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _yl_err(e):
    try:
        j = json.loads(e.read().decode("utf-8", "replace"))
        err = j.get("error") or {}
        return "YL %s %s: %s" % (e.code, err.get("code", ""), err.get("message", ""))
    except Exception:
        return "YL HTTP %s" % e.code


def yl_transcribe(path, name=""):
    """-> {"yiddish", "english", "raw"}"""
    key = os.environ["YL_API_KEY"]
    try:
        if yl_mode() == "flash":
            return _yl_flash(path, key)
        return _yl_standard(path, key, name)
    except urllib.error.HTTPError as e:
        raise RuntimeError(_yl_err(e))


def _clean_yi(t):
    """Standard API prefixes speaker turns with markers like ⟦#1⟧ - noise on a voicemail."""
    t = re.sub(r"\u27e6#?\d+\u27e7\s*", "", t)
    return re.sub(r"[ \t]+\n", "\n", t).strip()


def _clean_en(t):
    """/process/text answers in Markdown; a voicemail note wants plain text."""
    t = re.sub(r"(?<!\w)[_*]{1,2}([^_*\n]+?)[_*]{1,2}(?!\w)", r"\1", t)
    return t.strip()


def _yl_standard(path, key, name):
    h = {"X-API-KEY": key}
    body, ct = _multipart({"language": "yi", "context": YL_CONTEXT, "name": name or os.path.basename(path)},
                          "file", path)
    j = _retry(lambda: _req(YL_STD + "/transcriptions/sync", data=body, method="POST", timeout=330,
                            headers=dict(h, **{"Content-Type": ct})))
    # >5 min audio comes back queued: poll
    for _ in range(90):
        if j.get("status") == "completed" or j.get("text"):
            break
        if j.get("status") in ("failed", "error"):
            raise RuntimeError("YL job %s failed" % j.get("id"))
        time.sleep(5)
        j = _req("%s/transcriptions/%s" % (YL_STD, j["id"]), headers=h)
    yi = _clean_yi(j.get("text") or "")
    en = ""
    if yi:
        try:
            t = _req(YL_STD + "/process/text", method="POST", timeout=120,
                     data=json.dumps({"text_content": yi, "action": "translate-english"}).encode(),
                     headers=dict(h, **{"Content-Type": "application/json"}))
            en = _clean_en(t.get("text") or "")
        except Exception:
            en = ""
    summ = _clean_yi(j.get("summary") or "")
    return {"yiddish": yi, "english": en, "summary": summ, "raw": json.dumps(j, ensure_ascii=False)[:8000]}


def _yl_flash(path, key):
    h = {"Authorization": "Bearer " + key}
    body, ct = _multipart({"language": "yi", "prompt": YL_CONTEXT[:200], "response_format": "json"},
                          "file", path)
    j = _retry(lambda: _req(YL_FLASH + "/audio/transcriptions", data=body, method="POST", timeout=330,
                            headers=dict(h, **{"Content-Type": ct})))
    yi = (j.get("text") or "").strip()
    en = ""
    try:
        body2, ct2 = _multipart({"language": "yi", "response_format": "json"}, "file", path)
        t = _retry(lambda: _req(YL_FLASH + "/audio/translations", data=body2, method="POST", timeout=330,
                                headers=dict(h, **{"Content-Type": ct2})))
        en = (t.get("text") or "").strip()
    except Exception:
        en = ""
    return {"yiddish": yi, "english": en, "summary": "", "raw": json.dumps(j, ensure_ascii=False)[:8000]}


# ---------- English gist (optional) ----------

def gist(yiddish, caller=""):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not yiddish.strip():
        return ""
    prompt = ("Voicemail left on a charity line (almanos / yesomim families call it). "
              "Caller: %s\n\nYiddish transcript:\n%s\n\n"
              "Reply in plain English, 2-4 short lines: who is calling (if said), what they "
              "want, any amount / date / callback number, and the one next action. No preamble."
              % (caller or "unknown", yiddish[:6000]))
    body = json.dumps({"model": os.environ.get("HQ_SUMMARY_MODEL", "claude-haiku-4-5"),
                       "max_tokens": 300,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    try:
        j = _req("https://api.anthropic.com/v1/messages", data=body, method="POST", timeout=60,
                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                          "Content-Type": "application/json"})
        return "".join(p.get("text", "") for p in j.get("content", [])).strip()
    except Exception:
        return ""


# ---------- Divrei HaYamim ----------

DH_URL = (os.environ.get("DH_URL") or "").rstrip("/")
_dh_cache = {"at": 0, "data": None}


def dh_configured():
    return bool(DH_URL and os.environ.get("DH_TOKEN"))


def dh_projects():
    """Project + category names from the chronicle app, cached ten minutes."""
    if not dh_configured():
        return {"projects": [], "categories": []}
    if _dh_cache["data"] and time.time() - _dh_cache["at"] < 600:
        return _dh_cache["data"]
    try:
        j = _req("%s/api/projects?t=%s" % (DH_URL, urllib.parse.quote(os.environ["DH_TOKEN"])), timeout=30)
        _dh_cache.update(at=time.time(), data={"projects": j.get("projects", []),
                                                "categories": j.get("categories", [])})
    except Exception:
        if not _dh_cache["data"]:
            return {"projects": [], "categories": []}
    return _dh_cache["data"]


def dh_push(row, files_dir, project, day, title="", category=""):
    """Post one voicemail to Divrei HaYamim as a story on `day` under `project`,
    recording attached. Returns the story id."""
    who = row["caller_name"] or fmt_phone(row["caller_number"]) or "Unknown caller"
    title = title or ("Voicemail from %s" % who)
    header = "Voicemail %s · %s · %ss" % ((row["ts"] or "")[:16].replace("T", " "),
                                          fmt_phone(row["caller_number"]), row["duration"] or "?")
    fields = {"date": day, "title": title, "project": project, "category": category or "",
              "body_en": ((row["english"] or "").strip() + "\n\n" + header).strip(),
              "body_yi": (row["yiddish"] or "").strip()}
    path = os.path.join(files_dir, "vm", row["stored_name"]) if row["stored_name"] else None
    if path and os.path.exists(path):
        body, ct = _multipart(fields, "files", path)
    else:
        body, ct = _multipart(fields, "files", "/dev/null")
    j = _req("%s/api/story?t=%s" % (DH_URL, urllib.parse.quote(os.environ["DH_TOKEN"])), data=body,
             method="POST", timeout=120, headers={"Content-Type": ct})
    if not j.get("ok"):
        raise RuntimeError(j.get("error") or "Divrei HaYamim refused the story")
    _dh_cache["at"] = 0  # a new project may have been typed in
    return j["id"], j.get("url", "")


# ---------- storage ----------

def ensure_schema(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS voicemails ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " rc_id TEXT UNIQUE NOT NULL,"
        " ext TEXT, ts TEXT, caller_number TEXT, caller_name TEXT,"
        " duration INTEGER, stored_name TEXT, rc_text TEXT,"
        " yiddish TEXT, english TEXT, yl_raw TEXT,"
        " tstatus TEXT NOT NULL DEFAULT 'new',"   # new | done | failed | skipped
        " terror TEXT, item_id INTEGER, handled INTEGER NOT NULL DEFAULT 0,"
        " received_at TEXT)")
    con.execute("CREATE INDEX IF NOT EXISTS vm_open ON voicemails(handled, ts)")
    cols = [r[1] for r in con.execute("PRAGMA table_info(voicemails)")]
    for c in ("dh_event_id TEXT", "dh_project TEXT", "dh_url TEXT",
              "notified INTEGER NOT NULL DEFAULT 0"):
        if c.split()[0] not in cols:
            con.execute("ALTER TABLE voicemails ADD COLUMN " + c)
    if "notified" not in cols:
        # everything already on the board was heard about some other way
        con.execute("UPDATE voicemails SET notified=1")
    # file what is already in: short recordings not yet transcribed, and transcripts with nothing in them
    con.execute("UPDATE voicemails SET tstatus='short' WHERE tstatus IN ('new','failed')"
                " AND duration IS NOT NULL AND duration <= ?", (SHORT_SEC,))
    for r in con.execute("SELECT id, yiddish FROM voicemails WHERE tstatus='done'").fetchall():
        if _is_empty_text(r[1]):
            con.execute("UPDATE voicemails SET tstatus='empty' WHERE id=?", (r[0],))
    # timestamps stored in UTC before to_local existed -> New York time
    for r in con.execute("SELECT id, ts FROM voicemails WHERE ts LIKE '%Z' OR ts LIKE '%+00:00'").fetchall():
        con.execute("UPDATE voicemails SET ts=? WHERE id=?", (to_local(r[1]), r[0]))
    # strip the queue prefix off names stored before the cleanup existed
    for r in con.execute("SELECT id, caller_name FROM voicemails WHERE caller_name LIKE '% - %'").fetchall():
        con.execute("UPDATE voicemails SET caller_name=? WHERE id=?", (_caller_name(r[1]), r[0]))


SHORT_SEC = int(os.environ.get("VM_SHORT_SEC", "5"))


def _is_empty_text(t):
    """A hang-up, breathing, or a single word: nothing worth reading."""
    words = re.findall(r"[\w\u0590-\u05ff']+", t or "")
    return len(words) < 3


def _caller_name(n):
    """RC labels queue voicemails "Shefa yoel - DASKAL,CHANA"; keep just the caller,
    and drop carrier placeholders that are not a name."""
    n = re.sub(r"^[^-]{1,40}\s+-\s+", "", n.strip())
    if re.fullmatch(r"(WIRELESS CALLER|UNKNOWN|UNAVAILABLE|ANONYMOUS|PRIVATE|Possible spam call|[A-Z ]{2,}\s{2,}[A-Z]{2})", n, re.I):
        return ""
    if "," in n and n.isupper():
        last, first = [x.strip() for x in n.split(",", 1)]
        n = ("%s %s" % (first, last)).title()
    return n


def _safe(s):
    return re.sub(r"[^A-Za-z0-9+_-]+", "_", s or "unknown").strip("_")[:40]


def sync(con, files_dir, log=None, date_from=None, transcribe=True, limit=None):
    """Pull anything RingCentral has that we do not. Returns (new, transcribed)."""
    log = log or (lambda *a: None)
    if not configured():
        return 0, 0
    vm_dir = os.path.join(files_dir, "vm")
    os.makedirs(vm_dir, exist_ok=True)
    have = {r[0] for r in con.execute("SELECT rc_id FROM voicemails")}
    recs = rc_list_voicemails(date_from or os.environ.get("RC_DATE_FROM"))
    recs.sort(key=lambda m: m.get("creationTime", ""))
    new = 0
    for m in recs:
        if str(m["id"]) in have:
            continue
        if limit and new >= limit:
            break
        frm = m.get("from") or {}
        num = frm.get("phoneNumber") or frm.get("extensionNumber") or ""
        name = _caller_name(frm.get("name") or "")
        got = rc_audio(m)
        stored = None
        dur = None
        if got:
            blob, ext, dur = got
            stamp = to_local(m["creationTime"]).replace("-", "").replace(":", "")[:13].replace("T", "_")
            stored = "%s_%s_%s.%s" % (stamp, _safe(name or num), m["id"], ext)
            with open(os.path.join(vm_dir, stored), "wb") as f:
                f.write(blob)
        con.execute(
            "INSERT INTO voicemails(rc_id, ext, ts, caller_number, caller_name, duration,"
            " stored_name, rc_text, tstatus, received_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(rc_id) DO NOTHING",
            (str(m["id"]), rc_extension_id(), to_local(m.get("creationTime")),
             num, name, dur, stored, rc_transcript(m),
             ("short" if (stored and dur is not None and int(dur) <= SHORT_SEC)
              else "new" if stored else "skipped"),
             datetime.now().isoformat(timespec="seconds")))
        con.commit()
        new += 1
        log("vm: stored %s", stored)
    done = transcribe_pending(con, files_dir, log) if transcribe else 0
    con.execute("INSERT INTO settings(k, v) VALUES('vm_last_sync', ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    return new, done


def transcribe_pending(con, files_dir, log=None, max_n=10):
    log = log or (lambda *a: None)
    if not yl_configured():
        return 0
    rows = con.execute("SELECT * FROM voicemails WHERE tstatus IN ('new','failed')"
                       " AND stored_name IS NOT NULL ORDER BY ts LIMIT ?", (max_n,)).fetchall()
    n = 0
    for r in rows:
        path = os.path.join(files_dir, "vm", r["stored_name"])
        try:
            t = yl_transcribe(path, name="VM %s %s" % ((r["ts"] or "")[:16], r["caller_number"] or ""))
            en = t["english"] or t.get("summary") or gist(t["yiddish"], r["caller_name"] or r["caller_number"])
            status = "empty" if _is_empty_text(t["yiddish"]) else "done"
            con.execute("UPDATE voicemails SET yiddish=?, english=?, yl_raw=?, tstatus=?,"
                        " terror=NULL WHERE id=?", (t["yiddish"], en, t["raw"], status, r["id"]))
            n += 1
        except Exception as e:
            con.execute("UPDATE voicemails SET tstatus='failed', terror=? WHERE id=?",
                        (str(e)[:500], r["id"]))
            log("vm: transcription failed for %s: %s", r["stored_name"], e)
        con.commit()
    return n


def fmt_phone(n):
    d = re.sub(r"\D", "", n or "")
    if len(d) == 11 and d[0] == "1":
        d = d[1:]
    if len(d) == 10:
        return "(%s) %s-%s" % (d[:3], d[3:6], d[6:])
    return n or ""
