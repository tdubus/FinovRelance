/**
 * Notes Management JavaScript
 * Handles dynamic search, filters, AJAX operations for notes and emails
 */

// Get CSRF token from meta tag
function getCSRFToken() {
    return document.querySelector('meta[name="csrf-token"]').getAttribute('content');
}

/**
 * Sanitize HTML content using DOMPurify to prevent XSS attacks
 * Use this for USER-CONTROLLED content (email bodies, templates, user input)
 * @param {string} html - The HTML content to sanitize
 * @returns {string} - Sanitized HTML safe for innerHTML
 */
function sanitizeHTML(html) {
    if (!html) return '';
    
    // Check if DOMPurify is available
    if (typeof DOMPurify === 'undefined') {
        console.error('DOMPurify is not loaded. Falling back to textContent.');
        const div = document.createElement('div');
        div.textContent = html;
        return div.innerHTML;
    }
    
    // STRICT configuration for user-controlled content
    // Security: NO event handlers, NO buttons/inputs/forms to prevent XSS and form hijacking
    const config = {
        ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'u', 'b', 'i', 'ul', 'ol', 'li', 'a', 'span', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code', 'img', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'small', 'label'],
        ALLOWED_ATTR: ['href', 'target', 'style', 'class', 'src', 'alt', 'width', 'height', 'title', 'colspan', 'rowspan', 'type', 'aria-label', 'aria-hidden', 'id'],
        ALLOW_DATA_ATTR: false,
        FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'select', 'textarea'],
        FORBID_ATTR: ['onclick', 'ondblclick', 'onmousedown', 'onmouseup', 'onmouseover', 'onmouseout', 'onmousemove', 'onkeydown', 'onkeyup', 'onkeypress', 'onfocus', 'onblur', 'onchange', 'onsubmit', 'onreset', 'onload', 'onerror', 'onabort', 'onscroll', 'onresize', 'onunload', 'onbeforeunload']
    };
    
    return DOMPurify.sanitize(html, config);
}

/**
 * Sanitize server-rendered HTML (from Flask templates)
 * Uses STRICT sanitization - NO event handlers allowed, uses data attributes for actions
 * Event handlers are attached via event delegation after insertion
 * @param {string} html - The HTML content from server
 * @returns {string} - Sanitized HTML
 */
function sanitizeTrustedHTML(html) {
    if (!html) return '';
    
    if (typeof DOMPurify === 'undefined') {
        console.error('DOMPurify not loaded for sanitizeTrustedHTML');
        return '';
    }
    
    // SECURE config - NO event handlers, uses data-action attributes instead
    // Event delegation handles clicks based on data-action and data-note-id
    const config = {
        ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'u', 'b', 'i', 'ul', 'ol', 'li', 'a', 'span', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code', 'img', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'small', 'button', 'label'],
        ALLOWED_ATTR: ['href', 'target', 'style', 'class', 'src', 'alt', 'width', 'height', 'title', 'colspan', 'rowspan', 'type', 'aria-label', 'aria-hidden', 'id', 'name', 'value', 'disabled', 'checked'],
        ALLOW_DATA_ATTR: true,
        FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'select', 'textarea'],
        FORBID_ATTR: ['onclick', 'ondblclick', 'onmousedown', 'onmouseup', 'onmouseover', 'onmouseout', 'onmousemove', 'onkeydown', 'onkeyup', 'onkeypress', 'onfocus', 'onblur', 'onchange', 'onsubmit', 'onreset', 'onload', 'onerror', 'onabort', 'onscroll', 'onresize', 'onunload', 'onbeforeunload']
    };
    
    return DOMPurify.sanitize(html, config);
}

/**
 * Toggle conversation child rows in the Notes table
 * Uses data-conversation-id attribute to handle special characters in IDs
 * Loads children dynamically if not present in the DOM
 */
function toggleConversation(badge) {
    const conversationId = badge.getAttribute('data-conversation-id');
    if (!conversationId) return;
    
    // Find parent row to get parent_note_id
    const parentRow = badge.closest('tr');
    const parentNoteId = badge.getAttribute('data-note-parent-id') || (parentRow ? parentRow.getAttribute('data-note-id') : null);
    
    // Find the container row by parent note ID (unique) to avoid collision
    // when two groups share the same conversation_id
    var containerRow;
    if (parentNoteId) {
        containerRow = document.querySelector('tr.conversation-children-container[data-parent-note-id="' + parentNoteId + '"]');
    }
    if (!containerRow) {
        containerRow = document.querySelector('tr.conversation-children-container[data-conversation-id="' + conversationId + '"]');
    }
    const containerWrapper = containerRow ? containerRow.querySelector('.conversation-children-wrapper') : null;
    
    // Check if already loaded
    const isLoaded = badge.hasAttribute('data-loaded');
    const chevron = badge.querySelector('.conv-chevron');
    
    // If not loaded yet, fetch children
    if (!isLoaded && !badge.hasAttribute('data-loading')) {
        badge.setAttribute('data-loading', 'true');
        
        // Show loading state
        if (chevron) {
            chevron.classList.remove('fa-chevron-down', 'fa-chevron-up');
            chevron.classList.add('fa-spinner', 'fa-spin');
        }
        
        // Build URL with parameters
        const url = '/notes/api/conversation_children?' + 
            'conversation_id=' + encodeURIComponent(conversationId) +
            (parentNoteId ? '&parent_note_id=' + parentNoteId : '');
        
        fetch(url, {
            method: 'GET',
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        })
        .then(function(response) { return response.json(); })
        .then(function(data) {
            badge.removeAttribute('data-loading');
            
            if (data.html && data.count > 0) {
                // Insert the HTML into the container wrapper
                if (containerWrapper) {
                    containerWrapper.innerHTML = sanitizeTrustedHTML(data.html);
                }
                
                // Show the container row
                if (containerRow) {
                    containerRow.style.display = 'table-row';
                }
                
                // Update chevron
                if (chevron) {
                    chevron.classList.remove('fa-spinner', 'fa-spin');
                    chevron.classList.add('fa-chevron-up');
                }
                
                // Mark as loaded
                badge.setAttribute('data-loaded', 'true');
            } else {
                // No children found, restore chevron
                if (chevron) {
                    chevron.classList.remove('fa-spinner', 'fa-spin');
                    chevron.classList.add('fa-chevron-down');
                }
            }
        })
        .catch(function(error) {
            console.error('Error loading conversation children:', error);
            badge.removeAttribute('data-loading');
            if (chevron) {
                chevron.classList.remove('fa-spinner', 'fa-spin');
                chevron.classList.add('fa-chevron-down');
            }
        });
        
        return;
    }
    
    // If already loaded, toggle visibility of the container row
    if (containerRow) {
        const isCurrentlyShown = containerRow.style.display !== 'none';
        containerRow.style.display = isCurrentlyShown ? 'none' : 'table-row';
        
        // Update chevron icon
        if (chevron) {
            if (isCurrentlyShown) {
                chevron.classList.remove('fa-chevron-up');
                chevron.classList.add('fa-chevron-down');
            } else {
                chevron.classList.remove('fa-chevron-down');
                chevron.classList.add('fa-chevron-up');
            }
        }
    }
}

