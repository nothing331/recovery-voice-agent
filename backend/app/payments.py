from datetime import datetime, timezone

INSTRUMENT_STATUSES = ("active", "expired", "lost")
_INSTRUMENT_FAILURE = {"expired": "card_expired", "lost": "card_lost"}


class DebitsBlocked(Exception):
    pass


class FakePaymentProvider:
    """Simulated bank. A charge succeeds only when the card/mandate is usable and the customer's
    balance covers the amount; a successful charge deducts the balance. Balance and card status
    are editable from the dashboard, so outcomes follow account state, not a scripted result.
    Swap for a Razorpay test-mode adapter later by keeping retry / charge_partial / schedule."""

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

    def _charge(self, customer_id, call_id, kind, amount, commit):
        c = self._customer(customer_id)
        instrument = c["instrument_status"] or "active"
        if instrument != "active":
            res = _INSTRUMENT_FAILURE.get(instrument, "bank_decline")
        elif amount > (c["balance"] or 0):
            res = "insufficient_funds"
        else:
            res = "success"
            self.conn.execute("UPDATE customers SET balance=balance-? WHERE id=?", (amount, customer_id))
        self._record(customer_id, call_id, kind, amount, None, res, commit)
        return res == "success", res

    def retry(self, customer_id, call_id, amount, commit=True):
        return self._charge(customer_id, call_id, "retry", amount, commit)

    def charge_partial(self, customer_id, call_id, amount, commit=True):
        return self._charge(customer_id, call_id, "partial", amount, commit)

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
