class FetchError(Exception):
    pass


class FetchAuthError(FetchError):
    """Fetch failed because the credential was rejected (401/403).

    Distinct from FetchError so the scheduler only attempts token refresh
    when the credential is actually the problem — refreshing on transient
    failures (429s, timeouts) hammers the OAuth endpoint for nothing.
    """


class FetchRateLimitError(FetchError):
    """Fetch was rate limited (429); the scheduler should back off.

    retry_after_seconds comes from the Retry-After header when present so
    the scheduler can skip the provider until the window passes instead of
    re-polling into the same limit every cycle.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
