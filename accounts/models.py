from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone

class User(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Administrador'),
        ('coordinator', 'Coordinador'),
        ('responsible', 'Responsable'),
        ('member', 'Miembro'),
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='member')

    def is_coordinator(self):
        return self.role == 'coordinator' or self.is_superuser

    def is_responsible(self):
        return self.role == 'responsible' or self.is_superuser

class Event(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    price = models.CharField(max_length=50, blank=True)  # p.e. "$500" o "Gratuito"
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_public = models.BooleanField(default=True)
    image = models.CharField(max_length=255, blank=True)  # ruta relativa en static/img/ (opcional)

    class Meta:
        ordering = ['date', 'time']

    def __str__(self):
        return f"{self.title} — {self.date}"
