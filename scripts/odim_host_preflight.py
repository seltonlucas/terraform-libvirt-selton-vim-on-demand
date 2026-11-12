# Read-only prerequisite audit; never a Kubernetes or 5G readiness assertion.
import json
import os
import platform
import re
import shutil
import stat
import subprocess
from pathlib import Path
from .odim_guard import run_bounded
import argparse
from datetime import datetime, timezone

TOOLS = {
    "hypervisor": {"qemu-img": ["--version"], "virsh": ["--version"]},
    "controller": {"terraform": ["version", "-json"], "git": ["--version"], "ssh": ["-V"], "scp": None, "jq": ["--version"], "genisoimage": ["-version"]},
    "image-builder": {"packer": ["version"], "qemu-img": ["--version"], "kvm": ["--version"], "genisoimage": ["-version"], "ansible-playbook": ["--version"], "ansible-galaxy": ["--version"], "ssh": ["-V"], "scp": None},
}
PATTERNS = {
    "packer": r"Packer v([0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z._-]+)?)",
    "kvm": r"QEMU emulator version\s+([0-9]+(?:\.[0-9]+)+)",
    "genisoimage": r"genisoimage\s+([0-9]+(?:\.[0-9]+)+)",
    "ansible-playbook": r"ansible-playbook\s+(?:\[core\s+)?([0-9]+(?:\.[0-9]+)+)",
    "ansible-galaxy": r"ansible-galaxy\s+(?:\[core\s+)?([0-9]+(?:\.[0-9]+)+)",
    "virsh": r"^\s*([0-9]+(?:\.[0-9]+)+)",
    "qemu-img": r"qemu-img version\s+([0-9]+(?:\.[0-9]+)+)",
    "git": r"git version\s+([0-9]+(?:\.[0-9]+)+)",
    "ssh": r"OpenSSH_([0-9]+(?:\.[0-9]+)+(?:p[0-9]+)?)",
    "jq": r"jq-([0-9]+(?:\.[0-9]+)+)",
}


def _run(argv, runner):
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "PACKER_PLUGIN_PATH", "XDG_CONFIG_HOME") if key in os.environ}
    environment.update({"LC_ALL": "C", "LANG": "C", "CHECKPOINT_DISABLE": "1", "PACKER_NO_COLOR": "1"})
    return runner(argv, timeout=5, text=True, env=environment)


def _tool(name, args, which, runner):
    result = {"id": "tool:" + name, "status": "failed", "version": None}
    if not which(name):
        return {**result, "reason": "not_in_path"}
    if args is None:
        return {**result, "status": "passed", "reason": "executable_available_no_version_probe"}
    try:
        response = _run([name, *args], runner)
        if response.returncode != 0:
            return {**result, "reason": "version_command_failed"}
        if name == "terraform":
            data = json.loads(response.stdout)
            version = data.get("terraform_version") if isinstance(data, dict) else None
            valid = isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is not None
            if not valid or tuple(map(int, version.split("."))) < (1, 9, 0):
                return {**result, "reason": "terraform_stable_version_below_1_9_0_or_unrecognized"}
        else:
            match = re.search(PATTERNS[name], response.stdout or response.stderr or "")
            if not match:
                return {**result, "reason": "version_unrecognized"}
            version = match.group(1)
        return {**result, "status": "passed", "version": version, "reason": "version_observed"}
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return {**result, "reason": "version_command_unavailable_or_timed_out"}


def _plugins(runner):
    try:
        response = _run(["packer", "plugins", "installed"], runner)
        versions = {}
        for name in ("qemu", "ansible"):
            pattern = r".*/github\.com/hashicorp/" + name + r"/packer-plugin-" + name + r"_v(1\.[0-9]+(?:\.[0-9]+)?)_[^/]+"
            matches = [re.fullmatch(pattern, line.strip()) for line in (response.stdout or "").splitlines()]
            versions[name] = sorted({match.group(1) for match in matches if match})
        valid = response.returncode == 0 and all(versions.values())
        return {"id": "packer:required_plugin_binaries", "status": "passed" if valid else "failed", "versions": versions, "api_compatibility_checked": False}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"id": "packer:required_plugin_binaries", "status": "failed", "reason": "plugin_listing_unavailable_or_timed_out", "api_compatibility_checked": False}


