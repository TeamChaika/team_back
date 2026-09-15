"""Short-lived intraday iiko snapshots, scoped before returning any rows to a user."""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from time import monotonic
from uuid import UUID, uuid5

import httpx
import psycopg
from fastapi import HTTPException

from app.import_sales_review import METRICS, parse_review
from app.sync_references import configured_sources, reference_lock
from app.sync_sales_history import approved_templates, collect_day, error_code
from app.web.coverage import ZONE

log = logging.getLogger(__name__)


def fetch_today(settings):
    day = datetime.now(ZONE).date()
    source = configured_sources(settings)[0]
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-live-sales",
        ) as db,
        reference_lock(db),
    ):
        template_id, templates = approved_templates(db)
        # Workers use this private collector too. Drop its idle session before taking a licence.
        try:
            response = httpx.post(
                "http://127.0.0.1:8010/api/v1/iiko/connections/primary/logout",
                timeout=10,
                trust_env=False,
            )
            response.raise_for_status()
            if response.json().get("state") != "logged_out":
                raise ValueError("live_sales_logout_failed")
        except httpx.ConnectError:
            pass  # Standalone portal, no private collector session exists.
        with TemporaryDirectory(prefix="chaika-live-") as directory:
            root = Path(directory)

            async def collect():
                await asyncio.wait_for(
                    collect_day(settings, source, day, templates, template_id, root, Event()),
                    timeout=45,
                )

            asyncio.run(collect())
            return parse_review(root, source.fingerprint, allow_discrepancies=True)


class LiveSales:
    def __init__(self, settings, *, fetch=fetch_today, clock=monotonic):
        self.settings, self.fetch, self.clock = settings, fetch, clock
        self.lock = Lock()
        self.bundle = None
        self.expires = 0.0
        self.last_success = 0.0
        self.stale = False

    def get(self):
        today = datetime.now(ZONE).date().isoformat()
        with self.lock:
            valid_day = self.bundle and self.bundle["manifest"]["business_date"] == today
            if valid_day and self.clock() < self.expires:
                return self.bundle, self.metadata(stale=self.stale)
            try:
                bundle = self.fetch(self.settings)
                if bundle["manifest"]["business_date"] != today:
                    raise ValueError("live_sales_day_changed")
            except Exception as error:
                log.warning("Live sales refresh unavailable: %s", error_code(error))
                if valid_day and self.clock() - self.last_success < 1200:
                    # Don't turn yesterday or an unbounded old response into live data.
                    self.expires = min(self.clock() + 60, self.last_success + 1200)
                    self.stale = True
                    return self.bundle, self.metadata(stale=True)
                raise HTTPException(
                    503, "Сегодняшние данные iiko пока недоступны. Повторите запрос через минуту."
                ) from None
            self.bundle = bundle
            self.last_success = self.clock()
            self.stale = False
            self.expires = self.last_success + self.settings.live_sales_cache_seconds
            return bundle, self.metadata()

    def metadata(self, *, stale=False):
        return {
            "source": "iiko_api",
            "date": self.bundle["manifest"]["business_date"],
            "observed_at": max(r["info"]["received_at"] for r in self.bundle["reports"].values()),
            "stale": stale,
            "cache_seconds": self.settings.live_sales_cache_seconds,
        }


def report_rows(bundle, kind, scope, *, dish_id=None, dish_name=None):
    visible = {str(i) for i in scope.ids}
    names = {str(d["id"]): d.get("name") for d in scope.departments}
    report = bundle["reports"][kind]
    result = []
    for ordinal, raw in enumerate(report["rows"]):
        if raw["Department.Id"] not in visible:
            continue
        if (
            kind == "discounts"
            and not str(raw.get("ItemSaleEventDiscountType") or "").strip()
            and not raw.get("DiscountSum")
        ):
            continue
        if dish_id is not None and raw.get("DishId") != dish_id:
            continue
        if dish_name is not None and (raw.get("DishId") or raw.get("DishName", "") != dish_name):
            continue
        result.append(
            {
                "business_date": datetime.fromisoformat(bundle["manifest"]["business_date"]).date(),
                "report_id": uuid5(bundle["id"], kind),
                "ordinal": ordinal,
                "department_id": UUID(raw["Department.Id"]),
                "department": names.get(raw["Department.Id"]),
                "observed_at": datetime.fromisoformat(report["info"]["received_at"]),
                "request": report["info"]["request"],
                "reviewed": False,
                "live": True,
                **{
                    key: Decimal(raw[field]) if raw.get(field) is not None else None
                    for key, field in METRICS.items()
                },
                "dimensions": {
                    key: raw[key] for key in report["info"]["request"]["groupByRowFields"]
                },
            }
        )
    return result


def report_coverage(bundle, kinds):
    return [
        {
            "business_date": datetime.fromisoformat(bundle["manifest"]["business_date"]).date(),
            "kind": kind,
            "reviewed": False,
            "checks": bundle["checks"],
            "observed_at": datetime.fromisoformat(bundle["reports"][kind]["info"]["received_at"]),
        }
        for kind in kinds
    ]
