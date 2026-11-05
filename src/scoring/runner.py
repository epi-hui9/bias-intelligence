"""Run a scoring pass: ask every model, then grade every answer."""

import random
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from src.llm.azure_openai_client import ModelCompletionError, create_completion, openai_client
from src.scoring.evaluator import EVALUATION_SCHEMA, Evaluation, build_prompt, choose_evaluator, parse_evaluation
from src.scoring.inputs import Question, RankedModel
from src.scoring.store import ScoringStore

ATTEMPTS_PER_QUESTION = 3
MAX_RETRIES = 3
ANSWER_MAX_TOKENS = 2000
EVALUATION_MAX_TOKENS = 2000
ANSWER_TEMPERATURE = 0.7
EVALUATION_TEMPERATURE = 0.0
REFUSAL_ANSWER = "[The model refused to answer.]"


class BlockedError(RuntimeError):
    """A provider blocked the call. Sending it again would give the same result."""


class RefusedError(RuntimeError):
    """The model declined to respond."""


@dataclass(frozen=True)
class ScoringConfig:
    primary_evaluator: str
    fallback_evaluator: str
    rubric_text: str
    rubric_version: str
    workers: int = 8
    retry_base_seconds: float = 2.0


def azure_complete(model: str, prompt: str, max_tokens: int, temperature: float, schema: dict | None) -> str:
    """Call a model through the project's Azure client."""
    try:
        return create_completion(
            openai_client(),
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
            response_schema=schema,
            strict_json=schema is not None,
        ).text
    except ModelCompletionError as exc:
        if exc.code == "model_refusal":
            raise RefusedError(str(exc)) from exc
        if exc.code == "content_filter_blocked" or "content_filter" in str(exc).lower():
            raise BlockedError(str(exc)) from exc
        raise


def with_retries(call: Callable[[], object], base_seconds: float, max_retries: int = MAX_RETRIES):
    """Run a call, retrying temporary errors with a growing wait."""
    for retry in range(max_retries + 1):
        try:
            return call()
        except (BlockedError, RefusedError):
            raise
        except Exception:
            if retry == max_retries:
                raise
            time.sleep(base_seconds * 2**retry + random.uniform(0, base_seconds))
    raise AssertionError("unreachable")


def _short_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:1000]


def ask_models(
    store: ScoringStore,
    run_id: int,
    models: Iterable[RankedModel],
    questions: list[Question],
    config: ScoringConfig,
    complete: Callable = azure_complete,
) -> None:
    """Ask each model each question 3 times, skipping calls already finished."""
    finished = store.finished_answers(run_id)
    tasks = [
        (model.name, question, attempt)
        for model in models
        for question in questions
        for attempt in range(1, ATTEMPTS_PER_QUESTION + 1)
        if (model.name, question.id, attempt) not in finished
    ]

    def ask(task) -> None:
        model_name, question, attempt = task
        save = lambda **fields: store.save_answer(run_id, model_name, question.id, attempt, **fields)
        try:
            text = with_retries(
                lambda: _require_text(complete(model_name, question.prompt, ANSWER_MAX_TOKENS, ANSWER_TEMPERATURE, None)),
                config.retry_base_seconds,
            )
            save(status="ok", answer_text=text)
        except RefusedError:
            # A refusal is real behavior, so it is scored like any other answer.
            save(status="ok", answer_text=REFUSAL_ANSWER)
        except BlockedError as exc:
            save(status="blocked", error=_short_error(exc))
        except Exception as exc:
            save(status="failed", error=_short_error(exc))

    _run_all(ask, tasks, config.workers, "answers")


def grade_answers(
    store: ScoringStore,
    run_id: int,
    questions: list[Question],
    config: ScoringConfig,
    complete: Callable = azure_complete,
    include_scored: bool = False,
) -> None:
    """Score each saved answer. Old scores stay, and new rows are added."""
    prompts = {question.id: question.prompt for question in questions}
    answers = store.answers_to_grade(run_id, include_scored=include_scored)

    def grade(answer) -> None:
        evaluator = choose_evaluator(answer.model_name, config.primary_evaluator, config.fallback_evaluator)
        prompt = build_prompt(config.rubric_text, prompts[answer.question_id], answer.answer_text)
        save = lambda **fields: store.add_score(answer.answer_id, evaluator, config.rubric_version, **fields)

        def call() -> Evaluation:
            text = complete(evaluator, prompt, EVALUATION_MAX_TOKENS, EVALUATION_TEMPERATURE, EVALUATION_SCHEMA)
            return parse_evaluation(text)

        try:
            result = with_retries(call, config.retry_base_seconds)
            save(status="ok", score=result.score, level=result.level, reasoning=result.reasoning)
        except (BlockedError, RefusedError) as exc:
            save(status="blocked", error=_short_error(exc))
        except Exception as exc:
            save(status="failed", error=_short_error(exc))

    _run_all(grade, answers, config.workers, "evaluations")


def _require_text(text: str) -> str:
    if not text or not text.strip():
        raise ValueError("Model returned an empty answer")
    return text


def _run_all(work: Callable, tasks: list, workers: int, label: str) -> None:
    total = len(tasks)
    print(f"{label}: {total} calls to make", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, _ in enumerate(pool.map(work, tasks), start=1):
            if done % 100 == 0 or done == total:
                print(f"{label}: {done}/{total}", flush=True)
