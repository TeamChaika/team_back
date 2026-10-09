import os
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BACKEND_DIR
from app.tenancy.config import load_runtime


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAIKA_WEB_",
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
                name: os.environ[f"RESTCONTROL_TENANT_WEB_{name.upper()}"]
                for name in settings_cls.model_fields
                if f"RESTCONTROL_TENANT_WEB_{name.upper()}" in os.environ
            }
            values.setdefault("origin", runtime.frontend_origin)
            values.setdefault("secure_cookie", True)
            values.setdefault("deposits_api_url", "")
            values.setdefault("documents_api_url", "")
            return values

        return init_settings, tenant_environment

    @model_validator(mode="after")
    def tenant_configuration(self):
        runtime = load_runtime()
        if runtime.mode == "tenant":
            if self.origin != runtime.frontend_origin or not self.secure_cookie:
                raise ValueError("Tenant web settings require exact origin and secure cookies")
            if (
                self.supabase_url == "https://your-supabase.example"
                or not self.anon_key.get_secret_value()
            ):
                raise ValueError("Explicit tenant Supabase URL and anon key are required")
            from urllib.parse import urlsplit

            for value in (self.deposits_api_url, self.documents_api_url):
                host = urlsplit(value).hostname or ""
                if host == "chaika.team" or host.endswith(".chaika.team"):
                    raise ValueError("Tenant service URLs cannot fall back to Chaika")
        return self

    supabase_url: str = "https://your-supabase.example"
    auth_admin_key: SecretStr = SecretStr("")
    anon_key: SecretStr = SecretStr("")
    origin: str = "http://127.0.0.1:8013"
    secure_cookie: bool = False
    session_days: int = Field(default=30, ge=1, le=90)
    frontend_dir: Path = BACKEND_DIR / "frontend/dist"
    max_login_attempts: int = Field(default=10, ge=1, le=100)
    deposits_api_url: str = "https://pay.chaika.team/api/v1"
    documents_api_url: str = "https://iiko.chaika.team/api/portal-documents"
    documents_enabled: bool = False
