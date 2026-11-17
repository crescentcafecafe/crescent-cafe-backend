# Example requests & responses

Captured from a real run of the test-suite scenario (fake payment/FCM providers). IDs, timestamps and tokens differ in your environment. Amounts are paise. `$BASE` = your API URL, `$ADMIN`/`$CUST`/`$RIDER` = `Authorization: Bearer …` headers.

## Admin login

```http
POST /api/v1/auth/admin/login
Content-Type: application/json
```
```json
{
  "email": "owner@example.com",
  "password": "••••••••••"
}
```
Response `200`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1Ni…",
  "refresh_token": "R2sKRZkO7QrFEKQJZ3…",
  "token_type": "bearer",
  "expires_in": 900,
  "role": "admin",
  "session_id": "6d46018a-908e-4b06-8688-8c69508b8199",
  "phone_required": null,
  "must_change_password": null
}
```

## Add a new menu item (variants + add-ons)

Then upload the photo with `POST /admin/menu/items/{id}/image` (multipart field `file`).

```http
POST /api/v1/admin/menu/items
Content-Type: application/json
```
```json
{
  "category_id": "c95e1432-ebfe-44c4-a902-93fb77422056",
  "name": "Cappuccino",
  "price_paise": 18000,
  "variants": [
    {
      "name": "Regular",
      "price_paise": 18000
    },
    {
      "name": "Large",
      "price_paise": 23000
    }
  ],
  "addons": [
    {
      "name": "Extra shot",
      "price_paise": 4000,
      "max_quantity": 2
    }
  ]
}
```
Response `201`:

```json
{
  "id": "bf62b43c-f24c-4982-8409-c9859837a117",
  "category_id": "c95e1432-ebfe-44c4-a902-93fb77422056",
  "name": "Cappuccino",
  "description": null,
  "price_paise": 18000,
  "image_url": null,
  "prep_minutes": null,
  "is_veg": null,
  "is_available": true,
  "sort_order": 0,
  "variants": [
    {
      "id": "e6e1fe61-6e08-461e-83c8-dc04f086c10d",
      "name": "Large",
      "price_paise": 23000,
      "is_available": true,
      "is_archived": false,
      "sort_order": 0
    },
    {
      "id": "8f10ff73-f9f7-4335-955a-79edf70992a3",
      "name": "Regular",
      "price_paise": 18000,
      "is_available": true,
      "is_archived": false,
      "sort_order": 0
    }
  ],
  "addons": [
    {
      "id": "8330fb01-1464-4f26-8e92-deb8f67ce82e",
      "name": "Extra shot",
      "price_paise": 4000,
      "max_quantity": 2,
      "is_available": true,
      "is_archived": false,
      "sort_order": 0
    }
  ],
  "prep_notes": null,
  "tax_setting_id": null,
  "is_archived": false,
  "image_path": null
}
```

## Customer registration

```http
POST /api/v1/auth/customer/register
Content-Type: application/json
```
```json
{
  "name": "Asha",
  "email": "asha@example.com",
  "password": "••••••••••••",
  "phone": "98765 43210"
}
```
Response `201`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1Ni…",
  "refresh_token": "gge3Dlq1VxhYwWnWf-…",
  "token_type": "bearer",
  "expires_in": 900,
  "role": "customer",
  "session_id": "029faec9-2dfd-4ed8-9934-cca73b17a1cb",
  "phone_required": false,
  "must_change_password": null
}
```

## Google (Gmail) sign-in

`phone_required: true` means the app must ask for the mobile number and call `PUT /customers/me/phone` before the first order.

```http
POST /api/v1/auth/customer/google
Content-Type: application/json
```
```json
{
  "id_token": "<Google ID token from the Android/web SDK>"
}
```
Response `200`:

```json
{
  "access_token": "eyJhbGciOiJIUzI1Ni…",
  "refresh_token": "iAH1IzHaa626XfDscC…",
  "token_type": "bearer",
  "expires_in": 900,
  "role": "customer",
  "session_id": "0b034753-d0a1-4bc0-bf4c-ccb67ad9bada",
  "phone_required": true,
  "must_change_password": null
}
```

