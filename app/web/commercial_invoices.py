"""Commercial invoice routes using the portal session and native warehouse policy."""

import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.commercial_invoices.policy import administration
from app.tenancy.actor import actor_from_verified_scope
from app.web.permissions import require_admin, require_section
from app.web.repository import Scope

Kind = Literal["purchase", "sale"]


async def bounded_body(request, limit):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, "Слишком большой запрос.")
    return bytes(data)


def create_commercial_invoices_router(access):
    router = APIRouter(prefix="/api/commercial-invoices", tags=["commercial-invoices"])

    async def allowed(request: Request, scope: Annotated[Scope, Depends(access)]):
        kind = request.path_params.get("kind")
        if kind in {"purchase", "sale"}:
            require_section(scope.user, "invoices" if kind == "purchase" else "outgoing")
        elif request.url.path.endswith("/pdf"):
            require_section(scope.user, "outgoing")
        else:
            require_admin(scope.user)
        if request.method != "GET":
            request.app.state.auth.check_origin(request)
        return scope

    Access = Annotated[Scope, Depends(allowed)]

    async def call(request, scope, kind, document_id=None, action=None):
        service = getattr(request.app.state, "commercial_invoices", None)
        if service is None:
            raise HTTPException(503, "Ввод накладных ещё не включён.")
        payload = None
        if request.method == "POST":
            raw = await bounded_body(request, 256 * 1024)
            if len(raw) > 256 * 1024:
                raise HTTPException(422, "Слишком большой документ.")
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError
                json.dumps(payload, allow_nan=False)
            except (ValueError, TypeError):
                raise HTTPException(422, "Проверьте формат документа.") from None
        elif len(str(request.query_params)) > 4000:
            raise HTTPException(422, "Слишком много параметров.")
        return await run_in_threadpool(
            service.dispatch,
            actor_from_verified_scope(scope),
            request.method,
            kind,
            document_id,
            action,
            payload=payload,
            params=dict(request.query_params),
        )

    @router.get("/admin/grants")
    async def grants(request: Request, scope: Access):
        service = getattr(request.app.state, "commercial_invoices", None)
        if service is None:
            raise HTTPException(503, "Ввод накладных ещё не включён.")
        return await run_in_threadpool(
            administration, service.database, actor_from_verified_scope(scope)
        )

    @router.post("/admin/grants/{user_id}")
    async def save_grants(user_id: int, request: Request, scope: Access):
        service = getattr(request.app.state, "commercial_invoices", None)
        if service is None:
            raise HTTPException(503, "Ввод накладных ещё не включён.")
        raw = await bounded_body(request, 128 * 1024)
        if len(raw) > 128 * 1024:
            raise HTTPException(422, "Слишком большой запрос.")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            json.dumps(body, allow_nan=False)
        except (ValueError, TypeError):
            raise HTTPException(422, "Проверьте формат прав.") from None
        return await run_in_threadpool(
            administration,
            service.database,
            actor_from_verified_scope(scope),
            user_id=user_id,
            body=body,
        )

    @router.get("/{kind}/options")
    async def options(kind: Kind, request: Request, scope: Access):
        return await call(request, scope, kind, action="options")

    @router.get("/admin/counterparty-grants")
    async def counterparty_grants(request: Request, scope: Access):
        from app.commercial_invoices.counterparties import grants

        service = getattr(request.app.state, "commercial_invoices", None)
        if service is None:
            raise HTTPException(503, "Ввод накладных ещё не включён.")
        return await run_in_threadpool(grants, service.database, actor_from_verified_scope(scope))

    @router.post("/admin/counterparty-grants/{user_id}")
    async def save_counterparty_grants(user_id: int, request: Request, scope: Access):
        from app.commercial_invoices.counterparties import grants

        service = getattr(request.app.state, "commercial_invoices", None)
        if service is None:
            raise HTTPException(503, "Ввод накладных ещё не включён.")
        try:
            body = json.loads(await bounded_body(request, 4096))
        except ValueError:
            raise HTTPException(422, "Проверьте формат прав.") from None
        return await run_in_threadpool(
            grants,
            service.database,
            actor_from_verified_scope(scope),
            user_id=user_id,
            body=body,
        )

    @router.post("/{kind}/counterparties", status_code=202)
    async def create_counterparty(kind: Kind, request: Request, scope: Access):
        from app.commercial_invoices.counterparties import command

        service = getattr(request.app.state, "commercial_invoices", None)
        if service is None:
            raise HTTPException(503, "Ввод накладных ещё не включён.")
        try:
            body = json.loads(await bounded_body(request, 16384))
        except ValueError:
            raise HTTPException(422, "Проверьте формат контрагента.") from None
        return await run_in_threadpool(
            command, service, actor_from_verified_scope(scope), kind, body
        )

    @router.get("/{kind}/counterparty-operations/{operation_id}")
    async def counterparty_operation(
        kind: Kind, operation_id: UUID, request: Request, scope: Access
    ):
        from app.commercial_invoices.counterparties import operation

        service = getattr(request.app.state, "commercial_invoices", None)
        if service is None:
            raise HTTPException(503, "Ввод накладных ещё не включён.")
        return await run_in_threadpool(
            operation, service, actor_from_verified_scope(scope), kind, operation_id
        )

    @router.get("/{kind}/products")
    async def products(kind: Kind, request: Request, scope: Access):
        return await call(request, scope, kind, action="products")

    @router.get("/{kind}/counterparties")
    async def counterparties(kind: Kind, request: Request, scope: Access):
        return await call(request, scope, kind, action="counterparties")

    @router.get("/{kind}")
    async def listing(kind: Kind, request: Request, scope: Access):
        return await call(request, scope, kind)

    @router.post("/{kind}", status_code=201)
    async def create(kind: Kind, request: Request, scope: Access):
        return await call(request, scope, kind)

    @router.get("/{kind}/{document_id}")
    async def detail(kind: Kind, document_id: UUID, request: Request, scope: Access):
        return await call(request, scope, kind, document_id)

    @router.get("/sale/{document_id}/pdf")
    async def pdf(document_id: UUID, request: Request, scope: Access):
        result = await call(request, scope, "sale", document_id, "pdf")
        return Response(
            result,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="invoice-{document_id}.pdf"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/{kind}/{document_id}/{action}")
    async def mutate(
        kind: Kind,
        document_id: UUID,
        action: Literal["edit", "submit"],
        request: Request,
        scope: Access,
    ):
        return await call(request, scope, kind, document_id, action)

    return router
