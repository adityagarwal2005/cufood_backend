"""Tests for the paths where a student's money is at stake.

Run against a throwaway SQLite database, never the real one:

    DATABASE_URL=sqlite:///test.db DJANGO_DEBUG=True python manage.py test outlets

Every Razorpay call is patched out. That is only safe because the test
runner builds its own empty database — the same patch pointed at the
production database once marked a real order refunded when no money moved.
"""
import base64
import json
import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import http_ece
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import views
from .models import (Location, Order, PushSubscription, Restaurant,
                     RestaurantPushSubscription, StudentProfile, StudentPushSubscription)


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

    def test_report_payout_is_food_sales_less_commission(self):
        self.paid_order(self.outlet_a, 30, status=Order.STATUS_COMPLETED, total="101.50")
        self.paid_order(self.outlet_a, 20, status=Order.STATUS_PREPARING, total="51.50")
        rejected = self.paid_order(self.outlet_a, 10, status=Order.STATUS_REJECTED, total="41.50")
        Order.objects.filter(pk=rejected.pk).update(payment_status=Order.PAYMENT_REFUNDED)
        self.client.force_authenticate(self.admin)
        body = self.client.get("/api/admin/report/").json()
        outlet = next(r for r in body["restaurants"] if r["restaurant_name"] == "Outlet A")
        # Food 100 + 50 = 150 (fees and the refunded order excluded), less
        # the 1% commission.
        self.assertEqual(Decimal(outlet["food_sales"]), Decimal("150.00"))
        self.assertEqual(Decimal(outlet["commission"]), Decimal("1.50"))
        self.assertEqual(Decimal(outlet["payout"]), Decimal("148.50"))
        self.assertEqual(Decimal(outlet["days"][0]["payout"]), Decimal("148.50"))
        self.assertEqual(outlet["upi_id"], "a@upi")
        self.assertEqual(Decimal(body["totals"]["payout"]), Decimal("148.50"))
        self.assertEqual(Decimal(body["totals"]["earnings"]), Decimal("4.50"))

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


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# A throwaway VAPID key, in the same PEM form production stores its key in —
# which is exactly the format that used to make every send fail.
TEST_VAPID_PEM = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()


