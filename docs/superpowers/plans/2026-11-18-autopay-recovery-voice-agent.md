# Autopay Recovery Voice Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an outbound voice agent that calls a +91 mobile through Plivo, recovers a failed autopay against fake data, and shows the outcomes on a React dashboard. The demo is at 11:00 on 2026-11-18.

**Architecture:** One local FastAPI process serves the dashboard API, the Plivo answer XML and the Plivo media WebSocket. Pipecat runs the voice pipeline: Deepgram speech-to-text, Gemini Flash with function tools, Deepgram Aura voice. The tools call a pure-Python domain layer (SQLite plus a fake payment service), which enforces every rule. A Vite React TypeScript dashboard talks to the API. A Cloudflare quick tunnel exposes port 8000 to Plivo.

**Tech Stack:** Python 3.12 (uv), FastAPI, Pipecat (latest, extras: `plivo`/websocket, `deepgram`, `google`, `webrtc`), Plivo Python SDK, stdlib `sqlite3`, pytest, Node 22, Vite, React 18, TypeScript.

**Spec:** `docs/superpowers/specs/2026-11-18-autopay-recovery-voice-agent-design.md`. Section 12 sets today's scope and overrides anything it cuts.

## Global Constraints

- The machine has Python 3.15; Pipecat needs an older Python. Pin `requires-python = ">=3.12,<3.13"` in `backend/pyproject.toml`.
- Currency is whole rupees (`int`). Dates are ISO `YYYY-MM-DD` in Asia/Kolkata. "Today" means the IST date when the call happens.
- A scheduled debit date `d` is valid only when `0 <= (d - today).days <= 5`.
- Account details (amount, plan, due date, reason) reach the LLM **only** from `confirm_identity(confirmed=true)`. The system prompt contains the customer's name and nothing else about the account.
- `flag_for_human` reasons: `identity_denied`, `do_not_call_request`, `needs_card_update`, `fraud`, `dispute`, `wants_human`, `hardship`, `abuse`, `extension_over_5_days`. A note is required.
- Call outcomes: `paid`, `scheduled`, `partial_paid_and_scheduled`, `escalated`, `voicemail`, `no_answer`, `incomplete`.
- Customer status: `due`, `paid`, `scheduled`, `partial_scheduled`, `escalated`, `blocked` (`blocked` means a fraud flag set `debits_blocked`).
- The agent doesn't say it's an AI but must never deny it. It gives a recording notice, offers no discounts, and never asks for card or bank numbers.
- Secrets live only in `backend/.env`, which is gitignored. Commit `backend/.env.example`.
- **Subagents do not commit.** The coordinator commits after each task. Each task touches only its listed files.

## Review Focus

1. **The LLM proposes a date 6 or more days away, or a past date.** Expected: `schedule_debit` and `take_partial_payment` reject it with `{"error": "date_out_of_range", "max_date": ...}`, and the prompt tells the agent to offer the escalation instead. Tested in Task 2.
2. **The person confirms identity, then says "actually I'm his brother".** Expected: `confirm_identity(false)` after a `true` locks the account tools again, and the payment tools return `identity_not_confirmed`. Tested in Task 2.
3. **The customer hangs up before `end_call`.** Expected: the call is still saved with its transcript and outcome `incomplete`. Covered by the wiring in Task 4.
4. **The partial amount is 0, negative, or at least the outstanding amount.** Expected: rejected with `invalid_amount`. Tested in Task 2.
5. **Call is clicked twice, or clicked for a fraud-blocked customer.** Expected: 409. Tested in Task 4.

---

### Task 1: Backend scaffold and Plivo test call (the risk gate)

**Files:**
- Create: `.gitignore` (Python, node, `.env`, `*.db`)
- Create: `backend/pyproject.toml`, `backend/.env.example`, `backend/app/__init__.py`, `backend/app/config.py`, `backend/app/main.py`, `backend/app/telephony.py`, `backend/app/bot.py`

