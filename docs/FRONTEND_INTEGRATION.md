# Crescent Cafe Website — Frontend Integration Contract

This document describes the existing FastAPI contract for a unified Customer, Admin and Rider web application. `/api/v1` is the API prefix. The API and Pydantic models are authoritative; when in doubt, inspect `/openapi.json` and source code.

## Local endpoints and headers

- Website: `http://localhost:3000`
- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- API root: `http://localhost:8000/api/v1`
- Send `Authorization: Bearer <access_token>` for authenticated endpoints.
- Send `X-Branch-Code: main` when using a non-default branch code.
- Send `Idempotency-Key: <unique value>` for order creation and refunds.
- For a guest-created order, retain the one-time `guest_token` response and send it as `X-Guest-Token` for order/payment/tracking calls that support guest access.
- Money fields ending in `_paise` are integer Indian paise. Display rupees by dividing by 100 and formatting INR.
- API errors use `{"error":{"code":"...","message":"...","details":...}}`.
- Paginated responses are `{items,total,limit,offset}` unless the individual endpoint says otherwise.

## Role authentication

| Interface | Login endpoint | Body | Notes |
|---|---|---|---|
| Customer | `POST /auth/customer/login` | `{email,password,device_label?}` | Customer register: `POST /auth/customer/register` with `{name,email,password,phone?}`. |
| Customer Google | `POST /auth/customer/google` | `{id_token,device_label?}` | `phone_required=true` means collect mobile via `PUT /customers/me/phone` before ordering. |
| Admin | `POST /auth/admin/login` | `{email,password,device_label?}` | No public admin registration; use protected bootstrap CLI. |
| Rider | `POST /auth/rider/login` | `{phone,password,device_label?}` | Rider credentials are created/reset by admin APIs. |

Login response includes `access_token`, `refresh_token`, `expires_in`, `role`, `session_id`, and optional role-specific flags. Access tokens expire; use `POST /auth/refresh` with `{refresh_token}`. Refresh tokens rotate on use. Logout with `POST /auth/logout` and optionally `{install_id}`. Do not share tokens between roles.

Customer profile: `GET/PATCH /customers/me`; set phone with `PUT /customers/me/phone` body `{phone}`. The backend has no saved-address CRUD endpoint in this version; collect the delivery address at checkout and send it with the order.

## Customer menu and checkout

1. Fetch `GET /menu`. Response: `{branch_id,categories:[{id,name,description,sort_order,is_active,items:[...]}]}`. Each item contains `id`, `category_id`, `name`, `description`, `price_paise`, `image_url`, `prep_minutes`, `is_veg`, `is_available`, `variants[]` and `addons[]`.
2. `GET /menu/items/{item_id}` returns a single item.
3. Fetch `GET /settings/public` for cafe open/closed state, enabled order types, payment methods, currency and delivery rules.
4. Fetch `GET /payments/methods` for currently offered methods. Disabled or unconfigured gateways are omitted.
5. For a quote, `POST /orders/quote` with `{order_type,items,coupon_code?,delivery_address?,customer_phone?}`. Each item contains `{menu_item_id,quantity,variant_id?,addons:[{addon_id,quantity}],options:[{name,value}],notes?}`. The response is the server-computed quote. Display the returned numbers; browser totals are estimates only.
6. Create an order with `POST /orders` using the same cart fields plus `payment_method`, `source:"qr_web"`, optional `customer_name`, `customer_email`, `special_instructions`, and for dine-in the `table_session_token`. Add a unique `Idempotency-Key` header.
7. Response is `{order,guest_token,duplicate,next_action}`. Store `guest_token` securely for that order only when present. Do not lose it: it is shown only at creation.
8. For online payment, call `POST /payments/orders/{order_id}/initiate` with `{gateway:"razorpay"|"cashfree"}`. Razorpay `client_payload` contains the checkout key/order/amount/currency and prefill. After the SDK callback call `POST /payments/orders/{order_id}/verify/razorpay` with `{razorpay_order_id,razorpay_payment_id,razorpay_signature}`; the response is server-verified. Cashfree `client_payload` includes `payment_session_id`, `order_id` and `environment`; configure `CASHFREE_RETURN_URL` as the website route `/payments/cashfree-return`. The backend appends the local order UUID as `order_id`; the frontend return handler calls `POST /payments/orders/{order_id}/sync` and then opens the order detail page. The redirect itself is never treated as payment confirmation. Never mark the order paid from the browser SDK callback alone.
9. Customer orders: `GET /orders`, `GET /orders/{order_id}`, `POST /orders/{order_id}/cancel` body `{reason}`. Guest access uses the `X-Guest-Token` header for supported endpoints.
10. Invoice JSON/PDF: `GET /invoices/order/{order_id}` and `/pdf`.

Order creation types: `dine_in`, `pickup`, `delivery`; `walk_in` is admin-only. Delivery requires `delivery_address` with at least `line1` and `postal_code`. Dine-in requires a resolved QR table-session token from `POST /qr/resolve` body `{token,pin?}`. A delivery OTP may be requested by an authorized customer/guest through `POST /deliveries/orders/{order_id}/otp`; the rider never receives the code.

## Admin endpoints used by the website

