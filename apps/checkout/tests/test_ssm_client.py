"""The SSM client gets its region from AWS_REGION, which the chart sets"""
import app


def test_region_comes_from_aws_region(monkeypatch):
    calls = []
    monkeypatch.setattr(app.boto3, "client", lambda *a, **kw: calls.append(kw) or object())
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    app._ssm_client()
    assert calls[0]["region_name"] == "us-east-1"


def test_falls_back_to_aws_default_region(monkeypatch):
    calls = []
    monkeypatch.setattr(app.boto3, "client", lambda *a, **kw: calls.append(kw) or object())
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    app._ssm_client()
    assert calls[0]["region_name"] == "eu-west-1"
