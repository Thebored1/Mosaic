"""
Master Data URL Configuration
=============================

Shared master data for both sales and purchase:
- States: Indian states for GST
- Parties: Customer & Supplier master

Note: Business locations/warehouses moved to configuration app.
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from sale.views import StateViewSet, PartyViewSet


router = DefaultRouter()

# States for GST place of supply
# GET /v1/master/states/ - List all Indian states
# GET /v1/master/states/{id}/ - Get state detail
router.register(r'states', StateViewSet, basename='states')

# Party Master (Customers/Suppliers)
# GET /v1/master/parties/ - List all parties
# POST /v1/master/parties/ - Create party
# GET /v1/master/parties/{id}/ - Get party detail
# GET /v1/master/parties/{id}/ledger/ - Party ledger
router.register(r'parties', PartyViewSet, basename='parties')


urlpatterns = [
    path('', include(router.urls)),
]