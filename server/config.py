"""
Configuration — loaded from environment variables or .env file.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL (Cloud SQL)
    PG_HOST:     str = "34.93.217.19"
    PG_PORT:     int = 5432
    PG_DB:       str = "domains"
    PG_USER:     str = "scraper"
    PG_PASSWORD: str = "Custarea@1"

    # OpenAI
    OPENAI_API_KEY: str
    OPENAI_MODEL:   str = "gpt-4o-mini"

    # Query limits
    MAX_RESULTS:          int = 200
    MAX_SQL_RESULTS:      int = 200
    DEFAULT_BATCH_SIZE:   int = 10
    MAX_EXCLUDED_DOMAINS: int = 2000   # cap excluded list to prevent huge queries

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
