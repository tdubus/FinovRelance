/**
 * Clients List - Dynamic Search and Filtering
 * Optimized for performance with debouncing and efficient DOM updates
 */

let isReadOnly = false;

// Exposer currentPage globalement pour le state manager
window.clientsCurrentPage = 1;

// Load clients from API
function loadClients(page = 1) {
    const searchInput = document.getElementById('searchInput');
    const balanceFilter = document.getElementById('balanceFilter');
    const collectorFilter = document.getElementById('collectorFilter');

    if (!searchInput || !balanceFilter) return;

    const search = searchInput.value.trim();
    const balance = balanceFilter.value;
    const collector = collectorFilter ? collectorFilter.value : '';

    // Build query parameters
    const params = new URLSearchParams({
        page: page,
        search: search,
        balance_filter: balance,
        collector_filter: collector
    });

    // Show loading state
    const container = document.getElementById('clientsContainer');
    if (container) {
        container.style.opacity = '0.6';
        container.style.pointerEvents = 'none';
    }

    // Fetch data from API
    fetch(`/clients/api/load?${params.toString()}`)
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            updateClientsTable(data.clients);
            updatePagination(data.pagination);
            updateTotalCount(data.pagination.total);
            window.clientsCurrentPage = data.pagination.page;
        })
        .catch(error => {
            console.error('Error loading clients:', error);
            alert('Erreur lors du chargement des clients');
        })
        .finally(() => {
            // Remove loading state
            if (container) {
                container.style.opacity = '1';
                container.style.pointerEvents = 'auto';
            }
        });
}

// Update clients table with new data
function updateClientsTable(clients) {
    const tbody = document.getElementById('clientsTableBody');
    if (!tbody) return;

    // Hide initial loader and show table wrapper on first load
    const initialLoader = document.getElementById('initialLoader');
    const tableWrapper = document.getElementById('clientsTableWrapper');
    if (initialLoader && tableWrapper) {
        initialLoader.style.display = 'none';
        tableWrapper.style.display = 'block';
    }

    if (clients.length === 0) {
        // Show empty message in table without destroying structure
        const colCount = isReadOnly ? 7 : 8;
        tbody.innerHTML = `
            <tr>
                <td colspan="${colCount}" class="text-center py-5">
                    <div class="mb-3">
                        <i class="fas fa-search" style="font-size: 48px; color: #6c757d;"></i>
                    </div>
                    <h5 class="text-muted">Aucun client trouvé</h5>
                    <p class="text-muted mb-3">
                        Aucun résultat ne correspond à votre recherche.
                    </p>
                    <button type="button" class="btn btn-outline-primary btn-sm" onclick="clearAllFilters()">
                        <i class="fas fa-times me-1"></i>
                        Effacer les filtres
                    </button>
                </td>
            </tr>
        `;
        return;
    }

    // Build table rows
    let html = '';
    clients.forEach(client => {
        const outstanding = client.outstanding_balance || 0;

        html += '<tr>';

        // Checkbox column (if not read-only)
        if (!isReadOnly) {
            html += `
                <td>
                    <input type="checkbox" name="client_ids" value="${client.id}" class="form-check-input client-checkbox">
                </td>
            `;
        }

        // Code client
        html += `
            <td>
                <span class="badge bg-secondary">${window.escapeHtml(client.code_client)}</span>
            </td>
        `;

        // Nom du client (cliquable)
        html += `
            <td>
                <a href="/clients/${client.id}" class="text-decoration-none text-dark client-name-link" style="cursor: pointer;">
                    <div class="fw-bold">${window.escapeHtml(client.name)}</div>
                </a>
                ${client.payment_terms ? `<small class="text-muted">${window.escapeHtml(client.payment_terms)}</small>` : ''}
            </td>
        `;

        // Contact
        html += '<td>';
        if (client.email) {
            html += `<div><i class="fas fa-envelope me-1"></i>${window.escapeHtml(client.email)}</div>`;
        }
        if (client.phone) {
            html += `<div><i class="fas fa-phone me-1"></i>${window.escapeHtml(client.phone)}</div>`;
        }
        if (!client.email && !client.phone) {
            html += '<span class="text-muted">Aucun contact</span>';
        }
        html += '</td>';

        // Collecteur
        html += '<td>';
        if (client.collector_name) {
            html += `<span class="badge badge-secondary">${window.escapeHtml(client.collector_name)}</span>`;
        } else {
            html += '<span class="text-muted">Non assigné</span>';
        }
        html += '</td>';

        // Représentant
        html += '<td>';
        if (client.representative_name) {
            html += window.escapeHtml(client.representative_name);
        } else {
            html += '<span class="text-muted">Non défini</span>';
        }
        html += '</td>';

        // Solde à recevoir
        html += '<td>';
        if (outstanding > 0) {
            html += `<span class="fw-bold text-primary">${window.formatCurrency(outstanding)}</span>`;
        } else {
            html += `<span class="text-success">${window.formatCurrency(0)}</span>`;
        }
        html += '</td>';

        // Actions - Menu déroulant (dropstart pour ouvrir à gauche)
        html += '<td class="text-end">';
        html += '<div class="dropstart">';
        html += '<button class="btn btn-sm btn-outline-secondary dropdown-toggle" type="button" data-bs-toggle="dropdown" aria-expanded="false">';
        html += '<i class="fas fa-ellipsis-v"></i>';
        html += '</button>';
        html += '<ul class="dropdown-menu">';
        html += `<li><a class="dropdown-item" href="/clients/${client.id}"><i class="fas fa-eye me-2"></i>Voir détails</a></li>`;
        if (!isReadOnly) {
            html += `<li><a class="dropdown-item" href="/clients/${client.id}/edit"><i class="fas fa-edit me-2"></i>Modifier</a></li>`;
            html += '<li><hr class="dropdown-divider"></li>';
            html += `<li><a class="dropdown-item text-danger delete-client-btn" href="#" data-client-id="${client.id}" data-client-name="${window.escapeHtmlAttribute(client.name)}"><i class="fas fa-trash me-2"></i>Supprimer</a></li>`;
        }
        html += '</ul>';
        html += '</div>';
        html += '</td>';

        html += '</tr>';
    });

    tbody.innerHTML = html;

    // Reattach event listeners for checkboxes
    if (!isReadOnly) {
        const checkboxes = document.querySelectorAll('input[name="client_ids"]');
        checkboxes.forEach(cb => {
            cb.addEventListener('change', updateBatchActions);
        });
    }
}

