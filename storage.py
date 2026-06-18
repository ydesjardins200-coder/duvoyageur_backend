"""
storage.py
==========
Screenshot object storage on Cloudflare R2 (S3-compatible).

Screenshots used to live as base64 inside the DB row, which bloats the database
fast. They now go to an R2 bucket and the DB keeps only a small object *key*.

A screenshot record is a dict, in one of two shapes:
  R2:      {"media_type", "key", "received_at"}
  inline:  {"media_type", "b64", "received_at"}   # fallback when R2 isn't set up

`make_screenshot` uploads to R2 when configured and otherwise falls back to
inline base64, so the app still runs locally and in tests with zero setup.
`read_screenshot` transparently handles both shapes, so old base64 rows keep
working during/after the migration.

Required env vars to enable R2 (set in Railway → Variables):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
  (R2_ENDPOINT is optional; derived from the account id otherwise.)
"""
import base64
import logging
import mimetypes
import uuid
from datetime import datetime

from config import settings

log = logging.getLogger("duvoyageur.storage")

_client_cache = None


def r2_enabled() -> bool:
    """True when enough R2 configuration is present to use object storage."""
    return bool(
        settings.R2_BUCKET
        and settings.R2_ACCESS_KEY_ID
        and settings.R2_SECRET_ACCESS_KEY
        and (settings.R2_ENDPOINT or settings.R2_ACCOUNT_ID)
    )


def _endpoint() -> str:
    return settings.R2_ENDPOINT or f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _client():
    global _client_cache
    if _client_cache is None:
        import boto3
        from botocore.config import Config

        _client_cache = boto3.client(
            "s3",
            endpoint_url=_endpoint(),
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )
    return _client_cache


def _ext(media_type: str) -> str:
    return mimetypes.guess_extension((media_type or "image/png").split(";")[0]) or ".png"


def make_screenshot(data: bytes, media_type: str) -> dict:
    """Persist one screenshot and return its record. Uploads to R2 when
    configured; otherwise falls back to inline base64 (never loses the image)."""
    rec = {
        "media_type": media_type or "image/png",
        "received_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    if r2_enabled():
        try:
            key = f"screenshots/{datetime.utcnow():%Y/%m}/{uuid.uuid4().hex}{_ext(media_type)}"
            _client().put_object(
                Bucket=settings.R2_BUCKET, Key=key, Body=data,
                ContentType=rec["media_type"],
            )
            rec["key"] = key
            return rec
        except Exception as e:  # noqa: BLE001 — never lose a submission over storage
            log.exception("R2 upload failed, falling back to inline base64: %s", e)
    rec["b64"] = base64.b64encode(data).decode("ascii")
    return rec


def read_screenshot(rec: dict) -> "tuple[bytes, str] | tuple[None, None]":
    """Return (bytes, media_type) for a record, handling both R2 keys and the
    legacy inline base64 shape."""
    if not rec:
        return None, None
    mt = rec.get("media_type", "image/png")
    key = rec.get("key")
    if key:
        try:
            obj = _client().get_object(Bucket=settings.R2_BUCKET, Key=key)
            return obj["Body"].read(), mt
        except Exception as e:  # noqa: BLE001
            log.exception("R2 read failed for %s: %s", key, e)
            return None, None
    b64 = rec.get("b64")
    if b64:
        try:
            return base64.b64decode(b64), mt
        except Exception:  # noqa: BLE001
            return None, None
    return None, None


def delete_screenshot(rec: dict) -> None:
    """Best-effort delete of the underlying R2 object (no-op for inline records)."""
    key = (rec or {}).get("key")
    if key and r2_enabled():
        try:
            _client().delete_object(Bucket=settings.R2_BUCKET, Key=key)
        except Exception as e:  # noqa: BLE001
            log.warning("R2 delete failed for %s: %s", key, e)
