"""Inspect the integration journal or create an audited retry of a failed event.

Examples: python -m scripts.integration_operations list --status failed
          python -m scripts.integration_operations show 123
          python -m scripts.integration_operations retry 123 --reason "Catalog form fixed"
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from sqlalchemy import text

from common.config import get_settings
from common.db import get_db_session, shutdown_db


SUMMARY_COLUMNS = "id, event_type, reg, source_topic, source_partition, source_offset, array_index, status, attempts, received_at, available_at, completed_at, error_code, retry_of, retry_reason"


async def inspect_operations(args):
    settings = get_settings()
    namespace = settings.INTEGRATION_SOURCE_NAMESPACE
    async with get_db_session() as session:
        if args.command == "retry":
            async with session.begin():
                # Preserve the original result/outbox. A retry has its own event
                # row and output identities; it cannot bypass revision checks.
                result = await session.execute(text("""
                    INSERT INTO integration_events (
                        source_namespace, dedupe_key, event_id, fingerprint,
                        source_topic, source_partition, source_offset, array_index,
                        event_type, entity_key, reg, source_revision, revision_stream,
                        payload, disposition, deadline_at, retry_of, retry_reason)
                    SELECT source_namespace, :dedupe_key, event_id, fingerprint,
                        source_topic, source_partition, source_offset, array_index,
                        event_type, entity_key, reg, source_revision, revision_stream,
                        payload, disposition, :deadline, id, :reason
                    FROM integration_events WHERE id = :id AND source_namespace = :namespace
                        AND status = 'failed'
                    RETURNING id, retry_of, status
                """), {"id": args.id, "namespace": namespace,
                       "dedupe_key": f"manual-retry:{args.id}:{uuid4()}", "reason": args.reason,
                       "deadline": datetime.now(timezone.utc) + timedelta(seconds=settings.INTEGRATION_EVENT_DEADLINE_SECONDS)})
                row = result.mappings().first()
                if row is None:
                    raise ValueError("Only an existing failed operation in this namespace can be retried")
                return dict(row)
        if args.command == "show":
            result = await session.execute(text(
                f"SELECT {SUMMARY_COLUMNS}, error_message, result FROM integration_events "
                "WHERE id = :id AND source_namespace = :namespace"
            ), {"id": args.id, "namespace": namespace})
            row = result.mappings().first()
            if row is None:
                raise ValueError("Operation not found")
            output = dict(row)
            # Stored business results may echo sensitive payloads; expose only
            # outgoing delivery identities/topics by default.
            outgoing = (output.pop("result") or {}).get("outgoing", [])
            output["outgoing"] = [{"event_id": item.get("event_id"), "topic": item.get("topic")} for item in outgoing]
            return output
        if args.command == "list":
            result = await session.execute(text(
                f"SELECT {SUMMARY_COLUMNS} FROM integration_events WHERE source_namespace = :namespace "
                "AND (:status IS NULL OR status = :status) ORDER BY id DESC LIMIT :limit"
            ), {"namespace": namespace, "status": args.status, "limit": args.limit})
            return [dict(row) for row in result.mappings()]
        events = await session.execute(text(
            "SELECT status, count(*) AS count, min(received_at) AS oldest FROM integration_events "
            "WHERE source_namespace = :namespace GROUP BY status ORDER BY status"
        ), {"namespace": namespace})
        outbox = await session.execute(text(
            "SELECT count(*) AS pending, min(o.created_at) AS oldest FROM integration_outbox o "
            "JOIN integration_events e ON e.id=o.operation_id "
            "WHERE e.source_namespace=:namespace AND o.sent_at IS NULL"
        ), {"namespace": namespace})
        return {"events": [dict(row) for row in events.mappings()], "outbox": dict(outbox.mappings().one())}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=["received", "waiting_dependency", "retry_wait", "running", "succeeded", "failed", "conflict", "superseded"])
    listing.add_argument("--limit", type=int, default=50)
    showing = commands.add_parser("show")
    showing.add_argument("id", type=int)
    retry = commands.add_parser("retry")
    retry.add_argument("id", type=int)
    retry.add_argument("--reason", required=True)
    commands.add_parser("summary")
    return root


async def run(args):
    try:
        return await inspect_operations(args)
    finally:
        await shutdown_db()


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    if args.command == "list" and not 1 <= args.limit <= 1000:
        root.error("--limit must be between 1 and 1000")
    if args.command == "retry" and not args.reason.strip():
        root.error("--reason must not be blank")
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
