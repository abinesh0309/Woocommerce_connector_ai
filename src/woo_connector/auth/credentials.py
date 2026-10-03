"""Credential storage: one JSON file keyed by normalised store URL, or environment variables."""

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit


class NotConnected(Exception):
    """No credentials are available for the requested store. The message says how to connect."""


@dataclass(frozen=True)
class Credentials:
    store_url: str
    consumer_key: str
    consumer_secret: str
    key_permissions: str = "unknown"  # read | write | read_write | unknown (never guessed for pasted keys)

    def __repr__(self) -> str:  # never leak secrets into logs / tracebacks
        return f"Credentials(store_url={self.store_url!r}, consumer_key='***', consumer_secret='***', key_permissions={self.key_permissions!r})"


def normalize_store_url(url: str) -> str:
    """``Shop.Example.com/`` -> ``https://shop.example.com``. Keeps a sub-directory install path."""
    url = url.strip()
    if not url:
        raise ValueError("Store URL is empty.")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"'{url}' is not a valid http(s) store URL.")
    path = parts.path.rstrip("/")
    for suffix in ("/wp-json", "/wp-admin"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}{path}"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "woo-connector"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def _read_all() -> dict[str, dict[str, str]]:
    path = credentials_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        raise NotConnected(f"Credentials file {path} is unreadable ({exc}). Run `woo-connector auth` again.") from exc
    stores = data.get("stores", {})
    return stores if isinstance(stores, dict) else {}


def _write_all(stores: dict[str, dict[str, str]]) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # permission bits are largely a no-op on Windows
        os.chmod(directory, 0o700)
    payload = json.dumps({"stores": stores}, indent=2).encode()
    # Atomic: create a private temp file (O_EXCL, 0600) then rename over the target.
    fd, tmp = tempfile.mkstemp(prefix=".credentials-", dir=directory)
    try:
        if hasattr(os, "fchmod"):  # POSIX only; mkstemp already creates the file 0600
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, credentials_path())
    except BaseException:
        with contextlib.suppress(OSError):  # on Windows an open file cannot be deleted; do not mask the real error
            os.unlink(tmp)
        raise


def save(creds: Credentials) -> Path:
    stores = _read_all()
    stores[creds.store_url] = {
        "consumer_key": creds.consumer_key,
        "consumer_secret": creds.consumer_secret,
        "key_permissions": creds.key_permissions,
    }
    _write_all(stores)
    return credentials_path()


def forget(store_url: str) -> bool:
    store_url = normalize_store_url(store_url)
    stores = _read_all()
    if store_url not in stores:
        return False
    del stores[store_url]
    if stores:
        _write_all(stores)
    else:
        with contextlib.suppress(FileNotFoundError):
            credentials_path().unlink()
    return True


def saved_stores() -> list[str]:
    return sorted(_read_all())


def load(store_url: str | None = None) -> Credentials:
    """Environment variables win over the file. Without a store URL, use the only saved store."""
    env_key = os.environ.get("WOO_CONSUMER_KEY")
    env_secret = os.environ.get("WOO_CONSUMER_SECRET")
    store_url = store_url or os.environ.get("WOO_STORE_URL")

    if env_key and env_secret:
        if not store_url:
            raise NotConnected("WOO_CONSUMER_KEY/WOO_CONSUMER_SECRET are set but the store URL is missing. Set WOO_STORE_URL or pass --store.")
        return Credentials(
            normalize_store_url(store_url),
            env_key,
            env_secret,
            os.environ.get("WOO_KEY_PERMISSIONS", "unknown"),
        )

    stores = _read_all()
    if store_url:
        key = normalize_store_url(store_url)
    elif len(stores) == 1:
        key = next(iter(stores))
    elif not stores:
        raise NotConnected(
            "No store is connected. Run `woo-connector auth --store https://your-shop.example` (or set WOO_STORE_URL, WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET)."
        )
    else:
        raise NotConnected("Several stores are saved; choose one with --store or WOO_STORE_URL: " + ", ".join(sorted(stores)))

    entry = stores.get(key)
    if not entry:
        raise NotConnected(f"No saved credentials for {key}. Run `woo-connector auth --store {key}`.")
    return Credentials(key, entry["consumer_key"], entry["consumer_secret"], entry.get("key_permissions", "unknown"))


def as_dict(creds: Credentials) -> dict[str, str]:  # pragma: no cover - debugging helper, secrets redacted
    d = asdict(creds)
    d["consumer_key"] = d["consumer_secret"] = "***"
    return d
