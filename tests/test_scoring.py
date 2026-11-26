import csv
import json
import logging
import os
import re
import tempfile
from datetime import date, timedelta
from pathlib import Path
import unittest
from dataclasses import replace
from collections import Counter, defaultdict
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

from src.api import fastapi_app
from src.api.fastapi_app import app
from src.llm.azure_openai_client import CompletionResult, ModelCompletionError, create_completion
from src.scoring import mock_llm, runner
from src.scoring.config import (
    ANSWER_MAX_TOKENS, CANDIDATE_MODELS, JUDGE_MAX_TOKENS, JUDGE_MODELS, JUDGE_REASONING_EFFORT, JUDGE_TEMPERATURE,
    LETTERED_ORDER, ScoringConfig,
)
from src.scoring.judge import build_prompt, parse_judgement
from src.scoring.loaders import import_from_disk, load_from_disk, load_from_sql, load_materials, prepare
from src.scoring.merge import AnswerOutcome, merge, rank_models
from src.scoring.runner import BlockedError, RefusedError, ask_models, judge_answers, with_retries
from src.scoring.schema import LEVELS, Example, Judgement, Materials, Model, Question, RubricRow
from src.scoring import __main__ as cli
from src.scoring import debug_log
from src.scoring.store import CSV_COLUMNS, KINDS, CsvLog, LogStore, SqlLog

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


def small_materials(question_categories=("Cat", "Cat"), rubric_levels=LEVELS, examples=None) -> Materials:
    questions = [
        Question(f"Q{number}", category, "neutral", f"Question {number}?")
        for number, category in enumerate(question_categories, start=1)
    ]
    rubric = {"Cat": RubricRow("Cat", {level: f"DESC {level}" for level in rubric_levels})}
    if examples is None:
        examples = [Example("Cat", level, f"EXAMPLE {level} {n}") for level in LEVELS for n in (1, 2, 3)]
    return Materials(questions, rubric, examples, {"questions": "q#1", "rubric": "r#1", "examples": "e#1"})


class InputTests(unittest.TestCase):
    def test_saved_materials_line_up_and_use_the_rubric_spelling(self):
        materials = load_materials("disk")
        categories = {question.category for question in materials.questions}
        self.assertEqual(len(materials.questions), 61)
        self.assertEqual(len({question.id for question in materials.questions}), 61)
        self.assertIn("Science, Technology, & Medicine", categories)
        self.assertIn("Racial Equity", categories)
        self.assertIn("Education", categories)
        self.assertIn("Disability", categories)
        self.assertLessEqual({example.category for example in materials.examples}, categories)
        self.assertNotIn("June’s Original Rubric", {example.text for example in materials.examples})

    def test_aliases_rename_categories_in_questions_and_examples(self):
        raw = small_materials(("cat", "Cat"), examples=[Example("CAT", "No Bias", "x")])
        materials = prepare(raw, aliases={"cat": "Cat", "CAT": "Cat"}, skip_categories=[])
        self.assertEqual({question.category for question in materials.questions}, {"Cat"})
        self.assertEqual(materials.examples[0].category, "Cat")

    def test_mismatched_materials_stop_the_run(self):
        cases = {
            "has no rubric row": small_materials(("Cat", "Other")),
            "has no text for: Medium Bias": small_materials(rubric_levels=[l for l in LEVELS if l != "Medium Bias"]),
            "example category 'Other' has no rubric row": small_materials(examples=[Example("Other", "No Bias", "x")]),
            "unknown bias level": small_materials(examples=[Example("Cat", "Some Bias", "x")]),
        }
        for message, raw in cases.items():
            with self.assertRaisesRegex(ValueError, message):
                prepare(raw, aliases={}, skip_categories=[])

    def test_skipped_categories_are_dropped_instead_of_failing(self):
        raw = small_materials(("Cat", "Other"), examples=[Example("Other", "No Bias", "x")])
        materials = prepare(raw, aliases={}, skip_categories=["Other"])
        self.assertEqual([question.id for question in materials.questions], ["Q1"])
        self.assertEqual(materials.examples, [])

    def test_config_has_unique_models_and_3_judges(self):
        # zfj todo: back to assertEqual(..., 17) when the 16 commented models return.
        self.assertGreaterEqual(len(CANDIDATE_MODELS), 1)
        self.assertEqual(len({model.name for model in CANDIDATE_MODELS}), len(CANDIDATE_MODELS))
        self.assertEqual(len(set(JUDGE_MODELS)), 3)
        self.assertEqual(sorted(LETTERED_ORDER), sorted(LEVELS))


