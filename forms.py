from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed
from wtforms import StringField, PasswordField, SelectField, TextAreaField, DecimalField, DateField, HiddenField, BooleanField, SubmitField, IntegerField, RadioField
from wtforms.validators import DataRequired, Email, Length, EqualTo, NumberRange, Optional, ValidationError, Regexp
from wtforms.widgets import TextArea
from datetime import date
# Import moved to functions to avoid circular imports
from app import db
from constants import FIELD_LENGTH_NAME_MAX

class LoginForm(FlaskForm):
    """Form for user login"""
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Mot de passe', validators=[DataRequired()])
    remember_me = BooleanField('Se souvenir de moi')
    submit = SubmitField('Se connecter')

class ChangePasswordForm(FlaskForm):
    """Form for mandatory password change"""
    current_password = PasswordField('Mot de passe actuel', validators=[DataRequired()])
    new_password = PasswordField('Nouveau mot de passe', validators=[
        DataRequired(),
        Length(min=8, max=128, message='Le mot de passe doit contenir entre 8 et 128 caractères'),
        Regexp(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$', message='Le mot de passe doit contenir au moins une minuscule, une majuscule et un chiffre')
    ])
    confirm_password = PasswordField('Confirmer le nouveau mot de passe',
                                   validators=[DataRequired(), EqualTo('new_password', message='Les mots de passe doivent correspondre')])
    submit = SubmitField('Changer le mot de passe')

    def validate_new_password(self, new_password):
        """Vérifier que le nouveau mot de passe est différent de l'ancien"""
        if hasattr(self, '_current_user') and self._current_user:
            from werkzeug.security import check_password_hash
            if check_password_hash(self._current_user.password_hash, new_password.data):
                raise ValidationError('Le nouveau mot de passe doit être différent de l\'ancien mot de passe.')

    def set_current_user(self, user):
        """Définir l'utilisateur actuel pour la validation"""
        self._current_user = user


class TwoFactorForm(FlaskForm):
    """Form for two-factor authentication verification.

    Accepts either a 6-digit email code, a 6-digit TOTP code,
    or a recovery code in XXXX-XXXX format.
    """
    code = StringField('Code de vérification', validators=[
        DataRequired(message='Le code de vérification est requis'),
        Length(min=6, max=9, message='Code invalide'),
        Regexp(r'^(\d{6}|[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4})$',
               message='Entrez un code a 6 chiffres ou un code de secours (XXXX-XXXX)')
    ])
    submit = SubmitField('Vérifier')

class AccountTypeForm(FlaskForm):
    """Form pour choisir le type de création"""
    account_type = RadioField('Type de compte',
                              choices=[
                                  ('new_account', 'Nouveau compte utilisateur'),
                                  ('new_company', 'Nouvelle entreprise pour mon compte existant')
                              ],
                              default='new_account')
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=120)])
    submit = SubmitField('Continuer')

