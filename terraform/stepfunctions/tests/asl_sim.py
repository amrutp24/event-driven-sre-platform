"""
Helpers for testing the state machine templates without AWS.

- render(): fills ${name} placeholders the way Terraform's templatefile does
- Simulator: walks a JSONata state machine with mocked task results

The simulator covers the states the runbook uses (Pass, Choice, Task, Wait,
Succeed, Fail). Retries aren't replayed: a mocked error is treated as what's
left after the retries ran out, and each task call records how many retries
the matching Retry rule would have allowed.
"""
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonata

STEPFUNCTIONS_DIR = Path(__file__).resolve().parent.parent
TERRAFORM_DIR = STEPFUNCTIONS_DIR.parent

PLACEHOLDER = re.compile(r"(?<!\$)\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")
DIRECTIVE = re.compile(r"(?<!%)%\{")


# ---------------------------------------------------------------- templates

def template_text(name):
    return (STEPFUNCTIONS_DIR / name).read_text(encoding="utf-8")


def placeholders(text):
    return set(PLACEHOLDER.findall(text))


def render(text, variables):
    """Substitute ${name} like templatefile; a missing key is an error there too."""
    missing = placeholders(text) - set(variables)
    if missing:
        raise KeyError(f"template uses vars that weren't passed: {sorted(missing)}")
    out = PLACEHOLDER.sub(lambda m: str(variables[m.group(1)]), text)
    return out.replace("$${", "${")


def load(name, variables):
    return json.loads(render(template_text(name), variables))


# ---------------------------------------------------------------- errors

class TaskError(Exception):
    """A task failure as Step Functions reports it: an error name and a cause."""

    def __init__(self, error, cause=""):
        super().__init__(f"{error}: {cause}")
        self.error = error
        self.cause = cause


class SimulationError(AssertionError):
    """Something Step Functions would reject, or the simulator can't model."""


# ---------------------------------------------------------------- expressions

_compiled = {}


def is_expression(value):
    return isinstance(value, str) and value.startswith("{%") and value.endswith("%}")


def _evaluate(expr, bindings, where):
    body = expr[2:-2].strip()
    if body not in _compiled:
        _compiled[body] = jsonata.Jsonata(body)
    try:
        result = _compiled[body].evaluate(None, bindings)
    except Exception as exc:  # jsonata raises its own JException
        raise SimulationError(f"{where}: {body!r} failed to evaluate: {exc}") from exc
    return copy.deepcopy(result)


def resolve(value, bindings, where, strict=True):
    """Evaluate every {% %} string in a nested value. With strict, an
    expression that comes back undefined fails the test."""
    if is_expression(value):
        result = _evaluate(value, bindings, where)
        if strict and result is None:
            raise SimulationError(f"{where}: {value!r} evaluated to undefined")
        return result
    if isinstance(value, str) and "{%" in value:
        raise SimulationError(f"{where}: {value!r} mixes text and an expression")
    if isinstance(value, dict):
        return {k: resolve(v, bindings, f"{where}.{k}", strict) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, bindings, f"{where}[{i}]", strict) for i, v in enumerate(value)]
    return copy.deepcopy(value)


def _error_matches(rule, error):
    names = rule["ErrorEquals"]
    if error in names:
        return True
    if "States.ALL" in names:
        return error != "States.Runtime"
    if "States.TaskFailed" in names:
        return error not in ("States.Timeout", "States.Runtime")
    return False


# ---------------------------------------------------------------- results

@dataclass
class TaskCall:
    state: str
    resource: str
    arguments: dict
    retries: int = 0
    error: str = None


@dataclass
class Execution:
    status: str = "RUNNING"
    end_state: str = None
    error: str = None
    cause: str = None
    output: object = None
    path: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    waits: list = field(default_factory=list)
    variables: dict = field(default_factory=dict)

    def calls_to(self, state):
        return [c for c in self.calls if c.state == state]

    def events(self, detail_type=None):
        """Entries sent with events:putEvents, optionally of one detail-type."""
        entries = []
        for call in self.calls:
            if call.resource.endswith(":events:putEvents"):
                entries.extend(call.arguments["Entries"])
        if detail_type:
            entries = [e for e in entries if e["DetailType"] == detail_type]
        return entries


# ---------------------------------------------------------------- simulator

DEFAULT_RESULTS = {
    "arn:aws:states:::dynamodb:updateItem": {},
    "arn:aws:states:::dynamodb:getItem": {},
    "arn:aws:states:::events:putEvents": {"Entries": [{"EventId": "11111111-2222"}], "FailedEntryCount": 0},
    "arn:aws:states:::lambda:invoke": {"StatusCode": 200, "Payload": {}},
}


