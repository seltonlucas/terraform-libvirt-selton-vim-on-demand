# ODIM readiness gate and workflow dispatch

This change concerns code and operation. It does not modify the manuscript or demonstrate new performance gains. Tests use synthetic fixtures, fake executables and simulated HTTP responses; they do not deploy anything in the laboratory.

## Readiness criteria

Successful Terraform provisioning and an assigned IP do not mean the VIM can already receive Free5GC. Before handoff, Pipeline 1 checks the selected endpoint for:

1. SSH access through the configured proxy.
2. A reachable Kubernetes `/readyz` API with the expected successful response.
3. The configured minimum number of nodes, all registered nodes reporting `Ready=True`, and at least one Ready node not marked unschedulable.
4. Three consecutive successful observations. The default polling interval is 5 seconds; an unsuccessful observation resets the consecutive count.

The default deadline is 900 seconds, measured with a monotonic clock. SSH queries are bounded, and their local process groups are terminated on timeout. Timeout returns exit code 124 and does not authorize dispatch. Invalid configuration also fails closed.

Pipeline 2 repeats the gate before creating OSM resources. This reduces the gap between the first observation and deployment, but it cannot guarantee permanent availability afterward. Cluster readiness is not proof of UE registration, a PDU session or working 5G traffic.

## Safe activation

- The code is provided on a review branch. No infrastructure was changed to validate it.
- `dispatch_free5gc` defaults to `false` to avoid duplicating an existing external dispatcher.
- First make the P2 version containing the new inputs and gate available. Then point P1 `free5gc_ref` to that version.
- Enable `dispatch_free5gc=true` only after reviewing the configuration and disabling duplicate dispatch in the external controller.
- Changing the Terraform fork does not automatically update the original organization repository where historical runs were found. Select the execution repository explicitly.

## CI secrets

The existing proxy, QEMU-host and OSM secrets remain necessary. Additional configuration is:

| Purpose | Configuration |
|---|---|
| P1 and P2 SSH gate | `CLUSTER_PASSWORD` or `CLUSTER_SSH_KEY` in each repository running the corresponding pipeline |
| Image login user | `cluster_user`, default `packer`; probe commands require non-interactive sudo |
| Automatic P1-to-P2 dispatch | `FREE5GC_DISPATCH_TOKEN`, authorized to create workflow_dispatch in the target repository |

Prefer a dedicated token or GitHub App installation with the minimum necessary access. Fine-grained dispatch authorization requires Actions write permission in the target repository. The personal token used by the agent for commits was not copied into CI secrets. Do not assume the source repository GITHUB_TOKEN is authorized to another repository.

The optional key is written only on the runner with restricted permissions. The gate passes the password through SSHPASS, not a process argument. The kubeconfig is retrieved as JSON, kept at mode 0600 and excluded from metrics artifacts.

### TLS trust for the new monitor

The NS monitor validates the HTTPS certificate using the remote osm host trust store. For a private CA, set repository variable `OSM_CA_FILE` to an absolute path to a CA file already present on that host. This is a path, not certificate contents. Relative paths and control characters are rejected, with no insecure fallback. This change did not install a CA in the laboratory.

Legacy POST requests still use `curl -k`: this change does not resolve TLS security for the entire pipeline. Review those requests before use outside a controlled environment. The new curl monitor validates the HTTPS endpoint before sending its credential; the SSH transport also relies on the configured host-key trust policy.

P2 has a 65-minute job limit to accommodate individual wait deadlines, the legacy sleep and setup/finalization margin. Tests check this budget; it is not a measured laboratory duration.

Failure reports preserve the experiment, producer run, attempt and upstream identifiers from the current context without reusing another run report. If dispatch POST returns an ID but confirmation fails, the outcome remains unknown and retains the ID and ref for reconciliation. The disabled path records the choice without giving that step a token.

Inputs and targets containing IPv6 scopes are rejected, not repaired. Shell summaries read metadata through Bash environment expansion; YAML env fields retain the appropriate Actions mapping.

## Event contract

Both workflows accept workflow_dispatch and repository_dispatch with event type `ondemand`. For repository_dispatch, use structured client_payload fields; command strings in `args` are not interpreted as shell code.

In P1, preserve the existing HCL argument in `terraform_variables`, or provide the provisioning `config` object in client_payload. Do not provide both. HCL is passed as one Terraform argument without a shell; the object is written to a private JSON var-file. Empty inputs do not silently authorize default provisioning.

Add these control fields to the existing payload:

