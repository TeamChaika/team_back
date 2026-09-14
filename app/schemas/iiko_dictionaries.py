"""Reference identifiers and names, without supplier credentials or personal contact fields."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.integrations.iiko.dictionaries import DictionaryKind
from app.schemas.iiko_catalog import IikoSnapshot


class Counteragent(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    code: str
    name: str
    deleted: StrictBool | None = None
    supplier: StrictBool | None = None
    employee: StrictBool | None = None
    client: StrictBool | None = None
    represents_store: StrictBool | None = Field(default=None, validation_alias="representsStore")
    represented_store_id: UUID | None = Field(default=None, validation_alias="representedStoreId")


class MeasureUnit(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    root_type: Literal["MeasureUnit"] = Field(validation_alias="rootType")
    code: str | None = None
    name: str
    deleted: StrictBool


class ProductCategory(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    name: str
    deleted: StrictBool
    code: str | None = None
    root_type: Literal["ProductCategory"] | None = Field(default=None, validation_alias="rootType")


class Account(BaseModel):
    """Full Account fields observed on Chain; unknown type codes remain source values."""

    model_config = ConfigDict(frozen=True)
    id: UUID
    root_type: Literal["Account"] = Field(validation_alias="rootType")
    code: str | None
    name: str
    deleted: StrictBool
    account_parent_id: UUID | None = Field(validation_alias="accountParentId")
    parent_corporate_id: UUID | None = Field(validation_alias="parentCorporateId")
    type: str
    system: StrictBool
    custom_transactions_allowed: StrictBool = Field(validation_alias="customTransactionsAllowed")


class DictionarySnapshot(IikoSnapshot):
    kind: DictionaryKind
    source_endpoint: str
    request: dict[str, str]
    deleted_count: int = Field(ge=0)
    without_code: int = Field(ge=0)


class DictionaryPage(BaseModel):
    snapshot: DictionarySnapshot
    offset: int
    limit: int
    items: list[Account | Counteragent | MeasureUnit | ProductCategory]
