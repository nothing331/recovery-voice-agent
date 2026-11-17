from datetime import datetime, timezone


class DebitsBlocked(Exception):
    pass


class FakePaymentProvider:
    """Results come from the customer's seeded retry_result / partial_result.
    Swap for a Razorpay test-mode adapter later by keeping these three methods."""

    def __init__(self, conn):
        self.conn = conn

    def _customer(self, customer_id):
        row = self.conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row["debits_blocked"]:
            raise DebitsBlocked(customer_id)
        return row

    def _record(self, customer_id, call_id, kind, amount, date, result, commit=True):
        self.conn.execute(
            "INSERT INTO payment_attempts(customer_id,call_id,kind,amount,date,result,created_at) VALUES(?,?,?,?,?,?,?)",
            (customer_id, call_id, kind, amount, date, result, datetime.now(timezone.utc).isoformat()),
        )
        if commit:
            self.conn.commit()

    def retry(self, customer_id, call_id, commit=True):
        c = self._customer(customer_id)
        res = c["retry_result"]
        self._record(customer_id, call_id, "retry", c["amount_due"], None, res, commit)
        return res == "success", ("success" if res == "success" else res)

    def charge_partial(self, customer_id, call_id, amount, commit=True):
        c = self._customer(customer_id)
        res = c["partial_result"]
        self._record(customer_id, call_id, "partial", amount, None, res, commit)
        return res == "success", ("success" if res == "success" else res)

    def schedule(self, customer_id, call_id, amount, date, commit=True):
        self._customer(customer_id)
        self._record(customer_id, call_id, "scheduled", amount, date, "scheduled", commit)


def ledger(conn, customer_id):
    """(charged, scheduled, has_schedule) from payment_attempts across all calls."""
    q = "SELECT COALESCE(SUM(amount),0) FROM payment_attempts WHERE customer_id=? AND %s"
    charged = conn.execute(q % "kind IN ('retry','partial') AND result='success'", (customer_id,)).fetchone()[0]
    sched = conn.execute(q % "kind='scheduled'", (customer_id,)).fetchone()[0]
    n = conn.execute("SELECT count(*) FROM payment_attempts WHERE customer_id=? AND kind='scheduled'",
                     (customer_id,)).fetchone()[0]
    return charged, sched, n > 0
