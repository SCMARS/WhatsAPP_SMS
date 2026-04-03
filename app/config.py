from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    ELEVENLABS_API_KEY: str
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    API_SECRET_KEY: str
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = False

    @property
    def async_database_url(self) -> str:
        """
        Railway provides DATABASE_URL as postgres:// or postgresql://
        SQLAlchemy async requires postgresql+asyncpg://
        """
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
