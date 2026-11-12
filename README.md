# ODIM on-demand VIM provisioning

This repository contains the Terraform side of the ODIM IaC-to-MANO workflow. It provisions the VIM and checks Kubernetes readiness before an explicitly enabled Free5GC handoff. A host with libvirt/KVM must exist before P1 starts; an already running Kubernetes cluster is not a host prerequisite.

- Read [the readiness and handoff guide](docs/readiness-setup.md) before enabling dispatch.
- Use [the host/image preparation guide](docs/host-preparation.md) for the read-only prerequisite checker and benchmark boundaries.
- The image factory is maintained separately in seltonlucas/packer_free5gc. Its qcow2 output is not automatically selected by this repository: each config entry must explicitly identify the intended image.
- The current config defaults refer to Ubuntu Jammy cloud images, while the inspected Packer template builds an Ubuntu Focal image. Treat that as an image-selection boundary, not evidence that previous runs used either default.
- No deployment or image build is performed by the offline test workflow. The manuscript is maintained separately and is not changed by these operational documents.

## Current Terraform contract

The source files, not older generated documentation, define the interface.

<!-- BEGIN_TF_DOCS -->
### Requirements and providers

| Component | Declared constraint |
|---|---|
| Terraform | >= 1.9.0 |
| dmacvicar/libvirt | 0.8.3 |

### Modules

| Name | Source | Revision |
|---|---|---|
| kubernetes | git::ssh://git@github.com/descomplicando-terraform/terraform-libvirt-selton-kubernetes.git | 0.0.2 |

### Inputs

| Name | Type | Default |
|---|---|---|
| config | map(any) | See variables.tf: edge and cp example configurations |

Each config entry supplies img, libvirt_disk_path, memory, vcpus, nodes and pool. The example defaults are not a verified laboratory inventory. Workflow event handling requires explicit provisioning inputs rather than silently applying them.

### Outputs

| Name | Value |
|---|---|
| k8s | module.kubernetes |
<!-- END_TF_DOCS -->

## Local tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use synthetic fixtures and do not contact the laboratory. Existing provider SSH trust and state-recovery settings require operator review before real runs.
