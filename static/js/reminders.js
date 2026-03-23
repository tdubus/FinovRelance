/**
 * Reminders Page JavaScript
 * Handles lazy loading, search, pagination, and modal interactions
 */

let currentPage = 1;
let currentFilter = 'all';
let searchDebounceTimer = null;

// Initialize the reminders page
window.initializeRemindersPage = function(filter) {
    currentFilter = filter || 'all';

    // Setup search input with debounce
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('input', function() {
            clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(() => {
                loadReminders(1);
            }, 300);
        });
    }

    // Setup clear search button
    const clearSearchBtn = document.getElementById('clearSearch');
    if (clearSearchBtn) {
        clearSearchBtn.addEventListener('click', function() {
            if (searchInput) {
                searchInput.value = '';
                loadReminders(1);
            }
        });
    }

    // Load initial reminders
    loadReminders(1);
};

// Load reminders from API
function loadReminders(page = 1) {
    const searchInput = document.getElementById('searchInput');
    const search = searchInput ? searchInput.value.trim() : '';

    // Build query parameters
    const params = new URLSearchParams({
        page: page,
        search: search,
        status: currentFilter
    });

    // Show loading state
    const container = document.getElementById('remindersContainer');
    const initialLoader = document.getElementById('initialLoader');

    if (container && page === 1) {
        container.style.opacity = '0.6';
        container.style.pointerEvents = 'none';
    }

    // Fetch data from API
    fetch(`/reminders/api/load?${params.toString()}`, {
        credentials: 'include'
    })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            updateRemindersContainer(data.reminders);
            updatePagination(data.pagination);
            updateTotalCount(data.pagination.total);
            updateCounts(data.counts);
            currentPage = data.pagination.page;

            // Hide initial loader and show container
            if (initialLoader) {
                initialLoader.style.display = 'none';
            }
            if (container) {
                container.classList.remove('d-none');
            }
        })
        .catch(error => {
            console.error('Error loading reminders:', error);
            showFlashMessage('Erreur lors du chargement des rappels', 'danger');
        })
        .finally(() => {
            // Remove loading state
            if (container) {
                container.style.opacity = '1';
                container.style.pointerEvents = 'auto';
            }
        });
}

// Update reminders container with data
function updateRemindersContainer(reminders) {
    const container = document.getElementById('remindersContainer');
    if (!container) return;

    container.innerHTML = '';

    if (reminders.length === 0) {
        container.innerHTML = `
            <div class="text-center py-5 text-muted">
                <i data-feather="bell" style="width: 48px; height: 48px;" class="mb-3"></i>
                <p>Aucun rappel trouvé</p>
                <a href="/clients" class="btn btn-outline-primary">
                    Gérer vos clients
                </a>
            </div>
        `;
        window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();
        return;
    }

    reminders.forEach(reminder => {
        const card = createReminderCard(reminder);
        container.appendChild(card);
    });

    // Re-initialize feather icons
    window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();

    // Attach event listeners
    attachReminderEventListeners();
}

