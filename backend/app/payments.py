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

    def _record(self, customer_id, call_id, kind, amount, date, result):
        self.conn.execute(
            "INSERT INTO payment_attempts(customer_id,call_id,kind,amount,date,result,created_at) VALUES(?,?,?,?,?,?,?)",
            (customer_id, call_id, kind, amount, date, result, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def retry(self, customer_id, call_id):
        c = self._customer(customer_id)
        res = c["retry_result"]
        self._record(customer_id, call_id, "retry", c["amount_due"], None, res)
        return res == "success", ("success" if res == "success" else res)

    def charge_partial(self, customer_id, call_id, amount):
        c = self._customer(customer_id)
        res = c["partial_result"]
        self._record(customer_id, call_id, "partial", amount, None, res)
        return res == "success", ("success" if res == "success" else res)

    def schedule(self, customer_id, call_id, amount, date):
        self._customer(customer_id)
        self._record(customer_id, call_id, "scheduled", amount, date, "scheduled")
