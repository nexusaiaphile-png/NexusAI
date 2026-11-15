# NexusAI Executive Control Centre — MVP Plan

## Purpose
Provide the Founder/CEO with one authenticated workspace to review company activity, ask questions about NexusAI operations, and initiate approved business workflows.

## Existing platform constraints
- Production is hosted on Render; do not introduce Cloudflare.
- FastAPI application entry point: `backend.src.main:app`.
- PostgreSQL is used when `DATABASE_URL` is configured.
- Existing distributor admin authentication uses `NEXUSAI_ADMIN_PIN`; do not expose or reuse raw PIN values in browser code.
- Keep all work on a feature branch and submit a pull request; do not commit directly to `main`.
- Do not modify `operator_bot/`.

## MVP scope
1. Executive dashboard shell with department cards: Sales, Partnerships, Distributors, Engineering, Operations.
2. Assistant chat endpoint backed by the OpenAI Responses API. Model is configurable with `OPENAI_MODEL`; initial target is `gpt-6-astra`.
3. Server-side API key only, stored as Render environment variable `OPENAI_API_KEY`. Never expose it to the browser, logs, Git, or client responses.
4. Read-only company context first: approved NexusAI business profile, current product description, pricing policy, integration status, and explicitly selected operational summaries.
5. Tool/action registry with allowlisted functions and strict schemas. Start with read-only tools (dashboard summaries, deployment health, sales pipeline summaries where connectors are configured).
6. Approval queue for consequential actions. Sending external emails, approving distributors, changing financial records, modifying production settings, and deploying code must require explicit human approval.
7. Audit log for assistant requests, tool calls, approvals, outcomes, actor, timestamps, and errors. Do not log secrets or unnecessary personal data.

## Out of scope for the first release
- Autonomous email sending or replying.
- Autonomous distributor approvals or payments.
- Direct production deployments or destructive database actions.
- Making fraud, misconduct, or employee guilt determinations.
- Importing all Gmail/CRM content into model context by default.

## Security and privacy requirements
- Authenticate every executive API route using a server-validated admin session; never trust a client-side flag.
- Reuse existing authentication only through a small, explicit server-side integration; do not duplicate or weaken PIN/session validation.
- Enforce role and action scopes on the server, not in model prompts alone.
- Validate all tool arguments with Pydantic models and apply tenant/site access controls.
- Use data minimisation, POPIA-aware retention, and redaction for personal/contact information.
- Treat model output and retrieved external content as untrusted input.
- Record an approval before executing any consequential tool call.

## Suggested implementation sequence
1. Inspect current admin auth and frontend routing; design route protection.
2. Add isolated executive-agent module and configuration checks.
3. Add read-only context providers and audit schema.
4. Add chat UI and a read-only assistant loop.
5. Add approval queue and one low-risk workflow at a time.
6. Test with mocked OpenAI responses and connector failures; deploy only after review and explicit approval.

## Environment variables (future)
- `OPENAI_API_KEY` — secret, configured only in Render.
- `OPENAI_MODEL` — default `gpt-6-astra`.
- `EXECUTIVE_AGENT_ENABLED` — feature flag, default false until explicitly enabled.
- `EXECUTIVE_AGENT_MAX_OUTPUT_TOKENS` — bounded response size.
- `EXECUTIVE_AGENT_DAILY_BUDGET_USD` — optional usage guardrail.

## Definition of done for MVP
- Executive dashboard is protected by server-side authentication.
- Assistant can answer from approved, current NexusAI context.
- No external action executes without the required approval.
- Every tool call and approval is auditable.
- Missing API configuration fails safely with a clear admin-facing message.
- Automated tests cover auth denial, valid session, invalid tool arguments, approval enforcement, API failure, and audit recording.