// Create a reminder card element
function createReminderCard(reminder) {
    const card = document.createElement('div');
    card.className = `reminder-card mb-3 p-3 border rounded ${reminder.border_class}`;
    card.id = `reminder-${reminder.id}`;

    const row = document.createElement('div');
    row.className = 'row align-items-center';

    // Left column: Client info and note preview
    const leftCol = document.createElement('div');
    leftCol.className = 'col-md-8';

    // Client name and badge
    const headerDiv = document.createElement('div');
    headerDiv.className = 'd-flex align-items-center mb-2';

    const clientLink = document.createElement('a');
    clientLink.href = `/clients/${reminder.client_id}`;
    clientLink.className = 'text-decoration-none me-3';
    clientLink.style.fontSize = '1.1em';
    clientLink.style.fontWeight = '600';
    clientLink.textContent = `${reminder.client_code} - ${reminder.client_name}`;

    const badge = document.createElement('span');
    badge.className = `badge ${reminder.badge_class}`;
    badge.textContent = reminder.badge_text;

    headerDiv.appendChild(clientLink);
    headerDiv.appendChild(badge);

    // Reminder date
    const dateDiv = document.createElement('div');
    dateDiv.className = 'small mb-2';
    if (reminder.status === 'overdue') {
        dateDiv.className += ' text-danger';
    } else if (reminder.status === 'today') {
        dateDiv.className += ' text-warning';
    } else {
        dateDiv.className += ' text-info';
    }
    const calendarIcon = document.createElement('i');
    calendarIcon.setAttribute('data-feather', 'calendar');
    calendarIcon.className = 'me-1';
    dateDiv.appendChild(calendarIcon);
    dateDiv.appendChild(document.createTextNode(reminder.reminder_date));

    // Note preview
    const notePreviewContainer = document.createElement('div');
    notePreviewContainer.className = 'note-preview-container';

    const notePreview = document.createElement('div');
    notePreview.className = 'note-preview';
    notePreview.setAttribute('data-full-text', reminder.note_text);
    const truncatedText = reminder.note_text.length > 100
        ? reminder.note_text.substring(0, 100) + '...'
        : reminder.note_text;
    notePreview.textContent = truncatedText;

    notePreviewContainer.appendChild(notePreview);

    if (reminder.note_text.length > 100) {
        const readMoreBtn = document.createElement('button');
        readMoreBtn.className = 'btn btn-link btn-sm p-0 read-more-btn';
        readMoreBtn.textContent = 'Lire plus';
        notePreviewContainer.appendChild(readMoreBtn);
    }

    // Meta info (author, type, date)
    const metaSmall = document.createElement('small');
    metaSmall.className = 'text-muted';

    const userIcon = document.createElement('i');
    userIcon.setAttribute('data-feather', 'user');
    userIcon.className = 'me-1';
    metaSmall.appendChild(userIcon);
    metaSmall.appendChild(document.createTextNode(`${reminder.user_name} • `));

    const typeIcon = document.createElement('i');
    typeIcon.setAttribute('data-feather', reminder.type_icon);
    typeIcon.className = 'me-1';
    metaSmall.appendChild(typeIcon);
    metaSmall.appendChild(document.createTextNode(reminder.created_at));

    leftCol.appendChild(headerDiv);
    leftCol.appendChild(dateDiv);
    leftCol.appendChild(notePreviewContainer);
    leftCol.appendChild(metaSmall);

    // Right column: Action buttons
    const rightCol = document.createElement('div');
    rightCol.className = 'col-md-4 text-end';

    const btnGroup = document.createElement('div');
    btnGroup.className = 'btn-group-vertical btn-group-sm';

    // Complete/Reactivate button
    if (!reminder.is_reminder_completed) {
        const completeBtn = document.createElement('button');
        completeBtn.className = 'btn btn-outline-success complete-reminder-btn';
        completeBtn.setAttribute('data-reminder-id', reminder.id);
        const checkIcon = document.createElement('i');
        checkIcon.setAttribute('data-feather', 'check');
        checkIcon.className = 'me-1';
        completeBtn.appendChild(checkIcon);
        completeBtn.appendChild(document.createTextNode('Marquer terminé'));
        btnGroup.appendChild(completeBtn);
    } else {
        const reactivateBtn = document.createElement('button');
        reactivateBtn.className = 'btn btn-outline-primary reactivate-reminder-btn';
        reactivateBtn.setAttribute('data-reminder-id', reminder.id);
        const rotateIcon = document.createElement('i');
        rotateIcon.setAttribute('data-feather', 'rotate-ccw');
        rotateIcon.className = 'me-1';
        reactivateBtn.appendChild(rotateIcon);
        reactivateBtn.appendChild(document.createTextNode('Réactiver'));
        btnGroup.appendChild(reactivateBtn);
    }

    // Delete button
    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn btn-outline-danger delete-reminder-btn';
    deleteBtn.setAttribute('data-reminder-id', reminder.id);
    const trashIcon = document.createElement('i');
    trashIcon.setAttribute('data-feather', 'trash-2');
    trashIcon.className = 'me-1';
    deleteBtn.appendChild(trashIcon);
    deleteBtn.appendChild(document.createTextNode('Supprimer'));
    btnGroup.appendChild(deleteBtn);

    // View client button
    const viewClientBtn = document.createElement('a');
    viewClientBtn.href = `/clients/${reminder.client_id}`;
    viewClientBtn.className = 'btn btn-outline-primary';
    const externalIcon = document.createElement('i');
    externalIcon.setAttribute('data-feather', 'external-link');
    externalIcon.className = 'me-1';
    viewClientBtn.appendChild(externalIcon);
    viewClientBtn.appendChild(document.createTextNode('Voir client'));
    btnGroup.appendChild(viewClientBtn);

    // Edit button
    const editBtn = document.createElement('button');
    editBtn.className = 'btn btn-outline-warning edit-reminder-btn';
    editBtn.setAttribute('data-reminder-id', reminder.id);
    const editIcon = document.createElement('i');
    editIcon.setAttribute('data-feather', 'edit');
    editIcon.className = 'me-1';
    editBtn.appendChild(editIcon);
    editBtn.appendChild(document.createTextNode('Modifier'));
    btnGroup.appendChild(editBtn);

    rightCol.appendChild(btnGroup);

    row.appendChild(leftCol);
    row.appendChild(rightCol);
    card.appendChild(row);

    return card;
}

