"""
This module is the data-fetching layer of the project. It talks to
external services and caches every response on disk, so re-running the
notebooks does not hammer free tier APIs.

Sources actually used in the analysis:
- DefiLlama (coins.llama.fi): hourly USD prices for USDC and USDT.
  Free, no API key, full history (max 500 points per request, so the
  client pages through longer ranges).
- DefiLlama (stablecoins.llama.fi): daily circulating supply per stablecoin.
  Supply changes are the on-chain mint/burn signal (redemption pressure).
- Coinbase Exchange public API: USDT-USDC candles with volume. Free, no
  API key, continuous history through the whole sample window.

Kept for reference but NOT usable for this study:
- CoinGecko: my original plan was to pull everything from CoinGecko, but the
  free tier only serves history from the last 365 days, and the depeg
  happened in March 2023. The client still works for recent dates so I left
  it in. See reports/findings.md for how this changed the project.
- Binance: has by far the deepest stablecoin books, but it delisted USDC
  pairs in September 2022 and only relisted USDCUSDT during the depeg
  itself (first candle 2023-03-11 14:00 UTC), so there is no pre-event
  volume baseline. Discovered the hard way in notebook 01.
- Etherscan: I also planned to reconstruct per-transfer exchange flows from
  Etherscan, but labelled exchange flow data at that scale sits behind paid
  products (Nansen, Glassnode), and the free endpoint is per-address, not
  per-token. Circulating supply from DefiLlama is the honest free
  alternative for an on-chain signal.

Cache:
All HTTP responses are cached as JSON files under data/raw/api_cache/
Cache keys are SHA-256 hashes of (function name, arguments).

Usage
-----
>>> from src.fetch import DefiLlamaClient, BinanceClient
>>> llama = DefiLlamaClient()
>>> prices = llama.get_price_history("usd-coin", "2023-03-01", "2023-03-20")
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests


# Cache infrastructure -------------------------------------------------


# Where cached API responses live. Anchored to the repo root (one level up
# from src/) so notebooks and scripts share the same cache no matter which directory they run from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "raw" / "api_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Bumping this constant invalidates every cached response without deleting files manually

CACHE_VERSION = "v1"


def _make_cache_key(func_name: str, args: tuple, kwargs: dict) -> str:
    """Hashes a function call into a stable filename safe key."""

    primitive_types = (str, int, float, bool, list, dict, tuple, type(None))
    if args and not isinstance(args[0], primitive_types):
        # First arg looks like an instance; replace with the class name
        stable_args: tuple = (args[0].__class__.__name__,) + args[1:]
    else:
        stable_args = args

    payload = {
        "version": CACHE_VERSION,
        "func": func_name,
        "args": stable_args,
        "kwargs": {k: kwargs[k] for k in sorted(kwargs)},
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def cache_response(func: Callable) -> Callable:
    """
    Decorator: cache the JSON serializable return value of "func" to disk.

    Set "FORCE_REFRESH=1" in the environment to bypass cache reads for a single run; 
    cache writes still happen so the next run picks up fresh data.
    Only works for functions whose return value is JSON serializable.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = _make_cache_key(func.__name__, args, kwargs)
        cache_path = CACHE_DIR / f"{key}.json"

        if cache_path.exists() and not os.environ.get("FORCE_REFRESH"):
            with cache_path.open("r", encoding="utf-8") as f:
                return json.load(f)

        result = func(*args, **kwargs)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(result, f)
        return result

    return wrapper


# CoinGecko client


