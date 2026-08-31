from celery import shared_task
from django.utils import timezone

from .models import CheckOut, OverdueNotice

BATCH_SIZE = 500


@shared_task
def flag_overdue_checkouts():
    """Create today's OverdueNotice for every open, overdue check-out.

    Idempotency is enforced by the database, not by this function: the
    unique constraint on (checkout, notice_date) plus ignore_conflicts
    means running this five times in a day still leaves one notice per
    check-out. IDs are streamed with .iterator() and inserted in batches
    so the task stays flat in memory at tens of thousands of rows.
    """
    today = timezone.localdate()
    overdue_ids = CheckOut.objects.filter(
        returned_at__isnull=True, due_at__lt=timezone.now()
    ).values_list("id", flat=True)

    processed = 0
    batch = []
    for checkout_id in overdue_ids.iterator(chunk_size=BATCH_SIZE):
        batch.append(OverdueNotice(checkout_id=checkout_id, notice_date=today))
        if len(batch) >= BATCH_SIZE:
            OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)
            processed += len(batch)
            batch = []
    if batch:
        OverdueNotice.objects.bulk_create(batch, ignore_conflicts=True)
        processed += len(batch)

    return f"processed {processed} overdue check-outs for {today.isoformat()}"
