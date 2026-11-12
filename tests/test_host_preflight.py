# Synthetic prerequisite fixtures: no server or VM is contacted.
from pathlib import Path
import subprocess
import unittest
try:
    from scripts import odim_host_preflight as host
except ImportError:
    host = None

class HostPreflightTests(unittest.TestCase):
    def test_hypervisor_check_does_not_require_an_existing_cluster(self):
        self.assertIsNotNone(host, "host preflight missing")
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            value = "10.0.0" if argv[0] == "virsh" else "qemu-img version 8.2.2"
            return subprocess.CompletedProcess(argv, 0, value, "")
        result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=runner, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": False}, bridge_state=True)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["scope"]["cluster_readiness_checked"])
        self.assertEqual([row[0][0] for row in calls], ["qemu-img", "virsh"])
        self.assertFalse(any("kubectl" in str(row[0]) for row in calls))

    def test_missing_or_unavailable_tools_fail_without_raw_error_output(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 1, "", "SYNTHETIC_PRIVATE_ERROR")
        result = host.audit("hypervisor", which=lambda name: None if name == "qemu-img" else "/fake/virsh", runner=runner, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": True}, bridge_state=True)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("qemu-img", [row[0][0] for row in calls], "missing binary must not be executed")
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", str(result))
        self.assertTrue(all(row[1]["timeout"] <= 5 for row in calls))

    def test_hypervisor_reports_missing_kvm_and_wrong_architecture(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "10.0.0" if argv[0] == "virsh" else "qemu-img version 8.2.2", "")
        for system, machine, present in [("Linux", "x86_64", False), ("Linux", "aarch64", True), ("Windows", "AMD64", True)]:
            with self.subTest(system=system, machine=machine, present=present):
                result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=runner, system=system, machine=machine, kvm_state={"present": present, "rw_access": False}, bridge_state=True)
                self.assertEqual(result["status"], "failed", "inspected images require a Linux x86_64 KVM host")

    def test_tool_versions_are_recorded_and_unrecognized_output_fails(self):
        def good(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "10.0.0" if argv[0] == "virsh" else "qemu-img version 8.2.2", "")
        result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=good, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": False}, bridge_state=True)
        versions = {row["id"]: row.get("version") for row in result["checks"]}
        self.assertEqual(versions["tool:qemu-img"], "8.2.2", "version evidence missing")
        self.assertEqual(versions["tool:virsh"], "10.0.0")
        result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "unrecognized output", ""), system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": True}, bridge_state=True)
        self.assertEqual(result["status"], "failed")

    def test_controller_checks_terraform_constraint_without_needing_kvm(self):
        import json
        self.assertIn("controller", host.TOOLS, "controller role missing")
        for version, expected in [("1.8.5", "failed"), ("1.9.0", "passed"), ("1.10.2", "passed")]:
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                values = {"terraform": json.dumps({"terraform_version": version}), "git": "git version 2.47.0", "ssh": "OpenSSH_9.6p1", "jq": "jq-1.7.1", "genisoimage": "genisoimage 1.1.11"}
                return subprocess.CompletedProcess(argv, 0, "" if argv[0] == "ssh" else values[argv[0]], values[argv[0]] if argv[0] == "ssh" else "")
            result = host.audit("controller", which=lambda name: "/fake/" + name, runner=runner, system="Linux", machine="aarch64", kvm_state={"present": False, "rw_access": False}, bridge_state=True)
            self.assertEqual(result["status"], expected)
            self.assertNotIn("scp", [row[0] for row in calls])
            self.assertFalse(any(value in row for row in calls for value in ("init", "apply", "build", "install", "sudo")))

    def test_image_builder_checks_exact_binary_plugins_and_direct_kvm_access(self):
        self.assertIn("image-builder", host.TOOLS, "image-builder role missing")
        versions = {"packer": "Packer v1.11.0", "qemu-img": "qemu-img version 8.2.2", "kvm": "QEMU emulator version 8.2.2", "genisoimage": "genisoimage 1.1.11", "ansible-playbook": "ansible-playbook [core 2.17.1]", "ansible-galaxy": "ansible-galaxy [core 2.17.1]", "ssh": "OpenSSH_9.6p1"}
        plugin_text = "\n".join("/synthetic/github.com/hashicorp/" + name + "/packer-plugin-" + name + "_v1.1.0_x5.0_linux_amd64" for name in ("qemu", "ansible"))
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, plugin_text if argv[1:] == ["plugins", "installed"] else versions[argv[0]], "")
        for access, expected in ((True, "passed"), (False, "failed")):
            result = host.audit("image-builder", which=lambda name: "/fake/" + name, runner=runner, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": access}, bridge_state=True)
            self.assertEqual(result["status"], expected)
        self.assertIn(["packer", "plugins", "installed"], [row[0] for row in calls])
        self.assertTrue(all(row[1]["env"]["CHECKPOINT_DISABLE"] == "1" for row in calls))
        self.assertTrue(all("GITHUB_TOKEN" not in row[1]["env"] and "SSHPASS" not in row[1]["env"] for row in calls))
        self.assertFalse(any(value in row[0] for row in calls for value in ("init", "apply", "build", "install", "sudo")))

    def test_optional_libvirt_probe_is_local_read_only_and_noninteractive(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            if "--connect" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "SYNTHETIC_PRIVATE_ERROR")
            return subprocess.CompletedProcess(argv, 0, "10.0.0" if argv[0] == "virsh" else "qemu-img version 8.2.2", "")
        result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=runner, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": False}, probe_libvirt=True, bridge_state=True)
        self.assertEqual(result["status"], "failed", "requested libvirt probe was ignored")
        self.assertIn(["virsh", "--readonly", "--no-pkttyagent", "--connect", "qemu:///system", "uri"], calls)
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR", str(result))

    def test_cli_plugin_listing_decode_failure_is_sanitized_json(self):
        import json
        import sys
        script = """
import subprocess
from scripts import odim_host_preflight as host

def runner(argv, **kwargs):
    if argv == ["packer", "plugins", "installed"]:
        raise UnicodeDecodeError("utf-8", bytes([255]), 0, 1, "SYNTHETIC_PRIVATE_PLUGIN_ERROR")
    versions = {"packer": "Packer v1.11.0", "qemu-img": "qemu-img version 8.2.2", "kvm": "QEMU emulator version 8.2.2", "genisoimage": "genisoimage 1.1.11", "ansible-playbook": "ansible-playbook [core 2.17.1]", "ansible-galaxy": "ansible-galaxy [core 2.17.1]", "ssh": "OpenSSH_9.6p1"}
    return subprocess.CompletedProcess(argv, 0, versions[argv[0]], "")

audit = host.audit
host.audit = lambda role, **kwargs: audit(role, which=lambda name: "/synthetic/" + name, runner=runner, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": True}, bridge_state=True, **kwargs)
raise SystemExit(host.main(["--role", "image-builder"]))
"""
        response = subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
        self.assertEqual(response.returncode, 1)
        self.assertNotIn("SYNTHETIC_PRIVATE_PLUGIN_ERROR", response.stdout + response.stderr)
        self.assertEqual(response.stderr, "")
        report = json.loads(response.stdout)
        self.assertEqual(report["status"], "failed")
        check = next(row for row in report["checks"] if row["id"] == "packer:required_plugin_binaries")
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["reason"], "plugin_listing_unavailable_or_timed_out")
        self.assertFalse(check["api_compatibility_checked"])
        self.assertTrue(all(row["status"] == "passed" for row in report["checks"] if row is not check))

    def test_cli_libvirt_probe_decode_failure_is_sanitized_json(self):
        import json
        import sys
        script = """
import subprocess
from scripts import odim_host_preflight as host

def runner(argv, **kwargs):
    if argv == ["virsh", "--readonly", "--no-pkttyagent", "--connect", "qemu:///system", "uri"]:
        raise UnicodeDecodeError("utf-8", bytes([255]), 0, 1, "SYNTHETIC_PRIVATE_LIBVIRT_ERROR")
    return subprocess.CompletedProcess(argv, 0, "10.0.0" if argv[0] == "virsh" else "qemu-img version 8.2.2", "")

audit = host.audit
host.audit = lambda role, **kwargs: audit(role, which=lambda name: "/synthetic/" + name, runner=runner, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": True}, bridge_state=True, **kwargs)
raise SystemExit(host.main(["--role", "hypervisor", "--probe-libvirt"]))
"""
        response = subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5)
        self.assertEqual(response.returncode, 1)
        self.assertNotIn("SYNTHETIC_PRIVATE_LIBVIRT_ERROR", response.stdout + response.stderr)
        self.assertEqual(response.stderr, "")
        report = json.loads(response.stdout)
        self.assertEqual(report["status"], "failed")
        check = next(row for row in report["checks"] if row["id"] == "libvirt:read_only_connection")
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["reason"], "local_connection_unavailable_or_timed_out")
        self.assertTrue(report["scope"]["libvirt_read_only_connection_checked"])
        self.assertTrue(all(row["status"] == "passed" for row in report["checks"] if row is not check))

    def test_cli_prints_a_json_report_without_an_install_mode(self):
        import io
        import json
        from unittest.mock import patch
        self.assertTrue(hasattr(host, "main"), "preflight CLI missing")
        fixture = {"schema_version": 1, "phase": "host_preflight", "status": "failed", "scope": {"cluster_readiness_checked": False}}
        output = io.StringIO()
        with patch.object(host, "audit", return_value=fixture), patch("sys.stdout", output):
            self.assertEqual(host.main(["--role", "hypervisor"]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
            host.main(["--install"])
        self.assertEqual(error.exception.code, 2)

    def test_timeouts_and_unsupported_platform_do_not_trigger_mutating_actions(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=runner, system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": True}, probe_libvirt=True, bridge_state=True)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("--connect", [part for row in calls for part in row])
        calls.clear()
        result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=runner, system="Windows", machine="AMD64", kvm_state={"present": False, "rw_access": False}, bridge_state=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(calls, [])

    def test_plugin_shaped_ancestor_does_not_count_as_installed_qemu(self):
        output = "/synthetic/github.com/hashicorp/qemu/packer-plugin-qemu_v1.1.0_cache/github.com/hashicorp/ansible/packer-plugin-ansible_v1.1.0_x5.0_linux_amd64"
        result = host._plugins(lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""))
        self.assertEqual(result["status"], "failed", "a plugin-shaped ancestor is not an installed QEMU binary")
        self.assertEqual(result["versions"], {"qemu": [], "ansible": ["1.1.0"]})

    def test_builder_missing_or_wrong_major_plugins_fail_closed(self):
        for output in ("", "/synthetic/github.com/hashicorp/qemu/packer-plugin-qemu_v2.0.0_x5.0_linux_amd64", "/synthetic/github.com/other/qemu/packer-plugin-qemu_v1.1.0_x5.0_linux_amd64"):
            result = host._plugins(lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, output, ""))
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["api_compatibility_checked"])

    def test_hypervisor_requires_the_preexisting_br0_used_by_the_pinned_module(self):
        import inspect
        self.assertIn("bridge_state", inspect.signature(host.audit).parameters, "bridge prerequisite missing")
        result = host.audit("hypervisor", which=lambda name: "/fake/" + name, runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "10.0.0" if argv[0] == "virsh" else "qemu-img version 8.2.2", ""), system="Linux", machine="x86_64", kvm_state={"present": True, "rw_access": True}, bridge_state=False)
        self.assertEqual(result["status"], "failed")
        bridge = next(row for row in result["checks"] if row["id"] == "host:br0")
        self.assertEqual(bridge["status"], "failed")

    def test_controller_requires_the_cloudinit_iso_tool_on_the_runner(self):
        import json
        versions = {"terraform": json.dumps({"terraform_version": "1.9.0"}), "git": "git version 2.47.0", "ssh": "OpenSSH_9.6p1", "jq": "jq-1.7.1"}
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, versions[argv[0]], "")
        result = host.audit("controller", which=lambda name: None if name == "genisoimage" else "/fake/" + name, runner=runner, system="Linux", machine="aarch64")
        self.assertEqual(result["status"], "failed", "controller ISO dependency missing")
        check = next(row for row in result["checks"] if row["id"] == "tool:genisoimage")
        self.assertEqual(check["reason"], "not_in_path")
