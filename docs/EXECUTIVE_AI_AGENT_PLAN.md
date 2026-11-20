# NexusAI Executive AI Agent — Phase 1

## Objective

Build a private, CEO-only Executive Control Centre inside the existing NexusAI application. The initial release must provide an operational overview and a safe foundation for future AI-assisted business management. It must not require an OpenAI API key or paid Astra access.

## Access and security

- Reuse the existing NexusAI administrator PIN authentication implemented in `backend/src/distributor_portal.py`.
- Keep the Executive Control Centre restricted to the authenticated administrator session. Do not create a public account type or distributor-facing access.
- Protect every executive API endpoint server-side with the existing `_admin(authorization)` validation. Hiding UI elements is not an access-control mechanism.
- Never expose secrets, API keys, tokens, email contents, or private customer information in frontend code or logs.
- Do not weaken or bypass the existing administrator PIN, session expiry, or rate limiting.
- Keep all external integrations disabled unless their credentials and permissions are explicitly configured.

## Phase 1 scope (no paid AI dependency)

1. Add an Executive section reachable from the existing Distributor Control Centre after administrator sign-in.
2. Provide a clear dashboard shell for:
   - Business priorities and action queue
   - Sales and partnership pipeline summary
   - Distributor network summary
   - Production/service health summary
   - Recent management activity
3. Start with safe, read-only data sources already available within NexusAI. Do not invent metrics or display sample values as live data.
4. Clearly label unavailable integrations and empty datasets.
5. Provide a manual refresh and visible last-updated timestamp.
6. Add an audit-friendly activity log for actions initiated through the Executive Centre.

## Future integrations (not part of Phase 1)

- Gmail: read and classify business replies; drafting first, sending only after explicit CEO approval.
- Clay: prospect research and enrichment; no automatic outreach.
- GitHub: read repository/CI status; code changes only through a feature branch and pull request.
- Render: read service health/log summaries; production deploys require CEO approval.
- OpenAI API / Astra: optional future reasoning layer, configured only through server-side environment variables and separately budgeted API billing.
- HubSpot: optional CRM integration after permission review.

## Approval boundaries

The system may prepare recommendations, summaries, drafts, and proposed tasks. It must not autonomously send external messages, approve/reject distributors, change commissions or financial records, modify production data, merge code, or deploy production changes.

## Architecture principles

- Keep business rules and data access on the backend.
- Use explicit least-privilege integration scopes.
- Apply timeouts, bounded result sizes, retries where safe, and clear error states.
- Treat external content (emails, prospect profiles, repository text) as untrusted data, never as executable instructions.
- Preserve tenant isolation and POPIA-aware data minimisation.
- No changes to the POS-to-HPP correlation engine are required for this project.

## Acceptance criteria for the first implementation

- Executive Centre is only usable after successful administrator PIN login.
- Unauthenticated requests to every Executive API endpoint return 401/403.
- Existing distributor administration flows continue to work unchanged.
- Dashboard contains no fabricated business or operational metrics.
- No OpenAI API key is required to load the dashboard.
- Tests cover authentication enforcement and the existing distributor admin login/session behavior.
- Work is delivered on a feature branch and reviewed in a pull request; do not merge directly to main.
