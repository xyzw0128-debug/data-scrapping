"""Twelve Data API client with per-endpoint credit accounting and error classification."""

from __future__ import annotations

import http.client
import json
import logging
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.config import ProviderConfig
from src.rate_limit import KeyPool, KeySlot, PerMinuteRateLimiter, RunCreditCap


API_ROOT = "https://api.twelvedata.com"
# Measured on 2026-10-20 with /api_usage daily_usage deltas on a Basic key.
# /dividends and /splits are not listed: Basic keys get HTTP 403 for them.
CREDIT_COSTS = {"time_series": 1, "api_usage": 1, "stocks": 0, "etfs": 0}
NETWORK_ATTEMPTS = 2
USAGE_SYNC_ATTEMPTS = 2
RATE_LIMIT_RETRIES = 2
RATE_LIMIT_SLEEP_SECONDS = 60

HttpGet = Callable[[str], tuple[int, Any]]


class TwelveDataError(Exception):
    """Error response from Twelve Data.

    ``uncertain_attempts`` counts attempts whose billing is unknown (network errors, lost
    responses, HTTP 5xx); callers charge them conservatively.
    """

    def __init__(self, message: str, status: int | None = None, uncertain_attempts: int = 0):
        super().__init__(message)
        self.status = status
        self.uncertain_attempts = uncertain_attempts


class TemporaryRateLimitError(TwelveDataError):
    """Per-minute limit; retry with the same key after a pause."""


class ApiCreditsExhaustedError(TwelveDataError):
    """The provider says this key has no credits left today."""


class SymbolNotFoundError(TwelveDataError):
    """The symbol is unknown to Twelve Data (delisted or renamed)."""


class BudgetExhaustedError(Exception):
    """No key budget or run credit cap left for another call."""


def classify_error(status: int | None, message: str) -> type[TwelveDataError]:
    """Map a Twelve Data error to the operational exception class."""
    msg = message.lower()
    if "for the day" in msg or "daily" in msg and "limit" in msg:
        return ApiCreditsExhaustedError
    if "current minute" in msg or "rate limit" in msg or status == 429:
        return TemporaryRateLimitError
    if status == 404 or "not found" in msg:
        return SymbolNotFoundError
    return TwelveDataError


def _error_message(payload: Any) -> str | None:
    """Return the error message of an error payload, or None for a successful payload."""
    if isinstance(payload, dict) and payload.get("status") == "error":
        return str(payload.get("message") or payload.get("code") or "unknown Twelve Data error")
    return None


def default_http_get(url: str) -> tuple[int, Any]:
    """GET a URL and return (HTTP status, parsed JSON); HTTP errors return their JSON body.

    An error body that cannot be read (connection dropped after the status line) falls back
    to the HTTP reason, so the status code still decides how the error is handled.
    """
    request = Request(url, headers={"User-Agent": "data-scrapping/0.2"})
    try:
        with urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except (http.client.HTTPException, OSError):
            body = ""
        try:
            payload: Any = json.loads(body)
        except ValueError:
            payload = {"status": "error", "message": body[:200] or exc.reason}
        return exc.code, payload


