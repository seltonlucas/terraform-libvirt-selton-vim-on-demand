# Host preparation, image factory and benchmark boundaries

## Scope of this increment

The future server and its operating system are not defined yet. This increment therefore supplies a portable Linux preflight, not an automatic package installer. It does not install software, change users or groups, create a bridge, enable services, create pools/networks/VMs, run Packer builds or require an existing Kubernetes cluster. It is not wired into the timed P1/P2 workflows.

All documentation is maintained in English. Operational conversation may use Portuguese. The manuscript is not modified by these documents.

## Keep three roles distinct

| Role | What the audit checks | What it does not imply |
|---|---|---|
| Hypervisor | Linux x86_64, a KVM character device, qemu-img and virsh, and the pre-existing br0 Linux bridge used by the pinned module | VM creation permissions, DHCP/bridge connectivity, storage capacity or a ready Kubernetes cluster |
| Image builder | Linux x86_64, direct read/write access to KVM, Packer, the exact kvm executable requested by the template, qemu-img, genisoimage, Ansible/Galaxy and SSH clients | A successful image build, compatible plugin API, a valid guest kernel/module or clean cloned identities |
| Controller/runner | Stable Terraform >= 1.9.0, Git, SSH/scp, jq and genisoimage | An initialized provider/module cache, working proxy credentials, or access to the hypervisor |

Install Packer and Ansible on the image builder, not automatically on every hypervisor. Terraform runs on the controller/CI runner; it does not have to be installed on the hypervisor if that role is separate. The module creates and attaches cloud-init ISOs, but its user_data assignment is commented out; creating those ISOs still needs the tool on the Terraform runner. Password-based cluster probes additionally need sshpass; the controller audit does not validate that optional authentication mode.

The image-builder audit lists installed Packer binaries for the two required HashiCorp plugins and checks for a 1.x installation. The official listing command ignores the plugin API version. Therefore a passed listing is not a compatibility or template-validation result. No packer init or dependency download is performed.

## Read-only commands

Run the relevant command locally on the intended Linux machine, as the user that will perform that role. Running only as root can hide access problems of the real operator.

```bash
python3 -m scripts.odim_host_preflight --role hypervisor
python3 -m scripts.odim_host_preflight --role hypervisor --probe-libvirt
python3 -m scripts.odim_host_preflight --role image-builder
python3 -m scripts.odim_host_preflight --role controller
```

The CLI prints a JSON report and exits 0 when the selected checks pass, 1 when prerequisites fail, and 2 for invalid arguments. There is deliberately no install/apply option. Reports describe host prerequisites only; they are not new deployment-time samples or claims of laboratory readiness.

The optional libvirt probe queries only the local qemu:///system URI using virsh --readonly --no-pkttyagent. It does not open an arbitrary remote URI or test management write permission. A connection can activate a socket-managed daemon, so perform it during preparation rather than treating it as a guaranteed zero-impact operation immediately before an otherwise cold trial.

Commands are bounded and receive a minimal environment without GitHub/SSH password variables. Reports retain parsed version tokens and fixed failure reasons, not raw command stderr or private configuration. Version tokens do not replace a full OS package/build manifest.

## Host network and storage contract

The selected Kubernetes module is pinned to 0.0.2. Its groundwork dependency creates directory storage pools and libvirt network wrappers, but its bridge-mode network points at an already existing br0. Merely installing libvirt is not sufficient. The checker verifies bridge presence only, not link state, DHCP, routing, MTU or external connectivity.

Do not automatically reconfigure the management NIC or create/replace br0 over SSH: that can interrupt access and change the network used by the experiment. Do not require an old Kubernetes cluster or previously created experiment pools to exist before provisioning. Review bridge, storage paths, user authorization and capacity against the chosen server before a controlled smoke test.

The current provider connects to the system libvirt instance through the qemu SSH alias. Existing SSH trust settings are legacy configuration, not a recommended security baseline; review host-key verification and access before using a new server.

## What the inspected Packer repository actually contains

Inspected source: seltonlucas/packer_free5gc at b1d69d74255012c554c3bbc1bff23ae6ef2510b0. It was cloned and inspected only; no image was built and its source was not modified.

- qemu-ubuntu.pkr.hcl requests KVM acceleration, the kvm binary and host CPU exposure. The default VM has 8192 MB RAM and a 20G qcow2 disk.
- Its base image comes from the mutable Ubuntu Focal current cloud-image location, with a checksum file from that same current location. This validates a download against that file, not a permanently pinned image revision.
- QEMU and Ansible plugin constraints are ~> 1. The Ansible Galaxy collection and gtp5g Git revision are not pinned to an immutable revision.
- Galaxy requirements declare only community.general. The playbook uses ansible.builtin.sysctl; verify its resolution to ansible.posix.sysctl for the selected Ansible version because ansible.posix is not declared there. Collection/module resolution is outside this binary audit. Do not infer compatibility from finding the ansible-playbook executable.
- The playbook installs MicroK8s from channel 1.26, enables storage/community/DNS/Multus/ingress, changes networking settings and builds/installs gtp5g. These are image contents, not tools that must all be installed on the physical hypervisor.
- The playbook refers to kernel headers and a GRUB selection for 5.4.0-162-generic while also installing linux-image-generic. Verify the running guest kernel, installed image/headers, gtp5g build/load compatibility and actual next-boot selection; the source alone does not establish that they match.
- Image cleanup/update-disable provisioning is commented out. Validate cloud-init state, machine identity, SSH host keys, Kubernetes identity/certificates and intended topology before treating clones as independent VIMs. Do not assume a multi-node join process from the number of VMs alone.
- The template contains a default password and password-enabled image access. Replace laboratory defaults appropriately; this document does not repeat the credential value.

