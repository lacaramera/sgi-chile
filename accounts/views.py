import csv, json, logging
logger = logging.getLogger(__name__)
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils.encoding import force_str, force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.forms import SetPasswordForm
from .models import User, Event, HomeBanner, ContributionReport, Contribution, Notification, Household, HouseholdMember, Sector, Zona, Grupo, FortunaIssue, FortunaIssuePage, FortunaPurchase, Profile, DivisionPost, ImportantDate, Notice, NewsPost
from decimal import Decimal, InvalidOperation
from django.db.models import Sum, Count,  Q
from django.http import HttpResponseForbidden, HttpResponse, JsonResponse, FileResponse, Http404
from .models import Sector, Zona, Grupo
from django.core.mail import send_mail
from django.conf import settings
from .forms import MemberCreateForm, MemberEditForm, SelfRegisterForm
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.db import models, connection, transaction
from django.template.loader import get_template
from calendar import monthrange
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.urls import reverse
from urllib.parse import quote, unquote
from django.core.paginator import Paginator
from datetime import date
from dateutil.relativedelta import relativedelta
from .utils import  send_activation_email

logger = logging.getLogger(__name__)


def _has_fortuna_access(user, issue: FortunaIssue) -> bool:
    # buyer manual
    profile, _ = Profile.objects.get_or_create(user=user)
    if profile.is_buyer:
        return True

    today = timezone.now().date()

    # acceso por compras aprobadas y vigentes (según tu lógica nueva)
    return FortunaPurchase.objects.filter(
        user=user,
        status=FortunaPurchase.STATUS_APPROVED,
        access_start__lte=today,
        access_end__gte=today,
    ).exists()



def _can_report_for_others(u):
    return u.is_authenticated and u.role in {
        User.ROLE_RESP_SECTOR,
        User.ROLE_RESP_ZONA,
        User.ROLE_RESP_GRUPO,
        User.ROLE_ADMIN,
        User.ROLE_DIRECTIVA,
    } or u.is_superuser

def _calculate_fortuna_period(plan: str, deposit_date: date):
    # acceso comienza el 1 del mes siguiente
    start = (deposit_date.replace(day=1) + relativedelta(months=1))

    months = {
        "trim": 3,
        "sem": 6,
        "anual": 12,
    }[plan]

    end = start + relativedelta(months=months) - relativedelta(days=1)
    return start, end



def _get_users_in_scope(u):
    """
    Devuelve un queryset de usuarios que este usuario puede reportar "en nombre de".
    """
    qs = User.objects.all().select_related("group", "group__zona", "group__zona__sector")

    # admin/directiva/superuser => todos (si quieres)
    if u.is_superuser or u.role in {User.ROLE_ADMIN, User.ROLE_DIRECTIVA}:
        return qs.order_by("first_name", "last_name", "id")

    # si no es responsable => solo él mismo (pero en UI no le mostramos selector)
    if u.role not in {User.ROLE_RESP_SECTOR, User.ROLE_RESP_ZONA, User.ROLE_RESP_GRUPO}:
        return User.objects.filter(id=u.id)

    # debe tener group asignado para deducir alcance
    if not u.group_id:
        return User.objects.none()

    if u.role == User.ROLE_RESP_GRUPO:
        return qs.filter(group_id=u.group_id).order_by("first_name", "last_name", "id")

    if u.role == User.ROLE_RESP_ZONA:
        zona_id = u.group.zona_id
        return qs.filter(group__zona_id=zona_id).order_by("first_name", "last_name", "id")

    # resp_sector
    sector_id = u.group.zona.sector_id if u.group and u.group.zona_id else None
    if not sector_id:
        return User.objects.none()

    return qs.filter(group__zona__sector_id=sector_id).order_by("first_name", "last_name", "id")


def _event_visible_to_user(ev, u):
    # no logueado
    if not u.is_authenticated:
        return ev.visibility == ev.VIS_PUBLIC

    # admin/directiva/superuser => ven todo
    if u.is_superuser or u.role in {u.ROLE_ADMIN, u.ROLE_DIRECTIVA}:
        return True

    # público => lo ve cualquiera
    if ev.visibility == ev.VIS_PUBLIC:
        return True

    # custom
    roles = set((ev.target_roles or []))
    divs  = set((ev.target_divisions or []))

    user_role = u.role

    user_div = (u.effective_division_for_menu() or "").lower() if hasattr(u, "effective_division_for_menu") else ""
    # por si acaso, también soporta division directa
    user_div2 = (u.division or "").lower() if getattr(u, "division", None) else ""

    role_ok = (not roles) or (user_role in roles)
    div_ok  = (not divs) or ((user_div and user_div in divs) or (user_div2 and user_div2 in divs))

    # ✅ CLAVE: si hay ambos filtros, deben cumplirse ambos (AND implícito)
    return role_ok and div_ok



def _members_scope_qs(user, qs):
    """
    Restringe el queryset de miembros según el rol del usuario.
    - Admin/Directiva/Superuser: ve todo
    - Resp Sector: ve solo miembros de su sector
    - Resp Zona: ve solo miembros de su zona
    - Resp Grupo: ve solo miembros de su grupo
    """
    if user.is_superuser or user.is_admin_like():
        return qs

    # Grupo del usuario (si no tiene, no puede ver nada)
    if not user.group_id:
        return qs.none()

    if user.is_responsable_grupo():
        return qs.filter(group_id=user.group_id)

    if user.is_responsable_zona():
        zona_id = getattr(user.group, "zona_id", None)
        if not zona_id:
            return qs.none()
        return qs.filter(group__zona_id=zona_id)

    if user.is_responsable_sector():
        sector = user.get_sector()
        if not sector:
            return qs.none()
        return qs.filter(group__zona__sector_id=sector.id)

    # miembro normal: no ve lista
    return qs.none()

def _can_view_member_profiles(user) -> bool:
    return (
        user.is_superuser
        or user.is_admin_like()
        or user.is_responsable_sector()
        or user.is_responsable_zona()
        or user.is_responsable_grupo()
    )

def _user_can_access_target_member(request_user, target_user) -> bool:
    """
    Verifica si request_user puede acceder a target_user según alcance.
    Admin/directiva/superuser: todo.
    Resp sector: mismo sector.
    Resp zona: misma zona.
    Resp grupo: mismo grupo.
    """
    if request_user.is_superuser or request_user.is_admin_like():
        return True

    # si no tiene grupo, no puede acotar bien
    if not request_user.group_id:
        return False

    if request_user.is_responsable_grupo():
        return target_user.group_id == request_user.group_id

    if request_user.is_responsable_zona():
        rz = getattr(request_user.group, "zona_id", None)
        tz = getattr(getattr(target_user.group, "zona", None), "id", None)
        # más directo:
        tz2 = getattr(target_user.group, "zona_id", None) if target_user.group_id else None
        return bool(rz and tz2 and rz == tz2)

    if request_user.is_responsable_sector():
        rs = request_user.get_sector()
        ts = target_user.get_sector() if hasattr(target_user, "get_sector") else None
        return bool(rs and ts and rs.id == ts.id)

    return False

def home(request):
    today = timezone.now().date()
    now = timezone.now()
    u = request.user

    # -------------------------------------------------
    # AVISOS
    # -------------------------------------------------
    notices_qs = (
        Notice.objects
        .filter(is_active=True)
        .filter(Q(start_at__isnull=True) | Q(start_at__lte=now))
        .filter(Q(end_at__isnull=True) | Q(end_at__gte=now))
    )

    if u.is_authenticated:
        sector_id = u.get_sector().id if u.get_sector() else None
        zona_id = u.group.zona_id if u.group and u.group.zona_id else None
        group_id = u.group_id

        notices = notices_qs.filter(
            Q(target=Notice.TARGET_GLOBAL)
            | Q(target=Notice.TARGET_SECTOR, sector_id=sector_id)
            | Q(target=Notice.TARGET_ZONA, zona_id=zona_id)
            | Q(target=Notice.TARGET_GRUPO, grupo_id=group_id)
        )[:10]
    else:
        notices = notices_qs.filter(target=Notice.TARGET_GLOBAL)[:10]

    # -------------------------------------------------
    # BANNERS
    # -------------------------------------------------
    banners = (
        HomeBanner.objects
        .filter(is_active=True)
        .order_by("order", "-created_at")
    )

    # -------------------------------------------------
    # EVENTOS DEL MES
    # -------------------------------------------------
    start = today.replace(day=1)
    end = today.replace(day=monthrange(today.year, today.month)[1])

    month_qs = (
        Event.objects
        .filter(date__range=[start, end])
        .order_by("date", "time", "title")
    )

    if not u.is_authenticated:
        upcoming = month_qs.filter(visibility=Event.VIS_PUBLIC)[:20]

    else:
        is_admin_like = (
            u.is_superuser
            or u.role in {u.ROLE_ADMIN, u.ROLE_DIRECTIVA}
        )

        if is_admin_like:
            upcoming = month_qs[:20]

        else:
            user_role = u.role
            user_div = (u.effective_division_for_menu() or "").lower()
            user_div2 = (
                (u.division or "").lower()
                if getattr(u, "division", None)
                else ""
            )

            # 🔹 SQLITE → filtrar en Python
            if connection.vendor == "sqlite":
                candidates = list(month_qs[:300])
                upcoming = [
                    ev for ev in candidates
                    if _event_visible_to_user(ev, u)
                ][:20]

            # 🔹 POSTGRES (producción)
            else:
                roles_q = Q()
                divs_q = Q()

                # si hay roles: debe contener el rol del usuario
                roles_q = Q(target_roles__contains=[user_role])

                # si hay divisiones: debe contener alguna división del usuario
                divs_q = Q()
                if user_div:
                    divs_q |= Q(target_divisions__contains=[user_div])
                if user_div2:
                    divs_q |= Q(target_divisions__contains=[user_div2])

                visible_q = (
                    Q(visibility=Event.VIS_PUBLIC)
                    |
                    (
                        Q(visibility=Event.VIS_CUSTOM)
                        &
                        (
                            # ✅ Caso 1: evento tiene roles y divisiones -> AND
                            (
                                Q(target_roles__len__gt=0) &
                                Q(target_divisions__len__gt=0) &
                                roles_q &
                                divs_q
                            )
                            |
                            # ✅ Caso 2: evento tiene SOLO roles
                            (
                                Q(target_roles__len__gt=0) &
                                Q(target_divisions__len=0) &
                                roles_q
                            )
                            |
                            # ✅ Caso 3: evento tiene SOLO divisiones
                            (
                                Q(target_roles__len=0) &
                                Q(target_divisions__len__gt=0) &
                                divs_q
                            )
                        )
                    )
                )


                upcoming = month_qs.filter(visible_q)[:20]

    # -------------------------------------------------
    # FECHAS IMPORTANTES
    # -------------------------------------------------
    important_dates = (
        ImportantDate.objects
        .filter(is_active=True, date__range=[start, end])
        .order_by("-priority", "date")[:10]
    )

    # -------------------------------------------------
    # NOTICIAS
    # -------------------------------------------------
    news = (
        NewsPost.objects
        .filter(is_published=True)
        .filter(published_at__lte=now)
        .filter(_news_for_user_q(u))
        [:6]
    )

    fallback_images = ["banner.jpg", "banner2.jpg", "banner3.jpg"]

    context = {
        "banners": banners,
        "fallback_images": fallback_images,
        "upcoming": upcoming,
        "important_dates": important_dates,
        "today": today,
        "notices": notices,
        "news": news,
    }

    return render(request, "home.html", context)


