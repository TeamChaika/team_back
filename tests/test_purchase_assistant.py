"""AI boundary tests: real authorization path, simulated providers, isolated PostgreSQL."""

import asyncio
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.core.config import Settings
from app.portal import create_portal
from app.web.assistant import AssistantSettings, Context, DataTools, Message, ModelClient, Selection
from app.web.assistant_store import AssistantStore, access_hash
from app.web.settings import WebSettings
from tests.test_portal import USER, FakeRepository, login, provider

PRODUCT, UNIT = UUID(int=31), UUID(int=32)
SELECTION = {"product_id": str(PRODUCT), "unit_id": str(UNIT), "linked": False}


def price_row():
    return dict(
        product_id=str(PRODUCT),
        unit_id=str(UNIT),
        linked=False,
        product="Мясо Бедро куриное",
        unit="кг",
        scope_label="Сеть",
        current=dict(
            date="2026-09-04",
            price="585",
            amount="10",
            sum="5850",
            lines=[
                dict(
                    document_id=str(UUID(int=40)),
                    document_number="123",
                    supplier="Поставщик",
                    store="Кухня",
                    amount="10",
                    sum="5850",
                )
            ],
        ),
        previous=dict(price="508.0098", amount="60", sum="30480.588", count=6, receipts=[]),
        delta="76.9902",
        percent="15.154",
    )


class Reports(FakeRepository):
    def __init__(self):
        self.calls = []

    def purchase_prices(self, scope, kind, exclude_household=True, selection=None, **kwargs):
        self.calls.append((scope, kind, exclude_household, selection, kwargs))
        return {"rows": [price_row()] if selection in (None, (PRODUCT, None, UNIT, False)) else []}

    def purchase_impact(self, scope, selection, analysis_department_id=None, all_departments=False):
        assert selection == (PRODUCT, None, UNIT, False)
        assert all_departments
        return dict(
            price=price_row(),
            start="2026-08-15",
            end="2026-09-13",
            coverage={"complete": False},
            blockers=["Нет продаж за 2 дня"],
            totals={"weekly_delta": None},
            rows=[dict(dish="Салат", weekly_delta=None)],
            excluded=[],
        )


class MemoryStore:
    def __init__(self):
        self.chats, self.results, self.attempts = {}, {}, []

    def list(self, scope):
        return []

    def reserve(self, scope, payload, limit):
        self.attempts.append(payload)
        cid = payload.conversation_id or uuid4()
        self.chats[cid] = dict(
            id=str(cid), context=payload.context.model_dump(mode="json"), turns=[]
        )
        return cid, False

    def finish(self, scope, request_id, result=None):
        self.results[request_id] = result
        if result:
            p = next(p for p in self.attempts if p.request_id == request_id)
            c = list(self.chats.values())[-1]
            c["turns"].append(dict(id=str(request_id), question=p.question, **result))

    def history(self, scope, cid):
        return deepcopy(self.chats[cid])


def app_client(model_handler, *, configured=True):
    repo, store = Reports(), MemoryStore()
    app = create_portal(
        Settings(),
        WebSettings(_env_file=None, anon_key="test"),
        auth_transport=httpx.MockTransport(provider),
        repository=repo,
        assistant_store=store,
        assistant_settings=AssistantSettings(
            _env_file=None, provider="openrouter", api_key="private-ai-key" if configured else ""
        ),
        assistant_transport=httpx.MockTransport(model_handler),
    )
    return TestClient(app, base_url="http://127.0.0.1:8013"), repo, store


def payload():
    return {
        "question": "Объясни цену",
        "request_id": str(uuid4()),
        "context": {"product": SELECTION},
    }


def tool_response(name="product_history", args=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args or SELECTION),
                                },
                            }
                        ],
                    },
                }
            ]
        },
    )


def text_response():
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Цена 585 ₽/кг; база 508 ₽/кг."},
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20},
        },
    )


