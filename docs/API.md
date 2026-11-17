# API reference (generated)

Generated from the OpenAPI schema by `python -m scripts.export_api_docs`. The interactive version is served at `/docs` (Swagger) and `/redoc`; the machine-readable schema at `/openapi.json`.

Conventions: all money is **integer paise** (INR). Errors are `{"error": {"code", "message", "details"}}`. Pagination uses `limit`/`offset` and returns `{items,total,limit,offset}`. `*` marks required parameters. Send `Idempotency-Key` on order creation and refunds. Guests use header `X-Guest-Token`.

## admin

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `POST` | `/api/v1/admin/assignments/{assignment_id}/cancel` | Admin | `CancelRequest` | - | Cancel Assignment |
| `GET` | `/api/v1/admin/audit-logs` | Admin |  | entity_type, entity_id, action, limit, offset | Audit Logs |
| `GET` | `/api/v1/admin/dashboard` | Admin |  | - | Dashboard |
| `GET` | `/api/v1/admin/deliveries` | Admin |  | state, limit, offset | Deliveries |
| `GET` | `/api/v1/admin/jobs` | Admin |  | status, limit | Inspect the background-job queue (failed jobs can be retried). |
| `POST` | `/api/v1/admin/jobs/{job_id}/retry` | Admin |  | - | Retry Job |
| `GET` | `/api/v1/admin/me` | Admin |  | - | Me |
| `POST` | `/api/v1/admin/notifications/broadcast` | Admin | `BroadcastIn` | - | Promotional push to customers who opted in to marketing. |
| `GET` | `/api/v1/admin/orders` | Admin |  | status, order_type, payment_status, table_id, table_session_id, phone, q, date_from, date_to, limit, offset | List Orders |
| `POST` | `/api/v1/admin/orders` | Admin | `OrderCreate` | Idempotency-Key | Walk-in order created by the Admin. Same validation, pricing, tax and history rules as customer orders. |
| `GET` | `/api/v1/admin/orders/pending` | Admin |  | - | Orders waiting for the Admin's accept/reject decision (drives the alarm; also use to resync after reconnecting). |
| `GET` | `/api/v1/admin/orders/{order_id}` | Admin |  | - | Order Detail |
| `POST` | `/api/v1/admin/orders/{order_id}/accept` | Admin |  | - | Accept |
| `POST` | `/api/v1/admin/orders/{order_id}/alarm/ack` | Admin |  | - | Admin app confirms it received/seen the new-order alarm. The alarm keeps ringing client-side until accept/reject unless the app chooses to silence it. |
| `POST` | `/api/v1/admin/orders/{order_id}/assign` | Admin | `AssignIn` | - | Manual rider assignment (the system never auto-assigns). Order must be 'ready'. |
| `POST` | `/api/v1/admin/orders/{order_id}/cancel` | Admin | `CancelRequest` | - | Cancel |
| `POST` | `/api/v1/admin/orders/{order_id}/delivery-otp/resend` | Admin |  | - | Issue a fresh delivery OTP and push it to the customer. The Admin never sees the code. |
| `POST` | `/api/v1/admin/orders/{order_id}/reject` | Admin | `CancelRequest` | - | Reject |
| `POST` | `/api/v1/admin/orders/{order_id}/status` | Admin | `AdminStatusChange` | - | Kitchen workflow: preparing -> ready -> completed (completed = served / collected; delivery orders finish via rider OTP only). |
| `GET` | `/api/v1/admin/payments` | Admin |  | status, method, gateway, order_id, limit, offset | Payments |
| `GET` | `/api/v1/admin/payments/counter-pending` | Admin |  | - | Cash / pay-at-counter payments awaiting the Admin's confirmation (excludes closed orders). |
| `POST` | `/api/v1/admin/payments/{payment_id}/confirm-cash` | Admin | `CashConfirmIn` | - | Confirm Cash |
| `POST` | `/api/v1/admin/payments/{payment_id}/refunds` | Admin | `RefundIn` | Idempotency-Key | Starts a refund. Online refunds stay 'pending' until the provider confirms (webhook or reconciliation); nothing is promised before that. |
| `POST` | `/api/v1/admin/payments/{payment_id}/reverse-cash` | Admin | `CashReverseIn` | - | Audited correction for a cash payment confirmed by mistake. |
| `POST` | `/api/v1/admin/payments/{payment_id}/sync` | Admin |  | - | Sync Payment |
| `GET` | `/api/v1/admin/refunds` | Admin |  | status, limit, offset | Refunds |
| `POST` | `/api/v1/admin/refunds/{refund_id}/confirm-cash` | Admin |  | - | For cash payments: Admin confirms the money was handed back. |
| `POST` | `/api/v1/admin/refunds/{refund_id}/retry` | Admin |  | - | Retry Refund |
| `GET` | `/api/v1/admin/riders` | Admin |  | active, available | Riders |
| `POST` | `/api/v1/admin/riders` | Admin | `RiderCreate` | - | Create Rider |
| `GET` | `/api/v1/admin/riders/{rider_id}` | Admin |  | - | Get Rider |
| `PATCH` | `/api/v1/admin/riders/{rider_id}` | Admin | `RiderPatch` | - | Patch Rider |
| `POST` | `/api/v1/admin/riders/{rider_id}/reset-password` | Admin | `ResetPasswordIn` | - | Reset Rider Password |
| `GET` | `/api/v1/admin/webhook-events` | Admin |  | status, limit | Webhook Events |