## Resolve a scanned table QR

```http
POST /api/v1/qr/resolve
Content-Type: application/json
```
```json
{
  "token": "<token from the QR url /t/{token}>"
}
```
Response `200`:

```json
{
  "table_session_token": "eyJhbGciOiJIUzI1Ni…",
  "table_session_id": "37020602-74a1-4fa3-bfb3-11746e05bae1",
  "table": {
    "id": "6e235d29-c1a1-4893-a1e0-a535e73aece6",
    "number": "1",
    "label": "Window",
    "area": "Ground"
  },
  "expires_in": 14400
}
```

## Server-side price quote

```http
POST /api/v1/orders/quote
Content-Type: application/json
```
```json
{
  "order_type": "dine_in",
  "items": [
    {
      "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
      "variant_id": "b9f529d8-d2ed-468d-9abd-c4d1a46ac8a1",
      "quantity": 2
    }
  ]
}
```
Response `200`:

```json
{
  "currency": "INR",
  "prices_include_tax": false,
  "subtotal_paise": 30000,
  "discount_paise": 0,
  "tax_paise": 1500,
  "delivery_charge_paise": 0,
  "total_paise": 31500,
  "tax_breakdown": [
    {
      "name": "GST 5%",
      "rate_bps": 500,
      "taxable_paise": 30000,
      "tax_paise": 1500
    }
  ],
  "discounts": [],
  "coupon_code": null,
  "lines": [
    {
      "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
      "name": "Latte",
      "variant": "Small",
      "quantity": 2,
      "unit_price_paise": 15000,
      "subtotal_paise": 30000,
      "discount_paise": 0,
      "tax_paise": 1500,
      "tax_rate_bps": 500,
      "total_paise": 31500
    }
  ]
}
```

## Validate a coupon (valid)

```http
POST /api/v1/coupons/validate
Content-Type: application/json
```
```json
{
  "order_type": "dine_in",
  "items": [
    {
      "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
      "variant_id": "b9f529d8-d2ed-468d-9abd-c4d1a46ac8a1",
      "quantity": 2
    }
  ],
  "coupon_code": "TEN"
}
```
Response `200`:

```json
{
  "valid": true,
  "coupon_code": "TEN",
  "discount_paise": 3000,
  "quote": {
    "currency": "INR",
    "prices_include_tax": false,
    "subtotal_paise": 30000,
    "discount_paise": 3000,
    "tax_paise": 1350,
    "delivery_charge_paise": 0,
    "total_paise": 28350,
    "tax_breakdown": [
      {
        "name": "GST 5%",
        "rate_bps": 500,
        "taxable_paise": 27000,
        "tax_paise": 1350
      }
    ],
    "discounts": [
      {
        "offer_id": "cc42ac8d-287e-4d80-8984-7a13d5d9dbd7",
        "name": "Ten off",
        "code": "TEN",
        "discount_paise": 3000
      }
    ],
    "coupon_code": "TEN",
    "lines": [
      {
        "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
        "name": "Latte",
        "variant": "Small",
        "quantity": 2,
        "unit_price_paise": 15000,
        "subtotal_paise": 30000,
        "discount_paise": 3000,
        "tax_paise": 1350,
        "tax_rate_bps": 500,
        "total_paise": 28350
      }
    ]
  }
}
```

## Validate a coupon (explains why not)

```http
POST /api/v1/coupons/validate
Content-Type: application/json
```
```json
{
  "order_type": "dine_in",
  "items": [
    {
      "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
      "variant_id": "b9f529d8-d2ed-468d-9abd-c4d1a46ac8a1",
      "quantity": 2
    }
  ],
  "coupon_code": "WRONG"
}
```
Response `200`:

```json
{
  "valid": false,
  "reason": "This coupon code does not exist",
  "code": "coupon_invalid"
}
```

## Place a guest dine-in order (QR website)

