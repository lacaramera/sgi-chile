from django.urls import reverse
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.contrib.auth.tokens import default_token_generator
from django.template.loader import render_to_string
from django.conf import settings
from django.core.mail import send_mail



import re, requests

RUT_RE = re.compile(r"^\d{7,8}-[\dkK]$")

def normalize_rut(value: str) -> str:
    if not value:
        return value
    v = value.strip().replace(".", "").replace(" ", "")
    # si viene sin guion, intentar ponerlo (opcional)
    if "-" not in v and len(v) in (8, 9):  # 7-8 cuerpo + DV
        v = v[:-1] + "-" + v[-1]
    v = v.upper()
    return v

def is_valid_rut_format(value: str) -> bool:
    return bool(value and RUT_RE.match(value))


def send_activation_email(user, request):
    """
    Envía un correo de activación con un token seguro que apunta a la vista 'activate'.
    En desarrollo el backend de email puede ser console, así que se imprimirá en la terminal.
    """
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse('activate', kwargs={'uidb64': uid, 'token': token})
    activation_link = request.build_absolute_uri(path)
    subject = 'Activa tu cuenta — Soka Gakkai Chile'
    message = render_to_string('accounts/activation_email.txt', {
        'user': user,
        'activation_link': activation_link,
    })
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'no-reply@sokagakkai.cl')
    # send_mail devuelve el número de emails enviados
    send_mail(subject, message, from_email, [user.email])


def send_email_resend(subject: str, message: str, to_email: str):
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