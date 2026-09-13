"""Tests for the paths where a student's money is at stake.

Run against a throwaway SQLite database, never the real one:

    DATABASE_URL=sqlite:///test.db DJANGO_DEBUG=True python manage.py test outlets

Every Razorpay call is patched out. That is only safe because the test
runner builds its own empty database — the same patch pointed at the
production database once marked a real order refunded when no money moved.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from . import views
from .models import Location, Order, Restaurant, StudentProfile


def fake_refund(order):
    order.razorpay_refund_id = "rfnd_testonly"
    order.payment_status = Order.PAYMENT_REFUNDED
    return True, None


def failing_refund(order):
    return False, "Refund failed: insufficient balance"


@patch("outlets.views.send_order_rejected_email", lambda *a, **k: None)
@patch("outlets.views.send_order_push", lambda *a, **k: None)
class UnansweredOrderSweepTests(TestCase):
    def setUp(self):
        views._last_global_sweep = 0.0
        loc = Location.objects.create(name="Block A")
        self.owner_a = User.objects.create_user("owner_a", password="x")
        self.owner_b = User.objects.create_user("owner_b", password="x")
        self.outlet_a = Restaurant.objects.create(name="Outlet A", location=loc, owner=self.owner_a,
                                                  upi_id="a@upi")
        self.outlet_b = Restaurant.objects.create(name="Outlet B", location=loc, owner=self.owner_b)
        self.student = User.objects.create_user("stud", password="x")
        StudentProfile.objects.create(user=self.student)
        self.admin = User.objects.create_superuser("root", password="x")
        self.client = APIClient()

    def paid_order(self, outlet, paid_minutes_ago, status=Order.STATUS_PLACED, total="26.50"):
        order = Order.objects.create(
            restaurant=outlet, student=self.student, student_name="stud",
            total_amount=Decimal(total), platform_fee=Decimal("1.50"),
            status=status, payment_status=Order.PAYMENT_PAID, razorpay_payment_id="pay_x",
        )
        paid_at = timezone.now() - timedelta(minutes=paid_minutes_ago)
        # .update() so auto_now doesn't stamp updated_at with "now", which
        # would look like a just-failed attempt to the backoff check.
        Order.objects.filter(pk=order.pk).update(payment_confirmed_at=paid_at, updated_at=paid_at)
        order.refresh_from_db()
        return order

    def test_other_outlets_overdue_order_is_refunded_from_any_dashboard(self):
        stranded = self.paid_order(self.outlet_b, paid_minutes_ago=10)
        self.client.force_authenticate(self.owner_a)
        with patch("outlets.views.refund_order_payment", side_effect=fake_refund):
            self.assertEqual(self.client.get("/api/me/orders/").status_code, 200)
        stranded.refresh_from_db()
        self.assertEqual(stranded.status, Order.STATUS_REJECTED)
        self.assertEqual(stranded.payment_status, Order.PAYMENT_REFUNDED)
        self.assertTrue(stranded.auto_declined)

    def test_order_inside_its_window_is_left_alone(self):
        fresh = self.paid_order(self.outlet_b, paid_minutes_ago=1)
        with patch("outlets.views.refund_order_payment", side_effect=fake_refund) as refund:
            views.sweep_all_unanswered_orders(force=True)
        refund.assert_not_called()
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, Order.STATUS_PLACED)

    def test_sweep_is_rate_limited_between_polls(self):
        views.sweep_all_unanswered_orders()  # uses up this interval
        self.paid_order(self.outlet_b, paid_minutes_ago=10)
        with patch("outlets.views.refund_order_payment", side_effect=fake_refund) as refund:
            views.sweep_all_unanswered_orders()
        refund.assert_not_called()

    def test_student_history_poll_refunds_their_own_overdue_order(self):
        mine = self.paid_order(self.outlet_b, paid_minutes_ago=10)
        views._last_global_sweep = 10**12  # prove the student's own sweep does it, not the global one
        self.client.force_authenticate(self.student)
        with patch("outlets.views.refund_order_payment", side_effect=fake_refund):
            body = self.client.get("/api/students/orders/").json()
        mine.refresh_from_db()
        self.assertEqual(mine.payment_status, Order.PAYMENT_REFUNDED)
        self.assertEqual(body[0]["payment_status"], Order.PAYMENT_REFUNDED)

    def test_failed_refund_keeps_money_owed_and_shows_on_admin_stats(self):
        stuck = self.paid_order(self.outlet_b, paid_minutes_ago=10)
        self.client.force_authenticate(self.admin)
        with patch("outlets.views.refund_order_payment", side_effect=failing_refund):
            body = self.client.get("/api/admin/stats/").json()
        stuck.refresh_from_db()
        # Never marked rejected without the money actually going back.
        self.assertEqual(stuck.status, Order.STATUS_PLACED)
        self.assertEqual(stuck.payment_status, Order.PAYMENT_PAID)
        self.assertEqual([r["order_code"] for r in body["stuck_refunds"]], [stuck.order_code])

    def test_admin_stats_hidden_from_non_superusers(self):
        self.client.force_authenticate(self.owner_a)
        self.assertEqual(self.client.get("/api/admin/stats/").status_code, 403)

    def test_report_payout_is_accepted_sales_less_platform_fee(self):
        self.paid_order(self.outlet_a, 30, status=Order.STATUS_COMPLETED, total="101.50")
        self.paid_order(self.outlet_a, 20, status=Order.STATUS_PREPARING, total="51.50")
        rejected = self.paid_order(self.outlet_a, 10, status=Order.STATUS_REJECTED, total="41.50")
        Order.objects.filter(pk=rejected.pk).update(payment_status=Order.PAYMENT_REFUNDED)
        self.client.force_authenticate(self.admin)
        body = self.client.get("/api/admin/report/").json()
        outlet = next(r for r in body["restaurants"] if r["restaurant_name"] == "Outlet A")
        self.assertEqual(Decimal(outlet["payout"]), Decimal("150.00"))
        self.assertEqual(outlet["upi_id"], "a@upi")
        self.assertEqual(Decimal(body["totals"]["payout"]), Decimal("150.00"))

    def test_platform_fee_is_two_percent_rounded_to_paise(self):
        self.assertEqual(Order.platform_fee_for(Decimal("100")), Decimal("2.00"))
        self.assertEqual(Order.platform_fee_for(Decimal("26.50")), Decimal("0.53"))
        self.assertEqual(Order.platform_fee_for(Decimal("12.25")), Decimal("0.25"))
        self.assertEqual(Order.platform_fee_for(Decimal("0")), Decimal("0.00"))

    def test_partial_upi_patch_does_not_wipe_it(self):
        self.client.force_authenticate(self.owner_a)
        self.assertEqual(self.client.patch("/api/me/restaurant/upi-id/", {}, format="json").status_code, 400)
        self.outlet_a.refresh_from_db()
        self.assertEqual(self.outlet_a.upi_id, "a@upi")
