"""
POS App Views
=============

This module provides API views for POS operations.

Views:
------
ShiftViewSet - CRUD operations for Shift model
CashTransactionViewSet - CRUD operations for CashTransaction model
"""

from rest_framework import viewsets, status, decorators
from rest_framework.response import Response
from django.db.models import Sum
from decimal import Decimal
from .models import Shift, CashTransaction
from .serializers import ShiftSerializer, CashTransactionSerializer


class ShiftViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Shift CRUD operations.
    
    Provides full CRUD capability for shift management.
    Additional actions for open/close shift operations.
    
    Actions:
        list - GET /shifts/ - List all shifts
        create - POST /shifts/ - Open new shift
        retrieve - GET /shifts/{id}/ - Get shift details
        update - PUT /shifts/{id}/ - Update shift
        partial_update - PATCH /shifts/{id}/ - Partial update
        destroy - DELETE /shifts/{id}/ - Delete shift
        
    Custom Actions:
        POST /shifts/{id}/close/ - Close shift
        
    Query Parameters:
        ?status=Open - Filter by status
        ?warehouse=1 - Filter by warehouse
        ?user=1 - Filter by cashier
        ?date=2025-04-28 - Filter by date
    
    Ordering:
        ?ordering=-opening_time - Order by most recent
    """
    queryset = Shift.objects.all()
    serializer_class = ShiftSerializer
    filterset_fields = ['status', 'warehouse', 'user']
    ordering_fields = ['shift_number', 'opening_time', 'closing_time']
    ordering = ['-opening_time']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return Shift.objects.none()
        queryset = Shift.objects.filter(warehouse__organization=self.request.auth)
        
        date = self.request.query_params.get('date')
        if date:
            from django.utils import timezone
            from datetime import datetime, timedelta
            
            try:
                filter_date = datetime.strptime(date, '%Y-%m-%d').date()
                start = timezone.make_aware(datetime.combine(filter_date, datetime.min.time()))
                end = timezone.make_aware(datetime.combine(filter_date, datetime.max.time()))
                queryset = queryset.filter(opening_time__gte=start, opening_time__lte=end)
            except ValueError:
                pass
        
        return queryset

    def perform_create(self, serializer):
        """Ensure user is set from request."""
        serializer.save(user=self.request.user)

    @decorators.action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """
        Close a shift.
        
        Action: POST /shifts/{id}/close/
        
        Request Body:
            {
                "closing_cash": 15000.00
            }
        
        Workflow:
        1. Validate shift is open
        2. Set closing_cash
        3. Calculate expected cash from transactions
        4. Calculate variance
        5. Set status to Closed
        
        Returns:
            Shift details with variance calculation
        
        Errors:
            400: Shift already closed or invalid data
        """
        shift = self.get_object()
        
        closing_cash = request.data.get('closing_cash')
        if closing_cash is None:
            return Response(
                {'detail': 'closing_cash is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            closing_cash = Decimal(str(closing_cash))
        except (ValueError, TypeError):
            return Response(
                {'detail': 'Invalid closing_cash value'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            result = shift.close(closing_cash)
        except Exception as e:
            return Response(
                {'detail': str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = self.get_serializer(shift)
        return Response({
            'message': 'Shift closed successfully',
            'shift': serializer.data,
            'variance_details': result
        })

    @decorators.action(detail=True, methods=['get'])
    def summary(self, request, pk=None):
        """
        Get shift summary with sales and cash details.
        
        Action: GET /shifts/{id}/summary/
        
        Returns:
            - Shift basic info
            - Sales total
            - Cash transactions summary
            - Variance details
        """
        shift = self.get_object()
        
        return Response({
            'shift_number': shift.shift_number,
            'user': shift.user.username,
            'warehouse': shift.warehouse.name,
            'status': shift.status,
            'opening_time': shift.opening_time,
            'closing_time': shift.closing_time,
            'opening_cash': shift.opening_cash,
            'closing_cash': shift.closing_cash,
            'expected_cash': shift.expected_cash,
            'variance': shift.variance,
            'sales_total': shift.sales_total,
            'cash_transactions': shift.transaction_summary
        })


class CashTransactionViewSet(viewsets.ModelViewSet):
    """
    ViewSet for CashTransaction CRUD operations.
    
    Provides CRUD for cash transactions during shifts.
    
    Actions:
        list - GET /transactions/ - List all transactions
        create - POST /transactions/ - Create transaction
        retrieve - GET /transactions/{id}/ - Get transaction
        update - PUT /transactions/{id}/ - Update transaction
        destroy - DELETE /transactions/{id}/ - Delete transaction
    
    Note:
        All transactions are accessed through shift context.
        Transactions linked to closed shifts cannot be modified.
    
    Query Parameters:
        ?shift=1 - Filter by shift
        ?transaction_type=CashIn - Filter by type
    """
    queryset = CashTransaction.objects.all()
    serializer_class = CashTransactionSerializer
    filterset_fields = ['shift', 'transaction_type']
    ordering_fields = ['created_at', 'amount']
    ordering = ['-created_at']

    def get_queryset(self):
        if not hasattr(self.request, 'auth') or self.request.auth is None:
            return CashTransaction.objects.none()
        queryset = CashTransaction.objects.filter(
            shift__warehouse__organization=self.request.auth
        )
        shift_id = self.kwargs.get('shift_pk')
        if shift_id:
            queryset = queryset.filter(shift_id=shift_id)
        return queryset

    def perform_create(self, serializer):
        """
        Set shift and user from context.
        
        For nested routes, shift comes from URL.
        For direct access, shift must be provided in data.
        """
        shift_id = self.kwargs.get('shift_pk')
        if shift_id:
            serializer.save(shift_id=shift_id, created_by=self.request.user)
        else:
            serializer.save(created_by=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """
        Prevent deletion of transactions from closed shifts.
        
        Returns:
            400: If shift is closed
            204: If deleted successfully
        """
        transaction = self.get_object()
        if transaction.shift.status == 'Closed':
            return Response(
                {'detail': 'Cannot delete transaction from closed shift'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)