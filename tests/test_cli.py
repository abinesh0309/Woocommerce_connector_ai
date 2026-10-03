import json
from pathlib import Path

import httpx
import pytest

from tests.conftest import API, STORE, page_response
from woo_connector import cli
from woo_connector.auth import credentials
from woo_connector.auth.credentials import Credentials

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for var in ("WOO_CONSUMER_KEY", "WOO_CONSUMER_SECRET", "WOO_STORE_URL", "WOO_KEY_PERMISSIONS"):
        monkeypatch.delenv(var, raising=False)


def test_spec_summary_counts_tools(capsys):
    assert cli.main(["spec", "--summary"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("14 tools, all read-only") and "get_order_tracking" in out


def test_committed_spec_is_not_stale(tmp_path):
    fresh = tmp_path / "spec.json"
    assert cli.main(["spec", "-o", str(fresh)]) == 0
    assert json.loads(fresh.read_text()) == json.loads((ROOT / "mcp-tool-spec.json").read_text()), (
        "run `uv run woo-connector spec` and commit mcp-tool-spec.json"
    )


def test_spec_contents(tmp_path):
    out = tmp_path / "s.json"
    cli.main(["spec", "-o", str(out)])
    spec = json.loads(out.read_text())
    assert spec["name"] == "woo-connector" and "stdio" in spec["transports"][0]
    for tool in spec["tools"]:
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["type"] == "object" and tool["outputSchema"]["type"] == "object"


def test_status_not_connected(capsys):
    assert cli.main(["status"]) == 1
    assert "woo-connector auth" in capsys.readouterr().err


def test_status_ok_and_auth_failure(router, monkeypatch, capsys):
    credentials.save(Credentials(STORE, "ck", "cs", "read"))
    route = router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([{"id": 1}], total=41))
    assert cli.main(["--store", STORE, "status"]) == 0
    assert "Orders visible to this key: 41" in capsys.readouterr().out and dict(route.calls.last.request.url.params)["per_page"] == "1"
    route.mock(return_value=httpx.Response(401, json={"code": "woocommerce_rest_cannot_view", "message": "bad"}))
    assert cli.main(["--store", STORE, "status"]) == 1
    assert "reconnect" in capsys.readouterr().err


def test_auth_manual_validates_before_saving(router, monkeypatch, capsys):
    monkeypatch.setenv("WOO_CONSUMER_KEY", "ck_pasted")
    monkeypatch.setenv("WOO_CONSUMER_SECRET", "cs_pasted")
    router.get(f"{API}/wc/v3/orders").mock(return_value=httpx.Response(401, json={"message": "bad"}))
    assert cli.main(["--store", STORE, "auth", "--manual"]) == 1
    assert credentials.saved_stores() == []  # a bad key is never saved
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([], total=3))
    assert cli.main(["--store", STORE, "auth", "--manual"]) == 0
    assert credentials.saved_stores() == [STORE] and "Key scope is unknown" in capsys.readouterr().out


def test_auth_requires_store(capsys):
    assert cli.main(["auth"]) == 2


def test_forget(capsys):
    credentials.save(Credentials(STORE, "ck", "cs", "read"))
    assert cli.main(["forget"]) == 0
    out = capsys.readouterr().out
    assert "Revoke" in out and credentials.saved_stores() == []
    assert cli.main(["forget"]) == 1