// Attach event listeners to reminder cards
function attachReminderEventListeners() {
    // Read more buttons
    document.querySelectorAll('.read-more-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const previewDiv = this.previousElementSibling;
            const fullText = previewDiv.getAttribute('data-full-text');
            if (previewDiv.textContent === fullText) {
                const truncatedText = fullText.length > 100 ? fullText.substring(0, 100) + '...' : fullText;
                previewDiv.textContent = truncatedText;
                this.textContent = 'Lire plus';
            } else {
                previewDiv.textContent = fullText;
                this.textContent = 'Lire moins';
            }
        });
    });

    // Complete buttons
    document.querySelectorAll('.complete-reminder-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const reminderId = this.getAttribute('data-reminder-id');
            completeReminder(reminderId);
        });
    });

    // Reactivate buttons
    document.querySelectorAll('.reactivate-reminder-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const reminderId = this.getAttribute('data-reminder-id');
            reactivateReminder(reminderId);
        });
    });

    // Delete buttons
    document.querySelectorAll('.delete-reminder-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const reminderId = this.getAttribute('data-reminder-id');
            if (confirm('Êtes-vous sûr de vouloir supprimer ce rappel ?')) {
                deleteReminder(reminderId);
            }
        });
    });

    // Edit buttons
    document.querySelectorAll('.edit-reminder-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const reminderId = this.getAttribute('data-reminder-id');
            editReminder(reminderId);
        });
    });
}

// Update pagination
function updatePagination(pagination) {
    const paginationContainer = document.getElementById('paginationContainer');
    if (!paginationContainer) return;

    paginationContainer.innerHTML = '';

    if (pagination.pages <= 1) {
        return;
    }

    const ul = document.createElement('ul');
    ul.className = 'pagination justify-content-center mb-0';

    // Previous button
    if (pagination.has_prev) {
        const prevLi = document.createElement('li');
        prevLi.className = 'page-item';
        const prevLink = document.createElement('a');
        prevLink.className = 'page-link';
        prevLink.href = '#';
        prevLink.textContent = 'Précédent';
        prevLink.onclick = (e) => {
            e.preventDefault();
            loadPage(pagination.page - 1);
        };
        prevLi.appendChild(prevLink);
        ul.appendChild(prevLi);
    }

    // Page numbers
    for (let i = 1; i <= pagination.pages; i++) {
        const pageLi = document.createElement('li');
        pageLi.className = 'page-item';
        if (i === pagination.page) {
            pageLi.classList.add('active');
        }
        const pageLink = document.createElement('a');
        pageLink.className = 'page-link';
        pageLink.href = '#';
        pageLink.textContent = i.toString();
        pageLink.onclick = (e) => {
            e.preventDefault();
            loadPage(i);
        };
        pageLi.appendChild(pageLink);
        ul.appendChild(pageLi);
    }

    // Next button
    if (pagination.has_next) {
        const nextLi = document.createElement('li');
        nextLi.className = 'page-item';
        const nextLink = document.createElement('a');
        nextLink.className = 'page-link';
        nextLink.href = '#';
        nextLink.textContent = 'Suivant';
        nextLink.onclick = (e) => {
            e.preventDefault();
            loadPage(pagination.page + 1);
        };
        nextLi.appendChild(nextLink);
        ul.appendChild(nextLi);
    }

    paginationContainer.appendChild(ul);
}