**Interfaces:**
- Produces `app.config.settings` with these fields: `plivo_auth_id`, `plivo_auth_token`, `plivo_from_number`, `public_base_url`, `default_to_number`, `verified_numbers: list[str]` (comma-separated in `.env`), `deepgram_api_key`, `google_api_key`, `gemini_model` (default: current Gemini Flash id), `deepgram_tts_voice` (an English Aura-2 voice), `company_name="Kitefibre"`, `agent_name="Meera"`, `db_path="recovery.db"`.
- Produces `app.telephony.place_call(to_number: str, call_id: int) -> str` (returns the Plivo request uuid), plus routes `POST /plivo/answer?call_id=` (returns Stream XML), `POST /plivo/hangup?call_id=`, and `WS /plivo/ws`.
- Produces `app.bot.run_bot(transport, call_id: int | None) -> Awaitable[None]`. In this task it's a hello bot that greets and chats. Task 4 replaces its body.

- [ ] **Step 1:** Follow the official Pipecat outbound example `pipecat-ai/pipecat-examples/plivo-chatbot/outbound` and the guide https://docs.pipecat.ai/pipecat/telephony/plivo-websockets, using the latest Pipecat release. Answer XML: a bidirectional `<Stream>` to `wss://<public_base_url host>/plivo/ws`, `audio/x-mulaw;rate=8000`, keepCallAlive. Pass `call_id` to the WebSocket handler (query string or Plivo `extraHeaders`, whichever the example supports). Turn on Plivo machine detection on the outbound call (`machine_detection="true"`, with a callback route stub).
- [ ] **Step 2:** Pipeline: `DeepgramSTTService` (model `nova-3`, language `en-IN`) → `GoogleLLMService` → `DeepgramTTSService`, with Silero VAD and interruptions on. The hello bot says: "Hi, this is Meera from Kitefibre. Can you hear me clearly?"
- [ ] **Step 3:** Add `GET /health` → `{"ok": true}`, plus a dev route `POST /dev/test-call {to_number}` that calls `place_call`.
- [ ] **Step 4:** Verify: `cd backend && uv sync && uv run uvicorn app.main:app --port 8000`, then `curl localhost:8000/health` → `{"ok":true}`. Running `uv run python -c "import app.bot, app.telephony"` exits 0. A real call needs the user's keys; report the exact manual test steps rather than faking them.

### Task 2: Domain core: data, fake payments, rules, tools (runs in parallel with Tasks 1 and 3)

**Files:**
- Create: `backend/app/db.py`, `backend/app/data/customers.json`, `backend/app/seed.py`, `backend/app/payments.py`, `backend/app/rules.py`, `backend/app/tools.py`
- Test: `backend/tests/test_rules.py`, `backend/tests/test_tools.py`
- Stdlib only. Do not edit `pyproject.toml`. Run tests with `cd backend && uv run --no-project --python 3.12 --with pytest pytest tests -q`.

**Interfaces (Produces):**
- `db.connect(path: str) -> sqlite3.Connection` (row factory = dict-like, `PRAGMA foreign_keys=ON`).
- `db.init_schema(conn)` creates these tables:
  - `customers(id TEXT PK, name, phone, plan, amount_due INT, due_date, failure_reason, past_attempts INT, persona, retry_result, partial_result, expected_outcome, status, debits_blocked INT)`
  - `calls(id INT PK, customer_id, to_number, channel TEXT 'phone'|'browser', plivo_uuid, status TEXT 'queued'|'ringing'|'in_progress'|'completed'|'failed', started_at, ended_at, outcome, identity_result TEXT 'confirmed'|'denied'|'not_reached', promise_amount INT, promise_date, summary, transcript_json TEXT)`
  - `payment_attempts(id, customer_id, call_id, kind 'retry'|'scheduled'|'partial', amount INT, date, result, created_at)`
  - `attention_items(id, customer_id, call_id, reason, notes, created_at, resolved INT, resolved_at)`
- `seed.reset(conn)` drops all rows and loads `customers.json`, with status `due` and `debits_blocked` 0.
- `customers.json`: 10 records matching the table in spec §7, using Indian names, plans for ₹499–₹2,999, and `failure_reason` ∈ `insufficient_funds|bank_technical_decline|card_expired|card_lost|bank_decline`. `retry_result` is `success` only for #1; the others carry their own failure reason. `partial_result` is `success`.
- `payments.FakePaymentProvider(conn)` with methods:
  - `retry(customer_id, call_id) -> tuple[bool, str]`
  - `charge_partial(customer_id, call_id, amount) -> tuple[bool, str]`
  - `schedule(customer_id, call_id, amount, date) -> None`

  Each method records a `payment_attempts` row and raises `DebitsBlocked` when `debits_blocked`.
