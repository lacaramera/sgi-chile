from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import User, Household, HouseholdMember, Grupo

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
            "group",
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
        fields = ["first_name", "last_name", "email", "role", "is_active"]

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