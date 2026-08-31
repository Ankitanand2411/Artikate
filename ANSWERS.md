# ANSWERS.md

---

# Part B — Diagnosing the three broken snippets

## Snippet 1 — overdue report view

### 1. What is wrong

1. **N+1 queries.** `c.asset.name` and `c.employee.full_name` inside the loop issue two extra queries per row. With 10,000 open check-outs that is ~20,001 queries for one page load.
2. **Filtering in Python instead of the database.** All open check-outs are fetched, then the overdue test runs in Python. The database could reduce the set with `due_at__lt=now` before a single row crosses the wire.
3. **`timezone.now()` is called per row (and again per row for the arithmetic).** Rows are compared against a drifting clock; on a slow pass a row can be "not overdue" at the top of the loop and overdue by the time a later row is measured. All rows should be measured against one captured instant.
4. **`.days` truncates toward zero.** An item 23 hours overdue reports `days_overdue = 0` — indistinguishable from "just now due". Fine if intended, but it should be a decision, not an accident (and the sort key collapses everything under 24h into one bucket).
5. **Sorting in Python** over the whole materialised list, when `ORDER BY due_at` does it in the database, streamed.
6. **No pagination and no authentication** — a plain Django view returning every row to anyone.

### 2. Why it looks correct locally

Local databases have 20 rows, so 41 queries finish in milliseconds and N+1 is invisible. The clock-drift bug needs a large row count or a paused debugger to reproduce. Truncation with `.days` only shows up when someone checks an item overdue by hours. Auth is missing but local testing is usually done as a logged-in developer who never notices.

### 3. Corrected code

```python
from django.db.models import DurationField, ExpressionWrapper, F
from django.db.models.functions import Now

def overdue_report(request):  # in real code: DRF view + IsAuthenticated + pagination
    rows = (
        CheckOut.objects
        .filter(returned_at__isnull=True, due_at__lt=Now())
        .select_related("asset", "employee")
        .annotate(overdue_for=ExpressionWrapper(Now() - F("due_at"),
                                                output_field=DurationField()))
        .order_by("due_at")
        .values("asset__name", "asset__asset_tag",
                "employee__full_name", "overdue_for")
    )
    data = [
        {
            "asset": r["asset__name"],
            "asset_tag": r["asset__asset_tag"],
            "employee": r["employee__full_name"],
            "days_overdue": r["overdue_for"].days,
        }
        for r in rows
    ]
    return JsonResponse({"count": len(data), "rows": data})
```

One query, one consistent `NOW()` evaluated in the database, sorted by the database. (The Part A implementation additionally wraps this in DRF with pagination and auth.)

### 4. What would have caught it

