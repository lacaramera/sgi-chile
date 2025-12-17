import csv, json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.forms import SetPasswordForm
from .models import User, Event, HomeBanner, ContributionReport, Contribution, Notification, Household, HouseholdMember, Sector, Zona, Grupo
from decimal import Decimal, InvalidOperation
from django.db.models import Sum, Count,  Q
from django.http import HttpResponseForbidden, HttpResponse, JsonResponse
from .models import Sector, Zona, Grupo
from django.core.mail import send_mail
from django.conf import settings
from .forms import MemberCreateForm, MemberEditForm
from django.core.exceptions import PermissionDenied
from django.contrib import messages
from django.db import models
from django.template.loader import get_template
from calendar import monthrange

def home(request):
    today = timezone.now().date()

    # 1) BANNERS: SIEMPRE definir antes del context
    banners = HomeBanner.objects.filter(is_active=True).order_by("order", "-created_at")

    # 2) EVENTOS: del mes actual (máx 20)
    start = today.replace(day=1)
    end = today.replace(day=monthrange(today.year, today.month)[1])

    month_qs = Event.objects.filter(date__range=[start, end]).order_by("date", "time")

    # Regla de visibilidad:
    # - responsables/directiva/admin: ven públicos + privados
    # - resto: solo públicos
    if request.user.is_authenticated and request.user.can_view_active_members():
        upcoming = month_qs[:20]
    else:
        upcoming = month_qs.filter(is_public=True)[:20]

    # 3) Fallback si no hay banners activos
    fallback_images = ["banner.jpg", "banner2.jpg", "banner3.jpg"]

    context = {
        "banners": banners,
        "fallback_images": fallback_images,
        "upcoming": upcoming,
    }
    return render(request, "home.html", context)

