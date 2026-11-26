"""Run a scoring pass: ask every model, then have every judge label every answer."""

import random
import time
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from src.llm.azure_openai_client import ModelCompletionError, create_completion, openai_client
from src.scoring.config import (
    ANSWER_MAX_TOKENS,
    JUDGE_MAX_TOKENS,
    JUDGE_REASONING_EFFORT,
    JUDGE_TEMPERATURE,
    MAX_RETRIES,
    ScoringConfig,
)
from src.scoring.judge import build_prompt, parse_judgement
from src.scoring.schema import Materials, Model
from src.scoring.store import LogStore

REFUSAL_ANSWER = "[The model refused to answer.]"


class BlockedError(RuntimeError):
    """A provider blocked the call. Sending it again would give the same result."""


class RefusedError(RuntimeError):
    """The model declined to respond."""


def azure_complete(model: str, prompt: str, max_tokens: int, temperature: float | None, schema: dict | None):
    """Call a model through the project's Azure client and return its CompletionResult.

    `temperature=None` means a candidate's answer: the provider's default
    settings. Otherwise it is a judge call, with the judge settings from the config.
    """
    try:
        return create_completion(
            openai_client(),
            prompt,
            max_tokens=max_tokens,
            temperature=JUDGE_TEMPERATURE if temperature is None else temperature,
            model=model,
            response_schema=schema,
            strict_json=schema is not None,
            reasoning_effort=JUDGE_REASONING_EFFORT,
            provider_defaults=temperature is None,
        )
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


def _text_and_settings(result) -> tuple[str, dict]:
    """A model call returns plain text (mock, tests) or a CompletionResult with its settings."""
    if isinstance(result, str):
        return result, {}
    return result.text, dict(getattr(result, "settings", None) or {})


def judge_orders_for(answer_key: tuple, judge_index: int, config: ScoringConfig) -> tuple[str, ...]:
    """The prompt orders one judge uses for one answer.

    "rotate": one order, shifted by the judge's position, so the judges of one
    answer each get a different order. The starting point depends on the answer,
    so across a run every judge uses every order about equally often.
    """
    orders = config.judge_orders
    if config.judge_order_mode == "all":
        return orders
    if config.judge_order_mode == "rotate":
        start = zlib.crc32("|".join(map(str, answer_key)).encode())
        return (orders[(start + judge_index) % len(orders)],)
    raise ValueError(f"Unknown JUDGE_ORDER_MODE {config.judge_order_mode!r}. Choose 'rotate' or 'all'.")


def _short_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:1000]


def ask_models(
    store: LogStore,
    run_id: int,
    models: list[Model],
    materials: Materials,
    config: ScoringConfig,
    complete: Callable = azure_complete,
) -> None:
    """Ask each model each question `n_answers` times, skipping calls already finished."""
    finished = store.finished_answers(run_id)
    tasks = [
        (model.name, question, attempt)
        for model in models
        for question in materials.questions
        for attempt in range(1, config.n_answers + 1)
        if (model.name, question.id, attempt) not in finished
    ]

    def ask(task) -> None:
        model_name, question, attempt = task
        save = lambda **fields: store.save_answer(
            run_id, model_name, question.id, attempt, category=question.category, **fields
        )
        try:
            # temperature None: the candidate answers with its provider's default settings.
            text, settings = with_retries(
                lambda: _require_text(complete(model_name, question.prompt, ANSWER_MAX_TOKENS, None, None)),
                config.retry_base_seconds,
            )
            save(status="ok", answer_text=text, call_settings=settings)
        except RefusedError:
            # A refusal is real behavior, so it is judged like any other answer.
            save(status="ok", answer_text=REFUSAL_ANSWER)
        except BlockedError as exc:
            save(status="blocked", error=_short_error(exc))
        except Exception as exc:
            save(status="failed", error=_short_error(exc))

    _run_all(ask, tasks, config.workers, "answers")


def judge_answers(
    store: LogStore,
    run_id: int,
    materials: Materials,
    config: ScoringConfig,
    complete: Callable = azure_complete,
    rejudge: bool = False,
) -> None:
    """Have each judge label each answer once per prompt order.

    Old judgements stay, and new rows are added. With `rejudge`, every call is
    made again, for use after the rubric or examples change.
    """
    questions = {question.id: question for question in materials.questions}
    finished = set() if rejudge else store.finished_judgements(run_id)
    tasks = [
        (answer, judge, order)
        for answer in store.answers_to_judge(run_id)
        # Answers to questions that are no longer in the materials are left alone.
        if answer.question_id in questions
        for judge_index, judge in enumerate(config.judge_models)
        if not (config.exclude_self_judging and judge == answer.model_name)
        for order in judge_orders_for(answer.key, judge_index, config)
        if (answer.key, judge, order) not in finished
    ]

    def judge_one(task) -> None:
        answer, judge, order = task
        question = questions[answer.question_id]
        prompt = build_prompt(
            question,
            answer.answer_text,
            materials.rubric[question.category],
            materials.examples,
            order,
            config.few_shots_per_level,
            config.lettered_order,
        )
        save = lambda **fields: store.add_judgement(
            run_id, answer, judge, order, materials.versions["rubric"], materials.versions["examples"], **fields
        )

        def call() -> tuple[str, str, dict]:
            text, settings = _text_and_settings(
                complete(judge, prompt.text, JUDGE_MAX_TOKENS, JUDGE_TEMPERATURE, prompt.schema)
            )
            return (*parse_judgement(text, prompt.labels), settings)

        try:
            level, reason, settings = with_retries(call, config.retry_base_seconds)
            save(status="ok", level=level, reason=reason, call_settings=settings)
        except (BlockedError, RefusedError) as exc:
            save(status="blocked", error=_short_error(exc))
        except Exception as exc:
            save(status="failed", error=_short_error(exc))

    _run_all(judge_one, tasks, config.workers, "judgements")


def _require_text(result) -> tuple[str, dict]:
    text, settings = _text_and_settings(result)
    if not text or not text.strip():
        raise ValueError("Model returned an empty answer")
    return text, settings


def _run_all(work: Callable, tasks: list, workers: int, label: str) -> None:
    total = len(tasks)
    print(f"{label}: {total} calls to make", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, _ in enumerate(pool.map(work, tasks), start=1):
            if done % 100 == 0 or done == total:
                print(f"{label}: {done}/{total}", flush=True)
