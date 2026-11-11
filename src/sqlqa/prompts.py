"""Prompt text. Everything about a particular database arrives through placeholders."""

import json

from sqlqa.state import State

SQL_SYSTEM = """\
You write SQL for a question-answering system. You are given a database schema and a request.

Rules for the query:
- One read-only SELECT statement in the {dialect} dialect. CTEs and subqueries are allowed.
- Use only tables and columns that appear in the schema.
- Read the notes at the end of the schema, if there are any. They say what the data means.
- Sample rows show how values are written. They are not the full data. Write filter values in the \
same format.
- When the request names a person or thing, also select a column that identifies it uniquely, \
because names can repeat.
- Do not add LIMIT unless the request asks for a top or bottom N.

Choose a status:
- "sql": you can write the query. Fill in `plan` and `sql`.
- "clarify": the request cannot be mapped to the schema without guessing what the user means. \
Ask one question in `message`. Use this rarely: if a reasonable reading exists, take it and state \
the assumption in `plan`.
- "unanswerable": the schema does not hold this information, or the request is not about this \
database. Explain in `message`.

Schema:
{schema}"""

ANSWER_SYSTEM = """\
You answer a user's question from the results of SQL queries that were run on a database.

Rules for the answer:
- Use only the results shown. Never invent a value.
- Refer to a person by name, or as "they". The data does not say whether someone is a "he" or \
a "she", so do not use those words.
- Be brief and plain, and include the figures.
- If a query made an assumption (see its plan), say so.
- If the results contain several different entities matching one name, report each one \
separately, told apart by a unique column.
- If a result is NULL, the value is not available. Say that, and why if the schema notes explain \
it. Do not report it as zero.
- If a result has 0 rows, judge whether "nothing matches" is the true answer or the query was \
probably wrong, for example a filter value written differently from the data.
- If a result is marked truncated, say that only the first rows are shown.

Schema:
{schema}"""

MORE_QUERIES_ALLOWED = """\
{used} of {limit} queries have been used. Choose a status:
- "answer": the results are enough. Write the answer.
- "need_query": the question cannot be answered without another query. Say in `reason` what it \
must find and why. Ask only when the answer truly depends on it."""

NO_MORE_QUERIES = """\
The limit of {limit} queries is reached, so no more can be run. Answer with what is available \
and say what is missing. Set `complete` to false if the results do not fully answer the question."""


def sql_messages(state: State) -> list[tuple[str, str]]:
    parts = [f"Question: {state['question']}"]
    if state["queries"]:
        parts.append("Queries already run:\n\n" + format_queries(state["queries"]))
        parts.append(f"Request for the next query: {state['reason']}")
    if state["error"]:
        parts.append(
            "Your previous attempt was rejected.\n"
            f"SQL: {state['sql'] or '(none)'}\n"
            f"Error: {state['error']}\n"
            "Write a corrected query."
        )
    system = SQL_SYSTEM.format(dialect=state["dialect"], schema=state["schema"])
    return [("system", system), ("human", "\n\n".join(parts))]


def answer_messages(state: State, max_queries: int) -> list[tuple[str, str]]:
    used = len(state["queries"])
    instruction = (
        MORE_QUERIES_ALLOWED.format(used=used, limit=max_queries)
        if used < max_queries
        else NO_MORE_QUERIES.format(limit=max_queries)
    )
    human = "\n\n".join(
        [f"Question: {state['question']}", format_queries(state["queries"]), instruction]
    )
    return [("system", ANSWER_SYSTEM.format(schema=state["schema"])), ("human", human)]


def format_queries(queries: list) -> str:
    """Each query with its reason, SQL and result, as the model will read them."""
    blocks = []
    for number, query in enumerate(queries, start=1):
        rows = query["rows"]
        if not rows:
            summary = "0 rows. The query matched nothing."
        else:
            summary = f"{len(rows)} row(s), columns: {', '.join(query['columns'])}"
            if query["truncated"]:
                summary += " (truncated: more rows exist, only the first are shown)"
            if all(value is None for row in rows for value in row):
                summary += ". Every value is NULL."
        lines = [
            f"Query {number}",
            f"Reason: {query['reason']}",
            f"Plan: {query['plan']}",
            f"SQL: {query['sql']}",
            f"Result: {summary}",
            *[json.dumps(row) for row in rows],
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