Header `Idempotency-Key` makes retries safe. `guest_token` is returned once: store it and send it as `X-Guest-Token` to track the order.

```http
POST /api/v1/orders
Content-Type: application/json
```
```json
{
  "order_type": "dine_in",
  "payment_method": "cash",
  "source": "qr_web",
  "table_session_token": "eyJhbGciOiJIUzI1Ni…",
  "items": [
    {
      "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
      "variant_id": "b9f529d8-d2ed-468d-9abd-c4d1a46ac8a1",
      "quantity": 2
    }
  ],
  "coupon_code": "TEN",
  "customer_name": "Ravi",
  "customer_phone": "9812345678",
  "special_instructions": "Less sugar"
}
```
Response `201`:

```json
{
  "order": {
    "id": "40523be4-9dd1-4084-aa01-b5f5a1fd4f2a",
    "order_number": "CC261113-X687B",
    "order_type": "dine_in",
    "source": "qr_web",
    "status": "placed",
    "payment_method": "cash",
    "payment_status": "unpaid",
    "currency": "INR",
    "customer_name": "Ravi",
    "customer_phone": "+919812345678",
    "table_label": "Table 1 (Window)",
    "table_id": "6e235d29-c1a1-4893-a1e0-a535e73aece6",
    "table_session_id": "37020602-74a1-4fa3-bfb3-11746e05bae1",
    "delivery_address": null,
    "special_instructions": "Less sugar",
    "created_at": "2026-11-13T07:27:46.033599+00:00",
    "updated_at": "2026-11-13T07:27:46.033602+00:00",
    "items": [
      {
        "id": "8d919f98-02ea-420d-b23e-f7dca7154073",
        "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
        "name": "Latte",
        "variant_name": "Small",
        "quantity": 2,
        "unit_price_paise": 15000,
        "line_subtotal_paise": 30000,
        "discount_paise": 3000,
        "tax_paise": 1350,
        "tax_rate_bps": 500,
        "line_total_paise": 28350,
        "notes": null,
        "customizations": []
      }
    ],
    "rejection_reason": null,
    "cancellation_reason": null,
    "subtotal_paise": 30000,
    "discount_paise": 3000,
    "tax_paise": 1350,
    "delivery_charge_paise": 0,
    "total_paise": 28350,
    "tax_breakdown": [
      {
        "name": "GST 5%",
        "rate_bps": 500,
        "taxable_paise": 27000,
        "tax_paise": 1350
      }
    ],
    "discount_breakdown": [
      {
        "offer_id": "cc42ac8d-287e-4d80-8984-7a13d5d9dbd7",
        "name": "Ten off",
        "code": "TEN",
        "discount_paise": 3000
      }
    ],
    "coupon_code": "TEN",
    "customer_email": null,
    "payments": [
      {
        "id": "5992097e-fbd5-424c-b533-96d64d1fd621",
        "method": "cash",
        "gateway": null,
        "status": "pending",
        "amount_paise": 28350,
        "refunded_paise": 0,
        "paid_at": null
      }
    ]
  },
  "guest_token": "vXFdhzQj1ryssa5bKnJ7RSshqM87dLQ1",
  "duplicate": false,
  "next_action": null
}
```

## Admin accepts the order (stops the alarm)

```http
POST /api/v1/admin/orders/{id}/accept
```
Response `200`:

