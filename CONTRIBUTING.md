# Contributing

Issues and pull requests are welcome. For anything bigger than a small fix, open an issue first so we can agree on the approach.

## Running the tests

```bash
# The module, against a mocked AWS provider (Terraform 1.7+)
terraform init -backend=false && terraform validate && terraform test

# Lambdas, with AWS calls mocked by moto
cd lambda/alert_ingest && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/
cd lambda/runbook_action && pip install -r requirements-dev.txt && AWS_DEFAULT_REGION=us-east-1 pytest tests/

# Both state machines, through a local JSONata simulator
cd stepfunctions && pip install -r tests/requirements.txt && pytest tests/

# The demo app
cd examples/complete/apps/checkout && pip install -r requirements-dev.txt && pytest tests/
```

CI runs all of these, plus Checkov, Trivy, tflint, `terraform validate` on Terraform 1.5.7 and the latest 1.x, and a check that the README's tables are current.

## pre-commit

```bash
pip install pre-commit
pre-commit install
pre-commit run -a
```

The hooks run `terraform fmt`, `terraform validate`, tflint, terraform-docs and ruff. tflint and terraform-docs have to be on your PATH. If you change a variable or an output, commit the regenerated README tables along with it.

## Commit and PR titles

Releases are made by release-please from the titles on `main`, so PR titles follow [Conventional Commits](https://www.conventionalcommits.org/): `fix: ...` for a patch release, `feat: ...` for a minor one, and `feat!: ...` or a `BREAKING CHANGE:` footer for a major one. `docs:`, `test:`, `ci:` and `chore:` don't make a release. Squash merge PRs so one conventional title lands on `main`; a PR with a single commit keeps that commit's title, so give the commit the same style.
