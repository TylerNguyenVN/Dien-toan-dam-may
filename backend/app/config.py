import os
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent

class Settings(BaseSettings):
    PROJECT_NAME: str = "CSV Analytics Dashboard API"
    VERSION: str = "1.0.0"
    SCHEMA_VERSION: str = "1.0.0"
    
    # Database URL: default SQLite for local zero-dependency testing, or PostgreSQL connection string
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'analytics.db'}")
    
    # Upload storage directory
    STORAGE_DIR: Path = BASE_DIR / "data" / "uploads"
    
    # Chunk size for streaming ingestion
    CHUNK_SIZE: int = 5000
    
    # CORS origins
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}

settings = Settings()
settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
