import json, ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CERTS = {h: (f"/certs/{h}.pem", f"/certs/{h}.key") for h in ("allowed.test", "metrics.test", "blocked.test")}
ctxs = {}
for h, (c, k) in CERTS.items():
    x = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); x.load_cert_chain(c, k); ctxs[h] = x
def sni(sock, name, ctx):
    if name in ctxs:
        sock.context = ctxs[name]
base = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); base.load_cert_chain(*CERTS["allowed.test"]); base.sni_callback = sni
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if self.headers.get("Host", "").startswith("metrics.test") and not auth.startswith("Bearer real-"):
            code, body = 401, {"error": "credential rejected"}
        else:
            code, body = 200, {"server_host": self.headers.get("Host"), "authorization_seen": auth}
        out = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = ThreadingHTTPServer(("0.0.0.0", 443), H); srv.socket = base.wrap_socket(srv.socket, server_side=True); srv.serve_forever()
