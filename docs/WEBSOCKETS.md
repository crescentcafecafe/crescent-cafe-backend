# WebSocket & push events

## Connect
```
wss://HOST/api/v1/ws?token=<access_jwt>                 # Admin / Customer / Rider (or header  Authorization: Bearer …)
wss://HOST/api/v1/ws?guest_token=<token>&order_id=<id>  # guest tracking one order
```
Invalid, expired or revoked credentials are refused at the handshake (close code **4401**). The server sends `{"type":"auth_expired"}` and closes (4401) when the access token expires: refresh the token and reconnect.

Server → client on connect: `{"type":"connected","role":"…"}`. You are auto-subscribed to your own `account:<role>:<id>` topic (admins also to `admin`).

## Client messages
| Message | Reply |
|---|---|
| `{"type":"subscribe","topic":"order:<uuid>"}` | `{"type":"subscribed","topic":…}` or `{"type":"error","code":"forbidden","topic":…}` |
| `{"type":"unsubscribe","topic":…}` | `{"type":"unsubscribed",…}` |
| `{"type":"ping"}` (send every ~30 s) | `{"type":"pong"}` |

## Topics and who may subscribe (checked on every subscribe)
| Topic | Allowed |
|---|---|
| `admin` | Admin |
| `account:<role>:<id>` | that user only |
| `order:<order_id>` | Admin; the owning Customer; a Guest holding that order's token; the Rider with an active offer/assignment |
| `tracking:<order_id>` | Admin; owning Customer; Guest token for that order (**not** riders) |
| `conversation:<id>` | members of the conversation |
| `menu` | anyone (availability changes) |

## Event envelope
```json
{"id": 1042, "type": "order.status_changed", "topic": "order:5f…", "ts": "2026-11-13T07:27:46+00:00", "data": {…}}
```
`id` is the durable event sequence (`notification_events.id`); `null` for ephemeral events (`tracking.location`, `chat.read`). **WebSocket delivery is not storage:** after every reconnect, refetch state via REST (`GET /orders/{id}`, `GET /admin/orders/pending`, `GET /chat/conversations`…) and call `GET /notifications/events?after_id=<last id>` to catch up.

## Event types
| type | topics | data (main fields) | push (FCM) |
|---|---|---|---|
| `order.new` | admin | order_id, order_number, order_type, total_paise, table_label, payment_method, `alarm:"start"` | Admin: **data-only, high priority** |
| `order.reminder` | admin | order_id, `alarm:"start"`, reminder | Admin (data-only) every minute while undecided, ≤15 min |
| `order.alarm_stop` | admin | order_id, decision, `alarm:"stop"` | Admin (data-only) |
| `order.alarm_ack` | admin | order_id | – |
| `order.created` | order, admin, customer account | order_id, status, total_paise | Customer |
| `order.status_changed` | order, admin, customer account | order_id, status, previous_status, payment_status, reason | Customer |
| `order.cancelled_by_customer` | admin | order_id | Admin |
| `payment.succeeded` / `payment.failed` / `payment.reversed` | order, customer account (+admin) | order_id, payment_id, payment_status | Customer |
| `payment.mismatch` / `payment.late` | admin | payment_id/order_id | Admin |
| `refund.succeeded` / `refund.failed` | order, customer / admin | refund_id, amount_paise | Customer / Admin |
| `assignment.offered` | rider account, admin | assignment_id, order_id, `alarm:"start"` | Rider: **data-only, high priority** |
| `assignment.accepted` | admin, order, rider | assignment_id, rider_id, `alarm:"stop"` | Admin |
| `assignment.rejected` | admin | assignment_id, reason | Admin (high priority) |
| `assignment.cancelled` | rider account, admin | assignment_id, `alarm:"stop"` | Rider (data-only) |
| `delivery.otp_issued` | order (customer/guest/admin; **no code inside**) | order_id, expires_at | the code itself is pushed once, not stored |
| `delivery.completed` | admin, order | order_id, rider_id | Admin |
| `tracking.location` (ephemeral) | tracking:<order>, admin | latitude, longitude, accuracy_m, recorded_at | – |
| `chat.message` | conversation, member accounts | conversation_id, message | recipient, unless already viewing the conversation on this instance |
| `chat.read` (ephemeral) | conversation | reader_role, read_up_to | – |
| `menu.availability_changed` | menu | item_id, is_available | – |
| `promotion` | – | – | opted-in customers |

## What the Android apps must do (the backend cannot)
* **Alarms:** the Admin app plays/repeats the sound after `order.new` (or the data-only FCM message) until it sees `order.alarm_stop` / accept / reject; the Rider app does the same for `assignment.offered` until accept/reject/`assignment.cancelled`. Use a high-importance notification channel (`new_orders`), a foreground service or full-screen intent, and ask the user to exempt the app from battery optimisation. Android Do-Not-Disturb, notification permission and OEM battery managers can still suppress sound: the backend cannot override them. `GET /admin/orders/pending` on app start/resume is the safety net.
* **FCM lifecycle:** request `POST_NOTIFICATIONS` (Android 13+), then `POST /notifications/devices` with a stable `install_id`; call `PUT /notifications/devices/{install_id}/token` from `onNewToken`; report permission changes via the same call (`notifications_enabled`); on logout send `POST /auth/logout {"install_id": …}`.
* **Rider tracking:** send `POST /tracking/location` every `rider_location_update_interval_seconds` (see `GET /settings/public`) only while a delivery is active; a 429 `location_too_frequent` means slow down.
* **Delivery OTP:** show the code from the push or from `POST /deliveries/orders/{id}/otp` (generating a new code invalidates the old one).