class CompanySettingsForm(FlaskForm):
    """Form for company settings"""
    name = StringField('Nom de l\'entreprise', validators=[DataRequired(), Length(max=200)])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=120)])
    phone = StringField('Téléphone', validators=[Optional(), Length(max=20)])
    address = TextAreaField('Adresse', validators=[Optional()])
    aging_calculation_method = SelectField(
        'Méthode de calcul de l\'âge',
        choices=[
            ('invoice_date', 'Date de facture'),
            ('due_date', 'Date d\'échéance')
        ],
        default='invoice_date'
    )
    logo = FileField('Logo', validators=[FileAllowed(['jpg', 'png', 'jpeg', 'gif'], 'Images seulement!')])
    primary_color = StringField('Couleur primaire', validators=[Optional(), Length(max=7)],
                               default='#007bff',
                               render_kw={'type': 'color'})
    secondary_color = StringField('Couleur secondaire', validators=[Optional(), Length(max=7)],
                                 default='#6c757d',
                                 render_kw={'type': 'color'})
    timezone = SelectField('Fuseau horaire',
                          choices=[
                              ('America/Montreal', 'Montréal (EST/EDT)'),
                              ('America/Toronto', 'Toronto (EST/EDT)'),
                              ('America/Vancouver', 'Vancouver (PST/PDT)'),
                              ('America/New_York', 'New York (EST/EDT)'),
                              ('Europe/Paris', 'Paris (CET/CEST)'),
                              ('UTC', 'UTC (Temps universel)')
                          ],
                          default='America/Montreal')
    currency = SelectField('Devise',
                          choices=[
                              ('CAD', '$ CAD (Dollar canadien)'),
                              ('USD', '$ USD (Dollar américain)'),
                              ('EUR', '€ EUR (Euro)'),
                              ('GBP', '£ GBP (Livre sterling)'),
                              ('CHF', 'CHF (Franc suisse)')
                          ],
                          default='CAD')

    # Project field configuration (optional hierarchy: Client > Project > Invoices)
    # Available only for Excel/CSV imports. Accounting connectors ignore this field.
    project_field_enabled = BooleanField('Activer le champ projet (hiérarchie optionnelle)', default=False)
    project_field_name = StringField('Nom du champ',
                                     validators=[Optional(), Length(max=50)],
                                     default='Projet',
                                     render_kw={'placeholder': 'Ex: Projet, Contrat, Chantier'})

    submit = SubmitField('Sauvegarder')

    def validate_project_field_name(self, field):
        """Ensure project field name is provided when feature is enabled"""
        if self.project_field_enabled.data and not field.data:
            raise ValidationError('Le nom du champ est requis lorsque la fonctionnalité projet est activée.')

class ClientForm(FlaskForm):
    """Form for client creation and editing"""
    code_client = StringField('Code client', validators=[DataRequired(), Length(min=1, max=50)])
    name = StringField('Nom du client', validators=[DataRequired(), Length(max=200)])
    email = StringField('Email', validators=[Optional(), Email(), Length(max=120)])
    phone = StringField('Téléphone', validators=[Optional(), Length(max=50)])
    address = TextAreaField('Adresse', validators=[Optional()])
    collector_id = SelectField('Collecteur assigné', coerce=lambda x: int(x) if x else None, validators=[Optional()])
    representative_name = StringField('Représentant', validators=[Optional(), Length(max=200)])
    payment_terms = StringField('Termes de paiement', validators=[Optional(), Length(max=100)])
    language = SelectField('Langue',
                          choices=[('fr', 'Français'), ('en', 'English')],
                          default='fr')
    parent_client_id = SelectField('Compte parent', coerce=lambda x: int(x) if x else None, validators=[Optional()])

    submit = SubmitField('Sauvegarder')

    def __init__(self, company_id=None, current_client_id=None, *args, **kwargs):
        super(ClientForm, self).__init__(*args, **kwargs)
        self.company_id = company_id
        self.current_client_id = current_client_id

        if company_id:
            from models import User, UserCompany, Client
            # Alimenter la liste des collecteurs avec les membres de l'équipe
            collectors = db.session.query(User).join(UserCompany).filter(
                UserCompany.company_id == company_id
            ).all()
            choices = [('', 'Non assigné')]
            for user in collectors:
                choices.append((str(user.id), f"{user.first_name} {user.last_name}"))
            self.collector_id.choices = choices

            # Alimenter la liste des comptes parents (exclure le client actuel et ses enfants)
            parent_choices = [('', 'Aucun parent')]
            potential_parents = Client.query.filter_by(company_id=company_id).all()

            for client in potential_parents:
                # Exclure le client actuel et ses enfants directs
                if current_client_id and (client.id == current_client_id or client.parent_client_id == current_client_id):
                    continue
                # Exclure seulement les comptes qui sont eux-mêmes des enfants (pas les parents)
                if client.parent_client_id is not None:
                    continue
                parent_choices.append((str(client.id), f"{client.code_client} - {client.name}"))

            self.parent_client_id.choices = parent_choices
        else:
            self.collector_id.choices = [('', 'Non assigné')]
            self.parent_client_id.choices = [('', 'Aucun parent')]

    def validate_code_client(self, field):
        """Valider que le code client est unique dans l'entreprise"""
        if self.company_id:
            from models import Client
            query = Client.query.filter_by(
                company_id=self.company_id,
                code_client=field.data
            )
            if self.current_client_id:
                query = query.filter(Client.id != self.current_client_id)

            existing_client = query.first()
            if existing_client:
                raise ValidationError(f'Le code client "{field.data}" existe déjà dans votre entreprise.')

