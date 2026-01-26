from django import forms
from django.core.exceptions import ValidationError
from django.contrib.auth.forms import UserCreationForm, SetPasswordForm
from django.contrib.auth.password_validation import validate_password

from .models import User, Household, HouseholdMember, Grupo
from .utils import normalize_rut, is_valid_rut_format


class UserRegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    role = forms.ChoiceField(choices=User.ROLE_CHOICES)

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "role", "password1", "password2")


class MemberCreateForm(UserCreationForm):
    class Meta:
        model = User
        fields = (
            "rut", "first_name", "last_name", "email",
            "role", "is_active", "birth_date",
            "address", "join_date", "is_only_family_member",
            "group", "division",
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
        self.fields["rut"].widget.attrs.update({"placeholder": "RUT (ej: 12345678-9, sin puntos)"})
        self.fields["first_name"].widget.attrs.update({"placeholder": "Nombre"})
        self.fields["last_name"].widget.attrs.update({"placeholder": "Apellido"})
        self.fields["email"].widget.attrs.update({"placeholder": "Correo (opcional)"})
        self.fields["address"].widget.attrs.update({"placeholder": "Dirección (opcional)"})

        # ✅ asegurar choices correctas
        self.fields["division"].choices = [("", "— Selecciona —")] + list(User.DIVISION_CHOICES)
        self.fields["national_division"].choices = [("", "— Selecciona —")] + list(User.NATIONAL_DIVISION_CHOICES)

        # importantísimo: el form debe aceptar cualquier grupo para validar lo que el JS setea
        self.fields["group"].queryset = Grupo.objects.select_related("zona__sector").all()

        # estilos
        css = "w-full border border-gray-200 rounded-2xl px-4 py-2 text-sm"
        for name, f in self.fields.items():
            if name in (
                "is_active",
                "is_only_family_member",
                "is_division_national_leader",
                "is_division_national_vice",
            ):
                continue
            f.widget.attrs.update({"class": css})

        # checkboxes
        self.fields["is_active"].widget.attrs.update({"class": "h-4 w-4"})
        self.fields["is_only_family_member"].widget.attrs.update({"class": "h-4 w-4"})
        self.fields["is_division_national_leader"].widget.attrs.update({"class": "h-4 w-4"})
        self.fields["is_division_national_vice"].widget.attrs.update({"class": "h-4 w-4"})

    def clean_rut(self):
        rut_raw = (self.cleaned_data.get("rut") or "").strip()
        rut = normalize_rut(rut_raw)

        if not is_valid_rut_format(rut):
            raise ValidationError("RUT inválido. Usa formato 12345678-9 (sin puntos).")

        if User.objects.filter(rut=rut).exists():
            raise ValidationError("Ya existe una cuenta con este RUT.")

        # username también será el rut
        if User.objects.filter(username=rut).exists():
            raise ValidationError("Ya existe una cuenta con este RUT.")

        return rut

    def clean(self):
        cleaned = super().clean()

        leader = cleaned.get("is_division_national_leader")
        vice = cleaned.get("is_division_national_vice")
        nat_div = cleaned.get("national_division")

        # no permitir ambos
        if leader and vice:
            self.add_error("is_division_national_vice", "No puedes marcar Responsable y Vice a la vez.")

        # si marca RN/Vice -> exige división nacional
        if (leader or vice) and not nat_div:
            self.add_error("national_division", "Debes elegir la división nacional que lidera (incluye DS).")

        # si NO marca RN/Vice -> limpia el campo
        if not (leader or vice):
            cleaned["national_division"] = None

        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)

        # ✅ normalizar rut y usarlo como username
        user.rut = normalize_rut(user.rut)
        user.username = user.rut

        # seguridad: si NO es RN/Vice, limpiar national_division
        if not (user.is_division_national_leader or user.is_division_national_vice):
            user.national_division = None

        if commit:
            user.save()
            self.save_m2m()

        return user


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
        fields = (
            "first_name",
            "last_name",
            "email",
            "role",
            "is_active",
            "division",
            "is_division_national_leader",
            "is_division_national_vice",
            "national_division",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ✅ asegurar choices correctas
        self.fields["division"].choices = [("", "— Selecciona —")] + list(User.DIVISION_CHOICES)
        self.fields["national_division"].choices = [("", "— Selecciona —")] + list(User.NATIONAL_DIVISION_CHOICES)

        css = "w-full border border-gray-200 rounded-2xl px-4 py-2 text-sm"
        for name, f in self.fields.items():
            if name in ("is_active", "is_division_national_leader", "is_division_national_vice"):
                continue
            f.widget.attrs.update({"class": css})

        self.fields["is_active"].widget.attrs.update({"class": "h-4 w-4"})
        self.fields["is_division_national_leader"].widget.attrs.update({"class": "h-4 w-4"})
        self.fields["is_division_national_vice"].widget.attrs.update({"class": "h-4 w-4"})

        # precargar household si existe
        if self.instance and self.instance.pk:
            try:
                hm = self.instance.household_membership
                self.fields["household"].initial = hm.household
                self.fields["relationship"].initial = hm.relationship
            except Exception:
                pass

    def clean(self):
        cleaned = super().clean()

        leader = cleaned.get("is_division_national_leader")
        vice = cleaned.get("is_division_national_vice")
        nat_div = cleaned.get("national_division")

        if leader and vice:
            self.add_error("is_division_national_vice", "No puedes marcar Responsable y Vice a la vez.")

        if (leader or vice) and not nat_div:
            self.add_error("national_division", "Debes elegir la división nacional que lidera (incluye DS).")

        if not (leader or vice):
            cleaned["national_division"] = None

        return cleaned


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
        )
        widgets = {
            "birth_date": forms.DateInput(attrs={"type": "date"}),
            "join_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["rut"].widget.attrs.update({"placeholder": "RUT (ej: 12345678-9, sin puntos)"})
        self.fields["first_name"].widget.attrs.update({"placeholder": "Nombre"})
        self.fields["last_name"].widget.attrs.update({"placeholder": "Apellido"})
        self.fields["email"].widget.attrs.update({"placeholder": "Correo"})
        self.fields["address"].widget.attrs.update({"placeholder": "Dirección (opcional)"})

        self.fields["group"].queryset = Grupo.objects.select_related("zona__sector").all()

        self.fields["division"].required = True
        self.fields["group"].required = True
        self.fields["join_date"].required = False

        css = "w-full border border-gray-200 rounded-2xl px-4 py-2 text-sm"
        for name, f in self.fields.items():
            f.widget.attrs.update({"class": css})

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

        user.rut = normalize_rut(user.rut)
        user.username = user.rut

        # registro público: NO es RN/Vice
        user.is_division_national_leader = False
        user.is_division_national_vice = False
        user.national_division = None

        # se activa inmediatamente (como dijiste)
        user.is_active = True

        if commit:
            user.set_password(self.cleaned_data["password1"])
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

        self.fields["new_password1"].widget.attrs.update({"placeholder": "Nueva contraseña"})
        self.fields["new_password2"].widget.attrs.update({"placeholder": "Repite la contraseña"})
