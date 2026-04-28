from django.apps import AppConfig


class ConfigurationConfig(AppConfig):
    """
    Configuration App Configuration
    
    This app manages business configuration including:
    - Physical warehouses/business locations with GSTIN
    - API configuration for bearer token authentication
    
    Default primary key field type: BigAutoField
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'configuration'
    verbose_name = 'Configuration'
    verbose_name_plural = 'Configuration'