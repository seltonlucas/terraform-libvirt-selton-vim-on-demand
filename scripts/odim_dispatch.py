# A ready cluster is a precondition for a handoff, not proof of a deployed NS.
from datetime import datetime, timezone
import json
from .odim_context import config_value, identifier, literal_address
import time
import urllib.error
import urllib.request

TARGET_REPOSITORY = "seltonlucas/free5gc-osm"
API_VERSION = "2026-03-10"


def _build_payload(context, report, now=None):
    if context.get("stage") != "p1" or context.get("dispatch_free5gc") is not True:
        raise ValueError("automatic dispatch is not enabled")
    if report.get("status") != "ready" or report.get("ready") is not True:
        raise ValueError("cluster is not ready")
    if report.get("experiment_id") != context.get("experiment_id") or report.get("producer_run_id") != context.get("run_id") or report.get("producer_run_attempt") != context.get("run_attempt"):
        raise ValueError("readiness evidence belongs to another request or attempt")
    observed = datetime.fromisoformat(report["observed_at_utc"].replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("readiness timestamp must include timezone")
    age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    if not -5 <= age <= 120:
        raise ValueError("readiness evidence is stale")
    if report.get("consecutive_ready", 0) < 3:
        raise ValueError("stable readiness has not been observed")
    observations = report.get("observations", [])
    if not isinstance(observations, list) or len(observations) < 3:
        raise ValueError("missing readiness observations")
    for item in observations[-3:]:
        if not isinstance(item, dict) or any(type(item.get(key)) is not int for key in ["ready_node_count", "observed_node_count", "schedulable_ready_count"]):
            raise ValueError("invalid readiness counts")
        if item.get("ready") is not True or item.get("api_ready") is not True or item.get("ready_node_count", 0) < context["minimum_ready_nodes"] or item.get("ready_node_count") != item.get("observed_node_count") or item.get("schedulable_ready_count", 0) < 1:
            raise ValueError("readiness observations are incomplete")
    target = report["target"]
    literal_address(target["address"])
    if context.get("cluster_ip") and context["cluster_ip"] != target["address"]:
        raise ValueError("target differs from the requested endpoint")
    if target["name"] != context["cluster_name"]:
        raise ValueError("target cluster name changed")
    inputs = {
        "cluster_ip": target["address"],
        "cluster_name": identifier(target["name"], "cluster_name"),
        "config_base64": config_value(context["config_base64"], True),
        "experiment_id": identifier(context["experiment_id"], "experiment_id"),
        "minimum_ready_nodes": str(context["minimum_ready_nodes"]),
        "readiness_timeout_s": str(context["readiness_timeout_s"]),
        "cluster_user": context["cluster_user"],
        "upstream_run_id": context["run_id"],
        "upstream_run_attempt": str(context["run_attempt"]),
    }
    payload = {"ref": context["free5gc_ref"], "inputs": inputs}
    if len(json.dumps(payload).encode()) > 65535:
        raise ValueError("dispatch payload is too large")
    return payload


def build_payload(context, report, now=None):
    try:
        return _build_payload(context, report, now)
    except (KeyError, TypeError, AttributeError, OverflowError):
        raise ValueError("malformed readiness evidence") from None




class DispatchUncertain(RuntimeError):
    def __init__(self, message, workflow_run_id=None, target_ref=None):
        super().__init__(message)
        self.workflow_run_id = workflow_run_id
        self.target_repository = TARGET_REPOSITORY
        self.target_ref = target_ref


class DispatchRejected(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_dispatch(payload, token, sender=None, sleep=time.sleep):
    if not isinstance(token, str) or not token:
        raise ValueError("ODIM_DISPATCH_TOKEN is required")
    sender = sender or urllib.request.build_opener(NoRedirect()).open
    base = "https://api.github.com/repos/" + TARGET_REPOSITORY
    headers = {
        "User-Agent": "ODIM-readiness-handoff",
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": API_VERSION,
    }
    request = urllib.request.Request(base + "/actions/workflows/deploy.yaml/dispatches", data=json.dumps(payload).encode(), headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with sender(request, timeout=20) as response:
            if response.status != 200:
                raise DispatchUncertain("dispatch response has no verifiable run ID")
            data = json.loads(response.read())
        if not isinstance(data, dict):
            raise DispatchUncertain("dispatch response is not an object")
        run_id = data.get("workflow_run_id")
        if type(run_id) is not int or run_id < 1:
            raise DispatchUncertain("dispatch response has no valid run ID")
    except urllib.error.HTTPError as error:
        if 400 <= error.code < 500 and error.code != 408:
            raise DispatchRejected("dispatch rejected: http_" + str(error.code)) from None
        raise DispatchUncertain("dispatch outcome unknown; reconcile before retry") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise DispatchUncertain("dispatch outcome unknown; reconcile before retry") from None
    for attempt in range(3):
        try:
            request = urllib.request.Request(base + "/actions/runs/" + str(run_id), headers=headers)
            with sender(request, timeout=10) as response:
                if response.status != 200:
                    raise ValueError("unexpected run response")
                run = json.loads(response.read())
            if not isinstance(run, dict):
                raise ValueError("run response is not an object")
            if run.get("id") != run_id or run.get("head_branch") != payload["ref"] or run.get("event") != "workflow_dispatch" or run.get("display_title") != "ODIM-" + payload["inputs"]["experiment_id"]:
                raise DispatchUncertain("created run identity could not be verified", workflow_run_id=run_id, target_ref=payload["ref"])
            return {
                "status": "accepted",
                "workflow_run_id": run_id,
                "read_back_verified": True,
                "target_repository": TARGET_REPOSITORY,
                "target_ref": payload["ref"],
            }
        except DispatchUncertain:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            if attempt < 2:
                sleep(1)
    raise DispatchUncertain("run was accepted but read-back failed; do not resend automatically", workflow_run_id=run_id, target_ref=payload["ref"])
