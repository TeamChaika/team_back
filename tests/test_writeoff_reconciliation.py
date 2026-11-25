"""Read back accepted writeoffs without ever repeating the import."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest

SOURCE, TARGET, PRODUCT = (UUID(int=n) for n in (10, 11, 12))
EXTRA, MISSING = (UUID(int=n) for n in (51, 52))
CREATED = datetime(2026, 1, 2, 22, 30, tzinfo=UTC)


def actual(number, status="PROCESSED", store=SOURCE):
    return {
        "id": str(uuid4()),
        "accountId": str(uuid4()),
        "storeId": str(store),
        "documentNumber": number,
        "status": status,
        "dateIncoming": "2026-01-04T01:30:00",
        "items": [{"num": 1, "productId": str(PRODUCT), "amount": "1.01"}],
    }


def transport(response):
    def handle(request):
        assert request.method == "GET"
        assert request.url.path == "/v2/documents/writeoff/byNumber"
        assert dict(request.url.params) == {"key": "test", "documentNumber": "DJ000123"}
        return httpx.Response(200, json=response)

    return httpx.Client(base_url="https://example.invalid/", transport=httpx.MockTransport(handle))


def test_changed_date_and_decimal_quantities_match_by_number_and_store():
    from app.documents.writeoff_reconciliation import compare_writeoff, lookup_writeoffs

    number = "DJ000123"
    rows = [actual(number, store=TARGET), actual(number)]
    rows[1]["items"].extend(
        [
            {"num": 2, "productId": str(PRODUCT), "amount": "0.09"},
            {"num": 3, "productId": str(EXTRA), "amount": "2.5"},
        ]
    )
    expected = {str(PRODUCT): Decimal("1.10"), str(MISSING): Decimal("0.25")}
    with transport(rows) as client:
        found = lookup_writeoffs(client, "test", number)
    result = compare_writeoff(CREATED, SOURCE, expected, found)
    assert result["state"] == "matched" and result["iiko_status"] == "PROCESSED"
    assert (result["expected_date"], result["actual_date"], result["date_differs"]) == (
        "2026-01-03",
        "2026-01-04",
        True,
    )
    assert result["quantity_differences"] == [
        {"product_id": str(EXTRA), "expected_amount": None, "actual_amount": "2.5"},
        {"product_id": str(MISSING), "expected_amount": "0.25", "actual_amount": None},
    ]


@pytest.mark.parametrize(
    ("stores", "state"),
    [([], "not_found"), ([TARGET], "not_found"), ([SOURCE, SOURCE], "ambiguous")],
)
def test_missing_or_ambiguous_never_claims_a_status(stores, state):
    from app.documents.writeoff_reconciliation import compare_writeoff, lookup_writeoffs

    with transport([actual("DJ000123", store=store) for store in stores]) as client:
        found = lookup_writeoffs(client, "test", "DJ000123")
    result = compare_writeoff(CREATED, SOURCE, {}, found)
    assert result["state"] == state and result["iiko_status"] is None
    assert result["actual_date"] is None and not result["quantity_differences"]


@pytest.mark.parametrize("response", [{"result": "ERROR"}, [actual("DJ999999")], [{}]])
def test_invalid_upstream_response_does_not_claim_absence(response):
    from app.documents.writeoff_reconciliation import lookup_writeoffs

    with transport(response) as client, pytest.raises(ValueError):
        lookup_writeoffs(client, "test", "DJ000123")


def test_first_verified_identity_survives_missing_and_replacement_documents():
    from app.documents.writeoff_reconciliation import compare_writeoff, lookup_writeoffs

    number = "DJ000123"
    original = actual(number)
    replacement = actual(number)
    anchor = None
    for response, state in (
        ([original], "matched"),
        ([], "not_found"),
        ([replacement], "ambiguous"),
        ([replacement, original], "matched"),
    ):
        with transport(response) as client:
            docs = lookup_writeoffs(client, "test", number)
        result = compare_writeoff(CREATED, SOURCE, {}, docs, anchor)
        assert result["state"] == state and result["iiko_id"] == original["id"]
        anchor = result["iiko_id"]
