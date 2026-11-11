"""Live evaluation: real questions, real model, checked against known answers.

Not run by default. It needs the settings in .env and costs a few cents:

    uv run pytest -m live

Each golden question lists the values that a correct run must have fetched from the
database. Comparing whole result sets would fail correct queries that merely select
an extra column, so only the key values are checked.

A question marked "missing_filter" names a value that is not in the database. For those,
the run must also contain a filter check that reported the value as not found. Such a run
may end as "unanswerable" after its query: once the check has shown the value does not
exist, saying the database cannot answer is as correct as saying so in an answer.
"""

import json
import os
from pathlib import Path

import pytest
from dotenv import find_dotenv, load_dotenv

from sqlqa.config import Settings
from sqlqa.db import Database
from sqlqa.graph import run
from sqlqa.llm import create_llm

pytestmark = pytest.mark.live

ROOT = Path(__file__).parent.parent
GOLDEN = json.loads((ROOT / "university" / "golden_questions.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def live(university_db_path):
    load_dotenv(find_dotenv(usecwd=True))
    if not os.environ.get("LLM_MODEL"):
        pytest.skip("LLM_MODEL is not set; copy .env.example to .env")
    settings = Settings(
        database_url=f"sqlite:///{university_db_path.as_posix()}",
        llm_model=os.environ["LLM_MODEL"],
        structured_output_method=os.environ.get("STRUCTURED_OUTPUT_METHOD") or None,
        trace_dir=str(ROOT / "traces" / "live"),
    )
    db = Database(settings.database_url, notes_file=ROOT / "university" / "notes.md")
    return settings, db, create_llm(settings.llm_model)


def found(expected, cells: list) -> bool:
    """Is `expected` among the values the queries returned?"""
    if isinstance(expected, list):  # any one of several acceptable values
        return any(found(option, cells) for option in expected)
    if expected is None:
        return None in cells
    if isinstance(expected, int | float):
        numbers = [c for c in cells if isinstance(c, int | float) and not isinstance(c, bool)]
        return any(abs(number - expected) < 0.01 for number in numbers)
    return any(isinstance(c, str) and expected.lower() in c.lower() for c in cells)


@pytest.mark.parametrize("case", GOLDEN, ids=[case["question"] for case in GOLDEN])
def test_golden_question(case, live):
    settings, db, llm = live
    state, trace_path = run(case["question"], llm, db, settings)
    where = f"\nanswer: {state['answer']}\ntrace: {trace_path}"

    assert state["status"] in case["status"], where
    if state["status"] != "answered" and not case.get("missing_filter"):
        assert state["queries"] == [], "no SQL should run for this question" + where

    cells = [value for query in state["queries"] for row in query["rows"] for value in row]
    missing = [expected for expected in case["expect"] if not found(expected, cells)]
    assert not missing, f"values not found in any query result: {missing}" + where

    if case.get("missing_filter"):
        # The question names a value that does not exist; check_filters must have said so.
        checks = [
            item
            for query in state["queries"]
            for item in (query["filter_check"] or {}).get("checked", [])
        ]
        assert any(item["found"] is False for item in checks), (
            "check_filters did not report a missing value" + where
        )
