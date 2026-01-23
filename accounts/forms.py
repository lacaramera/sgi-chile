from django import forms
from django.contrib.auth.forms import UserCreationForm, SetPasswordForm
from django.core.exceptions import ValidationError
from .models import User, Household, HouseholdMember, Grupo
from .utils import normalize_rut, is_valid_rut_format
class UserRegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    role = forms.ChoiceField(choices=User.ROLE_CHOICES)

    class Meta:
        model = User
        fields = ('username','email','first_name','last_name','role','password1','password2')

class MemberCreateForm(UserCreationForm):
    class Meta:
        model = User
        fields = (
            "username", "rut", "first_name", "last_name", "email",
            "role", "is_active","birth_date",
            "address", "join_date", "is_only_family_member",
            "group","division",
            "is_division_national_leader",
            "is_division_national_vice",
            "national_division",

        )
        widgets = {
            "join_date": forms.DateInput(attrs={"type": "date"}),
            "birth_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # placeholders
        self.fields["username"].widget.attrs.update({"placeholder": "Username (ej: miguel.perez o 12345678-9)"})
        self.fields["rut"].widget.attrs.update({"placeholder": "RUT (ej: 12345678-9)"})
        self.fields["first_name"].widget.attrs.update({"placeholder": "Nombre"})
        self.fields["last_name"].widget.attrs.update({"placeholder": "Apellido"})
        self.fields["email"].widget.attrs.update({"placeholder": "Correo (opcional)"})
        self.fields["address"].widget.attrs.update({"placeholder": "Dirección (opcional)"})

        # importantísimo: el form debe aceptar cualquier grupo para validar lo que el JS setea
        self.fields["group"].queryset = Grupo.objects.select_related("zona__sector").all()

        # estilos (para que se vea igual SGI)
        for name, f in self.fields.items():
            if name in ("is_active", "is_only_family_member"):
                continue
            css = "w-full border border-gray-200 rounded-2xl px-4 py-2 text-sm"
            f.widget.attrs.update({"class": css})

        # checkboxes
        self.fields["is_active"].widget.attrs.update({"class": "h-4 w-4"})
        self.fields["is_only_family_member"].widget.attrs.update({"class": "h-4 w-4"})

class MemberEditForm(forms.ModelForm):
    household = forms.ModelChoiceField(
        queryset=Household.objects.all(),
        required=False,
        label="Hogar (viven juntos)"
    )
    relationship = forms.ChoiceField(
        choices=HouseholdMember.REL_CHOICES,
        required=False,
        label="Relación en el hogar"
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "role", "is_active","division",
        "is_division_national_leader",
        "is_division_national_vice",
        "national_division",]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # precargar household si existe
        if self.instance and self.instance.pk:
            try:
                hm = self.instance.household_membership
                self.fields["household"].initial = hm.household
                self.fields["relationship"].initial = hm.relationship
            except Exception:
                pass



class SelfRegisterForm(forms.ModelForm):
    """
    Registro público:
    - username = rut (obligatorio)
    - NO permite elegir role / flags nacionales
    - Puede elegir: división, fecha ingreso, sector/zona/grupo (group)
    - ✅ el usuario elige su contraseña en el mismo registro
    - ✅ queda ACTIVO inmediatamente (sin correo)
    """
    email = forms.EmailField(required=True)

    password1 = forms.CharField(
        label="Contraseña",
        required=True,
        widget=forms.PasswordInput(attrs={
            "placeholder": "Crea una contraseña",
            "autocomplete": "new-password",
        }),
    )
    password2 = forms.CharField(
        label="Confirmar contraseña",
        required=True,
        widget=forms.PasswordInput(attrs={
            "placeholder": "Repite la contraseña",
            "autocomplete": "new-password",
        }),
    )

    class Meta:
        model = User
        fields = (
            "rut",
            "first_name",
            "last_name",
            "email",
            "birth_date",
            "join_date",
            "address",
            "division",
            "group",
            # OJO: password1/password2 NO van aquí porque no son del modelo
        )
        widgets = {
            "birth_date": forms.DateInput(attrs={"type": "date"}),
            "join_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # placeholders
        self.fields["rut"].widget.attrs.update({"placeholder": "RUT (ej: 12345678-9, sin puntos)"})
        self.fields["first_name"].widget.attrs.update({"placeholder": "Nombre"})
        self.fields["last_name"].widget.attrs.update({"placeholder": "Apellido"})
        self.fields["email"].widget.attrs.update({"placeholder": "Correo"})
        self.fields["address"].widget.attrs.update({"placeholder": "Dirección (opcional)"})

        # ✅ el JS setea group por cascada, entonces el form debe aceptar cualquier grupo
        self.fields["group"].queryset = Grupo.objects.select_related("zona__sector").all()

        # Reglas (ajusta si quieres)
        self.fields["division"].required = True
        self.fields["group"].required = True
        self.fields["join_date"].required = False

        # estilos (mismo look SGI)
        css = "w-full border border-gray-200 rounded-2xl px-4 py-2 text-sm"
        for name, f in self.fields.items():
            f.widget.attrs.update({"class": css})

        # estilo a passwords (también)
        self.fields["password1"].widget.attrs.update({"class": css})
        self.fields["password2"].widget.attrs.update({"class": css})

    def clean_rut(self):
        rut_raw = (self.cleaned_data.get("rut") or "").strip()
        rut = normalize_rut(rut_raw)

        if not is_valid_rut_format(rut):
            raise ValidationError("RUT inválido. Usa formato 12345678-9 (sin puntos).")

        if User.objects.filter(rut=rut).exists():
            raise ValidationError("Ya existe una cuenta con este RUT.")
        if User.objects.filter(username=rut).exists():
            raise ValidationError("Ya existe una cuenta con este RUT.")
        return rut

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise ValidationError("Debes ingresar un correo.")
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("Ya existe una cuenta con este correo.")
        return email

    def clean_password1(self):
        pw = self.cleaned_data.get("password1")
        if pw:
            validate_password(pw)
        return pw

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Las contraseñas no coinciden.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)

        # normalizar RUT
        user.rut = normalize_rut(user.rut)

        # username SIEMPRE = rut
        user.username = user.rut

        # seguridad: NO permitir que se autoasignen permisos
        user.role = User.ROLE_MIEMBRO
        user.is_superuser = False
        user.is_staff = False

        # flags nacionales limpias
        user.is_division_national_leader = False
        user.is_division_national_vice = False
        user.national_division = None

        # ✅ activo inmediatamente (ya no hay activación por correo)
        user.is_active = True

        # ✅ setear contraseña elegida por el usuario
        password = self.cleaned_data.get("password1")
        user.set_password(password)

        if commit:
            user.save()
            self.save_m2m()

        return user
    

class ActivationSetPasswordForm(SetPasswordForm):
    """
    Form para el link de activación:
    el usuario elige contraseña (new_password1 / new_password2)
    con el mismo estilo del SGI.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        css = "w-full border border-gray-200 rounded-2xl px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-sky-500"
        for f in self.fields.values():
            f.widget.attrs.update({"class": css})

        # placeholders (opcional, ayuda mucho)
        self.fields["new_password1"].widget.attrs.update({"placeholder": "Nueva contraseña"})
        self.fields["new_password2"].widget.attrs.update({"placeholder": "Repite la contraseña"})