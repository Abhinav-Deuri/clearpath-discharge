"""Environment-driven settings for the ClearPath sidecar."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # AI endpoint (OpenAI-compatible)
    ai_api_key: str = ""
    ai_base_url: str = ""
    ai_model_name: str = ""
    ai_timeout_seconds: float = 30.0
    use_mock_llm: bool = True

    # Optional multimodal endpoints (OpenAI-compatible). Unset -> deterministic demo fallback.
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model_name: str = ""
    asr_riva_server: str = "grpc.nvcf.nvidia.com:443"
    asr_riva_function_id: str = ""
    asr_language_code: str = "en-US"
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model_name: str = ""

    # SQLite
    sqlite_path: str = "clearpath_state.db"

    # Odoo JSON-RPC
    odoo_url: str = "http://localhost:8069"
    odoo_db: str = "clearpath"
    odoo_user: str = "admin"
    odoo_password: str = "admin"


settings = Settings()
