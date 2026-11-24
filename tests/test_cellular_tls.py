"""Over a real TLS handshake: the worker connects to an address, not a name.

The metered link permits no DNS, so the worker sends its one request to a
pinned IP literal. That must not cheapen the HTTPS it gets back: the plan's
words are "preserve the dashboard hostname for HTTPS certificate verification
and virtual-host routing". In code that is two request details — an SNI
override and a ``Host`` header — and the mocked-transport tests can only show
they are *set*. This module shows what they *do*, against a local HTTPS server:

* a certificate issued for the hostname is accepted, although the connection
  went to an address the certificate says nothing about;
* a certificate for any other name is refused;
* a certificate valid for the *address* is refused too — the address is not
  what gets verified.

Skipped where ``openssl`` is not installed (it cuts the throwaway certificates).
"""
from __future__ import annotations

import http.server
import json
import shutil
import ssl
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from usage_dashboard.client import cellular_worker as cw
from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="needs the openssl command for certificates"
)

HOST = "dash.example"
ADDRESS = "127.0.0.1"


def _reading() -> dict[str, object]:
    at = datetime(2026, 11, 20, 9, 30, 0)
    return Reading(Provider.CLAUDE, ReadingStatus.CURRENT, 40.0, at, 55.0, at, at, False).to_dict()


def _certificate(directory: Path, common_name: str, alt_name: str) -> Path:
    cert = directory / f"{common_name}.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", str(cert.with_suffix(".key")), "-out", str(cert),
            "-days", "2", "-subj", f"/CN={common_name}",
            "-addext", f"subjectAltName={alt_name}",
        ],
        check=True, capture_output=True, timeout=60,
    )
    return cert


class _Server(http.server.ThreadingHTTPServer):
    """A one-route HTTPS server that records how it was addressed."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, certificate: Path, status: int = 200) -> None:
        super().__init__((ADDRESS, 0), _Handler)
        self.status = status
        self.cert = certificate
        self.requests: list[dict[str, str | None]] = []
        self.sni: list[str | None] = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, certificate.with_suffix(".key"))

        def named(sslobject: ssl.SSLObject, name: str | None, ctx: ssl.SSLContext) -> None:
            self.sni.append(name)

        context.sni_callback = named
        self.socket = context.wrap_socket(self.socket, server_side=True)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _Server)
        server.requests.append(
            {"path": self.path, "host": self.headers.get("Host"),
             "authorization": self.headers.get("Authorization")}
        )
        body = json.dumps([_reading()]).encode()
        self.send_response(server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


@pytest.fixture
def make_server(tmp_path: Path):
    made: list[_Server] = []

    def make(common_name: str, alt_name: str, status: int = 200) -> _Server:
        server = _Server(_certificate(tmp_path, common_name, alt_name), status=status)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


def _config(port: int, tmp_path: Path) -> cw.WorkerConfig:
    return cw.WorkerConfig(
        host=HOST, port=port, path=cw.READINGS_PATH, display_key="display-test",
        destinations=("192.0.2.10",), interface="cell0", helper="/helper",
        state_dir=tmp_path, request_dir=tmp_path / "request",
        probe_interval=120.0, retry_delay=20.0, poll_interval=600.0, min_interval=60.0,
        backoff_max=3600.0, awake_timeout=150.0, connect_timeout=5.0, read_timeout=5.0,
        resolve_timeout=5.0, budget_bytes=1,
    )


def _client_trusting(certificate: Path) -> httpx.Client:
    # The production client trusts the system/Certifi roots; here the root is
    # the throwaway certificate itself. Everything else is the worker's own.
    context = ssl.create_default_context(cafile=str(certificate))
    return httpx.Client(
        verify=context, trust_env=False, follow_redirects=False, http2=False,
        timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=1.0),
    )


def _address(server: _Server) -> str:
    assert server.server_address[0] == ADDRESS
    return ADDRESS


class TestOverARealHandshake:
    def test_a_certificate_for_the_hostname_is_accepted_at_any_address(
        self, tmp_path: Path, make_server: Any
    ) -> None:
        server = make_server(HOST, f"DNS:{HOST}")
        outcome = cw._request(
            _client_trusting(Path(server.cert)),  # type: ignore[attr-defined]
            _config(server.server_address[1], tmp_path), _address(server),
        )
        assert outcome.ok and outcome.payload is not None
        assert outcome.payload[0]["provider"] == "claude"
        # Addressed to the hostname three ways: the name in the handshake, the
        # Host header the ingress routes on, and the credential.
        assert server.sni == [HOST]
        assert server.requests == [{
            "path": cw.READINGS_PATH, "host": HOST,
            "authorization": "Bearer display-test",
        }]

    def test_a_certificate_for_another_name_is_refused(
        self, tmp_path: Path, make_server: Any
    ) -> None:
        server = make_server("other.example", "DNS:other.example")
        outcome = cw._request(
            _client_trusting(Path(server.cert)),  # type: ignore[attr-defined]
            _config(server.server_address[1], tmp_path), _address(server),
        )
        assert not outcome.reachable and outcome.label() == "tls_verify"
        # Refused before a single byte of the request was sent.
        assert server.requests == []

    def test_a_certificate_for_the_address_alone_is_refused(
        self, tmp_path: Path, make_server: Any
    ) -> None:
        # If the address were what got verified, this would pass. It must not:
        # a pinned address is routing, not identity.
        server = make_server(ADDRESS, f"IP:{ADDRESS}")
        outcome = cw._request(
            _client_trusting(Path(server.cert)),  # type: ignore[attr-defined]
            _config(server.server_address[1], tmp_path), _address(server),
        )
        assert not outcome.reachable and outcome.label() == "tls_verify"
        assert server.requests == []

    def test_the_real_fetcher_makes_the_same_request(
        self, tmp_path: Path, make_server: Any
    ) -> None:
        server = make_server(HOST, f"DNS:{HOST}")
        certificate = Path(server.cert)  # type: ignore[attr-defined]
        fetcher = cw.HttpFetcher(
            _config(server.server_address[1], tmp_path),
            resolve=lambda host, port: [_address(server)],
            client_factory=lambda: _client_trusting(certificate),
        )
        # Both routes, since they share this code.
        assert fetcher.read(_address(server), cellular=False).ok
        assert fetcher.read(_address(server), cellular=True).ok
        assert server.sni == [HOST, HOST]
        assert [request["host"] for request in server.requests] == [HOST, HOST]
        fetcher.close()

    def test_an_error_status_comes_back_as_itself(self, tmp_path: Path, make_server: Any) -> None:
        server = make_server(HOST, f"DNS:{HOST}", status=401)
        outcome = cw._request(
            _client_trusting(Path(server.cert)),  # type: ignore[attr-defined]
            _config(server.server_address[1], tmp_path), _address(server),
        )
        assert outcome.reachable and not outcome.ok and outcome.label() == "http_401"