import logging
logger = logging.getLogger(__name__)


def register_member(request):
    """
    Registro público:
    - username = rut
    - crea usuario INACTIVO
    - envía correo de activación para crear contraseña
    - si el correo falla, IGUAL deja el usuario creado (y se puede reenviar desde admin)
    """

    if request.method == "POST":
        form = SelfRegisterForm(request.POST)

        if form.is_valid():
            user = form.save()  # ✅ se crea sí o sí

            try:
                _send_activation_email(request, user)

                messages.success(
                    request,
                    "✅ Registro recibido. Revisa tu correo para activar tu cuenta y crear tu contraseña."
                )
                return redirect("login")

            except Exception:
                logger.exception("Fallo enviando correo de activación a %s", user.email)

                messages.warning(
                    request,
                    "✅ Cuenta creada, pero NO se pudo enviar el correo de activación. "
                    "Contacta al administrador para reenviar la activación."
                )
                return redirect("login")

    else:
        form = SelfRegisterForm()

    return render(request, "registration/register.html", {
        "form": form,
        "sectors": Sector.objects.all().order_by("name"),
    })
    
#def _send_activation_email(request, user: User):
 #   uid = urlsafe_base64_encode(force_bytes(user.pk))
  #  token = default_token_generator.make_token(user)

#    activation_link = request.build_absolute_uri(
 #       reverse("activate", kwargs={"uidb64": uid, "token": quote(token)})
  #  )

   # subject = "Activa tu cuenta - SGI Chile"
    #message = get_template("accounts/activation_email.txt").render({
     #   "user": user,
      #  "activation_link": activation_link,
#    })
#
 #   logger.info(
  #      "Enviando activación por Resend → to=%s from=%s api_key_set=%s",
   #     user.email,
    #    getattr(settings, "DEFAULT_FROM_EMAIL", None),
     #   bool(getattr(settings, "RESEND_API_KEY", None)),
    #)


 #   send_email_resend(
  #      subject=subject,
   #     message=message,
    #    to_email=user.email,
    #)
def _send_activation_email(request, user):
    return send_activation_email(user, request)


def activate_account(request, uidb64, token):
    token = unquote(token)

    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except Exception:
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return render(request, "accounts/activation_invalid.html")

    # Si ya está activo, no tiene sentido reactivar
    if user.is_active:
        messages.info(request, "Tu cuenta ya estaba activada. Inicia sesión.")
        return redirect("login")

    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            user.is_active = True
            user.save(update_fields=["is_active"])

            # ✅ IMPORTANTE: NO hacemos login automático
            messages.success(request, "✅ Cuenta activada. Ya puedes iniciar sesión.")
            return redirect("login")  # o "home" si quisieras, pero login es lo más lógico
    else:
        form = SetPasswordForm(user)

    return render(request, "accounts/activate.html", {"form": form})

@login_required
def dashboard(request):
    # Solo admin-like
    if not _is_admin_like(request.user):
        messages.error(request, "No tienes permisos para ver el dashboard.")
        return redirect("home")
        # o si prefieres bloqueo duro:
        # raise PermissionDenied("No tienes permisos para ver el dashboard.")

    return render(request, "dashboard.html", {"user": request.user})

@login_required
def kofu_view(request):
    return render(request, "kofu.html")

@login_required
def kofu_view(request):
    user = request.user
    context = {
        "can_view_active_members": user.can_view_active_members(),
    }
    return render(request, "kofu.html", context)

@login_required
def kofu_report(request):
    """
    Formulario para informar una contribución (depósito ya realizado).
    Crea un ContributionReport en estado 'pending'.
    """
    context = {}

    u = request.user

    # ✅ MODO: solo permitir "por otro miembro" si vienes desde el botón (?for=other)
    other_mode = (request.GET.get("for") == "other") or (request.POST.get("__other_mode") == "1")
    context["other_mode"] = other_mode

    # ✅ lista para selector "en nombre de" (solo en other_mode)
    can_report_for_others = other_mode and _can_report_for_others(u)
    scope_users_qs = _get_users_in_scope(u) if can_report_for_others else User.objects.none()

    context["can_report_for_others"] = can_report_for_others
    context["scope_users"] = scope_users_qs


    # =========================================================
    # ✅ DEFINIR A QUIÉN SE REPORTA (target_user)
    # =========================================================
    target_user = u  # por defecto: para mí

    # Si el responsable seleccionó a otro miembro (POST)
    report_for_user_id = (request.POST.get("report_for_user_id") or request.GET.get("report_for_user_id") or "").strip()

    if request.method == "POST" and can_report_for_others and report_for_user_id:
        try:
            selected_id = int(report_for_user_id)
        except Exception:
            selected_id = None

        if selected_id and selected_id != u.id:
            # ✅ validar alcance
            if not scope_users_qs.filter(id=selected_id).exists():
                return HttpResponseForbidden("No puedes reportar para un miembro fuera de tu alcance.")
            target_user = User.objects.get(id=selected_id)

    # Para que el HTML pueda marcar el select correctamente
    context["target_user"] = target_user
    context["report_for_user_id"] = str(target_user.id)

    # =========================================================
    # ✅ FAMILIA DEL TARGET_USER (distribución familiar)
    # =========================================================
    family_members = [target_user]
    try:
        membership = target_user.household_membership
        qs = (
            HouseholdMember.objects
            .filter(household=membership.household)
            .select_related("user")
            .order_by("-is_primary", "user__first_name", "user__last_name", "user__id")
        )

        ordered = [m.user for m in qs]
        # mover target_user al inicio sí o sí
        ordered = [target_user] + [x for x in ordered if x.id != target_user.id]
        family_members = ordered
    except HouseholdMember.DoesNotExist:
        pass


    context["family_members"] = family_members

    if request.method == "POST":
        amount_raw = request.POST.get("amount", "").strip()
        date_raw = request.POST.get("deposit_date", "").strip()
        note = request.POST.get("note", "").strip()
        receipt = request.FILES.get("receipt")

        # ✅ OJO: esto es lo correcto (IDs + montos)
        family_user_ids = request.POST.getlist("family_user_id[]")
        family_amounts = request.POST.getlist("family_amount[]")

        errors = {}

        # validar monto principal
        amount = None
        try:
            amount = Decimal(amount_raw.replace(".", "").replace(",", "."))
            if amount <= 0:
                raise InvalidOperation()
        except Exception:
            errors["amount"] = "Ingresa un monto válido mayor a 0."

        # validar fecha
        deposit_date = None
        try:
            if not date_raw:
                raise ValueError()
            deposit_date = timezone.datetime.strptime(date_raw, "%Y-%m-%d").date()
        except Exception:
            errors["deposit_date"] = "Ingresa una fecha de depósito válida."

        # validar archivo
        if not receipt:
            errors["receipt"] = "Debes adjuntar el comprobante de depósito."

        # ✅ construir splits reales (por user_id)
        splits = []
        lines = []
        family_sum = Decimal("0")

        # seguridad: solo IDs del hogar
        allowed_ids = set(u.id for u in family_members)

        for uid_text, monto_text in zip(family_user_ids, family_amounts):
            uid_text = (uid_text or "").strip()
            monto_text = (monto_text or "").strip()

            # fila vacía -> ignorar
            if not uid_text and not monto_text:
                continue

            # uid válido
            try:
                uid = int(uid_text)
            except Exception:
                errors["family_distribution"] = "Selección de familiar inválida."
                break

            if uid not in allowed_ids:
                errors["family_distribution"] = "No puedes distribuir a usuarios fuera de tu hogar."
                break

            # monto válido
            try:
                monto_text_clean = (
                    (monto_text or "")
                    .replace("$", "")
                    .replace(" ", "")
                    .replace(".", "")
                    .replace(",", ".")
                    .strip()
                )

                # si quedó vacío o es 0 → ignorar fila
                if monto_text_clean in ("", "0"):
                    continue

                try:
                    amt = Decimal(monto_text_clean)
                except Exception:
                    errors["family_distribution"] = f"Monto inválido: {monto_text}"
                    break

            except Exception:
                errors["family_distribution"] = f"Monto inválido: {monto_text}"
                break

            if amt < 0:
                errors["family_distribution"] = "No se permiten montos negativos."
                break

            if amt == 0:
                continue

            member_u = User.objects.filter(id=uid).first()
            if not member_u:
                errors["family_distribution"] = "Usuario familiar no existe."
                break

            splits.append({"user_id": member_u.id, "amount": float(amt)})
            lines.append(f"{member_u.first_name} {member_u.last_name}: {amt}")

            family_sum += amt

        # ✅ si no distribuyó nada, dejamos todo al mismo usuario
        if amount is not None and not splits:
            member_u = target_user
            splits = [{"user_id": member_u.id, "amount": float(amount)}]
            lines = [f"{member_u.first_name} {member_u.last_name}: {amount}"]


            family_sum = amount

        # ✅ si distribuyó, debe cuadrar con el monto principal
        if amount is not None and splits and family_sum != amount:
            errors["family_distribution"] = (
                f"La suma de la distribución (${family_sum}) no coincide con el monto informado (${amount})."
            )

        if errors:
            context.update({
                "errors": errors,
                "amount_value": amount_raw,
                "date_value": date_raw,
                "note_value": note,
                "success": False,
                # opcional: si quieres repoblar inputs en el HTML después
                "family_amounts_prefill": family_amounts,
            })
        else:
            distribution_payload = {
                "total": float(amount),
                "splits": splits,
            }

            ContributionReport.objects.create(
                user=target_user,                 # ✅ el “dueño” del aporte
                reported_by=u if target_user.id != u.id else None,
                reported_for=target_user if target_user.id != u.id else None,

                deposit_amount=amount,
                deposit_date=deposit_date,
                receipt=receipt,
                note=note,
                distribution=distribution_payload,
                family_distribution="\n".join(lines),
                status=ContributionReport.STATUS_PENDING,
            )

            context["success"] = True
            context["amount_value"] = ""
            context["date_value"] = ""
            context["note_value"] = ""

    else:
        context["amount_value"] = ""
        context["date_value"] = ""
        context["note_value"] = ""

    return render(request, "kofu_report.html", context)





@login_required
def kofu_history(request):
    """
    Historial de contribuciones CONFIRMADAS del usuario actual.
    """
    contributions = (
        Contribution.objects
        .filter(member=request.user, is_confirmed=True)
        .order_by("-date", "-created_at")
    )

    total = contributions.aggregate(Sum("amount"))["amount__sum"] or Decimal("0")

    context = {
        "contributions": contributions,
        "total_amount": total,
    }
    return render(request, "kofu_history.html", context)

