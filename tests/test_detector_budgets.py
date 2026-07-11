"""Tests for P1 model-aware detector budgets (stall-aware detector).

Co-designed with ChatGPT (conversation 6a4ebc1e, 2026-07-08). Replaces the
single PHASE_STALL_SECONDS=90 with a model-aware, phase-substate-aware state
machine: first-content-wait vs stream-idle, with longer first-content budgets
for reasoning models that think silently before streaming.

Covers:
  - classify_model: maps a model slug to "reasoning" | "default"
  - DetectorConfig: the 5-key config on ChatGPTConfig
  - DetectorBudgets: resolved per-call budgets from config + model class
"""

import pytest

from chatgpt_web2api.completion_detector import (
    DetectorBudgets,
    classify_model,
)

# ── classify_model ──────────────────────────────────────────────────────


class TestClassifyModel:
    """classify_model maps a ChatGPT web slug to a model class bucket."""

    @pytest.mark.parametrize("slug", [
        "gpt-5-4-thinking",
        "gpt-5-5-thinking",
        "gpt-5-4-t-mini",       # "Thinking Mini"
        "o3",
        "research",
    ])
    def test_reasoning_models_classified_as_reasoning(self, slug):
        assert classify_model(slug) == "reasoning"

    @pytest.mark.parametrize("slug", [
        "gpt-5-5",
        "gpt-5-5-instant",
        "gpt-5-3",
        "gpt-5-mini",
        "gpt-5-5-mini",
        "auto",
        "gpt-5.5-wm",
    ])
    def test_non_reasoning_models_classified_as_default(self, slug):
        assert classify_model(slug) == "default"

    def test_unknown_model_defaults_to_default(self):
        """An unrecognized slug is conservative — default budgets, not reasoning.
        Reasoning budgets are longer, so defaulting an unknown model to 'default'
        fails fast rather than holding a possibly-dead generation too long."""
        assert classify_model("some-new-model-xyz") == "default"

    def test_none_or_empty_defaults_to_default(self):
        assert classify_model(None) == "default"
        assert classify_model("") == "default"

    def test_case_insensitive(self):
        """Model slugs from the web are lowercase, but be robust."""
        assert classify_model("GPT-5-5-Thinking") == "reasoning"
        assert classify_model("O3") == "reasoning"


# ── DetectorBudgets ─────────────────────────────────────────────────────


class TestDetectorBudgets:
    """DetectorBudgets holds the resolved per-call timeout values."""

    def test_default_budgets_match_legacy_90s(self):
        """A default-class model with default config should reproduce the old
        90s behavior on both first-content and stream-idle — no behavior change
        for non-reasoning models."""
        budgets = DetectorBudgets.default()
        assert budgets.first_content_timeout_seconds == 90
        assert budgets.stream_idle_timeout_seconds == 90
        assert budgets.hard_timeout_seconds == 900

    def test_reasoning_budgets_have_longer_first_content(self):
        """The core P1 fix: reasoning models get a longer first-content window
        so their silent thinking phase isn't falsely aborted."""
        budgets = DetectorBudgets.reasoning()
        assert budgets.first_content_timeout_seconds > 90, (
            "reasoning first-content must be longer than the legacy 90s"
        )
        assert budgets.first_content_timeout_seconds == 300

    def test_reasoning_stream_idle_is_shorter_than_first_content(self):
        """Once text has started streaming, a long idle IS suspicious — stream-idle
        should be shorter than first-content for reasoning models."""
        budgets = DetectorBudgets.reasoning()
        assert budgets.stream_idle_timeout_seconds < budgets.first_content_timeout_seconds

    def test_hard_cap_is_absolute_for_both_classes(self):
        """The hard cap bounds total observation time regardless of DOM signals.
        Both default and reasoning share the same cap."""
        d = DetectorBudgets.default()
        r = DetectorBudgets.reasoning()
        assert d.hard_timeout_seconds == r.hard_timeout_seconds == 900

    def test_from_config_default_model(self):
        """Building budgets from config + a default-class model yields the
        default first-content / stream-idle from config."""
        from chatgpt_web2api.config import ChatGPTConfig

        cfg = ChatGPTConfig()
        budgets = DetectorBudgets.from_config(cfg, model="gpt-5-5")
        assert budgets.first_content_timeout_seconds == cfg.detector_default_first_content_timeout_seconds
        assert budgets.stream_idle_timeout_seconds == cfg.detector_default_stream_idle_timeout_seconds

    def test_from_config_reasoning_model(self):
        """Building budgets from config + a reasoning model yields the reasoning
        first-content / stream-idle from config."""
        from chatgpt_web2api.config import ChatGPTConfig

        cfg = ChatGPTConfig()
        budgets = DetectorBudgets.from_config(cfg, model="gpt-5-5-thinking")
        assert budgets.first_content_timeout_seconds == cfg.detector_reasoning_first_content_timeout_seconds
        assert budgets.stream_idle_timeout_seconds == cfg.detector_reasoning_stream_idle_timeout_seconds
