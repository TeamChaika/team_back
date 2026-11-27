"""Shared strict source attribution for digest and interactive receipts."""

from pathlib import Path
from uuid import UUID

PROCUREMENT_GROUP = UUID("231a6b55-3b64-4b80-ad84-b97881bda814")
EXTERNAL_PREDICATE = """t.source_id = 'primary' AND t.status = 'PROCESSED'
      AND nullif(t.details ->> 'linked_outgoing_invoice_id', '') IS NULL
      AND NOT coalesce(s.represents_store, false) AND s.represented_store_id IS NULL"""
ROUTING = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")


def routing_ctes(*, schema, day_pair_window, destination_guard, receipt_end, procurement_group):
    return ROUTING.format(
        schema=schema,
        day_pair_window=day_pair_window,
        destination_guard=destination_guard,
        receipt_end=receipt_end,
        procurement_group=procurement_group,
        external_predicate=EXTERNAL_PREDICATE,
    )
