/**
 * Receivables List - Dynamic Search, Filtering and Sorting
 * Optimized for performance with lazy loading and efficient DOM updates
 */

let currentPage = 1;
let currentSort = 'total';
let currentSortOrder = 'desc';
let isReadOnly = false;

// Project breakdown state - optimized for performance
const projectsCache = new Map(); // clientId -> {data, fetchedAt}
const expandedClients = new Set(); // Track which clients are expanded
const pendingFetches = new Map(); // clientId -> Promise (prevent duplicate requests)
const CACHE_DURATION = 5 * 60 * 1000; // 5 minutes

// Expose variables globally for state manager
window.currentPage = currentPage;
window.currentSort = currentSort;
window.currentSortOrder = currentSortOrder;

// Update sort indicators in table headers
function updateSortIndicators() {
    // Clear all sort indicators
    document.querySelectorAll('.sort-indicator').forEach(indicator => {
        indicator.innerHTML = '';
    });

    // Add icon to active sort column
    const activeHeader = document.querySelector(`th[data-sort-column="${currentSort}"] .sort-indicator`);
    if (activeHeader) {
        const iconName = currentSortOrder === 'asc' ? 'chevron-up' : 'chevron-down';
        activeHeader.innerHTML = `<i data-feather="${iconName}" class="ms-1"></i>`;

        // Re-initialize feather icons
        window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();
    }
}

// Load receivables from API
function loadReceivables(page = 1) {
    const searchInput = document.getElementById('searchInput');
    const collectorFilter = document.getElementById('collector_id');

    if (!searchInput || !collectorFilter) return;

    const search = searchInput.value.trim();
    const collector = collectorFilter.value;

    // Build query parameters
    const params = new URLSearchParams({
        page: page,
        search: search,
        collector_id: collector,
        sort_by: currentSort,
        sort_order: currentSortOrder
    });

    // Show loading state
    const container = document.getElementById('receivablesContainer');
    if (container) {
        container.style.opacity = '0.6';
        container.style.pointerEvents = 'none';
    }

    // Fetch data from API
    fetch(`/receivables/api/load?${params.toString()}`)
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            updateReceivablesTable(data.receivables);
            updatePagination(data.pagination);
            updateTotalCount(data.pagination.total);
            updateSortIndicators();
            currentPage = data.pagination.page;

            // Update global variables for state manager
            window.currentPage = currentPage;
            window.currentSort = currentSort;
            window.currentSortOrder = currentSortOrder;

            // IMPORTANT: Restore scroll BEFORE saving state to avoid overwriting saved scroll position
            // Only restore scroll if we haven't already restored it (first load after navigation)
            const shouldRestoreScroll = sessionStorage.getItem('ar_scroll_position') !== null;
            if (shouldRestoreScroll && typeof window.restoreReceivablesScroll === 'function') {
                window.restoreReceivablesScroll();
                // Clear the scroll flag after restoration
                setTimeout(() => {
                    sessionStorage.setItem('ar_scroll_restored', 'true');
                }, 200);
            }

            // Save state after successful load (after scroll restoration)
            if (typeof window.saveReceivablesState === 'function') {
                setTimeout(() => window.saveReceivablesState(), 150);
            }
        })
        .catch(error => {
            console.error('Error loading receivables:', error);
            alert('Erreur lors du chargement des comptes à recevoir');
        })
        .finally(() => {
            // Remove loading state
            if (container) {
                container.style.opacity = '1';
                container.style.pointerEvents = 'auto';
            }
        });
}

