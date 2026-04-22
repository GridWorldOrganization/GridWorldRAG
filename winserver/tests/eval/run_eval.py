"""Run the eval suite defined in tests/eval/questions.yml.

Usage:
    python -m tests.eval.run_eval
    python -m tests.eval.run_eval --json
    python -m tests.eval.run_eval --write   # persist result to daemon_config

Each question has:
    query           — natural-language query
    expected_any_of — list of drive_file_id prefixes (the Google Drive file_id,
                      before the _chunk_ / _sheet_ suffix). If any of these
                      appear in the top `top_k` results, the question PASSES.
    top_k           — how many results to consider (default from top_k_default)

Score: fraction passed. Writes `eval_last` to public.daemon_config so the
Web UI can display the score.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db
from src.mcp_server import _embed_query
from src.reranker import rerank as _rerank
from src.config import ENABLE_RERANKER, RERANKER_CANDIDATE_K


QUESTIONS_PATH = _ROOT / "tests" / "eval" / "questions.yml"


def _extract_file_id_prefix(drive_file_id: str) -> str:
    """{file_id}_chunk_N or {file_id}_sheet_{gid}_chunk_N -> {file_id}"""
    if "_chunk_" in drive_file_id:
        head = drive_file_id.split("_chunk_", 1)[0]
    else:
        head = drive_file_id
    if "_sheet_" in head:
        head = head.split("_sheet_", 1)[0]
    return head


def run(write_to_db: bool = False, as_json: bool = False) -> int:
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    questions = cfg.get("questions") or []
    top_k_default = int(cfg.get("top_k_default", 3))

    if not questions:
        msg = "0 questions — add entries to tests/eval/questions.yml"
        if as_json:
            print(json.dumps({"score": None, "note": msg}))
        else:
            print(msg)
        return 0

    conn = db.connect()
    try:
        schemas = db.search_enabled_schemas(conn)
        if not schemas:
            msg = "No drives in search scope — enable some on the MCP tab first"
            if as_json:
                print(json.dumps({"score": None, "note": msg}))
            else:
                print(msg)
            return 0

        results = []
        passed = 0
        t0 = time.time()
        for q in questions:
            qid = q.get("id") or q.get("query", "")[:20]
            query = q["query"]
            expected = set(q.get("expected_any_of") or [])
            top_k = int(q.get("top_k", top_k_default))

            candidate_k = max(top_k,
                              RERANKER_CANDIDATE_K if ENABLE_RERANKER else top_k)
            emb = _embed_query(query)
            rows = db.search_across_schemas(conn, emb, schemas,
                                            n_results=candidate_k)
            # We don't have drive_file_id here (search returns content + meta
            # but not the raw drive_file_id). For eval, we need it: fall back
            # to per-row lookup by source_url + chunk_index? Simpler — extend
            # search_across_schemas later. For now, dedupe by source_url,
            # which is 1:1 with file_id for most cases.
            candidates = []
            seen_urls = set()
            for r in rows:
                # file_id can be extracted from source_url via db helper
                fid = db.extract_file_id_from_url(r.get("source_url") or "") or ""
                candidates.append({**r, "_file_id_prefix": fid})
            reranked = _rerank(query, candidates, top_n=top_k,
                               text_key="content")
            top_file_ids = [r.get("_file_id_prefix", "") for r in reranked]
            hit = bool(expected & set(top_file_ids))
            if hit:
                passed += 1
            results.append({
                "id": qid, "query": query, "pass": hit,
                "expected": sorted(expected),
                "got": top_file_ids,
            })

        score = passed / len(questions)
        duration_ms = int((time.time() - t0) * 1000)
        summary = {
            "score": round(score, 3),
            "passed": passed,
            "total": len(questions),
            "duration_ms": duration_ms,
            "reranked": ENABLE_RERANKER,
            "questions": results,
        }

        if write_to_db:
            db.set_config(conn, "eval_last", json.dumps(summary, ensure_ascii=False))

    finally:
        conn.close()

    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Eval: {passed}/{len(questions)} passed ({score:.1%}) in {duration_ms}ms"
              + (" [reranked]" if ENABLE_RERANKER else ""))
        for r in results:
            marker = "✓" if r["pass"] else "✗"
            print(f"  {marker} {r['id']}: {r['query'][:50]}")
            if not r["pass"]:
                print(f"     expected: {r['expected'][:3]}")
                print(f"     got:      {r['got']}")

    return 0 if passed == len(questions) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit JSON summary")
    ap.add_argument("--write", action="store_true",
                    help="persist result to daemon_config.eval_last")
    args = ap.parse_args()
    sys.exit(run(write_to_db=args.write, as_json=args.json))


if __name__ == "__main__":
    main()