## admin-catalog

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `PATCH` | `/api/v1/admin/coupons/{coupon_id}/active` | Admin | `AvailabilityIn` | - | Toggle Coupon |
| `GET` | `/api/v1/admin/menu` | Admin |  | - | Everything including inactive categories and archived items (admin view). |
| `PATCH` | `/api/v1/admin/menu/addons/{addon_id}` | Admin | `AddonPatch` | - | Patch Addon |
| `POST` | `/api/v1/admin/menu/categories` | Admin | `CategoryIn` | - | Create Category |
| `POST` | `/api/v1/admin/menu/categories/reorder` | Admin | `ReorderIn` | - | Reorder Categories |
| `PATCH` | `/api/v1/admin/menu/categories/{category_id}` | Admin | `CategoryPatch` | - | Patch Category |
| `POST` | `/api/v1/admin/menu/items` | Admin | `ItemIn` | - | Add a new product. Upload its photo afterwards with POST /admin/menu/items/{id}/image. |
| `POST` | `/api/v1/admin/menu/items/reorder` | Admin | `ReorderIn` | - | Reorder Items |
| `GET` | `/api/v1/admin/menu/items/{item_id}` | Admin |  | - | Get Item |
| `PATCH` | `/api/v1/admin/menu/items/{item_id}` | Admin | `ItemPatch` | - | Patch Item |
| `DELETE` | `/api/v1/admin/menu/items/{item_id}` | Admin |  | - | Soft delete: old orders keep their item snapshot. |
| `POST` | `/api/v1/admin/menu/items/{item_id}/addons` | Admin | `app__schemas__admin__AddonIn` | - | Add Addon |
| `PUT` | `/api/v1/admin/menu/items/{item_id}/availability` | Admin | `AvailabilityIn` | - | Instant Available / Unavailable switch (no ingredient stock tracking). Orders re-check this at submission time. |
| `POST` | `/api/v1/admin/menu/items/{item_id}/image` | Admin | multipart/form-data | - | Upload/replace the product photo (stored in Supabase Storage). Max 5 MB; the content is verified, not just the file extension. |
| `DELETE` | `/api/v1/admin/menu/items/{item_id}/image` | Admin |  | - | Delete Image |
| `POST` | `/api/v1/admin/menu/items/{item_id}/restore` | Admin |  | - | Restore Item |
| `POST` | `/api/v1/admin/menu/items/{item_id}/variants` | Admin | `VariantIn` | - | Add Variant |
| `PATCH` | `/api/v1/admin/menu/variants/{variant_id}` | Admin | `VariantPatch` | - | Patch Variant |
| `GET` | `/api/v1/admin/offers` | Admin |  | status | List Offers |
| `POST` | `/api/v1/admin/offers` | Admin | `OfferIn` | - | Create Offer |
| `PATCH` | `/api/v1/admin/offers/{offer_id}` | Admin | `OfferPatch` | - | Activate / pause / expire an offer (history is never deleted) or edit its rules. |
| `POST` | `/api/v1/admin/offers/{offer_id}/coupons` | Admin | `CouponIn` | - | Add Coupon |
| `GET` | `/api/v1/admin/offers/{offer_id}/redemptions` | Admin |  | limit | Redemptions |
| `GET` | `/api/v1/admin/table-sessions` | Admin |  | status, table_id, limit | List Sessions |
| `POST` | `/api/v1/admin/table-sessions/{session_id}/close` | Admin | `CloseSessionIn` | - | Close Session |
| `GET` | `/api/v1/admin/table-sessions/{session_id}/orders` | Admin |  | - | Every order of the session shown separately (each customer's order is billed on its own). |
| `POST` | `/api/v1/admin/table-sessions/{session_id}/transfer` | Admin | `TransferIn` | - | Transfer Session |
| `GET` | `/api/v1/admin/tables` | Admin |  | include_archived | List Tables |
| `POST` | `/api/v1/admin/tables` | Admin | `TableIn` | - | No maximum table count. Number must be unique among non-archived tables. |
| `POST` | `/api/v1/admin/tables/qr/bulk` | Admin | `BulkQrIn` | - | Bulk export data for printable ZIP/PDF generation on the frontend. Rotates the selected tables' tokens (hashes cannot be reversed), |
| `PATCH` | `/api/v1/admin/tables/{table_id}` | Admin | `TablePatch` | - | Patch Table |
| `POST` | `/api/v1/admin/tables/{table_id}/qr` | Admin |  | - | Generate (or regenerate) the table's QR token. The previous token is revoked immediately and cannot open new sessions. |
| `POST` | `/api/v1/admin/tables/{table_id}/qr/revoke` | Admin | `RevokeIn` | - | Revoke Qr |

## admin-settings

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/admin/invoices` | Admin |  | limit, offset | List Invoices |
| `GET` | `/api/v1/admin/payment-gateways` | Admin |  | - | Gateway switches + whether server credentials are present. Secret keys are never returned. |
| `PUT` | `/api/v1/admin/payment-gateways` | Admin | `GatewayConfigIn` | - | Set Gateways |
| `GET` | `/api/v1/admin/settings/cafe` | Admin |  | - | Get Cafe |
| `PATCH` | `/api/v1/admin/settings/cafe` | Admin | `CafeSettingsPatch` | - | Patch Cafe |
| `GET` | `/api/v1/admin/settings/delivery` | Admin |  | - | Get Delivery |
| `PATCH` | `/api/v1/admin/settings/delivery` | Admin | `DeliverySettingsPatch` | - | Patch Delivery |
| `GET` | `/api/v1/admin/settings/taxes` | Admin |  | - | List Taxes |
| `POST` | `/api/v1/admin/settings/taxes` | Admin | `TaxIn` | - | Tax classes are configurable; items may point at a class, otherwise the default class applies. The owner must verify rates. |
| `PATCH` | `/api/v1/admin/settings/taxes/{tax_id}` | Admin | `TaxPatch` | - | Patch Tax |

## auth

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `POST` | `/api/v1/auth/admin/login` | Public / optional (guest token or Bearer) | `AdminLogin` | - | Admin Login |
| `POST` | `/api/v1/auth/change-password` | Signed-in (role-checked) | `ChangePassword` | - | Change Password |
| `POST` | `/api/v1/auth/customer/google` | Public / optional (guest token or Bearer) | `GoogleLogin` | - | Sign in with Google (Gmail). The response's phone_required=true means the app must collect the mobile number next. |
| `POST` | `/api/v1/auth/customer/login` | Public / optional (guest token or Bearer) | `CustomerLogin` | - | Customer Login |
| `POST` | `/api/v1/auth/customer/register` | Public / optional (guest token or Bearer) | `CustomerRegister` | - | Customer Register |
| `POST` | `/api/v1/auth/logout` | Signed-in (role-checked) | JSON | - | Logout |
| `POST` | `/api/v1/auth/logout-all` | Signed-in (role-checked) |  | - | Logout All |
| `POST` | `/api/v1/auth/refresh` | Public / optional (guest token or Bearer) | `RefreshRequest` | - | Refresh |
| `POST` | `/api/v1/auth/rider/login` | Public / optional (guest token or Bearer) | `RiderLogin` | - | Rider Login |
| `GET` | `/api/v1/auth/sessions` | Signed-in (role-checked) |  | - | Sessions |
| `DELETE` | `/api/v1/auth/sessions/{session_id}` | Signed-in (role-checked) |  | - | Revoke |

## chat

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/chat/conversations` | Signed-in (role-checked) |  | status, kind, limit, offset | Admin: the single centralized inbox (all conversations). Customer/Rider: only their own. |
| `POST` | `/api/v1/chat/conversations` | Signed-in (role-checked) | `ChatOpen` | - | Open Conversation |
| `POST` | `/api/v1/chat/conversations/admin` | Signed-in (role-checked) | `AdminStartChat` | - | Admin Start |
| `POST` | `/api/v1/chat/conversations/{conversation_id}/close` | Signed-in (role-checked) |  | - | Close |
| `GET` | `/api/v1/chat/conversations/{conversation_id}/messages` | Signed-in (role-checked) |  | before_id, limit | Messages |
| `POST` | `/api/v1/chat/conversations/{conversation_id}/messages` | Signed-in (role-checked) | `ChatSend` | - | Send |
| `POST` | `/api/v1/chat/conversations/{conversation_id}/read` | Signed-in (role-checked) | JSON | - | Read |

## customers

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/customers/me` | Signed-in (role-checked) |  | - | Me |
| `DELETE` | `/api/v1/customers/me` | Signed-in (role-checked) |  | - | Right-to-erasure: personal data is anonymised; order records are kept for accounting. |
| `PATCH` | `/api/v1/customers/me` | Signed-in (role-checked) | `ProfileUpdate` | - | Update Me |
| `GET` | `/api/v1/customers/me/devices` | Signed-in (role-checked) |  | - | My Devices |
| `PUT` | `/api/v1/customers/me/phone` | Signed-in (role-checked) | `PhoneUpdate` | - | Mobile number is mandatory before ordering (collected right after Google / email sign-in). |

## deliveries

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/deliveries/mine` | Signed-in (role-checked) |  | active | Mine |
| `POST` | `/api/v1/deliveries/orders/{order_id}/otp` | Signed-in (role-checked) |  | x-guest-token | Customer / guest asks for a (new) delivery code. Generating a new one invalidates the previous code. |
| `GET` | `/api/v1/deliveries/{assignment_id}` | Signed-in (role-checked) |  | - | One |
| `POST` | `/api/v1/deliveries/{assignment_id}/accept` | Signed-in (role-checked) |  | - | Accept |
| `POST` | `/api/v1/deliveries/{assignment_id}/out-for-delivery` | Signed-in (role-checked) |  | - | Starts the final leg and sends the delivery OTP to the customer (the rider never sees the code). |
| `POST` | `/api/v1/deliveries/{assignment_id}/picked-up` | Signed-in (role-checked) |  | - | Picked Up |
| `POST` | `/api/v1/deliveries/{assignment_id}/reject` | Signed-in (role-checked) | `RejectAssignment` | - | Reject |
| `POST` | `/api/v1/deliveries/{assignment_id}/verify-otp` | Signed-in (role-checked) | `OtpVerifyIn` | - | The ONLY way to complete a delivery. Wrong codes are counted; the code expires and locks after too many attempts. |

## health

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/diagnostics` | Signed-in (role-checked) |  | x-diagnostics-token | Protected: admin JWT or X-Diagnostics-Token (DIAGNOSTICS_TOKEN env). |
| `GET` | `/health` | Public / optional (guest token or Bearer) |  | - | Liveness only: no infrastructure detail is exposed. |
| `GET` | `/ready` | Public / optional (guest token or Bearer) |  | - | Readiness: can we reach the database? |

## invoices

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/invoices/order/{order_id}` | Signed-in (role-checked) |  | x-guest-token | Stable invoice data (JSON) for the frontend to render/share. Admin or the order's own customer/guest only. |
| `GET` | `/api/v1/invoices/order/{order_id}/pdf` | Signed-in (role-checked) |  | x-guest-token | Invoice Pdf |

## menu

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/menu` | Public / optional (guest token or Bearer) |  | x-branch-code | Active customer-facing menu. Unavailable items are included with is_available=false so apps can grey them out. |
| `GET` | `/api/v1/menu/items/{item_id}` | Public / optional (guest token or Bearer) |  | x-branch-code | Get Item |

## notifications

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/notifications/devices` | Signed-in (role-checked) |  | - | My Devices |
| `POST` | `/api/v1/notifications/devices` | Signed-in (role-checked) | `DeviceIn` | - | Register / update this install's FCM token for the signed-in account (multiple devices per account supported). |
| `DELETE` | `/api/v1/notifications/devices/{install_id}` | Signed-in (role-checked) |  | - | Remove Device |
| `PUT` | `/api/v1/notifications/devices/{install_id}/token` | Signed-in (role-checked) | `DeviceTokenRefresh` | - | Call from FirebaseMessagingService.onNewToken and whenever the OS notification permission changes. |
| `GET` | `/api/v1/notifications/events` | Signed-in (role-checked) |  | after_id, limit | Catch-up after a WebSocket reconnect: durable events addressed to me or my topics since after_id. |

## offers

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `POST` | `/api/v1/coupons/validate` | Signed-in (role-checked) | `CouponValidate` | x-branch-code | Always HTTP 200: {valid: false, reason} explains why a coupon cannot be used; valid=true returns the server-calculated quote. |
| `GET` | `/api/v1/offers` | Signed-in (role-checked) |  | phone, x-branch-code | Currently running offers a customer could use. Per-customer limits / first-order rules are evaluated when identity is known. |
| `POST` | `/api/v1/orders/quote` | Signed-in (role-checked) | `QuoteRequest` | x-branch-code | Price preview computed by the server (items, discount, tax, delivery, total). Clients must display these numbers. |

## orders

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `POST` | `/api/v1/orders` | Signed-in (role-checked) | `OrderCreate` | Idempotency-Key, x-branch-code | Place a customer/guest order (app or QR website). Send an Idempotency-Key header to make retries safe. |
| `GET` | `/api/v1/orders` | Signed-in (role-checked) |  | status, limit_, offset | My Orders |
| `GET` | `/api/v1/orders/{order_id}` | Signed-in (role-checked) |  | x-guest-token | Get Order |
| `POST` | `/api/v1/orders/{order_id}/cancel` | Signed-in (role-checked) | `CancelRequest` | x-guest-token | Customers/guests may cancel only before the cafe accepts the order. |

## payments

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/payments/methods` | Public / optional (guest token or Bearer) |  | x-branch-code | Payment choices currently offered to customers (disabled / unconfigured gateways are never listed). |
| `GET` | `/api/v1/payments/orders/{order_id}` | Signed-in (role-checked) |  | x-guest-token | Order Payments |
| `POST` | `/api/v1/payments/orders/{order_id}/initiate` | Signed-in (role-checked) | `InitiatePayment` | x-guest-token | Create (or reuse) a gateway payment for an order awaiting payment. Returns only the client-SDK payload, never secrets. |
| `POST` | `/api/v1/payments/orders/{order_id}/sync` | Signed-in (role-checked) |  | x-guest-token | Poll after returning from a gateway page (Cashfree redirect etc.): refreshes status straight from the provider. |
| `POST` | `/api/v1/payments/orders/{order_id}/verify/razorpay` | Signed-in (role-checked) | `RazorpayVerify` | x-guest-token | Optional fast-path after the Razorpay checkout callback. The result still comes from a server-side status fetch. |

## qr

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `POST` | `/api/v1/qr/resolve` | Public / optional (guest token or Bearer) | `QrResolve` | - | Exchange a scanned QR token for a short-lived table-session token used when placing dine-in orders. |

## reports

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/admin/reports/export` | Admin |  | kind, start, end, granularity | Export |
| `GET` | `/api/v1/admin/reports/summary` | Admin |  | start, end, granularity | Daily/monthly sales, order counts, by type & payment method, cash collection, gateway summaries, coupons, refunds, best sellers. |

## riders

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/riders/me` | Signed-in (role-checked) |  | - | Me |
| `PATCH` | `/api/v1/riders/me/availability` | Signed-in (role-checked) | `AvailabilityToggle` | - | Availability |

## settings

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/settings/public` | Public / optional (guest token or Bearer) |  | x-branch-code | Public Settings |

## tracking

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `GET` | `/api/v1/tracking/active` | Signed-in (role-checked) |  | - | Latest location of every active delivery (admin live map). |
| `POST` | `/api/v1/tracking/location` | Signed-in (role-checked) | `LocationIn` | - | Post Location |
| `GET` | `/api/v1/tracking/orders/{order_id}` | Signed-in (role-checked) |  | x-guest-token | Customer (own order), guest (token) or admin. Returns last known location + staleness flag; null once delivery ended. |
| `GET` | `/api/v1/tracking/orders/{order_id}/history` | Signed-in (role-checked) |  | limit_ | History |

## webhooks

| Method | Path | Auth | Body | Params | Description |
|---|---|---|---|---|---|
| `POST` | `/api/v1/webhooks/cashfree` | Public / optional (guest token or Bearer) |  | - | Cashfree Webhook |
| `POST` | `/api/v1/webhooks/razorpay` | Public / optional (guest token or Bearer) |  | - | Razorpay Webhook |

## Schemas

Request/response models are listed in `/openapi.json` under `components.schemas`.
