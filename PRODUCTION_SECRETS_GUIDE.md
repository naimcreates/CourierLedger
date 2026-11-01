# CourierLedger — Production Secrets Guide

This guide is the secrets inventory, rotation record, and handling rules for CourierLedger.
It contains **no secret values** — only boolean/count facts and procedures. Never add a value
to this file.

## Secrets inventory

| Secret | Where it lives | Who uses it | Rotation |
|---|---|---|---|
| Local-dev DB password (both roles) | Gitignored `m1/backend/.env` (`DATABASE_URL`); GitHub repo secret `CI_POSTGRES_PASSWORD` | Local PostgreSQL roles `courierledger` (owner) and `courierledger_runtime` (app); CI service containers | Rotated in Phase 1 (2026-11-01) — see below |
| `SECRET_KEY` / `SESSION_SECRET` | Gitignored `m1/backend/.env` (local dev); must come from the secret manager in production | FastAPI app (session signing) | **Not rotated in Phase 1** — local-dev values only; production must use fresh random values from the secret manager |
| `CI_POSTGRES_PASSWORD` | GitHub repo secret | GitHub Actions workflows (`ci.yml`, `ci-selfhosted.yml`) | Rotated in Phase 1 (2026-11-01) — same value as the local-dev DB password |
| `POSTGRES_PASSWORD` (compose) | Environment variable, fail-closed (`${POSTGRES_PASSWORD:?... must be set}`) | `docker-compose.yml` db service | No default — must be set per environment |
| Password-reset tokens | DB `password_reset_tokens` (SHA-256 hash only, never the raw token) | Password-reset flow | Per-token, 1-hour TTL; prior unused tokens invalidated on re-issue (Phase 1) |
| Session tokens | DB `user_sessions` (SHA-256 hash only) | Cookie sessions | Per-session, 7-day max age; revocable via `revoked_at` (Phase 1) |
| CSRF tokens | Cookie `cl_csrf` + in-memory JS variable | Double-submit CSRF | Rotated at login (Phase 1); 7-day max age |

**No production secrets exist yet.** There is no production deployment, no production database,
no email provider, and no secret manager configured. Nothing in this inventory is a production
credential.

## Rotation status — Phase 1 (2026-11-01)

**What was rotated:** one fresh random password (`secrets.token_urlsafe(32)`, 43 chars,
value never printed or written to a file) was set in all three stores:
1. GitHub repo secret `CI_POSTGRES_PASSWORD` (`gh secret set`, exit code 0).
2. Both local PostgreSQL roles — `courierledger` and `courierledger_runtime` — reset to it
   as the postgres superuser.
3. Gitignored `m1/backend/.env` `DATABASE_URL` updated to match.

**Boolean verification (values never printed):**
- Old credential invalid? **Yes** — the old documented placeholder value now fails auth
  for both roles (boolean False/False).
- Roles rotated? **Yes** — both roles reset to the fresh value; both roles authenticate
  against both DBs (`courierledger_app`, `courierledger_dev`) with the new value
  (boolean True/True/True/True).
- Any reuse? **No** — the fresh value is used only for the two local-dev DB roles and the
  CI secret; it is not used anywhere else, and it is not the same as any previously
  committed value.
- Production credential committed? **No** — no production credential was ever committed
  (no production credential exists).
- Secrets in the current tree? **No literal values.** Boolean grep counts: the historical
  10-char literal — 0 occurrences in the current tree (replaced with
  `<your-local-db-password>` in 3 test files, 5 occurrences); the 19-char fallback
  literal — 0 occurrences in both workflow files, `m1/LOCAL_VERIFICATION.md`,
  `m1/IMPLEMENTATION_CHECKLIST.md`; the documented placeholder string — 0 occurrences in
  `m1/README.md`, `m1/backend/README.md`, `m1/IMPLEMENTATION_CHECKLIST.md`,
  `m1/LOCAL_VERIFICATION.md`, both workflow files. Remaining "documented dev placeholder"
  phrases in `m1/IMPLEMENTATION_CHECKLIST.md:85,122,212` are historical narrative (no value).
  The only secret-bearing file in the tree is gitignored `m1/backend/.env` (never staged).

**What was NOT rotated in Phase 1:** `SECRET_KEY` / `SESSION_SECRET` (local-dev values only;
production must use fresh random values from the secret manager — see below). The
`courierledger_migrator` and `courierledger_maintenance` roles have no passwords set
(NOLOGIN); they are provisioned without credentials and must be given vault-managed
credentials only when used.

**Incident note (first rotation attempt):** the first `gh secret set` succeeded but the
role-reset step failed (venv permission error, exit code 126) and the in-memory value was
lost, so a second fresh value was generated and all three stores were completed with it.
No value was ever written to a file or printed. The GitHub secret therefore holds the
second value, matching the local roles and `.env`.

**Re-rotation (2026-11-01, after a container replacement):** the VM/container was replaced
mid-task and the PostgreSQL cluster was wiped (fresh cluster, no roles, no databases).
The cluster was re-provisioned from scratch per `m1/LOCAL_VERIFICATION.md` §1–2 with the
then-current rotated value. During that rebuild, a helper script echoed a command line
containing a prefix of that value into an agent tool log. To invalidate the partially
exposed value, a **third** fresh random password (`secrets.token_urlsafe(32)`, 43 chars)
was generated and set in all three stores (GitHub repo secret, both DB roles, gitignored
`.env`), and verified: both roles authenticate against both DBs with the new value
(boolean True/True/True/True). The partially exposed value is now invalid. No value was
printed in the re-rotation step.

## Historical credential exposure assessment

