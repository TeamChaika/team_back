"""A separate, disabled-by-default token grants one fixed venue's aggregate GET only."""

import hmac
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.web.coverage import ZONE
from app.web.password_policy import require_personal_password
from app.web.permissions import require_section
from app.web.repository import serial
from app.web.warehouse_analytics import require_department_report


def decimal_value(value):
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("invalid_aggregate")
    return str(number)


def check_count(value):
    if value is None:
        return None
    number = Decimal(str(value))
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("invalid_check_count")
    return int(number)


def envelope(scope, now, cache_seconds):
    day = now.astimezone(ZONE).date()
    selected = scope.ids[0]
    name = next((d.get("name") for d in scope.departments if d["id"] == selected), None)
    return {
        "restaurant": {"id": str(selected), "name": name},
        "period": {
            "business_date": day.isoformat(),
            "start": datetime.combine(day, time.min, ZONE).isoformat(),
            "end_exclusive": datetime.combine(day + timedelta(days=1), time.min, ZONE).isoformat(),
            "timezone": str(ZONE),
            "partial": True,
        },
        "source": {
            "provider": "iiko_api",
            "report": "live_sales",
            "date_field": "OpenDate.Typed",
            "hour_field": "HourOpen",
        },
        "revenue": None,
        "checks": None,
        "average_check": None,
        "hourly": [{"hour": hour, "revenue": None, "checks": None} for hour in range(24)],
        "fetched_at": now.isoformat(),
        "last_good": None,
        "cache_age_seconds": None,
        "cache_seconds": cache_seconds,
        "is_fresh": False,
        "completeness": "unavailable",
        "coverage": {"status": "unavailable"},
    }


def create_ha_bridge_router(repo, live_sales, settings):
    router = APIRouter(prefix="/api/integrations/home-assistant", tags=["aggregate feed"])

    def permitted_scope():
        # Resolve current DB grants even for an owner, always with explicit selection.
        scope = repo.scope(settings.ha_bridge_user_id, settings.ha_bridge_department_id)
        require_personal_password(scope.user)
        require_section(scope.user, "sales")
        require_department_report(scope)
        if scope.ids != [settings.ha_bridge_department_id]:
            raise HTTPException(403, "Нет доступа к выбранному ресторану.")
        return scope

    @router.get("/today")
    def today(request: Request):
        if not settings.ha_bridge_enabled:
            raise HTTPException(404, "Маршрут не найден.")
        expected = settings.ha_bridge_token_sha256.get_secret_value()
        if (
            not re.fullmatch(r"[a-f0-9]{64}", expected)
            or settings.ha_bridge_user_id is None
            or settings.ha_bridge_department_id is None
        ):
            raise HTTPException(503, "Интеграция не настроена.")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if (
            scheme.lower() != "bearer"
            or not 32 <= len(token) <= 512
            or not hmac.compare_digest(sha256(token.encode()).hexdigest(), expected)
        ):
            raise HTTPException(
                401, "Неверный токен интеграции.", headers={"WWW-Authenticate": "Bearer"}
            )
        if request.query_params:
            raise HTTPException(422, "Этот маршрут не принимает параметры.")
        scope = permitted_scope()
        cache_seconds = live_sales.settings.live_sales_cache_seconds if live_sales else 300
        result = envelope(scope, datetime.now(UTC), cache_seconds)
        try:
            if live_sales is None:
                raise ValueError("live_sales_disabled")
            bundle, metadata = live_sales.get(scope)
            now = datetime.now(UTC)
            result = envelope(scope, now, cache_seconds)
            day = now.astimezone(ZONE).date()
            if (
                bundle is None
                or metadata.get("source") != "iiko_api"
                or bundle["manifest"]["business_date"] != day.isoformat()
            ):
                raise ValueError("today_unavailable")
            observed = [
                datetime.fromisoformat(bundle["reports"][kind]["info"]["received_at"])
                for kind in ("daily", "hours")
            ]
            if any(value.tzinfo is None for value in observed):
                raise ValueError("missing_observation_timezone")
            last_good = min(observed).astimezone(UTC)
            age = (now - last_good).total_seconds()
            if not -30 <= age < 1200:
                raise ValueError("observation_expired")
            daily = repo.sales(scope, "daily", day, day, live_bundle=bundle)
            hours = repo.sales(scope, "hours", day, day, live_bundle=bundle)
            result.update(
                revenue=decimal_value(daily["totals"]["revenue"]),
                checks=check_count(daily["totals"]["checks"]),
                average_check=decimal_value(daily["totals"]["average_check"]),
            )
            seen = set()
            for row in hours["hourly"]:
                hour = int(row["hour"])
                if not 0 <= hour <= 23 or hour in seen:
                    raise ValueError("invalid_hour")
                seen.add(hour)
                result["hourly"][hour] = {
                    "hour": hour,
                    "revenue": decimal_value(row["revenue"]),
                    "checks": check_count(row["checks"]),
                }
            issues = [
                issue for issue in hours["reconciliation_issues"] if issue["report"] == "hours"
            ]
            reconciled = not issues and all(
                daily["totals"][key] is not None
                and hours["totals"][key] is not None
                and Decimal(str(daily["totals"][key])) == Decimal(str(hours["totals"][key]))
                for key in ("revenue", "checks")
            )
            complete = (
                bool(daily["rows"])
                and bool(hours["rows"])
                and daily["complete"]
                and hours["complete"]
                and reconciled
            )
            result.update(
                last_good=last_good.isoformat() if complete else None,
                cache_age_seconds=max(0, int(age)) if complete else None,
                completeness="complete" if complete else "incomplete",
                is_fresh=bool(complete and not metadata.get("stale") and age < cache_seconds),
                coverage={
                    "status": "complete" if complete else "incomplete",
                    "daily_present": bool(daily["rows"]),
                    "hourly_present": bool(hours["rows"]),
                    "observed_hour_count": len(seen),
                    "reconciled": reconciled,
                    "reconciliation_issue_count": len(issues),
                    "observed_at": last_good.isoformat(),
                },
            )
            if not complete:
                # Consumers must not chart a partially reconciled day as real sales.
                empty = envelope(scope, now, cache_seconds)
                for key in ("revenue", "checks", "average_check", "hourly"):
                    result[key] = empty[key]
        except HTTPException as error:
            if error.status_code not in (423, 503):
                raise
        except (ValueError, InvalidOperation, KeyError, TypeError, OverflowError):
            pass
        # A refresh can take time: revoked grants must also prevent cached output now.
        permitted_scope()
        # SQL and permission reads can cross midnight or a cache deadline.
        final_now = datetime.now(UTC)
        result["fetched_at"] = final_now.isoformat()
        if result["period"]["business_date"] != final_now.astimezone(ZONE).date().isoformat():
            result["completeness"] = "unavailable"
        if result["last_good"] is not None:
            final_age = (final_now - datetime.fromisoformat(result["last_good"])).total_seconds()
            result["cache_age_seconds"] = max(0, int(final_age))
            if not -30 <= final_age < 1200:
                result["completeness"] = "unavailable"
            if final_age >= cache_seconds:
                result["is_fresh"] = False
        status = 200
        if result["completeness"] == "unavailable":
            # Never leak partially built values from a malformed or expired snapshot.
            unavailable = envelope(scope, datetime.now(UTC), cache_seconds)
            unavailable.update(
                last_good=result["last_good"], cache_age_seconds=result["cache_age_seconds"]
            )
            result, status = unavailable, 503
        return JSONResponse(
            serial(result), status_code=status, headers={"Cache-Control": "no-store"}
        )

    return router