- `rules.validate_schedule_date(date_str: str, today: date) -> str | None` returns an error code (`invalid_date`, `date_out_of_range`) or None. `rules.MAX_SCHEDULE_DAYS = 5`.
- `tools.CallSession(conn, call_id: int, customer_id: str, today: date)` with methods returning JSON-able dicts:
  - `confirm_identity(confirmed: bool)`: `true` returns `{plan, amount_due, due_date, failure_reason_plain}`; `false` sets the call to `denied`, locks the account tools, and returns an instruction to share nothing and flag `identity_denied`.
  - `retry_debit()`: at most 1 per call, otherwise `{"error": "already_retried"}`. Success sets the customer to `paid` and the call's promise.
  - `schedule_debit(date: str, amount: int | None = None)`: `amount` defaults to the outstanding amount. Sets the customer to `scheduled`.
  - `take_partial_payment(amount_now: int, rest_date: str)`: `0 < amount_now < amount_due`. Sets the customer to `partial_scheduled`.
  - `flag_for_human(reason: str, notes: str)`: `fraud` sets `debits_blocked=1` and status `blocked`; any other reason sets status `escalated` unless the customer is already `paid`, `scheduled` or `partial_scheduled`. Creates an attention item.
  - `end_call(outcome: str, summary: str)`: validates the outcome and stores the outcome, summary and ended_at.

  Every account tool returns `{"error": "identity_not_confirmed"}` before confirmation or after a denial.
- `tools.TOOL_SCHEMAS: list[dict]` holds the JSON-schema descriptions of the six tools, used by Task 4 for Gemini function calling.

- [ ] **Step 1:** Write the failing tests:
  - `test_rules.py`: today+0 and today+5 are OK; +6 → `date_out_of_range`; −1 → `date_out_of_range`; `"tomorrow"` → `invalid_date`.
  - `test_tools.py`, using an in-memory DB seeded by `seed.reset`:
    - account tools before identity → `identity_not_confirmed`,
    - confirm true then false → tools locked again,
    - customer #1 `retry_debit` → success and status `paid`,
    - a second retry → `already_retried`,
    - customer #2 schedule at +3 → status `scheduled` and a payment_attempts row,
    - schedule at +6 → error with `max_date`,
    - partial with amount 0, with amount ≥ amount_due, and with a +7 date → errors,
    - a valid partial → status `partial_scheduled` and two attempt rows,
    - flag `fraud` → `blocked` and a later `retry_debit` → `{"error": "debits_blocked"}`,
    - flag with an empty note → error,
    - flag with an unknown reason → error,
    - `end_call` with an unknown outcome → error.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the modules above.
- [ ] **Step 4:** Run → all PASS.

### Task 3: React dashboard (runs in parallel; touches only `frontend/`)

**Files:** Create `frontend/` with Vite React TypeScript. The dev server proxies `/api` → `http://localhost:8000`.

**Interfaces (Consumes):** this API contract, which Task 4 implements exactly:
- `GET /api/customers` → `[{id, name, plan, amount_due, due_date, failure_reason, persona, expected_outcome, status, debits_blocked, open_attention_count, last_outcome}]`
- `GET /api/numbers` → `{default: string, numbers: string[]}`
- `POST /api/calls {customer_id, to_number}` → `{call_id}`. 409 means `{detail}` for blocked or already in progress; 400 means the number isn't verified.
- `POST /api/browser-session {customer_id}` → `{url}`. Open it in a new tab.
- `GET /api/calls` → `[{id, customer_id, customer_name, to_number, channel, status, outcome, summary, started_at, ended_at}]`
- `GET /api/calls/{id}` → the above plus `identity_result, promise_amount, promise_date, transcript: [{role: "agent"|"customer", text}], payment_attempts: [...], attention_items: [...]`
- `GET /api/attention?resolved=false` → `[{id, customer_id, customer_name, call_id, reason, notes, created_at, resolved}]`
- `POST /api/attention/{id}/resolve` → `{ok: true}`
- `POST /api/demo/reset` → `{ok: true}`

