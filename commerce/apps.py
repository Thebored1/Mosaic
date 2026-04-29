from django.apps import AppConfig


class CommerceConfig(AppConfig):
    """
    Application config for the commerce layer.

    This app hosts the buyer-facing catalog, cart, address book, and checkout
    models that sit on top of the existing tenant and inventory structure.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'commerce'
