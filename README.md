# Field Asset Check-Out Service

Internal REST API tracking physical equipment checked out to and returned by
employees. Django 5.1 / DRF / PostgreSQL 15 / Celery + Redis.

Answers to Parts B, C and D are in [ANSWERS.md](ANSWERS.md).

## Quick start (Docker)

```bash
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data
```

To start from a genuinely empty database (this is what the screen recording
shows), wipe the volume first:

```bash
docker compose down -v
docker compose up --build -d
```

`seed_demo_data` prints an API token on its last line. Every endpoint except
`/health/` requires it. Copy the token itself — no angle brackets:

```bash
# health (unauthenticated)
curl http://localhost:8000/api/v1/health/

# paste the 40-character token printed by seed_demo_data
TOKEN=b0d9f54ee7809287d1492173796dc80a51d67640

# list assets
curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/v1/assets/

# check out an available asset
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d '{"asset_tag":"SEN-001","employee_code":"EMP-001","due_at":"2026-09-10T00:00:00Z"}' \
  http://localhost:8000/api/v1/checkouts/

# run that same command again -> 409, the asset is now CHECKED_OUT

# inactive employee (EMP-004 is seeded inactive) -> 400
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d '{"asset_tag":"SEN-002","employee_code":"EMP-004","due_at":"2026-09-10T00:00:00Z"}' \
  http://localhost:8000/api/v1/checkouts/

# return it -- substitute the numeric id from the 201 response
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
  -d '{"condition_note":"all good","needs_maintenance":false}' \
  http://localhost:8000/api/v1/checkouts/7/return/

# employee summary / overdue report
curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/v1/employees/EMP-001/summary/
curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/v1/reports/overdue/
```

Piping through `jq` (`sudo apt install jq`) makes the JSON readable:
`curl -s ... | jq`.

A fresh token can also be obtained at `POST /api/v1/auth/token/` with
`{"username": "demo", "password": "demo-password"}`, or by re-printing the
existing one with `manage.py drf_create_token demo`.

## Running the tests

```bash
docker compose exec web pytest
```

(Locally: `pip install -r requirements.txt`, point `POSTGRES_HOST` at a
running PostgreSQL, then `pytest`.) 20 tests, including a real threaded
concurrency test — they need PostgreSQL, not SQLite, by design.

## Endpoints

All under `/api/v1/`, token/session auth required except `/health/`,
lists paginated at 20.

| Method & path | Behaviour |
|---|---|
| `POST /assets/` | Create an asset |
| `GET /assets/` | List; filters `status`, `category`; `search` over name/tag |
| `GET /assets/{id}/` | Detail incl. `current_holder` |
| `POST /checkouts/` | `{asset_tag, employee_code, due_at}` → 201 |
| `POST /checkouts/{id}/return/` | `{condition_note, needs_maintenance}` → 200 |
| `GET /employees/{code}/summary/` | Four numbers, single aggregate query |
| `GET /reports/overdue/` | Open & past due, most overdue first, no N+1 |
| `GET /health/` | Public; reports DB connectivity |

Note that `/assets/{id}/` takes the numeric primary key while
`/employees/{code}/summary/` takes the employee code (`EMP-001`), matching
the paths given in the specification.

## Background task

`tracker.tasks.flag_overdue_checkouts` creates one `OverdueNotice` per open
overdue check-out per day. Idempotency is enforced by the database — a
unique constraint on `(checkout, notice_date)` plus
`bulk_create(ignore_conflicts=True)` — so repeated runs cannot duplicate.
Scheduled hourly via Celery Beat (`beat` service in docker-compose,
Redis broker).

To run it by hand:

```bash
docker compose exec web python manage.py shell -c \
  "from tracker.tasks import flag_overdue_checkouts; print(flag_overdue_checkouts())"
```

## Design decisions & assumptions

- **Auth: DRF TokenAuth** (SessionAuth kept for browsable API). Chosen over
  JWT because this is an internal tool with no third-party clients; token
  revocation is a row delete and there is no refresh-token machinery to get
  wrong. Stated trade-off: no built-in expiry.
