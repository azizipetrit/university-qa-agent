"""The model, the shapes it must answer in, and the one function that calls it."""

import time
from typing import Literal

from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field


class SqlDecision(BaseModel):
    """What generate_sql asks the model for."""

    status: Literal["sql", "clarify", "unanswerable"]
    plan: str = Field(
        description="One or two sentences: how the query answers the request, "
        "including any assumption made. Empty if status is not 'sql'."
    )
    sql: str | None = Field(default=None, description="The query, when status is 'sql'.")
    message: str | None = Field(
        default=None,
        description="When status is 'clarify': one question for the user. "
        "When 'unanswerable': why the database cannot answer.",
    )


class AnswerDecision(BaseModel):
    """What answer asks the model for while more queries are still allowed."""

    status: Literal["answer", "need_query"]
    answer: str | None = Field(default=None, description="The final answer for the user.")
    reason: str | None = Field(
        default=None,
        description="When status is 'need_query': what the next query must find, and why.",
    )


class FinalAnswer(BaseModel):
    """What answer asks the model for once the query limit is reached."""

    answer: str
    complete: bool = Field(description="False if the results do not fully answer the question.")


def create_llm(model: str):
    """Any chat model langchain knows, chosen by name."""
    return init_chat_model(model)


def call_llm(llm, schema: type[BaseModel], messages: list[tuple[str, str]], tracer, method=None):
    """Ask for a `schema` object. Returns it, or None if the reply did not fit the schema.

    Every model call in the application goes through here, so every call is traced.
    """
    options = {"method": method} if method else {}
    started = time.perf_counter()
    result = llm.with_structured_output(schema, include_raw=True, **options).invoke(messages)
    parsed, raw, error = result["parsed"], result["raw"], result["parsing_error"]
    tracer.llm_call(
        schema=schema.__name__,
        messages=[list(message) for message in messages],
        output=parsed.model_dump() if parsed else None,
        raw=_without_signatures(getattr(raw, "content", None)),
        usage=getattr(raw, "usage_metadata", None),
        error=str(error) if error else None,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return parsed


def _without_signatures(content):
    """Drop the long opaque `signature` some providers attach to reasoning blocks.

    It is only meaningful to the provider and makes traces hard to read.
    """
    if not isinstance(content, list):
        return content
    return [
        {key: value for key, value in block.items() if key != "signature"}
        if isinstance(block, dict)
        else block
        for block in content
    ]
