import os
from pathlib import Path
from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Detect Vercel Serverless environment (filesystem is read-only except /tmp)
IS_VERCEL = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

if IS_VERCEL:
    DEFAULT_DB_URL = "sqlite:////tmp/analytics.db"
    DEFAULT_STORAGE_DIR = Path("/tmp") / "uploads"
else:
    DEFAULT_DB_URL = f"sqlite:///{BASE_DIR / 'data' / 'analytics.db'}"
    DEFAULT_STORAGE_DIR = BASE_DIR / "data" / "uploads"

class Settings(BaseSettings):
    PROJECT_NAME: str = "CSV Analytics Dashboard API"
    VERSION: str = "1.0.0"
    SCHEMA_VERSION: str = "1.0.0"
    
    # Database URL: default SQLite in /tmp for Vercel, or PostgreSQL connection string
    DATABASE_URL: str = os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    
    # Upload storage directory
    STORAGE_DIR: Path = DEFAULT_STORAGE_DIR
    
    # Chunk size for streaming ingestion
    CHUNK_SIZE: int = 5000
    
    # CORS origins
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}

settings = Settings()

try:
    settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
