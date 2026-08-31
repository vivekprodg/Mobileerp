from django.contrib.auth.mixins import UserPassesTestMixin


class OwnerOnlyMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (user.is_superuser or getattr(user, 'role', '') == 'OWNER')


class ManagerOrOwnerMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER'])


class AccountantOrManagerMixin(UserPassesTestMixin):
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER', 'ACCOUNTANT'])


class StaffManagementAccessMixin(UserPassesTestMixin):
    """
    Grants access to Super Admins, Store Owners, and Branch Managers.
    Blocks cashiers, sales staff, and technicians from accessing staff directory.
    """
    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or getattr(user, 'role', '') in ['OWNER', 'MANAGER']
        )