/**
 * Setup event delegation for conversation children
 * Handles clicks on dynamically loaded content using data-action attributes
 * Security: This replaces inline onclick handlers to prevent XSS
 */
function setupConversationToggle() {
    // Event delegation for conversation thread actions
    document.addEventListener('click', function(e) {
        // Find the closest element with a data-action attribute
        const actionElement = e.target.closest('[data-action]');
        if (!actionElement) return;
        
        const action = actionElement.dataset.action;
        const noteId = actionElement.dataset.noteId;
        
        // Only handle actions within conversation threads
        const isInThread = actionElement.closest('.conversation-thread') || 
                          actionElement.closest('.conversation-children-container');
        if (!isInThread) return;
        
        switch (action) {
            case 'view':
                // View note - but not if clicking on action buttons
                if (!e.target.closest('.child-note-actions')) {
                    if (noteId) viewNote(parseInt(noteId, 10));
                }
                break;
            case 'edit':
                e.stopPropagation();
                if (noteId) editNote(parseInt(noteId, 10));
                break;
            case 'delete':
                e.stopPropagation();
                if (noteId) confirmDelete(parseInt(noteId, 10));
                break;
        }
    });
}

// Initialize page
function initializeNotesPage() {
    // Initialize client search for note modal
    initializeClientSearch('noteClientSearch', 'noteClient', 'noteClientDropdown');
    initializeClientSearch('emailClientSearch', 'emailClient', 'emailClientDropdown');
    
    // Setup conversation toggle for table rows (Bootstrap collapse doesn't work well with multiple tr targets)
    setupConversationToggle();
    
    // Setup search input with debounce (300ms)
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('input', window.debounce(function() {
            loadNotes();
        }, 300));
    }
    
    // Setup filter changes
    const collectorFilter = document.getElementById('collectorFilter');
    const typeFilter = document.getElementById('typeFilter');
    
    if (collectorFilter) {
        collectorFilter.addEventListener('change', function() {
            loadNotes();
        });
    }
    
    if (typeFilter) {
        typeFilter.addEventListener('change', function() {
            loadNotes();
        });
    }
    
    // Setup clear filters button
    const clearFiltersBtn = document.getElementById('clearFilters');
    if (clearFiltersBtn) {
        clearFiltersBtn.addEventListener('click', function() {
            searchInput.value = '';
            if (collectorFilter) collectorFilter.value = '';
            if (typeFilter) typeFilter.value = '';
            loadNotes();
        });
    }
    
    // Setup modal events
    const noteModal = document.getElementById('noteModal');
    if (noteModal) {
        // Only reset when explicitly creating a new note (not when editing)
        noteModal.addEventListener('shown.bs.modal', function(event) {
            // Check if we're in edit mode (noteId has a value)
            const noteId = document.getElementById('noteId').value;
            if (!noteId) {
                // We're creating a new note, so reset is already done by openNewNoteModal
            }
        });
    }
    
    const emailModal = document.getElementById('emailModal');
    if (emailModal) {
        // Event: Modal hidden (after closing) - Reset everything
        emailModal.addEventListener('hidden.bs.modal', function() {
            console.log('[Email Modal] Modal hidden - resetting form');
            
            // Reset form fields
            const form = document.getElementById('emailForm');
            if (form) {
                form.reset();
            }
            
            // Clear text fields explicitly
            const emailTo = document.getElementById('emailTo');
            const emailCc = document.getElementById('emailCc');
            const emailSubject = document.getElementById('emailSubject');
            if (emailTo) emailTo.value = '';
            if (emailCc) emailCc.value = '';
            if (emailSubject) emailSubject.value = '';
            
            // Clear Quill editor content
            if (emailQuillEditor) {
                emailQuillEditor.setText('');
                document.getElementById('emailContent').value = '';
            }
            
            // Clear file input and display
            const fileInput = document.getElementById('emailExternalFiles');
            if (fileInput) {
                fileInput.value = '';
            }
            
            // Clear selected files array and update display
            selectedFiles = [];
            // Call updateFilesList() to properly hide/clear file display elements
            if (typeof updateFilesList === 'function') {
                updateFilesList();
            }
            
            // Reset client search
            document.getElementById('emailClientSearch').value = '';
            document.getElementById('emailClient').value = '';
            document.getElementById('emailClientDropdown').style.display = 'none';
            
            // Reset and disable contact dropdowns
            const contactsBtn = document.getElementById('contactsDropdownBtn');
            const contactsCcBtn = document.getElementById('contactsCcDropdownBtn');
            if (contactsBtn) contactsBtn.disabled = true;
            if (contactsCcBtn) contactsCcBtn.disabled = true;
            
            // Hide variables section
            const variablesSection = document.getElementById('emailVariablesSection');
            if (variablesSection) {
                variablesSection.style.display = 'none';
            }
            
            // Reset checkboxes
            document.getElementById('emailAttachStatement')?.setAttribute('checked', 'checked');
            document.getElementById('emailAttachInvoices')?.removeAttribute('checked');
            document.getElementById('emailIncludeChildren')?.removeAttribute('checked');
            document.getElementById('emailIncludeChildren')?.setAttribute('disabled', 'disabled');
            document.getElementById('emailHighImportance')?.removeAttribute('checked');
            document.getElementById('emailReadReceipt')?.removeAttribute('checked');
            document.getElementById('emailDeliveryReceipt')?.removeAttribute('checked');
            
            // Reset template selector
            const templateSelect = document.getElementById('emailTemplate');
            if (templateSelect) {
                templateSelect.value = '';
            }
            
            console.log('[Email Modal] Reset complete');
        });
    }
}

