"""
Forms for the admin panel
"""
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, IntegerField, BooleanField, TextAreaField, SubmitField, PasswordField
from wtforms.validators import DataRequired, Email, Length, NumberRange, Optional
from models import Plan, Company, User
from constants import FIELD_LENGTH_NAME_MAX, FIELD_LENGTH_DISPLAY_NAME_MAX


def coerce_optional_int(value):
    """Helper function to coerce empty strings or None to None, otherwise to int"""
    if value == '' or value is None:
        return None
    return int(value)


class PlanForm(FlaskForm):
    """Form for creating/editing plans"""
    name = StringField('Nom technique',
                       validators=[DataRequired(),
                                   Length(min=2, max=FIELD_LENGTH_NAME_MAX)])
    display_name = StringField(
        'Nom d\'affichage',
        validators=[DataRequired(), Length(min=2, max=FIELD_LENGTH_DISPLAY_NAME_MAX)])
    description = TextAreaField('Description', validators=[Optional()])
    plan_level = IntegerField(
        'Niveau de plan',
        validators=[DataRequired(),
                    NumberRange(min=1, max=100)])
    is_active = BooleanField('Actif', default=True)
    is_free = BooleanField('Gratuit', default=False)

    # REFONTE STRIPE V2 - Structure simplifiée avec une seule licence
    stripe_product_id = StringField('ID produit Stripe',
                                    validators=[Optional()])
    stripe_price_id = StringField('ID prix Stripe', validators=[Optional()])

    # Plan limits and features
    max_clients = IntegerField('Limite clients',
                               validators=[Optional(),
                                           NumberRange(min=0)])
    daily_sync_limit = IntegerField(
        'Limite sync/jour', validators=[Optional(),
                                        NumberRange(min=0)])
    allows_email_sending = BooleanField('Autoriser envoi emails',
                                        default=False)
    allows_email_connection = BooleanField('Autoriser connexion email',
                                           default=False)
    allows_accounting_connection = BooleanField(
        'Autoriser connexion comptable', default=False)
    allows_team_management = BooleanField('Autoriser gestion équipe',
                                          default=False)
    allows_email_templates = BooleanField('Autoriser modèles emails',
                                          default=False)


class CompanyForm(FlaskForm):
    """Form for creating/editing companies"""
    name = StringField('Nom de l\'entreprise',
                       validators=[DataRequired(),
                                   Length(min=2, max=200)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    phone = StringField('Téléphone', validators=[Optional()])
    address = TextAreaField('Adresse', validators=[Optional()])

    # Plan selection
    plan_id = SelectField('Forfait', validators=[Optional()], coerce=int)
    plan_status = SelectField('Statut forfait',
                              choices=[('active', 'Actif'),
                                       ('cancelled', 'Annulé'),
                                       ('past_due', 'Impayé')],
                              default='active')

    # REFONTE STRIPE V2 - Champs simplifiés
    stripe_customer_id = StringField('ID client Stripe',
                                     validators=[Optional()])
    stripe_subscription_id = StringField('ID abonnement Stripe',
                                         validators=[Optional()])
    quantity_licenses = IntegerField(
        'Nombre de licences',
        validators=[Optional(), NumberRange(min=1)],
        default=1)

    # Manual override
    is_free_account = BooleanField('Compte gratuit (créé manuellement)',
                                   default=False)
    client_limit = IntegerField('Limite clients',
                                validators=[Optional(),
                                            NumberRange(min=0)])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate plan choices - CORRECTION: Utiliser des integers, pas des strings
        from models import Plan
        self.plan_id.choices = [(0, 'Aucun forfait')] + [
            (p.id, p.display_name)
            for p in Plan.query.filter_by(is_active=True).all()
        ]


class UserForm(FlaskForm):
    """Form for creating/editing users"""
    first_name = StringField(
        'Prénom', validators=[DataRequired(),
                              Length(min=2, max=FIELD_LENGTH_NAME_MAX)])
    last_name = StringField('Nom',
                            validators=[DataRequired(),
                                        Length(min=2, max=FIELD_LENGTH_NAME_MAX)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    is_superuser = BooleanField('Super administrateur', default=False)
    must_change_password = BooleanField('Doit changer le mot de passe',
                                        default=True)

    company_id = SelectField('Entreprise (optionnel)',
                             validators=[Optional()],
                             coerce=coerce_optional_int)
    role = SelectField('Rôle dans l\'entreprise',
                       choices=[('employe', 'Employé'),
                                ('admin', 'Administrateur'),
                                ('super_admin', 'Super Admin'),
                                ('lecteur', 'Lecteur')],
                       default='employe')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate company choices
        self.company_id.choices = [('', 'Aucune entreprise')] + [
            (c.id, c.name) for c in Company.query.all()
        ]


class UserCompanyForm(FlaskForm):
    """Form for adding/editing user-company relationships"""
    user_id = SelectField('Utilisateur',
                          validators=[DataRequired()],
                          coerce=coerce_optional_int)
    company_id = SelectField('Entreprise',
                             validators=[DataRequired()],
                             coerce=coerce_optional_int)
    role = SelectField('Rôle',
                       choices=[('employe', 'Employé'),
                                ('admin', 'Administrateur'),
                                ('super_admin', 'Super Admin'),
                                ('lecteur', 'Lecteur')],
                       default='employe')
    is_active = BooleanField('Actif', default=True)
    bypass_license = BooleanField('Ignorer la limite de licences (Support)',
                                  default=False)
    admin_password = PasswordField('Mot de passe de confirmation')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Populate choices
        self.user_id.choices = [(u.id,
                                 f"{u.first_name} {u.last_name} ({u.email})")
                                for u in User.query.all()]
        self.company_id.choices = [(c.id, c.name) for c in Company.query.all()]


class GuidePageForm(FlaskForm):
    """Form for creating/editing guide pages"""
    title = StringField('Titre',
                        validators=[DataRequired(),
                                    Length(min=2, max=200)])
    slug = StringField('Slug (URL)',
                       validators=[DataRequired(),
                                   Length(min=2, max=200)])
    meta_description = TextAreaField('Meta description (SEO)',
                                     validators=[Optional(),
                                                 Length(max=300)])
    content = TextAreaField('Contenu', validators=[DataRequired()])
    image_url = TextAreaField('URL de l\'image principale',
                              validators=[Optional()])
    video_url = TextAreaField('URL de la vidéo (embed)',
                              validators=[Optional()])
    is_published = BooleanField('Publié', default=False)
    order = IntegerField('Ordre d\'affichage',
                         validators=[Optional(),
                                     NumberRange(min=0)],
                         default=0)
    submit = SubmitField('Enregistrer')
