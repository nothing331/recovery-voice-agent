import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def _no_livekit_from_env(monkeypatch):
    """Tests use the direct WebRTC path unless they configure LiveKit themselves (ignore a local .env)."""
    from app.config import settings

    monkeypatch.setattr(settings, "livekit_url", "")
    monkeypatch.setattr(settings, "dashboard_password", "")  # a local .env may set one for the laptop link
