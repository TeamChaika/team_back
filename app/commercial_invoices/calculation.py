"""Exact, bounded monetary arithmetic shared by saved invoices and their PDFs."""

import re
from decimal import ROUND_HALF_UP, Decimal

from app.documents.policy import invalid

VAT_RATES = ("0", "5", "7", "10", "20", "22")
CENT = Decimal("0.01")


def decimal_string(value, *, scale, positive=False, maximum=Decimal("1000000000")):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{1,12}(?:\.\d{1," + str(scale) + r"})?", value
    ):
        invalid("Количество и цена должны быть десятичными строками допустимой точности.")
    number = Decimal(value)
    if number > maximum or (number <= 0 if positive else number < 0):
        invalid("Количество или цена вне допустимого диапазона.")
    return number


def money(value):
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), ".2f")


def calculate(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        invalid("Добавьте от 1 до 200 строк.")
    result = []
    totals = {key: Decimal(0) for key in ("net", "vat", "total")}
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "product_id",
            "quantity",
            "price",
            "vat_rate",
            "price_includes_vat",
        }:
            invalid("Проверьте поля товарной строки.")
        quantity = decimal_string(item["quantity"], scale=3, positive=True)
        price = decimal_string(item["price"], scale=4)
        rate = item["vat_rate"]
        if rate is not None and (not isinstance(rate, str) or rate not in VAT_RATES):
            invalid("Выберите ставку НДС или «Без НДС».")
        if not isinstance(item["price_includes_vat"], bool):
            invalid("Укажите, включён ли НДС в цену.")
        base = (quantity * price).quantize(CENT, rounding=ROUND_HALF_UP)
        if base > Decimal("1000000000000"):
            invalid("Сумма строки превышает допустимый предел.")
        tax = Decimal(rate or "0") / 100
        if item["price_includes_vat"]:
            total = base
            vat = (total * tax / (1 + tax)).quantize(CENT, rounding=ROUND_HALF_UP)
            net = total - vat
        else:
            net = base
            vat = (net * tax).quantize(CENT, rounding=ROUND_HALF_UP)
            total = net + vat
        values = {"net": net, "vat": vat, "total": total}
        result.append(
            {
                **item,
                "quantity": format(quantity, "f"),
                "price": format(price, "f"),
                **{key: money(value) for key, value in values.items()},
            }
        )
        for key, value in values.items():
            totals[key] += value
    return result, {key: money(value) for key, value in totals.items()}
