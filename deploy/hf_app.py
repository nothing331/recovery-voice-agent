"""Entry point for a Hugging Face Space (Gradio SDK, which runs `python app.py` on port 7860).

The Space doesn't use Gradio: this starts the same FastAPI app the Dockerfile runs, which serves
the API, the Plivo webhooks/media WebSocket, and the prebuilt dashboard in frontend/dist.
scripts/push_to_hf.sh copies this file to app.py at the Space root.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DB_PATH", "/tmp/recovery.db")  # Space disk is ephemeral; seed data reloads on start

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)),
                proxy_headers=True, forwarded_allow_ips="*")
