from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from .models import User, Event, Contribution, ContributionReport, Notification, Sector, Zona, Grupo, FortunaIssue, FortunaPurchase, Profile
from django.utils.html import format_html
from .utils import send_activation_email  # si ya lo tienes


def send_activation(modeladmin, request, queryset):
    for user in queryset:
        if user.email:
            send_activation_email(user, request)
send_activation.short_description = "Enviar correo de activación a los usuarios seleccionados"

class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    extra = 0

class CustomUserAdmin(DjangoUserAdmin):
    inlines = (ProfileInline,)

    # Mostrar rut y role en edición
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Extra", {"fields": ("rut", "role")}),
    )

    # Mostrar rut en la lista
    list_display = (
        "username", "rut", "email", "first_name", "last_name",
        "role", "is_staff", "is_active"
    )

    # Que en "Agregar usuario" aparezca rut y role también
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Extra", {"fields": ("rut", "role")}),
    )

    actions = [send_activation]


admin.site.register(User, CustomUserAdmin)

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'date', 'time', 'location', 'price', 'is_public')
    list_filter = ('date','is_public')
    search_fields = ('title','location','description')

@admin.register(Contribution)
class ContributionAdmin(admin.ModelAdmin):
    list_display = ("member", "date", "amount", "contribution_type", "is_confirmed")
    list_filter = ("contribution_type", "is_confirmed", "date")
    search_fields = ("member__username", "member__first_name", "member__last_name")


@admin.register(ContributionReport)
class ContributionReportAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "deposit_date", "deposit_amount", "status", "created_at")
    list_filter = ("status", "deposit_date", "created_at")
    search_fields = ("user__username", "user__first_name", "user__last_name")

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "title", "is_read", "created_at")
    list_filter = ("is_read", "created_at")
    search_fields = ("user__username", "title", "message")


@admin.register(Sector)
class SectorAdmin(admin.ModelAdmin):
    search_fields = ("name",)

@admin.register(Zona)
class ZonaAdmin(admin.ModelAdmin):
    list_display = ("name", "sector")
    list_filter = ("sector",)
    search_fields = ("name", "sector__name")

@admin.register(Grupo)
class GrupoAdmin(admin.ModelAdmin):
    list_display = ("name", "zona")
    list_filter = ("zona__sector", "zona")
    search_fields = ("name", "zona__name", "zona__sector__name")

@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "is_buyer")
    list_editable = ("is_buyer",)
    search_fields = ("user__username", "user__email", "user__rut")


@admin.register(FortunaIssue)
class FortunaIssueAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "is_active", "is_public_archive", "created_at")
    list_filter = ("is_active", "is_public_archive")
    search_fields = ("code", "title")
    fields = ("code", "title", "cover_image", "material_pdf", "material_url", "is_active", "is_public_archive")



@admin.register(FortunaPurchase)
class FortunaPurchaseAdmin(admin.ModelAdmin):
    list_display = ("issue", "user", "status", "created_at")
    list_filter = ("status", "issue")
    search_fields = ("user__username", "user__email", "issue__code", "issue__title")


