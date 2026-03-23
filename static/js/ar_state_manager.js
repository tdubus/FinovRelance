/**
 * Accounts Receivable State Manager (Lazy Load Version)
 * Sauvegarde et restaure l'état de la page Comptes à recevoir (filtres, tri, pagination, scroll)
 * Utilise SessionStorage pour persister durant la session
 * Compatible avec le système de lazy loading de receivables.js
 */

(function() {
    'use strict';

    const STATE_KEY = 'ar_page_state';
    const SCROLL_KEY = 'ar_scroll_position';

    // Fonction pour sauvegarder l'état actuel
    function saveState() {
        try {
            // Lire les valeurs depuis les inputs et les variables globales de receivables.js
            const searchInput = document.getElementById('searchInput');
            const collectorFilter = document.getElementById('collector_id');

            const state = {
                search: searchInput?.value || '',
                collector_id: collectorFilter?.value || '',
                sort_by: window.currentSort || 'total',
                sort_order: window.currentSortOrder || 'desc',
                page: window.currentPage || 1,
                timestamp: Date.now()
            };

            sessionStorage.setItem(STATE_KEY, JSON.stringify(state));

            // Sauvegarder aussi la position de scroll
            sessionStorage.setItem(SCROLL_KEY, window.scrollY.toString());
        } catch (e) {
            console.error('Error saving AR state:', e);
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

            // Restaurer les valeurs dans les inputs
            const searchInput = document.getElementById('searchInput');
            const collectorFilter = document.getElementById('collector_id');

            if (searchInput && state.search) {
                searchInput.value = state.search;
            }

            if (collectorFilter && state.collector_id) {
                collectorFilter.value = state.collector_id;
            }

            // Mettre à jour les variables globales de receivables.js
            if (typeof window.currentSort !== 'undefined' && state.sort_by) {
                window.currentSort = state.sort_by;
            }

            if (typeof window.currentSortOrder !== 'undefined' && state.sort_order) {
                window.currentSortOrder = state.sort_order;
            }

            return true;
        } catch (e) {
            console.error('Error restoring AR state:', e);
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
            }, 100);
        } catch (e) {
            console.error('Error restoring scroll:', e);
        }
    }

    // Fonction pour nettoyer l'état
    function clearState() {
        sessionStorage.removeItem(STATE_KEY);
        sessionStorage.removeItem(SCROLL_KEY);
    }

    // Initialisation au chargement de la page
    document.addEventListener('DOMContentLoaded', function() {
        // Si on est sur la page AR, configurer les hooks
        if (window.location.pathname.includes('/receivables')) {
            // Restauration automatique si on vient d'un détail client
        }
    });

    // Exposer les fonctions globalement pour receivables.js
    window.saveReceivablesState = saveState;
    window.restoreReceivablesState = restoreState;
    window.clearReceivablesState = clearState;
    window.restoreReceivablesScroll = restoreScroll;
})();
