/**
 * Clients State Manager
 * Sauvegarde et restaure l'état de la page Clients (filtres, pagination, scroll)
 * Utilise SessionStorage pour persister durant la session
 */

(function() {
    'use strict';

    const STATE_KEY = 'clients_page_state';
    const SCROLL_KEY = 'clients_scroll_position';

    // Fonction pour extraire les paramètres de l'URL
    function getURLParams() {
        const params = new URLSearchParams(window.location.search);
        return {
            search: params.get('search') || '',
            balance_filter: params.get('balance_filter') || '',
            page: params.get('page') || ''
        };
    }

    // Fonction pour sauvegarder l'état actuel
    function saveState() {
        try {
            const state = {
                search: document.getElementById('searchInput')?.value || '',
                balance_filter: document.getElementById('balanceFilter')?.value || '',
                page: window.clientsCurrentPage || 1,
                timestamp: Date.now()
            };

            sessionStorage.setItem(STATE_KEY, JSON.stringify(state));

            // Sauvegarder aussi la position de scroll
            sessionStorage.setItem(SCROLL_KEY, window.scrollY.toString());
        } catch (e) {
            console.error('Error saving Clients state:', e);
        }
    }

    // Fonction pour restaurer l'état
    function restoreState() {
        try {
            const savedState = sessionStorage.getItem(STATE_KEY);
            if (!savedState) {
                return false;
            }

            const state = JSON.parse(savedState);

            // Vérifier que l'état n'est pas trop ancien (max 1 heure)
            if (Date.now() - state.timestamp > 3600000) {
                sessionStorage.removeItem(STATE_KEY);
                sessionStorage.removeItem(SCROLL_KEY);
                return false;
            }

            // Restaurer les valeurs des filtres
            const searchInput = document.getElementById('searchInput');
            const balanceFilter = document.getElementById('balanceFilter');

            if (searchInput && state.search) {
                searchInput.value = state.search;
            }

            if (balanceFilter && state.balance_filter) {
                balanceFilter.value = state.balance_filter;
            }

            // Restaurer la page
            if (state.page) {
                window.clientsCurrentPage = parseInt(state.page, 10);
            }

            return true;
        } catch (e) {
            console.error('Error restoring Clients state:', e);
            return false;
        }
    }

    // Fonction pour restaurer le scroll uniquement
    function restoreScroll() {
        try {
            const savedScroll = sessionStorage.getItem(SCROLL_KEY);
            if (!savedScroll) return;

            setTimeout(() => {
                window.scrollTo({
                    top: parseInt(savedScroll, 10),
                    behavior: 'smooth'
                });
            }, 200);
        } catch (e) {
            console.error('Error restoring scroll:', e);
        }
    }

    // Fonction pour vérifier si des filtres sont actifs
    function hasActiveFilters() {
        const searchInput = document.getElementById('searchInput');
        const balanceFilter = document.getElementById('balanceFilter');

        return !!(
            (searchInput && searchInput.value) ||
            (balanceFilter && balanceFilter.value)
        );
    }

    // Fonction pour compter le nombre de filtres actifs
    function countActiveFilters() {
        let count = 0;
        const searchInput = document.getElementById('searchInput');
        const balanceFilter = document.getElementById('balanceFilter');

        if (searchInput && searchInput.value) count++;
        if (balanceFilter && balanceFilter.value) count++;

        return count;
    }

    // Fonction pour améliorer le bouton "Effacer" quand des filtres sont actifs
    function enhanceClearButton() {
        const clearButton = document.getElementById('clearFilters');
        if (!clearButton) return;

        const filterCount = countActiveFilters();

        if (filterCount > 0) {
            // Rendre le bouton plus visible
            clearButton.classList.remove('btn-outline-secondary');
            clearButton.classList.add('btn-warning');

            // Ajouter un badge avec le nombre de filtres
            if (!clearButton.querySelector('.badge')) {
                const badge = document.createElement('span');
                badge.className = 'badge bg-danger ms-1';
                badge.textContent = filterCount;
                badge.style.fontSize = '0.7rem';
                clearButton.appendChild(badge);
            }
        } else {
            // Remettre le style normal
            clearButton.classList.remove('btn-warning');
            clearButton.classList.add('btn-outline-secondary');
            const badge = clearButton.querySelector('.badge');
            if (badge) {
                badge.remove();
            }
        }
    }

    // Fonction pour afficher un indicateur de filtres actifs
    function showFilterIndicator() {
        const filterCard = document.querySelector('.card.mb-4 .card-body');
        if (!filterCard) return;

        const existingIndicator = document.getElementById('filter-indicator');

        if (!hasActiveFilters()) {
            // Supprimer l'indicateur s'il existe
            if (existingIndicator) {
                existingIndicator.remove();
            }
            return;
        }

        // Si l'indicateur existe déjà, le mettre à jour
        if (existingIndicator) {
            const filterCount = countActiveFilters();
            const strongElement = existingIndicator.querySelector('strong');
            if (strongElement) {
                strongElement.textContent = filterCount.toString();
                const textNode = strongElement.nextSibling;
                if (textNode) {
                    textNode.textContent = ` filtre${filterCount > 1 ? 's' : ''} actif${filterCount > 1 ? 's' : ''}`;
                }
            }
            return;
        }

        // Créer un nouvel indicateur
        const filterCount = countActiveFilters();
        const indicator = document.createElement('div');
        indicator.id = 'filter-indicator';
        indicator.className = 'alert alert-info d-flex align-items-center mb-3';

        const icon = document.createElement('i');
        icon.setAttribute('data-feather', 'filter');
        icon.className = 'me-2';

        const textSpan = document.createElement('span');
        const strongElement = document.createElement('strong');
        strongElement.textContent = filterCount.toString();

        textSpan.appendChild(strongElement);
        textSpan.appendChild(document.createTextNode(` filtre${filterCount > 1 ? 's' : ''} actif${filterCount > 1 ? 's' : ''}`));

        indicator.appendChild(icon);
        indicator.appendChild(textSpan);

        filterCard.parentNode.insertBefore(indicator, filterCard.parentNode.firstChild);

        // Réactiver les icônes Feather
        if (typeof window.safeFeatherReplace === 'function') window.safeFeatherReplace();
    }

    // Fonction pour nettoyer l'état
    function clearState() {
        sessionStorage.removeItem(STATE_KEY);
        sessionStorage.removeItem(SCROLL_KEY);
    }

    // Initialisation au chargement de la page
    document.addEventListener('DOMContentLoaded', function() {
        // Si on est sur la page Clients
        if (window.location.pathname.includes('/clients/') && !window.location.pathname.match(/\/clients\/\d+/)) {
            const fromClientDetail = document.referrer.includes('/clients/') && document.referrer.match(/\/clients\/\d+/);

            // Restaurer l'état si on vient d'un détail client
            if (fromClientDetail) {
                const stateRestored = restoreState();

                // Si l'état a été restauré, charger les clients avec ces filtres
                if (stateRestored && typeof loadClients === 'function') {
                    const savedState = JSON.parse(sessionStorage.getItem(STATE_KEY));
                    loadClients(savedState.page || 1);

                    // Restaurer le scroll après le chargement
                    setTimeout(restoreScroll, 500);
                }
            }

            // Améliorer l'UX des filtres
            showFilterIndicator();
            enhanceClearButton();

            // Sauvegarder l'état quand on clique sur un lien client
            setTimeout(() => {
                document.querySelectorAll('a[href*="/clients/"]').forEach(link => {
                    // Ne pas intercepter les liens vers la liste ou nouveau client
                    if (link.getAttribute('href') === '/clients/' ||
                        link.getAttribute('href').includes('/new') ||
                        link.getAttribute('href').includes('/edit')) {
                        return;
                    }

                    link.addEventListener('click', function() {
                        saveState();
                    });
                });
            }, 1000);

            // Sauvegarder quand on change les filtres
            const searchInput = document.getElementById('searchInput');
            const balanceFilter = document.getElementById('balanceFilter');

            if (searchInput) {
                searchInput.addEventListener('input', function() {
                    saveState();
                    updateFilterUI();
                });
            }

            if (balanceFilter) {
                balanceFilter.addEventListener('change', function() {
                    saveState();
                    updateFilterUI();
                });
            }

            // Intercepter le bouton "Effacer" pour nettoyer l'état
            const clearButton = document.getElementById('clearFilters');
            if (clearButton) {
                clearButton.addEventListener('click', function() {
                    clearState();
                    // Mettre à jour l'UI après que les filtres soient effacés
                    setTimeout(() => {
                        updateFilterUI();
                    }, 500);
                });
            }
        }

        // Intercepter les liens vers la page Clients depuis d'autres pages
        if (!window.location.pathname.includes('/clients/')) {
            document.querySelectorAll('a[href="/clients/"], a[href$="/clients/"]').forEach(link => {
                const href = link.getAttribute('href');
                const hasParams = href.includes('?');

                // Si le lien a déjà des paramètres, ne pas intercepter
                if (hasParams) {
                    return;
                }

                // Ne rien faire de spécial, juste laisser la navigation normale
                // L'état sera restauré au chargement de la page si on vient d'un détail client
            });
        }
    });

    // Fonction pour mettre à jour l'UI des filtres (indicateur et bouton)
    function updateFilterUI() {
        showFilterIndicator();
        enhanceClearButton();
    }

    // Exposer les fonctions globalement pour être appelées depuis clients.js
    window.saveClientsState = saveState;
    window.updateFilterUI = updateFilterUI;
})();
