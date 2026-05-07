from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import requests

# Base directory for cached API responses
CACHE_DIR = Path("data") / "raw" / "api_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _make_cache_key(func_name: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> str:
    key_dict = {
        "func": func_name,
        "args": args,
        "kwargs": {k: kwargs[k] for k in sorted(kwargs)},
    }
    return hashlib.sha256(
        json.dumps(key_dict, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

def _cache_path_for_key(key: str) -> Path:
    # Map a cache key hash to a file path in the cache directory.
    return CACHE_DIR / f"{key}.json"

def cache_response(func: Callable) -> Callable:
    """
    Decorator that caches the JSONable return value of an API call to disk
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        key = _make_cache_key(func.__name__, args, kwargs)
        cache_path = _cache_path_for_key(key)

        if cache_path.exists():
            with cache_path.open("r", encoding="utf-8") as f:
                return json.load(f)

        # Cache miss: call the function
        result = func(*args, **kwargs)

        # Persist result to disk
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(result, f)

        return result

    return wrapper

class CoinGeckoClient:
    """
    Minimal client for the CoinGecko API, focused on historical price data
    """

    BASE_URL = "https://api.coingecko.com/api/v3"

    def __init__(self, session: requests.Session | None = None) -> None:
        # Uses a shared session for connection pooling. fall back to a new session if not provided
        self.session = session or requests.Session()

    @cache_response
    def _get_json(self, path: str, params: Dict[str, Any]) -> Any:
        """
        Issues a GET request and return parsed JSON, with caching to avoid redundant network calls
        """
        url = f"{self.BASE_URL}{path}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    

def get_price_history(
    self,
    coin_id: str,
    vs_currency: str,
    start_timestamp: int,
    end_timestamp: int,
) -> Dict[str, Any]:
    """Return raw CoinGecko market_chart JSON for a stablec over a time window"""
    path = f"/coins/{coin_id}/market_chart/range"
    params = {
        "vs_currency": vs_currency,
        "from": start_timestamp,
        "to": end_timestamp,
    }
    return self._get_json(path, params)