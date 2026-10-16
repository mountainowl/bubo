import ast
import html
import importlib.util
import inspect
import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("reliability_pilot", HERE / "run.py")
pilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pilot)
CASES = json.loads((HERE / "cases.json").read_text())


def decode_sections(prompt, variant):
    if variant == "xml":
        return [
            (html.unescape(title), html.unescape(body))
            for title, body in re.findall(
                r'<section name="([^"]+)">\n(.*?)\n</section>', prompt, re.S
            )
        ]
    if variant == "hybrid":
        prefix, evidence = prompt.rsplit('<section name="Evidence">\n', 1)
        pieces = [
            (p.partition("\n")[0], p.partition("\n")[2].strip())
            for p in re.split(r"(?m)^## ", prefix)
            if p.strip()
        ]
        return [*pieces, ("Evidence", html.unescape(evidence.removesuffix("\n</section>")))]
    return [
        (p.partition("\n")[0], p.partition("\n")[2].strip())
        for p in re.split(r"(?m)^## ", prompt)
        if p.strip()
    ]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
@pytest.mark.parametrize("variant", pilot.FORMATS)
def test_formats_preserve_identical_content_without_oracle(case, variant):
    prompt = pilot.render(case, variant)
    assert decode_sections(prompt, variant) == pilot.canonical_sections(case)
    evidence = json.loads(dict(decode_sections(prompt, variant))["Evidence"])
    assert set(evidence) == {"mode", "revision", "requirements", "sources"}
    assert evidence["revision"] not in ("synthetic-bug", "synthetic-correct")
    assert case["id"] not in prompt
    assert case["oracle"]["description"] not in prompt


def test_pairs_have_identical_requirements_and_honest_provenance():
    assert len(CASES) == 12
    for family in {c["family"] for c in CASES}:
        pair = [c for c in CASES if c["family"] == family]
        assert len(pair) == 2
        assert pair[0]["requirements"] == pair[1]["requirements"]
        assert {c["oracle"]["known_defect"] for c in pair} == {True, False}
        assert all(c["sources"] for c in pair)


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["family"] == "provider-credential"], ids=lambda c: c["id"]
)
def test_historical_credential_oracle_with_fake_values(case):
    source = "\n\n".join(c["text"] for c in case["sources"])
    nodes = [
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.Assign)
        or (isinstance(n, ast.FunctionDef) and n.name == "classifier_env")
    ]
    namespace = {
        "_EXTRA_PATH": "/fake-extra",
        "ROOT": Path("/fake-root"),
        "Mapping": Mapping,
        "ReviewConfig": object,
        "os": SimpleNamespace(environ={}),
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "<historical-fixture>", "exec"), namespace)
    fake = {
        "PATH": "/fake",
        "LLM_API_KEY": "fake-test-key",
        "LLM_BASE_URL": "https://test.invalid",
        "GITHUB_TOKEN": "fake-scm",
    }
    fn = namespace["classifier_env"]
    actual = (
        fn(fake, SimpleNamespace(llm_base_url="https://test.invalid"))
        if "cfg" in inspect.signature(fn).parameters
        else fn(fake)
    )
    assert ("LLM_API_KEY" not in actual) == case["oracle"]["known_defect"]
    assert "GITHUB_TOKEN" not in actual
    if not case["oracle"]["known_defect"]:
        ordinary = fn(fake, SimpleNamespace(llm_base_url=None))
        assert "LLM_API_KEY" not in ordinary
        assert "LLM_BASE_URL" not in ordinary


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["family"] == "zero-order-report"], ids=lambda c: c["id"]
)
def test_sql_oracle_executes_exact_snapshot(case):
    with sqlite3.connect(":memory:") as db:
        db.executescript(
            "CREATE TABLE customers(id INTEGER PRIMARY KEY); "
            "CREATE TABLE orders(id INTEGER PRIMARY KEY,customer_id INTEGER,status TEXT);"
            " INSERT INTO customers VALUES(1),(2),(3);"
            " INSERT INTO orders VALUES(1,2,'unpaid'),(2,3,'paid'),(3,3,'paid');"
        )
        rows = sorted(db.execute(case["sources"][0]["text"]).fetchall())
    assert (rows != [(1, 0), (2, 0), (3, 2)]) == case["oracle"]["known_defect"]


@pytest.mark.parametrize(("mode", "expected"), [("audit", "complete"), ("diff", "failed")])
def test_pilot_requires_requested_mode_and_records_model(tmp_path, monkeypatch, mode, expected):
    from types import SimpleNamespace

    result = SimpleNamespace(
        stdout=json.dumps(
            {
                "schema_version": 1,
                "status": "complete",
                "mode": mode,
                "findings": [],
                "coverage": {"reviewed": ["fixture"], "unreviewed": [], "limitations": []},
                "questions": [],
                "omitted_findings_count": 0,
            }
        ),
        diagnostic_stdout="",
        stderr="model: test-model\n",
        returncode=0,
    )

    def fake_run(command, **kwargs):
        assert kwargs["review_config"].model == "test-model"
        return result

    (tmp_path / "blinded").mkdir()
    monkeypatch.setattr(pilot, "run_review", fake_run)
    record = pilot.run_one((CASES[0], "markdown", 0), tmp_path, 10, "test-model")
    assert record["status"] == expected
    assert record["requested_model"] == record["reported_model"] == "test-model"
    if expected == "failed":
        assert "Wrong review mode" in record["error"]
