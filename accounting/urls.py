from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import ExpenseViewSet, FiscalPeriodViewSet, JournalEntryViewSet, LedgerAccountViewSet, PostingBatchViewSet, ReconciliationViewSet, ReportsViewSet


router = DefaultRouter()
router.register(r'accounts', LedgerAccountViewSet, basename='accounting-accounts')
router.register(r'journal-entries', JournalEntryViewSet, basename='accounting-journal-entries')
router.register(r'posting-batches', PostingBatchViewSet, basename='accounting-posting-batches')
router.register(r'fiscal-periods', FiscalPeriodViewSet, basename='accounting-fiscal-periods')
router.register(r'reconciliations', ReconciliationViewSet, basename='accounting-reconciliations')
router.register(r'expenses', ExpenseViewSet, basename='accounting-expenses')
router.register(r'reports', ReportsViewSet, basename='accounting-reports')

urlpatterns = [
    path('', include(router.urls)),
]
