"""Company API gateway. Unknown route families fail closed; portal retains ACLs."""

import re
import time
from collections import OrderedDict
from contextlib import AsyncExitStack
from http.cookies import SimpleCookie
from threading import Lock

import httpx
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .auth_validation import csrf_matches
from .entitlements import evaluate_feature
from .repository import Problem

_PUBLIC_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def public_route(path, method):
    if (method, path) in {
        ("GET", "/api/auth/recovery/telegram"),
        ("POST", "/api/auth/recovery/reset"),
    }:
        return "recovery"
    if method == "POST" and re.fullmatch(r"/api/payment-callbacks/" + _PUBLIC_ID, path):
        return "callback"
    if method == "GET" and re.fullmatch(r"/api/guest-deposits/" + _PUBLIC_ID, path):
        return "guest_read"
    if method == "POST" and re.fullmatch(
        r"/api/guest-deposits/" + _PUBLIC_ID + r"/(prepare|reconcile)", path
    ):
        return "guest_create" if path.endswith("/prepare") else "guest_reconcile"
    return None


def route_features(path, method="GET"):
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "api":
        return ()
    family = parts[1]
    simple = {
        "me": "profile.account",
        "overview": "analytics.overview",
        "sales": "analytics.sales",
        "discount-details": "analytics.sales",
        "indicators": "analytics.indicators",
        "balance-products": "inventory.balances",
        "employees": "employees.management",
        "topology": "iiko.order_events",
        "status": "operations.sync",
        "assistant": "assistant.chat",
        "profile": "profile.account",
        "deposits": "deposits.bookings",
        "payment-settings": "management.settings",
    }
    if family in simple:
        if family == "profile" and len(parts) > 2 and parts[2] == "telegram":
            return (simple[family], "notifications.telegram")
        return (simple[family],)
    if family == "auth" and parts[2:] == ["logout"]:
        return ("profile.account",)
    if family == "purchase-prices":
        return (
            "purchases.impact" if len(parts) > 2 and parts[2] == "impact" else "purchases.prices",
        )
    if family == "management":
        return (
            "management.users"
            if len(parts) > 2 and parts[2] == "accounts"
            else "management.settings",
        )
    if family == "resources" and len(parts) > 2:
        feature = {
            "products": "inventory.catalog",
            "charts": "inventory.catalog",
            "balances": "inventory.balances",
            "employees": "employees.management",
            "cash-shifts": "iiko.cash_shifts",
            "events": "iiko.order_events",
            "invoices": "iiko.history",
            "outgoing": "iiko.history",
            "transfers": "iiko.history",
            "writeoffs": "iiko.history",
        }.get(parts[2])
        return (feature,) if feature else ()
    if family == "documents" and len(parts) > 2:
        if parts[2] == "admin":
            return ("management.users",)
        feature = {
            "waybills": "documents.waybills",
            "waybill": "documents.waybills",
            "writeoff": "documents.writeoffs",
            "transfers": "documents.waybills",
            "writeoffs": "documents.writeoffs",
        }.get(parts[2])
        if not feature:
            return ()
        extras = (
            ("documents.approval",)
            if parts[-1]
            in {
                "approve",
                "accept",
                "reject",
                "confirm",
                "deny",
                "receive",
                "confirm_receipt",
                "reject_receipt",
            }
            else ("documents.dispatch",)
            if parts[-1] in {"send", "retry"}
            else ()
        )
        if parts[-1] in {"confirm", "confirm_receipt", "receive"}:
            extras = (*extras, "documents.dispatch")
        return (feature, *extras)
    if family == "commercial-invoices" and len(parts) > 2:
        feature = {
            "incoming": "commercial.incoming",
            "purchase": "commercial.incoming",
            "receipt": "commercial.incoming",
            "sale": "commercial.outgoing",
            "outgoing": "commercial.outgoing",
            "existing-outgoing": "commercial.outgoing",
            "admin": "management.users",
        }.get(parts[2])
        if not feature:
            return ()
        extras = (
            ("commercial.invoice_pdf",)
            if parts[-1] == "pdf"
            else ("commercial.counterparties",)
            if "counterparties" in parts and method == "POST"
            else ()
        )
        return (feature, *extras)
    return ()


def route_methods(path):
    if re.fullmatch(r"/api/deposits/" + _PUBLIC_ID, path):
        return {"GET", "PATCH"}
    public = {method for method in ("GET", "POST") if public_route(path, method)}
    if public:
        return public
    return {"GET", "POST"} if route_features(path) else set()


