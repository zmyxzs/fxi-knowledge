"""Cost readers observe the real attempt identities without duplicating writes."""

from fxi_novel.models.call_budget import (
    begin_provider_attempt,
    observe_provider_attempts,
    provider_call_budget,
)


def test_observation_keeps_one_persistence_owner_and_counts_fallbacks() -> None:
    persisted = []
    with provider_call_budget(
        None, scope_type="service_operation", scope_id="method-review",
        receipt_sink=persisted.append,
    ):
        with observe_provider_attempts() as observed:
            with provider_call_budget(2) as allowance:
                failed = begin_provider_attempt(
                    operation="chat_turn", route_id="primary", configured_model="primary",
                )
                failed.fail(RuntimeError("provider failed"))
                succeeded = begin_provider_attempt(
                    operation="chat_turn", route_id="fallback", configured_model="fallback",
                    fallback_index=1, fallback_reason="provider failed",
                )
                succeeded.complete(provider_model="fallback", usage={"total_tokens": 17})
        later = begin_provider_attempt(
            operation="chat_turn", route_id="writer", configured_model="writer",
        )
        later.complete(provider_model="writer", usage={"total_tokens": 5})
    assert allowance.used == 2
    assert len(persisted) == 3
    assert len(observed) == 2
    assert [row["attempt_id"] for row in observed] == [row["attempt_id"] for row in persisted[:2]]
    assert observed[-1]["usage"]["total_tokens"] == 17
    assert len({row["attempt_id"] for row in persisted}) == 3
