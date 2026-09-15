"""Настройки из CHAIKA_* и собственного файла backend/.env."""

from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAIKA_",
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="Chaika Team API", min_length=1)
    database_url: SecretStr = SecretStr("")
    sync_api_key: SecretStr = SecretStr("")
    iiko_all_assembly_timeout_seconds: float = Field(default=180, gt=0, le=300)
    iiko_all_assembly_max_response_bytes: int = Field(
        default=128 * 1024 * 1024, gt=0, le=256 * 1024 * 1024
    )
    iiko_base_url: HttpUrl | None = None
    iiko_login: str = ""
    iiko_password: SecretStr = SecretStr("")
    iiko_connections_file: Path | None = None
    iiko_connections_json: SecretStr = SecretStr("")
    sync_enabled: bool = False
    live_sales_enabled: bool = False
    live_sales_cache_seconds: int = Field(default=300, ge=300, le=600)
    iiko_timeout_seconds: float = Field(default=15, gt=0, le=120)
    iiko_products_timeout_seconds: float = Field(default=180, gt=0, le=180)
    iiko_products_max_response_bytes: int = Field(
        default=256 * 1024 * 1024, gt=0, le=256 * 1024 * 1024
    )
    iiko_groups_timeout_seconds: float = Field(default=60, gt=0, le=180)
    iiko_groups_max_response_bytes: int = Field(default=16 * 1024 * 1024, gt=0, le=64 * 1024 * 1024)
    iiko_assembly_timeout_seconds: float = Field(default=30, gt=0, le=120)
    iiko_assembly_max_response_bytes: int = Field(
        default=4 * 1024 * 1024, gt=0, le=16 * 1024 * 1024
    )
    iiko_invoices_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_outgoing_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_outgoing_max_response_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    iiko_invoices_max_response_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    iiko_stores_timeout_seconds: float = Field(default=30, gt=0, le=120)
    iiko_stores_max_response_bytes: int = Field(default=4 * 1024 * 1024, gt=0, le=16 * 1024 * 1024)
    iiko_balances_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_balances_max_response_bytes: int = Field(
        default=4 * 1024 * 1024, gt=0, le=16 * 1024 * 1024
    )
    iiko_store_balances_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_store_balances_max_response_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    iiko_employees_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_dictionaries_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_dictionaries_max_response_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    iiko_employees_max_response_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )

    iiko_transfers_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_transfers_max_response_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    iiko_writeoffs_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_writeoffs_max_response_bytes: int = Field(
        default=16 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    iiko_cash_shifts_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_cash_shifts_max_response_bytes: int = Field(
        default=8 * 1024 * 1024, gt=0, le=32 * 1024 * 1024
    )
    iiko_olap_columns_timeout_seconds: float = Field(default=30, gt=0, le=120)
    iiko_olap_columns_max_response_bytes: int = Field(
        default=4 * 1024 * 1024, gt=0, le=16 * 1024 * 1024
    )
    iiko_olap_sales_timeout_seconds: float = Field(default=60, gt=0, le=120)
    iiko_olap_sales_max_response_bytes: int = Field(
        default=4 * 1024 * 1024, gt=0, le=16 * 1024 * 1024
    )

    @field_validator("iiko_base_url", "iiko_connections_file", mode="before")
    @classmethod
    def empty_url_is_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("iiko_base_url")
    @classmethod
    def validate_iiko_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is None:
            return None
        if value.scheme != "https":
            raise ValueError("Для iiko требуется HTTPS с проверкой сертификата")
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("Адрес iiko не должен содержать учётные данные или параметры")
        if (value.path or "").rstrip("/") != "/resto/api":
            raise ValueError("Укажите полный адрес API с окончанием /resto/api")
        return value

    @property
    def iiko_configured(self) -> bool:
        return bool(
            self.iiko_base_url and self.iiko_login and self.iiko_password.get_secret_value()
        )
