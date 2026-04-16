// ==========================================================================
// FinovRelance - Cookie Consent (Loi 25 / PIPEDA)
// Système granulaire : essentiel / statistiques / marketing
// ==========================================================================

(function() {
    'use strict';

    var CONSENT_KEY = 'finova_cookie_consent';
    var CONSENT_VERSION = '1.0';

    // ── GTM Consent Mode (default denied) ─────────────────────
    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    gtag('consent', 'default', {
        analytics_storage: 'denied',
        ad_storage: 'denied',
        ad_user_data: 'denied',
        ad_personalization: 'denied',
        wait_for_update: 500
    });

    // ── Read stored consent ───────────────────────────────────
    function getConsent() {
        try {
            var stored = localStorage.getItem(CONSENT_KEY);
            if (!stored) return null;
            var data = JSON.parse(stored);
            if (data.consentVersion !== CONSENT_VERSION) return null;
            if (!data.cguAccepted || !data.privacyAccepted) return null;
            return data;
        } catch (e) {
            return null;
        }
    }

    function saveConsent(preferences, cguAccepted, privacyAccepted) {
        var data = {
            preferences: {
                essential: true,
                statistics: !!preferences.statistics,
                marketing: !!preferences.marketing
            },
            cguAccepted: !!cguAccepted,
            privacyAccepted: !!privacyAccepted,
            consentDate: new Date().toISOString(),
            consentVersion: CONSENT_VERSION
        };
        localStorage.setItem(CONSENT_KEY, JSON.stringify(data));
        applyConsent(data);
        hideBanner();
        logConsentToServer(data);
    }

    // ── Apply consent (GTM) ────────────────────────────────────
    function applyConsent(data) {
        var prefs = data.preferences;

        // GTM consent mode update
        var analyticsGranted = prefs.statistics ? 'granted' : 'denied';
        var adsGranted = prefs.marketing ? 'granted' : 'denied';
        gtag('consent', 'update', {
            analytics_storage: analyticsGranted,
            ad_storage: adsGranted,
            ad_user_data: adsGranted,
            ad_personalization: adsGranted
        });

    }

    // ── Log to server ─────────────────────────────────────────
    function logConsentToServer(data) {
        try {
            fetch('/auth/api/log-cookie-consent', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    accepted: data.preferences.statistics || data.preferences.marketing,
                    preferences: data.preferences,
                    consentVersion: data.consentVersion
                })
            }).catch(function() {});
        } catch (e) {}
    }

    // ── Banner show/hide ──────────────────────────────────────
    function showBanner() {
        var banner = document.getElementById('cookieConsentBanner');
        if (!banner) return;
        banner.style.display = 'block';
        setTimeout(function() { banner.classList.add('cc-visible'); }, 50);
    }

    function hideBanner() {
        var banner = document.getElementById('cookieConsentBanner');
        if (!banner) return;
        banner.classList.remove('cc-visible');
        setTimeout(function() { banner.style.display = 'none'; }, 300);
    }

    // ── Public API ────────────────────────────────────────────
    window.ccAcceptAll = function() {
        saveConsent({ essential: true, statistics: true, marketing: true }, true, true);
    };

    window.ccRejectAll = function() {
        saveConsent({ essential: true, statistics: false, marketing: false }, true, true);
    };

    window.ccSavePreferences = function() {
        var stats = document.getElementById('ccStatistics');
        var mktg = document.getElementById('ccMarketing');
        saveConsent({
            essential: true,
            statistics: stats ? stats.checked : false,
            marketing: mktg ? mktg.checked : false
        }, true, true);
    };

    window.ccShowCustomize = function() {
        document.getElementById('ccSimpleView').style.display = 'none';
        document.getElementById('ccCustomizeView').style.display = 'block';
        // Restore current preferences to toggles
        var consent = getConsent();
        if (consent) {
            var stats = document.getElementById('ccStatistics');
            var mktg = document.getElementById('ccMarketing');
            if (stats) stats.checked = consent.preferences.statistics;
            if (mktg) mktg.checked = consent.preferences.marketing;
        }
    };

    window.ccShowSimple = function() {
        document.getElementById('ccSimpleView').style.display = 'block';
        document.getElementById('ccCustomizeView').style.display = 'none';
    };

    window.openCookieSettings = function() {
        document.getElementById('ccSimpleView').style.display = 'block';
        document.getElementById('ccCustomizeView').style.display = 'none';
        showBanner();
    };

    // ── Init on DOMContentLoaded ──────────────────────────────
    document.addEventListener('DOMContentLoaded', function() {
        var consent = getConsent();
        if (consent) {
            applyConsent(consent);
        } else {
            showBanner();
        }
    });
})();
