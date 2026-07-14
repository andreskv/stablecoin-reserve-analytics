"""
This module is the data-fetching layer of the project. It talks to three
external services and caches every response on disk, so re-running the
notebooks does not hammer free tier APIs.

Sources:
- CoinGecko: daily price for USDC and USDT.
- DefiLlama
- Etherscan

Cache:
All HTTP responses are cached as JSON files under data/raw/api_cache/
Cache keys are SHA-256 hashes of (function name, arguments).

Usage

-----

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


# Cache infrastructure --------------------------


# Where cached API responses live. Anchored to the repo root (one level up
# from src/) so notebooks and scripts share the same cache no matter which
# directory they run from.
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