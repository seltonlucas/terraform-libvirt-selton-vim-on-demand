import unittest
try:
    from scripts import odim_guard as guard
except ImportError:
    guard = None


def node(name="node-a", state="True", unschedulable=False):
    return {"metadata": {"name": name}, "spec": {"unschedulable": unschedulable}, "status": {"conditions": [{"type": "Ready", "status": state}]}}


class NodeReadinessTests(unittest.TestCase):
    def test_ready_node_is_accepted(self):
        self.assertIsNotNone(guard, "readiness guard not implemented")
        result = guard.assess_nodes({"items": [node()]}, minimum=1)
        self.assertTrue(result["ready"])
        self.assertEqual(result["ready_node_count"], 1)


    def test_incomplete_or_unschedulable_clusters_are_rejected(self):
        cases = [[node(), node("node-b", "False")], [node(unschedulable=True)], [node(), node()]]
        for items in cases:
            with self.subTest(items=items):
                self.assertFalse(guard.assess_nodes({"items": items}, minimum=1)["ready"])



class FakeClock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class StabilityTests(unittest.TestCase):
    def test_three_consecutive_observations_are_required(self):
        self.assertTrue(hasattr(guard, "wait_for_ready"), "stable readiness wait missing")
        clock = FakeClock()
        states = iter([False, True, False, True, True, True])
        result = guard.wait_for_ready(lambda remaining: {"ready": next(states)}, timeout=20, interval=1, consecutive=3, clock=clock, sleep=clock.sleep)
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["observations"]), 6)
        self.assertEqual(result["consecutive_ready"], 3)



    def test_timeout_never_claims_readiness(self):
        clock = FakeClock()
        result = guard.wait_for_ready(lambda remaining: {"ready": False}, timeout=3, interval=1, consecutive=3, clock=clock, sleep=clock.sleep)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["elapsed_s"], 3)

    def test_malformed_nodes_are_not_ready(self):
        for value in [None, [], {}, {"items": []}, {"items": [None]}, {"items": [{"metadata": {"name": "n"}, "status": {"conditions": []}}]}]:
            with self.subTest(value=value):
                self.assertFalse(guard.assess_nodes(value)["ready"])

    def test_probe_errors_are_observations_not_success(self):
        clock = FakeClock()
        def probe(remaining):
            raise OSError("synthetic test transport failure")
        result = guard.wait_for_ready(probe, timeout=2, interval=1, clock=clock, sleep=clock.sleep)
        self.assertFalse(result["ready"])
        self.assertTrue(all(x["reason"] == "probe_failed" for x in result["observations"]))



class SSHProbeTests(unittest.TestCase):
    def test_remote_api_and_nodes_must_both_be_ready(self):
        self.assertTrue(hasattr(guard, "SSHProbe"), "bounded SSH probe missing")
        import json
        import subprocess
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            output = "ok\n" if len(calls) == 1 else json.dumps({"items": [node()]})
            return subprocess.CompletedProcess(argv, 0, output, "")
        probe = guard.SSHProbe("192.0.2.10", password="SYNTHETIC_TEST_PASSWORD", runner=runner)
        verdict = probe(10)
        self.assertTrue(verdict["ready"])
        self.assertTrue(verdict["api_ready"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("SYNTHETIC_TEST_PASSWORD" not in str(argv) for argv, _ in calls))
        self.assertIn("packer@192.0.2.10", calls[0][0])



    def test_api_failure_does_not_check_nodes_or_claim_ready(self):
        import subprocess
        calls = []
        def runner(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "not ready", "")
        verdict = guard.SSHProbe("192.0.2.10", password="TEST", runner=runner)(5)
        self.assertFalse(verdict["ready"])
        self.assertEqual(len(calls), 1)

    def test_malformed_node_json_is_not_ready(self):
        import subprocess
        replies = iter(["ok", "not-json"])
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, next(replies), "")
        self.assertFalse(guard.SSHProbe("192.0.2.10", password="TEST", runner=runner)(5)["ready"])

    def test_shell_fragments_cannot_be_an_address(self):
        with self.assertRaises(ValueError):
            guard.SSHProbe("192.0.2.10;untrusted", password="TEST")



    def test_key_authentication_does_not_use_sshpass(self):
        import json
        import subprocess
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / "synthetic-key"
            identity.write_text("SYNTHETIC_TEST_KEY")
            identity.chmod(0o600)
            calls = []
            def runner(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, "ok" if len(calls) == 1 else json.dumps({"items": [node()]}), "")
            verdict = guard.SSHProbe("192.0.2.10", identity=str(identity), runner=runner)(5)
            self.assertTrue(verdict["ready"])
            self.assertTrue(all(argv[0] == "ssh" for argv in calls))
            self.assertIn(str(identity), calls[0])



