"""A failed instrument must be runnable again from its own row.

Flight_CCT0809 restored with MicaSense showing Failed. The row offered no
button: Reprocess appears only for a job that previously *completed*, and the
backend refused a reprocess of anything else with "has no completed result to
reprocess". The instrument was selectable and Start Processing would have run
it, but nothing on the row said so, and a restored project opens on exactly
that state - which is when an operator most wants to press something.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.enums import ProcessingStatus

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "app" / "assets" / "dashboard.js").read_text(encoding="utf-8")
BACKEND = (ROOT / "app" / "scan_backend.py").read_text(encoding="utf-8")


class TestTheRowOffersSomething:
    def test_a_failed_job_gets_a_retry_button(self):
        block = SCRIPT[SCRIPT.index("const actions = [];"):]
        block = block[: block.index("const selectable")]

        assert "job.status === 'failed'" in block
        assert ">Retry</button>" in block

    def test_it_goes_through_the_same_confirmation(self):
        block = SCRIPT[SCRIPT.index("const actions = [];"):]
        block = block[: block.index("const selectable")]
        # One action, so the queue only has one way in.
        assert block.count('data-queue-action="reprocess"') == 2

    def test_a_completed_job_still_says_reprocess(self):
        """The two are different questions and must not be worded alike."""
        block = SCRIPT[SCRIPT.index("const actions = [];"):]
        block = block[: block.index("const selectable")]

        assert ">Reprocess</button>" in block


class TestTheDialogTellsTheTruth:
    def test_a_failure_is_not_described_as_a_result_worth_keeping(self):
        block = SCRIPT[SCRIPT.index("function confirmReprocess("):]
        block = block[: block.index("showModal();")]

        assert "no result" in block
        assert "Keep previous result" in block, "the completed wording survives"
        assert "Leave it" in block, "the failed wording is its own"

    def test_the_row_state_decides_the_wording(self):
        block = SCRIPT[SCRIPT.index("if (button.dataset.queueAction === 'reprocess')"):]
        assert "classList.contains('is-failed')" in block[:400]


class TestTheBackendAcceptsIt:
    def test_a_failed_status_is_allowed(self):
        start = BACKEND.index('elif action == "reprocess":')
        block = BACKEND[start: start + 2200]

        assert "ProcessingStatus.FAILED," in block

    def test_confirmation_is_still_required(self):
        """Requeueing is cheap to ask for and expensive to run."""
        start = BACKEND.index('elif action == "reprocess":')
        block = BACKEND[start: start + 600]

        assert 'request.get("confirmed") is not True' in block

    def test_the_step_says_which_it_was(self):
        start = BACKEND.index('elif action == "reprocess":')
        block = BACKEND[start: start + 2600]

        assert "Queued to run again after a failed run" in block
        assert "Queued for explicitly confirmed reprocessing" in block

    def test_source_data_is_still_checked(self):
        """A retry of an instrument whose data has gone is not a retry."""
        start = BACKEND.index('elif action == "reprocess":')
        block = BACKEND[start: start + 2600]

        assert "_instrument_is_processable(state)" in block


class TestAFailedJobWasAlreadySelectable:
    """Which is why the fix is an affordance, not a permission."""

    def test_previously_completed_excludes_failure(self):
        start = BACKEND.index("previously_completed = job.status in {")
        block = BACKEND[start: start + 260]

        assert "ProcessingStatus.COMPLETE," in block
        assert "ProcessingStatus.WARNING," in block
        assert "FAILED" not in block

    def test_so_it_is_available_for_selection(self):
        # Anchored after previously_completed, because an earlier "ready = ("
        # belongs to SIF and would pass this without meaning anything.
        anchor = BACKEND.index("previously_completed = job.status in {")
        start = BACKEND.index("ready = (", anchor)
        block = BACKEND[start: start + 320]

        assert "not previously_completed" in block
        assert "available_for_selection" in BACKEND[start: start + 700]
