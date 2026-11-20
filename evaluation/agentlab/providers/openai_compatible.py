from __future__ import annotations

import os
import copy
import re
import time
from typing import Any

import httpx

from agentlab.config import ProviderProfile
from agentlab.security import redact
from .base import ProviderResult

HTTP_MAX_RETRIES = 3
HTTP_RETRY_DELAY_SECONDS = 20.0


class ProviderRequestCancelled(RuntimeError):
    """Stopped before another model request; no environment action exists."""


def normalize_usage(usage, schema="openai"):
    """Keep cache-inclusive context accounting distinct from gateway wire usage.

    GPTGE's Anthropic adapter renames the uncached input count to prompt_tokens
    while retaining separate cache read/write counts. This explicit profile
    setting must never be inferred from cache size for ordinary OpenAI usage.
    """
    result=copy.deepcopy(usage or {})
    if schema=="openai" or not result:return result
    if schema!="anthropic_cache_additive":raise ValueError("unsupported provider usage schema")
    if 'prompt_tokens' not in result:return result
    details=result.get('prompt_tokens_details') or {}
    uncached=result['prompt_tokens'];cached=details.get('cached_tokens',0) or 0
    written=details.get('cache_write_tokens',0) or 0
    if any(type(v) is not int or v<0 for v in (uncached,cached,written)):
        raise ValueError("invalid provider cache usage counts")
    result['prompt_tokens']=uncached+cached+written
    result['prompt_tokens_details']=dict(details,uncached_tokens=uncached)
    if 'completion_tokens' in result:
        result['total_tokens']=result['prompt_tokens']+result['completion_tokens']
    return result


def retry_policy(profile):
    return {'version': 'provider_http_retry_v2', 'max_retries': min(HTTP_MAX_RETRIES, max(0, profile.max_retries)),
            'delay_seconds': HTTP_RETRY_DELAY_SECONDS, 'retry_http_statuses': [400, 408, 429, '5xx'],
            'retry_transport_errors': True, 'scope': 'same_model_request_before_environment_action'}