// Update receivables table with new data
function updateReceivablesTable(receivables) {
    const tbody = document.getElementById('receivablesTableBody');
    if (!tbody) return;

    // Hide initial loader and show table wrapper on first load
    const initialLoader = document.getElementById('initialLoader');
    const tableWrapper = document.getElementById('receivablesTableWrapper');
    if (initialLoader && tableWrapper) {
        initialLoader.style.display = 'none';
        tableWrapper.classList.remove('d-none');
    }

    if (receivables.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="9" class="text-center py-5">
                    <div class="mb-3">
                        <i class="fas fa-search" style="font-size: 48px; color: #6c757d;"></i>
                    </div>
                    <h5 class="text-muted">Aucun compte à recevoir trouvé</h5>
                    <p class="text-muted">Aucun résultat ne correspond à votre recherche.</p>
                </td>
            </tr>
        `;
        return;
    }

    // Build table rows
    let html = '';
    receivables.forEach(data => {
        const client = data;
        const balances = data.aged_balances;

        // Escape user-controlled data to prevent XSS
        const clientName = window.escapeHtml(client.name);
        const clientNameAttr = window.escapeHtmlAttribute(client.name);
        const clientCode = window.escapeHtml(client.code_client);
        const collectorName = client.collector ? window.escapeHtml(client.collector.full_name) : '';

        // Collector badge
        const collectorBadge = client.collector
            ? `<span class="badge bg-primary">${collectorName}</span>`
            : `<span class="badge bg-secondary">Non assigné</span>`;

        html += `
            <tr data-client-id="${client.id}" class="client-row">
                <td>
                    <a href="/clients/${client.id}" class="text-decoration-none fw-medium">
                        ${clientName}
                    </a>
                    <br>
                    <small class="text-muted">${clientCode}</small>
                </td>
                <td>${collectorBadge}</td>
                <td class="text-end">
                    <span class="amount-current">${window.formatCurrency(balances.current)}</span>
                </td>
                <td class="text-end">
                    <span class="amount-aging-0-30">${window.formatCurrency(balances.days_30)}</span>
                </td>
                <td class="text-end">
                    <span class="amount-aging-31-60">${window.formatCurrency(balances.days_60)}</span>
                </td>
                <td class="text-end">
                    <span class="amount-aging-61-90">${window.formatCurrency(balances.days_90)}</span>
                </td>
                <td class="text-end">
                    <span class="amount-aging-90-plus">${window.formatCurrency(balances.over_90)}</span>
                </td>
                <td class="text-end">
                    <strong>${window.formatCurrency(data.total_outstanding)}</strong>
                </td>
                <td>
                    <div class="dropstart">
                        <button class="btn btn-sm btn-outline-secondary"
                                type="button"
                                data-bs-toggle="dropdown"
                                aria-expanded="false"
                                title="Actions">
                            <i data-feather="more-vertical"></i>
                        </button>
                        <ul class="dropdown-menu">
                            <li>
                                <a class="dropdown-item" href="/clients/${client.id}">
                                    <i data-feather="eye" class="me-2"></i>
                                    Voir détails
                                </a>
                            </li>
                            ${!isReadOnly ? `
                            <li>
                                <button type="button"
                                       class="dropdown-item add-note-btn"
                                       data-client-id="${client.id}"
                                       data-client-name="${clientNameAttr}">
                                    <i data-feather="plus" class="me-2"></i>
                                    Ajouter une note
                                </button>
                            </li>
                            ` : ''}
                            ${(window.projectFeatureEnabled && client.has_projects) ? `
                            <li><hr class="dropdown-divider"></li>
                            <li>
                                <button type="button"
                                       class="dropdown-item expand-projects-btn"
                                       data-client-id="${client.id}"
                                       data-client-name="${clientNameAttr}">
                                    <i data-feather="plus-square" class="me-2"></i>
                                    Voir par ${window.projectLabel ? window.projectLabel.toLowerCase() : 'projet'}
                                </button>
                            </li>
                            ` : ''}
                        </ul>
                    </div>
                </td>
            </tr>
        `;
    });

    tbody.innerHTML = html;

    // Attach event listeners for "Add note" buttons
    const addNoteButtons = document.querySelectorAll('.add-note-btn');
    addNoteButtons.forEach(button => {
        button.addEventListener('click', function() {
            const clientId = this.getAttribute('data-client-id');
            const clientName = this.getAttribute('data-client-name');

            // Update modal with client info
            const modalClientName = document.getElementById('modalClientName');
            if (modalClientName) {
                modalClientName.textContent = clientName;
            }

            // Set form action URL
            const noteForm = document.getElementById('noteForm');
            if (noteForm) {
                noteForm.action = '/client/' + clientId + '/note/add?return_to=receivables';
            }

            // Reset form
            if (noteForm) {
                noteForm.reset();
                const noteDateEl = document.getElementById('note_date');
                if (noteDateEl) {
                    noteDateEl.valueAsDate = new Date();
                }
            }

            // Open modal
            const modal = new bootstrap.Modal(document.getElementById('addNoteModal'));
            modal.show();
        });
    });

    // Save state when clicking on client links
    const clientLinks = tbody.querySelectorAll('a[href*="/clients/"]');
    clientLinks.forEach(link => {
        link.addEventListener('click', function() {
            if (typeof window.saveReceivablesState === 'function') {
                window.saveReceivablesState();
            }
        });
    });

    // Re-initialize feather icons
    window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();

    // Initialize project expanders (if feature enabled)
    if (window.projectFeatureEnabled) {
        initializeProjectExpanders();
    }
}

// =============================================================================
// PROJECT EXPANSION FUNCTIONALITY
// =============================================================================

/**
 * Initialize event listeners for project expand/collapse buttons
 */
function initializeProjectExpanders() {
    const expandButtons = document.querySelectorAll('.expand-projects-btn');
    expandButtons.forEach(button => {
        button.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation();
            handleProjectToggle(this);
        });
    });
}

/**
 * Handle toggle of project breakdown for a client
 */
function handleProjectToggle(button) {
    const clientId = parseInt(button.getAttribute('data-client-id'));

    if (expandedClients.has(clientId)) {
        // Collapse
        collapseClientRow(clientId, button);
    } else {
        // Expand
        expandClientRow(clientId, button);
    }
}

/**
 * Expand client row to show project breakdown
 */
function expandClientRow(clientId, button) {
    // Change icon to loading spinner
    const icon = button.querySelector('i');
    if (icon) {
        icon.setAttribute('data-feather', 'loader');
        icon.classList.add('rotating');
        window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();
    }

    // Load project breakdown
    loadProjectBreakdown(clientId)
        .then(data => {
            // Render project rows
            renderProjectRows(clientId, data, button);
            expandedClients.add(clientId);

            // Change icon to minus
            if (icon) {
                icon.setAttribute('data-feather', 'minus-square');
                icon.classList.remove('rotating');
                window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();
            }
        })
        .catch(error => {
            console.error('Error loading projects:', error);
            alert('Erreur lors du chargement des projets');

            // Reset icon
            if (icon) {
                icon.setAttribute('data-feather', 'plus-square');
                icon.classList.remove('rotating');
                window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();
            }
        });
}

/**
 * Collapse client row to hide project breakdown
 */
function collapseClientRow(clientId, button) {
    // Remove all project rows for this client
    const projectRows = document.querySelectorAll(`tr.project-row[data-parent-client="${clientId}"]`);
    projectRows.forEach(row => row.remove());

    // Update state
    expandedClients.delete(clientId);

    // Change icon back to plus
    const icon = button.querySelector('i');
    if (icon) {
        icon.setAttribute('data-feather', 'plus-square');
        window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();
    }
}

/**
 * Load project breakdown from API with caching
 */
function loadProjectBreakdown(clientId) {
    // Check cache first
    const cached = projectsCache.get(clientId);
    if (cached && (Date.now() - cached.fetchedAt < CACHE_DURATION)) {
        return Promise.resolve(cached.data);
    }

    // Check if fetch is already in progress
    if (pendingFetches.has(clientId)) {
        return pendingFetches.get(clientId);
    }

    // Fetch from API
    const fetchPromise = fetch(`/receivables/api/client/${clientId}/projects_breakdown`)
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            // Cache the result
            projectsCache.set(clientId, {
                data: data,
                fetchedAt: Date.now()
            });

            // Remove from pending
            pendingFetches.delete(clientId);

            return data;
        })
        .catch(error => {
            // Remove from pending on error
            pendingFetches.delete(clientId);
            throw error;
        });

    // Store pending fetch
    pendingFetches.set(clientId, fetchPromise);

    return fetchPromise;
}

/**
 * Render project rows under the client row
 */
function renderProjectRows(clientId, data, button) {
    const clientRow = button.closest('tr');
    if (!clientRow) return;

    const projects = data.breakdown || [];
    if (projects.length === 0) {
        // No projects - show message
        const messageRow = buildNoProjectsRow(clientId, data.project_label || 'projet');
        clientRow.insertAdjacentHTML('afterend', messageRow);
    } else {
        // Build all rows HTML in correct order
        let allRowsHtml = '';
        projects.forEach((project, index) => {
            allRowsHtml += buildProjectRow(clientId, project, index === 0);
        });
        // Insert all rows at once after the client row
        clientRow.insertAdjacentHTML('afterend', allRowsHtml);
    }

    // Re-initialize feather icons for new rows
    window.safeFeatherReplace ? window.safeFeatherReplace() : feather.replace();
}

/**
 * Build HTML for a project row
 */
function buildProjectRow(clientId, project, isFirst) {
    const projectName = window.escapeHtml(project.project_name);

    return `
        <tr class="project-row ${isFirst ? 'project-row-first' : ''}" data-parent-client="${clientId}">
            <td class="ps-5">
                <div class="d-flex align-items-center">
                    <i data-feather="corner-down-right" class="text-muted me-2" style="width: 16px; height: 16px;"></i>
                    <span class="text-muted">${projectName}</span>
                </div>
            </td>
            <td></td>
            <td class="text-end text-muted">${window.formatCurrency(project.current)}</td>
            <td class="text-end text-muted">${window.formatCurrency(project['30_days'])}</td>
            <td class="text-end text-muted">${window.formatCurrency(project['60_days'])}</td>
            <td class="text-end text-muted">${window.formatCurrency(project['90_days'])}</td>
            <td class="text-end text-muted">${window.formatCurrency(project.over_90_days)}</td>
            <td class="text-end"><strong class="text-muted">${window.formatCurrency(project.total)}</strong></td>
            <td></td>
        </tr>
    `;
}

/**
 * Build HTML for "no projects" message row
 */
function buildNoProjectsRow(clientId, projectLabel) {
    return `
        <tr class="project-row project-row-first" data-parent-client="${clientId}">
            <td colspan="9" class="ps-5 text-muted fst-italic">
                <i data-feather="info" class="me-2" style="width: 16px; height: 16px;"></i>
                Aucun ${projectLabel.toLowerCase()} trouvé pour ce client
            </td>
        </tr>
    `;
}

// =============================================================================
// END PROJECT EXPANSION
// =============================================================================

// Update pagination controls
function updatePagination(pagination) {
    const container = document.getElementById('paginationContainer');
    if (!container) return;

    if (pagination.total === 0) {
        container.innerHTML = '';
        return;
    }

    let html = '<div class="d-flex justify-content-between align-items-center">';
    html += `<div class="text-muted">Total: ${pagination.total} compte(s)</div>`;
    html += '<nav><ul class="pagination mb-0">';

    // Previous button
    if (pagination.has_prev) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="changePage(${pagination.page - 1}); return false;">Précédent</a></li>`;
    } else {
        html += '<li class="page-item disabled"><span class="page-link">Précédent</span></li>';
    }

    // Page numbers (show current and adjacent pages)
    const startPage = Math.max(1, pagination.page - 2);
    const endPage = Math.min(pagination.pages, pagination.page + 2);

    if (startPage > 1) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="changePage(1); return false;">1</a></li>`;
        if (startPage > 2) {
            html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
        }
    }

    for (let i = startPage; i <= endPage; i++) {
        if (i === pagination.page) {
            html += `<li class="page-item active"><span class="page-link">${i}</span></li>`;
        } else {
            html += `<li class="page-item"><a class="page-link" href="#" onclick="changePage(${i}); return false;">${i}</a></li>`;
        }
    }

    if (endPage < pagination.pages) {
        if (endPage < pagination.pages - 1) {
            html += '<li class="page-item disabled"><span class="page-link">...</span></li>';
        }
        html += `<li class="page-item"><a class="page-link" href="#" onclick="changePage(${pagination.pages}); return false;">${pagination.pages}</a></li>`;
    }

    // Next button
    if (pagination.has_next) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="changePage(${pagination.page + 1}); return false;">Suivant</a></li>`;
    } else {
        html += '<li class="page-item disabled"><span class="page-link">Suivant</span></li>';
    }

    html += '</ul></nav></div>';
    container.innerHTML = html;
}

