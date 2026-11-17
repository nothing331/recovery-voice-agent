# Autopay Recovery Voice Agent — Design

Date: 2026-11-18
Status: Draft, awaiting approval

## 1. What this is and why

An assignment / interview demo: a voice agent that places a **real phone call** to a customer whose autopay failed and tries to recover the payment. It works against 10 fake customer records.

**Success** means a live call, made in front of a reviewer, where the agent:

1. confirms who it's talking to,
2. explains that the autopay failed,
3. recovers the payment, or routes the case correctly,
4. saves the outcome, which then shows on a dashboard.

**Deliverables:** a GitHub repo with a README (setup, design, the 10 scenarios), plus a live demo. No recorded video is required.

## 2. Goals, non-goals, anti-goals

**Goals**
- A real outbound PSTN call to a +91 mobile that works end to end.
- A believable recovery conversation in Indian English, with tool calls that change state.
- A dashboard to start calls and review outcomes, transcripts and items flagged for a human.
- Ten scenarios that each exercise a different branch of the agent.

**Non-goals (version 1)**
- Real money movement. Payments are faked.
- SMS or payment links. Dropped because Indian DLT rules make sending them impractical.
- A live transcript while the call is running.
- Deployment. Everything runs on the developer's laptop.
- Automatic retries of unanswered calls, and live transfer to a human.
- Hindi or Hinglish.
- Production compliance (TRAI/DLT, Indian caller ID, consent management) and dashboard auth.

**Anti-goals** (failures even if the code "works")
- The agent mentions an amount or account detail before the person confirms, by name, that they are the customer.
- The agent invents offers: dates beyond 5 days, discounts or waivers.
- The agent denies being an AI when asked directly.
- The live demo breaks because of an external payment sandbox or another non-essential dependency.

## 3. Constraints

- **Phone numbers:** the target is the developer's +91 mobile, plus an interviewer's number when needed.
- **Plivo trial account:**
  - Outbound calls only reach numbers verified in the Plivo sandbox. The interviewer's number gets verified beforehand with an OTP from them.
  - India must be enabled in Plivo's geo permissions.
  - The caller ID is foreign, so the phone shows "International call". Warn the interviewer.
  - About $10 of trial credit, reportedly valid for 3 weeks.
- **Cost:** stay on free tiers. Claude API isn't available; the $20 Claude Pro plan includes no API credit.
- **Hosting:** runs locally. Plivo needs a public HTTPS/WSS URL, so we expose the local server through a tunnel (ngrok or Cloudflare Tunnel).

## 4. Approaches considered

| Approach | Trade-off | Verdict |
|---|---|---|
| **Pipecat + Plivo (WebSocket media stream)** | Official Pipecat Plivo serializer and outbound example. Media is a plain WebSocket, with no SIP trunk. The same bot code also runs in the browser via SmallWebRTC. Python only. | **Chosen** |
| LiveKit Agents + Plivo Zentrunk (SIP) | Documented by both vendors, but needs SIP trunk setup and India region pinning on LiveKit Cloud (which plan includes it is unclear), and there was an India SIP outage in June 2026. Better for scale. | Rejected for v1 |
| Hosted platforms (Vapi / Retell) | Fastest to a call, but their built-in numbers are US-only and the user preferred Plivo + an open framework. | Rejected |

