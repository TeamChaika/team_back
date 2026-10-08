"""Dedicated owner API and SPA with explicit local and production boundaries."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ConfigDict, Field, StrictInt, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from .auth_validation import CredentialsModel, csrf_matches
from .config import SupabaseSettings, validate_origin, validate_private_key
from .connection_check import check_connection
from .connections import problem
from .models import CompanyWrite, ConnectionCredential, Model, metadata_url
from .repository import Problem
from .tenant_routes import mount_tenant_routes

BASE = "/api/saas-admin"
COOKIE = "saas_owner_session"


class Login(CredentialsModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)


class SavedCheck(Model):
    expected_version: StrictInt = Field(ge=1)


class DraftCheck(ConnectionCredential):
    url: str
    company_id: str | None = None
    connection_id: str | None = None
    expected_version: StrictInt | None = Field(default=None, ge=1)
    _url = field_validator("url")(metadata_url)


def write_credentials(body):
    if body.connection_credentials is None:
        return None
    return {
        key: {
            "login": item.login,
            "password": item.password.get_secret_value() if item.password else None,
        }
        for key, item in body.connection_credentials.items()
    }


def error_response(status, code, message, field=None):
    detail = {"code": code, "message": message}
    if field:
        detail["field"] = field
    return JSONResponse({"detail": detail}, status_code=status)


def create_app(
    data_dir, dist_dir=None, origin="http://127.0.0.1:8210", mode="local", *, repository=None
):
    allowed_host = validate_origin(origin, mode)
    secure = mode == "production"
    if secure:
        validate_private_key(data_dir)
        if not dist_dir:
            raise ValueError("Production requires a built static directory")
        root = Path(dist_dir).resolve()
        entry = root / "saas-admin.html"
        if (
            not root.is_dir()
            or not entry.is_file()
            or entry.is_symlink()
            or not (root / "assets").is_dir()
        ):
            raise ValueError("Production requires saas-admin.html and assets")
    data_root = Path(data_dir).resolve()
    if dist_dir:
        static_root = Path(dist_dir).resolve()
        if static_root.is_relative_to(data_root) or data_root.is_relative_to(static_root):
            raise ValueError("Data and static directories must not overlap")
        assets_root = (static_root / "assets").resolve()
        if assets_root == static_root or not assets_root.is_relative_to(static_root):
            raise ValueError("Assets directory must remain inside the static directory")
        for path in static_root.rglob("*"):
            if path.is_symlink():
                target = path.resolve()
                if target.is_relative_to(data_root) or data_root.is_relative_to(target):
                    raise ValueError("Static symlinks must not expose the data directory")
    owned_repository = repository is None
    if repository is None:
        from .postgres_repository import PostgresRepository
        from .supabase_auth import SupabaseAuthClient

        settings = SupabaseSettings()
        repository = PostgresRepository(
            settings.database_url.get_secret_value(),
            data_dir,
            auth=SupabaseAuthClient(
                settings.supabase_url + "/auth/v1",
                settings.anon_key.get_secret_value(),
                settings.auth_admin_key.get_secret_value(),
            ),
        )
        repository.validate_ready()
    repo = repository

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned_repository:
            await run_in_threadpool(repo.auth.close)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.repository = repo
    app.state.connection_tester = check_connection
    app.state.mode = mode

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return error_response(exc.status, exc.code, exc.message, exc.field)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        item = exc.errors()[0]
        field = ".".join(str(x) for x in item["loc"] if x != "body")
        return error_response(422, "validation_error", item["msg"], field)

    @app.middleware("http")
    async def boundary(request, call_next):
        hosts = request.headers.getlist("host")
        if len(hosts) != 1:
            return error_response(403, "invalid_host", "Недопустимый адрес сервера")
        host = hosts[0]
        company = None
        if host != allowed_host:
            try:
                validate_origin("https://" + host, mode="production")
            except ValueError:
                return error_response(403, "invalid_host", "Недопустимый адрес сервера")
            if not secure or not hasattr(repo, "company_for_domain"):
                return error_response(403, "invalid_host", "Недопустимый адрес сервера")
            matched = await run_in_threadpool(repo.company_for_domain, host)
            if matched is None:
                return error_response(403, "invalid_host", "Домен компании не подключён")
            company = {key: str(matched[key]) for key in ("id", "name", "slug")}
            path = request.url.path
            tenant_prefix = "/api/saas-tenant/" + company["slug"] + "/"
            if (
                path.startswith("/api/")
                and path != "/api/saas-context"
                and not path.startswith(tenant_prefix)
            ):
                return error_response(403, "tenant_boundary", "Этот раздел недоступен")
            if path.startswith("/tenant/") and path.rstrip("/") != "/tenant/" + company["slug"]:
                return error_response(403, "tenant_boundary", "Этот раздел недоступен")
        request.state.saas_company = company
        expected_origin = "https://" + host if secure else origin
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.getlist("origin") != [expected_origin]:
                return error_response(403, "invalid_origin", "Недопустимый источник запроса")
            if request.headers.get("sec-fetch-site") == "cross-site":
                return error_response(403, "invalid_origin", "Межсайтовый запрос запрещён")
            if request.headers.get("content-length", "").isdigit():
                if int(request.headers["content-length"]) > 128_000:
                    return error_response(422, "body_too_large", "Слишком большой запрос")
            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 128_000:
                    return error_response(422, "body_too_large", "Слишком большой запрос")
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def owner(request: Request):
        session = repo.session(request.cookies.get(COOKIE, ""))
        if request.method not in ("GET", "HEAD"):
            if not csrf_matches(request.headers.get("x-csrf-token", ""), session["csrf_token"]):
                raise Problem(403, "csrf_failed", "Обновите страницу и повторите действие")
        return session

    owner_dependency = Depends(owner)

    @app.get("/api/saas-context")
    def context(request: Request):
        company = request.state.saas_company
        return {"surface": "tenant" if company else "platform", "company": company}

    @app.get(BASE + "/health")
    def health():
        return {"status": "ok", "mode": mode}

    @app.post(BASE + "/auth/login")
    def login(body: Login, request: Request, response: Response):
        token, session = repo.login(
            body.username, body.password, request.client.host if request.client else "unknown"
        )
        previous = request.cookies.get(COOKIE)
        if previous:
            repo.logout(previous)
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=secure,
            max_age=8 * 3600,
            path=BASE,
        )
        return session

    @app.get(BASE + "/auth/me")
    def me(session=owner_dependency):
        return session

    @app.post(BASE + "/auth/logout", status_code=204)
    def logout(request: Request, session=owner_dependency):
        repo.logout(request.cookies.get(COOKIE, ""))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path=BASE, httponly=True, samesite="strict", secure=secure)
        return response

    @app.get(BASE + "/companies")
    def companies(
        q: str = Query("", max_length=200),
        status: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        session=owner_dependency,
    ):
        if status not in (None, "", "draft", "active", "suspended"):
            raise Problem(422, "validation_error", "Некорректный статус", "status")
        return repo.listing(q, status, limit, offset)

    @app.post(BASE + "/companies", status_code=201)
    def create(body: CompanyWrite, session=owner_dependency):
        return repo.save(
            body.model_dump(mode="json", exclude={"connection_credentials"}),
            session["user"],
            credentials=write_credentials(body),
        )

    @app.get(BASE + "/companies/{company_id}")
    def get(company_id: str, session=owner_dependency):
        return repo.get(company_id)

    @app.patch(BASE + "/companies/{company_id}")
    def patch(company_id: str, body: dict, session=owner_dependency):
        expected = body.get("expected_version")
        if type(expected) is not int or expected < 1:
            raise Problem(422, "validation_error", "Требуется версия записи", "expected_version")
        values = {k: v for k, v in repo.get(company_id).items() if k in CompanyWrite.model_fields}
        values.update({k: v for k, v in body.items() if k != "expected_version"})
        try:
            validated = CompanyWrite.model_validate(values)
        except ValidationError as exc:
            item = exc.errors()[0]
            raise Problem(
                422, "validation_error", item["msg"], ".".join(str(x) for x in item["loc"])
            ) from exc
        return repo.save(
            validated.model_dump(mode="json", exclude={"connection_credentials"}),
            session["user"],
            company_id,
            expected,
            credentials=write_credentials(validated),
        )

    @app.delete(BASE + "/companies/{company_id}", status_code=204)
    def archive(
        company_id: str, expected_version: int = Query(..., ge=1), session=owner_dependency
    ):
        values = {k: v for k, v in repo.get(company_id).items() if k in CompanyWrite.model_fields}
        repo.save(values, session["user"], company_id, expected_version, archive=True)
        return Response(status_code=204)

    @app.get(BASE + "/companies/{company_id}/events")
    def events(
        company_id: str,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        session=owner_dependency,
    ):
        return repo.events(company_id, limit, offset)

    @app.get(BASE + "/companies/{company_id}/connections")
    def connections(company_id: str, session=owner_dependency):
        return repo.connections(company_id)

    @app.post(BASE + "/connections/test")
    def draft_check(body: DraftCheck, session=owner_dependency):
        if body.password is None:
            if not body.company_id or not body.connection_id or body.expected_version is None:
                problem(422, "credentials_required", "Укажите пароль подключения")
            url, login, password = repo.check_inputs(
                body.company_id, body.connection_id, body.expected_version, body.url, body.login
            )
        else:
            url, login, password = body.url, body.login, body.password.get_secret_value()
        repo.reserve_check(session["user"]["id"], url)
        return app.state.connection_tester(url, login, password)

    @app.post(BASE + "/companies/{company_id}/connections/{connection_id}/test")
    def saved_check(
        company_id: str, connection_id: str, body: SavedCheck, session=owner_dependency
    ):
        url, login, password = repo.check_inputs(company_id, connection_id, body.expected_version)
        repo.reserve_check(session["user"]["id"], url)
        check = app.state.connection_tester(url, login, password)
        return repo.record_check(
            company_id, connection_id, body.expected_version, check, session["user"]
        )

    @app.get(BASE + "/companies/{company_id}/admin-access")
    def admin_access(company_id: str, session=owner_dependency):
        return repo.admin_access(company_id)

    @app.post(BASE + "/companies/{company_id}/admin-access", status_code=201)
    def create_admin_access(company_id: str, body: SavedCheck, session=owner_dependency):
        return repo.provision_admin(company_id, body.expected_version, session["user"])

    @app.post(BASE + "/companies/{company_id}/admin-access/reset")
    def reset_admin_access(company_id: str, body: SavedCheck, session=owner_dependency):
        return repo.provision_admin(company_id, body.expected_version, session["user"], reset=True)

    mount_tenant_routes(app, repo, mode=mode)

    if dist_dir:
        root = Path(dist_dir).resolve()

        @app.get("/{path:path}")
        def static(path: str):
            if path.startswith("api/"):
                raise Problem(404, "not_found", "Маршрут не найден")
            candidate = (root / path).resolve()
            if not candidate.is_relative_to(root):
                raise Problem(404, "not_found", "Файл не найден")
            if path.startswith("assets/"):
                assets = (root / "assets").resolve()
                if (
                    assets == root
                    or not assets.is_relative_to(root)
                    or not candidate.is_relative_to(assets)
                    or not candidate.is_file()
                ):
                    raise Problem(404, "not_found", "Файл не найден")
                return FileResponse(candidate)
            if path == "saas-admin.html" and candidate.is_file():
                return FileResponse(candidate)
            if Path(path).suffix:
                raise Problem(404, "not_found", "Файл не найден")
            entry = (root / "saas-admin.html").resolve()
            if not entry.is_relative_to(root) or not entry.is_file():
                raise Problem(404, "not_found", "Интерфейс ещё не собран")
            return FileResponse(entry)

    return app