- [ ] **Step 1:** Build a single page with three tabs: Customers, Calls, Needs attention.
  - **Customers:** a table (name, plan, ₹amount, reason, status chip, attention badge) with **Call** and **Talk in browser** buttons. Call is disabled when `debits_blocked`. A "Call to" `<select>` from `/api/numbers` sits on top, alongside a **Reset demo data** button. Persona and expected outcome show as small grey text; it's a demo aid.
  - **Calls:** the list, refreshed by polling every 3 s. Clicking a call opens a detail panel with the transcript as chat bubbles, the outcome, the promise, attempts and flags.
  - **Needs attention:** a coloured badge for each reason, the notes, a link to the call, and **Mark resolved**.
- [ ] **Step 2:** Show API errors as a visible toast. Use plain CSS with no UI library.
- [ ] **Step 3:** Verify: `cd frontend && npm install && npm run build` exits 0 and `npx tsc --noEmit` is clean.

### Task 4: Wire the agent end to end (after Tasks 1 and 2)

**Files:** Create `backend/app/prompt.py`, `backend/app/api.py`, `backend/tests/test_api.py`. Modify `backend/app/bot.py`, `backend/app/telephony.py`, `backend/app/main.py`.

**Interfaces:**
- Consumes everything from Task 2's Produces and Task 1's Produces.
- Produces the API contract in Task 3.
- `prompt.build_system_prompt(customer_name: str, company: str, agent_name: str, today: date) -> str`.

- [ ] **Step 1:** Write the failing `test_api.py` with FastAPI TestClient and a temporary DB, with `place_call` monkeypatched:
  - `POST /api/calls` for a blocked customer → 409,
  - two calls while one is `in_progress` → 409,
  - an unverified number → 400,
  - `POST /api/demo/reset` → customers back to `due`,
  - resolving an attention item → `resolved=1`.
- [ ] **Step 2:** Implement `api.py` per the contract. Seed the DB at startup if it's empty.
- [ ] **Step 3:** Write `prompt.py`, encoding the call flow in spec §6.2 and the hard rules:
  - The flow: name confirmation, explain, then retry / schedule / partial, then escalation through `flag_for_human`. Low balance means ask when funds arrive, not blindly retry. "Don't call me" goes to `do_not_call_request`. Voicemail gets a detail-free message.
  - The rules: if asked whether it's AI, answer "I'm an automated assistant from Kitefibre". Short spoken sentences, Indian English, amounts spoken as "rupees".
  - Always call `end_call` before saying goodbye.
- [ ] **Step 4:** In `bot.py`:
  - Register the `TOOL_SCHEMAS` with the Gemini LLM service, and route each function call to a `CallSession` method.
  - After `end_call`, let the goodbye play, then end the pipeline. The Plivo serializer then hangs up.
  - On client disconnect, save the transcript (from the LLM context, with system and tool messages dropped) to `transcript_json`. If no outcome is set yet, set `incomplete`.
  - In `telephony.py`, the hangup callback maps an unanswered or busy call to outcome `no_answer`. When machine detection reports a machine, the bot leaves the voicemail line and records `voicemail`.
- [ ] **Step 5:** Browser fallback. Mount Pipecat's SmallWebRTC prebuilt client and offer route. `POST /api/browser-session` creates a `calls` row with channel `browser`, stores it as the pending browser call, and returns the client URL. The WebRTC connection handler runs `run_bot(transport, call_id)` for that pending call.
- [ ] **Step 6:** Run `uv run pytest -q` → all PASS, including Task 2's tests. Then start the server and confirm that `POST /api/browser-session` returns a URL that loads.

### Task 5: README and end-to-end check

**Files:** Create `README.md`.

- [ ] **Step 1:** Write the README. It covers:
  - what the project is, the architecture diagram (ASCII), and the stack,
  - setup: Plivo signup, enabling India in Geo Permissions, Sandbox Numbers OTP, the caller ID note, and getting the Deepgram and Gemini keys,
  - `brew install cloudflared`, then `cloudflared tunnel --url http://localhost:8000`, with the resulting URL going into `PUBLIC_BASE_URL`,
  - running the backend and frontend,
  - the 10-scenario table with how to role-play each one,
  - the design decisions, linked to the spec,
  - known limits: the §12 cuts, the international caller ID, the free-tier limits.
- [ ] **Step 2:** Run the end-to-end check with the user: a real phone call for scenarios 1, 2 and 7, checked on the dashboard. Then one browser call.
