# Remaining Ecommerce Functions

This document lists the ecommerce work that is still deferred for later.

The core commerce layer is in place:
- unified auth and accounts
- catalog listings
- carts and checkout
- manual fulfillment
- returns and refunds
- B2B price overrides
- marketplace settlement splits
- audit events and throttling

The items below are intentionally left for later so the current backend can stay stable while the frontend and payment layers are built in sequence.

## Priority 1: Web Frontend Integration

- Connect the Next.js storefront to the commerce APIs.
- Add cross-origin support if the frontend and backend are on different origins.
- Finalize the browser auth contract for cookie + CSRF requests.
- Build public storefront pages that consume the catalog, wishlist, reviews, and content APIs.

## Priority 2: Background Jobs

- Expire stale carts and reservations automatically.
- Release reserved stock when an order is abandoned or payment never arrives.
- Send notifications for order placement, shipment, return receipt, and payout updates.
- Retry failed side effects such as settlement state transitions or future gateway callbacks.

## Priority 3: Storefront UX

- Improve public search, sorting, and category browsing.
- Add featured products, homepage sections, and SEO-oriented content pages.
- Add richer buyer order history and filtering.
- Add wishlist UI and review submission UI.

## Priority 4: Operational Safety

- Add idempotency keys for checkout and future webhook endpoints.
- Add more targeted throttles for sensitive endpoints.
- Expand audit coverage for every commerce mutation.
- Add reconciliation tools for manual stock adjustments against active reservations.

## Priority 5: Fulfillment Expansion

- Keep fulfillment manual for now.
- If carrier integration is added later, implement:
  - labels
  - tracking sync
  - shipping webhooks
  - split shipment handling

## Priority 6: Payment Gateway Integration

- Build payment intent and payment status tracking.
- Connect the gateway webhook flow.
- Support capture, failure, reversal, and refund callbacks.
- Wire payment completion into order state transitions.

## Priority 7: Marketplace Enhancements

- Add payout provider integration after the gateway layer is ready.
- Add reconciliation reports for marketplace settlements and payouts.
- Add reversal handling for returns that happen after seller payout.
- Extend settlement reports for finance and seller self-service.

## Deferred by Choice

- Guest checkout is not planned right now.
- Automatic carrier fulfillment is not planned right now.
- Party-specific pricing beyond seller-defined B2B overrides is not planned right now.
- Advanced recommendation or merchandising logic is not planned right now.

## Summary

The backend now has the commerce primitives required for a real ecommerce platform. The remaining work is mostly:
- frontend integration
- async/background processing
- payment gateway wiring
- operational polish

Those can be added later without reworking the current identity or commerce model.
