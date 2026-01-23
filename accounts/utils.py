import re
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


RUT_RE = re.compile(r"^\d{7,8}-[\dkK]$")

def normalize_rut(value: str) -> str:
    if not value:
        return value
    v = value.strip().replace(".", "").replace(" ", "")
    if "-" not in v and len(v) in (8, 9):
        v = v[:-1] + "-" + v[-1]
    return v.upper()

def is_valid_rut_format(value: str) -> bool:
    return bool(value and RUT_RE.match(value))


def send_activation_email(user, request):
    """
    ÚNICA fuente de verdad para enviar activación (Register + Admin).
    Devuelve True/False según si se envió.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    activation_link = request.build_absolute_uri(
        reverse("activate", kwargs={"uidb64": uid, "token": token})
    )

    subject = "Activa tu cuenta - SGI Chile"
    message = render_to_string("accounts/activation_email.txt", {
        "user": user,
        "activation_link": activation_link,
    })

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)

    # send_mail devuelve número de correos enviados
    sent = send_mail(
        subject=subject,
        message=message,
        from_email=from_email,
        recipient_list=[user.email],
        fail_silently=False,
    )
    return sent == 1
