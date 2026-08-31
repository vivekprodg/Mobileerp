from django.urls import path
from apps.users.views import (
    ShopLoginView, ShopLogoutView, UserListView,
    UserCreateView, UserUpdateView, ValidateManagerPinAPIView
)

app_name = 'users'

urlpatterns = [
    path('login/', ShopLoginView.as_view(), name='login'),
    path('logout/', ShopLogoutView.as_view(), name='logout'),
    path('', UserListView.as_view(), name='user_list'),
    path('create/', UserCreateView.as_view(), name='user_create'),
    path('<int:pk>/edit/', UserUpdateView.as_view(), name='user_edit'),
    path('api/validate-pin/', ValidateManagerPinAPIView.as_view(), name='validate_pin'),
]