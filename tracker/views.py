from django.db import DatabaseError, connection, transaction
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.db.models.functions import Now
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import Conflict
from .models import Asset, CheckOut, Employee
from .serializers import (
    AssetDetailSerializer,
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutSerializer,
    OverdueRowSerializer,
    ReturnSerializer,
)

MAX_OPEN_CHECKOUTS = 3


class AssetViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Asset.objects.all().order_by("id")
    filterset_fields = ["status", "category"]
    search_fields = ["name", "asset_tag"]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AssetDetailSerializer
        return AssetSerializer


class CheckOutViewSet(viewsets.GenericViewSet):
    queryset = CheckOut.objects.all()
    serializer_class = CheckOutSerializer

    def create(self, request):
        params = CheckOutCreateSerializer(data=request.data)
        params.is_valid(raise_exception=True)  # rule 4 -> 400
        data = params.validated_data

        with transaction.atomic():
            # Lock order is always employee -> asset so two concurrent
            # requests can never acquire the same pair in opposite order
            # (deadlock). Locking the employee row also serialises the
            # open-checkout count in rule 3, which locking only the asset
            # would not protect (two different assets, same employee).
            try:
                employee = Employee.objects.select_for_update().get(
                    employee_code=data["employee_code"]
                )
            except Employee.DoesNotExist:
                raise NotFound("Unknown employee_code.")  # rule 8
            try:
                asset = Asset.objects.select_for_update().get(
                    asset_tag=data["asset_tag"]
                )
            except Asset.DoesNotExist:
                raise NotFound("Unknown asset_tag.")  # rule 8

            if not employee.is_active:  # rule 2
                raise ValidationError({"employee_code": "Employee is not active."})

            # Rule 1 + rule 7: this check runs AFTER the row lock is held.
            # The second of two simultaneous requests blocks on
            # select_for_update, then sees CHECKED_OUT here and gets 409.
            if asset.status != Asset.Status.AVAILABLE:
                raise Conflict("Asset is not available.")

            open_count = CheckOut.objects.filter(
                employee=employee, returned_at__isnull=True
            ).count()
            if open_count >= MAX_OPEN_CHECKOUTS:  # rule 3
                raise Conflict(
                    f"Employee already holds {MAX_OPEN_CHECKOUTS} open check-outs."
                )

            # Rule 5: both writes inside one transaction -> atomic.
            checkout = CheckOut.objects.create(
                asset=asset, employee=employee, due_at=data["due_at"]
            )
            asset.status = Asset.Status.CHECKED_OUT
            asset.save(update_fields=["status", "updated_at"])

        return Response(
            CheckOutSerializer(checkout).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="return")
    def return_asset(self, request, pk=None):
        params = ReturnSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        data = params.validated_data

        with transaction.atomic():
            try:
                checkout = CheckOut.objects.select_for_update().get(pk=pk)
            except CheckOut.DoesNotExist:
                raise NotFound("Unknown check-out.")

            if checkout.returned_at is not None:  # rule 6
                raise Conflict("Check-out already returned.")

            asset = Asset.objects.select_for_update().get(pk=checkout.asset_id)

            checkout.returned_at = timezone.now()
            checkout.condition_note = data["condition_note"]
            checkout.save(update_fields=["returned_at", "condition_note"])

            asset.status = (
                Asset.Status.MAINTENANCE
                if data["needs_maintenance"]
                else Asset.Status.AVAILABLE
            )
            asset.save(update_fields=["status", "updated_at"])

        return Response(CheckOutSerializer(checkout).data, status=status.HTTP_200_OK)


class EmployeeSummaryView(APIView):
    def get(self, request, employee_code):
        try:
            employee = Employee.objects.get(employee_code=employee_code)
        except Employee.DoesNotExist:
            raise NotFound("Unknown employee_code.")

        now = timezone.now()
        # One aggregate query; every number is computed by PostgreSQL.
        summary = CheckOut.objects.filter(employee=employee).aggregate(
            lifetime_checkouts=Count("id"),
            currently_held=Count("id", filter=Q(returned_at__isnull=True)),
            currently_overdue=Count(
                "id", filter=Q(returned_at__isnull=True, due_at__lt=now)
            ),
            mean_hold=Avg(
                ExpressionWrapper(
                    F("returned_at") - F("checked_out_at"),
                    output_field=DurationField(),
                ),
                filter=Q(returned_at__isnull=False),
            ),
        )
        mean_hold = summary.pop("mean_hold")
        summary["mean_hold_days"] = (
            round(mean_hold.total_seconds() / 86400, 2)
            if mean_hold is not None
            else None
        )
        summary["employee_code"] = employee.employee_code
        return Response(summary)


class OverdueReportView(ListAPIView):
    serializer_class = OverdueRowSerializer

    def get_queryset(self):
        # select_related keeps this at one query for the page (rule: no
        # query per row); the annotation computes overdue duration in the
        # database against a single NOW() so rows are consistent.
        return (
            CheckOut.objects.filter(
                returned_at__isnull=True, due_at__lt=Now()
            )
            .select_related("asset", "employee")
            .annotate(
                overdue_for=ExpressionWrapper(
                    Now() - F("due_at"), output_field=DurationField()
                )
            )
            .order_by("due_at")  # earliest due first == most overdue first
        )


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            db_ok = True
        except DatabaseError:
            db_ok = False
        body = {"status": "ok" if db_ok else "degraded", "database": db_ok}
        return Response(
            body,
            status=status.HTTP_200_OK
            if db_ok
            else status.HTTP_503_SERVICE_UNAVAILABLE,
        )
