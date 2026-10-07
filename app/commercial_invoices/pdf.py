"""Render a commercial invoice from its saved snapshot, without external resources.

Amounts are decimal strings. ``price`` is the entered sale price; an item's
``price_includes_vat`` selects whether quantity * price equals total or net.
A null VAT rate means no VAT; zero means the distinct zero-percent VAT rate.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any
from xml.sax.saxutils import escape

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_CENT = Decimal("0.01")
_MAX_AMOUNT = Decimal("999999999999999.99")
_FONT_LOCK = Lock()


def _text(value: Any, limit: int = 2000) -> str:
    result = str(value or "")
    if len(result) > limit:
        raise ValueError("Invoice text exceeds the permitted length")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in result):
        raise ValueError("Invoice text contains control characters")
    return result


def _decimal(value: Any, *, money: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid invoice amount") from exc
    if not result.is_finite() or result < 0 or result > _MAX_AMOUNT:
        raise ValueError("Invoice amount is out of range")
    if money and result != result.quantize(_CENT, rounding=ROUND_HALF_UP):
        raise ValueError("Invoice monetary amounts must have at most two decimal places")
    return result


def _field(data: dict, key: str) -> Any:
    if key not in data:
        raise ValueError(f"Missing invoice field: {key}")
    return data[key]


def _money(value: Any) -> str:
    return f"{_decimal(value):,.2f}".replace(",", "\u00a0").replace(".", ",")


def _date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
    except ValueError as exc:
        raise ValueError("Invoice dates must use YYYY-MM-DD") from exc


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    if 11 <= number % 100 <= 14:
        return forms[2]
    remainder = number % 10
    return forms[0] if remainder == 1 else forms[1] if remainder in (2, 3, 4) else forms[2]


def _triplet(number: int, feminine: bool = False) -> list[str]:
    ones = (
        "",
        "одна" if feminine else "один",
        "две" if feminine else "два",
        "три",
        "четыре",
        "пять",
        "шесть",
        "семь",
        "восемь",
        "девять",
    )
    teens = (
        "десять",
        "одиннадцать",
        "двенадцать",
        "тринадцать",
        "четырнадцать",
        "пятнадцать",
        "шестнадцать",
        "семнадцать",
        "восемнадцать",
        "девятнадцать",
    )
    tens = (
        "",
        "",
        "двадцать",
        "тридцать",
        "сорок",
        "пятьдесят",
        "шестьдесят",
        "семьдесят",
        "восемьдесят",
        "девяносто",
    )
    hundreds = (
        "",
        "сто",
        "двести",
        "триста",
        "четыреста",
        "пятьсот",
        "шестьсот",
        "семьсот",
        "восемьсот",
        "девятьсот",
    )
    parts = [hundreds[number // 100]] if number >= 100 else []
    remainder = number % 100
    if 10 <= remainder <= 19:
        parts.append(teens[remainder - 10])
    else:
        parts.extend((tens[remainder // 10], ones[remainder % 10]))
    return [part for part in parts if part]


def amount_in_words(value: Any) -> str:
    """Russian rubles with numeric kopecks, up to 999,999,999,999,999.99."""
    amount = _decimal(value)
    rubles = int(amount)
    kopecks = int((amount - rubles) * 100)
    parts: list[str] = []
    scales = (
        (10**12, ("триллион", "триллиона", "триллионов"), False),
        (10**9, ("миллиард", "миллиарда", "миллиардов"), False),
        (10**6, ("миллион", "миллиона", "миллионов"), False),
        (1000, ("тысяча", "тысячи", "тысяч"), True),
    )
    remaining = rubles
    for scale, forms, feminine in scales:
        group, remaining = divmod(remaining, scale)
        if group:
            parts.extend(_triplet(group, feminine))
            parts.append(_plural(group, forms))
    parts.extend(_triplet(remaining) if remaining else (["ноль"] if rubles == 0 else []))
    parts.extend(
        (
            _plural(rubles, ("рубль", "рубля", "рублей")),
            f"{kopecks:02}",
            _plural(kopecks, ("копейка", "копейки", "копеек")),
        )
    )
    return " ".join(parts).capitalize()


def _normalise(snapshot: dict) -> tuple[list[dict], dict[str, Decimal]]:
    source = snapshot.get("items")
    if not isinstance(source, list) or not 1 <= len(source) <= 200:
        raise ValueError("Invoice must contain between 1 and 200 items")
    items = []
    for item in source:
        if not isinstance(item, dict):
            raise ValueError("Invalid invoice item")
        quantity = _decimal(_field(item, "quantity"), money=False)
        if quantity <= 0 or quantity.normalize().as_tuple().exponent < -6:
            raise ValueError("Invoice quantity must be positive with at most six decimal places")
        price = _decimal(_field(item, "price"), money=False)
        if price.normalize().as_tuple().exponent < -4:
            raise ValueError("Invoice sale prices must have at most four decimal places")
        net = _decimal(_field(item, "net"))
        vat = _decimal(_field(item, "vat"))
        total = _decimal(_field(item, "total"))
        rate_raw = item.get("vat_rate")
        rate = None if rate_raw is None else _decimal(rate_raw, money=False)
        if rate is not None and rate > 100:
            raise ValueError("Invalid VAT rate")
        includes = item.get("price_includes_vat", True)
        if not isinstance(includes, bool):
            raise ValueError("price_includes_vat must be a boolean")
        expected = total if includes else net
        if (price * quantity).quantize(_CENT, rounding=ROUND_HALF_UP) != expected:
            raise ValueError("Invoice quantity and sale price do not match the saved amount")
        if net + vat != total:
            raise ValueError("Invoice net and VAT do not match its total")
        expected_vat = (
            Decimal(0)
            if rate is None
            else (total * rate / (Decimal(100) + rate) if includes else net * rate / 100).quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
        )
        if vat != expected_vat:
            raise ValueError("Invoice VAT does not match the saved VAT rate")
        items.append(
            dict(
                name=_text(_field(item, "name"), 1500),
                unit=_text(item.get("unit"), 30),
                quantity=quantity,
                price=price,
                net=net,
                vat=vat,
                total=total,
                vat_rate=rate,
                price_includes_vat=includes,
            )
        )
    raw_totals = snapshot.get("totals")
    if not isinstance(raw_totals, dict):
        raise ValueError("Missing invoice totals")
    totals = {key: _decimal(_field(raw_totals, key)) for key in ("net", "vat", "total")}
    if any(sum((item[key] for item in items), Decimal(0)) != totals[key] for key in totals):
        raise ValueError("Saved invoice totals do not match its items")
    if totals["total"] <= 0:
        raise ValueError("Invoice total must be positive")
    return items, totals


def build_payment_qr_payload(snapshot: dict) -> str:
    """Build an ST00012 UTF-8 payment payload using only saved bank details."""
    _, totals = _normalise(snapshot)
    seller = snapshot.get("seller")
    if not isinstance(seller, dict):
        raise ValueError("Missing invoice seller")
    for key, lengths in (
        ("inn", (10, 12)),
        ("bic", (9,)),
        ("account", (20,)),
        ("correspondent_account", (20,)),
    ):
        value = _field(seller, key)
        if (
            not isinstance(value, str)
            or len(value) not in lengths
            or not re.fullmatch(r"[0-9]+", value)
        ):
            raise ValueError(f"Invalid seller bank detail: {key}")
    if seller.get("kpp") and not re.fullmatch(r"[0-9]{9}", str(seller["kpp"])):
        raise ValueError("Invalid seller KPP")
    if (
        not _text(seller.get("name"), 300).strip()
        or not _text(seller.get("bank_name"), 300).strip()
    ):
        raise ValueError("Missing seller or bank name")

    def clean(value: Any) -> str:
        return _text(value, 300).replace("|", " ").replace("\r", " ").replace("\n", " ")

    fields = [
        ("Name", _field(seller, "name")),
        ("PersonalAcc", _field(seller, "account")),
        ("BankName", _field(seller, "bank_name")),
        ("BIC", _field(seller, "bic")),
        ("CorrespAcc", _field(seller, "correspondent_account")),
        ("PayeeINN", _field(seller, "inn")),
    ]
    if seller.get("kpp"):
        fields.append(("KPP", seller["kpp"]))
    fields.append(("Sum", str(int(totals["total"] * 100))))
    vat_phrase = (
        "Без НДС."
        if all(item.get("vat_rate") is None for item in snapshot["items"])
        else f"НДС по документу: {_money(totals['vat'])} руб."
    )
    fields.append(
        (
            "Purpose",
            f"Оплата по счёту № {clean(snapshot['number'])} "
            f"от {_date(snapshot['date'])}. {vat_phrase}",
        )
    )
    payload = "ST00012|" + "|".join(f"{key}={clean(value)}" for key, value in fields)
    if len(payload.encode("utf-8")) > 1800:
        raise ValueError("Invoice payment QR data is too long")
    return payload


def _fonts() -> None:
    with _FONT_LOCK:
        if "InvoiceSans" in pdfmetrics.getRegisteredFontNames():
            return
        assets = Path(__file__).with_name("assets")
        pdfmetrics.registerFont(TTFont("InvoiceSans", str(assets / "LiberationSans-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("InvoiceSansBold", str(assets / "LiberationSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("InvoiceSans", normal="InvoiceSans", bold="InvoiceSansBold")


def render_invoice_pdf(snapshot: dict) -> bytes:
    """Render a validated frozen snapshot as a searchable, multi-page A4 PDF."""
    items, totals = _normalise(snapshot)
    _fonts()
    number = _text(snapshot["number"], 100)
    invoice_date = _date(snapshot["date"])
    seller = snapshot["seller"]
    buyer = snapshot.get("counterparty")
    if not isinstance(seller, dict) or not isinstance(buyer, dict):
        raise ValueError("Missing invoice parties")
    styles = {
        key: ParagraphStyle(
            key,
            fontName="InvoiceSansBold" if key in ("title", "bold", "boldright") else "InvoiceSans",
            fontSize=size,
            leading=size * 1.25,
            alignment=alignment,
            splitLongWords=True,
        )
        for key, size, alignment in [
            ("body", 9, TA_LEFT),
            ("small", 8, TA_LEFT),
            ("bold", 9, TA_LEFT),
            ("title", 16, TA_LEFT),
            ("right", 9, TA_RIGHT),
            ("boldright", 9, TA_RIGHT),
            ("center", 8, TA_CENTER),
        ]
    }

    def p(value: Any, style: str = "body") -> Paragraph:
        return Paragraph(escape(_text(value)).replace("\n", "<br/>"), styles[style])

    width = A4[0] - 56
    payload = build_payment_qr_payload(snapshot)
    payable = snapshot.get("state") in {"accepted", "processed"}
    if not payable:
        payment_code = [p("Не для оплаты", "center")]
    else:
        qr = QrCodeWidget(payload, barLevel="M")
        x0, y0, x1, y1 = qr.getBounds()
        qr_size = 105
        drawing = Drawing(
            qr_size, qr_size, transform=[qr_size / (x1 - x0), 0, 0, qr_size / (y1 - y0), 0, 0]
        )
        drawing.add(qr)
        payment_code = [drawing, p("Для оплаты", "center")]
    bank_width = width - 112
    bank_rows = [
        [
            p(_field(seller, "bank_name"), "small"),
            p("БИК", "small"),
            p(_field(seller, "bic"), "small"),
        ],
        [
            p("Банк получателя", "small"),
            p("Сч. №", "small"),
            p(_field(seller, "correspondent_account"), "small"),
        ],
        [
            p(
                "ИНН "
                + _text(seller["inn"])
                + ("   КПП " + _text(seller["kpp"]) if seller.get("kpp") else ""),
                "small",
            ),
            p("Сч. №", "small"),
            p(seller["account"], "small"),
        ],
        [p(seller["name"], "small"), "", ""],
        [p("Получатель", "small"), "", ""],
    ]
    bank = Table(bank_rows, colWidths=[bank_width * 0.55, 32, bank_width * 0.45 - 32])
    bank.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.55, colors.black),
                ("SPAN", (1, 2), (1, 4)),
                ("SPAN", (2, 2), (2, 4)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    top = Table([[bank, payment_code]], colWidths=[bank_width, 112])
    top.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story = [top, Spacer(1, 15)]
    if not payable:
        status = "Черновик" if snapshot.get("state") == "draft" else "Не для оплаты"
        story.extend([p(status, "bold"), Spacer(1, 5)])
    story.extend(
        [
            p(f"Счёт на оплату № {number} от {invoice_date}", "title"),
            Spacer(1, 9),
            HRFlowable(width="100%", thickness=1),
            Spacer(1, 10),
        ]
    )

    def party_details(party: dict) -> str:
        details = [_text(_field(party, "name"))]
        if party.get("inn"):
            details.append("ИНН " + _text(party["inn"]))
        if party.get("kpp"):
            details.append("КПП " + _text(party["kpp"]))
        if party.get("address"):
            details.append(_text(party["address"]))
        return ", ".join(details)

    story.extend(
        [
            p("Поставщик (Исполнитель): " + party_details(seller)),
            Spacer(1, 8),
            p("Покупатель (Заказчик): " + party_details(buyer)),
            Spacer(1, 8),
        ]
    )
    if snapshot.get("comment"):
        story.extend(
            [p("Основание / примечание: " + _text(snapshot["comment"], 2000)), Spacer(1, 8)]
        )
    rows = [
        [
            p(label, "center")
            for label in ("№", "Товары (работы, услуги)", "Кол-во", "Ед.", "Цена", "Сумма к оплате")
        ]
    ]
    for index, item in enumerate(items, 1):
        price_places = max(2, -item["price"].as_tuple().exponent)
        price_text = (
            format(item["price"], f",.{price_places}f").replace(",", "\u00a0").replace(".", ",")
        )
        if item["vat_rate"] is not None:
            price_text += "\n" + ("с НДС" if item["price_includes_vat"] else "без НДС")
        quantity_text = (
            format(item["quantity"], "f").rstrip("0").rstrip(".")
            if "." in format(item["quantity"], "f")
            else str(item["quantity"])
        )
        rows.append(
            [
                p(index, "center"),
                p(item["name"]),
                p(quantity_text, "right"),
                p(item["unit"], "center"),
                p(price_text, "right"),
                p(_money(item["total"]), "right"),
            ]
        )
    table = LongTable(
        rows, colWidths=[25, width - 225, 48, 35, 57, 60], repeatRows=1, splitByRow=1, splitInRow=1
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.extend([table, Spacer(1, 10)])
    summary_rows = [[p("Итого без НДС:", "right"), p(_money(totals["net"]), "right")]]
    rates = sorted({item["vat_rate"] for item in items if item["vat_rate"] is not None})
    if any(item["vat_rate"] is None for item in items):
        no_vat = sum((item["total"] for item in items if item["vat_rate"] is None), Decimal(0))
        summary_rows.append([p("Без НДС:", "right"), p(_money(no_vat), "right")])
    for rate in rates:
        tax = sum((item["vat"] for item in items if item["vat_rate"] == rate), Decimal(0))
        summary_rows.append(
            [
                p(f"В том числе НДС {format(rate.normalize(), 'f')}%:", "right"),
                p(_money(tax), "right"),
            ]
        )
    summary_rows.append(
        [p("Всего к оплате:", "boldright"), p(_money(totals["total"]), "boldright")]
    )
    summary = Table(summary_rows, colWidths=[width - 90, 90])
    summary.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    conclusion = [
        summary,
        Spacer(1, 10),
        p(f"Всего наименований: {len(items)}, на сумму {_money(totals['total'])} руб."),
        Spacer(1, 5),
        p(amount_in_words(totals["total"]), "bold"),
    ]
    due = snapshot.get("payment_due") or snapshot.get("due_date")
    if due:
        conclusion.extend([Spacer(1, 8), p("Оплатить до: " + _date(due))])
    if snapshot.get("payment_terms"):
        conclusion.extend([Spacer(1, 8), p(_text(snapshot["payment_terms"], 1500), "small")])
    conclusion.extend(
        [
            Spacer(1, 20),
            HRFlowable(width="100%", thickness=0.6),
            Spacer(1, 10),
            p("Руководитель ____________________       Бухгалтер ____________________"),
        ]
    )
    story.append(KeepTogether(conclusion))
    stream = BytesIO()
    document = SimpleDocTemplate(
        stream,
        pagesize=A4,
        leftMargin=28,
        rightMargin=28,
        topMargin=28,
        bottomMargin=35,
        title=f"Счёт на оплату № {number}",
        author=_text(seller["name"]),
        pageCompression=1,
    )

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("InvoiceSans", 8)
        canvas.drawRightString(A4[0] - 28, 20, f"Страница {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()
