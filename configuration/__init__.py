"""
Configuration App - Business Configuration & Multi-Location Management
===================================================================

This module provides configuration models for:
1. Warehouse - Physical locations with GSTIN support (replaces BusinessLocation from sale app)
2. ApiConfiguration - API bearer token management

These models are used across the entire system for inventory and transaction tracking.
"""

default_app_config = 'configuration.apps.ConfigurationConfig'