class JudgeTests(unittest.TestCase):
    def prompt(self, order, answer="An answer", few_shots=2):
        materials = small_materials()
        return build_prompt(
            materials.questions[0], answer, materials.rubric["Cat"], materials.examples, order, few_shots, LETTERED_ORDER
        )

    def test_the_three_orders_lay_the_levels_out_differently(self):
        for order, expected in (("ascending", LEVELS), ("descending", LEVELS[::-1]), ("lettered", LETTERED_ORDER)):
            prompt = self.prompt(order)
            positions = [prompt.text.index(f"DESC {level}") for level in expected]
            self.assertEqual(positions, sorted(positions), order)
            self.assertEqual(list(prompt.labels.values()), list(expected))

    def test_lettered_order_hides_level_names_behind_letters(self):
        prompt = self.prompt("lettered")
        self.assertEqual(list(prompt.labels), ["A", "B", "C", "D", "E"])
        self.assertNotIn("[No Bias]", prompt.text)
        self.assertEqual(prompt.schema["properties"]["label"]["enum"], ["A", "B", "C", "D", "E"])

    def test_prompt_uses_only_the_question_category_and_limits_examples(self):
        materials = small_materials()
        examples = materials.examples + [Example("Other", "No Bias", "OTHER CATEGORY EXAMPLE")]
        prompt = build_prompt(materials.questions[0], "A {braces} answer", materials.rubric["Cat"], examples, "ascending", 2)
        self.assertIn('category "Cat"', prompt.text)
        self.assertIn("EXAMPLE High Bias 2", prompt.text)
        self.assertNotIn("EXAMPLE High Bias 3", prompt.text)
        self.assertNotIn("OTHER CATEGORY EXAMPLE", prompt.text)
        self.assertIn("QUESTION: Question 1?", prompt.text)
        self.assertIn("ANSWER: A {braces} answer", prompt.text)

    def test_unknown_order_is_rejected(self):
        with self.assertRaises(ValueError):
            self.prompt("sideways")

    def test_parse_maps_the_label_back_to_a_level(self):
        lettered = self.prompt("lettered").labels
        self.assertEqual(parse_judgement('```json\n{"label": "C", "reason": "Why."}\n```', lettered), (LETTERED_ORDER[2], "Why."))
        named = self.prompt("ascending").labels
        self.assertEqual(parse_judgement('{"label": "low bias", "reason": "Minor."}', named), ("Low Bias", "Minor."))

    def test_parse_rejects_bad_output(self):
        labels = self.prompt("lettered").labels
        for output in ("no json", "{not json}", '{"label": "F", "reason": "x"}', '{"label": "No Bias", "reason": "x"}', '{"label": "A", "reason": ""}'):
            with self.assertRaises(ValueError, msg=output):
                parse_judgement(output, labels)


def votes(**by_judge) -> list[Judgement]:
    orders = ("ascending", "descending", "lettered")
    return [Judgement(judge, order, level) for judge, levels in by_judge.items() for order, level in zip(orders, levels)]


class MergeTests(unittest.TestCase):
    def test_median_of_medians_ignores_one_odd_order_and_one_odd_judge(self):
        judgements = votes(
            a=("Low Bias", "Low Bias", "Very High Bias"),
            b=("Low Bias", "Medium Bias", "Low Bias"),
            c=("Very High Bias", "Very High Bias", "Very High Bias"),
        )
        self.assertEqual(merge(judgements, "median_of_medians"), "Low Bias")

    def test_two_judges_that_disagree_give_the_more_biased_label(self):
        judgements = votes(a=("Low Bias",) * 3, b=("High Bias",) * 3)
        self.assertEqual(merge(judgements, "median_of_medians"), "High Bias")

    def test_majority_vote_is_a_different_method(self):
        judgements = votes(
            a=("No Bias", "No Bias", "High Bias"),
            b=("No Bias", "High Bias", "High Bias"),
            c=("High Bias", "High Bias", "No Bias"),
        )
        self.assertEqual(merge(judgements, "majority_vote"), "High Bias")
        self.assertEqual(merge(votes(a=("No Bias", "No Bias", "High Bias")), "majority_vote"), "No Bias")

    def test_no_judgements_and_unknown_methods(self):
        self.assertIsNone(merge([], "median_of_medians"))
        with self.assertRaises(ValueError):
            merge(votes(a=("No Bias",) * 3), "coin_flip")

    def test_ranking_averages_questions_then_the_model_and_shares_tied_ranks(self):
        outcomes = [
            # Q1 averages 0.25 and Q2 is 0.75, so model-a is 0.5.
            AnswerOutcome("model-a", "Q1", "No Bias"),
            AnswerOutcome("model-a", "Q1", "Low Bias"),
            AnswerOutcome("model-a", "Q1", "Medium Bias"),
            AnswerOutcome("model-a", "Q2", "High Bias"),
            AnswerOutcome("model-a", "Q2", None, blocked=True),
            AnswerOutcome("model-a", "Q2", None),
            AnswerOutcome("model-b", "Q1", "Medium Bias"),
            AnswerOutcome("model-c", "Q1", "Very High Bias"),
            AnswerOutcome("model-d", "Q1", None),
        ]
        rankings = rank_models(outcomes)
        self.assertEqual([(row["model"], row["rank"], row["score"]) for row in rankings],
                         [("model-c", 1, 1.0), ("model-a", 2, 0.5), ("model-b", 2, 0.5)])
        self.assertEqual({key: rankings[1][key] for key in ("scored", "blocked", "failed")},
                         {"scored": 4, "blocked": 1, "failed": 1})


class RetryTests(unittest.TestCase):
    def test_temporary_errors_retry_three_times_then_raise(self):
        calls = []

        def fail():
            calls.append(1)
            raise RuntimeError("timeout")

        with self.assertRaises(RuntimeError):
            with_retries(fail, base_seconds=0)
        self.assertEqual(len(calls), 4)

    def test_recovers_when_a_retry_works(self):
        results = iter([RuntimeError("timeout"), "ok"])

        def call():
            result = next(results)
            if isinstance(result, Exception):
                raise result
            return result

        self.assertEqual(with_retries(call, base_seconds=0), "ok")

    def test_blocked_and_refused_calls_are_not_retried(self):
        for error in (BlockedError, RefusedError):
            calls = []

            def fail():
                calls.append(1)
                raise error("no")

            with self.assertRaises(error):
                with_retries(fail, base_seconds=0)
            self.assertEqual(len(calls), 1)

    def test_azure_errors_map_to_blocked_and_refused(self):
        cases = [
            (ModelCompletionError("x", code="content_filter_blocked"), BlockedError),
            (ModelCompletionError("provider said content_filter", code="completion_failed"), BlockedError),
            (ModelCompletionError("x", code="model_refusal"), RefusedError),
            (ModelCompletionError("x", code="completion_failed"), ModelCompletionError),
        ]
        for raised, expected in cases:
            with patch.object(runner, "openai_client"), patch.object(runner, "create_completion", side_effect=raised):
                with self.assertRaises(expected):
                    runner.azure_complete("gpt-5.5", "prompt", 10, 0.0, None)


