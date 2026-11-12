from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowWiringTests(unittest.TestCase):
    def test_p2_monitor_passes_only_the_optional_explicit_ca_path(self):
        path = ROOT / ".github/workflows/deploy.yaml"
        if not path.exists():
            self.skipTest("P2 workflow not in this repository")
        monitor = path.read_text().split("id: ns-monitor", 1)[1].split("      - name:", 1)[0]
        self.assertIn("OSM_CA_FILE: " + chr(36) + "{{ vars.OSM_CA_FILE }}", monitor)

    def test_p1_dispatch_is_after_the_readiness_gate_and_explicitly_guarded(self):
        path = ROOT / ".github/workflows/ondemand.yaml"
        if not path.exists():
            self.skipTest("P1 workflow not in this repository")
        text = path.read_text()
        self.assertIn("id: cluster-ready", text)
        self.assertIn("scripts.odim_ci", text)
        self.assertLess(text.index("context p1"), text.index("Terraform Init"))
        self.assertLess(text.index("id: cluster-ready"), text.index("name: Dispatch Free5GC after readiness"))
        self.assertIn("steps.cluster-ready.outputs.ready", text)
        self.assertIn("ODIM_DISPATCH_FREE5GC", text)
        self.assertNotIn(chr(36) + "{{ inputs.terraform_variables }}", text)


    def test_p2_checks_cluster_before_osm_and_accepts_both_event_types(self):
        path = ROOT / ".github/workflows/deploy.yaml"
        if not path.exists():
            self.skipTest("P2 workflow not in this repository")
        text = path.read_text()
        self.assertIn("id: cluster-ready", text)
        self.assertIn("repository_dispatch:", text)
        self.assertLess(text.index("context p2"), text.index("Configure SSH"))
        self.assertLess(text.index("id: cluster-ready"), text.index("Retrieve OSM token"))
        self.assertNotIn(chr(36) + "{{ inputs.cluster_ip }}", text)
        self.assertNotIn(chr(36) + "{{ inputs.config_base64 }}", text)
        self.assertIn("metrics/cluster-readiness.json", text)



    def test_guard_checks_workflow_only_runs_offline_tests(self):
        path = ROOT / ".github/workflows/guard-tests.yaml"
        self.assertTrue(path.exists(), "offline CI test workflow missing")
        text = path.read_text()
        self.assertIn("unittest discover", text)
        for forbidden in ["terraform apply", "terraform destroy", "workflow run", "odim_ci dispatch"]:
            self.assertNotIn(forbidden, text)



    def test_workflows_do_not_use_the_unsupported_checkout_v3(self):
        for path in (ROOT / ".github/workflows").glob("*.yaml"):
            self.assertNotIn("actions/checkout@v3", path.read_text(), path.name)



    def test_p2_monitor_is_structured_and_uses_the_retrieved_token(self):
        p2 = ROOT / ".github/workflows/deploy.yaml"
        if not p2.exists():
            self.skipTest("P2 workflow not in this repository")
        text = p2.read_text()
        self.assertIn("scripts.odim_ns", text, "structured NS monitor not wired")
        self.assertIn("metrics/ns-readiness.json", text)
        self.assertNotIn("STATUS_WORD", text)
        headers = [line for line in text.splitlines() if "Authorization: Bearer" in line]
        self.assertTrue(headers and all("${OSM_TOKEN}" in line for line in headers), "OSM requests must use the newly retrieved token")


    def test_p2_job_budget_covers_configured_deadlines_and_finalization(self):
        import inspect
        import re
        from scripts import odim_context, odim_ns
        path = ROOT / ".github/workflows/deploy.yaml"
        if not path.exists():
            self.skipTest("P2 workflow not in this repository")
        text = path.read_text()
        job_seconds = int(re.search(r"timeout-minutes: ([0-9]+)", text).group(1)) * 60
        gate_maximum = inspect.signature(odim_context.integer).parameters["high"].default
        ns_timeout = inspect.signature(odim_ns.monitor).parameters["timeout"].default
        sleeps = sum(int(value) for value in re.findall(r"\bsleep ([0-9]+)", text))
        self.assertGreaterEqual(job_seconds, gate_maximum + ns_timeout + sleeps + 600, "job budget must include setup and evidence publication")


    def test_p1_optout_is_recorded_without_a_dispatch_secret(self):
        path = ROOT / ".github/workflows/ondemand.yaml"
        if not path.exists():
            self.skipTest("P1 workflow not in this repository")
        text = path.read_text()
        name = "name: Record disabled Free5GC handoff"
        self.assertIn(name, text, "disabled handoff needs an explicit report")
        step = text.split(name, 1)[1].split("      - name:", 1)[0]
        self.assertIn("ODIM_DISPATCH_FREE5GC", step)
        self.assertIn("dispatch --readiness-report", step)
        self.assertNotIn("ODIM_DISPATCH_TOKEN", step)
        self.assertNotIn("secrets.", step)


    def test_p2_summary_uses_environment_expansion_not_actions_interpolation(self):
        path = ROOT / ".github/workflows/deploy.yaml"
        if not path.exists():
            self.skipTest("P2 workflow not in this repository")
        summary = path.read_text().split("name: 📊 Performance Summary", 1)[1]
        self.assertNotIn(chr(36) + "{{ env.ODIM_CLUSTER_IP }}", summary)
        self.assertNotIn(chr(36) + "{{ env.ODIM_CLUSTER_NAME }}", summary)
        self.assertIn(chr(36) + "{ODIM_CLUSTER_IP}", summary)


    def test_p2_yaml_env_mapping_expands_cluster_metadata(self):
        path = ROOT / ".github/workflows/deploy.yaml"
        if not path.exists():
            self.skipTest("P2 workflow not in this repository")
        text = path.read_text()
        for name, source in (("CLUSTER_HOST", "ODIM_CLUSTER_IP"), ("VIM_NAME", "ODIM_CLUSTER_NAME"), ("CLUSTER_NAME", "ODIM_CLUSTER_NAME")):
            self.assertIn(name + ": " + chr(36) + "{{ env." + source + " }}", text, "YAML env values must use Actions interpolation, not shell syntax")


if __name__ == "__main__":
    unittest.main()