def browser_subscription():
    """What a browser hands the site when it subscribes: its public key and
    auth secret. The private half is kept so the test can decrypt the push
    exactly as the phone would."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = os.urandom(16)
    return key, auth, {"p256dh": b64url(public), "auth": b64url(auth)}


def capture_push_requests(status_code=201):
    sent = []

    def fake_post(url, data=None, headers=None, timeout=None, **kwargs):
        sent.append({"url": url, "data": data, "headers": {k.lower(): v for k, v in (headers or {}).items()},
                     "timeout": timeout})
        return MagicMock(status_code=status_code, text="", headers={})

    return sent, patch("requests.post", fake_post)


@override_settings(VAPID_PRIVATE_KEY=TEST_VAPID_PEM, VAPID_CLAIM_EMAIL="mailto:test@example.com")
@patch("outlets.views.send_order_confirmation_email", lambda *a, **k: None)
class PushDeliveryTests(TestCase):
    def setUp(self):
        views._vapid_signer = None
        loc = Location.objects.create(name="Block P")
        self.owner = User.objects.create_user("push_owner", password="x")
        self.outlet = Restaurant.objects.create(name="Push Outlet", location=loc, owner=self.owner)
        self.student = User.objects.create_user("push_stud", password="x")
        self.admin = User.objects.create_superuser("push_root", password="x")

    def tearDown(self):
        views._vapid_signer = None

    def new_order(self):
        return Order.objects.create(
            restaurant=self.outlet, student=self.student, student_name="push_stud",
            total_amount=Decimal("102.00"), platform_fee=Decimal("2.00"), razorpay_order_id="order_x",
        )

    def test_paid_order_alerts_owner_in_a_form_their_phone_can_decrypt(self):
        key, auth, keys = browser_subscription()
        RestaurantPushSubscription.objects.create(
            restaurant=self.outlet, endpoint="https://fcm.googleapis.com/fcm/send/owner", **keys)
        order = self.new_order()
        sent, capturing = capture_push_requests()
        with capturing:
            self.assertTrue(views.mark_order_paid(order, "pay_1"))

        self.assertEqual(len(sent), 1)
        headers = sent[0]["headers"]
        self.assertEqual(str(headers["ttl"]), str(views.OWNER_PUSH_TTL_SECONDS))
        self.assertEqual(headers["urgency"], "high")
        self.assertTrue(headers["authorization"].startswith("vapid "))
        self.assertEqual(headers["content-encoding"], "aes128gcm")
        payload = json.loads(http_ece.decrypt(sent[0]["data"], private_key=key, auth_secret=auth,
                                              version="aes128gcm"))
        self.assertEqual(payload["title"], "New order!")
        self.assertEqual(payload["kind"], "new_order")
        self.assertEqual(payload["tag"], f"new-{order.order_code}")
        self.assertIn("₹100.00", payload["body"])  # the outlet's share, not the student's total

    def test_student_update_is_held_for_a_sleeping_phone(self):
        key, auth, keys = browser_subscription()
        order = self.new_order()
        PushSubscription.objects.create(order=order, endpoint="https://web.push.apple.com/stud", **keys)
        sent, capturing = capture_push_requests()
        with capturing:
            views.send_order_push(order, "Order accepted", "Push Outlet is preparing your order.")
        headers = sent[0]["headers"]
        self.assertEqual(str(headers["ttl"]), str(views.STUDENT_PUSH_TTL_SECONDS))
        self.assertEqual(headers["urgency"], "high")
        payload = json.loads(http_ece.decrypt(sent[0]["data"], private_key=key, auth_secret=auth,
                                              version="aes128gcm"))
        self.assertEqual(payload["title"], "Order accepted")
        self.assertEqual(payload["url"], f"/order-status.html?code={order.order_code}")

    def test_revoked_subscriptions_are_cleaned_up(self):
        for code in (404, 410):
            _k, _a, keys = browser_subscription()
            RestaurantPushSubscription.objects.create(
                restaurant=self.outlet, endpoint=f"https://fcm.googleapis.com/fcm/send/{code}", **keys)
            sent, capturing = capture_push_requests(status_code=code)
            with capturing:
                views.send_owner_push(self.outlet, "New order!", "x", tag="t")
            self.assertEqual(RestaurantPushSubscription.objects.count(), 0)

    def test_admin_sees_which_outlets_have_alerts_on(self):
        _k, _a, keys = browser_subscription()
        RestaurantPushSubscription.objects.create(
            restaurant=self.outlet, endpoint="https://fcm.googleapis.com/fcm/send/a", **keys)
        client = APIClient()
        client.force_authenticate(self.admin)
        alerts = client.get("/api/admin/stats/").json()["outlet_alerts"]
        self.assertIn({"restaurant_name": "Push Outlet", "devices": 1}, alerts)


@override_settings(VAPID_PRIVATE_KEY=TEST_VAPID_PEM, VAPID_CLAIM_EMAIL="mailto:test@example.com")
class StudentAccountPushTests(TestCase):
    """Students sign a phone up once, on their account, and hear about every
    order after — which is what makes notifications work in the app, where
    a per-order sign-up is cut off by the Razorpay redirect."""

    URL = "/api/students/push/subscribe/"

    def setUp(self):
        views._vapid_signer = None
        loc = Location.objects.create(name="Block S")
        self.outlet = Restaurant.objects.create(name="S Outlet", location=loc)
        self.student = User.objects.create_user("acct_stud", password="x")
        StudentProfile.objects.create(user=self.student)
        self.client = APIClient()

    def tearDown(self):
        views._vapid_signer = None

    def order(self):
        return Order.objects.create(restaurant=self.outlet, student=self.student, student_name="acct_stud",
                                    total_amount=Decimal("51.00"), platform_fee=Decimal("1.00"))

    def test_one_signup_covers_every_later_order(self):
        key, auth, keys = browser_subscription()
        self.client.force_authenticate(self.student)
        body = {"endpoint": "https://fcm.googleapis.com/fcm/send/stud", "keys": keys}
        self.assertEqual(self.client.post(self.URL, body, format="json").status_code, 201)
        self.assertEqual(self.client.post(self.URL, body, format="json").status_code, 201)  # app reopened
        self.assertEqual(StudentPushSubscription.objects.count(), 1)
        for order in (self.order(), self.order()):
            sent, capturing = capture_push_requests()
            with capturing:
                views.send_order_push(order, "Order accepted", "S Outlet is preparing your order.")
            self.assertEqual(len(sent), 1)
            payload = json.loads(http_ece.decrypt(sent[0]["data"], private_key=key, auth_secret=auth,
                                                  version="aes128gcm"))
            self.assertEqual(payload["tag"], f"order-{order.order_code}")

    def test_phone_signed_up_both_ways_is_notified_once(self):
        _k, _a, keys = browser_subscription()
        endpoint = "https://fcm.googleapis.com/fcm/send/both"
        order = self.order()
        PushSubscription.objects.create(order=order, endpoint=endpoint, **keys)
        StudentPushSubscription.objects.create(user=self.student, endpoint=endpoint, **keys)
        sent, capturing = capture_push_requests()
        with capturing:
            views.send_order_push(order, "Ready for pickup!", "Go collect it.")
        self.assertEqual(len(sent), 1)

    def test_signup_needs_a_student_and_a_well_formed_subscription(self):
        _k, _a, keys = browser_subscription()
        good = {"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": keys}
        self.assertIn(self.client.post(self.URL, good, format="json").status_code, (401, 403))
        self.client.force_authenticate(User.objects.create_user("owner_not_student", password="x"))
        self.assertEqual(self.client.post(self.URL, good, format="json").status_code, 404)
        self.client.force_authenticate(self.student)
        for bad in ({"endpoint": "http://insecure.example", "keys": keys},
                    {"endpoint": good["endpoint"], "keys": "not-an-object"},
                    {"endpoint": "https://x.example/" + "a" * 600, "keys": keys},
                    {"endpoint": good["endpoint"], "keys": {"p256dh": "", "auth": "x"}},
                    {}):
            self.assertEqual(self.client.post(self.URL, bad, format="json").status_code, 400, bad)
        self.assertEqual(StudentPushSubscription.objects.count(), 0)

    def test_order_link_signup_rejects_malformed_keys_instead_of_crashing(self):
        order = self.order()
        resp = self.client.post(f"/api/orders/{order.order_code}/subscribe/",
                                {"endpoint": "https://fcm.googleapis.com/x", "keys": "oops"}, format="json")
        self.assertEqual(resp.status_code, 400)


class UnlistedOutletTests(TestCase):
    """An outlet that isn't a partner is hidden everywhere a student could
    find or order from it, without deleting it."""

    def setUp(self):
        self.location = Location.objects.create(name="Food Republic Test")
        self.listed = Restaurant.objects.create(name="Listed Outlet", location=self.location)
        self.hidden = Restaurant.objects.create(name="Hidden Outlet", location=self.location, is_listed=False)
        for outlet in (self.listed, self.hidden):
            outlet.menu_items.create(name="Masala Chai", category="Tea", price=Decimal("20.00"))
        self.client = APIClient()

    def test_hidden_outlet_is_not_listed_or_counted(self):
        names = [r["name"] for r in self.client.get(f"/api/restaurants/?location={self.location.slug}").json()]
        self.assertEqual(names, ["Listed Outlet"])

    def test_hidden_outlet_page_is_not_found(self):
        self.assertEqual(self.client.get(f"/api/restaurants/{self.listed.slug}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/restaurants/{self.hidden.slug}/").status_code, 404)

    def test_hidden_outlet_is_not_in_search(self):
        results = self.client.get(f"/api/search/?location={self.location.slug}&q=chai").json()
        self.assertEqual([r["restaurant_name"] for r in results], ["Listed Outlet"])

    def test_hidden_outlet_cannot_take_an_order(self):
        student = User.objects.create_user("hidden_outlet_stud", password="x")
        StudentProfile.objects.create(user=student)
        self.client.force_authenticate(student)
        resp = self.client.post("/api/orders/create/", {
            "restaurant_slug": self.hidden.slug,
            "items": [{"menu_item_id": self.hidden.menu_items.first().id, "quantity": 1}],
        }, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_hidden_outlet_is_left_out_of_admin_alerts(self):
        admin = User.objects.create_superuser("hidden_outlet_root", password="x")
        self.client.force_authenticate(admin)
        names = [r["restaurant_name"] for r in self.client.get("/api/admin/stats/").json()["outlet_alerts"]]
        self.assertIn("Listed Outlet", names)
        self.assertNotIn("Hidden Outlet", names)