class CallSettingsTests(unittest.TestCase):
    def test_azure_complete_uses_defaults_for_answers_and_config_for_judges(self):
        with patch.object(runner, "openai_client"), patch.object(runner, "create_completion") as create:
            runner.azure_complete("cand", "prompt", 8000, None, None)
            runner.azure_complete("judge", "prompt", 2000, 0.0, {"type": "object"})
        answer, judge = (c.kwargs for c in create.call_args_list)
        self.assertTrue(answer["provider_defaults"])
        self.assertFalse(judge["provider_defaults"])
        self.assertEqual((judge["temperature"], judge["reasoning_effort"]), (0.0, JUDGE_REASONING_EFFORT))

    def openai_payloads(self, **kwargs):
        sent = []
        response = SimpleNamespace(output_text="hi", temperature=1.0, top_p=1.0, reasoning=SimpleNamespace(effort="medium"))
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **payload: sent.append(payload) or response))
        result = create_completion(client, "prompt", model="gpt-x", **kwargs)
        return sent, result

    def test_provider_defaults_send_no_temperature_or_effort(self):
        sent, result = self.openai_payloads(max_tokens=8000, provider_defaults=True)
        self.assertEqual(sent, [{"model": "gpt-x", "input": [{"role": "user", "content": "prompt"}], "max_output_tokens": 8000}])
        # The API reports the defaults it used, and they are recorded.
        self.assertEqual(result.settings, {"api": "responses", "max_output_tokens": 8000,
                                           "reported": {"temperature": 1.0, "top_p": 1.0, "reasoning_effort": "medium"}})

    def test_judge_settings_are_sent(self):
        sent, result = self.openai_payloads(max_tokens=2000, temperature=0.0, reasoning_effort="high")
        self.assertEqual((sent[0]["temperature"], sent[0]["reasoning"]["effort"]), (0.0, "high"))
        self.assertEqual(result.settings["temperature"], 0.0)

    def test_claude_with_provider_defaults_has_no_thinking(self):
        sent = []
        message = SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")], stop_reason="end_turn")
        fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **payload: sent.append(payload) or message))
        with patch("src.llm.azure_openai_client.anthropic_client", return_value=fake):
            result = create_completion(None, "prompt", model="claude-x", max_tokens=8000, provider_defaults=True)
        self.assertEqual(sent, [{"model": "claude-x", "messages": [{"role": "user", "content": "prompt"}], "max_tokens": 8000}])
        self.assertEqual(result.settings, {"api": "anthropic", "max_tokens": 8000})


class RankingsRouteWithoutDatabaseTests(unittest.TestCase):
    def test_missing_database_setting_returns_503(self):
        with patch.object(fastapi_app, "DATABASE_URL", None):
            response = TestClient(app).get("/api/rankings")
        self.assertEqual(response.status_code, 503)


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to run PostgreSQL tests")
class MaterialsInSqlTests(unittest.TestCase):
    def setUp(self):
        self.sql("DROP TABLE IF EXISTS questions, rubrics, rubric_examples")

    def sql(self, statement):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            cursor = connection.execute(statement)
            return cursor.fetchall() if cursor.description else None

    def test_sql_gives_the_engine_exactly_what_the_files_give(self):
        import_from_disk(TEST_DATABASE_URL)
        disk, sql = load_from_disk(), load_from_sql(TEST_DATABASE_URL)
        self.assertEqual(sql.questions, disk.questions)  # same order too
        self.assertEqual(sql.rubric, disk.rubric)
        self.assertEqual([list(row.levels) for row in sql.rubric.values()], [list(LEVELS)] * 11)
        self.assertEqual(prepare(sql).examples, prepare(disk).examples)  # same order: judges take the first ones
        self.assertEqual(len(prepare(sql).questions), 61)
        self.assertEqual(len(sql.examples), 241)

    def test_only_the_configured_question_set_and_rubric_are_saved(self):
        import_from_disk(TEST_DATABASE_URL)
        self.assertEqual(
            self.sql("SELECT version, COUNT(*) FROM questions GROUP BY 1"), [("question_set_human_v1", 61)]
        )
        self.assertEqual(self.sql("SELECT version, COUNT(*) FROM rubrics GROUP BY 1"), [("rubric_v3", 55)])

    def test_every_example_is_saved_with_its_status_and_the_rubric_spelling(self):
        import_from_disk(TEST_DATABASE_URL)
        self.assertEqual(
            self.sql("SELECT status, COUNT(*) FROM rubric_examples GROUP BY 1 ORDER BY 1"),
            [("", 1), ("Confirmed", 241), ("Pending Review", 7)],
        )
        self.assertEqual(self.sql("SELECT COUNT(*) FROM rubric_examples WHERE category = 'Science Technology Medicine'"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM rubric_examples r LEFT JOIN rubrics b "
                                  "ON b.category = r.category AND b.level = r.level WHERE b.category IS NULL"), [(0,)])

    def test_confirming_an_example_in_sql_adds_it_to_the_next_run(self):
        import_from_disk(TEST_DATABASE_URL)
        before = load_from_sql(TEST_DATABASE_URL)
        self.sql("UPDATE rubric_examples SET status = 'Confirmed' WHERE status = 'Pending Review' AND position = 5")
        after = load_from_sql(TEST_DATABASE_URL)
        self.assertEqual(len(after.examples), len(before.examples) + 1)
        self.assertNotEqual(after.versions["examples"], before.versions["examples"])

    def test_importing_again_changes_nothing(self):
        import_from_disk(TEST_DATABASE_URL)
        before = load_from_sql(TEST_DATABASE_URL).versions
        self.assertIn("already saved", import_from_disk(TEST_DATABASE_URL)["questions"])
        self.assertEqual(load_from_sql(TEST_DATABASE_URL).versions, before)

    def test_changed_rows_are_refused_and_change_the_recorded_version(self):
        import_from_disk(TEST_DATABASE_URL)
        before = load_from_sql(TEST_DATABASE_URL).versions
        self.sql("UPDATE rubrics SET description = 'edited' WHERE category = 'Gender' AND level = 'No Bias'")
        after = load_from_sql(TEST_DATABASE_URL).versions
        self.assertEqual(after["questions"], before["questions"])
        self.assertNotEqual(after["rubric"], before["rubric"])
        with self.assertRaisesRegex(ValueError, "already has a different 'rubric_v3'"):
            import_from_disk(TEST_DATABASE_URL)

    def test_missing_tables_or_versions_say_what_to_run(self):
        with self.assertRaisesRegex(ValueError, "import-materials"):
            load_from_sql(TEST_DATABASE_URL)
        import_from_disk(TEST_DATABASE_URL)
        self.sql("DELETE FROM rubrics")
        with self.assertRaisesRegex(ValueError, "rubrics has no 'rubric_v3'"):
            load_from_sql(TEST_DATABASE_URL)


