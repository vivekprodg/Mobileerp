from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.hashers import make_password, check_password
from django.utils.translation import gettext_lazy as _
from apps.branches.models import Branch


class User(AbstractUser):
    """
    Custom Shop User model enforcing Role-Based Scoping, Branch Isolation,
    and Salted PBKDF2-Hashed POS Manager Override PINs (SEC-02 Compliance).
    Features high-performance compound indexing on role, branch, and status.
    """
    ROLE_CHOICES = [
        ('OWNER', 'Shop Owner / Central Admin (मालिक)'),
        ('MANAGER', 'Branch Manager (शाखा प्रबन्धक)'),
        ('STAFF', 'Cashier / Sales Staff (विक्रेता)'),
        ('ACCOUNTANT', 'Accountant (लेखापाल)'),
    ]

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='STAFF',
        db_index=True,
        verbose_name=_("System Role")
    )
    phone_number = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        verbose_name=_("Mobile Number (Nepal 98XXXXXXXX)"),
        help_text=_("Primary 10-digit mobile number for terminal login and notifications.")
    )
    assigned_branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='staff_members',
        db_index=True,
        verbose_name=_("Assigned Store Branch"),
        help_text=_("Standard Cashiers and Sales Staff are strictly locked to this store branch.")
    )
    pin_code = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        verbose_name=_("Manager Override PIN (Salted Hash)"),
        help_text=_("Salted cryptographic hash of the 4-6 digit numeric PIN used for POS authorizations.")
    )
    failed_login_attempts = models.PositiveIntegerField(default=0, verbose_name=_("Failed Login Counter"))
    is_locked = models.BooleanField(default=False, verbose_name=_("Account Locked"))

    REQUIRED_FIELDS = ['phone_number']

    class Meta:
        db_table = 'auth_users'
        ordering = ['username']
        verbose_name = _('System User')
        verbose_name_plural = _('System Users')
        indexes = [
            models.Index(fields=['role', 'assigned_branch']),
            models.Index(fields=['is_active', 'assigned_branch']),
            models.Index(fields=['role', 'is_active']),
            models.Index(fields=['phone_number']),
        ]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_owner(self) -> bool:
        return self.role == 'OWNER' or self.is_superuser

    @property
    def is_manager(self) -> bool:
        return self.role in ['OWNER', 'MANAGER'] or self.is_superuser

    @property
    def is_accountant(self) -> bool:
        return self.role in ['OWNER', 'ACCOUNTANT'] or self.is_superuser

    def set_pin(self, raw_pin: str) -> None:
        """Cryptographically hashes and assigns the quick override PIN using PBKDF2."""
        if raw_pin and str(raw_pin).strip():
            self.pin_code = make_password(str(raw_pin).strip())
        else:
            self.pin_code = None

    def check_pin(self, raw_pin: str) -> bool:
        """
        Constant-time verification of raw PIN against the stored hash.
        Includes automatic self-healing for legacy unhashed PIN strings.
        """
        if not self.pin_code or not raw_pin:
            return False

        clean_raw_pin = str(raw_pin).strip()
        stored_hash = str(self.pin_code).strip()

        # 1. Standard hashed verification (pbkdf2 / argon2 / bcrypt)
        if stored_hash.startswith(('pbkdf2_', 'argon2', 'bcrypt', 'scrypt')):
            return check_password(clean_raw_pin, stored_hash)

        # 2. Self-healing on-the-fly upgrade for legacy plaintext strings
        if clean_raw_pin == stored_hash:
            self.set_pin(clean_raw_pin)
            self.save(update_fields=['pin_code'])
            return True

        return False

    def has_pin(self) -> bool:
        """Returns True if the user has an active override PIN configured."""
        return bool(self.pin_code and str(self.pin_code).strip())