from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BACKEND_DIR


class DocumentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAIKA_DOCUMENTS_",
        env_file=BACKEND_DIR / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    native_enabled: bool = False
    worker_enabled: bool = False
    database_url: SecretStr = SecretStr("")
    dashboard_url: str = "https://dashboard.chaika.team"
    iiko_url: str = ""
    iiko_login: str = ""
    iiko_password_hash: SecretStr = SecretStr("")
    bot_token: SecretStr = SecretStr("")
    telegram_local_address: Literal["", "0.0.0.0", "::"] = ""
