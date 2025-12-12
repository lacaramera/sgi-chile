from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone



from decimal import Decimal

class User(AbstractUser):
    first_name = models.CharField("nombre", max_length=150, blank=False)
    last_name  = models.CharField("apellido", max_length=150, blank=False)

    rut = models.CharField("RUT", max_length=12, unique=True)
    # Nombres internos (valores que se guardan en la BD)
    ROLE_ADMIN = "admin"
    ROLE_DIRECTIVA = "directiva"
    ROLE_RESP_SECTOR = "resp_sector"
    ROLE_RESP_ZONA = "resp_zona"
    ROLE_RESP_GRUPO = "resp_grupo"
    ROLE_MIEMBRO = "miembro"

    ROLE_CHOICES = [
        (ROLE_ADMIN, "Administrador"),
        (ROLE_DIRECTIVA, "Directiva"),
        (ROLE_RESP_SECTOR, "Responsable sector / región"),
        (ROLE_RESP_ZONA, "Responsable zona"),
        (ROLE_RESP_GRUPO, "Responsable grupo"),
        (ROLE_MIEMBRO, "Miembro"),
    ]

    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default=ROLE_MIEMBRO,
    )

    # ---- Helpers de permisos ----

    def is_miembro(self):
        return self.role == self.ROLE_MIEMBRO

    def is_responsable_grupo(self):
        return self.role == self.ROLE_RESP_GRUPO

    def is_responsable_zona(self):
        return self.role == self.ROLE_RESP_ZONA

    def is_responsable_sector(self):
        return self.role == self.ROLE_RESP_SECTOR

    def is_directiva(self):
        return self.role == self.ROLE_DIRECTIVA

    def is_admin_sistema(self):
        # superuser de Django o admin/directiva
        return self.is_superuser or self.role in {
            self.ROLE_ADMIN,
            self.ROLE_DIRECTIVA,
        }

    def can_view_active_members(self):
        """
        Permiso para ver 'Miembros activos' en Kofu.
        Desde responsable de grupo hacia arriba.
        """
        return self.role in {
            self.ROLE_RESP_GRUPO,
            self.ROLE_RESP_ZONA,
            self.ROLE_RESP_SECTOR,
            self.ROLE_DIRECTIVA,
            self.ROLE_ADMIN,
        } or self.is_superuser

class Household(models.Model):
    name = models.CharField(max_length=120, blank=True)  # ej: "Familia Pérez"
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or f"Hogar #{self.id}"


class HouseholdMember(models.Model):
    REL_HEAD = "head"
    REL_SPOUSE = "spouse"
    REL_CHILD = "child"
    REL_SIBLING = "sibling"
    REL_OTHER = "other"

    REL_CHOICES = [
        (REL_HEAD, "Jefe/a de hogar"),
        (REL_SPOUSE, "Esposo/a"),
        (REL_CHILD, "Hijo/a"),
        (REL_SIBLING, "Hermano/a"),
        (REL_OTHER, "Otro"),
    ]

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="memberships")
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="household_membership")
    relationship = models.CharField(max_length=20, choices=REL_CHOICES, default=REL_OTHER)
    is_primary = models.BooleanField(default=False)  # “hogar principal” por si a futuro hay más de uno

    class Meta:
        unique_together = ("household", "user")

    def __str__(self):
        return f"{self.user.username} -> {self.household}"

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

class Contribution(models.Model):
    """
    Contribución ya confirmada.
    Esta es la que se usa para:
      - Historial de contribuciones
      - Calcular si un miembro es "activo" (>= $12.000 confirmados)
    """

    TYPE_REGULAR = "regular"
    TYPE_ESPECIAL = "especial"
    TYPE_CHOICES = [
        (TYPE_REGULAR, "Contribución regular"),
        (TYPE_ESPECIAL, "Contribución especial"),
    ]

    member = models.ForeignKey(
        "User",
        on_delete=models.CASCADE,
        related_name="contributions",
        verbose_name="Miembro",
    )
    date = models.DateField(default=timezone.now, verbose_name="Fecha aporte")
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name="Monto",
    )
    contribution_type = models.CharField(
        max_length=20,
        choices=TYPE_CHOICES,
        default=TYPE_REGULAR,
        verbose_name="Tipo de contribución",
    )
    note = models.TextField(blank=True, default="", verbose_name="Nota interna")
    is_confirmed = models.BooleanField(default=True, verbose_name="Confirmado")

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contributions_created",
        verbose_name="Registrado por",
    )

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Contribución"
        verbose_name_plural = "Contribuciones"

    def __str__(self):
        return f"{self.member.username} - {self.date} - {self.amount}"


from django.db import models
from django.utils import timezone
from decimal import Decimal