// Update pagination
function updatePagination(pagination) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;

    if (pagination.pages <= 1) {
        container.innerHTML = '';
        return;
    }

    // Security: Explicitly convert to integers to ensure type safety (defense-in-depth)
    const page = parseInt(pagination.page, 10);
    const pages = parseInt(pagination.pages, 10);
    const total = parseInt(pagination.total, 10);
    const prevNum = pagination.prev_num ? parseInt(pagination.prev_num, 10) : null;
    const nextNum = pagination.next_num ? parseInt(pagination.next_num, 10) : null;

    // Validate that conversions succeeded
    if (isNaN(page) || isNaN(pages) || isNaN(total)) {
        console.error('Invalid pagination data received');
        return;
    }

    let html = '<nav aria-label="Pagination des clients"><ul class="pagination pagination-sm justify-content-center mb-0">';

    // Previous button
    if (pagination.has_prev && prevNum) {
        html += `
            <li class="page-item">
                <a class="page-link" href="#" onclick="loadPage(${prevNum}); return false;">
                    Précédent
                </a>
            </li>
        `;
    }

    // Page numbers
    const maxPages = 7;
    let startPage = Math.max(1, page - 3);
    let endPage = Math.min(pages, startPage + maxPages - 1);

    if (endPage - startPage < maxPages - 1) {
        startPage = Math.max(1, endPage - maxPages + 1);
    }

    if (startPage > 1) {
        html += `
            <li class="page-item">
                <a class="page-link" href="#" onclick="loadPage(1); return false;">1</a>
            </li>
        `;
        if (startPage > 2) {
            html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
        }
    }

    for (let i = startPage; i <= endPage; i++) {
        const activeClass = i === page ? 'active' : '';
        html += `
            <li class="page-item ${activeClass}">
                <a class="page-link" href="#" onclick="loadPage(${i}); return false;">${i}</a>
            </li>
        `;
    }

    if (endPage < pages) {
        if (endPage < pages - 1) {
            html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
        }
        html += `
            <li class="page-item">
                <a class="page-link" href="#" onclick="loadPage(${pages}); return false;">${pages}</a>
            </li>
        `;
    }

    // Next button
    if (pagination.has_next && nextNum) {
        html += `
            <li class="page-item">
                <a class="page-link" href="#" onclick="loadPage(${nextNum}); return false;">
                    Suivant
                </a>
            </li>
        `;
    }

    html += '</ul></nav>';

    // Add page info
    html += `
        <div class="text-center text-muted mt-2">
            <small>
                Page ${page} sur ${pages}
                (${total} client${total !== 1 ? 's' : ''})
            </small>
        </div>
    `;

    container.innerHTML = html;
}

// Update total count badge
function updateTotalCount(total) {
    const badge = document.getElementById('totalCount');
    if (badge) {
        badge.textContent = total;
    }
}

