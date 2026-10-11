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


def test_dry_run_previews_without_saving(store):
    preview = store.create_lead("Ada Lovelace", idempotency_key="d1", dry_run=True)
    assert preview.dry_run is True
    assert preview.name == "Ada Lovelace"
    assert store.search_leads().leads == []
    with pytest.raises(CRMError, match="No lead"):
        store.get_lead(preview.id)

    lead = store.create_lead("Ada Lovelace", idempotency_key="d2")
    moved = store.update_lead_status(lead.id, "won", idempotency_key="d3", dry_run=True)
    note = store.add_note(lead.id, "Signed.", idempotency_key="d4", dry_run=True)
    assert (moved.status, moved.dry_run, note.dry_run) == ("won", True, True)

    detail = store.get_lead(lead.id)
    assert detail.status == "new"
    assert detail.notes == []
    assert [e["operation"] for e in store.audit_entries(lead.id)] == ["create_lead"]


def test_dry_run_leaves_the_key_free_for_the_real_write(store):
    store.create_lead("Grace Hopper", idempotency_key="approve-me", dry_run=True)
    lead = store.create_lead("Grace Hopper", idempotency_key="approve-me")
    assert lead.dry_run is False
    assert store.get_lead(lead.id).name == "Grace Hopper"


def test_dry_run_reports_the_same_errors_as_the_real_write(store):
    with pytest.raises(CRMError, match="No lead"):
        store.add_note("lead_missing", "hello", idempotency_key="e1", dry_run=True)
    lead = store.create_lead("Alan Turing", idempotency_key="e2")
    with pytest.raises(CRMError, match="already used for create_lead"):
        store.add_note(lead.id, "hello", idempotency_key="e2", dry_run=True)


def test_lead_history_lists_saved_writes_in_order(store):
    lead = store.create_lead("Hedy Lamarr", idempotency_key="h1")
    store.update_lead_status(lead.id, "contacted", idempotency_key="h2")
    store.update_lead_status(lead.id, "won", idempotency_key="h3", dry_run=True)
    history = store.get_lead_history(lead.id)
    assert [e.operation for e in history.entries] == ["create_lead", "update_lead_status"]
    assert history.entries[1].detail == {"from": "new", "to": "contacted"}
    with pytest.raises(CRMError, match="search_leads"):
        store.get_lead_history("lead_nope")


def test_writes_are_audited(store):
    lead = store.create_lead("Hedy Lamarr", idempotency_key="a1")
    store.update_lead_status(lead.id, "contacted", idempotency_key="a2")
    ops = [e["operation"] for e in store.audit_entries(lead.id)]
    assert ops == ["create_lead", "update_lead_status"]
