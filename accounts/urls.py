from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard, name='dashboard'),
    path('activate/<uidb64>/<token>/', views.activate_account, name='activate'),
]
