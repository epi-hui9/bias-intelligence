"""Command line for the scoring engine. Run from the repo root.

    python -m src.scoring init-db
    python -m src.scoring run
    python -m src.scoring run --run-id 3            # finish a stopped run
    python -m src.scoring rescore --run-id 3        # add new scores with the current rubric
    python -m src.scoring show --run-id 3
    python -m src.scoring publish --run-id 3        # show this run on the website
"""

import argparse

from src.config import DATABASE_URL, SCORING_FALLBACK_EVALUATOR, SCORING_PRIMARY_EVALUATOR
from src.llm.azure_openai_client import openai_client
from src.scoring.inputs import QUESTIONS_PATH, RUBRIC_PATH, file_version, load_models, load_questions, load_rubric_text
from src.scoring.runner import ScoringConfig, ask_models, grade_answers
from src.scoring.store import ScoringStore


def _config(workers: int) -> ScoringConfig:
    return ScoringConfig(
        primary_evaluator=SCORING_PRIMARY_EVALUATOR,
        fallback_evaluator=SCORING_FALLBACK_EVALUATOR,
        rubric_text=load_rubric_text(),
        rubric_version=file_version(RUBRIC_PATH),
        workers=workers,
    )


def _show(store: ScoringStore, run_id: int) -> None:
    for row in store.run_rankings(run_id):
        print(
            f"{row['rank']:>3}  {row['score']:.4f}  {row['model']}  "
            f"(scored {row['scored']}, blocked {row['blocked']}, failed {row['failed']})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.scoring", description="Bias scoring engine")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create the database tables")

    run = commands.add_parser("run", help="Start a run, or finish one with --run-id")
    run.add_argument("--run-id", type=int)
    run.add_argument("--models", help="Comma-separated model names, to test a subset")
    run.add_argument("--limit-questions", type=int, help="Use only the first N questions, to test cheaply")
    run.add_argument("--workers", type=int, default=8)

    rescore = commands.add_parser("rescore", help="Add new scores for a run with the current rubric")
    rescore.add_argument("--run-id", type=int, required=True)
    rescore.add_argument("--workers", type=int, default=8)

    for name, help_text in (("show", "Print a run's ranking"), ("publish", "Show a run on the website")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run-id", type=int, required=True)

    args = parser.parse_args()
    try:
        store = ScoringStore(DATABASE_URL)
        if args.command in {"run", "rescore"}:
            # Stop now if keys are missing, rather than retrying thousands of calls.
            openai_client()
    except ValueError as exc:
        parser.error(str(exc))

    if args.command == "init-db":
        store.init_schema()
        print("Database tables are ready.")
    elif args.command == "run":
        questions = load_questions()
        if args.limit_questions:
            questions = questions[: args.limit_questions]
        models = load_models()
        if args.models:
            wanted = {name.strip() for name in args.models.split(",")}
            unknown = wanted - {model.name for model in models}
            if unknown:
                parser.error(f"Not in models.csv: {sorted(unknown)}")
            models = [model for model in models if model.name in wanted]
        if args.run_id:
            if store.run_status(args.run_id) is None:
                parser.error(f"Run {args.run_id} does not exist")
            run_id = args.run_id
        else:
            run_id = store.create_run(file_version(QUESTIONS_PATH))
        print(f"Run {run_id}")
        store.save_models(models)
        config = _config(args.workers)
        ask_models(store, run_id, models, questions, config)
        grade_answers(store, run_id, load_questions(), config)
        store.calculate(run_id)
        _show(store, run_id)
        print(f"Review the ranking, then publish it with: python -m src.scoring publish --run-id {run_id}")
    elif args.command == "rescore":
        if store.run_status(args.run_id) is None:
            parser.error(f"Run {args.run_id} does not exist")
        grade_answers(store, args.run_id, load_questions(), _config(args.workers), include_scored=True)
        store.calculate(args.run_id)
        _show(store, args.run_id)
    elif args.command == "show":
        _show(store, args.run_id)
    elif args.command == "publish":
        try:
            store.publish(args.run_id)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Run {args.run_id} is now the published ranking.")


if __name__ == "__main__":
    main()
