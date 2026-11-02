"""Tests for buy order deduplication logic."""

from src.dynamo_store import DynamoTradeLog


class TestBuyDeduplication:
    """Test has_pending_buy() prevents duplicate orders."""

    def test_no_pending_buy(self, any_trade_log: DynamoTradeLog):
        """No existing orders → not a duplicate."""
        assert any_trade_log.has_pending_buy("AAPL", "macd") is False

    def test_pending_buy_blocks(self, any_trade_log: DynamoTradeLog):
        """Submitted (unfilled) buy for same symbol+strategy → blocked."""
        any_trade_log.log_trade(
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
        assert any_trade_log.has_pending_buy("AAPL", "macd") is True

    def test_different_strategy_allowed(self, any_trade_log: DynamoTradeLog):
        """Pending buy from different strategy → not blocked (pyramiding)."""
        any_trade_log.log_trade(
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
        assert any_trade_log.has_pending_buy("AAPL", "bollinger") is False

    def test_different_symbol_allowed(self, any_trade_log: DynamoTradeLog):
        """Pending buy for different symbol → not blocked."""
        any_trade_log.log_trade(
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
        assert any_trade_log.has_pending_buy("MSFT", "macd") is False

    def test_filled_order_not_blocked(self, any_trade_log: DynamoTradeLog):
        """Filled buy → allows new buy (pyramiding after confirmation)."""
        trade_id = any_trade_log.log_trade(
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
        any_trade_log.update_trade_status(trade_id, "filled", fill_price=150.0)
        assert any_trade_log.has_pending_buy("AAPL", "macd") is False

    def test_sell_order_not_counted(self, any_trade_log: DynamoTradeLog):
        """Pending sell order doesn't affect buy dedup."""
        any_trade_log.log_trade(
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
        assert any_trade_log.has_pending_buy("AAPL", "macd") is False

    def test_multiple_pending_buys_still_blocks(self, any_trade_log: DynamoTradeLog):
        """Multiple pending buys for same symbol+strategy → still blocked."""
        for i in range(3):
            any_trade_log.log_trade(
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
        assert any_trade_log.has_pending_buy("AAPL", "macd") is True
