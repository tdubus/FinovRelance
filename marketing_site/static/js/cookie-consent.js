document.addEventListener('DOMContentLoaded', function() {
    const cookieConsent = document.getElementById('cookieConsent');
    const acceptBtn = document.getElementById('acceptCookies');
    const declineBtn = document.getElementById('declineCookies');

    const COOKIE_CONSENT_KEY = 'finovRelanceCookieConsent';
    const COOKIE_EXPIRY_DAYS = 365;

    function getCookieConsent() {
        return localStorage.getItem(COOKIE_CONSENT_KEY);
    }

    function setCookieConsent(value) {
        const expiryDate = new Date();
        expiryDate.setDate(expiryDate.getDate() + COOKIE_EXPIRY_DAYS);

        localStorage.setItem(COOKIE_CONSENT_KEY, value);
        localStorage.setItem(COOKIE_CONSENT_KEY + '_expiry', expiryDate.toISOString());
    }

    function checkConsentExpiry() {
        const expiry = localStorage.getItem(COOKIE_CONSENT_KEY + '_expiry');
        if (!expiry) return false;

        const expiryDate = new Date(expiry);
        if (new Date() <= expiryDate) return false;

        localStorage.removeItem(COOKIE_CONSENT_KEY);
        localStorage.removeItem(COOKIE_CONSENT_KEY + '_expiry');
        return true;
    }

    function showCookieBanner() {
        cookieConsent.style.display = 'block';
        setTimeout(() => {
            cookieConsent.classList.add('show');
        }, 100);
    }

    function hideCookieBanner() {
        cookieConsent.classList.remove('show');
        setTimeout(() => {
            cookieConsent.style.display = 'none';
        }, 300);
    }

    checkConsentExpiry();

    const consent = getCookieConsent();
    if (!consent) {
        showCookieBanner();
    }

    function logConsentToServer(accepted) {
        fetch('/auth/api/log-cookie-consent', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ accepted: accepted })
        }).catch(err => {
            console.error('Erreur enregistrement consentement:', err);
        });
    }

    acceptBtn.addEventListener('click', function() {
        setCookieConsent('accepted');
        logConsentToServer(true);
        hideCookieBanner();
    });

    declineBtn.addEventListener('click', function() {
        setCookieConsent('declined');
        logConsentToServer(false);
        hideCookieBanner();
    });
});
