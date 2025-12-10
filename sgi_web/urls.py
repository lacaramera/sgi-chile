from django.contrib import admin
from django.urls import path, include
from accounts import views as accounts_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', accounts_views.home, name='home'),
    path('accounts/', include('django.contrib.auth.urls')),  # login/logout/password
    path('accounts/', include('accounts.urls')),             # dashboard, activate
    path('kofu/', accounts_views.kofu_view, name='kofu'),
]
