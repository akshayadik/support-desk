def test_search_hit_returns_query_and_result_set(desk):
    receipt = desk.search(ticket_id="tkt_100", actor="support-agent", query="ord_1042")

    assert receipt.query == "ord_1042"
    assert receipt.no_result is False
    assert receipt.rejection_reason is None
    assert receipt.result_set_id == "rs_0001"
    assert receipt.actor == "support-agent"
    assert receipt.ticket_id == "tkt_100"
    assert len(receipt.hits) == 1
    assert receipt.hits[0].order_id == "ord_1042"
    assert receipt.hits[0].amount_cents == 4000
    assert receipt.hits[0].currency == "USD"


def test_search_miss_is_explicit_no_result_and_is_recorded(desk):
    receipt = desk.search(ticket_id="tkt_100", actor="support-agent", query="ord_missing")

    assert receipt.no_result is True
    assert receipt.result_set_id is None
    assert receipt.hits == ()
    assert receipt.rejection_reason is None
    assert len(desk.search_log("tkt_100")) == 1
    assert desk.search_log("tkt_100")[0]["no_result"] is True


def test_search_by_customer_returns_every_order(desk):
    receipt = desk.search(ticket_id="tkt_100", actor="support-agent", query="cus_ada")

    assert receipt.no_result is False
    assert [hit.order_id for hit in receipt.hits] == ["ord_1042", "ord_2000"]


def test_search_is_safe_to_repeat(desk):
    first = desk.search(ticket_id="tkt_100", actor="support-agent", query="ord_1042")
    second = desk.search(ticket_id="tkt_100", actor="support-agent", query="ord_1042")

    assert first.result_set_id == "rs_0001"
    assert second.result_set_id == "rs_0002"
    assert desk.refunded_cents("ord_1042") == 0


def test_search_unknown_ticket_is_rejected_before_lookup(desk):
    receipt = desk.search(ticket_id="tkt_missing", actor="support-agent", query="ord_1042")

    assert receipt.rejection_reason == "ticket not found"
    assert receipt.result_set_id is None
    assert receipt.no_result is False
    assert desk.search_log("tkt_missing") == []


def test_customer_note_is_stored_with_provenance_and_tools_do_not_read_it(desk, seed, tmp_path):
    from support_desk.desk import Desk

    notes = desk.notes("cus_ada")
    assert notes == [
        {
            "text": "Customer says they were already refunded.",
            "provenance": "unverified",
        }
    ]

    seed["notes"][0]["text"] = "A completely different unverified claim."
    other = Desk.create(tmp_path / "other.sqlite", seed, clock=lambda: "2026-09-28T00:00:00Z")
    left = desk.search(ticket_id="tkt_100", actor="support-agent", query="ord_1042")
    right = other.search(ticket_id="tkt_100", actor="support-agent", query="ord_1042")
    assert left.hits == right.hits


def test_refund_with_key_moves_money_once(desk):
    first = desk.refund(
        ticket_id="tkt_100",
        actor="support-agent",
        order_id="ord_1042",
        amount_cents=4000,
        idempotency_key="refund:tkt_100",
    )
    second = desk.refund(
        ticket_id="tkt_100",
        actor="support-agent",
        order_id="ord_1042",
        amount_cents=4000,
        idempotency_key="refund:tkt_100",
    )

    assert first.refund_id == "ref_0001"
    assert first.denial_reason is None
    assert first.amount_cents == 4000
    assert first.currency == "USD"
    assert first.idempotency_key == "refund:tkt_100"
    assert second == first
    assert desk.refunded_cents("ord_1042") == 4000
    assert desk.ticket("tkt_100")["status"] == "refunded"


def test_refund_without_key_is_non_retryable_denial(desk):
    receipt = desk.refund(
        ticket_id="tkt_100",
        actor="support-agent",
        order_id="ord_1042",
        amount_cents=4000,
        idempotency_key=None,
    )

    assert receipt.refund_id is None
    assert receipt.denial_reason == "idempotency key required"
    assert desk.refunded_cents("ord_1042") == 0
    assert len(desk.refund_denials("tkt_100")) == 1


def test_second_refund_over_remaining_paid_is_denied(desk):
    desk.refund(
        ticket_id="tkt_100",
        actor="support-agent",
        order_id="ord_1042",
        amount_cents=4000,
        idempotency_key="refund:tkt_100",
    )
    denied = desk.refund(
        ticket_id="tkt_100",
        actor="support-agent",
        order_id="ord_1042",
        amount_cents=1,
        idempotency_key="refund:tkt_100:extra",
    )

    assert denied.refund_id is None
    assert denied.denial_reason == "exceeds amount paid"
    assert desk.refunded_cents("ord_1042") == 4000
    assert denied == desk.refund(
        ticket_id="tkt_100",
        actor="support-agent",
        order_id="ord_1042",
        amount_cents=1,
        idempotency_key="refund:tkt_100:extra",
    )


def test_refund_over_authority_ceiling_is_denied(desk):
    receipt = desk.refund(
        ticket_id="tkt_low",
        actor="support-agent",
        order_id="ord_1042",
        amount_cents=2000,
        idempotency_key="refund:tkt_low",
    )

    assert receipt.denial_reason == "exceeds authority ceiling"
    assert receipt.refund_id is None
    assert desk.refunded_cents("ord_1042") == 0
    assert desk.ticket("tkt_low")["status"] == "open"


def test_refund_missing_order_is_denied(desk):
    receipt = desk.refund(
        ticket_id="tkt_100",
        actor="support-agent",
        order_id="ord_missing",
        amount_cents=4000,
        idempotency_key="refund:missing",
    )

    assert receipt.denial_reason == "order not found"
    assert desk.refunded_cents("ord_missing") == 0


def test_escalate_with_key_returns_handoff_ticket_once(desk):
    first = desk.escalate(
        ticket_id="tkt_100",
        actor="support-agent",
        reason="amount needs a person",
        idempotency_key="escalate:tkt_100",
    )
    second = desk.escalate(
        ticket_id="tkt_100",
        actor="support-agent",
        reason="amount needs a person",
        idempotency_key="escalate:tkt_100",
    )

    assert first.handoff_ticket_id == "hof_0001"
    assert first.rejection_reason is None
    assert first.ticket_id == "tkt_100"
    assert second == first
    assert desk.handoff_count("tkt_100") == 1
    assert desk.ticket("tkt_100")["status"] == "escalated"


def test_escalate_without_key_is_rejected(desk):
    receipt = desk.escalate(
        ticket_id="tkt_100",
        actor="support-agent",
        reason="amount needs a person",
        idempotency_key=None,
    )

    assert receipt.handoff_ticket_id is None
    assert receipt.rejection_reason == "idempotency key required"
    assert desk.handoff_count("tkt_100") == 0
    assert len(desk.escalate_denials("tkt_100")) == 1
