# Shared ODIM cluster readiness checks. No third-party dependencies.
import math
import time
import json
import os
from pathlib import Path
import re
import subprocess
import signal
from .odim_context import identifier, literal_address
from urllib.parse import urlsplit, urlunsplit

def assess_nodes(document, minimum=1):
    if type(minimum) is not int or minimum < 1:
        raise ValueError("minimum must be a positive integer")
    result = {
        "ready": False,
        "ready_node_count": 0,
        "observed_node_count": 0,
        "schedulable_ready_count": 0,
        "reason": "invalid_nodes",
    }
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        return result
    items = document["items"]
    result["observed_node_count"] = len(items)
    names = set()
    for item in items:
        if not isinstance(item, dict):
            return result
        metadata, status, spec = item.get("metadata"), item.get("status"), item.get("spec", {})
        if not isinstance(metadata, dict) or not isinstance(status, dict) or not isinstance(spec, dict):
            return result
        name = metadata.get("name")
        if not isinstance(name, str) or not name or name in names:
            return result
        names.add(name)
        conditions = status.get("conditions")
        if not isinstance(conditions, list) or not all(isinstance(c, dict) for c in conditions):
            return result
        ready = [c for c in conditions if c.get("type") == "Ready"]
        if len(ready) != 1:
            return result
        if ready[0].get("status") == "True":
            result["ready_node_count"] += 1
            if spec.get("unschedulable", False) is False:
                result["schedulable_ready_count"] += 1
    result["reason"] = "nodes_not_ready"
    if len(items) < minimum or result["ready_node_count"] < minimum:
        result["reason"] = "not_enough_ready_nodes"
    elif result["ready_node_count"] == len(items) and result["schedulable_ready_count"] > 0:
        result["ready"], result["reason"] = True, "ready"
    elif result["ready_node_count"] == len(items):
        result["reason"] = "no_schedulable_nodes"
    return result




def wait_for_ready(probe, timeout=900, interval=5, consecutive=3, clock=time.monotonic, sleep=time.sleep):
    if not all(math.isfinite(x) and x > 0 for x in [timeout, interval]) or type(consecutive) is not int or consecutive < 1:
        raise ValueError("positive finite timing and observation count required")
    start = clock()
    deadline = start + timeout
    observations = []
    stable = 0
    while clock() < deadline:
        try:
            verdict = probe(max(0.001, deadline - clock()))
            if not isinstance(verdict, dict) or type(verdict.get("ready")) is not bool:
                verdict = {"ready": False, "reason": "invalid_probe_response"}
        except (OSError, ValueError, TimeoutError):
            verdict = {"ready": False, "reason": "probe_failed"}
        elapsed = clock() - start
        observations.append({**verdict, "elapsed_s": elapsed})
        stable = stable + 1 if verdict.get("ready") is True else 0
        if stable >= consecutive and clock() < deadline:
            return {
                "ready": True,
                "reason": "stable_ready",
                "elapsed_s": elapsed,
                "consecutive_ready": stable,
                "observations": observations,
            }
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    return {
        "ready": False,
        "reason": "timeout",
        "elapsed_s": clock() - start,
        "consecutive_ready": stable,
        "observations": observations,
    }