@login_required
def kofu_active_members(request):
    """
    Miembros activos en contribución (Kofu).
    Activo = total contribuciones confirmadas >= THRESHOLD.
    - admin/directiva: ven todos + ven montos
    - responsables: ven SOLO su alcance (sector/zona/grupo) + NO ven montos
    """
    if not request.user.can_view_active_members():
        return HttpResponseForbidden("No tienes permiso para ver esta sección.")

    THRESHOLD = Decimal("12000.00")
    q = (request.GET.get("q") or "").strip()

    qs = (
        Contribution.objects
        .filter(is_confirmed=True)
        .values(
            "member__id",
            "member__first_name",
            "member__last_name",
            "member__username",
            "member__role",
            "member__group__zona__sector__name",
            "member__group__zona__name",
            "member__group__name",
            "member__group__zona__sector_id",
            "member__group__zona_id",
            "member__group_id","member__division",
            "member__is_division_national_leader",
            "member__is_division_national_vice",
            "member__national_division",

        )
        .annotate(
            total_amount=Sum("amount"),
            contributions_count=Count("id"),
        )
        .filter(total_amount__gte=THRESHOLD)
        .order_by("-total_amount")
    )

    # 🔎 buscador
    if q:
        qs = qs.filter(
            Q(member__first_name__icontains=q) |
            Q(member__last_name__icontains=q) |
            Q(member__username__icontains=q)
        )

    # ✅ aplicar alcance por rol (sector/zona/grupo) derivado de user.group
    scope = _user_scope_filters(request.user)
    if scope is not None:
        mapped = {}
        for k, v in scope.items():
            if k == "id__in":
                mapped["member__id__in"] = v
            else:
                mapped[f"member__{k}"] = v
        qs = qs.filter(**mapped)

    role_map = dict(User.ROLE_CHOICES)

    active = list(qs)
    for m in active:
        m["role_label"] = role_map.get(m["member__role"], m["member__role"])

    context = {
        "active_members": active,
        "threshold": THRESHOLD,
        "show_amounts": _can_see_kofu_amounts(request.user),
        "q": q,
    }
    return render(request, "kofu_active_members.html", context)


@login_required
def kofu_admin_reports(request):
    """
    Pantalla SOLO para admin/directiva:
    - Ver informes de contribución pendientes
    - Aprobar o rechazar
    """
    if not _is_admin_or_directiva(request.user):
        return HttpResponseForbidden("No tienes permiso para ver esta sección.")

    message = None
    error = None

    if request.method == "POST":
        report_id = request.POST.get("report_id")
        action = request.POST.get("action")
        reason = request.POST.get("reason", "").strip()

        try:
            report = ContributionReport.objects.get(id=report_id)
        except ContributionReport.DoesNotExist:
            error = "El informe seleccionado ya no existe."
        else:
            if action == "approve":
                if report.status == ContributionReport.STATUS_APPROVED:
                    error = "Este informe ya estaba aprobado."
                else:
                    report.approve(request.user)
                    message = f"Informe #{report.id} aprobado y registrado como contribución."

                    # Email (best-effort)
                    try:
                        subject = "Contribución aprobada"
                        body = (
                            f"Hola {report.user.first_name or report.user.username},\n\n"
                            f"Tu informe de contribución #{report.id} por "
                            f"${report.deposit_amount} ha sido aprobado.\n\n"
                            "Muchas gracias por tu aporte.\n\n"
                            "Departamento de contribución SGI Chile"
                        )
                        send_mail(
                            subject,
                            body,
                            getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@sgi-chile.cl"),
                            [report.user.email],
                            fail_silently=True,
                        )
                    except Exception:
                        pass

                    Notification.objects.create(
                        user=report.user,
                        title="Contribución aprobada",
                        message=f"Tu informe #{report.id} por ${report.deposit_amount} ha sido aprobado.",
                    )

            elif action == "reject":
                if report.status == ContributionReport.STATUS_REJECTED:
                    error = "Este informe ya estaba rechazado."
                else:
                    report.reject(request.user, reason)
                    message = f"Informe #{report.id} rechazado."
            else:
                error = "Acción no válida."

    pending_reports = ContributionReport.objects.filter(
        status=ContributionReport.STATUS_PENDING
    )

    processed_reports = ContributionReport.objects.exclude(
        status=ContributionReport.STATUS_PENDING
    )[:20]

    context = {
        "pending_reports": pending_reports,
        "processed_reports": processed_reports,
        "message": message,
        "error": error,
    }
    return render(request, "kofu_admin_reports.html", context)



@login_required
def notifications_center(request):
    """
    Lista de notificaciones del usuario.
    Marca todas como leídas al entrar.
    """
    qs = Notification.objects.filter(user=request.user).order_by("-created_at")
    qs.filter(is_read=False).update(is_read=True)
    return render(request, "notifications.html", {"notifications": qs})

@login_required
def kofu_active_members_export(request):
    """
    Exporta lista de miembros activos en Kofu (CSV).
    SOLO admin/directiva (porque incluye montos).
    """
    if not _is_admin_or_directiva(request.user):
        return HttpResponseForbidden("No tienes permiso para exportar este informe.")

    THRESHOLD = Decimal("12000.00")

    qs = (
        Contribution.objects
        .filter(is_confirmed=True)
        .values(
            "member__first_name",
            "member__last_name",
            "member__username",
        )
        .annotate(
            total_amount=Sum("amount"),
            contributions_count=Count("id"),
        )
        .filter(total_amount__gte=THRESHOLD)
        .order_by("-total_amount")
    )

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="kofu_miembros_activos.csv"'

    writer = csv.writer(response, delimiter=';')
    writer.writerow(["Nombre", "Usuario", "N° contribuciones", "Total aportado (CLP)"])

    for m in qs:
        nombre = f"{m['member__first_name']} {m['member__last_name']}".strip()
        writer.writerow([
            nombre,
            m["member__username"],
            m["contributions_count"],
            m["total_amount"],
        ])

    return response


@login_required
def create_member(request):
    u = request.user

    # ✅ Permisos: admin/directiva/superuser o responsable sector
    if not (u.is_superuser or u.is_admin_like() or u.is_responsable_sector()):
        raise PermissionDenied("No tienes permisos para crear miembros.")

    if request.method == "POST":
        form = MemberCreateForm(request.POST)
        if form.is_valid():
            new_member = form.save(commit=False)

            # ✅ Resp_sector: validar que el group del nuevo miembro pertenece a su sector
            if u.is_responsable_sector() and not (u.is_superuser or u.is_admin_like()):
                rs = u.get_sector()
                if new_member.group_id:
                    g = Grupo.objects.select_related("zona__sector").filter(id=new_member.group_id).first()
                    if not g or not rs or g.zona.sector_id != rs.id:
                        raise PermissionDenied("No puedes crear miembros fuera de tu sector.")
                else:
                    # si quieres obligar a asignar grupo, cambia esto por error en form
                    pass

            new_member.save()
            messages.success(request, "✅ Miembro creado correctamente.")
            return redirect("home")
    else:
        form = MemberCreateForm(initial={"is_active": True})

    # ✅ Resp_sector: limitar sectores mostrados (solo el suyo)
    sectors_qs = Sector.objects.all().order_by("name")
    if u.is_responsable_sector() and not (u.is_superuser or u.is_admin_like()):
        rs = u.get_sector()
        sectors_qs = Sector.objects.filter(id=rs.id) if rs else Sector.objects.none()

    return render(request, "accounts/create_member.html", {
        "form": form,
        "sectors": sectors_qs,
        "role_choices": getattr(request.user, "ROLE_CHOICES", None),
    })




def _is_admin_like(user):
    return user.is_authenticated and (user.is_superuser or user.is_staff or getattr(user, "is_admin_sistema", lambda: False)())


def _apply_members_filters(request, qs):
    q = (request.GET.get("q") or "").strip()
    sector_id = (request.GET.get("sector_id") or "").strip()
    zona_id = (request.GET.get("zona_id") or "").strip()
    group_id = (request.GET.get("group_id") or "").strip()
    role = (request.GET.get("role") or "").strip()

    if q:
        qs = qs.filter(
            models.Q(first_name__icontains=q) |
            models.Q(last_name__icontains=q) |
            models.Q(username__icontains=q) |
            models.Q(email__icontains=q)
        )

    if role:
        qs = qs.filter(role=role)

    if sector_id:
        qs = qs.filter(group__zona__sector_id=sector_id)

    if zona_id:
        qs = qs.filter(group__zona_id=zona_id)

    if group_id:
        qs = qs.filter(group_id=group_id)

    return qs


@login_required
def members_list(request):
    # ✅ Permisos para entrar (ya no solo admin)
    u = request.user
    if not (u.is_superuser or u.is_admin_like() or u.is_responsable_sector() or u.is_responsable_zona() or u.is_responsable_grupo()):
        raise PermissionDenied("No tienes permisos para ver miembros.")

    base_qs = User.objects.select_related("group__zona__sector").all().order_by(
        "first_name", "last_name", "username"
    )

    # ✅ 1) aplicar scope primero
    base_qs = _members_scope_qs(u, base_qs)

    # ✅ 2) luego aplicar filtros del form
    qs = _apply_members_filters(request, base_qs)

    # datos para filtros dependientes
    sector_id = (request.GET.get("sector_id") or "").strip()
    zona_id = (request.GET.get("zona_id") or "").strip()

    # ✅ Opciones de filtros también deben respetar scope
    # Admin/directiva ven todo; responsables ven solo lo suyo
    if u.is_superuser or u.is_admin_like():
        sectors = Sector.objects.all().order_by("name")
    elif u.is_responsable_sector():
        sec = u.get_sector()
        sectors = Sector.objects.filter(id=sec.id).order_by("name") if sec else Sector.objects.none()
    else:
        # resp zona/grupo no deberían elegir sector
        sectors = Sector.objects.none()

    zonas = Zona.objects.none()
    grupos = Grupo.objects.none()

    # Para admin / resp_sector: zonas dependen de sector seleccionado (o el único sector)
    if sector_id:
        zonas = Zona.objects.filter(sector_id=sector_id).order_by("name")

    # Para resp_zona: zonas fijo = su zona
    if u.is_responsable_zona() and u.group and u.group.zona_id:
        zonas = Zona.objects.filter(id=u.group.zona_id)

    # grupos
    if zona_id:
        grupos = Grupo.objects.filter(zona_id=zona_id).order_by("name")

    # Para resp_grupo: grupo fijo = su grupo
    if u.is_responsable_grupo():
        grupos = Grupo.objects.filter(id=u.group_id)

    context = {
        "members": qs,
        "q": (request.GET.get("q") or "").strip(),
        "selected_role": (request.GET.get("role") or "").strip(),
        "selected_sector_id": sector_id,
        "selected_zona_id": zona_id,
        "selected_group_id": (request.GET.get("group_id") or "").strip(),
        "sectors": sectors,
        "zonas": zonas,
        "grupos": grupos,
        "role_choices": User.ROLE_CHOICES,
        "querystring": request.GET.urlencode(),
    }
    return render(request, "accounts/members_list.html", context)