```json
{
  "id": "40523be4-9dd1-4084-aa01-b5f5a1fd4f2a",
  "order_number": "CC261113-X687B",
  "order_type": "dine_in",
  "source": "qr_web",
  "status": "accepted",
  "payment_method": "cash",
  "payment_status": "unpaid",
  "currency": "INR",
  "customer_name": "Ravi",
  "customer_phone": "+919812345678",
  "table_label": "Table 1 (Window)",
  "table_id": "6e235d29-c1a1-4893-a1e0-a535e73aece6",
  "table_session_id": "37020602-74a1-4fa3-bfb3-11746e05bae1",
  "delivery_address": null,
  "special_instructions": "Less sugar",
  "created_at": "2026-11-13T07:27:46.033599+00:00",
  "updated_at": "2026-11-13T07:27:46.078695+00:00",
  "items": [
    {
      "id": "8d919f98-02ea-420d-b23e-f7dca7154073",
      "menu_item_id": "0bc7b8b9-ccf3-4d8e-af4a-8525f561f851",
      "name": "Latte",
      "variant_name": "Small",
      "quantity": 2,
      "unit_price_paise": 15000,
      "line_subtotal_paise": 30000,
      "discount_paise": 3000,
      "tax_paise": 1350,
      "tax_rate_bps": 500,
      "line_total_paise": 28350,
      "notes": null,
      "customizations": []
    }
  ],
  "rejection_reason": null,
  "cancellation_reason": null,
  "subtotal_paise": 30000,
  "discount_paise": 3000,
  "tax_paise": 1350,
  "delivery_charge_paise": 0,
  "total_paise": 28350,
  "tax_breakdown": [
    {
      "name": "GST 5%",
      "rate_bps": 500,
      "taxable_paise": 27000,
      "tax_paise": 1350
    }
  ],
  "discount_breakdown": [
    {
      "offer_id": "cc42ac8d-287e-4d80-8984-7a13d5d9dbd7",
      "name": "Ten off",
      "code": "TEN",
      "discount_paise": 3000
    }
  ],
  "coupon_code": "TEN",
  "customer_email": null,
  "payments": [
    {
      "id": "5992097e-fbd5-424c-b533-96d64d1fd621",
      "method": "cash",
      "gateway": null,
      "status": "pending",
      "amount_paise": 28350,
      "refunded_paise": 0,
      "paid_at": null
    }
  ]
}
```

## Admin confirms cash at the counter

```http
POST /api/v1/admin/payments/{id}/confirm-cash
Content-Type: application/json
```
```json
{
  "amount_received_paise": 50000
}
```
Response `200`:

```json
{
  "id": "5992097e-fbd5-424c-b533-96d64d1fd621",
  "order_id": "40523be4-9dd1-4084-aa01-b5f5a1fd4f2a",
  "method": "cash",
  "gateway": null,
  "gateway_order_id": null,
  "gateway_payment_id": null,
  "amount_paise": 28350,
  "currency": "INR",
  "status": "paid",
  "verification_status": "verified",
  "refunded_paise": 0,
  "tendered_paise": 50000,
  "change_paise": 21650,
  "confirmed_by_admin_id": "d5af2315-6b49-4dd4-8357-d4a1dfafe94f",
  "paid_at": "2026-11-13T07:27:46.109865+00:00",
  "verified_at": "2026-11-13T07:27:46.109865+00:00",
  "created_at": "2026-11-13T07:27:46.046854+00:00",
  "failure_reason": null
}
```

## Invalid transition is rejected safely

```http
POST /api/v1/admin/orders/{id}/status
Content-Type: application/json
```
```json
{
  "status": "ready"
}
```
Response `409`:

```json
{
  "error": {
    "code": "invalid_transition",
    "message": "An order cannot move from 'accepted' to 'ready'",
    "details": {
      "from": "accepted",
      "to": "ready",
      "allowed": [
        "cancelled",
        "preparing"
      ]
    }
  }
}
```

## Start an online payment (Razorpay)

Only SDK-safe fields are returned. Success is confirmed server-side (webhook / provider fetch), never by the client callback.

```http
POST /api/v1/payments/orders/{id}/initiate
Content-Type: application/json
```
```json
{
  "gateway": "razorpay"
}
```
Response `200`:

```json
{
  "payment_id": "91e17e26-bd37-4ae5-8b4b-8ccf7a1d34da",
  "gateway": "razorpay",
  "amount_paise": 10500,
  "currency": "INR",
  "client_payload": {
    "order_id": "razorpay_order_1",
    "amount": 10500,
    "currency": "INR"
  },
  "expires_at": "2026-11-13T07:47:46.165946+00:00"
}
```

