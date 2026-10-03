import os
import stat
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from woo_connector.auth import credentials
from woo_connector.auth.connect import ConsentError, ConsentSession, manual_credentials
from woo_connector.auth.credentials import Credentials, NotConnected, normalize_store_url

KEY = {"key_id": 1, "consumer_key": "ck_abc", "consumer_secret": "cs_def", "key_permissions": "read"}


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for var in ("WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET", "WOO_STORE_URL", "WOO_KEY_PERMISSIONS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "cfg"


@pytest.fixture
def session():
    s = ConsentSession("https://Shop.Test/", timeout=3)
    url = s.start()
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    yield s, url, q
    s.close()


def post_key(q, state=None, body=None):
    with httpx.Client(verify=False) as http:  # self-signed throwaway cert
        return http.post(q["callback_url"], json=body or {**KEY, "user_id": state or q["user_id"]})


def land(q, **params):
    with httpx.Client() as http:
        return http.get(q["return_url"], params={"user_id": q["user_id"], **params})


def test_authorize_url_shape(session):
    s, url, q = session
    assert url.startswith("https://shop.test/wc-auth/v1/authorize?")
    assert q["scope"] == "read" and len(q["user_id"]) == 32
    assert q["callback_url"].startswith("https://") and q["return_url"].startswith("http://")


def test_happy_path_saves_only_after_browser_returns(session, config_home):
    s, _, q = session
    assert post_key(q).status_code == 200
    assert not (config_home / "woo-connector" / "credentials.json").exists()  # key is only held in memory so far
    assert land(q, success="1").status_code == 200
    creds = s.wait()
    assert (creds.consumer_key, creds.key_permissions, creds.store_url) == ("ck_abc", "read", "https://shop.test")


def test_wrong_state_on_callback_is_403_and_key_is_discarded(session):
    s, _, q = session
    assert post_key(q, state="attacker" * 4).status_code == 403
    assert land(q, success="1").status_code == 400  # nothing was held, so there is nothing to save
    with pytest.raises(ConsentError):
        s.wait()


def test_wrong_state_on_return_is_403(session):
    s, _, q = session
    post_key(q)
    with httpx.Client() as http:
        assert http.get(q["return_url"], params={"user_id": "nope", "success": "1"}).status_code == 403


def test_failure_after_callback_discards_the_key(session):
    s, _, q = session
    post_key(q)
    assert land(q, success="0").status_code == 400
    with pytest.raises(ConsentError, match="did not complete"):
        s.wait()


def test_timeout_message():
    s = ConsentSession("https://shop.test", timeout=0.2)
    s.start()
    try:
        with pytest.raises(ConsentError, match="Timed out"):
            s.wait()
    finally:
        s.close()


def test_non_https_callback_is_rejected():
    with pytest.raises(ConsentError, match="https"):
        ConsentSession("https://shop.test", callback_url="http://tunnel.example/cb")


def test_malformed_callback_bodies(session):
    _, _, q = session
    with httpx.Client(verify=False) as http:
        assert http.post(q["callback_url"], content=b"{not json", headers={"content-type": "application/json"}).status_code == 400
        assert http.post(q["callback_url"], json={"user_id": q["user_id"]}).status_code == 400  # no key


def test_credentials_file_is_private_atomic_and_multi_store(config_home):
    credentials.save(Credentials("https://a.test", "ck_a", "cs_a", "read"))
    path = credentials.save(Credentials("https://b.test", "ck_b", "cs_b", "read"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["credentials.json"]  # no temp files left behind
    assert credentials.saved_stores() == ["https://a.test", "https://b.test"]
    assert credentials.load("https://b.test").consumer_key == "ck_b"
    with pytest.raises(NotConnected, match="Several stores"):
        credentials.load()
    assert credentials.forget("https://a.test") and not credentials.forget("https://a.test")
    assert credentials.load().store_url == "https://b.test"  # now unambiguous


def test_env_overrides_file_and_scope_is_not_guessed(config_home, monkeypatch):
    credentials.save(Credentials("https://a.test", "ck_file", "cs_file", "read"))
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck_env")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs_env")
    c = credentials.load("https://a.test")
    assert (c.consumer_key, c.key_permissions) == ("ck_env", "unknown")
    monkeypatch.setenv("WOO_KEY_PERMISSIONS", "read")
    assert credentials.load("https://a.test").key_permissions == "read"
    monkeypatch.delenv("WOO_STORE_URL", raising=False)
    with pytest.raises(NotConnected, match="store URL is missing"):
        credentials.load()


def test_not_connected_message_tells_what_to_do():
    with pytest.raises(NotConnected, match="woo-connector auth"):
        credentials.load()


def test_corrupt_credentials_file(config_home):
    path = credentials.credentials_path()
    path.parent.mkdir(parents=True)
    path.write_text("{oops")
    with pytest.raises(NotConnected, match="unreadable"):
        credentials.load("https://a.test")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Shop.Example.com/", "https://shop.example.com"),
        ("http://localhost:8080/", "http://localhost:8080"),
        ("https://x.test/blog/wp-json/", "https://x.test/blog"),
        ("https://x.test/wp-admin", "https://x.test"),
    ],
)
def test_normalize_store_url(raw, expected):
    assert normalize_store_url(raw) == expected


@pytest.mark.parametrize("bad", ["", "ftp://x.test", "https://"])
def test_normalize_rejects_bad_urls(bad):
    with pytest.raises(ValueError):
        normalize_store_url(bad)


def test_manual_credentials_never_guess_scope():
    c = manual_credentials("shop.test", "ck_1", "cs_1")
    assert c.key_permissions == "unknown" and c.store_url == "https://shop.test"
    with pytest.raises(ConsentError):
        manual_credentials("shop.test", "", "")


def test_callback_server_runs_off_the_main_thread(session):
    assert threading.active_count() > 1


def test_saving_works_where_os_has_no_fchmod(config_home, monkeypatch):
    """Windows has no os.fchmod; this crashed `auth --manual` there."""
    monkeypatch.delattr(os, "fchmod")
    path = credentials.save(Credentials("https://a.test", "ck_a", "cs_a", "read"))
    assert credentials.load("https://a.test").consumer_key == "ck_a" and path.exists()