Sources: [Pipecat Plivo guide](https://docs.pipecat.ai/pipecat/telephony/plivo-websockets), [outbound example](https://github.com/pipecat-ai/pipecat-examples/tree/main/plivo-chatbot/outbound), [LiveKit + Plivo](https://docs.livekit.io/telephony/start/providers/plivo/).

## 5. Decided stack

| Layer | Choice | Notes |
|---|---|---|
| Telephony | Plivo (trial), outbound call through the REST API, answer XML `<Stream>` → our WSS | Machine detection for voicemail. |
| Voice pipeline | Pipecat (Python) | `PlivoFrameSerializer`. |
| STT | Deepgram Nova-3, `en-IN` | Within the $200 free credit. |
| TTS | Deepgram voices during development, **Sarvam Bulbul** for demo calls, **Cartesia** as fallback | Switch by config. Sarvam's free credit is only ₹100 (about 40 minutes), so save it for the demo. |
| LLM | **Gemini Flash** (free tier), with **Groq** (Llama) as a fallback | Switch by config. Free-tier data may be used by Google; fine because the data is fake. |
| Backend | Python, FastAPI, SQLite | |
| Frontend | React + TypeScript | Polls or fetches; no live streaming needed. |
| Exposure | ngrok or Cloudflare Tunnel | Builder's choice. |

Every provider must be switchable through config or `.env`, so a slow or out-of-credit provider can be replaced without code changes.

## 6. Product behaviour

### 6.1 Company and persona
- A fictional **subscription** company, broadband or OTT, with monthly autopay of ₹499–₹2,999. The tone is friendly and low-stakes. *(Name: builder's call, e.g. "Kitefibre". Check that it isn't a real brand.)*
- An agent with a human first name (builder's call). The agent **does not say it is an AI**, but if asked directly ("am I talking to a bot?") it answers honestly: "I'm an automated assistant from <Company>", and carries on.
- It opens with a recording notice: "this call may be recorded for quality purposes."

### 6.2 Call flow

1. **Opening:** greet, give the company name, and ask for the customer by full name.
2. **Identity check: name confirmation only.** The agent asks "Am I speaking with <full name>?" If the person says yes, the agent treats them as the customer and continues. No PIN code, date of birth or other secret is asked for. (User's decision: someone acting in bad faith would just claim not to know a secret, so asking for one adds friction without adding safety.)
3. **Identity denied:** if the person says they are not the customer (wrong number, "that's not me", a family member answering, "they're not here"), the agent shares **nothing** about the account, says the team will follow up, and ends politely. It calls `flag_for_human(identity_denied, note)`, with a note recording what the person said (e.g. "says wrong number", "spouse answered, customer at work"). Outcome `escalated`.
4. **Explain:** "Your autopay of ₹X for <plan> on <date> didn't go through." Then give the reason in plain words.
5. **Resolve.** The agent has exactly three payment actions:
   - **Retry the debit now** on the account or mandate on file.
   - **Schedule the debit** for a date the customer chooses, **at most 5 calendar days** from today (counting today as day 0).
   - **Partial payment now + schedule the rest**, with the rest also within 5 days.
   - If the balance is low, the agent should not blindly retry. It asks when funds will be available and schedules for then.
6. **Everything else is flagged for a human and ends politely.** The agent says someone from the team will call back:
   - The customer needs **more than 5 days**.
   - The card is **expired or lost**. The agent explains that the card on file can't be charged, and the call is flagged `needs_card_update`. No link is sent.
   - **Fraud** ("I never signed up / didn't authorise this"). All future debits for that customer stop.
   - **Dispute** of the amount or charge.
   - **Wants a human.**
   - **Hardship** (job loss, medical, etc.). The agent is gentle, offers up to 5 days if the customer wants it, and otherwise escalates.
   - **Abuse or threats.** The agent ends the call politely.
7. **"Don't call me again":** the agent does **not** opt the customer out by itself. A defaulting customer could say this just to stop collection. The agent acknowledges the request, says the team will review it, and calls `flag_for_human(do_not_call_request, note)` with the customer's words and context. A human decides; the customer stays callable until then. Outcome `escalated`.
8. **Voicemail** (machine detected): leave a short message with no details. Outcome `voicemail`.
9. **No answer or busy:** outcome `no_answer`. Re-dialling is manual from the dashboard.
10. **Close:** confirm what was agreed (amount and date), thank the customer, hang up.

**Hard rules in the system prompt:** no amounts before the person confirms they are the customer; no discounts or waivers; no dates beyond 5 days; never ask for card or bank numbers; never deny being an AI.

### 6.3 Agent tools (LLM function calls)

The names are indicative; the builder may rename them.

- `confirm_identity(confirmed: bool)` → records whether the person confirmed by name; account details are only returned to the LLM after `confirmed=true`.
- `retry_debit()` → success / failure with a reason, from the fake payment service.
- `schedule_debit(date, amount)` → validates the 5-day limit on the server, not just in the prompt.
- `take_partial_payment(amount_now, rest_date)` → charges part now and schedules the rest; the 5-day limit is validated on the server.
- `flag_for_human(reason, notes)` → reason ∈ {`identity_denied`, `do_not_call_request`, `needs_card_update`, `fraud`, `dispute`, `wants_human`, `hardship`, `abuse`, `extension_over_5_days`}. `notes` is required: what the person said, in brief.
- `end_call(outcome, summary)`

### 6.4 Fake payment service

- An internal module behind a `PaymentProvider` interface, so that a Razorpay test-mode adapter (UPI AutoPay / eNACH) can be added later.
- Results are **set per customer** in the seed data. For example: a retry succeeds for customer A; a retry fails with `insufficient_funds` for customer B; a scheduled debit always succeeds. This keeps the live demo predictable.
- It records every attempt (time, amount, result) in the database.
- A `fraud` flag blocks every future debit for that customer.
- **Scheduled debits** *(my call)*: booking one always succeeds, and the customer's status becomes `scheduled`. Nothing runs on a real clock. Instead, a dashboard button, **"Run scheduled debits (simulate)"**, executes every pending scheduled debit at once using its set result and updates the status (e.g. to `paid`). That shows the promise turning into money without building a real scheduler.

## 7. The 10 customer records

Each record has: id, name, phone (defaults to the developer's number), plan/product, amount due, due date, failure reason, past attempts, a **persona note** (a script for whoever plays the customer), the set payment results, and the expected outcome.

*Proposed mix (my call; the builder may adjust names and amounts):*

| # | Failure reason | Persona (for whoever answers) | Set payment result | Expected outcome |
|---|---|---|---|---|
| 1 | Bank technical decline | Cooperative | Retry succeeds | `paid` |
| 2 | Low balance | Salary arrives in 3 days | Retry would fail; schedule succeeds | `scheduled` |
| 3 | Low balance | Can pay part now | Partial succeeds | `partial_paid_and_scheduled` |
| 4 | Card expired | Cooperative | — | Flag `needs_card_update` |
| 5 | Card lost | Slightly worried | — | Flag `needs_card_update` |
| 6 | Low balance | Needs 10 days | — | Flag `extension_over_5_days` |
| 7 | Bank decline | "I never signed up" | — | Flag `fraud`, debits blocked |
| 8 | Low balance | Angry, then abusive | — | Flag `abuse` |
| 9 | Low balance | Lost job recently | Schedule if accepted | `scheduled` or flag `hardship` |
| 10 | Low balance | Spouse answers, says the customer isn't available | — | Flag `identity_denied` with a note |

## 8. Data saved per call (SQLite)

- `customers`: the record above, plus current status and `debits_blocked`.
- `calls`:
  - customer, dialled number, Plivo call id, start and end times, duration, cost (from the Plivo API if available, otherwise estimated)
  - outcome, identity result (`confirmed` / `denied` / `not_reached`)
  - promise amount and date
  - transcript (turns with speaker and timestamps), summary, path to the recording
- `payment_attempts`: customer, call, type (`retry` / `scheduled` / `partial`), amount, date, result.
- `attention_items`: customer, call, reason, notes, created time, `resolved` and resolved time.

**Recording:** a local WAV of both sides, captured in the pipeline (Pipecat's audio buffer). Using Plivo's recording instead is the builder's call.

**Outcome values:** `paid`, `scheduled`, `partial_paid_and_scheduled`, `escalated` (any `flag_for_human`, including identity denied and don't-call requests), `voicemail`, `no_answer`.

## 9. Dashboard (React + TypeScript)

- **Customers:** the 10 records with amount, reason, current status and latest outcome. There's a **Call** button per row, disabled only when debits are blocked by a fraud flag. A customer with an open attention item (e.g. `do_not_call_request`, `identity_denied`) shows a warning badge but stays callable *(my call)*. Above the table is a **"Call to" dropdown** listing only numbers already verified in Plivo, with the developer's number as the default. The verified numbers are kept in config. The dropdown sends the call to that number while acting out the chosen customer's scenario.
- **Call detail:** outcome, flags, summary, full transcript, recording player, and payment attempts.
- **Needs attention:** open items with a coloured badge for each reason, the notes, a link to the call, and a **Mark resolved** button.
- **No live transcript.** Status updates by refresh or polling once the call ends.
- **Reset demo data** button to reseed the database. *(My call: the scenarios need resetting between demo runs.)*

## 10. Runtime and setup

- One local FastAPI process handles:
  - REST for the dashboard,
  - the Plivo answer-XML webhook,
  - the WSS media endpoint for Pipecat,
  - Plivo hangup and machine-detection callbacks.
- A tunnel URL goes in `.env` as `PUBLIC_BASE_URL`.
- `.env` holds Plivo auth id and token, the Plivo from-number, verified to-numbers, the default to-number, Deepgram / Sarvam / Cartesia / Gemini / Groq keys, and the TTS / LLM provider choice.
- The README covers:
  - Plivo signup, enabling India in geo permissions, adding sandbox numbers by OTP, and renting or choosing a caller ID,
  - getting each API key,
  - starting the tunnel, backend and frontend,
  - running a call,
  - the 10 scenarios and how to role-play each.
- **The first build step is a phone-path test** *(my call)*. Plivo trial to the developer's +91 number has not been tested yet; the trial terms are only "reported". So before any product work, the plan begins by placing a bare Pipecat + Plivo call (the official outbound example) to the +91 number and confirming two-way audio. If that call can't be made to work, the SmallWebRTC browser path below becomes the **main** demo path, not a fallback, and we go back to the user before continuing.
- **Fallback if the phone path fails on the day:** Pipecat's SmallWebRTC browser transport running the same bot. It's a last resort, so it's optional for version 1 if time is short.

## 11. Left to the builder

- Company and agent names, the exact voice, and the exact prompt wording.
- Tool names and signatures, within the rules above.
- Tunnel tool, frontend build tool (e.g. Vite), and styling.
- How call cost is obtained.
- Project layout, and tests: at minimum, unit tests for the fake payment service, the 5-day rule and the tool handlers.

## 12. Scope for the 11:00 demo (2026-11-18)

The demo is at 11:00 on 2026-11-18 and building starts at 01:30. The user approved cutting to the list below. It is unknown whether reviewers will run the repo or the interviewer will improvise, so we assume an **improvising interviewer**: the rules are enforced on the server, not only in the prompt. There is **no no-keys mode**; the README covers setup.

**Build, in this order:**
1. A bare Pipecat + Plivo outbound call to the +91 number with two-way audio (the risk gate).
2. The agent with every branch in §6, with the 5-day limit, partial payments, the identity gate and human-review flags all validated in the tool handlers.
3. The 10 seeded customers, the fake payment service, and SQLite tables for calls (outcome, transcript, flags, summary), payment attempts and attention items.
4. A React + TypeScript dashboard: customers with a Call button and the "Call to" dropdown, a call list and detail with the transcript, "Needs attention" with "Mark resolved", and a **Reset demo data** button.
5. The browser call (SmallWebRTC) as a safety net, using the same bot.
6. The README.

**Cut for today (later work):**
- call recordings and cost tracking,
- the Sarvam and Cartesia voices and the Groq fallback: one voice (Deepgram Aura) and one model (Gemini Flash),
- the Razorpay adapter: keep a simple `PaymentProvider` class without building the adapter,
- the "Run scheduled debits (simulate)" button,
- tests beyond the payment and rule validation.
