"""Command line for the scoring engine. Run from the repo root.

    python -m src.scoring init-db
    python -m src.scoring check                     # check the materials line up, without calling a model
    python -m src.scoring run
    python -m src.scoring run --run-id 3            # finish a stopped run
    python -m src.scoring rejudge --run-id 3        # judge every answer again with the current rubric
    python -m src.scoring merge --run-id 3          # re-merge saved judgements with the current MERGE_METHOD
    python -m src.scoring show --run-id 3
    python -m src.scoring publish --run-id 3        # show this run on the website

Settings live in src/scoring/config.py.
"""

import argparse
from collections import Counter
from dataclasses import replace

from src.config import DATABASE_URL
from src.llm.azure_openai_client import openai_client
from src.scoring.config import ScoringConfig
from src.scoring.loaders import load_materials
from src.scoring.runner import ask_models, judge_answers
from src.scoring.store import ScoringStore


def _show(store: ScoringStore, run_id: int) -> None:
    for row in store.run_rankings(run_id):
        print(
            f"{row['rank']:>3}  {row['score']:.4f}  {row['model']}  "
            f"(scored {row['scored']}, blocked {row['blocked']}, failed {row['failed']})"
        )


def _calculate(store: ScoringStore, run_id: int, config: ScoringConfig) -> None:
    store.calculate(run_id, config.merge_method, config.judge_models, config.judge_orders)
    _show(store, run_id)


def _check(config: ScoringConfig) -> None:
    materials = load_materials()
    per_category = Counter(question.category for question in materials.questions)
    answers = len(config.models) * len(materials.questions) * config.n_answers
    print(f"Materials line up: {len(materials.questions)} questions, {len(materials.examples)} examples.")
    for category, count in sorted(per_category.items()):
        print(f"  {category}: {count} questions")
    print(f"A full run makes {answers} answer calls and up to "
          f"{answers * len(config.judge_models) * len(config.judge_orders)} judge calls.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.scoring", description="Bias scoring engine")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create the database tables")
    commands.add_parser("check", help="Check the materials line up and print the size of a run")

    run = commands.add_parser("run", help="Start a run, or finish one with --run-id")
    run.add_argument("--run-id", type=int)
    run.add_argument("--models", help="Comma-separated model names, to test a subset")
    run.add_argument("--limit-questions", type=int, help="Use only the first N questions, to test cheaply")

    for name, help_text in (
        ("rejudge", "Judge every answer of a run again with the current rubric and examples"),
        ("merge", "Re-merge a run's saved judgements with the current merge method"),
        ("show", "Print a run's ranking"),
        ("publish", "Show a run on the website"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--run-id", type=int, required=True)

    args = parser.parse_args()
    config = ScoringConfig()
    try:
        if args.command == "check":
            _check(config)
            return
        store = ScoringStore(DATABASE_URL)
        if args.command in {"run", "rejudge"}:
            # Stop now if keys are missing, rather than retrying thousands of calls.
            openai_client()
            materials = load_materials()
    except ValueError as exc:
        parser.error(str(exc))

    if args.command == "init-db":
        store.init_schema()
        print("Database tables are ready.")
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
        print(f"Run {run_id}")
        store.save_models(models)
        ask_models(store, run_id, models, materials, config)
        judge_answers(store, run_id, materials, config)
        _calculate(store, run_id, config)
        print(f"Review the ranking, then publish it with: python -m src.scoring publish --run-id {run_id}")
    elif args.command == "rejudge":
        judge_answers(store, args.run_id, materials, config, rejudge=True)
        _calculate(store, args.run_id, config)
    elif args.command == "merge":
        _calculate(store, args.run_id, config)
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
