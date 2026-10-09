"""Synthetic search regression corpus; no providers, documents or global cache."""

from datetime import UTC, datetime, timedelta
from time import perf_counter
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.documents import reads
from app.documents.catalog import product_suggestions
from app.documents.product_search import search_products

CATALOG = [
    {"id": "chicken", "name": "Мясо Бедро куриное"},
    {"id": "milk", "name": "Молоко 3.2% 0.5л", "article": "10021", "unit": "л"},
    {"id": "milk-other", "name": "Молоко 2.5% 1л", "article": "10022"},
    {"id": "tomato", "name": "Томат свежий"},
    {"id": "tree", "name": "Ликёр Ёлка"},
]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("кур бедр", ["chicken"]),
        ("бедр кур", ["chicken"]),
        ("молако", ["milk-other", "milk"]),
        ("vjkjrj", ["milk-other", "milk"]),
        ("vjkjrj 3.2", ["milk"]),
        ("vjkjrj 3,2", ["milk"]),
        ("vjkjrj 0.5k", ["milk"]),
        ("vjkjrj 0,5k", ["milk"]),
        ("vjkjrj 3.3", []),
        ("vjkjrj 3,3", []),
        ("vjkjrj 0.6k", []),
        ("ликер елка", ["tree"]),
        ("помидор", ["tomato"]),
        ("мол 3.2", ["milk"]),
        ("мол 3,2%", ["milk"]),
        ("мол 0.5л", ["milk"]),
        ("10021", ["milk"]),
        ("10023", []),
        ("мол 3.3", []),
        ("мол 0.6л", []),
        ("молоко бедр", []),
        ("молоко молоко", []),
        ("кур абракадабра", []),
        ("!!!", []),
    ],
)
def test_query_words_and_numeric_qualifiers(query, expected):
    assert [r["id"] for r in search_products(CATALOG, query)["rows"]] == expected


def test_rank_exact_then_prefix_then_alias_then_typo_is_stable():
    rows = [
        {"id": "typo", "name": "Томат"},
        {"id": "alias", "name": "Помидор"},
        {"id": "prefix", "name": "Томаты свежие"},
        {"id": "exact", "name": "Томаты"},
    ]
    assert [r["id"] for r in search_products(rows, "томаты")["rows"]] == [
        "exact",
        "prefix",
        "alias",
        "typo",
    ]
    assert search_products(rows, "томаты") == search_products(rows[::-1], "томаты")


def test_reordered_terms_with_overlapping_aliases_keep_identical_ranking():
    rows = [
        {"id": "a", "name": "Помидоры томаты молоко"},
        {"id": "b", "name": "Томат помидоры молоко"},
    ]
    forward = search_products(rows, "томат томаты молоко")
    assert forward == search_products(rows, "томаты томат молоко")
    assert forward == search_products(rows[::-1], "молоко томат томаты")


def test_metadata_limits_and_two_typos_are_conservative():
    assert search_products(CATALOG, "10021")["rows"][0]["unit_name"] == "л"
    assert "article" not in search_products(CATALOG, "кур бедр")["rows"][0]
    assert search_products(CATALOG, "мяси бедра") == {"rows": [], "total": 0}
    rows = [{"id": str(i), "name": "Молоко"} for i in range(100)]
    result = search_products(rows, "мол")
    assert len(result["rows"]) == 50 and result["total"] == 100


def test_article_cannot_supply_a_missing_name_or_numeric_qualifier():
    rows = [
        {"id": "salt", "name": "Соль 10 кг", "article": "1"},
        {"id": "milk", "name": "Молоко 2.5%", "article": "3.2%"},
    ]
    assert search_products(rows, "соль 1")["total"] == 0
    assert search_products(rows, "молоко 3.2%")["total"] == 0


@pytest.mark.parametrize("query", ["x" * 201, None, "\x00", " ".join(["мол"] * 13)])
def test_query_bounded(query):
    with pytest.raises(HTTPException) as error:
        search_products(CATALOG, query)
    assert error.value.status_code == 422


def test_native_permission_gate_runs_before_catalog(monkeypatch):
    monkeypatch.setattr(reads, "stores_for", lambda *_: [])
    monkeypatch.setattr(reads, "product_suggestions", lambda *_: pytest.fail("catalog accessed"))
    with pytest.raises(HTTPException) as error:
        reads.suggestions(None, {"id": "unauthorized"}, "writeoff", {"query": "мол"})
    assert error.value.status_code == 403


class CatalogDB:
    def __init__(self, fresh=True):
        self.fresh = fresh

    def execute(self, sql):
        if "commercial_products" in sql:
            row = {
                "data": CATALOG,
                "updated_at": datetime.now(UTC) - timedelta(days=0 if self.fresh else 3),
            }
        else:
            row = {
                "data": {"milk": "Молоко", "native": "Своя позиция"},
                "updated_at": datetime.now(UTC),
            }
        return SimpleNamespace(fetchone=lambda: row)


def test_native_uses_own_fresh_metadata_only():
    fresh = product_suggestions(CatalogDB())
    assert fresh[0] == {"id": "milk", "name": "Молоко", "article": "10021", "unit_name": "л"}
    assert product_suggestions(CatalogDB(False))[0] == {"id": "milk", "name": "Молоко"}
    assert fresh[1] == {"id": "native", "name": "Своя позиция"}


def test_ten_thousand_products_and_adversarial_repeated_terms():
    corpus = [
        {"id": str(i), "name": f"Молоко пастеризованное 3.2% Артикул {i}"} for i in range(10_000)
    ]
    start = perf_counter()
    result = search_products(corpus, "молако 3.2")
    elapsed = perf_counter() - start
    assert result["total"] == 10_000 and len(result["rows"]) == 50
    assert elapsed < 5, f"10k search took {elapsed:.3f}s"
    assert (
        search_products(
            [{"id": "repeated", "name": " ".join(["молоко"] * 11)}], " ".join(["молоко"] * 12)
        )["total"]
        == 0
    )


@pytest.mark.parametrize("kind", ["purchase", "sale"])
def test_commercial_permission_gate_before_search(monkeypatch, kind):
    from contextlib import contextmanager

    from app.commercial_invoices import service as commercial

    @contextmanager
    def connection(**_):
        yield None

    service = commercial.CommercialInvoiceService.__new__(commercial.CommercialInvoiceService)
    service.database = SimpleNamespace(connection=connection)
    service._actor = lambda *_: {"id": "actor"}
    monkeypatch.setattr(commercial, "stores_for", lambda *_: [])
    monkeypatch.setattr(commercial, "product_catalog", lambda *_: pytest.fail("catalog accessed"))
    with pytest.raises(HTTPException) as error:
        service.dispatch("actor", "GET", kind, action="products", params={"q": "мол"})
    assert error.value.status_code == 403


@pytest.mark.parametrize("kind", ["purchase", "sale"])
def test_commercial_uses_common_engine_and_keeps_response_contract(monkeypatch, kind):
    from contextlib import contextmanager

    from app.commercial_invoices import service as commercial

    @contextmanager
    def connection(**_):
        yield None

    service = commercial.CommercialInvoiceService.__new__(commercial.CommercialInvoiceService)
    service.database = SimpleNamespace(connection=connection)
    service._actor = lambda *_: {"id": "actor"}
    monkeypatch.setattr(commercial, "stores_for", lambda *_: ["own-warehouse"])
    monkeypatch.setattr(commercial, "product_catalog", lambda *_: CATALOG)
    result = service.dispatch("actor", "GET", kind, action="products", params={"q": "кур бедр"})
    assert result == {"items": [CATALOG[0]], "total": 1}