```json
{
  "experiment_id": "ensaio-001",
  "cluster_name": "ensaio-001",
  "target_cluster_ip": "192.0.2.10",
  "dispatch_free5gc": false,
  "free5gc_ref": "ci/readiness-gated-dispatch",
  "minimum_ready_nodes": 1,
  "readiness_timeout_s": 900,
  "cluster_user": "packer"
}
```

This address is for documentation, not the laboratory. The fragment does not replace the Terraform config or Free5GC config_base64. When automatic dispatch is enabled, cluster_name and config_base64 are required. If Terraform outputs multiple endpoints, target_cluster_ip must select exactly one; the first IP is never chosen silently.

The automatic sender targets deploy.yaml with an explicit ref and carries experiment_id, upstream_run_id and upstream_run_attempt. P2 preserves the reference to the P1 attempt in its reports.

## Evidence and dispatch outcomes

Handoff evidence must belong to the same experiment, run and attempt, contain three valid observations and be recent. The sender validates the target before POST. The API is pinned to `2026-03-10`; the response containing the run ID is followed by a read-back of that run to verify branch, event and experiment identity.

There is no blind POST retry. If a response is lost or read-back cannot confirm creation, reconcile the unknown outcome before resending. This does not implement exactly-once delivery or lifecycle-wide OSM/Terraform idempotency.

| State | Interpretation |
|---|---|
| readiness checking without completion | Incomplete execution; do not treat it as ready |
| readiness ready | The cluster passed the observed gate |
| readiness timeout/failed | Handoff was not authorized |
| dispatch disabled | The new sender did not attempt dispatch |
| dispatch accepted | The P2 run was created and read back; this does not mean Free5GC is functional |
| dispatch blocked/rejected | No authorized dispatch was confirmed |
| dispatch unknown | Reconcile the remote run; do not resend automatically |

Artifacts `readiness-<run_id>-<run_attempt>` contain readiness and handoff JSON. They exclude private context, config_base64, kubeconfig and tokens. Reports identify the target IP and node counts; sanitize identifiers before scientific publication. Retention depends on repository policy, so export required evidence before it expires.

## OSM state monitor

The monitor uses GET JSON from `/osm/nslcm/v1/ns_instances_content/{id}`, the read endpoint used by the official v16 client. It does not tokenize the YAML output of `osm ns-show --literal`. It validates the exact ID, top-level fields and absence of duplicate JSON keys.

Only `nsState=READY`, `currentOperation=IDLE`, `operational-status=running` and `config-status=configured` together are accepted. Configuration failure, BROKEN, DEGRADED and termination are not success even if another field still says running. Recognized progress states keep polling; unsupported formats fail closed. Compatibility with the installed version still requires validation against a sanitized laboratory record.

Queries are read-only and bounded. They carry bearer headers through SSH/curl stdin rather than process arguments. `metrics/ns-readiness.json` preserves P1 correlation without publishing the full NS response. Timeout returns 124; other errors also return nonzero. NS ID creation is distinguished from OSM lifecycle completion, which is still not proof of UE-to-DN traffic.

Set minimum_ready_nodes to match the expected topology: default 1 does not require future, unregistered nodes to have joined. The gate does not measure workload capacity or prove CoreDNS functionality. Legacy POSTs and shared remote temporary payload names still require a separate concurrency/idempotency review before parallel trials.

Reference contract inspected in official v16.0.0 source:
- https://osm.etsi.org/gitweb/?p=osm/osmclient.git;a=blob_plain;f=osmclient/sol005/ns.py;hb=refs/tags/v16.0.0
- https://osm.etsi.org/gitweb/?p=osm/LCM.git;a=blob_plain;f=osm_lcm/ns.py;hb=refs/tags/v16.0.0

## Local verification: WSL/Docker only

From the checkout root:

```bash
python3 -m unittest discover -s tests -v
```

The guard-tests workflow runs offline tests only: no Terraform apply/destroy, real SSH or real dispatch. Laboratory queries and OSM/Terraform operations run only when the operational workflow is explicitly triggered.

State-backend configuration, apply-failure recovery and OSM POST idempotency still require separate review. This change does not automatically replace existing state or the laboratory recovery policy. Pod listing is inspection, not a functional 5G connectivity test.

## Implementation references

- Kubernetes API health endpoints: https://kubernetes.io/docs/reference/using-api/health-checks/
- GitHub workflow dispatch API: https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
- GitHub repository_dispatch: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#repository_dispatch
