/**
 * FinovRelance - Notification System
 * AJAX-based notification polling (compatible with Gunicorn)
 * Extracted from base.html for cache-friendliness.
 */
(function () {
    'use strict';

    let notificationCount = 0;
    let notificationPolling;
    let lastNotificationId = 0;
    let lastUnreadCount = 0;
    let currentPollingInterval = 90000;
    const POLL_INTERVAL_FAST = 45000;
    const POLL_INTERVAL_NORMAL = 90000;
    const POLL_INTERVAL_SLOW = 180000;

    function initNotifications() {
        console.log('Initializing adaptive notification polling system');

        const badge = document.getElementById('notification-badge');
        const markAllBtn = document.getElementById('mark-all-read');
        const notificationList = document.getElementById('notification-list');

        loadNotifications();
        startAdaptivePolling();

        if (markAllBtn) {
            markAllBtn.addEventListener('click', function () {
                markAllNotificationsRead();
            });
        }
    }

    function startAdaptivePolling() {
        if (notificationPolling) {
            clearInterval(notificationPolling);
        }
        notificationPolling = setInterval(loadNotifications, currentPollingInterval);
        console.log('Notification polling: ' + (currentPollingInterval / 1000) + 's interval');
    }

    function adjustPollingInterval(hasNewNotifications, unreadCount) {
        let newInterval = currentPollingInterval;

        if (hasNewNotifications || unreadCount > lastUnreadCount) {
            newInterval = POLL_INTERVAL_FAST;
        } else if (unreadCount > 0) {
            newInterval = POLL_INTERVAL_NORMAL;
        } else {
            newInterval = POLL_INTERVAL_SLOW;
        }

        if (newInterval !== currentPollingInterval) {
            currentPollingInterval = newInterval;
            startAdaptivePolling();
        }

        lastUnreadCount = unreadCount;
    }

    function loadNotifications() {
        fetch('/api/notifications/recent', {
            method: 'GET',
            credentials: 'same-origin',
            headers: {
                'X-Requested-With': 'XMLHttpRequest'
            }
        })
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (data.notifications) {
                    var unreadCount = data.notifications.filter(function (n) { return !n.is_read; }).length;
                    var hasNewNotifications = data.notifications.some(function (n) { return n.id > lastNotificationId; });

                    if (data.notifications.length > 0) {
                        var maxId = Math.max.apply(null, data.notifications.map(function (n) { return n.id; }));
                        if (maxId > lastNotificationId) {
                            lastNotificationId = maxId;
                        }
                    }

                    displayNotifications(data.notifications);
                    updateNotificationBadge();
                    adjustPollingInterval(hasNewNotifications, unreadCount);
                }
            })
            .catch(function (error) {
                console.log('Could not load notifications:', error);
                if (currentPollingInterval < POLL_INTERVAL_SLOW) {
                    currentPollingInterval = POLL_INTERVAL_SLOW;
                    startAdaptivePolling();
                }
            });
    }

    function displayNotifications(notifications) {
        var notificationList = document.getElementById('notification-list');
        notificationList.innerHTML = '';

        if (!notifications || notifications.length === 0) {
            notificationList.innerHTML =
                '<div class="text-muted text-center py-3">' +
                '<i class="fas fa-bell-slash mb-2"></i><br>' +
                'Aucune nouvelle notification' +
                '</div>';
            updateNotificationBadge(0);
            return;
        }

        var unreadCount = notifications.filter(function (n) { return !n.is_read; }).length;
        updateNotificationBadge(unreadCount);

        notifications.forEach(function (notification) {
            addNotificationToUI(notification);
        });
    }

    function updateNotificationBadge(unreadCount) {
        var badge = document.getElementById('notification-badge');
        var markAllBtn = document.getElementById('mark-all-read');

        if (unreadCount === undefined || unreadCount === null) {
            var unreadItems = document.querySelectorAll('.notification-item:not(.read)');
            unreadCount = unreadItems.length;
        }

        if (badge) {
            if (unreadCount > 0) {
                badge.textContent = unreadCount;
                badge.style.display = 'inline-block';
            } else {
                badge.style.display = 'none';
            }
        }

        if (markAllBtn) {
            markAllBtn.style.display = unreadCount > 0 ? 'inline-block' : 'none';
        }
    }

    function addNotificationToUI(notification) {
        var notificationList = document.getElementById('notification-list');

        var noNotificationMsg = notificationList.querySelector('.text-muted.text-center');
        if (noNotificationMsg) {
            noNotificationMsg.remove();
        }

        var notificationElement = document.createElement('div');
        notificationElement.className = 'notification-item p-2 mb-2 border-bottom ' + (!notification.is_read ? 'bg-light' : 'text-muted');
        notificationElement.setAttribute('data-notification-id', notification.id);

        var typeIcon = getNotificationIcon(notification.type);
        var timeAgo = getTimeAgo(notification.created_at);

        var markReadBtn = '';
        if (!notification.is_read) {
            markReadBtn =
                '<button class="btn btn-sm btn-link text-muted p-0 ms-2" onclick="window._finovMarkNotificationRead(' + notification.id + ')">' +
                '<i class="fas fa-check" style="font-size: 12px;"></i>' +
                '</button>';
        }

        notificationElement.innerHTML =
            '<div class="d-flex align-items-start">' +
            '<div class="me-2">' +
            '<i class="' + typeIcon + '" style="color: ' + getNotificationColor(notification.type) + ';"></i>' +
            '</div>' +
            '<div class="flex-grow-1">' +
            '<h6 class="mb-1 small fw-bold">' + escapeHtml(notification.title) + '</h6>' +
            '<p class="mb-1 small">' + escapeHtml(notification.message) + '</p>' +
            '<small class="text-muted">' + timeAgo + '</small>' +
            '</div>' +
            markReadBtn +
            '</div>';

        notificationList.prepend(notificationElement);
    }

    function escapeHtml(text) {
        var div = document.createElement('div');
        div.appendChild(document.createTextNode(text || ''));
        return div.innerHTML;
    }

    function getNotificationIcon(type) {
        switch (type) {
            case 'quickbooks_sync':
            case 'business_central_sync':
                return 'fas fa-sync-alt';
            case 'business_central_sync_error':
            case 'error':
                return 'fas fa-exclamation-triangle';
            case 'success':
                return 'fas fa-check-circle';
            default:
                return 'fas fa-info-circle';
        }
    }

    function getNotificationColor(type) {
        switch (type) {
            case 'quickbooks_sync':
                return '#007bff';
            case 'success':
                return '#28a745';
            case 'error':
                return '#dc3545';
            default:
                return '#6c757d';
        }
    }

    function getTimeAgo(dateString) {
        var date = new Date(dateString);
        var now = new Date();
        var diffInSeconds = Math.floor((now - date) / 1000);

        if (diffInSeconds < 60) return "A l'instant";
        if (diffInSeconds < 3600) return Math.floor(diffInSeconds / 60) + ' min';
        if (diffInSeconds < 86400) return Math.floor(diffInSeconds / 3600) + ' h';
        return Math.floor(diffInSeconds / 86400) + ' j';
    }

    function markNotificationRead(notificationId) {
        var csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');

        fetch('/api/notifications/' + notificationId + '/mark-read', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
                'X-Requested-With': 'XMLHttpRequest'
            }
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('HTTP error! status: ' + response.status);
                }
                return response.json();
            })
            .then(function (data) {
                if (data.success) {
                    var notificationElement = document.querySelector('[data-notification-id="' + notificationId + '"]');
                    if (notificationElement) {
                        notificationElement.remove();
                    }
                    updateNotificationBadge();
                    loadNotifications();
                }
            })
            .catch(function (error) { console.log('Erreur lors du marquage de notification:', error.message); });
    }

    function markAllNotificationsRead() {
        var csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');

        fetch('/api/notifications/mark-all-read', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
                'X-Requested-With': 'XMLHttpRequest'
            }
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error('HTTP error! status: ' + response.status);
                }
                return response.json();
            })
            .then(function (data) {
                if (data.success) {
                    var notificationList = document.getElementById('notification-list');
                    notificationList.innerHTML = '<div class="dropdown-item text-center text-muted">Aucune notification</div>';
                    updateNotificationBadge();
                    loadNotifications();
                }
            })
            .catch(function (error) { console.log('Erreur lors du marquage global:', error.message); });
    }

    // Expose functions needed by onclick handlers in the dynamically-created HTML
    window._finovMarkNotificationRead = markNotificationRead;
    window._finovMarkAllNotificationsRead = markAllNotificationsRead;

    // Initialize when DOM is ready (if user is authenticated)
    document.addEventListener('DOMContentLoaded', function () {
        if (document.getElementById('userDropdown')) {
            initNotifications();
        }
    });
})();
