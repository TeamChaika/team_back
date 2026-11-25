"""Morning purchase variance for direct and routed receipts at each venue."""

from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import sql

from app.operation_settings import settings_schema
from app.purchase_digest_xlsx import render_workbook
from app.web.purchase_prices import HISTORY_SIZE, HOUSEHOLD_GROUP_ID

QUERY = sql.SQL(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))
MOSCOW = ZoneInfo("Europe/Moscow")
# The primary iiko directory owns this stable UUID even if the department is renamed.
PROCUREMENT_GROUP = UUID("231a6b55-3b64-4b80-ad84-b97881bda814")


def build_report(db, department_id: UUID | str, business_date: date) -> bytes:
    """Return the XLSX for receipts available in the preceding 09:00–09:00 MSK window."""
    identifier = UUID(str(department_id))
    end = datetime.combine(business_date, time(9), MOSCOW)
    db.execute("SET LOCAL statement_timeout='75s'")
    db.execute("SET LOCAL enable_nestloop=off")
    source = db.execute(
        QUERY.format(schema=sql.Identifier(settings_schema(db))),
        {
            "venue": identifier,
            "procurement_group": PROCUREMENT_GROUP,
            "household": HOUSEHOLD_GROUP_ID,
            "history": HISTORY_SIZE,
            "start": end - timedelta(days=1),
            "end": end,
        },
    ).fetchone()
    if not source or str(source["venue_id"]) != str(identifier):
        raise ValueError("purchase_digest_venue_unavailable")
    return render_workbook(source, business_date)
