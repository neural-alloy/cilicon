"""The free→platform seam: the `registered to 0 fleets` dangle, and registering a
signed boot proof to a Neural Alloy fleet (WEDGE_SPEC §3/§4). All offline."""
import base64
import contextlib
import http.server
import io
import json
import threading
from types import SimpleNamespace

from cilicon.cli import _register_and_dangle

GREEN = [SimpleNamespace(ok=True), SimpleNamespace(ok=True)]


def _run(cfg, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _register_and_dangle(cfg, args, GREEN)
    return buf.getvalue()


def test_dangle_no_fleet():
    out = _run(SimpleNamespace(neural_alloy={}), SimpleNamespace(register=None, attestation=None))
    assert "boot-proven · registered to 0 fleets" in out


def test_dangle_signed():
    out = _run(SimpleNamespace(neural_alloy={}), SimpleNamespace(register=None, attestation="x.dsse"))
    assert "signed, boot-proven · registered to 0 fleets" in out


def test_fleet_set_but_unsigned_warns():
    out = _run(SimpleNamespace(neural_alloy={"fleet": "http://x"}),
               SimpleNamespace(register=None, attestation=None))
    assert "nothing signed to register" in out


def test_register_posts_the_signed_proof(tmp_path):
    captured = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            captured["path"] = self.path
            captured["body"] = json.loads(self.rfile.read(n))
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"units":0}')

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.handle_request, daemon=True).start()

    att = tmp_path / "att.dsse"
    att.write_text(json.dumps({"payloadType": "application/vnd.in-toto+json",
                               "payload": "e30=", "signatures": []}))
    cfg = SimpleNamespace(neural_alloy={"fleet": f"http://127.0.0.1:{port}",
                                        "cohort": "edda", "version": "v9.9.9"})
    out = _run(cfg, SimpleNamespace(register=None, attestation=str(att)))

    assert captured["path"] == "/v1/releases"
    b = captured["body"]
    assert b["version"] == "v9.9.9" and b["cohort"] == "edda"
    assert base64.b64decode(b["boot_test_attestation"]).startswith(b'{"payloadType"')
    assert "registered v9.9.9" in out


def test_register_unreachable_never_raises():
    # a dead fleet endpoint must not fail the run (best-effort)
    cfg = SimpleNamespace(neural_alloy={"fleet": "http://127.0.0.1:9"})
    out = _run(cfg, SimpleNamespace(register=None, attestation="/nonexistent.dsse"))
    assert "register skipped" in out or "unreachable" in out
