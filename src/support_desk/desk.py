import sqlite3
from pathlib import Path

from support_desk.receipts import EscalateReceipt, OrderHit, RefundReceipt, SearchReceipt

SCHEMA = """
CREATE TABLE customers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE payments (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL
);
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    text TEXT NOT NULL,
    provenance TEXT NOT NULL
);
CREATE TABLE tickets (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    order_id TEXT NOT NULL REFERENCES orders(id),
    requested_amount_cents INTEGER NOT NULL,
    authority_ceiling_cents INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE searches (
    id TEXT PRIMARY KEY,
    result_set_id TEXT UNIQUE,
    query TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    no_result INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE search_hits (
    result_set_id TEXT NOT NULL REFERENCES searches(result_set_id),
    order_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    customer_id TEXT NOT NULL
);
CREATE TABLE refunds (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    order_id TEXT NOT NULL REFERENCES orders(id),
    actor TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    currency TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE refund_denials (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    ticket_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    amount_cents INTEGER,
    currency TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE handoffs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE escalate_denials (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    ticket_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Desk:
    def __init__(self, path, clock):
        self.path = Path(path)
        self.clock = clock

    @classmethod
    def create(cls, path, seed, clock):
        path = Path(path)
        for suffix in ("", "-journal", "-wal", "-shm"):
            existing = Path(str(path) + suffix) if suffix else path
            if existing.exists():
                existing.unlink()
        desk = cls(path, clock)
        with desk._session() as conn:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT INTO customers (id, name) VALUES (:id, :name)",
                seed["customers"],
            )
            conn.executemany(
                """
                INSERT INTO orders (id, customer_id, amount_cents, currency, status)
                VALUES (:id, :customer_id, :amount_cents, :currency, :status)
                """,
                seed["orders"],
            )
            conn.executemany(
                """
                INSERT INTO payments (id, order_id, amount_cents, currency)
                VALUES (:id, :order_id, :amount_cents, :currency)
                """,
                seed["payments"],
            )
            conn.executemany(
                """
                INSERT INTO notes (customer_id, text, provenance)
                VALUES (:customer_id, :text, :provenance)
                """,
                seed["notes"],
            )
            conn.executemany(
                """
                INSERT INTO tickets (
                    id, customer_id, order_id, requested_amount_cents,
                    authority_ceiling_cents, status
                ) VALUES (
                    :id, :customer_id, :order_id, :requested_amount_cents,
                    :authority_ceiling_cents, :status
                )
                """,
                seed["tickets"],
            )
        return desk

    def search(self, *, ticket_id, actor, query):
        with self._session() as conn:
            ticket = self._ticket_row(conn, ticket_id)
            if ticket is None:
                return SearchReceipt(
                    query=query,
                    result_set_id=None,
                    no_result=False,
                    rejection_reason="ticket not found",
                    hits=(),
                    actor=actor,
                    ticket_id=ticket_id,
                )
            orders = self._matching_orders(conn, query)
            if not orders:
                self._insert_search(conn, query, ticket_id, actor, None, True)
                return SearchReceipt(
                    query=query,
                    result_set_id=None,
                    no_result=True,
                    rejection_reason=None,
                    hits=(),
                    actor=actor,
                    ticket_id=ticket_id,
                )
            hits = tuple(self._hit_for(conn, order) for order in orders)
            result_set_id = self._next_id(
                conn, "searches", "rs", "result_set_id IS NOT NULL"
            )
            self._insert_search(conn, query, ticket_id, actor, result_set_id, False)
            conn.executemany(
                """
                INSERT INTO search_hits (
                    result_set_id, order_id, amount_cents, currency, status, customer_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        result_set_id,
                        hit.order_id,
                        hit.amount_cents,
                        hit.currency,
                        hit.status,
                        hit.customer_id,
                    )
                    for hit in hits
                ],
            )
            return SearchReceipt(
                query=query,
                result_set_id=result_set_id,
                no_result=False,
                rejection_reason=None,
                hits=hits,
                actor=actor,
                ticket_id=ticket_id,
            )

    def refund(self, *, ticket_id, actor, order_id, amount_cents, idempotency_key):
        with self._session() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM refunds WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._refund_from_row(existing)
                denied = conn.execute(
                    "SELECT * FROM refund_denials WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if denied is not None:
                    return self._refund_denial_from_row(denied)

            if not idempotency_key:
                self._insert_refund_denial(
                    conn,
                    idempotency_key,
                    ticket_id,
                    order_id,
                    actor,
                    amount_cents,
                    None,
                    "idempotency key required",
                )
                return RefundReceipt(
                    refund_id=None,
                    denial_reason="idempotency key required",
                    idempotency_key=None,
                    amount_cents=amount_cents,
                    currency=None,
                    order_id=order_id,
                    ticket_id=ticket_id,
                    actor=actor,
                )

            order = conn.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            ticket = self._ticket_row(conn, ticket_id)
            if order is None:
                return self._deny_refund(
                    conn, idempotency_key, ticket_id, order_id, actor, amount_cents, None, "order not found"
                )
            if ticket is None:
                return self._deny_refund(
                    conn, idempotency_key, ticket_id, order_id, actor, amount_cents, order["currency"], "ticket not found"
                )
            if amount_cents > ticket["authority_ceiling_cents"]:
                return self._deny_refund(
                    conn,
                    idempotency_key,
                    ticket_id,
                    order_id,
                    actor,
                    amount_cents,
                    order["currency"],
                    "exceeds authority ceiling",
                )
            remaining = self._paid_cents(conn, order_id) - self._refunded_cents(conn, order_id)
            if amount_cents > remaining or amount_cents <= 0:
                reason = "exceeds amount paid" if amount_cents > remaining else "amount must be positive"
                return self._deny_refund(
                    conn, idempotency_key, ticket_id, order_id, actor, amount_cents, order["currency"], reason
                )

            refund_id = self._next_id(conn, "refunds", "ref")
            conn.execute(
                """
                INSERT INTO refunds (
                    id, idempotency_key, ticket_id, order_id, actor,
                    amount_cents, currency, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    refund_id,
                    idempotency_key,
                    ticket_id,
                    order_id,
                    actor,
                    amount_cents,
                    order["currency"],
                    self.clock(),
                ),
            )
            conn.execute(
                "UPDATE tickets SET status = 'refunded' WHERE id = ?",
                (ticket_id,),
            )
            return RefundReceipt(
                refund_id=refund_id,
                denial_reason=None,
                idempotency_key=idempotency_key,
                amount_cents=amount_cents,
                currency=order["currency"],
                order_id=order_id,
                ticket_id=ticket_id,
                actor=actor,
            )

    def escalate(self, *, ticket_id, actor, reason, idempotency_key):
        with self._session() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM handoffs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._handoff_from_row(existing)
                denied = conn.execute(
                    "SELECT * FROM escalate_denials WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if denied is not None:
                    return self._escalate_denial_from_row(denied)

            if not idempotency_key:
                self._insert_escalate_denial(
                    conn, None, ticket_id, actor, reason, "idempotency key required"
                )
                return EscalateReceipt(
                    handoff_ticket_id=None,
                    rejection_reason="idempotency key required",
                    idempotency_key=None,
                    ticket_id=ticket_id,
                    actor=actor,
                    reason=reason,
                )

            ticket = self._ticket_row(conn, ticket_id)
            if ticket is None:
                self._insert_escalate_denial(
                    conn, idempotency_key, ticket_id, actor, reason, "ticket not found"
                )
                return EscalateReceipt(
                    handoff_ticket_id=None,
                    rejection_reason="ticket not found",
                    idempotency_key=idempotency_key,
                    ticket_id=ticket_id,
                    actor=actor,
                    reason=reason,
                )

            handoff_id = self._next_id(conn, "handoffs", "hof")
            conn.execute(
                """
                INSERT INTO handoffs (id, idempotency_key, ticket_id, actor, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (handoff_id, idempotency_key, ticket_id, actor, reason, self.clock()),
            )
            conn.execute(
                "UPDATE tickets SET status = 'escalated' WHERE id = ?",
                (ticket_id,),
            )
            return EscalateReceipt(
                handoff_ticket_id=handoff_id,
                rejection_reason=None,
                idempotency_key=idempotency_key,
                ticket_id=ticket_id,
                actor=actor,
                reason=reason,
            )

    def notes(self, customer_id):
        with self._session() as conn:
            rows = conn.execute(
                "SELECT text, provenance FROM notes WHERE customer_id = ? ORDER BY id",
                (customer_id,),
            ).fetchall()
        return [{"text": row["text"], "provenance": row["provenance"]} for row in rows]

    def search_log(self, ticket_id):
        with self._session() as conn:
            rows = conn.execute(
                "SELECT query, no_result, result_set_id FROM searches WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        return [
            {
                "query": row["query"],
                "no_result": bool(row["no_result"]),
                "result_set_id": row["result_set_id"],
            }
            for row in rows
        ]

    def refunded_cents(self, order_id):
        with self._session() as conn:
            return self._refunded_cents(conn, order_id)

    def refund_denials(self, ticket_id):
        with self._session() as conn:
            rows = conn.execute(
                "SELECT reason, idempotency_key FROM refund_denials WHERE ticket_id = ? ORDER BY id",
                (ticket_id,),
            ).fetchall()
        return [{"reason": row["reason"], "idempotency_key": row["idempotency_key"]} for row in rows]

    def ticket(self, ticket_id):
        with self._session() as conn:
            row = self._ticket_row(conn, ticket_id)
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def handoff_count(self, ticket_id):
        with self._session() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM handoffs WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()["n"]

    def escalate_denials(self, ticket_id):
        with self._session() as conn:
            rows = conn.execute(
                """
                SELECT reason, rejection_reason, idempotency_key
                FROM escalate_denials WHERE ticket_id = ? ORDER BY id
                """,
                (ticket_id,),
            ).fetchall()
        return [
            {
                "reason": row["reason"],
                "rejection_reason": row["rejection_reason"],
                "idempotency_key": row["idempotency_key"],
            }
            for row in rows
        ]

    def _session(self):
        return _Session(self.path)

    def _ticket_row(self, conn, ticket_id):
        return conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()

    def _matching_orders(self, conn, query):
        by_id = conn.execute("SELECT * FROM orders WHERE id = ?", (query,)).fetchall()
        if by_id:
            return by_id
        return conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY id",
            (query,),
        ).fetchall()

    def _hit_for(self, conn, order):
        return OrderHit(
            order_id=order["id"],
            amount_cents=self._paid_cents(conn, order["id"]),
            currency=order["currency"],
            status=order["status"],
            customer_id=order["customer_id"],
        )

    def _insert_search(self, conn, query, ticket_id, actor, result_set_id, no_result):
        attempt_id = self._next_id(conn, "searches", "sat")
        conn.execute(
            """
            INSERT INTO searches (
                id, result_set_id, query, ticket_id, actor, no_result, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (attempt_id, result_set_id, query, ticket_id, actor, int(no_result), self.clock()),
        )

    def _deny_refund(self, conn, key, ticket_id, order_id, actor, amount_cents, currency, reason):
        self._insert_refund_denial(
            conn, key, ticket_id, order_id, actor, amount_cents, currency, reason
        )
        return RefundReceipt(
            refund_id=None,
            denial_reason=reason,
            idempotency_key=key,
            amount_cents=amount_cents,
            currency=currency,
            order_id=order_id,
            ticket_id=ticket_id,
            actor=actor,
        )

    def _insert_refund_denial(self, conn, key, ticket_id, order_id, actor, amount_cents, currency, reason):
        denial_id = self._next_id(conn, "refund_denials", "den")
        conn.execute(
            """
            INSERT INTO refund_denials (
                id, idempotency_key, ticket_id, order_id, actor,
                amount_cents, currency, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (denial_id, key, ticket_id, order_id, actor, amount_cents, currency, reason, self.clock()),
        )

    def _insert_escalate_denial(self, conn, key, ticket_id, actor, reason, rejection_reason):
        denial_id = self._next_id(conn, "escalate_denials", "edn")
        conn.execute(
            """
            INSERT INTO escalate_denials (
                id, idempotency_key, ticket_id, actor, reason, rejection_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (denial_id, key, ticket_id, actor, reason, rejection_reason, self.clock()),
        )

    def _refund_from_row(self, row):
        return RefundReceipt(
            refund_id=row["id"],
            denial_reason=None,
            idempotency_key=row["idempotency_key"],
            amount_cents=row["amount_cents"],
            currency=row["currency"],
            order_id=row["order_id"],
            ticket_id=row["ticket_id"],
            actor=row["actor"],
        )

    def _refund_denial_from_row(self, row):
        return RefundReceipt(
            refund_id=None,
            denial_reason=row["reason"],
            idempotency_key=row["idempotency_key"],
            amount_cents=row["amount_cents"],
            currency=row["currency"],
            order_id=row["order_id"],
            ticket_id=row["ticket_id"],
            actor=row["actor"],
        )

    def _handoff_from_row(self, row):
        return EscalateReceipt(
            handoff_ticket_id=row["id"],
            rejection_reason=None,
            idempotency_key=row["idempotency_key"],
            ticket_id=row["ticket_id"],
            actor=row["actor"],
            reason=row["reason"],
        )

    def _escalate_denial_from_row(self, row):
        return EscalateReceipt(
            handoff_ticket_id=None,
            rejection_reason=row["rejection_reason"],
            idempotency_key=row["idempotency_key"],
            ticket_id=row["ticket_id"],
            actor=row["actor"],
            reason=row["reason"],
        )

    def _paid_cents(self, conn, order_id):
        return conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()["n"]

    def _refunded_cents(self, conn, order_id):
        return conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM refunds WHERE order_id = ?",
            (order_id,),
        ).fetchone()["n"]

    def _next_id(self, conn, table, prefix, where=None):
        sql = f"SELECT COUNT(*) AS n FROM {table}"
        if where is not None:
            sql += f" WHERE {where}"
        count = conn.execute(sql).fetchone()["n"]
        return f"{prefix}_{count + 1:04d}"


class _Session:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
