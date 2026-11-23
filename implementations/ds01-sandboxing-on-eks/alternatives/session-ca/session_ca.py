"""A certificate authority per session (alternative to the single gateway CA that every sandbox trusts in the tested build).

The gateway ends TLS for one internal host, so the sandbox has to trust a certificate that the gateway made up. In the tested build that
is one CA shared by every sandbox. Here each session gets its own CA, with three limits:
  * it can only issue for the one host that session needs (X.509 name constraints, marked critical);
  * it is a CA with path length 0, so it cannot create more CAs;
  * it expires with the session.
The sandbox is given only the CA's certificate (public). The key stays with the gateway and is dropped when the session ends. If a key
ever leaked, it could not be used to impersonate any other host, and it would not work for any other session.
"""
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _pem_key(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def new_session_ca(session_id: str, host: str, valid_for: datetime.timedelta = datetime.timedelta(hours=2)):
    """Returns (certificate PEM, private key PEM) of a CA that may issue certificates only for `host`."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"ds01 session CA {session_id}")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + valid_for)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(host)], excluded_subtrees=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), _pem_key(key)


def issue_leaf(ca_cert_pem: bytes, ca_key_pem: bytes, host: str, valid_for: datetime.timedelta = datetime.timedelta(hours=1)):
    """Returns (certificate PEM, private key PEM) for `host`, signed by the session CA."""
    ca = x509.load_pem_x509_certificate(ca_cert_pem)
    ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])).issuer_name(ca.subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(min(now + valid_for, ca.not_valid_after_utc))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), _pem_key(key)
