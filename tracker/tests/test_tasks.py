import pytest

from tracker.models import OverdueNotice
from tracker.tasks import flag_overdue_checkouts
from .factories import make_checkout

pytestmark = pytest.mark.django_db


def test_flag_overdue_creates_one_notice_per_overdue_checkout():
    overdue_1 = make_checkout(checked_out_days_ago=10, due_in_days=-3)
    overdue_2 = make_checkout(checked_out_days_ago=10, due_in_days=-1)
    make_checkout(checked_out_days_ago=1, due_in_days=5)  # not overdue
    make_checkout(checked_out_days_ago=9, due_in_days=-2, returned_days_ago=1)  # returned

    flag_overdue_checkouts()

    assert OverdueNotice.objects.count() == 2
    assert OverdueNotice.objects.filter(checkout=overdue_1).count() == 1
    assert OverdueNotice.objects.filter(checkout=overdue_2).count() == 1


def test_flag_overdue_is_idempotent_within_a_day():
    make_checkout(checked_out_days_ago=10, due_in_days=-3)

    flag_overdue_checkouts()
    flag_overdue_checkouts()  # second run same day: no duplicates

    assert OverdueNotice.objects.count() == 1
