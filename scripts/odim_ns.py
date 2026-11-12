# OSM v16 NS record contract; this is not a functional 5G traffic probe.
import json
import math
import os
import re
import shlex
import subprocess
import time
from .odim_guard import run_bounded
import argparse
from datetime import datetime, timezone
import sys
from .odim_ci import write_json

FIELDS = ("nsState", "currentOperation", "operational-status", "config-status")

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def assess_ns(raw, ns_id):
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or data.get("_id") != ns_id or any(not isinstance(data.get(key), str) for key in FIELDS):
        raise ValueError("invalid NS record")
    states = {key: data[key] for key in FIELDS}
    if states["nsState"] in {"BROKEN", "DEGRADED", "TERMINATING"} or states["operational-status"] == "failed" or states["config-status"] == "failed":
        return {"state": "failed"}
    if tuple(states[key] for key in FIELDS) == ("READY", "IDLE", "running", "configured"):
        return {"state": "ready", **states}
    if states["nsState"] in {"NOT_INSTANTIATED", "BUILDING", "READY"} and states["currentOperation"] in {"IDLE", "INSTANTIATING"} and states["operational-status"] in {"init", "running"} and states["config-status"] in {"init", "configuring", "configured"}:
        return {"state": "pending", **states}
    raise ValueError("unsupported NS state")




def monitor(ns_id, hostname, token, timeout=900, interval=10, runner=run_bounded, clock=time.monotonic, sleep=time.sleep):
    if not isinstance(ns_id, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", ns_id):
        raise ValueError("NS instance ID must be a literal UUID")
    if not isinstance(hostname, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", hostname):
        raise ValueError("invalid OSM hostname")
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n\x00"):
        raise ValueError("OSM token missing or invalid")
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in (timeout, interval)):
        raise ValueError("positive finite timing required")
    url = "https://" + hostname + "/osm/nslcm/v1/ns_instances_content/" + ns_id
    ca_file = os.environ.get("OSM_CA_FILE", "")
    if ca_file and (not ca_file.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in ca_file)):
        raise ValueError("OSM_CA_FILE must be an absolute remote CA path without controls")
    ca_option = "--cacert " + shlex.quote(ca_file) + " " if ca_file else ""
    remote = "curl --fail --silent --show-error --connect-timeout 5 --max-time 10 --header @- " + ca_option + shlex.quote(url)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "osm", remote]
    headers = "Authorization: Bearer " + token + "\nAccept: application/json\n"
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SSH_AUTH_SOCK") if key in os.environ}
    start = clock()
    deadline = start + timeout
    observations = []
    status = "timeout"
    while clock() < deadline:
        try:
            response = runner(argv, timeout=min(15, deadline - clock()), input=headers, text=True, env=environment)
            verdict = assess_ns(response.stdout, ns_id) if response.returncode == 0 else {"state": "pending", "reason": "query_unavailable"}
        except subprocess.TimeoutExpired:
            verdict = {"state": "pending", "reason": "query_timeout"}
        except (ValueError, OSError):
            verdict = {"state": "failed", "reason": "invalid_or_unavailable_response"}
        observations.append({"elapsed_s": clock() - start, **verdict})
        if clock() >= deadline:
            break
        if verdict["state"] in ("ready", "failed"):
            status = verdict["state"]
            break
        sleep(min(interval, max(0, deadline - clock())))
    return {
        "status": status,
        "ready": status == "ready",
        "elapsed_s": clock() - start,
        "observations": observations,
    }




def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only bounded OSM NS readiness monitor")
    parser.add_argument("--context", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--id", dest="ns_id", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--interval", type=float, default=10)
    args = parser.parse_args(argv)
    base = {
        "schema_version": 1,
        "phase": "ns_readiness",
        "status": "checking",
        "ready": False,
    }
    try:
        with open(args.context, encoding="utf-8") as stream:
            context = json.load(stream)
        if context.get("stage") != "p2":
            raise ValueError("NS readiness belongs to P2")
        base.update({
            "experiment_id": context["experiment_id"],
            "producer_run_id": context["run_id"],
            "producer_run_attempt": context["run_attempt"],
            "upstream_run_id": context.get("upstream_run_id", ""),
            "upstream_run_attempt": context.get("upstream_run_attempt"),
        })
        write_json(args.report, base)
        result = monitor(args.ns_id, args.hostname, os.environ.get("OSM_TOKEN", ""), timeout=args.timeout, interval=args.interval)
        base.update(result)
        base["ns_instance_id"] = args.ns_id
        code = 0 if result["ready"] else (124 if result["status"] == "timeout" else 1)
    except (OSError, KeyError, TypeError, ValueError):
        base.update({"status": "failed", "reason": "invalid_configuration_or_response", "ready": False})
        code = 2
    base["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(args.report, base)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("ready_status=" + ("true" if base["ready"] else "false") + "\n")
            stream.write("ns_monitor_duration=" + str(base.get("elapsed_s", 0)) + "\n")
    print(json.dumps({"phase": "ns_readiness", "status": base["status"], "ready": base["ready"]}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
