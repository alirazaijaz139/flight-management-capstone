# Flight Management System — Capstone
FastAPI + n8n + Supabase Postgres + Pinecone + Gemini RAG + Gmail

A dual-writer flight management system: FastAPI handles all live,
transactional booking/admin operations; n8n independently runs scheduled
automation against the same Postgres ledger. An AI support agent answers
policy questions grounded in the customer's actual booking, gated by
human approval before any email is sent.

## Architecture
- **FastAPI** — flights admin (role-tiered), search, atomic booking with
  holds & idempotency keys, group booking (all-or-nothing), fare-branched
  cancellations & refunds, waitlist join. Every write audit-logged.
- **n8n (8 workflows)** — check-in reminders (timezone-correct),
  price-drop alerts (5% de-dup), refund escalation, ops reporting,
  hold-expiry sweeper, waitlist promotion (FOR UPDATE SKIP LOCKED,
  6-hour claim window), fraud scoring (JSONB evidence), Pinecone policy
  ingestion, RAG support agent with human approval gate.
- **Supabase Postgres** — single ledger of truth; CHECK constraints,
  FKs and enums enforce invariants below both writers.
- **Pinecone + Gemini** — policy docs embedded (768-dim); agent answers
  reflect the booking's actual fare type, not a generic match.

## Key proofs
- **Concurrency test (`concurrency_test.py`)**: 2 threads racing the last
  seat → exactly 1 win; 10 threads racing 3 seats → exactly 3 wins.
  No oversell possible by construction (conditional atomic UPDATE).
- **Idempotency**: duplicate booking requests return the same booking —
  verified during a real connection failure.
- **Approval gate**: RAG drafts stay in the DB until a human sets
  approved=true; only then does Gmail fire (3-run proof: gate holds /
  approval releases / no double-send).

## Coordination rules
See [COORDINATION.md](COORDINATION.md) — table ownership, polling
justification, conflict resolution, autonomy boundaries.

## Run locally
1. `python -m venv venv` && activate
2. `pip install -r requirements.txt`
3. Copy `.env.example` → `.env`, fill your Supabase connection string
4. `uvicorn main:app --reload` → docs at http://127.0.0.1:8000/docs
5. Stress test: `python concurrency_test.py` (stage inventory first — see file header)

## n8n workflows
Exported JSONs in `/n8n-workflows` (import into any n8n instance;
requires Postgres, Gmail, Pinecone, Gemini credentials).