"""
Local embedding client using Ollama (nomic-embed-text).
Falls back gracefully if Ollama/model unavailable.
"""

from typing import List, Optional
import requests

OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"


def _ollama_alive() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        return r.ok
    except Exception:
        return False


def _model_available(model: str) -> bool:
    """Return True if the embed model is already available locally.
    We do not trigger pulls here to honor "local-only" usage.
    """
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        if not r.ok:
            return False
        data = r.json() or {}
        models = data.get("models") or []
        names = {m.get("name") for m in models if isinstance(m, dict)}
        return model in names
    except Exception:
        return False


def embed_text(text: str, model: str = EMBED_MODEL) -> Optional[List[float]]:
    try:
        if not _ollama_alive():
            return None
        # Use only locally available model; do not pull
        if not _model_available(model):
            return None
        payload = {"model": model, "input": text}
        r = requests.post(f"{OLLAMA_URL}/api/embeddings", json=payload, timeout=20)
        if not r.ok:
            return None
        out = r.json()
        vec = out.get("embedding") or out.get("data", [{}])[0].get("embedding")
        if isinstance(vec, list):
            return [float(x) for x in vec]
        return None
    except Exception:
        return None