@login_required
def members_export(request):
    u = request.user
    if not (u.is_superuser or u.is_admin_like() or u.is_responsable_sector() or u.is_responsable_zona() or u.is_responsable_grupo()):
        raise PermissionDenied("No tienes permisos para exportar miembros.")

    base_qs = User.objects.select_related("group__zona__sector").all().order_by(
        "first_name", "last_name", "username"
    )

    # ✅ scope primero
    base_qs = _members_scope_qs(u, base_qs)

    # ✅ filtros después
    qs = _apply_members_filters(request, base_qs)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="miembros.csv"'
    response.write("\ufeff")

    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "Nombre","Apellido","Username","RUT","Email","Rol","Activo",
        "Fecha nacimiento","Edad","Sector","Zona","Grupo","Direccion",
        "Fecha ingreso","Unico miembro familia",
    ])

    for m in qs:
        sector_name = ""
        zona_name = ""
        grupo_name = ""
        if m.group:
            grupo_name = m.group.name or ""
            if getattr(m.group, "zona", None):
                zona_name = m.group.zona.name or ""
                if getattr(m.group.zona, "sector", None):
                    sector_name = m.group.zona.sector.name or ""

        birth = getattr(m, "birth_date", None)
        age = getattr(m, "age", None)

        writer.writerow([
            m.first_name or "",
            m.last_name or "",
            m.username or "",
            getattr(m, "rut", "") or "",
            m.email or "",
            m.get_role_display() if hasattr(m, "get_role_display") else (m.role or ""),
            "SI" if m.is_active else "NO",
            birth.strftime("%Y-%m-%d") if birth else "",
            age if age is not None else "",
            sector_name,
            zona_name,
            grupo_name,
            getattr(m, "address", "") or "",
            getattr(m, "join_date", None).strftime("%Y-%m-%d") if getattr(m, "join_date", None) else "",
            "SI" if getattr(m, "is_only_family_member", False) else "NO",
        ])

    return response


@login_required
def edit_member(request, user_id):
    u = request.user

    # ✅ Permisos: admin/directiva/superuser o responsable sector
    if not (u.is_superuser or u.is_admin_like() or u.is_responsable_sector()):
        raise PermissionDenied("No tienes permisos para editar miembros.")

    member = get_object_or_404(User.objects.select_related("group__zona__sector"), id=user_id)

    # ✅ Alcance: resp_sector SOLO puede editar miembros de su sector
    if u.is_responsable_sector() and not (u.is_superuser or u.is_admin_like()):
        if not _user_can_access_target_member(u, member):
            raise PermissionDenied("Solo puedes editar miembros de tu sector.")

    # hogar actual (si tiene)
    current_membership = getattr(member, "household_membership", None)
    current_household = current_membership.household if current_membership else None

    # lista de usuarios para agregar al hogar (búsqueda simple)
    q = (request.GET.get("q") or "").strip()
    candidates = []
    if q:
        candidates = User.objects.filter(
            models.Q(username__icontains=q) |
            models.Q(first_name__icontains=q) |
            models.Q(last_name__icontains=q)
        ).exclude(id=member.id)[:20]

    if request.method == "POST":
        action = request.POST.get("action")

        # 1) Actualizar datos base del usuario
        if action == "save_user":
            member.first_name = (request.POST.get("first_name") or "").strip()
            member.last_name = (request.POST.get("last_name") or "").strip()
            member.email = (request.POST.get("email") or "").strip()
            member.role = request.POST.get("role") or member.role
            member.is_active = True if request.POST.get("is_active") == "on" else False
            member.division = (request.POST.get("division") or "").strip()

            member.address = (request.POST.get("address") or "").strip()
            member.is_only_family_member = True if request.POST.get("is_only_family_member") == "on" else False

            bd = (request.POST.get("birth_date") or "").strip()
            if bd:
                member.birth_date = timezone.datetime.strptime(bd, "%Y-%m-%d").date()
            else:
                member.birth_date = None

            jd = (request.POST.get("join_date") or "").strip()
            if jd:
                member.join_date = timezone.datetime.strptime(jd, "%Y-%m-%d").date()
            else:
                member.join_date = None

            # ✅ group_id con validación de alcance para resp_sector
            gid = (request.POST.get("group_id") or "").strip()
            new_group_id = int(gid) if gid else None

            if u.is_responsable_sector() and not (u.is_superuser or u.is_admin_like()):
                if new_group_id is None:
                    # le permitimos dejarlo sin grupo si quieres; si NO, cambia esto por error.
                    pass
                else:
                    # validar que el grupo pertenece a SU sector
                    rs = u.get_sector()
                    g = Grupo.objects.select_related("zona__sector").filter(id=new_group_id).first()
                    if not g or not g.zona_id or not g.zona.sector_id or not rs or g.zona.sector_id != rs.id:
                        raise PermissionDenied("No puedes asignar un grupo fuera de tu sector.")

            member.group_id = new_group_id

            member.is_division_national_leader = (request.POST.get("is_division_national_leader") == "on")
            member.is_division_national_vice = (request.POST.get("is_division_national_vice") == "on")
            member.national_division = (request.POST.get("national_division") or "").strip() or None

            # seguridad: si NO es RN ni Vice RN, limpiar national_division
            if not (member.is_division_national_leader or member.is_division_national_vice):
                member.national_division = None

            member.save()
            messages.success(request, "✅ Datos del miembro actualizados.")
            return redirect("edit_member", user_id=member.id)

        # 2) Crear hogar si no existe y asignar miembro como primary
        if action == "create_household":
            if not current_household:
                h = Household.objects.create(name=f"Hogar de {member.first_name} {member.last_name}".strip())
                HouseholdMember.objects.create(
                    household=h,
                    user=member,
                    relationship=HouseholdMember.REL_HEAD,
                    is_primary=True,
                )
                messages.success(request, "✅ Hogar creado y asignado.")
            return redirect("edit_member", user_id=member.id)

        # 3) Agregar usuario a hogar actual
        if action == "add_to_household":
            if not current_household:
                messages.error(request, "Primero crea/asigna un hogar a este miembro.")
                return redirect("edit_member", user_id=member.id)

            uid = request.POST.get("add_user_id")
            rel = request.POST.get("relationship") or HouseholdMember.REL_OTHER
            try:
                add_u = User.objects.get(id=int(uid))
            except Exception:
                messages.error(request, "Usuario inválido.")
                return redirect("edit_member", user_id=member.id)

            # ✅ alcance también para agregar familiares: resp_sector solo gente de su sector
            if u.is_responsable_sector() and not (u.is_superuser or u.is_admin_like()):
                if not _user_can_access_target_member(u, add_u):
                    raise PermissionDenied("No puedes agregar usuarios fuera de tu sector.")

            if HouseholdMember.objects.filter(user=add_u).exists():
                messages.error(request, "Ese usuario ya pertenece a otro hogar.")
                return redirect("edit_member", user_id=member.id)

            HouseholdMember.objects.create(
                household=current_household,
                user=add_u,
                relationship=rel,
                is_primary=False,
            )
            messages.success(request, f"✅ {add_u.first_name} {add_u.last_name} agregado al hogar.")
            return redirect("edit_member", user_id=member.id)

        # 4) Quitar usuario del hogar
        if action == "remove_from_household":
            mid = request.POST.get("membership_id")
            m = get_object_or_404(HouseholdMember, id=mid)

            if not current_household or m.household_id != current_household.id:
                messages.error(request, "Acción no válida.")
                return redirect("edit_member", user_id=member.id)

            if m.is_primary and HouseholdMember.objects.filter(household=current_household).exclude(id=m.id).exists():
                messages.error(request, "No puedes quitar al miembro principal si quedan otros en el hogar.")
                return redirect("edit_member", user_id=member.id)

            m.delete()
            messages.success(request, "✅ Miembro removido del hogar.")
            return redirect("edit_member", user_id=member.id)

    # recargar datos de hogar y miembros
    current_membership = getattr(member, "household_membership", None)
    current_household = current_membership.household if current_membership else None
    household_memberships = []
    if current_household:
        household_memberships = (
            HouseholdMember.objects.filter(household=current_household)
            .select_related("user")
            .order_by("-is_primary", "user__first_name", "user__last_name", "user__username")
        )

    member_sector_id = None
    member_zona_id = None
    if getattr(member, "group", None) and member.group and member.group.zona and member.group.zona.sector:
        member_zona_id = member.group.zona_id
        member_sector_id = member.group.zona.sector_id

    # ✅ Para resp_sector: limitar sectores mostrados (solo el suyo)
    sectors_qs = Sector.objects.all().order_by("name")
    if u.is_responsable_sector() and not (u.is_superuser or u.is_admin_like()):
        rs = u.get_sector()
        sectors_qs = Sector.objects.filter(id=rs.id) if rs else Sector.objects.none()

    return render(request, "accounts/edit_member.html", {
        "member": member,
        "household": current_household,
        "memberships": household_memberships,
        "candidates": candidates,
        "q": q,
        "role_choices": User.ROLE_CHOICES,
        "rel_choices": HouseholdMember.REL_CHOICES,
        "sectors": sectors_qs,
        "member_sector_id": member_sector_id,
        "member_zona_id": member_zona_id,
    })

def ajax_zonas_by_sector(request):
    sector_id = (request.GET.get("sector_id") or "").strip()
    if not sector_id:
        return JsonResponse({"zonas": []})

    # ✅ Registro público: devolver siempre
    if not request.user.is_authenticated:
        zonas = Zona.objects.filter(sector_id=sector_id).order_by("name")
        return JsonResponse({"zonas": [{"id": z.id, "name": z.name} for z in zonas]})

    # ✅ Si está logueado, aplicas tus reglas por rol
    u = request.user
    zonas = Zona.objects.none()

    if u.is_superuser or u.is_admin_like():
        zonas = Zona.objects.filter(sector_id=sector_id).order_by("name")
    elif u.is_responsable_sector():
        user_sector = u.get_sector()
        if user_sector and str(user_sector.id) == sector_id:
            zonas = Zona.objects.filter(sector_id=user_sector.id).order_by("name")
    elif u.is_responsable_zona() or u.is_responsable_grupo():
        if u.group and u.group.zona:
            zonas = Zona.objects.filter(id=u.group.zona_id)

    return JsonResponse({"zonas": [{"id": z.id, "name": z.name} for z in zonas]})


def ajax_grupos_by_zona(request):
    zona_id = (request.GET.get("zona_id") or "").strip()
    if not zona_id:
        return JsonResponse({"grupos": []})

    # ✅ Registro público: devolver siempre
    if not request.user.is_authenticated:
        grupos = Grupo.objects.filter(zona_id=zona_id).order_by("name")
        return JsonResponse({"grupos": [{"id": g.id, "name": g.name} for g in grupos]})

    # ✅ Logueado: tus reglas
    u = request.user
    grupos = Grupo.objects.none()

    if u.is_superuser or u.is_admin_like():
        grupos = Grupo.objects.filter(zona_id=zona_id).order_by("name")
    elif u.is_responsable_sector():
        user_sector = u.get_sector()
        if user_sector:
            grupos = Grupo.objects.filter(zona_id=zona_id, zona__sector_id=user_sector.id).order_by("name")
    elif u.is_responsable_zona():
        if u.group and u.group.zona and zona_id == str(u.group.zona_id):
            grupos = Grupo.objects.filter(zona_id=u.group.zona_id).order_by("name")
    elif u.is_responsable_grupo():
        if u.group and zona_id == str(u.group.zona_id):
            grupos = Grupo.objects.filter(id=u.group.id)

    return JsonResponse({"grupos": [{"id": g.id, "name": g.name} for g in grupos]})
