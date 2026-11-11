"""A scripted stand-in for the chat model, so the agent can be tested offline."""

import json
from pathlib import Path

from sqlqa.config import Settings
from sqlqa.graph import run
from sqlqa.llm import AnswerDecision, FinalAnswer, SqlDecision

MALFORMED = object()  # script this to simulate a reply that does not fit the schema


class FakeLLM:
    """Returns the scripted replies in order and remembers every prompt it was given."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []  # {"schema": ..., "system": ..., "human": ...}

    def with_structured_output(self, schema, include_raw=False, **_options):
        return _Structured(self, schema)


class _Structured:
    def __init__(self, fake: FakeLLM, schema):
        self.fake, self.schema = fake, schema

    def invoke(self, messages):
        self.fake.calls.append({"schema": self.schema, **dict(messages)})
        assert self.fake.replies, "the agent called the model more often than the test scripted"
        reply = self.fake.replies.pop(0)
        if reply is MALFORMED:
            return {"raw": None, "parsed": None, "parsing_error": ValueError("not valid JSON")}
        # The script must match what the node asks for; otherwise the test is wrong.
        assert isinstance(reply, self.schema), (
            f"scripted {type(reply).__name__}, node wanted {self.schema.__name__}"
        )
        return {"raw": None, "parsed": reply, "parsing_error": None}


# Short constructors so test scripts read like a conversation.


def sql(query: str, plan: str = "test plan") -> SqlDecision:
    return SqlDecision(status="sql", plan=plan, sql=query)


def clarify(question: str) -> SqlDecision:
    return SqlDecision(status="clarify", plan="", message=question)


def unanswerable(why: str) -> SqlDecision:
    return SqlDecision(status="unanswerable", plan="", message=why)


def answer(text: str) -> AnswerDecision:
    return AnswerDecision(status="answer", answer=text)


def need_query(reason: str) -> AnswerDecision:
    return AnswerDecision(status="need_query", reason=reason)


def final(text: str, complete: bool = True) -> FinalAnswer:
    return FinalAnswer(answer=text, complete=complete)


class Run:
    """The outcome of one agent run in a test."""

    def __init__(self, state: dict, trace_path: Path, llm: FakeLLM):
        self.state = state
        self.trace_path = trace_path
        self.trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.llm = llm
        self.path = [step["node"] for step in self.trace["steps"]]


def ask(db, tmp_path, question: str, *replies, **settings) -> Run:
    """Run the real graph against `db` with a scripted model."""
    llm = FakeLLM(*replies)
    config = Settings(
        database_url="unused: db is passed in",
        llm_model="fake",
        trace_dir=str(tmp_path),
        **settings,
    )
    state, trace_path = run(question, llm, db, config)
    assert not llm.replies, f"{len(llm.replies)} scripted replies were never used"
    return Run(state, trace_path, llm)
