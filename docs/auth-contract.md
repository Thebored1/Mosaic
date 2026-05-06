# Auth Contract

This document defines how client applications authenticate against the API.

## Core Rules

- Browser clients use an `HttpOnly` auth cookie plus CSRF protection.
- Desktop and non-browser clients may continue using bearer tokens in the `Authorization` header.
- Every authenticated principal has one `UserAccount`.
- A `UserAccount` may exist without an organization.
- Normal organization users authenticate with `ApiToken`.
- Super admin access uses `SuperAdminToken`.
- Tokens are opaque to the client and should never be parsed.

## Header Format

```http
Authorization: Bearer <token>
```

## Browser Flow

### 1. Bootstrap CSRF

`GET /v1/account/csrf/`

Use this first in the browser to obtain a CSRF cookie and token.

Response includes:
- `csrfToken`

The browser must send:
- `credentials: 'include'`
- `X-CSRFToken: <csrfToken>`

### 2. Onboard or Login with Cookie Transport

Browser requests must add:
- `X-Auth-Transport: cookie`

That tells the backend to set the auth token as an `HttpOnly` cookie and omit the token from the JSON body.

### `POST /v1/account/signup/`

Creates an ecommerce-only account with no organization.

Request fields:
- `username`
- `email`
- `password`
- `first_name`
- `last_name`
- `phone`

Response includes:
- `organization` as `null`
- `account`
- `token` for non-browser clients only

Browser response:
- auth cookie is set
- `token` is omitted from JSON

Use case:
- Initial B2C/B2B buyer signup before organization creation.

### `POST /v1/account/create-organization/`

Upgrades the authenticated ecommerce account into an organization owner.

Requirements:
- authenticated user
- no existing organization membership

Request fields:
- `name`
- `trade_name`
- `gstin`
- `address`
- `phone`
- `email`

Response includes:
- `organization`
- `account`

Use case:
- Buyer becomes a seller or company owner later without changing login.

### `POST /v1/account/onboard/`

Creates the first organization and its owner in one transaction.

Request fields:
- `organization_name`
- `organization_trade_name`
- `organization_gstin`
- `organization_address`
- `organization_phone`
- `organization_email`
- `owner_username`
- `owner_email`
- `owner_password`
- `owner_first_name`
- `owner_last_name`
- `owner_phone`

Response includes:
- `organization`
- `owner`
- `token` for non-browser clients only

Browser response:
- auth cookie is set
- `token` is omitted from JSON

Use case:
- One-step bootstrap for a brand-new organization owner.

### `POST /v1/account/login/`

Authenticates an existing organization user with username and password.

Request fields:
- `username`
- `password`

Response includes:
- `organization`
- `account`
- `token` for non-browser clients only

Browser response:
- auth cookie is set
- `token` is omitted from JSON

Use case:
- Sign in after onboarding or after token loss.

### `GET /v1/account/me/`

Returns the authenticated principal's current account shape.

Response includes:
- `user`
- `account`
- `organization`

Use case:
- Determine whether the current user is still ecommerce-only or already belongs to an organization.

### `POST /v1/account/refresh/`

Rotates the current bearer token.

Requirements:
- Browser clients: send `credentials: 'include'` and `X-CSRFToken`.
- Non-browser clients: send the current bearer token in `Authorization`.

Response:
- `token` for non-browser clients only

Browser response:
- auth cookie is updated
- `status: refreshed`

Use case:
- Replace an existing token without creating a new account.

### `POST /v1/account/logout/`

Revokes the current bearer token.

Requirements:
- Browser clients: send `credentials: 'include'` and `X-CSRFToken`.
- Non-browser clients: send the current bearer token in `Authorization`.

Response:
- `204 No Content`

Browser response:
- auth cookie is cleared

Use case:
- Explicit sign out.

### `POST /v1/account/password-reset/request/`

Starts password reset without revealing whether the account exists.

Request fields:
- `identifier`: username or email

Response:
- `status: ok`

Server behavior:
- If a matching user exists and has an email, the backend sends a reset link by email.
- The API response must not include `uid`, `token`, or `reset_url`.
- Missing users get the same response as real users.

### `POST /v1/account/password-reset/confirm/`

Completes password reset using the link from email.

Request fields:
- `uid`
- `token`
- `new_password`

Response:
- `status: password_updated`

Server behavior:
- Validates Django's password-reset token.
- Updates the password.
- Revokes active API and super-admin tokens for that user.

## Protected Requests

### Browser clients

After onboarding or login, the browser keeps the auth cookie and sends:

```http
Cookie: mosaic_auth=...
X-CSRFToken: <csrfToken>
```

The frontend should use:

```js
fetch(url, {
  credentials: 'include',
  headers: { 'X-CSRFToken': csrfToken }
})
```

### Non-browser clients

```http
Authorization: Bearer <token>
```

The backend authenticates the request and resolves:
- `request.user`
- `request.auth`

## Client Storage Guidance

### Web

- Prefer the `HttpOnly`, `Secure`, `SameSite=Lax` auth cookie.
- Do not store the auth token in `localStorage` or `sessionStorage`.
- Keep the CSRF token in memory or read it from the CSRF cookie as needed.

### Desktop

- Store the token in the operating system's secure credential store.
- Do not place it in plain JSON, config files, or local logs.

## Expected Flow

1. Shopper or buyer signs up through `signup`.
2. Browser receives an auth cookie and CSRF cookie.
3. Browser sends `credentials: 'include'` plus `X-CSRFToken` on unsafe requests.
4. If needed later, the authenticated user calls `create-organization/` and becomes an `Owner`.
5. Desktop/API clients keep using bearer tokens.
6. Client refreshes or logs out as needed.

## Deployment Settings Required

Password reset depends on outbound email. Configure SMTP before enabling it in any real environment.

Required frontend/reset settings:

```env
FRONTEND_ORIGINS=https://app.example.com
PASSWORD_RESET_FRONTEND_URL=https://app.example.com
DEFAULT_FROM_EMAIL=no-reply@example.com
```

Required SMTP settings for the Django deployment:

```env
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=smtp-user
EMAIL_HOST_PASSWORD=smtp-password
EMAIL_USE_TLS=1
EMAIL_USE_SSL=0
```

Token lifetime settings:

```env
API_TOKEN_MAX_AGE_SECONDS=2592000
SUPER_ADMIN_TOKEN_MAX_AGE_SECONDS=604800
```

Notes:
- `API_TOKEN_MAX_AGE_SECONDS` defaults to 30 days.
- `SUPER_ADMIN_TOKEN_MAX_AGE_SECONDS` defaults to 7 days.
- Set a value less than or equal to `0` only if you intentionally want non-expiring tokens.
- Existing tokens receive expiry during the `configuration` migration.

Deployment checklist:
- Run migrations after deploying token expiry fields.
- Verify the SMTP account can send mail from `DEFAULT_FROM_EMAIL`.
- Verify reset links point to the frontend route that collects `uid`, `token`, and `new_password`.
- Keep SMTP credentials in environment or a secret manager, not in source control.
- Do not log reset URLs or reset tokens.

## Notes

- Public onboarding is only for the initial organization owner.
- Normal employee user creation remains token-scoped.
- Ecommerce-only accounts cannot use org-scoped ERP endpoints until they create or join an organization.
- Super admin tokens are system-level credentials and should be handled separately from org user tokens.
- If the frontend and backend are on different origins, `FRONTEND_ORIGINS` must include the frontend origin so Django accepts the CSRF request.