def setup_route(path, method):
    """Only platform-owner configuration required before acceptance can finish."""
    if (method, path) in {
        ("GET", "/api/me"),
        ("GET", "/api/management/venues"),
        ("GET", "/api/management/accounts"),
        ("GET", "/api/payment-settings"),
        ("GET", "/api/profile/telegram"),
        ("POST", "/api/profile/password"),
        ("POST", "/api/auth/logout"),
    }:
        return True
    return method == "POST" and (
        re.fullmatch(
            r"/api/(?:payment-settings|management)/venues/"
            + _PUBLIC_ID
            + r"(?:/terminals/"
            + _PUBLIC_ID
            + r")?",
            path,
        )
        is not None
        or re.fullmatch(
            r"/api/payment-settings/venues/"
            + _PUBLIC_ID
            + r"/terminals/"
            + _PUBLIC_ID
            + r"/validate",
            path,
        )
        is not None
    )


def history_request(path, method):
    return method in {"GET", "HEAD"} or (
        method == "POST"
        and (
            path == "/api/discount-details"
            or re.fullmatch(r"/api/indicators/(query|metric/[^/]+|options/[^/]+)", path) is not None
        )
    )


class RecoveryLimiter:
    """Bounded per-company transport-peer window; untrusted forwarded headers ignored."""

    def __init__(self):
        self.entries, self.lock = OrderedDict(), Lock()

    def check(self, company_id, peer):
        if not peer:
            raise Problem(503, "recovery_peer_unavailable", "Восстановление временно недоступно")
        now = time.monotonic()
        key = (str(company_id), peer)
        with self.lock:
            while self.entries and next(iter(self.entries.values()))[0] <= now - 300:
                self.entries.popitem(last=False)
            started, count = self.entries.get(key, (now, 0))
            if count >= 10:
                raise Problem(429, "recovery_rate_limited", "Повторите попытку позднее")
            if key not in self.entries and len(self.entries) >= 10000:
                self.entries.popitem(last=False)
            self.entries[key] = (started, count + 1)


