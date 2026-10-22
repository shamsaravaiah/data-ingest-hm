from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ingest:ingest@localhost:5432/ingest"
    azure_storage_connection_string: str = ""
    azure_blob_container: str = ""
    crawl_delay_seconds: float = 1.0
    max_pages: int = 2000
    max_depth: int = 8
    max_image_bytes: int = 8_000_000
    max_images_per_product: int = 8
    request_timeout_seconds: float = 45.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 CatalogIngest/1.0"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
