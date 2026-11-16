#!/usr/bin/env python3
# Copyright (c) 2026 Robin Schultka
# SPDX-License-Identifier: MIT
"""run_flask_endurance.py — 5-Bug Continuous Session Endurance Benchmark on Flask.

Apples-to-apples comparison across harnesses (Hermes vs Cline) and arms
(baseline vs tr_default vs tr_optin), all executing the EXACT SAME 5 sequential
bug fixes inside ONE continuous session on an isolated copy of Flask:

  Bug 1: src/flask/config.py    — Config.get_namespace() inverted lowercase flag
  Bug 2: src/flask/sessions.py  — SessionInterface.should_set_cookie() wrong boolean op
  Bug 3: src/flask/ctx.py       — _AppCtxGlobals.pop() fails to pop / raise KeyError
  Bug 4: src/flask/helpers.py   — get_flashed_messages() inverted category_filter
  Bug 5: src/flask/config.py    — Config.from_mapping() accepts non-uppercase keys
                                  (revisits config.py after Bug 1 -> tests P2 delta reads)

All API traffic routes through an isolated per-arm ForwardProxy + DaemonServer +
MCPBridge (with isolated HOME, SQLite DB, and proxy_usage.csv) so billed cost,
prompt/completion/cached tokens, and prefix_bust_tokens are measured identically.
Live ~/.toolrecall/ state is never touched.

Usage:
    python3 bench/run_flask_endurance.py --dry-run
    python3 bench/run_flask_endurance.py --harness both --seeds 1 --temperature 0.0
    python3 bench/run_flask_endurance.py --harness both --seeds 5 --temperature 0.0
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash-1017"
DEFAULT_FLASK_SRC = Path("/home/hermes/workspace/flask")
RESULTS_DIR = REPO_ROOT / "bench" / "results"
DATA_DIR = REPO_ROOT / "bench" / "data"

# ---------------------------------------------------------------------------
# 5 Realistic Non-Overlapping Bugs + Regression Suite
# ---------------------------------------------------------------------------

BUG_SPECS = [
    {
        "id": "bug_1",
        "title": "Config.get_namespace() fails to lowercase keys when lowercase=True",
        "file": "src/flask/config.py",
        "find": "            if lowercase:\n                key = key.lower()",
        "corrupt": "            if not lowercase:\n                key = key.lower()",
        "prompt": (
            "Bug 1/5: `app.config.get_namespace('IMAGE_STORE_')` is returning uppercase "
            "keys even though `lowercase=True` is the default. Find the root cause in the "
            "codebase, fix it with `patch`, and verify with `run_pytest(k='bug_1')`. "
            "If tests fail, read `pytest_session.log` for details."
        ),
    },
    {
        "id": "bug_2",
        "title": "SessionInterface.should_set_cookie() sets cookie for unmodified non-permanent sessions",
        "file": "src/flask/sessions.py",
        "find": (
            "        return session.modified or (\n"
            '            session.permanent and app.config["SESSION_REFRESH_EACH_REQUEST"]\n'
            "        )"
        ),
        "corrupt": (
            "        return session.modified or (\n"
            '            session.permanent or app.config["SESSION_REFRESH_EACH_REQUEST"]\n'
            "        )"
        ),
        "prompt": (
            "Bug 2/5: `should_set_cookie()` is returning `True` for unmodified, "
            "non-permanent sessions whenever `SESSION_REFRESH_EACH_REQUEST` is `True`. "
            "Find and fix the root cause, then verify with `run_pytest(k='bug_1 or bug_2')`."
        ),
    },
    {
        "id": "bug_3",
        "title": "_AppCtxGlobals.pop() does not remove keys or raise KeyError when no default is given",
        "file": "src/flask/ctx.py",
        "find": (
            "        if default is _sentinel:\n"
            "            return self.__dict__.pop(name)\n"
            "        else:\n"
            "            return self.__dict__.pop(name, default)"
        ),
        "corrupt": (
            "        if default is _sentinel:\n"
            "            return self.__dict__.get(name)\n"
            "        else:\n"
            "            return self.__dict__.pop(name, default)"
        ),
        "prompt": (
            "Bug 3/5: `flask.g.pop('key')` without a default neither removes the attribute "
            "nor raises `KeyError` when the attribute is missing. Find and fix the root cause, "
            "then verify with `run_pytest(k='bug_1 or bug_2 or bug_3')`."
        ),
    },
    {
        "id": "bug_4",
        "title": "get_flashed_messages() inverts category_filter",
        "file": "src/flask/helpers.py",
        "find": "        flashes = list(filter(lambda f: f[0] in category_filter, flashes))",
        "corrupt": "        flashes = list(filter(lambda f: f[0] not in category_filter, flashes))",
        "prompt": (
            "Bug 4/5: `flask.get_flashed_messages(category_filter=['error'])` returns "
            "non-matching categories and excludes `'error'` messages. Find and fix the root cause, "
            "then verify with `run_pytest(k='bug_1 or bug_2 or bug_3 or bug_4')`."
        ),
    },
    {
        "id": "bug_5",
        "title": "Config.from_mapping() stores non-uppercase keys",
        "file": "src/flask/config.py",
        "find": "        for key, value in mappings.items():\n            if key.isupper():\n                self[key] = value",
        "corrupt": "        for key, value in mappings.items():\n            if True:\n                self[key] = value",
        "prompt": (
            "Bug 5/5: `app.config.from_mapping({'SECRET': 'ok', 'lower_key': 'bad'})` is "
            "unexpectedly storing lowercase/mixed-case keys instead of ignoring non-uppercase keys. "
            "Re-check the config implementation, fix the bug, and verify ALL 5 bugs pass with `run_pytest()`."
        ),
    },
]

ENDURANCE_TEST_PY = '''"""Regression tests for the 5-bug continuous Flask endurance benchmark."""
import flask
from flask import Flask, g, get_flashed_messages, flash, session


def test_bug_1_get_namespace_lowercase():
    app = Flask(__name__)
    app.config["IMAGE_STORE_TYPE"] = "fs"
    app.config["IMAGE_STORE_PATH"] = "/var/img"
    ns = app.config.get_namespace("IMAGE_STORE_")
    assert ns == {"type": "fs", "path": "/var/img"}


def test_bug_2_should_set_cookie_unmodified_non_permanent():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True
    with app.test_request_context("/"):
        assert not session.modified
        assert not session.permanent
        assert app.session_interface.should_set_cookie(app, session) is False


def test_bug_3_g_pop_removes_and_raises_keyerror():
    app = Flask(__name__)
    with app.app_context():
        g.token = "abc"
        assert g.pop("token") == "abc"
        assert "token" not in g
        try:
            g.pop("missing")
            raise AssertionError("Expected KeyError")
        except KeyError:
            pass


def test_bug_4_get_flashed_messages_category_filter():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context("/"):
        flash("bad", "error")
        flash("ok", "info")
        assert get_flashed_messages(category_filter=["error"]) == ["bad"]


def test_bug_5_from_mapping_ignores_lowercase():
    app = Flask(__name__)
    app.config.from_mapping({"VALID_KEY": 1, "invalid_key": 2, "MixedCase": 3})
    assert app.config.get("VALID_KEY") == 1
    assert "invalid_key" not in app.config
    assert "MixedCase" not in app.config
'''

BUG_SPECS_16 = [
    {
        "id": "bug_1",
        "title": "Config.get_namespace() fails to lowercase keys when lowercase=True",
        "file": "src/flask/config.py",
        "find": "            if lowercase:\n                key = key.lower()",
        "corrupt": "            if not lowercase:\n                key = key.lower()",
        "prompt": (
            "Bug 1/16: `app.config.get_namespace('IMAGE_STORE_')` is returning uppercase "
            "keys even though `lowercase=True` is the default. Find the root cause in the "
            "codebase, fix it with `patch`, and verify with `run_pytest(k='bug_1')`."
        ),
    },
    {
        "id": "bug_2",
        "title": "SessionInterface.should_set_cookie() sets cookie for unmodified non-permanent sessions",
        "file": "src/flask/sessions.py",
        "find": (
            "        return session.modified or (\n"
            '            session.permanent and app.config["SESSION_REFRESH_EACH_REQUEST"]\n'
            "        )"
        ),
        "corrupt": (
            "        return session.modified or (\n"
            '            session.permanent or app.config["SESSION_REFRESH_EACH_REQUEST"]\n'
            "        )"
        ),
        "prompt": (
            "Bug 2/16: `should_set_cookie()` returns `True` for unmodified, non-permanent "
            "sessions whenever `SESSION_REFRESH_EACH_REQUEST` is `True`. Fix it and verify "
            "with `run_pytest(k='bug_2')`."
        ),
    },
    {
        "id": "bug_3",
        "title": "_AppCtxGlobals.pop() does not remove keys or raise KeyError when no default is given",
        "file": "src/flask/ctx.py",
        "find": (
            "        if default is _sentinel:\n"
            "            return self.__dict__.pop(name)\n"
            "        else:\n"
            "            return self.__dict__.pop(name, default)"
        ),
        "corrupt": (
            "        if default is _sentinel:\n"
            "            return self.__dict__.get(name)\n"
            "        else:\n"
            "            return self.__dict__.pop(name, default)"
        ),
        "prompt": (
            "Bug 3/16: `flask.g.pop('key')` without a default neither removes the attribute "
            "nor raises `KeyError` when missing. Fix it and verify with `run_pytest(k='bug_3')`."
        ),
    },
    {
        "id": "bug_4",
        "title": "get_flashed_messages() inverts category_filter",
        "file": "src/flask/helpers.py",
        "find": "        flashes = list(filter(lambda f: f[0] in category_filter, flashes))",
        "corrupt": "        flashes = list(filter(lambda f: f[0] not in category_filter, flashes))",
        "prompt": (
            "Bug 4/16: `flask.get_flashed_messages(category_filter=['error'])` returns "
            "non-matching categories and excludes `'error'`. Fix it and verify with `run_pytest(k='bug_4')`."
        ),
    },
    {
        "id": "bug_5",
        "title": "Request.blueprint splits from left instead of right on nested blueprints",
        "file": "src/flask/wrappers.py",
        "find": '            return endpoint.rpartition(".")[0]',
        "corrupt": '            return endpoint.partition(".")[0]',
        "prompt": (
            "Bug 5/16: For a nested blueprint endpoint `'parent.child.view'`, `request.blueprint` "
            "returns `'parent'` instead of `'parent.child'`. Fix it and verify with `run_pytest(k='bug_5')`."
        ),
    },
    {
        "id": "bug_6",
        "title": "render_template_string() drops template context",
        "file": "src/flask/templating.py",
        "find": (
            "    template = ctx.app.jinja_env.from_string(source)\n"
            "    return _render(ctx, template, context)"
        ),
        "corrupt": (
            "    template = ctx.app.jinja_env.from_string(source)\n"
            "    return _render(ctx, template, {})"
        ),
        "prompt": (
            "Bug 6/16: `flask.render_template_string('Hello {{ name }}', name='Ada')` renders "
            "`'Hello '` with an empty context. Fix it and verify with `run_pytest(k='bug_6')`."
        ),
    },
    {
        "id": "bug_7",
        "title": "MethodView.dispatch_request() does not fall back to GET for HEAD",
        "file": "src/flask/views.py",
        "find": '        if meth is None and request.method == "HEAD":',
        "corrupt": '        if meth is None and request.method == "POST":',
        "prompt": (
            "Bug 7/16: A `MethodView` with only `get()` defined fails `HEAD` requests with an "
            "unimplemented method error instead of falling back to `get()`. Fix it and verify "
            "with `run_pytest(k='bug_7')`."
        ),
    },
    {
        "id": "bug_8",
        "title": "TagDict.to_python() strips only 1 char instead of 2 from escaped key",
        "file": "src/flask/json/tag.py",
        "find": "        return {key[:-2]: value[key]}",
        "corrupt": "        return {key[:-1]: value[key]}",
        "prompt": (
            "Bug 8/16: `TaggedJSONSerializer` round-trip of `{' t': 'val'}` leaves a trailing "
            "underscore in the decoded dict key (`' t_'`). Fix it and verify with `run_pytest(k='bug_8')`."
        ),
    },
    {
        "id": "bug_9",
        "title": "DefaultJSONProvider.dumps() ignores self.sort_keys",
        "file": "src/flask/json/provider.py",
        "find": '        kwargs.setdefault("sort_keys", self.sort_keys)',
        "corrupt": '        kwargs.setdefault("sort_keys", False)',
        "prompt": (
            "Bug 9/16: Setting `app.json.sort_keys = True` on `DefaultJSONProvider` has no "
            "effect in `app.json.dumps({'b': 1, 'a': 2})`. Fix it and verify with `run_pytest(k='bug_9')`."
        ),
    },
    {
        "id": "bug_10",
        "title": "EnvironBuilder.json_dumps() serializes empty dict instead of obj",
        "file": "src/flask/testing.py",
        "find": "        return self.app.json.dumps(obj, **kwargs)",
        "corrupt": "        return self.app.json.dumps({}, **kwargs)",
        "prompt": (
            "Bug 10/16: `app.test_client().post('/echo', json={'x': 42})` sends `'{}'` instead "
            "of the JSON payload. Fix it and verify with `run_pytest(k='bug_10')`."
        ),
    },
    {
        "id": "bug_11",
        "title": "Scaffold.static_url_path omits leading slash when derived from static_folder",
        "file": "src/flask/sansio/scaffold.py",
        "find": '            return f"/{basename}".rstrip("/")',
        "corrupt": '            return f"{basename}".rstrip("/")',
        "prompt": (
            "Bug 11/16: `Flask(__name__, static_folder='assets').static_url_path` returns "
            "`'assets'` without the leading `'/'`. Fix it and verify with `run_pytest(k='bug_11')`."
        ),
    },
    {
        "id": "bug_12",
        "title": "BlueprintSetupState ignores name option when registering blueprint",
        "file": "src/flask/sansio/blueprints.py",
        "find": '        self.name = self.options.get("name", blueprint.name)',
        "corrupt": "        self.name = blueprint.name",
        "prompt": (
            "Bug 12/16: `app.register_blueprint(bp, name='v2')` registers routes under "
            "`bp.name` instead of `'v2'`. Fix it and verify with `run_pytest(k='bug_12')`."
        ),
    },
    {
        "id": "bug_13",
        "title": "App.select_jinja_autoescape() is case-sensitive on filename extension",
        "file": "src/flask/sansio/app.py",
        "find": '        return filename.lower().endswith((".html", ".htm", ".xml", ".xhtml", ".svg"))',
        "corrupt": '        return filename.endswith((".html", ".htm", ".xml", ".xhtml", ".svg"))',
        "prompt": (
            "Bug 13/16: `app.select_jinja_autoescape('INDEX.HTML')` returns `False` instead of "
            "`True`. Fix it and verify with `run_pytest(k='bug_13')`."
        ),
    },
    {
        "id": "bug_14",
        "title": "prepare_import() reverses dotted module name components",
        "file": "src/flask/cli.py",
        "find": '    return ".".join(module_name[::-1])',
        "corrupt": '    return ".".join(module_name)',
        "prompt": (
            "Bug 14/16: `flask.cli.prepare_import('/tmp/pkg/sub/app.py')` (where `pkg` and "
            "`sub` have `__init__.py`) returns `'app.sub.pkg'` reversed instead of `'pkg.sub.app'`. "
            "Fix it and verify with `run_pytest(k='bug_14')`."
        ),
    },
    {
        "id": "bug_15",
        "title": "Flask.url_for() drops blueprint_name on relative '.endpoint' URLs",
        "file": "src/flask/app.py",
        "find": '                if blueprint_name is not None:\n                    endpoint = f"{blueprint_name}{endpoint}"',
        "corrupt": "                if blueprint_name is not None:\n                    endpoint = endpoint[1:]",
        "prompt": (
            "Bug 15/16: Inside a blueprint request, `url_for('.detail')` drops the blueprint "
            "prefix and fails to build `'bp.detail'`. Fix it and verify with `run_pytest(k='bug_15')`."
        ),
    },
    {
        "id": "bug_16",
        "title": "Config.from_mapping() stores non-uppercase keys",
        "file": "src/flask/config.py",
        "find": "        for key, value in mappings.items():\n            if key.isupper():\n                self[key] = value",
        "corrupt": "        for key, value in mappings.items():\n            if True:\n                self[key] = value",
        "prompt": (
            "Bug 16/16: `app.config.from_mapping({'SECRET': 'ok', 'lower_key': 'bad'})` "
            "unexpectedly stores non-uppercase keys. Re-check the config implementation, fix it, "
            "and verify ALL 16 bugs pass with `run_pytest()`."
        ),
    },
]

ENDURANCE_TEST_16_PY = '''"""Regression tests for the 16-task continuous Flask endurance benchmark."""
from pathlib import Path
from flask import Blueprint, Flask, g, get_flashed_messages, flash, render_template_string, request, session, url_for
from flask.cli import prepare_import
from flask.json.tag import TaggedJSONSerializer
from flask.views import MethodView


def test_bug_1_get_namespace_lowercase():
    app = Flask(__name__)
    app.config["IMAGE_STORE_TYPE"] = "fs"
    app.config["IMAGE_STORE_PATH"] = "/var/img"
    assert app.config.get_namespace("IMAGE_STORE_") == {"type": "fs", "path": "/var/img"}


def test_bug_2_should_set_cookie_unmodified_non_permanent():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True
    with app.test_request_context("/"):
        assert not session.modified
        assert not session.permanent
        assert app.session_interface.should_set_cookie(app, session) is False


def test_bug_3_g_pop_removes_and_raises_keyerror():
    app = Flask(__name__)
    with app.app_context():
        g.token = "abc"
        assert g.pop("token") == "abc"
        assert "token" not in g
        try:
            g.pop("missing")
            raise AssertionError("Expected KeyError")
        except KeyError:
            pass


def test_bug_4_get_flashed_messages_category_filter():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context("/"):
        flash("bad", "error")
        flash("ok", "info")
        assert get_flashed_messages(category_filter=["error"]) == ["bad"]


def test_bug_5_nested_blueprint_property():
    app = Flask(__name__)
    parent = Blueprint("parent", __name__)
    child = Blueprint("child", __name__)

    @child.route("/item")
    def item():
        return request.blueprint or ""

    parent.register_blueprint(child, url_prefix="/child")
    app.register_blueprint(parent, url_prefix="/parent")
    resp = app.test_client().get("/parent/child/item")
    assert resp.get_data(as_text=True) == "parent.child"


def test_bug_6_render_template_string_context():
    app = Flask(__name__)
    with app.app_context():
        assert render_template_string("Hello {{ name }}", name="Ada") == "Hello Ada"


def test_bug_7_method_view_head_fallback():
    app = Flask(__name__)

    class ItemView(MethodView):
        def get(self):
            return "ok"

    app.add_url_rule("/item", view_func=ItemView.as_view("item"))
    resp = app.test_client().head("/item")
    assert resp.status_code == 200


def test_bug_8_tag_dict_roundtrip():
    s = TaggedJSONSerializer()
    data = {" t": "val"}
    assert s.loads(s.dumps(data)) == data


def test_bug_9_json_provider_sort_keys():
    app = Flask(__name__)
    app.json.sort_keys = True
    assert app.json.dumps({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'


def test_bug_10_test_client_json_payload():
    app = Flask(__name__)

    @app.post("/echo")
    def echo():
        return request.get_json()

    resp = app.test_client().post("/echo", json={"x": 42})
    assert resp.get_json() == {"x": 42}


def test_bug_11_static_url_path_leading_slash():
    app = Flask(__name__, static_folder="assets")
    assert app.static_url_path == "/assets"


def test_bug_12_blueprint_register_name_override():
    app = Flask(__name__)
    bp = Blueprint("orig", __name__)

    @bp.route("/ping")
    def ping():
        return "pong"

    app.register_blueprint(bp, name="v2", url_prefix="/v2")
    with app.test_request_context("/"):
        assert url_for("v2.ping") == "/v2/ping"


def test_bug_13_select_jinja_autoescape_case_insensitive():
    app = Flask(__name__)
    assert app.select_jinja_autoescape("INDEX.HTML") is True


def test_bug_14_prepare_import_order(tmp_path: Path):
    pkg = tmp_path / "mypkg" / "subpkg"
    pkg.mkdir(parents=True)
    (tmp_path / "mypkg" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    app_file = pkg / "app.py"
    app_file.write_text("from flask import Flask\\napp = Flask(__name__)\\n")
    assert prepare_import(str(app_file)) == "mypkg.subpkg.app"


def test_bug_15_relative_url_for_in_blueprint():
    app = Flask(__name__)
    bp = Blueprint("bp", __name__)

    @bp.route("/home")
    def home():
        return url_for(".detail")

    @bp.route("/detail")
    def detail():
        return "detail"

    app.register_blueprint(bp, url_prefix="/bp")
    resp = app.test_client().get("/bp/home")
    assert resp.get_data(as_text=True) == "/bp/detail"


def test_bug_16_from_mapping_ignores_lowercase():
    app = Flask(__name__)
    app.config.from_mapping({"VALID_KEY": 1, "invalid_key": 2, "MixedCase": 3})
    assert app.config.get("VALID_KEY") == 1
    assert "invalid_key" not in app.config
    assert "MixedCase" not in app.config
'''


def get_suite(suite: str) -> tuple[list[dict], str]:
    if suite == "16task":
        return BUG_SPECS_16, ENDURANCE_TEST_16_PY
    return BUG_SPECS, ENDURANCE_TEST_PY


# ---------------------------------------------------------------------------
# Helpers: Keys, Ports, Workspace, Process Lifecycle
# ---------------------------------------------------------------------------


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


def seed_flask_workspace(flask_src: Path, dest: Path, suite: str = "5bug") -> Path:
    """Copy Flask src+tests into an isolated workspace and seed all suite bugs."""
    bug_specs, test_py = get_suite(suite)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(flask_src / "src", dest / "src")
    (dest / "tests").mkdir(exist_ok=True)
    (dest / "tests" / "test_endurance_bugs.py").write_text(test_py, encoding="utf-8")
    # Symlink Flask's existing .venv so pytest runs in <1s without copying site-packages
    src_venv = flask_src / ".venv"
    if src_venv.exists():
        os.symlink(src_venv, dest / ".venv")

    for spec in bug_specs:
        fpath = dest / spec["file"]
        text = fpath.read_text(encoding="utf-8")
        if spec["find"] not in text:
            raise RuntimeError(f"Could not locate target snippet in {fpath} for {spec['id']}")
        fpath.write_text(text.replace(spec["find"], spec["corrupt"], 1), encoding="utf-8")
    return dest


def run_flask_pytest(workspace: Path, k_expr: str | None = None) -> tuple[int, str]:
    py = workspace / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)
    cmd = [str(py), "-m", "pytest", "tests/test_endurance_bugs.py", "-v", "--tb=short"]
    if k_expr:
        cmd.extend(["-k", k_expr])
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workspace / "src")
    proc = subprocess.run(
        cmd,
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()


# ---------------------------------------------------------------------------
# Isolated ToolRecall Environment (Daemon + Proxy + MCPBridge)
# ---------------------------------------------------------------------------


class IsolatedEnvironment:
    """Runs an isolated DaemonServer + ForwardProxy (+ optional MCPBridge)."""

    def __init__(
        self,
        *,
        run_tag: str,
        workspace: Path,
        harness: str,
        arm: str,
        seed: int,
    ):
        self.run_tag = run_tag
        self.workspace = workspace
        self.harness = harness
        self.arm = arm
        self.seed = seed

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix=f"tr-endurance-{run_tag}-"))
        (self.home / ".toolrecall").mkdir(parents=True, exist_ok=True)
        self.db_path = DATA_DIR / f"toolrecall-flask5bug-{run_tag}.db"
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
        env["TOOLRECALL_PROXY_USAGE_CSV"] = str(self.usage_csv)
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

        if self.arm == "tr_optin":
            env["TOOLRECALL_INCREMENTAL_READS"] = "true"
            env["TOOLRECALL_INCREMENTAL_MIN_BYTES"] = "2048"
            env["TOOLRECALL_STABILIZE_PREFIX"] = "true"
            env["TOOLRECALL_FAILURE_LEDGER"] = "true"
            env["TOOLRECALL_EGRESS_MODE"] = "redact"
        else:
            env["TOOLRECALL_INCREMENTAL_READS"] = "false"
            env["TOOLRECALL_STABILIZE_PREFIX"] = "false"
            env["TOOLRECALL_FAILURE_LEDGER"] = "false"
            env["TOOLRECALL_EGRESS_MODE"] = "off"
        return env

    def start(self) -> None:
        env = self.build_env()
        # Start ForwardProxy in all arms so proxy_usage.csv records identical billed truth
        proxy_script = (
            f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
            f"from toolrecall.proxy import run_forward_proxy; "
            f"run_forward_proxy(bind='127.0.0.1', port={self.proxy_port})"
        )
        if self.arm != "baseline":
            daemon_script = (
                f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
                f"from toolrecall.daemon import DaemonServer; "
                f"d = DaemonServer(socket_path={str(self.sock_path)!r}); "
                f"d.start()"
            )
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

            # Instantiate in-process MCPBridge pointing to our isolated daemon
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

        client = TransportClient(str(self.sock_path))
        try:
            return client.send({"cmd": "cache_status"})
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
            }
        total_pt = total_ct = total_crt = total_bust = rows = 0
        total_cost = 0.0
        with self.usage_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows += 1
                total_pt += int(row.get("prompt_tokens") or 0)
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
        }


# ---------------------------------------------------------------------------
# Tool Execution (Baseline Native vs ToolRecall MCPBridge)
# ---------------------------------------------------------------------------


def build_openai_tools(arm: str, harness: str) -> list[dict]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List Python files in the Flask repository.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file inside the Flask repository workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative or absolute path"},
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
                    "properties": {
                        "k": {"type": "string", "description": "Optional -k expression"},
                    },
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
    """Execute one tool call; returns (tool_output_text, tracked_file_path_if_read)."""
    if name == "list_files":
        files = sorted(str(p.relative_to(workspace)) for p in (workspace / "src").rglob("*.py"))
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
        rc, output = run_flask_pytest(workspace, k_expr=k_expr)
        # Append to growing session log so tr_optin can read incrementally if desired
        with log_path.open("a", encoding="utf-8") as lf:
            lf.write(f"\n=== pytest k={k_expr!r} rc={rc} ===\n{output}\n")
        if rc != 0:
            return (
                json.dumps(
                    {
                        "exit_code": rc,
                        "summary": "pytest failed; read pytest_session.log for traceback",
                        "log_file": "pytest_session.log",
                    }
                ),
                None,
            )
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


# ---------------------------------------------------------------------------
# Continuous 5-Bug Session Loop
# ---------------------------------------------------------------------------


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
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def evict_clean_files_hermes(messages: list[dict], env_mgr: IsolatedEnvironment) -> int:
    """Hermes-only checkpoint + cost-ranked clean-file eviction (P4.5) when context > 35K est."""
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
    flask_src: Path,
    harness: str,
    arm: str,
    seed: int,
    model: str,
    temperature: float,
    max_turns_per_bug: int,
    dry_run: bool,
    suite: str = "5bug",
) -> dict:
    bug_specs, _ = get_suite(suite)
    run_tag = f"{suite}-{harness}-{arm}-s{seed}"
    ws_dir = Path(tempfile.mkdtemp(prefix=f"flask-ws-{run_tag}-"))
    seed_flask_workspace(flask_src, ws_dir, suite=suite)
    log_path = ws_dir / "pytest_session.log"

    # Pre-check: all seeded bugs must fail before agent runs
    rc0, _out0 = run_flask_pytest(ws_dir)
    if rc0 == 0:
        raise RuntimeError("Expected seeded bugs to fail before agent run, but pytest passed!")

    env_mgr = IsolatedEnvironment(
        run_tag=run_tag,
        workspace=ws_dir,
        harness=harness,
        arm=arm,
        seed=seed,
    )
    t_start = time.time()
    per_bug: list[dict] = []
    solved_count = 0

    try:
        env_mgr.start()
        if dry_run:
            # Exercise read + write + pytest plumbing without calling OpenRouter
            for idx, spec in enumerate(bug_specs, 1):
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
                "suite": suite,
                "harness": harness,
                "arm": arm,
                "seed": seed,
                "dry_run": True,
                "pre_check_rc": rc0,
                "daemon_stats": env_mgr.read_daemon_stats(),
                "wall_s": round(time.time() - t_start, 2),
            }

        api_key = load_openrouter_key()
        tools = build_openai_tools(arm, harness)

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
                    f"<!-- run_id: flask-endurance-{run_tag} -->\n"
                    f"You are fixing {len(bug_specs)} sequential bugs in a Flask repository at `{ws_dir}` "
                    "in one continuous session. For each bug, read only the needed file(s), "
                    "apply a minimal `patch`, run `run_pytest` to confirm tests pass, and "
                    "reply concisely when fixed."
                ),
            }
        ]

        call_seq = 10
        for bug_idx, spec in enumerate(bug_specs, 1):
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

            # Verify cumulative non-regression through bug_idx
            k_expr = " or ".join(b["id"] for b in bug_specs[:bug_idx])
            v_rc, _ = run_flask_pytest(ws_dir, k_expr=k_expr)
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
                    "completion_tokens": (
                        after_usage["completion_tokens"] - before_usage["completion_tokens"]
                    ),
                    "cache_read_tokens": (
                        after_usage["cache_read_tokens"] - before_usage["cache_read_tokens"]
                    ),
                    "cost_usd": round(after_usage["cost_usd"] - before_usage["cost_usd"], 6),
                }
            )

        final_rc, _ = run_flask_pytest(ws_dir)
        return {
            "run_tag": run_tag,
            "suite": suite,
            "harness": harness,
            "arm": arm,
            "seed": seed,
            "model": model,
            "temperature": temperature,
            "all_5_passed": final_rc == 0,
            "all_passed": final_rc == 0,
            "cumulative_solved": solved_count,
            "total_bugs": len(bug_specs),
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
    ap.add_argument("--suite", choices=["5bug", "16task"], default="5bug")
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
    ap.add_argument("--flask-src", type=Path, default=DEFAULT_FLASK_SRC)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    harnesses = ["hermes", "cline"] if args.harness == "both" else [args.harness]
    results: list[dict] = []

    for seed in range(1, args.seeds + 1):
        for harness in harnesses:
            for arm in args.arms:
                print(
                    f"[run] suite={args.suite} harness={harness} arm={arm} "
                    f"seed={seed} dry_run={args.dry_run}",
                    flush=True,
                )
                res = run_single_endurance(
                    flask_src=args.flask_src,
                    harness=harness,
                    arm=arm,
                    seed=seed,
                    model=args.model,
                    temperature=args.temperature,
                    max_turns_per_bug=args.max_turns_per_bug,
                    dry_run=args.dry_run,
                    suite=args.suite,
                )
                results.append(res)
                if not args.dry_run:
                    u = res["usage"]
                    print(
                        f"  -> pass={res['all_passed']} ({res['cumulative_solved']}/{res['total_bugs']}) "
                        f"prompt={u['prompt_tokens']} cached={u['cache_read_tokens']} "
                        f"cost=${u['cost_usd']:.5f} bust={u['cache_bust_tokens']} wall={res['wall_s']}s",
                        flush=True,
                    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"flask_endurance_{args.suite}_{ts}.json"
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"Saved results to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
