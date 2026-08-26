"""Minimal Web Push (RFC 8291 aes128gcm + RFC 8292 VAPID) using only `cryptography`.

Exists because the usual pywebpush -> http-ece dependency fails to build on
current setuptools. Verified against the RFC 8291 section 5 test vector.
"""
import os
import json
import time
import base64
import struct
import urllib.request
import urllib.error
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64d(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.urlsafe_b64decode(s + b"=" * ((4 - len(s) % 4) % 4))


def b64e(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload, ua_public_b64, auth_b64, salt=None, as_private=None):
    """Return the aes128gcm body to POST to a push endpoint."""
    ua_public_raw = b64d(ua_public_b64)
    auth = b64d(auth_b64)
    salt = salt or os.urandom(16)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public_raw = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_raw)
    shared = as_private.exchange(ec.ECDH(), ua_public)

    prk = _hkdf(auth, shared, b"WebPush: info\x00" + ua_public_raw + as_public_raw, 32)
    cek = _hkdf(salt, prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, prk, b"Content-Encoding: nonce\x00", 12)

    if isinstance(payload, str):
        payload = payload.encode()
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!L", 4096) + bytes([len(as_public_raw)]) + as_public_raw + ciphertext


def vapid_auth_header(endpoint, private_key, subject):
    """RFC 8292 'vapid t=<jwt>, k=<publickey>' Authorization header value."""
    parts = urlparse(endpoint)
    claims = {"aud": "%s://%s" % (parts.scheme, parts.netloc),
              "exp": int(time.time()) + 12 * 3600,
              "sub": subject}
    header = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64e(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = (header + "." + body).encode()

    der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")     # JOSE wants raw r||s

    jwt = header + "." + body + "." + b64e(sig)
    pub = private_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return "vapid t=%s, k=%s" % (jwt, b64e(pub))


def send(subscription, data, pem_path, subject="mailto:admin@example.com", ttl=3600):
    """POST one notification. Returns the push service's HTTP status."""
    with open(pem_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    endpoint = subscription["endpoint"]
    body = encrypt(data, subscription["keys"]["p256dh"], subscription["keys"]["auth"])
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Encoding", "aes128gcm")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("TTL", str(ttl))
    req.add_header("Authorization", vapid_auth_header(endpoint, private_key, subject))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