@login_required
def profile(request, user_id=None):
    """
    - /perfil/ => mi perfil (editable limitado)
    - /miembros/<id>/perfil/ => admin/directiva pueden ver/editar otro
    """
    if user_id is None:
        member = request.user
    else:
        member = get_object_or_404(User, id=user_id)
        if not _is_admin_like(request.user):
            raise PermissionDenied("No tienes permisos para ver este perfil.")

    is_admin = _is_admin_like(request.user)  # admin/directiva (o tu helper)

    if request.method == "POST":
        # === Campos que cualquiera puede editar (solo si es su propio perfil o admin) ===
        if member != request.user and not is_admin:
            raise PermissionDenied("No puedes editar este perfil.")

        member.first_name = (request.POST.get("first_name") or "").strip()
        member.last_name  = (request.POST.get("last_name") or "").strip()
        member.email      = (request.POST.get("email") or "").strip()
        member.address    = (request.POST.get("address") or "").strip()

        # birth_date (si lo tienes en el modelo)
        bd = (request.POST.get("birth_date") or "").strip()
        if hasattr(member, "birth_date"):
            if bd:
                member.birth_date = timezone.datetime.strptime(bd, "%Y-%m-%d").date()
            else:
                member.birth_date = None

        # foto (si suben archivo)
        if "profile_photo" in request.FILES:
            member.profile_photo = request.FILES["profile_photo"]

        # === Solo admin/directiva: rol + grupo + flags ===
        if is_admin:
            role = (request.POST.get("role") or "").strip()
            if role:
                member.role = role

            # cascada: recibe group_id
            gid = (request.POST.get("group_id") or "").strip()
            member.group_id = int(gid) if gid else None

            # flags (si existen)
            if hasattr(member, "is_only_family_member"):
                member.is_only_family_member = (request.POST.get("is_only_family_member") == "on")

            if "is_active" in request.POST:
                member.is_active = (request.POST.get("is_active") == "on")

        member.save()
        messages.success(request, "✅ Perfil actualizado.")
        return redirect("profile" if user_id is None else "member_profile", user_id=member.id) if user_id else redirect("profile")

    context = {
        "member": member,
        "is_admin": is_admin,
        "role_choices": User.ROLE_CHOICES,
        "grupos": Grupo.objects.select_related("zona__sector").all(),
    }
    return render(request, "accounts/profile.html", context)

@login_required
def my_profile(request):
    return render(request, "accounts/my_profile.html", {"member": request.user})

@login_required
def edit_my_profile(request):
    u = request.user

    if request.method == "POST":
        # campos permitidos para el usuario (tú decides)
        u.first_name = (request.POST.get("first_name") or "").strip()
        u.last_name  = (request.POST.get("last_name") or "").strip()
        u.email      = (request.POST.get("email") or "").strip()
        u.address    = (request.POST.get("address") or "").strip()

        bd = (request.POST.get("birth_date") or "").strip()
        u.birth_date = bd or None

        # foto (input type=file)
        if "profile_photo" in request.FILES:
            u.profile_photo = request.FILES["profile_photo"]

        u.save()
        messages.success(request, "✅ Perfil actualizado.")
        return redirect("my_profile")

    return render(request, "accounts/edit_my_profile.html", {"member": u})


@login_required
def member_profile(request, user_id):
    u = request.user

    if not _can_view_member_profiles(u):
        raise PermissionDenied("No tienes permisos para ver perfiles de miembros.")

    target = get_object_or_404(User.objects.select_related("group__zona__sector"), id=user_id)

    # ✅ alcance
    if not _user_can_access_target_member(u, target):
        raise PermissionDenied("No tienes permisos para ver este perfil.")

    # Renderiza tu mismo template (ajusta el nombre si es otro)
    return render(request, "accounts/member_profile.html", {
        "member": target,
        "is_admin_directiva": _is_admin_or_directiva(u),
    })


def _is_admin_or_directiva(user):
    return user.is_superuser or getattr(user, "role", None) in {user.ROLE_ADMIN, user.ROLE_DIRECTIVA}


def _is_admin_or_directiva(u):
    return u.is_superuser or u.role in {u.ROLE_ADMIN, u.ROLE_DIRECTIVA}

def _user_scope_filters(u):
    """
    Devuelve un dict para filtrar User por alcance (para responsables).
    Si es admin/directiva => None (no filtrar).
    Si es responsable_grupo => solo su group_id
    Si es responsable_zona  => solo su zona (por group__zona_id)
    Si es responsable_sector => solo su sector (por group__zona__sector_id)
    """
    if _is_admin_or_directiva(u):
        return None

    # si el usuario no tiene grupo asignado, no puede filtrar por alcance
    if not u.group_id:
        # para no romper, lo dejamos "solo él"
        return {"id": u.id}

    if u.role == u.ROLE_RESP_GRUPO:
        return {"group_id": u.group_id}

    if u.role == u.ROLE_RESP_ZONA:
        # requiere que el user tenga zona (viene por su group)
        if u.group and u.group.zona_id:
            return {"group__zona_id": u.group.zona_id}
        return {"id": u.id}

    if u.role == u.ROLE_RESP_SECTOR:
        # requiere sector (viene por group -> zona -> sector)
        if u.group and u.group.zona and u.group.zona.sector_id:
            return {"group__zona__sector_id": u.group.zona.sector_id}
        return {"id": u.id}

    # cualquier otro rol: por seguridad "solo él"
    return {"id": u.id}



def _can_see_kofu_amounts(user):
    """Solo admin/directiva (y superuser) ve montos en Kofu activos."""
    return _is_admin_or_directiva(user)



@login_required
def manage_banners(request):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    banners = HomeBanner.objects.all().order_by("order", "-created_at")

    context = {
        "banners": banners,
    }
    return render(request, "accounts/banners/manage_banners.html", context)


@login_required
def create_banner(request):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        subtitle = (request.POST.get("subtitle") or "").strip()
        link_url = (request.POST.get("link_url") or "").strip()
        order = request.POST.get("order") or "0"
        is_active = True if request.POST.get("is_active") == "on" else False
        image = request.FILES.get("image")

        if not image:
            messages.error(request, "Debes subir una imagen.")
            return redirect("create_banner")

        HomeBanner.objects.create(
            title=title,
            subtitle=subtitle,
            link_url=link_url,
            order=int(order),
            is_active=is_active,
            image=image,
        )
        messages.success(request, "✅ Banner creado.")
        return redirect("manage_banners")

    return render(request, "accounts/banners/banner_form.html", {"mode": "create"})


@login_required
def edit_banner(request, banner_id):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    banner = get_object_or_404(HomeBanner, id=banner_id)

    if request.method == "POST":
        banner.title = (request.POST.get("title") or "").strip()
        banner.subtitle = (request.POST.get("subtitle") or "").strip()
        banner.link_url = (request.POST.get("link_url") or "").strip()
        banner.order = int(request.POST.get("order") or 0)
        banner.is_active = True if request.POST.get("is_active") == "on" else False

        new_image = request.FILES.get("image")
        if new_image:
            banner.image = new_image

        banner.save()
        messages.success(request, "✅ Banner actualizado.")
        return redirect("manage_banners")

    return render(
        request,
        "accounts/banners/banner_form.html",
        {"mode": "edit", "banner": banner},
    )


@login_required
def delete_banner(request, banner_id):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    banner = get_object_or_404(HomeBanner, id=banner_id)

    if request.method == "POST":
        banner.delete()
        messages.success(request, "🗑️ Banner eliminado.")
        return redirect("manage_banners")

    return render(request, "accounts/banners/banner_delete.html", {"banner": banner})

from django.shortcuts import render, redirect, get_object_or_404
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.utils import timezone
from calendar import monthrange

from .models import Event  # ya lo tienes


@login_required
def manage_events(request):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    today = timezone.now().date()
    year = int(request.GET.get("y") or today.year)
    month = int(request.GET.get("m") or today.month)

    start = today.replace(year=year, month=month, day=1)
    end_day = monthrange(year, month)[1]
    end = today.replace(year=year, month=month, day=end_day)

    events = (
        Event.objects
        .filter(date__range=[start, end])
        .order_by("date", "time", "title")
    )

    return render(request, "accounts/events/manage_events.html", {
        "events": events,
        "year": year,
        "month": month,
    })


@login_required
def create_event(request):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()
        description = (request.POST.get("description") or "").strip()
        location = (request.POST.get("location") or "").strip()
        price = (request.POST.get("price") or "").strip()
        visibility = (request.POST.get("visibility") or Event.VIS_PUBLIC).strip()
        if visibility not in {Event.VIS_PUBLIC, Event.VIS_CUSTOM}:
            visibility = Event.VIS_PUBLIC

        target_roles = request.POST.getlist("target_roles")
        target_divisions = request.POST.getlist("target_divisions")

        # Sanear valores permitidos
        allowed_roles = {User.ROLE_RESP_SECTOR, User.ROLE_RESP_ZONA, User.ROLE_RESP_GRUPO, User.ROLE_MIEMBRO}
        allowed_divs = {User.DIV_DJM, User.DIV_DJF, User.DIV_CABALLEROS, User.DIV_DAMAS}

        target_roles = [r for r in target_roles if r in allowed_roles]
        target_divisions = [d for d in target_divisions if d in allowed_divs]
        # Si es público, ignoramos selección
        if visibility == Event.VIS_PUBLIC:
            is_public = True
            target_roles = []
            target_divisions = []
        else:
            is_public = False

        is_public = (visibility == Event.VIS_PUBLIC)

        date_str = (request.POST.get("date") or "").strip()
        time_str = (request.POST.get("time") or "").strip()

        if not title or not date_str:
            messages.error(request, "Título y fecha son obligatorios.")
            return redirect("create_event")

        try:
            date_val = timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            messages.error(request, "Fecha inválida.")
            return redirect("create_event")

        time_val = None
        if time_str:
            try:
                time_val = timezone.datetime.strptime(time_str, "%H:%M").time()
            except Exception:
                messages.error(request, "Hora inválida (usa HH:MM).")
                return redirect("create_event")

        Event.objects.create(
            title=title,
            description=description,
            location=location,
            price=price,
            date=date_val,
            time=time_val,
            is_public=is_public,
            visibility=visibility,
            target_roles=target_roles,
            target_divisions=target_divisions,
            created_by=request.user,
        )

        messages.success(request, "✅ Actividad creada.")
        return redirect("manage_events")

    return render(request, "accounts/events/event_form.html", {"mode": "create"})


