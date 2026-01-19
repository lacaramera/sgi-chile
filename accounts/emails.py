from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

def send_activation_email(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)

    activation_link = request.build_absolute_uri(
        reverse("accounts:activate", kwargs={"uidb64": uid, "token": token})
    )

    subject = "Activa tu cuenta - SGI Chile"
    message = render_to_string("activation_email.txt", {
        "user": user,
        "activation_link": activation_link,
    })

    send_mail(
        subject=subject,
        message=message,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=[user.email],
        fail_silently=False,
    )
