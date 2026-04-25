terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
}

# ---------------------------------------------------------------
# Shared Basic Auth secret (plaintext in tfvars — sandbox only)
# ---------------------------------------------------------------
locals {
  project = "winserverrag"
}

# ---------------------------------------------------------------
# SQS: request queue (Hirai → Akasaka)
# ---------------------------------------------------------------
resource "aws_sqs_queue" "requests" {
  name                      = "${local.project}-requests"
  visibility_timeout_seconds = 60
  message_retention_seconds = 600
  receive_wait_time_seconds = 20  # long polling
  # Enable AWS-managed SSE so messages (MCP request bodies) are encrypted
  # at rest even for the 10-minute retention window. Trivy AWS-0096 HIGH.
  sqs_managed_sse_enabled   = true
}

# ---------------------------------------------------------------
# DynamoDB: response correlation (Akasaka → Hirai via Lambda poll)
# ---------------------------------------------------------------
resource "aws_dynamodb_table" "responses" {
  name         = "${local.project}-responses"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "msg_id"

  attribute {
    name = "msg_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# ---------------------------------------------------------------
# IAM: Lambda execution role
# ---------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.project}-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_policy" {
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:*:*:*"]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.requests.arn]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.responses.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${local.project}-lambda-policy"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_policy.json
}

# ---------------------------------------------------------------
# Lambda: mcp_handler
# ---------------------------------------------------------------
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/build/mcp_handler.zip"
}

resource "aws_lambda_function" "mcp_handler" {
  function_name    = "${local.project}-mcp-handler"
  role             = aws_iam_role.lambda.arn
  handler          = "mcp_handler.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      REQ_QUEUE_URL   = aws_sqs_queue.requests.url
      RESP_TABLE_NAME = aws_dynamodb_table.responses.name
      # Single-user legacy pair (kept for rollback compatibility when
      # basic_users is not supplied).
      BASIC_USER      = var.basic_user
      BASIC_PASS      = var.basic_pass
      # Multi-user list consumed by mcp_handler._build_auth_map.
      # Format: "user1:pw1,user2:pw2". Empty string disables multi-user
      # mode and falls back to BASIC_USER/BASIC_PASS.
      BASIC_USERS     = join(",", [for u, p in var.basic_users : "${u}:${p}"])
      POLL_INTERVAL_MS = "200"
      MAX_WAIT_SEC    = "28"
    }
  }
}

# ---------------------------------------------------------------
# Lambda Function URL (public endpoint)
# ---------------------------------------------------------------
# ---------------------------------------------------------------
# API Gateway HTTP API (v2) — public endpoint with Lambda integration
# ---------------------------------------------------------------
resource "aws_apigatewayv2_api" "mcp" {
  name          = "${local.project}-mcp"
  protocol_type = "HTTP"
  target        = aws_lambda_function.mcp_handler.arn
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp_handler.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.mcp.execution_arn}/*/*"
}

# ---------------------------------------------------------------
# IAM: Akasaka bridge user (least-privilege)
# ---------------------------------------------------------------
resource "aws_iam_user" "bridge" {
  name = "${local.project}-bridge"
}

data "aws_iam_policy_document" "bridge_policy" {
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.requests.arn]
  }
  statement {
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.responses.arn]
  }
}

resource "aws_iam_user_policy" "bridge" {
  name   = "${local.project}-bridge-policy"
  user   = aws_iam_user.bridge.name
  policy = data.aws_iam_policy_document.bridge_policy.json
}

resource "aws_iam_access_key" "bridge" {
  user = aws_iam_user.bridge.name
}
