"""Fixed read-only contracts for reference dictionaries."""

from dataclasses import dataclass
from typing import Literal

DictionaryKind = Literal["counteragents", "measure-units", "categories", "accounts"]

# Keep the established three-dictionary sync independent of the account catalogue.
BUNDLED_DICTIONARIES: tuple[DictionaryKind, ...] = ("counteragents", "measure-units", "categories")


@dataclass(frozen=True)
class DictionarySpec:
    endpoint: str
    params: dict[str, str]
    format: Literal["xml", "json"]
    root_type: str | None
    table: str


DICTIONARIES: dict[DictionaryKind, DictionarySpec] = {
    "accounts": DictionarySpec(
        "v2/entities/list",
        {"rootType": "Account", "includeDeleted": "true", "revisionFrom": "-1"},
        "json",
        "Account",
        "accounts",
    ),
    "counteragents": DictionarySpec(
        "suppliers", {"revisionFrom": "-1"}, "xml", None, "counteragents"
    ),
    "measure-units": DictionarySpec(
        "v2/entities/list",
        {"rootType": "MeasureUnit", "includeDeleted": "true", "revisionFrom": "-1"},
        "json",
        "MeasureUnit",
        "measure_units",
    ),
    "categories": DictionarySpec(
        "v2/entities/products/category/list",
        {"includeDeleted": "true", "revisionFrom": "-1"},
        "json",
        "ProductCategory",
        "product_categories",
    ),
}