// Initialize client search with autocomplete
function initializeClientSearch(searchInputId, hiddenInputId, dropdownId) {
    const searchInput = document.getElementById(searchInputId);
    const hiddenInput = document.getElementById(hiddenInputId);
    const dropdown = document.getElementById(dropdownId);
    
    if (!searchInput || !hiddenInput || !dropdown) return;
    
    let debounceTimer = null;
    let selectedIndex = -1;
    
    // Search on input with debounce
    searchInput.addEventListener('input', function() {
        const searchTerm = this.value.trim();
        
        // Clear hidden value when typing
        hiddenInput.value = '';
        
        clearTimeout(debounceTimer);
        
        if (searchTerm.length < 1) {
            dropdown.style.display = 'none';
            return;
        }
        
        debounceTimer = setTimeout(() => {
            searchClients(searchTerm, dropdown, hiddenInput, searchInput);
        }, 300);
    });
    
    // Show dropdown on focus if there's text
    searchInput.addEventListener('focus', function() {
        if (this.value.trim().length >= 1) {
            searchClients(this.value.trim(), dropdown, hiddenInput, searchInput);
        }
    });
    
    // Keyboard navigation
    searchInput.addEventListener('keydown', function(e) {
        const items = dropdown.querySelectorAll('.autocomplete-item');
        
        if (items.length === 0) return;
        
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
            updateSelection(items, selectedIndex);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            selectedIndex = Math.max(selectedIndex - 1, 0);
            updateSelection(items, selectedIndex);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (selectedIndex >= 0) {
                items[selectedIndex].click();
            }
        } else if (e.key === 'Escape') {
            dropdown.style.display = 'none';
        }
    });
    
    // Close dropdown when clicking outside
    document.addEventListener('click', function(e) {
        if (!searchInput.contains(e.target) && !dropdown.contains(e.target)) {
            dropdown.style.display = 'none';
        }
    });
    
    function updateSelection(items, index) {
        items.forEach((item, i) => {
            if (i === index) {
                item.classList.add('active');
                item.scrollIntoView({ block: 'nearest' });
            } else {
                item.classList.remove('active');
            }
        });
    }
}

// Search clients with autocomplete
function searchClients(searchTerm, dropdown, hiddenInput, searchInput) {
    fetch(`/notes/api/clients?q=${encodeURIComponent(searchTerm)}`, {
        headers: {
            'X-CSRF-Token': getCSRFToken()
        }
    })
    .then(response => response.json())
    .then(data => {
        dropdown.innerHTML = '';
        
        if (data.clients.length === 0) {
            dropdown.innerHTML = '<div class="autocomplete-empty">Aucun client trouvé</div>';
            dropdown.style.display = 'block';
            return;
        }
        
        data.clients.forEach(client => {
            const item = document.createElement('div');
            item.className = 'autocomplete-item';
            
            const strong = document.createElement('strong');
            strong.textContent = client.code;
            item.appendChild(strong);
            
            item.appendChild(document.createTextNode(' - ' + client.name));
            
            item.dataset.clientId = client.id;
            item.dataset.clientDisplay = client.display;
            
            item.addEventListener('click', function() {
                hiddenInput.value = this.dataset.clientId;
                searchInput.value = this.dataset.clientDisplay;
                dropdown.style.display = 'none';
                
                // Trigger change event for email client (to load contacts)
                if (hiddenInput.id === 'emailClient') {
                    loadClientContacts();
                }
            });
            
            dropdown.appendChild(item);
        });
        
        dropdown.style.display = 'block';
    })
    .catch(error => {
        console.error('Error loading clients:', error);
        dropdown.innerHTML = '<div class="autocomplete-empty">Erreur de chargement</div>';
        dropdown.style.display = 'block';
    });
}

// Load client contacts when client is selected
function loadClientContacts() {
    const clientId = document.getElementById('emailClient').value;
    
    if (!clientId) {
        // Disable dropdown buttons and hide variables
        const dropdownBtn = document.getElementById('contactsDropdownBtn');
        const ccDropdownBtn = document.getElementById('contactsCcDropdownBtn');
        const variablesSection = document.getElementById('emailVariablesSection');
        
        if (dropdownBtn) dropdownBtn.disabled = true;
        if (ccDropdownBtn) ccDropdownBtn.disabled = true;
        if (variablesSection) variablesSection.style.display = 'none';
        return;
    }
    
    fetch(`/notes/api/client-contacts/${clientId}`, {
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.contacts) {
            populateContactDropdowns(data.contacts);
            // Enable dropdown buttons
            const dropdownBtn = document.getElementById('contactsDropdownBtn');
            const ccDropdownBtn = document.getElementById('contactsCcDropdownBtn');
            if (dropdownBtn) dropdownBtn.disabled = false;
            if (ccDropdownBtn) ccDropdownBtn.disabled = false;
        }
        
        if (data.client) {
            // Show and populate variables section
            populateEmailVariables(data.client);
        }
    })
    .catch(error => {
        console.error('Error loading contacts:', error);
        showAlert('Erreur lors du chargement des contacts', 'danger');
    });
}

// Populate email variables with client data
function populateEmailVariables(client) {
    const variablesSection = document.getElementById('emailVariablesSection');
    if (!variablesSection) return;
    
    // Show variables section
    variablesSection.style.display = '';
    
    // Get today's date formatted
    const today = new Date();
    const todayFormatted = today.toLocaleDateString('fr-FR', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric'
    });
    
    // Update variable values
    const variables = {
        '{client_name}': client.name || 'Non renseigné',
        '{client_code}': client.code || 'Non renseigné',
        '{client_email}': client.email || 'Non renseigné',
        '{client_phone}': client.phone || 'Non renseigné',
        '{client_payment_terms}': client.payment_terms || 'Non renseigné',
        '{client_total_outstanding}': client.total_outstanding || '0,00 $',
        '{today_date}': todayFormatted
    };
    
    // Update button text with actual values AND store value in data-value attribute
    variablesSection.querySelectorAll('.variable-btn').forEach(btn => {
        const variable = btn.dataset.variable;
        const valueSpan = btn.querySelector('.var-value');
        if (valueSpan && variables[variable]) {
            valueSpan.textContent = variables[variable];
            // Store real value in data-value attribute for insertion
            btn.dataset.value = variables[variable];
        }
    });
    
    // Enable/disable "Include children" checkbox based on has_children
    const checkbox = document.getElementById('emailIncludeChildren');
    const hint = document.getElementById('includeChildrenHint');
    
    if (checkbox && hint) {
        if (client.has_children) {
            checkbox.disabled = false;
            checkbox.checked = false;
            hint.textContent = 'Les pièces jointes incluront les données du parent et des enfants';
        } else {
            checkbox.disabled = true;
            checkbox.checked = false;
            hint.textContent = 'Ce client n\'a pas de clients enfants';
        }
    }
}

