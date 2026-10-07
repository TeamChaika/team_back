"""Payloads validated against unmodified XSDs retrieved from official iiko docs."""

from pathlib import Path
from uuid import UUID

import pytest

from app.commercial_invoices.calculation import calculate
from app.commercial_invoices.transport import CommercialTransport


@pytest.mark.parametrize("kind,schema_name", [("purchase", "incoming"), ("sale", "outgoing")])
@pytest.mark.parametrize("rate,included", [(None, True), ("0", True), ("20", True), ("22", False)])
def test_xml_matches_official_xsd(kind, schema_name, rate, included):
    etree = pytest.importorskip("lxml.etree", reason="Optional XSD validation dependency required")
    items, totals = calculate(
        [
            {
                "product_id": str(UUID(int=1)),
                "quantity": "1.337",
                "price": "100.01",
                "vat_rate": rate,
                "price_includes_vat": included,
            }
        ]
    )
    items[0].update(unit_id=str(UUID(int=2)), name="Synthetic", unit="шт.")
    snapshot = {
        "id": str(UUID(int=3)),
        "version": 2,
        "number": "CI-S-00000001",
        "date": "2026-10-07",
        "store_id": str(UUID(int=4)),
        "counterparty_id": str(UUID(int=5)),
        "external_number": "SYNTHETIC-1",
        "items": items,
        "totals": totals,
    }
    payload = CommercialTransport.payload(kind, snapshot)
    schema = etree.XMLSchema(
        etree.parse(str(Path("tests/fixtures") / f"commercial_{schema_name}.xsd"))
    )
    schema.assertValid(etree.fromstring(payload))
