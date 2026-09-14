"""Метаданные полей SALES; значения показателей этот метод не получает."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.schemas.iiko_catalog import IikoSnapshot


class OlapColumnsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_type: Literal["SALES"] = Field(default="SALES", description="На этом шаге — продажи")


class OlapColumn(BaseModel):
    name: str = Field(min_length=1, description="Название в iikoOffice, не ключ запроса")
    type: str = Field(min_length=1, description="Тип поля, как его сообщает iiko")
    aggregation_allowed: StrictBool = Field(validation_alias="aggregationAllowed")
    grouping_allowed: StrictBool = Field(validation_alias="groupingAllowed")
    filtering_allowed: StrictBool = Field(validation_alias="filteringAllowed")
    tags: list[str]


type OlapColumns = dict[Annotated[str, Field(min_length=1)], OlapColumn]


class OlapColumnsResponse(IikoSnapshot):
    connection_id: Literal["primary"] = "primary"
    request: OlapColumnsQuery
    columns: OlapColumns = Field(
        description="Ключи — исходные FieldName iiko для составления OLAP-запроса"
    )
