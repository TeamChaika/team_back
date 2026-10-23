from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import UUID
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from app.low_markup_report import build_reports, export_xlsx, prepare_report, request_body

DAY = date(2026, 10, 19)
DEPT = str(UUID(int=1))
DISH = str(UUID(int=2))
DEPARTMENTS = {DEPT: "Ресторан"}


def source(**changes):
    return {
        "OpenDate.Typed": DAY.isoformat(),
        "Department.Id": DEPT,
        "DishId": DISH,
        "DishName": "Суп",
        "DishSumInt": Decimal("240"),
        "ProductCostBase.ProductCost": Decimal("100"),
        "DishAmountInt": Decimal("2"),
        **changes,
    }


def report(rows):
    return build_reports(rows, DAY, DEPARTMENTS)[DEPT]


def test_gross_not_discounted_and_strict_boundary():
    assert report([source(DishSumInt=250, DishDiscountSumInt=1)]).rows == ()
    assert report([source(DishSumInt=Decimal("250.001"))]).rows == ()
    row = report([source(DishSumInt=Decimal("249.999"), DishDiscountSumInt=1)]).rows[0]
    assert row.markup == Decimal("149.999")
    assert row.unit_price == Decimal("124.9995")


def test_fractional_quantity_and_aggregation_by_identity():
    rows = report(
        [
            source(DishAmountInt=Decimal("0.5"), DishName="Старое имя"),
            source(DishAmountInt=Decimal("1.5"), DishName="Новое имя"),
            source(DishId=str(UUID(int=3)), DishName="Старое имя", DishSumInt=150),
        ]
    ).rows
    assert len(rows) == 2
    assert rows[0].markup == 50
    assert rows[1].quantity == 2
    assert rows[1].unit_cost == 100
    assert rows[1].unit_price == 240


@pytest.mark.parametrize("cost", [None, 0, -1])
def test_unknown_or_nonpositive_cost_excludes_whole_dish(cost):
    result = report([source(), source(**{"ProductCostBase.ProductCost": cost})])
    assert result.rows == ()
    assert result.excluded_cost_count == 1


def test_missing_cost_is_excluded_and_nonpositive_quantity_skipped():
    item = source()
    del item["ProductCostBase.ProductCost"]
    result = report([item, source(DishId=str(UUID(int=3)), DishAmountInt=0)])
    assert result.rows == ()
    assert result.excluded_cost_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("DishSumInt", None),
        ("DishAmountInt", None),
        ("DishSumInt", "240"),
        ("DishAmountInt", True),
        ("ProductCostBase.ProductCost", "100"),
        ("DishSumInt", Decimal("NaN")),
        ("DishSumInt", float("inf")),
        ("Department.Id", str(UUID(int=8))),
        ("Department.Id", None),
        ("OpenDate.Typed", "2026-10-18"),
        ("DishId", "invalid"),
        ("DishName", None),
        ("DishName", ""),
    ],
)
def test_malformed_or_out_of_scope_rows_fail(field, value):
    with pytest.raises(ValueError):
        report([source(**{field: value})])


@pytest.mark.parametrize("rows", [{}, None, [None]])
def test_shape_validation(rows):
    with pytest.raises(ValueError):
        report(rows)


def test_request_and_prepare(monkeypatch):
    body = request_body(DAY, DEPARTMENTS)
    assert len(body["groupByRowFields"]) + len(body["aggregateFields"]) == 7
    assert body["filters"]["Storned"]["values"] == ["FALSE"]
    assert body["filters"]["OrderDeleted"]["values"] == ["NOT_DELETED"]
    assert body["filters"]["Department.Id"]["values"] == [DEPT]
    assert body["filters"]["OpenDate.Typed"]["to"] == "2026-10-20T00:00:00"
    calls = []

    def collect(settings, bodies):
        calls.append(bodies)
        return [[source()]]

    monkeypatch.setattr("app.low_markup_report.collect_reports", collect)
    assert prepare_report(None, DAY, DEPARTMENTS)[DEPT].rows[0].markup == 140
    assert calls == [[body]]
    assert prepare_report(None, DAY, {}) == {}
    assert calls == [[body]]


def test_empty_report_retains_venue():
    result = report([])
    assert result.title == "Ресторан"
    assert result.business_date == DAY
    assert result.rows == ()
    assert result.excluded_cost_count == 0


def test_xlsx_numeric_formula_cache_and_literal_malicious_name():
    name = '=HYPERLINK("https://example.test","<danger>&")\x00'
    exported = export_xlsx(report([source(DishName=name, DishAmountInt=Decimal("0.5"))]))
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(exported)) as archive:
        assert archive.testzip() is None
        for path in archive.namelist():
            ET.fromstring(archive.read(path))
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        cells = {cell.attrib["r"]: cell for cell in sheet.findall(".//s:c", ns)}
        assert cells["A2"].attrib["t"] == "inlineStr"
        assert cells["A2"].find("s:is/s:t", ns).text == name.replace("\x00", "")
        assert cells["A2"].find("s:f", ns) is None
        assert cells["B2"].find("s:v", ns).text == "0.5"
        assert cells["C2"].find("s:f", ns).text == "(E2-D2)/D2*100"
        assert Decimal(cells["C2"].find("s:v", ns).text) == 140
        assert Decimal(cells["D2"].find("s:v", ns).text) == 200
        assert Decimal(cells["E2"].find("s:v", ns).text) == 480
        assert sheet.find("s:autoFilter", ns).attrib["ref"] == "A1:E2"
        assert sheet.find("s:sheetViews/s:sheetView/s:pane", ns).attrib["state"] == "frozen"
