# Module tests against a mocked AWS provider: no credentials, nothing created.
# They check the module's own logic (inputs, toggles, wiring, the rendered
# state machine). What AWS itself accepts is only proven by a real deploy.
#
#   terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_vpc" {
    defaults = { cidr_block = "10.40.0.0/16" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/module-key" }
  }

  # The provider still checks ARN arguments, so computed ARNs need the right shape
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock-role" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock-topic" }
  }
  mock_resource "aws_sqs_queue" {
    defaults = { arn = "arn:aws:sqs:us-east-1:123456789012:mock-queue" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_cloudwatch_event_bus" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:event-bus/mock" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/mock" }
  }
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_lambda_function" {
    defaults = {
      arn        = "arn:aws:lambda:us-east-1:123456789012:function:mock"
      invoke_arn = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:123456789012:function:mock/invocations"
    }
  }
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/mock" }
  }
  mock_resource "aws_ssm_parameter" {
    defaults = { arn = "arn:aws:ssm:us-east-1:123456789012:parameter/checkout/degraded_mode" }
  }
  mock_resource "aws_api_gateway_rest_api" {
    defaults = { execution_arn = "arn:aws:execute-api:us-east-1:123456789012:mockapi" }
  }
}

variables {
  cluster_name              = "test-cluster"
  vpc_id                    = "vpc-0123456789abcdef0"
  subnet_ids                = ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"]
  cluster_security_group_id = "sg-0123456789abcdef0"
  remediation_namespaces    = ["apps"]
}

run "defaults" {
  command = apply

  assert {
    condition     = length(aws_kms_key.platform) == 1 && output.kms_key_arn == aws_kms_key.platform[0].arn
    error_message = "The module should create its own KMS key by default and output it."
  }

  assert {
    condition     = aws_dynamodb_table.incidents.server_side_encryption[0].kms_key_arn == aws_kms_key.platform[0].arn
    error_message = "The incidents table should use the module's key."
  }

  assert {
    condition     = aws_dynamodb_table.incidents.deletion_protection_enabled && aws_dynamodb_table.incidents.ttl[0].attribute_name == "expires_at"
    error_message = "The incidents table should have deletion protection and a TTL on expires_at."
  }

  assert {
    condition     = aws_sns_topic.alerts.kms_master_key_id == aws_kms_key.platform[0].arn && aws_sqs_queue.events_dlq.kms_master_key_id == aws_kms_key.platform[0].arn
    error_message = "The SNS topic and the DLQ should use the module's key."
  }

  assert {
    condition = alltrue([
      for lg in [
        aws_cloudwatch_log_group.events,
        aws_cloudwatch_log_group.runbook,
        aws_cloudwatch_log_group.incident_recorder,
        aws_cloudwatch_log_group.alert_ingest,
        aws_cloudwatch_log_group.runbook_action,
      ] : lg.retention_in_days == 30 && lg.kms_key_id == aws_kms_key.platform[0].arn
    ])
    error_message = "Every log group should keep 30 days and use the module's key."
  }

  assert {
    condition     = length(keys(jsondecode(aws_sfn_state_machine.runbook.definition).States.EvaluateAlert.Assign.routes)) == 0
    error_message = "No alert should be routed to an action by default."
  }

  assert {
    condition = (
      strcontains(jsondecode(aws_sfn_state_machine.runbook.definition).States.ChooseAction.Assign.target.namespace, "'apps'")
      && endswith(jsondecode(aws_sfn_state_machine.runbook.definition).States.ChooseAction.Assign.target.deployment, ": '')) %}")
      && endswith(jsondecode(aws_sfn_state_machine.runbook.definition).States.ChooseAction.Assign.target.replicas, ": 4 %}")
    )
    error_message = "The runbook should default to the first remediation namespace, no deployment and 4 replicas."
  }

  assert {
    condition     = aws_lambda_function.runbook_action.environment[0].variables["CLUSTER_NAME"] == "test-cluster" && aws_lambda_function.runbook_action.environment[0].variables["ALLOWED_NAMESPACES"] == "apps"
    error_message = "runbook_action should target the given cluster and the apps namespace."
  }

  assert {
    condition     = aws_lambda_function.runbook_action.reserved_concurrent_executions == -1 && aws_lambda_function.runbook_action.vpc_config[0].security_group_ids == toset([aws_security_group.runbook_lambda.id])
    error_message = "runbook_action should be unreserved by default and run in its own security group."
  }

  assert {
    condition     = strcontains(aws_iam_role_policy.runbook_lambda_policy.policy, "cluster/test-cluster") && !strcontains(aws_iam_role_policy.runbook_lambda_policy.policy, "ssm:")
    error_message = "runbook_action's policy should be scoped to the cluster, with no SSM access while degraded mode is off."
  }

  assert {
    condition = (
      length(aws_ssm_parameter.degraded_mode) == 0
      && output.degraded_mode_parameter_name == null
      && output.degraded_mode_parameter_arn == null
      && length(setintersection(keys(aws_lambda_function.runbook_action.environment[0].variables), ["DEGRADED_DEPLOYMENT", "DEGRADED_PARAM", "DRAINABLE_NODE_LABEL"])) == 0
    )
    error_message = "Degraded mode and drain_node should be off by default: no parameter, and nothing in runbook_action's environment."
  }

  assert {
    condition     = length(aws_eks_access_entry.runbook_action) == 1 && contains(aws_eks_access_entry.runbook_action[0].kubernetes_groups, "sre-remediation")
    error_message = "runbook_action's role should get an access entry in the sre-remediation group."
  }

  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.cluster_from_runbook_action.security_group_id == "sg-0123456789abcdef0"
      && aws_vpc_security_group_ingress_rule.cluster_from_runbook_action.from_port == 443
      && aws_vpc_security_group_ingress_rule.cluster_from_runbook_action.referenced_security_group_id == aws_security_group.runbook_lambda.id
    )
    error_message = "The cluster security group should allow 443 from runbook_action."
  }

  assert {
    condition     = aws_api_gateway_rest_api.alerts_api.endpoint_configuration[0].types[0] == "REGIONAL" && length(aws_vpc_endpoint.webhook) == 0
    error_message = "The webhook should be a regional API with no VPC endpoint by default."
  }

  assert {
    condition     = length(aws_api_gateway_api_key.webhook) == 2 && aws_api_gateway_method.webhook_post.api_key_required
    error_message = "The webhook should require an API key, with one key each for Alertmanager and Datadog."
  }

  assert {
    condition     = output.kubernetes_group == "sre-remediation"
    error_message = "Outputs should report the Kubernetes group."
  }
}

