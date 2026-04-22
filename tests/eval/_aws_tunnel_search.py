"""E2E test: Hirai Cowork → AWS Lambda URL → SQS → Akasaka aws_bridge → DDB.

Simulates what Cowork does when configured against the serverless pipe.
"""
from __future__ import annotations

import base64
import json
import sys
import time

import requests

URL = "https://REDACTED-API-GW-HOST/"
AUTH = "Basic " + base64.b64encode(b"***:***").decode()

sys.stdout.reconfigure(encoding="utf-8")


def _rpc(method: str, params: dict, id_: int):
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
    t0 = time.time()
    r = requests.post(
        URL,
        headers={"Authorization": AUTH, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    dt_ms = (time.time() - t0) * 1000
    r.raise_for_status()
    return r.json(), dt_ms


def call_tool(name: str, args: dict, id_: int):
    env, dt = _rpc("tools/call", {"name": name, "arguments": args}, id_=id_)
    if "error" in env:
        raise RuntimeError(env["error"])
    r = env["result"]
    if "structuredContent" in r:
        return r["structuredContent"], dt
    return r, dt


def main():
    queries = sys.argv[1:] if len(sys.argv) > 1 else [
        "Claude Code のスキルについて",
        "Google Workspace MCP の使い方",
    ]

    print(f"TUNNEL: {URL}")
    print("[1] initialize ...", end=" ", flush=True)
    init, dt = _rpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "aws-sim", "version": "1.0"},
    }, id_=1)
    print(f"{dt:.0f} ms")
    print(f"    server: {init.get('result', {}).get('serverInfo')}")

    print("[2] tools/list ...", end=" ", flush=True)
    tl, dt = _rpc("tools/list", {}, id_=2)
    tools = tl.get("result", {}).get("tools", [])
    print(f"{dt:.0f} ms, {len(tools)} tools: {[t['name'] for t in tools]}")

    print("[3] list_drives ...", end=" ", flush=True)
    try:
        drv, dt = call_tool("list_drives", {}, id_=3)
        print(f"{dt:.0f} ms, {drv.get('count')} drives in scope")
        for d in drv.get("drives", []):
            print(f"    {d['name']:30} files={d['file_count']:>5} chunks={d['chunk_count']:>6}")
    except Exception as e:
        print(f"FAIL: {e}")

    for i, q in enumerate(queries, 1):
        print(f"\n[4.{i}] search({q!r}) ...")
        try:
            res, dt = call_tool("search", {"query": q, "n_results": 3}, id_=10 + i)
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")
            continue
        print(f"    {dt:.0f} ms, {len(res.get('results') or [])} hits"
              f"{' [reranked]' if res.get('reranked') else ''}")
        for j, hit in enumerate(res.get("results") or [], 1):
            rerank = hit.get("rerank_score")
            score = f"rerank={rerank:+.3f}" if rerank is not None else f"dist={hit.get('distance'):.3f}"
            print(f"    #{j} [{score}] {hit.get('title')}")
            snippet = (hit.get("content") or "").strip().replace("\n", " ")[:140]
            if snippet:
                print(f"        > {snippet}")


if __name__ == "__main__":
    main()
