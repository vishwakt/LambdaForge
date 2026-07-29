"""Tests for the symbol/strategy scan plan — the LLM cost-containment layer."""

from src.scheduler import plan_symbol_strategies

GENERAL = ["SPY", "QQQ", "JPM"]
COHORT = ["AAPL", "MSFT", "GOOGL"]


class TestPlanSymbolStrategies:
    def test_llm_never_runs_on_general_watchlist(self):
        plan = plan_symbol_strategies(GENERAL, COHORT, ["macd", "llm"], 10)
        for symbol in GENERAL:
            assert "llm" not in plan[symbol]

    def test_llm_runs_only_on_cohort(self):
        plan = plan_symbol_strategies(GENERAL, COHORT, ["macd", "llm"], 10)
        for symbol in COHORT:
            assert plan[symbol] == ["llm"]

    def test_cap_slices_cohort(self):
        plan = plan_symbol_strategies(GENERAL, COHORT, ["llm"], 2)
        assert "AAPL" in plan and "MSFT" in plan
        assert "GOOGL" not in plan  # beyond the cap — never scanned

    def test_llm_disabled_when_not_in_strategies(self):
        plan = plan_symbol_strategies(GENERAL, COHORT, ["macd"], 10)
        assert set(plan) == set(GENERAL)

    def test_empty_cohort_means_llm_never_runs(self):
        plan = plan_symbol_strategies(GENERAL, [], ["macd", "llm"], 10)
        assert all("llm" not in strats for strats in plan.values())

    def test_overlapping_symbol_gets_both(self):
        """If a symbol is in both lists, technical strategies + llm both run."""
        plan = plan_symbol_strategies(["AAPL"], ["AAPL"], ["macd", "llm"], 10)
        assert plan["AAPL"] == ["macd", "llm"]

    def test_general_strategies_unchanged_by_llm_config(self):
        plan = plan_symbol_strategies(GENERAL, COHORT, ["macd", "bollinger"], 10)
        assert plan["SPY"] == ["macd", "bollinger"]