## Admin manually assigns a rider

```http
POST /api/v1/admin/orders/{id}/assign
Content-Type: application/json
```
```json
{
  "rider_id": "b45a0bfc-1894-480f-af13-b06178cc3e0c"
}
```
Response `200`:

```json
{
  "assignment_id": "0b399be3-5039-4803-bdde-e73e0348f516",
  "status": "offered",
  "rider_id": "b45a0bfc-1894-480f-af13-b06178cc3e0c",
  "order_id": "3601d9bf-9ec1-4285-a492-293692d16308"
}
```

## Rider accepts

```http
POST /api/v1/deliveries/{assignment_id}/accept
```
Response `200`:

```json
{
  "assignment_id": "0b399be3-5039-4803-bdde-e73e0348f516",
  "status": "accepted",
  "offered_at": "2026-11-13T07:27:46.333298+00:00",
  "responded_at": "2026-11-13T07:27:46.352940+00:00",
  "order": {
    "id": "3601d9bf-9ec1-4285-a492-293692d16308",
    "order_number": "CC261113-KQULW",
    "order_type": "delivery",
    "source": "customer_app",
    "status": "rider_assigned",
    "payment_method": "cash",
    "payment_status": "unpaid",
    "currency": "INR",
    "customer_name": "Dee",
    "customer_phone": "+919000022222",
    "table_label": null,
    "table_id": null,
    "table_session_id": null,
    "delivery_address": {
      "line1": "12 MG Road",
      "line2": null,
      "landmark": null,
      "city": null,
      "postal_code": "560001",
      "latitude": null,
      "longitude": null
    },
    "special_instructions": null,
    "created_at": "2026-11-13T07:27:46.256104+00:00",
    "updated_at": "2026-11-13T07:27:46.355587+00:00",
    "items": [
      {
        "id": "1d783de9-244d-4490-aca4-6aea49e0e2f7",
        "menu_item_id": "d3991562-6324-4bbd-9310-59674992614e",
        "name": "Cake",
        "variant_name": null,
        "quantity": 1,
        "unit_price_paise": 10000,
        "line_subtotal_paise": 10000,
        "discount_paise": 0,
        "tax_paise": 500,
        "tax_rate_bps": 500,
        "line_total_paise": 10500,
        "notes": null,
        "customizations": []
      }
    ],
    "rejection_reason": null,
    "cancellation_reason": null,
    "collect_cash_paise": 13500
  }
}
```

## Rider sends GPS

```http
POST /api/v1/tracking/location
Content-Type: application/json
```
```json
{
  "assignment_id": "0b399be3-5039-4803-bdde-e73e0348f516",
  "latitude": 12.9716,
  "longitude": 77.5946,
  "accuracy_m": 8.5
}
```
Response `200`:

```json
{
  "id": 1,
  "recorded_at": "2026-11-13T07:27:46.407753+00:00"
}
```

## Customer tracks the delivery

```http
GET /api/v1/tracking/orders/{order_id}
```
Response `200`:

```json
{
  "order_id": "3601d9bf-9ec1-4285-a492-293692d16308",
  "tracking_active": true,
  "update_interval_seconds": 10,
  "location": {
    "latitude": 12.9716,
    "longitude": 77.5946,
    "accuracy_m": 8.5,
    "recorded_at": "2026-11-13T07:27:46.407753+00:00",
    "age_seconds": 0,
    "is_stale": false
  }
}
```

## Customer reveals / regenerates the delivery OTP

```http
POST /api/v1/deliveries/orders/{order_id}/otp
```
Response `200`:

```json
{
  "otp": "224973",
  "valid_minutes": 240
}
```

## Rider enters a wrong OTP

```http
POST /api/v1/deliveries/{assignment_id}/verify-otp
Content-Type: application/json
```
```json
{
  "otp": "000000"
}
```
Response `422`:

```json
{
  "error": {
    "code": "otp_incorrect",
    "message": "Incorrect delivery code",
    "details": {
      "attempts_left": 4
    }
  }
}
```

