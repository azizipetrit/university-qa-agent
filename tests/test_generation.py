"""SQL generation from natural language: what the model is shown, and what is done with its reply.

Offline tests cannot judge whether a real model writes good SQL. They pin down the
two things the code controls: the prompt going in, and the handling of what comes out.
"""

from fakes import MALFORMED, answer, ask, clarify, need_query, sql, unanswerable

from sqlqa.db import Database
from sqlqa.llm import AnswerDecision, SqlDecision

QUESTION = "How many teachers are there?"
COUNT_TEACHERS = "SELECT COUNT(*) AS n FROM teachers"


def test_prompt_contains_the_question_and_the_introspected_schema(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, sql(COUNT_TEACHERS), answer("Four."))
    call = run.llm.calls[0]

    assert call["schema"] is SqlDecision
    assert f"Question: {QUESTION}" in call["human"]
    assert db.describe_schema() in call["system"]  # the live schema, word for word
    assert "in the sqlite dialect" in call["system"]
    assert "A NULL grade means the enrollment is in progress" in call["system"]  # notes file


def test_prompt_for_another_database_contains_that_schema_instead(bookstore_db, tmp_path):
    run = ask(
        bookstore_db,
        tmp_path,
        "How many books are there?",
        sql("SELECT COUNT(*) FROM books"),
        answer("Two."),
    )
    system = run.llm.calls[0]["system"]
    assert "Table books" in system and "Table authors" in system
    assert "teachers" not in system and "enrollments" not in system


def test_generated_sql_and_plan_are_recorded_and_executed(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, sql(COUNT_TEACHERS, plan="Count the rows."), answer("Four."))
    query = run.state["queries"][0]
    assert query["sql"] == COUNT_TEACHERS
    assert query["plan"] == "Count the rows."
    assert query["rows"] == [[4]]


def test_first_prompt_has_no_error_or_earlier_queries(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, sql(COUNT_TEACHERS), answer("Four."))
    human = run.llm.calls[0]["human"]
    assert "rejected" not in human
    assert "Queries already run" not in human


def test_retry_prompt_contains_the_failed_sql_and_the_database_error(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        QUESTION,
        sql("SELECT COUNT(*) FROM teachers WHERE salary > 0"),
        sql(COUNT_TEACHERS),
        answer("Four."),
    )
    retry = run.llm.calls[1]["human"]
    assert "Your previous attempt was rejected." in retry
    assert "SQL: SELECT COUNT(*) FROM teachers WHERE salary > 0" in retry
    assert "no such column: salary" in retry


def test_retry_prompt_contains_the_guard_error(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, sql("SELECT * FROM staff"), sql(COUNT_TEACHERS), answer("4"))
    retry = run.llm.calls[1]["human"]
    assert "Unknown table 'staff'" in retry
    assert "Available tables:" in retry


def test_malformed_reply_counts_as_an_attempt_and_is_reported_back(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, MALFORMED, sql(COUNT_TEACHERS), answer("Four."))
    assert "did not match the required format" in run.llm.calls[1]["human"]
    assert run.state["status"] == "answered"


def test_sql_status_without_sql_is_treated_as_a_failed_attempt(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, sql("   "), sql(COUNT_TEACHERS), answer("Four."))
    assert "no SQL was given" in run.llm.calls[1]["human"]


def test_follow_up_prompt_contains_earlier_results_and_the_reason(db, tmp_path):
    run = ask(
        db,
        tmp_path,
        "Who teaches the most offerings, and what do they teach?",
        sql("SELECT teacher_id, COUNT(*) AS n FROM course_offerings GROUP BY 1 ORDER BY n DESC"),
        need_query("Look up the name of teacher 1."),
        sql("SELECT name FROM teachers WHERE id = 1"),
        answer("Elena Markovic."),
    )
    follow_up = run.llm.calls[2]
    assert follow_up["schema"] is SqlDecision
    assert "Queries already run:" in follow_up["human"]
    assert "SELECT teacher_id, COUNT(*) AS n" in follow_up["human"]
    assert "[1, 3]" in follow_up["human"]  # a row of the first result
    assert "Request for the next query: Look up the name of teacher 1." in follow_up["human"]


def test_answer_prompt_contains_sql_rows_and_the_query_budget(db, tmp_path):
    run = ask(db, tmp_path, QUESTION, sql(COUNT_TEACHERS), answer("Four."))
    call = run.llm.calls[1]
    assert call["schema"] is AnswerDecision
    assert f"SQL: {COUNT_TEACHERS}" in call["human"]
    assert "Result: 1 row(s), columns: n" in call["human"]
    assert "[4]" in call["human"]
    assert "1 of 3 queries have been used" in call["human"]


def test_answer_prompt_flags_empty_null_and_truncated_results(db, tmp_path):
    empty = ask(
        db, tmp_path, "q", sql("SELECT * FROM teachers WHERE name = 'Nobody'"), answer("None.")
    )
    assert "Result: 0 rows. The query matched nothing." in empty.llm.calls[1]["human"]

    null = ask(
        db,
        tmp_path,
        "q",
        sql("SELECT AVG(grade) FROM enrollments WHERE offering_id = 8"),
        answer("Not graded yet."),
    )
    assert "Every value is NULL." in null.llm.calls[1]["human"]

    small = Database(str(db.engine.url), max_rows=5)  # the row limit belongs to the database
    truncated = ask(small, tmp_path, "q", sql("SELECT id FROM enrollments"), answer("Many."))
    assert "truncated: more rows exist" in truncated.llm.calls[1]["human"]


def test_clarify_and_unanswerable_replies_end_without_any_sql(db, tmp_path):
    asked = ask(db, tmp_path, "Who is the best?", clarify("Best by what measure?"))
    assert (asked.state["status"], asked.state["answer"]) == ("clarify", "Best by what measure?")
    assert asked.state["queries"] == []

    refused = ask(db, tmp_path, "What is the weather?", unanswerable("No weather data here."))
    assert refused.state["status"] == "unanswerable"
    assert refused.state["queries"] == []
