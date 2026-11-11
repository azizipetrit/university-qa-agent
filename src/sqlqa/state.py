"""The state that flows through the graph."""

import operator
from typing import Annotated, TypedDict


class Query(TypedDict):
    """One query that ran successfully, with the reason it was run."""

    reason: str  # what this query was needed for
    plan: str  # how the model said the SQL answers it
    sql: str
    columns: list[str]
    rows: list[list]
    truncated: bool
    filter_check: dict | None  # set by check_filters when the result was empty


class State(TypedDict, total=False):
    question: str

    # set by load_schema
    schema: str
    tables: list[str]
    dialect: str

    # the query currently being worked on
    reason: str  # what it must find
    plan: str
    sql: str | None
    error: str | None  # why the last attempt failed; sent back to the model
    attempts: int  # attempts at the current query
    pending: Query | None  # an empty result, held until check_filters has looked at it

    # the whole run
    queries: Annotated[list[Query], operator.add]  # nodes append; LangGraph concatenates
    llm_calls: int

    # set once, by whichever node ends the run
    status: str  # answered | clarify | unanswerable | failed
    answer: str
    verification: dict | None  # set by verify_answer: figures checked, figures not found


def is_empty(rows: list[list]) -> bool:
    """No rows, or nothing but NULLs (an aggregate over no rows returns one NULL row)."""
    return all(value is None for row in rows for value in row)


def initial_state(question: str) -> State:
    return {
        "question": question,
        "reason": "Answer the user's question.",
        "sql": None,
        "error": None,
        "attempts": 0,
        "pending": None,
        "queries": [],
        "llm_calls": 0,
    }
