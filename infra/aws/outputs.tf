output "mcp_endpoint" {
  description = "Public MCP endpoint for Cowork (API Gateway HTTP API)"
  value       = aws_apigatewayv2_api.mcp.api_endpoint
}

output "request_queue_url" {
  value = aws_sqs_queue.requests.url
}

output "response_table_name" {
  value = aws_dynamodb_table.responses.name
}

output "bridge_access_key_id" {
  value     = aws_iam_access_key.bridge.id
  sensitive = true
}

output "bridge_secret_access_key" {
  value     = aws_iam_access_key.bridge.secret
  sensitive = true
}
