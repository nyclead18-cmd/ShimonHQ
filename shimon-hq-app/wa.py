"""WhatsApp via the TimelinesAI public API.

Read-only. Nothing here can send a message from Shimon's number - the only calls
made are GETs, so the 50-a-month send credit is never touched.

Chats carry a stable numeric id, which makes anchoring exact: a chat tied to a
task stays tied, with none of the subject-guessing that email needs.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://app.timelines.ai/integrations/api"
TOKEN = (os.environ.get("TIMELINES_TOKEN") or "").strip()


# TimelinesAI sits behind Cloudflare, which rejects urllib's default signature
# outright (error 1010) before the token is ever looked at.
UA = (os.environ.get("TIMELINES_UA") or "").strip() or \
     "ShimonHQ/1.0 (+https://shimonhq.onrender.com)"


def _get(path, params=None, timeout=20, ua=None):
    if not TOKEN:
        raise RuntimeError("no TIMELINES_TOKEN set")
    url = BASE + path
    if params:
        clean = dict((k, v) for k, v in params.items() if v not in (None, ""))
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": ua or UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _rows(payload):
    """TimelinesAI wraps results a couple of different ways depending on the call."""
    if isinstance(payload, list):
        return payload, False
    if not isinstance(payload, dict):
        return [], False
    more = bool(payload.get("has_more_pages"))
    for key in ("data", "results", "chats", "messages", "items"):
        v = payload.get(key)
        if isinstance(v, list):
            return v, more
        if isinstance(v, dict):
            for k2 in ("chats", "messages", "results", "items"):
                if isinstance(v.get(k2), list):
                    return v[k2], more or bool(v.get("has_more_pages"))
    return [], more


def chats(page=1, ua=None, **filters):
    payload = _get("/chats", dict(filters, page=page), ua=ua)
    return _rows(payload)


MAX_PAGES = 40      # 50 chats a page; his whole list, well inside the call quota


def all_chats(max_pages=MAX_PAGES, **filters):
    out = []
    for page in range(1, max_pages + 1):
        rows, more = chats(page=page, **filters)
        out.extend(rows)
        if not more or not rows:
            break
    return out


def messages(chat_id, after=None, limit_pages=3, sorting_order="asc"):
    out = []
    for page in range(1, limit_pages + 1):
        payload = _get("/chats/%s/messages" % chat_id,
                       {"after": after, "sorting_order": sorting_order, "page": page})
        rows, more = _rows(payload)
        out.extend(rows)
        if not more or not rows:
            break
    return out


def active_since(iso, max_pages=MAX_PAGES):
    """Chats whose newest message landed at or after `iso` - newest first.

    One cheap listing call tells us where to look, instead of walking every chat.
    """
    out = []
    for c in all_chats(max_pages=max_pages):
        ts = (c.get("last_message_timestamp") or "")
        if not ts:
            continue                      # never had a message - nothing to read
        if iso and ts[:19] < iso[:19]:
            continue
        out.append(c)
    out.sort(key=lambda c: c.get("last_message_timestamp") or "", reverse=True)
    return out


def one_line(m, width=220):
    """A message boiled down to something a sweep can read quickly."""
    who = "You" if m.get("from_me") else (m.get("sender_name") or m.get("sender_phone") or "?")
    txt = (m.get("text") or "").replace("\n", " ").strip()
    if not txt and m.get("has_attachment"):
        txt = "[%s]" % (m.get("attachment_filename") or "attachment")
    if len(txt) > width:
        txt = txt[:width].rsplit(" ", 1)[0] + "…"
    return "%s  %s: %s" % ((m.get("timestamp") or "")[:16].replace("T", " "), who, txt)


def status(ua=None):
    if not TOKEN:
        return {"token": False, "works": False,
                "detail": "No TIMELINES_TOKEN set - WhatsApp is not connected."}
    try:
        rows, _more = chats(page=1, ua=ua)
    except urllib.error.HTTPError as e:
        try:
            body = e.read(400).decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        why = "TimelinesAI refused the token"
        if e.code == 403 and "1010" in body:
            why = ("Cloudflare blocked the client before the token was checked "
                   "(error 1010) - try another User-Agent")
        return {"token": True, "works": False, "ua": ua or UA,
                "detail": "%s: HTTP %s %s" % (why, e.code, body[:200])}
    except Exception as e:
        return {"token": True, "works": False, "detail": "Could not reach TimelinesAI: %s" % e}
    # the listing runs oldest first, so the live chats are on the last pages -
    # walk the lot rather than judging by page one
    try:
        every = all_chats()
    except Exception as e:
        return {"token": True, "works": True, "chats_first_page": len(rows), "ua": ua or UA,
                "detail": "Connected, but could not page the full chat list: %s" % e}
    stamped = [c for c in every if c.get("last_message_timestamp")]
    newest = max([c["last_message_timestamp"] for c in stamped], default="")
    return {"token": True, "works": True, "ua": ua or UA,
            "chats_total": len(every), "chats_with_messages": len(stamped),
            "detail": "Connected. %d chats, %d with messages, newest %s."
                      % (len(every), len(stamped), newest[:16].replace("T", " ") or "unknown")}
