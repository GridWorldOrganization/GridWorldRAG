"""Lambda URL handler: Cowork (Hirai) → SQS → Akasaka → DynamoDB → response.

Stateless. Uses env vars:
  REQ_QUEUE_URL    SQS queue URL
  RESP_TABLE_NAME  DynamoDB table name
  BASIC_USER       HTTP Basic Auth username
  BASIC_PASS       HTTP Basic Auth password
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
BASIC_USER = os.environ["BASIC_USER"]
BASIC_PASS = os.environ["BASIC_PASS"]
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_MS", "200")) / 1000.0
MAX_WAIT_SEC = int(os.environ.get("MAX_WAIT_SEC", "55"))

EXPECTED_AUTH = "Basic " + base64.b64encode(f"{BASIC_USER}:{BASIC_PASS}".encode()).decode()


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

    # Basic Auth
    auth = headers.get("authorization", "")
    if auth != EXPECTED_AUTH:
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
        # Fire-and-forget: enqueue anyway so Akasaka sees the notification
        try:
            sqs.send_message(
                QueueUrl=REQ_QUEUE_URL,
                MessageBody=json.dumps({
                    "msg_id": str(uuid.uuid4()),
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
