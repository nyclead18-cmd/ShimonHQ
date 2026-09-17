"""Two-step sign-in: TOTP (Google Authenticator / Authy / 1Password / iPhone Passwords),
recovery codes, and the QR code that sets a phone up. No third-party TOTP library -
the algorithm is forty lines of RFC 6238 and one fewer dependency is one fewer thing
to break on a Sunday-night deploy.
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import urllib.parse

DIGITS = 6
PERIOD = 30
WINDOW = 1          # accept the code before and after the current one (clock drift)


def new_secret():
    """160-bit secret, base32 without padding - what the apps expect to be typed."""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _key(secret):
    s = secret.strip().replace(" ", "").upper()
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s, casefold=True)


def code_at(secret, counter):
    mac = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return "%0*d" % (DIGITS, n % (10 ** DIGITS))


def verify(secret, code, last_counter=-1, now=None):
    """Returns the matched counter, or None. A counter at or below the last one that
    signed somebody in is refused: a code is good once, and a replayed one is the
    classic way a shoulder-surfed TOTP gets used."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    ctr = int((now or time.time()) // PERIOD)
    for c in range(ctr - WINDOW, ctr + WINDOW + 1):
        if c > last_counter and hmac.compare_digest(code_at(secret, c), code):
            return c
    return None


def otpauth_uri(secret, account, issuer):
    label = urllib.parse.quote("%s:%s" % (issuer, account), safe="")
    return "otpauth://totp/%s?%s" % (label, urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD}))


def qr_svg(text):
    """Inline SVG of a QR code, or None if the qrcode package is missing - the
    page then shows the key to type by hand, which every app also accepts."""
    try:
        import qrcode
        import qrcode.image.svg as svg
    except Exception:
        return None
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    q.add_data(text)
    q.make(fit=True)
    img = q.make_image(image_factory=svg.SvgPathImage)
    return img.to_string(encoding="unicode")


def pretty_secret(secret):
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


# ---------- recovery codes ----------
# Eight one-time codes for the day the phone is lost. Stored hashed; shown exactly once.

def new_recovery_codes(n=8):
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O/1/I
    out = []
    for _ in range(n):
        raw = "".join(secrets.choice(alphabet) for _ in range(8))
        out.append(raw[:4] + "-" + raw[4:])
    return out


def hash_code(code):
    norm = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return hashlib.sha256(norm.encode()).hexdigest()


def use_recovery(hashes, code):
    """Remove the matching hash and return the remaining list, or None if no match."""
    h = hash_code(code)
    for stored in hashes:
        if hmac.compare_digest(stored, h):
            return [x for x in hashes if x != stored]
    return None


# ---------- trusted device ----------
# "Remember this phone for 30 days": a signed cookie naming the user and an expiry.
# Signed with the app secret, so it cannot be forged; bound to the user's TOTP secret,
# so turning two-step off and on again invalidates every remembered device.

def trust_token(secret_key, uid, totp_secret, days=30):
    exp = int(time.time()) + days * 86400
    msg = "%d.%d" % (uid, exp)
    sig = hmac.new(_bytes(secret_key) + _key(totp_secret), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return "%s.%s" % (msg, sig)


def trust_ok(secret_key, token, uid, totp_secret):
    try:
        u, exp, sig = token.split(".")
        if int(u) != uid or int(exp) < time.time():
            return False
        msg = "%s.%s" % (u, exp)
        want = hmac.new(_bytes(secret_key) + _key(totp_secret), msg.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(want, sig)
    except Exception:
        return False


def _bytes(k):
    return k.encode() if isinstance(k, str) else bytes(k)
