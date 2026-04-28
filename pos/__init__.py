"""
POS App - Point of Sale Operations
================================

This module provides POS-specific functionality for retail operations.

App Purpose:
-----------
Counter/Cashier shift management for retail operations.
Tracks shift open/close, cash transactions, and variance tracking.

Models:
-------
Shift - Cashier shift with opening/closing cash
CashTransaction - Cash in/out during shift
"""

default_app_config = 'pos.apps.PosConfig'