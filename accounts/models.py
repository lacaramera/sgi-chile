from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone
from django.db import transaction
from decimal import Decimal
from datetime import date
from django.conf import settings
from django.contrib.auth import get_user_model



class User(AbstractUser):
    first_name = models.CharField("nombre", max_length=150, blank=False)
    last_name = models.CharField("apellido", max_length=150, blank=False)

    rut = models.CharField("RUT", max_length=12, unique=True)
    address = models.CharField("Dirección", max_length=255, blank=True, default="")
    join_date = models.DateField("Fecha de ingreso", null=True, blank=True)
    is_only_family_member = models.BooleanField("Único miembro de su familia", default=False)

    birth_date = models.DateField("Fecha de nacimiento", null=True, blank=True)

    group = models.ForeignKey(
        "Grupo",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="members",
        verbose_name="Grupo",
    )

    profile_photo = models.ImageField(
        upload_to="avatars/",
        blank=True,
        null=True,
        verbose_name="Foto de perfil",
    )

    # -------------------------
    # División (pertenencia)
    # -------------------------
    DIV_DJM = "djm"
    DIV_DJF = "djf"
    DIV_CABALLEROS = "caballeros"
    DIV_DAMAS = "damas"

    # ✅ SOLO para liderazgo nacional (Sucesores)
    DIV_DS = "ds"

    # ✅ PERTENENCIA: NO incluye DS
    DIVISION_CHOICES = [
        (DIV_DJM, "División Juvenil Masculina (DJM)"),
        (DIV_DJF, "División Juvenil Femenina (DJF)"),
        (DIV_CABALLEROS, "División Caballeros"),
        (DIV_DAMAS, "División Damas"),
    ]

    division = models.CharField(
        "División",
        max_length=20,
        choices=DIVISION_CHOICES,
        blank=True,
        null=True,
    )

    # -------------------------
    # Liderazgo nacional
    # -------------------------
    is_division_national_leader = models.BooleanField(
        "Responsable nacional de división",
        default=False,
    )

    is_division_national_vice = models.BooleanField(
        "Vice responsable nacional de división",
        default=False,
    )

    # ✅ LIDERAZGO NACIONAL: incluye DS
    NATIONAL_DIVISION_CHOICES = [
        (DIV_DJM, "División Juvenil Masculina (DJM)"),
        (DIV_DJF, "División Juvenil Femenina (DJF)"),
        (DIV_CABALLEROS, "División Caballeros"),
        (DIV_DAMAS, "División Damas"),
        (DIV_DS, "División de Sucesores (DS)"),
    ]

    national_division = models.CharField(
        "División nacional que lidera",
        max_length=20,
        choices=NATIONAL_DIVISION_CHOICES,
        blank=True,
        null=True,
        help_text="Si es responsable/vice nacional, indica qué división lidera (incluye DS).",
    )

    # (opcional)
    division_national_role = models.CharField(max_length=20, blank=True, default="")

    # -------------------------
    # Roles del sistema
    # -------------------------
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

    # -------------------------
    # Props / Helpers
    # -------------------------
    @property
    def age(self):
        if not self.birth_date:
            return None
        today = timezone.now().date()
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years

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

    # --- Admin / Directiva ---
    def is_admin_sistema(self):
        return self.is_superuser or self.role in {self.ROLE_ADMIN, self.ROLE_DIRECTIVA}

    # ✅ Método (NO property) para evitar "'bool' object is not callable"
    def is_admin_like(self):
        return self.is_admin_sistema()

    # -------------------------
    # División nacional helpers
    # -------------------------
    def is_national_division_lead(self):
        return bool(self.is_division_national_leader and self.national_division)

    def is_national_division_vice(self):
        return bool(self.is_division_national_vice and self.national_division)

    def is_national_division_role(self):
        return self.is_national_division_lead() or self.is_national_division_vice()

    # -------------------------
    # División: menú / permisos
    # -------------------------
    def effective_division_for_menu(self):
        """
        División que debe ver en el menú:
        - si es RN/Vice RN -> national_division
        - sino -> division (pertenencia)
        """
        if self.is_national_division_role():
            return (self.national_division or "").lower() or None
        return (self.division or "").lower() or None

    def can_view_division(self, division_key: str) -> bool:
        """
        Quién puede ver una división:
        - admin/directiva/superuser: todas
        - RN/Vice RN: su national_division
        - miembro normal: su division
        """
        division_key = (division_key or "").lower()

        if self.is_admin_like():
            return True

        eff = self.effective_division_for_menu()
        return bool(eff and eff == division_key)

    def can_manage_division_posts(self, division_key: str) -> bool:
        """
        Quién puede administrar publicaciones:
        - admin_like: todas
        - RN/Vice RN: solo su national_division
        """
        division_key = (division_key or "").lower()

        if self.is_admin_like():
            return True

        if self.is_national_division_role():
            return (self.national_division or "").lower() == division_key

        return False

    # -------------------------
    # Kofu helpers
    # -------------------------
    def can_view_active_members(self):
        return self.role in {
            self.ROLE_RESP_GRUPO,
            self.ROLE_RESP_ZONA,
            self.ROLE_RESP_SECTOR,
            self.ROLE_DIRECTIVA,
            self.ROLE_ADMIN,
        } or self.is_superuser

    def get_sector(self):
        if self.group_id and self.group and self.group.zona_id and self.group.zona and self.group.zona.sector_id:
            return self.group.zona.sector
        return None


