# Reporting definitions

All reports are computed on the server in the **cafe's time zone** (`cafe_settings.timezone`, default Asia/Kolkata) and use these fixed definitions so the figures reconcile:

| Figure | Definition |
|---|---|
| **orders placed** | orders *created* in the range, any status |
| **orders completed** | orders that reached `completed` (dine-in / pickup / walk-in) or `delivered`, by completion time |
| **orders cancelled / rejected** | cancelled (incl. later refunded) / rejected orders created in the range |
| **gross sales** | `total_paise` of completed orders (item total − discounts + tax (if exclusive) + delivery charge), by completion time |
| **discounts / tax / delivery charges** | the corresponding components of those completed orders |
| **payments received** | payments in status paid / partially_refunded / refunded, by `paid_at` (money in; may include orders not yet completed; cash counts only after the Admin confirms it) |
| **cash collected** | cash payments confirmed by the Admin in the range (`cash_collections` = count) |
| **refunds** | refunds with status `succeeded` (provider-confirmed, or Admin-confirmed for cash), by `processed_at` |
| **net sales** | gross sales − refunds in the range |
| **by order type / payment method** | completed orders grouped by `order_type` / chosen `payment_method` |
| **payments by gateway** | paid payments and refunds grouped by gateway (`razorpay`, `cashfree`, `cash`) |
| **coupon usage** | `consumed` redemptions by offer (reserved/released ones are not counted) |
| **best sellers / item quantities** | `order_items` of completed orders, grouped by name (+ variant) |

Endpoints: `GET /admin/reports/summary?start=&end=&granularity=day|month` and `GET /admin/reports/export?kind=sales|items|payments|coupons` (CSV). Range limit 400 days. Note: grouping is done in the application for portability; at very large volumes move to SQL roll-up tables (see UPGRADES.md).