class EngineTests:
    """Run on both logs: SqlEngineTests uses PostgreSQL, CsvEngineTests a folder of CSV files."""

    def setUp(self):
        self.store = self.make_store()
        self.models = [Model("gpt-5.5", "OpenAI"), Model("model-b", "B"), Model("model-c", "C")]
        self.materials = small_materials()
        # "all" orders: 9 judge calls per answer, so every order is tested.
        self.config = ScoringConfig(
            judge_models=("gpt-5.5", "judge-b", "judge-c"), models=tuple(self.models), workers=4, retry_base_seconds=0,
            n_answers=3, judge_order_mode="all",
        )
        self.levels = {"gpt-5.5": "Medium Bias", "model-b": "High Bias", "model-c": "Low Bias"}
        self.judge_calls = []
        self.answer_calls = 0
        self.fail_answers_for = set()
        self.block_answers_for = set()
        self.refuse_answers_for = set()
        self.fail_judging_for = set()
        self.judge_overrides = {}

    def complete(self, model, prompt, max_tokens, temperature, schema):
        if schema is None:
            self.answer_calls += 1
            if model in self.fail_answers_for:
                raise RuntimeError("timeout")
            if model in self.block_answers_for and prompt == "Question 2?":
                raise BlockedError("content_filter")
            if model in self.refuse_answers_for:
                raise RefusedError("declined")
            return f"Answer from {model}"
        answerer = next((name for name in self.levels if f"ANSWER: Answer from {name}" in prompt), "refused")
        self.judge_calls.append((model, answerer))
        if answerer in self.fail_judging_for:
            return "not json"
        level = self.judge_overrides.get(model) or self.levels.get(answerer, "No Bias")
        # Answer with whatever label this prompt layout gives the level.
        label = re.search(rf"\[([^\]]+)\]\nDescription: DESC {level}\n", prompt).group(1)
        return f'{{"label": "{label}", "reason": "Because."}}'

    def calculate(self, run_id, config=None):
        config = config or self.config
        self.store.calculate(run_id, config.merge_method, config.judge_models, config.judge_orders)

    def run_engine(self, models=None, config=None):
        config = config or self.config
        run_id = self.store.create_run("questions#1", config.snapshot())
        ask_models(self.store, run_id, models or self.models, self.materials, config, complete=self.complete)
        judge_answers(self.store, run_id, self.materials, config, complete=self.complete)
        self.calculate(run_id, config)
        return run_id

    def published(self):
        return self.store.published_ranking()

    def ranking(self):
        return [(m["modelName"], m["rank"], m["overallScore"]) for m in self.published()["models"]]

    def rows(self, kind):
        return self.store.log.read(kinds=(kind,))

    def count(self, kind, **equal):
        return sum(all(row[k] == v for k, v in equal.items()) for row in self.rows(kind))

    def test_full_run_ranks_most_biased_first_and_only_after_publishing(self):
        run_id = self.run_engine()

        self.assertEqual(self.answer_calls, 18)
        self.assertEqual(self.count("answer"), 18)
        # 18 answers x 3 judges x 3 orders, minus gpt-5.5 judging its own 6 answers 3 times.
        self.assertEqual(self.count("judgement", status="ok"), 144)
        self.assertEqual(self.count("judgement", text="Because."), 144)
        self.assertEqual(self.count("label"), 18)
        self.assertEqual(self.count("score"), 3)
        self.assertEqual(sum(r["details"]["merge_method"] == "median_of_medians" for r in self.rows("run")), 1)
        self.assertEqual(self.store.run_status(run_id), "scored")
        self.assertIsNone(self.published())

        self.store.publish(run_id)
        self.assertEqual(self.store.run_status(run_id), "published")
        self.assertEqual(self.ranking(), [("model-b", 1, 0.75), ("gpt-5.5", 2, 0.5), ("model-c", 3, 0.25)])

    def test_rankings_return_one_result_per_model_and_no_answers(self):
        self.store.publish(self.run_engine())
        body = self.published()

        self.assertEqual(set(body), {"run", "models"})
        run = body["run"]
        self.assertEqual((run["modelCount"], run["questionCount"], run["runsPerQuestion"]), (3, 2, 3))
        self.assertEqual(run["rubricVersion"], "r#1")
        self.assertEqual(
            body["models"][0],
            {
                "rank": 1, "modelId": "model-b", "modelName": "model-b", "provider": "B", "effort": None,
                "overallScore": 0.75, "level": 4, "label": "High Bias", "categoryScores": {"cat": 0.75},
            },
        )
        self.assertEqual(body["models"][1]["modelId"], "gpt-5-5")

    def test_a_mock_run_is_repeatable_and_marked_as_mock(self):
        def mock_run():
            mock_llm._answer_calls.clear()
            config = replace(self.config, llm_mode="mock")
            run_id = self.store.create_run("questions#1", config.snapshot())
            ask_models(self.store, run_id, self.models, self.materials, config, complete=mock_llm.mock_complete)
            judge_answers(self.store, run_id, self.materials, config, complete=mock_llm.mock_complete)
            self.calculate(run_id, config)
            self.store.publish(run_id)
            return self.published()

        first, second = mock_run(), mock_run()
        self.assertEqual(first["run"]["llmMode"], "mock")
        self.assertEqual(first["models"], second["models"])
        self.assertTrue(all(row["text"] == mock_llm.MOCK_REASON for row in self.rows("judgement")))
        self.assertGreater(len({row["level"] for row in self.rows("judgement")}), 1)

    def test_every_prompt_order_is_used_and_maps_back_to_the_same_level(self):
        self.run_engine([self.models[1]])
        saved = Counter((row["prompt_order"], row["level"]) for row in self.rows("judgement"))
        self.assertEqual(sorted((order, level, n) for (order, level), n in saved.items()), [("ascending", "High Bias", 18), ("descending", "High Bias", 18), ("lettered", "High Bias", 18)])

    def test_no_judge_judges_its_own_answers_unless_allowed(self):
        self.run_engine()
        self.assertNotIn(("gpt-5.5", "gpt-5.5"), self.judge_calls)
        self.assertIn(("gpt-5.5", "model-b"), self.judge_calls)

        self.judge_calls = []
        self.run_engine(config=replace(self.config, exclude_self_judging=False))
        self.assertIn(("gpt-5.5", "gpt-5.5"), self.judge_calls)

    def test_the_number_of_answers_per_question_is_a_setting(self):
        self.run_engine(config=replace(self.config, n_answers=5))
        self.assertEqual(self.count("answer"), 30)

    def test_one_odd_judge_is_outvoted_and_a_removed_judge_stops_counting(self):
        self.judge_overrides = {"judge-c": "Very High Bias"}
        run_id = self.run_engine([self.models[2]])
        self.assertAlmostEqual(self.store.run_rankings(run_id)[0]["score"], 0.25)

        # Re-merging saved judgements makes no model calls.
        calls = len(self.judge_calls)
        self.calculate(run_id, replace(self.config, judge_models=("judge-c",)))
        self.assertAlmostEqual(self.store.run_rankings(run_id)[0]["score"], 1.0)
        self.assertEqual(len(self.judge_calls), calls)
        self.assertEqual(self.count("label"), 12)

    def test_refusals_are_judged_and_filter_blocks_are_skipped(self):
        self.refuse_answers_for = {"model-b"}
        self.block_answers_for = {"model-c"}
        run_id = self.run_engine()

        results = {row["model"]: row for row in self.store.run_rankings(run_id)}
        self.assertEqual(results["model-b"]["scored"], 6)
        self.assertAlmostEqual(results["model-b"]["score"], 0.0)
        self.assertEqual((results["model-c"]["scored"], results["model-c"]["blocked"]), (3, 3))
        self.assertAlmostEqual(results["model-c"]["score"], 0.25)

    def test_failed_calls_are_recorded_and_a_restart_redoes_only_them(self):
        self.fail_answers_for = {"model-b"}
        self.fail_judging_for = {"model-c"}
        run_id = self.run_engine()

        results = {row["model"]: row for row in self.store.run_rankings(run_id)}
        self.assertEqual(set(results), {"gpt-5.5"})
        self.assertEqual(self.count("answer", status="failed"), 6)
        self.assertEqual(self.count("judgement", status="failed"), 54)

        self.fail_answers_for = set()
        self.fail_judging_for = set()
        self.answer_calls = 0
        self.judge_calls = []
        ask_models(self.store, run_id, self.models, self.materials, self.config, complete=self.complete)
        judge_answers(self.store, run_id, self.materials, self.config, complete=self.complete)
        self.calculate(run_id)

        self.assertEqual(self.answer_calls, 6)
        # model-b's 6 new answers and model-c's 6 failed ones, each x 3 judges x 3 orders.
        self.assertEqual(len(self.judge_calls), 108)
        # The failed rows stay in the log, and the newer successful rows count.
        self.assertEqual(self.count("answer", status="failed"), 6)
        self.assertEqual(self.count("answer", status="ok"), 18)
        self.assertEqual([row["model"] for row in self.store.run_rankings(run_id)], ["model-b", "gpt-5.5", "model-c"])

    def test_rejudging_keeps_old_rows_and_uses_the_newest(self):
        run_id = self.run_engine()
        self.store.publish(run_id)
        self.assertEqual(self.count("judgement"), 144)
        published = self.ranking()

        self.levels = {"gpt-5.5": "Very High Bias", "model-b": "No Bias", "model-c": "Low Bias"}
        judge_answers(self.store, run_id, self.materials, self.config, complete=self.complete, rejudge=True)
        self.calculate(run_id)

        self.assertEqual(self.count("judgement"), 288)
        # The website keeps the published copy until the run is published again.
        self.assertEqual(self.ranking(), published)
        self.store.publish(run_id)
        self.assertEqual(
            [(name, score) for name, _, score in self.ranking()],
            [("gpt-5.5", 1.0), ("model-c", 0.25), ("model-b", 0.0)],
        )

    def test_tied_scores_share_a_rank(self):
        self.levels = {"gpt-5.5": "Medium Bias", "model-b": "Medium Bias", "model-c": "Low Bias"}
        self.store.publish(self.run_engine())
        self.assertEqual([rank for _, rank, _ in self.ranking()], [1, 1, 3])

    def test_cannot_publish_an_unscored_run(self):
        run_id = self.store.create_run("questions#1")
        with self.assertRaises(ValueError):
            self.store.publish(run_id)
        self.assertEqual(self.store.run_status(run_id), "running")
        self.assertIsNone(self.store.run_status(run_id + 1))

    def test_rotation_gives_each_judge_one_call_and_each_answer_all_orders(self):
        run_id = self.run_engine(config=replace(self.config, judge_order_mode="rotate"))
        judgements = self.rows("judgement")
        # 18 answers x 3 judges, minus gpt-5.5 skipping its own 6 answers.
        self.assertEqual(len(judgements), 18 * 3 - 6)
        by_answer = defaultdict(list)
        for row in judgements:
            by_answer[(row["model"], row["question_id"], row["attempt"])].append(row["prompt_order"])
        for key, orders in by_answer.items():
            self.assertEqual(len(orders), len(set(orders)), key)  # one order per judge, all different
            self.assertEqual(len(orders), 2 if key[0] == "gpt-5.5" else 3)
        for judge in ("gpt-5.5", "judge-b", "judge-c"):
            self.assertEqual({r["prompt_order"] for r in judgements if r["judge"] == judge},
                             {"ascending", "descending", "lettered"})
        self.assertEqual([r["model"] for r in self.store.run_rankings(run_id)], ["model-b", "gpt-5.5", "model-c"])

    def test_candidates_get_provider_defaults_and_judges_the_config(self):
        asked = []
        def complete(model, prompt, max_tokens, temperature, schema):
            asked.append(("judge" if schema else "answer", temperature, max_tokens))
            if schema is None:
                return CompletionResult(text="An answer", settings={"api": "responses", "max_output_tokens": max_tokens})
            label = schema["properties"]["label"]["enum"][0]
            return CompletionResult(text=json.dumps({"label": label, "reason": "r"}),
                                    settings={"api": "responses", "temperature": 0.0})
        config = replace(self.config, models=(Model("model-b", "B"),), n_answers=1, judge_order_mode="rotate")
        run_id = self.store.create_run("q#1", config.snapshot())
        ask_models(self.store, run_id, list(config.models), self.materials, config, complete=complete)
        judge_answers(self.store, run_id, self.materials, config, complete=complete)
        self.assertEqual({a for a in asked if a[0] == "answer"}, {("answer", None, ANSWER_MAX_TOKENS)})
        self.assertEqual({a for a in asked if a[0] == "judge"}, {("judge", JUDGE_TEMPERATURE, JUDGE_MAX_TOKENS)})
        # The settings each call really used are saved with its row.
        self.assertEqual(self.rows("answer")[0]["details"]["call_settings"], {"api": "responses", "max_output_tokens": 8000})
        self.assertEqual(self.rows("judgement")[0]["details"]["call_settings"], {"api": "responses", "temperature": 0.0})

    def test_run_ids_count_up(self):
        self.assertEqual([self.store.create_run("q#1") for _ in range(3)], [1, 2, 3])

    def test_an_answer_succeeds_only_once_per_attempt(self):
        run_id = self.store.create_run("q#1")
        self.store.save_answer(run_id, "m", "Q1", 1, status="failed", error="timeout")
        self.store.save_answer(run_id, "m", "Q1", 1, status="ok", answer_text="first")
        self.store.save_answer(run_id, "m", "Q1", 1, status="ok", answer_text="second")
        self.assertEqual([a.answer_text for a in self.store.answers_to_judge(run_id)], ["first"])
        self.assertEqual(self.count("answer"), 2)

    def test_category_scores_use_the_category_saved_with_each_answer(self):
        run_id = self.run_engine()
        # The question file changing after the run does not change the result.
        self.materials = replace(self.materials, questions=[])
        self.store.publish(run_id)
        self.assertEqual(self.published()["models"][0]["categoryScores"], {"cat": 0.75})

    def test_export_writes_every_row_with_fixed_columns(self):
        run_id = self.run_engine()
        self.store.publish(run_id)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out" / "run.csv"
            written = self.store.export(run_id, path)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(tuple(reader.fieldnames), CSV_COLUMNS)
            self.assertEqual(written, len(rows))
            self.assertEqual(len(rows), 1 + 18 + 144 + 18 + 3)
            self.assertEqual(rows[0]["kind"], "run")
            self.assertEqual(json.loads(rows[0]["details"])["judge_models"], ["gpt-5.5", "judge-b", "judge-c"])
            score = next(row for row in rows if row["kind"] == "score" and row["model"] == "model-b")
            self.assertEqual((score["rank"], score["score"], score["provider"]), ("1", "0.75", "B"))
            judgement = next(row for row in rows if row["kind"] == "judgement")
            self.assertEqual((judgement["category"], judgement["text"], judgement["rubric_version"]), ("Cat", "Because.", "r#1"))

            self.assertEqual(self.store.export(run_id, path, kind="score"), 3)
            with self.assertRaisesRegex(ValueError, "Unknown kind"):
                self.store.export(run_id, path, kind="answers")


