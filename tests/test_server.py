import pytest
from mcp.server.mcpserver.exceptions import ToolError

from crm_mcp.server import build_server
from crm_mcp.store import LeadStore


@pytest.fixture
def server():
    store = LeadStore(":memory:")
    yield build_server(store)
    store.close()


async def test_tools_are_listed_with_annotations(server):
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == {"search_leads", "get_lead", "create_lead", "update_lead_status", "add_note"}
    assert tools["search_leads"].annotations.read_only_hint is True
    assert tools["create_lead"].annotations.read_only_hint is False
    assert tools["create_lead"].annotations.idempotent_hint is True


async def test_agent_flow_create_search_update_note(server):
    created = await server.call_tool("create_lead", {"name": "Ada Lovelace", "idempotency_key": "t1"})
    lead_id = created.structured_content["id"]

    found = await server.call_tool("search_leads", {"query": "Ada"})
    assert [lead["id"] for lead in found.structured_content["leads"]] == [lead_id]

    await server.call_tool(
        "update_lead_status", {"lead_id": lead_id, "status": "contacted", "idempotency_key": "t2"}
    )
    await server.call_tool("add_note", {"lead_id": lead_id, "body": "Intro call went well.", "idempotency_key": "t3"})

    detail = await server.call_tool("get_lead", {"lead_id": lead_id})
    assert detail.structured_content["status"] == "contacted"
    assert detail.structured_content["notes"][0]["body"] == "Intro call went well."


async def test_bad_input_returns_an_actionable_error(server):
    with pytest.raises(ToolError, match="search_leads to find the right id"):
        await server.call_tool("get_lead", {"lead_id": "lead_unknown"})
