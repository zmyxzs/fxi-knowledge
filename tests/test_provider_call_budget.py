"""Read-only validation can forbid provider attempts in the shared budget."""

import pytest

from fxi_novel.models.call_budget import ProviderCallBudgetExceeded, provider_call_budget, provider_call_stage


def test_zero_allowance_rejects_first_attempt_without_recording_a_call():
    with provider_call_budget(0) as budget:
        with pytest.raises(ProviderCallBudgetExceeded):
            budget.consume()
        assert budget.used == 0
        with provider_call_budget(2) as child:
            with pytest.raises(ProviderCallBudgetExceeded):
                child.consume()
            assert child.used == budget.used == 0


@pytest.mark.parametrize("maximum", [-1, True, False, 1.5, "0"])
def test_invalid_allowances_are_still_rejected(maximum):
    with pytest.raises(ValueError, match="non-negative integer"):
        with provider_call_budget(maximum):
            pytest.fail("invalid budget was accepted")


def test_positive_allowance_still_records_each_attempt_and_stops_at_limit():
    with provider_call_budget(2) as budget:
        budget.consume()
        budget.consume()
        with pytest.raises(ProviderCallBudgetExceeded):
            budget.consume()
        assert budget.used == 2


def test_recovery_allowance_cannot_regenerate_plot_or_escape_through_child_budget():
    with provider_call_budget(1, allowed_stages=frozenset({"chapter_planning_scene_flags"})) as parent:
        with provider_call_stage("chapter_planning"), provider_call_budget(5) as child:
            with pytest.raises(ProviderCallBudgetExceeded, match="outside the recovery"):
                child.consume()
            assert child.used == parent.used == 0
        with provider_call_stage("chapter_planning_scene_flags"):
            parent.consume()
            with pytest.raises(ProviderCallBudgetExceeded, match="exhausted"):
                parent.consume()
        assert parent.used == 1


@pytest.mark.parametrize("stages", [["chapter_planning"], frozenset({""}), frozenset({None})])
def test_invalid_recovery_stage_allowance(stages):
    with pytest.raises(ValueError, match="frozen set"):
        with provider_call_budget(1, allowed_stages=stages):
            pytest.fail("invalid stage allowance accepted")
