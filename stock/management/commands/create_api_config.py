import secrets
from django.core.management.base import BaseCommand
from stock.models import ApiConfiguration


class Command(BaseCommand):
    help = 'Create or regenerate API configuration token'

    def add_arguments(self, parser):
        parser.add_argument(
            '--regenerate',
            action='store_true',
            help='Regenerate a new token (invalidates old one)',
        )

    def handle(self, *args, **options):
        regenerate = options['regenerate']

        try:
            config = ApiConfiguration.objects.get(pk=1)
            if regenerate:
                token = f"sk_live_{secrets.token_urlsafe(32)}"
                config.api_bearer_token = token
                config.save()
                self.stdout.write(self.style.SUCCESS(f'API token regenerated: {token}'))
            else:
                self.stdout.write(self.style.WARNING(f'API config already exists. Token: {config.api_bearer_token}'))
                self.stdout.write(self.style.WARNING('Use --regenerate to create a new token.'))
        except ApiConfiguration.DoesNotExist:
            token = f"sk_live_{secrets.token_urlsafe(32)}"
            config = ApiConfiguration.objects.create(api_bearer_token=token)
            self.stdout.write(self.style.SUCCESS(f'API configuration created. Token: {token}'))
            self.stdout.write(self.style.SUCCESS('Use this token in Authorization header: Bearer <token>'))