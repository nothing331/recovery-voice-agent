from datetime import date, datetime, timezone

from contextlib import contextmanager

from .calls import derive_outcome
from .payments import DebitsBlocked, FakePaymentProvider, ledger
from .rules import MAX_SCHEDULE_DAYS, validate_schedule_date
from datetime import timedelta

FLAG_REASONS = ["identity_denied", "do_not_call_request", "needs_card_update", "fraud", "dispute",
                "wants_human", "hardship", "abuse", "extension_over_5_days", "callback_requested"]
FINANCIAL = ("paid", "scheduled", "partial_paid_and_scheduled")
OUTCOMES = ["paid", "scheduled", "partial_paid_and_scheduled", "escalated", "callback", "voicemail", "no_answer",
            "incomplete"]
MAX_RETRIES_PER_CALL = 2

REASON_PLAIN = {
    "insufficient_funds": "there wasn't enough balance in the account at the time",
    "bank_technical_decline": "the bank declined it because of a technical issue on their side",
    "card_expired": "the card on file has expired",
    "card_lost": "the card on file was reported lost or blocked",
    "bank_decline": "the bank declined the payment",
}


def _now():
    return datetime.now(timezone.utc).isoformat()


class CallSession:
    def __init__(self, conn, call_id: int, customer_id: str, today: date):
        self.conn, self.call_id, self.customer_id, self.today = conn, call_id, customer_id, today
        self.identity_confirmed = False
        self.retries = 0
        self.payments = FakePaymentProvider(conn)

    # helpers
    def _customer(self):
        return self.conn.execute("SELECT * FROM customers WHERE id=?", (self.customer_id,)).fetchone()

    def _set_status(self, status, commit=True):
        self.conn.execute("UPDATE customers SET status=? WHERE id=?", (status, self.customer_id))
        if commit:
            self.conn.commit()

    def _promise(self, amount, date_str, commit=True):
        self.conn.execute("UPDATE calls SET promise_amount=?, promise_date=? WHERE id=?",
                          (amount, date_str, self.call_id))
        if commit:
            self.conn.commit()

    @contextmanager
    def _txn(self):
        """Single SQLite transaction: COMMIT on success, ROLLBACK if anything raises."""
        self.conn.commit()
        self.conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _remaining(self):
        charged, sched, _ = ledger(self.conn, self.customer_id)
        return self._customer()["amount_due"] - charged - sched

    def _arrangement(self, before_this_call: bool = False) -> dict | None:
        """Payment decision already on the account (from any call, or only earlier calls)."""
        q = "SELECT kind, amount, date FROM payment_attempts WHERE customer_id=? AND (kind='scheduled' OR result='success')"
        args = [self.customer_id]
        if before_this_call:
            q += " AND (call_id IS NULL OR call_id != ?)"
            args.append(self.call_id)
        rows = self.conn.execute(q + " ORDER BY id", args).fetchall()
        if not rows and self._customer()["status"] != "paid":
            return None
        paid = sum(r["amount"] for r in rows if r["kind"] != "scheduled")
        scheduled = [{"amount": r["amount"], "date": r["date"]} for r in rows if r["kind"] == "scheduled"]
        parts = []
        if paid:
            parts.append(f"{paid} rupees already paid")
        parts += [f"{x['amount']} rupees scheduled for {x['date']}" for x in scheduled]
        return {"paid": paid, "scheduled": scheduled,
                "summary": "; ".join(parts) or "the account is already marked paid"}

    def _resolved(self):
        """Backend gate: one payment decision per case."""
        _, _, has_schedule = ledger(self.conn, self.customer_id)
        if self._customer()["status"] == "paid" or has_schedule or self._remaining() <= 0:
            arr = self._arrangement()
            what = arr["summary"] if arr else "a payment decision is already recorded"
            return {"error": "already_resolved", "existing_arrangement": arr,
                    "message": f"Not a system error: this account already has an arrangement ({what}). "
                               "Do not charge or schedule again, and do not tell the customer something went wrong. "
                               "Tell them the existing arrangement, then end the call. If they want to change it, "
                               "call flag_for_human with reason wants_human and their requested change."}
        return None

    @staticmethod
    def _failed(e):
        return {"error": "payment_error", "message": f"The payment could not be recorded ({type(e).__name__}). Nothing was charged. Apologise and flag_for_human."}

    def _locked(self):
        if not self.identity_confirmed:
            return {"error": "identity_not_confirmed",
                    "message": "Identity is not confirmed. Share no account details; ask for the customer by name and call confirm_identity."}
        if self._customer()["debits_blocked"]:
            return {"error": "debits_blocked",
                    "message": "Debits are blocked for this customer. Do not take payment; wrap up politely."}
        return None

    def _date_error(self, d):
        code = validate_schedule_date(d, self.today)
        if not code:
            return None
        max_date = (self.today + timedelta(days=MAX_SCHEDULE_DAYS)).isoformat()
        if code == "invalid_date":
            return {"error": "invalid_date", "max_date": max_date,
                    "message": f"Date must be YYYY-MM-DD between {self.today.isoformat()} and {max_date}. Ask the customer for a specific date."}
        return {"error": "date_out_of_range", "max_date": max_date,
                "message": f"Latest allowed date is {max_date}. If the customer needs longer, call flag_for_human with extension_over_5_days."}

    # tools
    def confirm_identity(self, confirmed: bool) -> dict:
        if not confirmed:
            self.identity_confirmed = False
            self.conn.execute("UPDATE calls SET identity_result='denied' WHERE id=?", (self.call_id,))
            self.conn.commit()
            return {"confirmed": False,
                    "message": "Share nothing about the account. Say the team will follow up, call flag_for_human with reason identity_denied and a note of what the person said, then end the call politely."}
        self.identity_confirmed = True
        self.conn.execute("UPDATE calls SET identity_result='confirmed' WHERE id=?", (self.call_id,))
        self.conn.commit()
        c = self._customer()
        out = {"confirmed": True, "plan": c["plan"], "amount_due": c["amount_due"], "due_date": c["due_date"],
               "failure_reason_plain": REASON_PLAIN.get(c["failure_reason"], c["failure_reason"])}
        if (arr := self._arrangement()):
            out["existing_arrangement"] = arr
            out["message"] = (f"This account already has an arrangement from an earlier call: {arr['summary']}. "
                              "Do not ask for payment. Remind the customer of it and end the call. If they want "
                              "to change it, call flag_for_human with reason wants_human and their requested change.")
        return out

    def retry_debit(self) -> dict:
        if (e := self._locked()):
            return e
        if self.retries >= MAX_RETRIES_PER_CALL:
            return {"error": "already_retried",
                    "message": "The debit was already retried on this call. Do not retry again. Offer to schedule it within 5 days or a partial payment instead."}
        if (e := self._resolved()):
            return e
        self.retries += 1
        amount = self._remaining()
        try:
            with self._txn():
                ok, reason = self.payments.retry(self.customer_id, self.call_id, amount, commit=False)
                if ok:
                    self._set_status("paid", commit=False)
                    self._promise(amount, self.today.isoformat(), commit=False)
        except DebitsBlocked:
            return self._locked() or {"error": "debits_blocked"}
        except Exception as e:
            return self._failed(e)
        if ok:
            return {"success": True, "amount": amount,
                    "message": "Payment went through. Confirm it to the customer, thank them and end the call."}
        return {"success": False, "reason": reason,
                "message": f"The retry failed ({REASON_PLAIN.get(reason, reason)}). Do not retry again unless the customer says they have just added money or fixed their card. Otherwise offer to schedule the debit within 5 days, a partial payment, or flag for a human if the card is unusable."}

    def schedule_debit(self, date: str, amount: int | None = None) -> dict:
        if (e := self._locked()):
            return e
        if (e := self._resolved()):
            return e
        remaining = self._remaining()
        if amount is None:
            amount = remaining
        if not isinstance(amount, int) or isinstance(amount, bool) or not 0 < amount <= remaining:
            return {"error": "invalid_amount",
                    "message": f"Amount must be between 1 and {remaining} rupees."}
        if (e := self._date_error(date)):
            return e
        try:
            with self._txn():
                self.payments.schedule(self.customer_id, self.call_id, amount, date, commit=False)
                self._set_status("scheduled", commit=False)
                self._promise(amount, date, commit=False)
        except DebitsBlocked:
            return self._locked() or {"error": "debits_blocked"}
        except Exception as e:
            return self._failed(e)
        return {"success": True, "amount": amount, "date": date,
                "message": f"Debit of {amount} rupees is scheduled for {date}. Confirm this with the customer and end the call."}

    def take_partial_payment(self, amount_now: int, rest_date: str) -> dict:
        if (e := self._locked()):
            return e
        if (e := self._resolved()):
            return e
        due = self._remaining()
        if not isinstance(amount_now, int) or isinstance(amount_now, bool) or not 0 < amount_now < due:
            return {"error": "invalid_amount",
                    "message": f"Partial amount must be more than 0 and less than {due} rupees. For the full amount use retry_debit or schedule_debit."}
        if (e := self._date_error(rest_date)):
            return e
        rest = due - amount_now
        try:
            with self._txn():
                ok, reason = self.payments.charge_partial(self.customer_id, self.call_id, amount_now, commit=False)
                if ok:
                    self.payments.schedule(self.customer_id, self.call_id, rest, rest_date, commit=False)
                    self._set_status("partial_scheduled", commit=False)
                    self._promise(rest, rest_date, commit=False)
        except DebitsBlocked:
            return self._locked() or {"error": "debits_blocked"}
        except Exception as e:
            return self._failed(e)
        if not ok:
            return {"success": False, "reason": reason,
                    "message": f"The partial charge failed ({REASON_PLAIN.get(reason, reason)}). Offer to schedule the full amount within 5 days instead."}
        return {"success": True, "paid_now": amount_now, "remaining": rest, "rest_date": rest_date,
                "message": f"Charged {amount_now} rupees now; remaining {rest} rupees scheduled for {rest_date}. Confirm both with the customer and end the call."}

    def flag_for_human(self, reason: str, notes: str) -> dict:
        if reason not in FLAG_REASONS:
            return {"error": "invalid_reason", "message": f"Reason must be one of: {', '.join(FLAG_REASONS)}."}
        if not notes or not notes.strip():
            return {"error": "note_required",
                    "message": "Include a brief note of what the person said and the context."}
        self.conn.execute(
            "INSERT INTO attention_items(customer_id,call_id,reason,notes,created_at,resolved) VALUES(?,?,?,?,?,0)",
            (self.customer_id, self.call_id, reason, notes.strip(), _now()))
        status = self._customer()["status"]
        if reason == "fraud":
            self.conn.execute("UPDATE customers SET debits_blocked=1 WHERE id=?", (self.customer_id,))
            self._set_status("blocked")
        elif reason == "callback_requested":
            if status in ("due", "callback"):
                self._set_status("callback")
        elif status not in ("paid", "scheduled", "partial_scheduled", "blocked"):
            self._set_status("escalated")
        self.conn.commit()
        return {"success": True,
                "message": ("Callback noted. Confirm the time with the customer, then end the call with outcome callback."
                            if reason == "callback_requested" else
                            "Flagged for the team. Tell the customer someone will follow up, then end the call politely.")}

    def end_call(self, outcome: str, summary: str) -> dict:
        if outcome not in OUTCOMES:
            return {"error": "invalid_outcome", "message": f"Outcome must be one of: {', '.join(OUTCOMES)}."}
        derived = derive_outcome(self.conn, self.call_id)
        if outcome in FINANCIAL and derived is None and (arr := self._arrangement(before_this_call=True)):
            # Confirming an arrangement made on an earlier call: no new payment on this call.
            self.conn.execute("UPDATE calls SET outcome=?, disposition=?, summary=?, ended_at=? WHERE id=?",
                              ("incomplete", "existing_arrangement", summary, _now(), self.call_id))
            self.conn.commit()
            return {"success": True, "note": f"No new payment on this call; existing arrangement: {arr['summary']}."}
        if outcome in FINANCIAL and outcome != derived:
            if derived:
                msg = f"Ledger shows {derived}. Use {derived}."
            else:
                msg = "Ledger shows no payment. Use escalated, voicemail, no_answer or incomplete."
            return {"error": "outcome_mismatch", "message": msg}
        self.conn.execute("UPDATE calls SET outcome=?, disposition=?, summary=?, ended_at=? WHERE id=?",
                          (derived or outcome, outcome, summary, _now(), self.call_id))
        self.conn.commit()
        return {"success": True}


