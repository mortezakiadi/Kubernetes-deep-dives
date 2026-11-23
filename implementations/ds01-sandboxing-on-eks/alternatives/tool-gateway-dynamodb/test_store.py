"""Tests for store.py against DynamoDB Local. Run with check.sh (it starts DynamoDB Local and this file in containers)."""
import os
import threading
import unittest

import boto3
import botocore.session

from store import ToolGatewayStore

ENDPOINT = os.environ.get("DDB_ENDPOINT", "http://localhost:8000")
TABLE = "ds01-tool-gateway-test"


def local_session():
    """DynamoDB Local needs some credentials and accepts any. Fixed local ones, set without touching the environment."""
    core = botocore.session.get_session()
    core.set_credentials("local", "local")
    return boto3.Session(botocore_session=core)
def make_table():
    ddb = local_session().client("dynamodb", endpoint_url=ENDPOINT, region_name="us-east-1")
    try:
        ddb.delete_table(TableName=TABLE)
    except ddb.exceptions.ResourceNotFoundException:
        pass
    ddb.create_table(
        TableName=TABLE,
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        BillingMode="PAY_PER_REQUEST",
    )


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_table()
        cls.store = ToolGatewayStore(TABLE, endpoint_url=ENDPOINT, session=local_session())

    def test_approval_flow_one_approval_one_call(self):
        aid = self.store.request_approval("sess-a", "ticket.create", "k1")
        self.assertEqual(self.store.get_approval(aid)["state"], "pending")
        self.assertFalse(self.store.consume(aid, "sess-a", "k1"), "a pending approval must not be usable")
        self.assertEqual(self.store.decide(aid, True)["state"], "approved")
        self.assertTrue(self.store.consume(aid, "sess-a", "k1"))
        self.assertFalse(self.store.consume(aid, "sess-a", "k1"), "a used approval must not be usable again")
        self.assertEqual(self.store.get_approval(aid)["state"], "used")

    def test_approval_is_bound_to_session_and_arguments(self):
        aid = self.store.request_approval("sess-a", "ticket.create", "k1")
        self.store.decide(aid, True)
        self.assertFalse(self.store.consume(aid, "sess-b", "k1"), "another session must not use it")
        self.assertFalse(self.store.consume(aid, "sess-a", "other-arguments"), "other arguments must not use it")
        self.assertTrue(self.store.consume(aid, "sess-a", "k1"))

    def test_denied_and_decided_approvals_cannot_be_decided_again(self):
        aid = self.store.request_approval("sess-a", "ticket.create", "k1")
        self.assertEqual(self.store.decide(aid, False)["state"], "denied")
        self.assertIsNone(self.store.decide(aid, True), "a denied approval must stay denied")
        self.assertFalse(self.store.consume(aid, "sess-a", "k1"))

    def test_unknown_approval(self):
        self.assertIsNone(self.store.get_approval("apr-doesnotexist"))
        self.assertFalse(self.store.consume("apr-doesnotexist", "sess-a", "k1"))
        self.assertIsNone(self.store.decide("apr-doesnotexist", True))

    def test_many_replicas_race_for_one_approval(self):
        aid = self.store.request_approval("sess-race", "ticket.create", "k1")
        self.store.decide(aid, True)
        wins, lock = [], threading.Lock()

        def replica():
            own = ToolGatewayStore(TABLE, endpoint_url=ENDPOINT, session=local_session())  # each "replica" has its own client
            ok = own.consume(aid, "sess-race", "k1")
            with lock:
                wins.append(ok)

        threads = [threading.Thread(target=replica) for _ in range(25)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(wins), 1, f"exactly one replica may win, got {sum(wins)} of {len(wins)}")

    def test_rate_limit_counts_across_replicas(self):
        limit, now = 20, 1_700_000_000.0
        replicas = [ToolGatewayStore(TABLE, endpoint_url=ENDPOINT, session=local_session()) for _ in range(3)]
        results = [replicas[i % 3].within_rate("sess-rate", limit, now) for i in range(25)]
        self.assertEqual(results.count(True), 20)
        self.assertEqual(results.count(False), 5)

    def test_rate_limit_window_resets_and_sessions_are_separate(self):
        limit, now = 3, 1_700_100_000.0
        for _ in range(3):
            self.assertTrue(self.store.within_rate("sess-w", limit, now))
        self.assertFalse(self.store.within_rate("sess-w", limit, now))
        self.assertTrue(self.store.within_rate("sess-w", limit, now + 60), "a new minute starts a new window")
        self.assertTrue(self.store.within_rate("sess-other", limit, now), "another session has its own counter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
