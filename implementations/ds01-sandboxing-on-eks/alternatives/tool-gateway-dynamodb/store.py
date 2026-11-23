"""Shared state for the tool gateway on DynamoDB (alternative to the in-memory dicts in platform/ds01/tool_gateway.py).

Why: the tested gateway keeps approvals and rate counters in memory, so it must run as one replica. With this store,
any number of replicas share them. Two properties matter and both are enforced by DynamoDB, not by Python:
  * one approval, one call: consuming an approval is a conditional write that only one caller can win;
  * one count per session per minute window: the counter is an atomic increment.

Table layout (one table, on-demand billing, TTL on expires_at):
  pk = "APPROVAL#<id>"   sk = "A"                 session, key, tool, state, expires_at
  pk = "RATE#<session>"  sk = "W#<minute number>" n, expires_at
"""
import secrets
import time

import boto3
from botocore.exceptions import ClientError

APPROVAL_TTL_SECONDS = 15 * 60


class ToolGatewayStore:
    def __init__(self, table_name: str, endpoint_url: str | None = None, region: str = "us-east-1", session=None):
        """`session` is a boto3 Session. The tests pass one with fixed local credentials; the gateway passes none and gets Pod Identity."""
        self._table = (session or boto3).resource("dynamodb", endpoint_url=endpoint_url, region_name=region).Table(table_name)

    # ---- approvals ---------------------------------------------------------------------------
    def request_approval(self, session: str, tool: str, key: str, now: float | None = None) -> str:
        """Create a pending approval for this session, tool and argument hash. Returns its id."""
        now = now or time.time()
        approval_id = "apr-" + secrets.token_hex(4)
        self._table.put_item(
            Item={"pk": f"APPROVAL#{approval_id}", "sk": "A", "session": session, "key": key, "tool": tool,
                  "state": "pending", "expires_at": int(now) + APPROVAL_TTL_SECONDS},
            ConditionExpression="attribute_not_exists(pk)",
        )
        return approval_id

    def get_approval(self, approval_id: str) -> dict | None:
        item = self._table.get_item(Key={"pk": f"APPROVAL#{approval_id}", "sk": "A"}, ConsistentRead=True).get("Item")
        return item

    def decide(self, approval_id: str, approve: bool) -> dict | None:
        """A person approves or denies. Only a pending approval can be decided. Returns the item, or None."""
        new_state = "approved" if approve else "denied"
        try:
            out = self._table.update_item(
                Key={"pk": f"APPROVAL#{approval_id}", "sk": "A"},
                UpdateExpression="SET #s = :new",
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={":new": new_state, ":pending": "pending"},
                ReturnValues="ALL_NEW",
            )
            return out["Attributes"]
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def consume(self, approval_id: str, session: str, key: str) -> bool:
        """Use an approval. True for exactly one caller, however many replicas race for it."""
        try:
            self._table.update_item(
                Key={"pk": f"APPROVAL#{approval_id}", "sk": "A"},
                UpdateExpression="SET #s = :used",
                ConditionExpression="#s = :approved AND #sess = :session AND #k = :key",
                ExpressionAttributeNames={"#s": "state", "#sess": "session", "#k": "key"},
                ExpressionAttributeValues={":used": "used", ":approved": "approved", ":session": session, ":key": key},
            )
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ---- rate limit --------------------------------------------------------------------------
    def within_rate(self, session: str, limit: int, now: float | None = None) -> bool:
        """Count one call in the current one-minute window. False once the window already holds `limit` calls."""
        now = now or time.time()
        window = int(now // 60)
        out = self._table.update_item(
            Key={"pk": f"RATE#{session}", "sk": f"W#{window}"},
            UpdateExpression="ADD n :one SET expires_at = :exp",
            ExpressionAttributeValues={":one": 1, ":exp": (window + 2) * 60},
            ReturnValues="UPDATED_NEW",
        )
        return int(out["Attributes"]["n"]) <= limit