- Dashboard: `GET /admin/dashboard`.
- Orders: `GET /admin/orders`, `GET /admin/orders/pending`, `GET /admin/orders/{id}`; accept `POST /admin/orders/{id}/accept`; reject/cancel with `POST /admin/orders/{id}/reject` or `/cancel` body `{reason}`; kitchen status with `POST /admin/orders/{id}/status` body `{status:"preparing"|"ready"|"completed"}`; assign a rider using `POST /admin/orders/{id}/assign` body `{rider_id}`; acknowledge alarm at `/alarm/ack`.
- Catalog: `GET /admin/menu`; create category `POST /admin/menu/categories`; update category `PATCH /admin/menu/categories/{id}`; create item `POST /admin/menu/items`; update `PATCH /admin/menu/items/{id}`; soft-delete `DELETE /admin/menu/items/{id}`; restore `POST /admin/menu/items/{id}/restore`; availability `PUT /admin/menu/items/{id}/availability` body `{is_available}`; image upload `POST /admin/menu/items/{id}/image` multipart field `file`; variants/add-ons are documented in `API.md`.
- Riders: `GET/POST /admin/riders`; `GET/PATCH /admin/riders/{id}`; password reset `POST /admin/riders/{id}/reset-password` body `{new_password}`.
- Payments/refunds: `GET /admin/payments`, `GET /admin/payments/counter-pending`, `POST /admin/payments/{id}/confirm-cash`, `POST /admin/payments/{id}/refunds`, `GET /admin/refunds`. Financial transitions are performed by the server and provider/webhook, not the frontend.
- Reports: `GET /admin/reports/summary`; CSV export `GET /admin/reports/export?kind=sales|items|payments|coupons&start=YYYY-MM-DD&end=YYYY-MM-DD`.
- Notifications: `POST /admin/notifications/broadcast` body `{title,body}`. This endpoint broadcasts only to customers opted in to marketing; it is not a per-user targeting API.
- Chat: `GET /chat/conversations`; messages `GET /chat/conversations/{id}/messages`; send `POST /chat/conversations/{id}/messages` body `{body,client_msg_id?,internal?}`; admin starts a thread with `POST /chat/conversations/admin` body `{customer_id?,rider_id?,order_id?}`.
- Settings: `/admin/settings/cafe`, `/admin/settings/delivery`, `/admin/settings/taxes`, `/admin/payment-gateways`. Read API.md for the request schemas.

Admin state machine constraints must be respected. The backend only permits valid transitions; delivery orders are completed by a rider's OTP verification, not the admin kitchen status action.

## Rider endpoints

- Profile/availability: `GET /riders/me`; `PATCH /riders/me/availability` body `{available:boolean}`.
- Active assignments: `GET /deliveries/mine?active=true`; history: `GET /deliveries/mine?active=false`; detail: `GET /deliveries/{assignment_id}`.
- Accept/reject: `POST /deliveries/{id}/accept`; `POST /deliveries/{id}/reject` body `{reason}`.
- Progress: `POST /deliveries/{id}/picked-up`; `POST /deliveries/{id}/out-for-delivery`; OTP completion: `POST /deliveries/{id}/verify-otp` body `{otp:"123456"}`. Delivery completion is only through OTP verification.
- GPS: `POST /tracking/location` body `{assignment_id,latitude,longitude,accuracy_m?,recorded_at?}`. Collect/send location only with explicit permission and while an active delivery is in progress. Stop after delivery ends.
- Chat is shared at `/chat/*` and authorization is checked per conversation.

## Chat, notifications and realtime

WebSocket: `ws://localhost:8000/api/v1/ws?token=<access_token>`. For guest order tracking use `?guest_token=<token>&order_id=<uuid>`. Server sends `connected`; subscribe with JSON `{"type":"subscribe","topic":"order:<uuid>"}` or `conversation:<uuid>`; send `{"type":"ping"}` roughly every 30 seconds. The full topic ACL and event catalogue are in `WEBSOCKETS.md`.

After a reconnect, fetch current REST state again and call `GET /notifications/events?after_id=<last_event_id>` to catch up. Do not treat WebSocket messages as durable storage.

Browser FCM requires Firebase Web Messaging configuration and HTTPS (localhost is accepted by modern browsers as a secure development origin). Register a web device with `POST /notifications/devices` body `{install_id,fcm_token,platform:"web",device_model?,os_version?,app_version?,locale?,notifications_enabled}`. The service-account JSON stays only on the backend. If Firebase Web config/VAPID key are absent, the rest of the website must still work and notifications should show an unconfigured state.

## Features not supported by this backend version

- Saved customer address CRUD (address is collected at checkout).
- Customer phone OTP verification (phones are normalized, not OTP-verified).
- Guest chat and FCM push for guests.
- Arbitrary single-customer/rider push targeting from the Admin panel; only opted-in promotional broadcast is present.
- Web frontend-specific browser Firebase config is not delivered by this API; set Firebase Web app config in frontend `.env.local`.
- Google login requires configured Google Web OAuth client IDs. Razorpay/Cashfree require server-side sandbox credentials and webhook URLs. The website must label any unconfigured flow unavailable rather than simulate success.

The web frontend must present these limitations honestly and must not invent endpoints or claim delivery/payment success without a verified backend response.