The cloud-init-wait script masks an error result, so a successful wrapper exit is not evidence that cloud-init succeeded. The source contains separate seed-file and QEMU builds. Ensure generated seed files exist before the QEMU build; this audit does not execute or validate their build ordering.

## The Packer-to-Terraform handoff must be explicit

With the template default vm_name, the expected output path is output-ubuntu-20.04-x86_64/ubuntu-20.04-x86_64.qcow2. This is a source-derived expected path, not an observed artifact.

Each Terraform config entry supplies img. The current default config points to Ubuntu Jammy current cloud images, not that Packer artifact. This does not prove what an external controller or historical run actually supplied; capture the effective per-run input rather than inferring it from defaults.

Provider 0.8.3 interprets a local image source relative to the machine running Terraform, or accepts an HTTP(S) source. A file existing only on the hypervisor is not automatically a local file on a GitHub-hosted runner. Select a stable image URI accessible to the controller, or explicitly design and review a different image-distribution contract. This change does not alter img defaults or upload images.

Record the Packer commit, effective variables, plugin versions, base-image identity, final qcow2 SHA-256, guest kernel/gtp5g/MicroK8s versions and the exact artifact URI used by every approach. Do not publish credentials or image contents in measurement artifacts. Hashing images can warm filesystem caches; do it in a controlled preparation step and keep the cache policy consistent across conditions.

## Preparation is not automatically neutral to results

Define the timing boundary before collecting data:

1. P0: host installation/configuration and offline image preparation, measured separately when relevant.
2. P1: the existing VIM workflow through observed Kubernetes readiness, including its normal CI/setup/wait costs.
3. P2: orchestration through the declared lifecycle/functional endpoint. OSM READY alone is not UE/PDU/traffic success.

P0 may be outside a conditional P1/P2 comparison only if every approach starts from the same declared, prepared host and the same final image. If the claim is deployment from an unprepared server, excluding P0 would understate the end-to-end cost. Report that scope explicitly instead of assuming bootstrap has no effect.

Keep host/guest versions, CPU/memory allocation, storage and bridge policy, image digests and cold/warm cache treatment equal across ODIM, sequential scripts and saved-configuration manual baselines. Installing newer components, baking additional work into an image, starting daemons or pre-pulling workload images changes the initial state even if performed before the timer. Do not retime historical runs to pretend those conditions were controlled.

## Later bootstrap implementation

Once the server OS/version and network/storage/access policy are selected, implement a separate idempotent, explicitly authorized bootstrap for that platform. It should use trusted package sources, record versions, expose a dry-run/plan, and require an explicit apply action for mutations. Membership changes, bridge/NIC changes, pool creation and performance tuning must not be silent side effects of checking prerequisites.

Until then, a failed preflight means the selected prerequisite checks did not pass; it is not evidence of a failed cluster deployment, nor an instruction to modify the machine automatically.

## Primary references

- Packer source snapshot: https://github.com/seltonlucas/packer_free5gc/tree/b1d69d74255012c554c3bbc1bff23ae6ef2510b0
- Pinned Kubernetes module: https://raw.githubusercontent.com/descomplicando-terraform/terraform-libvirt-selton-kubernetes/0.0.2/main.tf
- Pinned groundwork bridge/pool definitions: https://raw.githubusercontent.com/descomplicando-terraform/terraform-libvirt-selton-groundwork/0.0.2/main.tf
- Provider 0.8.3 image-source semantics: https://raw.githubusercontent.com/dmacvicar/terraform-provider-libvirt/v0.8.3/website/docs/r/volume.html.markdown
- Read-only virsh option: https://libvirt.org/manpages/virsh.html
- Packer QEMU builder: https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu
- Packer Ansible provisioner: https://developer.hashicorp.com/packer/integrations/hashicorp/ansible/latest/components/provisioner/ansible
- Packer installed-plugin listing: https://developer.hashicorp.com/packer/docs/commands/plugins/installed

- Ansible sysctl collection requirements: https://raw.githubusercontent.com/ansible-collections/ansible.posix/main/plugins/modules/sysctl.py
- Ansible core 2.17 module redirects: https://raw.githubusercontent.com/ansible/ansible/stable-2.17/lib/ansible/config/ansible_builtin_runtime.yml
