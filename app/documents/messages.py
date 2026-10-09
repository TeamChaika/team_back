"""Readable Telegram HTML previews with complete, safely paginated document data."""

from datetime import datetime
from decimal import Decimal
from html import escape

from app.documents.context import local_zone, runtime_of


def quantity(value) -> str:
    # Preserve actual quantities, including tiny fractions; never invent a unit.
    number = format(Decimal(str(value)), "f")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    whole, dot, fraction = number.partition(".")
    whole = f"{int(whole):,}".replace(",", " ")
    return whole + ("," + fraction if dot else "")


def html_size(text: str) -> int:
    # Counting the encoded HTML is conservative, also for non-BMP emoji.
    return len(text.encode("utf-16-le")) // 2


def escaped_chunks(text: str):
    """Split before escaping so neither a character nor an HTML entity is cut."""
    chunk, size = [], 0
    for char in text:
        encoded = escape(char, quote=False)
        length = html_size(encoded)
        if size + length > 3000:
            yield "".join(chunk)
            chunk, size = [], 0
        chunk.append(encoded)
        size += length
    yield "".join(chunk)


def document_messages(kind: str, doc: dict, runtime=None):
    title = "Накладная" if kind == "waybill" else "Списание"
    brand = "CHAIKA · " if runtime_of(runtime).mode == "legacy" else ""
    heading = (
        f"<b>{brand}{title} №{escape(str(doc['number']), quote=False)}</b>\n"
        f"На согласовании · версия {int(doc['version'])}"
    )
    if doc.get("receipt_state") == "pending_sender":
        heading += "\n<b>Расхождения при приёмке · требуется подтверждение отправителя</b>"
    elif doc.get("receipt_state") == "rejected":
        heading += "\n<b>Отправитель отклонил расхождения · проверьте приёмку повторно</b>"
    pages = [""]

    def add(text, bold=False):
        for chunk in escaped_chunks(str(text)):
            line = (f"<b>{chunk}</b>" if bold else chunk) + "\n"
            if html_size(pages[-1] + line) > 3400:
                pages.append("")
            pages[-1] += line

    add("Откуда" if kind == "waybill" else "Склад", bold=True)
    add(doc["store"])
    add("")
    add("Куда" if kind == "waybill" else "Причина списания", bold=True)
    add(doc["counteragent"] if kind == "waybill" else doc["reason"])
    add("")
    add(f"Состав · {len(doc['items'])} поз.", bold=True)
    for index, item in enumerate(doc["items"], 1):
        amount = quantity(item["amount"])
        if item.get("unit"):
            amount += " " + str(item["unit"])
        if item.get("received_amount") is not None:
            amount += " → факт: " + quantity(item["received_amount"])
        add(f"{index}. {item['name']} — {amount}")
    if doc.get("comment", "").strip():
        add("")
        add("Комментарий", bold=True)
        add(doc["comment"])
    add("")
    add("Создал: " + doc["created_by"])
    if doc.get("created_at"):
        created = datetime.fromisoformat(doc["created_at"])
        if created.tzinfo is not None:
            created = created.astimezone(local_zone(runtime))
        add(created.strftime("%d.%m.%Y · %H:%M"))

    for page, body in enumerate(pages, 1):
        page_label = f" · часть {page}/{len(pages)}" if len(pages) > 1 else ""
        text = heading + page_label + "\n\n" + body.rstrip("\n")
        if page == len(pages):
            text += "\n\nПроверьте состав перед согласованием."
        yield text, page, len(pages)
