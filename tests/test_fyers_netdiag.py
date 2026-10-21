import errno
import http.client
import shutil
import socket
import ssl
import subprocess
import threading
import urllib.error

import pytest

from nifty_signal_engine.fyers import FyersNetworkError, diagnose, urllib_transport
from nifty_signal_engine.fyers.auth import urllib_post_transport
from nifty_signal_engine.fyers.netdiag import (
    PERMANENT_CATEGORIES,
    NetworkCategory,
    classify_network_error,
    network_error_from,
)

from fyers_helpers import ScriptedTransport, json_response, make_client, ok_payload

SECRET = "AUTHCODE.abcdef123456.zzz"  # stands in for an auth code / app secret / token / request body
C = NetworkCategory


def cert_error(code=20):
    exc = ssl.SSLCertVerificationError(1, "library text that must never be shown")
    exc.verify_code = code
    exc.verify_message = f"raw openssl text {SECRET}"
    return exc


# --- classification ----------------------------------------------------------------------------

@pytest.mark.parametrize("exc, category, sent", [
    (urllib.error.URLError(cert_error()), C.TLS_CERTIFICATE, "no"),
    (cert_error(), C.TLS_CERTIFICATE, "no"),                       # raised raw, not wrapped by urllib
    (urllib.error.URLError(ssl.SSLError(1, "handshake")), C.TLS, "no"),
    (ssl.SSLEOFError(8, "eof"), C.TLS, "unknown"),
    (urllib.error.URLError(socket.gaierror(-2, "x")), C.DNS, "no"),
    (socket.gaierror(-2, "x"), C.DNS, "no"),
    (TimeoutError("x"), C.TIMEOUT, "unknown"),
    (urllib.error.URLError(TimeoutError("x")), C.TIMEOUT, "unknown"),
    (socket.timeout("x"), C.TIMEOUT, "unknown"),
    (OSError(errno.ETIMEDOUT, "x"), C.TIMEOUT, "unknown"),
    (urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "x")), C.CONNECTION, "no"),
    (OSError(errno.ENETUNREACH, "x"), C.CONNECTION, "no"),
    (OSError(errno.EHOSTUNREACH, "x"), C.CONNECTION, "no"),
    (ConnectionResetError(errno.ECONNRESET, "x"), C.CONNECTION, "maybe"),
    (ConnectionAbortedError(errno.ECONNABORTED, "x"), C.CONNECTION, "maybe"),
    (BrokenPipeError(errno.EPIPE, "x"), C.CONNECTION, "maybe"),
    (http.client.RemoteDisconnected("x"), C.CONNECTION, "maybe"),
    (http.client.BadStatusLine("x"), C.PROTOCOL, "maybe"),
    (http.client.IncompleteRead(b"x"), C.PROTOCOL, "maybe"),
    (urllib.error.URLError("unknown url type"), C.OTHER, "unknown"),
    (OSError(errno.EACCES, "x"), C.OTHER, "unknown"),
    (ValueError("x"), C.OTHER, "unknown"),
])
def test_classification(exc, category, sent):
    failure = classify_network_error(exc)
    assert (failure.category, failure.request_sent) == (category, sent)
    error = network_error_from(exc)
    assert isinstance(error, FyersNetworkError)
    assert (error.category, error.request_sent) == (category.value, sent)
    assert f"[{category.value}]" in str(error) and str(error).startswith("network error")


def test_urlerror_chain_is_reported_by_class_name_only():
    failure = classify_network_error(urllib.error.URLError(cert_error()))
    assert failure.chain == ("URLError", "SSLCertVerificationError")
    assert "URLError > SSLCertVerificationError" in str(network_error_from(urllib.error.URLError(cert_error())))


@pytest.mark.parametrize("code, text", [
    (20, "unable to get local issuer certificate"), (18, "self-signed certificate"),
    (19, "self-signed certificate in certificate chain"), (10, "certificate has expired"),
    (62, "certificate does not match the host name"), (999, "reason not recognised"), (None, "reason not recognised"),
])
def test_certificate_failures_use_a_fixed_vocabulary(code, text):
    message = str(network_error_from(urllib.error.URLError(cert_error(code))))
    assert text in message
    assert "library text that must never be shown" not in message and SECRET not in message


