from __future__ import annotations

from datetime import datetime

from usage_dashboard.shared.models import (
    ModelUsage,
    Provider,
    Reading,
    ReadingStatus,
    make_offline_reading,
    make_stale_reading,
)


def _make_reading(**overrides: object) -> Reading:
    defaults = {
        "provider": Provider.CLAUDE,
        "status": ReadingStatus.CURRENT,
        "session_percent": 50.0,
        "session_resets_at": datetime(2026, 1, 15, 10, 0, 0),
        "weekly_percent": 60.0,
        "weekly_resets_at": datetime(2026, 1, 19, 0, 0, 0),
        "fetched_at": datetime(2026, 1, 14, 12, 0, 0),
        "stale": False,
    }
    defaults.update(overrides)
    return Reading(**defaults)  # type: ignore[arg-type]


def test_reading_creation():
    reading = _make_reading()
    assert reading.provider is Provider.CLAUDE
    assert reading.status is ReadingStatus.CURRENT
    assert reading.session_percent == 50.0
    assert reading.weekly_percent == 60.0
    assert reading.stale is False


def test_reading_to_dict_round_trip():
    original = _make_reading()
    data = original.to_dict()
    restored = Reading.from_dict(data)
    assert restored == original


def test_reading_to_dict_formats_datetime():
    reading = _make_reading()
    data = reading.to_dict()
    assert data["session_resets_at"] == "2026-01-15T10:00:00Z"
    assert data["fetched_at"] == "2026-01-14T12:00:00Z"


def test_reading_from_dict_parses_datetime():
    reading = _make_reading()
    data = reading.to_dict()
    restored = Reading.from_dict(data)
    assert restored.session_resets_at == datetime(2026, 1, 15, 10, 0, 0)
    assert restored.weekly_resets_at == datetime(2026, 1, 19, 0, 0, 0)
    assert restored.fetched_at == datetime(2026, 1, 14, 12, 0, 0)


def test_reading_to_dict_none_fields():
    reading = _make_reading(session_percent=None, session_resets_at=None)
    data = reading.to_dict()
    assert data["session_percent"] is None
    assert data["session_resets_at"] is None


def test_reading_from_dict_none_fields():
    reading = _make_reading(session_percent=None, session_resets_at=None)
    data = reading.to_dict()
    restored = Reading.from_dict(data)
    assert restored.session_percent is None
    assert restored.session_resets_at is None


def test_make_offline_reading_produces_none_fields():
    fetched = datetime(2026, 1, 14, 12, 0, 0)
    reading = make_offline_reading(Provider.ZAI, fetched)
    assert reading.provider is Provider.ZAI
    assert reading.status is ReadingStatus.OFFLINE
    assert reading.session_percent is None
    assert reading.session_resets_at is None
    assert reading.weekly_percent is None
    assert reading.weekly_resets_at is None
    assert reading.fetched_at == fetched
    assert reading.stale is True


def test_make_stale_reading_sets_stale_and_status():
    original = _make_reading(stale=False, status=ReadingStatus.CURRENT)
    stale = make_stale_reading(original)
    assert stale.stale is True
    assert stale.status is ReadingStatus.STALE
    assert stale.provider == original.provider
    assert stale.session_percent == original.session_percent
    assert stale.weekly_percent == original.weekly_percent


def test_make_stale_reading_preserves_other_fields():
    original = _make_reading()
    stale = make_stale_reading(original)
    assert stale.session_percent == original.session_percent
    assert stale.session_resets_at == original.session_resets_at
    assert stale.weekly_percent == original.weekly_percent
    assert stale.weekly_resets_at == original.weekly_resets_at
    assert stale.fetched_at == original.fetched_at


def test_provider_enum_values():
    assert Provider.CLAUDE.value == "claude"
    assert Provider.ZAI.value == "zai"
    assert Provider.OLLAMA.value == "ollama"


def test_provider_enum_from_value():
    assert Provider("claude") is Provider.CLAUDE
    assert Provider("zai") is Provider.ZAI
    assert Provider("ollama") is Provider.OLLAMA


def test_reading_status_enum_values():
    assert ReadingStatus.CURRENT.value == "current"
    assert ReadingStatus.STALE.value == "stale"
    assert ReadingStatus.OFFLINE.value == "offline"


def test_reading_status_from_value():
    assert ReadingStatus("current") is ReadingStatus.CURRENT
    assert ReadingStatus("stale") is ReadingStatus.STALE
    assert ReadingStatus("offline") is ReadingStatus.OFFLINE


def test_reading_frozen():
    reading = _make_reading()
    try:
        reading.provider = Provider.ZAI  # type: ignore[misc]
        assert False, "Should raise FrozenInstanceError"
    except AttributeError:
        pass


def test_reading_round_trip_with_all_providers():
    for provider in Provider:
        reading = _make_reading(provider=provider)
        data = reading.to_dict()
        restored = Reading.from_dict(data)
        assert restored == reading


def test_reading_detail_defaults_to_none():
    reading = _make_reading()
    assert reading.detail is None


def test_reading_detail_round_trip():
    reading = _make_reading(provider=Provider.UMANS, detail="pk 2/4  req 161  tok 63.9M")
    restored = Reading.from_dict(reading.to_dict())
    assert restored.detail == "pk 2/4  req 161  tok 63.9M"
    assert restored == reading


def test_reading_from_dict_tolerates_missing_detail_key():
    data = _make_reading().to_dict()
    del data["detail"]
    reading = Reading.from_dict(data)
    assert reading.detail is None


def test_model_usage_round_trip():
    mu = ModelUsage(name="minimax-m3", requests=1841, share_percent=68.0)
    data = mu.to_dict()
    restored = ModelUsage.from_dict(data)
    assert restored == mu


def test_reading_models_default_none():
    reading = _make_reading()
    assert reading.models is None


def test_reading_models_round_trip():
    reading = _make_reading(
        provider=Provider.OLLAMA,
        models=[
            ModelUsage(name="minimax-m3", requests=1841, share_percent=68.0),
            ModelUsage(name="nemotron-3-ultra", requests=588, share_percent=27.5),
        ],
    )
    restored = Reading.from_dict(reading.to_dict())
    assert restored.models is not None
    assert len(restored.models) == 2
    assert restored.models[0].name == "minimax-m3"
    assert restored.models[0].requests == 1841
    assert restored.models[0].share_percent == 68.0
    assert restored == reading


def test_reading_from_dict_tolerates_missing_models_key():
    data = _make_reading().to_dict()
    del data["models"]
    reading = Reading.from_dict(data)
    assert reading.models is None


def test_reading_from_dict_tolerates_null_models():
    data = _make_reading().to_dict()
    data["models"] = None
    reading = Reading.from_dict(data)
    assert reading.models is None
