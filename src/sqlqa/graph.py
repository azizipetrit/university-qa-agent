"""The graph: which node runs after which, and when the run stops.

    load_schema -> generate_sql -> validate_sql -> execute_sql -> answer -> END

Three things can send the run back to generate_sql: the guard rejects the SQL, the
database rejects it, or the answer node asks for another query. Three limits stop it:
attempts per query, queries per question, and model calls per run.
"""

from pathlib import Path

from langgraph.graph import END, START, StateGraph

from sqlqa.config import Settings
from sqlqa.db import Database
from sqlqa.nodes import Nodes
from sqlqa.state import State, initial_state
from sqlqa.tracing import Tracer

# LangGraph stops a run after this many node executions. The limits above stop it
# far earlier; this only needs to be higher than the worst case they allow.
RECURSION_LIMIT = 60


def build_graph(llm, db: Database, tracer: Tracer, settings: Settings):
    nodes = Nodes(llm, db, tracer, settings)

    def calls_left(state: State) -> bool:
        return state["llm_calls"] < settings.max_llm_calls

    def retry_or_give_up(state: State) -> str:
        if state["attempts"] < settings.max_attempts and calls_left(state):
            return "generate_sql"
        return "give_up"

    def after_generate(state: State) -> str:
        if state.get("status"):  # clarify or unanswerable
            return END
        if state["error"]:  # the model's reply was unusable
            return retry_or_give_up(state)
        return "validate_sql"

    def after_validate(state: State) -> str:
        return retry_or_give_up(state) if state["error"] else "execute_sql"

    def after_execute(state: State) -> str:
        if state["error"]:
            return retry_or_give_up(state)
        return "answer" if calls_left(state) else "give_up"

    def after_answer(state: State) -> str:
        if state.get("status"):  # answered, or failed
            return END
        return "generate_sql" if calls_left(state) else "give_up"  # another query was requested

    graph = StateGraph(State)
    for name in (
        "load_schema",
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "answer",
        "give_up",
    ):
        graph.add_node(name, tracer.wrap(name, getattr(nodes, name)))

    graph.add_edge(START, "load_schema")
    graph.add_edge("load_schema", "generate_sql")
    graph.add_conditional_edges(
        "generate_sql", after_generate, ["validate_sql", "generate_sql", "give_up", END]
    )
    graph.add_conditional_edges(
        "validate_sql", after_validate, ["execute_sql", "generate_sql", "give_up"]
    )
    graph.add_conditional_edges("execute_sql", after_execute, ["answer", "generate_sql", "give_up"])
    graph.add_conditional_edges("answer", after_answer, ["generate_sql", "give_up", END])
    graph.add_edge("give_up", END)
    return graph.compile()


def run(question: str, llm, db: Database, settings: Settings) -> tuple[State, Path]:
    """Answer one question. Returns the final state and the path of the trace file."""
    tracer = Tracer(settings.trace_dir, question, settings.llm_model)
    graph = build_graph(llm, db, tracer, settings)
    try:
        state = graph.invoke(initial_state(question), {"recursion_limit": RECURSION_LIMIT})
    except Exception as exc:
        tracer.finish(error=repr(exc))  # a crashed run still leaves a trace
        raise
    return state, tracer.finish(state)
