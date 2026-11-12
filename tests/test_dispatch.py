import base64
from datetime import datetime, timezone
import unittest
try:
    from scripts import odim_dispatch as dispatch
except ImportError:
    dispatch = None


def context_fixture():
    return {"stage": "p1", "experiment_id": "test-experiment", "run_id": "123", "run_attempt": 1, "dispatch_free5gc": True, "cluster_ip": "192.0.2.10", "cluster_name": "cluster-a", "cluster_user": "packer", "config_base64": base64.b64encode(b"nsName: test\n").decode(), "free5gc_ref": "metrics", "minimum_ready_nodes": 1, "readiness_timeout_s": 900}


def report_fixture():
    observation = {"ready": True, "api_ready": True, "ready_node_count": 1, "observed_node_count": 1, "schedulable_ready_count": 1}
    return {"schema_version": 1, "experiment_id": "test-experiment", "producer_run_id": "123", "producer_run_attempt": 1, "status": "ready", "ready": True, "consecutive_ready": 3, "observed_at_utc": datetime.now(timezone.utc).isoformat(), "target": {"address": "192.0.2.10", "name": "cluster-a"}, "observations": [dict(observation) for _ in range(3)]}


class DispatchTests(unittest.TestCase):
    def test_nonready_cluster_cannot_produce_a_dispatch_payload(self):
        self.assertIsNotNone(dispatch, "dispatch guard missing")
        report = report_fixture()
        report["status"], report["ready"] = "timeout", False
        with self.assertRaises(ValueError):
            dispatch.build_payload(context_fixture(), report)


    def test_fresh_evidence_preserves_correlation_and_target(self):
        payload = dispatch.build_payload(context_fixture(), report_fixture())
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["inputs"]["experiment_id"], "test-experiment")
        self.assertEqual(payload["inputs"]["cluster_ip"], "192.0.2.10")
        self.assertEqual(payload["inputs"]["upstream_run_id"], "123")
        self.assertEqual(payload["inputs"]["upstream_run_attempt"], "1")



    def test_old_or_foreign_evidence_is_rejected(self):
        from datetime import timedelta
        for mutation in ["time", "request", "attempt", "observations"]:
            with self.subTest(mutation=mutation):
                report = report_fixture()
                if mutation == "time":
                    report["observed_at_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
                elif mutation == "request":
                    report["experiment_id"] = "other"
                elif mutation == "attempt":
                    report["producer_run_attempt"] = 2
                else:
                    report["observations"] = []
                with self.assertRaises(ValueError):
                    dispatch.build_payload(context_fixture(), report)



    def test_malformed_evidence_is_a_validation_error_not_a_crash(self):
        boolean_count = report_fixture()
        boolean_count["observations"][0]["ready_node_count"] = True
        null_observations = report_fixture()
        null_observations["observations"] = [None, None, None]
        for value in [None, [], {"status": "ready", "ready": True}, boolean_count, null_observations]:
            with self.subTest(value=value):
                try:
                    dispatch.build_payload(context_fixture(), value)
                except ValueError:
                    continue
                except Exception as error:
                    self.fail("unexpected validation exception: " + type(error).__name__)
                self.fail("malformed evidence was accepted")



class HTTPDispatchTests(unittest.TestCase):
    def test_dispatch_run_is_read_back_before_confirmation(self):
        self.assertTrue(hasattr(dispatch, "post_dispatch"), "verified dispatch client missing")
        import json
        class Response:
            def __init__(self, data):
                self.status, self.data = 200, data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(self.data).encode()
        replies = [Response({"workflow_run_id": 9001}), Response({"id": 9001, "head_branch": "metrics", "event": "workflow_dispatch", "display_title": "ODIM-test-experiment"})]
        methods = []
        def sender(request, timeout):
            methods.append(request.get_method())
            return replies.pop(0)
        payload = dispatch.build_payload(context_fixture(), report_fixture())
        result = dispatch.post_dispatch(payload, "SYNTHETIC_TEST_TOKEN", sender=sender)
        self.assertEqual(result["workflow_run_id"], 9001)
        self.assertTrue(result["read_back_verified"])
        self.assertEqual(methods, ["POST", "GET"])
        self.assertNotIn("SYNTHETIC_TEST_TOKEN", json.dumps(result))



    def test_http_failure_never_retries_the_post_or_exposes_token(self):
        import urllib.error
        calls = []
        def sender(request, timeout):
            calls.append(request.get_method())
            raise urllib.error.HTTPError(request.full_url, 403, "SYNTHETIC_TEST_TOKEN", {}, None)
        with self.assertRaises(dispatch.DispatchRejected) as caught:
            dispatch.post_dispatch(dispatch.build_payload(context_fixture(), report_fixture()), "SYNTHETIC_TEST_TOKEN", sender=sender)
        self.assertEqual(calls, ["POST"])
        self.assertNotIn("SYNTHETIC_TEST_TOKEN", str(caught.exception))

    def test_ambiguous_network_failure_is_not_retried(self):
        import urllib.error
        calls = []
        def sender(request, timeout):
            calls.append(request.get_method())
            raise urllib.error.URLError("synthetic response lost")
        with self.assertRaises(dispatch.DispatchUncertain):
            dispatch.post_dispatch(dispatch.build_payload(context_fixture(), report_fixture()), "TEST", sender=sender)
        self.assertEqual(calls, ["POST"])



    def test_nonobject_success_response_is_unknown_without_another_post(self):
        class Response:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"[]"
        methods = []
        def sender(request, timeout):
            methods.append(request.get_method())
            return Response()
        try:
            dispatch.post_dispatch(dispatch.build_payload(context_fixture(), report_fixture()), "TEST", sender=sender)
        except dispatch.DispatchUncertain:
            pass
        except Exception as error:
            self.fail("ambiguous dispatch must be classified: " + type(error).__name__)
        else:
            self.fail("invalid response was accepted")
        self.assertEqual(methods, ["POST"])



if __name__ == "__main__":
    unittest.main()