// Handle multiple file selection
let selectedFiles = [];
const MAX_TOTAL_SIZE = 20 * 1024 * 1024; // 20 MB

document.addEventListener('DOMContentLoaded', function() {
    const fileInput = document.getElementById('emailExternalFiles');
    if (fileInput) {
        fileInput.addEventListener('change', handleFileSelection);
    }
});

function handleFileSelection(e) {
    const files = Array.from(e.target.files);
    selectedFiles = files;
    
    updateFilesList();
}

function updateFilesList() {
    const filesList = document.getElementById('filesList');
    const filesListItems = document.getElementById('filesListItems');
    const totalSizeElement = document.getElementById('totalSize');
    
    if (!filesList || !filesListItems || !totalSizeElement) return;
    
    if (selectedFiles.length === 0) {
        filesList.style.display = 'none';
        totalSizeElement.textContent = '';  // Clear total size when no files
        return;
    }
    
    filesList.style.display = 'block';
    filesListItems.innerHTML = '';
    
    let totalSize = 0;
    selectedFiles.forEach((file, index) => {
        totalSize += file.size;
        
        const li = document.createElement('li');
        li.className = 'list-group-item d-flex justify-content-between align-items-center py-1 px-2';
        
        // Create span container for file info
        const span = document.createElement('span');
        
        // Add file icon
        const icon = document.createElement('i');
        icon.className = 'fas fa-file me-2';
        span.appendChild(icon);
        
        // Add file name as text (safe from XSS)
        span.appendChild(document.createTextNode(file.name));
        
        // Add file size
        const small = document.createElement('small');
        small.className = 'text-muted';
        small.textContent = ` (${formatFileSize(file.size)})`;
        span.appendChild(small);
        
        // Create remove button
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-sm btn-outline-danger';
        button.onclick = () => removeFile(index);
        
        const buttonIcon = document.createElement('i');
        buttonIcon.className = 'fas fa-times';
        button.appendChild(buttonIcon);
        
        // Append elements to li
        li.appendChild(span);
        li.appendChild(button);
        filesListItems.appendChild(li);
    });
    
    // Show total size with color indication
    const sizeClass = totalSize > MAX_TOTAL_SIZE ? 'text-danger' : 'text-success';
    
    // Clear existing content
    totalSizeElement.textContent = '';
    
    // Build DOM elements safely
    const strong = document.createElement('strong');
    strong.textContent = 'Taille totale : ';
    totalSizeElement.appendChild(strong);
    
    const sizeSpan = document.createElement('span');
    sizeSpan.className = sizeClass;
    sizeSpan.textContent = formatFileSize(totalSize);
    totalSizeElement.appendChild(sizeSpan);
    
    totalSizeElement.appendChild(document.createTextNode(' / ' + formatFileSize(MAX_TOTAL_SIZE)));
    
    if (totalSize > MAX_TOTAL_SIZE) {
        const warningSpan = document.createElement('span');
        warningSpan.className = 'text-danger';
        
        const icon = document.createElement('i');
        icon.className = 'fas fa-exclamation-triangle';
        warningSpan.appendChild(icon);
        
        warningSpan.appendChild(document.createTextNode(' Limite dépassée'));
        totalSizeElement.appendChild(document.createTextNode(' '));
        totalSizeElement.appendChild(warningSpan);
    }
}

function removeFile(index) {
    selectedFiles.splice(index, 1);
    
    // Update the file input
    const fileInput = document.getElementById('emailExternalFiles');
    const dataTransfer = new DataTransfer();
    selectedFiles.forEach(file => dataTransfer.items.add(file));
    fileInput.files = dataTransfer.files;
    
    updateFilesList();
}

function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

// Insert variable (or real value) into Quill editor
function insertEmailVariable(value) {
    if (emailQuillEditor) {
        const range = emailQuillEditor.getSelection();
        const index = range ? range.index : emailQuillEditor.getLength();
        emailQuillEditor.insertText(index, value + ' ');
        emailQuillEditor.focus();
        document.getElementById('emailContent').value = emailQuillEditor.root.innerHTML;
    }
}

// Populate contact dropdowns with available contacts
function populateContactDropdowns(contacts) {
    const toDropdown = document.getElementById('emailToDropdown');
    const ccDropdown = document.getElementById('emailCcDropdown');
    
    if (!toDropdown || !ccDropdown) return;
    
    // Clear existing items
    toDropdown.innerHTML = '';
    ccDropdown.innerHTML = '';
    
    if (contacts.length === 0) {
        toDropdown.innerHTML = '<li><span class="dropdown-item-text text-muted">Aucun contact disponible</span></li>';
        ccDropdown.innerHTML = '<li><span class="dropdown-item-text text-muted">Aucun contact disponible</span></li>';
        return;
    }
    
    // Add each contact to both dropdowns
    contacts.forEach(contact => {
        // Create item for "To" dropdown - safe DOM construction
        const toItem = document.createElement('li');
        const toLink = document.createElement('a');
        toLink.className = 'dropdown-item';
        toLink.href = '#';
        toLink.onclick = function() {
            addEmailToField('emailTo', contact.email);
            return false;
        };
        
        const toNameDiv = document.createElement('div');
        toNameDiv.className = 'fw-bold';
        toNameDiv.textContent = contact.full_name;
        
        const toBadge = document.createElement('span');
        toBadge.className = `badge bg-${contact.language === 'fr' ? 'success' : 'info'} ms-1`;
        toBadge.textContent = (contact.language || 'FR').toUpperCase();
        toNameDiv.appendChild(toBadge);
        
        const toEmailSmall = document.createElement('small');
        toEmailSmall.className = 'text-muted';
        toEmailSmall.textContent = contact.email;
        
        toLink.appendChild(toNameDiv);
        toLink.appendChild(toEmailSmall);
        toItem.appendChild(toLink);
        toDropdown.appendChild(toItem);
        
        // Create item for "CC" dropdown - safe DOM construction
        const ccItem = document.createElement('li');
        const ccLink = document.createElement('a');
        ccLink.className = 'dropdown-item';
        ccLink.href = '#';
        ccLink.onclick = function() {
            addEmailToField('emailCc', contact.email);
            return false;
        };
        
        const ccNameDiv = document.createElement('div');
        ccNameDiv.className = 'fw-bold';
        ccNameDiv.textContent = contact.full_name;
        
        const ccBadge = document.createElement('span');
        ccBadge.className = `badge bg-${contact.language === 'fr' ? 'success' : 'info'} ms-1`;
        ccBadge.textContent = (contact.language || 'FR').toUpperCase();
        ccNameDiv.appendChild(ccBadge);
        
        const ccEmailSmall = document.createElement('small');
        ccEmailSmall.className = 'text-muted';
        ccEmailSmall.textContent = contact.email;
        
        ccLink.appendChild(ccNameDiv);
        ccLink.appendChild(ccEmailSmall);
        ccItem.appendChild(ccLink);
        ccDropdown.appendChild(ccItem);
    });
}

