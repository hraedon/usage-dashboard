class FetchError(Exception):
    pass


class FetchAuthError(FetchError):
    """Fetch failed because the credential was rejected (401/403).

    Distinct from FetchError so the scheduler only attempts token refresh
    when the credential is actually the problem — refreshing on transient
    failures (429s, timeouts) hammers the OAuth endpoint for nothing.
    """