// Load specific page
function loadPage(page) {
    loadClients(page);

    // Sauvegarder l'état après changement de page
    if (typeof window.saveClientsState === 'function') {
        setTimeout(() => window.saveClientsState(), 100);
    }
}

// Clear all filters
function clearAllFilters() {
    const searchInput = document.getElementById('searchInput');
    const balanceFilter = document.getElementById('balanceFilter');
    const collectorFilter = document.getElementById('collectorFilter');

    if (searchInput) searchInput.value = '';
    if (balanceFilter) balanceFilter.value = '';
    if (collectorFilter) collectorFilter.value = '';

    loadClients(1);

    // Mettre à jour les indicateurs de filtre
    if (typeof window.updateFilterUI === 'function') {
        setTimeout(() => window.updateFilterUI(), 100);
    }
}

// Export clients to Excel with current filters
function exportClientsToExcel() {
    const searchInput = document.getElementById('searchInput');
    const balanceFilter = document.getElementById('balanceFilter');
    const collectorFilter = document.getElementById('collectorFilter');

    const search = searchInput ? searchInput.value.trim() : '';
    const balance = balanceFilter ? balanceFilter.value : '';
    const collector = collectorFilter ? collectorFilter.value : '';

    // Build query parameters
    const params = new URLSearchParams({
        search: search,
        balance_filter: balance,
        collector_filter: collector
    });

    // Navigate to export URL
    window.location.href = `/clients/export/excel?${params.toString()}`;
}

// Initialize page
function initializeClientsPage() {
    // Check if user is read-only
    const readOnlyMeta = document.querySelector('meta[name="user-read-only"]');
    isReadOnly = readOnlyMeta ? readOnlyMeta.content === 'true' : false;

    // Setup search input with debounce
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('input', window.debounce(() => {
            loadClients(1);
            // Mettre à jour les indicateurs de filtre
            if (typeof window.updateFilterUI === 'function') {
                setTimeout(() => window.updateFilterUI(), 100);
            }
        }, 300));
    }

    // Setup balance filter
    const balanceFilter = document.getElementById('balanceFilter');
    if (balanceFilter) {
        balanceFilter.addEventListener('change', () => {
            loadClients(1);
            // Mettre à jour les indicateurs de filtre
            if (typeof window.updateFilterUI === 'function') {
                setTimeout(() => window.updateFilterUI(), 100);
            }
        });
    }

    // Setup collector filter
    const collectorFilter = document.getElementById('collectorFilter');
    if (collectorFilter) {
        collectorFilter.addEventListener('change', () => {
            loadClients(1);
            // Mettre à jour les indicateurs de filtre
            if (typeof window.updateFilterUI === 'function') {
                setTimeout(() => window.updateFilterUI(), 100);
            }
        });
    }

    // Setup clear button
    const clearButton = document.getElementById('clearFilters');
    if (clearButton) {
        clearButton.addEventListener('click', clearAllFilters);
    }

    // Setup export Excel button
    const exportButton = document.getElementById('exportExcel');
    if (exportButton) {
        exportButton.addEventListener('click', exportClientsToExcel);
    }

    // Setup delete client buttons with event delegation
    document.addEventListener('click', function(e) {
        const deleteBtn = e.target.closest('.delete-client-btn');
        if (deleteBtn) {
            e.preventDefault();
            const clientId = deleteBtn.getAttribute('data-client-id');
            const clientName = deleteBtn.getAttribute('data-client-name');
            deleteClient(clientId, clientName);
        }
    });

    // LAZY LOADING: Load clients automatically on page load
    // Check if there's a saved page state
    let initialPage = 1;
    if (typeof window.clientsCurrentPage !== 'undefined' && window.clientsCurrentPage > 1) {
        initialPage = window.clientsCurrentPage;
    }

    // Load clients immediately
    loadClients(initialPage);
}

// Delete client function
function deleteClient(clientId, clientName) {
    if (confirm(`Êtes-vous sûr de vouloir supprimer le client "${clientName}" ?\n\nCette action supprimera également toutes les factures et notes associées.`)) {
        fetch(`/clients/${clientId}/delete`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrfToken()
            }
        })
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            if (data.success) {
                alert(data.message);
                location.reload();
            } else {
                alert('Erreur: ' + data.message);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            alert('Erreur lors de la suppression: ' + error.message);
        });
    }
}

// Get CSRF token from meta tag
function getCsrfToken() {
    const metaTag = document.querySelector('meta[name="csrf-token"]');
    return metaTag ? metaTag.getAttribute('content') : '';
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    initializeClientsPage();
});
