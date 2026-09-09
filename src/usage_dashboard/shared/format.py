"""Display formatting shared by the Pi client and the web dashboard.

The two surfaces are separate renderers over the same model, so anything that
decides *what text a user reads* belongs here rather than in either one. This
module exists because ``client/format.format_duration`` and
``server/api._countdown_short`` had drifted into byte-identical copies of each
other — harmless while they agreed, but it is the same duplication that let the
z.ai peak countdown ship on the panel and never reach the web view (WI-030,
and WI-020 before it).
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from usage_dashboard.shared.models import Provider, Reading, ReadingStatus

# The detail shapes fetch_umans_wallet can produce. Anything else is not
# wallet data: the production DB still holds pre-PR#22 ``umans`` rows whose
# detail is the retired trailing-usage line ("24h req 1234 tok 5.6M"), and
# re-adding the enum member made those parse again. Presenting one of those
# as a wallet balance would be worse than showing nothing — especially during
# a wallet-fetch outage, where the stale path preserves the old detail.
_WALLET_DETAIL_RE = re.compile(
    r"^(?:\$\d[\d,]*\.\d{2}(?:, promo: \$\d[\d,]*\.\d{2})?|no wallet)$"
)


def format_duration(total_seconds: float) -> str:
    """Compact duration label: 45 -> '1m', 12240 -> '3h 24m', 176400 -> '2d 1h'.

    Sub-minute durations round up to '1m' rather than showing '0m', so a
    countdown never reads as already-expired while the window is still open.
    """
    seconds = max(0, int(total_seconds))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def umans_wallet_line(readings: Iterable[Reading]) -> str | None:
    """The Umans wallet line (Plan 004): ``Umans: $15.94, promo: $7.14``.

    One rule for both surfaces (the touch panel's corner line and the web
    dashboard's header capsule), so they cannot drift the way WI-020/WI-030
    did. The money text is formatted server-side in the reading's ``detail``;
    this only prefixes the label, and only when the detail is actually
    wallet-shaped (see ``_WALLET_DETAIL_RE``). Nothing honest to show —
    provider absent, offline, no detail yet, or a non-wallet detail from a
    pre-wallet legacy row — means None, and each surface then renders nothing
    rather than a wrong figure presented as live.
    """
    reading = next((r for r in readings if r.provider is Provider.UMANS), None)
    if reading is None or reading.status is ReadingStatus.OFFLINE:
        return None
    if reading.detail is None or not _WALLET_DETAIL_RE.match(reading.detail):
        return None
    return f"Umans: {reading.detail}"
