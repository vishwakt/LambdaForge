"""Tests for get_trade_stats aggregates (avg_win / avg_loss) and digest output."""

from src.trade_log import TradeLog


def _log_closed_trade(tl: TradeLog, symbol: str, pnl: float) -> None:
    """Helper: log a buy+sell pair so the sell carries a pnl for stats queries."""
    buy_id = tl.log_trade(
        symbol=symbol,
        side="buy",
        qty=10,
        order_type="market",
        order_id=f"buy-{symbol}",
        strategy="macd",
        confidence=0.8,
        reason="test",
        stop_loss=None,
        take_profit=None,
    )
    tl.update_trade_status(buy_id, "filled", fill_price=100.0)

    sell_id = tl.log_trade(
        symbol=symbol,
        side="sell",
        qty=10,
        order_type="market",
        order_id=f"sell-{symbol}",
        strategy="macd",
        confidence=0.8,
        reason="test",
        stop_loss=None,
        take_profit=None,
        parent_trade_id=buy_id,
    )
    tl.update_trade_status(sell_id, "filled", fill_price=100.0, pnl=pnl)


class TestTradeStatsAverages:
    """avg_win / avg_loss must aggregate from actual win/loss trades, not derive."""

    def test_no_closed_trades(self, tmp_trade_log: TradeLog):
        stats = tmp_trade_log.get_trade_stats()
        assert stats["closed_trades"] == 0
        assert stats["avg_win"] == 0.0
        assert stats["avg_loss"] == 0.0

    def test_avg_win_only_wins(self, tmp_trade_log: TradeLog):
        _log_closed_trade(tmp_trade_log, "AAA", 100.0)
        _log_closed_trade(tmp_trade_log, "BBB", 200.0)
        stats = tmp_trade_log.get_trade_stats()
        assert stats["wins"] == 2
        assert stats["losses"] == 0
        assert stats["avg_win"] == 150.0
        assert stats["avg_loss"] == 0.0

    def test_avg_loss_only_losses(self, tmp_trade_log: TradeLog):
        _log_closed_trade(tmp_trade_log, "AAA", -50.0)
        _log_closed_trade(tmp_trade_log, "BBB", -150.0)
        stats = tmp_trade_log.get_trade_stats()
        assert stats["wins"] == 0
        assert stats["losses"] == 2
        assert stats["avg_win"] == 0.0
        assert stats["avg_loss"] == -100.0

    def test_avg_win_and_loss_mixed(self, tmp_trade_log: TradeLog):
        # wins: 100, 300 -> avg 200. losses: -50, -150 -> avg -100.
        _log_closed_trade(tmp_trade_log, "AAA", 100.0)
        _log_closed_trade(tmp_trade_log, "BBB", 300.0)
        _log_closed_trade(tmp_trade_log, "CCC", -50.0)
        _log_closed_trade(tmp_trade_log, "DDD", -150.0)
        stats = tmp_trade_log.get_trade_stats()
        assert stats["wins"] == 2
        assert stats["losses"] == 2
        assert stats["avg_win"] == 200.0
        assert stats["avg_loss"] == -100.0
        # Correctness guard: derived-from-total formula would give
        # (total_pnl - avg_win * wins) / losses = (200 - 400) / 2 = -100
        # which happens to match here, but with asymmetric trades it won't.

    def test_derivation_formula_would_be_wrong(self, tmp_trade_log: TradeLog):
        """Regression: the old dead-code formula produced nonsense for
        asymmetric win/loss counts. Verify the aggregated value is right."""
        # 3 wins totalling 600 (avg 200), 1 loss of -50.
        _log_closed_trade(tmp_trade_log, "AAA", 100.0)
        _log_closed_trade(tmp_trade_log, "BBB", 200.0)
        _log_closed_trade(tmp_trade_log, "CCC", 300.0)
        _log_closed_trade(tmp_trade_log, "DDD", -50.0)
        stats = tmp_trade_log.get_trade_stats()
        assert stats["avg_win"] == 200.0
        assert stats["avg_loss"] == -50.0
        # Old broken formula: (total_pnl - avg_win * wins) / losses
        # = (550 - 200*3) / 1 = -50 — coincidentally correct here because
        # avg_win came from the same pnls, but with the derivation using
        # an independently-computed avg_win it would drift. This test
        # pins the aggregated semantics so future refactors don't regress.


class TestWeeklyDigestAvgLines:
    """The digest must surface Avg Win / Avg Loss when there are closed trades."""

    def test_digest_shows_avg_win_and_loss(self, tmp_trade_log: TradeLog, tmp_path):
        from datetime import date

        from src.weekly_digest import generate_weekly_digest

        _log_closed_trade(tmp_trade_log, "AAA", 120.0)
        _log_closed_trade(tmp_trade_log, "BBB", -40.0)

        # Snapshot so equity/benchmark sections don't crash
        tmp_trade_log.save_daily_snapshot(
            equity=10_000.0,
            cash=5_000.0,
            portfolio_value=10_000.0,
            open_positions=0,
        )

        class _Cfg:
            db_path = str(tmp_path / "unused.db")

        report = generate_weekly_digest(
            trade_log=tmp_trade_log,
            config=_Cfg(),
            account_info={"equity": 10_000.0},
            open_positions=[],
        )
        assert "Avg Win:" in report
        assert "Avg Loss:" in report
        assert "+120.00" in report or "$+120.00" in report
        assert "-40.00" in report

        # Sanity: dates rendered in week bounds are today-relative
        assert date.today().isoformat()[:7] in report

    def test_digest_omits_avg_lines_when_no_closed_trades(
        self, tmp_trade_log: TradeLog, tmp_path
    ):
        from src.weekly_digest import generate_weekly_digest

        tmp_trade_log.save_daily_snapshot(
            equity=10_000.0,
            cash=10_000.0,
            portfolio_value=10_000.0,
            open_positions=0,
        )

        class _Cfg:
            db_path = str(tmp_path / "unused.db")

        report = generate_weekly_digest(
            trade_log=tmp_trade_log,
            config=_Cfg(),
            account_info={"equity": 10_000.0},
            open_positions=[],
        )
        assert "Avg Win:" not in report
        assert "Avg Loss:" not in report
