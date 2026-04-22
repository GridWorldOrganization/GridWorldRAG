"""Manual RAG search test against the running local MCP server.

Uses Streamable HTTP over localhost:17600 with Basic Auth (***:***),
not via any SDK — plain requests + SSE parsing. Prints top-N results for
each query, cleanly formatted.
"""
from __future__ import annotations

import base64
import json
import sys
import time

import requests

URL = "http://127.0.0.1:17600/mcp/"
AUTH = "Basic " + base64.b64encode(b"***:***").decode()

_session_id = None


def _parse_sse(text: str):
    """Extract the JSON payload from a Streamable HTTP / SSE response body."""
    data_lines = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    if not data_lines:
        raise ValueError("no data: line in response\n" + text[:400])
    return json.loads("\n".join(data_lines))


def _rpc(method: str, params: dict, id_: int = 1):
    """Make a JSON-RPC call, handle Mcp-Session-Id cookie propagation."""
    global _session_id
    headers = {
        "Authorization": AUTH,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
    r = requests.post(URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    sid = r.headers.get("Mcp-Session-Id")
    if sid:
        _session_id = sid
    return _parse_sse(r.text)


def _notify(method: str, params: dict | None = None):
    """Send a JSON-RPC notification (no id, no response expected)."""
    headers = {
        "Authorization": AUTH,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    requests.post(URL, headers=headers, json=body, timeout=30)


def initialize():
    res = _rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "manual_search", "version": "1.0"},
    }, id_=1)
    # Spec requires notifications/initialized AFTER initialize
    _notify("notifications/initialized")
    return res


def call_tool(name: str, args: dict, id_: int = 2):
    res = _rpc("tools/call", {"name": name, "arguments": args}, id_=id_)
    if "error" in res:
        raise RuntimeError(res["error"])
    # Tool result comes wrapped: { result: { content: [{type,text}], structuredContent: {...} } }
    r = res["result"]
    if "structuredContent" in r:
        return r["structuredContent"]
    # Fallback: parse first text content as JSON
    for item in r.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except Exception:
                return {"_raw": item["text"]}
    return r


def main():
    queries = sys.argv[1:] if len(sys.argv) > 1 else [
        "Claude Code のスキルについて",
        "Google Workspace MCP の使い方",
        "Playwright ブラウザ操作の注意点",
        "Gemini のプロンプト設計",
        "RAG システム構築",
    ]

    print(f"URL:  {URL}")
    print("init ...", end=" ", flush=True)
    t0 = time.time()
    r = initialize()
    print(f"OK ({(time.time()-t0)*1000:.0f} ms)")
    print(f"  server: {r.get('result',{}).get('serverInfo')}")
    print(f"  session: {_session_id}")
    print()

    print("list_drives ...")
    t0 = time.time()
    ld = call_tool("list_drives", {}, id_=2)
    print(f"  {(time.time()-t0)*1000:.0f} ms, {ld.get('count')} drives:")
    for d in ld.get("drives", []):
        print(f"    {d['drive_id'][:20]}  {d['name']:40}  files={d['file_count']:>5} chunks={d['chunk_count']:>6}")
    print()

    for i, q in enumerate(queries, 1):
        print(f"━━━ [{i}/{len(queries)}] query: {q} ━━━")
        t0 = time.time()
        try:
            res = call_tool("search", {"query": q, "n_results": 5}, id_=10 + i)
        except Exception as e:
            print(f"  FAIL: {e}")
            continue
        dt = (time.time() - t0) * 1000
        results = res.get("results") or []
        print(f"  {dt:.0f} ms, {len(results)} hits{' (reranked)' if res.get('reranked') else ''}")
        for j, hit in enumerate(results, 1):
            title = hit.get("title", "")
            fld = hit.get("folder_path", "") or ""
            url = hit.get("source_url", "")
            rerank = hit.get("rerank_score")
            dist = hit.get("distance")
            score = f"rerank={rerank:.2f}" if rerank is not None else f"dist={dist:.3f}"
            print(f"  #{j} [{score}] {title}")
            if fld:
                print(f"       {fld}")
            if url:
                print(f"       {url[:100]}")
            content = (hit.get("content") or "").strip().replace("\n", " ")
            if content:
                print(f"       → {content[:180]}")
        print()


if __name__ == "__main__":
    main()
