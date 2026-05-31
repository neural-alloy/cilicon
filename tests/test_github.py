"""Webhook signature (security-critical) + event parsing — both pure."""
import hashlib
import hmac

from cilicon.service import github


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_valid():
    body = b'{"a":1}'
    assert github.verify_signature("sekret", body, _sign("sekret", body))


def test_signature_wrong_secret():
    body = b'{"a":1}'
    assert not github.verify_signature("sekret", body, _sign("other", body))


def test_signature_missing_or_malformed():
    assert not github.verify_signature("s", b"x", "")
    assert not github.verify_signature("s", b"x", "md5=abc")
    assert not github.verify_signature("s", b"x", "deadbeef")


def test_signature_body_tampered():
    sig = _sign("s", b'{"a":1}')
    assert not github.verify_signature("s", b'{"a":2}', sig)


def test_parse_push_runs_on_branch():
    e = github.parse_event("push", {
        "after": "a" * 40, "ref": "refs/heads/main",
        "repository": {"id": 1, "full_name": "o/r", "default_branch": "main", "private": False},
        "installation": {"id": 42}, "sender": {"login": "dev"},
        "head_commit": {"message": "do thing\n\ndetails"},
    })
    assert e.should_run and e.sha == "a" * 40 and e.installation_id == 42
    assert e.message == "do thing"          # first line only
    assert e.private is False


def test_parse_push_skips_branch_delete():
    e = github.parse_event("push", {
        "after": "0" * 40, "ref": "refs/heads/gone",
        "repository": {"id": 1}, "installation": {"id": 42},
    })
    assert not e.should_run


def test_parse_push_skips_tags():
    e = github.parse_event("push", {
        "after": "a" * 40, "ref": "refs/tags/v1",
        "repository": {"id": 1}, "installation": {"id": 42},
    })
    assert not e.should_run


def test_parse_pr_actions():
    base = {"pull_request": {"number": 3, "title": "T", "head": {"sha": "b" * 40, "ref": "f"}},
            "repository": {"id": 1, "full_name": "o/r"}, "installation": {"id": 9}}
    assert github.parse_event("pull_request", {**base, "action": "opened"}).should_run
    assert github.parse_event("pull_request", {**base, "action": "synchronize"}).should_run
    assert not github.parse_event("pull_request", {**base, "action": "closed"}).should_run


def test_parse_installation_event():
    e = github.parse_event("installation", {
        "action": "created",
        "installation": {"id": 77, "account": {"login": "acme", "type": "Organization"}},
    })
    assert e.installation_id == 77 and e.account_login == "acme"
    assert e.account_type == "Organization" and not e.should_run
