import pytest

from crm_mcp.store import CRMError, LeadStore


@pytest.fixture
def store():
    s = LeadStore(":memory:")
    yield s
    s.close()


def test_create_and_get_lead(store):
    lead = store.create_lead("Ada Lovelace", idempotency_key="k1", email="ada@example.com", company="Engines Ltd")
    assert lead.status == "new"
    detail = store.get_lead(lead.id)
    assert detail.name == "Ada Lovelace"
    assert detail.notes == []


def test_same_idempotency_key_does_not_write_twice(store):
    first = store.create_lead("Grace Hopper", idempotency_key="retry-me")
    second = store.create_lead("Grace Hopper", idempotency_key="retry-me")
    assert first.id == second.id
    assert len(store.search_leads(query="Grace").leads) == 1


def test_idempotency_key_reused_for_other_operation_is_rejected(store):
    lead = store.create_lead("Alan Turing", idempotency_key="k-shared")
    with pytest.raises(CRMError, match="already used for create_lead"):
        store.add_note(lead.id, "hello", idempotency_key="k-shared")


def test_failed_write_does_not_burn_the_key(store):
    with pytest.raises(CRMError, match="No lead"):
        store.add_note("lead_missing", "hello", idempotency_key="k-fail")
    lead = store.create_lead("Katherine Johnson", idempotency_key="k-ok")
    note = store.add_note(lead.id, "hello", idempotency_key="k-fail")
    assert note.lead_id == lead.id


def test_cursor_pagination_walks_every_lead_once(store):
    for i in range(7):
        store.create_lead(f"Lead {i}", idempotency_key=f"c{i}")
    seen, cursor = [], None
    while True:
        page = store.search_leads(limit=3, cursor=cursor)
        seen += [lead.name for lead in page.leads]
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert seen == [f"Lead {i}" for i in range(7)]


def test_search_treats_wildcard_characters_as_text(store):
    discount = store.create_lead("Ada Lovelace", idempotency_key="w1", company="50% Off Ltd")
    snake = store.create_lead("Grace Hopper", idempotency_key="w2", email="grace_h@example.com")
    assert [x.id for x in store.search_leads(query="%").leads] == [discount.id]
    assert [x.id for x in store.search_leads(query="_").leads] == [snake.id]
    assert [x.id for x in store.search_leads(query="50% off").leads] == [discount.id]


def test_status_filter_and_update(store):
    lead = store.create_lead("Margaret Hamilton", idempotency_key="s1")
    store.update_lead_status(lead.id, "qualified", idempotency_key="s2")
    assert [x.id for x in store.search_leads(status="qualified").leads] == [lead.id]
    assert store.search_leads(status="new").leads == []


def test_errors_tell_the_caller_what_to_do(store):
    with pytest.raises(CRMError, match="Use one of: new, contacted"):
        store.search_leads(status="hot")
    with pytest.raises(CRMError, match="next_cursor"):
        store.search_leads(cursor="not-a-cursor")
    with pytest.raises(CRMError, match="search_leads"):
        store.get_lead("lead_nope")


def test_writes_are_audited(store):
    lead = store.create_lead("Hedy Lamarr", idempotency_key="a1")
    store.update_lead_status(lead.id, "contacted", idempotency_key="a2")
    ops = [e["operation"] for e in store.audit_entries(lead.id)]
    assert ops == ["create_lead", "update_lead_status"]
