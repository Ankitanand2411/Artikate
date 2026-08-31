"""Tiny hand-rolled factories -- enough for these tests without adding
factory_boy as a dependency."""
import itertools
from datetime import timedelta

from django.utils import timezone

from tracker.models import Asset, CheckOut, Employee

_seq = itertools.count(1)


def make_asset(**kwargs):
    n = next(_seq)
    defaults = {
        "asset_tag": f"TAG-{n:04d}",
        "name": f"Asset {n}",
        "category": Asset.Category.LAPTOP,
        "purchase_date": timezone.now().date(),
    }
    defaults.update(kwargs)
    return Asset.objects.create(**defaults)


def make_employee(**kwargs):
    n = next(_seq)
    defaults = {
        "employee_code": f"EMP-{n:04d}",
        "full_name": f"Employee {n}",
        "email": f"emp{n}@example.com",
        "is_active": True,
    }
    defaults.update(kwargs)
    return Employee.objects.create(**defaults)


def make_checkout(asset=None, employee=None, checked_out_days_ago=1,
                  due_in_days=7, returned_days_ago=None, **kwargs):
    """checked_out_at is auto_now_add, so backdating happens via a
    queryset update after create."""
    now = timezone.now()
    asset = asset or make_asset(status=Asset.Status.CHECKED_OUT)
    employee = employee or make_employee()
    co = CheckOut.objects.create(
        asset=asset,
        employee=employee,
        due_at=now + timedelta(days=due_in_days),
        **kwargs,
    )
    updates = {"checked_out_at": now - timedelta(days=checked_out_days_ago)}
    if returned_days_ago is not None:
        updates["returned_at"] = now - timedelta(days=returned_days_ago)
    CheckOut.objects.filter(pk=co.pk).update(**updates)
    co.refresh_from_db()
    return co