class TwelveDataClient:
    """Make Twelve Data calls inside the key budgets, the run credit cap, and the per-minute limit."""

    def __init__(
        self,
        config: ProviderConfig,
        key_pool: KeyPool,
        rate_limiter: PerMinuteRateLimiter,
        cap: RunCreditCap,
        logger: logging.Logger,
        http_get: HttpGet = default_http_get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.key_pool = key_pool
        self.rate_limiter = rate_limiter
        self.cap = cap
        self.logger = logger
        self.http_get = http_get
        self.sleep = sleep

    def can_afford(self, cost: int) -> bool:
        """True when both the run cap and some key can pay ``cost`` credits."""
        return self.cap.allows(cost) and self.key_pool.current_slot(cost) is not None

    def _get(self, endpoint: str, params: dict[str, Any], api_key: str) -> tuple[int, Any, int]:
        """Return (status, payload, uncertain attempts); retries network errors and 5xx once."""
        base = self.config.base_url if endpoint == "time_series" and self.config.base_url else f"{API_ROOT}/{endpoint}"
        url = f"{base}?{urlencode({**params, 'apikey': api_key})}"
        last_error: Exception | None = None
        uncertain = 0
        for attempt in range(1, NETWORK_ATTEMPTS + 1):
            self.rate_limiter.wait()
            try:
                status, payload = self.http_get(url)
            # HTTPException (IncompleteRead, BadStatusLine, ...) is not an OSError: a response
            # lost mid-transfer has an unknown billing outcome like any network error.
            except (URLError, TimeoutError, ValueError, OSError, http.client.HTTPException) as exc:
                last_error = exc
                uncertain += 1
            else:
                if status < 500:
                    return status, payload, uncertain
                last_error = TwelveDataError(f"HTTP {status}", status)
                uncertain += 1
            if attempt < NETWORK_ATTEMPTS:
                self.sleep(2.0 * attempt)
        raise TwelveDataError(f"{endpoint} request failed after {NETWORK_ATTEMPTS} attempts: {last_error}", uncertain_attempts=uncertain)

    def _charge(self, slot: KeySlot, cost: int) -> None:
        if cost:
            slot.budget.spend(cost)
            self.cap.spend(cost)

    def request(self, endpoint: str, params: dict[str, Any], slot: KeySlot | None = None) -> Any:
        """Call an endpoint and return its JSON payload.

        Rotates to the next key when the provider reports the daily limit, pauses on
        per-minute limits, and raises BudgetExhaustedError when nothing can pay the call.
        Failed calls are charged conservatively, including attempts with an unknown outcome
        (network errors, 5xx); per-minute rejections and 403s are not billed by the provider.
        A fixed ``slot`` is used only while that key still has daily budget.
        """
        cost = CREDIT_COSTS.get(endpoint, 1)
        rate_limit_retries = 0
        while True:
            if not self.cap.allows(cost):
                raise BudgetExhaustedError("run_credit_cap")
            if slot is not None:
                if not slot.api_key or not slot.budget.can_spend(cost).allowed:
                    raise BudgetExhaustedError("key_budget_exhausted")
                current = slot
            else:
                current = self.key_pool.current_slot(cost)
                if current is None:
                    raise BudgetExhaustedError("all_keys_exhausted")
            try:
                status, payload, uncertain = self._get(endpoint, params, current.api_key)
            except TwelveDataError as exc:
                self._charge(current, cost * exc.uncertain_attempts)
                raise
            self._charge(current, cost * uncertain)
            message = _error_message(payload) if status < 400 else (_error_message(payload) or f"HTTP {status}")
            if message is None:
                self._charge(current, cost)
                return payload

            kind = classify_error(status, message)
            if kind is ApiCreditsExhaustedError:
                current.budget.force_exhaust()
                self.logger.warning("Twelve Data key%s reports its daily credits are used up: %s", current.index, message)
                if slot is not None:
                    raise kind(message, status)
                continue
            if kind is TemporaryRateLimitError and rate_limit_retries < RATE_LIMIT_RETRIES:
                rate_limit_retries += 1
                self.logger.warning("Twelve Data per-minute limit (%s); sleeping %ss", message, RATE_LIMIT_SLEEP_SECONDS)
                self.sleep(RATE_LIMIT_SLEEP_SECONDS)
                continue
            if status != 403 and kind is not TemporaryRateLimitError:
                self._charge(current, cost)
            raise kind(message, status)

    def time_series(self, symbol: str, adjust: str) -> dict:
        """Daily bars for one symbol with the given adjust mode (splits, all, none)."""
        return self.request(
            "time_series",
            {"symbol": symbol, "interval": self.config.interval, "outputsize": self.config.outputsize, "adjust": adjust},
        )

    def sync_usage(self) -> list[dict]:
        """Adopt each key's server-side daily usage (/api_usage) when it exceeds the local count.

        A key whose usage cannot be read after USAGE_SYNC_ATTEMPTS tries is excluded for the
        rest of the run: unseen external usage could otherwise eat into the daily reserve.
        A key with no local budget left is not queried (it would not be used anyway), and no
        key is queried once the run credit cap cannot pay for /api_usage: the cap only grows
        its usage, so it blocks every paid call for the rest of the run.
        """
        cost = CREDIT_COSTS["api_usage"]
        results = []
        for slot in self.key_pool.active_slots():
            if not slot.budget.can_spend(cost).allowed:
                results.append({"key_index": slot.index, "skipped": "no_local_budget"})
                continue
            if not self.cap.allows(cost):
                results.append({"key_index": slot.index, "skipped": "run_credit_cap"})
                continue
            error: Exception | None = None
            for attempt in range(1, USAGE_SYNC_ATTEMPTS + 1):
                try:
                    payload = self.request("api_usage", {}, slot=slot)
                    server_used = int(payload["daily_usage"])
                except BudgetExhaustedError as exc:
                    # A retry blocked by a budget the failed attempt used up keeps that failure as the reason.
                    error = error or exc
                    break
                except (TwelveDataError, KeyError, ValueError, TypeError) as exc:
                    error = exc
                    if attempt < USAGE_SYNC_ATTEMPTS:
                        self.sleep(5.0)
                    continue
                error = None
                slot.budget.sync_server_usage(server_used)
                results.append({"key_index": slot.index, "server_daily_usage": server_used, "local_used": slot.budget.calls_used})
                break
            if error is not None:
                slot.disabled_reason = f"usage_sync_failed: {error}"
                self.logger.warning("Excluding Twelve Data key%s for this run: usage could not be verified (%s)", slot.index, error)
                results.append({"key_index": slot.index, "error": str(error), "excluded": True})
        return results