// Function to add email to field (called from dropdown items)
function addEmailToField(fieldId, email) {
    const field = document.getElementById(fieldId);
    if (!field) return;
    
    const currentValue = field.value.trim();
    
    if (currentValue === '') {
        field.value = email;
    } else {
        // Check if email is already in the list
        const emails = currentValue.split(';').map(e => e.trim());
        if (!emails.includes(email)) {
            field.value = currentValue + '; ' + email;
        }
    }
}

// Load notes with current filters
function loadNotes(page = 1) {
    const search = document.getElementById('searchInput').value;
    const collectorId = document.getElementById('collectorFilter').value;
    const noteType = document.getElementById('typeFilter').value;
    
    const params = new URLSearchParams({
        page: page,
        search: search,
        collector_id: collectorId || '',
        note_type: noteType || ''
    });
    
    fetch(`/notes/api/search?${params.toString()}`, {
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        updateNotesTable(data);
        updatePagination(data);
        document.getElementById('totalCount').textContent = data.total;
    })
    .catch(error => {
        console.error('Error loading notes:', error);
        showAlert('Erreur lors du chargement des notes', 'danger');
    });
}

// Update notes table with server-rendered HTML
function updateNotesTable(data) {
    const container = document.getElementById('notesContainer');
    
    if (!container) {
        console.error('notesContainer element not found');
        return;
    }
    
    // If no results, show empty state
    if (data.total === 0) {
        container.innerHTML = `
            <div class="text-center py-5">
                <i class="fas fa-sticky-note fa-3x text-muted mb-3"></i>
                <p class="text-muted">Aucune note trouvée</p>
            </div>
        `;
        return;
    }
    
    // Check if table structure exists
    let tbody = document.getElementById('notesTableBody');
    
    // If table doesn't exist (after showing empty state), recreate it
    if (!tbody) {
        container.innerHTML = `
            <div class="table-responsive">
                <table class="table table-hover mb-0">
                    <thead>
                        <tr>
                            <th width="140">Date</th>
                            <th width="100">Type</th>
                            <th width="200">Client</th>
                            <th width="150">Auteur</th>
                            <th>Contenu</th>
                            <th width="100" class="text-end">Actions</th>
                        </tr>
                    </thead>
                    <tbody id="notesTableBody"></tbody>
                </table>
            </div>
        `;
        tbody = document.getElementById('notesTableBody');
    }
    
    // Sanitize server-rendered HTML using DOMPurify with full table context
    if (typeof DOMPurify === 'undefined') {
        console.error('DOMPurify is required but not loaded');
        tbody.textContent = 'Erreur de chargement';
        return;
    }
    // Wrap in table context before sanitizing to preserve td/tr structure
    const wrappedHtml = '<table><tbody>' + data.html + '</tbody></table>';
    const sanitizedHtml = DOMPurify.sanitize(wrappedHtml, {
        ALLOWED_TAGS: ['table', 'tbody', 'tr', 'td', 'th', 'a', 'span', 'div', 'i', 'button', 'small', 'strong', 'em', 'br', 'p', 'img'],
        ALLOWED_ATTR: ['href', 'class', 'id', 'data-id', 'data-bs-toggle', 'data-bs-target', 'title', 'onclick', 'style', 'src', 'alt', 'width', 'height', 'colspan', 'rowspan', 'scope'],
        ALLOW_DATA_ATTR: true
    });
    // Parse and extract rows from sanitized table
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = sanitizedHtml;
    const sanitizedTbody = tempDiv.querySelector('tbody');
    tbody.textContent = '';
    if (sanitizedTbody) {
        while (sanitizedTbody.firstChild) {
            tbody.appendChild(sanitizedTbody.firstChild);
        }
    }
}

// Update pagination
function updatePagination(data) {
    const paginationContainer = document.getElementById('paginationContainer');
    
    if (!paginationContainer) {
        return;
    }
    
    if (data.pages <= 1) {
        paginationContainer.style.display = 'none';
        return;
    }
    
    paginationContainer.style.display = 'block';
    
    // Build pagination using DOM methods
    paginationContainer.textContent = '';
    
    const ul = document.createElement('ul');
    ul.className = 'pagination justify-content-center mb-0';
    
    // Previous button
    if (data.has_prev) {
        const prevLi = document.createElement('li');
        prevLi.className = 'page-item';
        const prevLink = document.createElement('a');
        prevLink.className = 'page-link';
        prevLink.href = '#';
        prevLink.textContent = 'Précédent';
        prevLink.onclick = function() {
            loadPage(data.current_page - 1);
            return false;
        };
        prevLi.appendChild(prevLink);
        ul.appendChild(prevLi);
    }
    
    // Page numbers
    for (let i = 1; i <= data.pages; i++) {
        const pageLi = document.createElement('li');
        pageLi.className = 'page-item';
        if (i === data.current_page) {
            pageLi.classList.add('active');
        }
        const pageLink = document.createElement('a');
        pageLink.className = 'page-link';
        pageLink.href = '#';
        pageLink.textContent = i.toString();
        pageLink.onclick = function() {
            loadPage(i);
            return false;
        };
        pageLi.appendChild(pageLink);
        ul.appendChild(pageLi);
    }
    
    // Next button
    if (data.has_next) {
        const nextLi = document.createElement('li');
        nextLi.className = 'page-item';
        const nextLink = document.createElement('a');
        nextLink.className = 'page-link';
        nextLink.href = '#';
        nextLink.textContent = 'Suivant';
        nextLink.onclick = function() {
            loadPage(data.current_page + 1);
            return false;
        };
        nextLi.appendChild(nextLink);
        ul.appendChild(nextLi);
    }
    
    paginationContainer.appendChild(ul);
}