- **What was exposed:** two local-dev-only values were in the repository:
  (a) the historical committed password from commit `6c6c03b` (10 chars) — present in git
  history and, until Phase 1, in 3 test files as example env values; (b) the 19-char
  literal fallback password in both workflow files and two docs; (c) the live local-dev
  password was the publicly documented placeholder string.
- **What it guarded:** local-dev PostgreSQL roles on this VM and CI service containers,
  holding synthetic data only. Never a production database; never real merchant data.
- **Current status:** the exposed values are **invalid** for both DB roles (boolean-verified
  above); both roles were **rotated** to a fresh random value (boolean-verified above);
  the values appear **nowhere** in the current tree (boolean-verified above).
- **What remains:** the historical value stays in git history (see the history-cleanup plan
  below). It is invalid and guards nothing, but its presence in history is a hygiene issue.

## `.env` handling

- `m1/backend/.env` is **gitignored** and must stay gitignored. Never stage it, never print
  its contents, never copy it into a shared location.
- The app refuses to start in production with a placeholder `SECRET_KEY` or
  `SESSION_SECRET` (existing guard in `main.py`) and now also refuses to start in
  production with `SESSION_COOKIE_SECURE=false` (Phase 1 guard).
- The startup log now states whether `.env` was loaded (`dotenv_loaded` boolean) — if it
  is `false`, the app is running on compiled-in defaults, which include placeholder
  credentials; do not trust that state.
- `docker-compose.yml` fails closed if `POSTGRES_PASSWORD`, `SECRET_KEY`,
  `SESSION_SECRET`, or the `DATABASE_URL` password is unset (`${VAR:?... must be set}`).
  There is no `CHANGE_ME` default anywhere in compose any more.
- Example passwords in docs use obviously-fake markers (`<choose-a-password>`,
  `<your-local-db-password>`) or `${VAR}` references — never a real-looking string.

## Production secrets management (to do before any production deployment)

1. **Choose a secret manager** (e.g. the hosting provider's secret store, or HashiCorp Vault /
   AWS Secrets Manager / Doppler — no choice has been made). Do not store production secrets
   in git, in `.env` files committed anywhere, in CI workflow files, or in chat.
2. **Generate fresh production values** for: DB owner password, DB runtime-role password,
   `SECRET_KEY`, `SESSION_SECRET`, and the email-provider API key. Each must be a fresh
   random value, distinct from every local-dev value and from every value that has ever
   appeared in git history.
3. **Set `ENVIRONMENT=production`**, `FRONTEND_URL` to the exact production HTTPS origin,
   `SESSION_COOKIE_SECURE=true` (enforced by the startup guard).
4. **CI:** `CI_POSTGRES_PASSWORD` is a CI-only secret; it must equal the current role
   password for self-hosted runs. The self-hosted workflow now uses dedicated CI-only
   databases (`courierledger_ci_app` / `courierledger_ci_dev`) and never resets shared role
   passwords. The GitHub-hosted workflow uses a service container with the secret.
5. **Rotation cadence:** rotate production DB and app secrets on a schedule (at least every
   90 days) and immediately after any suspected exposure. Record each rotation here with
   boolean-only details (date, what was rotated, verification that the old value is invalid).
6. **Password reset:** choose an email provider before production; the reset link must be
   emailed and must **never** be logged. The log-only reset path is local-dev only.

## History-cleanup plan (APPROVAL-GATED — NOT executed)

The historical local-dev DB password (commit `6c6c03b`, and the initial commit `fe913b5`
test files) remains in git history. It is **invalid** (both roles rotated, boolean-verified)
and guards only synthetic-data local databases, so this is a hygiene issue, not an active
exposure. Rewriting history is destructive and must NOT be done without Naim's explicit
approval.

**Safe plan (only with approval):**
1. **Backup first:** `git clone --mirror` the repository to a backup location; verify the
   backup contains all branches and tags; keep it until the cleanup is verified.
2. **Affected history:** the literal appears in commit `6c6c03b` (first `ci-selfhosted.yml`
   version, which contained a literal local-dev DB password) and in the initial commit
   `fe913b5` test files (example env values). Also confirm with
   `git log -S <value> --oneline` (run by the person doing the cleanup, value never
   printed) that no other commit carries it.
3. **Rewrite:** use `git filter-repo` (preferred) or `git filter-branch` with
   `--replace-text` on a file containing the literal, on a fresh clone. This rewrites
   every commit hash after the earliest affected commit.
4. **Consequences:** all commit hashes change; the remote `main` must be force-pushed
   (normally forbidden — this is the one exception, and only with explicit approval);
   every collaborator must re-clone; any open PRs/CI runs referencing old hashes break;
   the GitHub Actions billing-locked runs are unaffected (they fail for billing reasons,
   not code reasons).
5. **Verify after:** clone fresh, grep the full history for both literal values (boolean
   counts must be 0), verify the current tree still builds and the full test suite passes,
   verify the remote HEAD matches the rewritten history.
6. **Alternative (no rewrite):** leave history as-is, since the value is invalid and
   local-dev-only. Document the decision here. This is the recommended option unless Naim
   wants a clean history for other reasons (e.g. before making the repo public).

## What must never be committed

- Any real password, token, API key, or secret value — in code, workflow files, docs,
  test files, or comments. Use `${VAR}` references and `<placeholder>` markers.
- `m1/backend/.env` or any `.env` file containing real values (all `.env` files are
  gitignored; `.env.example` templates must contain placeholders only).
- The contents of the Secure Vault, or any credential captured through it.
- Log output containing tokens, reset links, or session cookies.
