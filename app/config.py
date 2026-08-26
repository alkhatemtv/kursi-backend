"""Application settings loaded from environment variables."""
from pydantic_settings import BaseSettings, SettingsConfigDict

# The environment labels the project recognises. Unknown values are still accepted
# (see Settings.environment) - this tuple documents the intended set.
KNOWN_ENVS = ("production", "staging", "development")


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
    # `ENV` is the canonical name (production | staging | development).
    # `APP_ENV` predates it and is still honoured so existing deployments that
    # only set APP_ENV keep reporting the same value. Read `settings.environment`
    # rather than either field directly.
    env: str | None = None
    app_env: str = "development"

    @property
    def environment(self) -> str:
        """Effective environment label. ENV wins, then APP_ENV, then 'development'.

        Unknown values pass through unchanged rather than raising - this is a label
        only, nothing branches on it, and an unrecognised string is more useful in
        a health check than a crash on boot.
        """
        value = (self.env or self.app_env or "development").strip().lower()
        return value or "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auth0_algorithms_list(self) -> list[str]:
        return [a.strip() for a in self.auth0_algorithms.split(",") if a.strip()]


settings = Settings()
