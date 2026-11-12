# Thin CI adapter. Sensitive inputs stay in a private runner-local context file.
import argparse
import base64
import subprocess
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from .odim_context import normalize_event, config_value
from .odim_guard import SSHProbe, select_target, wait_for_ready, rewrite_kubeconfig
from .odim_dispatch import build_payload, post_dispatch, DispatchRejected, DispatchUncertain

def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        json.dump(data, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
    os.replace(temporary, path)


def export_env(values):
    destination = os.environ.get("GITHUB_ENV")
    if not destination:
        return
    with open(destination, "a", encoding="utf-8") as stream:
        for key, value in values.items():
            value = str(value)
            if "\n" in value or "\r" in value:
                raise ValueError("multiline value is not an exported metadata field")
            stream.write(key + "=" + value + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="ODIM readiness and safe handoff")
    parser.add_argument("--context", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    context_parser = commands.add_parser("context")
    context_parser.add_argument("stage", choices=["p1", "p2"])
    gate_parser = commands.add_parser("gate")
    gate_parser.add_argument("--report", required=True)
    gate_parser.add_argument("--terraform-output")
    gate_parser.add_argument("--interval", type=float, default=5)
    config_parser = commands.add_parser("config")
    config_parser.add_argument("--output", required=True)
    commands.add_parser("plan")
    dispatch_parser = commands.add_parser("dispatch")
    dispatch_parser.add_argument("--readiness-report", required=True)
    dispatch_parser.add_argument("--report", required=True)
    kubeconfig_parser = commands.add_parser("kubeconfig")
    kubeconfig_parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    context = {}
    try:
        if args.command == "context":
            with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as stream:
                event = json.load(stream)
            context = normalize_event(args.stage, os.environ["GITHUB_EVENT_NAME"], event, os.environ["GITHUB_RUN_ID"], os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
            write_json(args.context, context)
            fields = ["experiment_id", "run_id", "run_attempt", "cluster_ip", "cluster_name", "cluster_user", "minimum_ready_nodes", "readiness_timeout_s", "free5gc_ref"]
            metadata = {"ODIM_" + key.upper(): context[key] for key in fields if key in context}
            metadata["ODIM_DISPATCH_FREE5GC"] = "true" if context.get("dispatch_free5gc") is True else "false"
            export_env(metadata)
            print(json.dumps({"stage": context["stage"], "experiment_id": context["experiment_id"], "status": "validated"}))
            return 0
        with open(args.context, encoding="utf-8") as stream:
            context = json.load(stream)
        if not isinstance(context, dict):
            context = {}
            raise ValueError("context must be an object")
        if args.command == "kubeconfig":
            probe = SSHProbe(context["cluster_ip"], user=context["cluster_user"], minimum=context["minimum_ready_nodes"], identity=os.environ.get("ODIM_SSH_IDENTITY") or None, password=os.environ.get("SSHPASS") or None)
            command = "sudo -n microk8s.kubectl config view --raw -o json"
            response = probe.runner([*probe.command, command], capture_output=True, text=True, timeout=min(30, context["readiness_timeout_s"]), env=probe.environment)
            if response.returncode != 0:
                raise ValueError("kubeconfig could not be retrieved")
            document = rewrite_kubeconfig(json.loads(response.stdout), context["cluster_ip"])
            write_json(args.output, document)
            return 0
        if args.command == "dispatch":
            return run_dispatch(args, context)
        if args.command == "plan":
            if context["stage"] != "p1":
                raise ValueError("plan belongs to P1")
            variables = context["terraform"]
            if variables["mode"] == "json":
                path = Path(args.context).parent / "odim.tfvars.json"
                write_json(path, variables["value"])
                argument = "-var-file=" + str(path)
            elif variables["mode"] == "hcl":
                argument = "-var=" + variables["value"]
            else:
                raise ValueError("invalid Terraform input mode")
            return subprocess.run(["terraform", "plan", argument, "-no-color", "-input=false", "-out=tfplan"]).returncode
        if args.command == "config":
            data = base64.b64decode(config_value(context["config_base64"], True), validate=True)
            descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.chmod(args.output, 0o600)
            return 0
        if args.command == "gate":
            return run_gate(args, context)
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
        if getattr(args, "report", None):
            write_json(args.report, {
                "schema_version": 1,
                "phase": "cluster_readiness" if args.command == "gate" else args.command,
                "status": "failed",
                "ready": False,
                "reason": "invalid_configuration_or_input",
                "experiment_id": context.get("experiment_id"),
                "producer_run_id": context.get("run_id"),
                "producer_run_attempt": context.get("run_attempt"),
                "upstream_run_id": context.get("upstream_run_id", ""),
                "upstream_run_attempt": context.get("upstream_run_attempt"),
                "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            })
        print("ODIM: operation failed; inspect phase evidence before retrying", file=sys.stderr)
        return 2
    return 2


def run_gate(args, context):
    base = {
        "schema_version": 1,
        "phase": "cluster_readiness",
        "experiment_id": context["experiment_id"],
        "producer_run_id": context["run_id"],
        "producer_run_attempt": context["run_attempt"],
        "upstream_run_id": context.get("upstream_run_id", ""),
        "upstream_run_attempt": context.get("upstream_run_attempt"),
        "status": "checking",
        "ready": False,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(args.report, base)
    if context["stage"] == "p1":
        if not args.terraform_output:
            raise ValueError("P1 readiness requires the observed Terraform output")
        with open(args.terraform_output, encoding="utf-8") as stream:
            target = select_target(json.load(stream), context["cluster_ip"], context["cluster_name"])
    else:
        target = {"address": context["cluster_ip"], "name": context["cluster_name"]}
    probe = SSHProbe(target["address"], user=context["cluster_user"], minimum=context["minimum_ready_nodes"], identity=os.environ.get("ODIM_SSH_IDENTITY") or None, password=os.environ.get("SSHPASS") or None)
    result = wait_for_ready(probe, timeout=context["readiness_timeout_s"], interval=args.interval, consecutive=3)
    report = {
        **base,
        **result,
        "target": target,
        "minimum_ready_nodes": context["minimum_ready_nodes"],
        "required_consecutive": 3,
        "status": "ready" if result["ready"] else "timeout",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(args.report, report)
    values = {
        "ready": "true" if result["ready"] else "false",
        "cluster_ip": target["address"],
        "cluster_name": target["name"],
        "experiment_id": context["experiment_id"],
    }
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(key + "=" + value + "\n")
    if result["ready"]:
        export_env({"ODIM_CLUSTER_IP": target["address"], "ODIM_CLUSTER_NAME": target["name"]})
    print(json.dumps({
        "experiment_id": context["experiment_id"],
        "phase": "cluster_readiness",
        "status": report["status"],
        "elapsed_s": report["elapsed_s"],
    }))
    return 0 if result["ready"] else 124



def run_dispatch(args, context):
    report = {
        "schema_version": 1,
        "phase": "dispatch",
        "experiment_id": context.get("experiment_id"),
        "producer_run_id": context.get("run_id"),
        "producer_run_attempt": context.get("run_attempt"),
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    if context.get("dispatch_free5gc") is not True:
        report["status"] = "disabled"
        write_json(args.report, report)
        return 0
    try:
        with open(args.readiness_report, encoding="utf-8") as stream:
            readiness = json.load(stream)
        payload = build_payload(context, readiness)
        result = post_dispatch(payload, os.environ.get("ODIM_DISPATCH_TOKEN", ""))
        report.update(result)
        code = 0
    except DispatchRejected:
        report.update({"status": "rejected", "reason": "remote_rejected_dispatch"})
        code = 1
    except DispatchUncertain as error:
        report.update({"status": "unknown", "reason": "reconcile_target_before_resending"})
        if error.workflow_run_id is not None:
            report.update({"workflow_run_id": error.workflow_run_id, "target_repository": error.target_repository, "target_ref": error.target_ref})
        code = 1
    except (ValueError, KeyError, TypeError, OSError):
        report.update({"status": "blocked", "reason": "readiness_or_dispatch_configuration_invalid"})
        code = 2
    write_json(args.report, report)
    print(json.dumps(report))
    return code



if __name__ == "__main__":
    raise SystemExit(main())