class InvoiceForm(FlaskForm):
    """Form for invoice creation and editing"""
    invoice_number = StringField('Numéro de facture', validators=[DataRequired(), Length(max=50)])
    client_id = HiddenField('Client', validators=[DataRequired()])
    original_amount = DecimalField('Montant Original (optionnel)', validators=[Optional(), NumberRange(min=0.01)], places=2)
    amount = DecimalField('Montant', validators=[DataRequired(), NumberRange(min=0.01)], places=2)
    invoice_date = DateField('Date de facture', validators=[DataRequired()])
    due_date = DateField('Date d\'échéance', validators=[DataRequired()])
    project_name = StringField('Projet', validators=[Optional(), Length(max=100)])
    # Pas de statut - toutes les factures sont impayées/en retard
    submit = SubmitField('Sauvegarder')

    def __init__(self, *args, **kwargs):
        kwargs.pop('company_id', None)
        super(InvoiceForm, self).__init__(*args, **kwargs)

class CommunicationNoteForm(FlaskForm):
    """Form for communication notes - simplified"""
    note_text = TextAreaField('Note', validators=[DataRequired()], widget=TextArea())
    note_type = SelectField('Type de note',
                           choices=[
                               ('general', 'Général'),
                               ('call', 'Appel téléphonique'),
                               ('email', 'Courriel'),
                               ('meeting', 'Rencontre')
                           ],
                           default='general')
    note_date = DateField('Date de la note', validators=[DataRequired()], default=date.today)
    reminder_date = DateField('Date de rappel', validators=[Optional()])
    tagged_users = HiddenField('Utilisateurs mentionnés')  # Hidden field for @ mentions

    # Email fields for when note_type is 'email'
    email_subject = StringField('Sujet du courriel', validators=[Optional()])
    email_body = TextAreaField('Corps du courriel', validators=[Optional()], widget=TextArea())
    email_from = StringField('De', validators=[Optional()])
    email_to = StringField('À', validators=[Optional()])

    submit = SubmitField('Ajouter la note')

    def __init__(self, company_id=None, *args, **kwargs):
        super(CommunicationNoteForm, self).__init__(*args, **kwargs)
        self.company_id = company_id

    def validate_reminder_date(self, reminder_date):
        """Valider que le rappel ne peut être mis que si un collecteur est assigné"""
        if reminder_date.data:
            from flask import request
            # Récupérer l'ID du client depuis l'URL
            client_id = request.view_args.get('id') if request.view_args else None
            if client_id:
                from models import Client
                client = Client.query.get(client_id)
                if client and not client.collector_id:
                    raise ValidationError('Veuillez assigner un collecteur à ce client pour pouvoir mettre des rappels.')

class EmailTemplateForm(FlaskForm):
    """Form for email templates"""
    name = StringField('Nom du modèle', validators=[DataRequired(), Length(max=100)])
    subject = StringField('Sujet', validators=[DataRequired(), Length(max=200)])
    content = TextAreaField('Contenu', validators=[DataRequired()], widget=TextArea())
    is_active = BooleanField('Modèle actif', default=True)
    is_shared = BooleanField('Partager avec l\'équipe', default=False)
    is_editable_by_team = BooleanField('Permettre à l\'équipe de modifier', default=False)
    submit = SubmitField('Sauvegarder')

    def __init__(self, user_role=None, *args, **kwargs):
        super(EmailTemplateForm, self).__init__(*args, **kwargs)

        # Only admins and super admins can share templates
        # Employees (employe) cannot share or allow team editing
        if user_role not in ['admin', 'super_admin']:
            # Remove sharing fields for employees
            del self.is_shared
            del self.is_editable_by_team