- **Concurrency (rule 7): `select_for_update` row locks**, acquired in a
  fixed order (employee → asset) inside one `transaction.atomic()`. The
  availability check runs *after* the lock, so the loser of a race blocks,
  re-reads `CHECKED_OUT`, and receives 409. Locking the employee row as well
  closes the *other* race the spec doesn't mention: two simultaneous
  check-outs of *different* assets by the same employee could both pass the
  3-open-checkouts count if only the asset were locked. The fixed lock order
  prevents deadlocks. Alternative considered: conditional
  `UPDATE ... WHERE status='AVAILABLE'` — rejected only because it doesn't
  cover the employee-count race, so locks were needed anyway.
- **"Overdue" means `due_at < now` strictly.** An item due at the exact
  query instant is not yet overdue; one second later it is, with
  `days_overdue = 0` (integer day truncation). Tests pin this down.
- **`days_overdue` is computed in the database** against a single `NOW()`
  (annotation), so all rows in a page are measured at the same instant.
- **Error precedence in check-out:** body validation (400) → unknown
  employee/asset (404) → inactive employee (400) → asset unavailable (409) →
  open-count limit (409). The spec doesn't prescribe an order; this one
  fails fastest and cheapest first.
- **Return endpoint stores `condition_note` on the check-out** (overwrites,
  not appends) and does not require the returning employee to match the
  holder — the spec's body has no employee field.
- **Employee summary**: employee existence check is one lookup (needed for
  the 404), then all four numbers come from a single `.aggregate()` query.
  `mean_hold_days` is `null` when the employee has no returned items.
- **Asset `status` is read-only through the serializer.** It changes only as
  a side effect of check-out and return, so an asset's status can never
  drift out of sync with its check-out history.
- **Seed command is safely re-runnable** rather than strictly no-op
  idempotent: assets/employees are `get_or_create`d; demo check-outs are
  rebuilt each run so the documented counts stay exact. `checked_out_at` is
  `auto_now_add`, so backdating uses queryset `update()` after create.
- **Extra model fields:** none added beyond the spec.
- **`beat` runs as its own compose service** (fifth service beyond the four
  required) rather than `worker -B`, so worker scaling never risks duplicate
  schedulers.

## Known gaps

- **Credentials in `docker-compose.yml` and `.env.example` are development
  defaults**, chosen so the stack comes up with zero configuration. In
  production these would come from the environment or a secrets manager,
  `DJANGO_SECRET_KEY` would be injected rather than defaulted, and `DEBUG`
  pinned off.
- **`collectstatic` is not run in the image**, so the DRF browsable API
  renders unstyled with `DEBUG=0`. It has no effect on the JSON API. The fix
  is a `RUN python manage.py collectstatic --noinput` line in the Dockerfile
  plus a static file server; left out because nothing in the assessment
  depends on the browsable UI.
- No rate limiting and no structured logging or metrics — out of scope for
  the time budget but first on the list for real production.
- The employee open-count uses a row lock rather than a database constraint;
  a belt-and-braces partial unique index (`UNIQUE(asset_id) WHERE
  returned_at IS NULL`) would make asset double-checkout impossible even for
  future code that forgets the lock. I would add it in a follow-up.
- Celery task failure handling is default (no retry policy configured);
  acceptable because the task is idempotent and rescheduled hourly.
- The browsable API is enabled; fine internally, would disable for prod.

## Screen recording

Video walkthrough: [Google Drive – demo recordings](https://drive.google.com/drive/folders/1CA7LS_9ouR_OKnCpYE-qeSygFWHIHZNC)

## Project layout

```
config/          settings, urls, celery app
tracker/         models, serializers, views, tasks, urls
tracker/management/commands/seed_demo_data.py
tracker/tests/   business rules, concurrency, summary/report, tasks
Dockerfile, docker-compose.yml (db, redis, web, worker, beat)
ANSWERS.md       Parts B, C, D
```