TOOL_SCHEMAS = [
    {"name": "confirm_identity",
     "description": "Record whether the person confirmed they are the customer by full name. Call with true only if they clearly said yes. Returns account details only when true.",
     "parameters": {"type": "object", "properties": {"confirmed": {"type": "boolean", "description": "True if the person confirmed they are the customer."}}, "required": ["confirmed"]}},
    {"name": "retry_debit",
     "description": "Retry the failed autopay debit now for the outstanding amount. At most twice per call: retry a second time only if the customer says they have just added money or fixed their card. Do not use when the customer says the balance is low.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "schedule_debit",
     "description": "Schedule the debit for a date at most 5 days from today (today counts as day 0). Amount defaults to the full outstanding amount.",
     "parameters": {"type": "object", "properties": {
         "date": {"type": "string", "description": "ISO date YYYY-MM-DD, IST, between today and today+5."},
         "amount": {"type": "integer", "description": "Amount in whole rupees. Optional; defaults to the outstanding amount."}},
         "required": ["date"]}},
    {"name": "take_partial_payment",
     "description": "Charge part of the amount now and schedule the rest within 5 days.",
     "parameters": {"type": "object", "properties": {
         "amount_now": {"type": "integer", "description": "Rupees to charge now; more than 0 and less than the outstanding amount."},
         "rest_date": {"type": "string", "description": "ISO date YYYY-MM-DD for the remainder, between today and today+5."}},
         "required": ["amount_now", "rest_date"]}},
    {"name": "flag_for_human",
     "description": "Escalate to the human team. Use for anything you cannot resolve with the payment tools.",
     "parameters": {"type": "object", "properties": {
         "reason": {"type": "string", "enum": FLAG_REASONS},
         "notes": {"type": "string", "description": "Brief note of what the person said and context. Required."}},
         "required": ["reason", "notes"]}},
    {"name": "end_call",
     "description": "Record the call outcome and summary, then end the call. Call once, after saying goodbye.",
     "parameters": {"type": "object", "properties": {
         "outcome": {"type": "string", "enum": OUTCOMES},
         "summary": {"type": "string", "description": "One or two sentences on what happened."}},
         "required": ["outcome", "summary"]}},
]
