"""
Sale App URL Configuration
==========================

This module defines all API endpoints for the sale app.

Each URL pattern includes documentation explaining:
- Endpoint path and purpose
- HTTP methods supported
- Interaction with views/models
- Common query parameters for filtering
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    # Master Data
    StateViewSet, BusinessLocationViewSet, PartyViewSet,
    # Orders
    OrderViewSet,
    DeliveryChallanViewSet,
    # Sales
    InvoiceViewSet, ReceiptViewSet, CreditNoteViewSet,
    # Purchase
    PurchaseOrderViewSet, GoodReceiptNoteViewSet,
    PurchaseInvoiceViewSet, DebitNoteViewSet, PaymentOutViewSet,
    # Reports
    ReportsViewSet,
    # Pricing and quotations
    PriceListViewSet, QuotationViewSet,
)


router = DefaultRouter()

# ===================== MASTER DATA ENDPOINTS =====================

# States for GST place of supply
# GET /sale/states/ - List all Indian states
# GET /sale/states/{id}/ - Get state detail
router.register(r'states', StateViewSet, basename='states')

# Business Locations (Multi-GSTIN)
# GET /sale/business-locations/ - List all locations
# POST /sale/business-locations/ - Create new location
# GET /sale/business-locations/{id}/ - Get location
# PUT /sale/business-locations/{id}/ - Update location
# DELETE /sale/business-locations/{id}/ - Delete location
router.register(r'business-locations', BusinessLocationViewSet, basename='business-locations')

# Party Master (Customers/Suppliers)
# GET /sale/parties/ - List all parties
# POST /sale/parties/ - Create party
# GET /sale/parties/{id}/ - Get party detail
# GET /sale/parties/{id}/ledger/ - Party ledger
# Filtering: /sale/parties/?party_type=Customer&state=1
router.register(r'parties', PartyViewSet, basename='parties')

# ===================== ORDER ENDPOINTS =====================

# POS Orders (Cart/Hold)
# GET /sale/orders/ - List orders
# POST /sale/orders/ - Create new order (cart)
# GET /sale/orders/{id}/ - Get order detail
# POST /sale/orders/{id}/hold/ - Hold order
# POST /sale/orders/{id}/recall/ - Recall held order
# POST /sale/orders/{id}/convert/ - Convert to invoice
# Filtering: /sale/orders/?status=Hold&business_location=1
router.register(r'orders', OrderViewSet, basename='orders')

# ===================== CHALLAN ENDPOINTS =====================

# Delivery Challans
# GET /sale/challans/ - List challans
# POST /sale/challans/ - Create challan
# POST /sale/challans/combine/ - Combine multiple challans into one invoice
router.register(r'challans', DeliveryChallanViewSet, basename='challans')

# ===================== SALES ENDPOINTS =====================

# Sales Invoices
# GET /sale/invoices/ - List invoices (paginated)
# POST /sale/invoices/ - Create invoice (draft)
# GET /sale/invoices/{id}/ - Get invoice detail
# PUT /sale/invoices/{id}/ - Update draft invoice
# POST /sale/invoices/{id}/finalize/ - Finalize & deduct stock
# POST /sale/invoices/{id}/cancel/ - Cancel invoice
# GET /sale/invoices/{id}/print/ - Print-friendly data
# Filtering: /sale/invoices/?party=1&status=Finalized&business_location=1
router.register(r'invoices', InvoiceViewSet, basename='invoices')

# Receipts (Payments Received)
# GET /sale/receipts/ - List receipts
# POST /sale/receipts/ - Record payment
# GET /sale/receipts/{id}/ - Get receipt
# Filtering: /sale/receipts/?invoice=1&party=1
router.register(r'receipts', ReceiptViewSet, basename='receipts')

# Credit Notes (Sales Returns)
# GET /sale/credit-notes/ - List credit notes
# POST /sale/credit-notes/ - Create return
# GET /sale/credit-notes/{id}/ - Get detail
router.register(r'credit-notes', CreditNoteViewSet, basename='credit-notes')

# ===================== PURCHASE ENDPOINTS =====================

# Purchase Orders
# GET /sale/purchase-orders/ - List POs
# POST /sale/purchase-orders/ - Create PO
# GET /sale/purchase-orders/{id}/ - Get PO detail
# POST /sale/purchase-orders/{id}/send/ - Mark as sent
# Filtering: /sale/purchase-orders/?status=Draft&supplier=1
router.register(r'purchase-orders', PurchaseOrderViewSet, basename='purchase-orders')

# Good Receipt Notes (GRN)
# GET /sale/grns/ - List GRNs
# POST /sale/grns/ - Create GRN
# GET /sale/grns/{id}/ - Get GRN detail
# POST /sale/grns/{id}/create-invoice/ - Create purchase invoice
# Filtering: /sale/grns/?supplier=1&purchase_order=1
router.register(r'grns', GoodReceiptNoteViewSet, basename='grns')

# Purchase Invoices
# GET /sale/purchase-invoices/ - List purchase invoices
# POST /sale/purchase-invoices/ - Create purchase invoice
# GET /sale/purchase-invoices/{id}/ - Get detail
# POST /sale/purchase-invoices/{id}/finalize/ - Finalize & add stock
# POST /sale/purchase-invoices/{id}/cancel/ - Cancel
router.register(r'purchase-invoices', PurchaseInvoiceViewSet, basename='purchase-invoices')

# Debit Notes (Purchase Returns)
# GET /sale/debit-notes/ - List debit notes
# POST /sale/debit-notes/ - Create return
router.register(r'debit-notes', DebitNoteViewSet, basename='debit-notes')

# Payments Out
# GET /sale/payments-out/ - List payments
# POST /sale/payments-out/ - Record payment
router.register(r'payments-out', PaymentOutViewSet, basename='payments-out')

# ===================== REPORTS ENDPOINTS =====================

# Reports
# GET /sale/reports/daily-sales/?date=2025-04-28
router.register(r'reports', ReportsViewSet, basename='reports')

# Quotations
router.register(r'quotations', QuotationViewSet, basename='quotations')

# Price Lists
router.register(r'price-lists', PriceListViewSet, basename='price-lists')


urlpatterns = [
    path('', include(router.urls)),
]


# ===================== ENDPOINT SUMMARY =====================

"""
Complete API Endpoint List:

