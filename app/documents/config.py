import os
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BACKEND_DIR
from app.tenancy.config import load_runtime


class DocumentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAIKA_DOCUMENTS_",
        env_file=BACKEND_DIR / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        runtime = load_runtime()
        if runtime.mode == "legacy":
            return init_settings, env_settings, dotenv_settings, file_secret_settings

        def tenant_environment():
            values = {
                name: os.environ[f"RESTCONTROL_TENANT_DOCUMENTS_{name.upper()}"]
                for name in settings_cls.model_fields
                if f"RESTCONTROL_TENANT_DOCUMENTS_{name.upper()}" in os.environ
            }
            values.setdefault("dashboard_url", runtime.frontend_origin)
            return values

        return init_settings, tenant_environment

    @model_validator(mode="after")
    def tenant_configuration(self):
        runtime = load_runtime()
        if runtime.mode == "tenant":
            if self.dashboard_url != runtime.frontend_origin:
                raise ValueError("Document links must use the tenant's exact frontend origin")
            if self.native_enabled or self.worker_enabled or self.commercial_enabled:
                if not self.database_url.get_secret_value():
                    raise ValueError("Explicit tenant document database URL is required")
                parsed = urlsplit(self.iiko_url)
                host = parsed.hostname or ""
                if (
                    parsed.scheme != "https"
                    or not host
                    or parsed.username
                    or parsed.password
                    or parsed.query
                    or parsed.fragment
                    or host == "chaika.team"
                    or host.endswith(".chaika.team")
                    or not self.iiko_login
                    or not self.iiko_password_hash.get_secret_value()
                ):
                    raise ValueError("Explicit tenant document iiko credentials are required")
                if parsed.path.rstrip("/") not in ("", "/resto/api"):
                    raise ValueError("Document iiko URL must be a server origin or /resto/api")
        return self

    native_enabled: bool = False
    worker_enabled: bool = False
    commercial_enabled: bool = False
    commercial_submit_enabled: bool = False
    commercial_counterparty_create_enabled: bool = False
    commercial_seller_json: SecretStr = SecretStr("")
    database_url: SecretStr = SecretStr("")
    dashboard_url: str = "https://dashboard.chaika.team"
    iiko_url: str = ""
    iiko_login: str = ""
    iiko_password_hash: SecretStr = SecretStr("")
    bot_token: SecretStr = SecretStr("")
    bot_username: str = ""
    telegram_local_address: Literal["", "0.0.0.0", "::"] = ""
