from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import Asset, CheckOut

MAX_LOAN_DAYS = 30


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = [
            "id",
            "asset_tag",
            "name",
            "category",
            "status",
            "purchase_date",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class AssetDetailSerializer(AssetSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta(AssetSerializer.Meta):
        fields = AssetSerializer.Meta.fields + ["current_holder"]

    def get_current_holder(self, obj):
        open_checkout = (
            obj.checkouts.filter(returned_at__isnull=True)
            .select_related("employee")
            .first()
        )
        if open_checkout is None:
            return None
        return {
            "employee_code": open_checkout.employee.employee_code,
            "full_name": open_checkout.employee.full_name,
        }


class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        now = timezone.now()
        if value <= now:
            raise serializers.ValidationError("due_at must be in the future.")
        if value > now + timedelta(days=MAX_LOAN_DAYS):
            raise serializers.ValidationError(
                f"due_at must be no more than {MAX_LOAN_DAYS} days from now."
            )
        return value


class CheckOutSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.asset_tag", read_only=True)
    employee_code = serializers.CharField(
        source="employee.employee_code", read_only=True
    )

    class Meta:
        model = CheckOut
        fields = [
            "id",
            "asset_tag",
            "employee_code",
            "checked_out_at",
            "due_at",
            "returned_at",
            "condition_note",
        ]


class ReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    needs_maintenance = serializers.BooleanField(required=False, default=False)


class OverdueRowSerializer(serializers.Serializer):
    """Read-only rows for the overdue report. days_overdue comes from a DB
    annotation so every row is measured against the same instant."""

    asset_name = serializers.CharField(source="asset.name")
    asset_tag = serializers.CharField(source="asset.asset_tag")
    employee_code = serializers.CharField(source="employee.employee_code")
    employee_name = serializers.CharField(source="employee.full_name")
    due_at = serializers.DateTimeField()
    days_overdue = serializers.SerializerMethodField()

    def get_days_overdue(self, obj):
        return obj.overdue_for.days