run "checkout_demo" {
  command = apply

  # What examples/complete passes
  variables {
    runbook_routes = {
      CheckoutHighErrorRate  = { action = "degrade_or_scale" }
      CheckoutHighLatencyP95 = { action = "degrade_or_scale" }
      CheckoutSLOBurnFast    = { action = "degrade_or_scale" }
      CheckoutDown           = { action = "restart" }
      NodeUnderPressure      = { action = "drain_node" }
    }
    default_deployment           = "checkout"
    degraded_mode_deployment     = "checkout"
    degraded_mode_parameter_name = "/checkout/degraded_mode"
    drainable_node_label         = "nodegroup=apps"
  }

  assert {
    condition     = length(keys(jsondecode(aws_sfn_state_machine.runbook.definition).States.EvaluateAlert.Assign.routes)) == 5 && jsondecode(aws_sfn_state_machine.runbook.definition).States.EvaluateAlert.Assign.routes.NodeUnderPressure.action == "drain_node"
    error_message = "The runbook should render the 5 checkout routes."
  }

  assert {
    condition     = strcontains(jsondecode(aws_sfn_state_machine.runbook.definition).States.ChooseAction.Assign.target.deployment, "'checkout'")
    error_message = "The runbook's default deployment should be checkout."
  }

  assert {
    condition = (
      length(aws_ssm_parameter.degraded_mode) == 1
      && aws_ssm_parameter.degraded_mode[0].name == "/checkout/degraded_mode"
      && aws_ssm_parameter.degraded_mode[0].value == "false"
      && output.degraded_mode_parameter_name == "/checkout/degraded_mode"
      && output.degraded_mode_parameter_arn == aws_ssm_parameter.degraded_mode[0].arn
    )
    error_message = "Degraded mode should create the parameter and output it."
  }

  assert {
    condition     = strcontains(aws_iam_role_policy.runbook_lambda_policy.policy, "ssm:PutParameter") && strcontains(aws_iam_role_policy.runbook_lambda_policy.policy, aws_ssm_parameter.degraded_mode[0].arn)
    error_message = "runbook_action should be allowed to set the degraded-mode parameter."
  }

  assert {
    condition = (
      aws_lambda_function.runbook_action.environment[0].variables["DEGRADED_DEPLOYMENT"] == "checkout"
      && aws_lambda_function.runbook_action.environment[0].variables["DEGRADED_PARAM"] == "/checkout/degraded_mode"
      && aws_lambda_function.runbook_action.environment[0].variables["DRAINABLE_NODE_LABEL"] == "nodegroup=apps"
    )
    error_message = "runbook_action should get the degraded-mode deployment, its parameter and the drainable label."
  }
}

