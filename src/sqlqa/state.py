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

    # the whole run
    queries: Annotated[list[Query], operator.add]  # nodes append; LangGraph concatenates
    llm_calls: int

    # set once, by whichever node ends the run
    status: str  # answered | clarify | unanswerable | failed
    answer: str


def initial_state(question: str) -> State:
    return {
        "question": question,
        "reason": "Answer the user's question.",
        "sql": None,
        "error": None,
        "attempts": 0,
        "queries": [],
        "llm_calls": 0,
    }
