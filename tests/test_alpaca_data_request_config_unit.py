"""Offline regression tests for explicit Alpaca stock-bar request settings."""

from types import SimpleNamespace

import pandas as pd
import pytest
from alpaca.data.enums import Adjustment, DataFeed

from lumibot.data_sources.alpaca_data import AlpacaData, _stock_request_days_needed
from lumibot.entities import Asset


def _source(**config_overrides):
    config = {"API_KEY": "unit-key", "API_SECRET": "unit-secret"}
    config.update(config_overrides)
    return AlpacaData(config, delay=0)


def test_hourly_stock_history_requests_enough_sessions():
    """Hourly indicator windows must not be reduced to a one-session request."""
    assert _stock_request_days_needed(200, "hour") == 31
    assert _stock_request_days_needed(200, "1h") == 31
    assert _stock_request_days_needed(390, "minute") == 3


def test_single_stock_request_uses_explicit_feed_and_adjustment(monkeypatch):
    source = _source(STOCK_DATA_FEED="iex", STOCK_DATA_ADJUSTMENT="split")
    captured = []

    class Client:
        def get_stock_bars(self, request):
            captured.append(request)
            return SimpleNamespace(df=pd.DataFrame())

    monkeypatch.setattr(source, "_get_stock_client", lambda: Client())

    result = source._get_dataframe_from_api(Asset("SPY"), 10, "day")

    assert result is None
    assert captured[0].feed is DataFeed.IEX
    assert captured[0].adjustment is Adjustment.SPLIT


def test_batched_stock_request_uses_explicit_feed_and_adjustment(monkeypatch):
    source = _source(STOCK_DATA_FEED=DataFeed.SIP, STOCK_DATA_ADJUSTMENT=Adjustment.RAW)
    captured = []

    class Client:
        def get_stock_bars(self, request):
            captured.append(request)
            return SimpleNamespace(df=pd.DataFrame())

    monkeypatch.setattr(source, "_get_stock_client", lambda: Client())

    assert source.get_bars([Asset("SPY")], 10, timestep="day") == {}
    assert captured[0].feed is DataFeed.SIP
    assert captured[0].adjustment is Adjustment.RAW


def test_invalid_explicit_stock_feed_fails_loudly():
    with pytest.raises(ValueError, match="stock_data_feed"):
        _source(STOCK_DATA_FEED="unknown-feed")
