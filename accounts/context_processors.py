from .models import Notification

def notifications(request):
    """
    Añade a todos los templates:
      - notifications_unread: últimas 5 no leídas
      - notifications_unread_count: cantidad de no leídas
    """
    if request.user.is_authenticated:
        qs = Notification.objects.filter(
            user=request.user, is_read=False
        ).order_by("-created_at")[:5]
        return {
            "notifications_unread": qs,
            "notifications_unread_count": qs.count(),
        }
    return {}
