import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def test_context_command_writes_private_data_without_logging_it(self):
        self.assertTrue((ROOT / "scripts/odim_ci.py").exists(), "CI command adapter missing")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = {"inputs": {"cluster_ip": "192.0.2.10", "cluster_name": "lab", "config_base64": base64.b64encode(b"nsName: test\n").decode()}}
            event = folder / "event.json"
            event.write_text(json.dumps(source))
            context = folder / "context.json"
            env = dict(os.environ, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_EVENT_PATH=str(event), GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="1", GITHUB_ENV=str(folder / "github.env"))
            p = subprocess.run([sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "context", "p2"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(context.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(context.read_text())["experiment_id"], "odim-123")
            self.assertNotIn(source["inputs"]["config_base64"], p.stdout + p.stderr)
            self.assertNotIn(source["inputs"]["config_base64"], (folder / "github.env").read_text())


    def test_gate_exercises_real_cli_with_fake_ssh_only(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary = folder / "bin"
            binary.mkdir()
            from tests.test_guard import node
            ssh = binary / "ssh"
            source = "#!/usr/bin/env python3\nimport json,sys\nif \"--raw=/readyz\" in sys.argv[-1]: print(\"ok\")\nelse: print(json.dumps(" + repr({"items": [node()]}) + "))\n"
            ssh.write_text(source)
            ssh.chmod(0o700)
            sshpass = binary / "sshpass"
            sshpass.write_text("#!/usr/bin/env python3\nimport os,sys\nos.execvp(sys.argv[2],sys.argv[2:])\n")
            sshpass.chmod(0o700)
            context = folder / "context.json"
            context.write_text(json.dumps({"stage": "p2", "experiment_id": "synthetic-cli-test", "run_id": "123", "run_attempt": 1, "cluster_ip": "192.0.2.10", "cluster_name": "lab", "cluster_user": "packer", "minimum_ready_nodes": 1, "readiness_timeout_s": 3, "upstream_run_id": "", "upstream_run_attempt": None}))
            report = folder / "readiness.json"
            env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ["PATH"], SSHPASS="SYNTHETIC_TEST_PASSWORD", GITHUB_ENV=str(folder / "github.env"), GITHUB_OUTPUT=str(folder / "output"))
            env.pop("ODIM_SSH_IDENTITY", None)
            p = subprocess.run([sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "gate", "--report", str(report), "--interval", "0.01"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            data = json.loads(report.read_text())
            self.assertEqual(data["status"], "ready")
            self.assertEqual(len(data["observations"]), 3)
            self.assertNotIn("SYNTHETIC_TEST_PASSWORD", report.read_text())
            self.assertIn("ready=true", (folder / "output").read_text())



    def test_config_decoder_preserves_private_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            context = folder / "context.json"
            content = b"nsName: synthetic-test\n"
            context.write_text(json.dumps({"config_base64": base64.b64encode(content).decode()}))
            output = folder / "config.yaml"
            p = subprocess.run([sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "config", "--output", str(output)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(output.read_bytes(), content)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)



    def test_plan_passes_hcl_as_one_argument_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary = folder / "bin"
            binary.mkdir()
            terraform = binary / "terraform"
            capture = folder / "argv.json"
            terraform.write_text("#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ[\"ODIM_TEST_CAPTURE\"]).write_text(json.dumps(sys.argv[1:]))\n")
            terraform.chmod(0o700)
            marker = folder / "must-not-exist"
            argument = "config=" + chr(36) + "(touch " + str(marker) + ")"
            context = folder / "context.json"
            context.write_text(json.dumps({"stage": "p1", "terraform": {"mode": "hcl", "value": argument}}))
            env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ["PATH"], ODIM_TEST_CAPTURE=str(capture))
            env.pop("ODIM_SSH_IDENTITY", None)
            p = subprocess.run([sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "plan"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("-var="+argument, json.loads(capture.read_text()))
            self.assertFalse(marker.exists())



    def test_dispatch_is_blocked_before_network_when_gate_failed(self):
        from tests.test_dispatch import context_fixture
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            context = folder / "context.json"
            context.write_text(json.dumps(context_fixture()))
            readiness = folder / "ready.json"
            readiness.write_text(json.dumps({"status": "timeout", "ready": False}))
            report = folder / "dispatch.json"
            env = dict(os.environ)
            env.pop("ODIM_DISPATCH_TOKEN", None)
            p = subprocess.run([sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "dispatch", "--readiness-report", str(readiness), "--report", str(report)], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
            self.assertTrue(report.exists(), "blocked dispatch must emit evidence")
            self.assertEqual(json.loads(report.read_text())["status"], "blocked")



    def test_gate_timeout_is_a_failed_step_with_a_retained_report(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary = folder / "bin"
            binary.mkdir()
            for name in ("ssh", "sshpass"):
                fake = binary / name
                fake.write_text("#!/usr/bin/env python3\nraise SystemExit(1)\n")
                fake.chmod(0o700)
            context = folder / "context.json"
            context.write_text(json.dumps({"stage": "p2", "experiment_id": "synthetic-timeout-test", "run_id": "123", "run_attempt": 1, "cluster_ip": "192.0.2.10", "cluster_name": "lab", "cluster_user": "packer", "minimum_ready_nodes": 1, "readiness_timeout_s": 1}))
            report = folder / "readiness.json"
            env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ["PATH"], SSHPASS="SYNTHETIC_TEST_PASSWORD", GITHUB_OUTPUT=str(folder / "output"))
            env.pop("ODIM_SSH_IDENTITY", None)
            p = subprocess.run([sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "gate", "--report", str(report), "--interval", "0.02"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 124, p.stderr)
            self.assertEqual(json.loads(report.read_text())["status"], "timeout")
            self.assertIn("ready=false", (folder / "output").read_text())



    def test_kubeconfig_command_keeps_key_material_out_of_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary = folder / "bin"
            binary.mkdir()
            source = {"current-context": "lab", "contexts": [{"name": "lab", "context": {"cluster": "lab"}}], "clusters": [{"name": "lab", "cluster": {"server": "https://127.0.0.1:16443"}}], "users": [{"name": "lab", "user": {"client-key-data": "SYNTHETIC_TEST_KEY"}}]}
            for name in ("ssh", "sshpass"):
                fake = binary / name
                fake.write_text("#!/usr/bin/env python3\nimport json\nprint(json.dumps(" + repr(source) + "))\n")
                fake.chmod(0o700)
            context = folder / "context.json"
            context.write_text(json.dumps({"cluster_ip": "192.0.2.10", "cluster_user": "packer", "minimum_ready_nodes": 1, "readiness_timeout_s": 3}))
            output = folder / "kubeconfig"
            env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ["PATH"], SSHPASS="SYNTHETIC_TEST_PASSWORD")
            env.pop("ODIM_SSH_IDENTITY", None)
            p = subprocess.run([sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "kubeconfig", "--output", str(output)], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(json.loads(output.read_text())["clusters"][0]["cluster"]["server"], "https://192.0.2.10:16443")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("SYNTHETIC_TEST_KEY", p.stdout + p.stderr)



    def test_cli_ssh_fixtures_are_safe_with_an_inherited_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            identity = folder / "synthetic-key"
            identity.write_text("SYNTHETIC_KEY_NOT_VALID_FOR_AUTH")
            identity.chmod(0o600)
            sentinel = folder / "ssh"
            marker = folder / "unexpected-ssh"
            sentinel.write_text("#!" + sys.executable + "\nimport os\nfrom pathlib import Path\nPath(os.environ[\"ODIM_FORBIDDEN_SSH_MARKER\"]).write_text(\"UNEXPECTED_SSH\")\nraise SystemExit(98)\n")
            sentinel.chmod(0o700)
            env = dict(os.environ, PATH=str(folder) + os.pathsep + os.environ["PATH"], ODIM_SSH_IDENTITY=str(identity), ODIM_FORBIDDEN_SSH_MARKER=str(marker))
            targets = ["test_gate_exercises_real_cli_with_fake_ssh_only", "test_gate_timeout_is_a_failed_step_with_a_retained_report", "test_kubeconfig_command_keeps_key_material_out_of_logs"]
            response = subprocess.run([sys.executable, "-m", "unittest", "-q", *["tests.test_cli.CLITests." + name for name in targets]], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(response.returncode, 0, "offline child suite must ignore inherited identity: " + response.stderr)
            self.assertFalse(marker.exists(), "a fixture escaped to the external SSH sentinel")


    def test_gate_configuration_errors_preserve_run_and_upstream_identity(self):
        for stage in ("p1", "p2"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                context = folder / "context.json"
                data = {"stage": stage, "experiment_id": "synthetic-error", "run_id": "101", "run_attempt": 2, "upstream_run_id": "77", "upstream_run_attempt": 3, "cluster_user": "packer", "cluster_ip": "" if stage == "p1" else "192.0.2.10", "cluster_name": "lab", "minimum_ready_nodes": 1, "readiness_timeout_s": 1}
                context.write_text(json.dumps(data))
                report = folder / "report.json"
                report.write_text(json.dumps({"producer_run_id": "stale-other-run"}))
                command = [sys.executable, "-m", "scripts.odim_ci", "--context", str(context), "gate", "--report", str(report)]
                if stage == "p1":
                    output = folder / "terraform.json"
                    output.write_text(json.dumps({"lab": {"domain": [{"address": "192.0.2.10"}, {"address": "192.0.2.11"}]}}))
                    command += ["--terraform-output", str(output)]
                env = dict(os.environ)
                for key in ("ODIM_SSH_IDENTITY", "SSHPASS"):
                    env.pop(key, None)
                response = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(response.returncode, 2)
                result = json.loads(report.read_text())
                expected = {"phase": "cluster_readiness", "producer_run_id": "101", "producer_run_attempt": 2, "upstream_run_id": "77", "upstream_run_attempt": 3, "experiment_id": "synthetic-error", "ready": False}
                self.assertEqual({key: result.get(key) for key in expected}, expected, "failure report lost correlation")


    def test_unknown_dispatch_retains_the_known_run_for_reconciliation(self):
        import urllib.error
        from unittest.mock import patch
        from scripts import odim_ci, odim_dispatch
        from tests.test_dispatch import context_fixture, report_fixture
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps({"workflow_run_id": 9001}).encode()
        methods = []
        def sender(request, timeout):
            methods.append(request.get_method())
            if request.get_method() == "POST":
                return Response()
            raise urllib.error.URLError("synthetic read-back failure")
        def dispatch(payload, token):
            return odim_dispatch.post_dispatch(payload, token, sender=sender, sleep=lambda seconds: None)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            context, readiness, report = (folder / name for name in ("context.json", "readiness.json", "dispatch.json"))
            context.write_text(json.dumps(context_fixture()))
            readiness.write_text(json.dumps(report_fixture()))
            with patch.dict(os.environ, {"ODIM_DISPATCH_TOKEN": "SYNTHETIC_TOKEN"}), patch.object(odim_ci, "post_dispatch", side_effect=dispatch), patch("sys.stdout", new_callable=io.StringIO) as captured_output:
                code = odim_ci.main(["--context", str(context), "dispatch", "--readiness-report", str(readiness), "--report", str(report)])
            self.assertNotEqual(code, 0)
            data = json.loads(report.read_text())
            self.assertEqual(data["status"], "unknown")
            self.assertEqual(data.get("workflow_run_id"), 9001, "known dispatch ID was lost")
            self.assertEqual(data["target_repository"], odim_dispatch.TARGET_REPOSITORY)
            self.assertEqual(data["target_ref"], "metrics")
            self.assertNotIn("SYNTHETIC_TOKEN", report.read_text())
            self.assertEqual(methods, ["POST", "GET", "GET", "GET"])


    def test_disabled_dispatch_records_optout_without_network_or_token(self):
        from unittest.mock import patch
        from scripts import odim_ci
        from tests.test_dispatch import context_fixture
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            data = context_fixture()
            data["dispatch_free5gc"] = False
            context = folder / "context.json"
            report = folder / "disabled.json"
            context.write_text(json.dumps(data))
            with patch.object(odim_ci, "post_dispatch", side_effect=AssertionError("network forbidden")) as post:
                code = odim_ci.main(["--context", str(context), "dispatch", "--readiness-report", str(folder / "absent.json"), "--report", str(report)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(report.read_text())["status"], "disabled")
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
