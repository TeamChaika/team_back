from datetime import date
from io import BytesIO
from uuid import UUID
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from app.purchase_digest_xlsx import render_workbook


def cells(book, part):
    sheet = ET.fromstring(book.read(f"xl/worksheets/sheet{part}.xml"))
    strings = [s.findtext("{*}t") for s in ET.fromstring(book.read("xl/sharedStrings.xml"))]
    result = {}
    for cell in sheet.findall(".//{*}c"):
        value = cell.findtext("{*}v")
        result[cell.get("r")] = strings[int(value)] if cell.get("t") == "s" else value
    return result, sheet


def record(number, store, cost):
    return {
        "doc": str(UUID(int=number)),
        "number": str(number),
        "product": "=FAKE()",
        "supplier": "Условный поставщик",
        "unit": "кг",
        "store": store,
        "qty": "2",
        "cost": cost,
        "prev_qty": "4",
        "prev_cost": "12",
        "stamp": "2026-01-01T09:00:00+03:00",
        "noticed": "2026-01-06T10:00:00",
    }


def test_mixed_invoice_is_counted_once_with_cached_prices_and_actual_warehouses():
    rows = [
        record(1, "Цех Пицца", "9"),
        record(1, "Бар", "4"),
        record(2, "Общий склад", "6"),
    ]
    with ZipFile(
        BytesIO(
            render_workbook(
                {"venue": "Пример", "rows": rows, "direct_invoices": 2}, date(2026, 1, 7)
            )
        )
    ) as book:
        sheets = ET.fromstring(book.read("xl/workbook.xml")).findall("{*}sheets/{*}sheet")
        assert [s.get("name") for s in sheets] == ["Сводка", "Кухня", "Бар", "Другие склады"]
        summary, _ = cells(book, 1)
        assert summary["A2"] == "Пример"
        assert summary["A3"].endswith("7 января 2026")
        assert [float(summary[f"{col}9"]) for col in "BCDEFGH"] == [2, 3, 19, 18, 3, -2, 1]
        for index, row in enumerate(rows, start=2):
            details, sheet = cells(book, index)
            assert details["A6"] == "=FAKE()"
            assert sheet.find(".//{*}c[@r='A6']/{*}f") is None
            assert details["O6"] == row["store"]
            assert details["I6"] == row["supplier"]
            assert details["J6"] == "№" + row["number"]
            assert 4.9 < float(details["L6"]) - float(details["K6"]) < 5.1
            assert float(details["E6"]) == 3
            assert sheet.find(".//{*}c[@r='B6']/{*}f") is not None
            assert sheet.find("{*}autoFilter").get("ref") == "A5:O6"
            links = ET.fromstring(book.read(f"xl/worksheets/_rels/sheet{index}.xml.rels"))
            assert links[0].get("Target") == "https://dashboard.chaika.team/invoices/" + row["doc"]


def test_zero_receipts_and_no_prior_comparables_are_distinguishable():
    for direct, message in (
        (0, "Новых приходных накладных поставщиков нет."),
        (1, "В новых накладных нет сопоставимых позиций."),
    ):
        with ZipFile(
            BytesIO(
                render_workbook(
                    {"venue": "Пример", "rows": [], "direct_invoices": direct}, date(2026, 1, 7)
                )
            )
        ) as book:
            summary, _ = cells(book, 1)
            assert summary["A10"] == message
            assert float(summary["H8"]) == 0
            assert cells(book, 2)[0]["A6"] == cells(book, 3)[0]["A6"]


def test_routed_purchase_uses_external_cost_and_opens_both_invoices():
    origin = str(UUID(int=51))
    row = {
        **record(3, "Склад Бар", "12"),
        "via": "Закупочный склад",
        "origin_doc": origin,
        "origin_number": "ВХ-51",
    }
    source = {"venue": "Ресторан", "rows": [row], "direct_invoices": 0, "routed_invoices": 1}
    with ZipFile(BytesIO(render_workbook(source, date(2026, 1, 7)))) as book:
        details, sheet = cells(book, 3)
        assert details["I6"] == row["supplier"]
        assert details["J5"] == "Приходная заведения"
        assert details["J6"] == "№3"
        assert details["P6"] == row["via"]
        assert details["Q6"] == "№ВХ-51"
        assert float(details["D6"]) == 6
        assert sheet.find("{*}autoFilter").get("ref") == "A5:Q6"
        links = ET.fromstring(book.read("xl/worksheets/_rels/sheet3.xml.rels"))
        assert [link.get("Target").rsplit("/", 1)[-1] for link in links] == [row["doc"], origin]
        summary, _ = cells(book, 1)
        assert [float(summary[f"{col}8"]) for col in "BCD"] == [1, 1, 12]


def test_review_sheet_separates_unresolved_and_known_supplier_without_baseline():
    no_match = {
        **record(3, "Склад Бар", "130"),
        "supplier": None,
        "via": "=Проверка()",
        "reason": "Исходная накладная не найдена",
    }
    no_history = {
        **record(4, "Кухня", "0"),
        "qty": "0",
        "via": "Закупочный склад",
        "origin_doc": str(UUID(int=55)),
        "origin_number": "ВХ-55",
        "reason": "Недостаточно данных для сравнения",
    }
    source = {
        "venue": "Ресторан",
        "rows": [],
        "direct_invoices": 0,
        "routed_invoices": 2,
        "review_rows": [no_match, no_history],
    }
    with ZipFile(BytesIO(render_workbook(source, date(2026, 1, 7)))) as book:
        sheets = ET.fromstring(book.read("xl/workbook.xml")).findall("{*}sheets/{*}sheet")
        assert [s.get("name") for s in sheets] == ["Сводка", "Кухня", "Бар", "На сверку"]
        summary, summary_xml = cells(book, 1)
        assert "2" in summary["A10"]
        assert summary_xml.find(".//{*}hyperlink[@ref='A10']").get("location") == "'На сверку'!A1"
        assert [float(summary[f"{col}8"]) for col in "BCDEFGH"] == [0] * 7
        detail, sheet = cells(book, 4)
        assert detail["B6"] == no_match["reason"]
        assert detail["D6"] == no_match["via"]
        assert sheet.find(".//{*}c[@r='D6']/{*}f") is None
        assert detail["E6"] == "Требует сверки"
        assert detail["F6"] == "№3"
        assert "G6" not in detail
        assert float(detail["J6"]) == 65
        assert float(detail["K6"]) == 130
        assert detail["B7"] == no_history["reason"]
        assert detail["E7"] == no_history["supplier"]
        assert detail["G7"] == "№ВХ-55"
        assert "J7" not in detail
