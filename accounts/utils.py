# accounts/utils.py
import re
import requests

from django.conf import settings
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.template.loader import render_to_string

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


# ✅ FUNCIÓN ÚNICA PÚBLICA
def send_activation_email(user, request):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    activation_link = request.build_absolute_uri(
        reverse("activate", kwargs={"uidb64": uid, "token": token})
    )

    subject = "Activa tu cuenta - SGI Chile"
    message = render_to_string(
        "accounts/activation_email.txt",
        {
            "user": user,
            "activation_link": activation_link,
        },
    )

    _send_email(subject, message, user.email)


# 🔒 IMPLEMENTACIÓN INTERNA (NO importar fuera)
def _send_email(subject: str, message: str, to_email: str):
    if not settings.RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY no configurada")

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": settings.DEFAULT_FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "text": message,
        },
        timeout=10,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Error Resend {response.status_code}: {response.text}"
        )