class SSHProbe:
    def __init__(self, address, user="packer", minimum=1, via="proxy", identity=None, password=None, runner=None):
        literal_address(address)
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("invalid SSH user")
        if via and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", via):
            raise ValueError("invalid jump alias")
        if type(minimum) is not int or minimum < 1:
            raise ValueError("invalid minimum node count")
        if not identity and not password:
            raise ValueError("cluster SSH credentials are required")
        self.environment = {k: os.environ[k] for k in ["PATH", "HOME", "LANG", "SSH_AUTH_SOCK"] if k in os.environ}
        self.command = ["ssh", "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1", "-o", "StrictHostKeyChecking=accept-new", "-o", "NumberOfPasswordPrompts=1"]
        if via:
            self.command += ["-J", via]
        if identity:
            if not Path(identity).is_file():
                raise ValueError("cluster SSH identity is missing")
            self.command += ["-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-i", identity]
        else:
            self.environment["SSHPASS"] = password
            self.command = ["sshpass", "-e", *self.command, "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no"]
        self.command += [user + "@" + address]
        self.minimum = minimum
        self.runner = runner or run_bounded

    def __call__(self, remaining):
        started = time.monotonic()
        result = {"ready": False, "api_ready": False, "reason": "ssh_unavailable"}
        command = "sudo -n microk8s.kubectl --request-timeout=5s get --raw=/readyz"
        try:
            response = self.runner([*self.command, command], capture_output=True, text=True, timeout=remaining, env=self.environment)
            if response.returncode != 0:
                return result
            if response.stdout.strip() != "ok":
                return {**result, "reason": "api_not_ready"}
            remaining -= time.monotonic() - started
            if remaining <= 0:
                return {**result, "reason": "probe_timeout"}
            command = "sudo -n microk8s.kubectl --request-timeout=5s get nodes -o json"
            response = self.runner([*self.command, command], capture_output=True, text=True, timeout=remaining, env=self.environment)
            if response.returncode != 0:
                return {**result, "api_ready": True, "reason": "nodes_unavailable"}
            verdict = assess_nodes(json.loads(response.stdout), self.minimum)
            return {**verdict, "api_ready": True}
        except subprocess.TimeoutExpired:
            return {**result, "reason": "probe_timeout"}
        except (OSError, ValueError):
            return {**result, "reason": "invalid_or_unavailable_response"}




def run_bounded(argv, timeout, capture_output=True, text=True, env=None, input=None):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, env=env, start_new_session=True)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)




def select_target(output, address="", name=""):
    if not isinstance(output, dict) or not output:
        raise ValueError("Terraform k8s output is missing")
    candidates = []
    seen = set()
    for group, data in output.items():
        if not isinstance(data, dict) or not isinstance(data.get("domain"), list):
            raise ValueError("unexpected Terraform module output")
        for domain in data["domain"]:
            if not isinstance(domain, dict) or not isinstance(domain.get("address"), str):
                raise ValueError("Terraform address is missing")
            current = domain["address"]
            literal_address(current)
            if current in seen:
                raise ValueError("duplicate Terraform addresses")
            seen.add(current)
            candidates.append({"address": current, "name": group})
    if address:
        literal_address(address)
        candidates = [item for item in candidates if item["address"] == address]
    if len(candidates) != 1:
        raise ValueError("select target_cluster_ip when Terraform creates multiple endpoints")
    selected = candidates[0]
    return {"address": selected["address"], "name": identifier(name or selected["name"], "cluster_name")}




def rewrite_kubeconfig(document, address):
    if not isinstance(address, str):
        raise ValueError("cluster address must be a literal IP")
    literal_address(address)
    result = json.loads(json.dumps(document, allow_nan=False))
    if not isinstance(result, dict) or not isinstance(result.get("contexts"), list) or not isinstance(result.get("clusters"), list):
        raise ValueError("invalid kubeconfig")
    context = [item for item in result["contexts"] if isinstance(item, dict) and item.get("name") == result.get("current-context")]
    if len(context) != 1 or not isinstance(context[0].get("context"), dict):
        raise ValueError("ambiguous kubeconfig context")
    cluster_name = context[0]["context"].get("cluster")
    clusters = [item for item in result["clusters"] if isinstance(item, dict) and item.get("name") == cluster_name]
    if len(clusters) != 1 or not isinstance(clusters[0].get("cluster"), dict):
        raise ValueError("ambiguous kubeconfig cluster")
    cluster = clusters[0]["cluster"]
    server = urlsplit(cluster.get("server", ""))
    if server.scheme != "https" or server.username or server.password or server.query or server.fragment or server.hostname not in ["127.0.0.1", "localhost", "::1", address]:
        raise ValueError("unexpected kubeconfig server")
    authority = "[" + address + "]" if ":" in address else address
    if server.port:
        authority += ":" + str(server.port)
    cluster["server"] = urlunsplit(("https", authority, server.path, "", ""))
    return result
