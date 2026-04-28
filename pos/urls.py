"""
POS App URL Configuration
=========================

This module defines all API endpoints for POS operations.

Endpoint Summary:
    /shifts/ - Shift management
    /transactions/ - Cash transactions
"""

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ShiftViewSet, CashTransactionViewSet


router = DefaultRouter()

# Shift endpoints
# GET /shifts/ - List all shifts
# POST /shifts/ - Open new shift
# GET /shifts/{id}/ - Get shift details
# POST /shifts/{id}/close/ - Close shift
# GET /shifts/{id}/summary/ - Get shift summary
router.register(r'shifts', ShiftViewSet, basename='shifts')

# Cash Transaction endpoints
# GET /transactions/ - List all transactions
# POST /transactions/ - Create transaction
# GET /transactions/{id}/ - Get transaction
# DELETE /transactions/{id}/ - Delete transaction
router.register(r'transactions', CashTransactionViewSet, basename='transactions')


urlpatterns = [
    path('', include(router.urls)),
    # Nested route for shift transactions
    path(
        'shifts/<int:shift_pk>/transactions/',
        CashTransactionViewSet.as_view({'get': 'list', 'post': 'create'}),
        name='shift-transactions-list'
    ),
    path(
        'shifts/<int:shift_pk>/transactions/<int:pk>/',
        CashTransactionViewSet.as_view({'get': 'retrieve', 'put': 'update', 'delete': 'destroy'}),
        name='shift-transactions-detail'
    ),
]