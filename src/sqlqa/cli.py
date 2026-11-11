"""Command line for the question-answering agent.

uv run sqlqa ask "How many rows are in the largest table?"
uv run sqlqa chat                                  # ask several questions, one after another
uv run sqlqa schema                                # the schema text exactly as the model sees it
uv run sqlqa render-trace traces/<run>.json        # a trace file as Markdown
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from sqlqa.config import Settings
from sqlqa.db import Database
from sqlqa.graph import run
from sqlqa.llm import create_llm
from sqlqa.tracing import render_markdown

CHAT_INTRO = (
    "Ask a question about the database. Each question is answered on its own: "
    "earlier questions and answers are not remembered.\n"
    "Type 'exit' to leave."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlqa", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser("ask", help="answer one question about the database")
    ask.add_argument("question")
    commands.add_parser("chat", help="ask several questions; each is answered on its own")
    commands.add_parser("schema", help="print the schema text exactly as the model sees it")
    render = commands.add_parser("render-trace", help="print a trace file as Markdown")
    render.add_argument("trace_file", type=Path)
    render.add_argument("-o", "--output", type=Path, help="write to this file instead")

    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codec
    if args.command == "render-trace":
        return _render(args.trace_file, args.output)

    load_dotenv(find_dotenv(usecwd=True))  # .env in the directory the command is run from
    try:
        settings = Settings.from_env(need_model=args.command != "schema")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    db = Database(
        settings.database_url,
        max_rows=settings.max_rows,
        sample_rows=settings.sample_rows,
        notes_file=settings.schema_notes_file,
    )

    if args.command == "schema":
        print(db.describe_schema())
        return 0
    llm = create_llm(settings.llm_model)
    if args.command == "chat":
        return _chat(llm, db, settings)
    return _answer(args.question, llm, db, settings)


def _answer(question: str, llm, db: Database, settings: Settings) -> int:
    """Run one question and print the answer, the SQL, the status and the trace path."""
    state, trace_path = run(question, llm, db, settings)
    print(state["answer"])
    print()
    for number, query in enumerate(state["queries"], start=1):
        print(f"Query {number}: {query['sql']}")
    print(f"Status: {state['status']}")
    print(f"Trace:  {trace_path}")
    return 0 if state["status"] != "failed" else 1


def _chat(llm, db: Database, settings: Settings) -> int:
    """Read questions until 'exit' or end of input. Every question is a separate run."""
    print(CHAT_INTRO)
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in ("exit", "quit"):
            return 0
        if not question:
            continue
        try:
            _answer(question, llm, db, settings)
        except Exception as exc:  # one failed question should not end the session
            print(
                f"That question failed: {exc}. Its trace was still written to {settings.trace_dir}."
            )


def _render(trace_file: Path, output: Path | None) -> int:
    markdown = render_markdown(json.loads(trace_file.read_text(encoding="utf-8")))
    if output:
        output.write_text(markdown, encoding="utf-8")
        print(f"Wrote {output}")
    else:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