class Sector(models.Model):
    name = models.CharField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Zona(models.Model):
    sector = models.ForeignKey(Sector, on_delete=models.CASCADE, related_name="zonas")
    name = models.CharField(max_length=120)

    class Meta:
        unique_together = ("sector", "name")
        ordering = ["sector__name", "name"]

    def __str__(self):
        return f"{self.sector} / {self.name}"


class Grupo(models.Model):
    zona = models.ForeignKey(Zona, on_delete=models.CASCADE, related_name="grupos")
    name = models.CharField(max_length=120)

    class Meta:
        unique_together = ("zona", "name")
        ordering = ["zona__sector__name", "zona__name", "name"]

    def __str__(self):
        return f"{self.zona} / {self.name}"

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
        (REL_HEAD, "Titular del Gohonzon"),
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
    # Modo de visibilidad
    VIS_PUBLIC = "public"
    VIS_CUSTOM = "custom"
    VISIBILITY_CHOICES = [
        (VIS_PUBLIC, "Visible para todos (público)"),
        (VIS_CUSTOM, "Visible para selección (roles/divisiones)"),
    ]

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    date = models.DateField()
    time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    price = models.CharField(max_length=50, blank=True)
    created_by = models.ForeignKey("User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # compatibilidad con tu código anterior (si quieres puedes dejarlo)
    is_public = models.BooleanField(default=True)

    visibility = models.CharField(
        max_length=20,
        choices=VISIBILITY_CHOICES,
        default=VIS_PUBLIC,
    )

    # ✅ Checkboxes (guardan listas)
    # Roles permitidos: resp_sector, resp_zona, resp_grupo, miembro (etc)
    target_roles = models.JSONField(default=list, blank=True)

    # Divisiones permitidas: djm, djf, caballeros, damas
    target_divisions = models.JSONField(default=list, blank=True)

    image = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["date", "time"]

    def __str__(self):
        return f"{self.title} — {self.date}"


    
class HomeBanner(models.Model):
    title = models.CharField(max_length=120, blank=True, default="")
    subtitle = models.CharField(max_length=200, blank=True, default="")
    image = models.ImageField(upload_to="home/banners/")
    link_url = models.URLField(blank=True, default="")  # opcional: link externo
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "-created_at"]

    def __str__(self):
        return self.title or f"Banner #{self.id}"

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

    # ✅ NUEVO: quién lo envió (responsable)
    reported_by = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contribution_reports_submitted",
        verbose_name="Enviado por",
    )

    # ✅ NUEVO: para quién es (miembro beneficiario)
    reported_for = models.ForeignKey(
        "User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contribution_reports_for",
        verbose_name="Reportado para",
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
        Devuelve la primera Contribution creada (compatibilidad).
        """
        # Solo permitir aprobar si está pendiente
        if self.status != self.STATUS_PENDING:
            return None

        payload = self.distribution or {}
        splits = payload.get("splits") or []

        created = []

        # ✅ nombre completo del que reportó (self.user)
        reporter_name = f"{self.user.first_name} {self.user.last_name}".strip() or self.user.username

        with transaction.atomic():
            # === Distribución familiar real ===
            if splits:
                for s in splits:
                    uid = s.get("user_id")
                    amt = s.get("amount")

                    if uid is None or amt is None:
                        continue

                    try:
                        u = User.objects.get(id=int(uid))
                    except User.DoesNotExist:
                        continue

                    try:
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
                                f"(reportado por {reporter_name})."
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

            # Marcar informe como aprobado
            self.status = self.STATUS_APPROVED
            self.reviewed_by = reviewer
            self.reviewed_at = timezone.now()
            self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

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
    


class FortunaIssue(models.Model):
    code = models.CharField(max_length=7, unique=True)
    title = models.CharField(max_length=200, blank=True)
    cover_image = models.ImageField(upload_to="fortuna/covers/", blank=True, null=True)

    # ✅ Nuevo: PDF subido desde admin
    material_pdf = models.FileField(upload_to="fortuna/pdfs/", blank=True, null=True)

    # ✅ Deja esto por ahora (compatibilidad)
    material_url = models.URLField(blank=True)

    is_active = models.BooleanField(default=False)
    is_public_archive = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-code"]

    def __str__(self):
        return self.title or f"Fortuna {self.code}"


class FortunaIssuePage(models.Model):
    issue = models.ForeignKey(FortunaIssue, on_delete=models.CASCADE, related_name="pages")
    page_number = models.PositiveIntegerField()
    image = models.ImageField(upload_to="fortuna/pages/")

    class Meta:
        unique_together = ("issue", "page_number")
        ordering = ["page_number"]

    def __str__(self):
        return f"{self.issue.code} - pág {self.page_number}"
    
    

class FortunaPurchase(models.Model):
    PLAN_TRIMESTRAL = "trim"
    PLAN_SEMESTRAL = "sem"
    PLAN_ANUAL = "anual"

    PLAN_CHOICES = [
        (PLAN_TRIMESTRAL, "Trimestral ($12.000)"),
        (PLAN_SEMESTRAL, "Semestral ($24.000)"),
        (PLAN_ANUAL, "Anual ($48.000)"),
    ]

    issue = models.ForeignKey(FortunaIssue, on_delete=models.CASCADE, related_name="purchases")

    # 👇 dueño real del acceso
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="fortuna_purchases"
    )

    # 👇 quien reportó (opcional)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fortuna_reports_created"
    )

    plan = models.CharField(max_length=10, choices=PLAN_CHOICES)

    access_start = models.DateField()
    access_end = models.DateField()

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pendiente"),
        (STATUS_APPROVED, "Aprobada"),
        (STATUS_REJECTED, "Rechazada"),
    ]

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)

    deposit_date = models.DateField(null=True, blank=True)
    receipt = models.FileField(upload_to="fortuna/receipts/", null=True, blank=True)
    note = models.TextField(blank=True, default="")
    reject_reason = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} - {self.get_plan_display()} ({self.status})"



class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    is_buyer = models.BooleanField(default=False)

    def __str__(self):
        return self.user.username

class DivisionPost(models.Model):
    DIVISION_CHOICES = [
        ("djm", "DJM"),
        ("djf", "DJF"),
        ("caballeros", "Caballeros"),
        ("damas", "Damas"),
    ]

    KIND_NEWS = "news"
    KIND_ACTIVITY = "activity"
    KIND_CHOICES = [
        (KIND_NEWS, "Noticia / Aviso"),
        (KIND_ACTIVITY, "Actividad"),
    ]

    division = models.CharField(max_length=20, choices=DIVISION_CHOICES)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_NEWS)

    title = models.CharField(max_length=140)
    description = models.TextField(blank=True)

    # Solo para actividades (si es noticia, puede quedar vacío)
    event_date = models.DateField(null=True, blank=True)

    image = models.ImageField(upload_to="division_posts/", null=True, blank=True)

    # Publicación y orden
    is_published = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)   # “Aviso destacado”
    priority = models.IntegerField(default=0)          # mayor = más arriba

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-priority", "-created_at"]

    def __str__(self):
        return f"{self.get_division_display()} - {self.title}"
    
class ImportantDate(models.Model):
    SCOPE_GENERAL = "general"
    SCOPE_CHILE = "chile"

    SCOPE_CHOICES = [
        (SCOPE_GENERAL, "General SGI"),
        (SCOPE_CHILE, "Chile"),
    ]

    date = models.DateField("Fecha")
    title = models.CharField("Título", max_length=150)
    description = models.TextField("Descripción", blank=True)
    scope = models.CharField(
        "Ámbito",
        max_length=20,
        choices=SCOPE_CHOICES,
        default=SCOPE_GENERAL,
    )
    is_active = models.BooleanField("Activo", default=True)
    priority = models.IntegerField("Prioridad", default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-priority", "date"]
        verbose_name = "Fecha importante"
        verbose_name_plural = "Fechas importantes"

    def __str__(self):
        return f"{self.date} - {self.title}"

class Notice(models.Model):
    TARGET_GLOBAL = "global"
    TARGET_SECTOR = "sector"
    TARGET_ZONA = "zona"
    TARGET_GRUPO = "grupo"

    TARGET_CHOICES = [
        (TARGET_GLOBAL, "Todos (Global)"),
        (TARGET_SECTOR, "Sector / Región"),
        (TARGET_ZONA, "Zona"),
        (TARGET_GRUPO, "Grupo"),
    ]

    title = models.CharField("Título", max_length=160)
    body = models.TextField("Contenido", blank=True)
    image = models.ImageField(
        "Imagen (opcional)",
        upload_to="notices/",
        null=True,
        blank=True,
    )
    target = models.CharField("Audiencia", max_length=20, choices=TARGET_CHOICES, default=TARGET_GLOBAL)

    # a qué apunta (según target)
    sector = models.ForeignKey("Sector", null=True, blank=True, on_delete=models.CASCADE)
    zona = models.ForeignKey("Zona", null=True, blank=True, on_delete=models.CASCADE)
    grupo = models.ForeignKey("Grupo", null=True, blank=True, on_delete=models.CASCADE)

    is_pinned = models.BooleanField("Destacado", default=False)
    priority = models.IntegerField("Prioridad", default=0)
    is_active = models.BooleanField("Activo", default=True)

    start_at = models.DateTimeField("Desde", null=True, blank=True)
    end_at = models.DateTimeField("Hasta", null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_pinned", "-priority", "-created_at"]
        verbose_name = "Aviso"
        verbose_name_plural = "Avisos"

    def __str__(self):
        return self.title

class NewsPost(models.Model):
    # Ámbito (para separar Chile vs general SGI)
    SCOPE_GENERAL = "general"
    SCOPE_CHILE = "chile"
    SCOPE_CHOICES = [
        (SCOPE_GENERAL, "General SGI"),
        (SCOPE_CHILE, "Chile"),
    ]

    # Audiencia (igual a Notice)
    TARGET_GLOBAL = "global"
    TARGET_SECTOR = "sector"
    TARGET_ZONA = "zona"
    TARGET_GRUPO = "grupo"
    TARGET_CHOICES = [
        (TARGET_GLOBAL, "Todos (Global)"),
        (TARGET_SECTOR, "Sector / Región"),
        (TARGET_ZONA, "Zona"),
        (TARGET_GRUPO, "Grupo"),
    ]

    title = models.CharField("Título", max_length=180)
    summary = models.TextField("Bajada / Resumen", blank=True, default="")
    body = models.TextField("Contenido", blank=True, default="")

    image = models.ImageField("Imagen (opcional)", upload_to="news/", null=True, blank=True)

    scope = models.CharField("Ámbito", max_length=20, choices=SCOPE_CHOICES, default=SCOPE_GENERAL)
    target = models.CharField("Audiencia", max_length=20, choices=TARGET_CHOICES, default=TARGET_GLOBAL)
    source_url = models.URLField("Fuente (URL)", blank=True, default="")
    
    sector = models.ForeignKey("Sector", null=True, blank=True, on_delete=models.CASCADE)
    zona = models.ForeignKey("Zona", null=True, blank=True, on_delete=models.CASCADE)
    grupo = models.ForeignKey("Grupo", null=True, blank=True, on_delete=models.CASCADE)

    is_pinned = models.BooleanField("Destacado", default=False)
    priority = models.IntegerField("Prioridad", default=0)
    is_published = models.BooleanField("Publicado", default=True)

    published_at = models.DateTimeField("Fecha publicación", default=timezone.now)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_pinned", "-priority", "-published_at", "-created_at"]
        verbose_name = "Noticia"
        verbose_name_plural = "Noticias"

    def __str__(self):
        return self.title