from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import BACKEND_DIR


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAIKA_WEB_",
        env_file=BACKEND_DIR / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )
    supabase_url: str = "https://your-supabase.example"
    anon_key: SecretStr = SecretStr("")
    origin: str = "http://127.0.0.1:8013"
    secure_cookie: bool = False
    frontend_dir: Path = BACKEND_DIR / "frontend/dist"
    max_login_attempts: int = Field(default=10, ge=1, le=100)
