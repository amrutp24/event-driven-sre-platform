config {
  call_module_type = "all"
  
  plugin "aws" {
    enabled = true
    version = "~> 0.30"
    source  = "terraform-linters/tflint-ruleset-aws"
  }
  
  plugin "terraform" {
    enabled = true
    version = "~> 0.9"
    source  = "terraform-linters/tflint-ruleset-terraform"
  }
}

rule "terraform_required_providers" {
  enabled = false
}

rule "terraform_required_version" {
  enabled = false
}

rule "terraform_comment_syntax" {
  enabled = false
}

rule "terraform_deprecated_index" {
  enabled = false
}

rule "terraform_documented_outputs" {
  enabled = false
}

rule "terraform_documented_variables" {
  enabled = false
}

rule "terraform_module_pinned_source" {
  enabled = false
}

rule "terraform_naming_convention" {
  enabled = false
}

rule "terraform_standard_module_structure" {
  enabled = false
}

rule "terraform_typed_variables" {
  enabled = false
}

rule "terraform_unused_declarations" {
  enabled = false
}

rule "terraform_workspace_remote" {
  enabled = false
}

rule "terraform_required_version" {
  enabled = false
}

rule "terraform_typed_variables" {
  enabled = false
}

# Skip all AWS provider rules that need credentials
plugin "aws" {
  enabled = false
}