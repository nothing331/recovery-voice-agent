from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    plivo_from_number: str = ""
    public_base_url: str = ""
    default_to_number: str = ""
    verified_numbers: Annotated[list[str], NoDecode] = []
    deepgram_api_key: str = ""
    google_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    deepgram_tts_voice: str = "aura-2-thalia-en"
    company_name: str = "Kitefibre"
    agent_name: str = "Meera"
    db_path: str = "recovery.db"

    @field_validator("verified_numbers", mode="before")
    @classmethod
    def _split_numbers(cls, v):
        if isinstance(v, str):
            return [n.strip() for n in v.split(",") if n.strip()]
        return v


settings = Settings()