def audit(role, which=shutil.which, runner=run_bounded, system=None, machine=None, kvm_state=None, probe_libvirt=False, bridge_state=None):
    if role not in TOOLS:
        raise ValueError("unsupported host role")
    if probe_libvirt and role != "hypervisor":
        raise ValueError("libvirt probe belongs to the hypervisor role")
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    checks = [{"id": "host:linux", "status": "passed" if system == "Linux" else "failed"}]
    if role in ("hypervisor", "image-builder"):
        if kvm_state is None:
            path = Path("/dev/kvm")
            try:
                present = stat.S_ISCHR(path.stat().st_mode)
            except OSError:
                present = False
            kvm_state = {"present": present, "rw_access": present and os.access(path, os.R_OK | os.W_OK)}
        checks += [
            {"id": "host:image_architecture", "status": "passed" if machine == "x86_64" else "failed"},
            {"id": "host:kvm_device", "status": "passed" if kvm_state["present"] else "failed", "direct_user_rw_access": kvm_state["rw_access"]},
        ]
    if role == "hypervisor":
        bridge_exists = Path("/sys/class/net/br0/bridge").is_dir() if bridge_state is None else bridge_state
        checks.append({"id": "host:br0", "status": "passed" if bridge_exists else "failed", "reason": "bridge_present" if bridge_exists else "required_bridge_missing"})
    if role == "image-builder":
        checks.append({"id": "host:direct_kvm_access", "status": "passed" if kvm_state["rw_access"] else "failed"})
    if system == "Linux":
        checks.extend(_tool(name, args, which, runner) for name, args in sorted(TOOLS[role].items()))
        if role == "image-builder":
            packer_ok = any(item["id"] == "tool:packer" and item["status"] == "passed" for item in checks)
            checks.append(_plugins(runner) if packer_ok else {"id": "packer:required_plugin_binaries", "status": "failed", "reason": "packer_unavailable"})
    connection_checked = False
    if probe_libvirt:
        connection = {"id": "libvirt:read_only_connection", "status": "failed", "reason": "virsh_unavailable_or_platform_unsupported"}
        virsh_ok = system == "Linux" and any(item["id"] == "tool:virsh" and item["status"] == "passed" for item in checks)
        if virsh_ok:
            connection_checked = True
            try:
                response = _run(["virsh", "--readonly", "--no-pkttyagent", "--connect", "qemu:///system", "uri"], runner)
                valid = response.returncode == 0 and response.stdout.strip() == "qemu:///system"
                connection.update({"status": "passed" if valid else "failed", "reason": "read_only_connection_confirmed" if valid else "local_connection_failed"})
            except (OSError, ValueError, subprocess.TimeoutExpired):
                connection["reason"] = "local_connection_unavailable_or_timed_out"
        checks.append(connection)
    return {
        "schema_version": 1, "phase": "host_preflight", "role": role,
        "status": "passed" if all(item["status"] == "passed" for item in checks) else "failed",
        "checks": checks,
        "scope": {"cluster_readiness_checked": False, "guest_image_validated": False, "libvirt_write_access_tested": False, "libvirt_read_only_connection_checked": connection_checked},
        "environment": {"system": system, "machine": machine, "wsl_kernel": "microsoft" in platform.release().lower()},
    }




def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only Linux prerequisite audit; no installation, VM creation or Kubernetes check")
    parser.add_argument("--role", choices=sorted(TOOLS), default="hypervisor")
    parser.add_argument("--probe-libvirt", action="store_true", help="also query the local qemu:///system URI through a read-only connection")
    args = parser.parse_args(argv)
    if args.probe_libvirt and args.role != "hypervisor":
        parser.error("--probe-libvirt requires --role hypervisor")
    report = audit(args.role, probe_libvirt=args.probe_libvirt)
    report["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
