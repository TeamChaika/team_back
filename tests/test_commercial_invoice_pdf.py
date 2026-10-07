from copy import deepcopy
from decimal import Decimal

import pytest

from app.commercial_invoices.pdf import (
    amount_in_words,
    build_payment_qr_payload,
    render_invoice_pdf,
)


def invoice():
    return {
        "number": "TEST-001",
        "date": "2026-10-07",
        "state": "processed",
        "seller": {
            "name": "ООО «Тестовый поставщик»",
            "inn": "1234567890",
            "kpp": "123456789",
            "bank_name": "Тестовый банк",
            "bic": "123456789",
            "account": "12345678901234567890",
            "correspondent_account": "12345678901234567890",
            "address": "Тестовый адрес",
        },
        "counterparty": {"name": "Тестовый покупатель", "inn": "123456789012"},
        "items": [
            {
                "name": "Тестовый товар",
                "quantity": "2",
                "unit": "шт",
                "price": "120.00",
                "net": "200.00",
                "vat": "40.00",
                "total": "240.00",
                "vat_rate": "20",
                "price_includes_vat": True,
            }
        ],
        "totals": {"net": "200.00", "vat": "40.00", "total": "240.00"},
    }


@pytest.mark.parametrize(
    ("amount", "words"),
    [
        ("0", "Ноль рублей 00 копеек"),
        ("1.01", "Один рубль 01 копейка"),
        ("2.02", "Два рубля 02 копейки"),
        ("11.11", "Одиннадцать рублей 11 копеек"),
        ("21.21", "Двадцать один рубль 21 копейка"),
        ("1000", "Одна тысяча рублей 00 копеек"),
        ("2001.05", "Две тысячи один рубль 05 копеек"),
        ("11000", "Одиннадцать тысяч рублей 00 копеек"),
        ("1000000001.99", "Один миллиард один рубль 99 копеек"),
    ],
)
def test_amount_in_words(amount, words):
    assert amount_in_words(amount) == words


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "1000000000000000", "1.005"])
def test_amount_in_words_rejects_invalid_amount(amount):
    with pytest.raises(ValueError):
        amount_in_words(amount)


def test_qr_uses_exact_saved_total_and_sanitizes_delimiters():
    data = invoice()
    data["number"] = "1|Sum=1\n"
    data["seller"]["name"] = "Тест|Банк\r\n"
    result = build_payment_qr_payload(data)
    assert result.startswith("ST00012|Name=Тест Банк  |PersonalAcc=")
    assert "|Sum=24000|" in result
    assert result.count("|Sum=") == 1
    assert "|Purpose=Оплата по счёту № 1 Sum=1  от 07.10.2026." in result
    assert "\n" not in result and "\r" not in result


@pytest.mark.parametrize("rate", [None, "0"])
def test_zero_vat_and_no_vat_render_differently(rate):
    data = invoice()
    data["items"][0].update(net="240.00", vat="0.00", vat_rate=rate)
    data["totals"].update(net="240.00", vat="0.00")
    payload = build_payment_qr_payload(data)
    assert ("Без НДС." in payload) is (rate is None)
    assert ("НДС по документу: 0,00 руб." in payload) is (rate == "0")
    assert render_invoice_pdf(data).startswith(b"%PDF-")


def test_exclusive_sale_price_and_fractional_quantity():
    data = invoice()
    data["items"][0].update(
        price="100.00",
        quantity="2.123456",
        net="212.35",
        vat="42.47",
        total="254.82",
        price_includes_vat=False,
    )
    data["totals"].update(net="212.35", vat="42.47", total="254.82")
    assert "|Sum=25482|" in build_payment_qr_payload(data)
    assert render_invoice_pdf(data).startswith(b"%PDF-")


@pytest.mark.parametrize("change", ["totals", "line_total", "price", "vat", "quantity", "rate"])
def test_rejects_financially_inconsistent_saved_snapshots(change):
    data = invoice()
    if change == "totals":
        data["totals"]["total"] = "241.00"
    elif change == "line_total":
        data["items"][0]["total"] = "239.00"
    elif change == "price":
        data["items"][0]["price"] = "119.00"
    elif change == "vat":
        data["items"][0].update(net="199.99", vat="40.01")
        data["totals"].update(net="199.99", vat="40.01")
    elif change == "quantity":
        data["items"][0]["quantity"] = "2.0000001"
    else:
        data["items"][0]["vat_rate"] = "101"
    with pytest.raises(ValueError):
        render_invoice_pdf(data)


def test_markup_is_literal_and_multipage_snapshot_is_not_mutated():
    data = invoice()
    data["items"] = [deepcopy(data["items"][0]) for _ in range(90)]
    for item in data["items"]:
        item["name"] = '<img src="https://example.com/secret"/> & ' + "Длинное название товара " * 8
    data["totals"] = {key: str(Decimal(value) * 90) for key, value in data["totals"].items()}
    before = deepcopy(data)
    result = render_invoice_pdf(data)
    assert result.startswith(b"%PDF-")
    assert data == before
    # Page object markers are independent of optional PDF reader dependencies.
    import re

    assert len(re.findall(rb"/Type\s*/Page\b", result)) > 2


def test_text_limits_and_empty_items_are_rejected():
    data = invoice()
    data["items"][0]["name"] = "a" * 1501
    with pytest.raises(ValueError):
        render_invoice_pdf(data)
    data["items"] = []
    with pytest.raises(ValueError):
        render_invoice_pdf(data)


def test_four_decimal_sale_price_is_kept_and_rounded_per_line():
    data = invoice()
    data["items"][0].update(price="120.0049", net="200.01", vat="40.00", total="240.01")
    data["totals"].update(net="200.01", vat="40.00", total="240.01")
    assert "|Sum=24001|" in build_payment_qr_payload(data)
    assert render_invoice_pdf(data).startswith(b"%PDF-")


@pytest.mark.parametrize("state", ["draft", "queued", "sending", "rejected", "unknown", None])
def test_unconfirmed_invoice_has_no_payment_qr(monkeypatch, state):
    from app.commercial_invoices import pdf

    data = invoice()
    data["state"] = state

    def unexpected_qr(*args, **kwargs):
        pytest.fail("An unconfirmed invoice must not contain a payment QR")

    monkeypatch.setattr(pdf, "QrCodeWidget", unexpected_qr)
    assert pdf.render_invoice_pdf(data).startswith(b"%PDF-")


@pytest.mark.parametrize(
    ("key", "value"), [("bic", "123"), ("account", "123"), ("inn", "abc"), ("bank_name", "")]
)
def test_incomplete_payment_details_are_rejected(key, value):
    data = invoice()
    data["seller"][key] = value
    with pytest.raises(ValueError):
        render_invoice_pdf(data)


def test_insignificant_decimal_padding_is_not_excess_precision():
    sample = invoice()
    sample["items"][0].update(price="120.000000", quantity="2.00000000")
    assert render_invoice_pdf(sample).startswith(b"%PDF-")
    sample["items"][0]["price"] = "120.000001"
    with pytest.raises(ValueError, match="four decimal"):
        render_invoice_pdf(sample)
