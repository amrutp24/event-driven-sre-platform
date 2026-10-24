"""
Structure checks for both state machine templates: the things Step Functions
or DynamoDB would reject at deploy or run time.
"""
import re

import pytest
from asl_sim import DIRECTIVE, is_expression, load, template_text
from conftest import TEMPLATES

JSONPATH_ONLY = {"Parameters", "ResultPath", "ResultSelector", "InputPath", "OutputPath"}
JSONATA_ONLY = {"Arguments", "Output"}
TERMINAL = {"Succeed", "Fail"}

# DynamoDB reserved words that could plausibly turn up as attribute names.
# Using one bare in an expression is a ValidationException.
RESERVED = {
    "ACTION", "ATTRIBUTE", "COMMENT", "CONDITION", "COUNT", "DATA", "DATE", "DAY", "DESC",
    "DURATION", "ERROR", "FILE", "HASH", "HOUR", "INDEX", "ITEM", "KEY", "LEVEL", "LIMIT",
    "LIST", "LOCATION", "LOG", "MAP", "NAME", "NUMBER", "OBJECT", "OWNER", "PATH", "REGION",
    "RESULT", "ROLE", "SECOND", "SIZE", "SOURCE", "STATE", "STATUS", "STRING", "TABLE",
    "TEXT", "TIME", "TIMESTAMP", "TOKEN", "TTL", "TYPE", "URL", "USER", "UUID", "VALUE",
    "YEAR", "ZONE",
}
EXPRESSION_WORDS = {"SET", "ADD", "REMOVE", "DELETE", "AND", "OR", "NOT", "BETWEEN", "IN"}
FUNCTIONS = {"if_not_exists", "list_append", "attribute_not_exists", "attribute_exists",
             "attribute_type", "begins_with", "contains", "size"}
EXPRESSION_FIELDS = ("UpdateExpression", "ConditionExpression", "ProjectionExpression",
                     "KeyConditionExpression", "FilterExpression")


def definitions():
    from conftest import RECORDER_VARS, RUNBOOK_VARS
    return [
        pytest.param(load("runbook.asl.json", RUNBOOK_VARS), id="runbook"),
        pytest.param(load("incident_recorder.asl.json", RECORDER_VARS), id="recorder"),
    ]


def targets(state):
    found = []
    if "Next" in state:
        found.append(state["Next"])
    if "Default" in state:
        found.append(state["Default"])
    found.extend(rule["Next"] for rule in state.get("Choices", []))
    found.extend(rule["Next"] for rule in state.get("Catch", []))
    return found


def query_language(definition, state):
    return state.get("QueryLanguage", definition.get("QueryLanguage", "JSONPath"))


def task_parameters(definition, state):
    key = "Arguments" if query_language(definition, state) == "JSONata" else "Parameters"
    return state.get(key, {})


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_no_terraform_directives(name):
    # templatefile treats %{ as the start of a directive
    assert not DIRECTIVE.search(template_text(name))


@pytest.mark.parametrize("definition", definitions())
def test_transitions_point_at_real_states(definition):
    states = definition["States"]
    assert definition["StartAt"] in states
    for name, state in states.items():
        for target in targets(state):
            assert target in states, f"{name} -> {target}, which doesn't exist"


@pytest.mark.parametrize("definition", definitions())
def test_every_state_is_reachable(definition):
    states = definition["States"]
    seen, todo = set(), [definition["StartAt"]]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(targets(states[name]))
    assert seen == set(states), f"unreachable: {sorted(set(states) - seen)}"


@pytest.mark.parametrize("definition", definitions())
def test_no_dead_ends(definition):
    for name, state in definition["States"].items():
        if state["Type"] in TERMINAL:
            assert "Next" not in state and "End" not in state, name
        elif state["Type"] == "Choice":
            assert state["Choices"], name
        else:
            assert ("Next" in state) != bool(state.get("End")), f"{name} needs exactly one of Next or End"


@pytest.mark.parametrize("definition", definitions())
def test_states_all_is_last_and_alone(definition):
    for name, state in definition["States"].items():
        for field_name in ("Retry", "Catch"):
            rules = state.get(field_name, [])
            for i, rule in enumerate(rules):
                if "States.ALL" in rule["ErrorEquals"]:
                    assert rule["ErrorEquals"] == ["States.ALL"], f"{name}.{field_name}[{i}]"
                    assert i == len(rules) - 1, f"{name}.{field_name}: States.ALL must be the last rule"


@pytest.mark.parametrize("definition", definitions())
def test_fields_match_the_query_language(definition):
    for name, state in definition["States"].items():
        language = query_language(definition, state)
        fields = set(state)
        for rule in state.get("Catch", []):
            fields |= set(rule)
        if language == "JSONata":
            assert not fields & JSONPATH_ONLY, f"{name} uses {fields & JSONPATH_ONLY} in a JSONata state"
            for rule in state.get("Choices", []):
                assert "Condition" in rule and "Variable" not in rule, name
        else:
            assert not fields & JSONATA_ONLY, f"{name} uses {fields & JSONATA_ONLY} in a JSONPath state"
            assert not _has_expression(state), f"{name} has a {{% %}} expression in a JSONPath state"


def _has_expression(value):
    if is_expression(value):
        return True
    if isinstance(value, dict):
        return any(_has_expression(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_expression(v) for v in value)
    return False


def _dynamodb_tasks(definition):
    for name, state in definition["States"].items():
        if state["Type"] == "Task" and ":dynamodb:" in state["Resource"]:
            yield name, task_parameters(definition, state)


@pytest.mark.parametrize("definition", definitions())
def test_dynamodb_placeholders_are_defined_and_used(definition):
    # DynamoDB rejects undefined placeholders and also unused ones
    for name, params in _dynamodb_tasks(definition):
        text = " ".join(params.get(f, "") for f in EXPRESSION_FIELDS)
        used_values = set(re.findall(r":[A-Za-z0-9_]+", text))
        used_names = set(re.findall(r"#[A-Za-z0-9_]+", text))
        values = set(params.get("ExpressionAttributeValues", {}))
        names = set(params.get("ExpressionAttributeNames", {}))
        assert used_values == values, f"{name}: used {sorted(used_values)}, defined {sorted(values)}"
        assert used_names == names, f"{name}: used {sorted(used_names)}, defined {sorted(names)}"


@pytest.mark.parametrize("definition", definitions())
def test_dynamodb_expressions_alias_reserved_words(definition):
    for name, params in _dynamodb_tasks(definition):
        for field_name in EXPRESSION_FIELDS:
            expression = params.get(field_name, "")
            for match in re.finditer(r"(?<![#:\w])([A-Za-z_][A-Za-z0-9_]*)(\s*\()?", expression):
                word, is_call = match.group(1), match.group(2)
                if word.upper() in EXPRESSION_WORDS or (is_call and word in FUNCTIONS):
                    continue
                assert word.upper() not in RESERVED, f"{name}.{field_name}: '{word}' is reserved, use #{word}"


@pytest.mark.parametrize("definition", definitions())
def test_dynamodb_typed_values_are_strings(definition):
    # S and N attribute values go over the wire as strings
    for name, params in _dynamodb_tasks(definition):
        for section in ("Key", "ExpressionAttributeValues"):
            for placeholder, typed in params.get(section, {}).items():
                for type_key, value in typed.items():
                    if type_key in ("S", "N") and not is_expression(value):
                        assert isinstance(value, str), f"{name}.{section}.{placeholder}.{type_key}"
                    if type_key == "N.$":
                        assert value.startswith("States.Format("), (
                            f"{name}.{section}.{placeholder}: a number needs States.Format to become a string"
                        )