class SendEmailForm(FlaskForm):
    """Form for sending emails"""
    template_id = SelectField('Modèle de courriel', coerce=lambda x: int(x) if x else None, validators=[Optional()])
    to_emails = StringField('Destinataires', validators=[DataRequired()],
                           render_kw={'placeholder': 'courriel1@exemple.com, courriel2@exemple.com'})
    cc_emails = StringField('Copie (CC)', validators=[Optional()],
                           render_kw={'placeholder': 'copie@exemple.com (optionnel)'})
    subject = StringField('Sujet', validators=[DataRequired(), Length(max=200)])
    content = TextAreaField('Contenu', validators=[DataRequired()], widget=TextArea())

    # Options email
    high_importance = BooleanField('Importance haute', default=False)
    read_receipt = BooleanField('Accusé de lecture', default=False)
    delivery_receipt = BooleanField('Accusé de réception', default=False)

    attach_pdf = BooleanField('Joindre relevé PDF', default=False)
    attach_excel = BooleanField('Joindre factures Excel', default=False)
    external_file = FileField('Fichier externe', validators=[
        FileAllowed(['pdf', 'xlsx', 'csv'], 'Seuls les fichiers .pdf, .xlsx et .csv sont autorisés.')
    ])
    attachment_language = SelectField('Langue des pièces jointes',
                                     choices=[('fr', 'Français'), ('en', 'English')],
                                     default='fr')
    include_children = BooleanField('Inclure les factures des clients enfants', default=False)
    send_copy = BooleanField('M\'envoyer une copie', default=False)
    submit = SubmitField('Envoyer')

    def __init__(self, company_id=None, user_id=None, *args, **kwargs):
        super(SendEmailForm, self).__init__(*args, **kwargs)

        if company_id and user_id:
            # Import here to avoid circular imports
            from models import EmailTemplate

            # Get templates that the user can access:
            # 1. Templates created by the user
            # 2. Templates shared with the team (is_shared=True)
            templates = EmailTemplate.query.filter(
                EmailTemplate.company_id == company_id,
                EmailTemplate.is_active == True,
                db.or_(
                    EmailTemplate.created_by == user_id,  # User's own templates
                    EmailTemplate.is_shared == True       # Shared templates
                )
            ).order_by(EmailTemplate.name).all()

            # Create choices with empty option first
            choices = [('', 'Sélectionner un modèle...')]
            for template in templates:
                choices.append((str(template.id), template.name))
            self.template_id.choices = choices

class CSVImportForm(FlaskForm):
    """Form for CSV imports"""
    import_type = SelectField('Type d\'importation',
                             choices=[
                                 ('clients', 'Clients'),
                                 ('invoices', 'Factures')
                             ],
                             validators=[DataRequired()])
    csv_file = FileField('Fichier CSV', validators=[DataRequired(), FileAllowed(['csv'], 'Fichiers CSV seulement!')])
    submit = SubmitField('Importer')

class AgeCalculationForm(FlaskForm):
    """Form for choosing aging calculation method"""
    method = SelectField('Méthode de calcul',
                        choices=[
                            ('invoice_date', 'Date de facture'),
                            ('due_date', 'Date d\'échéance')
                        ],
                        default='invoice_date')
    submit = SubmitField('Appliquer')

