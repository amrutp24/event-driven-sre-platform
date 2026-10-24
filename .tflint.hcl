# tflint settings used by CI. The terraform ruleset ships with tflint, so
# nothing is downloaded.
config {
  # Lint the example's call into the local module too
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
