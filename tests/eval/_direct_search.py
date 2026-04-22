"""Directly call the MCP tool function (bypassing HTTP/SSE) to exercise
the search pipeline against GJ_AI. Simpler / more reliable than going
through Streamable HTTP for a local experiment."""
from __future__ import annotations

import sys, time
from pathlib import Path

# Force stdout to UTF-8 so Japanese titles render correctly in the PowerShell console.
sys.stdout.reconfigure(encoding="utf-8")

# Make `src` importable regardless of cwd
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Set a deterministic user for the ContextVar so the tool can resolve scope.
from src.mcp_server import _current_user, search as mcp_search, list_drives as mcp_list_drives, stats as mcp_stats
_current_user.set("tobisako")


def main():
    queries = sys.argv[1:] if len(sys.argv) > 1 else [
        "Claude Code のスキルについて",
        "Google Workspace MCP の使い方",
        "Playwright ブラウザ操作の注意点",
        "Gemini のプロンプト設計",
        "RAG システム構築",
        "gcloud の設定手順",
        "社外秘ドキュメントの扱い",
    ]

    print("Warming up embedding + reranker models (first call is ~20-30s)...")
    t0 = time.time()
    stats = mcp_stats.__wrapped__() if hasattr(mcp_stats, "__wrapped__") else mcp_stats()
    print(f"  stats: {stats}  ({(time.time()-t0)*1000:.0f} ms)\n")

    drives = mcp_list_drives.__wrapped__() if hasattr(mcp_list_drives, "__wrapped__") else mcp_list_drives()
    print(f"Scope: {drives.get('count')} drives")
    for d in drives.get("drives", []):
        print(f"  {d['drive_id'][:20]}  {d['name']}  files={d['file_count']} chunks={d['chunk_count']}")
    print()

    # Unwrap the @_logged_tool decorator so we can call search directly
    search_fn = mcp_search
    while hasattr(search_fn, "__wrapped__"):
        search_fn = search_fn.__wrapped__

    for i, q in enumerate(queries, 1):
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"[{i}/{len(queries)}] query: {q}")
        t0 = time.time()
        try:
            res = search_fn(q, n_results=5)
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            continue
        dt = (time.time() - t0) * 1000
        results = res.get("results") or []
        print(f"  {dt:.0f} ms, {len(results)} hits{' [reranked]' if res.get('reranked') else ''}")
        print()
        for j, hit in enumerate(results, 1):
            title = hit.get("title") or ""
            fld = hit.get("folder_path") or ""
            url = hit.get("source_url") or ""
            rerank = hit.get("rerank_score")
            dist = hit.get("distance")
            score = f"rerank={rerank:+.3f}" if rerank is not None else f"dist={dist:.3f}"
            print(f"  #{j}  [{score}]  {title}")
            if fld: print(f"        folder: {fld}")
            if url: print(f"        {url}")
            content = (hit.get("content") or "").strip().replace("\n", " ")
            if content:
                snippet = content[:200]
                print(f"        ▶ {snippet}{'...' if len(content) > 200 else ''}")
            print()


if __name__ == "__main__":
    main()
