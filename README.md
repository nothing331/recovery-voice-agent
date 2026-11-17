# Autopay Recovery Voice Agent

A voice agent that places a **real outbound phone call** to a customer whose autopay failed, confirms who it is talking to, explains the failure, and tries to recover the payment. Outcomes are saved and shown on a React dashboard.

It runs against 10 fictional customer records. Payments are faked, so every demo is deterministic. This is an interview demo, not a production system.

## Architecture

```
                        +-----------------------------+
  Dashboard (React) --> |  FastAPI  (localhost:8000)  |
  Vite :5173, /api      |  REST · Plivo webhooks ·    |
                        |  WSS media · /client        |
                        +--------------+--------------+
                                       |
                 +---------------------+----------------------+
                 |                     |                      |
          Pipecat pipeline        Fake PaymentProvider    SQLite
   Deepgram STT -> Gemini -> Deepgram TTS   (per-customer   (customers, calls,
   (Silero VAD, tools)                      results)         payment_attempts,
                 |                                            attention_items)
                 v
  Plivo REST: place call  -->  phone (+91)  <--  Plivo answer XML <Stream>
                                                 streams audio to our WSS
  PUBLIC_BASE_URL = cloudflared tunnel -> http://localhost:8000
```

## Stack

| Layer | Choice |
|---|---|
| Telephony | Plivo (trial), outbound REST call, answer XML `<Stream>` over WebSocket |
| Voice pipeline | Pipecat (Python), Silero VAD |
| Speech-to-text | Deepgram |
| Text-to-speech | Deepgram Aura (`aura-2-thalia-en`) |
| LLM | Google Gemini Flash (`gemini-3.6-flash`) |
| Backend | Python 3.12, FastAPI, SQLite |
| Frontend | React + TypeScript, Vite |
| Browser fallback | Pipecat SmallWebRTC with the prebuilt client at `/client` |
| Tunnel | Cloudflare quick tunnel (`cloudflared`) |

## Quick start

### 1. Accounts and keys

1. **Plivo:** sign up for a trial account. Under **Voice → Geo Permissions**, enable **India**. Under **Phone Numbers → Sandbox Numbers**, add each destination number and confirm it with the OTP. Trial calls only reach verified numbers. Note your Auth ID, Auth Token and a Plivo number to call from.
2. **Deepgram:** create a key at console.deepgram.com. New accounts include $200 of free credit.
3. **LLM:** default is OpenRouter (`LLM_PROVIDER=openrouter`): create a key at openrouter.ai and set `OPENROUTER_API_KEY`; `OPENROUTER_MODEL` defaults to `google/gemini-2.5-flash`. To use Gemini directly, set `LLM_PROVIDER=google` and `GOOGLE_API_KEY` (aistudio.google.com).

**Caller ID:** Plivo trial calls come from a foreign number, so the recipient's phone shows **"International call"**. This is expected for the demo. Warn anyone receiving a call.

### 2. Configure

```bash
cp backend/.env.example backend/.env
# fill in PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_FROM_NUMBER,
# VERIFIED_NUMBERS, DEFAULT_TO_NUMBER, DEEPGRAM_API_KEY, OPENROUTER_API_KEY
```

### 3. Expose the backend (needed for calls)

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```

Copy the `https://….trycloudflare.com` URL into `PUBLIC_BASE_URL` in `backend/.env`, then restart the backend. Plivo fetches call instructions and streams audio through this URL, so it must stay running during calls. The URL changes each time the tunnel restarts.

**Security:** requests that arrive through the tunnel (Cloudflare headers, or a non-localhost `Host`) can only reach `/plivo/*`; the dashboard, transcripts, dialing and reset are local-only (404 via the tunnel). Plivo callbacks must carry a valid `X-Plivo-Signature-V3` (computed against `PUBLIC_BASE_URL`; set `PLIVO_VERIFY_SIGNATURES=false` to disable if it misbehaves), and the media WebSocket needs a per-call token. There is no `/dev/test-call` endpoint; calls go through the dashboard, which enforces the verified-number list.

### 4. Run

```bash
# Backend
cd backend && uv sync && uv run uvicorn app.main:app --port 8000

# Frontend (second terminal; Vite proxies /api to :8000)
cd frontend && npm install && npm run dev
```