def _timestamp_to_unix(ts: pd.Timestamp) -> int:
    """Convert a "pd.Timestamp" to a UTC Unix integer, as expected by CoinGecko's API."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.timestamp())


class CoinGeckoClient:
    """
    Minimal CoinGecko client for daily price history.
    Free tier rate-limits at roughly 30 calls per minute; sleep before each request to stay under that limit.

    NOTE: the free tier only serves the most recent 365 days. Requests for
    March 2023 return error 10012, which is why the analysis uses
    DefiLlamaClient below instead. Kept because it works for recent dates
    and I did not want to throw away tested code.

    Coin IDs:
        usd-coin — USDC
        tether   — USDT
    """

    BASE_URL = "https://api.coingecko.com/api/v3"

    def __init__(
        self,
        session: requests.Session | None = None,
        sleep_seconds: float = 2.5,
    ) -> None:
        # A shared Session reuses underlying TCP connections across calls, faster than creating a new connection each time.
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "stablecoin-reserve-analysis/0.1"}
        )
        self.sleep_seconds = sleep_seconds

    @cache_response
    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        """Issue a GET to CoinGecko and return parsed JSON.
        """
        time.sleep(self.sleep_seconds)  # polite citizen of the free tier
        url = f"{self.BASE_URL}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()  # raises HTTPError if the response was error
        return resp.json()


    def get_price_history(
        self,
        coin_id: str,
        vs_currency: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        """Return daily mean price for "coin_id" over "[start, end]"

        CoinGecko's "market_chart/range" endpoint returns intraday data points whose cadence varies by date range.
        I aggregate to daily mean as a defensible VWAP proxy at this resolution.

        Parameters
        ----------
        coin_id
            CoinGecko ID, for example "usd-coin" or "tether".
        vs_currency
            Pricing currency, for example "usd".
        start, end
            Inclusive UTC date bounds for the query.

        Returns
        -------
        pd.DataFrame
            Indexed by date with one column f"price_{coin_id}".
        """
        params = {
            "vs_currency": vs_currency,
            "from": _timestamp_to_unix(start),
            "to": _timestamp_to_unix(end),
        }
        data = self._get_json(f"/coins/{coin_id}/market_chart/range", params)

        if "prices" not in data or not data["prices"]:
            raise ValueError(
                f"CoinGecko returned no price data for "
                f"{coin_id}/{vs_currency} between {start} and {end}"
            )

        # CoinGecko returns prices as [[milliseconds_since_epoch, price], ...]
        prices = pd.DataFrame(data["prices"], columns=["ts_ms", "price"])
        prices["date"] = (
            pd.to_datetime(prices["ts_ms"], unit="ms", utc=True).dt.date
        )
        daily = (
            prices.groupby("date", as_index=False)["price"]
            .mean()
            .rename(columns={"price": f"price_{coin_id}"})
        )
        daily["date"] = pd.to_datetime(daily["date"])
        return daily.set_index("date").sort_index()


# DefiLlama client
# Two separate hosts: coins.llama.fi for prices, stablecoins.llama.fi for circulating supply. 
# Both are free, and unlike CoinGecko they serve the full history.


class DefiLlamaClient:
    """
    Client for DefiLlama's price and stablecoin APIs.

    Prices come from /chart/coingecko:{coin_id} which reuses CoinGecko's
    coin IDs ("usd-coin", "tether"), so the IDs I already looked up for the
    CoinGecko client carry over.

    Circulating supply comes from /stablecoincharts/all?stablecoin={id}
    where the IDs are DefiLlama's own (USDT = 1, USDC = 2). The endpoint
    always returns the full history since launch; I filter locally.
    """

    COINS_URL = "https://coins.llama.fi"
    STABLECOINS_URL = "https://stablecoins.llama.fi"

    # DefiLlama's internal stablecoin IDs (from /stablecoins listing)
    STABLECOIN_IDS = {"USDT": 1, "USDC": 2}

    # The chart endpoint rejects requests for more than 500 points, so
    # longer ranges are fetched in chunks (each chunk cached separately).
    MAX_SPAN = 500

    def __init__(
        self,
        session: requests.Session | None = None,
        sleep_seconds: float = 0.5,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "stablecoin-reserve-analysis/0.1"}
        )
        self.sleep_seconds = sleep_seconds

    @cache_response
    def _get_json(self, url: str) -> Any:
        """Issue a GET for a full URL and return parsed JSON."""
        time.sleep(self.sleep_seconds)
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_price_history(
        self,
        coin_id: str,
        start: str | pd.Timestamp,
        end: str | pd.Timestamp,
        period: str = "1d",
    ) -> pd.DataFrame:
        """Return USD price snapshots for `coin_id` over [start, end].

        DefiLlama returns one snapshot per `period` ("1d" or "1h"), taken
        close to the period boundary (within a few minutes of midnight UTC
        for daily data). These are aggregated across venues by DefiLlama,
        so they are genuine USD prices. Important here, because pricing
        USDC in USDT during the depeg would mix two moving pegs.

        Returns a DataFrame indexed by UTC timestamp with one column,
        "price".
        """
        start = pd.Timestamp(start)
        end = pd.Timestamp(end)
        if period == "1d":
            step_seconds = 86_400
            span = (end - start).days + 1
        elif period == "1h":
            step_seconds = 3_600
            span = int((end - start).total_seconds() // 3600) + 1
        else:
            raise ValueError(f"Unsupported period: {period!r}")

        # Page through the range in chunks of at most MAX_SPAN points
        points: list[dict] = []
        fetched = 0
        while fetched < span:
            chunk_span = min(self.MAX_SPAN, span - fetched)
            chunk_start = _timestamp_to_unix(start) + fetched * step_seconds
            url = (
                f"{self.COINS_URL}/chart/coingecko:{coin_id}"
                f"?start={chunk_start}&span={chunk_span}&period={period}"
            )
            data = self._get_json(url)
            try:
                points.extend(data["coins"][f"coingecko:{coin_id}"]["prices"])
            except KeyError:
                raise ValueError(
                    f"DefiLlama returned no prices for {coin_id!r}"
                )
            fetched += chunk_span

        df = pd.DataFrame(points)
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df = df.drop_duplicates(subset="timestamp")
        return df.set_index("timestamp")[["price"]].sort_index()

    def get_circulating_supply(self, symbol: str) -> pd.DataFrame:
        """Return the full daily circulating supply history for `symbol`.

        A drop in circulating supply means more tokens were burned than
        minted that day — i.e. net primary-market redemptions. This is the
        on-chain signal in the Depeg Pressure Index.

        Returns a DataFrame indexed by date with one column, "supply"
        (USD-pegged tokens outstanding).
        """
        if symbol not in self.STABLECOIN_IDS:
            raise ValueError(
                f"Unknown symbol {symbol!r}; known: {list(self.STABLECOIN_IDS)}"
            )
        coin_id = self.STABLECOIN_IDS[symbol]
        url = f"{self.STABLECOINS_URL}/stablecoincharts/all?stablecoin={coin_id}"
        data = self._get_json(url)

        rows = []
        for entry in data:
            rows.append(
                {
                    # the API sends the unix date as a string
                    "date": pd.to_datetime(int(entry["date"]), unit="s"),
                    "supply": entry["totalCirculating"]["peggedUSD"],
                }
            )
        return pd.DataFrame(rows).set_index("date").sort_index()