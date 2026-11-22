"""Branded HTML + plain-text e-mails. Every value that comes from a person is HTML-escaped."""
from __future__ import annotations

from html import escape as e

BRAND = "Crescent Cafe"
GOLD, BROWN, CREAM = "#d99a1f", "#2b1b0d", "#fff7eb"


def rupees(paise: int | None) -> str:
    return f"₹{(paise or 0) / 100:,.2f}"


def _layout(title: str, body_html: str, preheader: str = "") -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title></head>
<body style="margin:0;background:#f4eee4;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{BROWN};">
<span style="display:none;max-height:0;overflow:hidden;opacity:0;">{e(preheader)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4eee4;padding:24px 12px;"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #eadfcf;">
<tr><td style="background:{BROWN};padding:20px 28px;"><span style="font-family:Georgia,serif;font-size:24px;font-weight:bold;color:{GOLD};">&#9790; {BRAND}</span></td></tr>
<tr><td style="padding:28px;font-size:15px;line-height:1.6;">{body_html}</td></tr>
<tr><td style="background:{CREAM};padding:16px 28px;font-size:12px;color:#7a6a58;">This message was sent by {BRAND}. If you did not expect it, you can ignore it.</td></tr>
</table></td></tr></table></body></html>"""


def _h(text: str) -> str:
    return f'<h1 style="font-family:Georgia,serif;font-size:22px;margin:0 0 14px;color:{BROWN};">{e(text)}</h1>'


def _p(text: str) -> str:
    return f'<p style="margin:0 0 14px;">{e(text)}</p>'


def _row(label: str, value: str) -> str:
    return f'<tr><td style="padding:6px 0;color:#7a6a58;width:42%;">{e(label)}</td><td style="padding:6px 0;font-weight:600;">{e(value)}</td></tr>'


def _table(rows: list[tuple[str, str]]) -> str:
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 16px;border-top:1px solid #eadfcf;border-bottom:1px solid #eadfcf;">' + "".join(_row(a, b) for a, b in rows) + "</table>"


def welcome(name: str) -> tuple[str, str, str]:
    subject = f"Welcome to {BRAND}!"
    html = _layout(subject, _h(f"Welcome, {name}!") + _p("Your account is ready. You can now browse the menu, order your favourites and track your delivery live.") + _p("We are happy to have you with us."), "Your account is ready")
    return subject, html, f"Welcome, {name}!\n\nYour {BRAND} account is ready. You can now browse the menu, order and track your delivery.\n"


def password_otp(name: str, otp: str, minutes: int) -> tuple[str, str, str]:
    subject = f"{BRAND} - Password Reset OTP"
    big = f'<div style="font-size:34px;letter-spacing:10px;font-weight:bold;text-align:center;background:{CREAM};border:1px dashed {GOLD};border-radius:12px;padding:16px 0;margin:8px 0 18px;">{e(otp)}</div>'
    html = _layout(subject, _h("Reset your password") + _p(f"Hello {name}, use this one-time code to reset your password.") + big
                   + _p(f"The code expires in {minutes} minutes and can be used once.") + _p("Never share this code with anyone. Our team will never ask for it. If you did not request a reset, ignore this email and your password stays the same."), f"Your code is {otp}")
    return subject, html, f"Hello {name},\n\nYour {BRAND} password reset code is {otp}.\nIt expires in {minutes} minutes and can be used once.\nNever share this code. If you did not request it, ignore this email.\n"


def password_changed(name: str) -> tuple[str, str, str]:
    subject = f"{BRAND} - Your password was changed"
    html = _layout(subject, _h("Password changed") + _p(f"Hello {name}, your password was just changed and you were signed out of other devices.") + _p("If this was not you, reset your password again right away and contact the cafe."), "Your password was changed")
    return subject, html, f"Hello {name},\n\nYour {BRAND} password was changed. If this was not you, reset it again and contact the cafe.\n"


def _items_html(items: list[dict]) -> str:
    rows = "".join(f'<tr><td style="padding:6px 0;">{e(i["name"])} &times; {i["qty"]}</td><td style="padding:6px 0;text-align:right;font-weight:600;">{e(rupees(i["total"]))}</td></tr>' for i in items)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:6px 0 10px;border-top:1px solid #eadfcf;">{rows}</table>'


def _items_text(items: list[dict]) -> str:
    return "\n".join(f"  {i['name']} x {i['qty']}  {rupees(i['total'])}" for i in items)


def order_confirmation(o: dict) -> tuple[str, str, str]:
    subject = f"{BRAND} - Order Confirmation"
    rows = [("Order ID", o["order_number"]), ("Payment method", o["payment_method"]), ("Payment status", o["payment_status"]), ("Order status", o["status"])]
    if o.get("address"):
        rows.append(("Delivery address", o["address"]))
    if o.get("table"):
        rows.append(("Table", o["table"]))
    body = (_h(f"Thank you, {o['name']}!") + _p("We have received your order.") + _items_html(o["items"])
            + f'<p style="text-align:right;margin:0 0 14px;font-size:16px;"><b>Total {e(rupees(o["total"]))}</b></p>' + _table(rows))
    text = f"Thank you, {o['name']}! We have received your order.\n\n" + "\n".join(f"{a}: {b}" for a, b in rows) + "\n\n" + _items_text(o["items"]) + f"\n\nTotal: {rupees(o['total'])}\n"
    return subject, _layout(subject, body, f"Order {o['order_number']} received"), text


def order_accepted(o: dict) -> tuple[str, str, str]:
    subject = f"Your {BRAND} Order Has Been Accepted!"
    rows = [("Order ID", o["order_number"]), ("Total", rupees(o["total"])), ("Payment", f"{o['payment_method']} ({o['payment_status']})")]
    if o.get("address"):
        rows.append(("Delivery address", o["address"]))
    body = _h("Your order is accepted") + _p(f"Good news {o['name']}, the cafe accepted your order and will start preparing it.") + _table(rows)
    return subject, _layout(subject, body, f"Order {o['order_number']} accepted"), f"Good news {o['name']}, order {o['order_number']} was accepted by the cafe.\nTotal: {rupees(o['total'])}\n"


def order_on_the_way(o: dict) -> tuple[str, str, str]:
    subject = f"Your {BRAND} Order Is On The Way!"
    rows = [("Order ID", o["order_number"]), ("Delivery status", "Out for delivery")]
    if o.get("rider_name"):
        rows.append(("Your rider", o["rider_name"]))
    if o.get("collect_cash"):
        rows.append(("Amount to pay on delivery", rupees(o["total"])))
    body = _h("Your order is on the way") + _p(f"{o['name']}, your rider has picked up your order.") + _table(rows) + _p("Open the app or website to see your delivery code and track the rider live. Share the code only when you receive your order.")
    return subject, _layout(subject, body, f"Order {o['order_number']} is on the way"), f"{o['name']}, order {o['order_number']} is on the way" + (f" with {o['rider_name']}" if o.get("rider_name") else "") + ".\nOpen the app to see your delivery code.\n"


def order_delivered(o: dict) -> tuple[str, str, str]:
    subject = f"Your {BRAND} Order Has Been Delivered!"
    rows = [("Order ID", o["order_number"]), ("Delivery", "Delivered"), ("Payment status", o["payment_status"]), ("Total", rupees(o["total"]))]
    body = _h("Delivered. Enjoy!") + _p(f"{o['name']}, your order has been delivered. We hope you enjoy it.") + _table(rows)
    return subject, _layout(subject, body, f"Order {o['order_number']} delivered"), f"{o['name']}, order {o['order_number']} was delivered. Payment: {o['payment_status']}.\n"


def order_rejected(o: dict) -> tuple[str, str, str]:
    subject = f"{BRAND} - Order Update"
    reason = o.get("reason") or "The cafe could not accept this order."
    body = _h("We could not accept your order") + _p(f"Sorry {o['name']}, order {o['order_number']} could not be accepted.") + _p(f"Reason: {reason}") + _p("If you paid online, your refund will be started automatically.")
    return subject, _layout(subject, body, "Order update"), f"Sorry {o['name']}, order {o['order_number']} could not be accepted. Reason: {reason}\n"


TEMPLATES = {"order_confirmation": order_confirmation, "order_accepted": order_accepted, "order_on_the_way": order_on_the_way,
             "order_delivered": order_delivered, "order_rejected": order_rejected}
