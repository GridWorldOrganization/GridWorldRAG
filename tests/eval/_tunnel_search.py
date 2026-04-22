"""End-to-end test: simulate the Cowork-at-Hirai flow.

Flow tested:
    this script  →  cloudflared tunnel (Akasaka PC's public URL)
                 →  WinServerRAG MCP server (localhost at Akasaka)
                 →  daemon DB → embedding → reranker → results
                 →  SSE back through tunnel
                 →  this script prints the results

Auth: HTTP Basic Auth (***:***).
"""
from __future__ import annotations

import base64
import json
import sys
import time

import requests

# The cloudflared public URL — same endpoint Cowork on the Hirai PC uses.
URL = "https://bread-before-gray-tiles.trycloudflare.com/mcp/"
AUTH = "Basic " + base64.b64encode(b"***:***").decode()

sys.stdout.reconfigure(encoding="utf-8")

_session_id = None


def _parse_sse(text: str):
    """Parse the JSON payload out of an SSE-framed response.

    Uses regex to extract the content between `data: ` and the terminating
    `\\r\\n\\r\\n` (or `\\n\\n`) rather than `splitlines()`, which may split
    on embedded Unicode newline-like characters inside JSON string literals.
    """
    import re
    # Match `data: <payload>` followed by a blank line (end of event)
    m = re.search(r'(?:^|\r?\n)data:\s?(.*?)(?:\r?\n){2,}', text, re.DOTALL)
    if not m:
        # Fallback: grab from first `data:` to end-of-text
        m = re.search(r'(?:^|\r?\n)data:\s?(.*)$', text, re.DOTALL)
        if not m:
            raise ValueError("no data: field in SSE response\n" + text[:300])
    payload = m.group(1)
    # If payload contains literal `\r\ndata: ` continuation (multi-line event),
    # join those continuations with "\n" per SSE spec.
    payload = re.sub(r'\r?\ndata:\s?', '\n', payload)
    return json.loads(payload)


def _headers():
    h = {
        "Authorization": AUTH,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _session_id:
        h["Mcp-Session-Id"] = _session_id
    return h


def _rpc(method: str, params: dict, id_: int):
    global _session_id
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
    t0 = time.time()
    r = requests.post(URL, headers=_headers(), json=body, timeout=120)
    dt_ms = (time.time() - t0) * 1000
    r.raise_for_status()
    sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
    if sid and not _session_id:
        _session_id = sid
    try:
        parsed = _parse_sse(r.text)
    except Exception as e:
        print(f"\n  [DEBUG] content-length={len(r.text)} chars, first 400 bytes:")
        print("  " + repr(r.text[:400]))
        print(f"  [DEBUG] last 200 bytes:")
        print("  " + repr(r.text[-200:]))
        raise
    return parsed, dt_ms


def _notify(method: str, params: dict | None = None):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    requests.post(URL, headers=_headers(), json=body, timeout=30)


def call_tool(name: str, args: dict, id_: int):
    res, dt = _rpc("tools/call", {"name": name, "arguments": args}, id_=id_)
    if "error" in res:
        raise RuntimeError(res["error"])
    r = res["result"]
    if "structuredContent" in r:
        return r["structuredContent"], dt
    for item in r.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item["text"]), dt
            except Exception:
                return {"_raw": item["text"]}, dt
    return r, dt


def main():
    queries = sys.argv[1:] if len(sys.argv) > 1 else [
        "Claude Code のスキルについて",
        "Google Workspace MCP の使い方",
        "Playwright ブラウザ操作の注意点",
    ]

    print(f"▶ TUNNEL URL: {URL}")
    print(f"▶ Simulating Cowork at Hirai PC connecting to this Akasaka PC\n")

    print("┃ initialize ...", end=" ", flush=True)
    t0 = time.time()
    init, _ = _rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "hirai-sim", "version": "1.0"},
    }, id_=1)
    print(f"{(time.time()-t0)*1000:.0f} ms")
    print(f"┃   server: {init.get('result',{}).get('serverInfo')}")
    print(f"┃   session: {_session_id}")
    _notify("notifications/initialized")
    print()

    print("┃ tools/call list_drives ...")
    try:
        drv, dt = call_tool("list_drives", {}, id_=2)
        print(f"┃   {dt:.0f} ms, {drv.get('count')} drives in MCP scope")
        for d in drv.get("drives", []):
            print(f"┃     {d['name']:30}  files={d['file_count']:>5} chunks={d['chunk_count']:>6}")
    except Exception as e:
        print(f"┃   FAIL: {e}")
    print()

    for i, q in enumerate(queries, 1):
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"[{i}/{len(queries)}] 「{q}」")
        try:
            res, dt = call_tool("search", {"query": q, "n_results": 3}, id_=10+i)
        except Exception as e:
            print(f"  FAIL: {type(e).__name__}: {e}")
            continue
        print(f"  round-trip: {dt:.0f} ms (tunnel + daemon + DB + rerank + tunnel back)")
        rs = res.get("results") or []
        print(f"  {len(rs)} hits{' [reranked]' if res.get('reranked') else ''}\n")
        for j, hit in enumerate(rs, 1):
            rerank = hit.get("rerank_score")
            score = f"rerank={rerank:+.3f}" if rerank is not None else f"dist={hit.get('distance'):.3f}"
            title = hit.get("title") or ""
            folder = hit.get("folder_path") or ""
            content = (hit.get("content") or "").strip().replace("\n", " ")
            print(f"  #{j}  [{score}]  {title}")
            if folder: print(f"        folder: {folder}")
            if content: print(f"        ▶ {content[:180]}")
            print()


if __name__ == "__main__":
    main()
