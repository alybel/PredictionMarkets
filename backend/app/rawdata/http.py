"""Network transport of the raw data layer: modules outside ``backend.app.rawdata`` reach external APIs only through
here, never through an HTTP library of their own (guarded by ``backend/tests/test_architecture_layers.py``)."""

import logging

import requests

logger = logging.getLogger(__name__)

RequestException = requests.RequestException
HTTPError = requests.HTTPError


def get(url: str, params: dict | None = None, **kwargs) -> requests.Response:
    """One plain GET (callers handle status codes, retries and errors)."""
    logger.debug("http get %s", url)
    return requests.get(url, params=params, **kwargs)


def new_session() -> requests.Session:
    """A fresh HTTP session for callers that page through one host."""
    return requests.Session()
