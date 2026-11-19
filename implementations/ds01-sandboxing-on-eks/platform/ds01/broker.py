"""Credential broker (Design 01: "Credentials: secrets never enter the sandbox").

Mints a short-lived credential for one session and one destination. Only the
egress gateway may call it. The credential is returned to the gateway, which adds
it to the outgoing request; it is never given to a sandbox.
"""
from ds01.common import Handler, audit, mint, read_secret, secrets_equal, serve, sha256_hex

# audience -> maximum lifetime in seconds. A caller cannot ask for a longer one.
AUDIENCES = {"metrics-api": 900, "tool-gateway-backend": 900}

broker_key = read_secret("/secrets/broker-key")
gateway_token = read_secret("/secrets/egress-gateway-token")
tool_gateway_token = read_secret("/secrets/tool-gateway-token")


class Broker(Handler):
    component = "credential-broker"

    def route(self, method, path, query, body):
        if path == "/healthz":
            return 200, {"ok": True}
        presented = self.headers.get("X-Internal-Token", "")
        caller = ("egress-gateway" if secrets_equal(presented, gateway_token)
                  else "tool-gateway" if secrets_equal(presented, tool_gateway_token) else None)
        if not caller:
            return 401, {"error": "unauthorized"}
        if path == "/mint" and method == "POST":
            session, tenant, aud = body.get("session_id"), body.get("tenant"), body.get("audience")
            if aud not in AUDIENCES or not session:
                audit(self.component, "mint.denied", caller=caller, session_id=session, audience=aud)
                return 403, {"error": "audience not allowed"}
            ttl = min(int(body.get("ttl_seconds", 900)), AUDIENCES[aud])
            token = mint(broker_key, {"aud": aud, "sub": session, "tenant": tenant, "scope": "read"}, ttl)
            audit(self.component, "credential.minted", caller=caller, session_id=session, tenant=tenant,
                  audience=aud, ttl_seconds=ttl, credential_id=sha256_hex(token)[:10])
            return 200, {"credential": token, "ttl_seconds": ttl}
        return 404, {"error": "not found"}


if __name__ == "__main__":
    serve(Broker, 8001)
