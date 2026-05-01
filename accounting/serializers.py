"""
Accounting serializers.

These serializers expose the ledger, posting batches, and reports in a shape
that mirrors the rest of the project: compact list views, nested detail views,
and stable read models for reporting screens.
"""

from rest_framework import serializers

from .models import Expense, FiscalPeriod, JournalEntry, JournalLine, LedgerAccount, PostingBatch, Reconciliation


class LedgerAccountSerializer(serializers.ModelSerializer):
    """Serialize chart-of-accounts rows for CRUD and list views."""
    class Meta:
        model = LedgerAccount
        fields = ['id', 'organization', 'code', 'name', 'category', 'normal_balance', 'parent', 'is_control', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class JournalLineSerializer(serializers.ModelSerializer):
    """Serialize the debit or credit lines inside a journal entry."""
    account_code = serializers.CharField(source='account.code', read_only=True)
    account_name = serializers.CharField(source='account.name', read_only=True)

    class Meta:
        model = JournalLine
        fields = ['id', 'line_no', 'account', 'account_code', 'account_name', 'party', 'debit', 'credit', 'memo', 'created_at']


class JournalEntrySerializer(serializers.ModelSerializer):
    """Serialize immutable journal entries with nested line items."""
    lines = JournalLineSerializer(many=True, read_only=True)

    class Meta:
        model = JournalEntry
        fields = ['id', 'batch', 'organization', 'entry_date', 'entry_kind', 'narration', 'reference', 'reversed_entry', 'reversal_reason', 'lines', 'created_at', 'created_by']


class PostingBatchSerializer(serializers.ModelSerializer):
    """Serialize posting batches and their linked journal entry."""
    journal_entry = JournalEntrySerializer(read_only=True)

    class Meta:
        model = PostingBatch
        fields = ['id', 'organization', 'source_app', 'source_model', 'source_object_id', 'source_event', 'source_reference', 'memo', 'batch_date', 'status', 'metadata', 'reversal_of', 'posted_at', 'posted_by', 'journal_entry']


class FiscalPeriodSerializer(serializers.ModelSerializer):
    """Serialize accounting periods for close and lock operations."""
    class Meta:
        model = FiscalPeriod
        fields = ['id', 'organization', 'name', 'start_date', 'end_date', 'is_closed', 'closed_at', 'closed_by', 'notes', 'created_at', 'updated_at']


class ReconciliationSerializer(serializers.ModelSerializer):
    """Serialize reconciliation records for cash and bank matching."""
    class Meta:
        model = Reconciliation
        fields = ['id', 'organization', 'account', 'statement_date', 'statement_balance', 'system_balance', 'variance', 'status', 'notes', 'created_at', 'created_by']


class ExpenseSerializer(serializers.ModelSerializer):
    """Serialize expense records with posting metadata."""

    class Meta:
        model = Expense
        fields = [
            'id', 'organization', 'business_location', 'party', 'expense_number',
            'category', 'expense_date', 'amount', 'tax_amount', 'payment_mode',
            'reference_number', 'notes', 'status', 'journal_entry',
            'created_at', 'created_by', 'updated_at'
        ]
        read_only_fields = ['id', 'expense_number', 'status', 'journal_entry', 'created_at', 'updated_at']
        extra_kwargs = {
            'organization': {'required': False},
        }
