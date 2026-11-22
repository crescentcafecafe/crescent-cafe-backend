"""Android channel routing used by the native app (channel ids must match the APK's NotificationChannels.kt)."""
import pytest

from app.core.config import get_settings
from app.services.push import android_channel_for


@pytest.mark.parametrize("event,channel", [
    ("order.new", "new_orders"), ("order.reminder", "new_orders"), ("assignment.offered", "new_orders"),   # unchanged behaviour
    ("chat.message", "messages"), ("promotion", "promotions"),
    ("assignment.cancelled", "delivery_updates"), ("delivery.rider_nearby", "delivery_updates"), ("delivery.otp_issued", "delivery_updates"),
    ("order.status_changed", "order_updates"), ("order.created", "order_updates"), ("order.alarm_stop", "order_updates"),
    ("payment.succeeded", "payments"), ("payment.failed", "payments"), ("refund.succeeded", "payments"), ("payment.mismatch", "payments"),
    ("assignment.reminder", "new_orders"), ("delivery.order_ready", "delivery_updates"), ("cash.collected", "delivery_updates"), ("cash.handover_due", "delivery_updates"),
    ("menu.availability_changed", "general"), (None, "general"), ("something.new", "general"),
])
def test_channel_mapping(event, channel):
    assert android_channel_for(event, get_settings()) == channel


def test_required_channel_ids_match_the_app():
    s = get_settings()
    assert s.fcm_android_channel_new_order == "new_orders" and s.fcm_android_channel_default == "general"
    assert s.fcm_android_channel_payments == "payments"