def test_complete_tool_cycle_history_sources_and_csrf():
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.host == "openrouter.ai"
        assert request.headers["authorization"] == "Bearer private-ai-key"
        assert body["model"] == "openai/gpt-5.4-mini"
        assert body["provider"]["require_parameters"]
        if len(requests) == 1:
            assert body["tool_choice"] == "required"
            return tool_response()
        data = json.loads(body["messages"][-1]["content"])
        assert data["latest"]["price"] == "585"
        assert data["baseline"]["count"] == 6
        assert "private-ai-key" not in request.content.decode()
        return text_response()

    client, repo, store = app_client(model)
    with client:
        assert client.post("/api/assistant/messages", json=payload()).status_code == 401
        login(client)
        assert (
            client.post(
                "/api/assistant/messages",
                json=payload(),
                headers={"Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        p = payload()
        response = client.post(
            "/api/assistant/messages", json=p, headers={"Origin": str(client.base_url).rstrip("/")}
        )
        assert response.status_code == 200, response.text
        result = response.json()["turns"][0]
        assert result["sources"][1]["href"] == "/invoices/" + str(UUID(int=40))
        assert result["usage"]["calls"] == 2
        assert repo.calls[0][3] == (PRODUCT, None, UNIT, False)
        assert store.results[UUID(p["request_id"])]
        assert len(requests) == 2


@pytest.mark.parametrize("status", [401, 402, 429, 500])
def test_provider_failure_is_redacted_and_recorded(status):
    client, _, store = app_client(
        lambda _: httpx.Response(status, json={"error": "secret-key-123"})
    )
    with client:
        login(client)
        response = client.post(
            "/api/assistant/messages", json=payload(), headers={"Origin": "http://127.0.0.1:8013"}
        )
        assert response.status_code == 503
        assert "secret-key" not in response.text
        assert list(store.results.values()) == [None]


def test_unconfigured_malformed_body_and_revoked_user():
    def forbidden(_):
        pytest.fail("No provider calls expected")

    client, repo, store = app_client(forbidden, configured=False)
    with client:
        login(client)
        assert client.get("/api/assistant/status").json()["configured"] is False
        for data in [
            payload() | {"question": " "},
            payload() | {"history": []},
            payload() | {"question": "a" * 2001},
        ]:
            assert client.post("/api/assistant/messages", json=data).status_code == 422
        assert (
            client.post(
                "/api/assistant/messages",
                json=payload(),
                headers={"Origin": "http://127.0.0.1:8013"},
            ).status_code
            == 503
        )
        assert not store.attempts
        repo.active = False
        assert client.get("/api/assistant/conversations").status_code == 403


def test_unknown_tool_and_bounded_loop():
    client, _, store = app_client(lambda _: tool_response("execute_sql", {"query": "DELETE"}))
    with client:
        login(client)
        assert (
            client.post(
                "/api/assistant/messages",
                json=payload(),
                headers={"Origin": "http://127.0.0.1:8013"},
            ).status_code
            == 422
        )
        assert list(store.results.values()) == [None]
    calls = []

    def loop(_):
        calls.append(1)
        return tool_response()

    client, _, _ = app_client(loop)
    with client:
        login(client)
        assert (
            client.post(
                "/api/assistant/messages",
                json=payload(),
                headers={"Origin": "http://127.0.0.1:8013"},
            ).status_code
            == 422
        )
        assert len(calls) == 5


def test_scope_filters_nulls_and_permission_change_between_tools():
    async def run():
        repo = Reports()
        scope = repo.scope(USER)
        tools = DataTools(repo, scope, Context(product=Selection(**SELECTION)))
        result = await tools.call("product_impact", json.dumps(SELECTION))
        assert result["totals"]["weekly_delta"] is None
        assert result["blockers"] == ["Нет продаж за 2 дня"]
        await tools.call("search_products", json.dumps({"query": "бедро", "sort": "percent_desc"}))
        assert repo.calls[0][1:3] == ("unlinked", True)
        assert repo.calls[0][-1]["recent_only"] is True
        repo.active = False
        with pytest.raises(HTTPException) as error:
            await tools.call("product_impact", json.dumps(SELECTION))
        assert error.value.status_code == 403

    asyncio.run(run())


def test_openai_responses_adapter_preserves_tool_call_identity():
    async def run():
        bodies = []

        def model(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "name": "product_history",
                            "call_id": "c1",
                            "arguments": json.dumps(SELECTION),
                        }
                    ],
                },
            )

        client = ModelClient(
            AssistantSettings(_env_file=None, api_key="test"), transport=httpx.MockTransport(model)
        )
        try:
            inputs = [{"role": "user", "content": "вопрос"}]
            calls, _, _ = await client.complete(inputs, required=True)
            assert calls[0][0] == "c1"
            assert bodies[0]["store"] is False
            client.tool_result(inputs, "c1", {"price": "1"})
            assert inputs[-1]["type"] == "function_call_output"
            assert inputs[-1]["call_id"] == "c1"
        finally:
            await client.client.aclose()

    asyncio.run(run())


