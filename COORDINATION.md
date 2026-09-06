# Coordination & Autonomy Rules — Flight Management System
FastAPI + n8n dual-writer against one Supabase Postgres ledger.

## 1. Table ownership (sole writer per concern)
| Table | Writer(s) | Notes |
|---|---|---|
| flights, seat_classes | FastAPI (admin endpoints) | n8n reads; writes ONLY seat release in hold-expiry sweeper |
| bookings | FastAPI (create/confirm/cancel) | n8n writes ONLY: status held→expired, reminder_sent, fraud_checked |
| payments, refunds (create) | FastAPI | n8n writes ONLY escalated_at |
| waitlist | n8n (promotion) + FastAPI (join) | row-locking required (see §3) |
| notifications_log, fraud_flags | n8n only | |
| audit_log | both | every writer records its own actions |
| policy_docs | admin via SQL; embedded flags by n8n | |

## 2. Change detection: status/flag columns (polling)
Chosen over LISTEN/NOTIFY: n8n Schedule Triggers poll tables using flag
columns (reminder_sent, embedded, fraud_checked, escalated_at, sent_at,
hold_expires_at). Justification: n8n cloud cannot hold persistent LISTEN
connections; flags give idempotent, restartable jobs; every flag doubles
as an audit fact. Cost: up to one polling interval of latency —
acceptable for all scheduled concerns (reminders, sweeps, escalation).

## 3. Concurrency & conflict resolution
- Seat inventory: single-statement conditional updates
  (UPDATE ... WHERE available_seats >= N RETURNING) — overselling is
  structurally impossible; no check-then-act windows.
- Booking state transitions are one-way and atomic
  (held→confirmed, held→expired, →cancelled, each guarded by
  WHERE status = ...). A hold can only expire once; a booking can only
  cancel once (verified: 409 on double-cancel).
- FastAPI cancel vs n8n waitlist promotion on the same seat: both operate
  through the same atomic seat_classes counter; a released seat becomes
  visible to the next promotion poll — never assigned twice, because the
  counter is the single source of truth.
- Row locks (SELECT ... FOR UPDATE) used where read-then-write is
  unavoidable: seat-class adjustment, booking cancel.

## 4. Autonomy boundaries
Auto-approved (no human in the loop):
- check-in reminders, price-drop alerts, ops reports
- hold-expiry sweep (release of unpaid seats)
- refund escalation NOTICE to ops (notice only, no payout)
- waitlist promotion offers

Human sign-off required:
- sending any RAG-drafted customer answer (approved flag + approved_by
  recorded before Gmail send)
- refund payout beyond standard in-policy amounts
- schedule-change / denied-boarding compensation
- acting on a fraud flag (reviewed flag on fraud_flags)
- flight creation and cancellation (super_admin role only; ops_agent
  limited to schedule edits and seat-class adjustments)

## 5. Audit trail
Every FastAPI admin/booking action → audit_log with actor, actor_role,
before_state, after_state. Every n8n customer notification →
notifications_log with type and details. Fraud flags carry machine-readable
evidence (reasons JSONB) and the flagging system (flagged_by). Together
sufficient to reconstruct and justify any automated decision to a
regulator or complaint review.