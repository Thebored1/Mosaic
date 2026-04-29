"""
Configuration App - Business Configuration & Multi-Location Management
===================================================================

This module provides configuration models for:
1. Warehouse - Physical locations with GSTIN support
2. State - Indian states for GST place of supply
3. ApiToken - User-based API tokens for authentication
4. SuperAdminToken - Cross-organization access tokens
"""

default_app_config = 'configuration.apps.ConfigurationConfig'