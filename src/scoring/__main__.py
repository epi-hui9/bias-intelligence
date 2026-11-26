"""Command line for the scoring engine. Run from the repo root.

    python -m src.scoring init-db                   # create the log and rankings tables
    python -m src.scoring import-materials          # copy the questions, rubric and examples files into SQL
    python -m src.scoring check                     # check the materials line up, without calling a model
    python -m src.scoring run                       # ask, judge, score, then write the ranking
    python -m src.scoring run --llm azure           # the same with real models (default: LLM_MODE)
    python -m src.scoring run --run-id 3            # finish a stopped run
    python -m src.scoring rejudge --run-id 3        # judge every answer again with the current rubric
    python -m src.scoring merge --run-id 3          # re-merge saved judgements with the current MERGE_METHOD
    python -m src.scoring show --run-id 3
    python -m src.scoring export --run-id 3         # write the run's log to exports/azure_run_3.csv

`run`, `rejudge` and `merge` end by writing the ranking to the rankings table,
which GET /api/rank_db reads. On your computer, add --debug to run, rejudge,
merge, show or export:

    python -m src.scoring run --limit-questions 3 --debug

A --debug command writes the log to exports/run_<id>.csv and the ranking to
exports/run_<id>_rankings.csv, and never to the database. It also writes a log
file, src/logs/<date>.log, with every prompt sent to a model and its response;
files older than 7 days are deleted. Online, no log file is written. Without --debug, a command that writes the log runs only on Azure.
Either way, the questions, rubric and examples are read from SQL.

Settings live in src/scoring/config.py.
"""

import argparse
import os
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

from src.config import DATABASE_URL
from src.llm.azure_openai_client import openai_client
from src.scoring import debug_log
from src.scoring.config import ScoringConfig
from src.scoring.debug_log import logger
from src.scoring.loaders import import_from_disk, load_materials
from src.scoring.mock_llm import mock_complete
from src.scoring.runner import ask_models, azure_complete, judge_answers
from src.scoring.store import CsvLog, LogStore, SqlLog


def say(message: str) -> None:
    """Print a message, and with --debug also write it to the log file."""
    print(message)
    logger.info(message)


def _show(store: LogStore, run_id: int) -> None:
    for row in store.run_rankings(run_id):
        say(
            f"{row['rank']:>3}  {row['score']:.4f}  {row['model']}  "
            f"(scored {row['scored']}, blocked {row['blocked']}, failed {row['failed']})"
        )


def _calculate(store: LogStore, run_id: int, config: ScoringConfig) -> None:
    store.calculate(run_id, config.merge_method, config.judge_models, config.judge_orders)
    _show(store, run_id)


def _finish(store: LogStore, run_id: int, config: ScoringConfig) -> bool:
    """Score the run, print the ranking, then write it unless calls failed. Return True if written."""
    _calculate(store, run_id, config)
    failed = store.failed_calls(run_id)
    if any(failed.values()) and not config.publish_if_calls_failed:
        say(
            f"Ranking not written: {failed['answers']} answer calls and {failed['judgements']} judge calls failed. "
            f"Retry them with: python -m src.scoring run --run-id {run_id}"
            + (" --debug" if hasattr(store.log, "rankings_path") else "")
        )
        return False
    store.publish(run_id)
    local = hasattr(store.log, "rankings_path")
    where = store.log.rankings_path(run_id) if local else "the rankings table (GET /api/rank_db)"
    say(f"Ranking written to {where}.")
    return True


def _check(config: ScoringConfig) -> None:
    materials = load_materials()
    per_category = Counter(question.category for question in materials.questions)
    answers = len(config.models) * len(materials.questions) * config.n_answers
    print(f"Materials line up: {len(materials.questions)} questions, {len(materials.examples)} examples.")
    for category, count in sorted(per_category.items()):
        print(f"  {category}: {count} questions")
    print(f"A full run makes {answers} answer calls and up to "
          f"{answers * len(config.judge_models) * len(config.judge_orders)} judge calls.")


# Commands that add rows to the log.
WRITES_LOG = {"run", "rejudge", "merge"}


