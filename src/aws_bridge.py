"""SQS/DDB bridge for the serverless pipe: AWS Lambda → SQS → this script → DDB.

Long-polls the WinServerRAG request queue on AWS, dispatches JSON-RPC calls to
the local FastMCP tool functions (unwrapped, invoked synchronously), and writes
the response back into the correlation DynamoDB table where the Lambda is
polling for it.

Credentials come from the `winserverrag-bridge` AWS profile (least-privilege
IAM user: sqs recv/delete + ddb put only). Region is ap-northeast-1.

Launch manually in its own PowerShell/Command Prompt window kept open:
    cd C:\\claude_code\\dev\\WinServerRAG
    .venv\\Scripts\\python.exe -m src.aws_bridge

Task Scheduler registration is forbidden per the project policy.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import traceback
from typing import Any, Callable

import boto3

from src import mcp_server
from src.logging_setup import setup_logger

log = setup_logger("aws_bridge")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
AWS_PROFILE   = os.environ.get("WINSERVERRAG_AWS_PROFILE", "winserverrag-bridge")
AWS_REGION    = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1")
REQ_QUEUE_URL = os.environ.get(
    "WINSERVERRAG_REQ_QUEUE_URL",
    "https://sqs.ap-northeast-1.amazonaws.com/375718567574/winserverrag-requests",
)
RESP_TABLE_NAME = os.environ.get("WINSERVERRAG_RESP_TABLE", "winserverrag-responses")
POLL_WAIT_SEC   = int(os.environ.get("WINSERVERRAG_POLL_WAIT", "20"))  # SQS long poll
RESP_TTL_SEC    = int(os.environ.get("WINSERVERRAG_RESP_TTL", "120"))

_stop = False


def _handle_sigterm(*_):
    global _stop
    _stop = True
    log.info("SIGTERM received, draining...")


signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)


# --------------------------------------------------------------------------- #
# Unwrap FastMCP-decorated tools to get the underlying Python callable
# --------------------------------------------------------------------------- #
def _unwrap(fn: Any) -> Callable:
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


TOOLS: dict[str, dict] = {
    "search": {
        "fn":   _unwrap(mcp_server.search),
        "desc": "Semantic search across all MCP-enabled shared drives.",
        "schema": {
            "type": "object",
            "properties": {
                "query":     {"type": "string", "description": "Natural-language query."},
                "n_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "owner":     {"type": ["string", "null"], "description": "Optional email filter."},
            },
            "required": ["query"],
        },
    },
    "list_drives": {
        "fn":   _unwrap(mcp_server.list_drives),
        "desc": "List shared drives currently in MCP search scope.",
        "schema": {"type": "object", "properties": {}, "required": []},
    },
    "lookup": {
        "fn":   _unwrap(mcp_server.lookup),
        "desc": "Fetch a specific chunk or spreadsheet tab by Google Drive URL.",
        "schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "stats": {
        "fn":   _unwrap(mcp_server.stats),
        "desc": "Return database statistics (drives, files, chunks, DB size).",
        "schema": {"type": "object", "properties": {}, "required": []},
    },
}

SERVER_INFO = {
    "name":    "WinServerRAG",
    "version": "0.4",
}


# --------------------------------------------------------------------------- #
# JSON-RPC dispatch
# --------------------------------------------------------------------------- #
def _build_response(rpc_id: Any, result: dict | None = None, error: dict | None = None) -> dict:
    resp = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result if result is not None else {}
    return resp


def _dispatch(method: str, params: dict, rpc_id: Any) -> dict:
    """Return a full JSON-RPC envelope for the given MCP call."""
    if method == "initialize":
        return _build_response(rpc_id, {
            "protocolVersion": params.get("protocolVersion", "2025-03-26"),
            "capabilities":    {"tools": {}},
            "serverInfo":      SERVER_INFO,
        })

    if method == "notifications/initialized":
        # Notifications have no response — Lambda already returned 202.
        return _build_response(rpc_id, {})

    if method == "tools/list":
        tools = [
            {
                "name":        name,
                "description": spec["desc"],
                "inputSchema": spec["schema"],
            }
            for name, spec in TOOLS.items()
        ]
        return _build_response(rpc_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS.get(name)
        if not tool:
            return _build_response(rpc_id, error={
                "code": -32601, "message": f"tool_not_found: {name}",
            })
        # Set ContextVar so the tool's _current_user.get() works.
        token = mcp_server._current_user.set("tobisako")
        try:
            t0 = time.time()
            data = tool["fn"](**args)
            dt_ms = (time.time() - t0) * 1000
            log.info(f"tool={name} dt={dt_ms:.0f}ms")
        except Exception as e:
            log.exception(f"tool={name} failed")
            return _build_response(rpc_id, error={
                "code": -32000, "message": f"{type(e).__name__}: {e}",
            })
        finally:
            mcp_server._current_user.reset(token)
        # MCP expects both `content` (for text display) and `structuredContent`.
        return _build_response(rpc_id, {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
            "structuredContent": data,
        })

    return _build_response(rpc_id, error={"code": -32601, "message": f"method_not_found: {method}"})


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def main():
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    sqs = session.client("sqs")
    ddb = session.resource("dynamodb").Table(RESP_TABLE_NAME)

    log.info(f"AWS bridge started. profile={AWS_PROFILE} region={AWS_REGION}")
    log.info(f"queue={REQ_QUEUE_URL}")
    log.info(f"table={RESP_TABLE_NAME}")

    while not _stop:
        try:
            resp = sqs.receive_message(
                QueueUrl=REQ_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=POLL_WAIT_SEC,
                VisibilityTimeout=60,
            )
        except Exception:
            log.exception("sqs receive failed, retrying in 5s")
            time.sleep(5)
            continue

        for m in resp.get("Messages", []):
            try:
                body = json.loads(m["Body"])
            except Exception:
                log.exception(f"malformed body, dropping: {m.get('MessageId')}")
                _delete(sqs, m)
                continue

            msg_id = body.get("msg_id")
            rpc_id = body.get("rpc_id")
            method = body.get("method")
            params = body.get("params") or {}
            is_notif = bool(body.get("notification"))

            log.info(f"recv msg_id={msg_id} method={method} notif={is_notif}")

            try:
                envelope = _dispatch(method, params, rpc_id)
            except Exception:
                log.exception("dispatch crashed")
                envelope = _build_response(rpc_id, error={
                    "code": -32000, "message": "bridge_internal_error",
                    "data": traceback.format_exc()[-500:],
                })

            if not is_notif:
                try:
                    ddb.put_item(Item={
                        "msg_id": msg_id,
                        "result": json.dumps(envelope, ensure_ascii=False, default=str),
                        "ttl":    int(time.time()) + RESP_TTL_SEC,
                    })
                except Exception:
                    log.exception(f"ddb put_item failed for msg_id={msg_id}")

            _delete(sqs, m)

    log.info("bridge stopped cleanly")


def _delete(sqs, msg):
    try:
        sqs.delete_message(QueueUrl=REQ_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
    except Exception:
        log.exception(f"delete failed for {msg.get('MessageId')}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    sys.exit(0)
