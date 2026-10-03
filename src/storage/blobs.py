"""Object storage for raw evidence (Phase 2.1).

Two interchangeable stores with the same three operations - exists, put once,
get - and deliberately NO delete or overwrite:

  LocalBlobStore  a directory; tests and dry runs
  R2BlobStore     Cloudflare R2 through its S3-compatible API, signed with
                  AWS Signature V4 using only the standard library (no SDK,
                  so the supply-chain surface stays at one package)

Keys are content-addressed by the caller (evidence.py), so writing the same
key twice can only ever mean the same bytes. put_if_absent never replaces an
existing object.

The R2 store talks to exactly one host, the configured account endpoint, and
never puts credentials into exceptions or logs. This module is separate from
src/sources on purpose: market-facing clients stay GET-only, and the only
write path in the system targets our own bucket.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KEY_SHAPE = re.compile(r"^[a-z0-9_]+(/[0-9A-Za-z_.\-]+)+$")
R2_ENDPOINT_SHAPE = re.compile(r"^https://[0-9a-f]{32}\.r2\.cloudflarestorage\.com$")
DEFAULT_BUCKET = "isr-evidence"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class BlobStoreError(RuntimeError):
    """A storage operation failed. Never carries credentials."""


def check_key(key: str) -> str:
    if not KEY_SHAPE.match(key) or ".." in key:
        raise BlobStoreError(f"refusing unsafe object key {key!r}")
    return key


class LocalBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def put_if_absent(self, key: str, data: bytes, content_type: str = "application/gzip") -> bool:
        path = self._path(key)
        if path.is_file():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return True

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise BlobStoreError(f"no object {key!r}") from None


# ---- AWS Signature V4 (pure; tested against AWS's published example) --------

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_authorization(method: str, url: str, headers: dict[str, str], payload_sha256: str,
                        access_key: str, secret_key: str, amz_date: str,
                        region: str = "auto", service: str = "s3") -> str:
    """The Authorization header value. `headers` must include host and x-amz-*."""
    parts = urllib.parse.urlsplit(url)
    canonical_uri = parts.path or "/"
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    canonical_query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(query))
    canon = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    signed = ";".join(sorted(canon))
    canonical_headers = "".join(f"{k}:{canon[k]}\n" for k in sorted(canon))
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed, payload_sha256])
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical_request.encode()).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode(), day)
    for part in (region, service, "aws4_request"):
        k = _hmac(k, part)
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}")


class R2BlobStore:
    """Cloudflare R2 bucket. HEAD, GET and conditional PUT only."""

    def __init__(self, endpoint: str, bucket: str, access_key_id: str, secret_access_key: str,
                 *, opener=None, clock=None, timeout: int = 60):
        if not R2_ENDPOINT_SHAPE.match(endpoint or ""):
            raise BlobStoreError("R2 endpoint must be https://<32-hex account id>.r2.cloudflarestorage.com")
        if not re.match(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", bucket or ""):
            raise BlobStoreError("invalid R2 bucket name")
        if not access_key_id or not secret_access_key:
            raise BlobStoreError("R2 credentials are not set")
        self.endpoint = endpoint
        self.bucket = bucket
        self._key_id = access_key_id
        self._secret = secret_access_key
        self._open = opener or urllib.request.urlopen
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.timeout = timeout

    @classmethod
    def from_env(cls, env=os.environ) -> "R2BlobStore":
        return cls(env.get("R2_ENDPOINT", ""), env.get("R2_BUCKET") or DEFAULT_BUCKET,
                   env.get("R2_ACCESS_KEY_ID", ""), env.get("R2_SECRET_ACCESS_KEY", ""))

    def __repr__(self) -> str:  # never show credentials
        return f"R2BlobStore(bucket={self.bucket!r})"

    def _url(self, key: str) -> str:
        return f"{self.endpoint}/{self.bucket}/{urllib.parse.quote(check_key(key), safe='/')}"

    def _send(self, method: str, key: str, body: bytes = b"", extra: dict | None = None):
        url = self._url(key)
        payload_sha = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        amz_date = self._clock().strftime("%Y%m%dT%H%M%SZ")
        headers = {"host": urllib.parse.urlsplit(url).netloc,
                   "x-amz-content-sha256": payload_sha, "x-amz-date": amz_date}
        headers.update(extra or {})
        auth = sigv4_authorization(method, url, headers, payload_sha, self._key_id, self._secret, amz_date)
        req = urllib.request.Request(url, data=body if method == "PUT" else None, method=method,
                                     headers={**headers, "Authorization": auth})
        try:
            with self._open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""
        except Exception as exc:  # transport: report type only, never the request
            raise BlobStoreError(f"{method} {key}: {type(exc).__name__}") from None

    def exists(self, key: str) -> bool:
        status, _ = self._send("HEAD", key)
        if status == 200:
            return True
        if status == 404:
            return False
        raise BlobStoreError(f"HEAD {key} -> HTTP {status}")

    def put_if_absent(self, key: str, data: bytes, content_type: str = "application/gzip") -> bool:
        if self.exists(key):
            return False
        status, _ = self._send("PUT", key, data,
                               {"content-type": content_type, "if-none-match": "*"})
        if status == 412:  # someone wrote the same content-addressed key first
            return False
        if status != 200:
            raise BlobStoreError(f"PUT {key} -> HTTP {status}")
        return True

    def get(self, key: str) -> bytes:
        status, body = self._send("GET", key)
        if status != 200:
            raise BlobStoreError(f"GET {key} -> HTTP {status}")
        return body