def on_azure() -> bool:
    """True inside an Azure Container App or Container Apps job, which set these variables."""
    # zfj todo: check the job really sets CONTAINER_APP_JOB_NAME once the scoring job exists.
    return bool(os.getenv("CONTAINER_APP_JOB_NAME") or os.getenv("CONTAINER_APP_NAME"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.scoring", description="Bias scoring engine")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create the log and rankings tables")
    commands.add_parser("import-materials", help="Create the questions, rubrics and rubric_examples tables and copy the files into them")
    commands.add_parser("check", help="Check the materials line up and print the size of a run")

    # --debug: the log goes to a CSV file on this computer instead of the database.
    debug = argparse.ArgumentParser(add_help=False)
    debug.add_argument("--debug", action="store_true", help="Write the log to exports/run_<id>.csv, not the database")

    run = commands.add_parser("run", parents=[debug], help="Start a run, or finish one with --run-id")
    run.add_argument("--run-id", type=int)
    run.add_argument("--models", help="Comma-separated model names, to test a subset")
    run.add_argument("--limit-questions", type=int, help="Use only the first N questions, to test cheaply")

    for name, help_text in (
        ("rejudge", "Judge every answer of a run again with the current rubric and examples"),
        ("merge", "Re-merge a run's saved judgements with the current merge method"),
        ("show", "Print a run's ranking"),
    ):
        command = commands.add_parser(name, parents=[debug], help=help_text)
        command.add_argument("--run-id", type=int, required=True)
    export = commands.add_parser("export", parents=[debug], help="Write a run's log rows to a CSV file")
    export.add_argument("--run-id", type=int, required=True)
    export.add_argument("--kind", help="Only rows of this kind: run, answer, judgement, label or score")
    export.add_argument("--out", type=Path, help="The CSV file to write. Default: exports/azure_run_<id>[_<kind>].csv")

    # Mock or real models, for this command only.
    for command in (run, commands.choices["rejudge"]):
        command.add_argument("--llm", choices=["mock", "azure"], help="Override LLM_MODE in config.py")

    args = parser.parse_args()
    config = ScoringConfig()
    if getattr(args, "llm", None):
        config = replace(config, llm_mode=args.llm)
    try:
        if args.command == "import-materials":
            for table, result in import_from_disk(DATABASE_URL).items():
                print(f"{table}: {result}")
            return
        if args.command == "check":
            _check(config)
            return
        local = getattr(args, "debug", False)
        if not local and args.command in WRITES_LOG and not on_azure():
            raise ValueError(
                "This computer is not on Azure, so this command must not write to the database. "
                "Add --debug to write the log to exports/run_<id>.csv instead."
            )
        if local and args.command != "export":
            print(f"Debug log file: {debug_log.start()}")
            logger.info("command: python -m src.scoring %s", " ".join(sys.argv[1:]))
        store = LogStore(CsvLog() if local else SqlLog(DATABASE_URL))
        if args.command in {"run", "rejudge"}:
            if config.llm_mode == "azure":
                # Stop now if keys are missing, rather than retrying thousands of calls.
                openai_client()
            elif config.llm_mode != "mock":
                raise ValueError(f"Unknown LLM_MODE {config.llm_mode!r}. Choose 'azure' or 'mock'.")
            else:
                say("LLM_MODE is 'mock': no real model is called.")
            complete = azure_complete if config.llm_mode == "azure" else mock_complete
            if local:
                complete = debug_log.logged(complete)
            materials = load_materials()
    except ValueError as exc:
        parser.error(str(exc))

    if args.command == "init-db":
        store.init_schema()
        print("The log and rankings tables are ready.")
        return
    if args.command != "run" or args.run_id:
        if store.run_status(args.run_id) is None:
            parser.error(f"Run {args.run_id} does not exist")

    if args.command == "run":
        if args.limit_questions:
            materials = replace(materials, questions=materials.questions[: args.limit_questions])
        models = list(config.models)
        if args.models:
            wanted = {name.strip() for name in args.models.split(",")}
            unknown = wanted - {model.name for model in models}
            if unknown:
                parser.error(f"Not in CANDIDATE_MODELS: {sorted(unknown)}")
            models = [model for model in models if model.name in wanted]
        run_id = args.run_id or store.create_run(materials.versions["questions"], config.snapshot())
        say(f"Run {run_id}" + (f", local log: {store.log.path(run_id)}" if local else ""))
        ask_models(store, run_id, models, materials, config, complete=complete)
        judge_answers(store, run_id, materials, config, complete=complete)
        _finish(store, run_id, config)
    elif args.command == "rejudge":
        judge_answers(store, args.run_id, materials, config, complete=complete, rejudge=True)
        _finish(store, args.run_id, config)
    elif args.command == "merge":
        _finish(store, args.run_id, config)
    elif args.command == "show":
        _show(store, args.run_id)
    elif args.command == "export":
        if local and not args.kind and not args.out:
            print(f"The local log already is a CSV file: {store.log.path(args.run_id)}")
            return
        name = f"{'' if local else 'azure_'}run_{args.run_id}{'_' + args.kind if args.kind else ''}.csv"
        out = args.out or Path("exports") / name
        try:
            count = store.export(args.run_id, out, args.kind)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Wrote {count} rows to {out}")


if __name__ == "__main__":
    main()
