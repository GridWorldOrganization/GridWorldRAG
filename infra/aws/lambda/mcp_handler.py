"""Lambda URL handler: Cowork (Hirai) → SQS → Akasaka → DynamoDB → response.

Stateless. Uses env vars:
  REQ_QUEUE_URL    SQS queue URL
  RESP_TABLE_NAME  DynamoDB table name
  BASIC_USERS      "user1:pw1,user2:pw2,..." — MCP accounts this Lambda
                   accepts. Any matched username is forwarded to the
                   Akasaka bridge so the daemon's per-user scope kicks
                   in. Falls back to BASIC_USER/BASIC_PASS (legacy
                   single-user mode) when BASIC_USERS is not set.
  BASIC_USER       Legacy single-user username (used if BASIC_USERS empty)
  BASIC_PASS       Legacy single-user password
  POLL_INTERVAL_MS DynamoDB poll interval in ms (default 200)
  MAX_WAIT_SEC     Max wait for response (default 55)
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid

import boto3

sqs = boto3.client("sqs")
ddb = boto3.resource("dynamodb").Table(os.environ["RESP_TABLE_NAME"])

REQ_QUEUE_URL = os.environ["REQ_QUEUE_URL"]
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_MS", "200")) / 1000.0
MAX_WAIT_SEC = int(os.environ.get("MAX_WAIT_SEC", "55"))


def _build_auth_map() -> dict[str, str]:
    """Parse BASIC_USERS into {expected_auth_header_value: username}. We
    index by full header value ("Basic <base64>") so the comparison stays
    constant-time-ish and no password ever leaves this map."""
    raw = os.environ.get("BASIC_USERS", "").strip()
    pairs: list[tuple[str, str]] = []
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            u, _, p = item.partition(":")
            u, p = u.strip(), p.strip()
            if u and p:
                pairs.append((u, p))
    else:
        u = os.environ.get("BASIC_USER", "").strip()
        p = os.environ.get("BASIC_PASS", "").strip()
        if u and p:
            pairs.append((u, p))

    out: dict[str, str] = {}
    for u, p in pairs:
        header = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
        out[header] = u
    return out


AUTH_MAP = _build_auth_map()


def _unauthorized():
    return {
        "statusCode": 401,
        "headers": {
            "WWW-Authenticate": 'Basic realm="WinServerRAG"',
            "Content-Type": "application/json",
        },
        "body": json.dumps({"error": "unauthorized"}),
    }


def _json_response(code: int, body: dict, extra_headers: dict | None = None):
    h = {"Content-Type": "application/json"}
    if extra_headers:
        h.update(extra_headers)
    return {"statusCode": code, "headers": h, "body": json.dumps(body)}


def handler(event, _context):
    # Lambda URL event has 'headers' (lowercased by runtime) and 'body' / 'isBase64Encoded'
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    # CORS preflight (Cowork clients may send OPTIONS)
    if method == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "authorization, content-type, mcp-session-id, accept",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
            },
            "body": "",
        }

    if method != "POST":
        return _json_response(405, {"error": "method_not_allowed"})

    # Basic Auth — multiple credentials allowed. Match the incoming header
    # against the precomputed map and remember which MCP user it
    # represents so the daemon can apply their per-user scope.
    auth = headers.get("authorization", "")
    username = AUTH_MAP.get(auth)
    if not username:
        return _unauthorized()

    # Body
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except Exception as e:
        return _json_response(400, {"error": f"invalid_json: {e}"})

    # MCP over JSON-RPC 2.0
    rpc_id = body.get("id")
    rpc_method = body.get("method")
    rpc_params = body.get("params") or {}

    # Notifications (no id) — ack and drop
    if rpc_id is None:
        try:
            sqs.send_message(
                QueueUrl=REQ_QUEUE_URL,
                MessageBody=json.dumps({
                    "msg_id": str(uuid.uuid4()),
                    "username": username,
                    "method": rpc_method,
                    "params": rpc_params,
                    "notification": True,
                }),
            )
        except Exception:
            pass
        return {"statusCode": 202, "headers": {"Content-Type": "application/json"}, "body": ""}

    msg_id = str(uuid.uuid4())
    sqs.send_message(
        QueueUrl=REQ_QUEUE_URL,
        MessageBody=json.dumps({
            "msg_id": msg_id,
            "rpc_id": rpc_id,
            "username": username,
            "method": rpc_method,
            "params": rpc_params,
        }),
    )

    # Poll DynamoDB for the response
    deadline = time.time() + MAX_WAIT_SEC
    while time.time() < deadline:
        item = ddb.get_item(Key={"msg_id": msg_id}).get("Item")
        if item:
            ddb.delete_item(Key={"msg_id": msg_id})
            try:
                payload = json.loads(item["result"])
            except Exception:
                payload = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32000, "message": "bad_bridge_payload"}}
            return _json_response(200, payload, extra_headers={"Access-Control-Allow-Origin": "*"})
        time.sleep(POLL_INTERVAL_S)

    return _json_response(504, {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32001, "message": "bridge_timeout", "data": {"msg_id": msg_id}},
    })
