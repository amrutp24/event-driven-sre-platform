"""
Architecture diagram for the README, drawn with Diagrams (diagrams as code).

Rebuild docs/architecture/architecture.png after changing this file. It needs
Graphviz, so the easiest way is a throwaway container from the repo root:

  docker run --rm -v "$PWD:/work" -w /work python:3.13-slim sh -c \
    "apt-get update -qq && apt-get install -y -qq graphviz >/dev/null \
     && pip install -q diagrams==0.25.1 && python docs/architecture/diagram.py"

(On Git Bash, prefix that with MSYS_NO_PATHCONV=1.)
"""
from diagrams import Cluster, Diagram, Edge
from diagrams.aws.compute import EKS, Lambda
from diagrams.aws.database import Dynamodb
from diagrams.aws.integration import SNS, SQS, Eventbridge, StepFunctions
from diagrams.aws.management import Cloudwatch, SystemsManagerParameterStore
from diagrams.aws.network import APIGateway
from diagrams.aws.security import KMS
from diagrams.k8s.compute import Deployment
from diagrams.onprem.client import Users
from diagrams.onprem.monitoring import Prometheus
from diagrams.saas.chat import Slack
from diagrams.saas.logging import Datadog

graph_attr = {
    "fontsize": "22",
    "pad": "0.5",
    "nodesep": "0.7",
    "ranksep": "1.1",
    "splines": "spline",
}

# Arrows that go back toward the cluster shouldn't pull the layout around
back = {"constraint": "false"}

with Diagram(
    "event-driven-sre: from alert to remediation",
    filename="docs/architecture/architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=graph_attr,
):
    with Cluster("EKS cluster"):
        prometheus = Prometheus("Prometheus +\nAlertmanager")
        checkout = Deployment("checkout\n(HPA 3-10)")
        eks_api = EKS("Kubernetes API")

    datadog = Datadog("Datadog\n(optional)")

    with Cluster("The module"):
        webhook = APIGateway("POST /webhook\n(API key)")
        ingest = Lambda("alert_ingest")
        bus = Eventbridge("event bus")

        with Cluster("Incident record"):
            recorder = StepFunctions("incident recorder")
            table = Dynamodb("incidents")

        with Cluster("Remediation"):
            runbook = StepFunctions("runbook (7 steps)")
            action = Lambda("runbook_action")
            flag = SystemsManagerParameterStore("degraded flag")

        with Cluster("Notifications"):
            alerts = SNS("alerts topic")
            chat = SNS("chat topic")

        with Cluster("Audit and replay"):
            logs = Cloudwatch("audit log\n+ archive")
            dlq = SQS("dead letters")

        KMS("one KMS key\nencrypts it all")

    email = Users("on-call email")
    slack = Slack("Slack")

    # Detection and ingestion
    prometheus >> Edge(label="firing / resolved") >> webhook
    datadog >> webhook
    checkout >> Edge(style="dashed", label="metrics") >> prometheus
    webhook >> ingest >> bus

    # Fan-out from the bus
    bus >> recorder >> table
    bus >> Edge(label="incident.created") >> runbook
    bus >> alerts >> email
    bus >> chat >> slack
    bus >> Edge(style="dashed") >> logs
    bus >> Edge(style="dashed", color="firebrick") >> dlq

    # Remediation
    runbook >> Edge(label="claim, verify", style="dashed") >> table
    runbook >> action
    action >> flag
    runbook >> Edge(label="action.completed,\nescalated, auto_resolved", **back) >> bus
    action >> Edge(label="scale, restart,\ndrain", color="darkorange", **back) >> eks_api
    flag >> Edge(label="read every 15s", style="dashed", color="darkorange", **back) >> checkout
