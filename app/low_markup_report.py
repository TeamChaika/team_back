"""Previous-day dish markup, using sales before discounts."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from io import BytesIO
from uuid import UUID
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from app.web.indicators import collect_reports


@dataclass(frozen=True)
class Row:
    dish_id: str
    name: str
    quantity: Decimal
    unit_cost: Decimal
    unit_price: Decimal
    markup: Decimal


@dataclass(frozen=True)
class Report:
    title: str
    business_date: date
    rows: tuple[Row, ...]
    excluded_cost_count: int


def request_body(business_date: date, departments: dict[str, str]) -> dict:
    for identifier in departments:
        UUID(identifier)
    return {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": ["OpenDate.Typed", "Department.Id", "DishId", "DishName"],
        "groupByColFields": [],
        "aggregateFields": ["DishSumInt", "ProductCostBase.ProductCost", "DishAmountInt"],
        "filters": {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "from": f"{business_date}T00:00:00",
                "to": f"{business_date + timedelta(days=1)}T00:00:00",
                "includeLow": True,
                "includeHigh": False,
            },
            "Department.Id": {"filterType": "IncludeValues", "values": sorted(departments)},
            "DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
            "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
            "Storned": {"filterType": "IncludeValues", "values": ["FALSE"]},
        },
    }


def _number(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("low_markup_invalid_number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("low_markup_invalid_number")
    return result


def build_reports(rows, business_date: date, departments: dict[str, str]) -> dict[str, Report]:
    if not isinstance(rows, list):
        raise ValueError("low_markup_invalid_rows")
    request_body(business_date, departments)
    grouped = {}
    with localcontext() as context:
        context.prec = 70
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("low_markup_invalid_row")
            department = row.get("Department.Id")
            if not isinstance(department, str) or department not in departments:
                raise ValueError("low_markup_department_mismatch")
            if row.get("OpenDate.Typed") != business_date.isoformat():
                raise ValueError("low_markup_date_mismatch")
            dish = row.get("DishId")
            if not isinstance(dish, str):
                raise ValueError("low_markup_invalid_dish")
            dish = str(UUID(dish))
            name = row.get("DishName")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("low_markup_invalid_name")
            quantity = _number(row.get("DishAmountInt"))
            gross = _number(row.get("DishSumInt"))
            raw_cost = row.get("ProductCostBase.ProductCost")
            cost = None if raw_cost is None else _number(raw_cost)
            if quantity <= 0:
                continue
            key = (department, dish)
            item = grouped.setdefault(
                key, dict(name=name, quantity=Decimal(0), gross=Decimal(0), cost=Decimal(0))
            )
            item["quantity"] += quantity
            item["gross"] += gross
            if cost is None or cost <= 0 or item["cost"] is None:
                item["cost"] = None
            else:
                item["cost"] += cost
        output = {identifier: [] for identifier in departments}
        excluded = dict.fromkeys(departments, 0)
        for (department, dish), item in grouped.items():
            cost, gross, quantity = item["cost"], item["gross"], item["quantity"]
            if cost is None:
                excluded[department] += 1
                continue
            # Compare totals before division or display rounding at the exact boundary.
            if (gross - cost) * 100 >= cost * 150:
                continue
            output[department].append(
                Row(
                    dish,
                    item["name"],
                    quantity,
                    cost / quantity,
                    gross / quantity,
                    (gross - cost) / cost * 100,
                )
            )
        return {
            identifier: Report(
                title,
                business_date,
                tuple(
                    sorted(
                        output[identifier], key=lambda item: (item.markup, item.name, item.dish_id)
                    )
                ),
                excluded[identifier],
            )
            for identifier, title in departments.items()
        }


def prepare_report(settings, business_date: date, departments: dict[str, str]) -> dict[str, Report]:
    if not departments:
        return {}
    results = collect_reports(settings, [request_body(business_date, departments)])
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("low_markup_invalid_reports")
    return build_reports(results[0], business_date, departments)


def _xml_text(value: str) -> str:
    return "".join(
        char
        for char in value
        if char in "\t\n\r"
        or "\x20" <= char <= "\ud7ff"
        or "\ue000" <= char <= "\ufffd"
        or "\U00010000" <= char <= "\U0010ffff"
    )


def export_xlsx(report: Report) -> bytes:
    """Write one numeric worksheet without treating dish names as formulas."""
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    ET.register_namespace("", namespace)

    def element(parent, name, attributes=None, text=None):
        child = ET.SubElement(parent, f"{{{namespace}}}{name}", attributes or {})
        child.text = text
        return child

    sheet = ET.Element(f"{{{namespace}}}worksheet")
    views = element(sheet, "sheetViews")
    view = element(views, "sheetView", {"workbookViewId": "0"})
    element(
        view,
        "pane",
        {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"},
    )
    columns = element(sheet, "cols")
    for index, width in enumerate((48, 16, 18, 22, 22), 1):
        element(
            columns,
            "col",
            {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"},
        )
    data = element(sheet, "sheetData")
    header = element(data, "row", {"r": "1"})
    for column, label in zip(
        "ABCDE",
        ("Блюдо", "Количество", "Наценка, %", "Себестоимость за ед.", "Цена продажи за ед."),
        strict=False,
    ):
        cell = element(header, "c", {"r": f"{column}1", "t": "inlineStr"})
        element(element(cell, "is"), "t", text=label)
    for index, item in enumerate(report.rows, 2):
        row = element(data, "row", {"r": str(index)})
        cell = element(row, "c", {"r": f"A{index}", "t": "inlineStr"})
        element(
            element(cell, "is"),
            "t",
            {"{http://www.w3.org/XML/1998/namespace}space": "preserve"},
            _xml_text(item.name),
        )
        for column, value in zip(
            "BCDE", (item.quantity, item.markup, item.unit_cost, item.unit_price), strict=False
        ):
            cell = element(row, "c", {"r": f"{column}{index}", "s": "1"})
            if column == "C":
                element(cell, "f", text=f"(E{index}-D{index})/D{index}*100")
            element(cell, "v", text=str(value))
    element(sheet, "autoFilter", {"ref": f"A1:E{len(report.rows) + 1}"})
    files = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            f'<workbook xmlns="{namespace}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Низкая наценка" sheetId="1" r:id="rId1"/></sheets>'
            '<calcPr calcMode="auto"/></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            "</Relationships>"
        ),
        "xl/styles.xml": (
            f'<styleSheet xmlns="{namespace}">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill '
            'patternType="gray125"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
            'borderId="0"/></cellStyleXfs>'
            '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" '
            'applyNumberFormat="1"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            "</styleSheet>"
        ),
        "xl/worksheets/sheet1.xml": ET.tostring(sheet, encoding="utf-8", xml_declaration=True),
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path, contents in files.items():
            archive.writestr(path, contents)
    return output.getvalue()
