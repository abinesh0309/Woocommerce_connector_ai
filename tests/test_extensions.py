import json

import httpx
import pytest

from tests.conftest import API, order, page_response
from woo_connector import agent
from woo_connector.agent import AgentError, ask
from woo_connector.logging_setup import JsonFormatter
from woo_connector.tools import common

URL = agent.OPENROUTER_URL


def reply(content=None, tool_calls=None):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}]})


def call(name, args, id="c1"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}


async def run(question="hi", **kw):
    async with httpx.AsyncClient() as http:
        return await ask(question, http=http, api_key="sk-or-test", **kw)


async def test_tool_call_then_answer(tools_ready, router):
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([order()]))
    llm = router.post(URL).mock(side_effect=[reply(tool_calls=[call("list_orders", {"status": "processing"})]), reply("You have 1 processing order.")])
    answer, history, used = await run("what's processing?")
    assert answer == "You have 1 processing order." and used == ["list_orders"]
    first = json.loads(llm.calls[0].request.content)
    assert first["tool_choice"] == "auto" and first["model"] == agent.DEFAULT_MODEL and len(first["tools"]) == 14
    assert first["tools"][0]["type"] == "function" and set(first["tools"][0]["function"]) == {"name", "description", "parameters"}
    assert first["messages"][0]["role"] == "system" and "never invent" in first["messages"][0]["content"].lower()
    headers = llm.calls[0].request.headers
    assert headers["authorization"] == "Bearer sk-or-test" and headers["x-title"].startswith("woo-connector") and headers["http-referer"]
    tool_msg = json.loads(llm.calls[1].request.content)["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1" and "d***@example.com" in tool_msg["content"]


async def test_tool_error_is_passed_back_so_the_model_can_recover(tools_ready, router):
    router.get(f"{API}/wc/v3/orders/999999").mock(return_value=httpx.Response(404, json={"code": "x", "message": "Invalid ID."}))
    router.get(f"{API}/wc/v3/orders/999999/notes").mock(return_value=httpx.Response(404, json={"code": "x", "message": "Invalid ID."}))
    llm = router.post(URL).mock(side_effect=[reply(tool_calls=[call("get_order", {"order_id": 999999})]), reply("That order does not exist.")])
    answer, _, _ = await run("order 999999?")
    assert answer == "That order does not exist."
    assert "does not exist. Use search_orders" in json.loads(llm.calls[1].request.content)["messages"][-1]["content"]


async def test_bad_json_arguments_do_not_crash(tools_ready, router):
    llm = router.post(URL).mock(side_effect=[reply(tool_calls=[call("get_order", "{not json")]), reply("Sorry, retrying.")])
    answer, _, _ = await run()
    assert answer == "Sorry, retrying."
    assert "Invalid tool arguments" in json.loads(llm.calls[1].request.content)["messages"][-1]["content"]


async def test_runaway_loop_is_stopped(tools_ready, router):
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([]))
    llm = router.post(URL).mock(return_value=reply(tool_calls=[call("list_orders", {})]))
    with pytest.raises(AgentError, match="Stopped after 8"):
        await run()
    assert llm.call_count == agent.MAX_STEPS


async def test_large_tool_results_are_truncated(tools_ready, router):
    router.get(f"{API}/wc/v3/orders").mock(return_value=page_response([order(i) for i in range(1, 101)]))
    llm = router.post(URL).mock(side_effect=[reply(tool_calls=[call("list_orders", {"per_page": 100})]), reply("ok")])
    await run()
    assert len(json.loads(llm.calls[1].request.content)["messages"][-1]["content"]) <= agent.MAX_TOOL_CHARS + 20


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401, json={"error": {"message": "No auth"}}), "OPENROUTER_API_KEY"),
        (httpx.Response(429, json={}), "rate limiting"),
        (httpx.Response(500, text="boom"), "server error"),
        (httpx.Response(200, json={"error": {"message": "Model not found"}}), "Model not found"),
        (httpx.Response(200, text="<html>"), "non-JSON"),
        (httpx.Response(200, json={"weird": 1}), "unexpected response"),
    ],
)
async def test_openrouter_failures_become_readable_errors(tools_ready, router, response, message):
    router.post(URL).mock(return_value=response)
    with pytest.raises(AgentError, match=message):
        await run()


async def test_missing_key_is_explained(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(AgentError, match="OPENROUTER_API_KEY"):
        await ask("hi")


async def test_history_is_kept_between_turns(tools_ready, router):
    router.post(URL).mock(side_effect=[reply("first"), reply("second")])
    _, history, _ = await run("one")
    _, history, _ = await run("two", history=history)
    assert [m["role"] for m in history] == ["system", "user", "assistant", "user", "assistant"]


def test_capabilities_doc_contains_the_agent_system_prompt():
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "CAPABILITIES.md").read_text()
    assert agent.SYSTEM_PROMPT.strip() in doc, "CAPABILITIES.md must embed the exact suggested agent instructions"


def test_json_log_format_has_no_multiline_leak():
    import logging

    rec = logging.LogRecord("x", logging.INFO, "f", 1, "hello %s", ("w",), None)
    assert json.loads(JsonFormatter().format(rec))["msg"] == "hello w"


def test_pii_mode_defaults_to_masked_and_rejects_garbage(monkeypatch):
    common.configure()
    monkeypatch.delenv("WOO_PII_MODE", raising=False)
    assert common.runtime.pii == "masked"
    monkeypatch.setenv("WOO_PII_MODE", "everything")
    assert common.runtime.pii == "masked"
    monkeypatch.setenv("WOO_PII_MODE", "full")
    assert common.runtime.pii == "full"
