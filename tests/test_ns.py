import json
import unittest
try:
    from scripts import odim_ns as ns
except ImportError:
    ns = None

# All responses in these tests are synthetic, not laboratory measurements.
NS_ID = "00000000-0000-4000-8000-000000000001"

def record(**changes):
    value = {"_id": NS_ID, "nsState": "READY", "currentOperation": "IDLE", "operational-status": "running", "config-status": "configured"}
    value.update(changes)
    return value

class NSStateTests(unittest.TestCase):
    def test_complete_top_level_state_is_required_for_readiness(self):
        self.assertIsNotNone(ns, "structured OSM monitor missing")
        self.assertEqual(ns.assess_ns(json.dumps(record()), NS_ID)["state"], "ready")


    def test_progress_does_not_count_as_ready(self):
        for state, operation in [("BUILDING", "INSTANTIATING"), ("NOT_INSTANTIATED", "IDLE")]:
            value = record(nsState=state, currentOperation=operation, **{"operational-status": "init", "config-status": "init"})
            try:
                self.assertEqual(ns.assess_ns(json.dumps(value), NS_ID)["state"], "pending")
            except ValueError:
                self.fail("valid in-progress NS was rejected")


    def test_configuration_failure_is_not_hidden_by_running(self):
        value = record(**{"config-status": "failed"})
        try:
            self.assertEqual(ns.assess_ns(json.dumps(value), NS_ID)["state"], "failed")
        except ValueError:
            self.fail("known failure must be classified")
        for value in [record(nsState="BROKEN"), record(nsState="DEGRADED"), record(nsState="TERMINATING")]:
            self.assertEqual(ns.assess_ns(json.dumps(value), NS_ID)["state"], "failed")


    def test_malformed_foreign_nested_and_duplicate_records_are_rejected(self):
        nested = {"_id": NS_ID, "nested": record()}
        duplicate = json.dumps(record())[:-1] + ", \"nsState\": \"READY\"}"
        values = ["not json", "[]", json.dumps(record(_id="other")), json.dumps(nested), json.dumps(record(nsState=True)), duplicate]
        for value in values:
            with self.subTest(kind=value[:20]):
                with self.assertRaises(ValueError):
                    ns.assess_ns(value, NS_ID)


class Clock:
    def __init__(self):
        self.value = 0
    def now(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds

class NSMonitorTests(unittest.TestCase):
    def test_monitor_rejects_nonabsolute_or_control_character_ca_paths(self):
        import os
        from unittest.mock import Mock, patch
        for ca in ("relative/roots.pem", "~/roots.pem", "/etc/ca\n.pem", "/etc/ca\r.pem"):
            with self.subTest(ca=ca), patch.dict(os.environ, {"OSM_CA_FILE": ca}):
                runner = Mock(return_value=__import__("subprocess").CompletedProcess([], 0, json.dumps(record()), ""))
                with self.assertRaises(ValueError):
                    ns.monitor(NS_ID, "osm.example.invalid", "TEST", runner=runner)
                runner.assert_not_called()

    def test_monitor_verifies_tls_and_quotes_the_optional_remote_ca(self):
        import os
        import shlex
        import subprocess
        from unittest.mock import patch
        ca_path = "/etc/odim CA/operator's roots.pem"
        for ca in ("", ca_path):
            with self.subTest(ca=ca), patch.dict(os.environ, {"OSM_CA_FILE": ca}):
                calls = []
                def runner(argv, **kwargs):
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, json.dumps(record()), "")
                self.assertTrue(ns.monitor(NS_ID, "osm.example.invalid", "TEST", runner=runner)["ready"])
                remote = shlex.split(calls[0][-1])
                self.assertNotIn("-k", remote)
                self.assertNotIn("--insecure", remote)
                if ca:
                    self.assertEqual(remote[remote.index("--cacert") + 1], ca_path)
                    self.assertEqual(remote.count(ca_path), 1)
                else:
                    self.assertNotIn("--cacert", remote)

    def test_pending_then_ready_uses_only_read_requests_and_private_headers(self):
        import subprocess
        self.assertTrue(hasattr(ns, "monitor"), "bounded NS monitor missing")
        clock = Clock()
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            value = record() if len(calls) == 2 else record(nsState="BUILDING", currentOperation="INSTANTIATING", **{"operational-status": "init", "config-status": "init"})
            return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")
        result = ns.monitor(NS_ID, "osm.example.invalid", "SYNTHETIC_TOKEN", runner=runner, clock=clock.now, sleep=clock.sleep, interval=1, timeout=3)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("SYNTHETIC_TOKEN", str(calls[0][0]))
        self.assertIn("Authorization: Bearer SYNTHETIC_TOKEN", calls[0][1]["input"])
        self.assertIn("ns_instances_content/" + NS_ID, calls[0][0][-1])
        self.assertNotIn("POST", calls[0][0][-1])


    def test_terminal_failure_and_expired_observation_never_succeed(self):
        import subprocess
        clock = Clock()
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps(record(nsState="BROKEN")), "")
        result = ns.monitor(NS_ID, "osm.example.invalid", "TEST", runner=runner, clock=clock.now, sleep=clock.sleep)
        self.assertEqual(result["status"], "failed")
        def late(argv, **kwargs):
            clock.value += 3
            return subprocess.CompletedProcess(argv, 0, json.dumps(record()), "")
        result = ns.monitor(NS_ID, "osm.example.invalid", "TEST", timeout=2, runner=late, clock=clock.now, sleep=clock.sleep)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["ready"])

class NSCLITests(unittest.TestCase):
    def test_cli_timeout_is_nonzero_and_preserves_correlated_sanitized_report(self):
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        self.assertTrue(hasattr(ns, "main"), "NS CLI missing")
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            executable = directory / "ssh"
            value = record(nsState="BUILDING", currentOperation="INSTANTIATING", **{"operational-status": "init", "config-status": "init"})
            executable.write_text("#!" + sys.executable + "\nprint(" + repr(json.dumps(value)) + ")\n")
            executable.chmod(0o700)
            context = directory / "context.json"
            context.write_text(json.dumps({"stage": "p2", "experiment_id": "fixture", "run_id": "2", "run_attempt": 1, "upstream_run_id": "1", "upstream_run_attempt": 1}))
            report = directory / "report.json"
            environment = {**os.environ, "PATH": str(directory) + ":" + os.environ["PATH"], "OSM_TOKEN": "SYNTHETIC_TEST_TOKEN", "GITHUB_OUTPUT": str(directory / "outputs")}
            command = [sys.executable, "-m", "scripts.odim_ns", "--context", str(context), "--report", str(report), "--id", NS_ID, "--hostname", "osm.example.invalid", "--timeout", "0.08", "--interval", "0.02"]
            response = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(response.returncode, 124, response.stderr)
            data = json.loads(report.read_text())
            self.assertEqual(data["status"], "timeout")
            self.assertEqual(data["upstream_run_id"], "1")
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("SYNTHETIC_TEST_TOKEN", report.read_text() + response.stdout + response.stderr)
            self.assertIn("ready_status=false", (directory / "outputs").read_text())

if __name__ == "__main__":
    unittest.main()
