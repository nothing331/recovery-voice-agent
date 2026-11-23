from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    plivo_from_number: str = ""
    public_base_url: str = ""
    plivo_verify_signatures: bool = True
    default_to_number: str = ""
    verified_numbers: Annotated[list[str], NoDecode] = []
    deepgram_api_key: str = ""
    llm_provider: Literal["openrouter", "google"] = "openrouter"
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-2.5-flash"
    google_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    deepgram_tts_voice: str = "aura-2-thalia-en"
    company_name: str = "Kitefibre"
    agent_name: str = "Meera"
    db_path: str = "recovery.db"
    # When set (hosted deploys), everything except /plivo/* needs HTTP Basic auth with this password,
    # from any host. When empty, the local-only tunnel guard applies instead.
    dashboard_password: str = ""
    # Render sets this to the service's https URL; used when PUBLIC_BASE_URL isn't set.
    render_external_url: str = ""
    # TURN relay for browser calls on hosts without inbound UDP (see app/ice.py).
    cloudflare_turn_key_id: str = ""
    cloudflare_turn_api_token: str = ""
    ice_servers: str = ""
    # LiveKit Cloud: when all three are set, "Talk in browser" runs through a LiveKit room
    # (works on hosts without inbound UDP). Otherwise the direct SmallWebRTC path is used.
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # Small hosts (Render free: 0.15 CPU) can't keep up with per-frame DEBUG logging or the
    # Smart Turn end-of-turn model; set LOG_LEVEL=INFO and SMART_TURN=false there.
    log_level: str = "DEBUG"
    smart_turn: bool = True

    @property
    def livekit_enabled(self) -> bool:
        return bool(self.livekit_url and self.livekit_api_key and self.livekit_api_secret)

    @field_validator("verified_numbers", mode="before")
    @classmethod
    def _split_numbers(cls, v):
        if isinstance(v, str):
            return [n.strip() for n in v.split(",") if n.strip()]
        return v

    def model_post_init(self, __context) -> None:
        if not self.public_base_url and self.render_external_url:
            self.public_base_url = self.render_external_url


settings = Settings()
