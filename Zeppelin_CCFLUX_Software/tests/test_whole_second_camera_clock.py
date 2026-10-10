"""A camera clock that records whole seconds is not a damaged delivery.

MicaSense writes EXIF DateTimeOriginal, which carries whole seconds, and the
camera triggers faster than that. Two captures then share a stamp. On
Flight_CC0808 that is 1 752 of 9 985 captures, every repeat a plain pair - and
it was reported as "1752 duplicated timestamp(s) detected" over a Warning
status, which reads as damage to a delivery in which nothing is wrong and
nothing is missing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core import time_extraction as te


def _observe(instrument: str, per_second: int = 2, count: int = 100):
    accumulator = te._Accumulator(instrument_id=instrument)
    began = datetime(2026, 8, 8, 8, 0, 0)
    for index in range(count):
        stamp = began + timedelta(seconds=index // per_second)
        accumulator.observe(
            te._RawTimestamp(
                original=stamp.isoformat(), row_number=index,
                source_file=Path("IMG_0000.zip"),
            )
        )
    return accumulator.result()


class TestNothingIsLost:
    def test_every_capture_is_counted(self):
        """The duplicate is a flag on a record, not a record removed."""
        result = _observe("micasense")

        assert result.records_examined == 100
        assert result.duplicated_timestamps == 50

    def test_the_window_still_spans_the_flight(self):
        result = _observe("micasense")

        assert result.original_min_timestamp.endswith("08:00:00")
        assert result.original_max_timestamp.endswith("08:00:49")


class TestWhatTheOperatorIsTold:
    def test_a_camera_is_told_why(self):
        warnings = _observe("micasense").timestamp_quality_warnings
        message = next(w for w in warnings if "share a second" in w)

        assert "whole seconds" in message
        assert "every capture is present and processed" in message
        assert "duplicated timestamp" not in message

    def test_an_instrument_with_a_real_clock_is_not_excused(self):
        """A repeated stamp from a logger is a fault worth reporting, and the
        wording for it is unchanged."""
        warnings = _observe("miro").timestamp_quality_warnings

        assert any("duplicated timestamp(s) detected" in w for w in warnings)
        assert not any("share a second" in w for w in warnings)

    def test_the_cameras_are_named_once(self):
        assert "micasense" in te.WHOLE_SECOND_CLOCK_INSTRUMENTS
        assert "gopro" in te.WHOLE_SECOND_CLOCK_INSTRUMENTS
        assert "miro" not in te.WHOLE_SECOND_CLOCK_INSTRUMENTS


class TestItDoesNotRaiseTheCard:
    """It stays in the timestamp diagnostics, where it is a fact about the
    clock, but it must not put the instrument's card into Warning."""

    SOURCE = (
        Path(__file__).resolve().parents[1] / "app" / "scan_backend.py"
    ).read_text(encoding="utf-8")

    def test_the_card_filters_it_out(self):
        block = self.SOURCE[self.SOURCE.index("card_timestamp_warnings = timestamp_warnings"):]
        block = block[: block.index("warnings.extend(card_timestamp_warnings)")]

        assert "WHOLE_SECOND_CLOCK_INSTRUMENTS" in block
        assert "share a second with another capture" in block

    def test_the_set_is_imported_rather_than_restated(self):
        assert "WHOLE_SECOND_CLOCK_INSTRUMENTS," in self.SOURCE