run "degraded_mode_parameter_name_default" {
  command = apply

  variables {
    name                     = "payments-sre"
    degraded_mode_deployment = "payments-api"
  }

  assert {
    condition     = aws_ssm_parameter.degraded_mode[0].name == "/payments-sre/degraded-mode" && aws_lambda_function.runbook_action.environment[0].variables["DEGRADED_PARAM"] == "/payments-sre/degraded-mode"
    error_message = "The parameter name should default to /<name>/degraded-mode."
  }
}

run "default_scale_replicas" {
  command = plan

  variables {
    default_scale_replicas   = 6
    remediation_max_replicas = 6
  }

  assert {
    condition     = endswith(jsondecode(aws_sfn_state_machine.runbook.definition).States.ChooseAction.Assign.target.replicas, ": 6 %}")
    error_message = "The runbook should scale to default_scale_replicas when the alert doesn't say."
  }
}

run "rejects_default_replicas_above_the_max" {
  command = plan

  variables {
    default_scale_replicas   = 11
    remediation_max_replicas = 10
  }

  expect_failures = [aws_sfn_state_machine.runbook]
}

run "bring_your_own_key" {
  command = apply

  variables {
    create_kms_key = false
    kms_key_arn    = "arn:aws:kms:us-east-1:123456789012:key/my-own-key"
  }

  assert {
    condition     = length(aws_kms_key.platform) == 0 && length(aws_kms_alias.platform) == 0
    error_message = "No key should be created when one is passed in."
  }

  assert {
    condition = (
      aws_dynamodb_table.incidents.server_side_encryption[0].kms_key_arn == var.kms_key_arn
      && aws_sns_topic.alerts.kms_master_key_id == var.kms_key_arn
      && aws_cloudwatch_log_group.events.kms_key_id == var.kms_key_arn
      && output.kms_key_arn == var.kms_key_arn
    )
    error_message = "Everything should use the key that was passed in."
  }
}

run "missing_key" {
  command = plan

  variables {
    create_kms_key = false
  }

  expect_failures = [aws_dynamodb_table.incidents]
}

run "private_webhook" {
  command = apply

  variables {
    webhook_private = true
  }

  assert {
    condition     = aws_api_gateway_rest_api.alerts_api.endpoint_configuration[0].types[0] == "PRIVATE"
    error_message = "webhook_private should make the API private."
  }

  assert {
    condition = (
      length(aws_vpc_endpoint.webhook) == 1
      && aws_vpc_endpoint.webhook[0].service_name == "com.amazonaws.us-east-1.execute-api"
      && aws_vpc_endpoint.webhook[0].private_dns_enabled
      && contains(aws_api_gateway_rest_api.alerts_api.endpoint_configuration[0].vpc_endpoint_ids, aws_vpc_endpoint.webhook[0].id)
    )
    error_message = "A private webhook needs an execute-api endpoint with private DNS, attached to the API."
  }

  assert {
    condition     = length(aws_api_gateway_rest_api_policy.private_only) == 1 && strcontains(aws_api_gateway_rest_api_policy.private_only[0].policy, aws_vpc_endpoint.webhook[0].id)
    error_message = "A private webhook should only accept requests through its endpoint."
  }
}

