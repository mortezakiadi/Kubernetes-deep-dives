"""Policy service for Envoy's ext_authz (gRPC). It does what the mitmproxy add-on did on its own:
find the session that owns the source address, check the host against its allowlist, rate limit, and
(for the host that needs one) supply the credential. Envoy does the proxying."""
import json, time
from collections import defaultdict, deque
from concurrent import futures
import grpc
from envoy.config.core.v3 import base_pb2
from envoy.service.auth.v3 import external_auth_pb2 as pb, external_auth_pb2_grpc as pbg
from envoy.type.v3 import http_status_pb2
from google.protobuf import struct_pb2
from google.rpc import code_pb2, status_pb2

SESSIONS = json.load(open("/sessions.json"))      # source ip -> {session_id, allow_hosts, inject}
RATE = 5                                          # per minute, small so the lab can trip it
hits = defaultdict(deque)

def header(k, v):
    return base_pb2.HeaderValueOption(header=base_pb2.HeaderValue(key=k, value=v),
                                      append_action=base_pb2.HeaderValueOption.OVERWRITE_IF_EXISTS_OR_ADD)

def meta(sid):
    m = struct_pb2.Struct(); m.update({"session_id": sid or ""}); return m

def deny(code, msg, sid=None):
    return pb.CheckResponse(status=status_pb2.Status(code=code_pb2.PERMISSION_DENIED), dynamic_metadata=meta(sid),
                            denied_response=pb.DeniedHttpResponse(status=http_status_pb2.HttpStatus(code=code), body=msg + "\n"))

class Authz(pbg.AuthorizationServicer):
    def Check(self, request, context):
        mode = dict(context.invocation_metadata()).get("x-ds01-mode", "check")
        a = request.attributes
        ip = a.source.address.socket_address.address
        host = a.request.http.host.split(":")[0].lower()
        print("CHECK", mode, a.request.http.method, a.request.http.host, "from", ip, flush=True)
        s = SESSIONS.get(ip)
        if not s:
            return deny(403, "egress denied: source is not a known sandbox")
        sid = s["session_id"]
        if host not in s["allow_hosts"]:
            return deny(403, f"egress denied: {host} is not on this session's allowlist", sid)
        if mode == "check":
            q, now = hits[sid], time.time()
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= RATE:
                return deny(429, "egress denied: request rate limit for this session", sid)
            q.append(now)
        headers = [header("x-ds01-session", sid)]
        if mode == "inject" and host in s["inject"]:
            headers.append(header("authorization", "Bearer real-credential-for-" + sid))
        return pb.CheckResponse(status=status_pb2.Status(code=code_pb2.OK), dynamic_metadata=meta(sid), ok_response=pb.OkHttpResponse(headers=headers))

server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
pbg.add_AuthorizationServicer_to_server(Authz(), server)
server.add_insecure_port("0.0.0.0:9000"); server.start(); print("authz up", flush=True); server.wait_for_termination()
