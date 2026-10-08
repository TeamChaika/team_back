"""Validated tenant iiko responses adapted to the existing dashboard contracts."""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.web.overview import build_overview, changes, dates, totals

from .repository import Problem

FIELDS = {
    "revenue": "DishDiscountSumInt",
    "cost": "ProductCostBase.ProductCost",
    "checks": "UniqOrderId",
    "guests": "GuestNum",
    "quantity": "DishAmountInt",
}


def invalid():
    raise Problem(503, "iiko_invalid_report", "iiko вернул неподдерживаемый или неполный отчёт")


def departments(body):
    try:
        root = ElementTree.fromstring(
            body, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        if root.tag != "corporateItemDtoes" or len(root) > 5000:
            invalid()
        nodes = {}
        for item in root:
            if item.tag != "corporateItemDto":
                invalid()
            values = {}
            for element in item:
                if element.tag not in {"id", "parentId", "name", "code", "type", "deleted"}:
                    continue
                if element.tag in values or len(element):
                    invalid()
                values[element.tag] = element.text or ""
            key = str(UUID(values["id"]))
            if key in nodes or not values.get("type"):
                invalid()
            parent = str(UUID(values["parentId"])) if values.get("parentId") else None
            nodes[key] = {
                "id": key,
                "name": values.get("name") or key,
                "code": values.get("code", ""),
                "parent": parent,
                "type": values["type"],
                "deleted": values.get("deleted"),
            }
        for key in nodes:
            visited = set()
            while key in nodes:
                if key in visited:
                    invalid()
                visited.add(key)
                key = nodes[key]["parent"]
        return sorted(
            [
                {k: node[k] for k in ("id", "name", "code")}
                for node in nodes.values()
                if node["type"] == "DEPARTMENT" and node["deleted"] not in ("true", "1")
            ],
            key=lambda node: (node["name"], node["id"]),
        )
    except (ParseError, DefusedXmlException, ValueError, KeyError, RecursionError):
        invalid()


def payload(body):
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(body, parse_float=Decimal, object_pairs_hook=unique_fields)
    except (ValueError, UnicodeError, RecursionError):
        invalid()
    if not isinstance(value, dict):
        invalid()
    return value


def capabilities(body):
    columns = payload(body)
    required = {
        "Department.Id": ("groupingAllowed", "filteringAllowed"),
        "OpenDate.Typed": ("groupingAllowed", "filteringAllowed"),
        "DishDiscountSumInt": ("aggregationAllowed",),
        "DishId": ("groupingAllowed", "filteringAllowed"),
        "DishName": ("groupingAllowed", "filteringAllowed"),
        "DeletedWithWriteoff": ("filteringAllowed",),
        "OrderDeleted": ("filteringAllowed",),
    }
    for name, flags in required.items():
        column = columns.get(name)
        if not isinstance(column, dict) or not all(column.get(flag) is True for flag in flags):
            raise Problem(
                503, "iiko_fields_unavailable", "В iiko нет полей для этого отчёта продаж"
            )
    return {
        key
        for key, name in FIELDS.items()
        if isinstance(columns.get(name), dict) and columns[name].get("aggregationAllowed") is True
    }


def query(start, end, ids, groups, fields, dish_id=None, dish_name=None):
    filters = {
        "Department.Id": {"filterType": "IncludeValues", "values": sorted(ids)},
        "OpenDate.Typed": {
            "filterType": "DateRange",
            "periodType": "CUSTOM",
            "from": f"{start}T00:00:00",
            "to": f"{end + timedelta(days=1)}T00:00:00",
            "includeLow": True,
            "includeHigh": False,
        },
        "DeletedWithWriteoff": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
        "OrderDeleted": {"filterType": "IncludeValues", "values": ["NOT_DELETED"]},
    }
    if dish_id is not None:
        filters["DishId"] = {"filterType": "IncludeValues", "values": [dish_id]}
    if dish_name is not None:
        filters["DishName"] = {"filterType": "IncludeValues", "values": [dish_name]}
    return {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": groups,
        "groupByColFields": [],
        "aggregateFields": [FIELDS[key] for key in fields],
        "filters": filters,
    }


def rows(body, request, start, end, ids):
    response = payload(body)
    if response.get("summary", []) != []:
        invalid()
    values = response.get("data")
    if not isinstance(values, list) or len(values) > 20000:
        invalid()
    result = []
    seen = set()
    try:
        for ordinal, value in enumerate(values):
            if not isinstance(value, dict):
                invalid()
            parsed = {"_ordinal": ordinal}
            for field in request["groupByRowFields"]:
                raw = value.get(field)
                if field in ("DishId", "DishName") and raw in (None, ""):
                    parsed[field] = None
                    continue
                if not isinstance(raw, str) or len(raw) > 500:
                    invalid()
                parsed[field] = raw
            if "Department.Id" in parsed:
                parsed["Department.Id"] = str(UUID(parsed["Department.Id"]))
                if parsed["Department.Id"] not in ids:
                    invalid()
            if "OpenDate.Typed" in parsed:
                raw_day = parsed["OpenDate.Typed"]
                day = (
                    datetime.fromisoformat(raw_day).date()
                    if "T" in raw_day
                    else date.fromisoformat(raw_day)
                )
                if not start <= day <= end:
                    invalid()
                parsed["OpenDate.Typed"] = day
            if parsed.get("DishId") is not None:
                parsed["DishId"] = str(UUID(parsed["DishId"]))
            if "DishId" in parsed and not parsed["DishId"] and not parsed.get("DishName"):
                invalid()
            group = tuple(parsed[field] for field in request["groupByRowFields"])
            if group in seen:
                invalid()
            seen.add(group)
            for key, field in FIELDS.items():
                raw = value.get(field)
                parsed[key] = None
                if field in request["aggregateFields"] and raw is not None:
                    if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
                        invalid()
                    number = Decimal(str(raw))
                    if not number.is_finite() or abs(number) > Decimal("1e30"):
                        invalid()
                    if key in ("checks", "guests") and number < 0:
                        invalid()
                    if key == "checks" and number != number.to_integral_value():
                        invalid()
                    parsed[key] = number
            # Without money, a row cannot establish a valid sale or be ranked.
            if parsed["revenue"] is None:
                invalid()
            result.append(parsed)
    except (ValueError, ArithmeticError, TypeError):
        invalid()
    return result


def aggregate(raw, available, days):
    if len(raw) > 1:
        invalid()
    values = {
        key: raw[0][key] if raw else Decimal(0) if key in available else None
        for key in ("revenue", "cost", "checks", "guests")
    }
    return totals([values], days)


def sales_rows(raw, kind, request, names, observed, report_id):
    result = []
    for index, row in enumerate(raw):
        result.append(
            {
                "ordinal": index,
                "request": request,
                "business_date": row["OpenDate.Typed"],
                "report_id": report_id,
                "observed_at": observed,
                "department_id": row["Department.Id"],
                "department": names[row["Department.Id"]],
                **{key: row[key] for key in FIELDS},
                "discount": None,
                "return_sum": None,
                "dimensions": {field: row[field] for field in request["groupByRowFields"]},
                "reviewed": False,
                "live": True,
                "drilldown_available": False,
            }
        )
    return result


def overview(
    scope, start, end, grain, daily, dishes, current_total, previous_total, observed, available
):
    previous_start = start - timedelta(days=(end - start).days + 1)
    coverage = [
        {
            "business_date": day,
            "kind": kind,
            "reviewed": False,
            "checks": [],
            "observed_at": observed,
        }
        for day in dates(previous_start, end)
        for kind in ("daily", "dishes")
    ]
    # Empty successful money reports mean zero sales. Counters remain unknown in
    # additive rows; only the separate whole-scope iiko aggregate supplies them.
    daily_index = {(row["OpenDate.Typed"], row["Department.Id"]): row for row in daily}
    normalized = []
    for day in dates(previous_start, end):
        for department_id in scope.ids:
            row = daily_index.get((day, department_id))
            normalized.append(
                {
                    "business_date": day,
                    "department_id": department_id,
                    "revenue": row["revenue"] if row else Decimal(0),
                    "cost": row["cost"] if row else Decimal(0) if "cost" in available else None,
                    "checks": None,
                    "guests": None,
                }
            )
    grouped = defaultdict(list)
    for row in dishes:
        grouped[
            (
                row["OpenDate.Typed"] >= start,
                row["DishId"],
                row["DishName"] if row["DishId"] is None else None,
            )
        ].append(row)
    dish_values = []
    for (current, dish_id, fallback_name), values in grouped.items():
        # Match the saved dashboard's ORDER BY business_date DESC, ordinal DESC.
        latest = max(values, key=lambda row: (row["OpenDate.Typed"], row["_ordinal"]))
        dish_values.append(
            {
                "is_current": current,
                "dish_id": dish_id,
                "fallback_name": fallback_name,
                "dish_name": latest["DishName"],
                "revenue": sum((row["revenue"] for row in values), Decimal(0)),
                "quantity": sum((row["quantity"] for row in values), Decimal(0))
                if all(row["quantity"] is not None for row in values)
                else None,
            }
        )
    result = build_overview(scope, start, end, grain, coverage, normalized, dish_values)
    result["current"]["totals"] = current_total
    result["previous"]["totals"] = previous_total
    result["changes"] = changes(current_total, previous_total)
    return result
