import csv, json
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from django.utils.encoding import force_str
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.forms import SetPasswordForm
from .models import User, Event, ContributionReport, Contribution, Notification, HouseholdMember
from decimal import Decimal, InvalidOperation
from django.db.models import Sum, Count
from django.http import HttpResponseForbidden, HttpResponse
from django.core.mail import send_mail
from django.conf import settings
from .forms import MemberCreateForm, MemberEditForm
from django.core.exceptions import PermissionDenied
from django.contrib import messages



def home(request):
    # carousel: imágenes estáticas (si los eventos tienen image, las usamos)
    # coger próximos 5 eventos públicos
    now = timezone.now().date()
    upcoming = Event.objects.filter(is_public=True, date__gte=now).order_by('date')[:6]

    # Si no hay imágenes en Events, puedes configurar imágenes por defecto en static/img/
    carousel_images = []
    for ev in upcoming:
        if ev.image:
            carousel_images.append(ev.image)
    # si no hay imágenes, usa una lista fija
    if not carousel_images:
        carousel_images = ['carousel1.jpg','carousel2.jpg','carousel3.jpg']  # guarda estas en static/img/

    context = {
        'carousel_images': carousel_images,
        'upcoming': upcoming,
    }
    return render(request, 'home.html', context)


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
        membership = request.user.household_membership  # HouseholdMember
        qs = HouseholdMember.objects.filter(
            household=membership.household
        ).select_related("user")
        qs = sorted(qs, key=lambda m: (not m.is_primary, m.user.id))
        family_members = [m.user for m in qs]
    except HouseholdMember.DoesNotExist:
        pass

    context["family_members"] = family_members

    if request.method == "POST":
        amount_raw = request.POST.get("amount", "").strip()
        date_raw = request.POST.get("deposit_date", "").strip()
        note = request.POST.get("note", "").strip()
        receipt = request.FILES.get("receipt")

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

        # Mapa para asegurar que solo se puedan usar IDs del hogar (seguridad)
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
        if amount is not None and splits:
            if family_sum != amount:
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
    Solo visible para responsables, directiva, admin, etc.
    Activo = total contribuciones confirmadas >= 12.000 CLP.
    """
    if not request.user.can_view_active_members():
        return HttpResponseForbidden("No tienes permiso para ver esta sección.")

    THRESHOLD = Decimal("12000.00")

    active = (
        Contribution.objects
        .filter(is_confirmed=True)
        .values(
            "member__id",
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

    return render(request, "accounts/create_member.html", {"form": form})

from .models import User, Household, HouseholdMember
from django.shortcuts import get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

def _is_admin_like(user):
    return user.is_authenticated and (user.is_superuser or user.is_staff or getattr(user, "is_admin_sistema", lambda: False)())

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
            if hasattr(u, "household_membership"):
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

    return render(request, "accounts/edit_member.html", {
        "member": member,
        "household": current_household,
        "memberships": household_memberships,
        "candidates": candidates,
        "q": q,
        "role_choices": User.ROLE_CHOICES,
        "rel_choices": HouseholdMember.REL_CHOICES,
    })
