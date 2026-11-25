"""Excel workbook for one venue's daily procurement report."""

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from uuid import UUID

from xlsxwriter import Workbook

MONTHS = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
KITCHEN = {"кухня", "заготовка", "пицца", "ролл", "кондитерка", "выпечка"}
DETAIL_HEADERS = (
    "Товар",
    "Отклонение, ₽",
    "Отклонение, %",
    "Цена, ₽/ед.",
    "Средняя сети, ₽/ед.",
    "Ед.",
    "Количество",
    "Сумма, ₽",
    "Поставщик",
    "Приходная заведения",
    "Дата поставки",
    "Загружена, МСК",
    "Объём сравнения",
    "Сумма сравнения, ₽",
    "Склад",
)
REVIEW_HEADERS = (
    "Товар",
    "Причина",
    "Склад заведения",
    "Через склад",
    "Поставщик",
    "Приходная заведения",
    "Накладная поставщика",
    "Количество",
    "Ед.",
    "Цена передачи, ₽/ед.",
    "Сумма передачи, ₽",
    "Дата поставки",
    "Загружена, МСК",
)
SUMMARY_HEADERS = (
    "Склад",
    "Накладные",
    "Позиции",
    "Закуплено, ₽",
    "По средней сети, ₽",
    "Удорожание, ₽",
    "Снижение, ₽",
    "Отклонение, ₽",
)
MONEY = "#,##0.00"
DELTA = "+#,##0.00;-#,##0.00;0.00"


def _group(store):
    words = set(re.findall(r"[^\W_]+", store.casefold()))
    kitchen = not KITCHEN.isdisjoint(words)
    bar = "бар" in words
    if kitchen == bar:
        return "Другие склады"
    return "Кухня" if kitchen else "Бар"


def _formats(book):
    base = {"font_name": "Arial", "font_size": 10, "font_color": "#213949", "valign": "vcenter"}
    return {
        "title": book.add_format({**base, "bold": True, "font_size": 16}),
        "subtitle": book.add_format({**base, "font_color": "#537080"}),
        "header": book.add_format(
            {
                **base,
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#173952",
                "text_wrap": True,
                "align": "center",
            }
        ),
        "money": book.add_format({**base, "num_format": MONEY}),
        "delta": book.add_format({**base, "num_format": DELTA}),
        "pct": book.add_format({**base, "num_format": "+0.0%;-0.0%;0.0%"}),
        "qty": book.add_format({**base, "num_format": "#,##0.###"}),
        "date": book.add_format({**base, "num_format": "dd.mm.yyyy hh:mm"}),
        "up": book.add_format({"bg_color": "#FBEBDC", "font_color": "#863C13"}),
        "down": book.add_format({"bg_color": "#E2F2EC", "font_color": "#166253"}),
        "total": book.add_format({**base, "bold": True, "top": 1, "top_color": "#173952"}),
        "total_money": book.add_format({**base, "bold": True, "top": 1, "num_format": MONEY}),
        "total_delta": book.add_format({**base, "bold": True, "top": 1, "num_format": DELTA}),
    }


def _frame(sheet, formats, title, subtitle, headers, widths):
    sheet.hide_gridlines(2)
    sheet.set_default_row(19)
    sheet.set_row(0, 8)
    sheet.set_row(1, 27)
    sheet.write_string("A2", title, formats["title"])
    sheet.write_string("A3", subtitle, formats["subtitle"])
    sheet.set_row(3, 8)
    sheet.set_row(4, 32)
    sheet.write_row("A5", headers, formats["header"])
    for i, width in enumerate(widths):
        sheet.set_column(i, i, width)


def _highlight(sheet, cell_range, formats):
    for op, key in ((">", "up"), ("<", "down")):
        sheet.conditional_format(
            cell_range,
            {
                "type": "cell",
                "criteria": op,
                "value": 0,
                "format": formats[key],
            },
        )


