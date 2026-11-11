"""The command line: ask, chat and schema. The model is scripted; nothing leaves the machine."""

import pytest
from fakes import FakeLLM, answer, clarify, sql

from sqlqa import cli
from sqlqa.db import Database

COUNT_TEACHERS = "SELECT COUNT(*) AS n FROM teachers"
COUNT_STUDENTS = "SELECT COUNT(*) AS n FROM students"


@pytest.fixture
def environment(monkeypatch, university_db_path, tmp_path):
    """Settings a command would normally read from .env, pointing at the test database."""
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)  # ignore a real .env
    for name in ("SCHEMA_NOTES_FILE", "SAMPLE_ROWS", "MAX_ROWS", "STRUCTURED_OUTPUT_METHOD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{university_db_path.as_posix()}")
    monkeypatch.setenv("LLM_MODEL", "fake")
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "traces"))
    return tmp_path / "traces"


def use_model(monkeypatch, *replies) -> FakeLLM:
    llm = FakeLLM(*replies)
    monkeypatch.setattr(cli, "create_llm", lambda model: llm)
    return llm


def type_lines(monkeypatch, *lines):
    """Feed `lines` to input(), then end the input as Ctrl+D would."""
    remaining = list(lines)

    def fake_input(prompt=""):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


# --- ask ---------------------------------------------------------------------


def test_ask_prints_answer_sql_status_and_trace_path(environment, monkeypatch, capsys):
    use_model(monkeypatch, sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    assert cli.main(["ask", "How many teachers are there?"]) == 0

    out = capsys.readouterr().out
    (trace_file,) = environment.glob("*.json")
    assert "There are 4 teachers." in out
    assert f"Query 1: {COUNT_TEACHERS}" in out
    assert "Status: answered" in out
    assert f"Trace:  {trace_file}" in out


def test_ask_exits_with_1_when_the_run_fails(environment, monkeypatch, capsys):
    use_model(monkeypatch, sql("SELECT * FROM a"), sql("SELECT * FROM b"), sql("SELECT * FROM c"))
    assert cli.main(["ask", "q"]) == 1
    assert "Status: failed" in capsys.readouterr().out


def test_missing_settings_exit_with_2_and_say_what_is_missing(environment, monkeypatch, capsys):
    monkeypatch.delenv("LLM_MODEL")
    assert cli.main(["ask", "q"]) == 2
    assert "Missing setting(s): LLM_MODEL" in capsys.readouterr().err


# --- chat --------------------------------------------------------------------


def test_chat_answers_each_question_and_prints_a_trace_path_each_time(
    environment, monkeypatch, capsys
):
    use_model(
        monkeypatch,
        sql(COUNT_TEACHERS),
        answer("There are 4 teachers."),
        sql(COUNT_STUDENTS),
        answer("There are 8 students."),
    )
    type_lines(monkeypatch, "How many teachers are there?", "And students?", "exit")
    assert cli.main(["chat"]) == 0

    out = capsys.readouterr().out
    traces = sorted(environment.glob("*.json"))
    assert len(traces) == 2  # one trace file per question
    assert out.index("There are 4 teachers.") < out.index("There are 8 students.")
    assert out.count("Trace:  ") == 2
    assert all(f"Trace:  {trace}" in out for trace in traces)


def test_chat_says_at_the_start_that_questions_are_independent(environment, monkeypatch, capsys):
    use_model(monkeypatch)
    type_lines(monkeypatch, "exit")
    cli.main(["chat"])
    out = capsys.readouterr().out
    assert out.startswith("Ask a question about the database.")
    assert "Each question is answered on its own" in out
    assert "earlier questions and answers are not remembered" in out


def test_chat_does_not_carry_one_question_into_the_next(environment, monkeypatch):
    llm = use_model(
        monkeypatch,
        sql(COUNT_TEACHERS),
        answer("There are 4 teachers."),
        clarify("Which students do you mean?"),
    )
    type_lines(monkeypatch, "How many teachers are there?", "And students?")
    cli.main(["chat"])

    second_question_prompt = llm.calls[2]["human"]
    assert second_question_prompt == "Question: And students?"
    assert "teachers" not in second_question_prompt  # nothing from the first question or answer


def test_chat_skips_blank_lines_and_ends_on_end_of_input(environment, monkeypatch, capsys):
    use_model(monkeypatch, sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    type_lines(monkeypatch, "", "   ", "How many teachers are there?")  # then end of input
    assert cli.main(["chat"]) == 0
    assert len(list(environment.glob("*.json"))) == 1
    assert "There are 4 teachers." in capsys.readouterr().out


@pytest.mark.parametrize("word", ["exit", "quit", "EXIT"])
def test_chat_ends_on_exit_or_quit_without_calling_the_model(environment, monkeypatch, word):
    llm = use_model(monkeypatch)
    type_lines(monkeypatch, word, "this line is never read")
    assert cli.main(["chat"]) == 0
    assert llm.calls == []


def test_chat_keeps_going_after_a_question_that_crashes(environment, monkeypatch, capsys):
    class FailsOnce(FakeLLM):
        failed = False

        def with_structured_output(self, schema, include_raw=False, **options):
            if not self.failed:
                self.failed = True
                raise ConnectionError("model provider unreachable")
            return super().with_structured_output(schema)

    llm = FailsOnce(sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    monkeypatch.setattr(cli, "create_llm", lambda model: llm)
    type_lines(monkeypatch, "first question", "How many teachers are there?", "exit")
    assert cli.main(["chat"]) == 0

    out = capsys.readouterr().out
    assert "That question failed: model provider unreachable." in out
    assert "There are 4 teachers." in out
    assert len(list(environment.glob("*.json"))) == 2  # the crashed run left a trace too


# --- schema ------------------------------------------------------------------


def test_schema_prints_exactly_what_the_model_is_shown(
    environment, monkeypatch, capsys, university_db_path
):
    llm = use_model(monkeypatch, sql(COUNT_TEACHERS), answer("There are 4 teachers."))
    assert cli.main(["schema"]) == 0
    printed = capsys.readouterr().out

    expected = Database(f"sqlite:///{university_db_path.as_posix()}").describe_schema()
    assert printed == expected + "\n"
    assert printed.startswith("SQL dialect: sqlite")

    # ...and it is, word for word, inside the prompt of a real run.
    cli.main(["ask", "How many teachers are there?"])
    assert expected in llm.calls[0]["system"]


def test_schema_honours_the_notes_file_and_the_sample_rows_setting(
    environment, monkeypatch, capsys, tmp_path
):
    notes = tmp_path / "notes.md"
    notes.write_text("- A note about the data.", encoding="utf-8")
    monkeypatch.setenv("SCHEMA_NOTES_FILE", str(notes))
    monkeypatch.setenv("SAMPLE_ROWS", "0")
    cli.main(["schema"])
    printed = capsys.readouterr().out
    assert printed.rstrip().endswith("Notes:\n- A note about the data.")
    assert "Sample rows" not in printed


def test_schema_needs_no_model_setting_and_calls_no_model(environment, monkeypatch, capsys):
    monkeypatch.delenv("LLM_MODEL")
    monkeypatch.setattr(cli, "create_llm", lambda model: pytest.fail("no model is needed"))
    assert cli.main(["schema"]) == 0
    assert "Table teachers" in capsys.readouterr().out


def test_schema_still_needs_a_database(environment, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL")
    assert cli.main(["schema"]) == 2
    assert "Missing setting(s): DATABASE_URL" in capsys.readouterr().err
