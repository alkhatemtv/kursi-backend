"""Application settings loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "sqlite:///./kursi.db"

    # Auth0
    auth0_domain: str = ""
    auth0_api_audience: str = ""
    auth0_algorithms: str = "RS256"
    auth0_namespace: str = "https://kursi.io/"

    # CORS — comma-separated origins
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # App
    app_env: str = "development"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth0_algorithms_list(self) -> list[str]:
        return [a.strip() for a in self.auth0_algorithms.split(",") if a.strip()]


settings = Settings()
