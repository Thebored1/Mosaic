from django.apps import AppConfig


class PosConfig(AppConfig):
    """
    POS App Configuration
    
    This app manages point-of-sale operations including:
    - Cashier shifts (open/close)
    - Cash transactions (cash in/out during shift)
    
    Default primary key field type: BigAutoField
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pos'
    verbose_name = 'Point of Sale'
    verbose_name_plural = 'Point of Sale'