MASTER DATA:
  GET    /sale/states/                      - List states
  GET    /sale/states/{id}/                 - State detail

  GET    /sale/business-locations/          - List locations
  POST   /sale/business-locations/          - Create location
  GET    /sale/business-locations/{id}/     - Location detail
  PUT    /sale/business-locations/{id}/     - Update location

  GET    /sale/parties/                     - List parties (filter: party_type, state)
  POST   /sale/parties/                     - Create party
  GET    /sale/parties/{id}/                - Party detail
  GET    /sale/parties/{id}/ledger/         - Party ledger statement

ORDERS (POS):
  GET    /sale/orders/                      - List orders (filter: status, party, location)
  POST   /sale/orders/                      - Create order (cart)
  GET    /sale/orders/{id}/                 - Order detail
  POST   /sale/orders/{id}/hold/            - Hold order
  POST   /sale/orders/{id}/recall/          - Recall order
  POST   /sale/orders/{id}/convert/         - Convert to invoice

SALES:
  GET    /sale/invoices/                    - List invoices (filter: party, finalized, date)
  POST   /sale/invoices/                    - Create invoice (draft)
  GET    /sale/invoices/{id}/               - Invoice detail with tax breakdown
  POST   /sale/invoices/{id}/finalize/     - Finalize & deduct stock
  POST   /sale/invoices/{id}/cancel/        - Cancel invoice
  GET    /sale/invoices/{id}/print/         - Print data

  GET    /sale/receipts/                    - List receipts
  POST   /sale/receipts/                    - Record payment
  GET    /sale/receipts/{id}/               - Receipt detail

  GET    /sale/credit-notes/                 - List credit notes
  POST   /sale/credit-notes/                 - Create return

PURCHASE:
  GET    /sale/purchase-orders/             - List POs
  POST   /sale/purchase-orders/             - Create PO
  GET    /sale/purchase-orders/{id}/       - PO detail
  POST   /sale/purchase-orders/{id}/send/  - Mark as sent

  GET    /sale/grns/                        - List GRNs
  POST   /sale/grns/                        - Create GRN
  GET    /sale/grns/{id}/                   - GRN detail
  POST   /sale/grns/{id}/create-invoice/    - Convert to purchase invoice

  GET    /sale/purchase-invoices/           - List purchase invoices
  POST   /sale/purchase-invoices/           - Create purchase invoice
  GET    /sale/purchase-invoices/{id}/      - Detail
  POST   /sale/purchase-invoices/{id}/finalize/ - Finalize & add stock

  GET    /sale/debit-notes/                 - List debit notes
  POST   /sale/debit-notes/                 - Create return

  GET    /sale/payments-out/                - List payments
  POST   /sale/payments-out/                - Record payment

REPORTS:
  GET    /sale/reports/daily-sales/         - Daily sales summary
"""
