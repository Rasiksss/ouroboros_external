from __future__ import annotations

import datetime
import socket
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

_VALIDITY = datetime.timedelta(hours=1)
_ACCEPT_SLICE_SEC = 0.05
_HANDSHAKE_TIMEOUT_SEC = 2.0
_JOIN_TIMEOUT_SEC = 2.0


@dataclass(frozen=True)
class TlsListener:
    address: str
    ca_path: Path


class _Issuer:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ouroboros-test-ca")])
        now = datetime.datetime.now(datetime.UTC)
        self._certificate = (
            self._builder(self._name, self._key.public_key(), now)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self._key, hashes.SHA256())
        )
        self.ca_path = directory / "ca.pem"
        self.ca_path.write_bytes(self._certificate.public_bytes(serialization.Encoding.PEM))

    def server_context(self, host: str) -> ssl.SSLContext:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        certificate = (
            self._builder(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]), key.public_key(), now)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        certificate_path = self._directory / "server.pem"
        key_path = self._directory / "server.key"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(certificate_path), str(key_path))
        return context

    def client_files(self, common_name: str) -> tuple[Path, Path]:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        certificate = self._builder(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]), key.public_key(), now
        ).sign(self._key, hashes.SHA256())
        certificate_path = self._directory / "client.pem"
        key_path = self._directory / "client.key"
        certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        return certificate_path, key_path

    def _builder(self, subject: x509.Name, public_key: ec.EllipticCurvePublicKey, now: datetime.datetime):
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._name)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _VALIDITY)
            .not_valid_after(now + _VALIDITY)
        )


class _Server:
    def __init__(self, context: ssl.SSLContext) -> None:
        self._context = context
        self._stopped = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(_ACCEPT_SLICE_SEC)
        self._thread = threading.Thread(target=self._accept, name="tls-listener", daemon=True)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self._listener.getsockname()[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(_JOIN_TIMEOUT_SEC)
        self._listener.close()

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            threading.Thread(target=self._handshake, args=(connection,), name="tls-handshake", daemon=True).start()

    def _handshake(self, connection: socket.socket) -> None:
        connection.settimeout(_HANDSHAKE_TIMEOUT_SEC)
        try:
            with self._context.wrap_socket(connection, server_side=True):
                self._stopped.wait(_HANDSHAKE_TIMEOUT_SEC)
        except OSError:
            connection.close()


@contextmanager
def tls_listener(directory: Path, *, certified_host: str) -> Iterator[TlsListener]:
    issuer = _Issuer(directory)
    server = _Server(issuer.server_context(certified_host))
    server.start()
    try:
        yield TlsListener(address=server.address, ca_path=issuer.ca_path)
    finally:
        server.stop()


def issue_client_certificate(directory: Path, *, common_name: str) -> tuple[Path, Path, Path]:
    """Returns the CA, the certificate and its key, in that order."""
    issuer = _Issuer(directory)
    certificate_path, key_path = issuer.client_files(common_name)
    return issuer.ca_path, certificate_path, key_path


__all__ = ["TlsListener", "issue_client_certificate", "tls_listener"]
