// Global alert function - FIXED XSS vulnerability
function showAlert(message, type = 'info', duration = 5000) {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show position-fixed`;
    alertDiv.style.top = '20px';
    alertDiv.style.right = '20px';
    alertDiv.style.zIndex = '9999';
    alertDiv.style.minWidth = '300px';

    // Create message text safely
    const messageSpan = document.createElement('span');
    messageSpan.textContent = message; // Safe - textContent escapes automatically

    // Create close button safely
    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'btn-close';
    closeButton.setAttribute('data-bs-dismiss', 'alert');

    alertDiv.appendChild(messageSpan);
    alertDiv.appendChild(closeButton);
    document.body.appendChild(alertDiv);

    // Auto-remove after duration
    setTimeout(() => {
        if (alertDiv.parentNode) {
            alertDiv.remove();
        }
    }, duration);
}

// Mode développeur - détection automatique des outils de développement
let devToolsOpen = false;
let devModeActive = false;

// Détecter si les outils de développement sont ouverts
function detectDevTools() {
    const threshold = 160;
    if (window.outerHeight - window.innerHeight > threshold ||
        window.outerWidth - window.innerWidth > threshold) {
        if (!devToolsOpen) {
            devToolsOpen = true;
            devModeActive = true;
            disableBackgroundRequests();
        }
    } else {
        if (devToolsOpen) {
            devToolsOpen = false;
        }
    }
}

// Désactiver toutes les requêtes en arrière-plan
function disableBackgroundRequests() {
    // Arrêter tous les timers existants
    for (let i = 1; i < 99999; i++) window.clearInterval(i);
    for (let i = 1; i < 99999; i++) window.clearTimeout(i);
}

// Fonction safeFetch améliorée avec détection dev tools
function safeFetch(url, options = {}) {
    // Si mode dev actif, annuler les requêtes non critiques
    if (devModeActive && !options.critical) {
        return Promise.reject(new Error('Request blocked in dev mode'));
    }

    const defaultOptions = {
        timeout: 5000, // Timeout réduit
        retries: 1, // Retry réduit
        ...options
    };

    return new Promise((resolve, reject) => {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), defaultOptions.timeout);

        fetch(url, {
            ...defaultOptions,
            signal: controller.signal,
            credentials: 'same-origin'
        })
        .then(response => {
            clearTimeout(timeoutId);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            resolve(response);
        })
        .catch(error => {
            clearTimeout(timeoutId);
            reject(error);
        });
    });
}

// Initialisation du mode développeur
document.addEventListener('DOMContentLoaded', function() {
    // Détecter les dev tools au chargement
    detectDevTools();

    // Surveiller les changements de taille de fenêtre
    setInterval(detectDevTools, 1000);
});

// Utility functions
function checkUserExists(email) {
    const csrfToken = document.querySelector('meta[name=csrf-token]')?.getAttribute('content');

    safeFetch('/api/check-user-exists', {
        method: 'POST',
        critical: true, // Marque comme critique
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify({email: email})
    })
    .then(response => response.json())
    .then(data => {
        const messageDiv = document.getElementById('userExistsMessage');
        if (!messageDiv) return;

        if (data.exists) {
            // Clear previous content
            messageDiv.innerHTML = '';

            if (data.already_in_company) {
                // Create warning alert safely
                const alertDiv = document.createElement('div');
                alertDiv.className = 'alert alert-warning';
                alertDiv.textContent = 'Cet utilisateur fait déjà partie de cette entreprise.';
                messageDiv.appendChild(alertDiv);
            } else {
                // Create info alert safely
                const alertDiv = document.createElement('div');
                alertDiv.className = 'alert alert-info';
                alertDiv.textContent = `Utilisateur existant trouvé: ${data.user_info.first_name} ${data.user_info.last_name}. Il sera ajouté à cette entreprise.`;
                messageDiv.appendChild(alertDiv);

                // Pre-fill form fields
                const firstNameField = document.getElementById('first_name');
                const lastNameField = document.getElementById('last_name');

                if (firstNameField) firstNameField.value = data.user_info.first_name;
                if (lastNameField) lastNameField.value = data.user_info.last_name;
            }
        } else {
            messageDiv.innerHTML = '';
        }
    })
    .catch(error => {
        console.error('Error checking user:', error);
        const messageDiv = document.getElementById('userExistsMessage');
        if (messageDiv) {
            // Create error alert safely
            messageDiv.innerHTML = ''; // Clear first
            const alertDiv = document.createElement('div');
            alertDiv.className = 'alert alert-warning';
            alertDiv.textContent = 'Erreur de connexion. Veuillez réessayer.';
            messageDiv.appendChild(alertDiv);
        }
    });
}

// Email template functions
function loadEmailTemplate(templateId, clientId = null) {
    let url = `/api/email-template/${templateId}`;
    if (clientId) {
        url += `?client_id=${clientId}`;
    }

    safeFetch(url)
    .then(response => response.json())
    .then(data => {
        if (!data.success) {
            showAlert('Erreur lors du chargement du modèle: ' + data.message, 'danger');
            return;
        }

        const subjectField = document.getElementById('subject');
        const contentField = document.getElementById('content');

        if (subjectField) subjectField.value = data.subject;
        if (contentField) contentField.value = data.content;

        if (!window.quill) return;

        try {
            window.quill.setContents([]);
            window.quill.clipboard.dangerouslyPasteHTML(0, data.content);
        } catch (error) {
            console.warn('Error updating Quill content:', error);
            window.quill.setText(data.content || '');
        }
    })
    .catch(error => {
        console.error('Error loading template:', error);
        showAlert('Erreur de connexion lors du chargement du modèle', 'warning');
    });
}

// SMTP and Microsoft testing functions
function testSmtpConnection() {
    const form = document.getElementById('settingsForm');
    if (!form) return;

    const formData = new FormData(form);
    const data = Object.fromEntries(formData.entries());
    const csrfToken = document.querySelector('meta[name=csrf-token]')?.getAttribute('content');

    const btn = document.getElementById('testSmtpBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Test en cours...';
    }

    fetch('/company/test-smtp', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
        } else {
            showAlert(data.message, 'danger');
        }
    })
    .catch(error => {
        console.error('Error testing SMTP:', error);
        showAlert('Erreur lors du test SMTP', 'danger');
    })
    .finally(() => {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Tester SMTP';
        }
    });
}

function testMicrosoftConnection() {
    const btn = document.getElementById('testMicrosoftBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Test en cours...';
    }

    fetch('/company/microsoft/test')
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
        } else {
            showAlert(data.message, 'danger');
        }
    })
    .catch(error => {
        console.error('Error testing Microsoft:', error);
        showAlert('Erreur lors du test Microsoft', 'danger');
    })
    .finally(() => {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Tester Microsoft';
        }
    });
}

// Note and reminder functions
// Note: editNote() and saveNoteChanges() functions removed
// These are now handled in individual page templates (detail.html, notes/list.html)

function deleteNote(noteId, clientId) {
    if (!confirm('Êtes-vous sûr de vouloir supprimer cette note ?')) {
        return;
    }

    const csrfToken = document.querySelector('meta[name=csrf-token]')?.getAttribute('content');

    fetch(`/clients/${clientId}/notes/${noteId}/delete`, {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            location.reload();
        } else {
            showAlert(data.message, 'danger');
        }
    })
    .catch(error => {
        console.error('Error deleting note:', error);
        showAlert('Erreur lors de la suppression', 'danger');
    });
}

// Reminder functions
function completeReminder(reminderId) {
    const csrfToken = document.querySelector('meta[name=csrf-token]')?.getAttribute('content');

    fetch(`/reminders/${reminderId}/complete`, {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            location.reload();
        } else {
            showAlert(data.message, 'danger');
        }
    })
    .catch(error => {
        console.error('Error completing reminder:', error);
        showAlert('Erreur lors de la completion', 'danger');
    });
}

function deleteReminder(reminderId) {
    if (!confirm('Êtes-vous sûr de vouloir supprimer ce rappel ?')) {
        return;
    }

    const csrfToken = document.querySelector('meta[name=csrf-token]')?.getAttribute('content');

    fetch(`/reminders/${reminderId}/delete`, {
        method: 'POST',
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            location.reload();
        } else {
            showAlert(data.message, 'danger');
        }
    })
    .catch(error => {
        console.error('Error deleting reminder:', error);
        showAlert('Erreur lors de la suppression', 'danger');
    });
}

// Form initialization
function initializeForms() {
    // Email template selector
    const templateSelect = document.getElementById('template_id');
    if (templateSelect) {
        templateSelect.addEventListener('change', function() {
            try {
                if (this.value) {
                    const clientId = this.dataset.clientId || null;
                    loadEmailTemplate(this.value, clientId);
                }
            } catch (error) {
                console.error('Error loading email template:', error);
            }
        });
    }

    // Form submissions
    // Note: editNoteForm submission is now handled in individual page templates

    const editReminderForm = document.getElementById('editReminderForm');
    if (editReminderForm) {
        editReminderForm.addEventListener('submit', function(e) {
            e.preventDefault();
            try {
                saveReminderChanges();
            } catch (error) {
                console.error('Error saving reminder:', error);
            }
        });
    }

    // User check on email input
    const emailInput = document.getElementById('email');
    if (emailInput) {
        emailInput.addEventListener('blur', function() {
            if (this.value) {
                checkUserExists(this.value);
            }
        });
    }
}

// Vérification de la connectivité réseau
function checkNetworkConnection() {
    return navigator.onLine;
}

// Gestionnaire pour les changements de connectivité
window.addEventListener('online', function() {
    showAlert('Connexion Internet rétablie', 'success', 3000);
});

window.addEventListener('offline', function() {
    showAlert('Connexion Internet perdue', 'warning', 5000);
});

// Document ready
document.addEventListener('DOMContentLoaded', function() {

    try {
        initializeForms();

        // Initialize Feather icons if available
        if (typeof window.safeFeatherReplace === 'function') window.safeFeatherReplace();

        // Initialize Bootstrap tooltips
        const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
        if (window.bootstrap) {
            tooltipTriggerList.map(function (tooltipTriggerEl) {
                return new bootstrap.Tooltip(tooltipTriggerEl);
            });
        }

        // Vérifier la connectivité initiale
        if (!checkNetworkConnection()) {
            showAlert('Pas de connexion Internet détectée', 'warning');
        }

    } catch (error) {
        console.error('Error during initialization:', error);
    }
});

// Global functions accessible from templates
window.showAlert = showAlert;
window.loadEmailTemplate = loadEmailTemplate;
window.testSmtpConnection = testSmtpConnection;
window.testMicrosoftConnection = testMicrosoftConnection;
window.deleteNote = deleteNote;
window.completeReminder = completeReminder;
window.deleteReminder = deleteReminder;