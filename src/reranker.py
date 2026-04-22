"""Cross-encoder reranker (BAAI/bge-reranker-v2-m3 by default).

Used by src.mcp_server.search to rerank the top-K candidates returned from
cosine-vector search. Dramatically improves result quality for
semantically-similar-but-different queries vs the mpnet embedding alone.

Disabled via ENABLE_RERANKER=0 in config/config.v2.env. Model is lazily
loaded on first reranked call (~500MB, first-call latency ~10-20s).
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from src.config import ENABLE_RERANKER, RERANKER_MODEL, EMBEDDING_DEVICE

log = logging.getLogger("reranker")

_model = None
_lock = threading.Lock()


def _get_reranker():
    global _model
    if not ENABLE_RERANKER:
        return None
    if _model is not None:
        return _model
    with _lock:
        if _model is None:
            try:
                from sentence_transformers import CrossEncoder
                import torch
                dev = EMBEDDING_DEVICE
                if dev == "auto":
                    dev = "cuda" if torch.cuda.is_available() else "cpu"
                log.info("loading reranker %s on %s...", RERANKER_MODEL, dev)
                _model = CrossEncoder(RERANKER_MODEL, device=dev, max_length=512)
            except Exception as e:
                log.error("reranker load failed, falling back to no-rerank: %s", e)
                _model = False  # sentinel: tried and failed
    return _model if _model is not False else None


def rerank(query: str, candidates: list[dict], top_n: int,
           text_key: str = "content") -> list[dict]:
    """Rerank candidates by the cross-encoder score. Returns top_n items.

    If the reranker is disabled or failed to load, returns candidates[:top_n]
    unchanged (behaving like a pass-through).
    """
    if top_n <= 0 or not candidates:
        return []
    model = _get_reranker()
    if model is None:
        return candidates[:top_n]
    pairs = [(query, (c.get(text_key) or "")[:2000]) for c in candidates]
    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as e:
        log.warning("rerank predict failed: %s", e)
        return candidates[:top_n]
    scored = sorted(zip(candidates, scores), key=lambda x: float(x[1]), reverse=True)
    out = []
    for cand, score in scored[:top_n]:
        cand = dict(cand)
        cand["rerank_score"] = float(score)
        out.append(cand)
    return out