class QuickBooksConnectionForm(FlaskForm):
    """Form for QuickBooks connection configuration"""
    system_name = StringField('Nom de la connexion',
                             validators=[DataRequired(), Length(max=100)],
                             default='QuickBooks Online')
    auto_sync = BooleanField('Synchronisation automatique', default=True)
    sync_frequency = SelectField('Fréquence de synchronisation',
                               choices=[
                                   ('hourly', 'Toutes les heures'),
                                   ('daily', 'Quotidienne'),
                                   ('weekly', 'Hebdomadaire'),
                                   ('manual', 'Manuelle uniquement')
                               ],
                               default='daily')

    # Sync settings
    sync_customers = BooleanField('Synchroniser les clients', default=True)
    sync_invoices = BooleanField('Synchroniser les factures', default=True)
    only_unpaid_invoices = BooleanField('Seulement les factures impayées', default=True)
    create_missing_clients = BooleanField('Créer les clients manquants', default=True)
    update_existing_clients = BooleanField('Mettre à jour les clients existants', default=True)

    submit = SubmitField('Sauvegarder la configuration')

class FieldMappingForm(FlaskForm):
    """Form for field mapping configuration"""
    # Client field mappings
    client_code_field = StringField('Champ code client QuickBooks',
                                   validators=[DataRequired()],
                                   default='Name')
    client_name_field = StringField('Champ nom client QuickBooks',
                                   validators=[DataRequired()],
                                   default='DisplayName')
    client_email_field = StringField('Champ email client QuickBooks',
                                    default='PrimaryEmailAddr.Address')
    client_phone_field = StringField('Champ téléphone client QuickBooks',
                                    default='PrimaryPhone.FreeFormNumber')

    # Invoice field mappings
    invoice_number_field = StringField('Champ numéro facture QuickBooks',
                                     validators=[DataRequired()],
                                     default='DocNumber')
    invoice_amount_field = StringField('Champ montant facture QuickBooks',
                                     validators=[DataRequired()],
                                     default='TotalAmt')
    invoice_date_field = StringField('Champ date facture QuickBooks',
                                   validators=[DataRequired()],
                                   default='TxnDate')
    invoice_due_date_field = StringField('Champ date échéance QuickBooks',
                                       validators=[DataRequired()],
                                       default='DueDate')

    submit = SubmitField('Sauvegarder le mapping')

class NoteForm(FlaskForm):
    """Form for creating and editing notes"""
    client_id = SelectField('Client', coerce=int, validators=[DataRequired(message='Veuillez sélectionner un client')])
    note_type = SelectField('Type de note',
                           choices=[
                               ('general', 'Général'),
                               ('call', 'Appel téléphonique'),
                               ('email', 'Courriel'),
                               ('meeting', 'Rencontre')
                           ],
                           validators=[DataRequired()])
    note_text = TextAreaField('Contenu', validators=[DataRequired(), Length(max=5000)], widget=TextArea())
    note_date = DateField('Date', validators=[DataRequired()], default=date.today)
    reminder_date = DateField('Date de rappel', validators=[Optional()])
    submit = SubmitField('Enregistrer')

class EmailNoteForm(FlaskForm):
    """Form for creating email notes"""
    client_id = SelectField('Client', coerce=int, validators=[DataRequired(message='Veuillez sélectionner un client')])
    email_from = StringField('De', validators=[DataRequired(), Email(), Length(max=255)])
    email_to = StringField('À', validators=[DataRequired(), Email(), Length(max=255)])
    email_subject = StringField('Sujet', validators=[DataRequired(), Length(max=500)])
    email_body = TextAreaField('Message', validators=[DataRequired()], widget=TextArea())
    submit = SubmitField('Créer courriel')

class EmailDetailForm(FlaskForm):
    """Form for viewing and adding notes to existing emails"""
    additional_note = TextAreaField('Note supplémentaire', validators=[Optional(), Length(max=5000)], widget=TextArea())
    reminder_date = DateField('Date de rappel', validators=[Optional()])
    submit = SubmitField('Enregistrer les modifications')

