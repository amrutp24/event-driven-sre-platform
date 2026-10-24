"""
The keys Terraform passes to templatefile() have to match the placeholders in
each template: a missing key fails the plan, and an unused one is usually a
typo. The values the other tests render with should match Terraform's defaults.
"""
import json
import math
import re

import pytest
from asl_sim import TERRAFORM_DIR, placeholders, template_text
from conftest import RECORDER_VARS, RUNBOOK_VARS, TEMPLATES

CALL = re.compile(r'templatefile\(\s*"\$\{path\.module\}/stepfunctions/([^"]+)"\s*,\s*\{')


def _block(text, start):
    """The text of the {...} starting at text[start], skipping braces in strings."""
    depth, in_string, i = 0, False, start
    while i < len(text):
        char = text[i]
        if in_string:
            if char == "\\":
                i += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    raise ValueError("unbalanced braces")


def templatefile_calls():
    calls = {}
    for path in sorted(TERRAFORM_DIR.glob("*.tf")):
        text = path.read_text(encoding="utf-8")
        for match in CALL.finditer(text):
            body = _block(text, match.end() - 1)
            keys = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", body, re.MULTILINE))
            calls[match.group(1)] = keys
    return calls


def variable_default(name):
    text = (TERRAFORM_DIR / "variables.tf").read_text(encoding="utf-8")
    pattern = r'variable\s+"' + re.escape(name) + r'"\s*\{(.*?)\n\}'
    match = re.search(pattern, text, re.DOTALL)
    assert match, f"variable {name} isn't in variables.tf"
    default = re.search(r"^\s*default\s*=\s*(.+)$", match.group(1), re.MULTILINE)
    assert default, f"variable {name} has no default"
    return json.loads(default.group(1).strip())


def test_every_template_is_rendered_by_terraform():
    assert set(templatefile_calls()) == set(TEMPLATES)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_terraform_passes_exactly_the_placeholders(name):
    passed = templatefile_calls()[name]
    used = placeholders(template_text(name))
    assert used - passed == set(), f"{name} uses vars Terraform doesn't pass"
    assert passed - used == set(), f"Terraform passes vars {name} doesn't use"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_test_values_cover_the_same_keys(name):
    assert set(TEMPLATES[name]) == templatefile_calls()[name]


def test_test_values_match_terraform_defaults():
    stabilization = variable_default("stabilization_seconds")
    poll = variable_default("verify_poll_seconds")
    timeout = variable_default("verify_timeout_seconds")

    assert RUNBOOK_VARS["stabilization_seconds"] == stabilization
    assert RUNBOOK_VARS["verify_poll_seconds"] == poll
    assert RUNBOOK_VARS["verify_attempts"] == math.floor(max(timeout - stabilization, 0) / poll) + 1
    assert RUNBOOK_VARS["auto_remediate_severities"] == ", ".join(
        f"'{s}'" for s in variable_default("auto_remediate_severities"))
    assert RECORDER_VARS["ttl_seconds"] == variable_default("incident_retention_days") * 86400