@login_required
def edit_event(request, event_id):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    ev = get_object_or_404(Event, id=event_id)

    if request.method == "POST":
        ev.title = (request.POST.get("title") or "").strip()
        ev.description = (request.POST.get("description") or "").strip()
        ev.location = (request.POST.get("location") or "").strip()
        ev.price = (request.POST.get("price") or "").strip()
        visibility = (request.POST.get("visibility") or Event.VIS_PUBLIC).strip()
        if visibility not in {Event.VIS_PUBLIC, Event.VIS_CUSTOM}:
            visibility = Event.VIS_PUBLI

        target_roles = request.POST.getlist("target_roles")
        target_divisions = request.POST.getlist("target_divisions")

        allowed_roles = {User.ROLE_RESP_SECTOR, User.ROLE_RESP_ZONA, User.ROLE_RESP_GRUPO, User.ROLE_MIEMBRO}
        allowed_divs = {User.DIV_DJM, User.DIV_DJF, User.DIV_CABALLEROS, User.DIV_DAMAS}

        target_roles = [r for r in target_roles if r in allowed_roles]
        target_divisions = [d for d in target_divisions if d in allowed_divs]


        ev.visibility = visibility
        if visibility == Event.VIS_PUBLIC:
            ev.is_public = True
            ev.target_roles = []
            ev.target_divisions = []
        else:
            ev.is_public = False
            ev.target_roles = target_roles
            ev.target_divisions = target_divisions

        date_str = (request.POST.get("date") or "").strip()
        time_str = (request.POST.get("time") or "").strip()

        if not ev.title or not date_str:
            messages.error(request, "Título y fecha son obligatorios.")
            return redirect("edit_event", event_id=ev.id)

        try:
            ev.date = timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            messages.error(request, "Fecha inválida.")
            return redirect("edit_event", event_id=ev.id)

        if time_str:
            try:
                ev.time = timezone.datetime.strptime(time_str, "%H:%M").time()
            except Exception:
                messages.error(request, "Hora inválida (usa HH:MM).")
                return redirect("edit_event", event_id=ev.id)
        else:
            ev.time = None

        ev.save()
        messages.success(request, "✅ Actividad actualizada.")
        return redirect("manage_events")

    return render(request, "accounts/events/event_form.html", {"mode": "edit", "event": ev})


@login_required
def delete_event(request, event_id):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    ev = get_object_or_404(Event, id=event_id)

    if request.method == "POST":
        ev.delete()
        messages.success(request, "🗑️ Actividad eliminada.")
        return redirect("manage_events")

    return render(request, "accounts/events/event_delete.html", {"event": ev})

@login_required
def fortuna_home(request):
    issue = FortunaIssue.objects.filter(is_active=True).order_by("-code").first()

    
    context = {
        "issue": issue,
        "is_admin_directiva": _is_admin_or_directiva(request.user),
        "cover_static": "img/fortuna/cover_nov_2025.png",
        "can_view_active_members": request.user.can_view_active_members(),
    }
    return render(request, "accounts/fortuna/fortuna_home.html", context)

    


@login_required
def fortuna_material(request):
    issue = FortunaIssue.objects.filter(is_active=True).first()
    if not issue:
        return render(request, "accounts/fortuna/fortuna_material_unavailable.html")

    # 1) Validar que haya material "convertido a imágenes"
    pages_qs = FortunaIssuePage.objects.filter(issue=issue).order_by("page_number")
    if not pages_qs.exists():
        return render(
            request,
            "accounts/fortuna/fortuna_material_unavailable.html",
            {
                "issue": issue,
                "message": "No hay páginas generadas para esta edición. (Falta convertir el PDF a imágenes)",
            },
        )

    # 2) Validar acceso por plan (access_start/access_end) o buyer manual
    profile, _ = Profile.objects.get_or_create(user=request.user)
    is_buyer = profile.is_buyer

    today = timezone.now().date()

    has_active_access = FortunaPurchase.objects.filter(
        user=request.user,
        status=FortunaPurchase.STATUS_APPROVED,
        access_start__lte=today,
        access_end__gte=today,
    ).exists()

    if not (is_buyer or has_active_access):
        return render(
            request,
            "accounts/fortuna/fortuna_acces_denied.html",
            {"issue": issue},
            status=403,
        )

    # 3) Viewer por páginas (GET ?p=1)
    try:
        page = int(request.GET.get("p", "1"))
    except Exception:
        page = 1

    total = pages_qs.count()
    if total <= 0:
        return render(
            request,
            "accounts/fortuna/fortuna_material_unavailable.html",
            {"issue": issue, "message": "No hay páginas generadas."},
        )

    page = max(1, min(page, total))
    current = pages_qs[page - 1]

    # ✅ CLAVE: rango 1..total para que el HTML pueda listar todos los botones
    page_range = range(1, total + 1)

    return render(
        request,
        "accounts/fortuna/fortuna_material_images.html",
        {
            "issue": issue,
            "current": current,
            "page": page,
            "total": total,
            "prev_page": page - 1 if page > 1 else None,
            "next_page": page + 1 if page < total else None,
            "page_range": page_range,  # ✅ nuevo
        },
    )

@login_required
def fortuna_ediciones(request):
    context = {
        "is_admin_directiva": _is_admin_or_directiva(request.user),
    }
    return render(request, "accounts/fortuna/fortuna_ediciones.html", context)


@login_required
def fortuna_comprar(request):
    issue = _get_fortuna_current_issue()

    other_mode = (request.GET.get("for") == "other") or (request.POST.get("__other_mode") == "1")
    can_report_for_others = _can_report_for_others(request.user)
    scope_users = _get_users_in_scope(request.user) if can_report_for_others else User.objects.none()

    # ---------------------------------
    # 1) Resolver target_user
    # ---------------------------------
    target_user = request.user  # por defecto

    if other_mode and can_report_for_others:
        target_id_raw = (request.POST.get("report_for_user_id") or request.GET.get("report_for_user_id") or "").strip()
        scope_ids = set(scope_users.values_list("id", flat=True))

        if target_id_raw:
            try:
                tid = int(target_id_raw)
                # permitir elegirse a sí mismo o alguien dentro del alcance
                if tid == request.user.id or tid in scope_ids:
                    tu = User.objects.filter(id=tid).first()
                    if tu:
                        target_user = tu
            except Exception:
                pass

    # ---------------------------------
    # Context base (SIEMPRE)
    # ---------------------------------
    context = {
        "issue": issue,
        "is_admin_directiva": _is_admin_or_directiva(request.user),
        "success": False,
        "errors": {},
        "date_value": "",
        "note_value": "",
        "selected_plan": "",
        "purchase": None,
        "already_approved": False,
        "previous_status": "",
        "reject_reason": "",
        "can_report_for_others": can_report_for_others,
        "scope_users": scope_users,
        "other_mode": other_mode,
        "target_user": target_user,
    }

    if not issue:
        return render(request, "accounts/fortuna/fortuna_comprar.html", context)

    # ---------------------------------
    # 2) Existing purchase (de este issue y target_user)
    # ---------------------------------
    existing = FortunaPurchase.objects.filter(issue=issue, user=target_user).first()
    context["purchase"] = existing

    if existing:
        context["previous_status"] = existing.status
        context["reject_reason"] = existing.reject_reason or ""

        if existing.status == FortunaPurchase.STATUS_APPROVED:
            # ✅ NO hacemos return: permitimos renovar
            context["already_approved"] = True
            context["selected_plan"] = existing.plan or ""

    # ---------------------------------
    # 3) POST: crear/actualizar solicitud
    # ---------------------------------
    if request.method == "POST":
        plan = (request.POST.get("plan") or "").strip()  # trim | sem | anual
        date_raw = (request.POST.get("deposit_date") or "").strip()
        note = (request.POST.get("note") or "").strip()
        receipt = request.FILES.get("receipt")

        errors = {}

        # ✅ validar plan según tu MODELO
        valid_plans = {FortunaPurchase.PLAN_TRIMESTRAL, FortunaPurchase.PLAN_SEMESTRAL, FortunaPurchase.PLAN_ANUAL}
        if plan not in valid_plans:
            errors["plan"] = "Selecciona un plan válido."

        # validar fecha
        deposit_date = None
        try:
            if not date_raw:
                raise ValueError()
            deposit_date = timezone.datetime.strptime(date_raw, "%Y-%m-%d").date()
        except Exception:
            errors["deposit_date"] = "Ingresa una fecha de depósito válida."

        # validar comprobante
        if not receipt:
            errors["receipt"] = "Debes adjuntar el comprobante de depósito."

        # repoblar form si hay error
        context["date_value"] = date_raw
        context["note_value"] = note
        context["selected_plan"] = plan

        if errors:
            context["errors"] = errors
            return render(request, "accounts/fortuna/fortuna_comprar.html", context)

        # calcular periodo
        access_start, access_end = _calculate_fortuna_period(plan, deposit_date)

        # ✅ crear o actualizar la solicitud (pending) para ESTE issue + target_user
        obj, created = FortunaPurchase.objects.get_or_create(
            issue=issue,
            user=target_user,
            defaults={
                "status": FortunaPurchase.STATUS_PENDING,
                "plan": plan,
                "access_start": access_start,
                "access_end": access_end,
                "deposit_date": deposit_date,
                "receipt": receipt,
                "note": note,
                "reject_reason": "",
                "reported_by": (request.user if target_user.id != request.user.id else None),
            },
        )

        # Si ya existía, actualizamos campos igual (renovación / reenvío)
        if not created:
            obj.status = FortunaPurchase.STATUS_PENDING
            obj.plan = plan
            obj.access_start = access_start
            obj.access_end = access_end
            obj.deposit_date = deposit_date
            obj.receipt = receipt
            obj.note = note
            obj.reject_reason = ""
            obj.reported_by = (request.user if target_user.id != request.user.id else None)
            obj.save()


        context["success"] = True
        context["purchase"] = obj
        context["date_value"] = ""
        context["note_value"] = ""
        context["selected_plan"] = obj.plan

        return render(request, "accounts/fortuna/fortuna_comprar.html", context)

    # ---------------------------------
    # 4) GET normal
    # ---------------------------------
    return render(request, "accounts/fortuna/fortuna_comprar.html", context)



def _calculate_fortuna_period(plan: str, deposit_date: date):
    """
    Regla: pago habilita meses FUTUROS.
    Ej: paga el 20/ene => access_start 01/feb.
    Plan:
      - trim: +3 meses
      - sem: +6 meses
      - anual: +12 meses
    access_end = último día del último mes incluido.
    """
    # inicio = primer día del mes siguiente
    start = (deposit_date.replace(day=1) + relativedelta(months=1))

    months_map = {
        "trim": 3,
        "sem": 6,
        "anual": 12,
    }
    months = months_map[plan]

    # fin = último día del último mes incluido
    end_exclusive = start + relativedelta(months=months)   # primer día del mes siguiente al periodo
    end = end_exclusive - relativedelta(days=1)            # último día del periodo

    return start, end



