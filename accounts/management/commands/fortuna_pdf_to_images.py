import os
import tempfile
from django.core.management.base import BaseCommand
from django.core.files.base import ContentFile
from django.conf import settings

from accounts.models import FortunaIssue, FortunaIssuePage

class Command(BaseCommand):
    help = "Convierte el PDF de una FortunaIssue a imágenes por página."

    def add_arguments(self, parser):
        parser.add_argument("issue_id", type=int)

    def handle(self, *args, **opts):
        issue_id = opts["issue_id"]
        issue = FortunaIssue.objects.get(id=issue_id)

        if not issue.material_pdf:
            self.stdout.write(self.style.ERROR("Esta issue no tiene material_pdf."))
            return

        # Borra páginas anteriores
        FortunaIssuePage.objects.filter(issue=issue).delete()

        # ✅ Usamos PyMuPDF (fitz) para renderizar cada página a PNG
        try:
            import fitz  # PyMuPDF
        except ImportError:
            self.stdout.write(self.style.ERROR("Falta PyMuPDF: pip install pymupdf"))
            return

        pdf_path = issue.material_pdf.path
        doc = fitz.open(pdf_path)

        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=160)  # 140–200 dpi suele andar bien
            img_bytes = pix.tobytes("png")

            obj = FortunaIssuePage(issue=issue, page_number=i + 1)
            obj.image.save(
                f"{issue.code}_page_{i+1}.png",
                ContentFile(img_bytes),
                save=True
            )

        self.stdout.write(self.style.SUCCESS(f"Listo. {doc.page_count} páginas generadas."))