def _invoice_link(sheet, index, col, document_id, number):
    doc = UUID(str(document_id))
    sheet.write_url(
        index, col, f"https://dashboard.chaika.team/invoices/{doc}", string=f"№{number or '—'}"
    )


def _detail(book, formats, group, title, subtitle, records):
    sheet = book.add_worksheet(group)
    headers = DETAIL_HEADERS
    widths = (37, 17, 16, 16, 18, 7, 15, 17, 29, 21, 21, 21, 17, 19, 28)
    if any(line.get("via") for line in records):
        headers += ("Через склад", "Накладная поставщика")
        widths += (28, 24)
    _frame(
        sheet,
        formats,
        title,
        subtitle,
        headers,
        widths,
    )
    sheet.freeze_panes(5, 1)
    if not records:
        sheet.write_string("A6", "Нет новых сопоставимых позиций")
        return (0, 0, 0, 0, 0)

    spend = base = higher = lower = Decimal(0)
    for index, line in enumerate(records, start=5):
        row = index + 1
        qty, cost, prev_qty, prev_cost = (
            Decimal(line[key]) for key in ("qty", "cost", "prev_qty", "prev_cost")
        )
        unit_price = cost / qty
        average = prev_cost / prev_qty
        comparison = qty * average
        diff = cost - comparison
        spend += cost
        base += comparison
        higher += max(0, diff)
        lower += min(0, diff)

        sheet.write_string(index, 0, line["product"])
        for col, expr, cached, key in (
            (1, f"H{row}-G{row}*E{row}", diff, "delta"),
            (2, f"D{row}/E{row}-1", unit_price / average - 1, "pct"),
            (3, f"H{row}/G{row}", unit_price, "money"),
            (4, f"N{row}/M{row}", average, "money"),
        ):
            sheet.write_formula(index, col, f"={expr}", formats[key], float(cached))
        sheet.write_string(index, 5, line["unit"])
        sheet.write_number(index, 6, float(qty), formats["qty"])
        sheet.write_number(index, 7, float(cost), formats["money"])
        sheet.write_string(index, 8, line["supplier"] or "Не указан")
        _invoice_link(sheet, index, 9, line["doc"], line["number"])
        sheet.write_datetime(index, 10, datetime.fromisoformat(line["stamp"]), formats["date"])
        sheet.write_datetime(index, 11, datetime.fromisoformat(line["noticed"]), formats["date"])
        sheet.write_number(index, 12, float(prev_qty), formats["qty"])
        sheet.write_number(index, 13, float(prev_cost), formats["money"])
        sheet.write_string(index, 14, line["store"])
        if line.get("via"):
            sheet.write_string(index, 15, line["via"])
            _invoice_link(sheet, index, 16, line["origin_doc"], line["origin_number"])

    end = 5 + len(records)
    sheet.autofilter(4, 0, end - 1, len(headers) - 1)
    _highlight(sheet, f"B6:C{end}", formats)
    return spend, base, higher, lower, spend - base


def _review(book, formats, venue, date_label, records):
    sheet = book.add_worksheet("На сверку")
    _frame(
        sheet, formats, "Поступления на сверку", f"{venue} · {date_label}", REVIEW_HEADERS,
        (37, 46, 28, 26, 30, 22, 24, 14, 7, 19, 19, 21, 21),
    )
    sheet.freeze_panes(5, 1)
    for index, line in enumerate(records, start=5):
        sheet.write_string(index, 0, line["product"])
        sheet.write_string(index, 1, line["reason"])
        sheet.write_string(index, 2, line["store"])
        sheet.write_string(index, 3, line["via"])
        sheet.write_string(index, 4, line.get("supplier") or "Требует сверки")
        _invoice_link(sheet, index, 5, line["doc"], line["number"])
        if line.get("origin_doc"):
            _invoice_link(sheet, index, 6, line["origin_doc"], line.get("origin_number"))
        qty = Decimal(line["qty"]) if line.get("qty") is not None else None
        cost = Decimal(line["cost"]) if line.get("cost") is not None else None
        if qty is not None:
            sheet.write_number(index, 7, float(qty), formats["qty"])
        sheet.write_string(index, 8, line["unit"])
        if cost is not None:
            if qty is not None and qty > 0 and cost >= 0:
                sheet.write_number(index, 9, float(cost / qty), formats["money"])
            sheet.write_number(index, 10, float(cost), formats["money"])
        sheet.write_datetime(index, 11, datetime.fromisoformat(line["stamp"]), formats["date"])
        sheet.write_datetime(index, 12, datetime.fromisoformat(line["noticed"]), formats["date"])
    sheet.autofilter(4, 0, 4 + len(records), len(REVIEW_HEADERS) - 1)


