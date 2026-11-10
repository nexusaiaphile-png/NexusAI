# NexusAI Global Distributor Portal

## Product decisions

- **Access:** public self-registration, followed by NexusAI administrator approval.
- **Rewards:** performance-based. A referral or contact submission does not earn a reward.
- **Design:** premium dark-navy interface with restrained neon-teal accents.
- **Separation:** distributor identities, referrals and reward records are separate from installer partner accounts, installer deployments and the existing proposed R100-per-eligible-camera installer incentive.

## Routes

| Route | Purpose |
|---|---|
| `/distributors` | Public programme information, registration and sign-in |
| `/distributors/admin` | Restricted administrator interface |
| `/api/distributors/register` | Public distributor application |
| `/api/distributors/login` | Approved distributor authentication |
| `/api/distributors/me` | Current distributor's dashboard and own referrals/rewards |
| `/api/distributors/referrals` | Submit an installer referral with contact permission |
| `/api/distributors/logout` | Revoke current session |
| `/api/admin/distributors` | List applications and performance totals |
| `/api/admin/distributors/{id}/status` | Approve, suspend or reactivate a distributor |
| `/api/admin/distributors/referrals` | Review referral pipeline |
| `/api/admin/distributors/referrals/{id}/review` | Record contact, qualification, conversion or rejection |
| `/api/admin/distributors/rewards` | View reward ledger |
| `/api/admin/distributors/rewards/{id}/pay` | Record a completed payment |

## Data model

- `nexusai_distributor_accounts`: company identity, account status and password hash.
- `nexusai_distributor_sessions`: hashed bearer tokens, expiry and revocation.
- `nexusai_distributor_referrals`: installer referral, consent confirmation, attribution, status and verified camera count.
- `nexusai_distributor_rewards`: one reward ledger record per converted referral, amount, payment status and reference.

The schema is initialized by the existing FastAPI startup path and supports the repository's SQLite development mode and PostgreSQL `DATABASE_URL` deployment mode. No existing installer or retail tables are altered.

## Performance reward controls

Referral lifecycle: `SUBMITTED → CONTACTED → QUALIFIED → CONVERTED` (or `REJECTED`).

Only an administrator can mark a referral `CONVERTED`. Conversion requires at least one verified camera and an explicit total reward amount. This creates a pending reward record. The administrator must record the signed distributor agreement version and a secure document reference before activating an account. Conversion is available only after the referral reaches `QUALIFIED`, and the administrator records the verified camera count and approved reward amount. A reward cannot be marked `PAID` unless an agreement record exists and a payment reference is supplied. The portal does not promise a fixed distributor rate, calculate a reward from raw leads, or auto-pay funds. The amount and qualifying milestone must be agreed in signed written terms before a distributor is eligible.

The installer must agree to be introduced before a distributor submits personal business contact information. Duplicate contact email submissions are rejected across the distributor referral pool. Each distributor API session can read only that distributor's own records.

## Security notes

- Passwords use PBKDF2-HMAC-SHA256 with a per-password random salt.
- Session tokens are random; only SHA-256 token hashes are stored. Sessions expire after 12 hours and can be revoked on suspension or logout.
- Registration and login have in-process IP rate limits. For multi-instance production scaling, move rate limiting to a shared store.
- Administrator endpoints require `NEXUSAI_ADMIN_KEY` in the request header. The admin UI keeps the key in page memory only; never place it in frontend source, Git, or a public page.
- The distributor portal does not expose HPP credentials, retailer POS data, camera feeds or other distributors' records.
- The initial release does not send emails. Applicants see a pending state and must wait for administrator approval.
- Add formal privacy notice, retention schedule, signed distributor agreement and POPIA review before collecting referrals at scale or paying commissions.

## Validation and release checklist

1. Run `python -m compileall backend/src`.
2. Run `pytest -q tests/test_distributor_portal.py` and the existing test suite.
3. Verify registration → pending rejection → admin approval → sign-in.
4. Verify one distributor cannot read another distributor's referral or reward records.
5. Verify contact permission is mandatory and duplicate installer contact referrals are rejected.
6. Verify no reward is created for a submitted, contacted or qualified lead.
7. Verify conversion requires verified camera count and an explicitly approved reward amount.
8. Verify only a pending reward can be marked paid and a payment reference is mandatory.
9. Deploy from the feature branch to a preview environment and complete an end-to-end test before merging to `main` or promoting the portal publicly.

## Current limitations / next hardening

- The public UI and API are implemented in the repository, but must be deployed and tested end-to-end before announcing availability.
- Rate limiting is process-local; use shared Redis/Valkey before horizontally scaling.
- Add email verification and notification workflows after SMTP/provider configuration is approved.
- Add an auditable agreement version and acceptance record before the reward programme is activated.
- Add administrator audit history and immutable reward adjustments before real payments.
- Confirm whether the portal should use a dedicated subdomain after preview acceptance.
