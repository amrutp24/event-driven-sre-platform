# Alert webhook: REST API, POST /webhook -> alert_ingest Lambda.
#
# A REST API rather than an HTTP API because it's the one that supports request
# body validation against a JSON schema, API keys and usage plans.

# webhook_private makes it reachable only through the VPC's execute-api
# endpoint. Datadog can't reach it then, so that's for Alertmanager-only setups.
#
# A TLS 1.2 minimum on the default endpoint (security_policy) needs AWS
# provider 6.x, and the EKS module (~> 20) holds the provider below 6.0.
resource "aws_api_gateway_rest_api" "alerts_api" {
  name        = "${var.name}-alerts-api"
  description = "Alert webhook for Alertmanager and Datadog"

  endpoint_configuration {
    types            = [var.webhook_private ? "PRIVATE" : "REGIONAL"]
    vpc_endpoint_ids = var.webhook_private ? [local.webhook_vpc_endpoint_id] : null
  }

  tags = var.tags
}

# A private API needs a resource policy before it can be deployed. This one
# only lets requests in through the VPC endpoint.
resource "aws_api_gateway_rest_api_policy" "private_only" {
  count = var.webhook_private ? 1 : 0

  rest_api_id = aws_api_gateway_rest_api.alerts_api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.alerts_api.execution_arn}/*"
      },
      {
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.alerts_api.execution_arn}/*"
        Condition = {
          StringNotEquals = { "aws:SourceVpce" = local.webhook_vpc_endpoint_id }
        }
      }
    ]
  })
}

resource "aws_api_gateway_resource" "webhook" {
  rest_api_id = aws_api_gateway_rest_api.alerts_api.id
  parent_id   = aws_api_gateway_rest_api.alerts_api.root_resource_id
  path_part   = "webhook"
}

resource "aws_api_gateway_model" "alert_payload" {
  rest_api_id  = aws_api_gateway_rest_api.alerts_api.id
  name         = "AlertPayload"
  description  = "Alertmanager or Datadog webhook payload"
  content_type = "application/json"
  schema       = file("${path.module}/api/alert_payload.schema.json")
}

resource "aws_api_gateway_request_validator" "body" {
  rest_api_id           = aws_api_gateway_rest_api.alerts_api.id
  name                  = "validate-body"
  validate_request_body = true
}

resource "aws_api_gateway_method" "webhook_post" {
  rest_api_id          = aws_api_gateway_rest_api.alerts_api.id
  resource_id          = aws_api_gateway_resource.webhook.id
  http_method          = "POST"
  authorization        = "NONE"
  api_key_required     = var.webhook_require_api_key
  request_validator_id = aws_api_gateway_request_validator.body.id
  request_models = {
    "application/json" = aws_api_gateway_model.alert_payload.name
  }
}

resource "aws_api_gateway_integration" "webhook_lambda" {
  rest_api_id             = aws_api_gateway_rest_api.alerts_api.id
  resource_id             = aws_api_gateway_resource.webhook.id
  http_method             = aws_api_gateway_method.webhook_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.alert_ingest.invoke_arn
}

# Errors API Gateway returns itself use the same JSON shape as alert_ingest's
# (docs/API.md). The schema validation message isn't included because it can
# contain quotes that would break the JSON.
locals {
  gateway_errors = {
    BAD_REQUEST_BODY = {
      status  = "400"
      code    = "INVALID_PAYLOAD"
      message = "Request body doesn't match the alert payload schema"
    }
    INVALID_API_KEY = {
      status  = "403"
      code    = "INVALID_API_KEY"
      message = "Missing or invalid X-API-Key header"
    }
    THROTTLED = {
      status  = "429"
      code    = "RATE_LIMIT_EXCEEDED"
      message = "Too many requests in a short time"
    }
    INTEGRATION_TIMEOUT = {
      status  = "504"
      code    = "TIMEOUT"
      message = "alert_ingest didn't respond in time"
    }
  }
}

resource "aws_api_gateway_gateway_response" "errors" {
  for_each = local.gateway_errors

  rest_api_id   = aws_api_gateway_rest_api.alerts_api.id
  response_type = each.key
  status_code   = each.value.status

  response_templates = {
    "application/json" = jsonencode({
      status     = "error"
      message    = each.value.message
      error_code = each.value.code
      details    = { request_id = "$context.requestId" }
    })
  }
}

resource "aws_api_gateway_deployment" "alerts_api" {
  rest_api_id = aws_api_gateway_rest_api.alerts_api.id

  # Redeploy whenever anything that shapes the API changes. A resource policy
  # change only takes effect after a redeploy.
  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_rest_api.alerts_api.endpoint_configuration,
      aws_api_gateway_rest_api_policy.private_only,
      aws_api_gateway_resource.webhook,
      aws_api_gateway_method.webhook_post,
      aws_api_gateway_integration.webhook_lambda,
      aws_api_gateway_model.alert_payload,
      aws_api_gateway_request_validator.body,
      aws_api_gateway_gateway_response.errors,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.alerts_api.id
  deployment_id = aws_api_gateway_deployment.alerts_api.id
  stage_name    = "prod"
  tags          = var.tags
}

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.alerts_api.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = var.webhook_rate_limit
    throttling_burst_limit = var.webhook_burst_limit
    metrics_enabled        = true
  }
}

# One key per sender, so either can be rotated or revoked on its own
resource "aws_api_gateway_api_key" "webhook" {
  for_each = var.webhook_require_api_key ? toset(["alertmanager", "datadog"]) : toset([])

  name        = "${var.name}-${each.key}"
  description = "Sends alerts to POST /webhook"
  tags        = var.tags
}

resource "aws_api_gateway_usage_plan" "webhook" {
  count = var.webhook_require_api_key ? 1 : 0

  name = "${var.name}-webhook"

  api_stages {
    api_id = aws_api_gateway_rest_api.alerts_api.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }

  throttle_settings {
    rate_limit  = var.webhook_rate_limit
    burst_limit = var.webhook_burst_limit
  }

  tags = var.tags
}

resource "aws_api_gateway_usage_plan_key" "webhook" {
  for_each = aws_api_gateway_api_key.webhook

  key_id        = each.value.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.webhook[0].id
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alert_ingest.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.alerts_api.execution_arn}/*/POST/webhook"
}
