"""Web Push notifications for members who installed the portal app (PWA).

Self-contained implementation of the two standards involved, on top of the
`cryptography` package (no pywebpush):
  * VAPID (RFC 8292): an ES256-signed JWT identifies our server to the push
    service (Apple, Google, Mozilla).
  * Message encryption (RFC 8291, aes128gcm): only the member's device can read
    the notification content.

The VAPID key pair is generated once and kept in the database (site_settings),
unless VAPID_PRIVATE_KEY is provided in the environment.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from db import PushSubscription, SessionLocal, get_setting, set_setting

log = logging.getLogger(__name__)
VAPID_SUB = os.getenv("VAPID_SUBJECT", "mailto:info@duvoyageur.ca")
_KEY_CACHE: dict = {}


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def ub64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw_public(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def vapid_key() -> ec.EllipticCurvePrivateKey:
    if "k" in _KEY_CACHE:
        return _KEY_CACHE["k"]
    pem = os.getenv("VAPID_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    if not pem:
        with SessionLocal() as db:
            pem = get_setting(db, "vapid").get("private_pem", "")
            if not pem:
                k = ec.generate_private_key(ec.SECP256R1())
                pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()).decode()
                set_setting(db, "vapid", {"private_pem": pem})
                db.commit()
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    _KEY_CACHE["k"] = key
    return key


def public_key_b64() -> str:
    """applicationServerKey for pushManager.subscribe()."""
    return b64u(_raw_public(vapid_key()))


def _vapid_header(endpoint: str) -> str:
    u = urlsplit(endpoint)
    aud = f"{u.scheme}://{u.netloc}"
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": VAPID_SUB},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    der = vapid_key().sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    jwt = f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={jwt}, k={public_key_b64()}"


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """RFC 8291 aes128gcm body for one push message."""
    ua_public = ub64(p256dh)
    auth_secret = ub64(auth)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_key)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!I", 4096) + bytes([len(as_public)]) + as_public + ciphertext


def send_one(sub: PushSubscription, data: dict, ttl: int = 86400) -> int:
    """Send one notification; returns the push service HTTP status (0 on network error)."""
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub.p256dh, sub.auth)
    req = urllib.request.Request(sub.endpoint, data=body, method="POST", headers={
        "Authorization": _vapid_header(sub.endpoint), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "normal"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        log.warning("Push send failed (network)", exc_info=True)
        return 0


def notify_client(client_id: int, title_fr: str, body_fr: str, url: str = "/portail",
                  title_en: str | None = None, body_en: str | None = None) -> tuple[int, int]:
    """Push to every device of a member, in each device's language. Expired
    subscriptions (404/410) are removed. Returns (sent, failed)."""
    sent = failed = 0
    with SessionLocal() as db:
        subs = db.query(PushSubscription).filter(PushSubscription.client_id == client_id).all()
        for s in subs:
            en = s.lang == "en"
            data = {"title": (title_en if en and title_en else title_fr), "body": (body_en if en and body_en else body_fr),
                    "url": url, "tag": f"dv-{int(time.time())}"}
            code = send_one(s, data)
            if 200 <= code < 300:
                s.last_ok_at = datetime.utcnow()
                sent += 1
            else:
                failed += 1
                if code in (404, 410):
                    db.delete(s)
        db.commit()
    return sent, failed


def notify_all(title_fr: str, body_fr: str, url: str = "/portail", title_en: str | None = None,
               body_en: str | None = None) -> tuple[int, int]:
    with SessionLocal() as db:
        ids = sorted({cid for (cid,) in db.query(PushSubscription.client_id).all()})
    sent = failed = 0
    for cid in ids:
        a, b = notify_client(cid, title_fr, body_fr, url, title_en, body_en)
        sent += a
        failed += b
    return sent, failed