// Load specific page
function loadPage(page) {
    loadNotes(page);
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// Save note (create or update)
function saveNote() {
    const form = document.getElementById('noteForm');
    const noteId = document.getElementById('noteId').value;
    
    const formData = new FormData(form);
    
    const url = noteId ? `/notes/${noteId}/edit` : '/notes/new';
    
    fetch(url, {
        method: 'POST',
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            bootstrap.Modal.getInstance(document.getElementById('noteModal')).hide();
            loadNotes();
        } else {
            showAlert(data.error || 'Erreur lors de l\'enregistrement', 'danger');
        }
    })
    .catch(error => {
        console.error('Error saving note:', error);
        showAlert('Erreur lors de l\'enregistrement de la note', 'danger');
    });
}

// Save email note (hybrid modal - only note_text and reminder_date)
function saveEmailNote() {
    const noteId = document.getElementById('editEmailNoteId').value;
    const formData = new FormData(document.getElementById('editEmailNoteForm'));
    
    if (!noteId) {
        showAlert('ID de note manquant', 'danger');
        return;
    }
    
    fetch(`/notes/${noteId}/edit`, {
        method: 'POST',
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            bootstrap.Modal.getInstance(document.getElementById('editEmailNoteModal')).hide();
            loadNotes();
        } else {
            showAlert(data.error || 'Erreur lors de la modification', 'danger');
        }
    })
    .catch(error => {
        console.error('Error saving email note:', error);
        showAlert('Erreur lors de l\'enregistrement', 'danger');
    });
}

// Save and send email
function saveEmail() {
    const form = document.getElementById('emailForm');
    const formData = new FormData(form);
    
    // Validate client is selected
    if (!formData.get('client_id')) {
        showAlert('Veuillez sélectionner un client', 'warning');
        return;
    }
    
    fetch('/notes/email/send', {
        method: 'POST',
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        },
        body: formData
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            bootstrap.Modal.getInstance(document.getElementById('emailModal')).hide();
            loadNotes();
        } else {
            showAlert(data.error || 'Erreur lors de l\'envoi du courriel', 'danger');
        }
    })
    .catch(error => {
        console.error('Error sending email:', error);
        showAlert('Erreur lors de l\'envoi du courriel', 'danger');
    });
}

// View note details
function viewNote(noteId) {
    fetch(`/notes/${noteId}`, {
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        if (data.error) {
            showAlert(data.error, 'danger');
            return;
        }
        
        // Build content safely using DOM methods
        const contentContainer = document.getElementById('viewNoteContent');
        contentContainer.textContent = ''; // Clear existing content
        
        // First row: Client and Date
        const row1 = document.createElement('div');
        row1.className = 'row mb-3';
        
        const clientCol = document.createElement('div');
        clientCol.className = 'col-md-6';
        const clientStrong = document.createElement('strong');
        clientStrong.textContent = 'Client: ';
        clientCol.appendChild(clientStrong);
        clientCol.appendChild(document.createTextNode(`${data.client_code} - ${data.client_name}`));
        
        const dateCol = document.createElement('div');
        dateCol.className = 'col-md-6';
        const dateStrong = document.createElement('strong');
        dateStrong.textContent = 'Date: ';
        dateCol.appendChild(dateStrong);
        dateCol.appendChild(document.createTextNode(data.created_at));
        
        row1.appendChild(clientCol);
        row1.appendChild(dateCol);
        contentContainer.appendChild(row1);
        
        // Second row: Type and Author
        const row2 = document.createElement('div');
        row2.className = 'row mb-3';
        
        const typeCol = document.createElement('div');
        typeCol.className = 'col-md-6';
        const typeStrong = document.createElement('strong');
        typeStrong.textContent = 'Type: ';
        typeCol.appendChild(typeStrong);
        typeCol.appendChild(document.createTextNode(data.note_type));
        
        const authorCol = document.createElement('div');
        authorCol.className = 'col-md-6';
        const authorStrong = document.createElement('strong');
        authorStrong.textContent = 'Auteur: ';
        authorCol.appendChild(authorStrong);
        authorCol.appendChild(document.createTextNode(data.author));
        
        row2.appendChild(typeCol);
        row2.appendChild(authorCol);
        contentContainer.appendChild(row2);
        
        // Separator
        const hr = document.createElement('hr');
        contentContainer.appendChild(hr);
        
        // Content based on type - check if it's a real email (has email_subject) or just a note with type 'email'
        if (data.email_subject) {
            // Real email sent - display email fields
            const fromDiv = document.createElement('div');
            fromDiv.className = 'mb-3';
            const fromStrong = document.createElement('strong');
            fromStrong.textContent = 'De: ';
            fromDiv.appendChild(fromStrong);
            fromDiv.appendChild(document.createTextNode(data.email_from || 'N/A'));
            contentContainer.appendChild(fromDiv);
            
            // Email To
            const toDiv = document.createElement('div');
            toDiv.className = 'mb-3';
            const toStrong = document.createElement('strong');
            toStrong.textContent = 'À: ';
            toDiv.appendChild(toStrong);
            toDiv.appendChild(document.createTextNode(data.email_to || 'N/A'));
            contentContainer.appendChild(toDiv);
            
            // Email Subject
            const subjectDiv = document.createElement('div');
            subjectDiv.className = 'mb-3';
            const subjectStrong = document.createElement('strong');
            subjectStrong.textContent = 'Sujet: ';
            subjectDiv.appendChild(subjectStrong);
            subjectDiv.appendChild(document.createTextNode(data.email_subject));
            contentContainer.appendChild(subjectDiv);
            
            // Email Body
            const messageDiv = document.createElement('div');
            messageDiv.className = 'mb-3';
            const messageStrong = document.createElement('strong');
            messageStrong.textContent = 'Message:';
            messageDiv.appendChild(messageStrong);
            
            const bodyDiv = document.createElement('div');
            bodyDiv.className = 'mt-2 p-3 bg-light rounded';
            // Sanitize HTML content with DOMPurify to prevent XSS attacks
            bodyDiv.innerHTML = sanitizeHTML(data.email_body_html || data.email_body || 'N/A');
            messageDiv.appendChild(bodyDiv);
            contentContainer.appendChild(messageDiv);
        } else {
            // Traditional note (any type, including 'email' type without actual email)
            const noteDiv = document.createElement('div');
            noteDiv.className = 'mb-3';
            const noteStrong = document.createElement('strong');
            noteStrong.textContent = 'Contenu:';
            noteDiv.appendChild(noteStrong);
            
            const textDiv = document.createElement('div');
            textDiv.className = 'mt-2 p-3 bg-light rounded';
            textDiv.textContent = data.note_text || 'N/A';
            noteDiv.appendChild(textDiv);
            contentContainer.appendChild(noteDiv);
        }
        
        // Reminder if present
        if (data.reminder_date) {
            const reminderDiv = document.createElement('div');
            reminderDiv.className = 'mb-3';
            const reminderStrong = document.createElement('strong');
            reminderStrong.textContent = 'Rappel: ';
            reminderDiv.appendChild(reminderStrong);
            reminderDiv.appendChild(document.createTextNode(data.reminder_date));
            contentContainer.appendChild(reminderDiv);
        }
        
        new bootstrap.Modal(document.getElementById('viewNoteModal')).show();
    })
    .catch(error => {
        console.error('Error loading note details:', error);
        showAlert('Erreur lors du chargement des détails', 'danger');
    });
}