class FullPortalProxy:
    def __init__(self, repository, registry, transport_factory=None, edge_peer=None):
        self.repository, self.registry = repository, registry
        self.recovery_limiter = RecoveryLimiter()
        self.edge_peer = edge_peer
        self.transport_factory = transport_factory or (
            lambda runtime: httpx.AsyncHTTPTransport(uds=runtime.socket_path)
        )

    async def forward(self, request, company):
        full_company = await run_in_threadpool(self.repository.get, company["id"])
        public = public_route(request.url.path, request.method)
        history = history_request(request.url.path, request.method)
        if public == "recovery":
            self.recovery_limiter.check(
                company["id"],
                self.edge_peer.resolve(request)
                if self.edge_peer
                else request.client.host
                if request.client
                else None,
            )
        working_resolver = getattr(self.registry, "resolve_working", self.registry.resolve)
        runtime = await run_in_threadpool(working_resolver, full_company)
        working = runtime is not None
        history_fallback = (
            history
            or public in {"callback", "guest_read", "guest_reconcile", "recovery"}
            or (request.method, request.url.path)
            in {("GET", "/api/me"), ("POST", "/api/profile/password"), ("POST", "/api/auth/logout")}
        )
        resolver = (
            getattr(self.registry, "resolve_existing", self.registry.resolve)
            if history_fallback
            else self.registry.resolve
        )
        if runtime is None and history_fallback:
            runtime = await run_in_threadpool(resolver, full_company)
        setup = False
        if runtime is None and not public:
            setup_resolver = getattr(self.registry, "resolve_setup", None)
            if setup_resolver is not None:
                runtime = await run_in_threadpool(setup_resolver, full_company)
                setup = runtime is not None
        if runtime is None:
            raise Problem(503, "runtime_not_ready", "Кабинет компании ещё не готов")
        if setup and not setup_route(request.url.path, request.method):
            raise Problem(403, "setup_route_only", "Доступна только первоначальная настройка")
        public = public_route(request.url.path, request.method)
        features = route_features(request.url.path, request.method)
        if public:
            features = ("payments.create",) if public == "guest_create" else ()
            token = ""
        elif not features:
            raise Problem(403, "route_not_enabled", "Раздел не подключён")
        else:
            token = request.cookies.get("saas_tenant_session", "")
            actor, session = await run_in_threadpool(
                self.repository.tenant_actor_session, token, company["id"]
            )
            if str(actor.company_id) != str(company["id"]):
                raise Problem(403, "tenant_boundary", "Другая компания")
            if setup and actor.kind != "platform_owner":
                raise Problem(403, "setup_owner_required", "Настройка доступна владельцу платформы")
            recovery_route = (request.method, request.url.path) in {
                ("GET", "/api/me"),
                ("POST", "/api/profile/password"),
                ("POST", "/api/auth/logout"),
            }
            if (
                session.get("must_change_password") or session.get("must_change")
            ) and not recovery_route:
                raise Problem(403, "password_change_required", "Измените временный пароль")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not csrf_matches(
                request.headers.get("x-csrf-token", ""), session["csrf_token"]
            ):
                raise Problem(403, "csrf_failed", "Обновите страницу")
        account_route = (request.method, request.url.path) in {
            ("GET", "/api/me"),
            ("POST", "/api/profile/password"),
            ("POST", "/api/auth/logout"),
        }
        for feature in () if account_route else features:
            decision = evaluate_feature(
                full_company,
                feature,
                operation="history" if history else "create",
                staff_allowed=True,
                warehouse_allowed=True,
            )
            if not decision.allowed:
                raise Problem(403, "feature_disabled", "Возможность недоступна по подписке")
        readiness = await run_in_threadpool(
            getattr(self.registry, "feature_readiness", lambda _: {}), full_company
        )
        owner_configuration = (
            not public
            and actor.kind == "platform_owner"
            and setup_route(request.url.path, request.method)
        )
        if working and not account_route and not setup and not owner_configuration:
            for feature in features:
                entry = readiness.get(feature, {})
                if entry.get("read" if history else "write") is not True:
                    raise Problem(403, "feature_not_ready", "Этот модуль ещё не настроен")
        terminal_change = (
            request.method == "POST"
            and re.fullmatch(
                r"/api/(?:payment-settings|management)/venues/"
                + _PUBLIC_ID
                + r"(?:/terminals/"
                + _PUBLIC_ID
                + r")?",
                request.url.path,
            )
            is not None
        )
        headers = {
            "host": request.headers["host"],
            "origin": request.headers.get("origin", ""),
            "cookie": "saas_tenant_session=" + token,
            "x-csrf-token": request.headers.get("x-csrf-token", ""),
        }
        if request.headers.get("content-type"):
            headers["content-type"] = request.headers["content-type"]
        async with AsyncExitStack() as stack:
            if terminal_change:
                change = getattr(self.registry, "payment_configuration_change", None)
                if change is None:
                    raise Problem(
                        503, "payment_readiness_unavailable", "Проверка терминала недоступна"
                    )
                guard = change(full_company)
                await run_in_threadpool(guard.__enter__)
                stack.push_async_callback(run_in_threadpool, guard.__exit__, None, None, None)
            async with httpx.AsyncClient(
                transport=self.transport_factory(runtime),
                timeout=60,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                result = await client.request(
                    request.method,
                    "http://runtime" + request.url.path,
                    params=request.query_params,
                    headers=headers,
                    content=await request.body(),
                )
        # Upstream is private, but redirects and arbitrary cookie/domain headers never escape.
        if 300 <= result.status_code < 400:
            raise Problem(502, "unexpected_redirect", "Ошибка внутреннего маршрута")
        safe = {
            k: v
            for k, v in result.headers.items()
            if k.lower() in {"content-type", "content-disposition"}
        }
        if request.method == "GET" and request.url.path == "/api/me" and result.status_code == 200:
            from .runtime_visibility import accepted_metadata

            try:
                metadata = result.json()
            except ValueError:
                raise Problem(502, "invalid_metadata", "Ошибка данных кабинета") from None
            if not isinstance(metadata, dict):
                raise Problem(502, "invalid_metadata", "Ошибка данных кабинета")
            response = JSONResponse(accepted_metadata(metadata, readiness, working=working))
        else:
            response = Response(result.content, status_code=result.status_code, headers=safe)
        if (
            request.method == "POST"
            and request.url.path == "/api/profile/password"
            and result.status_code in {200, 503}
        ):
            rotate_tenant_cookie(response, result.headers.get_list("set-cookie"))
        if request.url.path == "/api/auth/logout" and result.is_success:
            response.delete_cookie(
                "saas_tenant_session", path="/", secure=True, httponly=True, samesite="strict"
            )
        return response


def rotate_tenant_cookie(response, headers):
    """Reconstruct only the approved opaque, host-only tenant cookie."""
    accepted = []
    for header in headers:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            continue
        if set(parsed) != {"saas_tenant_session"}:
            continue
        item = parsed["saas_tenant_session"]
        if (
            item["domain"]
            or item["path"] != "/"
            or not item["secure"]
            or not item["httponly"]
            or item["samesite"].lower() != "strict"
        ):
            continue
        try:
            age = int(item["max-age"])
        except ValueError:
            continue
        if item.value == "" and age == 0:
            accepted.append(("", 0))
        elif re.fullmatch(r"[A-Za-z0-9_-]{32,128}", item.value) and 0 < age <= 8 * 3600:
            accepted.append((item.value, age))
    if len(accepted) != 1:
        return
    token, age = accepted[0]
    if age == 0:
        response.delete_cookie(
            "saas_tenant_session", path="/", secure=True, httponly=True, samesite="strict"
        )
    else:
        response.set_cookie(
            "saas_tenant_session",
            token,
            path="/",
            max_age=age,
            secure=True,
            httponly=True,
            samesite="strict",
        )