def activate_account(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user is not None and default_token_generator.check_token(user, token):
        # Si GET -> mostrar formulario para elegir contraseña
        if request.method == 'POST':
            form = SetPasswordForm(user, request.POST)
            if form.is_valid():
                form.save()  # guarda la nueva contraseña
                user.is_active = True
                user.save()
                login(request, user)  # loguea al usuario
                return redirect('dashboard')
        else:
            form = SetPasswordForm(user)
        return render(request, 'accounts/activate.html', {'form': form})
    else:
        return render(request, 'accounts/activation_invalid.html')

@login_required
def dashboard(request):
    user = request.user
    context = {'user': user}
    return render(request, 'dashboard.html', context)

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

    # ✅ Familia real desde Household (fallback: solo el usuario)
    family_members = [request.user]
    try:
        membership = request.user.household_membership  # HouseholdMember (OneToOne)
        qs = (
            HouseholdMember.objects
            .filter(household=membership.household)
            .select_related("user")
            .order_by("-is_primary", "user__first_name", "user__last_name", "user__id")
        )
        family_members = [m.user for m in qs]
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
        family_amounts  = request.POST.getlist("family_amount[]")

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
                amt = Decimal(monto_text.replace(".", "").replace(",", "."))
            except Exception:
                errors["family_distribution"] = f"Monto inválido: {monto_text}"
                break

            if amt < 0:
                errors["family_distribution"] = "No se permiten montos negativos."
                break

            if amt == 0:
                continue

            u = User.objects.filter(id=uid).first()
            if not u:
                errors["family_distribution"] = "Usuario familiar no existe."
                break

            splits.append({"user_id": u.id, "amount": float(amt)})
            lines.append(f"{u.first_name} {u.last_name} (@{u.username}): {amt}")
            family_sum += amt

        # ✅ si no distribuyó nada, dejamos todo al mismo usuario
        if amount is not None and not splits:
            u = request.user
            splits = [{"user_id": u.id, "amount": float(amount)}]
            lines = [f"{u.first_name} {u.last_name} (@{u.username}): {amount}"]
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
                user=request.user,
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
    """
    if not request.user.can_view_active_members():
        return HttpResponseForbidden("No tienes permiso para ver esta sección.")

    THRESHOLD = Decimal("12000.00")

    qs = (
        Contribution.objects
        .filter(is_confirmed=True)
        .values(
            "member__id",
            "member__first_name",
            "member__last_name",
            "member__role",  # ✅ rol (código)
            "member__group__zona__sector__name",
            "member__group__zona__name",
            "member__group__name",
        )
        .annotate(
            total_amount=Sum("amount"),
            contributions_count=Count("id"),
        )
        .filter(total_amount__gte=THRESHOLD)
        .order_by("-total_amount")
    )

    role_map = dict(User.ROLE_CHOICES)

    active = list(qs)  # ✅ recién aquí lo convertimos a lista
    for m in active:
        m["role_label"] = role_map.get(m["member__role"], m["member__role"])

    context = {
        "active_members": active,
        "threshold": THRESHOLD,
    }
    return render(request, "kofu_active_members.html", context)

@login_required
def kofu_admin_reports(request):
    """
    Pantalla para responsables/directiva/admin:
    - Ver informes de contribución pendientes
    - Aprobar o rechazar
    """
    # Solo roles con permiso especial (mismo criterio que miembros activos)
    if not request.user.can_view_active_members():
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
                    contrib = report.approve(request.user)
                    message = f"Informe #{report.id} aprobado y registrado como contribución."

                    # === EMAIL al miembro ===
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
                        # en desarrollo, si algo falla, simplemente seguimos
                        pass

                    # === NOTIFICACIÓN interna ===
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
    )[:20]  # últimos 20 para referencia

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
    Exporta la lista de miembros activos en Kofu en formato CSV (para Excel).
    """
    if not request.user.can_view_active_members():
        return HttpResponseForbidden("No tienes permiso para ver esta sección.")

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

def _is_admin_like(user):
    return user.is_authenticated and (user.is_superuser or user.is_staff or getattr(user, "is_admin_sistema", lambda: False)())

@login_required
def create_member(request):
    if not _is_admin_like(request.user):
        raise PermissionDenied("No tienes permisos para crear miembros.")

    if request.method == "POST":
        form = MemberCreateForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "✅ Miembro creado correctamente.")
            return redirect("home")
    else:
        form = MemberCreateForm(initial={"is_active": True})

    return render(request, "accounts/create_member.html", {
        "form": form,
        "sectors": Sector.objects.all().order_by("name"),  # ✅ para cascada
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
    if not _is_admin_like(request.user):
        raise PermissionDenied("No tienes permisos para ver miembros.")

    base_qs = User.objects.select_related("group__zona__sector").all().order_by(
        "first_name", "last_name", "username"
    )

    qs = _apply_members_filters(request, base_qs)

    # datos para filtros dependientes
    sector_id = (request.GET.get("sector_id") or "").strip()
    zona_id = (request.GET.get("zona_id") or "").strip()

    sectors = Sector.objects.all().order_by("name")
    zonas = Zona.objects.none()
    grupos = Grupo.objects.none()

    if sector_id:
        zonas = Zona.objects.filter(sector_id=sector_id).order_by("name")

    if zona_id:
        grupos = Grupo.objects.filter(zona_id=zona_id).order_by("name")

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
        "querystring": request.GET.urlencode(),  # para el botón export
    }
    return render(request, "accounts/members_list.html", context)


@login_required
def members_export(request):
    if not _is_admin_like(request.user):
        raise PermissionDenied("No tienes permisos para exportar miembros.")

    base_qs = User.objects.select_related("group__zona__sector").all().order_by(
        "first_name", "last_name", "username"
    )
    qs = _apply_members_filters(request, base_qs)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="miembros.csv"'

    # BOM para Excel (evita caracteres raros en acentos)
    response.write("\ufeff")

    writer = csv.writer(response, delimiter=";")
    writer.writerow([
        "Nombre",
        "Apellido",
        "Username",
        "RUT",
        "Email",
        "Rol",
        "Activo",
        "Fecha nacimiento",
        "Edad",
        "Sector",
        "Zona",
        "Grupo",
        "Direccion",
        "Fecha ingreso",
        "Unico miembro familia",
    ])

    for u in qs:
        sector_name = ""
        zona_name = ""
        grupo_name = ""
        if u.group:
            grupo_name = u.group.name or ""
            if getattr(u.group, "zona", None):
                zona_name = u.group.zona.name or ""
                if getattr(u.group.zona, "sector", None):
                    sector_name = u.group.zona.sector.name or ""

        birth = getattr(u, "birth_date", None)  # si tu campo se llama distinto, cámbialo aquí
        age = getattr(u, "age", None)  # si tienes @property age, perfecto

        writer.writerow([
            u.first_name or "",
            u.last_name or "",
            u.username or "",
            getattr(u, "rut", "") or "",
            u.email or "",
            u.get_role_display() if hasattr(u, "get_role_display") else (u.role or ""),
            "SI" if u.is_active else "NO",
            birth.strftime("%Y-%m-%d") if birth else "",
            age if age is not None else "",
            sector_name,
            zona_name,
            grupo_name,
            getattr(u, "address", "") or "",
            getattr(u, "join_date", None).strftime("%Y-%m-%d") if getattr(u, "join_date", None) else "",
            "SI" if getattr(u, "is_only_family_member", False) else "NO",
        ])

    return response


@login_required
def edit_member(request, user_id):
    if not _is_admin_like(request.user):
        raise PermissionDenied("No tienes permisos para editar miembros.")

    member = get_object_or_404(User, id=user_id)

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

            # nuevos campos
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

            gid = (request.POST.get("group_id") or "").strip()
            member.group_id = int(gid) if gid else None

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
                u = User.objects.get(id=int(uid))
            except Exception:
                messages.error(request, "Usuario inválido.")
                return redirect("edit_member", user_id=member.id)

            # si ese usuario ya tiene hogar, lo impedimos (porque OneToOne)
            if HouseholdMember.objects.filter(user=u).exists():
                messages.error(request, "Ese usuario ya pertenece a otro hogar.")
                return redirect("edit_member", user_id=member.id)




            HouseholdMember.objects.create(
                household=current_household,
                user=u,
                relationship=rel,
                is_primary=False,
            )
            messages.success(request, f"✅ {u.first_name} {u.last_name} agregado al hogar.")
            return redirect("edit_member", user_id=member.id)

        # 4) Quitar usuario del hogar
        if action == "remove_from_household":
            mid = request.POST.get("membership_id")
            m = get_object_or_404(HouseholdMember, id=mid)

            # solo permitir quitar dentro del mismo hogar que estamos editando
            if not current_household or m.household_id != current_household.id:
                messages.error(request, "Acción no válida.")
                return redirect("edit_member", user_id=member.id)

            # no permitimos quitar al primary si quedan otros miembros
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

    return render(request, "accounts/edit_member.html", {
        "member": member,
        "household": current_household,
        "memberships": household_memberships,
        "candidates": candidates,
        "q": q,
        "role_choices": User.ROLE_CHOICES,
        "rel_choices": HouseholdMember.REL_CHOICES,
         "sectors": Sector.objects.all(),
        "member_sector_id": member_sector_id,
        "member_zona_id": member_zona_id,
         
    })
@login_required
def ajax_zonas_by_sector(request):
    sector_id = request.GET.get("sector_id")
    zonas = []

    if sector_id:
        zonas = Zona.objects.filter(sector_id=sector_id).order_by("name")

    data = [{"id": z.id, "name": z.name} for z in zonas]
    return JsonResponse({"zonas": data})

@login_required
def ajax_grupos_by_zona(request):
    zona_id = request.GET.get("zona_id")
    grupos = []

    if zona_id:
        grupos = Grupo.objects.filter(zona_id=zona_id).order_by("name")

    data = [{"id": g.id, "name": g.name} for g in grupos]
    return JsonResponse({"grupos": data})

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
    if not _is_admin_like(request.user):
        raise PermissionDenied("No tienes permisos para ver perfiles de miembros.")

    member = get_object_or_404(
        User.objects.select_related("group__zona__sector"),
        id=user_id
    )
    return render(request, "accounts/member_profile.html", {"member": member})

def _is_admin_or_directiva(user):
    return user.is_superuser or getattr(user, "role", None) in {user.ROLE_ADMIN, user.ROLE_DIRECTIVA}


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
        is_public = True if request.POST.get("is_public") == "on" else False

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
        ev.is_public = True if request.POST.get("is_public") == "on" else False

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
