from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create/update least-privilege human administrator groups."

    def handle(self, *args, **options):
        operations, _ = Group.objects.get_or_create(name="Operations Admin")
        readonly, _ = Group.objects.get_or_create(name="Read Only")
        view = Permission.objects.filter(
            content_type__app_label__in=["rooms", "meetings", "audit"],
            codename__in=["view_room", "view_meeting", "view_auditlog"],
        )
        readonly.permissions.set(view)
        operations.permissions.set(
            view
            | Permission.objects.filter(
                codename__in=["change_room", "change_meeting"],
                content_type__app_label__in=["rooms", "meetings"],
            )
        )
        self.stdout.write(
            "Roles configured. Assign is_staff separately; only superusers manage users and credentials."
        )