class ContributionReport(models.Model):
    """
    Informe de contribución enviado por el usuario (depósito ya realizado).
    Queda 'pending' hasta que un coordinador/admin lo revise.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente de revisión"),
        (STATUS_APPROVED, "Aprobado"),
        (STATUS_REJECTED, "Rechazado"),
    ]

    user = models.ForeignKey(
        "User",
        on_delete=models.CASCADE,
        related_name="contribution_reports",
        verbose_name="Usuario que informa",
    )

    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Monto informado")
    deposit_date = models.DateField(verbose_name="Fecha de depósito")

    receipt = models.FileField(
        upload_to="contribuciones/comprobantes/",
        verbose_name="Comprobante de depósito",
        blank=True,
        null=True,
    )

    # Aquí guardaremos la distribución real (IDs y montos)
    # Ej:
    # {"total": 12000, "splits":[{"user_id": 10, "amount": 5000}, ...]}
    distribution = models.JSONField(blank=True, null=True, verbose_name="Distribución familiar")

    note = models.TextField(blank=True, default="", verbose_name="Comentario adicional")

    # (opcional) si quieres seguir guardando texto “bonito” para lectura rápida
    family_distribution = models.TextField(blank=True, default="", verbose_name="Distribución (texto)")

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="Estado",
    )

    reviewed_by = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contribution_reports_reviewed",
        verbose_name="Revisado por",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Informe de contribución"
        verbose_name_plural = "Informes de contribución"

    def __str__(self):
        return f"Informe {self.id} - {self.user.username} - {self.deposit_amount}"

    def approve(self, reviewer):
        """
        Aprueba el informe y crea contribuciones confirmadas.
        - Si hay distribution.splits => crea 1 Contribution por cada split (monto > 0).
        - Si NO hay splits => crea 1 Contribution para el usuario (total).
        """
        if self.status == self.STATUS_APPROVED:
            return None

        created = []

        payload = self.distribution or {}
        splits = payload.get("splits") or []

        # === Distribución familiar real ===
        if splits:
            for s in splits:
                uid = s.get("user_id")
                amt = s.get("amount")

                if not uid or not amt:
                    continue

                try:
                    u = User.objects.get(id=int(uid))
                    amount = Decimal(str(amt))
                except Exception:
                    continue

                if amount <= 0:
                    continue

                created.append(
                    Contribution.objects.create(
                        member=u,
                        date=self.deposit_date,
                        amount=amount,
                        contribution_type=Contribution.TYPE_REGULAR,
                        note=(
                            f"Aporte distribuido desde informe #{self.id} "
                            f"(reportado por @{self.user.username})."
                        ),
                        is_confirmed=True,
                        created_by=reviewer,
                    )
                )

        # === Sin distribución: aporte directo ===
        else:
            created.append(
                Contribution.objects.create(
                    member=self.user,
                    date=self.deposit_date,
                    amount=self.deposit_amount,
                    contribution_type=Contribution.TYPE_REGULAR,
                    note=f"Aporte informado vía web (informe #{self.id}).",
                    is_confirmed=True,
                    created_by=reviewer,
                )
            )

        # === Marcar informe como aprobado ===
        self.status = self.STATUS_APPROVED
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        # 🔑 Compatibilidad: devolvemos una sola contribución
        return created[0] if created else None


    def reject(self, reviewer, reason=""):
        self.status = self.STATUS_REJECTED
        if reason:
            extra = f"\n[RECHAZADO]: {reason}"
            self.note = (self.note or "") + extra
        self.reviewed_by = reviewer
        self.reviewed_at = timezone.now()
        self.save(update_fields=["status", "note", "reviewed_by", "reviewed_at"])


class ContributionSplit(models.Model):
    report = models.ForeignKey("ContributionReport", on_delete=models.CASCADE, related_name="splits")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="contribution_splits")
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"Split #{self.id} report={self.report_id} user={self.user_id} amount={self.amount}"

class Notification(models.Model):
    """
    Notificación simple para el usuario (ej: contribución aprobada).
    """
    user = models.ForeignKey(
        "User",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} - {self.title}"

class Household(models.Model):
    name = models.CharField(max_length=120, blank=True)  # ej: "Familia Wilson" (opcional)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name or f"Hogar #{self.id}"


class HouseholdMember(models.Model):
    REL_HEAD = "head"
    REL_SPOUSE = "spouse"
    REL_CHILD = "child"
    REL_SIBLING = "sibling"
    REL_OTHER = "other"

    REL_CHOICES = [
        (REL_HEAD, "Jefe/a de hogar"),
        (REL_SPOUSE, "Esposo/a"),
        (REL_CHILD, "Hijo/a"),
        (REL_SIBLING, "Hermano/a"),
        (REL_OTHER, "Otro"),
    ]

    household = models.ForeignKey("Household", on_delete=models.CASCADE, related_name="memberships")
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="household_membership")
    relationship = models.CharField(max_length=20, choices=REL_CHOICES, default=REL_OTHER)
    is_primary = models.BooleanField(default=False)

    class Meta:
        unique_together = ("household", "user")

    def __str__(self):
        return f"{self.user.username} -> {self.household}"