// Load a specific page
function loadPage(page) {
    loadReminders(page);
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// Update total count badge
function updateTotalCount(total) {
    const totalCountEl = document.getElementById('totalCount');
    if (totalCountEl) {
        totalCountEl.textContent = total;
    }
}

// Update sidebar counts
function updateCounts(counts) {
    // Update badge counts in sidebar (if they exist in DOM)
    const sidebar = document.querySelector('.col-md-3');
    if (!sidebar) return;

    const badges = sidebar.querySelectorAll('.badge');
    if (badges.length >= 5) {
        badges[0].textContent = counts.total;
        badges[1].textContent = counts.overdue;
        badges[2].textContent = counts.today;
        badges[3].textContent = counts.upcoming;
        badges[4].textContent = counts.completed;
    }
}

// Complete a reminder
function completeReminder(reminderId) {
    fetch(`/reminders/complete/${reminderId}`, {
        method: 'POST',
        credentials: 'include',
        headers: {
            'X-CSRFToken': getCSRFToken(),
            'Content-Type': 'application/x-www-form-urlencoded',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showFlashMessage(data.message, 'success');
            loadReminders(currentPage);
        } else {
            showFlashMessage(data.message || 'Erreur lors de la mise à jour', 'danger');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showFlashMessage('Erreur lors de la mise à jour du rappel', 'danger');
    });
}

// Reactivate a reminder
function reactivateReminder(reminderId) {
    fetch(`/reminders/reactivate/${reminderId}`, {
        method: 'POST',
        credentials: 'include',
        headers: {
            'X-CSRFToken': getCSRFToken(),
            'Content-Type': 'application/x-www-form-urlencoded',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showFlashMessage(data.message, 'success');
            loadReminders(currentPage);
        } else {
            showFlashMessage(data.message || 'Erreur lors de la réactivation', 'danger');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showFlashMessage('Erreur lors de la réactivation du rappel', 'danger');
    });
}

// Delete a reminder
function deleteReminder(reminderId) {
    fetch(`/reminders/delete/${reminderId}`, {
        method: 'POST',
        credentials: 'include',
        headers: {
            'X-CSRFToken': getCSRFToken(),
            'Content-Type': 'application/x-www-form-urlencoded',
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showFlashMessage(data.message, 'success');
            loadReminders(currentPage);
        } else {
            showFlashMessage(data.message || 'Erreur lors de la suppression', 'danger');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        showFlashMessage('Erreur lors de la suppression du rappel', 'danger');
    });
}

// Edit reminder - fetch data and open appropriate modal
function editReminder(reminderId) {
    fetch(`/reminders/api/${reminderId}`, {
        credentials: 'include',
        headers: {
            'X-CSRFToken': getCSRFToken()
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            // Check if this is a real email or a regular note
            if (data.is_real_email) {
                openEditEmailNoteModal(data);
            } else {
                openEditNormalNoteModal(data);
            }
        } else {
            showFlashMessage(data.error || 'Erreur lors du chargement', 'danger');
        }
    })
    .catch(error => {
        console.error('Error loading reminder data:', error);
        showFlashMessage('Erreur lors du chargement des données', 'danger');
    });
}

// Open edit modal for normal notes/reminders
function openEditNormalNoteModal(data) {
    // Populate modal fields
    document.getElementById('editReminderId').value = data.id;
    document.getElementById('editNoteText').value = data.note_text || '';
    document.getElementById('editClientName').textContent = data.client_name;

    if (data.reminder_date) {
        document.getElementById('editReminderDate').value = data.reminder_date;
    } else {
        document.getElementById('editReminderDate').value = '';
    }

    // Set form action
    document.getElementById('editReminderForm').action = `/reminders/${data.id}/edit`;

    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('editReminderModal'));
    modal.show();
}

// Open edit modal for email reminders (with email content in read-only)
function openEditEmailNoteModal(data) {
    // Populate read-only email fields
    document.getElementById('emailNoteClient').textContent = data.client_display || `${data.client_code} - ${data.client_name}`;
    document.getElementById('emailNoteDate').textContent = data.created_at;
    document.getElementById('emailNoteFrom').textContent = data.email_from || 'N/A';
    document.getElementById('emailNoteTo').textContent = data.email_to || 'N/A';
    document.getElementById('emailNoteSubject').textContent = data.email_subject || 'Sans sujet';

    // Populate email body (sanitized to prevent XSS)
    const bodyDiv = document.getElementById('emailNoteBody');
    if (data.email_body_html) {
        // Use sanitizeHTML if available (defined in the template), otherwise fallback to textContent
        if (typeof sanitizeHTML === 'function') {
            bodyDiv.innerHTML = sanitizeHTML(data.email_body_html);
        } else {
            console.warn('sanitizeHTML not available, displaying as text');
            bodyDiv.textContent = data.email_body_html;
        }
    } else if (data.email_body) {
        bodyDiv.textContent = data.email_body;
    } else {
        bodyDiv.textContent = 'Aucun contenu';
    }

    // Populate attachments if present
    const attachmentsContainer = document.getElementById('emailNoteAttachments');
    const attachmentsList = document.getElementById('emailNoteAttachmentsList');

    if (data.attachments && data.attachments.length > 0) {
        attachmentsList.innerHTML = '';
        data.attachments.forEach(att => {
            const li = document.createElement('li');
            const icon = document.createElement('i');
            icon.className = 'fas fa-paperclip me-1';
            li.appendChild(icon);
            li.appendChild(document.createTextNode(`${att.filename} `));
            const small = document.createElement('small');
            small.className = 'text-muted';
            small.textContent = `(${formatFileSize(att.size)})`;
            li.appendChild(small);
            attachmentsList.appendChild(li);
        });
        attachmentsContainer.style.display = 'block';
    } else {
        attachmentsContainer.style.display = 'none';
    }

    // Populate editable fields
    document.getElementById('editEmailNoteId').value = data.id;
    document.getElementById('emailNoteAdditional').value = data.note_text || '';
    document.getElementById('emailNoteReminder').value = data.reminder_date || '';

    // Set form action
    document.getElementById('editEmailNoteForm').action = `/reminders/${data.id}/edit`;

    // Show modal
    const modal = new bootstrap.Modal(document.getElementById('editEmailNoteModal'));
    modal.show();
}

// Save edited reminder (normal note)
function saveEditedReminder() {
    const form = document.getElementById('editReminderForm');
    const formData = new FormData(form);
    const reminderId = document.getElementById('editReminderId').value;

    fetch(`/reminders/${reminderId}/edit`, {
        method: 'POST',
        credentials: 'include',
        headers: {
            'X-CSRFToken': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showFlashMessage(data.message || 'Rappel modifié avec succès', 'success');
            const modal = bootstrap.Modal.getInstance(document.getElementById('editReminderModal'));
            if (modal) modal.hide();
            loadReminders(currentPage);
        } else {
            showFlashMessage(data.error || 'Erreur lors de la modification', 'danger');
        }
    })
    .catch(error => {
        console.error('Error saving reminder:', error);
        showFlashMessage('Erreur lors de la sauvegarde', 'danger');
    });
}

// Save edited email note
function saveEditedEmailNote() {
    const form = document.getElementById('editEmailNoteForm');
    const formData = new FormData(form);
    const noteId = document.getElementById('editEmailNoteId').value;

    fetch(`/reminders/${noteId}/edit`, {
        method: 'POST',
        credentials: 'include',
        headers: {
            'X-CSRFToken': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showFlashMessage(data.message || 'Note modifiée avec succès', 'success');
            const modal = bootstrap.Modal.getInstance(document.getElementById('editEmailNoteModal'));
            if (modal) modal.hide();
            loadReminders(currentPage);
        } else {
            showFlashMessage(data.error || 'Erreur lors de la modification', 'danger');
        }
    })
    .catch(error => {
        console.error('Error saving email note:', error);
        showFlashMessage('Erreur lors de la sauvegarde', 'danger');
    });
}

// Helper: Format file size
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Helper: Get CSRF token
function getCSRFToken() {
    const metaTag = document.querySelector('meta[name="csrf-token"]');
    return metaTag ? metaTag.getAttribute('content') : '';
}

// Helper: Show flash message
function showFlashMessage(message, type) {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    const messageText = document.createTextNode(message);
    alertDiv.appendChild(messageText);
    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'btn-close';
    closeButton.setAttribute('data-bs-dismiss', 'alert');
    alertDiv.appendChild(closeButton);

    const container = document.querySelector('.container-fluid') || document.body;
    container.insertBefore(alertDiv, container.firstChild);

    setTimeout(() => {
        alertDiv.remove();
    }, 5000);
}

// Make saveEditedReminder and saveEditedEmailNote available globally
window.saveEditedReminder = saveEditedReminder;
window.saveEditedEmailNote = saveEditedEmailNote;
