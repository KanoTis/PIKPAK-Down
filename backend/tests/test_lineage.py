from __future__ import annotations

from app.services.windows import analyze_windows


def test_window_union_baseline() -> None:
    size = 462_049_171
    result = analyze_windows(
        size,
        [
            (0, 285_584_324),
            (444_472_335, 462_049_170),
            (0, 410_337_600),
        ],
    )
    assert result["merged"] == [(0, 410_337_600), (444_472_335, 462_049_170)]
    assert result["missing"] == [(410_337_601, 444_472_334)]
    assert result["covered_bytes"] == 427_914_437
    assert result["union_ratio"] == 427_914_437 / 462_049_171
    assert result["conclusion"] == "anonymous_incomplete"


def test_window_full_coverage() -> None:
    result = analyze_windows(100, [(0, 49), (50, 99)])
    assert result["missing"] == []
    assert result["conclusion"] == "anonymous_full"
