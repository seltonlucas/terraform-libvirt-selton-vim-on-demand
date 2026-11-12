import base64
import unittest
try:
    from scripts import odim_context as context
except ImportError:
    context = None


def p2_inputs():
    return {"cluster_ip": "192.0.2.10", "cluster_name": "cluster-a", "config_base64": base64.b64encode(b"nsName: test\n").decode()}


class ContextTests(unittest.TestCase):
    def test_manual_p2_event_is_normalized_without_changing_values(self):
        self.assertIsNotNone(context, "event normalizer missing")
        source = p2_inputs()
        result = context.normalize_event("p2", "workflow_dispatch", {"inputs": source}, run_id="123", run_attempt="1")
        self.assertEqual(result["cluster_ip"], source["cluster_ip"])
        self.assertEqual(result["config_base64"], source["config_base64"])
        self.assertEqual(result["experiment_id"], "odim-123")
        self.assertEqual(result["minimum_ready_nodes"], 1)


    def test_repository_dispatch_uses_the_same_validated_fields(self):
        source = p2_inputs()
        source["experiment_id"] = "experiment-fixed"
        result = context.normalize_event("p2", "repository_dispatch", {"client_payload": source}, run_id="456", run_attempt="1")
        self.assertEqual(result["experiment_id"], "experiment-fixed")
        self.assertEqual(result["cluster_ip"], "192.0.2.10")

    def test_invalid_addresses_and_configuration_are_rejected(self):
        for key,value in [("cluster_ip", "192.0.2.10;untrusted"), ("cluster_name", "bad name"), ("config_base64", "not-base64"), ("minimum_ready_nodes", "0"), ("readiness_timeout_s", "nan")]:
            with self.subTest(key=key):
                source = p2_inputs()
                source[key] = value
                with self.assertRaises(ValueError):
                    context.normalize_event("p2", "workflow_dispatch", {"inputs": source}, run_id="123", run_attempt="1")



    def test_repository_p1_config_is_a_json_var_file_not_shell_text(self):
        self.assertTrue(hasattr(context, "terraform_inputs"), "Terraform event input normalizer missing")
        source = {"config": {"lab": {"nodes": ["lab-1"], "memory": 4096}}}
        parsed = context.terraform_inputs(source)
        self.assertEqual(parsed, {"mode": "json", "value": source})
        result = context.normalize_event("p1", "repository_dispatch", {"client_payload": source}, run_id="123", run_attempt="1")
        self.assertEqual(result["terraform"], parsed)
        self.assertFalse(result["dispatch_free5gc"])



    def test_false_string_never_enables_dispatch(self):
        result = context.normalize_event("p1", "workflow_dispatch", {"inputs": {"terraform_variables": "config={}", "dispatch_free5gc": "false"}}, run_id="123", run_attempt="1")
        self.assertFalse(result["dispatch_free5gc"])

    def test_dispatch_requires_configuration_and_explicit_cluster_name(self):
        source = {"terraform_variables": "config={}", "dispatch_free5gc": True}
        with self.assertRaises(ValueError):
            context.normalize_event("p1", "workflow_dispatch", {"inputs": source}, run_id="123", run_attempt="1")

    def test_missing_or_ambiguous_terraform_configuration_is_rejected(self):
        for value in [{}, {"terraform_variables": "x=1", "config": {"a": {}}}, {"config": {}}]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    context.terraform_inputs(value)



    def test_upstream_attempt_is_not_lost_in_p2(self):
        source = p2_inputs()
        source.update({"upstream_run_id": "123", "upstream_run_attempt": "2"})
        result = context.normalize_event("p2", "workflow_dispatch", {"inputs": source}, run_id="456", run_attempt="1")
        self.assertEqual(result.get("upstream_run_attempt"), 2)




class ScopedAddressRejectionTests(unittest.TestCase):
    def test_scopes_are_rejected_at_every_cluster_address_boundary(self):
        import base64
        from scripts import odim_context, odim_guard, odim_dispatch
        from tests.test_dispatch import context_fixture, report_fixture
        scopes = ["fe80::1%eth0", "fe80::1%" + chr(36) + "(printf ODIM_PROBE)"]
        for address in scopes:
            with self.subTest(address=address):
                payload = {"cluster_ip": address, "cluster_name": "lab", "config_base64": base64.b64encode(b"nsName: test").decode()}
                with self.assertRaises(ValueError):
                    odim_context.normalize_event("p2", "workflow_dispatch", {"inputs": payload}, "1", "1")
                with self.assertRaises(ValueError):
                    odim_context.normalize_event("p1", "workflow_dispatch", {"inputs": {"target_cluster_ip": address, "terraform_variables": "config={}"}}, "1", "1")
                with self.assertRaises(ValueError):
                    odim_guard.SSHProbe(address, password="SYNTHETIC")
                with self.assertRaises(ValueError):
                    odim_guard.select_target({"lab": {"domain": [{"address": address}]}})
                with self.assertRaises(ValueError):
                    odim_guard.rewrite_kubeconfig({}, address)
                context, report = context_fixture(), report_fixture()
                context["cluster_ip"], report["target"]["address"] = address, address
                with self.assertRaises(ValueError):
                    odim_dispatch.build_payload(context, report)

if __name__ == "__main__":
    unittest.main()
