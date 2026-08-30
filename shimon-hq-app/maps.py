"""Google Maps for Shimon HQ.

Deep links (open in Maps, directions) always work and need no API key.
The embedded map preview and live traffic-aware travel time switch on
as soon as GOOGLE_MAPS_KEY is set in the environment.
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request

KEY = (os.environ.get("GOOGLE_MAPS_KEY") or "").strip()

# things that look like a "location" but are not a place on earth
_NOT_A_PLACE = re.compile(
    r"^\s*(https?://|zoom|teams|microsoft\s+teams|skype|webex|google\s+meet|dialpad"
    r"|phone|call|conference|tbd|tba|n/?a|online|virtual|remote)\b", re.I)
_MEETING_NOISE = re.compile(r"meeting\s*id|passcode|conference\s*id|dial[- ]?in", re.I)


def is_place(loc):
    """True when this location string is worth putting on a map."""
    s = (loc or "").strip()
    if len(s) < 4:
        return False
    if _NOT_A_PLACE.search(s):
        return False
    if _MEETING_NOISE.search(s):
        return False
    return True


def link(loc):
    """Open this place in Maps."""
    if not is_place(loc):
        return ""
    return ("https://www.google.com/maps/search/?api=1&query="
            + urllib.parse.quote_plus(loc.strip()))


def directions(loc, origin=""):
    """Turn-by-turn directions to this place (from `origin` when we know it)."""
    if not is_place(loc):
        return ""
    q = [("api", "1"), ("destination", loc.strip())]
    if origin:
        q.append(("origin", origin.strip()))
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(q)


def embed(loc):
    """Embeddable map iframe src — empty string when no key is configured."""
    if not (KEY and is_place(loc)):
        return ""
    return ("https://www.google.com/maps/embed/v1/place?key="
            + urllib.parse.quote(KEY) + "&q=" + urllib.parse.quote_plus(loc.strip())
            + "&zoom=15")


# ---------- live travel time (Routes API) ----------

_cache = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 10 * 60          # a drive time is good enough for ten minutes


def _cache_get(k):
    with _cache_lock:
        hit = _cache.get(k)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
    return None


def _cache_put(k, v):
    with _cache_lock:
        _cache[k] = (time.time(), v)
        if len(_cache) > 200:
            for old in sorted(_cache, key=lambda x: _cache[x][0])[:100]:
                _cache.pop(old, None)


_COORD = re.compile(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$")


def as_waypoint(place):
    """Routes API waypoint - a real lat,lng when we have one, else a plain address."""
    m = _COORD.match(place or "")
    if m:
        return {"location": {"latLng": {"latitude": float(m.group(1)),
                                        "longitude": float(m.group(2))}}}
    return {"address": (place or "").strip()}


def is_coord(place):
    return bool(_COORD.match(place or ""))


def drive_seconds(origin, dest, depart_utc=None, timeout=8):
    """Traffic-aware drive time in seconds, or None if we cannot work it out.

    depart_utc: a datetime in UTC. Google rejects a departure in the past, so
    anything not in the future is simply dropped and we get the typical time.
    """
    if not (KEY and origin and (is_place(dest) or is_coord(dest))):
        return None
    ck = (origin.strip().lower(), dest.strip().lower(),
          depart_utc.strftime("%Y%m%d%H%M")[:-1] if depart_utc else "now")
    hit = _cache_get(ck)
    if hit is not None:
        return hit or None

    body = {
        "origin": as_waypoint(origin),
        "destination": as_waypoint(dest),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    if depart_utc:
        from datetime import datetime, timezone
        if depart_utc > datetime.now(timezone.utc):
            body["departureTime"] = depart_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Goog-Api-Key": KEY,
                 "X-Goog-FieldMask": "routes.duration"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        routes = data.get("routes") or []
        if not routes:
            _cache_put(ck, 0)
            return None
        secs = int(str(routes[0].get("duration", "0s")).rstrip("s") or 0)
        # a six-hour-plus "drive" across town means the address geocoded to the
        # wrong place entirely - better to say nothing than "17h from the office"
        if secs > 6 * 3600:
            _cache_put(ck, 0)
            return None
        _cache_put(ck, secs)
        return secs or None
    except Exception:
        return None


def pretty_minutes(secs):
    if not secs:
        return ""
    m = int(round(secs / 60.0))
    if m < 60:
        return "%d min" % m
    h, m = divmod(m, 60)
    return "%dh %02dm" % (h, m) if m else "%dh" % h


def embed_check(timeout=8):
    """Ask Google directly whether the Embed API will serve this key.

    A key restricted by HTTP referrer would refuse a server-side probe even though
    it works in a browser - so the message is reported, never guessed at."""
    if not KEY:
        return {"ok": False, "detail": "no key"}
    url = embed("885 3rd Ave, New York, NY")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ShimonHQ/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "replace")
        low = body.lower()
        if "not authorized" in low or "api key" in low and "error" in low:
            return {"ok": False, "detail": body.strip()[:200]}
        return {"ok": True, "detail": "Embed API answered %s." % r.status}
    except urllib.error.HTTPError as e:
        try:
            msg = e.read(1000).decode("utf-8", "replace").strip()
        except Exception:
            msg = ""
        return {"ok": False, "detail": "HTTP %s %s" % (e.code, msg[:200])}
    except Exception as e:
        return {"ok": False, "detail": "could not reach Google: %s" % e}


def status():
    """For the setup screen: is the key in place, and does each API actually answer?"""
    if not KEY:
        return {"key": False, "routes": False, "embed": False,
                "detail": "No GOOGLE_MAPS_KEY set - tap-to-open links only."}
    secs = drive_seconds("Brooklyn, NY", "885 3rd Ave, New York, NY")
    emb = embed_check()
    bits = []
    if secs:
        bits.append("Routes API live (test route %s)." % pretty_minutes(secs))
    else:
        bits.append("Routes API refused the key - enable Routes API and check billing.")
    bits.append("Embed API: " + ("live." if emb["ok"] else emb["detail"]))
    return {"key": True, "routes": bool(secs), "embed": emb["ok"],
            "works": bool(secs), "detail": "  ".join(bits)}
