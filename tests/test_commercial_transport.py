from copy import deepcopy
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

import httpx
import pytest

from app.commercial_invoices.calculation import calculate
from app.commercial_invoices.transport import CommercialTransport, matches


@pytest.fixture
def snapshot():
    items, totals = calculate(
        [
            {
                "product_id": "00000000-0000-0000-0000-000000000010",
                "quantity": "2",
                "price": "100.25",
                "vat_rate": "22",
                "price_includes_vat": False,
            }
        ]
    )
    items[0].update(unit_id="00000000-0000-0000-0000-000000000011", unit="кг", name="Товар")
    return {
        "id": "00000000-0000-0000-0000-000000000012",
        "number": "RC-10001",
        "version": 2,
        "date": "2026-10-07",
        "external_number": "П-1",
        "store_id": "00000000-0000-0000-0000-000000000013",
        "counterparty_id": "00000000-0000-0000-0000-000000000014",
        "comment": "<Только текст>",
        "items": items,
        "totals": totals,
    }


@pytest.mark.parametrize("kind", ["purchase", "sale"])
def test_invoice_prices_vat_and_quantity_are_real(kind, snapshot):
    root = fromstring(CommercialTransport.payload(kind, snapshot))
    item = root.find("items/item")
    assert item.findtext("price") == "122.3050"
    assert item.findtext("sum") == "244.61"
    assert item.findtext("vatSum") == "44.11"
    assert item.findtext("amount") == "2"
    assert root.findtext("status") == "PROCESSED"
    assert root.find("id") is None
    assert "<Только текст>" in root.findtext("comment")
    assert item.find("priceWithoutVat") is None
    if kind == "purchase":
        assert root.findtext("dateIncoming") == "07.10.2026"
        assert item.findtext("store") == snapshot["store_id"]
        assert item.findtext("amountUnit") == snapshot["items"][0]["unit_id"]
    else:
        assert item.find("storeId") is None


def test_no_vat_is_explicit_not_inherited(snapshot):
    snapshot["items"][0].update(vat_rate=None, price_includes_vat=True, price="122.305", vat="0")
    assert (
        fromstring(CommercialTransport.payload("sale", snapshot)).findtext("items/item/vatPercent")
        == "0"
    )


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (
            200,
            "<documentValidationResult><valid>true</valid><documentNumber>RC-10001</documentNumber></documentValidationResult>",
            "accepted",
        ),
        (
            200,
            "<documentValidationResult><valid>false</valid></documentValidationResult>",
            "rejected",
        ),
        (
            200,
            "<documentValidationResult><valid>true</valid><documentNumber>OTHER</documentNumber></documentValidationResult>",
            "unknown",
        ),
        (500, "timeout", "unknown"),
        (200, "<html/>", "unknown"),
    ],
)
def test_import_result_no_blind_retry(status, body, expected, snapshot):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, text=body)

    with httpx.Client(
        base_url="https://iiko.invalid/", transport=httpx.MockTransport(handler)
    ) as c:
        result = CommercialTransport(None).send_authenticated(
            c, "test-key", "sale", CommercialTransport.payload("sale", snapshot)
        )
    assert result == {"state": expected}
    assert len(seen) == 1
    assert seen[0].url.path == "/documents/import/outgoingInvoice"


@pytest.mark.parametrize("kind", ["purchase", "sale"])
def test_readback_requires_exact_body_and_store(kind, snapshot):
    root = fromstring(CommercialTransport.payload(kind, snapshot))
    root.find("dateIncoming").text = snapshot["date"] + "T09:00:00"
    SubElement(root, "id").text = "00000000-0000-0000-0000-000000000099"
    assert matches(root, kind, snapshot)
    for tag, value in [
        ("amount", "3"),
        ("sum", "999"),
        ("store" if kind == "purchase" else "storeId", "wrong"),
    ]:
        altered = deepcopy(root)
        item = altered.find("items/item")
        field = item.find(tag)
        if field is None:
            field = SubElement(item, tag)
        field.text = value
        assert not matches(altered, kind, snapshot)
    wrapper = Element("outgoingInvoiceDtoes" if kind == "sale" else "incomingInvoiceDtoes")
    wrapper.append(root)
    with httpx.Client(
        base_url="https://iiko.invalid/",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=tostring(wrapper))),
    ) as c:
        result = CommercialTransport(None).lookup(c, "test-key", kind, snapshot)
    assert result["state"] == "processed"
    assert result["iiko_id"].endswith("99")


def test_exclusive_vat_rounding_conflict_never_enters_queue(snapshot):
    from fastapi import HTTPException

    item = snapshot["items"][0]
    source = {
        k: item[k] for k in ("product_id", "quantity", "price", "vat_rate", "price_includes_vat")
    }
    source["quantity"] = "2.5"
    rows, totals = calculate([source])
    rows[0].update(unit_id=item["unit_id"])
    snapshot.update(items=rows, totals=totals)
    with pytest.raises(HTTPException, match="Округление"):
        CommercialTransport.payload("sale", snapshot)
