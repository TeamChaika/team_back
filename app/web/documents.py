"""Fixed Django routes using the dashboard's existing HttpOnly session."""

import json
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.tenancy.actor import actor_from_verified_scope
from app.web.auth import ACCESS_COOKIE
from app.web.permissions import require_admin, require_section
from app.web.repository import Scope

Kind = Literal["waybill", "writeoff"]
Action = Literal[
    "edit", "copy", "cancel", "confirm", "deny", "receive", "confirm_receipt", "reject_receipt"
]
SECTIONS = {"waybill": "transfers", "writeoff": "writeoffs"}
ERRORS = {
    400: "Проверьте параметры документа.",
    401: "Войдите в систему.",
    403: "Нет права на это действие с документами.",
    404: "Документ не найден или недоступен.",
    409: "Документ или права изменились. Обновите данные перед повтором.",
    422: "Проверьте поля документа и выбранные склады.",
    429: "Слишком много запросов. Повторите позже.",
}
CODES = {
    "not_linked": "Администратор должен подключить рабочий профиль и склады.",
    "unknown_result": "Результат отправки в iiko требует проверки. Повторная отправка запрещена.",
    "version": "Документ изменён. Откройте актуальную версию.",
    "revision": "Права изменены другим администратором. Обновите список.",
    "linked": "Аккаунт dashboard уже привязан к другому рабочему профилю.",
    "processed": "Документ уже обработан.",
    "catalog": "Справочник товаров временно недоступен.",
}


class DocumentsClient:
    def __init__(self, url, enabled, transport=None):
        self.enabled = enabled
        self.client = httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            timeout=httpx.Timeout(75, connect=5, pool=5),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def call(self, token, method, path, *, csv=False, **kwargs):
        if not self.enabled:
            raise HTTPException(503, "Раздел документов ещё не включён.")
        if not token:
            raise HTTPException(401, "Войдите в систему.")
        try:
            async with self.client.stream(
                method, path, headers={"Authorization": "Bearer " + token}, **kwargs
            ) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 16 * 1024 * 1024:
                        raise HTTPException(503, "Слишком большой ответ сервиса документов.")
                if response.status_code in ERRORS:
                    try:
                        payload = json.loads(data)
                        code = payload.get("code") if isinstance(payload, dict) else None
                        if not isinstance(code, str):
                            code = None
                    except ValueError:
                        code = None
                    raise HTTPException(
                        response.status_code, CODES.get(code, ERRORS[response.status_code])
                    )
                if response.status_code not in {200, 201}:
                    raise HTTPException(503, "Сервис документов временно недоступен.")
                if csv:
                    if not response.headers.get("content-type", "").startswith("text/csv"):
                        raise ValueError("Invalid export")
                    return bytes(data)
                payload = json.loads(data)
                if not isinstance(payload, dict):
                    raise ValueError("Invalid response")
                return payload
        except (httpx.HTTPError, ValueError):
            # Never retry an uncertain POST. The same request_id can be checked later.
            raise HTTPException(
                503, "Ответ сервиса документов не получен. Обновите карточку перед повтором."
            ) from None


def create_documents_router(access):
    router = APIRouter(prefix="/api/documents", tags=["documents"])

    async def allowed(request: Request, scope: Annotated[Scope, Depends(access)]):
        if request.path_params.get("kind") in SECTIONS:
            require_section(scope.user, SECTIONS[request.path_params["kind"]])
        else:
            require_admin(scope.user)
        if request.method != "GET":
            request.app.state.auth.check_origin(request)
        if scope.warehouse_restricted and not hasattr(request.app.state.documents, "dispatch"):
            raise HTTPException(403, "Ограничение по складам требует нативного сервиса документов.")
        request.state.documents_identity = actor_from_verified_scope(scope)
        return scope

    Access = Annotated[Scope, Depends(allowed)]

    async def call(request, path, *, csv=False):
        kwargs = {}
        if request.method == "POST":
            raw = await request.body()
            if len(raw) > 128 * 1024:
                raise HTTPException(422, "Слишком большой документ.")
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError
                json.dumps(payload, allow_nan=False)
            except (ValueError, TypeError):
                raise HTTPException(422, "Проверьте формат документа.") from None
            kwargs["json"] = payload
        else:
            if len(str(request.query_params)) > 4000:
                raise HTTPException(422, "Слишком много параметров.")
            kwargs["params"] = list(request.query_params.multi_items())
        service = request.app.state.documents
        if hasattr(service, "dispatch"):
            return await run_in_threadpool(
                service.dispatch,
                request.state.documents_identity,
                request.method,
                path,
                csv=csv,
                payload=kwargs.get("json"),
                params=dict(request.query_params),
            )
        return await service.call(
            request.cookies.get(ACCESS_COOKIE), request.method, path, csv=csv, **kwargs
        )

    @router.get("/admin/staff")
    async def staff(request: Request, scope: Access):
        return await call(request, "admin/staff")

    @router.post("/admin/staff/{user_id}")
    async def save_staff(user_id: int, request: Request, scope: Access):
        if user_id < 0:
            raise HTTPException(422, "Некорректный профиль.")
        return await call(request, f"admin/staff/{user_id}")

    @router.get("/{kind}/export")
    async def export(kind: Kind, request: Request, scope: Access):
        content = await call(request, f"{kind}/export", csv=True)
        return Response(
            content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{kind}-documents.csv"',
            },
        )

    @router.get("/{kind}/options")
    async def options(kind: Kind, request: Request, scope: Access):
        return await call(request, f"{kind}/options")

    @router.get("/{kind}/products")
    async def products(kind: Kind, request: Request, scope: Access):
        return await call(request, f"{kind}/products")

    @router.get("/{kind}")
    async def listing(kind: Kind, request: Request, scope: Access):
        return await call(request, kind)

    @router.post("/{kind}", status_code=201)
    async def create(kind: Kind, request: Request, scope: Access):
        return await call(request, kind)

    @router.get("/{kind}/{document_id}")
    async def detail(kind: Kind, document_id: int, request: Request, scope: Access):
        if document_id < 1:
            raise HTTPException(404, "Документ не найден.")
        return await call(request, f"{kind}/{document_id}")

    @router.post("/{kind}/{document_id}/{action}")
    async def act(kind: Kind, document_id: int, action: Action, request: Request, scope: Access):
        if document_id < 1:
            raise HTTPException(404, "Документ не найден.")
        return await call(request, f"{kind}/{document_id}/{action}")

    return router