def render_workbook(source, day):
    venue = source["venue"]
    grouped = defaultdict(list)
    for line in source["rows"]:
        grouped[_group(line["store"])].append(line)
    labels = ["Кухня", "Бар"]
    if "Другие склады" in grouped:
        labels.append("Другие склады")
    date_label = f"{day.day} {MONTHS[day.month - 1]} {day.year}"
    buffer = BytesIO()
    with Workbook(
        buffer,
        {
            "in_memory": True,
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "remove_timezone": True,
        },
    ) as book:
        formats = _formats(book)
        summary = book.add_worksheet("Сводка")
        _frame(
            summary,
            formats,
            venue,
            f"Отклонения закупочных цен · {date_label}",
            SUMMARY_HEADERS,
            (29, 14, 13, 19, 23, 18, 18, 19),
        )
        group_totals = []
        for i, group in enumerate(labels, start=5):
            records = grouped[group]
            stores = {r["store"] for r in records}
            title = next(iter(stores)) if len(stores) == 1 else group
            totals = _detail(book, formats, group, title, f"{venue} · {date_label}", records)
            invoices = len({r["doc"] for r in records})
            group_totals.append((invoices, len(records), *totals))
            summary.write_string(i, 0, title)
            summary.write_number(i, 1, invoices)
            last = 5 + len(records)
            diffs = f"'{group}'!B6:B{last}"
            expressions = (
                f"COUNTA('{group}'!A6:A{last})",
                f"SUM('{group}'!H6:H{last})",
                f"D{i + 1}-H{i + 1}",
                f'SUMIF({diffs},">0",{diffs})',
                f'SUMIF({diffs},"<0",{diffs})',
                f"SUM({diffs})",
            )
            for col, (formula, value) in enumerate(
                zip(expressions, (len(records), *totals), strict=True), start=2
            ):
                style = None if col == 2 else formats["money" if col < 5 else "delta"]
                if records:
                    summary.write_formula(i, col, "=" + formula, style, float(value))
                else:
                    summary.write_number(i, col, 0, style)

        footer = 5 + len(labels)
        summary.write_string(footer, 0, venue, formats["total"])
        summary.write_number(footer, 1, len({r["doc"] for r in source["rows"]}), formats["total"])
        for col, letter in enumerate("CDEFGH", start=2):
            total = sum(row[col - 1] for row in group_totals)
            style = "total" if col == 2 else "total_money" if col < 5 else "total_delta"
            summary.write_formula(
                footer, col, f"=SUM({letter}6:{letter}{footer})", formats[style], float(total)
            )
        _highlight(summary, f"H6:H{footer + 1}", formats)
        review_rows = source.get("review_rows", [])
        if review_rows:
            _review(book, formats, venue, date_label, review_rows)
            summary.write_url(
                footer + 2, 0, "internal:'На сверку'!A1",
                string=f"Позиций на сверку · {len(review_rows)}",
            )
        elif not source["rows"]:
            text = (
                "В новых накладных нет сопоставимых позиций."
                if source["direct_invoices"] or source.get("routed_invoices")
                else "Новых приходных накладных поставщиков нет."
            )
            summary.write_string(footer + 2, 0, text)
    return buffer.getvalue()