class OpenAICompatibleProvider:
    supports_retry_control = True
    def __init__(self, profile: ProviderProfile):
        if not profile.base_url or not profile.secret_env:
            raise ValueError("openai_chat profile requires base_url and secret_env")
        self.profile = profile

    def _key(self) -> str:
        key = os.getenv(self.profile.secret_env or "")
        if not key:
            raise RuntimeError(f"missing environment variable: {self.profile.secret_env}")
        return key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key()}", "Content-Type": "application/json"}

    def _error_details(self, response: httpx.Response) -> dict[str, Any]:
        """Keep bounded provider diagnostics, never the request or credentials."""
        def clean(value: Any) -> str:
            text = str(value)
            key = os.getenv(self.profile.secret_env or "")
            if key:
                text = text.replace(key, "[REDACTED_API_KEY]")
            text = re.sub(r"data:[^\s,]+,[A-Za-z0-9+/=_-]+", "[image omitted]", text)
            text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
            return " ".join(redact(text).split())[:1200]

        details: dict[str, Any] = {"http_status": response.status_code}
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error", body) if isinstance(body, dict) else None
        if isinstance(error, dict):
            for field in ("message", "type", "code", "param"):
                value = error.get(field)
                if isinstance(value, (str, int, float, bool)):
                    details[field] = clean(value)
        elif isinstance(error, str):
            details["message"] = clean(error)
        # Non-JSON errors can contain a proxy's HTML page; keep only a short
        # excerpt instead of serializing its entire response or headers.
        if not details.get("message") and body is None:
            details["message"] = clean(response.text)
        for name in ("x-request-id", "x-oneapi-request-id", "request-id", "cf-ray"):
            if response.headers.get(name):
                details.setdefault("request_ids", {})[name] = clean(response.headers[name])[:200]
        return details

    def output_budget(self, multiplier: int = 1) -> dict[str, int]:
        if multiplier not in (1, 2, 4, 16):
            raise ValueError("output budget multiplier must be 1, 2, 4, or 16")
        parameters = {"max_tokens": 1200, **self.profile.parameters}
        for name in self.profile.omit_parameters:
            parameters.pop(name, None)
        name = ("max_completion_tokens" if "max_completion_tokens" in parameters
                else "max_tokens")
        limit = parameters.get(name)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return {}
        return {name: limit * multiplier}

    def complete(self, messages: list[dict[str, Any]], *,
                 output_token_multiplier: int = 1, cancel_event=None, on_retry=None) -> ProviderResult:
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "max_tokens": 1200,
            **self.profile.parameters,
        }
        for name in self.profile.omit_parameters:
            payload.pop(name, None)
        payload.update(self.output_budget(output_token_multiplier))
        started = time.monotonic()
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []
        policy = retry_policy(self.profile)
        def cancelled():
            if cancel_event is not None and cancel_event.is_set():
                failure = ProviderRequestCancelled('model request cancelled before another HTTP attempt')
                failure.transport_audit = {'attempts': attempts, 'retry_count': max(0,len(attempts)-1), 'cancelled': True}
                raise failure
        for attempt in range(policy['max_retries'] + 1):
            cancelled()
            attempt_started = time.monotonic()
            row: dict[str, Any] = {"attempt": attempt + 1}
            attempts.append(row)
            try:
                with httpx.Client(timeout=self.profile.timeout_seconds, trust_env=False) as client:
                    response = client.post(
                        f"{self.profile.base_url.rstrip('/')}/chat/completions",
                        headers=self._headers(), json=payload,
                    )
                    row["http_status"] = response.status_code
                    response.raise_for_status()
                    raw = response.json()
                content = raw["choices"][0]["message"]["content"]
                row.update(elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
                           outcome="response", backoff_ms=0)
                # Private audit only; never added to model messages or usage.
                raw["_transport_audit"] = {"attempts": attempts,
                    "retry_count": len(attempts) - 1,
                    "request_elapsed_ms": round(sum(a["elapsed_ms"] for a in attempts), 1),
                    "backoff_ms": sum(a.get("backoff_ms", 0) for a in attempts)}
                return ProviderResult(
                    content=content,
                    raw=raw,
                    usage=normalize_usage(raw.get("usage", {}),self.profile.usage_schema),
                    latency_ms=(time.monotonic() - started) * 1000,
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                row.update(elapsed_ms=round((time.monotonic() - attempt_started) * 1000, 1),
                           outcome=type(error).__name__, backoff_ms=0)
                last_error = error
                retryable = isinstance(error, (httpx.TransportError, httpx.TimeoutException))
                if isinstance(error, httpx.HTTPStatusError):
                    row["provider_error"] = self._error_details(error.response)
                    retryable = error.response.status_code in (400, 408, 429) or 500 <= error.response.status_code <= 599
                if not retryable or attempt == policy['max_retries']:
                    break
                pause = policy['delay_seconds']
                row['retry_delay_seconds'] = pause
                if on_retry is not None:
                    on_retry({'retry':attempt+1, 'max_retries':policy['max_retries'],
                        'delay_seconds':pause, 'retry_at_unix':time.time()+pause,
                        'http_status':row.get('http_status'), 'error_type':type(error).__name__,
                        'provider_error':row.get('provider_error'),
                        'attempts': [dict(a) for a in attempts]})
                wait_started = time.monotonic()
                if cancel_event is None:
                    time.sleep(pause)
                    row['backoff_ms'] = pause*1000
                else:
                    interrupted = cancel_event.wait(pause)
                    row['backoff_ms'] = round((time.monotonic()-wait_started)*1000,1) if interrupted else pause*1000
                    cancelled()
        cancelled()
        details = attempts[-1].get("provider_error", {})
        if details:
            message = f"provider request failed: HTTP {details['http_status']}"
            for field in ("type", "code", "param", "message"):
                if details.get(field):
                    message += f"; {field}={details[field]}"
            if details.get("request_ids"):
                message += "; " + ", ".join(f"{k}={v}" for k, v in details["request_ids"].items())
        else:
            message = f"provider request failed: {last_error}"
        failure = RuntimeError(message)
        failure.transport_audit = {"attempts": attempts, "retry_count": len(attempts) - 1,
            'request_elapsed_ms':round(sum(a['elapsed_ms'] for a in attempts),1),
            'backoff_ms':sum(a.get('backoff_ms',0) for a in attempts), 'policy':policy}
        raise failure from last_error

    def test_connection(self) -> dict[str, Any]:
        started = time.monotonic()
        with httpx.Client(timeout=min(self.profile.timeout_seconds, 30), trust_env=False) as client:
            response = client.get(f"{self.profile.base_url.rstrip('/')}/models", headers=self._headers())
            response.raise_for_status()
            raw = response.json()
        model_ids = [item.get("id") for item in raw.get("data", []) if isinstance(item, dict)]
        return {
            "ok": self.profile.model in model_ids,
            "model": self.profile.model,
            "model_available": self.profile.model in model_ids,
            "available_model_count": len(model_ids),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