Open the dashboard at the Vite URL (default http://localhost:5173).

**Browser fallback:** if the phone path fails, click **Talk in browser** on a customer row, a browser tab opens at `/client/?sid=…` (single-use, valid 5 minutes); click **Connect** there. Each click creates its own session bound to that customer's call, so multiple tabs do not mix up cases. Open the dashboard locally (not via the tunnel). This runs the same bot over WebRTC, with no phone needed.

**Tests:** `cd backend && uv run pytest -q`

## Demo script

1. Open the dashboard and click **Reset demo data**.
2. Choose a verified number in the **Call to** dropdown (the default is your own phone).
3. Click **Call** on **Rohan Mehta** (scenario 1). Answer, play the persona, and confirm the name. Expect `paid`.
4. Repeat for **Priya Nair** (scenario 2) and **Karthik Menon** (scenario 7, the fraud case, which blocks future debits).
5. Check each outcome under **Calls** (transcript, payment attempts) and **Needs attention**. Use **Mark resolved** on a flagged item.

## Scenarios

Each customer has a persona for whoever answers. Use the persona from the table. Ask the agent to confirm your name, then follow the script.

| # | Customer | Situation | How to role-play | Expected outcome |
|---|---|---|---|---|
| 1 | Rohan Mehta | Bank technical decline | Cooperative. Agree to a retry. | `paid` |
| 2 | Priya Nair | Low balance | Salary arrives in 3 days. Ask to pay then. | `scheduled` |
| 3 | Arjun Reddy | Low balance | Can pay about half now, rest in 4 days. | `partial_paid_and_scheduled` |
| 4 | Sneha Iyer | Card expired | Cooperative. Say the card on file has expired. | Flag `needs_card_update` |
| 5 | Vikram Singh | Card lost | Slightly worried. Say the card was lost last week. | Flag `needs_card_update` |
| 6 | Ananya Das | Low balance | Needs 10 days to pay. | Flag `extension_over_5_days` |
| 7 | Karthik Menon | Bank decline | Say "I never signed up for this." | Flag `fraud`, debits blocked |
| 8 | Deepa Joshi | Low balance | Get angry, then abusive. | Flag `abuse`, call ends politely |
| 9 | Manoj Kulkarni | Low balance | Lost job recently. Accept a short extension if offered, otherwise hardship. | `scheduled` or flag `hardship` |
| 10 | Neha Kapoor | Low balance | A spouse answers and says the customer is not available. | Flag `identity_denied` with a note |

Also test by asking "am I talking to a bot?" during any call. The agent should say it is an automated assistant and continue. Ask it to stop calling you to see the `do_not_call_request` flag, which goes to a human and does not opt the customer out.

## Design decisions

Full rationale is in [the design spec](docs/superpowers/specs/2026-11-18-autopay-recovery-voice-agent-design.md).

- **Identity by name only (§6.2).** The agent asks "Am I speaking with <full name>?" and asks for no PIN or date of birth. A bad-faith caller could claim not to know a secret, so a secret adds friction without adding safety. Amounts and account details are not revealed until the name is confirmed.
- **Identity denial and do-not-call go to human review (§6.2).** If someone says they are not the customer, the agent shares nothing and flags `identity_denied`. A "don't call me again" request is logged with `do_not_call_request`. The agent does not opt the customer out, because a defaulting customer could use that request to stop collection.
- **Five-day schedule limit enforced on the server (§6.3).** `schedule_debit` and `take_partial_payment` validate the 5-day limit in the tool handlers, not only in the prompt. The agent cannot promise a date beyond 5 days, even if it tries.
- **Fake payment provider for deterministic demos (§6.4).** Each customer has preset results (for example, retry succeeds for Rohan and fails with `insufficient_funds` for Priya). The provider sits behind a `PaymentProvider` interface, so a real adapter (Razorpay / UPI AutoPay) can be added without changing the agent.
- **Fixed action set.** The agent has exactly three payment actions and a fixed set of flag reasons. Everything else is flagged for a human and the call ends politely (§6.2).

## Known limits

- **Cut for v1 (§12):** call recordings and cost tracking, the Sarvam and Cartesia voices, the Groq fallback, the Razorpay adapter, the "Run scheduled debits (simulate)" button, and tests beyond payment and rule validation. Only one TTS voice and one LLM are used.
- **International caller ID:** trial calls show as an international number. Production use would need a local caller ID and TRAI/DLT registration.
- **Free tiers:** Plivo trial (about $10 credit, verified numbers only, reportedly valid for 3 weeks), Deepgram $200 free credit, and Gemini free-tier rate limits. A slow or exhausted provider can be replaced by changing config.
- **Not production-ready:** no dashboard auth, no consent management, no live transcript during calls, no automatic retries of unanswered calls. Everything runs on a laptop, and the tunnel URL changes on restart.
- **Compliance:** this demo ignores TRAI/DLT rules. Do not use it on real customers.
- **Identity by name only (by design):** the agent confirms identity by the customer's name alone. Production use needs a merchant-approved verification step.
- **"Don't call me" handling:** the request goes to human review and the customer stays callable, but the dashboard requires explicit confirmation ("Call anyway?", enforced by the backend) while the request is open. Production use needs immediate suppression of automated calls on opt-out.