class Simulator:
    """
    tasks maps a state name to what the task returns:
      - a value, returned on every call
      - a list, one item per call (the last one repeats)
      - a callable(arguments, call_number) returning a value
    Any of these may be (or return, or raise) a TaskError.
    """

    EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:event-driven-sre-runbook:test"
    START = datetime(2026, 10, 22, 10, 0, 0, tzinfo=UTC)

    def __init__(self, definition, tasks=None, max_steps=500):
        if definition.get("QueryLanguage") != "JSONata":
            raise SimulationError("the simulator only runs JSONata state machines")
        self.definition = definition
        self.tasks = tasks or {}
        self.max_steps = max_steps

    def run(self, execution_input):
        run = Execution()
        clock = self.START
        variables = {}
        state_input = copy.deepcopy(execution_input)
        name = self.definition["StartAt"]
        counts = {}

        for _ in range(self.max_steps):
            state = self.definition["States"][name]
            if state.get("QueryLanguage", "JSONata") != "JSONata":
                raise SimulationError(f"{name}: JSONPath states aren't supported")
            run.path.append(name)
            context = {
                "Execution": {
                    "Id": self.EXECUTION_ARN,
                    "Name": "test",
                    "Input": copy.deepcopy(execution_input),
                    "StartTime": _iso(self.START),
                },
                "State": {"Name": name, "EnteredTime": _iso(clock)},
                "StateMachine": {"Id": self.EXECUTION_ARN.replace(":execution:", ":stateMachine:").rsplit(":", 1)[0]},
            }
            states = {"input": state_input, "context": context}
            bindings = dict(variables, states=states)
            kind = state["Type"]

            if kind == "Succeed":
                run.status, run.end_state, run.output = "SUCCEEDED", name, state_input
                break

            if kind == "Fail":
                run.status, run.end_state = "FAILED", name
                run.error = resolve(state.get("Error"), bindings, f"{name}.Error")
                run.cause = resolve(state.get("Cause"), bindings, f"{name}.Cause")
                break

            if kind == "Pass":
                output = self._output(state, bindings, state_input, name)
                variables = self._assign(state.get("Assign"), bindings, variables, name)
                state_input, name = output, state["Next"]

            elif kind == "Wait":
                seconds = resolve(state["Seconds"], bindings, f"{name}.Seconds")
                if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
                    raise SimulationError(f"{name}: Seconds must be a non-negative integer, got {seconds!r}")
                run.waits.append((name, seconds))
                clock += timedelta(seconds=seconds)
                output = self._output(state, bindings, state_input, name)
                variables = self._assign(state.get("Assign"), bindings, variables, name)
                state_input, name = output, state["Next"]

            elif kind == "Choice":
                if "Assign" in state:
                    raise SimulationError(f"{name}: top-level Assign on a Choice isn't modelled")
                for rule in state["Choices"]:
                    matched = resolve(rule["Condition"], bindings, f"{name}.Condition")
                    if not isinstance(matched, bool):
                        raise SimulationError(f"{name}: Condition returned {matched!r}, not a boolean")
                    if matched:
                        variables = self._assign(rule.get("Assign"), bindings, variables, name)
                        name = rule["Next"]
                        break
                else:
                    if "Default" not in state:
                        run.status, run.end_state, run.error = "FAILED", name, "States.NoChoiceMatched"
                        break
                    name = state["Default"]

            elif kind == "Task":
                arguments = resolve(state.get("Arguments", {}), bindings, f"{name}.Arguments")
                call = TaskCall(name, state["Resource"], arguments)
                run.calls.append(call)
                counts[name] = counts.get(name, 0) + 1
                try:
                    result = self._task_result(name, state["Resource"], arguments, counts[name])
                except TaskError as exc:
                    call.error = exc.error
                    call.retries = self._retries(state, exc.error)
                    caught = self._catch(state, exc, bindings, variables, name)
                    if caught is None:
                        run.status, run.end_state = "FAILED", name
                        run.error, run.cause = exc.error, exc.cause
                        break
                    variables, state_input, name = caught
                    clock += timedelta(seconds=1)
                    continue
                task_bindings = dict(bindings, states=dict(states, result=result))
                output = self._output(state, task_bindings, result, name)
                variables = self._assign(state.get("Assign"), task_bindings, variables, name)
                state_input, name = output, state["Next"]

            else:
                raise SimulationError(f"{name}: state type {kind} isn't modelled")

            clock += timedelta(seconds=1)
        else:
            raise SimulationError(f"no terminal state after {self.max_steps} steps: {run.path[-10:]}")

        run.variables = variables
        return run

    # -------------------------------------------------------------- pieces

    @staticmethod
    def _assign(assign, bindings, variables, where):
        if not assign:
            return variables
        # Every value sees the variables as they were when the state started
        values = resolve(assign, bindings, f"{where}.Assign")
        merged = dict(variables)
        merged.update(values)
        return merged

    @staticmethod
    def _output(state, bindings, default, where):
        if "Output" in state:
            return resolve(state["Output"], bindings, f"{where}.Output")
        return copy.deepcopy(default)

    def _task_result(self, name, resource, arguments, call_number):
        if name not in self.tasks:
            if resource not in DEFAULT_RESULTS:
                raise SimulationError(f"{name}: no mock for {resource}")
            return copy.deepcopy(DEFAULT_RESULTS[resource])
        mock = self.tasks[name]
        if callable(mock) and not isinstance(mock, TaskError):
            value = mock(arguments, call_number)
        elif isinstance(mock, list):
            value = mock[min(call_number, len(mock)) - 1]
        else:
            value = mock
        if isinstance(value, TaskError):
            raise value
        return copy.deepcopy(value)

    @staticmethod
    def _retries(state, error):
        for rule in state.get("Retry", []):
            if _error_matches(rule, error):
                return rule.get("MaxAttempts", 3)
        return 0

    @staticmethod
    def _catch(state, exc, bindings, variables, where):
        error_output = {"Error": exc.error, "Cause": exc.cause}
        for rule in state.get("Catch", []):
            if _error_matches(rule, exc.error):
                states = dict(bindings["states"], errorOutput=error_output)
                catch_bindings = dict(bindings, states=states)
                output = (resolve(rule["Output"], catch_bindings, f"{where}.Catch.Output")
                          if "Output" in rule else error_output)
                new_vars = Simulator._assign(rule.get("Assign"), catch_bindings, variables, f"{where}.Catch")
                return new_vars, output, rule["Next"]
        return None


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
