"""vuln.py is pure: parse a scan + judge a policy, no grype installed."""
from cilicon import vuln


def _scan(*rows):
    # grype-shaped: matches[].{vulnerability{id,severity,fix}, artifact{name,version}}
    return {"matches": [
        {"vulnerability": {"id": i, "severity": s, "fix": {"versions": [fx] if fx else []}},
         "artifact": {"name": pkg, "version": ver}}
        for (i, s, pkg, ver, fx) in rows
    ]}


CRIT = ("CVE-2024-0001", "Critical", "openssl", "3.0.0", "3.0.1")
HIGH = ("CVE-2024-0002", "High", "zlib", "1.2.0", "")
LOW = ("CVE-2024-0003", "Low", "curl", "8.0.0", "")


def test_parse_reads_grype_matches():
    hits = vuln.parse(_scan(CRIT, LOW))
    assert {h.id for h in hits} == {"CVE-2024-0001", "CVE-2024-0003"}
    crit = next(h for h in hits if h.id == "CVE-2024-0001")
    assert crit.severity == "critical" and crit.package == "openssl" and crit.fixed_in == "3.0.1"


def test_none_policy_reports_but_never_gates():
    r = vuln.evaluate(_scan(CRIT, HIGH), policy="none")
    assert r.ok is True and "critical" in r.detail and "high" in r.detail


def test_critical_policy_blocks_critical():
    r = vuln.evaluate(_scan(CRIT, LOW), policy="critical")
    assert r.ok is False and r.blocked == ["CVE-2024-0001"]


def test_high_policy_blocks_high_and_critical():
    r = vuln.evaluate(_scan(CRIT, HIGH, LOW), policy="high")
    assert r.ok is False and set(r.blocked) == {"CVE-2024-0001", "CVE-2024-0002"}
    assert "CVE-2024-0003" not in r.blocked          # low doesn't gate


def test_waiver_reports_but_does_not_gate():
    r = vuln.evaluate(_scan(CRIT), policy="critical", waivers=["CVE-2024-0001"])
    assert r.ok is True and r.waived == ["CVE-2024-0001"] and not r.blocked


def test_kev_policy_needs_a_catalog_else_degrades_honestly():
    # no catalog -> honest report-only, never a silent pass/fail
    r = vuln.evaluate(_scan(LOW), policy="kev", kev_ids=None)
    assert r.ok is True and "no KEV catalog" in r.detail
    # with a catalog, an unwaived KEV gates even at Low severity
    r2 = vuln.evaluate(_scan(LOW), policy="kev", kev_ids={"CVE-2024-0003"})
    assert r2.ok is False and r2.blocked == ["CVE-2024-0003"]


def test_kev_gates_under_a_severity_policy_too():
    r = vuln.evaluate(_scan(LOW), policy="critical", kev_ids={"CVE-2024-0003"})
    assert r.ok is False and r.blocked == ["CVE-2024-0003"]   # KEV overrides severity floor


def test_clean_scan_is_ok():
    assert vuln.evaluate({"matches": []}, policy="critical").ok is True
    assert vuln.evaluate(None, policy="critical").ok is True
