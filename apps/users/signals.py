from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.users.models import User
from apps.core.models import AuditLog

@receiver(post_save, sender=User)
def audit_user_modifications(sender, instance, created, **kwargs):
    action = 'CREATE' if created else 'UPDATE'
    AuditLog.objects.create(
        user=instance if not created else None,
        branch=instance.assigned_branch,
        action_type=action,
        module='Users',
        object_repr=f"{instance.username} ({instance.role})",
        details={'phone': instance.phone_number, 'is_active': instance.is_active}
    )