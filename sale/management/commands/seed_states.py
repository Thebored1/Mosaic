from django.core.management.base import BaseCommand
from sale.models import State


class Command(BaseCommand):
    help = 'Seed Indian states for GST'

    def handle(self, *args, **options):
        states = [
            ('01', 'Jammu and Kashmir'),
            ('02', 'Himachal Pradesh'),
            ('03', 'Punjab'),
            ('04', 'Chandigarh'),
            ('05', 'Uttarakhand'),
            ('06', 'Haryana'),
            ('07', 'Delhi'),
            ('08', 'Rajasthan'),
            ('09', 'Uttar Pradesh'),
            ('10', 'Bihar'),
            ('11', 'Sikkim'),
            ('12', 'Arunachal Pradesh'),
            ('13', 'Nagaland'),
            ('14', 'Manipur'),
            ('15', 'Mizoram'),
            ('16', 'Tripura'),
            ('17', 'Meghalaya'),
            ('18', 'Assam'),
            ('19', 'West Bengal'),
            ('20', 'Jharkhand'),
            ('21', 'Odisha'),
            ('22', 'Chattisgarh'),
            ('23', 'Madhya Pradesh'),
            ('24', 'Gujarat'),
            ('25', 'Daman and Diu'),
            ('26', 'Dadra and Nagar Haveli'),
            ('27', 'Maharashtra'),
            ('28', 'Andhra Pradesh (Old)'),
            ('29', 'Karnataka'),
            ('30', 'Goa'),
            ('31', 'Lakshadweep'),
            ('32', 'Kerala'),
            ('33', 'Tamil Nadu'),
            ('34', 'Puducherry'),
            ('35', 'Andaman and Nicobar Islands'),
            ('36', 'Telangana'),
            ('37', 'Andhra Pradesh (New)'),
            ('38', 'Ladakh'),
        ]

        created = 0
        for code, name in states:
            if not State.objects.filter(state_code=code).exists():
                State.objects.create(name=name, state_code=code, is_active=True)
                created += 1

        self.stdout.write(self.style.SUCCESS(f'Created {created} states'))