"""Embedding model (lazy-loaded sentence-transformers)."""
from __future__ import annotations

import threading

from src.config import EMBEDDING_MODEL, EMBEDDING_DEVICE, BATCH_SIZE

_model = None
_lock = threading.Lock()


def _pick_device() -> str:
    if EMBEDDING_DEVICE != "auto":
        return EMBEDDING_DEVICE
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                device = _pick_device()
                _model = SentenceTransformer(EMBEDDING_MODEL, device=device)
    return _model


def embed_batch(texts: list[str]):
    model = get_model()
    return model.encode(texts, batch_size=BATCH_SIZE, show_progress_bar=False,
                        convert_to_numpy=True, normalize_embeddings=False)
