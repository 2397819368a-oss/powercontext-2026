"""A runnable local-file collector for dynamic ticket targets."""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field

from powercontext.source_management import Capability, TargetIdentity
from powercontext.sources import ConnectorRunCompletion, ConnectorRunStatus, ConnectorSubmissionStatus

from .definition import TicketCapture, TicketDefinition

plugin_api_version = "2"
connector_name = "tickets"
connector_version = "1"
ticket_definition = TicketDefinition()
definitions = (ticket_definition,)
capabilities = (
    Capability(
        name="tickets.item",
        version="1",
        description="Collect a ticket from the local JSON file",
        input_schema={
            "type": "object",
            "properties": {"id": {"type": "string", "minLength": 1}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    Capability(
        name="ticket.write",
        version="1",
        kind="write",
        description="Save complete ticket content supplied by the host",
        input_schema=TicketCapture.model_json_schema(),
        definition_name=ticket_definition.name,
        definition_version=ticket_definition.version,
    ),
)


class Config(BaseModel):
    file: str = "tickets.json"
    namespace: str = "tickets"
    project: str = "default"
    poll_interval: float = Field(default=1, gt=0)


async def identify_target(request, config):
    identifier = request.input["id"].strip()
    if not identifier:
        raise ValueError("Ticket ID must not be blank")  # noqa: TRY003
    return TargetIdentity(
        target_key=f"{config.namespace}:{config.project}:{identifier}", normalized_input={"id": identifier}
    )


class TicketConnector:
    name = connector_name
    version = connector_version
    source_definitions = frozenset({ticket_definition.name})

    def __init__(self, ctx, config):
        self.ctx, self.config, self.targets = ctx, config, ()

    async def run(self, session):
        records = json.loads((self.ctx.plugin_dir / self.config.file).read_text(encoding="utf-8"))
        values = {str(record["id"]): record for record in records}
        seen = dict((session.checkpoint or {}).get("seen", {}))
        complete = True
        for target in self.targets:
            ticket = values.get(target.input["id"])
            if ticket is None:
                continue
            content = ticket.get("body", "")
            capture = TicketCapture(
                namespace=self.config.namespace,
                project=self.config.project,
                ticket_id=target.input["id"],
                title=ticket.get("title", ""),
                body=content,
                labels=ticket.get("labels", []),
                metadata=ticket.get("metadata", {}),
            )
            digest = capture.snapshot_id()
            if seen.get(target.target_id) == digest:
                continue
            receipt = await self.ctx.targets.submit(
                target,
                session=session,
                item_id=target.target_id,
                definition_name=ticket_definition.name,
                value=capture,
            )
            accepted = receipt.status == ConnectorSubmissionStatus.ACCEPTED

            complete = complete and accepted
            if accepted:
                seen[target.target_id] = digest
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE if complete else ConnectorRunStatus.INCOMPLETE, checkpoint={"seen": seen}
        )


async def apply(ctx, config):
    connector = TicketConnector(ctx, config)
    ctx.connect(connector)
    while True:
        targets = await ctx.targets.list()
        for scope_id in sorted({target.scope_id for target in targets}):
            selected = tuple(target for target in targets if target.scope_id == scope_id)
            connector.targets = selected
            await ctx.run_batch(scope_id=scope_id, targets=selected)
        await asyncio.sleep(config.poll_interval)
