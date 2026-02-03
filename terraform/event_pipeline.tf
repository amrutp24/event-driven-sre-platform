resource "aws_dynamodb_table" "incidents" {
  name         = "${var.name}-incidents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "incident_id"
  attribute {
    name = "incident_id"
    type = "S"
  }
}

resource "aws_sns_topic" "alerts" {
  name = "${var.name}-alerts"
}

resource "aws_cloudwatch_event_bus" "sre" {
  name = "${var.name}-bus"
}

resource "aws_iam_role" "lambda_role" {
  name = "${var.name}-alert-ingest-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "lambda.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  role = aws_iam_role.lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = [
          "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"
        ],
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name}-alert-ingest:*"
      },
      {
        Effect   = "Allow",
        Action   = ["events:PutEvents"],
        Resource = aws_cloudwatch_event_bus.sre.arn
      },
      {
        Effect   = "Allow",
        Action   = ["dynamodb:PutItem"],
        Resource = aws_dynamodb_table.incidents.arn
      },
      {
        Effect   = "Allow",
        Action   = ["sns:Publish"],
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Effect   = "Allow",
        Action   = ["states:StartExecution"],
        Resource = aws_sfn_state_machine.runbook.arn
      }
    ]
  })
}

resource "aws_iam_role" "sfn_role" {
  name = "${var.name}-sfn-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "states.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sfn_policy" {
  role = aws_iam_role.sfn_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        # Step Functions log delivery requires broad permissions to manage CloudWatch Logs
        # These actions don't support resource-level permissions per AWS documentation
        Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
          "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
        "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"],
        Resource = "*"
      }
    ]
  })
}

resource "aws_sfn_state_machine" "runbook" {
  name       = "${var.name}-runbook"
  role_arn   = aws_iam_role.sfn_role.arn
  definition = file("${path.module}/stepfunctions/runbook.asl.json")
}

resource "aws_lambda_function" "alert_ingest" {
  function_name = "${var.name}-alert-ingest"
  role          = aws_iam_role.lambda_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  filename      = "${path.module}/lambda/alert_ingest.zip"
  timeout       = 15

  environment {
    variables = {
      EVENT_BUS_NAME     = aws_cloudwatch_event_bus.sre.name
      INCIDENT_TABLE     = aws_dynamodb_table.incidents.name
      RUNBOOK_ARN        = aws_sfn_state_machine.runbook.arn
      SNS_TOPIC_ARN      = aws_sns_topic.alerts.arn
      CLUSTER_NAME       = var.eks_cluster_name
      REGION             = var.aws_region
      DEGRADED_PARAM     = "/checkout/degraded_mode"
      RUNBOOK_ACTION_ARN = aws_lambda_function.runbook_action.arn
    }
  }
}

resource "aws_apigatewayv2_api" "alerts_api" {
  name          = "${var.name}-alerts-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id           = aws_apigatewayv2_api.alerts_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.alert_ingest.invoke_arn
}

resource "aws_apigatewayv2_route" "alert" {
  api_id    = aws_apigatewayv2_api.alerts_api.id
  route_key = "POST /alert"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.alerts_api.id
  name        = "prod"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alert_ingest.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.alerts_api.execution_arn}/*/*"
}

resource "aws_ssm_parameter" "checkout_degraded_mode" {
  name  = "/checkout/degraded_mode"
  type  = "String"
  value = "false"
}

# Runbook action Lambda: performs remediation against EKS and/or SSM
resource "aws_iam_role" "runbook_lambda_role" {
  name = "${var.name}-runbook-action-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Principal = { Service = "lambda.amazonaws.com" },
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "runbook_lambda_policy" {
  role = aws_iam_role.runbook_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name}-runbook-action:*"
      },
      {
        Effect   = "Allow",
        Action   = ["eks:DescribeCluster"],
        Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.eks_cluster_name}"
      },
      {
        Effect   = "Allow",
        Action   = ["ssm:PutParameter", "ssm:GetParameter"],
        Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/checkout/*"
      },
      {
        Effect = "Allow",
        # sts:GetCallerIdentity doesn't support resource-level permissions
        # This is required for the Lambda to authenticate with EKS
        Action   = ["sts:GetCallerIdentity"],
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "runbook_action" {
  function_name = "${var.name}-runbook-action"
  role          = aws_iam_role.runbook_lambda_role.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  filename      = "${path.module}/lambda/runbook_action.zip"
  timeout       = 30

  environment {
    variables = {
      REGION            = var.aws_region
      CLUSTER_NAME      = var.eks_cluster_name
      TARGET_NAMESPACE  = "apps"
      TARGET_DEPLOYMENT = "checkout"
      DEGRADED_PARAM    = aws_ssm_parameter.checkout_degraded_mode.name
    }
  }
}

# Allow Step Functions to invoke the runbook action lambda
resource "aws_iam_role_policy" "sfn_invoke_lambda" {
  role = aws_iam_role.sfn_role.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect   = "Allow",
        Action   = ["lambda:InvokeFunction"],
        Resource = [aws_lambda_function.runbook_action.arn]
      }
    ]
  })
}