def test_certificate_hint_covers_the_macos_cause_and_never_suggests_disabling_verification():
    message = str(network_error_from(urllib.error.URLError(cert_error())))
    assert "Install Certificates.command" in message and "certifi" in message and "SSL_CERT_FILE" in message
    assert "diagnose" in message and "auth code was not used" in message
    assert "Never disable certificate verification" in message


def test_timeout_and_reset_warn_that_the_auth_code_may_be_used_up():
    assert "may already be used up" in str(network_error_from(TimeoutError()))
    assert "may already be used up" in str(network_error_from(ConnectionResetError(errno.ECONNRESET, "x")))
    assert "was not used" in str(network_error_from(socket.gaierror(-2, "x")))
    assert "was not used" in str(network_error_from(ConnectionRefusedError(errno.ECONNREFUSED, "x")))


@pytest.mark.parametrize("exc", [
    urllib.error.URLError(cert_error()),
    urllib.error.URLError(SECRET),
    urllib.error.URLError(OSError(errno.ECONNRESET, SECRET)),
    ssl.SSLError(1, SECRET),
    TimeoutError(SECRET),
    socket.gaierror(-2, SECRET),
    ConnectionRefusedError(errno.ECONNREFUSED, SECRET),
    http.client.BadStatusLine(SECRET),
    OSError(SECRET),
    Exception(SECRET),
])
def test_no_raw_exception_text_ever_reaches_the_message(exc):
    error = network_error_from(exc)
    assert SECRET not in str(error) and SECRET not in repr(error) and SECRET not in repr(classify_network_error(exc))


def test_tls_failures_are_the_permanent_categories():
    assert PERMANENT_CATEGORIES == {C.TLS_CERTIFICATE, C.TLS}


# --- real stack over loopback: both transports ------------------------------------------------

@pytest.fixture(scope="module")
def self_signed(tmp_path_factory):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl binary not available")
    directory = tmp_path_factory.mktemp("tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    done = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                           "-out", str(cert), "-days", "2", "-subj", "/CN=localhost"], capture_output=True)
    if done.returncode != 0:
        pytest.skip("could not create a throwaway certificate")
    return cert, key


@pytest.fixture
def tls_server(self_signed):
    cert, key = self_signed
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.2)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            try:
                context.wrap_socket(connection, server_side=True).close()
            except OSError:
                connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    thread.join(2)
    listener.close()


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def post(url, timeout=3):
    return urllib_post_transport(url, {"Content-Type": "application/json"}, f'{{"code": "{SECRET}"}}'.encode(), timeout)


def get(url, timeout=3):
    return urllib_transport(url, {"Authorization": f"app:{SECRET}"}, timeout)


@pytest.mark.parametrize("call", [post, get], ids=["post", "get"])
def test_untrusted_certificate_is_reported_as_a_tls_certificate_failure(tls_server, call):
    """The real-world suspect: urllib raises URLError(SSLCertVerificationError); curl would not notice."""
    url = f"https://127.0.0.1:{tls_server}/api/v3/validate-authcode"
    with pytest.raises(FyersNetworkError) as info:
        call(url)
    error = info.value
    assert (error.category, error.request_sent) == ("tls_certificate", "no")
    message = str(error)
    assert "URLError > SSLCertVerificationError" in message and "self-signed" in message
    assert SECRET not in message and url not in message and str(tls_server) not in message


@pytest.mark.parametrize("call", [post, get], ids=["post", "get"])
def test_refused_connection_is_a_connection_failure(call):
    with pytest.raises(FyersNetworkError) as info:
        call(f"http://127.0.0.1:{free_port()}/x")
    assert (info.value.category, info.value.request_sent) == ("connection", "no")
    assert "ECONNREFUSED" in str(info.value) and SECRET not in str(info.value)


def test_silent_server_is_a_timeout():
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)  # completes the TCP handshake in the kernel but never answers
    try:
        with pytest.raises(FyersNetworkError) as info:
            post(f"http://127.0.0.1:{silent.getsockname()[1]}/x", timeout=0.4)
    finally:
        silent.close()
    assert (info.value.category, info.value.request_sent) == ("timeout", "unknown")
    assert SECRET not in str(info.value)