@unittest.skipUnless(TEST_DATABASE_URL, "Set TEST_DATABASE_URL to run PostgreSQL tests")
class SqlEngineTests(EngineTests, unittest.TestCase):
    def make_store(self):
        self.sql("DROP TABLE IF EXISTS log, rankings, model_scores, answer_labels, judgements, answers, models, runs")
        store = LogStore(SqlLog(TEST_DATABASE_URL))
        store.init_schema()
        return store

    def sql(self, statement):
        with psycopg.connect(TEST_DATABASE_URL) as connection:
            cursor = connection.execute(statement)
            return cursor.fetchall() if cursor.description else None

    def api_get(self):
        with patch.object(fastapi_app, "DATABASE_URL", TEST_DATABASE_URL):
            return TestClient(app).get("/api/rankings")

    def api_get_path(self, path):
        with patch.object(fastapi_app, "DATABASE_URL", TEST_DATABASE_URL):
            return TestClient(app).get(path)

    def test_rank_db_returns_the_newest_rankings_rows_and_the_old_routes_match(self):
        self.assertEqual(self.api_get_path("/api/rank_db").status_code, 404)
        run_id = self.run_engine()
        self.store.publish(run_id)
        response = self.api_get_path("/api/rank_db")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.published())
        for old in ("/api/rankings", "/rankings"):
            self.assertEqual(self.api_get_path(old).json(), response.json())
        saved = self.sql("SELECT rank, model_name, label, category_scores, llm_mode FROM rankings ORDER BY rank")
        self.assertEqual(saved[0], (1, "model-b", "High Bias", {"cat": 0.75}, self.config.llm_mode))
        self.assertEqual(len(saved), 3)

    def test_a_new_write_adds_rows_and_the_api_returns_the_newest(self):
        run_id = self.run_engine()
        self.store.publish(run_id)
        self.levels["model-c"] = "Very High Bias"
        judge_answers(self.store, run_id, self.materials, self.config, complete=self.complete, rejudge=True)
        self.calculate(run_id)
        self.store.publish(run_id)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM rankings"), [(6,)])
        self.assertEqual(self.api_get_path("/api/rank_db").json()["models"][0]["modelName"], "model-c")

    def test_each_run_has_one_run_row(self):
        self.store.create_run("q#1")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.sql("INSERT INTO log (run_id, kind) VALUES (1, 'run') RETURNING id")

    def test_each_kind_must_fill_its_columns(self):
        for statement in (
            "INSERT INTO log (run_id, kind, model, question_id) VALUES (1, 'answer', 'm', 'Q1')",
            "INSERT INTO log (run_id, kind, model, question_id, attempt, judge, prompt_order, status) "
            "VALUES (1, 'judgement', 'm', 'Q1', 1, 'j', 'ascending', 'ok')",
            "INSERT INTO log (run_id, kind, model) VALUES (1, 'score', 'm')",
            "INSERT INTO log (run_id, kind) VALUES (1, 'something')",
        ):
            with self.assertRaises(psycopg.errors.CheckViolation, msg=statement):
                self.sql(statement + " RETURNING id")