class ProcessDeadlineTests(unittest.TestCase):
    def test_timeout_terminates_the_probe_process_group(self):
        self.assertTrue(hasattr(guard, "run_bounded"), "bounded process-group runner missing")
        import os
        from pathlib import Path
        import subprocess
        import sys
        child = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,\"-c\",\"import time; time.sleep(60)\"]); print(p.pid,flush=True); time.sleep(60)"
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            guard.run_bounded([sys.executable, "-c", child], timeout=0.2, capture_output=True, text=True, env=dict(os.environ))
        pid = int(caught.exception.output.strip())
        stat = Path("/proc") / str(pid) / "stat"
        try:
            state = stat.read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            state = None
        self.assertIn(state, [None, "Z"])



    def test_bounded_runner_can_pass_private_stdin_without_argv_data(self):
        import inspect
        import sys
        self.assertIn("input", inspect.signature(guard.run_bounded).parameters, "private stdin missing")
        response = guard.run_bounded([sys.executable, "-c", "import sys; print(sys.stdin.read())"], timeout=2, input="SYNTHETIC_HEADER")
        self.assertEqual(response.stdout.strip(), "SYNTHETIC_HEADER")
        self.assertNotIn("SYNTHETIC_HEADER", response.args)


class TargetSelectionTests(unittest.TestCase):
    def test_multiple_vm_addresses_require_an_explicit_target(self):
        self.assertTrue(hasattr(guard, "select_target"), "Terraform target selector missing")
        output = {"lab": {"domain": [{"address": "192.0.2.10"}, {"address": "192.0.2.11"}]}}
        with self.assertRaises(ValueError):
            guard.select_target(output)
        result = guard.select_target(output, address="192.0.2.11", name="cluster-b")
        self.assertEqual(result, {"address": "192.0.2.11", "name": "cluster-b"})



    def test_one_valid_endpoint_is_unambiguous(self):
        output = {"lab": {"domain": [{"address": "192.0.2.10"}]}}
        self.assertEqual(guard.select_target(output)["name"], "lab")

    def test_unlisted_or_duplicate_targets_are_rejected(self):
        output = {"lab": {"domain": [{"address": "192.0.2.10"}]}}
        with self.assertRaises(ValueError):
            guard.select_target(output, address="192.0.2.99")
        output["other"] = output["lab"]
        with self.assertRaises(ValueError):
            guard.select_target(output, address="192.0.2.10")



class KubeconfigTests(unittest.TestCase):
    def test_only_the_selected_cluster_server_is_rebound_without_changing_credentials(self):
        self.assertTrue(hasattr(guard, "rewrite_kubeconfig"), "safe kubeconfig rebinding missing")
        source = {"current-context": "lab", "contexts": [{"name": "lab", "context": {"cluster": "lab"}}], "clusters": [{"name": "lab", "cluster": {"server": "https://127.0.0.1:16443", "certificate-authority-data": "SYNTHETIC_CERTIFICATE"}}], "users": [{"name": "lab", "user": {"client-key-data": "SYNTHETIC_TEST_KEY"}}]}
        result = guard.rewrite_kubeconfig(source, "2001:db8::10")
        self.assertEqual(result["clusters"][0]["cluster"]["server"], "https://[2001:db8::10]:16443")
        self.assertEqual(result["users"], source["users"])
        self.assertEqual(source["clusters"][0]["cluster"]["server"], "https://127.0.0.1:16443")



    def test_unrelated_kubeconfig_server_is_not_silently_repaired(self):
        source = {"current-context": "lab", "contexts": [{"name": "lab", "context": {"cluster": "lab"}}], "clusters": [{"name": "lab", "cluster": {"server": "https://192.0.2.99:16443"}}]}
        with self.assertRaises(ValueError):
            guard.rewrite_kubeconfig(source, "192.0.2.10")



if __name__ == "__main__":
    unittest.main()
