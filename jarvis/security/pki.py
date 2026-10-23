"""Private certificate authority and TLS contexts (ECDSA P-256, TLS 1.3 only)."""
import datetime
import ipaddress
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEVICE_CERT_DAYS = 90
SERVER_CERT_DAYS = 365


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_key(pem: bytes):
    return serialization.load_pem_private_key(pem, password=None)


def cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def load_cert(pem: bytes) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem)


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _usage(**kw) -> x509.KeyUsage:
    base = dict(
        digital_signature=False, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=False,
        crl_sign=False, encipher_only=False, decipher_only=False,
    )
    base.update(kw)
    return x509.KeyUsage(**base)


def create_ca(common_name: str = "Jarvis Hub CA", years: int = 10):
    key = new_key()
    name = _name(common_name)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=365 * years))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_usage(key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _leaf(ca_cert, ca_key, public_key, cn, days, eku, sans=None) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn)).issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - datetime.timedelta(minutes=5))
        .not_valid_after(_now() + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return builder.sign(ca_key, hashes.SHA256())


def issue_server_cert(ca_cert, ca_key, key, addresses=()):
    """Hub certificate. serverAuth only, so a device certificate can never act as the hub."""
    sans = [x509.DNSName("jarvis.local"), x509.DNSName("localhost")]
    for a in addresses:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(a)))
        except ValueError:
            sans.append(x509.DNSName(a))
    return _leaf(ca_cert, ca_key, key.public_key(), "jarvis-hub", SERVER_CERT_DAYS,
                 ExtendedKeyUsageOID.SERVER_AUTH, sans)


def make_csr(key, cn: str = "jarvis-device") -> x509.CertificateSigningRequest:
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(_name(cn))
        .sign(key, hashes.SHA256())
    )


def validate_csr(csr_pem: bytes) -> x509.CertificateSigningRequest:
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError("bad CSR signature")
    pub = csr.public_key()
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256R1):
        raise ValueError("CSR key must be ECDSA P-256")
    return csr


def sign_csr(ca_cert, ca_key, csr, device_id: str, days: int = DEVICE_CERT_DAYS):
    """Issue a clientAuth certificate. Subject and extensions come from the hub, never the CSR."""
    return _leaf(ca_cert, ca_key, csr.public_key(), device_id, days,
                 ExtendedKeyUsageOID.CLIENT_AUTH)


def csr_der(csr) -> bytes:
    return csr.public_bytes(serialization.Encoding.DER)


def cert_cn(cert: x509.Certificate) -> str:
    return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value


def _base(ctx: ssl.SSLContext) -> ssl.SSLContext:
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


def server_context(cert: x509.Certificate, key, ca_cert=None, require_client=False, tmpdir: Path | None = None):
    """Build a server context. Python's ssl only loads chains from files, so we use a private temp dir."""
    import tempfile
    ctx = _base(ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER))
    with tempfile.TemporaryDirectory(dir=tmpdir) as d:
        c, k = Path(d, "c.pem"), Path(d, "k.pem")
        c.write_bytes(cert_pem(cert))
        k.write_bytes(key_pem(key))
        ctx.load_cert_chain(c, k)
    if require_client:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=cert_pem(ca_cert).decode())
    return ctx


def client_context(ca_pem: bytes, cert_file: Path, key_file: Path) -> ssl.SSLContext:
    """Verify the hub against our private CA (chain + serverAuth purpose) and present our cert."""
    ctx = _base(ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    ctx.check_hostname = False  # the hub's address changes (LAN, hotspot, overlay); the CA chain is the identity
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=ca_pem.decode())
    ctx.load_cert_chain(cert_file, key_file)
    return ctx