// Edit note - dispatches to appropriate modal based on note type
function editNote(noteId) {
    fetch(`/notes/${noteId}`, {
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        if (data.error) {
            showAlert(data.error, 'danger');
            return;
        }
        
        // Check if this is a real sent email (has email_subject or email_body)
        if (data.is_real_email) {
            openEditEmailNoteModal(data);
        } else {
            openEditNormalNoteModal(data);
        }
    })
    .catch(error => {
        console.error('Error loading note for edit:', error);
        showAlert('Erreur lors du chargement de la note', 'danger');
    });
}

// Open edit modal for normal notes (including manual email-type notes)
function openEditNormalNoteModal(data) {
    const canEdit = data.can_edit !== false;
    
    document.getElementById('noteId').value = data.id;
    document.getElementById('noteClient').value = data.client_id;
    document.getElementById('noteClientSearch').value = data.client_display || `${data.client_code} - ${data.client_name}`;
    document.getElementById('noteType').value = data.note_type;
    document.getElementById('noteDate').value = data.note_date;
    document.getElementById('noteText').value = data.note_text;
    
    if (data.reminder_date) {
        document.getElementById('noteReminder').value = data.reminder_date;
    } else {
        document.getElementById('noteReminder').value = '';
    }
    
    // Handle read-only mode
    const formFields = ['noteClientSearch', 'noteType', 'noteDate', 'noteText', 'noteReminder'];
    formFields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.disabled = !canEdit;
            field.readOnly = !canEdit;
        }
    });
    
    // Update modal title and save button visibility
    document.getElementById('noteModalLabel').textContent = canEdit ? 'Modifier note' : 'Détail de la note';
    
    // Handle save button in modal footer
    const modal = document.getElementById('noteModal');
    const saveBtn = modal.querySelector('.modal-footer .btn-primary');
    if (saveBtn) {
        saveBtn.style.display = canEdit ? '' : 'none';
    }
    
    // Update cancel button text
    const cancelBtn = modal.querySelector('.modal-footer .btn-secondary');
    if (cancelBtn) {
        cancelBtn.textContent = canEdit ? 'Annuler' : 'Fermer';
    }
    
    new bootstrap.Modal(modal).show();
}

// Open hybrid modal for real sent emails (read-only email + editable note/reminder)
function openEditEmailNoteModal(data) {
    const canEdit = data.can_edit !== false;
    
    // Populate read-only email fields
    document.getElementById('emailNoteClient').textContent = data.client_display || `${data.client_code} - ${data.client_name}`;
    document.getElementById('emailNoteDate').textContent = data.created_at;
    document.getElementById('emailNoteFrom').textContent = data.email_from || 'N/A';
    document.getElementById('emailNoteTo').textContent = data.email_to || 'N/A';
    document.getElementById('emailNoteSubject').textContent = data.email_subject || 'Sans sujet';
    
    // Populate email body (use HTML version if available, sanitized to prevent XSS)
    const bodyDiv = document.getElementById('emailNoteBody');
    if (data.email_body_html) {
        bodyDiv.innerHTML = sanitizeHTML(data.email_body_html);
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
            // Use filename or name (fallback for different storage formats)
            const attachmentName = att.filename || att.name || 'Pièce jointe';
            li.appendChild(document.createTextNode(attachmentName + ' '));
            const sizeSpan = document.createElement('small');
            sizeSpan.className = 'text-muted';
            sizeSpan.textContent = '(' + formatFileSize(att.size) + ')';
            li.appendChild(sizeSpan);
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
    
    // Handle read-only mode for editable fields
    const editableFields = ['emailNoteAdditional', 'emailNoteReminder'];
    editableFields.forEach(fieldId => {
        const field = document.getElementById(fieldId);
        if (field) {
            field.disabled = !canEdit;
            field.readOnly = !canEdit;
        }
    });
    
    // Update editable section header based on mode
    const modal = document.getElementById('editEmailNoteModal');
    const editableHeader = modal.querySelector('.card-header.bg-primary h6');
    if (editableHeader) {
        if (canEdit) {
            editableHeader.innerHTML = '<i class="fas fa-edit me-2"></i>Notes et rappel (modifiable)';
        } else {
            editableHeader.innerHTML = '<i class="fas fa-eye me-2"></i>Notes et rappel (lecture seule)';
        }
    }
    
    // Update modal title
    const modalTitle = modal.querySelector('.modal-title');
    if (modalTitle) {
        modalTitle.innerHTML = canEdit 
            ? '<i class="fas fa-envelope me-2"></i>Courriel envoyé'
            : '<i class="fas fa-envelope me-2"></i>Détail du courriel';
    }
    
    // Handle save button visibility
    const saveBtn = modal.querySelector('.modal-footer .btn-primary');
    if (saveBtn) {
        saveBtn.style.display = canEdit ? '' : 'none';
    }
    
    // Update cancel button text
    const cancelBtn = modal.querySelector('.modal-footer .btn-secondary');
    if (cancelBtn) {
        cancelBtn.textContent = canEdit ? 'Annuler' : 'Fermer';
    }
    
    // Show modal
    new bootstrap.Modal(modal).show();
}

// Confirm delete
function confirmDelete(noteId) {
    if (confirm('Êtes-vous sûr de vouloir supprimer cette note ?')) {
        deleteNote(noteId);
    }
}

// Delete note
function deleteNote(noteId) {
    fetch(`/notes/${noteId}/delete`, {
        method: 'POST',
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showAlert(data.message, 'success');
            loadNotes();
        } else {
            showAlert(data.error || 'Erreur lors de la suppression', 'danger');
        }
    })
    .catch(error => {
        console.error('Error deleting note:', error);
        showAlert('Erreur lors de la suppression de la note', 'danger');
    });
}

