from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import User, Household, HouseholdMember

class UserRegisterForm(UserCreationForm):
    email = forms.EmailField(required=True)
    role = forms.ChoiceField(choices=User.ROLE_CHOICES)

    class Meta:
        model = User
        fields = ('username','email','first_name','last_name','role','password1','password2')

class MemberCreateForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("username", "rut", "first_name", "last_name", "email", "role", "is_active")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # placeholders bonitos
        self.fields["username"].widget.attrs.update({"placeholder": "Username (ej: 12345678-9)"})
        self.fields["rut"].widget.attrs.update({"placeholder": "RUT (ej: 12345678-9)"})
        self.fields["first_name"].widget.attrs.update({"placeholder": "Nombre"})
        self.fields["last_name"].widget.attrs.update({"placeholder": "Apellido"})
        self.fields["email"].widget.attrs.update({"placeholder": "Correo (opcional)"})

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