@pytest.fixture
def chat_db():
    url = os.environ.get("CHAIKA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Isolated database required")
    assert "127.0.0.1:15438/" in url
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as db, db.transaction():
        users = [uuid4(), uuid4()]
        for uid in users:
            db.execute("INSERT INTO auth.users (id) VALUES (%s)", (uid,))
            db.execute(
                "INSERT INTO chaika.web_users (id,display_name,role) VALUES (%s,'Test','manager')",
                (uid,),
            )
        db.execute("SET LOCAL ROLE chaika_backend")

        class Pool:
            def open(self):
                pass

            @contextmanager
            def connection(self):
                yield db

        class Repo:
            _pool = Pool()

        store = AssistantStore(Repo())
        scope = FakeRepository().scope(USER)
        scopes = [replace(scope, user={**scope.user, "id": uid}) for uid in users]
        yield db, store, scopes
        raise psycopg.Rollback()


def test_database_user_isolation_idempotency_budget_and_history(chat_db):
    db, store, (alice, bob) = chat_db
    message = Message(**payload())
    cid, replay = store.reserve(alice, message, 2)
    assert not replay
    with pytest.raises(HTTPException) as busy:
        store.reserve(alice, Message(**payload()), 2)
    assert busy.value.status_code == 429
    store.finish(
        alice, message.request_id, dict(answer="Ответ", sources=[], usage={}, model="test")
    )
    assert store.reserve(alice, message, 2) == (cid, True)
    assert store.history(alice, cid)["turns"][0]["answer"] == "Ответ"
    assert store.list(bob) == []
    with pytest.raises(HTTPException) as hidden:
        store.history(bob, cid)
    assert hidden.value.status_code == 404
    with store.connection(bob) as scoped_db:
        assert scoped_db.execute("SELECT id FROM chaika.assistant_turns").fetchall() == []
        assert (
            scoped_db.execute("UPDATE chaika.assistant_turns SET answer='tampered'").rowcount == 0
        )
    with pytest.raises(HTTPException) as changed:
        store.history(replace(alice, store_ids=()), cid)
    assert changed.value.status_code == 409
    second = Message(**payload())
    store.reserve(alice, second, 2)
    store.finish(alice, second.request_id)
    with pytest.raises(HTTPException) as budget:
        store.reserve(alice, Message(**payload()), 2)
    assert budget.value.status_code == 429
    assert access_hash(alice) != access_hash(replace(alice, store_ids=()))


def test_database_rls_missing_identity_and_forged_insert(chat_db):
    db, store, (alice, bob) = chat_db
    message = Message(**payload())
    cid, _ = store.reserve(alice, message, 20)
    with db.transaction():
        db.execute("SELECT set_config('chaika.assistant_user','',true)")
        assert db.execute("SELECT * FROM chaika.assistant_conversations").fetchall() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege), db.transaction():
        db.execute("SELECT set_config('chaika.assistant_user',%s,true)", (str(bob.user["id"]),))
        db.execute(
            "INSERT INTO chaika.assistant_turns (id,conversation_id,user_id,question) "
            "VALUES (%s,%s,%s,'forged')",
            (uuid4(), cid, alice.user["id"]),
        )


def test_oversized_context_never_reaches_provider():
    async def run():
        def forbidden(request):
            pytest.fail("Oversized context must not be billed")

        client = ModelClient(
            AssistantSettings(_env_file=None, api_key="test"),
            transport=httpx.MockTransport(forbidden),
        )
        try:
            with pytest.raises(HTTPException) as error:
                await client.complete([{"role": "user", "content": "x" * 110001}])
            assert error.value.status_code == 422
        finally:
            await client.client.aclose()

    asyncio.run(run())
