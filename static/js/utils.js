/**
 * Utility functions shared across the application
 * Centralized helpers to avoid code duplication
 */

/**
 * Currency configuration for supported currencies
 */
window.CURRENCY_CONFIG = {
    'CAD': { locale: 'fr-CA', currency: 'CAD' },
    'USD': { locale: 'en-US', currency: 'USD' },
    'EUR': { locale: 'fr-FR', currency: 'EUR' },
    'GBP': { locale: 'en-GB', currency: 'GBP' },
    'CHF': { locale: 'fr-CH', currency: 'CHF' }
};

/**
 * Get the company's currency from the page or default to CAD
 * @returns {string} Currency code (CAD, USD, EUR, GBP, CHF)
 */
window.getCompanyCurrency = function() {
    const currencyMeta = document.querySelector('meta[name="company-currency"]');
    return currencyMeta ? currencyMeta.content : 'CAD';
};

/**
 * Format amount as currency using company's configured currency
 * @param {number} amount - Amount to format
 * @param {string} [currency] - Optional currency code override. If not provided, uses company currency.
 * @returns {string} Formatted currency string
 */
window.formatCurrency = function(amount, currency) {
    const currencyCode = currency || window.getCompanyCurrency();
    const config = window.CURRENCY_CONFIG[currencyCode] || window.CURRENCY_CONFIG['CAD'];

    return new Intl.NumberFormat(config.locale, {
        style: 'currency',
        currency: config.currency
    }).format(amount);
};

/**
 * Debounce function to limit rate of function execution
 * Uses local timeout to avoid global variable pollution
 * @param {Function} func - Function to debounce
 * @param {number} delay - Delay in milliseconds
 * @returns {Function} Debounced function
 */
window.debounce = function(func, delay) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, delay);
    };
};

/**
 * Escape HTML to prevent XSS attacks
 * @param {string} text - Text to escape
 * @returns {string} Escaped HTML
 */
window.escapeHtml = function(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
};

/**
 * Escape HTML for use in attributes (includes quotes)
 * Prevents XSS in HTML attributes by escaping quotes and special characters
 * @param {string} text - Text to escape
 * @returns {string} Escaped text safe for HTML attributes
 */
window.escapeHtmlAttribute = function(text) {
    if (!text) return '';
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
};

/**
 * Format file size in bytes to human-readable format
 * @param {number} bytes - File size in bytes
 * @returns {string} Formatted file size (e.g., "1.5 MB")
 */
window.formatFileSize = function(bytes) {
    if (bytes === 0) return '0 Bytes';
    if (!bytes) return '';

    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));

    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
};