@login_required
def fortuna_compradores(request):
    # admin/directiva y responsables pueden ver, pero responsables con alcance
    if not request.user.can_view_active_members():
        raise PermissionDenied("No tienes permisos para ver compradores.")

    issue = _get_fortuna_current_issue()
    if not issue:
        return render(request, "accounts/fortuna/fortuna_compradores.html", {
            "issue": None,
            "buyers": [],
            "sectors": Sector.objects.all().order_by("name"),
            "zonas": Zona.objects.none(),
            "grupos": Grupo.objects.none(),
            "q": "",
            "selected_sector_id": "",
            "selected_zona_id": "",
            "selected_group_id": "",
        })

    approved_user_ids = FortunaPurchase.objects.filter(
        issue=issue,
        status=FortunaPurchase.STATUS_APPROVED
    ).values_list("user_id", flat=True)

    manual_buyer_ids = Profile.objects.filter(is_buyer=True).values_list("user_id", flat=True)

    buyers_ids = set(approved_user_ids) | set(manual_buyer_ids)

    base_qs = User.objects.select_related("group__zona__sector").filter(id__in=buyers_ids).order_by(
        "first_name", "last_name", "username"
    )

    # ✅ aplicar alcance para responsables (admin/directiva => None)
    scope = _user_scope_filters(request.user)
    if scope is not None:
        base_qs = base_qs.filter(**scope)

    # --- filtros tipo members_list ---
    q = (request.GET.get("q") or "").strip()
    sector_id = (request.GET.get("sector_id") or "").strip()
    zona_id = (request.GET.get("zona_id") or "").strip()
    group_id = (request.GET.get("group_id") or "").strip()

    qs = base_qs
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(username__icontains=q) |
            Q(email__icontains=q)
        )

    if sector_id:
        qs = qs.filter(group__zona__sector_id=sector_id)

    if zona_id:
        qs = qs.filter(group__zona_id=zona_id)

    if group_id:
        qs = qs.filter(group_id=group_id)

    sectors = Sector.objects.all().order_by("name")
    zonas = Zona.objects.none()
    grupos = Grupo.objects.none()

    if sector_id:
        zonas = Zona.objects.filter(sector_id=sector_id).order_by("name")
    if zona_id:
        grupos = Grupo.objects.filter(zona_id=zona_id).order_by("name")

    return render(request, "accounts/fortuna/fortuna_compradores.html", {
        "issue": issue,
        "buyers": qs,
        "q": q,
        "sectors": sectors,
        "zonas": zonas,
        "grupos": grupos,
        "selected_sector_id": sector_id,
        "selected_zona_id": zona_id,
        "selected_group_id": group_id,
    })


@login_required
def fortuna_compradores_export(request):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos para exportar compradores.")

    issue = _get_fortuna_current_issue()
    if not issue:
        return HttpResponse("No hay ediciones Fortuna.", status=400)

    approved_user_ids = FortunaPurchase.objects.filter(
        issue=issue,
        status=FortunaPurchase.STATUS_APPROVED
    ).values_list("user_id", flat=True)

    manual_buyer_ids = Profile.objects.filter(is_buyer=True).values_list("user_id", flat=True)
    buyers_ids = set(approved_user_ids) | set(manual_buyer_ids)

    qs = User.objects.select_related("group__zona__sector").filter(id__in=buyers_ids).order_by(
        "first_name", "last_name", "username"
    )

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="fortuna_compradores_{issue.code}.csv"'
    response.write("\ufeff")

    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Nombre", "Apellido", "Username", "Email", "Rol", "Sector", "Zona", "Grupo"])

    for u in qs:
        sec = u.get_sector().name if u.get_sector() else ""
        zona = u.group.zona.name if (u.group and u.group.zona) else ""
        grupo = u.group.name if u.group else ""
        writer.writerow([
            u.first_name or "",
            u.last_name or "",
            u.username or "",
            u.email or "",
            u.get_role_display() if hasattr(u, "get_role_display") else (u.role or ""),
            sec, zona, grupo
        ])

    return response



@login_required
@xframe_options_sameorigin
def fortuna_pdf(request, issue_id: int):
    issue = get_object_or_404(FortunaIssue, id=issue_id)

    # Debe existir PDF
    if not issue.material_pdf:
        raise Http404("No hay PDF para esta edición.")

    # ✅ misma lógica de acceso que fortuna_material
    profile, _ = Profile.objects.get_or_create(user=request.user)
    is_buyer = profile.is_buyer

    has_approved_purchase = FortunaPurchase.objects.filter(
        issue=issue,
        user=request.user,
        status=FortunaPurchase.STATUS_APPROVED,
    ).exists()

    if not (is_buyer or has_approved_purchase):
        raise PermissionDenied("No tienes acceso a este material.")

    # Entregar archivo como stream
    return FileResponse(issue.material_pdf.open("rb"), content_type="application/pdf")

def _get_fortuna_current_issue():
    # 1) activa
    issue = FortunaIssue.objects.filter(is_active=True).first()
    if issue:
        return issue
    # 2) fallback: la más nueva (por code)
    return FortunaIssue.objects.order_by("-code").first()

@login_required
def help_view(request):
    return render(request, "help.html")


@login_required
def fortuna_admin_purchases(request):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    issue = _get_fortuna_current_issue()

    if request.method == "POST":
        purchase_id = request.POST.get("purchase_id")
        action = request.POST.get("action")
        reason = (request.POST.get("reason") or "").strip()

        p = get_object_or_404(FortunaPurchase, id=purchase_id)

        if action == "approve":
            p.status = FortunaPurchase.STATUS_APPROVED
            p.reject_reason = ""
            p.save()

            # ✅ NOTIFICACIÓN INTERNA
            Notification.objects.create(
                user=p.user,
                title="Compra Fortuna aprobada",
                message=(
                    f"Tu solicitud de compra para la edición {p.issue.code} fue aprobada. "
                    "Ya tienes acceso al material."
                ),
            )

            # ✅ EMAIL (opcional, igual que Kofu)
            try:
                if p.user.email:
                    subject = "Compra Fortuna aprobada"
                    body = (
                        f"Hola {p.user.first_name or p.user.username},\n\n"
                        f"Tu solicitud de compra para la edición {p.issue.code} fue aprobada.\n"
                        "Ya tienes acceso al material.\n\n"
                        "SGI Chile"
                    )
                    send_mail(
                        subject,
                        body,
                        getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@sgi-chile.cl"),
                        [p.user.email],
                        fail_silently=True,
                    )
            except Exception:
                pass

            messages.success(
                request,
                f"✅ Compra aprobada para {p.user.first_name} {p.user.last_name}."
            )

        elif action == "reject":
            p.status = FortunaPurchase.STATUS_REJECTED
            p.reject_reason = reason
            p.save()

            # ✅ NOTIFICACIÓN INTERNA
            msg = f"Tu solicitud de compra para la edición {p.issue.code} fue rechazada."
            if reason:
                msg += f" Motivo: {reason}"

            Notification.objects.create(
                user=p.user,
                title="Compra Fortuna rechazada",
                message=msg,
            )

            # ✅ EMAIL (opcional)
            try:
                if p.user.email:
                    subject = "Compra Fortuna rechazada"
                    body = (
                        f"Hola {p.user.first_name or p.user.username},\n\n"
                        f"Tu solicitud de compra para la edición {p.issue.code} fue rechazada.\n"
                    )
                    if reason:
                        body += f"\nMotivo: {reason}\n"
                    body += "\nSi crees que es un error, puedes volver a enviar tu solicitud.\n\nSGI Chile"

                    send_mail(
                        subject,
                        body,
                        getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@sgi-chile.cl"),
                        [p.user.email],
                        fail_silently=True,
                    )
            except Exception:
                pass

            messages.success(
                request,
                f"❌ Compra rechazada para {p.user.first_name} {p.user.last_name}."
            )

        else:
            messages.error(request, "Acción inválida.")

        # (Opcional) para evitar re-POST al refrescar
        return redirect("fortuna_admin_purchases")

    pending = (
        FortunaPurchase.objects.select_related("user", "issue")
        .filter(issue=issue, status=FortunaPurchase.STATUS_PENDING)
        .order_by("-created_at")
        if issue else []
    )

    processed = (
        FortunaPurchase.objects.select_related("user", "issue")
        .filter(issue=issue)
        .exclude(status=FortunaPurchase.STATUS_PENDING)
        .order_by("-created_at")[:20]
        if issue else []
    )

    return render(request, "accounts/fortuna/fortuna_admin_purchases.html", {
        "issue": issue,
        "pending": pending,
        "processed": processed,
    })


@login_required
def fortuna_admin_requests(request):
    if not _is_admin_or_directiva(request.user):
        raise PermissionDenied("No tienes permisos.")

    issue = _get_fortuna_current_issue()
    if not issue:
        return render(request, "accounts/fortuna/fortuna_admin_requests.html", {"issue": None, "requests": []})

    qs = (
        FortunaPurchase.objects
        .select_related("user", "issue")
        .filter(issue=issue)
        .order_by("-created_at")
    )

    if request.method == "POST":
        purchase_id = request.POST.get("purchase_id")
        action = request.POST.get("action")
        reason = (request.POST.get("reason") or "").strip()

        purchase = get_object_or_404(FortunaPurchase, id=purchase_id)

        if action == "approve":
            purchase.status = FortunaPurchase.STATUS_APPROVED
            purchase.reject_reason = ""
            purchase.save()
            messages.success(request, "✅ Solicitud aprobada.")
        elif action == "reject":
            purchase.status = FortunaPurchase.STATUS_REJECTED
            purchase.reject_reason = reason
            purchase.save()
            messages.success(request, "❌ Solicitud rechazada.")
        else:
            messages.error(request, "Acción inválida.")

        return redirect("fortuna_admin_requests")

    return render(request, "accounts/fortuna/fortuna_admin_requests.html", {
        "issue": issue,
        "requests": qs,
    })


DIVS = {"djm": "DJM", "djf": "DJF", "caballeros": "Caballeros", "damas": "Damas"}

@login_required
def division_home(request, division):
    division = (division or "").lower()
    if division not in DIVS:
        raise Http404("División no válida")

    user = request.user
    if user.is_authenticated:
        if not user.can_view_division(division):
            return HttpResponseForbidden("No tienes permiso para ver esta división.")
    else:
        return HttpResponseForbidden("Debes iniciar sesión.")

    today = timezone.localdate()

    qs = DivisionPost.objects.filter(division=division, is_published=True)

    # Destacado: lo más prioritario marcado como destacado
    featured = (
        qs.filter(is_featured=True)
          .order_by("-priority", "-created_at")
          .first()
    )

    # Próximas actividades: kind=activity y fecha >= hoy
    upcoming = (
        qs.filter(kind=DivisionPost.KIND_ACTIVITY, event_date__gte=today)
          .order_by("event_date", "-priority")[:6]
    )

    # Pasadas: kind=activity y fecha < hoy
    past = (
        qs.filter(kind=DivisionPost.KIND_ACTIVITY, event_date__lt=today)
          .order_by("-event_date", "-priority")[:10]
    )

    # Noticias/Avisos: kind=news (da igual si tiene fecha o no, pero lo normal es sin fecha)
    news = (
        qs.filter(kind=DivisionPost.KIND_NEWS)
          .order_by("-priority", "-created_at")[:10]
    )

    # Título bonito
    if division == "djm":
        division_title = "División Juvenil Masculina (DJM)"
    elif division == "djf":
        division_title = "División Juvenil Femenina (DJF)"
    elif division == "caballeros":
        division_title = "División Caballeros"
    else:
        division_title = "División Damas"

    can_manage_posts = user.can_manage_division_posts(division)

    context = {
        "division_key": division,
        "division_title": division_title,
        "featured": featured,
        "upcoming": upcoming,
        "past": past,
        "news": news,
        "can_manage_posts": can_manage_posts,
    }
    return render(request, "divisions/division_home.html", context)


