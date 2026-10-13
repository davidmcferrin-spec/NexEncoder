"""Read-only client for a local Prometheus server."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

QUERY_PATHS = {
    "query": "/api/v1/query",
    "query_range": "/api/v1/query_range",
    "names": "/api/v1/label/__name__/values",
}
_ALLOWED_PARAMS = frozenset({"query", "time", "start", "end", "step", "timeout"})


class PrometheusError(RuntimeError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _check_params(params: dict[str, str]) -> None:
    unknown = set(params) - _ALLOWED_PARAMS
    if unknown:
        raise PrometheusError("unsupported query parameter")
    query = params.get("query", "")
    if len(query) > 2000:
        raise PrometheusError("query is too long")
    step = params.get("step", "")
    if step and not all(ch.isdigit() or ch in "smhd." for ch in step):
        raise PrometheusError("step is not a duration")


def fetch(base_url: str, kind: str, params: dict[str, str], timeout: float = 2.0) -> tuple[int, dict]:
    """Return the Prometheus HTTP status and JSON body.

    Connection failures raise PrometheusError. An HTTP error from Prometheus
    is returned so the console can show the server's message.
    """
    path = QUERY_PATHS.get(kind)
    if path is None:
        raise PrometheusError("unsupported Prometheus request")
    _check_params(params)
    url = base_url.rstrip("/") + path
    if params:
        url = url + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    except URLError as exc:
        raise PrometheusError(f"Prometheus is not reachable at {base_url}") from exc
    except TimeoutError as exc:
        raise PrometheusError(f"Prometheus timed out at {base_url}") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrometheusError("Prometheus returned a response that is not JSON") from exc
    if not isinstance(payload, dict):
        raise PrometheusError("Prometheus returned an unexpected payload")
    return status, payload
