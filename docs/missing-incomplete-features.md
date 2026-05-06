# Missing & Incomplete Features

This document lists features that are genuinely missing or incomplete in the backend, separate from the intentionally deferred work documented in `remaining-ecommerce-functions.md`.

## Non-Functional (Placeholder)

### E-way/E-invoice Generation
- Model methods exist but generate dummy/placeholder data
- Requires real GST portal API integration (not yet available)

---

## Still Missing (Lower Priority)

1. **Password reset flow** - No API endpoint
2. **Print template customization** - No API for invoice/receipt templates
3. **Bank/Cheque tracking** - Not implemented
4. **Barcode/QR code generation** - Not implemented
5. **Background job infrastructure** - Already has Celery, just needs more tasks

---

## Notes

Most core ERP functionality is complete. The remaining items are operational utilities rather than core business logic.