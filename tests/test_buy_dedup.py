"""Tests for buy order deduplication logic."""

from src.trade_log import TradeLog


class TestBuyDeduplication:
    """Test has_pending_buy() prevents duplicate orders."""

    def test_no_pending_buy(self, tmp_trade_log: TradeLog):
        """No existing orders → not a duplicate."""
        assert tmp_trade_log.has_pending_buy("AAPL", "macd") is False

    def test_pending_buy_blocks(self, tmp_trade_log: TradeLog):
        """Submitted (unfilled) buy for same symbol+strategy → blocked."""
        tmp_trade_log.log_trade(
            symbol="AAPL",
            side="buy",
            qty=10,
            order_type="market",
            order_id="ord-1",
            strategy="macd",
            confidence=0.8,
            reason="test",
            stop_loss=145.0,
            take_profit=160.0,
        )
        assert tmp_trade_log.has_pending_buy("AAPL", "macd") is True

    def test_different_strategy_allowed(self, tmp_trade_log: TradeLog):
        """Pending buy from different strategy → not blocked (pyramiding)."""
        tmp_trade_log.log_trade(
            symbol="AAPL",
            side="buy",
            qty=10,
            order_type="market",
            order_id="ord-1",
            strategy="macd",
            confidence=0.8,
            reason="test",
            stop_loss=145.0,
            take_profit=160.0,
        )
        assert tmp_trade_log.has_pending_buy("AAPL", "bollinger") is False

    def test_different_symbol_allowed(self, tmp_trade_log: TradeLog):
        """Pending buy for different symbol → not blocked."""
        tmp_trade_log.log_trade(
            symbol="AAPL",
            side="buy",
            qty=10,
            order_type="market",
            order_id="ord-1",
            strategy="macd",
            confidence=0.8,
            reason="test",
            stop_loss=145.0,
            take_profit=160.0,
        )
        assert tmp_trade_log.has_pending_buy("MSFT", "macd") is False

    def test_filled_order_not_blocked(self, tmp_trade_log: TradeLog):
        """Filled buy → allows new buy (pyramiding after confirmation)."""
        trade_id = tmp_trade_log.log_trade(
            symbol="AAPL",
            side="buy",
            qty=10,
            order_type="market",
            order_id="ord-1",
            strategy="macd",
            confidence=0.8,
            reason="test",
            stop_loss=145.0,
            take_profit=160.0,
        )
        tmp_trade_log.update_trade_status(trade_id, "filled", fill_price=150.0)
        assert tmp_trade_log.has_pending_buy("AAPL", "macd") is False

    def test_sell_order_not_counted(self, tmp_trade_log: TradeLog):
        """Pending sell order doesn't affect buy dedup."""
        tmp_trade_log.log_trade(
            symbol="AAPL",
            side="sell",
            qty=10,
            order_type="market",
            order_id="ord-1",
            strategy="macd",
            confidence=0.8,
            reason="test",
            stop_loss=None,
            take_profit=None,
        )
        assert tmp_trade_log.has_pending_buy("AAPL", "macd") is False

    def test_multiple_pending_buys_still_blocks(self, tmp_trade_log: TradeLog):
        """Multiple pending buys for same symbol+strategy → still blocked."""
        for i in range(3):
            tmp_trade_log.log_trade(
                symbol="AAPL",
                side="buy",
                qty=10,
                order_type="market",
                order_id=f"ord-{i}",
                strategy="macd",
                confidence=0.8,
                reason="test",
                stop_loss=145.0,
                take_profit=160.0,
            )
        assert tmp_trade_log.has_pending_buy("AAPL", "macd") is True
