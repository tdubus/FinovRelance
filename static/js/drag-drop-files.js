/**
 * Drag and Drop File Handler
 * Handles file drag-and-drop and file selection with validation
 * Each drop zone maintains its own isolated file collection that persists across modal show/hide
 */

(function() {
    'use strict';

    // Configuration
    const MAX_FILE_SIZE = 20 * 1024 * 1024; // 20 MB
    const ALLOWED_EXTENSIONS = ['.pdf', '.xlsx', '.csv', '.doc', '.docx'];

    // File icons mapping
    const FILE_ICONS = {
        'pdf': 'fas fa-file-pdf text-danger',
        'xlsx': 'fas fa-file-excel text-success',
        'xls': 'fas fa-file-excel text-success',
        'csv': 'fas fa-file-csv text-info',
        'doc': 'fas fa-file-word text-primary',
        'docx': 'fas fa-file-word text-primary'
    };

    // Track initialized drop zones to prevent duplicate initialization
    const initializedDropZones = new WeakSet();

    /**
     * Initialize drag and drop functionality for a specific drop zone
     * Each drop zone has its own isolated file collection that persists across modal show/hide
     * Prevents duplicate initialization using WeakSet tracking
     *
     * @param {string} dropZoneId - ID of the drop zone element
     * @param {string} fileInputId - ID of the file input element
     * @param {string} filesListId - ID of the files list container
     * @param {string} filesListItemsId - ID of the files list items container
     * @param {string} totalSizeId - ID of the total size display element
     * @returns {Object|null} API with reset(), getFiles(), and hydrateFiles() methods, or null if already initialized or elements not found
     */
    window.initializeDragAndDrop = function(dropZoneId, fileInputId, filesListId, filesListItemsId, totalSizeId) {
        const dropZone = document.getElementById(dropZoneId);
        const fileInput = document.getElementById(fileInputId);
        const filesList = document.getElementById(filesListId);
        const filesListItems = document.getElementById(filesListItemsId);
        const totalSizeElement = document.getElementById(totalSizeId);

        if (!dropZone || !fileInput) {
            console.warn('[DragDrop] Elements not found:', { dropZoneId, fileInputId });
            return null;
        }

        // Prevent duplicate initialization
        if (initializedDropZones.has(dropZone)) {
            // Return existing API if stored
            return dropZone._dragDropAPI || null;
        }

        // Mark as initialized
        initializedDropZones.add(dropZone);

        // Create isolated file storage for this drop zone
        // This persists across modal show/hide cycles
        let selectedFiles = new DataTransfer();

        // Track modal lifecycle
        const modal = dropZone.closest('.modal');
        if (modal) {
            // Refresh display when modal is shown (without resetting files)
            modal.addEventListener('show.bs.modal', function() {
                updateFilesList();
            });
        }

        // Prevent default drag behaviors
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            dropZone.addEventListener(eventName, preventDefaults, false);
        });

        // Highlight drop zone when dragging over it
        ['dragenter', 'dragover'].forEach(eventName => {
            dropZone.addEventListener(eventName, highlight, false);
        });

        ['dragleave', 'drop'].forEach(eventName => {
            dropZone.addEventListener(eventName, unhighlight, false);
        });

        // Handle dropped files
        dropZone.addEventListener('drop', handleDrop, false);

        // Handle click to select files
        dropZone.addEventListener('click', function(e) {
            if (e.target.tagName !== 'BUTTON' && e.target.tagName !== 'LABEL') {
                fileInput.click();
            }
        });

        // Handle file input change
        fileInput.addEventListener('change', function(e) {
            handleFiles(this.files);
        });

        function preventDefaults(e) {
            e.preventDefault();
            e.stopPropagation();
        }

        function highlight(e) {
            dropZone.classList.add('drag-over');
        }

        function unhighlight(e) {
            dropZone.classList.remove('drag-over');
        }

        function handleDrop(e) {
            const dt = e.dataTransfer;
            const files = dt.files;
            handleFiles(files);
        }

        /**
         * Handle file selection/drop - accumulate files with proper validation
         * Uses two-pass approach: validate first, then mutate state only if all checks pass
         */
        function handleFiles(files) {
            const filesArray = Array.from(files);
            const validFilesToAdd = [];
            let errorMessage = '';

            // First pass: validate all files and collect valid ones
            for (let file of filesArray) {
                const ext = '.' + file.name.split('.').pop().toLowerCase();

                // Check extension
                if (!ALLOWED_EXTENSIONS.includes(ext)) {
                    errorMessage = `Le fichier "${file.name}" n'est pas un format accepté. Formats acceptés : PDF, Excel, CSV, Word.`;
                    break;
                }

                // Check if file already exists (by name and size)
                let fileExists = false;
                for (let i = 0; i < selectedFiles.files.length; i++) {
                    if (selectedFiles.files[i].name === file.name && selectedFiles.files[i].size === file.size) {
                        fileExists = true;
                        break;
                    }
                }

                if (!fileExists) {
                    validFilesToAdd.push(file);
                }
            }

            // If extension error occurred, abort without mutating state
            if (errorMessage) {
                alert(errorMessage);
                return;
            }

            // Calculate what the total size would be if we add these files
            let currentTotalSize = 0;
            for (let i = 0; i < selectedFiles.files.length; i++) {
                currentTotalSize += selectedFiles.files[i].size;
            }

            let newTotalSize = currentTotalSize;
            for (let file of validFilesToAdd) {
                newTotalSize += file.size;
            }

            // Check if adding these files would exceed the limit
            if (newTotalSize > MAX_FILE_SIZE) {
                alert('La taille totale des fichiers dépasse 20 Mo. Veuillez réduire le nombre ou la taille des fichiers.');
                return;
            }

            // All validations passed, now safely add the files
            for (let file of validFilesToAdd) {
                selectedFiles.items.add(file);
            }

            // Update the file input with accumulated files
            fileInput.files = selectedFiles.files;

            // Update the display
            updateFilesList();
        }

        /**
         * Update the files list display
         */
        function updateFilesList() {
            // Check if filesListItems is still in the DOM to prevent errors on detached nodes
            if (!filesListItems || !document.body.contains(filesListItems)) {
                return;
            }

            filesListItems.innerHTML = '';

            if (selectedFiles.files.length === 0) {
                if (filesList) filesList.style.display = 'none';
                return;
            }

            if (filesList) filesList.style.display = 'block';

            let totalSize = 0;

            for (let i = 0; i < selectedFiles.files.length; i++) {
                const file = selectedFiles.files[i];
                totalSize += file.size;

                const ext = file.name.split('.').pop().toLowerCase();
                const icon = FILE_ICONS[ext] || 'fas fa-file text-secondary';

                const li = document.createElement('li');
                li.className = 'list-group-item';
                li.innerHTML = `
                    <div class="file-info">
                        <i class="${icon} file-icon"></i>
                        <div class="file-details">
                            <div class="file-name">${escapeHtml(file.name)}</div>
                            <div class="file-size">${formatFileSize(file.size)}</div>
                        </div>
                    </div>
                    <button type="button" class="btn btn-sm btn-outline-danger btn-remove" data-index="${i}">
                        <i class="fas fa-times"></i>
                    </button>
                `;

                // Add remove button event
                li.querySelector('.btn-remove').addEventListener('click', function() {
                    removeFile(parseInt(this.getAttribute('data-index')));
                });

                filesListItems.appendChild(li);
            }

            // Update total size display
            if (totalSizeElement) {
                const sizeClass = totalSize > MAX_FILE_SIZE * 0.9 ? 'text-danger' : '';
                totalSizeElement.innerHTML = `Taille totale : <span class="${sizeClass}">${formatFileSize(totalSize)}</span> / ${formatFileSize(MAX_FILE_SIZE)}`;
            }
        }

        /**
         * Remove a file from the selection
         */
        function removeFile(index) {
            // Check if filesListItems is still in the DOM
            if (!filesListItems || !document.body.contains(filesListItems)) {
                console.warn('[DragDrop] Cannot remove file: DOM element detached');
                return;
            }

            // Create new DataTransfer without the file at index
            const newFiles = new DataTransfer();
            for (let i = 0; i < selectedFiles.files.length; i++) {
                if (i !== index) {
                    newFiles.items.add(selectedFiles.files[i]);
                }
            }
            selectedFiles = newFiles;

            // Update the file input
            fileInput.files = selectedFiles.files;

            // Update display
            updateFilesList();
        }

        /**
         * Format file size for display
         */
        function formatFileSize(bytes) {
            if (bytes === 0) return '0 octets';
            const k = 1024;
            const sizes = ['octets', 'Ko', 'Mo'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        /**
         * Escape HTML to prevent XSS
         */
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Public API for explicit control
        const api = {
            /**
             * Explicitly reset all files - call this after successful email send or when composing new email
             */
            reset: function() {
                selectedFiles = new DataTransfer();
                fileInput.files = selectedFiles.files;
                updateFilesList();
            },

            /**
             * Get current files (for draft saving or inspection)
             */
            getFiles: function() {
                return Array.from(selectedFiles.files);
            },

            /**
             * Hydrate from existing files (for draft editing)
             * NOTE: This is a placeholder for future implementation when draft editing is added.
             * Currently not used as the application only supports composing new emails.
             *
             * To implement in future:
             * 1. Fetch file data from server as Blob
             * 2. Create File objects from Blobs
             * 3. Call this method with the File array
             *
             * @param {File[]} files - Array of File objects to hydrate
             */
            hydrateFiles: function(files) {
                selectedFiles = new DataTransfer();
                for (let file of files) {
                    selectedFiles.items.add(file);
                }
                fileInput.files = selectedFiles.files;
                updateFilesList();
            }
        };

        // Store API reference on the drop zone for retrieval
        dropZone._dragDropAPI = api;

        return api;
    };
})();
