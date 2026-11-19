"""A stand-in for "the company's metrics API" (Design 01: "Tools and external services").

It is an internal HTTPS service that refuses every request that does not carry a
valid, unexpired credential minted by the broker for this audience. A sandbox
holds only the word "placeholder", so a successful call proves the egress gateway
injected a real credential on the way out.
"""
from ds01.common import Handler, audit, read_secret, serve, verify

broker_key = read_secret("/secrets/broker-key")

DATA = {
    "orders_per_minute": [412, 398, 431, 455, 470, 466],
    "p99_latency_ms": [182, 175, 190, 188, 204, 199],
}


class MetricsApi(Handler):
    component = "metrics-api"

    def route(self, method, path, query, body):
        if path == "/healthz":
            return 200, {"ok": True}
        if path != "/v1/metrics" or method != "GET":
            return 404, {"error": "not found"}
        try:
            claims = verify(broker_key, self.bearer(), audience="metrics-api")
        except ValueError as exc:
            audit(self.component, "request.rejected", reason=str(exc), peer=self.client_address[0])
            return 401, {"error": f"credential rejected: {exc}"}
        name = (query.get("name") or ["orders_per_minute"])[0]
        audit(self.component, "request.served", session_id=claims["sub"], tenant=claims.get("tenant"), metric=name)
        return 200, {"metric": name, "points": DATA.get(name, []), "caller_session": claims["sub"],
                     "credential_expires_in": int(claims["exp"] - __import__("time").time())}


if __name__ == "__main__":
    serve(MetricsApi, 8443, tls_cert="/tls/tls.crt", tls_key="/tls/tls.key")
