# Normalize GitHub event data as data, never as executable shell text.
import base64
import binascii
import ipaddress
import json
import re

def integer(value, name, low=1, high=1800):
    if type(value) is bool or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError(name + " must be a positive integer")
    result = int(value)
    if not low <= result <= high:
        raise ValueError(name + " outside allowed range")
    return result


def boolean(value):
    if type(value) is bool:
        return value
    if value in ["true", "false"]:
        return value == "true"
    raise ValueError("dispatch_free5gc must be true or false")


def identifier(value, name, required=True):
    if not value and not required:
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ValueError("invalid " + name)
    return value


def config_value(value, required):
    if value == "" and not required:
        return ""
    if not isinstance(value, str) or not value or len(value) > 60000:
        raise ValueError("config_base64 is required and must fit the dispatch payload")
    try:
        decoded = base64.b64decode(value, validate=True)
        decoded.decode("utf-8")
        if not decoded.strip():
            raise ValueError("empty configuration")
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError("config_base64 must encode nonempty UTF-8 data") from None
    return value


def normalize_event(stage, event_name, event, run_id, run_attempt):
    if stage not in ["p1", "p2"]:
        raise ValueError("unsupported stage")
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    if event_name == "workflow_dispatch":
        payload = event.get("inputs", {})
    elif event_name == "repository_dispatch":
        payload = event.get("client_payload", {})
    else:
        raise ValueError("unsupported event type")
    if not isinstance(payload, dict):
        raise ValueError("event payload must be an object")
    experiment = identifier(payload.get("experiment_id") or "odim-" + str(run_id), "experiment_id")
    user = payload.get("cluster_user", "packer")
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("invalid cluster_user")
    dispatch = boolean(payload.get("dispatch_free5gc", False)) if stage == "p1" else False
    address = payload.get("cluster_ip" if stage == "p2" else "target_cluster_ip", "")
    if not isinstance(address, str):
        raise ValueError("cluster_ip must be a literal IP")
    if address or stage == "p2":
        literal_address(address)
    upstream = payload.get("upstream_run_id", "")
    if upstream and (not isinstance(upstream, str) or not upstream.isdecimal()):
        raise ValueError("upstream_run_id must be a run ID")
    result = {
        "stage": stage,
        "experiment_id": experiment,
        "run_id": str(run_id),
        "run_attempt": integer(run_attempt, "run_attempt", high=100000),
        "cluster_user": user,
        "cluster_ip": address,
        "cluster_name": identifier(payload.get("cluster_name", ""), "cluster_name", required=stage == "p2" or dispatch),
        "config_base64": config_value(payload.get("config_base64", ""), stage == "p2" or dispatch),
        "minimum_ready_nodes": integer(payload.get("minimum_ready_nodes", 1), "minimum_ready_nodes", high=1000),
        "readiness_timeout_s": integer(payload.get("readiness_timeout_s", 900), "readiness_timeout_s"),
        "upstream_run_id": upstream,
    }

    upstream_attempt = payload.get("upstream_run_attempt", "")
    if bool(upstream) != bool(upstream_attempt):
        raise ValueError("upstream run and attempt must be supplied together")
    result["upstream_run_attempt"] = integer(upstream_attempt, "upstream_run_attempt", high=100000) if upstream else None
    if stage == "p1":
        ref = payload.get("free5gc_ref", "metrics")
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", ref) or ".." in ref or "//" in ref or ref.endswith("/"):
            raise ValueError("invalid free5gc_ref")
        result.update({"dispatch_free5gc": dispatch, "free5gc_ref": ref, "terraform": terraform_inputs(payload)})
    return result


def terraform_inputs(payload):
    argument = payload.get("terraform_variables", "")
    config = payload.get("config")
    if argument and config is not None:
        raise ValueError("provide either terraform_variables or config, not both")
    if config is not None:
        if not isinstance(config, dict) or not config:
            raise ValueError("config must be a nonempty object")
        json.dumps(config, allow_nan=False)
        return {"mode": "json", "value": {"config": config}}
    if not isinstance(argument, str) or not argument.strip():
        raise ValueError("explicit Terraform inputs are required")
    return {"mode": "hcl", "value": argument}


def literal_address(value):
    if not isinstance(value, str) or "%" in value:
        raise ValueError("cluster IP must be literal and must not include an IPv6 scope")
    ipaddress.ip_address(value)
    return value
