"""Checks the session CA with two independent verifiers: the cryptography library's web-PKI verifier and the OpenSSL command line
(run in the alpine/openssl image by check.sh, which passes its path in OPENSSL_CMD)."""
import os
import pathlib
import subprocess
import tempfile
import unittest

from cryptography import x509
from cryptography.x509 import DNSName
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

from session_ca import issue_leaf, new_session_ca

HOST = "metrics-api.sandbox-platform.svc"
OTHER = "pypi.org"


def library_verifies(ca_pem: bytes, leaf_pem: bytes, host: str) -> bool:
    ca = x509.load_pem_x509_certificate(ca_pem)
    leaf = x509.load_pem_x509_certificate(leaf_pem)
    verifier = PolicyBuilder().store(Store([ca])).build_server_verifier(DNSName(host))
    try:
        verifier.verify(leaf, [])
        return True
    except VerificationError:
        return False


def openssl_verifies(ca_pem: bytes, leaf_pem: bytes, host: str) -> bool | None:
    cmd = os.environ.get("OPENSSL_CMD")
    if not cmd:
        return None  # not available here; the library check still runs
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d)
        (p / "ca.pem").write_bytes(ca_pem)
        (p / "leaf.pem").write_bytes(leaf_pem)
        r = subprocess.run(cmd.split() + ["verify", "-CAfile", str(p / "ca.pem"), "-verify_hostname", host, str(p / "leaf.pem")],
                           capture_output=True, text=True)
        return r.returncode == 0


class SessionCaTests(unittest.TestCase):
    def test_leaf_for_the_permitted_host_verifies(self):
        ca, key = new_session_ca("sess-a", HOST)
        leaf, _ = issue_leaf(ca, key, HOST)
        self.assertTrue(library_verifies(ca, leaf, HOST))
        self.assertIn(openssl_verifies(ca, leaf, HOST), (True, None))

    def test_a_leaf_for_any_other_host_is_refused_even_with_the_ca_key(self):
        """Simulates a leaked CA key: the key can sign, but name constraints stop the certificate from being accepted."""
        ca, key = new_session_ca("sess-a", HOST)
        good, _ = issue_leaf(ca, key, HOST)
        self.assertTrue(library_verifies(ca, good, HOST), "positive control: the permitted host must verify, or the refusals below mean nothing")
        leaf, _ = issue_leaf(ca, key, OTHER)
        self.assertFalse(library_verifies(ca, leaf, OTHER))
        self.assertIn(openssl_verifies(ca, leaf, OTHER), (False, None))

    def test_another_sessions_ca_does_not_verify(self):
        ca_a, key_a = new_session_ca("sess-a", HOST)
        ca_b, _ = new_session_ca("sess-b", HOST)
        leaf, _ = issue_leaf(ca_a, key_a, HOST)
        self.assertTrue(library_verifies(ca_a, leaf, HOST), "positive control: session A's own CA must accept it")
        self.assertFalse(library_verifies(ca_b, leaf, HOST), "session B must not trust a certificate made by session A's CA")
        self.assertIn(openssl_verifies(ca_b, leaf, HOST), (False, None))

    def test_the_ca_certificate_carries_its_limits(self):
        ca, _ = new_session_ca("sess-a", HOST)
        cert = x509.load_pem_x509_certificate(ca)
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        nc = cert.extensions.get_extension_for_class(x509.NameConstraints)
        self.assertTrue(bc.critical and bc.value.ca and bc.value.path_length == 0)
        self.assertTrue(nc.critical)
        self.assertEqual([g.value for g in nc.value.permitted_subtrees], [HOST])


if __name__ == "__main__":
    unittest.main(verbosity=2)
