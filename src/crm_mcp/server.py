"""MCP server exposing the lead store as tools an AI agent can call.

Read tools and write tools are separate, and each carries MCP annotations
(read-only, idempotent, destructive) so a client can decide what needs a person's approval.
"""

from __future__ import annotations

import argparse
import os
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from crm_mcp.models import Lead, LeadDetail, LeadPage, LeadStatus, Note
from crm_mcp.store import MAX_PAGE_SIZE, CRMError, LeadStore

INSTRUCTIONS = """\
Tools for a small CRM of sales leads.
Find a lead with search_leads before reading or changing it; never guess an id.
Every write needs an idempotency_key: make a new one for each intended change and
reuse it only when retrying that same change.
"""

READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

IdempotencyKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        description="Unique per intended change (a UUID works). Reuse it only to retry the same change.",
    ),
]


def build_server(store: LeadStore) -> MCPServer:
    server = MCPServer("crm-mcp-server", instructions=INSTRUCTIONS, version="0.1.0")

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CRMError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=READ)
    def search_leads(
        query: Annotated[str | None, Field(description="Text to match in name, email or company.")] = None,
        status: Annotated[LeadStatus | None, Field(description="Only return leads with this status.")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description="Page size.")] = 20,
        cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
    ) -> LeadPage:
        """Search leads by text and status. Results are paginated; follow next_cursor for more."""
        return run(store.search_leads, query=query, status=status, limit=limit, cursor=cursor)

    @server.tool(annotations=READ)
    def get_lead(lead_id: Annotated[str, Field(description="Lead id from search_leads.")]) -> LeadDetail:
        """Get one lead with all of its notes."""
        return run(store.get_lead, lead_id)

    @server.tool(annotations=WRITE)
    def create_lead(
        name: Annotated[str, Field(min_length=1, description="Person or company name.")],
        idempotency_key: IdempotencyKey,
        email: Annotated[str | None, Field(description="Contact email, if known.")] = None,
        company: Annotated[str | None, Field(description="Company name, if known.")] = None,
    ) -> Lead:
        """Create a new lead with status 'new'. Search first so you do not create a duplicate."""
        return run(store.create_lead, name=name, idempotency_key=idempotency_key, email=email, company=company)

    @server.tool(annotations=WRITE)
    def update_lead_status(
        lead_id: Annotated[str, Field(description="Lead id from search_leads.")],
        status: LeadStatus,
        idempotency_key: IdempotencyKey,
    ) -> Lead:
        """Move a lead to a new pipeline status."""
        return run(store.update_lead_status, lead_id, status, idempotency_key)

    @server.tool(annotations=WRITE)
    def add_note(
        lead_id: Annotated[str, Field(description="Lead id from search_leads.")],
        body: Annotated[str, Field(min_length=1, max_length=5000, description="Note text.")],
        idempotency_key: IdempotencyKey,
    ) -> Note:
        """Add a note to a lead, for example a call summary."""
        return run(store.add_note, lead_id, body, idempotency_key)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CRM MCP server.")
    parser.add_argument("--db", default=os.environ.get("CRM_MCP_DB", "crm.db"), help="SQLite file path.")
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio", help="MCP transport."
    )
    args = parser.parse_args()
    build_server(LeadStore(args.db)).run(transport=args.transport)


if __name__ == "__main__":
    main()