run "custom_routes_and_targets" {
  command = apply

  variables {
    runbook_routes         = { PaymentsDown = { action = "restart" } }
    remediation_namespaces = ["payments", "apps"]
    default_deployment     = "payments-api"
  }

  assert {
    condition     = keys(jsondecode(aws_sfn_state_machine.runbook.definition).States.EvaluateAlert.Assign.routes) == ["PaymentsDown"]
    error_message = "The runbook should use the routes passed in, and only those."
  }

  assert {
    condition = (
      aws_lambda_function.runbook_action.environment[0].variables["ALLOWED_NAMESPACES"] == "payments,apps"
      && strcontains(jsondecode(aws_sfn_state_machine.runbook.definition).States.ChooseAction.Assign.target.namespace, "'payments'")
      && strcontains(jsondecode(aws_sfn_state_machine.runbook.definition).States.ChooseAction.Assign.target.deployment, "'payments-api'")
    )
    error_message = "The allowed namespaces and the runbook's default target should follow the inputs."
  }
}

run "optional_pieces_off" {
  command = apply

  variables {
    create_access_entry            = false
    degraded_mode_deployment       = "checkout"
    degraded_mode_parameter_name   = "/checkout/degraded_mode"
    create_degraded_mode_parameter = false
    webhook_require_api_key        = false
  }

  assert {
    condition     = length(aws_eks_access_entry.runbook_action) == 0 && length(aws_ssm_parameter.degraded_mode) == 0
    error_message = "The access entry and the parameter should be optional."
  }

  assert {
    condition     = output.degraded_mode_parameter_arn == "arn:aws:ssm:us-east-1:123456789012:parameter/checkout/degraded_mode"
    error_message = "Without the parameter, its ARN should be built from the name."
  }

  assert {
    condition     = length(aws_api_gateway_api_key.webhook) == 0 && length(aws_api_gateway_usage_plan.webhook) == 0 && !aws_api_gateway_method.webhook_post.api_key_required && output.alertmanager_api_key == ""
    error_message = "With webhook_require_api_key off there should be no keys or usage plan."
  }
}

run "rejects_unknown_route_action" {
  command = plan

  variables {
    runbook_routes = { SomeAlert = { action = "reboot_cluster" } }
  }

  expect_failures = [var.runbook_routes]
}

run "rejects_bad_inputs" {
  command = plan

  variables {
    remediation_namespaces    = []
    drainable_node_label      = "apps"
    log_retention_days        = 31
    auto_remediate_severities = ["page"]
  }

  expect_failures = [
    var.remediation_namespaces,
    var.drainable_node_label,
    var.log_retention_days,
    var.auto_remediate_severities,
  ]
}

run "no_notifications_by_default" {
  command = plan

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 0 && length(aws_sns_topic.chat) == 0 && length(aws_cloudwatch_event_target.to_chat) == 0
    error_message = "Nothing should subscribe or publish to chat unless asked."
  }
}

run "notifications" {
  command = apply

  variables {
    notification_emails = ["oncall@example.com", "sre@example.com"]
    chat_notifications  = true
  }

  assert {
    condition     = length(aws_sns_topic_subscription.email) == 2 && aws_sns_topic_subscription.email["oncall@example.com"].protocol == "email"
    error_message = "Each email should subscribe to the alerts topic."
  }

  assert {
    condition     = length(aws_sns_topic.chat) == 1 && aws_sns_topic.chat[0].kms_master_key_id == aws_kms_key.platform[0].arn && output.chat_topic_arn == aws_sns_topic.chat[0].arn
    error_message = "chat_notifications should create an encrypted chat topic and output it."
  }

  assert {
    condition     = toset(keys(aws_cloudwatch_event_target.to_chat)) == toset(["created", "resolved", "escalated", "auto_resolved"])
    error_message = "All four incident rules should feed the chat topic."
  }

  assert {
    condition = (
      jsondecode(aws_cloudwatch_event_target.to_chat["escalated"].input_transformer[0].input_template).source == "custom"
      && strcontains(jsondecode(aws_cloudwatch_event_target.to_chat["escalated"].input_transformer[0].input_template).content.title, "<alertname>")
      && strcontains(jsondecode(aws_cloudwatch_event_target.to_chat["escalated"].input_transformer[0].input_template).content.description, "<reason>")
    )
    error_message = "Chat messages should be custom notifications with plain <placeholders> for EventBridge."
  }
}

run "rejects_bad_email" {
  command = plan

  variables {
    notification_emails = ["not-an-email"]
  }

  expect_failures = [var.notification_emails]
}