// Update total count badge
function updateTotalCount(total) {
    const badge = document.getElementById('totalCount');
    if (badge) {
        badge.textContent = total;
    }
}

// Change page
function changePage(page) {
    loadReceivables(page);
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

// Change sort column
function changeSortColumn(column) {
    if (currentSort === column) {
        // Toggle sort order
        currentSortOrder = currentSortOrder === 'asc' ? 'desc' : 'asc';
    } else {
        // New column, default to desc
        currentSort = column;
        currentSortOrder = 'desc';
    }

    // Update global variables
    window.currentSort = currentSort;
    window.currentSortOrder = currentSortOrder;

    loadReceivables(1);
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    // Check if user is read-only
    const readOnlyMeta = document.querySelector('meta[name="user-read-only"]');
    isReadOnly = readOnlyMeta && readOnlyMeta.getAttribute('content') === 'true';

    // Setup search input with debounce
    const searchInput = document.getElementById('searchInput');
    if (searchInput) {
        searchInput.addEventListener('input', window.debounce(function() {
            loadReceivables(1);
        }, 300));
    }

    // Setup collector filter
    const collectorFilter = document.getElementById('collector_id');
    if (collectorFilter) {
        collectorFilter.addEventListener('change', function() {
            loadReceivables(1);
        });
    }

    // Setup clear filters button
    const clearButton = document.getElementById('clearFilters');
    if (clearButton) {
        clearButton.addEventListener('click', function() {
            if (searchInput) searchInput.value = '';
            if (collectorFilter) collectorFilter.value = '';
            currentSort = 'total';
            currentSortOrder = 'desc';

            // Update global variables
            window.currentSort = currentSort;
            window.currentSortOrder = currentSortOrder;

            // Clear saved state
            if (typeof window.clearReceivablesState === 'function') {
                window.clearReceivablesState();
            }

            loadReceivables(1);
        });
    }

    // Setup table header sorting with event delegation
    const tableHeader = document.querySelector('.receivables-table thead');
    if (tableHeader) {
        tableHeader.addEventListener('click', function(e) {
            const th = e.target.closest('th[data-sort-column]');
            if (th) {
                changeSortColumn(th.dataset.sortColumn);
            }
        });
    }

    // Handle dropdown auto-close (close other dropdowns when opening a new one)
    // Using event delegation since dropdowns are generated dynamically
    document.addEventListener('click', function(event) {
        // Check if clicked element is a dropdown toggle
        const dropdownToggle = event.target.closest('.dropstart .btn[data-bs-toggle="dropdown"]');
        if (dropdownToggle) {
            // Close all other open dropdowns
            setTimeout(function() {
                const allDropdowns = document.querySelectorAll('.dropstart .dropdown-menu.show');
                allDropdowns.forEach(function(menu) {
                    if (menu.previousElementSibling !== dropdownToggle) {
                        const bsDropdown = bootstrap.Dropdown.getInstance(menu.previousElementSibling);
                        if (bsDropdown) {
                            bsDropdown.hide();
                        }
                    }
                });
            }, 10);
        }
    });

    // Try to restore state from previous visit
    let initialPage = 1;

    if (typeof window.restoreReceivablesState === 'function') {
        const stateRestored = window.restoreReceivablesState();

        if (stateRestored) {
            // Get the saved page from sessionStorage
            const savedState = JSON.parse(sessionStorage.getItem('ar_page_state'));
            if (savedState && savedState.page) {
                initialPage = savedState.page;
            }

            // Update local variables from restored global ones
            currentSort = window.currentSort || 'total';
            currentSortOrder = window.currentSortOrder || 'desc';
        }
    }

    // Initial load
    loadReceivables(initialPage);
});
