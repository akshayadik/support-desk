from dataclasses import dataclass


@dataclass(frozen=True)
class OrderHit:
    order_id: str
    amount_cents: int
    currency: str
    status: str
    customer_id: str


@dataclass(frozen=True)
class SearchReceipt:
    query: str
    result_set_id: str | None
    no_result: bool
    rejection_reason: str | None
    hits: tuple[OrderHit, ...]
    actor: str
    ticket_id: str


@dataclass(frozen=True)
class RefundReceipt:
    refund_id: str | None
    denial_reason: str | None
    idempotency_key: str | None
    amount_cents: int | None
    currency: str | None
    order_id: str
    ticket_id: str
    actor: str


@dataclass(frozen=True)
class EscalateReceipt:
    handoff_ticket_id: str | None
    rejection_reason: str | None
    idempotency_key: str | None
    ticket_id: str
    actor: str
    reason: str
