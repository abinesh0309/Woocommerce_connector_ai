"""OAuth 1.0a one-legged signing, compatible with WooCommerce's PHP verifier (HMAC-SHA256).

Used only for plain-``http://`` stores (WooCommerce accepts Basic auth over HTTPS only).
"""

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

Params = list[tuple[str, str]]


def rfc3986(value: str) -> str:
    """Percent-encode per RFC 3986 (PHP ``rawurlencode``): only ``A-Za-z0-9-._~`` stay literal."""
    return quote(str(value), safe="")


def normalize(params: Params) -> str:
    """Encode keys and values, then sort by the *encoded* key (strcmp order), then by value."""
    encoded = sorted((rfc3986(k), rfc3986(v)) for k, v in params)
    return "&".join(f"{k}={v}" for k, v in encoded)


def signature(method: str, base_url: str, params: Params, consumer_secret: str) -> str:
    base = "&".join([method.upper(), rfc3986(base_url), rfc3986(normalize(params))])
    key = (rfc3986(consumer_secret) + "&").encode()  # trailing '&': there is no token secret
    digest = hmac.new(key, base.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def sign_url(
    method: str,
    base_url: str,
    query: Params,
    consumer_key: str,
    consumer_secret: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> str:
    """Return the full request URL with the OAuth parameters (and signature) in the query string.

    A fresh nonce and timestamp are generated on every call, so call this once per *attempt*:
    WooCommerce rejects a reused nonce.
    """
    oauth: Params = [
        ("oauth_consumer_key", consumer_key),
        ("oauth_timestamp", str(timestamp if timestamp is not None else int(time.time()))),
        ("oauth_nonce", nonce or secrets.token_hex(16)),
        ("oauth_signature_method", "HMAC-SHA256"),
    ]
    all_params = [*query, *oauth]
    sig = signature(method, base_url, all_params, consumer_secret)
    all_params.append(("oauth_signature", sig))
    return base_url + "?" + "&".join(f"{rfc3986(k)}={rfc3986(v)}" for k, v in all_params)