def test_server_that_hangs_up_is_a_dropped_connection():
    hangup = socket.socket()
    hangup.bind(("127.0.0.1", 0))
    hangup.listen(5)
    threading.Thread(target=lambda: hangup.accept()[0].close(), daemon=True).start()
    try:
        with pytest.raises(FyersNetworkError) as info:
            post(f"http://127.0.0.1:{hangup.getsockname()[1]}/x")
    finally:
        hangup.close()
    assert info.value.category in ("connection", "protocol") and info.value.request_sent == "maybe"


@pytest.mark.parametrize("call", [post, get], ids=["post", "get"])
def test_unresolvable_host_is_a_dns_failure(monkeypatch, call):
    def no_dns(*args, **kwargs):
        raise socket.gaierror(-2, f"Name or service not known {SECRET}")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    with pytest.raises(FyersNetworkError) as info:
        call("https://api-t1.fyers.example/api/v3/validate-authcode")
    assert (info.value.category, info.value.request_sent) == ("dns", "no")
    assert SECRET not in str(info.value) and "fyers.example" not in str(info.value)


def test_http_error_statuses_are_not_network_errors(tls_server):
    """HTTP-level failures keep flowing as HttpResponse so callers can show the status code."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def reply():
        connection, _ = listener.accept()
        connection.recv(4096)
        connection.sendall(b"HTTP/1.0 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        connection.close()

    threading.Thread(target=reply, daemon=True).start()
    try:
        response = post(f"http://127.0.0.1:{listener.getsockname()[1]}/api/v3/validate-authcode")
    finally:
        listener.close()
    assert response.status == 403


# --- retry policy of the GET client ------------------------------------------------------------

def test_certificate_failures_are_not_retried_and_keep_their_category():
    transport = ScriptedTransport(network_error_from(urllib.error.URLError(cert_error())))
    client, sleeps = make_client(transport)
    with pytest.raises(FyersNetworkError) as info:
        client.fetch_history("NSE:NIFTY50-INDEX", "5", 1, 2)
    assert len(transport.calls) == 1 and sleeps == []
    assert info.value.category == "tls_certificate" and "Install Certificates.command" in str(info.value)


def test_transient_failures_are_retried_and_keep_their_category():
    transport = ScriptedTransport(network_error_from(TimeoutError()))
    client, sleeps = make_client(transport, max_retries=2)
    with pytest.raises(FyersNetworkError) as info:
        client.fetch_history("NSE:NIFTY50-INDEX", "5", 1, 2)
    assert len(transport.calls) == 3 and len(sleeps) == 2
    assert info.value.category == "timeout"


def test_a_transient_failure_followed_by_success_still_works():
    transport = ScriptedTransport(network_error_from(ConnectionResetError(errno.ECONNRESET, "x")),
                                  json_response(ok_payload([[1704167100, 1, 2, 3, 4, 0]])))
    client, _ = make_client(transport)
    assert client.fetch_history("NSE:NIFTY50-INDEX", "5", 1, 2)["s"] == "ok"


# --- the diagnose command ----------------------------------------------------------------------

PATHS = ssl.DefaultVerifyPaths(cafile=None, capath=None, openssl_cafile_env="SSL_CERT_FILE",
                               openssl_cafile="/Users/someone/secret-location/cert.pem",
                               openssl_capath_env="SSL_CERT_DIR", openssl_capath="/Users/someone/certs")


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass


class FakeContext:
    def __init__(self, failure=None):
        self.failure = failure

    def wrap_socket(self, raw, server_hostname):
        assert server_hostname == "api-t1.fyers.in"  # certificate must be checked against the real host name
        if self.failure:
            raise self.failure

        class Tls(FakeSocket):
            def version(self):
                return "TLSv1.3"

        return Tls()


def stages_for(*, dns=None, tcp=None, tls=None, http=None, env=None, requests=None):
    def resolver(host, port, type=None):
        if dns:
            raise dns
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.7", 443))]

    def connector(address, timeout):
        assert address == ("api-t1.fyers.in", 443)
        if tcp:
            raise tcp
        return FakeSocket()

    def http_get(url, headers, timeout):
        (requests if requests is not None else []).append((url, dict(headers), timeout))
        if isinstance(http, Exception):
            raise http
        return type("R", (), {"status": http or 200})()

    return diagnose.run_diagnostics(timeout=4, env=env or {}, resolver=resolver, connector=connector,
                                    context_factory=lambda: FakeContext(tls), http_get=http_get,
                                    verify_paths=lambda: PATHS)


def statuses(stages):
    return {s.name: s.status for s in stages}


def test_diagnose_all_stages_pass():
    stages = stages_for()
    assert statuses(stages) == {"python-tls": "info", "dns": "ok", "tcp": "ok", "tls": "ok", "http": "ok"}
    assert "All stages passed" in diagnose.format_report(stages)


def test_diagnose_reports_http_403_as_reachable():
    """The reported curl evidence: an HTTP 403 means the transport works; only the cause above HTTP differs."""
    stages = stages_for(http=403)
    http = next(s for s in stages if s.name == "http")
    assert http.status == "ok" and "HTTP 403" in http.detail


def test_diagnose_dns_failure_skips_the_rest_and_hides_raw_text():
    stages = stages_for(dns=socket.gaierror(-2, f"raw {SECRET}"))
    assert statuses(stages) == {"python-tls": "info", "dns": "fail", "tcp": "skipped", "tls": "skipped",
                                "http": "skipped"}
    report = diagnose.format_report(stages)
    assert "dns" in report and "resolved" in report and SECRET not in report


def test_diagnose_connection_refused():
    stages = stages_for(tcp=ConnectionRefusedError(errno.ECONNREFUSED, "x"))
    assert statuses(stages)["tcp"] == "fail" and statuses(stages)["tls"] == "skipped"
    assert next(s for s in stages if s.name == "tcp").category == "connection"


def test_diagnose_tls_certificate_failure_is_the_macos_case():
    stages = stages_for(tls=cert_error(20))
    assert statuses(stages) == {"python-tls": "info", "dns": "ok", "tcp": "ok", "tls": "fail", "http": "skipped"}
    report = diagnose.format_report(stages)
    assert "unable to get local issuer certificate" in report
    assert "Install Certificates.command" in report and "certifi" in report
    assert SECRET not in report and "library text that must never be shown" not in report
    assert "default CA file present: no" in report


def test_diagnose_http_stage_failure_uses_the_classified_error():
    stages = stages_for(http=network_error_from(TimeoutError()))
    http = next(s for s in stages if s.name == "http")
    assert http.status == "fail" and http.category == "timeout" and "may already be used up" in http.hint


def test_diagnose_sends_no_credentials_and_does_not_touch_the_exchange_endpoint():
    requests = []
    stages_for(requests=requests, env={"FYERS_APP_SECRET": SECRET, "FYERS_ACCESS_TOKEN": SECRET, "FYERS_APP_ID": "x"})
    assert requests == [("https://api-t1.fyers.in/", {}, 4)]  # host root, no headers, no body
    assert "validate-authcode" not in requests[0][0]


def test_diagnose_facts_contain_no_paths_or_environment_values():
    report = diagnose.format_report(stages_for(env={"SSL_CERT_FILE": "/Users/someone/ca.pem", "SSL_CERT_DIR": "/x"}))
    assert "SSL_CERT_FILE set: yes (file exists: no)" in report and "SSL_CERT_DIR set: yes" in report
    for leaked in ("/Users/someone", "ca.pem", "secret-location"):
        assert leaked not in report


def test_diagnose_cli_exit_codes_and_timeout_validation(capsys):
    lines = []
    fakes = dict(resolver=lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("203.0.113.7", 443))],
                 connector=lambda *a, **k: FakeSocket(), context_factory=lambda: FakeContext(),
                 http_get=lambda *a: type("R", (), {"status": 404})(), verify_paths=lambda: PATHS, env={})
    assert diagnose.main(["--timeout", "5"], out=lines.append, **fakes) == 0
    fakes["context_factory"] = lambda: FakeContext(cert_error())
    assert diagnose.main([], out=lines.append, **fakes) == 1
    assert diagnose.main(["--timeout", "0"], out=lines.append, **fakes) == 1
    assert "--timeout must be between" in capsys.readouterr().err
