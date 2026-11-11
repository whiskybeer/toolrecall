"""The shim verdict must be PER-VENV, not "a shim works somewhere".

Live failure this pins (2026-11-11): three venvs discovered, one shimmed, the
interpreter that actually ran the agent missing the package entirely — and
``toolrecall healthcheck`` reported ``shim=active`` because the old probe did
``"applied: yes" in out`` over the whole multi-venv output.
"""

import toolrecall.healthcheck as hc

MIXED = """Discovered 3 venv(s):
  /opt/a/bin/python3.14
  /opt/b/bin/python3.14
❌ /opt/a
     python: /opt/a/bin/python3.14
     package: MISSING | shim: missing | applied: no | probe: FAIL
✅ /opt/b
     python: /opt/b/bin/python3.14
     package: ok | shim: present | applied: yes | probe: pass
❌ /srv/agent-runtime
     python: /srv/agent-runtime/bin/python3.11
     package: MISSING | shim: missing | applied: no | probe: FAIL
"""

ALL_GOOD = """Discovered 1 venv(s):
✅ /opt/b
     python: /opt/b/bin/python3.14
     package: ok | shim: present | applied: yes | probe: pass
"""


def test_parser_splits_venvs_and_keeps_bare_rows():
    rows = hc._parse_shim_venvs(MIXED)
    assert [r["root"] for r in rows] == ["/opt/a", "/opt/b", "/srv/agent-runtime"]
    assert [r["applied"] for r in rows] == [False, True, False]
    assert [r["probe_ok"] for r in rows] == [False, True, False]


def test_aggregate_is_active_but_bare_venvs_are_reported(monkeypatch):
    monkeypatch.setattr(hc, "_required_venv_globs", lambda: [])
    probe = hc._classify_shim_output(MIXED, 1.0)
    assert probe["shim_state"] == hc.SHIM_ACTIVE
    assert probe["shim_bare"] == ["/opt/a", "/srv/agent-runtime"]
    assert probe["shim_bare_required"] == []


def test_required_venv_bare_is_a_failure(monkeypatch):
    monkeypatch.setattr(hc, "_required_venv_globs", lambda: ["/srv/agent-*"])
    probe = hc._classify_shim_output(MIXED, 1.0)
    assert probe["shim_state"] == hc.SHIM_INACTIVE
    assert probe["shim_reason"] == "required_venv_bare"
    assert probe["shim_bare_required"] == ["/srv/agent-runtime"]


def test_all_working_required_venv_is_active(monkeypatch):
    monkeypatch.setattr(hc, "_required_venv_globs", lambda: ["/opt/b"])
    probe = hc._classify_shim_output(ALL_GOOD, 1.0)
    assert probe["shim_state"] == hc.SHIM_ACTIVE
    assert probe["shim_bare"] == []
    assert len(probe["shim_venvs"]) == 1


def test_unparseable_output_falls_back_to_substring(monkeypatch):
    monkeypatch.setattr(hc, "_required_venv_globs", lambda: [])
    probe = hc._classify_shim_output("applied: yes\nprobe: pass\n", 0.5)
    assert probe["shim_state"] == hc.SHIM_ACTIVE
    assert probe["shim_venvs"] == []
    assert probe["shim_bare"] == []


def test_applied_but_probe_failing_is_inactive(monkeypatch):
    monkeypatch.setattr(hc, "_required_venv_globs", lambda: [])
    out = """✅ /opt/b
     package: ok | shim: present | applied: yes | probe: FAIL
"""
    probe = hc._classify_shim_output(out, 1.0)
    assert probe["shim_state"] == hc.SHIM_INACTIVE
    assert probe["shim_reason"] == "probe_failed"
