"""
Accounting API views.

This module exposes the accounting layer as a tenant-scoped API surface:

1. chart-of-accounts CRUD
2. immutable journal browsing
3. fiscal period close controls
4. reconciliation records
5. ledger and financial statement reports
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from configuration.authentication import ScopedRolePermission
from sale.models import Invoice, Receipt, PurchaseInvoice, PaymentOut, CreditNote, DebitNote

from .models import Expense, FiscalPeriod, JournalEntry, JournalLine, LedgerAccount, PostingBatch, Reconciliation
from .serializers import ExpenseSerializer, FiscalPeriodSerializer, JournalEntrySerializer, LedgerAccountSerializer, PostingBatchSerializer, ReconciliationSerializer


def org_filter(qs, request):
    """Limit a queryset to the authenticated organization."""
    if not hasattr(request, 'auth') or request.auth is None:
        return qs.none()
    if hasattr(request.auth, 'pk'):
        return qs.filter(organization=request.auth)
    return qs.none()


class LedgerAccountViewSet(viewsets.ModelViewSet):
    """CRUD API for chart-of-accounts rows."""
    queryset = LedgerAccount.objects.all()
    serializer_class = LedgerAccountSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'accounting'

    def get_queryset(self):
        """Return accounts visible to the current organization."""
        return org_filter(self.queryset, self.request)


class JournalEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only API for posted journal entries."""
    queryset = JournalEntry.objects.select_related('batch', 'organization').prefetch_related('lines')
    serializer_class = JournalEntrySerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'accounting'

    def get_queryset(self):
        """Return posted journals visible to the current organization."""
        return org_filter(self.queryset, self.request)


class PostingBatchViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only API for source-document posting batches."""
    queryset = PostingBatch.objects.select_related('journal_entry').all()
    serializer_class = PostingBatchSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'accounting'

    def get_queryset(self):
        """Return posting batches visible to the current organization."""
        return org_filter(self.queryset, self.request)


class FiscalPeriodViewSet(viewsets.ModelViewSet):
    """CRUD API for fiscal periods and close actions."""
    queryset = FiscalPeriod.objects.all()
    serializer_class = FiscalPeriodSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'accounting'

    def get_queryset(self):
        """Return fiscal periods visible to the current organization."""
        return org_filter(self.queryset, self.request)

    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """Close the selected fiscal period."""
        period = self.get_object()
        period.close(request.user)
        return Response(FiscalPeriodSerializer(period).data)


class ReconciliationViewSet(viewsets.ModelViewSet):
    """CRUD API for account reconciliation records."""
    queryset = Reconciliation.objects.select_related('account').all()
    serializer_class = ReconciliationSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'accounting'

    def get_queryset(self):
        """Return reconciliations visible to the current organization."""
        return org_filter(self.queryset, self.request)


class ExpenseViewSet(viewsets.ModelViewSet):
    """CRUD API for operational expenses."""
    queryset = Expense.objects.select_related('business_location', 'party', 'journal_entry').all()
    serializer_class = ExpenseSerializer
    permission_classes = [ScopedRolePermission]
    permission_scope = 'accounting'

    def get_queryset(self):
        """Return expenses visible to the current organization."""
        return org_filter(self.queryset, self.request)

    def perform_create(self, serializer):
        """Create the expense under the authenticated organization."""
        serializer.save(organization=self.request.auth, created_by=self.request.user)

    def perform_update(self, serializer):
        """Update the expense under the authenticated organization."""
        serializer.save(organization=self.request.auth)

    def update(self, request, *args, **kwargs):
        """Prevent editing a posted expense; use cancel and recreate instead."""
        raise ValidationError({'expense': 'Expense records are immutable once posted.'})

    def partial_update(self, request, *args, **kwargs):
        """Prevent editing a posted expense; use cancel and recreate instead."""
        raise ValidationError({'expense': 'Expense records are immutable once posted.'})

    def destroy(self, request, *args, **kwargs):
        """Cancel an expense instead of deleting the posted record."""
        expense = self.get_object()
        expense.cancel(user=request.user)
        return Response({'status': 'cancelled'})


class ReportsViewSet(viewsets.ViewSet):
    """Reporting endpoints for trial balance and financial statements."""
    permission_classes = [ScopedRolePermission]
    permission_scope = 'accounting'

    def _org(self, request):
        """Resolve the authenticated organization for reporting."""
        if not hasattr(request, 'auth') or not hasattr(request.auth, 'pk'):
            raise ValidationError('Organization token required.')
        return request.auth

    @action(detail=False, methods=['get'])
    def trial_balance(self, request):
        """Return account-level debit and credit totals."""
        organization = self._org(request)
        accounts = LedgerAccount.objects.filter(organization=organization, is_active=True).order_by('code')
        rows = []
        debit_total = Decimal('0')
        credit_total = Decimal('0')
        for account in accounts:
            debit = account.journal_lines.aggregate(total=Sum('debit'))['total'] or Decimal('0')
            credit = account.journal_lines.aggregate(total=Sum('credit'))['total'] or Decimal('0')
            rows.append({'account': account.code, 'name': account.name, 'debit': str(debit), 'credit': str(credit)})
            debit_total += debit
            credit_total += credit
        return Response({'rows': rows, 'debit_total': str(debit_total), 'credit_total': str(credit_total)})

    @action(detail=False, methods=['get'])
    def general_ledger(self, request):
        """Return posted journal entries for one ledger account."""
        organization = self._org(request)
        account_id = request.query_params.get('account')
        if not account_id:
            raise ValidationError({'account': 'account is required'})
        lines = JournalEntry.objects.filter(organization=organization, lines__account_id=account_id).distinct().order_by('-entry_date', '-id')
        return Response(JournalEntrySerializer(lines, many=True).data)

    @action(detail=False, methods=['get'])
    def balance_sheet(self, request):
        """Return asset, liability, and equity balances."""
        organization = self._org(request)
        categories = ['Asset', 'Liability', 'Equity']
        data = {}
        for category in categories:
            rows = LedgerAccount.objects.filter(organization=organization, category=category).annotate(
                debit_total=Sum('journal_lines__debit'),
                credit_total=Sum('journal_lines__credit'),
            )
            data[category] = [
                {
                    'code': acc.code,
                    'name': acc.name,
                    'balance': str((acc.debit_total or Decimal('0')) - (acc.credit_total or Decimal('0'))),
                }
                for acc in rows
            ]
        return Response(data)

    @action(detail=False, methods=['get'])
    def profit_loss(self, request):
        """Return revenue and expense balances for the period."""
        organization = self._org(request)
        rows = LedgerAccount.objects.filter(organization=organization, category__in=['Revenue', 'Expense']).annotate(
            debit_total=Sum('journal_lines__debit'),
            credit_total=Sum('journal_lines__credit'),
        )
        data = []
        for acc in rows:
            data.append({
                'code': acc.code,
                'name': acc.name,
                'balance': str((acc.credit_total or Decimal('0')) - (acc.debit_total or Decimal('0'))),
            })
        return Response({'rows': data})

    @action(detail=False, methods=['get'])
    def aging(self, request):
        """Return simple receivable and payable aging data."""
        organization = self._org(request)
        receivables = []
        for invoice in Invoice.objects.filter(business_location__organization=organization, status='Finalized').select_related('party'):
            received = Receipt.objects.filter(party=invoice.party, business_location__organization=organization).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            outstanding = invoice.grand_total - received
            receivables.append({'reference': invoice.invoice_number, 'party': invoice.party.name if invoice.party else '', 'outstanding': str(outstanding)})
        payables = []
        for invoice in PurchaseInvoice.objects.filter(business_location__organization=organization, status='Finalized').select_related('supplier'):
            paid = PaymentOut.objects.filter(supplier=invoice.supplier, business_location__organization=organization).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            outstanding = invoice.grand_total - paid
            payables.append({'reference': invoice.invoice_number, 'party': invoice.supplier.name if invoice.supplier else '', 'outstanding': str(outstanding)})
        return Response({'receivables': receivables, 'payables': payables})

    @action(detail=False, methods=['get'])
    def party_statement(self, request):
        """Return a partywise P&L style statement from ledger lines."""
        organization = self._org(request)
        party_id = request.query_params.get('party')
        if not party_id:
            raise ValidationError({'party': 'party is required'})

        lines = JournalLine.objects.filter(
            entry__organization=organization,
            party_id=party_id,
        ).select_related('account', 'entry', 'party')

        summary = {
            'revenue': Decimal('0'),
            'returns': Decimal('0'),
            'cogs': Decimal('0'),
            'tax': Decimal('0'),
            'receipts': Decimal('0'),
            'payables': Decimal('0'),
            'expenses': Decimal('0'),
        }
        entries = []
        for line in lines.order_by('entry__entry_date', 'id'):
            if line.account.category == 'Revenue':
                if line.account.code == '4100':
                    summary['returns'] += line.debit - line.credit
                else:
                    summary['revenue'] += line.credit - line.debit
            elif line.account.code == '5000':
                summary['cogs'] += line.debit - line.credit
            elif line.account.code in {'2100', '2110'}:
                summary['tax'] += line.credit - line.debit
            elif line.account.code == '1100':
                summary['receipts'] += line.credit - line.debit
            elif line.account.code == '2000':
                summary['payables'] += line.debit - line.credit
            elif line.account.category == 'Expense':
                summary['expenses'] += line.debit - line.credit

            entries.append({
                'date': line.entry.entry_date,
                'reference': line.entry.reference,
                'account': line.account.code,
                'account_name': line.account.name,
                'debit': str(line.debit),
                'credit': str(line.credit),
            })

        gross_profit = summary['revenue'] - summary['returns'] - summary['cogs']
        return Response({
            'party_id': int(party_id),
            'summary': {key: str(value) for key, value in summary.items()},
            'gross_profit': str(gross_profit),
            'entries': entries,
        })

    @action(detail=False, methods=['get'])
    def invoice_profit(self, request):
        """Return invoice-level profit built from stored cost snapshots."""
        organization = self._org(request)
        invoice_id = request.query_params.get('invoice')
        invoices = Invoice.objects.filter(business_location__organization=organization).order_by('-invoice_date')
        if invoice_id:
            invoices = invoices.filter(pk=invoice_id)
        results = []
        for invoice in invoices:
            results.append({
                'invoice_id': invoice.id,
                'invoice_number': invoice.invoice_number,
                'party': invoice.party.name if invoice.party else '',
                'gross_profit': str(invoice.gross_profit_amount),
                'tcs_amount': str(invoice.tcs_amount),
                'net_sales': str(invoice.taxable_amount),
            })
        return Response({'results': results})

    @action(detail=False, methods=['get'])
    def expense_report(self, request):
        """Return expenses grouped by category and date range."""
        organization = self._org(request)
        qs = Expense.objects.filter(organization=organization).order_by('-expense_date')
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        if start_date:
            qs = qs.filter(expense_date__gte=start_date)
        if end_date:
            qs = qs.filter(expense_date__lte=end_date)
        category = request.query_params.get('category')
        if category:
            qs = qs.filter(category__iexact=category)
        total = qs.aggregate(total=Sum('amount'))['total'] or Decimal('0')
        return Response({
            'total': str(total),
            'expenses': ExpenseSerializer(qs, many=True).data,
        })
