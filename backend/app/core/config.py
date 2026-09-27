from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # LLM - OpenRouter (primary)
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-3.5-sonnet"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # LLM - Gemini (fallback)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-1.5-pro"

    model_timeout_seconds: int = 60
    model_max_output_tokens: int = 16000
    model_temperature: float = 0.2

    llm_primary_provider: str = "gemini"

    frontend_url: str = "http://localhost:3000"

    preview_start_port: int = 3001
    preview_max_port: int = 3100

    preview_bind_host: str = "127.0.0.1"
    preview_public_host: str = "127.0.0.1"

    max_debug_attempts: int = 3

    # Railway writable paths
    database_url: str = "sqlite:////tmp/app.db"
    projects_dir: str = "/tmp/projects"

    class Config:
        env_file = ".env"
        protected_namespaces = ("settings_",)

    @property
    def projects_path(self) -> Path:
        p = Path(self.projects_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