class CreateUserForm(FlaskForm):
    """Form for admin to create new users in their company"""
    user_type = SelectField('Type d\'utilisateur',
                           choices=[('new', 'Nouvel utilisateur'), ('existing', 'Utilisateur existant')],
                           default='new',
                           validators=[DataRequired()])
    first_name = StringField('Prénom', validators=[
        DataRequired(),
        Length(min=2, max=FIELD_LENGTH_NAME_MAX),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le prénom contient des caractères non autorisés')
    ])
    last_name = StringField('Nom', validators=[
        DataRequired(),
        Length(min=2, max=FIELD_LENGTH_NAME_MAX),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le nom contient des caractères non autorisés')
    ])
    email = StringField('Email', validators=[
        DataRequired(),
        Email(message='Format d\'email invalide'),
        Length(max=120),
        Regexp(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', message='Format d\'email invalide')
    ])
    password = PasswordField('Mot de passe temporaire', validators=[
        Optional(),
        Length(min=8, max=128, message='Le mot de passe doit contenir entre 8 et 128 caractères'),
        Regexp(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$', message='Le mot de passe doit contenir au moins une minuscule, une majuscule et un chiffre')
    ])
    role = SelectField('Rôle',
                      choices=[], # Sera rempli dynamiquement
                      default='employe')
    submit = SubmitField('Créer l\'utilisateur')

    def __init__(self, company_id=None, *args, **kwargs):
        super(CreateUserForm, self).__init__(*args, **kwargs)
        from utils.role_utils import get_role_choices
        self.role.choices = get_role_choices()
        self.company_id = company_id

    def validate_email(self, email):
        # Vérifier uniquement dans la même entreprise pour le système multi-entreprises
        if self.company_id:
            from models import User
            user = User.query.filter_by(email=email.data, company_id=self.company_id).first()
            if user:
                raise ValidationError('Cette adresse email est déjà utilisée dans cette entreprise.')

    def validate_password(self, password):
        """Validation pour le mot de passe en fonction du type d'utilisateur.
        Le mot de passe est optionnel - s'il n'est pas fourni, il sera auto-généré par le backend."""
        from models import User
        existing_user = User.query.filter_by(email=self.email.data).first() if self.email.data else None

        if self.user_type.data == 'new':
            if existing_user:
                raise ValidationError('Veuillez choisir "Utilisateur existant".')

        elif self.user_type.data == 'existing':
            if not existing_user:
                raise ValidationError('Veuillez choisir "Nouvel utilisateur".')
            password.data = None

class EditUserForm(FlaskForm):
    """Form for admin to edit existing users in their company"""
    first_name = StringField('Prénom', validators=[
        DataRequired(),
        Length(min=2, max=FIELD_LENGTH_NAME_MAX),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le prénom contient des caractères non autorisés')
    ])
    last_name = StringField('Nom', validators=[
        DataRequired(),
        Length(min=2, max=FIELD_LENGTH_NAME_MAX),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le nom contient des caractères non autorisés')
    ])
    email = StringField('Email', validators=[
        DataRequired(),
        Email(message='Format d\'email invalide'),
        Length(max=120),
        Regexp(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', message='Format d\'email invalide')
    ])
    role = SelectField('Rôle',
                      choices=[], # Sera rempli dynamiquement
                      default='employe')
    can_create_campaigns = BooleanField('Autoriser la création de campagnes')
    reset_password = BooleanField('Réinitialiser le mot de passe')
    new_password = PasswordField('Nouveau mot de passe temporaire', validators=[
        Optional(),
        Length(min=8, max=128, message='Le mot de passe doit contenir entre 8 et 128 caractères'),
        Regexp(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$', message='Le mot de passe doit contenir au moins une minuscule, une majuscule et un chiffre')
    ])
    submit = SubmitField('Mettre à jour l\'utilisateur')

    def __init__(self, original_email=None, company_id=None, *args, **kwargs):
        super(EditUserForm, self).__init__(*args, **kwargs)
        from utils.role_utils import get_role_choices
        self.role.choices = get_role_choices()
        self.original_email = original_email
        self.company_id = company_id

    def validate_email(self, email):
        if email.data != self.original_email and self.company_id:
            from models import User
            user = User.query.filter_by(email=email.data, company_id=self.company_id).first()
            if user:
                raise ValidationError('Cette adresse email est déjà utilisée dans cette entreprise.')

    def validate_new_password(self, new_password):
        if self.reset_password.data and not new_password.data:
            raise ValidationError('Veuillez saisir un nouveau mot de passe temporaire.')

class ClientContactForm(FlaskForm):
    """Form for client contact"""
    first_name = StringField('Prénom', validators=[
        DataRequired(),
        Length(min=2, max=100),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le prénom contient des caractères non autorisés')
    ])
    last_name = StringField('Nom', validators=[
        DataRequired(),
        Length(min=2, max=100),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le nom contient des caractères non autorisés')
    ])
    email = StringField('Courriel', validators=[
        DataRequired(),
        Email(message='Format d\'email invalide'),
        Length(max=120),
        Regexp(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', message='Format d\'email invalide')
    ])
    phone = StringField('Téléphone', validators=[Optional(), Length(max=50)])
    position = StringField('Fonction', validators=[Optional(), Length(max=100)])
    language = SelectField('Langue',
                          choices=[('fr', 'Français'), ('en', 'English')],
                          default='fr')
    is_primary = BooleanField('Contact principal')
    campaign_allowed = BooleanField('Campagne autorisée')


class UserProfileForm(FlaskForm):
    """Form for user profile settings"""
    first_name = StringField('Prénom', validators=[
        DataRequired(),
        Length(min=2, max=FIELD_LENGTH_NAME_MAX),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le prénom contient des caractères non autorisés')
    ])
    last_name = StringField('Nom', validators=[
        DataRequired(),
        Length(min=2, max=FIELD_LENGTH_NAME_MAX),
        Regexp(r'^[a-zA-ZÀ-ÿ\s\-\']+$', message='Le nom contient des caractères non autorisés')
    ])
    email = StringField('Courriel', validators=[
        DataRequired(),
        Email(message='Format d\'email invalide'),
        Length(max=120),
        Regexp(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', message='Format d\'email invalide')
    ])
    submit = SubmitField('Mettre à jour le profil')

class EmailConfigurationForm(FlaskForm):
    """Form for email configuration"""
    outlook_email = StringField('Adresse Outlook/Hotmail', validators=[Optional(), Email()])
    gmail_email = StringField('Adresse Gmail', validators=[Optional(), Email()])
    email_signature = TextAreaField('Signature Courriel', validators=[Optional()])

    submit = SubmitField('Sauvegarder')

class ForgotPasswordForm(FlaskForm):
    """Form for requesting password reset"""
    email = StringField('Adresse email', validators=[
        DataRequired(),
        Email(message='Format d\'email invalide'),
        Length(max=120),
        Regexp(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', message='Format d\'email invalide')
    ])
    submit = SubmitField('Envoyer le lien de réinitialisation')

class ResetPasswordForm(FlaskForm):
    """Form for resetting password with token"""
    password = PasswordField('Nouveau mot de passe', validators=[
        DataRequired(),
        Length(min=8, max=128, message='Le mot de passe doit contenir entre 8 et 128 caractères'),
        Regexp(r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,}$', message='Le mot de passe doit contenir au moins une minuscule, une majuscule et un chiffre')
    ])
    password2 = PasswordField('Confirmer le nouveau mot de passe',
                             validators=[DataRequired(), EqualTo('password', message='Les mots de passe doivent correspondre')])
    submit = SubmitField('Réinitialiser le mot de passe')

class SubscriptionForm(FlaskForm):
    """Form for subscription management"""
    plan_id = SelectField('Forfait', validators=[DataRequired()], coerce=int)
    # REFONTE STRIPE 2.0 : Architecture simplifiée
    quantity_licenses = IntegerField('Nombre de licences', validators=[DataRequired(), NumberRange(min=1)], default=1)
    submit = SubmitField('Souscrire')

    def __init__(self, *args, **kwargs):
        super(SubscriptionForm, self).__init__(*args, **kwargs)
        # Charger les forfaits actifs
        from models import Plan
        plans = Plan.query.filter_by(is_active=True).all()
        self.plan_id.choices = [(plan.id, f"{plan.display_name} - {plan.description}") for plan in plans]


