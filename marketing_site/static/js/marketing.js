// ==========================================================================
// FinovRelance - Site Marketing
// JavaScript Interactions
// ==========================================================================

document.addEventListener('DOMContentLoaded', function() {

    // ==========================================================================
    // Mobile Menu Toggle
    // ==========================================================================

    const burger = document.querySelector('.burger');
    const navMenu = document.querySelector('.nav-menu');

    if (burger && navMenu) {
        burger.addEventListener('click', () => {
            navMenu.classList.toggle('active');
        });

        // Close menu when clicking on a link
        const navLinks = document.querySelectorAll('.nav-link');
        navLinks.forEach(link => {
            link.addEventListener('click', () => {
                navMenu.classList.remove('active');
            });
        });

        // Close menu when clicking outside
        document.addEventListener('click', (e) => {
            if (!burger.contains(e.target) && !navMenu.contains(e.target)) {
                navMenu.classList.remove('active');
            }
        });
    }

    // Glassmorphism navbar mobile burger
    const lpBurger = document.querySelector('.lp-navbar-burger');
    const lpMobile = document.querySelector('.lp-navbar-mobile');
    if (lpBurger && lpMobile) {
        lpBurger.addEventListener('click', () => {
            lpMobile.classList.toggle('open');
        });
        document.addEventListener('click', (e) => {
            if (!lpBurger.contains(e.target) && !lpMobile.contains(e.target)) {
                lpMobile.classList.remove('open');
            }
        });
    }

    // ==========================================================================
    // Smooth Scroll for Anchor Links
    // ==========================================================================

    const anchorLinks = document.querySelectorAll('a[href^="#"]');

    anchorLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            const href = this.getAttribute('href');

            // Skip if href is just "#"
            if (href === '#') return;

            e.preventDefault();

            const target = document.querySelector(href);
            if (!target) return;

            const headerEl = document.querySelector('.header') || document.querySelector('.lp-navbar');
            const headerHeight = headerEl ? headerEl.offsetHeight : 80;
            const targetPosition = target.offsetTop - headerHeight - 20;

            window.scrollTo({
                top: targetPosition,
                behavior: 'smooth'
            });
        });
    });

    // ==========================================================================
    // FAQ Accordion
    // ==========================================================================

    const faqQuestions = document.querySelectorAll('.faq-question');

    faqQuestions.forEach(question => {
        question.addEventListener('click', () => {
            const faqItem = question.closest('.faq-item');
            const isActive = faqItem.classList.contains('active');

            // Close all FAQ items
            document.querySelectorAll('.faq-item').forEach(item => {
                item.classList.remove('active');
            });

            // Open clicked item if it wasn't active
            if (!isActive) {
                faqItem.classList.add('active');
            }
        });
    });

    // ==========================================================================
    // Form Validation (Contact Form)
    // ==========================================================================

    const contactForm = document.querySelector('#contact-form');

    if (contactForm) {
        contactForm.addEventListener('submit', function(e) {
            const name = document.querySelector('#name').value.trim();
            const email = document.querySelector('#email').value.trim();
            const subject = document.querySelector('#subject').value.trim();
            const message = document.querySelector('#message').value.trim();

            // Simple validation
            let isValid = true;

            if (name === '') {
                showError('name', 'Le nom est requis');
                isValid = false;
            } else {
                clearError('name');
            }

            if (email === '') {
                showError('email', 'L\'email est requis');
                isValid = false;
            } else if (!isValidEmail(email)) {
                showError('email', 'Email invalide');
                isValid = false;
            } else {
                clearError('email');
            }

            if (subject === '') {
                showError('subject', 'Le sujet est requis');
                isValid = false;
            } else {
                clearError('subject');
            }

            if (message === '') {
                showError('message', 'Le message est requis');
                isValid = false;
            } else {
                clearError('message');
            }

            if (!isValid) {
                e.preventDefault();
            }
            // Si valide, le formulaire sera soumis au serveur normalement
        });
    }

    function showError(fieldId, message) {
        const field = document.querySelector(`#${fieldId}`);
        const errorDiv = document.querySelector(`#${fieldId}-error`);

        field.classList.add('error');
        if (errorDiv) {
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
        }
    }

    function clearError(fieldId) {
        const field = document.querySelector(`#${fieldId}`);
        const errorDiv = document.querySelector(`#${fieldId}-error`);

        field.classList.remove('error');
        if (errorDiv) {
            errorDiv.textContent = '';
            errorDiv.style.display = 'none';
        }
    }

    function isValidEmail(email) {
        const re = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        return re.test(email);
    }

    // ==========================================================================
    // Scroll Animations (Fade in on scroll)
    // ==========================================================================

    const observerOptions = {
        threshold: 0.15,
        rootMargin: '0px 0px -100px 0px'
    };

    const observer = new IntersectionObserver(function(entries) {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                // Add animation class when element becomes visible
                if (entry.target.classList.contains('fade-in-left') ||
                    entry.target.classList.contains('fade-in-right') ||
                    entry.target.classList.contains('fade-in-up')) {
                    entry.target.classList.add('animate');
                } else {
                    entry.target.style.opacity = '1';
                    entry.target.style.transform = 'translateY(0)';
                }
            }
        });
    }, observerOptions);

    // Elements to animate
    const fadeElements = document.querySelectorAll('.card, .feature-alt, .pricing-card');
    fadeElements.forEach(el => {
        el.style.opacity = '0';
        el.style.transform = 'translateY(20px)';
        el.style.transition = 'opacity 0.6s ease-out, transform 0.6s ease-out';
        observer.observe(el);
    });

    // Observe comparison rows for fade-in animations
    const comparisonRows = document.querySelectorAll('.comparison-row');
    comparisonRows.forEach(row => {
        observer.observe(row);
    });

    // Observe feature-fullwidth sections for directional fade-in
    const featureFullwidth = document.querySelectorAll('.feature-fullwidth');
    featureFullwidth.forEach(feature => {
        observer.observe(feature);
    });

    // ==========================================================================
    // Sticky Header Effect (IntersectionObserver — no scroll listener)
    // ==========================================================================

    const header = document.querySelector('.header');

    if (header) {
        const sentinel = document.createElement('div');
        sentinel.style.height = '100px';
        sentinel.style.position = 'absolute';
        sentinel.style.top = '0';
        sentinel.style.left = '0';
        sentinel.style.width = '1px';
        sentinel.style.pointerEvents = 'none';
        document.body.prepend(sentinel);

        const headerObserver = new IntersectionObserver(([entry]) => {
            header.style.boxShadow = entry.isIntersecting
                ? 'none'
                : '0 2px 10px rgba(0, 0, 0, 0.1)';
        });
        headerObserver.observe(sentinel);
    }

});