## Rider enters the correct OTP: delivered

```http
POST /api/v1/deliveries/{assignment_id}/verify-otp
Content-Type: application/json
```
```json
{
  "otp": "<6-digit code from the customer>"
}
```
Response `200`:

```json
{
  "order_id": "3601d9bf-9ec1-4285-a492-293692d16308",
  "status": "delivered",
  "delivered_at": "2026-11-13T07:27:46.452527+00:00"
}
```

## Register an FCM device token

```http
POST /api/v1/notifications/devices
Content-Type: application/json
```
```json
{
  "install_id": "dee-phone-1",
  "fcm_token": "<FCM token>",
  "device_model": "Pixel 8",
  "app_version": "1.0.0"
}
```
Response `201`:

```json
{
  "device_id": "93d4cc8c-5ebd-4cc9-9f4d-0405f3f13bb3",
  "install_id": "dee-phone-1",
  "notifications_enabled": true
}
```

## Customer sends a chat message

```http
POST /api/v1/chat/conversations/{id}/messages
Content-Type: application/json
```
```json
{
  "body": "Please ring the bell",
  "client_msg_id": "c-1"
}
```
Response `201`:

```json
{
  "id": 1,
  "conversation_id": "fa1f21d3-33b6-4d21-9943-3be5f5fafc41",
  "sender_role": "customer",
  "sender_id": "e95ca1a6-3623-49ec-a988-4798dc71d9f1",
  "body": "Please ring the bell",
  "client_msg_id": "c-1",
  "created_at": "2026-11-13T07:27:46.492982+00:00"
}
```

## Admin inbox

```http
GET /api/v1/chat/conversations
```
Response `200`:

```json
[
  {
    "id": "fa1f21d3-33b6-4d21-9943-3be5f5fafc41",
    "kind": "customer_admin",
    "subject": "Order CC261113-KQULW",
    "order_id": "3601d9bf-9ec1-4285-a492-293692d16308",
    "status": "open",
    "participant": {
      "role": "customer",
      "id": "e95ca1a6-3623-49ec-a988-4798dc71d9f1",
      "name": "Dee",
      "phone": "+919000022222"
    },
    "last_message": {
      "id": 1,
      "conversation_id": "fa1f21d3-33b6-4d21-9943-3be5f5fafc41",
      "sender_role": "customer",
      "sender_id": "e95ca1a6-3623-49ec-a988-4798dc71d9f1",
      "body": "Please ring the bell",
      "client_msg_id": "c-1",
      "created_at": "2026-11-13T07:27:46.492982+00:00",
      "is_internal": false
    },
    "last_message_at": "2026-11-13T07:27:46.494848+00:00",
    "unread_count": 1
  }
]
```

## Invoice data

```http
GET /api/v1/invoices/order/{order_id}
```
Response `200`:

```json
{
  "id": "b86573e9-2cdc-4265-bcd5-c2759fb7ed6e",
  "invoice_number": "CC-2026-000001",
  "order_id": "3601d9bf-9ec1-4285-a492-293692d16308",
  "order_number": "CC261113-KQULW",
  "order_type": "delivery",
  "table_label": null,
  "issued_at": "2026-11-13T07:27:46.522930+00:00",
  "order_created_at": "2026-11-13T07:27:46.256104+00:00",
  "currency": "INR",
  "seller": {
    "name": "Crescent Cafe",
    "address": null,
    "gstin": null,
    "phone": null,
    "email": null
  },
  "buyer": {
    "name": "Dee",
    "phone": "+919000022222",
    "email": "dee52828@example.com",
    "address": {
      "line1": "12 MG Road",
      "line2": null,
      "landmark": null,
      "city": null,
      "postal_code": "560001",
      "latitude": null,
      "longitude": null
    }
  },
  "payment_method": "cash",
  "payment_status": "unpaid",
  "items": [
    {
      "description": "Cake",
      "quantity": 1,
      "unit_price_paise": 10000,
      "discount_paise": 0,
      "tax_rate_bps": 500,
      "tax_paise": 500,
      "total_paise": 10500
    }
  ],
  "subtotal_paise": 10000,
  "discount_paise": 0,
  "tax_paise": 500,
  "delivery_charge_paise": 3000,
  "total_paise": 13500,
  "tax_breakdown": [
    {
      "name": "GST 5%",
      "rate_bps": 500,
      "taxable_paise": 10000,
      "tax_paise": 500
    }
  ],
  "prices_include_tax": false,
  "footer": null
}
```