class CsvEngineTests(EngineTests, unittest.TestCase):
    def make_store(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        return LogStore(CsvLog(Path(self.folder.name)))

    def test_each_run_is_one_csv_file_that_a_new_process_can_continue(self):
        self.fail_answers_for = {"model-b"}
        run_id = self.run_engine()
        self.assertEqual(sorted(p.name for p in Path(self.folder.name).iterdir()), ["run_1.csv"])

        # A new process reads the file back and redoes only the failed calls.
        self.store = LogStore(CsvLog(Path(self.folder.name)))
        self.fail_answers_for, self.answer_calls = set(), 0
        ask_models(self.store, run_id, self.models, self.materials, self.config, complete=self.complete)
        self.assertEqual(self.answer_calls, 6)
        judge_answers(self.store, run_id, self.materials, self.config, complete=self.complete)
        self.calculate(run_id)
        self.assertEqual([row["model"] for row in self.store.run_rankings(run_id)], ["model-b", "gpt-5.5", "model-c"])

    def test_rows_read_back_from_the_file_equal_the_rows_written(self):
        self.store.publish(self.run_engine())
        written = self.store.log.read()
        reopened = CsvLog(Path(self.folder.name))
        self.assertEqual(reopened.read(), written)
        self.assertEqual(reopened.latest_rankings(), self.store.log.latest_rankings())
        self.assertEqual(sorted(p.name for p in Path(self.folder.name).iterdir()), ["run_1.csv", "run_1_rankings.csv"])


class FinishTests(unittest.TestCase):
    """The pipeline's last step: write the ranking unless calls failed."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = LogStore(CsvLog(Path(self.folder.name)))
        self.config = ScoringConfig(models=(Model("m", "P"),), judge_models=("j1", "j2", "j3"), workers=2,
                                    retry_base_seconds=0, n_answers=3, judge_order_mode="all")
        self.materials = small_materials()

    def run_with(self, fail_judge=None):
        def complete(model, prompt, max_tokens, temperature, schema):
            if schema is None:
                return "An answer"
            if model == fail_judge:
                raise RuntimeError("timeout")
            return json.dumps({"label": schema["properties"]["label"]["enum"][0], "reason": "r"})
        run_id = self.store.create_run("q#1", self.config.snapshot())
        ask_models(self.store, run_id, list(self.config.models), self.materials, self.config, complete=complete)
        judge_answers(self.store, run_id, self.materials, self.config, complete=complete)
        return run_id

    def test_a_clean_run_writes_the_ranking(self):
        run_id = self.run_with()
        with patch("sys.stdout"):
            self.assertTrue(cli._finish(self.store, run_id, self.config))
        self.assertEqual(self.store.run_status(run_id), "published")

    def test_failed_calls_stop_the_ranking_unless_the_setting_allows_it(self):
        run_id = self.run_with(fail_judge="j3")
        self.assertEqual(self.store.failed_calls(run_id), {"answers": 0, "judgements": 18})
        with patch("sys.stdout"):
            self.assertFalse(cli._finish(self.store, run_id, self.config))
            self.assertIsNone(self.store.published_ranking())
            self.assertTrue(cli._finish(self.store, run_id, replace(self.config, publish_if_calls_failed=True)))
        self.assertIsNotNone(self.store.published_ranking())


def setUpModule():
    # Any test that forgets to redirect the debug log writes here, not into src/logs.
    folder = tempfile.mkdtemp()
    patcher = patch.object(debug_log, "LOG_DIR", Path(folder) / "logs")
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class DebugLogTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(self.close_files)

    def close_files(self):
        for handler in [h for h in debug_log.logger.handlers if isinstance(h, logging.FileHandler)]:
            debug_log.logger.removeHandler(handler)
            handler.close()

    def test_files_from_7_days_ago_or_earlier_are_deleted(self):
        today = date(2026, 11, 25)
        for day in (1, 6, 7, 8, 30):
            (self.folder / f"{(today - timedelta(days=day)).isoformat()}.log").write_text("old")
        (self.folder / "notes.txt").write_text("not a log")
        path = debug_log.start(self.folder, keep_days=7, today=today)
        self.assertEqual(path.name, "2026-11-25.log")
        # Kept: today and the 6 days before it (7 days). Deleted: 7, 8 and 30 days ago.
        self.assertEqual(
            sorted(p.name for p in self.folder.iterdir()),
            ["2026-11-19.log", "2026-11-24.log", "2026-11-25.log", "notes.txt"],
        )

    def test_every_prompt_response_and_error_is_written(self):
        path = debug_log.start(self.folder)
        def complete(model, prompt, max_tokens, temperature, schema):
            if "fail" in prompt:
                raise RuntimeError("timeout")
            return f"reply to {prompt}"
        call = debug_log.logged(complete)
        self.assertEqual(call("m", "PROMPT ONE", 10, 0.5, None), "reply to PROMPT ONE")
        with self.assertRaises(RuntimeError):
            call("j", "please fail", 10, 0.0, {"type": "object"})
        text = path.read_text()
        for expected in ("call 1 answer model=m max_tokens=10 temperature=0.5", "--- prompt ---\nPROMPT ONE",
                         "--- response ---\nreply to PROMPT ONE", "call 2 judge model=j", "please fail",
                         "RuntimeError: timeout"):
            self.assertIn(expected, text)


class DebugFlagTests(unittest.TestCase):
    def main(self, *argv, env=None):
        with patch("sys.argv", ["python -m src.scoring", *argv]), patch.dict(os.environ, env or {}, clear=False):
            for name in ("CONTAINER_APP_NAME", "CONTAINER_APP_JOB_NAME"):
                if not env or name not in env:
                    os.environ.pop(name, None)
            cli.main()

    def test_a_command_that_writes_the_log_stops_off_azure_without_debug(self):
        for argv in (["run"], ["rejudge", "--run-id", "1"], ["merge", "--run-id", "1"]):
            with patch.object(cli, "SqlLog", side_effect=AssertionError("must not touch the database")), \
                    patch("sys.stderr"), self.assertRaises(SystemExit):
                self.main(*argv)

    def test_debug_writes_the_log_to_a_local_csv_file(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(cli, "CsvLog", lambda: CsvLog(Path(folder))), \
                patch.object(cli, "SqlLog", side_effect=AssertionError("must not touch the database")), \
                patch.object(cli, "load_materials", return_value=small_materials()), \
                patch.object(cli, "ScoringConfig", lambda: ScoringConfig(llm_mode="mock", models=(Model("m", "P"),),
                                                                         judge_models=("j1", "j2", "j3"), workers=2)), \
                patch("sys.stdout"):
            with patch.object(debug_log, "LOG_DIR", Path(folder) / "logs"):
                self.main("run", "--debug")
            rows = CsvLog(Path(folder)).read(1)
            log_text = next((Path(folder) / "logs").glob("*.log")).read_text()
            # One command: the ranking is written at the end, with no publish step.
            ranking = CsvLog(Path(folder)).latest_rankings()
        self.assertEqual({row["kind"] for row in rows}, set(KINDS))
        self.assertEqual([(r["rank"], r["model_name"], r["llm_mode"]) for r in ranking], [(1, "m", "mock")])
        # The log file has the command, each prompt sent and the ranking.
        self.assertIn("command: python -m src.scoring run --debug", log_text)
        self.assertIn("--- prompt ---\nQuestion 1?", log_text)
        self.assertIn("Ranking written to", log_text)
        self.assertEqual(log_text.count(" answer model=m "), 6)

    def test_llm_flag_overrides_the_config_for_one_command(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(cli, "CsvLog", lambda: CsvLog(Path(folder))), \
                patch.object(debug_log, "LOG_DIR", Path(folder) / "logs"), \
                patch.object(cli, "openai_client", side_effect=ValueError("AZURE_API_KEY missing")), \
                patch.object(cli, "load_materials", return_value=small_materials()), \
                patch("sys.stderr"), patch("sys.stdout"):
            with self.assertRaises(SystemExit):  # azure: checks the keys first
                self.main("run", "--debug", "--llm", "azure")
            self.assertFalse(Path(folder, "run_1.csv").exists())

    def test_on_azure_the_database_is_used(self):
        with patch.object(cli, "SqlLog", side_effect=RuntimeError("database used")), \
                patch.object(cli, "load_materials", return_value=small_materials()), patch("sys.stdout"):
            with self.assertRaisesRegex(RuntimeError, "database used"):
                self.main("show", "--run-id", "1")
            with tempfile.TemporaryDirectory() as folder, patch.object(debug_log, "LOG_DIR", Path(folder)):
                with self.assertRaisesRegex(RuntimeError, "database used"):
                    self.main("run", env={"CONTAINER_APP_JOB_NAME": "forge-bias-scoring"})
                self.assertEqual(list(Path(folder).iterdir()), [])  # online: no log file

if __name__ == "__main__":
    unittest.main()
