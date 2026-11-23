import unittest

import workspace


class FakeK8s:
    """Records every call and answers from a small script."""
    def __init__(self, sandbox="sess-1111-sb", ready_after=2):
        self.calls, self.sandbox, self.ready_after, self.gets = [], sandbox, ready_after, 0

    def request(self, method, path, body=None, timeout=15, content_type=None):
        self.calls.append((method, path, body, content_type))
        if method == "GET" and path.endswith("/sandboxclaims/sess-1111"):
            return 200, {"status": {"sandbox": {"name": self.sandbox}}}
        if method == "GET" and path.endswith("/sandboxclaims/sess-nobody"):
            return 404, {}
        if method == "GET" and "/sandboxes/" in path:
            self.gets += 1
            ready = self.gets >= self.ready_after
            return 200, {"status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}
        return 200, {}


class WorkspaceTests(unittest.TestCase):
    def test_suspend_patches_only_the_operating_mode_with_a_merge_patch(self):
        k = FakeK8s()
        self.assertEqual(workspace.suspend(k, "sess-1111"), "sess-1111-sb")
        method, path, body, ctype = k.calls[-1]
        self.assertEqual((method, path), ("PATCH", f"{workspace.SANDBOXES}/sess-1111-sb"))
        self.assertEqual(body, {"spec": {"operatingMode": "Suspended"}})
        self.assertEqual(ctype, "application/merge-patch+json")

    def test_suspend_never_deletes_anything(self):
        k = FakeK8s()
        workspace.suspend(k, "sess-1111")
        self.assertNotIn("DELETE", [c[0] for c in k.calls])

    def test_resume_waits_until_ready(self):
        k = FakeK8s(ready_after=3)
        naps = []
        workspace.resume(k, "sess-1111", timeout=30, poll=1, sleep=lambda s: naps.append(s))
        patch = [c for c in k.calls if c[0] == "PATCH"][0]
        self.assertEqual(patch[2], {"spec": {"operatingMode": "Running"}})
        self.assertEqual(k.gets, 3)
        self.assertEqual(len(naps), 2)

    def test_resume_times_out(self):
        k = FakeK8s(ready_after=10**12)
        with self.assertRaises(TimeoutError):
            workspace.resume(k, "sess-1111", timeout=0.05, poll=0.01, sleep=lambda s: None)

    def test_checkpoint_snapshots_the_workspace_volume_of_that_sandbox(self):
        k = FakeK8s()
        name = workspace.checkpoint(k, "sess-1111", "before-refactor")
        self.assertEqual(name, "sess-1111-before-refactor")
        method, path, body, _ = k.calls[-1]
        self.assertEqual((method, path), ("POST", workspace.SNAPSHOTS))
        self.assertEqual(body["spec"]["source"]["persistentVolumeClaimName"], "workspace-sess-1111-sb")
        self.assertEqual(body["spec"]["volumeSnapshotClassName"], "ds01-workspace")

    def test_an_unknown_session_is_an_error(self):
        with self.assertRaises(LookupError):
            workspace.suspend(FakeK8s(), "sess-nobody")


if __name__ == "__main__":
    unittest.main(verbosity=2)