// Open modal for creating a new note
function openNewNoteModal() {
    // Reset form
    document.getElementById('noteForm').reset();
    document.getElementById('noteId').value = '';
    document.getElementById('noteModalLabel').textContent = 'Nouvelle note';
    document.getElementById('noteClientSearch').value = '';
    document.getElementById('noteClient').value = '';
    document.getElementById('noteClientDropdown').style.display = 'none';
    
    // Set today's date
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('noteDate').value = today;
    
    // Open modal
    new bootstrap.Modal(document.getElementById('noteModal')).show();
}

// Global variable for Quill editor instance
let emailQuillEditor = null;

// Open modal for creating a new email
function openNewEmailModal() {
    // Reset form
    document.getElementById('emailForm').reset();
    document.getElementById('emailClientSearch').value = '';
    document.getElementById('emailClient').value = '';
    document.getElementById('emailClientDropdown').style.display = 'none';
    
    // Reset and disable contact dropdowns
    const contactsDropdownBtn = document.getElementById('contactsDropdownBtn');
    const contactsCcDropdownBtn = document.getElementById('contactsCcDropdownBtn');
    const toDropdown = document.getElementById('emailToDropdown');
    const ccDropdown = document.getElementById('emailCcDropdown');
    
    if (contactsDropdownBtn) {
        contactsDropdownBtn.disabled = true;
    }
    if (contactsCcDropdownBtn) {
        contactsCcDropdownBtn.disabled = true;
    }
    if (toDropdown) {
        toDropdown.innerHTML = '<li><span class="dropdown-item-text text-muted">Sélectionnez d\'abord un client</span></li>';
    }
    if (ccDropdown) {
        ccDropdown.innerHTML = '<li><span class="dropdown-item-text text-muted">Sélectionnez d\'abord un client</span></li>';
    }
    
    // Load available templates
    loadEmailTemplates();
    
    // Initialize Quill if not already initialized
    initializeEmailQuill();
    
    // Clear Quill content
    if (emailQuillEditor) {
        emailQuillEditor.setText('');
        document.getElementById('emailContent').value = '';
    }
    
    // Open modal
    new bootstrap.Modal(document.getElementById('emailModal')).show();
}

// Initialize Quill editor for email modal
function initializeEmailQuill() {
    if (emailQuillEditor) {
        // Editor already exists, just clear it
        emailQuillEditor.setText('');
        return;
    }
    
    // Standard Quill configuration for email editing
    emailQuillEditor = new Quill('#emailEditor', {
        theme: 'snow',
        placeholder: 'Rédigez votre message ici...',
        modules: {
            toolbar: [
                [{ 'header': [1, 2, 3, false] }],
                ['bold', 'italic', 'underline', 'strike'],
                [{ 'color': [] }, { 'background': [] }],
                [{ 'list': 'ordered'}, { 'list': 'bullet' }],
                [{ 'align': [] }],
                ['link'],
                ['clean']
            ]
        }
    });
    
    // Update hidden field when content changes
    emailQuillEditor.on('text-change', function() {
        document.getElementById('emailContent').value = emailQuillEditor.root.innerHTML;
    });
    
    // Load existing content if any
    const existingContent = document.getElementById('emailContent').value;
    if (existingContent) {
        // Sanitize content with DOMPurify to prevent XSS attacks
        emailQuillEditor.root.innerHTML = sanitizeHTML(existingContent);
    }
}

// Load email templates for the company
function loadEmailTemplates() {
    fetch('/notes/api/email-templates', {
        headers: {
            'X-CSRF-Token': getCSRFToken(),
            'X-Requested-With': 'XMLHttpRequest'
        }
    })
    .then(response => response.json())
    .then(data => {
        const templateSelect = document.getElementById('emailTemplate');
        templateSelect.innerHTML = '<option value="">Sélectionner un modèle...</option>';
        
        data.templates.forEach(template => {
            const option = document.createElement('option');
            option.value = template.id;
            option.textContent = template.name;
            option.dataset.subject = template.subject;
            option.dataset.content = template.content;
            templateSelect.appendChild(option);
        });
    })
    .catch(error => {
        console.error('Error loading templates:', error);
    });
}

// Handle template selection and variable buttons
document.addEventListener('DOMContentLoaded', function() {
    const templateSelect = document.getElementById('emailTemplate');
    if (templateSelect) {
        templateSelect.addEventListener('change', function() {
            const selectedOption = this.options[this.selectedIndex];
            if (selectedOption.value && selectedOption.value !== '') {
                // Update subject
                document.getElementById('emailSubject').value = selectedOption.dataset.subject || '';
                
                // Update content in Quill editor
                if (emailQuillEditor) {
                    // Sanitize template content with DOMPurify to prevent XSS attacks
                    const sanitizedContent = sanitizeHTML(selectedOption.dataset.content || '');
                    emailQuillEditor.root.innerHTML = sanitizedContent;
                    document.getElementById('emailContent').value = sanitizedContent;
                } else {
                    // Fallback if Quill not initialized yet - sanitize content
                    document.getElementById('emailContent').value = sanitizeHTML(selectedOption.dataset.content || '');
                }
            }
        });
    }
    
    // Handle variable button clicks
    document.addEventListener('click', function(e) {
        if (e.target.closest('.variable-btn')) {
            e.preventDefault();
            const btn = e.target.closest('.variable-btn');
            const realValue = btn.dataset.value;  // Get real value instead of placeholder
            if (realValue) {
                insertEmailVariable(realValue);
            }
        }
    });
});

// Show alert message
function showAlert(message, type) {
    // Create alert element
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.setAttribute('role', 'alert');
    
    // Add message text safely
    alertDiv.textContent = message;
    
    // Create and add close button
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn-close';
    closeBtn.setAttribute('data-bs-dismiss', 'alert');
    closeBtn.setAttribute('aria-label', 'Close');
    alertDiv.appendChild(closeBtn);
    
    // Insert at top of content
    const content = document.querySelector('.content') || document.querySelector('main') || document.body;
    content.insertBefore(alertDiv, content.firstChild);
    
    // Auto-dismiss after 3 seconds with fade animation
    setTimeout(() => {
        alertDiv.classList.remove('show');
        alertDiv.classList.add('fade');
        // Remove from DOM after animation completes
        setTimeout(() => {
            alertDiv.remove();
        }, 300);
    }, 3000);
}