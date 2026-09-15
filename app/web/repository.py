"""Explicit, parameterized read queries with a shared scope for lists and details."""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import Settings
from app.web.balances import product_suggestions, read_balances
from app.web.coverage import partial_days
from app.web.overview import read_overview
from app.web.purchase_impact import read_purchase_impact
from app.web.purchase_impact_summary import add_weekly_impacts
from app.web.purchase_prices import read_purchase_prices


def serial(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


@dataclass(frozen=True)
class Scope:
    user: dict
    departments: tuple[dict, ...]
    selected: UUID | None
    store_ids: tuple[UUID, ...]
    rms_ids: tuple[str, ...]

    @property
    def unrestricted(self):
        return self.user["role"] == "owner" and self.selected is None

    @property
    def ids(self):
        return [self.selected] if self.selected else [d["id"] for d in self.departments]

    @property
    def codes(self):
        return [d["code"] for d in self.departments if d["id"] in self.ids and d["code"]]


def has_store_scope(store_id, parents, allowed):
    seen = set()
    while store_id is not None and store_id not in seen:
        if store_id in allowed:
            return True
        seen.add(store_id)
        store_id = parents.get(store_id)
    return False


class Repository:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pool = ConnectionPool(
            settings.database_url.get_secret_value(),
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 10,
                "application_name": "chaika-web",
                "autocommit": True,
                "prepare_threshold": None,
            },
            min_size=2,
            max_size=4,
            max_waiting=20,
            timeout=10,
            check=ConnectionPool.check_connection,
            open=False,
            name="chaika-web",
        )

    def open(self):
        self._pool.open(wait=True, timeout=10)

    def close(self):
        self._pool.close()

    @contextmanager
    def connection(self, *, repeatable=False):
        # open() is idempotent; standalone read-only scripts can also use the repository.
        self._pool.open()
        with self._pool.connection() as db, db.pipeline():
            # Supavisor may ignore startup options. Keep these transaction-local so they
            # cannot be lost on checkout or leak into the collector's pooled sessions.
            # BEGIN/COMMIT stay in this pipeline. On error, the pool connection context
            # rolls back before returning the session to another request.
            db.execute(
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
                if repeatable
                else "BEGIN READ ONLY"
            )
            db.execute("SET LOCAL statement_timeout='15000ms'")
            yield db
            db.execute("COMMIT")

    def scope(self, user_id: UUID, selected: UUID | None = None) -> Scope:
        with self.connection() as db:
            # Queue independent reads before fetching: one network round trip, fresh permissions.
            users = db.execute(
                "SELECT id,display_name,role FROM chaika.web_users WHERE id=%s AND active",
                (user_id,),
            )
            node_rows = db.execute(
                "SELECT id,parent_id,name,code,type FROM chaika.corporate_nodes WHERE "
                "source_id='primary'"
            )
            grants = db.execute(
                "SELECT department_id FROM chaika.web_department_access WHERE user_id=%s "
                "AND source_id='primary'",
                (user_id,),
            )
            store_rows = db.execute(
                "SELECT id,parent_id FROM chaika.stores WHERE source_id='primary'"
            )
            rms_rows = db.execute(
                "SELECT source_id,department_id FROM chaika.rms_bindings WHERE "
                "state='matched' AND chain_source_id='primary'"
            )
            user = users.fetchone()
            if not user:
                raise HTTPException(403, "Доступ к Chaika Team не назначен или отключён.")
            nodes = node_rows.fetchall()
            if user["role"] == "owner":
                allowed = {
                    n["id"]
                    for n in nodes
                    if n["type"] in {"DEPARTMENT", "CENTRALSTORE", "MANUFACTURE"}
                }
            else:
                allowed = {r["department_id"] for r in grants.fetchall()}
            if not allowed or (selected and selected not in allowed):
                raise HTTPException(403, "Нет доступа к выбранному ресторану.")
            departments = tuple(
                sorted((n for n in nodes if n["id"] in allowed), key=lambda n: n["name"] or "")
            )
            stores = store_rows.fetchall()
            parents = {n["id"]: n["parent_id"] for n in nodes + stores}
            visible = {selected} if selected else allowed
            store_ids = tuple(s["id"] for s in stores if has_store_scope(s["id"], parents, visible))
            rms = rms_rows.fetchall()
            rms_ids = tuple(r["source_id"] for r in rms if r["department_id"] in visible)
            return Scope(user, departments, selected, store_ids, rms_ids)

    def metadata(self, scope):
        with self.connection() as db:
            dates = db.execute(
                "SELECT business_date FROM chaika.sales_report_days WHERE "
                "source_id='primary' ORDER BY business_date DESC"
            )
            balances = db.execute(
                "SELECT accounting_timestamp FROM chaika.store_balance_reports WHERE "
                "source_id='primary' ORDER BY accounting_timestamp DESC"
            )
            dates, balances = dates.fetchall(), balances.fetchall()
        return serial(
            {
                "user": scope.user,
                "departments": scope.departments,
                "sales_dates": [r["business_date"] for r in dates],
                "balance_dates": [r["accounting_timestamp"] for r in balances],
            }
        )

    def overview(self, scope, start: date, end: date, granularity: str, *, live_bundle=None):
        with self.connection(repeatable=True) as db:
            return serial(
                read_overview(db, scope, start, end, granularity, live_bundle=live_bundle)
            )

    def sales(
        self,
        scope,
        kind: str,
        start: date,
        end: date,
        *,
        dish_id=None,
        dish_name=None,
        live_bundle=None,
    ):
        if kind not in {"daily", "dishes", "payments", "discounts", "returns", "waiters", "hours"}:
            raise HTTPException(404, "Отчёт не найден.")
        with self.connection() as db:
            coverage = db.execute(
                "SELECT d.business_date,s.checks,r.observed_at FROM chaika.sales_report_days d "
                "JOIN chaika.sales_report_sets s ON s.id=d.current_set_id "
                "JOIN chaika.sales_reports r ON r.set_id=d.current_set_id "
                "WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s "
                "AND r.kind=%s ORDER BY d.business_date",
                (start, end, kind),
            ).fetchall()
            loaded_dates = [row["business_date"] for row in coverage]
            result = db.execute(
                "SELECT d.business_date,r.id AS report_id,r.observed_at,s.reviewed,r.request, "
                "x.ordinal,x.department_id,n.name AS department,x.revenue,x.cost,x.checks,x.guests,"
                "x.discount,x.return_sum,x.quantity,x.dimensions "
                "FROM chaika.sales_report_days d JOIN chaika.sales_report_sets s ON "
                "s.id=d.current_set_id "
                "JOIN chaika.sales_reports r ON r.set_id=s.id JOIN "
                "chaika.sales_report_rows x ON x.report_id=r.id "
                "LEFT JOIN chaika.corporate_nodes n ON n.source_id=d.source_id AND "
                "n.id=x.department_id "
                "WHERE d.source_id='primary' AND d.business_date BETWEEN %s AND %s AND r.kind=%s "
                "AND x.department_id=ANY(%s::uuid[]) "
                "AND (r.kind<>'discounts' OR "
                "NULLIF(BTRIM(x.dimensions->>'ItemSaleEventDiscountType'),'') IS NOT NULL "
                "OR COALESCE(x.discount,0)<>0) "
                "AND (%s::text IS NULL OR x.dimensions->>'DishId'=%s) "
                "AND (%s::text IS NULL OR (NULLIF(x.dimensions->>'DishId','') IS NULL "
                "AND COALESCE(x.dimensions->>'DishName','')=%s)) "
                "ORDER BY d.business_date,x.ordinal "
                "LIMIT 20001",
                (start, end, kind, scope.ids, dish_id, dish_id, dish_name, dish_name),
            ).fetchall()
            if len(result) > 20000:
                raise HTTPException(
                    413, "Слишком много строк. Выберите меньший период или один ресторан."
                )
        if live_bundle is not None:
            from app.web.live_sales import report_coverage, report_rows

            day = date.fromisoformat(live_bundle["manifest"]["business_date"])
            coverage = [r for r in coverage if r["business_date"] != day] + report_coverage(
                live_bundle, [kind]
            )
            loaded_dates = sorted({r["business_date"] for r in coverage})
            result = [r for r in result if r["business_date"] != day] + report_rows(
                live_bundle, kind, scope, dish_id=dish_id, dish_name=dish_name
            )
            if len(result) > 20000:
                raise HTTPException(
                    413, "Слишком много строк. Выберите меньший период или один ресторан."
                )
        missing_dates = [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if start + timedelta(days=i) not in loaded_dates
        ]
        complete = not missing_dates
        departments = {str(d["id"]): d.get("name") for d in scope.departments}
        visible = {str(value) for value in scope.ids}
        reconciliation_issues = [
            {
                "date": day["business_date"],
                "report": check["report"],
                "field": check["field"],
                "department": departments.get(difference["department_id"]),
                **difference,
            }
            for day in coverage
            for check in day["checks"]
            if not check["exact_match"]
            for difference in check.get("differences", [])
            if difference["department_id"] in visible
        ]
        fields = ["revenue", "cost", "checks", "guests", "discount", "return_sum", "quantity"]

        def totals(rows):
            with localcontext() as ctx:
                ctx.prec = 70
                sums = {
                    key: sum((r[key] for r in rows), Decimal(0))
                    if rows and all(r[key] is not None for r in rows)
                    else None
                    for key in fields
                }
                if kind in {"payments", "discounts"}:
                    sums["checks"] = None
                sums["gross_profit"] = (
                    sums["revenue"] - sums["cost"]
                    if sums["revenue"] is not None and sums["cost"] is not None
                    else None
                )
                sums["average_check"] = (
                    sums["revenue"] / sums["checks"]
                    if sums["revenue"] is not None and sums["checks"]
                    else None
                )
                return sums

        hourly = []
        payment_groups = []
        if kind == "payments":
            groups = {r["dimensions"].get("PayTypes.Group") for r in result}
            for group in sorted(groups, key=lambda value: str(value or "")):
                rows = [r for r in result if r["dimensions"].get("PayTypes.Group") == group]
                payment_groups.append(
                    {"group": group, "totals": totals(rows if complete else []), "rows": rows}
                )
        discount_groups = []
        if kind == "discounts":
            by_department = {}
            for row in result:
                by_department.setdefault(row["department_id"], []).append(row)
            for department_id, rows in by_department.items():
                sums = totals(rows if complete else [])
                discount_groups.append(
                    {
                        "department_id": department_id,
                        "department": rows[0]["department"] or departments.get(str(department_id)),
                        "totals": {key: sums[key] for key in ("revenue", "discount")},
                    }
                )
            discount_groups.sort(
                key=lambda group: (
                    (group["department"] or "").casefold(),
                    str(group["department_id"]),
                )
            )
        if kind == "hours" and complete:
            for hour in sorted({r["dimensions"]["HourOpen"] for r in result}, key=int):
                hourly.append(
                    {
                        "hour": hour,
                        **totals([r for r in result if r["dimensions"]["HourOpen"] == hour]),
                    }
                )
        return serial(
            {
                "kind": kind,
                "reconciliation_issues": reconciliation_issues,
                "rows": result,
                "totals": totals(result if complete else []),
                "hourly": hourly,
                "payment_groups": payment_groups,
                "discount_groups": discount_groups,
                "loaded_dates": loaded_dates,
                "partial_days": partial_days(coverage),
                "missing_dates": missing_dates,
                "complete": complete,
                "start": start,
                "end": end,
            }
        )

    def purchase_prices(
        self,
        scope,
        kind="unlinked",
        exclude_household=True,
        selection=None,
        *,
        recent_only=True,
        include_impact=False,
    ):
        with self.connection(repeatable=True) as db:
            report = read_purchase_prices(
                db,
                scope,
                self.resource_query(scope, "invoices"),
                kind,
                exclude_household,
                selection,
                recent_only=recent_only,
            )
            if selection is None and include_impact:
                add_weekly_impacts(db, scope, report)
            return serial(report)

    def purchase_impact(self, scope, selection, analysis_department_id=None, all_departments=False):
        with self.connection(repeatable=True) as db:
            return serial(
                read_purchase_impact(
                    db,
                    scope,
                    self.resource_query(scope, "invoices"),
                    selection,
                    analysis_department_id,
                    all_departments,
                )
            )

    def resource_query(self, scope, resource):
        """All expressions below are fixed code; external values are bound parameters."""
        stores, params = list(scope.store_ids), []
        guard = "true"
        columns = []
        if resource in {"invoices", "outgoing"}:
            incoming = resource == "invoices"
            table = "incoming_invoices" if incoming else "outgoing_invoices"
            items = "incoming_invoice_items" if incoming else "outgoing_invoice_items"
            counterparty = "supplier_id" if incoming else "counteragent_id"
            base = (
                f"chaika.{table} t LEFT JOIN chaika.stores s ON s.source_id=t.source_id "
                "AND s.id=t.default_store_id LEFT JOIN chaika.counteragents c ON "
                f"c.source_id=t.source_id AND c.id=t.{counterparty}"
            )
            fields = (
                "t.id,t.source_id,t.document_number AS title,t.date_incoming AS date,"
                "t.status,s.name AS store,c.name AS counterparty,t.last_seen_at"
            )
            columns = [
                ("title", "Номер"),
                ("date", "Дата документа"),
                ("status", "Статус"),
                ("store", "Склад"),
                ("counterparty", "Контрагент"),
            ]
            if not scope.unrestricted:
                guard = (
                    "(t.default_store_id=ANY(%s::uuid[]) OR EXISTS(SELECT 1 FROM "
                    f"chaika.{items} i WHERE i.source_id=t.source_id AND i.document_id=t.id "
                    "AND i.present_in_latest AND i.store_id=ANY(%s::uuid[]))) AND NOT "
                    f"EXISTS(SELECT 1 FROM chaika.{items} i WHERE i.source_id=t.source_id AND "
                    "i.document_id=t.id AND i.present_in_latest AND NOT "
                    "COALESCE(COALESCE(i.store_id,t.default_store_id)=ANY(%s::uuid[]),false))"
                )
                params = [stores, stores, stores]
            day = "t.last_export_date"
            search = "concat_ws(' ',t.document_number,c.name,s.name)"
        elif resource in {"writeoffs", "transfers"}:
            transfer = resource == "transfers"
            table = "internal_transfers" if transfer else "writeoffs"
            store = "store_from_id" if transfer else "store_id"
            base = (
                f"chaika.{table} t LEFT JOIN chaika.stores s ON s.source_id=t.source_id "
                f"AND s.id=t.{store}"
            )
            fields = (
                "t.id,t.source_id,t.document_number AS title,t.date_incoming AS date,"
                "t.status,s.name AS store,t.last_seen_at"
            )
            columns = [
                ("title", "Номер"),
                ("date", "Дата"),
                ("status", "Статус"),
                ("store", "Со склада" if transfer else "Склад"),
            ]
            if not transfer:
                fields += (
                    ", (SELECT CASE WHEN count(*)>0 AND count(i.cost)=count(*) "
                    "THEN sum(i.cost) END FROM chaika.writeoff_items i "
                    "WHERE i.source_id=t.source_id AND i.document_id=t.id "
                    "AND i.present_in_latest) AS writeoff_sum"
                )
                columns.append(("writeoff_sum", "Сумма списания, ₽"))
            if transfer:
                base += (
                    " LEFT JOIN chaika.stores dst ON dst.source_id=t.source_id AND "
                    "dst.id=t.store_to_id"
                )
                fields += ",dst.name AS destination"
                columns.append(("destination", "На склад"))
            if not scope.unrestricted:
                guard = f"t.{store}=ANY(%s::uuid[])"
                params = [stores]
                if transfer:
                    guard = "(" + guard + " OR t.store_to_id=ANY(%s::uuid[]))"
                    params.append(stores)
            day = "t.last_export_date"
            search = "concat_ws(' ',t.document_number,s.name)"
        elif resource == "products":
            base = (
                "chaika.products t LEFT JOIN chaika.product_groups g ON "
                "g.source_id=t.source_id AND g.id=t.group_id LEFT JOIN "
                "chaika.measure_units u ON u.source_id=t.source_id AND "
                "u.id=t.main_unit_id"
            )
            fields = (
                "t.id,t.source_id,t.name AS title,t.code,t.num,t.type,g.name AS category,"
                "u.name AS unit,t.default_sale_price AS price,t.deleted,"
                "t.present_in_latest,t.last_seen_at"
            )
            columns = [
                ("title", "Номенклатура"),
                ("num", "Артикул"),
                ("type", "Тип"),
                ("category", "Группа"),
                ("unit", "Ед."),
                ("price", "Цена продажи, ₽"),
            ]
            day = None
            search = "concat_ws(' ',t.name,t.code,t.num,g.name)"
        elif resource == "charts":
            base = (
                "chaika.assembly_charts t LEFT JOIN chaika.products p ON "
                "p.source_id=t.source_id AND p.id=t.product_id"
            )
            fields = (
                "t.id,t.source_id,COALESCE(p.name,t.product_id::text) AS title,"
                "t.product_id,t.date_from,t.date_to,t.assembled_amount AS amount,"
                "t.last_seen_at"
            )
            columns = [
                ("title", "Блюдо / полуфабрикат"),
                ("date_from", "Действует с"),
                ("date_to", "Действует до"),
                ("amount", "Выход"),
            ]
            day = None
            search = "concat_ws(' ',p.name,t.product_id)"
        elif resource == "cash-shifts":
            base = (
                "chaika.cash_shifts t LEFT JOIN chaika.corporate_nodes n ON "
                "n.source_id=t.source_id AND n.id=t.department_id "
                "LEFT JOIN chaika.employees m ON m.source_id=t.source_id AND m.id=t.manager_id "
                "LEFT JOIN chaika.employees e ON e.source_id=t.source_id "
                "AND e.id=t.responsible_user_id"
            )
            fields = (
                "t.id,t.source_id,t.session_number::text AS title,t.open_date,t.close_date,"
                "t.accept_date,t.session_status,n.name AS restaurant,t.point_of_sale_name,"
                "t.cash_reg_number,t.cash_reg_serial,t.fiscal_number,t.pay_orders,"
                "t.sales_cash,t.sales_card,t.sales_credit,t.session_start_cash,t.sum_writeoff_orders,"
                "t.pay_in,t.pay_out,t.pay_income,t.cash_remain,t.cash_diff,t.mapping_state,"
                "m.name AS manager,e.name AS cashier,t.manager_id,t.responsible_user_id,"
                "t.department_id,t.group_id,t.point_of_sale_id,t.groups_snapshot_id,t.last_seen_at"
            )
            columns = [
                ("title", "Смена"),
                ("restaurant", "Ресторан"),
                ("point_of_sale_name", "Точка продаж"),
                ("open_date", "Открыта"),
                ("close_date", "Закрыта"),
                ("session_status", "Статус iiko"),
                ("pay_orders", "Заказы после скидок, ₽"),
                ("sales_cash", "Наличные, ₽"),
                ("sales_card", "Карты, ₽"),
                ("sales_credit", "В кредит, ₽"),
                ("mapping_state", "Связь с рестораном"),
            ]
            if not scope.unrestricted:
                guard = "t.mapping_state='matched' AND t.department_id=ANY(%s::uuid[])"
                params = [scope.ids]
            day = "t.open_date::date"
            search = "concat_ws(' ',t.session_number,n.name,t.point_of_sale_name,m.name,e.name)"
        elif resource == "employees":
            base = (
                "chaika.employees t LEFT JOIN chaika.employee_roles r ON "
                "r.source_id=t.source_id AND r.id=t.main_role_id"
            )
            fields = (
                "t.id,t.source_id,t.name AS title,t.code,r.name AS role,t.deleted,"
                "t.present_in_latest,t.last_seen_at"
            )
            columns = [
                ("title", "Сотрудник"),
                ("code", "Код"),
                ("role", "Основная должность"),
                ("deleted", "Удалён в iiko"),
            ]
            if not scope.unrestricted:
                guard = (
                    "(t.preferred_department_code=ANY(%s::text[]) OR t.department_codes && "
                    "%s::text[] OR t.responsibility_department_codes && %s::text[])"
                )
                params = [scope.codes] * 3
            day = None
            search = "concat_ws(' ',t.name,t.code,r.name)"
        else:
            raise HTTPException(404, "Раздел не найден.")
        return dict(
            base=base,
            fields=fields,
            columns=columns,
            guard=guard,
            params=params,
            day=day,
            search=search,
        )

    def resources(
        self, scope, resource, start=None, end=None, q="", offset=0, item_id=None, status=None
    ):
        spec = self.resource_query(scope, resource)
        clauses = ["t.source_id='primary'", spec["guard"]]
        params = list(spec["params"])
        if status:
            if resource not in {"invoices", "outgoing", "writeoffs", "transfers"}:
                raise HTTPException(422, "Фильтр статуса доступен для документов.")
            clauses.append("t.status=%s")
            params.append(status)
        if item_id:
            clauses.append("t.id=%s")
            params.append(item_id)
        else:
            if start and spec["day"]:
                clauses.append(spec["day"] + " BETWEEN %s AND %s")
                params.extend([start, end])
            if q:
                clauses.append(spec["search"] + " ILIKE %s")
                params.append(
                    "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
        with self.connection() as db:
            count = db.execute(
                "SELECT count(*) AS n FROM " + spec["base"] + " WHERE " + " AND ".join(clauses),
                params,
            ).fetchone()["n"]
            rows = db.execute(
                "SELECT "
                + spec["fields"]
                + " FROM "
                + spec["base"]
                + " WHERE "
                + " AND ".join(clauses)
                + " ORDER BY "
                + (spec["day"] + " DESC," if spec["day"] else "")
                + "t.id LIMIT 50 OFFSET %s",
                [*params, offset],
            ).fetchall()
        return serial(
            {
                "rows": rows,
                "total": count,
                "columns": [{"key": k, "label": v} for k, v in spec["columns"]],
                "offset": offset,
                "limit": 50,
            }
        )

    def detail(self, scope, resource, item_id):
        visible = self.resources(scope, resource, item_id=item_id)
        if not visible["rows"]:
            raise HTTPException(404, "Документ не найден или недоступен.")
        header = visible["rows"][0]
        specs = {
            "invoices": (
                "incoming_invoice_items",
                "document_id",
                "num,num_occurrence",
                "i.num,i.amount,i.actual_amount,i.price,i.sum",
                [
                    ("amount", "Количество"),
                    ("actual_amount", "Фактически"),
                    ("price", "Цена, ₽"),
                    ("sum", "Сумма, ₽"),
                ],
            ),
            "outgoing": (
                "outgoing_invoice_items",
                "document_id",
                "line_num",
                "i.line_num AS num,i.amount,i.price,i.sum",
                [("amount", "Количество"), ("price", "Цена, ₽"), ("sum", "Сумма, ₽")],
            ),
            "writeoffs": (
                "writeoff_items",
                "document_id",
                "num",
                "i.num,i.amount,i.cost,CASE WHEN count(i.cost) OVER()=count(*) OVER() "
                "THEN sum(i.cost) OVER() END AS document_sum",
                [("amount", "Количество"), ("cost", "Стоимость iiko, ₽")],
            ),
            "transfers": (
                "internal_transfer_items",
                "document_id",
                "num",
                "i.num,i.amount,i.cost",
                [("amount", "Количество"), ("cost", "Стоимость iiko, ₽")],
            ),
            "charts": (
                "assembly_chart_items",
                "chart_id",
                "sort_weight,id",
                "i.amount_in,i.amount_middle,i.amount_out",
                [("amount_in", "Брутто"), ("amount_middle", "Нетто"), ("amount_out", "Выход")],
            ),
        }
        items, columns, provenance = [], [], None
        with self.connection() as db:
            if resource in specs:
                table, parent, order, fields, columns = specs[resource]
                order = ",".join("i." + k for k in order.split(","))
                items = db.execute(
                    "SELECT i.product_id,COALESCE(p.name,i.product_id::text) AS product,"
                    f"{fields} FROM chaika.{table} i LEFT JOIN chaika.products p ON "
                    "p.source_id=i.source_id AND p.id=i.product_id WHERE "
                    f"i.source_id='primary' AND i.{parent}=%s AND i.present_in_latest ORDER BY "
                    f"{order} LIMIT 2001",
                    (item_id,),
                ).fetchall()
                if len(items) > 2000:
                    raise HTTPException(
                        413, "В документе более 2000 строк. Нужна отдельная выгрузка."
                    )
                if resource == "writeoffs":
                    # Use the same observation as the displayed composition even if
                    # a sync committed between loading the header and its lines.
                    header["writeoff_sum"] = items[0]["document_sum"] if items else None
                    for item in items:
                        item.pop("document_sum")
            parent_table = {
                "invoices": "incoming_invoices",
                "outgoing": "outgoing_invoices",
                "transfers": "internal_transfers",
                "charts": "assembly_charts",
                "cash-shifts": "cash_shifts",
            }.get(resource, resource)
            provenance = db.execute(
                f"SELECT r.id,r.resource,r.observed_at,r.sha256 FROM chaika.{parent_table} "
                "t JOIN chaika.raw_snapshots r ON r.id=t.last_snapshot_id WHERE "
                "t.source_id='primary' AND t.id=%s",
                (item_id,),
            ).fetchone()
            if resource == "charts":
                extra = db.execute(
                    "SELECT details->>'technology_description' AS technology FROM "
                    "chaika.assembly_charts WHERE source_id='primary' AND id=%s",
                    (item_id,),
                ).fetchone()
                header.update(extra)
            if resource == "cash-shifts":
                evidence = db.execute(
                    "SELECT id,observed_at,sha256 FROM chaika.raw_snapshots WHERE id=%s",
                    (header["groups_snapshot_id"],),
                ).fetchone()
                provenance.update(
                    groups_snapshot_id=evidence["id"],
                    groups_observed_at=evidence["observed_at"],
                    groups_sha256=evidence["sha256"],
                )
        return serial(
            {
                "header": header,
                "items": items,
                "columns": [{"key": "product", "label": "Номенклатура"}]
                + [{"key": k, "label": v} for k, v in columns],
                "provenance": provenance,
            }
        )

    def balances(
        self, scope, q="", offset=0, store_id=None, product_id=None, sort="sum", direction="desc"
    ):
        with self.connection(repeatable=True) as db:
            return serial(
                read_balances(db, scope, q, offset, store_id, product_id, sort, direction)
            )

    def balance_products(self, scope, q="", store_id=None):
        with self.connection() as db:
            return serial(product_suggestions(db, scope, q, store_id))

    def events(self, scope, start, end, q="", offset=0):
        params = [list(scope.rms_ids), start, end]
        extra = ""
        if q:
            extra = " AND e.order_number=%s"
            params.append(q)
        with self.connection() as db:
            rows = db.execute(
                "SELECT e.id,e.source_id,e.order_number AS title,e.order_id,e.occurred_at AS date,"
                "COALESCE(t.label,e.event_type) AS event,n.name AS department,e.event_sum,"
                "e.order_sum_after_discount,"
                "COALESCE(emp.name,e.actor_id::text) AS actor,count(*) OVER() AS total "
                "FROM chaika.rms_events e LEFT JOIN chaika.rms_event_types t ON "
                "t.source_id=e.source_id AND t.id=e.event_type "
                "JOIN chaika.rms_bindings b ON b.source_id=e.source_id LEFT JOIN "
                "chaika.corporate_nodes n ON n.source_id=b.chain_source_id AND "
                "n.id=b.department_id "
                "LEFT JOIN chaika.employees emp ON emp.source_id='primary' AND emp.id=e.actor_id "
                "WHERE e.source_id=ANY(%s::text[]) AND e.order_id IS NOT NULL "
                "AND e.order_number IS NOT NULL AND "
                "e.occurred_at>=(%s::date::timestamp AT TIME ZONE 'Europe/Simferopol') "
                "AND e.occurred_at<((%s::date+1)::timestamp AT TIME ZONE 'Europe/Simferopol')"
                + extra
                + " ORDER BY e.occurred_at DESC,e.id LIMIT 50 OFFSET %s",
                [*params, offset],
            ).fetchall()
        return serial(
            {
                "rows": rows,
                "total": rows[0]["total"] if rows else 0,
                "offset": offset,
                "limit": 50,
                "columns": [
                    {"key": k, "label": v}
                    for k, v in [
                        ("date", "Время"),
                        ("title", "Заказ"),
                        ("department", "Ресторан"),
                        ("event", "Событие"),
                        ("actor", "Сотрудник"),
                        ("event_sum", "Сумма, ₽"),
                    ]
                ],
            }
        )

    def status(self, scope):
        with self.connection() as db:
            coverage = db.execute(
                "SELECT d.source_id,n.name AS restaurant,min(d.event_date) AS date_from,"
                "max(d.event_date) AS date_to,count(*) AS days,max(d.observed_at) AS "
                "observed_at FROM chaika.rms_event_days d JOIN chaika.rms_bindings b ON "
                "b.source_id=d.source_id JOIN chaika.corporate_nodes n ON "
                "n.source_id=b.chain_source_id AND n.id=b.department_id WHERE "
                "d.source_id=ANY(%s::text[]) GROUP BY d.source_id,n.name ORDER BY n.name",
                (list(scope.rms_ids),),
            ).fetchall()
            runs = []
            scheduled = []
            if scope.user["role"] == "owner":
                runs = db.execute(
                    "SELECT job,status,started_at,finished_at,error_code,counts->>'resource' "
                    "AS resource,counts->>'completed_through' AS completed_through FROM "
                    "chaika.sync_runs ORDER BY started_at DESC LIMIT 20"
                ).fetchall()
                if self.settings.sync_enabled:
                    from app.scheduler import JOBS

                    latest = db.execute(
                        "SELECT DISTINCT ON (job) job,status,slot,started_at,finished_at,"
                        "error_code,"
                        "next_retry_at,attempts FROM chaika.scheduled_sync_runs "
                        "ORDER BY job,slot DESC"
                    ).fetchall()
                    by_key = {r["job"]: r for r in latest}
                    scheduled = [
                        {
                            "job": j.key,
                            "label": j.label,
                            "schedule": j.schedule,
                            **by_key.get(j.key, {"status": "waiting"}),
                        }
                        for j in JOBS
                    ]
            observations = (
                db.execute(
                    "SELECT resource,max(observed_at) AS observed_at FROM "
                    "chaika.raw_snapshots WHERE source_id='primary' GROUP BY resource ORDER "
                    "BY resource"
                ).fetchall()
                if scope.user["role"] == "owner"
                else []
            )
            cash_days = (
                db.execute(
                    "SELECT d.open_day AS date,d.row_count AS shifts,d.observed_at "
                    "FROM chaika.cash_shift_days d WHERE d.source_id='primary' ORDER BY d.open_day"
                ).fetchall()
                if scope.unrestricted
                else db.execute(
                    "SELECT open_date::date AS date,count(*) AS shifts,"
                    "max(last_seen_at) AS observed_at "
                    "FROM chaika.cash_shifts WHERE source_id='primary' AND mapping_state='matched' "
                    "AND department_id=ANY(%s::uuid[]) "
                    "GROUP BY open_date::date ORDER BY open_date::date",
                    (scope.ids,),
                ).fetchall()
            )
        return serial(
            {
                "events": coverage,
                "runs": runs,
                "observations": observations,
                "cash_shift_days": cash_days,
                "scheduled": scheduled,
            }
        )