@login_required
def divisions_index(request):
    u = request.user

    # Admin/Directiva: puede ver cualquiera (por ahora lo mandas a DJM)
    if u.is_admin_like():
        return redirect("division_home", division="djm")


    eff = u.effective_division_for_menu()
    if not eff or eff not in DIVS:
        return HttpResponseForbidden("No tienes división asignada.")

    return redirect("division_home", division=eff)



@login_required
def my_division_redirect(request):
    u = request.user

    eff = u.effective_division_for_menu()
    if not eff or eff not in DIVS:
        messages.error(request, "No tienes división asignada todavía.")
        return redirect("home")

    return redirect("division_home", division=eff)

def register_member(request):
    """
    Registro público:
    - username = rut
    - crea usuario INACTIVO
    - envía correo de activación para crear contraseña
    - si el correo falla, IGUAL deja el usuario creado (y se puede reenviar desde admin)
    """

    if request.method == "POST":
        form = SelfRegisterForm(request.POST)

        if form.is_valid():
            user = form.save()  # ✅ se crea sí o sí

            try:
                _send_activation_email(request, user)

                messages.success(
                    request,
                    "✅ Registro recibido. Revisa tu correo para activar tu cuenta y crear tu contraseña."
                )
                return redirect("login")

            except Exception:
                logger.exception("Fallo enviando correo de activación a %s", user.email)

                messages.warning(
                    request,
                    "✅ Cuenta creada, pero NO se pudo enviar el correo de activación. "
                    "Contacta al administrador para reenviar la activación."
                )
                return redirect("login")

    else:
        form = SelfRegisterForm()

    return render(request, "registration/register.html", {
        "form": form,
        "sectors": Sector.objects.all().order_by("name"),
    })


def _news_for_user_q(u):
    """
    Devuelve un Q() con lo que el usuario puede ver:
    - global siempre
    - sector si coincide
    - zona si coincide
    - grupo si coincide
    Si no está autenticado: solo global
    """
    if not u.is_authenticated:
        return Q(target=NewsPost.TARGET_GLOBAL)

    sector_id = u.get_sector().id if u.get_sector() else None
    zona_id = u.group.zona_id if u.group and u.group.zona_id else None
    group_id = u.group_id

    return (
        Q(target=NewsPost.TARGET_GLOBAL) |
        Q(target=NewsPost.TARGET_SECTOR, sector_id=sector_id) |
        Q(target=NewsPost.TARGET_ZONA, zona_id=zona_id) |
        Q(target=NewsPost.TARGET_GRUPO, grupo_id=group_id)
    )


@login_required
def news_list(request):
    now = timezone.now()
    u = request.user

    scope = (request.GET.get("scope") or "").strip()  # "general" | "chile" | ""
    q = (request.GET.get("q") or "").strip()

    qs = (
        NewsPost.objects
        .filter(is_published=True, published_at__lte=now)
        .filter(_news_for_user_q(u))
    )

    if scope in (NewsPost.SCOPE_GENERAL, NewsPost.SCOPE_CHILE):
        qs = qs.filter(scope=scope)

    if q:
        qs = qs.filter(
            Q(title__icontains=q) |
            Q(summary__icontains=q) |
            Q(body__icontains=q)
        )

    paginator = Paginator(qs, 9)  # 9 cards por página
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(request, "news/news_list.html", {
        "page_obj": page_obj,
        "scope": scope,
        "q": q,
    })


def news_detail(request, pk):
    now = timezone.now()
    u = request.user

    qs = (
        NewsPost.objects
        .filter(is_published=True)
        .filter(Q(published_at__isnull=True) | Q(published_at__lte=now))
        .filter(_news_for_user_q(u))   # IMPORTANTE: respeta permisos también en detail
    )

    post = get_object_or_404(qs, pk=pk)

    return render(request, "news/news_detail.html", {"post": post})

@login_required
def members_division_national_list(request):
    u = request.user

    # Permiso: RN/Vice RN (o admin)
    if not (u.is_admin_like() or u.is_national_division_role()):
        raise PermissionDenied("No tienes permisos para ver miembros por división nacional.")

    # Determinar división objetivo
    division_key = (u.national_division or "").lower() if u.is_national_division_role() else ""
    if u.is_admin_like() and not division_key:
        # Admin: puede ver todos o filtrar por querystring "division"
        division_key = (request.GET.get("division") or "").strip().lower()

    base_qs = User.objects.select_related("group__zona__sector").all().order_by(
        "first_name", "last_name", "username"
    )

    # Si no es admin, forzar división nacional
    if not u.is_admin_like():
        if not division_key:
            return render(request, "accounts/members_list.html", {
                "members": User.objects.none(),
                "q": "",
                "selected_role": "",
                "selected_division": "",
                "selected_sector_id": "",
                "selected_zona_id": "",
                "selected_group_id": "",
                "sectors": Sector.objects.none(),
                "zonas": Zona.objects.none(),
                "grupos": Grupo.objects.none(),
                "role_choices": User.ROLE_CHOICES,
                "can_filter_sector": False,
                "can_filter_zona": False,
                "can_filter_grupo": False,
                "querystring": request.GET.urlencode(),
                "division_national_mode": True,
                "division_national_label": "Sin división nacional asignada",
            })

        base_qs = base_qs.filter(division=division_key)

    # --- filtros ---
    q = (request.GET.get("q") or "").strip()
    role = (request.GET.get("role") or "").strip()
    division = (request.GET.get("division") or "").strip()  # solo admin debería usarlo aquí
    sector_id = (request.GET.get("sector_id") or "").strip()
    zona_id = (request.GET.get("zona_id") or "").strip()
    group_id = (request.GET.get("group_id") or "").strip()

    qs = base_qs

    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(username__icontains=q) |
            Q(email__icontains=q)
        )

    if role:
        qs = qs.filter(role=role)

    # En modo nacional: división ya está “fijada” (salvo admin)
    if u.is_admin_like() and division:
        qs = qs.filter(division=division)

    # ✅ en modo nacional sí quieres filtrar por sector/zona/grupo dentro de la división
    if sector_id:
        qs = qs.filter(group__zona__sector_id=sector_id)
    if zona_id:
        qs = qs.filter(group__zona_id=zona_id)
    if group_id:
        qs = qs.filter(group_id=group_id)

    # --- dropdown data ---
    sectors = Sector.objects.all().order_by("name")
    zonas = Zona.objects.none()
    grupos = Grupo.objects.none()

    if sector_id:
        zonas = Zona.objects.filter(sector_id=sector_id).order_by("name")
    if zona_id:
        grupos = Grupo.objects.filter(zona_id=zona_id).order_by("name")

    context = {
        "members": qs,
        "q": q,
        "selected_role": role,

        # si no es admin, dejamos selected_division fijo
        "selected_division": division if u.is_admin_like() else division_key,

        "selected_sector_id": sector_id,
        "selected_zona_id": zona_id,
        "selected_group_id": group_id,

        "sectors": sectors,
        "zonas": zonas,
        "grupos": grupos,

        "role_choices": User.ROLE_CHOICES,

        # En nacional: todos estos filtros tienen sentido
        "can_filter_sector": True,
        "can_filter_zona": True,
        "can_filter_grupo": True,

        "querystring": request.GET.urlencode(),

        # flags para el template
        "division_national_mode": True,
        "division_national_label": dict(User.DIVISION_CHOICES).get(division_key, division_key),
    }
    return render(request, "accounts/members_list.html", context)


@login_required
def members_division_national_export(request):
    u = request.user

    if not (u.is_admin_like() or u.is_national_division_role()):
        raise PermissionDenied("No tienes permisos para exportar miembros por división nacional.")

    division_key = (u.national_division or "").lower() if u.is_national_division_role() else ""
    if u.is_admin_like() and not division_key:
        division_key = (request.GET.get("division") or "").strip().lower()

    base_qs = User.objects.select_related("group__zona__sector").all().order_by(
        "first_name", "last_name", "username"
    )

    if not u.is_admin_like():
        base_qs = base_qs.filter(division=division_key)
    else:
        if division_key:
            base_qs = base_qs.filter(division=division_key)

    # Reusar filtros simples del list:
    q = (request.GET.get("q") or "").strip()
    role = (request.GET.get("role") or "").strip()
    sector_id = (request.GET.get("sector_id") or "").strip()
    zona_id = (request.GET.get("zona_id") or "").strip()
    group_id = (request.GET.get("group_id") or "").strip()

    qs = base_qs
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(username__icontains=q) |
            Q(email__icontains=q)
        )
    if role:
        qs = qs.filter(role=role)
    if sector_id:
        qs = qs.filter(group__zona__sector_id=sector_id)
    if zona_id:
        qs = qs.filter(group__zona_id=zona_id)
    if group_id:
        qs = qs.filter(group_id=group_id)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="miembros_division_nacional.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")

    writer.writerow([
        "Nombre","Apellido","Username","RUT","Email","Rol","Activo",
        "Fecha nacimiento","Edad","División","Sector","Zona","Grupo","Dirección","Fecha ingreso","Único miembro familia"
    ])

    division_map = dict(User.DIVISION_CHOICES)

    for m in qs:
        sector_name = ""
        zona_name = ""
        grupo_name = ""
        if m.group:
            grupo_name = m.group.name or ""
            if getattr(m.group, "zona", None):
                zona_name = m.group.zona.name or ""
                if getattr(m.group.zona, "sector", None):
                    sector_name = m.group.zona.sector.name or ""

        birth = getattr(m, "birth_date", None)
        age = getattr(m, "age", None)

        writer.writerow([
            m.first_name or "",
            m.last_name or "",
            m.username or "",
            getattr(m, "rut", "") or "",
            m.email or "",
            m.get_role_display(),
            "SI" if m.is_active else "NO",
            birth.strftime("%Y-%m-%d") if birth else "",
            age if age is not None else "",
            division_map.get((m.division or "").lower(), m.division or ""),
            sector_name,
            zona_name,
            grupo_name,
            getattr(m, "address", "") or "",
            getattr(m, "join_date", None).strftime("%Y-%m-%d") if getattr(m, "join_date", None) else "",
            "SI" if getattr(m, "is_only_family_member", False) else "NO",
        ])

    return response
