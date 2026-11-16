#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
"""run_django_endurance.py — 20-Bug Large-Context Endurance Benchmark on Django 5.1.4.

Apples-to-apples comparison across harnesses (Hermes vs Cline) and arms
(baseline vs tr_default vs tr_optin), all executing the EXACT SAME 20 sequential
bug fixes inside ONE continuous session on an isolated copy of Django 5.1.4.

Designed to push baseline single-turn context toward ~350K-500K+ tokens across
large Django modules (1,000-3,100 lines each) so ToolRecall's clean-file eviction,
file_cache, incremental_reads, and mcp_delta are tested at true long-context scale.

Usage:
    python3 bench/run_django_endurance.py --dry-run
    python3 bench/run_django_endurance.py --harness both --seeds 1 --temperature 0.0
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_DJANGO_SRC = Path("/home/hermes/workspace/django")
RESULTS_DIR = REPO_ROOT / "bench" / "results"
DATA_DIR = REPO_ROOT / "bench" / "data"

BUG_SPECS = [
    {
        "id": "bug_1",
        "file": "django/http/request.py",
        "find": '    def is_secure(self):\n        return self.scheme == "https"',
        "corrupt": '    def is_secure(self):\n        return self.scheme == "http"',
        "prompt": (
            "Bug 1/20: `HttpRequest.is_secure()` returns `False` when `request.scheme == 'https'` "
            "and `True` for `'http'`. Find the root cause, fix it with `patch`, and verify with "
            "`run_pytest(k='bug_1')`."
        ),
    },
    {
        "id": "bug_2",
        "file": "django/template/defaultfilters.py",
        "find": "    return Truncator(value).chars(length)",
        "corrupt": "    return Truncator(value).chars(max(0, length - 1))",
        "prompt": (
            "Bug 2/20: Django's `truncatechars('abcdef', 5)` template filter returns `'abc…'` "
            "(one character too short) instead of `'abcd…'`. Fix it and verify with `run_pytest(k='bug_2')`."
        ),
    },
    {
        "id": "bug_3",
        "file": "django/forms/widgets.py",
        "find": '        if value is True or value is False or value is None or value == "":\n            return',
        "corrupt": '        if value is None or value == "":\n            return',
        "prompt": (
            "Bug 3/20: `CheckboxInput.format_value(True)` returns `'True'` instead of `None`. "
            "Fix it and verify with `run_pytest(k='bug_3')`."
        ),
    },
    {
        "id": "bug_4",
        "file": "django/forms/fields.py",
        "find": "            if self.strip:\n                value = value.strip()",
        "corrupt": "            if not self.strip:\n                value = value.strip()",
        "prompt": (
            "Bug 4/20: `django.forms.CharField(strip=True).to_python('  hi  ')` fails to strip "
            "leading/trailing whitespace. Fix it and verify with `run_pytest(k='bug_4')`."
        ),
    },
    {
        "id": "bug_5",
        "file": "django/db/models/fields/__init__.py",
        "find": '        if value in ("f", "False", "0"):\n            return False',
        "corrupt": '        if value in ("f", "False"):\n            return False',
        "prompt": (
            "Bug 5/20: `django.db.models.BooleanField().to_python('0')` raises `ValidationError` "
            "instead of returning `False`. Fix it and verify with `run_pytest(k='bug_5')`."
        ),
    },
    {
        "id": "bug_6",
        "file": "django/db/models/sql/query.py",
        "find": "            if self.high_mark is not None:\n                self.high_mark = min(self.high_mark, self.low_mark + high)",
        "corrupt": "            if self.high_mark is not None:\n                self.high_mark = max(self.high_mark, self.low_mark + high)",
        "prompt": (
            "Bug 6/20: `Query.set_limits(low=0, high=2)` after an earlier `set_limits(low=0, high=5)` "
            "keeps `high_mark=5` instead of narrowing to `2`. Fix it and verify with `run_pytest(k='bug_6')`."
        ),
    },
    {
        "id": "bug_7",
        "file": "django/db/models/query.py",
        "find": "        clone.query.standard_ordering = not clone.query.standard_ordering",
        "corrupt": "        clone.query.standard_ordering = clone.query.standard_ordering",
        "prompt": (
            "Bug 7/20: `QuerySet.reverse()` does not toggle `clone.query.standard_ordering`. "
            "Fix it and verify with `run_pytest(k='bug_7')`."
        ),
    },
    {
        "id": "bug_8",
        "file": "django/db/models/base.py",
        "find": "        if self._meta.concrete_model != other._meta.concrete_model:\n            return False",
        "corrupt": "        if self._meta.concrete_model == other._meta.concrete_model:\n            return False",
        "prompt": (
            "Bug 8/20: Two unsaved/saved model instances of the same concrete model with identical `pk` "
            "compare unequal (`m1 == m2` is `False`). Fix it and verify with `run_pytest(k='bug_8')`."
        ),
    },
    {
        "id": "bug_9",
        "file": "django/db/models/expressions.py",
        "find": '        return "{}({})".format(self.__class__.__name__, self.name)',
        "corrupt": '        return "{}()".format(self.__class__.__name__)',
        "prompt": (
            "Bug 9/20: `repr(F('price'))` returns `'F()'` instead of `'F(price)'`. "
            "Fix it and verify with `run_pytest(k='bug_9')`."
        ),
    },
    {
        "id": "bug_10",
        "file": "django/db/models/fields/related.py",
        "find": '    def get_attname(self):\n        return "%s_id" % self.name',
        "corrupt": "    def get_attname(self):\n        return self.name",
        "prompt": (
            "Bug 10/20: `ForeignKey.get_attname()` returns `self.name` without the `'_id'` suffix. "
            "Fix it and verify with `run_pytest(k='bug_10')`."
        ),
    },
    {
        "id": "bug_11",
        "file": "django/db/models/lookups.py",
        "find": '        if not self.prepare_rhs or hasattr(self.rhs, "resolve_expression"):',
        "corrupt": '        if self.prepare_rhs and hasattr(self.rhs, "resolve_expression"):',
        "prompt": (
            "Bug 11/20: `Lookup.get_prep_lookup()` ignores `prepare_rhs=False` and still calls "
            "`get_prep_value` on direct values. Fix it and verify with `run_pytest(k='bug_11')`."
        ),
    },
    {
        "id": "bug_12",
        "file": "django/db/migrations/state.py",
        "find": "        new_state.is_delayed = self.is_delayed\n        return new_state",
        "corrupt": "        new_state.is_delayed = not self.is_delayed\n        return new_state",
        "prompt": (
            "Bug 12/20: `ProjectState.clone()` inverts `is_delayed` on the cloned state. "
            "Fix it and verify with `run_pytest(k='bug_12')`."
        ),
    },
    {
        "id": "bug_13",
        "file": "django/template/base.py",
        "find": "                    context.template_name = self.name\n                    return self._render(context)",
        "corrupt": "                    context.template_name = None\n                    return self._render(context)",
        "prompt": (
            "Bug 13/20: `Template.render(context)` fails to bind `context.template_name = self.name`. "
            "Fix it and verify with `run_pytest(k='bug_13')`."
        ),
    },
    {
        "id": "bug_14",
        "file": "django/template/defaulttags.py",
        "find": "        old_setting = context.autoescape\n        context.autoescape = self.setting",
        "corrupt": "        old_setting = context.autoescape\n        context.autoescape = not self.setting",
        "prompt": (
            "Bug 14/20: `{% autoescape off %}` enables autoescaping instead of disabling it. "
            "Fix it and verify with `run_pytest(k='bug_14')`."
        ),
    },
    {
        "id": "bug_15",
        "file": "django/test/client.py",
        "find": '        query_params = {} if query_params is None else query_params\n        return self.generic(\n            "GET",',
        "corrupt": '        query_params = {}\n        return self.generic(\n            "GET",',
        "prompt": (
            "Bug 15/20: `django.test.Client().get('/path', {'a': '1'})` drops the query parameters. "
            "Fix it and verify with `run_pytest(k='bug_15')`."
        ),
    },
    {
        "id": "bug_16",
        "file": "django/urls/resolvers.py",
        "find": '        if args and kwargs:\n            raise ValueError("Don\'t mix *args and **kwargs in call to reverse()!")',
        "corrupt": '        if False:\n            raise ValueError("Don\'t mix *args and **kwargs in call to reverse()!")',
        "prompt": (
            "Bug 16/20: `URLResolver._reverse_with_prefix()` no longer raises `ValueError` when both "
            "`*args` and `**kwargs` are passed. Fix it and verify with `run_pytest(k='bug_16')`."
        ),
    },
    {
        "id": "bug_17",
        "file": "django/db/backends/base/operations.py",
        "find": '    def no_limit_value(self):\n        """\n        Return the value to use for the LIMIT when we are wanting "LIMIT\n        infinity". Return None if the limit clause can be omitted in this case.\n        """\n        raise NotImplementedError(',
        "corrupt": '    def no_limit_value(self):\n        """\n        Return the value to use for the LIMIT when we are wanting "LIMIT\n        infinity". Return None if the limit clause can be omitted in this case.\n        """\n        return 0\n        raise NotImplementedError(',
        "prompt": (
            "Bug 17/20: `BaseDatabaseOperations.no_limit_value()` returns `0` instead of raising "
            "`NotImplementedError`. Fix it and verify with `run_pytest(k='bug_17')`."
        ),
    },
    {
        "id": "bug_18",
        "file": "django/db/models/options.py",
        "find": "            if not self.apps.models_ready:\n                raise FieldDoesNotExist(",
        "corrupt": "            if self.apps.models_ready:\n                raise FieldDoesNotExist(",
        "prompt": (
            "Bug 18/20: `Options.get_field('missing')` omits the `'app cache isn't ready yet'` hint "
            "when `models_ready` is `False` (inverted condition in `get_field`). Fix it and verify "
            "with `run_pytest(k='bug_18')`."
        ),
    },
    {
        "id": "bug_19",
        "file": "django/db/models/fields/related_descriptors.py",
        "find": "    def is_cached(self, instance):\n        return self.field.is_cached(instance)",
        "corrupt": "    def is_cached(self, instance):\n        return not self.field.is_cached(instance)",
        "prompt": (
            "Bug 19/20: `ForwardManyToOneDescriptor.is_cached(instance)` returns the inverted boolean. "
            "Fix it and verify with `run_pytest(k='bug_19')`."
        ),
    },
    {
        "id": "bug_20",
        "file": "django/http/request.py",
        "find": "        if location is None:\n            # Make it an absolute url (but schemeless and domainless) for the\n            # edge case that the path starts with '//'.\n            location = \"//%s\" % self.get_full_path()",
        "corrupt": "        if location is None:\n            # Make it an absolute url (but schemeless and domainless) for the\n            # edge case that the path starts with '//'.\n            location = self.get_full_path()",
        "prompt": (
            "Bug 20/20: Revisit `HttpRequest.build_absolute_uri()` — when `location=None` and "
            "`get_full_path()` starts with `'//evil.com/path'`, it fails to guard with `'//%s'`. "
            "Fix it and verify ALL 20 bugs pass with `run_pytest()`."
        ),
    },
]

ENDURANCE_TEST_PY = '''"""Regression tests for the 20-bug continuous Django endurance benchmark."""
import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth"],
        SECRET_KEY="bench-secret",
        ALLOWED_HOSTS=["*"],
        TEMPLATES=[{"BACKEND": "django.template.backends.django.DjangoTemplates"}],
        ROOT_URLCONF=__name__,
    )
    django.setup()

urlpatterns = []

from types import SimpleNamespace
import pytest
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.db.backends.base.operations import BaseDatabaseOperations
from django.db.migrations.state import ProjectState
from django.db.models import F, Value
from django.db.models.fields import BooleanField
from django.db.models.fields.related import ForeignKey
from django.db.models.fields.related_descriptors import ForwardManyToOneDescriptor
from django.db.models.lookups import Lookup
from django.db.models.sql.query import Query
from django.forms import CharField as FormCharField
from django.forms.widgets import CheckboxInput
from django.http import HttpRequest
from django.template import Context, Engine
from django.template.defaultfilters import truncatechars
from django.test import Client
from django.urls.resolvers import URLResolver, RegexPattern


def test_bug_1_http_request_is_secure():
    req = HttpRequest()
    req._get_scheme = lambda: "https"
    assert req.is_secure() is True
    req._get_scheme = lambda: "http"
    assert req.is_secure() is False


def test_bug_2_truncatechars_length():
    assert truncatechars("abcdef", 5) == "abcd…"


def test_bug_3_checkbox_input_format_value_bool():
    w = CheckboxInput()
    assert w.format_value(True) is None
    assert w.format_value(False) is None


def test_bug_4_form_charfield_strip():
    f = FormCharField(strip=True)
    assert f.to_python("  hello  ") == "hello"


def test_bug_5_model_booleanfield_zero():
    f = BooleanField()
    assert f.to_python("0") is False


def test_bug_6_sql_query_set_limits_narrows():
    from django.contrib.auth.models import User
    q = Query(User)
    q.set_limits(low=0, high=5)
    q.set_limits(low=0, high=2)
    assert q.high_mark == 2


def test_bug_7_queryset_reverse_toggles_ordering():
    from django.contrib.auth.models import User
    qs = User.objects.all()
    assert qs.reverse().query.standard_ordering is False


def test_bug_8_model_equality_same_pk():
    from django.contrib.auth.models import User
    u1 = User(pk=7, username="a")
    u2 = User(pk=7, username="b")
    assert u1 == u2


def test_bug_9_f_expression_repr():
    assert repr(F("price")) == "F(price)"


def test_bug_10_foreign_key_attname():
    from django.contrib.auth.models import User
    fk = ForeignKey(User, on_delete=models.CASCADE)
    fk.set_attributes_from_name("author")
    assert fk.get_attname() == "author_id"


def test_bug_11_lookup_prepare_rhs_false():
    class DummyField:
        def get_prep_value(self, v):
            return "PREPPED"
    class RawLookup(Lookup):
        prepare_rhs = False
    lhs = SimpleNamespace(output_field=DummyField())
    lk = RawLookup(lhs, "raw")
    assert lk.get_prep_lookup() == "raw"


def test_bug_12_project_state_clone_is_delayed():
    st = ProjectState()
    st.is_delayed = False
    assert st.clone().is_delayed is False


def test_bug_13_template_render_binds_template_name():
    from django.template.base import Node, NodeList
    eng = Engine()
    class CaptureNode(Node):
        def render(self, c):
            return str(c.template_name)
    tpl2 = eng.from_string("")
    tpl2.name = "my_tpl.html"
    tpl2.nodelist = NodeList([CaptureNode()])
    assert tpl2.render(Context({})) == "my_tpl.html"


def test_bug_14_autoescape_off_tag():
    eng = Engine()
    tpl = eng.from_string("{% autoescape off %}{{ val }}{% endautoescape %}")
    assert tpl.render(Context({"val": "<b>x</b>"})) == "<b>x</b>"


def test_bug_15_test_client_get_query_params():
    c = Client()
    resp = c.get("/missing/", {"a": "1"})
    assert resp.wsgi_request.GET.get("a") == "1"


def test_bug_16_url_resolver_no_mix_args_kwargs():
    r = URLResolver(RegexPattern(r"^$"), [])
    with pytest.raises(ValueError):
        r._reverse_with_prefix("x", "/", 1, a=2)


def test_bug_17_base_db_operations_no_limit_value():
    ops = BaseDatabaseOperations(connection=None)
    with pytest.raises(NotImplementedError):
        ops.no_limit_value()


def test_bug_18_options_get_field_not_ready_message():
    from django.contrib.auth.models import User
    opts = User._meta
    orig_apps = opts.apps
    opts.apps = SimpleNamespace(models_ready=False)
    try:
        with pytest.raises(FieldDoesNotExist, match="app cache isn't ready yet"):
            opts.get_field("definitely_missing_xyz")
    finally:
        opts.apps = orig_apps


def test_bug_19_forward_many_to_one_is_cached():
    fake_field = SimpleNamespace(is_cached=lambda inst: True)
    desc = ForwardManyToOneDescriptor(fake_field)
    assert desc.is_cached(object()) is True


def test_bug_20_build_absolute_uri_double_slash():
    req = HttpRequest()
    req.META = {"SERVER_NAME": "example.com", "SERVER_PORT": "80"}
    req.path = "//evil.com/path"
    assert req.build_absolute_uri() == "http://example.com//evil.com/path"
'''


def load_openrouter_key() -> str:
    for var in ("OPENROUTER_API_KEY", "TOOLRECALL_API_KEY"):
        val = (os.environ.get(var) or "").strip().strip('"').strip("'")
        if val:
            return val
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        for line in env_file.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY=") and not line.startswith("#"):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    raise RuntimeError("OPENROUTER_API_KEY not found in environment or ~/.hermes/.env")


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def seed_django_workspace(django_src: Path, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(django_src / "django", dest / "django")
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "tests" / "test_endurance_bugs.py").write_text(ENDURANCE_TEST_PY, encoding="utf-8")
    src_venv = django_src / ".venv"
    if src_venv.exists():
        os.symlink(src_venv, dest / ".venv")

    for spec in BUG_SPECS:
        fpath = dest / spec["file"]
        text = fpath.read_text(encoding="utf-8")
        if spec["find"] not in text:
            raise RuntimeError(f"Could not locate target snippet in {fpath} for {spec['id']}")
        fpath.write_text(text.replace(spec["find"], spec["corrupt"], 1), encoding="utf-8")
    return dest


def run_django_pytest(workspace: Path, k_expr: str | None = None) -> tuple[int, str]:
    py = workspace / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    cmd = [str(py), "-m", "pytest", "tests/test_endurance_bugs.py", "-v", "--tb=short"]
    if k_expr:
        cmd.extend(["-k", k_expr])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace)
    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()


class IsolatedEnvironment:
    def __init__(self, *, run_tag: str, workspace: Path, harness: str, arm: str, seed: int):
        self.run_tag = run_tag
        self.workspace = workspace
        self.harness = harness
        self.arm = arm
        self.seed = seed

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix=f"tr-django-{run_tag}-"))
        (self.home / ".toolrecall").mkdir(parents=True, exist_ok=True)
        self.db_path = DATA_DIR / f"toolrecall-django-{run_tag}.db"
        if self.db_path.exists():
            self.db_path.unlink()
        self.usage_csv = self.home / ".toolrecall" / "proxy_usage.csv"
        self.saved_usage_csv = DATA_DIR / f"usage-{run_tag}.csv"
        if self.saved_usage_csv.exists():
            self.saved_usage_csv.unlink()
        self.sock_path = self.home / ".toolrecall" / "daemon.sock"
        self.proxy_port = pick_free_port()

        self._daemon_proc: subprocess.Popen | None = None
        self._proxy_proc: subprocess.Popen | None = None
        self.bridge = None

    def build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["TOOLRECALL_NO_AUTOSTART"] = "1"
        env["TOOLRECALL_SOCKET"] = str(self.sock_path)
        env["TOOLRECALL_CACHE_DB"] = str(self.db_path)
        env["TOOLRECALL_KNOWLEDGE_DB"] = str(self.home / ".toolrecall" / "knowledge.db")
        env["TOOLRECALL_MCP_ALLOWED_PATHS"] = str(self.workspace)
        env["TOOLRECALL_MCP_ALLOW_TERMINAL"] = "true"
        env["TOOLRECALL_MCP_ALLOWED_TERMINAL_COMMANDS"] = json.dumps(
            ["pytest", "python", "python3"]
        )
        env["TOOLRECALL_MCP_EMIT_CONTEXT_HINTS"] = "true" if self.harness == "hermes" else "false"
        env["TOOLRECALL_MCP_TOOL_ACCESS_CONTROL"] = "false"
        env["TOOLRECALL_MCP_COGNITIVE_CHECK_ENABLED"] = "false"
        env["TOOLRECALL_MCP_AST_CHECK_ENABLED"] = "false"
        env["TOOLRECALL_UPDATE_ENABLED"] = "false"
        env["TOOLRECALL_API_TTL"] = "0"
        env["TOOLRECALL_FILE_TTL"] = "-1"
        if self.arm == "tr_optin":
            env["TOOLRECALL_INCREMENTAL_READS"] = "true"
            env["TOOLRECALL_INCREMENTAL_MIN_BYTES"] = "2048"
            env["TOOLRECALL_MCP_DELTA"] = "true"
            env["TOOLRECALL_MCP_DEDUP"] = "true"
            env["TOOLRECALL_STABILIZE_PREFIX"] = "true"
            env["TOOLRECALL_FAILURE_LEDGER"] = "true"
            env["TOOLRECALL_EGRESS_MODE"] = "redact"
        else:
            env["TOOLRECALL_INCREMENTAL_READS"] = "false"
            env["TOOLRECALL_MCP_DELTA"] = "false"
            env["TOOLRECALL_MCP_DEDUP"] = "false"
            env["TOOLRECALL_STABILIZE_PREFIX"] = "false"
            env["TOOLRECALL_FAILURE_LEDGER"] = "false"
            env["TOOLRECALL_EGRESS_MODE"] = "off"
        return env

    def start(self) -> None:
        env = self.build_env()
        if self.arm != "baseline":
            daemon_script = (
                "import sys; sys.path.insert(0, {!r}); "
                "from toolrecall.daemon import DaemonServer; "
                "d = DaemonServer(socket_path={!r}); "
                "d.start()"
            ).format(str(REPO_ROOT), str(self.sock_path))
            self._daemon_proc = subprocess.Popen(
                [sys.executable, "-c", daemon_script],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.time() + 10.0
            while not self.sock_path.exists():
                if time.time() > deadline:
                    raise RuntimeError("Isolated daemon socket did not appear within 10s")
                time.sleep(0.05)

            prev_env = {k: os.environ.get(k) for k in env}
            try:
                for k, v in env.items():
                    if k.startswith("TOOLRECALL_") or k == "HOME":
                        os.environ[k] = v
                from toolrecall.mcp_bridge import MCPBridge

                self.bridge = MCPBridge(
                    socket_path=str(self.sock_path),
                    emit_context_hints=(self.harness == "hermes"),
                )
            finally:
                for k, old in prev_env.items():
                    if k.startswith("TOOLRECALL_") or k == "HOME":
                        if old is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = old

        proxy_script = (
            "import sys; sys.path.insert(0, {!r}); "
            "from toolrecall.proxy import run_forward_proxy; "
            "run_forward_proxy(bind='127.0.0.1', port={!r})"
        ).format(str(REPO_ROOT), self.proxy_port)
        self._proxy_proc = subprocess.Popen(
            [sys.executable, "-c", proxy_script],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10.0
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=0.5):
                    break
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("Isolated proxy port did not open within 10s")
                time.sleep(0.05)

    def stop(self) -> None:
        for proc in (self._proxy_proc, self._daemon_proc):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self.usage_csv.exists():
            shutil.copy2(self.usage_csv, self.saved_usage_csv)
        if self.home.exists():
            shutil.rmtree(self.home, ignore_errors=True)

    def read_daemon_stats(self) -> dict:
        if self.arm == "baseline" or not self.sock_path.exists():
            return {}
        from toolrecall.transport import TransportClient

        try:
            return TransportClient(str(self.sock_path)).send({"cmd": "cache_status"})
        except Exception:
            return {}

    def read_usage_summary(self) -> dict:
        if not self.usage_csv.exists():
            return {
                "rows": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
                "cache_bust_tokens": 0,
                "max_single_turn_pt": 0,
                "last_single_turn_pt": 0,
            }
        total_pt = total_ct = total_crt = total_bust = rows = max_pt = last_pt = 0
        total_cost = 0.0
        with self.usage_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows += 1
                pt = int(row.get("prompt_tokens") or 0)
                last_pt = pt
                if pt > max_pt:
                    max_pt = pt
                total_pt += pt
                total_ct += int(row.get("completion_tokens") or 0)
                total_crt += int(row.get("cache_read_tokens") or 0)
                total_bust += int(row.get("cache_bust_tokens") or 0)
                total_cost += float(row.get("cost_usd") or 0.0)
        return {
            "rows": rows,
            "prompt_tokens": total_pt,
            "completion_tokens": total_ct,
            "cache_read_tokens": total_crt,
            "cost_usd": total_cost,
            "cache_bust_tokens": total_bust,
            "max_single_turn_pt": max_pt,
            "last_single_turn_pt": last_pt,
        }


def build_openai_tools(arm: str) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List Python source files under django/.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file inside the repository.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "patch",
                "description": "Find and replace an exact string in a file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_pytest",
                "description": "Run pytest on tests/test_endurance_bugs.py (optionally with -k filter).",
                "parameters": {
                    "type": "object",
                    "properties": {"k": {"type": "string"}},
                    "required": [],
                },
            },
        },
    ]
    if arm == "tr_optin":
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "failures_search",
                    "description": "Search normalized recurring test/tool failure resolutions.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        )
    return tools


def execute_tool(
    *,
    name: str,
    args: dict,
    workspace: Path,
    env_mgr: IsolatedEnvironment,
    log_path: Path,
    call_idx: int,
) -> tuple[str, str | None]:
    if name == "list_files":
        files = sorted(str(p.relative_to(workspace)) for p in (workspace / "django").rglob("*.py"))
        return json.dumps({"files": files}), None

    if name == "read_file":
        raw_path = str(args.get("path", ""))
        abs_path = (
            str((workspace / raw_path).resolve()) if not os.path.isabs(raw_path) else raw_path
        )
        if env_mgr.arm == "baseline" or env_mgr.bridge is None:
            try:
                content = Path(abs_path).read_text(encoding="utf-8")
                if args.get("offset") or args.get("limit"):
                    lines = content.splitlines(keepends=True)
                    start = max(0, int(args.get("offset") or 1) - 1)
                    end = start + int(args.get("limit")) if args.get("limit") else len(lines)
                    content = "".join(lines[start:end])
                return json.dumps({"content": content, "path": abs_path}), abs_path
            except Exception as e:
                return json.dumps({"error": str(e)}), None
        rpc_args: dict = {"path": abs_path}
        if args.get("offset"):
            rpc_args["offset"] = int(args["offset"])
        if args.get("limit"):
            rpc_args["limit"] = int(args["limit"])
        resp = env_mgr.bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": call_idx,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": rpc_args},
            }
        )
        return resp["result"]["content"][0]["text"], abs_path

    if name == "patch":
        raw_path = str(args.get("path", ""))
        abs_path = (
            str((workspace / raw_path).resolve()) if not os.path.isabs(raw_path) else raw_path
        )
        old_s = str(args.get("old_string", ""))
        new_s = str(args.get("new_string", ""))
        if env_mgr.arm == "baseline" or env_mgr.bridge is None:
            try:
                p = Path(abs_path)
                txt = p.read_text(encoding="utf-8")
                if old_s not in txt:
                    return json.dumps({"error": f"old_string not found in {abs_path}"}), None
                p.write_text(txt.replace(old_s, new_s, 1), encoding="utf-8")
                return json.dumps({"patched": True, "path": abs_path}), abs_path
            except Exception as e:
                return json.dumps({"error": str(e)}), None
        resp = env_mgr.bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": call_idx,
                "method": "tools/call",
                "params": {
                    "name": "patch",
                    "arguments": {"path": abs_path, "old_string": old_s, "new_string": new_s},
                },
            }
        )
        return resp["result"]["content"][0]["text"], abs_path

    if name == "run_pytest":
        k_expr = args.get("k")
        rc, output = run_django_pytest(workspace, k_expr=k_expr)
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(f"\n=== pytest k={k_expr!r} rc={rc} ===\n{output}\n")
        if rc != 0:
            return json.dumps(
                {
                    "exit_code": rc,
                    "summary": "pytest failed; read pytest_session.log",
                    "log_file": "pytest_session.log",
                }
            ), None
        return json.dumps({"exit_code": rc, "output": output}), None

    if name == "failures_search" and env_mgr.bridge is not None:
        resp = env_mgr.bridge.handle_request(
            {
                "jsonrpc": "2.0",
                "id": call_idx,
                "method": "tools/call",
                "params": {
                    "name": "failures_search",
                    "arguments": {"query": str(args.get("query", ""))},
                },
            }
        )
        return resp["result"]["content"][0]["text"], None

    return json.dumps({"error": f"Unknown tool {name}"}), None


def call_openrouter_via_proxy(
    *,
    proxy_port: int,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    temperature: float,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "temperature": temperature,
        "max_tokens": 2048,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Target-Host": "openrouter.ai",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def evict_clean_files_hermes(messages: list[dict], env_mgr: IsolatedEnvironment) -> int:
    if env_mgr.bridge is None:
        return 0
    est_tokens = sum(len(json.dumps(m)) for m in messages) // 4
    if est_tokens < 35000:
        return 0
    dirty_resp = env_mgr.bridge.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9001,
            "method": "tools/call",
            "params": {"name": "context_get_dirty", "arguments": {}},
        }
    )
    raw = json.loads(dirty_resp["result"]["content"][0]["text"])
    clean_paths = set(raw.get("clean", raw.get("clean_files", [])))
    if not clean_paths:
        return 0

    evicted = 0
    for msg in messages:
        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            for cp in clean_paths:
                if cp in msg["content"] and len(msg["content"]) > 400:
                    msg["content"] = json.dumps({"evicted_clean": True, "path": cp})
                    evicted += 1
                    break

    env_mgr.bridge.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9002,
            "method": "tools/call",
            "params": {"name": "context_set_checkpoint", "arguments": {}},
        }
    )
    return evicted


def run_single_endurance(
    *,
    django_src: Path,
    harness: str,
    arm: str,
    seed: int,
    model: str,
    temperature: float,
    max_turns_per_bug: int,
    dry_run: bool,
) -> dict:
    run_tag = f"django20-{harness}-{arm}-s{seed}"
    ws_dir = Path(tempfile.mkdtemp(prefix=f"django-ws-{run_tag}-"))
    seed_django_workspace(django_src, ws_dir)
    log_path = ws_dir / "pytest_session.log"

    rc0, _out0 = run_django_pytest(ws_dir)
    if rc0 == 0:
        raise RuntimeError(
            "Expected seeded Django bugs to fail before agent run, but pytest passed!"
        )

    env_mgr = IsolatedEnvironment(
        run_tag=run_tag, workspace=ws_dir, harness=harness, arm=arm, seed=seed
    )
    t_start = time.time()
    per_bug: list[dict] = []
    solved_count = 0

    try:
        env_mgr.start()
        if dry_run:
            for idx, spec in enumerate(BUG_SPECS, 1):
                txt, _ = execute_tool(
                    name="read_file",
                    args={"path": spec["file"]},
                    workspace=ws_dir,
                    env_mgr=env_mgr,
                    log_path=log_path,
                    call_idx=idx * 10 + 1,
                )
                assert "content" in txt or "unchanged" in txt or "diff" in txt
            return {
                "run_tag": run_tag,
                "harness": harness,
                "arm": arm,
                "seed": seed,
                "dry_run": True,
                "pre_check_rc": rc0,
                "daemon_stats": env_mgr.read_daemon_stats(),
                "wall_s": round(time.time() - t_start, 2),
            }

        api_key = load_openrouter_key()
        tools = build_openai_tools(arm)
        if harness == "hermes" and env_mgr.bridge is not None:
            env_mgr.bridge.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "context_set_checkpoint", "arguments": {}},
                }
            )

        messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    f"<!-- run_id: django-endurance-{run_tag} -->\n"
                    f"You are fixing {len(BUG_SPECS)} sequential bugs in a Django repository at `{ws_dir}` "
                    "in one continuous session. For each bug, read only the needed file(s), "
                    "apply a minimal `patch`, run `run_pytest` to confirm tests pass, and "
                    "reply concisely when fixed."
                ),
            }
        ]

        call_seq = 10
        for bug_idx, spec in enumerate(BUG_SPECS, 1):
            bug_t0 = time.time()
            before_usage = env_mgr.read_usage_summary()
            messages.append({"role": "user", "content": spec["prompt"]})

            turns_used = 0
            for _turn in range(max_turns_per_bug):
                turns_used += 1
                resp = call_openrouter_via_proxy(
                    proxy_port=env_mgr.proxy_port,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                )
                choice = resp["choices"][0]["message"]
                messages.append(choice)
                tool_calls = choice.get("tool_calls") or []
                if not tool_calls:
                    break
                for tc in tool_calls:
                    call_seq += 1
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        fn_args = {}
                    out_text, _ = execute_tool(
                        name=fn_name,
                        args=fn_args,
                        workspace=ws_dir,
                        env_mgr=env_mgr,
                        log_path=log_path,
                        call_idx=call_seq,
                    )
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": out_text})

            k_expr = " or ".join(b["id"] for b in BUG_SPECS[:bug_idx])
            v_rc, _ = run_django_pytest(ws_dir, k_expr=k_expr)
            if v_rc == 0:
                solved_count = bug_idx

            evicted = 0
            if harness == "hermes" and env_mgr.bridge is not None:
                evicted = evict_clean_files_hermes(messages, env_mgr)

            after_usage = env_mgr.read_usage_summary()
            per_bug.append(
                {
                    "bug": spec["id"],
                    "turns": turns_used,
                    "evicted": evicted,
                    "cumulative_pass": v_rc == 0,
                    "wall_s": round(time.time() - bug_t0, 2),
                    "prompt_tokens": after_usage["prompt_tokens"] - before_usage["prompt_tokens"],
                    "completion_tokens": after_usage["completion_tokens"]
                    - before_usage["completion_tokens"],
                    "cache_read_tokens": after_usage["cache_read_tokens"]
                    - before_usage["cache_read_tokens"],
                    "max_single_turn_pt": after_usage["max_single_turn_pt"],
                    "last_single_turn_pt": after_usage["last_single_turn_pt"],
                    "cost_usd": round(after_usage["cost_usd"] - before_usage["cost_usd"], 6),
                }
            )

        final_rc, _ = run_django_pytest(ws_dir)
        return {
            "run_tag": run_tag,
            "harness": harness,
            "arm": arm,
            "seed": seed,
            "model": model,
            "temperature": temperature,
            "all_passed": final_rc == 0,
            "cumulative_solved": solved_count,
            "total_bugs": len(BUG_SPECS),
            "usage": env_mgr.read_usage_summary(),
            "per_bug": per_bug,
            "daemon_stats": env_mgr.read_daemon_stats(),
            "wall_s": round(time.time() - t_start, 2),
        }
    finally:
        env_mgr.stop()
        shutil.rmtree(ws_dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harness", choices=["hermes", "cline", "both"], default="both")
    ap.add_argument(
        "--arms",
        nargs="+",
        default=["baseline", "tr_default", "tr_optin"],
        choices=["baseline", "tr_default", "tr_optin"],
    )
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-turns-per-bug", type=int, default=10)
    ap.add_argument("--django-src", type=Path, default=DEFAULT_DJANGO_SRC)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    harnesses = ["hermes", "cline"] if args.harness == "both" else [args.harness]
    results: list[dict] = []

    for seed in range(1, args.seeds + 1):
        for harness in harnesses:
            for arm in args.arms:
                print(
                    f"[run] django20 harness={harness} arm={arm} seed={seed} dry_run={args.dry_run}",
                    flush=True,
                )
                res = run_single_endurance(
                    django_src=args.django_src,
                    harness=harness,
                    arm=arm,
                    seed=seed,
                    model=args.model,
                    temperature=args.temperature,
                    max_turns_per_bug=args.max_turns_per_bug,
                    dry_run=args.dry_run,
                )
                results.append(res)
                if not args.dry_run:
                    u = res["usage"]
                    print(
                        f"  -> pass={res['all_passed']} ({res['cumulative_solved']}/{res['total_bugs']}) "
                        f"prompt={u['prompt_tokens']} peak_turn={u['max_single_turn_pt']} "
                        f"last_turn={u['last_single_turn_pt']} cached={u['cache_read_tokens']} "
                        f"cost=${u['cost_usd']:.5f} bust={u['cache_bust_tokens']} wall={res['wall_s']}s",
                        flush=True,
                    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"django_endurance_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Saved results to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
