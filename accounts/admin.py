from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Event
from django.utils.html import format_html
from .utils import send_activation_email  # si ya lo tienes

def send_activation(modeladmin, request, queryset):
    for user in queryset:
        if user.email:
            send_activation_email(user, request)
send_activation.short_description = "Enviar correo de activación a los usuarios seleccionados"

class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ('Extra', {'fields': ('role',)}),
    )
    list_display = ('username', 'email', 'first_name', 'last_name', 'role', 'is_staff', 'is_active')
    actions = [send_activation]

admin.site.register(User, CustomUserAdmin)

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'date', 'time', 'location', 'price', 'is_public')
    list_filter = ('date','is_public')
    search_fields = ('title','location','description')