## Daily/monthly report

```http
GET /api/v1/admin/reports/summary?granularity=day
```
Response `200`:

```json
{
  "range": {
    "start": "2026-11-01",
    "end": "2026-11-13",
    "timezone": "Asia/Kolkata",
    "granularity": "day"
  },
  "totals": {
    "orders_placed": 3,
    "orders_completed": 1,
    "orders_cancelled": 0,
    "orders_rejected": 0,
    "gross_sales_paise": 13500,
    "discount_paise": 0,
    "tax_paise": 500,
    "delivery_charge_paise": 3000,
    "payments_received_paise": 28350,
    "cash_collected_paise": 28350,
    "cash_collections": 1,
    "refunds_paise": 0,
    "net_sales_paise": 13500
  },
  "series": [
    {
      "period": "2026-11-13",
      "orders_placed": 3,
      "orders_completed": 1,
      "gross_sales_paise": 13500,
      "discount_paise": 0,
      "tax_paise": 500,
      "delivery_charge_paise": 3000,
      "refunds_paise": 0,
      "net_sales_paise": 13500
    }
  ],
  "by_order_type": {
    "delivery": {
      "orders": 1,
      "sales_paise": 13500
    }
  },
  "by_payment_method": {
    "cash": {
      "orders": 1,
      "sales_paise": 13500
    }
  },
  "payments_by_gateway": {
    "cash": {
      "payments": 1,
      "received_paise": 28350,
      "refunded_paise": 0
    }
  },
  "coupon_usage": {
    "Ten off": {
      "redemptions": 1,
      "discount_paise": 3000
    }
  },
  "best_selling_items": [
    {
      "item": "Cake",
      "quantity": 1,
      "revenue_paise": 10500
    }
  ],
  "item_quantities": [
    {
      "item": "Cake",
      "quantity": 1,
      "revenue_paise": 10500
    }
  ]
}
```

## Admin dashboard

```http
GET /api/v1/admin/dashboard
```
Response `200`:

```json
{
  "pending_decisions": 0,
  "active_orders_by_status": {
    "accepted": 1,
    "payment_pending": 1
  },
  "orders_placed_today": 3,
  "sales_today_paise": 13500,
  "pending_counter_payments": 1,
  "active_deliveries": 0,
  "available_riders": 1,
  "unread_messages": 1,
  "accepting_orders": true,
  "setup_warnings": [
    "Opening hours are not set (the cafe is treated as always open)."
  ],
  "server_time": "2026-11-13T07:27:46.565465+00:00"
}
```

## Generate a table QR (shown once)

```http
POST /api/v1/admin/tables/{id}/qr
```
Response `200`:

```json
{
  "table_id": "6e235d29-c1a1-4893-a1e0-a535e73aece6",
  "table_number": "1",
  "label": "Window",
  "area": "Ground",
  "qr_token_id": "2b8d9093-4870-41c5-a86e-2fee76fd6a89",
  "token": "gwGepJhk9wE_dJu_yOKSxeH2j5vN9Vtl",
  "url": "https://order.example.com/t/gwGepJhk9wE_dJu_yOKSxeH2j5vN9Vtl",
  "generated_at": "2026-11-13T07:27:46.570859+00:00"
}
```

## Standard error format

```http
GET /api/v1/orders/00000000-0000-0000-0000-000000000000
```
Response `404`:

```json
{
  "error": {
    "code": "order_not_found",
    "message": "Order not found",
    "details": null
  }
}
```