- `assertNumQueries(1)` (or pytest-django's `django_assert_num_queries`) in the view's test — catches the N+1 immediately.
- `django-silk` or `nplusone` in development / CI middleware.
- A test fixture with an item overdue by 12 hours asserting it appears — catches nothing here (it does appear), but a test asserting **ordering across sub-day differences** exposes the truncated sort key.
- A load test (even `locust` with 5k seeded rows) makes the latency obvious before production does.

---

## Snippet 2 — check-out endpoint

### 1. What is wrong

1. **Race condition on availability (TOCTOU).** Two simultaneous requests both read `status == "AVAILABLE"`, both pass the check, both create a CheckOut. The check and the write are not atomic and there is no lock. Same race on the 3-open-checkouts count.
2. **No transaction.** `CheckOut.objects.create(...)` and `asset.save()` are separate autocommits. If the process dies (or `asset.save()` raises) between them, a CheckOut row exists next to an AVAILABLE asset — exactly the state rule 5 forbids. Worse: the checkout is created **before** the status flip, so the window is real.
3. **Bare `.get()` → 500 on unknown input.** `Asset.DoesNotExist` / `Employee.DoesNotExist` propagate as server errors instead of 404 (rule 8).
4. **`request.data["asset_tag"]` → `KeyError` → 500** on a missing field instead of 400.
5. **No validation of `due_at`.** Rule 4 is simply not implemented; a past date or a malformed string goes straight to the DB (malformed → `ValidationError` → 500 in this code path).
6. **No inactive-employee check.** Rule 2 is not implemented.
7. **`asset.save()` with no `update_fields`** rewrites every column, silently clobbering any concurrent change to other fields.

### 2. Why it looks correct locally

Races need genuinely concurrent requests; `runserver` in development plus one person clicking cannot produce them. The missing transaction only matters when a crash lands in a microsecond window. Manual testing always sends well-formed bodies with known tags, so the 500s never fire. Happy-path tests (the only kind this code has seen) pass every time.

### 3. Corrected code

This is essentially the Part A implementation (`tracker/views.py`), the core of it being:

```python
serializer = CheckOutCreateSerializer(data=request.data)  # KeyError/format/rule 4 -> 400
serializer.is_valid(raise_exception=True)
data = serializer.validated_data

with transaction.atomic():
    try:
        employee = Employee.objects.select_for_update().get(
            employee_code=data["employee_code"])
    except Employee.DoesNotExist:
        raise NotFound(...)                                   # rule 8 -> 404
    try:
        asset = Asset.objects.select_for_update().get(
            asset_tag=data["asset_tag"])
    except Asset.DoesNotExist:
        raise NotFound(...)

    if not employee.is_active:
        raise ValidationError(...)                            # rule 2 -> 400
    if asset.status != Asset.Status.AVAILABLE:                # checked AFTER the lock
        raise Conflict(...)                                   # rules 1 & 7 -> 409
    if CheckOut.objects.filter(employee=employee,
                               returned_at__isnull=True).count() >= 3:
        raise Conflict(...)                                   # rule 3 -> 409

    checkout = CheckOut.objects.create(asset=asset, employee=employee,
                                       due_at=data["due_at"])
    asset.status = Asset.Status.CHECKED_OUT
    asset.save(update_fields=["status", "updated_at"])        # rule 5: same txn
```

The essential points: the availability check happens **after** `select_for_update` acquires the row lock, so the loser of the race blocks, re-reads `CHECKED_OUT`, and 409s; both writes share one transaction; and locking the employee row serialises the open-count check (locking only the asset would leave rule 3 racy across two different assets). Locks are always taken employee→asset so two requests can never deadlock by acquiring the pair in opposite orders.

An equally valid lock-free alternative for the asset half is a conditional update — `Asset.objects.filter(pk=asset.pk, status="AVAILABLE").update(status="CHECKED_OUT")` and 409 if it returns 0 — but it does not protect the employee open-count, so I used row locks for both.

### 4. What would have caught it

- A **threaded concurrency test** (`django_db(transaction=True)`, two threads, a `Barrier`, assert exactly one 201) — this exact test exists in `tracker/tests/test_concurrency.py` and fails against the snippet.
- A test posting `{"asset_tag": "NOPE"}` asserting 404, and one posting `{}` asserting 400.
- Code review checklist item: "every multi-write invariant is inside `transaction.atomic()`".
- In production tooling terms: a DB-level `CHECK`/exclusion constraint or a partial unique index on open checkouts per asset (`UNIQUE (asset_id) WHERE returned_at IS NULL`) acts as a last line of defence that turns the race into an `IntegrityError` instead of corrupt data.

---

## Snippet 3 — nightly notice task

### 1. What is wrong

1. **Not idempotent / unsafe to retry.** `OverdueNotice.objects.create(...)` unconditionally inserts. If the task is retried after a partial failure (Celery retries are normal), every check-out processed before the failure gets a second notice — and a second email. Five runs a day = five notices.
2. **Duplicate emails are sent even without retries** if two workers pick up the schedule (Beat misconfiguration, or a stuck task past `visibility_timeout` being redelivered).
3. **Model instances passed to `.delay()`.** `deliver_email.delay(c.employee, c)` serialises Django model objects. With the JSON serializer it crashes outright; with pickle it "works" and ships **stale snapshots** — by the time the email task runs, the check-out may already be returned. Pass primary keys.
4. **Unbounded memory.** `for c in overdue:` materialises the whole queryset. At tens of thousands of rows (the stated growth path) this loads everything into worker memory at once; `.iterator()` streams it.
5. **`overdue.count()` re-runs the query after the loop.** The returned "sent N notices" number is computed at a different instant than the loop — new rows that became overdue mid-run inflate it, so the log lies. (It is also one extra full query.)
6. **Emails are dispatched before the notice insert is durable** and there is no transaction strategy at all: if the task dies mid-loop, some employees have an email but no notice row, others neither. Notices and their emails should be tied together — insert first, dispatch on commit (`transaction.on_commit`).
7. `timezone.now().date()` is called per row — mostly harmless, but a run that crosses midnight stamps rows with two different dates; capture it once.

### 2. Why it looks correct locally

Locally the task is run once, by hand, on a ten-row table: no retries, no concurrent workers, no memory pressure, and the count drift is invisible at that speed. The model-instance serialisation bug hides if local Celery runs with `task_always_eager=True` (nothing is actually serialised) — which is exactly how most people test Celery locally.

### 3. Corrected code

This is `tracker/tasks.py` in Part A, extended with the email dispatch:

```python
@shared_task
def send_overdue_notices():
    today = timezone.localdate()
    now = timezone.now()
    overdue_ids = (CheckOut.objects
                   .filter(returned_at__isnull=True, due_at__lt=now)
                   .values_list("id", flat=True))

    processed = 0
    for checkout_id in overdue_ids.iterator(chunk_size=500):
        with transaction.atomic():
            notice, created = OverdueNotice.objects.get_or_create(
                checkout_id=checkout_id, notice_date=today)
            if created:
                # queue the email only if THIS run created the notice,
                # and only once the row is committed
                transaction.on_commit(
                    lambda cid=checkout_id: deliver_email.delay(cid))
                processed += 1
    return f"sent {processed} notices for {today.isoformat()}"
```

Idempotency is guaranteed twice over: `get_or_create` keyed on (checkout, notice_date), backed by the **database unique constraint** on the same pair, so even two workers racing on the same row cannot double-insert — one of them hits the constraint. Retries re-run safely because already-noticed rows come back `created=False` and send nothing. The email task receives a primary key, not an object. (Where email isn't in the loop — the Part A version — batched `bulk_create(..., ignore_conflicts=True)` is cheaper than row-at-a-time `get_or_create`.)

### 4. What would have caught it

- A test that **runs the task twice and asserts one notice** (exists: `test_flag_overdue_is_idempotent_within_a_day`).
- A test that runs the task with `CELERY_TASK_ALWAYS_EAGER=False` semantics — i.e. asserting the arguments passed to `deliver_email.delay` are JSON-serialisable.
- Making the unique constraint exist at all: the schema review question "what stops two of these per day?" has no answer in the snippet's world unless the model constraint from Part A exists.
- A memory profiler (`memray`) or simply seeding 100k overdue rows in staging.

---

# Part C — Optimising the slow PostgreSQL query

## 1. Rewritten query

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at
FROM checkouts c
JOIN employees e ON e.id = c.employee_id AND e.is_active
WHERE c.checked_out_at >= timestamptz '2026-01-01 00:00:00+05:30'
  AND c.checked_out_at <  timestamptz '2026-07-01 00:00:00+05:30'
  AND c.returned_at IS NULL
ORDER BY c.due_at ASC;
```

Change by change:

- **`DATE(c.checked_out_at) BETWEEN ...` → half-open range on the raw column.** Wrapping the column in `DATE()` makes the predicate non-sargable: no index on `checked_out_at` can ever be used, so the planner is forced to scan and compute `DATE()` for 4.2M rows. The half-open form (`>= start AND < day-after-end`) is index-usable and avoids the classic BETWEEN off-by-one on the last day. There is also a **correctness** issue hiding here: `DATE()` on a `timestamptz` converts using the session's `TimeZone`, so the same query returns different row sets for clients in different timezones. The rewrite pins explicit timestamptz bounds — I have assumed business-local midnight (IST); that choice needs confirming with whoever owns the report.
- **`SELECT *` → explicit columns.** `condition_note` is unbounded text; dragging it through the sort and over the wire for a reporting screen costs real I/O (and possibly TOAST fetches per row). Also, `SELECT *` silently changes behaviour when columns are added later.
- **`IN (subquery)` → `JOIN ... AND e.is_active`.** Postgres usually optimises `IN` to a semi-join anyway, so this is often neutral — but the join form is explicit, lets the planner reorder freely, and reads honestly in review. (If duplicates were possible it would change semantics; `employees.id` is a PK, so it cannot.) `EXISTS` is equally good.
- **Cost of the changes:** the explicit column list must be maintained; the timezone-pinned bounds push a decision (whose midnight?) into the open — which is a gain disguised as a cost.

## 2. Indexes

```sql
CREATE INDEX CONCURRENTLY idx_checkouts_open_checked_out
    ON checkouts (checked_out_at)
    WHERE returned_at IS NULL;
```

This is the index that earns its place, and the reasoning is the interesting part:

- **Partial (`WHERE returned_at IS NULL`) is right here** because open check-outs are a small, roughly constant slice of the table. 12,000 employees × ≤3 open each caps open rows at ~36k out of 4.2M — under 1%. The partial index is ~100× smaller than a full one, stays hot in cache, and is cheaper to maintain on the 8,000 daily inserts (rows drop out of it when returned). A full composite `(checked_out_at, returned_at)` would be 4.2M entries deep to serve a query that can only ever return ≤36k rows.
- **`checked_out_at` as the key column** because it is the selective range predicate the rewrite made sargable.
- **Why I did *not* index `due_at` for the ORDER BY:** with ≤36k candidate rows after the index scan, an in-memory quicksort is microseconds; adding `due_at` to the index to get pre-sorted output doesn't pay because the leading range predicate on `checked_out_at` destroys the sort order anyway (a range on the first column means the second column is not globally ordered).
- **Why not an index on `employees(is_active)`:** 12,000 rows; the whole table is a handful of pages and likely already cached. A boolean index over it earns nothing.

If measurement (question 5) showed open rows were *not* a small fraction — say returns lag badly and 40% of rows are open — the partial index premise collapses and I would reconsider a composite.

## 3. Expected EXPLAIN (ANALYZE, BUFFERS) before and after

**Before:** `Seq Scan on checkouts` (or a parallel seq scan) with a filter line containing `date(checked_out_at)`, `Rows Removed by Filter` in the millions, `Buffers: shared read=` in the tens of thousands of pages, feeding an external merge `Sort` on `due_at` (possibly `Sort Method: external merge Disk: ...`), with the employees side as a hash semi-join. Total time ~8s dominated by the scan.

**After:** `Index Scan` (or bitmap heap scan) `using idx_checkouts_open_checked_out on checkouts`, actual rows in the low tens of thousands, `Buffers: shared hit=` a few hundred, `Sort Method: quicksort Memory: ...`, hash join to employees unchanged and cheap.

**The single line that proves the fix:** the scan node changing from `Seq Scan on checkouts / Rows Removed by Filter: ~4,150,000` to `Index Scan using idx_checkouts_open_checked_out` with actual rows ≈ the result size — that is the moment 4.2M rows stopped being touched.

## 4. What breaks first as the table grows

At +8k rows/day (~3M/year) the first thing to break is not this query — the partial index keeps it flat because *open* rows don't grow with table size. What breaks first is everything else that touches the full table: **any remaining seq-scan query, autovacuum duration, and backup/restore times** grow linearly; index bloat on the default FK indexes accumulates; and eventually the reporting screen's *other* queries (anything over historical, returned rows) degrade. Before that happens I would: (a) audit for other non-sargable predicates with `pg_stat_statements`, (b) **partition `checkouts` by range on `checked_out_at`** (monthly/quarterly), which caps scan and vacuum cost per partition and makes archiving old rows a `DETACH PARTITION` instead of a mega-DELETE, and (c) if the product allows it, move closed rows past a retention horizon into an archive table. Partitioning is a disruptive migration — the time to do it is while the table is 4M rows, not 40M.

## 5. The one thing to measure first

The actual selectivity of `returned_at IS NULL` — i.e. `SELECT count(*) FILTER (WHERE returned_at IS NULL), count(*) FROM checkouts` (plus the live `EXPLAIN (ANALYZE, BUFFERS)` of the current query). My entire index choice rests on the assumption that open rows are a tiny fraction of the table. That is implied by the domain (3-per-employee cap) but not guaranteed by the schema — nothing stops returns from lagging for months or the cap from being newer than the data. If open rows turned out to be 30% of the table, the partial index buys little and I would instead lead with the `checked_out_at` range in a full composite and reassess whether the sort needs index support. Without measuring, I would be optimising a table I imagined rather than the one in production.

---

# Part D — Production reasoning

## D1. Zero-downtime addition of a non-nullable `location_id` FK

**Three deploys.** The invariant throughout: every deployed code version must be correct against both the schema before and after the migration step it ships with, because four instances roll one at a time and in-flight requests overlap.

**Deploy 1 — additive schema, tolerant code.** Migration: `ADD COLUMN location_id bigint NULL` (nullable, **no default**) — on Postgres 15 this is a metadata-only change, taking a brief `ACCESS EXCLUSIVE` lock that queues but does not rewrite 4.2M rows. Add the FK as `NOT VALID` (checks new writes only, no full-table validation lock), and build the index with `CREATE INDEX CONCURRENTLY` — which cannot run inside Django's transactional migration, so it's a non-atomic migration (`atomic = False`). Code in this deploy *writes* `location_id` on every new/updated row but never assumes it is present on read. Old instances still running during the rollout simply don't write it — which is fine, the column is nullable.

**Deploy 2 — backfill.** A management command (not a migration) updates rows in batches of ~5–10k by PK range, sleeping between batches, so no long transaction, no lock pileup, and replication lag stays bounded. Then `VALIDATE CONSTRAINT` — takes only a `SHARE UPDATE EXCLUSIVE` lock, concurrent writes continue.

**Deploy 3 — enforce.** `ALTER COLUMN location_id SET NOT NULL`. On PG12+ this skips the full-table scan if a validated `CHECK (location_id IS NOT NULL)` constraint already exists — so: add that check `NOT VALID`, validate it (online), then `SET NOT NULL` (instant), then drop the redundant check. Code may now rely on the column.

**The specific thing that locks the table if done wrong:** `ADD COLUMN ... NOT NULL DEFAULT` was the classic full-rewrite (pre-PG11), but the live trap today is adding the FK or `SET NOT NULL` *without* the `NOT VALID`/pre-validated-check dance — a plain `ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY` or bare `SET NOT NULL` takes `ACCESS EXCLUSIVE` **and holds it while scanning 4.2M rows**, blocking every read and write behind it. Also worth naming: even the fast metadata `ALTER` queues behind any long-running query and everything queues behind *it* — so run DDL with a `lock_timeout` and retry.

## D2. Latency triage: 25s overnight, no deploy in nine days

Order of checks, and what each rules in/out:

1. **Is it this endpoint or everything?** Dashboard/APM p95 across endpoints. Everything slow → infrastructure (DB CPU, connection pool, noisy neighbour), stop looking at this view. Only this endpoint → data- or plan-shaped.
2. **`pg_stat_activity`** right now: is the endpoint's query itself slow, or is it *waiting* (locks, `wait_event`)? A pile of the same query stacked up → the query; waits on locks → someone/something holding a lock (a stuck migration, a backup tool, a long transaction from a batch job).
3. **Row counts.** `SELECT count(*) FROM checkouts WHERE returned_at IS NULL AND due_at < now()`. The overdue set is unbounded by design — if a batch of returns stopped being processed nine days ago, the open set balloons and a previously fine plan drowns. This checks in seconds and rules the "data grew" family in or out.
4. **`EXPLAIN (ANALYZE, BUFFERS)`** the exact production query. Compare the plan to the expected one: seq scan where an index scan used to be → plan flip.
5. **`pg_stat_user_tables`**: `n_dead_tup`, `last_autovacuum`, `last_autoanalyze` for `checkouts`. Dead tuples and stale stats explain both bloat and plan flips.
6. Cache hit ratio / instance memory graphs — did the working set fall out of cache overnight (instance resize, restart, another table's job evicting it)?

**Two most likely causes given no code changed:**
(a) **The data crossed a planner threshold** — organic growth or a stuck return-processing job pushed row estimates past the point where Postgres flipped from index scan to seq scan (or the overdue set genuinely 100×'d). Confirm: check 3's count plus the plan from check 4; history from `pg_stat_statements` shows when mean exec time stepped.
(b) **Autovacuum/autoanalyze fell behind** (long-running transaction pinned the xmin horizon, or a vacuum was cancelled repeatedly), leaving bloat and stale statistics → bad plan. Confirm: check 5's timestamps and dead-tuple counts; a manual `ANALYZE checkouts` that instantly fixes the plan is the smoking gun.

## D3. CI/CD on GitHub Actions

**On pull request:** lint (`ruff`), `python manage.py makemigrations --check --dry-run` (fails the build if models and migrations diverge — the single cheapest schema-safety gate), full test suite against real PostgreSQL and Redis service containers (not SQLite — the locking tests are meaningless on SQLite), and a `docker build`. Optionally `pip-audit`. Branch protection requires all green plus review.

**On merge to main:** build the production image once, tag with the git SHA, push to the registry, deploy to staging, run migrations there, and run a small smoke suite against staging (health, auth, one write path). The image that passed staging is byte-identical to what production gets — no rebuild.

**Gating production:** manual approval on a GitHub Environment (with required reviewers). The deploy job then runs in two distinct steps: **step 1 applies migrations** (a one-off job/container running `manage.py migrate` against prod, with a `lock_timeout` set), **step 2 rolls out the new code** instance-by-instance behind the load balancer with health checks. Migrations run *before* new code, which imposes the discipline that every migration must be backward-compatible with the *currently running* code — additive changes only; destructive changes (dropping a column, renaming) are split across releases exactly as in D1.

**Rollback story when the schema already moved:** code rolls back freely — redeploy the previous image tag, which works because the schema change was required to be backward-compatible with it. The schema itself is **not** rolled back: reverse migrations on a live database are where data goes to die (a dropped-then-recreated column is empty; a reversed backfill is unrecoverable). Instead we roll *forward* — ship a fix, or a new migration that neutralises the problem. The only exception is a migration that itself broke production *during* step 1, before any new code ran; those are additive by policy, so `migrate app previous_migration` is safe precisely because nothing depends on the addition yet. This is also why long-lived feature flags guard any code path that starts *using* a new schema element: turning the flag off is the real instant rollback.
