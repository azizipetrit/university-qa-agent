"""Command line: ask a question, or render a trace file as Markdown.

uv run sqlqa ask "How many rows are in the largest table?"
uv run sqlqa render-trace traces/<run>.json
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlqa", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    ask = commands.add_parser("ask", help="answer a question about the database")
    ask.add_argument("question")

    render = commands.add_parser("render-trace", help="print a trace file as Markdown")
    render.add_argument("trace_file", type=Path)
    render.add_argument("-o", "--output", type=Path, help="write to this file instead")

    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy codec
    return _ask(args.question) if args.command == "ask" else _render(args.trace_file, args.output)


def _ask(question: str) -> int:
    load_dotenv(find_dotenv(usecwd=True))  # .env in the directory the command is run from
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    db = Database(
        settings.database_url,
        max_rows=settings.max_rows,
        sample_rows=settings.sample_rows,
        notes_file=settings.schema_notes_file,
    )
    state, trace_path = run(question, create_llm(settings.llm_model), db, settings)

    print(state["answer"])
    print()
    for number, query in enumerate(state["queries"], start=1):
        print(f"Query {number}: {query['sql']}")
    print(f"Status: {state['status']}")
    print(f"Trace:  {trace_path}")
    return 0 if state["status"] != "failed" else 1


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
