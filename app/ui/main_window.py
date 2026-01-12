"""
Main application window using PySide6.
"""

import logging
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QLabel, QTableWidget, QTableWidgetItem,
    QFileDialog, QMessageBox, QProgressBar, QStatusBar,
    QHeaderView, QGroupBox, QTextEdit, QSplitter, QTabWidget,
    QLineEdit, QCompleter, QListWidget, QListWidgetItem, QComboBox,
    QApplication, QCheckBox, QProgressDialog, QInputDialog, QFrame
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QFont, QIcon, QDesktopServices, QShortcut, QKeySequence
import json
import os
import subprocess
import time

from app.core.scan import scan_directory, get_directory_stats
from app.core.plan import create_move_plan, validate_move_plan, get_plan_summary
from app.core.apply import apply_moves, validate_destination_space
from app.core.settings import settings
from app.core.search import search_service
from app.core.database import file_index
from app.core.supabase_client import supabase_auth
from app.core.query_parser import (
    parse_query, get_date_range, TYPE_EXTENSIONS,
    UI_DATE_MAPPING, UI_TYPE_MAPPING, FILTER_TO_UI_DATE, FILTER_TO_UI_TYPE
)
from app.ui.quick_search_overlay import QuickSearchOverlay
from app.ui.win_hotkey import register_global_hotkey, unregister_global_hotkey, get_foreground_hwnd, set_foreground_hwnd, set_foreground_hwnd_robust, get_window_rect
from app.ui.theme_manager import theme_manager


logger = logging.getLogger(__name__)

# QuickSearch heuristics: localized button/label names
CONFIRM_NAMES = [
    "Open", "Save", "OK", "Select", "Choose"
]
FILENAME_LABELS = [
    "File name:", "Filename:", "Name:", "Dateiname:", "Nom du fichier:", "Nombre de archivo:",
]


class ScanWorker(QThread):
    """Worker thread for directory scanning."""
    
    scan_completed = Signal(list)
    scan_error = Signal(str)
    progress_updated = Signal(str)
    
    def __init__(self, source_path: Path):
        super().__init__()
        self.source_path = source_path
    
    def run(self):
        try:
            self.progress_updated.emit("Scanning directory...")
            files = scan_directory(self.source_path)
            
            # Add source path to each file metadata
            for file_data in files:
                file_data['source_path'] = str(self.source_path / file_data['name'])
            
            self.scan_completed.emit(files)
        except Exception as e:
            self.scan_error.emit(str(e))


class IndexWorker(QThread):
    """Worker thread for directory indexing with pause/resume support."""
    
    index_completed = Signal(dict)
    index_error = Signal(str)
    progress_updated = Signal(str)
    progress_percent = Signal(int, int, int)  # current, total, percent
    progress_data = Signal(int, int, str)  # done, total, message - for UI updates
    
    def __init__(self, directory_path: Path):
        super().__init__()
        self.directory_path = directory_path
        self._paused = False
        self._cancelled = False
        self._pause_condition = None
    
    def pause(self):
        """Pause the indexing process."""
        self._paused = True
    
    def resume(self):
        """Resume the indexing process."""
        self._paused = False
    
    def cancel(self):
        """Cancel the indexing process."""
        self._cancelled = True
        self._paused = False  # Unpause so it can exit
    
    def is_paused(self) -> bool:
        """Check if indexing is paused."""
        return self._paused
    
    def is_cancelled(self) -> bool:
        """Check if indexing was cancelled."""
        return self._cancelled
    
    def wait_if_paused(self):
        """Block while paused (called from indexing loop)."""
        while self._paused and not self._cancelled:
            import time
            time.sleep(0.1)  # Check every 100ms
    
    def run(self):
        try:
            self.progress_updated.emit("Indexing directory...")
            result = search_service.index_directory(self.directory_path)
            self.index_completed.emit(result)
        except Exception as e:
            self.index_error.emit(str(e))


class BatchOperationWorker(QThread):
    """Worker thread for batch file operations to prevent UI freezing."""
    
    operation_completed = Signal(dict)  # Result stats
    operation_error = Signal(str)
    progress_updated = Signal(int, int, str)  # current, total, message
    
    def __init__(self, operation: str, file_ids: list = None, file_paths: list = None, extra_data: dict = None):
        super().__init__()
        self.operation = operation
        self.file_ids = file_ids or []
        self.file_paths = file_paths or []
        self.extra_data = extra_data or {}
        self._cancelled = False
    
    def cancel(self):
        """Request cancellation of the operation."""
        self._cancelled = True
    
    def run(self):
        """Execute the batch operation."""
        try:
            from app.core.file_operations import get_file_operations
            file_ops = get_file_operations()
            
            if self.operation == 'remove':
                result = file_ops.remove_from_index(self.file_ids)
            elif self.operation == 'reindex':
                def progress_cb(current, total):
                    if not self._cancelled:
                        self.progress_updated.emit(current, total, f"Re-indexing file {current}/{total}...")
                result = file_ops.reindex_files(self.file_paths, progress_callback=progress_cb)
            elif self.operation == 'add_tags':
                tags = self.extra_data.get('tags', [])
                result = file_ops.batch_add_tags(self.file_ids, tags)
            else:
                result = {'error': f'Unknown operation: {self.operation}'}
            
            self.operation_completed.emit(result)
            
        except Exception as e:
            self.operation_error.emit(str(e))


class AutoIndexWorker(QThread):
    """Background worker for auto-indexing individual files."""
    
    file_indexed = Signal(str, str)  # (filename, status: 'success'|'skipped'|'error')
    status_update = Signal(str)  # status message for UI
    
    def __init__(self):
        super().__init__()
        self._queue = []
        self._running = False
    
    def add_file(self, file_path: Path):
        """Add a file to the indexing queue."""
        self._queue.append(file_path)
        if not self._running:
            self.start()
    
    def run(self):
        """Process files in the queue."""
        import hashlib
        from datetime import datetime
        from app.core.categorize import get_file_metadata
        from app.core.database import file_index
        from app.core.vision import analyze_image, gpt_vision_fallback, _file_to_b64
        from app.core.settings import settings
        
        self._running = True
        
        while self._queue:
            file_path = self._queue.pop(0)
            
            try:
                # Check if file already exists in index with tags
                existing = file_index.get_file_by_path(str(file_path))
                if existing:
                    has_tags = existing.get('tags') and existing['tags'] not in ['[]', '', None]
                    has_label = existing.get('label') and existing['label'] not in ['', None]
                    has_caption = existing.get('caption') and existing['caption'] not in ['', None]
                    
                    if has_tags or has_label or has_caption:
                        logger.info(f"Skipping already indexed file: {file_path}")
                        self.file_indexed.emit(file_path.name, 'skipped')
                        continue
                
                # Get basic metadata
                metadata = get_file_metadata(file_path)
                metadata['source_path'] = str(file_path)
                
                # Compute content hash
                try:
                    h = hashlib.sha256()
                    with open(file_path, 'rb') as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                            h.update(chunk)
                    metadata['content_hash'] = h.hexdigest()
                except Exception:
                    metadata['content_hash'] = None
                
                metadata['last_indexed_at'] = datetime.utcnow().isoformat()
                
                # AI Vision analysis for images
                ext = file_path.suffix.lower()
                if ext in {'.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif', '.gif', '.webp', '.avif', '.heic', '.heif', '.ico', '.raw', '.cr2', '.nef', '.arw', '.pdf'}:
                    self.status_update.emit(f"Analyzing: {file_path.name}")
                    
                    if settings.use_openai_fallback:
                        image_b64 = _file_to_b64(file_path)
                        if image_b64:
                            vision = gpt_vision_fallback(image_b64, filename=file_path.name)
                            if vision:
                                metadata.update(vision)
                                metadata['ai_source'] = 'openai'
                    else:
                        vision = analyze_image(file_path)
                        if vision:
                            metadata.update(vision)
                            metadata['ai_source'] = 'local'
                
                # Add to index
                file_index.add_file(metadata)
                logger.info(f"Auto-indexed: {file_path}")
                self.file_indexed.emit(file_path.name, 'success')
                
            except Exception as e:
                logger.error(f"Error auto-indexing {file_path}: {e}")
                self.file_indexed.emit(file_path.name, 'error')
        
        self._running = False


class MainWindow(QMainWindow):
    """Main application window."""
    
    def __init__(self):
        super().__init__()
        self.source_path = None
        self.destination_path = None
        self.scanned_files = []
        self.move_plan = []
        
        # Indexing queue system
        self.index_queue = []  # List of Path objects to index
        self.is_indexing = False  # Whether indexing is in progress
        
        self.setup_ui()
        self.setup_connections()
        self.setup_quick_search()
        
        # Enable drag and drop
        self.setAcceptDrops(True)
        
        # Initialize auto-index if enabled
        if settings.auto_index_downloads:
            self._start_downloads_watcher()
        
        # Auto-load indexed files on startup
        QTimer.singleShot(100, self.refresh_debug_view)
        
        logger.info("Main window initialized")
    
    def setup_ui(self):
        """Setup the user interface."""
        self.setWindowTitle("File Search Assistant v1.0")
        self.setMinimumSize(1200, 800)
        
        # Central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        
        # Create tab widget
        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)
        
        # Create tabs
        # self.setup_organize_tab()  # Hidden for MVP - search-only mode
        self.setup_search_tab()
        self.setup_debug_tab()  # Restored: View all indexed files
        self.setup_settings_tab()
        
        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")
    
    def setup_organize_tab(self):
        """Setup the file organization tab."""
        organize_widget = QWidget()
        organize_layout = QVBoxLayout(organize_widget)
        
        # Folder selection group
        folder_group = QGroupBox("Folder Selection")
        folder_layout = QVBoxLayout(folder_group)
        
        # Source folder
        source_layout = QHBoxLayout()
        self.source_label = QLabel("Source folder: Not selected")
        self.source_label.setObjectName("secondaryLabel")
        self.source_button = QPushButton("Select Source Folder")
        source_layout.addWidget(self.source_label)
        source_layout.addWidget(self.source_button)
        folder_layout.addLayout(source_layout)
        
        # Destination folder
        dest_layout = QHBoxLayout()
        self.dest_label = QLabel("Destination folder: Not selected")
        self.dest_label.setObjectName("secondaryLabel")
        self.dest_button = QPushButton("Select Destination Folder")
        dest_layout.addWidget(self.dest_label)
        dest_layout.addWidget(self.dest_button)
        folder_layout.addLayout(dest_layout)
        
        organize_layout.addWidget(folder_group)
        
        # Action buttons
        action_layout = QHBoxLayout()
        self.scan_button = QPushButton("Scan & Plan (Dry Run)")
        self.scan_button.setEnabled(False)
        self.apply_button = QPushButton("Apply Moves")
        self.apply_button.setEnabled(False)
        action_layout.addWidget(self.scan_button)
        action_layout.addWidget(self.apply_button)
        organize_layout.addLayout(action_layout)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        organize_layout.addWidget(self.progress_bar)
        
        # Results area
        results_splitter = QSplitter(Qt.Vertical)
        
        # File table
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels([
            "File Name", "Category", "Size", "Planned Destination"
        ])
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        results_splitter.addWidget(self.file_table)
        
        # Summary text
        self.summary_text = QTextEdit()
        self.summary_text.setMaximumHeight(150)
        self.summary_text.setReadOnly(True)
        results_splitter.addWidget(self.summary_text)
        
        organize_layout.addWidget(results_splitter)
        
        # Add organize tab
        self.tab_widget.addTab(organize_widget, "Organize Files")
    
    def setup_search_tab(self):
        """Setup the search tab."""
        search_widget = QWidget()
        search_layout = QVBoxLayout(search_widget)
        
        # Indexing group
        self.index_group = QGroupBox("Index Directory for Search")
        index_layout = QVBoxLayout(self.index_group)
        
        # Drop zone for drag and drop
        self.drop_zone = QLabel("📁 Drag & drop files or folders here to index them")
        self.drop_zone.setAlignment(Qt.AlignCenter)
        self.drop_zone.setFixedHeight(70)  # Fixed height to prevent expansion
        self.drop_zone.setStyleSheet("""
            QLabel {
                border: 2px dashed #00B8D4;
                border-radius: 8px;
                background-color: rgba(0, 184, 212, 0.05);
                color: #00B8D4;
                font-size: 13px;
                padding: 10px;
            }
        """)
        index_layout.addWidget(self.drop_zone)
        
        # Add spacing after drop zone
        index_layout.addSpacing(15)
        
        # Index folder selection
        index_folder_layout = QHBoxLayout()
        self.index_label = QLabel("Index folder: Not selected")
        self.index_label.setObjectName("secondaryLabel")
        self.index_button = QPushButton("Select Folder to Index")
        index_folder_layout.addWidget(self.index_label)
        index_folder_layout.addWidget(self.index_button)
        index_layout.addLayout(index_folder_layout)
        
        # Index button
        self.index_button_action = QPushButton("Index Directory")
        self.index_button_action.setObjectName("primaryButton")
        self.index_button_action.setEnabled(False)
        index_layout.addWidget(self.index_button_action)
        
        # Pause and Cancel buttons side by side (hidden until indexing starts)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        
        self.index_pause_btn = QPushButton("⏸ Pause")
        self.index_pause_btn.setObjectName("secondaryButton")
        self.index_pause_btn.setVisible(False)
        self.index_pause_btn.setMinimumWidth(120)
        btn_row.addWidget(self.index_pause_btn)
        
        self.index_cancel_btn = QPushButton("✕ Cancel")
        self.index_cancel_btn.setVisible(False)
        self.index_cancel_btn.setMinimumWidth(120)
        btn_row.addWidget(self.index_cancel_btn)
        
        index_layout.addLayout(btn_row)
        
        # Progress bar for indexing with percentage
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p% complete (%v / %m files)")
        self.progress_bar.setMinimumWidth(300)
        self.progress_bar.setMinimumHeight(25)
        self.progress_bar.setProperty("paused", False)
        index_layout.addWidget(self.progress_bar)
        
        # Prominent percentage label
        self.index_percent_label = QLabel("0%")
        self.index_percent_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #00B8D4;")
        self.index_percent_label.setAlignment(Qt.AlignCenter)
        self.index_percent_label.setVisible(False)
        index_layout.addWidget(self.index_percent_label)
        
        # Progress label for current file
        self.index_progress_label = QLabel("")
        self.index_progress_label.setObjectName("secondaryLabel")
        self.index_progress_label.setVisible(False)
        index_layout.addWidget(self.index_progress_label)
        
        # Spacer before queue row (expands when queue is visible)
        self.queue_top_spacer = QWidget()
        self.queue_top_spacer.setFixedHeight(0)
        self.queue_top_spacer.setVisible(False)
        index_layout.addWidget(self.queue_top_spacer)
        
        # Queue indicator row with proper spacing
        self.queue_row = QFrame()  # Use QFrame for proper border styling
        self.queue_row.setObjectName("queueRow")
        self.queue_row.setVisible(False)
        self.queue_row.setMinimumHeight(45)
        self.queue_row.setStyleSheet("""
            QFrame#queueRow {
                background-color: rgba(0, 184, 212, 0.15);
                border: 1px solid rgba(0, 184, 212, 0.3);
                border-radius: 6px;
            }
        """)
        queue_row_layout = QHBoxLayout(self.queue_row)
        queue_row_layout.setContentsMargins(12, 10, 12, 10)
        queue_row_layout.setSpacing(12)
        
        self.queue_label = QLabel("📋 Queue: 0 pending")
        self.queue_label.setStyleSheet("color: #00B8D4; font-weight: bold; font-size: 13px; background: transparent;")
        queue_row_layout.addWidget(self.queue_label)
        
        self.queue_items_label = QLabel("")
        self.queue_items_label.setStyleSheet("color: #888888; font-size: 12px; background: transparent;")
        queue_row_layout.addWidget(self.queue_items_label)
        
        queue_row_layout.addStretch()
        
        self.clear_queue_btn = QPushButton("Clear")
        self.clear_queue_btn.setMaximumWidth(60)
        self.clear_queue_btn.setMaximumHeight(24)
        self.clear_queue_btn.clicked.connect(self._clear_index_queue)
        queue_row_layout.addWidget(self.clear_queue_btn)
        
        index_layout.addWidget(self.queue_row)
        
        # Add a spacer that will expand when queue is visible
        self.queue_spacer = QWidget()
        self.queue_spacer.setFixedHeight(0)  # Initially no extra space
        self.queue_spacer.setVisible(False)
        index_layout.addWidget(self.queue_spacer)
        
        search_layout.addWidget(self.index_group)
        
        # Quick Index Options group
        quick_index_group = QGroupBox("Quick Index Options")
        quick_index_layout = QVBoxLayout(quick_index_group)
        
        # Index entire PC button
        pc_index_layout = QHBoxLayout()
        self.index_pc_button = QPushButton("🖥️ Index Entire PC")
        self.index_pc_button.setToolTip("Index all files on your computer. This may take a long time.")
        pc_index_layout.addWidget(self.index_pc_button)
        pc_index_layout.addStretch()
        quick_index_layout.addLayout(pc_index_layout)
        
        # Auto-index Downloads toggle
        auto_index_layout = QHBoxLayout()
        self.auto_index_downloads_btn = QPushButton("📥 Auto-Index New Files: OFF")
        self.auto_index_downloads_btn.setCheckable(True)
        self.auto_index_downloads_btn.setChecked(settings.auto_index_downloads)
        if settings.auto_index_downloads:
            self.auto_index_downloads_btn.setText("📥 Auto-Index New Files: ON")
        self.auto_index_downloads_btn.setToolTip("Automatically index new files added to common folders (Downloads, Desktop, Documents, etc.)")
        auto_index_layout.addWidget(self.auto_index_downloads_btn)
        auto_index_layout.addStretch()
        quick_index_layout.addLayout(auto_index_layout)
        
        # Status label for auto-index
        self.auto_index_status = QLabel("")
        self.auto_index_status.setObjectName("secondaryLabel")
        quick_index_layout.addWidget(self.auto_index_status)
        
        search_layout.addWidget(quick_index_group)
        
        # Search group
        search_group = QGroupBox("Search Files")
        search_group_layout = QVBoxLayout(search_group)
        
        # Search input
        search_input_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search (operators: type:<label>, tag:<text>, has:ocr, has:vision)")
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("primaryButton")
        self.search_button.setEnabled(False)
        # New: GPT rerank toggle
        self.gpt_rerank_button = QPushButton("GPT Rerank: OFF")
        self.gpt_rerank_button.setCheckable(True)
        self.gpt_rerank_button.setChecked(settings.use_openai_search_rerank)
        if settings.use_openai_search_rerank:
            self.gpt_rerank_button.setText("GPT Rerank: ON")
        search_input_layout.addWidget(self.search_input)
        search_input_layout.addWidget(self.search_button)
        search_input_layout.addWidget(self.gpt_rerank_button)
        search_group_layout.addLayout(search_input_layout)

        # Query debug info
        self.search_debug_label = QLabel("")
        self.search_debug_label.setObjectName("secondaryLabel")
        search_group_layout.addWidget(self.search_debug_label)
        
        # Filter row
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filters:"))
        
        # File type dropdown
        self.type_filter = QComboBox()
        self.type_filter.addItems(["All Types", "Images", "Documents", "PDFs", "Videos", "Audio", "Code"])
        self.type_filter.setMinimumWidth(100)
        filter_layout.addWidget(self.type_filter)
        
        # Date range dropdown
        self.date_filter = QComboBox()
        self.date_filter.addItems(["Any Time", "Today", "Yesterday", "This Week", "This Month", "This Year"])
        self.date_filter.setMinimumWidth(100)
        filter_layout.addWidget(self.date_filter)
        
        # Clear filters button
        self.clear_filters_btn = QPushButton("Clear Filters")
        self.clear_filters_btn.setToolTip("Reset all filters")
        filter_layout.addWidget(self.clear_filters_btn)
        
        # Filter status label
        self.filter_status_label = QLabel("")
        self.filter_status_label.setObjectName("secondaryLabel")
        filter_layout.addWidget(self.filter_status_label)
        
        filter_layout.addStretch()
        search_group_layout.addLayout(filter_layout)
        
        # Quick Actions bar (hidden by default, shown when files selected)
        self.quick_actions_widget = QWidget()
        self.quick_actions_widget.setObjectName("quickActionsBar")
        quick_actions_layout = QHBoxLayout(self.quick_actions_widget)
        quick_actions_layout.setContentsMargins(8, 6, 8, 6)
        quick_actions_layout.setSpacing(8)
        
        # Selection counter
        self.selection_count_label = QLabel("0 files selected")
        self.selection_count_label.setObjectName("selectionLabel")
        quick_actions_layout.addWidget(self.selection_count_label)
        
        quick_actions_layout.addWidget(self._create_separator())
        
        # Action buttons
        self.action_remove_btn = QPushButton("🗑️ Remove from Index")
        self.action_remove_btn.setToolTip("Remove selected files from the index (files stay on PC)")
        self.action_remove_btn.setObjectName("quickActionBtn")
        quick_actions_layout.addWidget(self.action_remove_btn)
        
        self.action_reindex_btn = QPushButton("🔄 Re-index")
        self.action_reindex_btn.setToolTip("Re-scan selected files to update metadata")
        self.action_reindex_btn.setObjectName("quickActionBtn")
        quick_actions_layout.addWidget(self.action_reindex_btn)
        
        self.action_add_tags_btn = QPushButton("🏷️ Add Tags")
        self.action_add_tags_btn.setToolTip("Add tags to selected files")
        self.action_add_tags_btn.setObjectName("quickActionBtn")
        quick_actions_layout.addWidget(self.action_add_tags_btn)
        
        self.action_copy_paths_btn = QPushButton("📋 Copy Paths")
        self.action_copy_paths_btn.setToolTip("Copy file paths to clipboard")
        self.action_copy_paths_btn.setObjectName("quickActionBtn")
        quick_actions_layout.addWidget(self.action_copy_paths_btn)
        
        self.action_open_folders_btn = QPushButton("📂 Open Folders")
        self.action_open_folders_btn.setToolTip("Open containing folders in Explorer")
        self.action_open_folders_btn.setObjectName("quickActionBtn")
        quick_actions_layout.addWidget(self.action_open_folders_btn)
        
        self.action_export_btn = QPushButton("📤 Export List")
        self.action_export_btn.setToolTip("Export selected files to CSV or TXT")
        self.action_export_btn.setObjectName("quickActionBtn")
        quick_actions_layout.addWidget(self.action_export_btn)
        
        quick_actions_layout.addStretch()
        
        # Select All / Clear Selection buttons
        self.action_select_all_btn = QPushButton("Select All")
        self.action_select_all_btn.setObjectName("quickActionBtnSecondary")
        quick_actions_layout.addWidget(self.action_select_all_btn)
        
        self.action_clear_selection_btn = QPushButton("Clear Selection")
        self.action_clear_selection_btn.setObjectName("quickActionBtnSecondary")
        quick_actions_layout.addWidget(self.action_clear_selection_btn)
        
        self.quick_actions_widget.setVisible(False)  # Hidden until files selected
        search_group_layout.addWidget(self.quick_actions_widget)
        
        # Search results
        self.search_results_table = QTableWidget()
        self.search_results_table.setShowGrid(False)
        self.search_results_table.setAlternatingRowColors(True)
        self.search_results_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.search_results_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.search_results_table.setColumnCount(15)  # Added checkbox column
        self.search_results_table.setHorizontalHeaderLabels([
            "✓", "File Name", "Category", "Size", "Relevance", "Label", "Tags", "Caption", "OCR Preview", "AI Source", "Vision Score", "Purpose", "Suggested Filename", "Path", "Actions"
        ])
        search_header = self.search_results_table.horizontalHeader()
        search_header.setSectionResizeMode(0, QHeaderView.Fixed)  # Checkbox
        self.search_results_table.setColumnWidth(0, 40)
        search_header.setSectionResizeMode(1, QHeaderView.Stretch)
        search_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(7, QHeaderView.Stretch)
        search_header.setSectionResizeMode(8, QHeaderView.Stretch)
        search_header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(10, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(11, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(12, QHeaderView.ResizeToContents)
        search_header.setSectionResizeMode(13, QHeaderView.Stretch)
        search_header.setSectionResizeMode(14, QHeaderView.ResizeToContents)
        search_group_layout.addWidget(self.search_results_table)
        
        # Search statistics
        self.search_stats_label = QLabel("No files indexed yet")
        search_group_layout.addWidget(self.search_stats_label)
        
        search_layout.addWidget(search_group)
        
        # Add search tab
        self.tab_widget.addTab(search_widget, "Search Files")
    
    def setup_debug_tab(self):
        """Setup the debug tab to show indexed files."""
        debug_widget = QWidget()
        debug_layout = QVBoxLayout(debug_widget)
        
        # Debug controls
        debug_controls = QHBoxLayout()
        self.refresh_debug_button = QPushButton("Refresh Index View")
        self.clear_index_button = QPushButton("Clear Index")
        debug_controls.addWidget(self.refresh_debug_button)
        debug_controls.addWidget(self.clear_index_button)
        debug_controls.addStretch()
        debug_layout.addLayout(debug_controls)
        
        # Quick Actions bar for Indexed Files tab (same style as Search tab)
        self.debug_quick_actions_widget = QWidget()
        self.debug_quick_actions_widget.setObjectName("quickActionsBar")
        debug_qa_layout = QHBoxLayout(self.debug_quick_actions_widget)
        debug_qa_layout.setContentsMargins(8, 6, 8, 6)
        debug_qa_layout.setSpacing(8)
        
        # Selection counter
        self.debug_selection_count_label = QLabel("0 files selected")
        self.debug_selection_count_label.setObjectName("selectionLabel")
        debug_qa_layout.addWidget(self.debug_selection_count_label)
        
        debug_qa_layout.addWidget(self._create_separator())
        
        # Action buttons
        self.debug_action_remove_btn = QPushButton("🗑️ Remove from Index")
        self.debug_action_remove_btn.setToolTip("Remove selected files from the index (files stay on PC)")
        self.debug_action_remove_btn.setObjectName("quickActionBtn")
        debug_qa_layout.addWidget(self.debug_action_remove_btn)
        
        self.debug_action_reindex_btn = QPushButton("🔄 Re-index")
        self.debug_action_reindex_btn.setToolTip("Re-scan selected files to update metadata")
        self.debug_action_reindex_btn.setObjectName("quickActionBtn")
        debug_qa_layout.addWidget(self.debug_action_reindex_btn)
        
        self.debug_action_add_tags_btn = QPushButton("🏷️ Add Tags")
        self.debug_action_add_tags_btn.setToolTip("Add tags to selected files")
        self.debug_action_add_tags_btn.setObjectName("quickActionBtn")
        debug_qa_layout.addWidget(self.debug_action_add_tags_btn)
        
        self.debug_action_copy_paths_btn = QPushButton("📋 Copy Paths")
        self.debug_action_copy_paths_btn.setToolTip("Copy file paths to clipboard")
        self.debug_action_copy_paths_btn.setObjectName("quickActionBtn")
        debug_qa_layout.addWidget(self.debug_action_copy_paths_btn)
        
        self.debug_action_open_folders_btn = QPushButton("📂 Open Folders")
        self.debug_action_open_folders_btn.setToolTip("Open containing folders in Explorer")
        self.debug_action_open_folders_btn.setObjectName("quickActionBtn")
        debug_qa_layout.addWidget(self.debug_action_open_folders_btn)
        
        self.debug_action_export_btn = QPushButton("📤 Export List")
        self.debug_action_export_btn.setToolTip("Export selected files to CSV or TXT")
        self.debug_action_export_btn.setObjectName("quickActionBtn")
        debug_qa_layout.addWidget(self.debug_action_export_btn)
        
        debug_qa_layout.addStretch()
        
        # Select All / Clear Selection buttons
        self.debug_action_select_all_btn = QPushButton("Select All")
        self.debug_action_select_all_btn.setObjectName("quickActionBtnSecondary")
        debug_qa_layout.addWidget(self.debug_action_select_all_btn)
        
        self.debug_action_clear_selection_btn = QPushButton("Clear Selection")
        self.debug_action_clear_selection_btn.setObjectName("quickActionBtnSecondary")
        debug_qa_layout.addWidget(self.debug_action_clear_selection_btn)
        
        self.debug_quick_actions_widget.setVisible(False)  # Hidden until files selected
        debug_layout.addWidget(self.debug_quick_actions_widget)
        
        # Debug table with checkbox column
        self.debug_table = QTableWidget()
        self.debug_table.setShowGrid(False)
        self.debug_table.setAlternatingRowColors(True)
        self.debug_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.debug_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.debug_table.setColumnCount(16)  # Added checkbox column
        self.debug_table.verticalHeader().setDefaultSectionSize(48)  # Increased row height for buttons
        self.debug_table.setHorizontalHeaderLabels([
            "✓", "File Name", "Category", "Size", "Has OCR", "Label", "Tags", "Caption", "OCR Text Preview", "AI Source", "Vision Score", "Purpose", "Suggested Filename", "Detected Text", "File Path", "Actions"
        ])
        debug_header = self.debug_table.horizontalHeader()
        debug_header.setSectionResizeMode(0, QHeaderView.Fixed)  # Checkbox
        self.debug_table.setColumnWidth(0, 40)
        debug_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(7, QHeaderView.Interactive)  # Caption
        self.debug_table.setColumnWidth(7, 200)
        debug_header.setSectionResizeMode(8, QHeaderView.Interactive)  # OCR Text Preview
        self.debug_table.setColumnWidth(8, 200)
        debug_header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(10, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(11, QHeaderView.ResizeToContents)
        debug_header.setSectionResizeMode(12, QHeaderView.Interactive)  # Suggested Filename
        self.debug_table.setColumnWidth(12, 200)
        debug_header.setSectionResizeMode(13, QHeaderView.Interactive)  # Detected Text
        self.debug_table.setColumnWidth(13, 150)
        debug_header.setSectionResizeMode(14, QHeaderView.Interactive)  # File Path
        self.debug_table.setColumnWidth(14, 300)
        debug_header.setSectionResizeMode(15, QHeaderView.Interactive)  # Actions
        self.debug_table.setColumnWidth(15, 140)  # Action buttons
        debug_layout.addWidget(self.debug_table)
        
        # Debug info
        self.debug_info_label = QLabel("Click 'Refresh Index View' to see what's in the database")
        self.debug_info_label.setObjectName("secondaryLabel")
        debug_layout.addWidget(self.debug_info_label)
        
        # Add debug tab
        self.tab_widget.addTab(debug_widget, "Indexed Files")

        # Handle edits in debug table
        self.debug_table.itemChanged.connect(self.on_debug_cell_changed)
        # Handle double-click to show full content
        self.debug_table.cellDoubleClicked.connect(self.on_debug_cell_double_clicked)

    def setup_settings_tab(self):
        """Settings tab for AI options."""
        settings_widget = QWidget()
        layout = QVBoxLayout(settings_widget)

        # Appearance / Theme
        appearance_group = QGroupBox("Appearance")
        appearance_layout = QHBoxLayout(appearance_group)
        
        appearance_layout.addWidget(QLabel("Theme:"))
        self.theme_toggle_btn = QPushButton()
        self.theme_toggle_btn.setCheckable(True)
        self._update_theme_button()
        self.theme_toggle_btn.clicked.connect(self._on_theme_toggle)
        appearance_layout.addWidget(self.theme_toggle_btn)
        appearance_layout.addStretch()
        
        layout.addWidget(appearance_group)

        # OpenAI toggle and key
        ai_group = QGroupBox("AI Providers")
        ai_layout = QVBoxLayout(ai_group)

        row1 = QHBoxLayout()
        self.use_openai_checkbox = QPushButton("Use ChatGPT (OpenAI) Fallback: OFF")
        self.use_openai_checkbox.setCheckable(True)
        self.use_openai_checkbox.setChecked(settings.use_openai_fallback)
        if settings.use_openai_fallback:
            self.use_openai_checkbox.setText("Use ChatGPT (OpenAI) Fallback: ON")
        row1.addWidget(self.use_openai_checkbox)
        ai_layout.addLayout(row1)

        row2 = QHBoxLayout()
        self.openai_key_input = QLineEdit()
        self.openai_key_input.setEchoMode(QLineEdit.Password)
        self.openai_key_input.setPlaceholderText("Enter OpenAI API key")
        if settings.openai_api_key:
            self.openai_key_input.setText(settings.openai_api_key)
        row2.addWidget(QLabel("OpenAI API Key:"))
        row2.addWidget(self.openai_key_input)
        self.save_ai_settings_button = QPushButton("Save")
        self.delete_ai_key_button = QPushButton("Delete Key")
        row2.addWidget(self.save_ai_settings_button)
        row2.addWidget(self.delete_ai_key_button)
        ai_layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("OpenAI Vision Model:"))
        self.openai_model_combo = QComboBox()
        self.openai_model_combo.setEditable(True)
        # Pre-populate common vision-capable models
        model_options = [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
        ]
        self.openai_model_combo.addItems(model_options)
        # Ensure current setting is present/selected
        current_model = settings.openai_vision_model
        if current_model and current_model not in model_options:
            self.openai_model_combo.addItem(current_model)
        idx = self.openai_model_combo.findText(current_model)
        if idx >= 0:
            self.openai_model_combo.setCurrentIndex(idx)
        else:
            self.openai_model_combo.setEditText(current_model)
        # Improve search in the dropdown
        completer = self.openai_model_combo.completer()
        if completer:
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            try:
                completer.setFilterMode(Qt.MatchContains)
            except Exception:
                pass
        row3.addWidget(self.openai_model_combo)
        ai_layout.addLayout(row3)

        layout.addWidget(ai_group)

        # Quick Search settings
        qs_group = QGroupBox("Quick Search")
        qs_layout = QVBoxLayout(qs_group)

        qs_row1 = QHBoxLayout()
        self.qs_autopaste_btn = QPushButton("Auto-Paste: ON" if settings.quick_search_autopaste else "Auto-Paste: OFF")
        self.qs_autopaste_btn.setCheckable(True)
        self.qs_autopaste_btn.setChecked(settings.quick_search_autopaste)
        qs_row1.addWidget(self.qs_autopaste_btn)
        self.qs_autoconfirm_btn = QPushButton("Auto-Confirm: ON" if settings.quick_search_auto_confirm else "Auto-Confirm: OFF")
        self.qs_autoconfirm_btn.setCheckable(True)
        self.qs_autoconfirm_btn.setChecked(settings.quick_search_auto_confirm)
        qs_row1.addWidget(self.qs_autoconfirm_btn)
        qs_layout.addLayout(qs_row1)

        qs_row2 = QHBoxLayout()
        qs_row2.addWidget(QLabel("Shortcut:"))
        self.qs_shortcut_input = QLineEdit(settings.quick_search_shortcut)
        qs_row2.addWidget(self.qs_shortcut_input)
        self.qs_shortcut_save = QPushButton("Save Shortcut")
        qs_row2.addWidget(self.qs_shortcut_save)
        qs_layout.addLayout(qs_row2)

        layout.addWidget(qs_group)
        
        # Database Maintenance section
        db_group = QGroupBox("Database Maintenance")
        db_layout = QVBoxLayout(db_group)
        
        # Resync file dates button
        resync_row = QHBoxLayout()
        self.resync_dates_btn = QPushButton("🔄 Extract File Dates from Metadata")
        self.resync_dates_btn.setToolTip(
            "Extract original dates from file metadata:\n"
            "• EXIF dates from photos (JPEG, etc.)\n"
            "• Creation dates from Office docs (Word, Excel, PowerPoint)\n"
            "• Creation dates from PDFs\n"
            "• Dates from filenames (screenshots)\n"
            "• Modified dates as fallback"
        )
        self.resync_dates_btn.clicked.connect(self._resync_file_dates)
        resync_row.addWidget(self.resync_dates_btn)
        self.resync_status_label = QLabel("")
        self.resync_status_label.setObjectName("secondaryLabel")
        resync_row.addWidget(self.resync_status_label)
        resync_row.addStretch()
        db_layout.addLayout(resync_row)
        
        layout.addWidget(db_group)
        
        # Account Management section
        account_group = QGroupBox("Account")
        account_layout = QVBoxLayout(account_group)
        
        # Email display
        email_row = QHBoxLayout()
        email_row.addWidget(QLabel("Email:"))
        self.account_email_label = QLabel("Not logged in")
        self.account_email_label.setObjectName("secondaryLabel")
        email_row.addWidget(self.account_email_label)
        email_row.addStretch()
        account_layout.addLayout(email_row)
        
        # Subscription status
        sub_row = QHBoxLayout()
        sub_row.addWidget(QLabel("Subscription:"))
        self.account_sub_label = QLabel("No subscription")
        self.account_sub_label.setObjectName("secondaryLabel")
        sub_row.addWidget(self.account_sub_label)
        sub_row.addStretch()
        account_layout.addLayout(sub_row)
        
        # Buttons row
        button_row = QHBoxLayout()
        self.refresh_account_btn = QPushButton("Refresh Account Info")
        self.refresh_account_btn.clicked.connect(self._refresh_account_info)
        button_row.addWidget(self.refresh_account_btn)
        
        self.signout_btn = QPushButton("Sign Out")
        self.signout_btn.clicked.connect(self._sign_out)
        button_row.addWidget(self.signout_btn)
        button_row.addStretch()
        account_layout.addLayout(button_row)
        
        layout.addWidget(account_group)
        
        # Load account info on startup
        self._refresh_account_info()
        
        layout.addStretch()

        self.tab_widget.addTab(settings_widget, "Settings")
    
    def _update_theme_button(self):
        """Update the theme toggle button text and state."""
        current = theme_manager.current_theme
        if current == 'dark':
            self.theme_toggle_btn.setText("🌙 Dark Mode")
            self.theme_toggle_btn.setChecked(True)
        else:
            self.theme_toggle_btn.setText("☀️ Light Mode")
            self.theme_toggle_btn.setChecked(False)
    
    def _on_theme_toggle(self):
        """Handle theme toggle button click."""
        new_theme = theme_manager.toggle_theme()
        self._update_theme_button()
        self.status_bar.showMessage(f"Switched to {new_theme} mode", 3000)
    
    def _refresh_account_info(self):
        """Refresh and display account information."""
        if supabase_auth.is_authenticated:
            # Display email
            email = supabase_auth.user_email or "Unknown"
            self.account_email_label.setText(email)
            
            # Check subscription status
            result = supabase_auth.check_subscription()
            if result.get('has_subscription'):
                status = result.get('status', 'active')
                expires_at = result.get('expires_at', 'N/A')
                if expires_at and expires_at != 'N/A':
                    try:
                        # Format the date nicely
                        dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
                        expires_str = dt.strftime('%Y-%m-%d')
                        self.account_sub_label.setText(f"✓ Active (until {expires_str})")
                    except Exception:
                        self.account_sub_label.setText(f"✓ Active ({status})")
                else:
                    self.account_sub_label.setText(f"✓ Active ({status})")
                self.account_sub_label.setStyleSheet("color: #00E5FF;")
            else:
                status = result.get('status')
                if status:
                    self.account_sub_label.setText(f"⚠ {status}")
                else:
                    self.account_sub_label.setText("No subscription")
                self.account_sub_label.setStyleSheet("color: #FF6B6B;")
        else:
            self.account_email_label.setText("Not logged in")
            self.account_sub_label.setText("No subscription")
            self.account_sub_label.setStyleSheet("")
    
    def _sign_out(self):
        """Sign out the current user and show login dialog."""
        reply = QMessageBox.question(
            self,
            "Sign Out",
            "Are you sure you want to sign out?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # Sign out from Supabase
            supabase_auth.sign_out()
            settings.clear_auth_tokens()
            
            # Hide main window
            self.hide()
            
            # Show auth dialog again
            from app.ui.auth_dialog import AuthDialog
            auth_dialog = AuthDialog()
            
            if auth_dialog.exec():
                # User logged in successfully, refresh account info and show window
                self._refresh_account_info()
                self.show()
                self.status_bar.showMessage("Welcome back!", 3000)
            else:
                # User cancelled login, close app
                QApplication.quit()
    
    def _resync_file_dates(self):
        """Resync file dates from Windows filesystem."""
        reply = QMessageBox.question(
            self,
            "Resync File Dates",
            "This will re-read file creation and modification dates from Windows\n"
            "for all indexed files. This may take a moment.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        
        if reply != QMessageBox.Yes:
            return
        
        self.resync_dates_btn.setEnabled(False)
        self.resync_status_label.setText("Resyncing...")
        QApplication.processEvents()
        
        try:
            # Perform the resync
            from app.core.database import file_index
            
            def progress_callback(current, total):
                self.resync_status_label.setText(f"Processing {current}/{total}...")
                QApplication.processEvents()
            
            stats = file_index.resync_file_dates(progress_callback)
            
            # Show result
            self.resync_status_label.setText(
                f"Done: {stats['updated']} updated, {stats['not_found']} not found"
            )
            
            metadata_count = stats.get('exif_found', 0)
            QMessageBox.information(
                self,
                "Resync Complete",
                f"File dates resynced:\n\n"
                f"• Updated: {stats['updated']} files\n"
                f"• Metadata dates extracted: {metadata_count} files\n"
                f"  (from EXIF, Office docs, PDFs, filenames)\n"
                f"• Not found: {stats['not_found']} files\n"
                f"• Errors: {stats['errors']} files\n\n"
                "Date filters now use the best available date\n"
                "for each file type."
            )
            
        except Exception as e:
            logger.error(f"Error resyncing file dates: {e}")
            self.resync_status_label.setText("Error!")
            QMessageBox.warning(self, "Error", f"Failed to resync file dates:\n{e}")
        
        finally:
            self.resync_dates_btn.setEnabled(True)
    
    def setup_connections(self):
        """Setup signal connections."""
        # Organize tab connections - Hidden for MVP (search-only mode)
        # self.source_button.clicked.connect(self.select_source_folder)
        # self.dest_button.clicked.connect(self.select_destination_folder)
        # self.scan_button.clicked.connect(self.scan_and_plan)
        # self.apply_button.clicked.connect(self.apply_moves)
        
        # Search tab connections
        self.index_button.clicked.connect(self.select_index_folder)
        self.index_button_action.clicked.connect(self.index_directory)
        self.index_pause_btn.clicked.connect(self._toggle_index_pause)
        self.index_cancel_btn.clicked.connect(self._cancel_indexing)
        self.search_button.clicked.connect(self.search_files)
        self.search_input.returnPressed.connect(self.search_files)
        
        # Filter connections
        self.type_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.date_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.clear_filters_btn.clicked.connect(self._clear_filters)
        
        # Quick Actions connections (itemChanged fires when checkbox is clicked)
        self.search_results_table.itemChanged.connect(self._on_selection_changed)
        self.action_remove_btn.clicked.connect(self._action_remove_from_index)
        self.action_reindex_btn.clicked.connect(self._action_reindex_selected)
        self.action_add_tags_btn.clicked.connect(self._action_add_tags)
        self.action_copy_paths_btn.clicked.connect(self._action_copy_paths)
        self.action_open_folders_btn.clicked.connect(self._action_open_folders)
        self.action_export_btn.clicked.connect(self._action_export_list)
        self.action_select_all_btn.clicked.connect(self._action_select_all)
        self.action_clear_selection_btn.clicked.connect(self._action_clear_selection)
        
        # Debug tab Quick Actions connections (itemChanged fires when checkbox is clicked)
        self.debug_table.itemChanged.connect(self._on_debug_selection_changed)
        self.debug_action_remove_btn.clicked.connect(lambda: self._action_remove_from_index(source='debug'))
        self.debug_action_reindex_btn.clicked.connect(lambda: self._action_reindex_selected(source='debug'))
        self.debug_action_add_tags_btn.clicked.connect(lambda: self._action_add_tags(source='debug'))
        self.debug_action_copy_paths_btn.clicked.connect(lambda: self._action_copy_paths(source='debug'))
        self.debug_action_open_folders_btn.clicked.connect(lambda: self._action_open_folders(source='debug'))
        self.debug_action_export_btn.clicked.connect(lambda: self._action_export_list(source='debug'))
        self.debug_action_select_all_btn.clicked.connect(lambda: self._action_select_all(source='debug'))
        self.debug_action_clear_selection_btn.clicked.connect(lambda: self._action_clear_selection(source='debug'))
        
        # Quick index options
        self.index_pc_button.clicked.connect(self.on_index_entire_pc)
        self.auto_index_downloads_btn.toggled.connect(self.on_toggle_auto_index_downloads)
        
        # Debug/Indexed Files tab connections
        self.refresh_debug_button.clicked.connect(self.refresh_debug_view)
        self.clear_index_button.clicked.connect(self.clear_index)
        
        # Settings tab connections
        if hasattr(self, 'use_openai_checkbox'):
            self.use_openai_checkbox.toggled.connect(self.on_toggle_openai)
        if hasattr(self, 'save_ai_settings_button'):
            self.save_ai_settings_button.clicked.connect(self.on_save_openai)
        if hasattr(self, 'delete_ai_key_button'):
            self.delete_ai_key_button.clicked.connect(self.on_delete_openai_key)
        if hasattr(self, 'gpt_rerank_button'):
            self.gpt_rerank_button.toggled.connect(self.on_toggle_gpt_rerank)
        # Quick search settings connections
        if hasattr(self, 'qs_autopaste_btn'):
            self.qs_autopaste_btn.toggled.connect(self.on_qs_autopaste)
        if hasattr(self, 'qs_autoconfirm_btn'):
            self.qs_autoconfirm_btn.toggled.connect(self.on_qs_autoconfirm)
        if hasattr(self, 'qs_shortcut_save'):
            self.qs_shortcut_save.clicked.connect(self.on_qs_save_shortcut)
        
        # Update search button state when text changes
        self.search_input.textChanged.connect(self.update_search_button_state)

    def setup_quick_search(self):
        """Register global hotkey and prepare overlay."""
        # Use None as parent so the popup doesn't bring up the main window
        self.quick_overlay = QuickSearchOverlay(None)
        self.quick_overlay.pathSelected.connect(self.on_quick_path_selected)
        logger.info("[QS] *** Signal connection established: pathSelected -> on_quick_path_selected")

        # Wrapper to show overlay and remember previously focused window
        def show_quick_overlay():
            try:
                self._prev_foreground_hwnd = get_foreground_hwnd()
                # Save mouse position relative to the dialog window
                self._rel_click_point = None
                try:
                    rect = get_window_rect(self._prev_foreground_hwnd)
                    if rect:
                        l, t, r, b = rect
                        # Get current cursor pos
                        pt = ctypes.wintypes.POINT()
                        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                        self._rel_click_point = (pt.x - l, pt.y - t)
                except Exception:
                    self._rel_click_point = None
            except Exception:
                self._prev_foreground_hwnd = 0
                self._rel_click_point = None
            self.quick_overlay.show_centered_bottom()

        # Register global hotkey via QHotkey → keyboard → WinAPI
        self._qhotkey = None
        self._win_hotkey = None
        try:
            from qhotkey import QHotkey  # type: ignore
            ks = settings.quick_search_shortcut or 'ctrl+alt+h'
            self._qhotkey = QHotkey(QKeySequence(ks), True, self)
            self._qhotkey.activated.connect(show_quick_overlay)
            logger.info(f"Registered global hotkey (QHotkey): {ks}")
        except Exception as e:
            logger.warning(f"QHotkey failed: {e}")
            # Skip keyboard library and go directly to WinAPI (more reliable)
            logger.warning("Skipping keyboard library, using WinAPI directly")
            # Use raw WinAPI RegisterHotKey for maximum reliability
            hk = register_global_hotkey(self,
                                        settings.quick_search_shortcut or 'ctrl+alt+h',
                                        lambda: QTimer.singleShot(0, show_quick_overlay))
            if hk:
                self._win_hotkey = hk
                logger.info("Registered global hotkey (WinAPI)")
            else:
                logger.error("Global hotkey not available; quick search disabled")
                # Final fallback: try keyboard library
                try:
                    import keyboard  # type: ignore
                    hotkey = settings.quick_search_shortcut or 'ctrl+alt+h'
                    keyboard.add_hotkey(hotkey, lambda: QTimer.singleShot(0, show_quick_overlay))
                    logger.info(f"Registered global hotkey (keyboard fallback): {hotkey}")
                except Exception as e2:
                    logger.warning(f"Keyboard hook also failed: {e2}")
        # App-focus fallback using QShortcut so it works when the app is focused
        try:
            ks = settings.quick_search_shortcut.replace('ctrl', 'Ctrl').replace('alt', 'Alt').replace('shift', 'Shift')
            self._focus_quick_shortcut = QShortcut(QKeySequence(ks or 'Ctrl+Alt+Space'), self)
            self._focus_quick_shortcut.setContext(Qt.ApplicationShortcut)
            self._focus_quick_shortcut.activated.connect(show_quick_overlay)
        except Exception:
            pass
        # Debug: dump active dialog tree (Ctrl+Alt+D)
        try:
            self._dump_tree_shortcut = QShortcut(QKeySequence('Ctrl+Alt+D'), self)
            self._dump_tree_shortcut.setContext(Qt.ApplicationShortcut)
            self._dump_tree_shortcut.activated.connect(self.dump_active_dialog_tree)
        except Exception:
            pass
        # Debug: comprehensive system state (Ctrl+Alt+S)
        try:
            self._debug_state_shortcut = QShortcut(QKeySequence('Ctrl+Alt+S'), self)
            self._debug_state_shortcut.setContext(Qt.ApplicationShortcut)
            self._debug_state_shortcut.activated.connect(self.debug_comprehensive_state)
        except Exception:
            pass
        # Quick overlay focus-mode toggle (Ctrl+Alt+F)
        try:
            self._focus_mode_shortcut = QShortcut(QKeySequence('Ctrl+Alt+F'), self)
            self._focus_mode_shortcut.setContext(Qt.ApplicationShortcut)
            self._focus_mode_shortcut.activated.connect(self.quick_overlay.enable_focus_mode)
        except Exception:
            pass

    

    def on_quick_path_selected(self, payload: str):
        logger.info(f"[QS] *** on_quick_path_selected CALLED with payload: {payload}")
        
        # payload may be 'path' or 'path||OPEN'
        path = payload
        do_open = False
        if payload.endswith('||OPEN'):
            path = payload[:-6]
            do_open = True
        
        # Copy to clipboard
        try:
            cb = QApplication.clipboard()
            cb.setText(path)
            self.status_bar.showMessage("Copied path to clipboard")
        except Exception:
            pass
        
        # Auto-fill using our enhanced Phase 1-3 system
        logger.info(f"[QS] Autopaste setting: {settings.quick_search_autopaste}")
        if settings.quick_search_autopaste:
            logger.info("[QS] === STARTING ENHANCED AUTOFILL ===")
            # Use a short delay to let the dialog settle after focus restoration
            def _run_enhanced_autofill(p=path):
                logger.info(f"[QS] Running enhanced autofill for: {p}")
                self.try_autofill_file_dialog(p)
            
            # Short delay since focus restoration already happened in Phase 2
            QTimer.singleShot(200, _run_enhanced_autofill)
        else:
            logger.info("[QS] Autopaste is DISABLED - skipping autofill")
        
        if do_open:
            self.open_file_in_os(path)

    def try_autofill_file_dialog(self, path: str) -> None:
        """
        Phase 3: Enhanced autofill pipeline with state-aware dialog targeting.
        Uses saved state from quick search overlay if available.
        """
        logger.info("[QS] Phase 3: Starting enhanced autofill pipeline")
        
        # Check if we have saved state from the quick search overlay
        overlay = getattr(self, 'quick_overlay', None)
        if overlay and overlay.has_valid_saved_state():
            logger.info("[QS] Using saved state from quick search overlay")
            success = self._autofill_with_saved_state(path, overlay)
            if success:
                return
            else:
                logger.warning("[QS] Saved state autofill failed, falling back to discovery")
        
        # Fallback to discovery-based autofill
        logger.info("[QS] Using discovery-based autofill")
        ok = self._autofill_uia_pipeline(path)
        if not ok:
            logger.info("[QS] UIA pipeline failed; trying win32 pipeline")
            self._autofill_win32_pipeline(path)

    def _autofill_with_saved_state(self, path: str, overlay) -> bool:
        """
        Phase 3: Autofill using saved state from the quick search overlay.
        This is more reliable than discovery because we know the exact dialog.
        """
        try:
            logger.info("[QS] Phase 3: Autofill with saved state")
            
            # Get saved state
            hwnd = overlay._saved_window_hwnd
            window_title = overlay._saved_window_title
            window_class = overlay._saved_window_class
            is_verified_dialog = overlay._is_dialog_verified
            
            logger.info(f"[QS] Target dialog: hwnd={hwnd}, title='{window_title}', class='{window_class}', verified={is_verified_dialog}")
            
            # Phase 4: Create debug report before attempting autofill
            from app.ui.win_hotkey import create_autofill_debug_report
            create_autofill_debug_report(hwnd, overlay._saved_cursor_pos, overlay._saved_window_rect, logger, "[QS]")
            
            # Verify the window still exists and is the same dialog
            from app.ui.win_hotkey import window_still_exists, get_window_title, get_window_class
            if not window_still_exists(hwnd):
                logger.warning("[QS] Target dialog no longer exists")
                return False
            
            current_title = get_window_title(hwnd)
            current_class = get_window_class(hwnd)
            
            if current_title != window_title or current_class != window_class:
                logger.warning(f"[QS] Dialog changed: was '{window_title}'/'{window_class}', now '{current_title}'/'{current_class}'")
                return False
            
            logger.info("[QS] Dialog verified, attempting targeted autofill")
            
            # Try multiple autofill strategies with increasing robustness
            strategies = [
                ("targeted_uia", self._autofill_targeted_uia),
                ("targeted_win32", self._autofill_targeted_win32),
                ("modern_directui", self._autofill_modern_directui),
                ("stealth_click_paste", self._autofill_stealth_click_paste)
            ]
            
            for i, (strategy_name, strategy_func) in enumerate(strategies):
                logger.info(f"[QS] === STRATEGY {i+1}/{len(strategies)}: {strategy_name.upper()} ===")
                try:
                    success = strategy_func(path, hwnd, overlay)
                    if success:
                        logger.info(f"[QS] ✅ Strategy {strategy_name} SUCCESS!")
                        self.status_bar.showMessage(f"QuickSearch: Autofilled via {strategy_name}")
                        return True
                    else:
                        logger.warning(f"[QS] ❌ Strategy {strategy_name} failed")
                except Exception as e:
                    logger.error(f"[QS] ❌ Strategy {strategy_name} exception: {e}")
                
                # Brief pause between strategies
                if i < len(strategies) - 1:
                    import time
                    time.sleep(0.2)
            
            logger.error("[QS] ❌ ALL AUTOFILL STRATEGIES FAILED")
            self.status_bar.showMessage("QuickSearch: All autofill methods failed")
            return False
            
        except Exception as e:
            logger.error(f"[QS] Exception in _autofill_with_saved_state: {e}")
            return False

    def _autofill_targeted_uia(self, path: str, hwnd: int, overlay) -> bool:
        """Strategy 1: Targeted UIA autofill using the specific window handle."""
        try:
            import time
            from pywinauto import Application
            
            start_time = time.time()
            logger.info("[QS] UIA Strategy: Starting targeted UIA autofill")
            
            # Connect directly to the specific window
            app = Application(backend="uia").connect(handle=hwnd)
            win = app.window(handle=hwnd)
            
            logger.info("[QS] Connected to target window via UIA")
            
            # Ensure window is focused
            try:
                win.set_focus()
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"[QS] Failed to set focus: {e}")
            
            # Find filename field using multiple strategies
            target = None
            
            # Strategy A: FileNameControlHost (modern dialogs)
            try:
                host = win.child_window(auto_id="FileNameControlHost", control_type="Pane")
                if host.exists():
                    eds = host.descendants(control_type='Edit')
                    if eds:
                        target = eds[0]
                        logger.info("[QS] Found filename field via FileNameControlHost")
            except Exception:
                pass
            
            # Strategy B: By label proximity
            if target is None:
                try:
                    from app.core.vision import FILENAME_LABELS
                    texts = win.descendants(control_type='Text')
                    edits = win.descendants(control_type='Edit')
                    
                    label_rects = []
                    for t in texts:
                        try:
                            name = (t.window_text() or '').strip()
                            if any(name.lower().startswith(lbl.lower().rstrip(':')) for lbl in FILENAME_LABELS):
                                label_rects.append(t.rectangle())
                        except Exception:
                            continue
                    
                    best = None
                    best_dx = 10**9
                    for e in edits:
                        try:
                            er = e.rectangle()
                            for lr in label_rects:
                                if er.left >= lr.right - 4 and (min(er.bottom, lr.bottom) - max(er.top, lr.top)) > 6:
                                    dx = er.left - lr.right
                                    if er.width() > 150 and er.height() < 60 and dx < best_dx:
                                        best = e
                                        best_dx = dx
                        except Exception:
                            continue
                    
                    if best:
                        target = best
                        logger.info("[QS] Found filename field via label proximity")
                except Exception:
                    pass
            
            # Strategy C: Bottom-most edit heuristic
            if target is None:
                try:
                    edits = win.descendants(control_type='Edit')
                    best = None
                    best_y = -1
                    for e in edits:
                        try:
                            rect = e.rectangle()
                            if rect.width() > 150 and rect.height() < 60 and rect.top > best_y:
                                best = e
                                best_y = rect.top
                        except Exception:
                            continue
                    if best:
                        target = best
                        logger.info("[QS] Found filename field via bottom-most heuristic")
                except Exception:
                    pass
            
            if not target:
                logger.warning("[QS] No filename field found in UIA")
                return False
            
            # Insert the path using multiple methods
            success = self._insert_path_uia(target, path, win)
            
            elapsed = time.time() - start_time
            if success:
                logger.info(f"[QS] UIA Strategy: SUCCESS in {elapsed:.2f}s")
            else:
                logger.warning(f"[QS] UIA Strategy: FAILED after {elapsed:.2f}s")
            
            return success
            
        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in locals() else 0
            logger.error(f"[QS] UIA Strategy: EXCEPTION after {elapsed:.2f}s: {e}")
            return False
    
    def _autofill_targeted_win32(self, path: str, hwnd: int, overlay) -> bool:
        """Strategy 2: Targeted Win32 autofill using the specific window handle."""
        try:
            import time
            from pywinauto import Application
            
            start_time = time.time()
            logger.info("[QS] Win32 Strategy: Starting targeted Win32 autofill")
            
            # Connect directly to the specific window
            app = Application(backend="win32").connect(handle=hwnd)
            win = app.window(handle=hwnd)
            
            logger.info("[QS] Connected to target window via Win32")
            
            # Ensure window is focused
            try:
                win.set_focus()
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"[QS] Failed to set focus: {e}")
            
            # Find filename field
            target = None
            
            # Strategy A: ComboBoxEx32 with Edit child (common in file dialogs)
            try:
                combo_hosts = win.descendants(class_name='ComboBoxEx32')
                for host in combo_hosts:
                    eds = host.descendants(class_name='Edit')
                    if eds:
                        target = eds[0]
                        logger.info("[QS] Found filename field via ComboBoxEx32")
                        break
            except Exception:
                pass
            
            # Strategy B: Last Edit control (fallback)
            if target is None:
                try:
                    edits = win.descendants(class_name='Edit')
                    if edits:
                        target = edits[-1]
                        logger.info("[QS] Found filename field via last Edit")
                except Exception:
                    pass
            
            if not target:
                logger.warning("[QS] No filename field found in Win32")
                return False
            
            # Insert the path
            success = self._insert_path_win32(target, path, win)
            
            elapsed = time.time() - start_time
            if success:
                logger.info(f"[QS] Win32 Strategy: SUCCESS in {elapsed:.2f}s")
            else:
                logger.warning(f"[QS] Win32 Strategy: FAILED after {elapsed:.2f}s")
            
            return success
            
        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in locals() else 0
            logger.error(f"[QS] Win32 Strategy: EXCEPTION after {elapsed:.2f}s: {e}")
            return False
    
    def _autofill_modern_directui(self, path: str, hwnd: int, overlay) -> bool:
        """Strategy 3: Modern DirectUI dialog autofill for Windows 10/11 file pickers."""
        try:
            import time
            from app.ui.win_hotkey import click_at_position, set_foreground_hwnd_robust
            
            start_time = time.time()
            logger.info("[QS] DirectUI Strategy: Starting modern DirectUI autofill")
            
            # Ensure the dialog is focused
            if not set_foreground_hwnd_robust(hwnd):
                logger.warning("[QS] DirectUI: Failed to focus dialog")
                return False
            
            time.sleep(0.3)  # Let focus settle
            
            # For modern DirectUI dialogs, we need to:
            # 1. Click at the saved cursor position (filename field)
            # 2. Use keyboard shortcuts to paste
            
            cursor_pos = overlay._saved_cursor_pos
            if not cursor_pos:
                logger.warning("[QS] DirectUI: No saved cursor position")
                return False
            
            logger.info(f"[QS] DirectUI: Clicking at saved position {cursor_pos}")
            
            # Click at the saved position (should be the filename field)
            if not click_at_position(cursor_pos[0], cursor_pos[1]):
                logger.warning("[QS] DirectUI: Failed to click at saved position")
                return False
            
            time.sleep(0.3)  # Let click register and focus filename field
            
            # Clear any existing text and paste the path
            try:
                cb = QApplication.clipboard()
                cb.setText(path)
                
                import keyboard
                
                # Clear existing text
                keyboard.send('ctrl+a')
                time.sleep(0.1)
                keyboard.send('delete')
                time.sleep(0.1)
                
                # Paste the path
                keyboard.send('ctrl+v')
                time.sleep(0.2)
                
                logger.info("[QS] DirectUI: Path pasted successfully")
                
                # Auto-confirm if enabled
                if settings.quick_search_auto_confirm:
                    time.sleep(0.3)  # Give time for path to register
                    keyboard.send('enter')
                    logger.info("[QS] DirectUI: Auto-confirmed via Enter")
                
                elapsed = time.time() - start_time
                logger.info(f"[QS] DirectUI Strategy: SUCCESS in {elapsed:.2f}s")
                self.status_bar.showMessage("QuickSearch: path filled via DirectUI" + (" and confirmed" if settings.quick_search_auto_confirm else ""))
                return True
                
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[QS] DirectUI Strategy: Failed to paste after {elapsed:.2f}s: {e}")
                return False
            
        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in locals() else 0
            logger.error(f"[QS] DirectUI Strategy: EXCEPTION after {elapsed:.2f}s: {e}")
            return False
    
    def _autofill_stealth_click_paste(self, path: str, hwnd: int, overlay) -> bool:
        """Strategy 3: Stealth click at saved cursor position + paste."""
        try:
            import time
            from app.ui.win_hotkey import click_at_position, set_foreground_hwnd_robust
            
            start_time = time.time()
            logger.info("[QS] Stealth Strategy: Starting stealth click + paste")
            
            # Ensure the dialog is focused
            if not set_foreground_hwnd_robust(hwnd):
                logger.warning("[QS] Failed to focus dialog for stealth click")
                return False
            
            time.sleep(0.3)  # Let focus settle
            
            # Get saved cursor position
            cursor_pos = overlay._saved_cursor_pos
            if not cursor_pos:
                logger.warning("[QS] No saved cursor position for stealth click")
                return False
            
            logger.info(f"[QS] Stealth clicking at saved position: {cursor_pos}")
            
            # Click at the saved position (should be the filename field)
            if not click_at_position(cursor_pos[0], cursor_pos[1]):
                logger.warning("[QS] Failed to click at saved position")
                return False
            
            time.sleep(0.2)  # Let click register
            
            # Clear existing text and paste new path
            try:
                cb = QApplication.clipboard()
                cb.setText(path)
                
                import keyboard
                keyboard.send('ctrl+a')  # Select all
                time.sleep(0.05)
                keyboard.send('ctrl+v')  # Paste
                time.sleep(0.1)
                
                logger.info("[QS] Stealth click + paste completed")
                
                # Auto-confirm if enabled
                if settings.quick_search_auto_confirm:
                    time.sleep(0.2)
                    keyboard.send('enter')
                    logger.info("[QS] Auto-confirmed via Enter")
                
                elapsed = time.time() - start_time
                logger.info(f"[QS] Stealth Strategy: SUCCESS in {elapsed:.2f}s")
                self.status_bar.showMessage("QuickSearch: path filled via stealth click" + (" and confirmed" if settings.quick_search_auto_confirm else ""))
                return True
                
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[QS] Stealth Strategy: Failed to paste after {elapsed:.2f}s: {e}")
                return False
            
        except Exception as e:
            elapsed = time.time() - start_time if 'start_time' in locals() else 0
            logger.error(f"[QS] Stealth Strategy: EXCEPTION after {elapsed:.2f}s: {e}")
            return False

    def _insert_path_uia(self, target, path: str, win) -> bool:
        """Insert path into UIA Edit control using multiple methods with verification."""
        try:
            import time
            from app.core.vision import CONFIRM_NAMES
            
            def _get_text_safe(ctrl):
                try:
                    return ctrl.get_value()
                except Exception:
                    try:
                        return ctrl.window_text()
                    except Exception:
                        return None
            
            # Try multiple insertion methods
            for attempt in range(3):
                logger.info(f"[QS] UIA insertion attempt {attempt + 1}")
                
                try:
                    target.set_focus()
                    time.sleep(0.15)
                except Exception:
                    pass
                
                filled = False
                
                # Method 1: ValuePattern.SetValue (most reliable)
                if attempt == 0:
                    try:
                        target.set_value(path)
                        filled = True
                        logger.info("[QS] UIA: Set via ValuePattern")
                    except Exception:
                        pass
                
                # Method 2: type_keys with clear
                if not filled:
                    try:
                        target.type_keys('^a{BACKSPACE}', set_foreground=True)
                        time.sleep(0.05)
                        target.type_keys(path, with_spaces=True, set_foreground=True)
                        filled = True
                        logger.info("[QS] UIA: Set via type_keys")
                    except Exception:
                        pass
                
                # Method 3: Clipboard paste fallback
                if not filled:
                    try:
                        cb = QApplication.clipboard()
                        cb.setText(path)
                        
                        import keyboard
                        keyboard.send('ctrl+a')
                        time.sleep(0.05)
                        keyboard.send('ctrl+v')
                        filled = True
                        logger.info("[QS] UIA: Set via clipboard paste")
                    except Exception:
                        pass
                
                # Verify the text was inserted
                time.sleep(0.15)
                current_text = _get_text_safe(target)
                if current_text and current_text.strip() == path.strip():
                    logger.info("[QS] UIA: Path insertion verified")
                    
                    # Auto-confirm if enabled
                    if settings.quick_search_auto_confirm:
                        time.sleep(0.2)
                        confirmed = False
                        
                        # Try to find and click Open/Save button
                        try:
                            for name in CONFIRM_NAMES:
                                try:
                                    btn = win.child_window(title=name, control_type='Button')
                                    if btn.exists():
                                        btn.invoke()
                                        confirmed = True
                                        logger.info(f"[QS] UIA: Confirmed via {name} button")
                                        break
                                except Exception:
                                    continue
                        except Exception:
                            pass
                        
                        # Fallback: Send Enter
                        if not confirmed:
                            try:
                                target.type_keys('{ENTER}', set_foreground=True)
                                logger.info("[QS] UIA: Confirmed via Enter")
                            except Exception:
                                try:
                                    win.type_keys('{ENTER}')
                                except Exception:
                                    pass
                    
                    self.status_bar.showMessage("QuickSearch: path filled via UIA" + (" and confirmed" if settings.quick_search_auto_confirm else ""))
                    return True
                else:
                    logger.warning(f"[QS] UIA: Text verification failed. Expected: '{path}', Got: '{current_text}'")
            
            return False
            
        except Exception as e:
            logger.error(f"[QS] Exception in _insert_path_uia: {e}")
            return False
    
    def _insert_path_win32(self, target, path: str, win) -> bool:
        """Insert path into Win32 Edit control using multiple methods with verification."""
        try:
            import time
            
            # Try multiple insertion methods
            for attempt in range(3):
                logger.info(f"[QS] Win32 insertion attempt {attempt + 1}")
                
                try:
                    target.set_focus()
                    time.sleep(0.15)
                except Exception:
                    pass
                
                filled = False
                
                # Method 1: type_keys with clear
                if attempt <= 1:
                    try:
                        target.type_keys('^a{BACKSPACE}')
                        time.sleep(0.05)
                        target.type_keys(path, with_spaces=True)
                        filled = True
                        logger.info("[QS] Win32: Set via type_keys")
                    except Exception:
                        pass
                
                # Method 2: Clipboard paste fallback
                if not filled:
                    try:
                        cb = QApplication.clipboard()
                        cb.setText(path)
                        
                        import keyboard
                        keyboard.send('ctrl+a')
                        time.sleep(0.05)
                        keyboard.send('ctrl+v')
                        filled = True
                        logger.info("[QS] Win32: Set via clipboard paste")
                    except Exception:
                        pass
                
                # Verify the text was inserted
                time.sleep(0.15)
                try:
                    current_text = target.window_text()
                    if current_text and current_text.strip() == path.strip():
                        logger.info("[QS] Win32: Path insertion verified")
                        
                        # Auto-confirm if enabled
                        if settings.quick_search_auto_confirm:
                            time.sleep(0.2)
                            confirmed = False
                            
                            # Try to find and click Open/Save button
                            try:
                                from app.core.vision import CONFIRM_NAMES
                                for name in CONFIRM_NAMES:
                                    try:
                                        btn = win.child_window(title=name, class_name='Button')
                                        if btn.exists():
                                            btn.click()
                                            confirmed = True
                                            logger.info(f"[QS] Win32: Confirmed via {name} button")
                                            break
                                    except Exception:
                                        continue
                            except Exception:
                                pass
                            
                            # Fallback: Send Enter
                            if not confirmed:
                                try:
                                    target.type_keys('{ENTER}')
                                    logger.info("[QS] Win32: Confirmed via Enter")
                                except Exception:
                                    try:
                                        win.type_keys('{ENTER}')
                                    except Exception:
                                        pass
                        
                        self.status_bar.showMessage("QuickSearch: path filled via Win32" + (" and confirmed" if settings.quick_search_auto_confirm else ""))
                        return True
                    else:
                        logger.warning(f"[QS] Win32: Text verification failed. Expected: '{path}', Got: '{current_text}'")
                except Exception as e:
                    logger.warning(f"[QS] Win32: Could not verify text insertion: {e}")
            
            return False
            
        except Exception as e:
            logger.error(f"[QS] Exception in _insert_path_win32: {e}")
            return False

    def _relative_click_into_filename(self, hwnd: int) -> bool:
        """If we saved a relative mouse point for this window, click it stealthily.
        Returns True if we clicked, False otherwise.
        """
        try:
            pt = getattr(self, '_rel_click_point', None)
            if not (hwnd and pt):
                return False
            rect = get_window_rect(hwnd)
            if not rect:
                return False
            l, t, r, b = rect
            x = l + max(0, pt[0])
            y = t + max(0, pt[1])
            # Stealth click using WinAPI: save cursor, click, restore
            user32 = ctypes.windll.user32
            cur = ctypes.wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(cur)):
                return False
            oldx, oldy = cur.x, cur.y
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.05)
            MOUSEEVENTF_LEFTDOWN = 0x0002
            MOUSEEVENTF_LEFTUP = 0x0004
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.02)
            user32.SetCursorPos(int(oldx), int(oldy))
            logger.info("[QS] Stealth clicked at %s,%s (relative fallback)", x, y)
            return True
        except Exception:
            return False

    def _paste_and_confirm(self, path: str) -> None:
        try:
            # Paste path and confirm
            try:
                cb = QApplication.clipboard(); cb.setText(path)
            except Exception:
                pass
            try:
                import keyboard  # type: ignore
                keyboard.send('ctrl+a')
                time.sleep(0.05)
                keyboard.send('ctrl+v')
                time.sleep(0.12)
                if settings.quick_search_auto_confirm:
                    keyboard.send('enter')
            except Exception:
                pass
            self.status_bar.showMessage("QuickSearch: path filled" + (" and confirmed" if settings.quick_search_auto_confirm else ""))
        except Exception:
            pass

    def _autofill_uia_pipeline(self, path: str) -> bool:
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            self.status_bar.showMessage("QuickSearch: locating file dialog…")
            logger.info("[QS] Autofill(UIA) start for path: %s", path)
            win = desktop.get_active()
            try:
                if win:
                    logger.info("[QS] Active window(UIA): title='%s' class='%s'", win.window_text(), getattr(win.element_info, 'class_name', '?'))
            except Exception:
                pass
            if not win:
                wins = desktop.windows()
                for w in reversed(wins):
                    try:
                        if not w.is_visible():
                            continue
                        btns = w.descendants(control_type='Button')
                        names = {b.window_text().lower() for b in btns}
                        if any(n in names for n in {'open', 'save', 'cancel'}):
                            edits = w.descendants(control_type='Edit')
                            if edits:
                                win = w
                                break
                    except Exception:
                        continue
            if not win:
                logger.info("[QS] No candidate file dialog found (UIA)")
                return False
            try:
                win.set_focus(); time.sleep(0.2)
            except Exception:
                pass
            target = None
            # A) FileNameControlHost
            try:
                host = win.child_window(auto_id="FileNameControlHost", control_type="Pane")
                eds = host.descendants(control_type='Edit') if host else []
                if eds:
                    target = eds[0]; logger.info("[QS] Using FileNameControlHost Edit")
            except Exception:
                pass
            # B) By label proximity
            if target is None:
                try:
                    texts = win.descendants(control_type='Text')
                except Exception:
                    texts = []
                try:
                    edits = win.descendants(control_type='Edit')
                except Exception:
                    edits = []
                label_rects = []
                for t in texts:
                    try:
                        name = (t.window_text() or '').strip()
                        if any(name.lower().startswith(lbl.lower().rstrip(':')) for lbl in FILENAME_LABELS):
                            label_rects.append(t.rectangle())
                    except Exception:
                        continue
                best = None
                best_dx = 10**9
                for e in edits:
                    try:
                        er = e.rectangle()
                        for lr in label_rects:
                            if er.left >= lr.right - 4 and (min(er.bottom, lr.bottom) - max(er.top, lr.top)) > 6:
                                dx = er.left - lr.right
                                if er.width() > 150 and er.height() < 60 and dx < best_dx:
                                    best = e; best_dx = dx
                    except Exception:
                        continue
                if best:
                    target = best; logger.info("[QS] Using Edit next to filename label")
            # C) Bottom-most edit heuristic
            if target is None:
                try:
                    edits = win.descendants(control_type='Edit')
                except Exception:
                    edits = []
                best = None; best_y = -1
                for e in edits:
                    try:
                        rect = e.rectangle()
                        if rect.width() > 150 and rect.height() < 60 and rect.top > best_y:
                            best = e; best_y = rect.top
                    except Exception:
                        continue
                target = best
            if not target:
                logger.info("[QS] No filename Edit found (UIA)")
                return False

            def _get_text_safe(ctrl):
                try:
                    return ctrl.get_value()
                except Exception:
                    try:
                        return ctrl.window_text()
                    except Exception:
                        return None

            for attempt in range(2):
                try:
                    target.set_focus(); time.sleep(0.12)
                    filled = False
                    if attempt == 0:
                        try:
                            target.set_value(path); filled = True; logger.info("[QS] Set via ValuePattern")
                        except Exception:
                            pass
                        if not filled:
                            try:
                                target.type_keys('^a{BACKSPACE}', set_foreground=True)
                                target.type_keys(path, with_spaces=True, set_foreground=True); filled = True; logger.info("[QS] Set via type_keys")
                            except Exception:
                                pass
                    else:
                        try:
                            target.type_keys('^a{BACKSPACE}', set_foreground=True)
                            target.type_keys(path, with_spaces=True, set_foreground=True); filled = True; logger.info("[QS] Retry set via type_keys")
                        except Exception:
                            pass
                        if not filled:
                            try:
                                cb = QApplication.clipboard(); cb.setText(path)
                                import keyboard  # type: ignore
                                keyboard.send('ctrl+v'); filled = True; logger.info("[QS] Retry set via clipboard paste")
                            except Exception:
                                pass
                    if not filled:
                        try:
                            cb = QApplication.clipboard(); cb.setText(path)
                            import keyboard  # type: ignore
                            keyboard.send('ctrl+v'); filled = True; logger.info("[QS] Set via clipboard paste (fallback)")
                        except Exception:
                            pass
                    time.sleep(0.12)
                    cur = _get_text_safe(target)
                    if cur and (cur.strip() == path):
                        if settings.quick_search_auto_confirm:
                            time.sleep(0.15)
                            confirmed = False
                            try:
                                for name in CONFIRM_NAMES:
                                    try:
                                        btn = win.child_window(title=name, control_type='Button')
                                        if btn:
                                            btn.invoke(); confirmed = True; logger.info("[QS] Confirmed via %s button", name); break
                                    except Exception:
                                        continue
                            except Exception:
                                pass
                            if not confirmed:
                                try:
                                    target.type_keys('{ENTER}', set_foreground=True); logger.info("[QS] Confirmed via Enter")
                                except Exception:
                                    try:
                                        win.type_keys('{ENTER}')
                                    except Exception:
                                        pass
                        self.status_bar.showMessage("QuickSearch: path filled" + (" and confirmed" if settings.quick_search_auto_confirm else ""))
                        return True
                except Exception:
                    logger.info("[QS] Exception in UIA attempt %d", attempt+1, exc_info=True)
                    continue
            return False
        except Exception:
            return False

    def _autofill_win32_pipeline(self, path: str) -> bool:
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="win32")
            logger.info("[QS] Autofill(win32) start for path: %s", path)
            win = desktop.get_active()
            try:
                if win:
                    logger.info("[QS] Active window(win32): title='%s' class='%s'", win.window_text(), getattr(win.element_info, 'class_name', '?'))
            except Exception:
                pass
            if not win:
                wins = desktop.windows()
                for w in reversed(wins):
                    try:
                        if not w.is_visible():
                            continue
                        btns = w.descendants(class_name='Button')
                        names = {b.window_text().lower() for b in btns}
                        if any(n in names for n in {'open', 'save', 'cancel'}):
                            edits = w.descendants(class_name='Edit')
                            if edits:
                                win = w; break
                    except Exception:
                        continue
            if not win:
                logger.info("[QS] No candidate file dialog found (win32)")
                return False
            try:
                win.set_focus(); time.sleep(0.2)
            except Exception:
                pass
            target = None
            try:
                combo_hosts = win.descendants(class_name='ComboBoxEx32')
                for host in combo_hosts:
                    eds = host.descendants(class_name='Edit')
                    if eds:
                        target = eds[0]; break
            except Exception:
                pass
            if target is None:
                try:
                    edits = win.descendants(class_name='Edit')
                except Exception:
                    edits = []
                if edits:
                    target = edits[-1]
            if not target:
                logger.info("[QS] No filename Edit found (win32)")
                return False
            for attempt in range(2):
                try:
                    target.set_focus(); time.sleep(0.12)
                    done = False
                    if attempt == 0:
                        try:
                            target.type_keys('^a{BACKSPACE}')
                            target.type_keys(path, with_spaces=True); done = True
                        except Exception:
                            pass
                    if not done:
                        try:
                            cb = QApplication.clipboard(); cb.setText(path)
                            import keyboard  # type: ignore
                            keyboard.send('ctrl+v'); done = True
                        except Exception:
                            pass
                    if not done:
                        continue
                    if settings.quick_search_auto_confirm:
                        time.sleep(0.15)
                        try:
                            for name in CONFIRM_NAMES:
                                btn = win.child_window(title=name, class_name='Button')
                                if btn:
                                    btn.click_input(); logger.info("[QS] Confirmed via %s button (win32)", name); break
                        except Exception:
                            pass
                        try:
                            win.type_keys('{ENTER}')
                        except Exception:
                            try:
                                target.type_keys('{ENTER}')
                            except Exception:
                                pass
                    self.status_bar.showMessage("QuickSearch: path filled" + (" and confirmed" if settings.quick_search_auto_confirm else ""))
                    return True
                except Exception:
                    logger.info("[QS] Exception in win32 attempt %d", attempt+1, exc_info=True)
                    continue
            return False
        except Exception:
            return False

    def on_toggle_openai(self, checked: bool):
        settings.set_use_openai_fallback(bool(checked))
        self.use_openai_checkbox.setText(
            "Use ChatGPT (OpenAI) Fallback: ON" if checked else "Use ChatGPT (OpenAI) Fallback: OFF"
        )
        self.status_bar.showMessage("OpenAI fallback " + ("enabled" if checked else "disabled"))

    def on_save_openai(self):
        key = self.openai_key_input.text().strip()
        settings.set_openai_api_key(key)
        model = self.openai_model_combo.currentText().strip() or settings.openai_vision_model
        settings.set_openai_vision_model(model)
        self.status_bar.showMessage("OpenAI settings saved")

    def on_delete_openai_key(self):
        settings.delete_openai_api_key()
        self.openai_key_input.clear()
        self.status_bar.showMessage("OpenAI API key deleted")

    def on_toggle_gpt_rerank(self, checked: bool):
        settings.set_use_openai_search_rerank(bool(checked))
        self.gpt_rerank_button.setText("GPT Rerank: ON" if checked else "GPT Rerank: OFF")
        self.status_bar.showMessage("GPT rerank " + ("enabled" if checked else "disabled"))

    # Quick Search settings handlers
    def on_qs_autopaste(self, checked: bool):
        settings.set_quick_search_autopaste(bool(checked))
        self.qs_autopaste_btn.setText("Auto-Paste: ON" if checked else "Auto-Paste: OFF")
        self.status_bar.showMessage("Quick Search auto-paste " + ("enabled" if checked else "disabled"))

    def on_qs_autoconfirm(self, checked: bool):
        settings.set_quick_search_auto_confirm(bool(checked))
        self.qs_autoconfirm_btn.setText("Auto-Confirm: ON" if checked else "Auto-Confirm: OFF")
        self.status_bar.showMessage("Quick Search auto-confirm " + ("enabled" if checked else "disabled"))

    def on_qs_save_shortcut(self):
        sc = (self.qs_shortcut_input.text() or '').strip()
        if not sc:
            QMessageBox.warning(self, "Shortcut", "Please enter a shortcut (e.g., ctrl+alt+h)")
            return
        settings.set_quick_search_shortcut(sc)
        self.status_bar.showMessage(f"Quick Search shortcut saved: {sc}")
        # Hotkey will take effect on next app start; to apply now, restart the app

    def on_debug_cell_changed(self, item: QTableWidgetItem) -> None:
        # Avoid handling during table population
        if getattr(self, '_populating_debug_table', False):
            return
        try:
            row = item.row()
            col = item.column()
            # file id is stored in column 0's user data
            name_item = self.debug_table.item(row, 0)
            file_id = name_item.data(Qt.UserRole) if name_item else None
            if not file_id:
                return
            text = item.text()
            if col == 4:  # Label
                ok = file_index.update_file_field(file_id, 'label', text)
            elif col == 5:  # Tags
                tags = [t.strip() for t in (text or '').split(',') if t.strip()]
                ok = file_index.update_file_field(file_id, 'tags', tags)
            elif col == 6:  # Caption
                ok = file_index.update_file_field(file_id, 'caption', text)
            elif col == 10:  # Purpose
                # update metadata JSON
                # read existing metadata from current table row if possible
                meta_text = self.debug_table.item(row, 12)  # detected text col; not metadata
                # fallback: fetch from db if needed is overkill; we set only one key
                meta = {}
                try:
                    rec = file_index.get_file_by_path(self.debug_table.item(row, 8).text())  # unlikely path in col8; ignore if fails
                except Exception:
                    rec = None
                meta = (rec or {}).get('metadata', {}) if rec else {}
                meta['purpose'] = text
                ok = file_index.update_file_field(file_id, 'metadata', meta)
            elif col == 11:  # Suggested filename
                meta = {}
                try:
                    rec = file_index.get_file_by_path(self.debug_table.item(row, 8).text())
                except Exception:
                    rec = None
                meta = (rec or {}).get('metadata', {}) if rec else {}
                meta['suggested_filename'] = text
                ok = file_index.update_file_field(file_id, 'metadata', meta)
            else:
                return
            if ok:
                self.status_bar.showMessage("Saved edit")
            else:
                QMessageBox.critical(self, "Save Error", "Failed to save your edit.")
        except Exception as e:
            QMessageBox.critical(self, "Edit Error", f"Failed to apply edit:\n{e}")
    
    def on_debug_cell_double_clicked(self, row: int, col: int):
        """Show full cell content in a popup when double-clicked, with edit option."""
        item = self.debug_table.item(row, col)
        if not item:
            return
        
        original_text = item.text()
        
        # Get column name
        header = self.debug_table.horizontalHeaderItem(col)
        column_name = header.text() if header else f"Column {col}"
        
        # Editable columns (others are read-only)
        editable_columns = {4: 'label', 5: 'tags', 6: 'caption', 10: 'purpose', 11: 'suggested_filename'}
        is_editable = col in editable_columns
        
        # Create a styled dialog
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QDialogButtonBox, QLabel
        
        dialog = QDialog(self)
        dialog.setWindowTitle(f"📄 {column_name}")
        dialog.setMinimumSize(600, 400)
        
        # Style based on current theme
        is_dark = settings.theme == 'dark'
        
        if is_dark:
            dialog_style = """
                QDialog {
                    background-color: #1E1E1E;
                }
                QLabel {
                    color: #00B8D4;
                    font-size: 16px;
                    font-weight: bold;
                    padding: 10px 0;
                    background-color: transparent;
                }
                QTextEdit {
                    background-color: #2A2A2A;
                    color: #FFFFFF;
                    border: 2px solid #00B8D4;
                    border-radius: 8px;
                    font-size: 14px;
                    padding: 15px;
                }
                QTextEdit:focus {
                    border: 2px solid #00E5FF;
                }
                QPushButton {
                    background-color: #00B8D4;
                    color: white;
                    font-size: 13px;
                    font-weight: bold;
                    border: none;
                    border-radius: 6px;
                    padding: 10px 25px;
                    min-width: 100px;
                }
                QPushButton:hover {
                    background-color: #00ACC1;
                }
            """
        else:
            dialog_style = """
                QDialog {
                    background-color: #FFFFFF;
                }
                QLabel {
                    color: #00838F;
                    font-size: 16px;
                    font-weight: bold;
                    padding: 10px 0;
                    background-color: transparent;
                }
                QTextEdit {
                    background-color: #F5F5F5;
                    color: #1A1A1A;
                    border: 2px solid #00B8D4;
                    border-radius: 8px;
                    font-size: 14px;
                    padding: 15px;
                }
                QTextEdit:focus {
                    border: 2px solid #00ACC1;
                }
                QPushButton {
                    background-color: #00B8D4;
                    color: white;
                    font-size: 13px;
                    font-weight: bold;
                    border: none;
                    border-radius: 6px;
                    padding: 10px 25px;
                    min-width: 100px;
                }
                QPushButton:hover {
                    background-color: #00ACC1;
                }
            """
        
        dialog.setStyleSheet(dialog_style)
        
        layout = QVBoxLayout(dialog)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # Header label
        header_label = QLabel(f"{column_name}")
        if is_editable:
            header_label.setText(f"{column_name} (editable)")
        layout.addWidget(header_label)
        
        # Text edit area
        text_edit = QTextEdit()
        text_edit.setPlainText(original_text or "")
        text_edit.setReadOnly(not is_editable)
        layout.addWidget(text_edit)
        
        # Buttons
        if is_editable:
            button_box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            button_box.accepted.connect(dialog.accept)
            button_box.rejected.connect(dialog.reject)
        else:
            button_box = QDialogButtonBox(QDialogButtonBox.Ok)
            button_box.accepted.connect(dialog.accept)
        layout.addWidget(button_box)
        
        result = dialog.exec()
        
        # Save changes if user clicked Save and content changed
        if is_editable and result == QDialog.Accepted:
            new_text = text_edit.toPlainText()
            if new_text != original_text:
                # Update the table cell
                item.setText(new_text)
                self.status_bar.showMessage(f"Updated {column_name}")
    
    def select_source_folder(self):
        """Select source folder."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Source Folder", str(Path.home())
        )
        
        if folder:
            self.source_path = Path(folder)
            self.source_label.setText(f"Source folder: {self.source_path}")
            self.source_label.setStyleSheet("")
            self.update_scan_button_state()
            self.status_bar.showMessage(f"Source folder selected: {self.source_path}")
    
    def select_destination_folder(self):
        """Select destination folder."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Destination Folder", str(Path.home())
        )
        
        if folder:
            self.destination_path = Path(folder)
            self.dest_label.setText(f"Destination folder: {self.destination_path}")
            self.dest_label.setStyleSheet("")
            self.update_scan_button_state()
            self.status_bar.showMessage(f"Destination folder selected: {self.destination_path}")
    
    def update_scan_button_state(self):
        """Update scan button enabled state."""
        self.scan_button.setEnabled(
            self.source_path is not None and self.destination_path is not None
        )
    
    def scan_and_plan(self):
        """Scan source folder and create move plan."""
        if not self.source_path or not self.destination_path:
            return
        
        # Clear previous results
        self.file_table.setRowCount(0)
        self.summary_text.clear()
        self.scanned_files = []
        self.move_plan = []
        
        # Show progress
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate progress
        self.scan_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        
        # Start scan worker
        self.scan_worker = ScanWorker(self.source_path)
        self.scan_worker.scan_completed.connect(self.on_scan_completed)
        self.scan_worker.scan_error.connect(self.on_scan_error)
        self.scan_worker.progress_updated.connect(self.status_bar.showMessage)
        self.scan_worker.start()
    
    def on_scan_completed(self, files: List[Dict[str, Any]]):
        """Handle scan completion."""
        self.scanned_files = files
        
        if not files:
            self.status_bar.showMessage("No files found in source directory")
            self.progress_bar.setVisible(False)
            self.scan_button.setEnabled(True)
            return
        
        # Create move plan
        self.status_bar.showMessage("Creating move plan...")
        self.move_plan = create_move_plan(files, self.source_path, self.destination_path)
        
        # Display results
        self.display_results()
        
        # Update UI
        self.progress_bar.setVisible(False)
        self.scan_button.setEnabled(True)
        self.apply_button.setEnabled(len(self.move_plan) > 0)
        
        self.status_bar.showMessage(f"Scan completed. Found {len(files)} files.")
    
    def on_scan_error(self, error: str):
        """Handle scan error."""
        QMessageBox.critical(self, "Scan Error", f"Error scanning directory:\n{error}")
        self.progress_bar.setVisible(False)
        self.scan_button.setEnabled(True)
        self.status_bar.showMessage("Scan failed")
    
    def display_results(self):
        """Display scan and plan results."""
        # Populate table
        self.file_table.setRowCount(len(self.move_plan))
        
        for row, move in enumerate(self.move_plan):
            # File name
            name_item = QTableWidgetItem(move['file_name'])
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.file_table.setItem(row, 0, name_item)
            
            # Category
            category_item = QTableWidgetItem(move['category'])
            category_item.setFlags(category_item.flags() & ~Qt.ItemIsEditable)
            self.file_table.setItem(row, 1, category_item)
            
            # Size
            size_mb = round(move['size'] / (1024 * 1024), 2)
            size_item = QTableWidgetItem(f"{size_mb} MB")
            size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
            self.file_table.setItem(row, 2, size_item)
            
            # Planned destination
            dest_item = QTableWidgetItem(move['relative_destination'])
            dest_item.setFlags(dest_item.flags() & ~Qt.ItemIsEditable)
            self.file_table.setItem(row, 3, dest_item)
        
        # Display summary
        summary = get_plan_summary(self.move_plan)
        summary_text = f"""
Move Plan Summary:
• Total files: {summary['total_files']}
• Total size: {summary['total_size_mb']} MB
• Categories:
"""
        
        for category, info in summary['categories'].items():
            count = info['count']
            size_mb = round(info['size'] / (1024 * 1024), 2)
            summary_text += f"  - {category}: {count} files ({size_mb} MB)\n"
        
        self.summary_text.setPlainText(summary_text)
    
    def apply_moves(self):
        """Apply the move plan."""
        if not self.move_plan:
            return
        
        # Validate plan
        is_valid, errors = validate_move_plan(
            self.move_plan, self.source_path, self.destination_path
        )
        
        if not is_valid:
            error_text = "\n".join(errors)
            QMessageBox.critical(self, "Validation Error", f"Move plan validation failed:\n{error_text}")
            return
        
        # Check disk space
        has_space, space_error = validate_destination_space(
            self.move_plan, self.destination_path
        )
        
        if not has_space:
            QMessageBox.critical(self, "Insufficient Space", space_error)
            return
        
        # Confirm action
        reply = QMessageBox.question(
            self, "Confirm Moves",
            f"Are you sure you want to move {len(self.move_plan)} files?\n\n"
            "This action cannot be undone in this version.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Apply moves
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(self.move_plan))
        self.apply_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        
        success, errors, log_file = apply_moves(self.move_plan)
        
        self.progress_bar.setVisible(False)
        self.scan_button.setEnabled(True)
        
        if success:
            QMessageBox.information(
                self, "Success",
                f"Successfully moved {len(self.move_plan)} files!\n\n"
                f"Move log saved to: {log_file}"
            )
            self.status_bar.showMessage("Moves completed successfully")
            
            # Clear results
            self.file_table.setRowCount(0)
            self.summary_text.clear()
            self.scanned_files = []
            self.move_plan = []
            self.apply_button.setEnabled(False)
        else:
            error_text = "\n".join(errors[:10])  # Show first 10 errors
            if len(errors) > 10:
                error_text += f"\n... and {len(errors) - 10} more errors"
            
            QMessageBox.critical(
                self, "Move Errors",
                f"Some files could not be moved:\n{error_text}"
            )
            self.status_bar.showMessage("Moves completed with errors")
            self.apply_button.setEnabled(True)

    # Search functionality methods
    def select_index_folder(self):
        """Select folder to index for search."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder to Index", str(Path.home())
        )
        
        if folder:
            self.index_path = Path(folder)
            self.index_label.setText(f"Index folder: {self.index_path}")
            self.index_label.setStyleSheet("")
            self.index_button_action.setEnabled(True)
            self.status_bar.showMessage(f"Index folder selected: {self.index_path}")
    
    def index_directory(self):
        """Index the selected directory for search."""
        if not hasattr(self, 'index_path') or not self.index_path:
            return
        
        # If already indexing, add to queue instead of replacing
        if self.is_indexing:
            self._add_to_index_queue(self.index_path)
            return
        
        # Start indexing
        self._start_indexing_path(self.index_path)
    
    def _add_to_index_queue(self, path: Path):
        """Add a path to the indexing queue."""
        logger.info(f"_add_to_index_queue called with path: {path}")
        if path not in self.index_queue:
            self.index_queue.append(path)
            logger.info(f"Queue now has {len(self.index_queue)} items: {[p.name for p in self.index_queue]}")
            self._update_queue_ui()
            self.status_bar.showMessage(f"Added to queue: {path.name} ({len(self.index_queue)} pending)")
    
    def _update_queue_ui(self):
        """Update the compact queue indicator and expand section when queue is visible."""
        logger.info(f"_update_queue_ui called. Queue has {len(self.index_queue)} items")
        if self.index_queue:
            logger.info("Queue not empty, showing queue_row")
            # Add spacing above the queue row
            self.queue_top_spacer.setVisible(True)
            self.queue_top_spacer.setFixedHeight(25)  # Space above queue
            
            self.queue_row.setVisible(True)
            self.queue_row.show()  # Force show
            self.queue_spacer.setVisible(True)
            self.queue_spacer.setFixedHeight(15)  # Space below queue
            logger.info(f"queue_row.isVisible() = {self.queue_row.isVisible()}")
            
            count = len(self.index_queue)
            self.queue_label.setText(f"📋 Queue: {count} pending")
            # Show first 2 item names inline
            names = [p.name for p in self.index_queue[:2]]
            if count > 2:
                names.append(f"+{count - 2} more")
            self.queue_items_label.setText(" • ".join(names))
            # Expand the index group to accommodate queue
            self.index_group.setMinimumHeight(self.index_group.sizeHint().height() + 80)
        else:
            self.queue_top_spacer.setVisible(False)
            self.queue_top_spacer.setFixedHeight(0)
            self.queue_row.setVisible(False)
            self.queue_spacer.setVisible(False)
            self.queue_spacer.setFixedHeight(0)
            # Reset minimum height
            self.index_group.setMinimumHeight(0)
    
    def _clear_index_queue(self):
        """Clear the indexing queue."""
        self.index_queue.clear()
        self._update_queue_ui()
        self.status_bar.showMessage("Queue cleared")
    
    def _start_indexing_path(self, path: Path):
        """Start indexing a specific path."""
        self.is_indexing = True
        self.index_path = path
        self.index_label.setText(f"Indexing: {path.name}")
        self.index_button_action.setText("➕ Add to Queue")
        self.index_button_action.setEnabled(True)  # Allow adding more
        
        # Show progress controls with explicit text settings
        self.progress_bar.setVisible(True)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p% (%v / %m files)")
        self.progress_bar.setRange(0, 0)  # Start indeterminate
        self.progress_bar.setProperty("paused", False)
        self.progress_bar.style().unpolish(self.progress_bar)
        self.progress_bar.style().polish(self.progress_bar)
        self.drop_zone.setVisible(False)  # Hide drop zone during indexing
        self.index_percent_label.setVisible(True)
        self.index_percent_label.setText("0%")
        self.index_pause_btn.setVisible(True)
        self.index_pause_btn.setText("⏸ Pause")
        self.index_cancel_btn.setVisible(True)
        self.index_progress_label.setVisible(True)
        # Keep button enabled so user can add more files to queue
        # self.index_button_action.setEnabled(False)  # Removed - allow adding to queue
        self.status_bar.showMessage("Indexing directory...")
        
        # Create the worker
        self.index_worker = IndexWorker(self.index_path)
        self.index_worker.index_completed.connect(self.on_index_completed)
        self.index_worker.index_error.connect(self.on_index_error)
        self.index_worker.progress_updated.connect(self.status_bar.showMessage)
        # Connect progress_data signal to slot for thread-safe UI updates
        self.index_worker.progress_data.connect(self._on_index_progress)
        
        # Progress callback that emits signal instead of using QTimer
        def progress_cb(done: int, total: int, message: str):
            # Check for cancel first
            if hasattr(self, 'index_worker') and self.index_worker:
                if self.index_worker.is_cancelled():
                    raise InterruptedError("Indexing cancelled by user")
            
            # Emit signal - this will be received on the main thread
            self.index_worker.progress_data.emit(done, total, message)
            
            # Now wait if paused
            if hasattr(self, 'index_worker') and self.index_worker:
                self.index_worker.wait_if_paused()
                if self.index_worker.is_cancelled():
                    raise InterruptedError("Indexing cancelled by user")

        # Monkey-patch run to inject callback without refactor
        def run_with_progress():
            try:
                result = search_service.index_directory(self.index_path, progress_cb=progress_cb)
                self.index_worker.index_completed.emit(result)
            except Exception as e:
                self.index_worker.index_error.emit(str(e))
        self.index_worker.run = run_with_progress  # type: ignore
        self.index_worker.start()
    
    def _toggle_index_pause(self):
        """Toggle pause/resume for indexing - IMMEDIATE UI response."""
        if not hasattr(self, 'index_worker') or not self.index_worker:
            return
        
        if self.index_worker.is_paused():
            # Resume - restart progress bar animation
            self.index_worker.resume()
            self.index_pause_btn.setText("⏸ Pause")
            self.status_bar.showMessage("Indexing resumed...")
            self.index_progress_label.setStyleSheet("")  # Normal color
            self.index_percent_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #00B8D4;")
            # Reset progress bar to normal color using property
            self.progress_bar.setProperty("paused", False)
            self.progress_bar.style().unpolish(self.progress_bar)
            self.progress_bar.style().polish(self.progress_bar)
        else:
            # Pause - IMMEDIATELY freeze the UI
            self.index_worker.pause()
            self.index_pause_btn.setText("▶ Resume")
            self.status_bar.showMessage("⏸ PAUSED - Click Resume to continue")
            self.index_progress_label.setText("⏸ PAUSED")
            self.index_progress_label.setStyleSheet("color: #FFA500; font-weight: bold;")  # Orange
            self.index_percent_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #FFA500;")
            # Change progress bar to orange when paused using property
            self.progress_bar.setProperty("paused", True)
            self.progress_bar.style().unpolish(self.progress_bar)
            self.progress_bar.style().polish(self.progress_bar)
    
    def _cancel_indexing(self):
        """Cancel the current indexing operation - IMMEDIATE UI response."""
        if not hasattr(self, 'index_worker') or not self.index_worker:
            return
        
        reply = QMessageBox.question(
            self,
            "Cancel Indexing",
            "Are you sure you want to cancel indexing?\n\nFiles already indexed will be kept.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            # IMMEDIATELY update UI before background thread stops
            self.index_worker.cancel()
            self._hide_index_controls()
            self.status_bar.showMessage("Indexing cancelled. Files already indexed have been saved.")
            
            # Refresh views with what was indexed
            stats = search_service.get_index_statistics()
            self.update_search_statistics(stats)
            self.search_button.setEnabled(True)
            self.refresh_debug_view()
    
    def _on_index_progress(self, done: int, total: int, message: str):
        """Slot to handle progress updates from the worker thread (runs on main thread)."""
        try:
            # Don't update UI if paused (keep showing PAUSED message)
            if hasattr(self, 'index_worker') and self.index_worker and self.index_worker.is_paused():
                return
            
            # Don't update if cancelled
            if hasattr(self, 'index_worker') and self.index_worker and self.index_worker.is_cancelled():
                return
            
            self.progress_bar.setVisible(True)
            if total > 0:
                self.progress_bar.setRange(0, total)
                self.progress_bar.setValue(done)
                percent = int((done / total) * 100)
                # Update the prominent percentage label
                self.index_percent_label.setText(f"{percent}%")
                self.index_percent_label.setStyleSheet("font-size: 18px; font-weight: bold; color: #00B8D4;")
                self.index_progress_label.setText(f"Processing file {done} of {total}")
                self.index_progress_label.setStyleSheet("")  # Reset style
            else:
                self.progress_bar.setRange(0, 0)
                self.index_percent_label.setText("...")
                self.index_progress_label.setText("Scanning files...")
            self.status_bar.showMessage(message)
        except Exception:
            pass  # UI might be closed
    
    def _hide_index_controls(self):
        """Hide indexing controls after completion."""
        self.progress_bar.setVisible(False)
        self.index_pause_btn.setVisible(False)
        self.index_cancel_btn.setVisible(False)
        self.index_progress_label.setVisible(False)
        self.index_percent_label.setVisible(False)
        self.drop_zone.setVisible(True)  # Show drop zone again
        self.index_button_action.setEnabled(True)
    
    def on_index_completed(self, result: Dict[str, Any]):
        """Handle index completion."""
        # Check if there are more items in the queue
        if self.index_queue:
            next_path = self.index_queue.pop(0)
            self._update_queue_ui()
            self.status_bar.showMessage(f"Starting next: {next_path.name}")
            QTimer.singleShot(300, lambda: self._start_indexing_path(next_path))
            return
        
        # No more items - finish up
        self.is_indexing = False
        self.index_button_action.setText("Index Directory")
        self._hide_index_controls()
        
        if 'error' in result:
            QMessageBox.critical(self, "Index Error", f"Error indexing directory:\n{result['error']}")
            self.status_bar.showMessage("Indexing failed")
            return
        
        if result.get('cancelled'):
            self.status_bar.showMessage(
                f"Indexing cancelled. Indexed {result.get('indexed_files', 0)} files before cancellation."
            )
            # Still update stats for what was indexed
            stats = search_service.get_index_statistics()
            self.update_search_statistics(stats)
            self.search_button.setEnabled(True)
            self.refresh_debug_view()
            return
        
        # Update search statistics
        stats = search_service.get_index_statistics()
        self.update_search_statistics(stats)
        
        # Enable search
        self.search_button.setEnabled(True)
        
        # Refresh debug view
        self.refresh_debug_view()
        
        self.status_bar.showMessage(
            f"Indexed {result['indexed_files']} files ({result['files_with_ocr']} with OCR)"
        )
    
    def on_index_error(self, error: str):
        """Handle index error."""
        self._hide_index_controls()
        
        # Check if it was a cancellation
        if "cancelled" in error.lower() or "interrupted" in error.lower():
            self.status_bar.showMessage("Indexing cancelled")
            return
        
        QMessageBox.critical(self, "Index Error", f"Error indexing directory:\n{error}")
        self.status_bar.showMessage("Indexing failed")
    
    def on_index_entire_pc(self):
        """Handle 'Index Entire PC' button click with warning."""
        # Show warning dialog
        reply = QMessageBox.warning(
            self,
            "Index Entire PC",
            "⚠️ This will scan and index ALL files on your computer.\n\n"
            "This operation can take a very long time (hours) depending on:\n"
            "• Number of files on your PC\n"
            "• Disk speed\n"
            "• AI analysis settings\n\n"
            "The app will remain usable during indexing.\n\n"
            "Do you want to continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self._start_pc_indexing()
    
    def _start_pc_indexing(self):
        """Start indexing all user folders on the PC."""
        import os
        from pathlib import Path
        
        # Get common user directories to index
        home = Path.home()
        user_folders = []
        
        # Windows common folders
        if os.name == 'nt':
            for folder_name in ['Desktop', 'Documents', 'Downloads', 'Pictures', 'Videos', 'Music']:
                folder = home / folder_name
                if folder.exists():
                    user_folders.append(folder)
            # Also check OneDrive folders
            for item in home.iterdir():
                if item.is_dir() and 'OneDrive' in item.name:
                    user_folders.append(item)
        else:
            # macOS/Linux
            for folder_name in ['Desktop', 'Documents', 'Downloads', 'Pictures', 'Videos', 'Music']:
                folder = home / folder_name
                if folder.exists():
                    user_folders.append(folder)
        
        if not user_folders:
            QMessageBox.information(self, "No Folders Found", "Could not find any standard user folders to index.")
            return
        
        # Show what will be indexed
        folder_list = "\n".join([f"• {f}" for f in user_folders])
        confirm = QMessageBox.information(
            self,
            "Indexing Started",
            f"Starting to index the following folders:\n\n{folder_list}\n\n"
            "This will run in the background. You can continue using the app.",
            QMessageBox.Ok
        )
        
        # Index each folder sequentially (in background)
        self._pc_index_queue = list(user_folders)
        self._index_next_pc_folder()
    
    def _index_next_pc_folder(self):
        """Index the next folder in the PC indexing queue."""
        if not hasattr(self, '_pc_index_queue') or not self._pc_index_queue:
            self.status_bar.showMessage("PC indexing complete!", 5000)
            return
        
        folder = self._pc_index_queue.pop(0)
        self.index_path = folder
        self.index_label.setText(f"Index folder: {folder}")
        self.status_bar.showMessage(f"Indexing: {folder}")
        
        # Start indexing this folder
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.index_button_action.setEnabled(False)
        
        def progress_cb(done: int, total: int, message: str):
            def update_ui():
                try:
                    self.progress_bar.setVisible(True)
                    if total > 0:
                        self.progress_bar.setRange(0, total)
                        self.progress_bar.setValue(done)
                    else:
                        self.progress_bar.setRange(0, 0)
                    self.status_bar.showMessage(f"{folder.name}: {message}")
                except Exception:
                    pass
            QTimer.singleShot(0, update_ui)
        
        self.index_worker = IndexWorker(folder)
        
        def on_complete(result):
            self.progress_bar.setVisible(False)
            self.index_button_action.setEnabled(True)
            stats = search_service.get_index_statistics()
            self.update_search_statistics(stats)
            # Continue with next folder
            QTimer.singleShot(100, self._index_next_pc_folder)
        
        def on_error(error):
            logger.error(f"Error indexing {folder}: {error}")
            # Continue with next folder despite error
            QTimer.singleShot(100, self._index_next_pc_folder)
        
        self.index_worker.index_completed.connect(on_complete)
        self.index_worker.index_error.connect(on_error)
        
        def run_with_progress():
            try:
                result = search_service.index_directory(folder, progress_cb=progress_cb)
                self.index_worker.index_completed.emit(result)
            except Exception as e:
                self.index_worker.index_error.emit(str(e))
        
        self.index_worker.run = run_with_progress
        self.index_worker.start()
    
    def on_toggle_auto_index_downloads(self, checked: bool):
        """Handle auto-index toggle with warning."""
        if checked:
            # Show warning before enabling
            reply = QMessageBox.warning(
                self,
                "Enable Auto-Index New Files",
                "📥 This will automatically index any NEW files added to common folders:\n\n"
                "• Downloads\n"
                "• Desktop\n"
                "• Documents\n"
                "• Pictures\n"
                "• Videos\n"
                "• Music\n"
                "• OneDrive (if present)\n\n"
                "Only files added AFTER enabling will be indexed.\n"
                "Existing files will NOT be touched.\n\n"
                "Do you want to enable this feature?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                settings.set_auto_index_downloads(True)
                self.auto_index_downloads_btn.setText("📥 Auto-Index New Files: ON")
                self._start_downloads_watcher()
                self.auto_index_status.setText("Monitoring folders for new files...")
            else:
                # User cancelled - uncheck the button
                self.auto_index_downloads_btn.setChecked(False)
        else:
            settings.set_auto_index_downloads(False)
            self.auto_index_downloads_btn.setText("📥 Auto-Index New Files: OFF")
            self._stop_downloads_watcher()
            self.auto_index_status.setText("")
    
    def _start_downloads_watcher(self):
        """Start watching common folders for new files."""
        from PySide6.QtCore import QFileSystemWatcher
        from pathlib import Path
        import os
        
        home = Path.home()
        
        # Common folders to watch
        folder_names = ['Downloads', 'Desktop', 'Documents', 'Pictures', 'Videos', 'Music']
        folders_to_watch = []
        
        for name in folder_names:
            folder = home / name
            if folder.exists() and folder.is_dir():
                folders_to_watch.append(folder)
        
        # Also check for OneDrive folders
        for item in home.iterdir():
            if item.is_dir() and 'OneDrive' in item.name:
                folders_to_watch.append(item)
                # Also add common subfolders in OneDrive
                for name in ['Desktop', 'Documents', 'Pictures']:
                    subfolder = item / name
                    if subfolder.exists() and subfolder.is_dir():
                        folders_to_watch.append(subfolder)
        
        if not folders_to_watch:
            self.auto_index_status.setText("No common folders found to watch")
            return
        
        # Initialize the background worker for auto-indexing
        if not hasattr(self, '_auto_index_worker'):
            self._auto_index_worker = AutoIndexWorker()
            self._auto_index_worker.file_indexed.connect(self._on_file_indexed)
            self._auto_index_worker.status_update.connect(self._on_auto_index_status)
        
        if not hasattr(self, '_folder_watcher'):
            self._folder_watcher = QFileSystemWatcher(self)
            self._folder_watcher.directoryChanged.connect(self._on_watched_folder_changed)
        
        # Track known files per folder (so we only index NEW files)
        self._watched_folders = {}
        for folder in folders_to_watch:
            self._folder_watcher.addPath(str(folder))
            # Capture current files as "known" - these won't be indexed
            try:
                self._watched_folders[str(folder)] = set(folder.iterdir())
            except Exception:
                self._watched_folders[str(folder)] = set()
        
        logger.info(f"Started watching {len(folders_to_watch)} folders for new files")
        self.auto_index_status.setText(f"Monitoring {len(folders_to_watch)} folders...")
    
    def _stop_downloads_watcher(self):
        """Stop watching folders."""
        if hasattr(self, '_folder_watcher'):
            self._folder_watcher.removePaths(self._folder_watcher.directories())
            self._watched_folders = {}
            logger.info("Stopped watching folders")
    
    def _on_watched_folder_changed(self, path: str):
        """Handle changes in any watched folder."""
        from pathlib import Path
        
        if not hasattr(self, '_watched_folders'):
            return
        
        folder_path = Path(path)
        if str(folder_path) not in self._watched_folders:
            return
        
        try:
            current_files = set(folder_path.iterdir())
        except Exception:
            return
        
        known_files = self._watched_folders.get(str(folder_path), set())
        
        # Find new files (only files added AFTER we started watching)
        new_files = current_files - known_files
        
        # Update known files
        self._watched_folders[str(folder_path)] = current_files
        
        for new_file in new_files:
            if new_file.is_file():
                logger.info(f"New file detected in {folder_path.name}: {new_file.name}")
                self.auto_index_status.setText(f"Queued: {new_file.name}")
                # Add to background worker queue (non-blocking)
                if hasattr(self, '_auto_index_worker'):
                    self._auto_index_worker.add_file(new_file)
    
    def _on_file_indexed(self, filename: str, status: str):
        """Handle file indexed signal from background worker."""
        if status == 'success':
            self.auto_index_status.setText(f"Indexed: {filename}")
        elif status == 'skipped':
            self.auto_index_status.setText(f"Already indexed: {filename}")
        else:
            self.auto_index_status.setText(f"Error indexing: {filename}")
        
        # Clear status after delay
        QTimer.singleShot(3000, lambda: self.auto_index_status.setText("Monitoring Downloads folder..."))
    
    def _on_auto_index_status(self, message: str):
        """Handle status update from background worker."""
        self.auto_index_status.setText(message)
    
    def update_search_button_state(self):
        """Update search button enabled state."""
        has_index = hasattr(self, 'index_path') and self.index_path is not None
        has_query = bool(self.search_input.text().strip())
        self.search_button.setEnabled(has_index and has_query)
    
    def search_files(self):
        """Search for files with NLP parsing and filters."""
        query = self.search_input.text().strip()
        
        # Check if we have UI filters even without a query
        ui_type = self.type_filter.currentText()
        ui_date = self.date_filter.currentText()
        has_ui_filters = ui_type != "All Types" or ui_date != "Any Time"
        
        if not query and not has_ui_filters:
            return
        
        self.status_bar.showMessage(f"Searching for: {query}" if query else "Browsing files...")
        
        # Parse query for natural language filters
        parsed = parse_query(query) if query else {'clean_query': '', 'date_filter': None, 'type_filter': None, 'date_range': (None, None), 'extensions': None}
        clean_query = parsed['clean_query']
        
        # UI filters already retrieved above
        
        # Determine type filter (NLP detected takes priority over UI)
        type_filter = parsed['type_filter']
        extensions = parsed['extensions']
        
        if type_filter:
            # NLP detected a type filter - it takes priority
            # Update UI dropdown to reflect the detected filter
            ui_name = FILTER_TO_UI_TYPE.get(type_filter)
            if ui_name:
                idx = self.type_filter.findText(ui_name)
                if idx >= 0:
                    self.type_filter.blockSignals(True)
                    self.type_filter.setCurrentIndex(idx)
                    self.type_filter.blockSignals(False)
            extensions = TYPE_EXTENSIONS.get(type_filter, [])
        elif ui_type != "All Types":
            # No NLP type filter - use UI dropdown selection
            type_filter = UI_TYPE_MAPPING.get(ui_type)
            extensions = TYPE_EXTENSIONS.get(type_filter, [])
        
        # Determine date filter (NLP detected takes priority over UI)
        date_filter = parsed['date_filter']
        date_start, date_end = parsed['date_range']
        specific_date = parsed.get('specific_date')  # For display purposes
        logger.info(f"[SEARCH] ui_date='{ui_date}', NLP date_filter='{date_filter}', specific_date='{specific_date}'")
        
        if date_filter:
            # NLP detected a date filter - it takes priority
            # Check if it's a specific date (parsed by dateparser)
            if date_filter.startswith('specific_date:'):
                # Specific date - date_start and date_end already set by parser
                # Reset UI dropdown since this is a custom date
                self.date_filter.blockSignals(True)
                self.date_filter.setCurrentIndex(0)  # "Any Time"
                self.date_filter.blockSignals(False)
                logger.info(f"[SEARCH] Specific date: {specific_date}, range={date_start} to {date_end}")
            else:
                # Standard date filter (today, yesterday, etc.)
                # Update UI dropdown to reflect the detected filter
                ui_name = FILTER_TO_UI_DATE.get(date_filter)
                if ui_name:
                    idx = self.date_filter.findText(ui_name)
                    if idx >= 0:
                        self.date_filter.blockSignals(True)
                        self.date_filter.setCurrentIndex(idx)
                        self.date_filter.blockSignals(False)
                # Use the NLP-detected date range (recalculate for standard filters)
                date_start, date_end = get_date_range(date_filter)
                logger.info(f"[SEARCH] NLP standard: date_filter='{date_filter}', date_start={date_start}, date_end={date_end}")
        elif ui_date != "Any Time":
            # No NLP date filter - use UI dropdown selection
            date_filter = UI_DATE_MAPPING.get(ui_date)
            date_start, date_end = get_date_range(date_filter) if date_filter else (None, None)
            logger.info(f"[SEARCH] UI filter: date_filter='{date_filter}', date_start={date_start}, date_end={date_end}")
        
        # Update filter status label
        filter_parts = []
        if type_filter:
            filter_parts.append(f"Type: {type_filter}")
        if date_filter:
            # Show user-friendly date label
            if specific_date:
                filter_parts.append(f"Date: {specific_date}")
            else:
                filter_parts.append(f"Date: {date_filter}")
        if filter_parts:
            self.filter_status_label.setText(f"Active: {', '.join(filter_parts)}")
        else:
            self.filter_status_label.setText("")
        
        # Determine search query - use empty string for date-only searches
        # Note: clean_query can be empty string "" for date-only searches, which is valid
        search_query = clean_query  # Use clean_query directly (can be empty string)
        is_date_only_search = (search_query == "") and (date_start or date_end)
        
        logger.info(f"[SEARCH] search_query='{search_query}', is_date_only={is_date_only_search}")
        
        # Perform search with filters
        results = search_service.search_files(
            search_query,  # Pass empty string for date-only searches
            limit=100,
            type_filter=type_filter,
            date_start=date_start,
            date_end=date_end,
            extensions=extensions
        )
        self._last_search_results = results  # cache for editing
        
        # Show parsed query debug info if available
        dbg = getattr(search_service, 'last_debug_info', '')
        if dbg:
            self.search_debug_label.setText(dbg)
        else:
            self.search_debug_label.setText("")
        
        # Display results
        self.display_search_results(results)
        
        # Update status message
        if is_date_only_search:
            date_label = specific_date if specific_date else date_filter
            self.status_bar.showMessage(f"Found {len(results)} files from {date_label}")
        else:
            self.status_bar.showMessage(f"Found {len(results)} results for '{query}'")
    
    def _on_filter_changed(self):
        """Re-run search when filter dropdowns change."""
        if self.search_input.text().strip():
            self.search_files()
    
    def _clear_filters(self):
        """Clear all search filters and reset dropdowns."""
        self.type_filter.blockSignals(True)
        self.date_filter.blockSignals(True)
        self.type_filter.setCurrentIndex(0)  # "All Types"
        self.date_filter.setCurrentIndex(0)  # "Any Time"
        self.type_filter.blockSignals(False)
        self.date_filter.blockSignals(False)
        self.filter_status_label.setText("")
        
        # Re-run search if there's a query
        if self.search_input.text().strip():
            self.search_files()
    
    def _create_separator(self) -> QWidget:
        """Create a vertical separator for the Quick Actions bar."""
        from PySide6.QtWidgets import QFrame
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFrameShadow(QFrame.Sunken)
        sep.setStyleSheet("color: #555;")
        return sep
    
    def _get_selected_file_ids(self, source: str = 'search') -> List[int]:
        """Get file IDs for all CHECKED rows (checkbox) in the specified table."""
        if source == 'debug':
            table = self.debug_table
            data_cache = getattr(self, '_last_debug_files', [])
        else:
            table = self.search_results_table
            data_cache = getattr(self, '_last_search_results', [])
        
        file_ids = []
        for row in range(table.rowCount()):
            # Check if checkbox in column 0 is checked
            checkbox_item = table.item(row, 0)
            if checkbox_item and checkbox_item.checkState() == Qt.Checked:
                if row < len(data_cache):
                    file_id = data_cache[row].get('id')
                    if file_id:
                        file_ids.append(file_id)
        return file_ids
    
    def _get_selected_files(self, source: str = 'search') -> List[Dict[str, Any]]:
        """Get full file data for all CHECKED rows (checkbox) in the specified table."""
        if source == 'debug':
            table = self.debug_table
            data_cache = getattr(self, '_last_debug_files', [])
        else:
            table = self.search_results_table
            data_cache = getattr(self, '_last_search_results', [])
        
        files = []
        for row in range(table.rowCount()):
            # Check if checkbox in column 0 is checked
            checkbox_item = table.item(row, 0)
            if checkbox_item and checkbox_item.checkState() == Qt.Checked:
                if row < len(data_cache):
                    files.append(data_cache[row])
        return files
    
    def _on_selection_changed(self, item=None):
        """Update Quick Actions bar when checkbox changes in Search tab."""
        # Only react to checkbox column changes (column 0)
        if item is not None and item.column() != 0:
            return
        
        checked_count = 0
        for row in range(self.search_results_table.rowCount()):
            checkbox_item = self.search_results_table.item(row, 0)
            if checkbox_item and checkbox_item.checkState() == Qt.Checked:
                checked_count += 1
        
        if checked_count > 0:
            self.selection_count_label.setText(f"{checked_count} file{'s' if checked_count != 1 else ''} selected")
            self.quick_actions_widget.setVisible(True)
        else:
            self.quick_actions_widget.setVisible(False)
    
    def _on_debug_selection_changed(self, item=None):
        """Update Quick Actions bar when checkbox changes in Indexed Files tab."""
        # Only react to checkbox column changes (column 0)
        if item is not None and item.column() != 0:
            return
        
        checked_count = 0
        for row in range(self.debug_table.rowCount()):
            checkbox_item = self.debug_table.item(row, 0)
            if checkbox_item and checkbox_item.checkState() == Qt.Checked:
                checked_count += 1
        
        if checked_count > 0:
            self.debug_selection_count_label.setText(f"{checked_count} file{'s' if checked_count != 1 else ''} selected")
            self.debug_quick_actions_widget.setVisible(True)
        else:
            self.debug_quick_actions_widget.setVisible(False)
    
    def _action_select_all(self, source: str = 'search'):
        """Check all checkboxes in the specified table."""
        if source == 'debug':
            table = self.debug_table
        else:
            table = self.search_results_table
        
        table.blockSignals(True)
        for row in range(table.rowCount()):
            checkbox_item = table.item(row, 0)
            if checkbox_item:
                checkbox_item.setCheckState(Qt.Checked)
        table.blockSignals(False)
        
        # Manually trigger update
        if source == 'debug':
            self._on_debug_selection_changed()
        else:
            self._on_selection_changed()
    
    def _action_clear_selection(self, source: str = 'search'):
        """Uncheck all checkboxes in the specified table."""
        if source == 'debug':
            table = self.debug_table
        else:
            table = self.search_results_table
        
        table.blockSignals(True)
        for row in range(table.rowCount()):
            checkbox_item = table.item(row, 0)
            if checkbox_item:
                checkbox_item.setCheckState(Qt.Unchecked)
        table.blockSignals(False)
        
        # Manually trigger update
        if source == 'debug':
            self._on_debug_selection_changed()
        else:
            self._on_selection_changed()
    
    def _action_remove_from_index(self, source: str = 'search'):
        """Remove selected files from the index using background thread."""
        file_ids = self._get_selected_file_ids(source)
        if not file_ids:
            return
        
        # Confirm removal
        reply = QMessageBox.question(
            self,
            "Remove from Index",
            f"Remove {len(file_ids)} file(s) from the index?\n\nThe actual files will NOT be deleted from your PC.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Show progress dialog
        progress = QProgressDialog("Removing files from index...", "Cancel", 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        
        # Run in background thread
        self._batch_worker = BatchOperationWorker('remove', file_ids=file_ids)
        self._batch_worker.operation_completed.connect(
            lambda result: self._on_batch_operation_complete('remove', result, progress, source)
        )
        self._batch_worker.operation_error.connect(
            lambda err: self._on_batch_operation_error(err, progress)
        )
        self._batch_worker.start()
    
    def _action_reindex_selected(self, source: str = 'search'):
        """Re-index selected files using background thread."""
        files = self._get_selected_files(source)
        if not files:
            return
        
        file_paths = [f.get('file_path') for f in files if f.get('file_path')]
        if not file_paths:
            return
        
        reply = QMessageBox.question(
            self,
            "Re-index Files",
            f"Re-index {len(file_paths)} file(s)?\n\nThis will refresh their metadata and AI analysis.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Show progress dialog
        progress = QProgressDialog("Re-indexing files...", "Cancel", 0, len(file_paths), self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        
        # Run in background thread
        self._batch_worker = BatchOperationWorker('reindex', file_paths=file_paths)
        self._batch_worker.progress_updated.connect(
            lambda curr, total, msg: self._on_batch_progress(progress, curr, total, msg)
        )
        self._batch_worker.operation_completed.connect(
            lambda result: self._on_batch_operation_complete('reindex', result, progress, source)
        )
        self._batch_worker.operation_error.connect(
            lambda err: self._on_batch_operation_error(err, progress)
        )
        self._batch_worker.start()
    
    def _action_add_tags(self, source: str = 'search'):
        """Add tags to selected files using background thread."""
        file_ids = self._get_selected_file_ids(source)
        if not file_ids:
            return
        
        # Show input dialog for tags
        tags_text, ok = QInputDialog.getText(
            self,
            "Add Tags",
            f"Enter tags to add to {len(file_ids)} file(s):\n(separate multiple tags with commas)",
            QLineEdit.Normal,
            ""
        )
        
        if not ok or not tags_text.strip():
            return
        
        # Parse tags
        new_tags = [t.strip() for t in tags_text.split(',') if t.strip()]
        if not new_tags:
            return
        
        # Show progress dialog
        progress = QProgressDialog("Adding tags...", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        
        # Run in background thread
        self._batch_worker = BatchOperationWorker('add_tags', file_ids=file_ids, extra_data={'tags': new_tags})
        self._batch_worker.operation_completed.connect(
            lambda result: self._on_batch_operation_complete('add_tags', result, progress, source, extra={'tags': new_tags})
        )
        self._batch_worker.operation_error.connect(
            lambda err: self._on_batch_operation_error(err, progress)
        )
        self._batch_worker.start()
    
    def _on_batch_progress(self, progress: QProgressDialog, current: int, total: int, message: str):
        """Update progress dialog during batch operation."""
        progress.setMaximum(total)
        progress.setValue(current)
        progress.setLabelText(message)
        QApplication.processEvents()
    
    def _on_batch_operation_complete(self, operation: str, result: dict, progress: QProgressDialog, source: str, extra: dict = None):
        """Handle batch operation completion."""
        progress.close()
        
        if operation == 'remove':
            QMessageBox.information(
                self,
                "Remove Complete",
                f"Removed {result.get('removed', 0)} file(s) from index.\n"
                f"Errors: {result.get('errors', 0)}"
            )
        elif operation == 'reindex':
            QMessageBox.information(
                self,
                "Re-index Complete",
                f"Updated: {result.get('updated', 0)}\n"
                f"Not found: {result.get('not_found', 0)}\n"
                f"Errors: {result.get('errors', 0)}"
            )
        elif operation == 'add_tags':
            tags = extra.get('tags', []) if extra else []
            QMessageBox.information(
                self,
                "Tags Added",
                f"Added tags to {result.get('updated', 0)} file(s).\n"
                f"Tags: {', '.join(tags)}\n"
                f"Errors: {result.get('errors', 0)}"
            )
        
        # Refresh views
        if self.search_input.text().strip():
            self.search_files()
        self.refresh_debug_view()
    
    def _on_batch_operation_error(self, error: str, progress: QProgressDialog):
        """Handle batch operation error."""
        progress.close()
        QMessageBox.critical(self, "Operation Failed", f"An error occurred:\n{error}")
    
    def _action_copy_paths(self, source: str = 'search'):
        """Copy file paths of selected files to clipboard."""
        files = self._get_selected_files(source)
        if not files:
            return
        
        paths = [f.get('file_path', '') for f in files if f.get('file_path')]
        
        if paths:
            clipboard = QApplication.clipboard()
            clipboard.setText('\n'.join(paths))
            self.status_bar.showMessage(f"Copied {len(paths)} file path(s) to clipboard")
    
    def _action_open_folders(self, source: str = 'search'):
        """Open containing folders of selected files."""
        files = self._get_selected_files(source)
        if not files:
            return
        
        # Get unique folder paths
        folders = set()
        for f in files:
            file_path = f.get('file_path')
            if file_path:
                folder = str(Path(file_path).parent)
                folders.add(folder)
        
        if len(folders) > 5:
            reply = QMessageBox.question(
                self,
                "Open Folders",
                f"This will open {len(folders)} different folders.\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        
        for folder in folders:
            try:
                if os.path.exists(folder):
                    os.startfile(folder)
            except Exception as e:
                logger.error(f"Error opening folder {folder}: {e}")
        
        self.status_bar.showMessage(f"Opened {len(folders)} folder(s)")
    
    def _action_export_list(self, source: str = 'search'):
        """Export selected files to CSV or TXT."""
        files = self._get_selected_files(source)
        if not files:
            return
        
        # Show save dialog
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export File List",
            "",
            "CSV Files (*.csv);;Text Files (*.txt)"
        )
        
        if not file_path:
            return
        
        # Determine format
        file_format = 'csv' if file_path.endswith('.csv') or 'CSV' in selected_filter else 'txt'
        
        # Ensure correct extension
        if file_format == 'csv' and not file_path.endswith('.csv'):
            file_path += '.csv'
        elif file_format == 'txt' and not file_path.endswith('.txt'):
            file_path += '.txt'
        
        from app.core.file_operations import get_file_operations
        file_ops = get_file_operations()
        
        if file_ops.export_file_list(files, file_path, file_format):
            self.status_bar.showMessage(f"Exported {len(files)} files to {file_path}")
            
            # Ask to open file
            reply = QMessageBox.question(
                self,
                "Export Complete",
                f"Exported {len(files)} file(s) to:\n{file_path}\n\nOpen the file?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply == QMessageBox.Yes:
                try:
                    os.startfile(file_path)
                except Exception as e:
                    logger.error(f"Error opening export file: {e}")
        else:
            QMessageBox.warning(self, "Export Failed", "Failed to export file list.")
    
    def display_search_results(self, results: List[Dict[str, Any]]):
        """Display search results in the table."""
        self.search_results_table.setRowCount(len(results))
        
        for row, result in enumerate(results):
            file_id = result.get('id')
            
            # Checkbox column (col 0)
            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            checkbox_item.setCheckState(Qt.Unchecked)
            self.search_results_table.setItem(row, 0, checkbox_item)
            
            # File name (col 1)
            name_item = QTableWidgetItem(result['file_name'])
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 1, name_item)
            
            # Category (col 2)
            category_item = QTableWidgetItem(result['category'])
            category_item.setFlags(category_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 2, category_item)
            
            # Size (col 3)
            size_item = QTableWidgetItem(result.get('size_formatted', 'Unknown'))
            size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 3, size_item)
            
            # Relevance score (col 4)
            relevance = result.get('relevance_score', 0)
            relevance_item = QTableWidgetItem(f"{relevance:.2f}")
            relevance_item.setFlags(relevance_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 4, relevance_item)
            
            # Label (col 5)
            label_item = QTableWidgetItem(result.get('label', '') or '')
            label_item.setFlags(label_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 5, label_item)

            # Tags (col 6)
            tags_val = result.get('tags')
            if isinstance(tags_val, list):
                tags_text = ", ".join(tags_val)
            else:
                tags_text = tags_val or ''
            tags_item = QTableWidgetItem(tags_text)
            tags_item.setFlags(tags_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 6, tags_item)

            # Caption (col 7)
            caption_item = QTableWidgetItem(result.get('caption', '') or '')
            caption_item.setFlags((caption_item.flags() | Qt.ItemIsEditable))
            self.search_results_table.setItem(row, 7, caption_item)

            # OCR preview (col 8)
            ocr_preview = result.get('ocr_preview', '')
            if ocr_preview:
                ocr_item = QTableWidgetItem(ocr_preview)
            else:
                ocr_item = QTableWidgetItem("No OCR text")
            ocr_item.setFlags(ocr_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 8, ocr_item)

            # AI Source (col 9)
            ai_source_item = QTableWidgetItem(result.get('ai_source', '') or '')
            ai_source_item.setFlags(ai_source_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 9, ai_source_item)

            # Vision score (col 10)
            vscore = result.get('vision_confidence', None)
            try:
                vscore_text = f"{float(vscore):.2f}" if vscore is not None else ''
            except Exception:
                vscore_text = ''
            vscore_item = QTableWidgetItem(vscore_text)
            vscore_item.setFlags(vscore_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 10, vscore_item)

            # Purpose & Suggested filename from metadata (col 11, 12)
            meta = result.get('metadata') or {}
            purpose_text = meta.get('purpose') or ''
            sfile_text = meta.get('suggested_filename') or ''
            purpose_item = QTableWidgetItem(purpose_text)
            purpose_item.setFlags((purpose_item.flags() | Qt.ItemIsEditable))
            self.search_results_table.setItem(row, 11, purpose_item)
            sfile_item = QTableWidgetItem(sfile_text)
            sfile_item.setFlags((sfile_item.flags() | Qt.ItemIsEditable))
            self.search_results_table.setItem(row, 12, sfile_item)

            # Path (col 13)
            path_text = result.get('file_path', '') or ''
            path_item = QTableWidgetItem(path_text)
            path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable)
            self.search_results_table.setItem(row, 13, path_item)

            # Actions (Copy, Open) (col 14)
            actions_widget = QWidget()
            actions_layout = QHBoxLayout(actions_widget)
            actions_layout.setContentsMargins(4, 4, 4, 4)
            actions_layout.setSpacing(8)
            btn_copy = QPushButton("Copy Path")
            btn_open = QPushButton("Open File")
            btn_copy.setToolTip("Copy file path to clipboard")
            btn_open.setToolTip("Open file with default app")
            actions_layout.addWidget(btn_copy)
            actions_layout.addWidget(btn_open)
            actions_layout.addStretch()
            self.search_results_table.setCellWidget(row, 14, actions_widget)

            # Connect actions
            file_path_for_row = path_text
            btn_copy.clicked.connect(lambda _, p=file_path_for_row: self.copy_path_to_clipboard(p))
            btn_open.clicked.connect(lambda _, p=file_path_for_row: self.open_file_in_os(p))

        # Hook up edit commits
        self.search_results_table.itemChanged.connect(self.on_search_cell_changed)

    def on_search_cell_changed(self, item: QTableWidgetItem) -> None:
        try:
            row = item.row()
            col = item.column()
            if not hasattr(self, '_last_search_results'):
                return
            if row >= len(self._last_search_results):
                return
            rec = self._last_search_results[row]
            file_id = rec.get('id')
            if not file_id:
                return
            # Determine which field is being edited
            new_val = item.text()
            if col == 6:  # Caption
                ok = file_index.update_file_field(file_id, 'caption', new_val)
            elif col == 10:  # Purpose (metadata)
                meta = rec.get('metadata') or {}
                meta['purpose'] = new_val
                ok = file_index.update_file_field(file_id, 'metadata', meta)
            elif col == 11:  # Suggested filename (metadata)
                meta = rec.get('metadata') or {}
                meta['suggested_filename'] = new_val
                ok = file_index.update_file_field(file_id, 'metadata', meta)
            elif col == 4:  # Label
                ok = file_index.update_file_field(file_id, 'label', new_val)
            elif col == 5:  # Tags (comma-separated)
                tags = [t.strip() for t in (new_val or '').split(',') if t.strip()]
                ok = file_index.update_file_field(file_id, 'tags', tags)
            else:
                return
            if ok:
                self.status_bar.showMessage("Saved edit")
                # refresh our cache minimally
                rec['caption'] = new_val if col == 6 else rec.get('caption')
                if col in (10, 11):
                    rec.setdefault('metadata', {})
                    if col == 10:
                        rec['metadata']['purpose'] = new_val
                    else:
                        rec['metadata']['suggested_filename'] = new_val
                if col == 4:
                    rec['label'] = new_val
                if col == 5:
                    rec['tags'] = tags
            else:
                QMessageBox.critical(self, "Save Error", "Failed to save your edit.")
        except Exception as e:
            QMessageBox.critical(self, "Edit Error", f"Failed to apply edit:\n{e}")

    def copy_path_to_clipboard(self, file_path: str) -> None:
        try:
            cb = QApplication.clipboard()
            cb.setText(file_path or "")
            self.status_bar.showMessage("Copied path to clipboard")
        except Exception as e:
            QMessageBox.critical(self, "Copy Error", f"Failed to copy path:\n{e}")

    def open_file_in_os(self, file_path: str) -> None:
        try:
            if not file_path:
                return
            # Prefer Qt for cross-platform support
            url = QUrl.fromLocalFile(file_path)
            if QDesktopServices.openUrl(url):
                return
            # Fallbacks
            if os.name == 'nt':
                os.startfile(file_path)  # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', file_path])
            else:
                subprocess.Popen(['xdg-open', file_path])
        except Exception as e:
            QMessageBox.critical(self, "Open Error", f"Failed to open file:\n{e}")
    
    # ==================== DRAG AND DROP ====================
    
    def dragEnterEvent(self, event):
        """Handle drag enter - show visual feedback."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            # Update drop zone styling to show it's active
            self.drop_zone.setStyleSheet("""
                QLabel {
                    border: 3px solid #00E5FF;
                    border-radius: 8px;
                    background-color: rgba(0, 229, 255, 0.15);
                    color: #00E5FF;
                    font-size: 14px;
                    font-weight: bold;
                    padding: 10px;
                }
            """)
            # Count files/folders being dragged
            urls = event.mimeData().urls()
            count = len(urls)
            self.drop_zone.setText(f"📥 Drop to index {count} item{'s' if count > 1 else ''}")
        else:
            event.ignore()
    
    def dragLeaveEvent(self, event):
        """Handle drag leave - restore normal styling."""
        self._reset_drop_zone_style()
        event.accept()
    
    def dropEvent(self, event):
        """Handle file/folder drop - start indexing."""
        self._reset_drop_zone_style()
        
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            urls = event.mimeData().urls()
            paths = []
            
            for url in urls:
                if url.isLocalFile():
                    path = url.toLocalFile()
                    paths.append(path)
            
            if paths:
                self._handle_dropped_paths(paths)
        else:
            event.ignore()
    
    def _reset_drop_zone_style(self):
        """Reset drop zone to default styling."""
        self.drop_zone.setStyleSheet("""
            QLabel {
                border: 2px dashed #00B8D4;
                border-radius: 8px;
                background-color: rgba(0, 184, 212, 0.05);
                color: #00B8D4;
                font-size: 13px;
                padding: 10px;
            }
        """)
        self.drop_zone.setText("📁 Drag & drop files or folders here to index them")
    
    def _handle_dropped_paths(self, paths: list):
        """Handle dropped file/folder paths and start indexing."""
        # Collect all files to index
        files_to_index = []
        folders_to_index = []
        
        for path_str in paths:
            path = Path(path_str)
            if path.is_dir():
                folders_to_index.append(path)
            elif path.is_file():
                files_to_index.append(path)
        
        # Show confirmation
        msg_parts = []
        if folders_to_index:
            msg_parts.append(f"{len(folders_to_index)} folder{'s' if len(folders_to_index) > 1 else ''}")
        if files_to_index:
            msg_parts.append(f"{len(files_to_index)} file{'s' if len(files_to_index) > 1 else ''}")
        
        msg = f"Index {' and '.join(msg_parts)}?"
        
        reply = QMessageBox.question(
            self,
            "Index Dropped Items",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Index folders
        if folders_to_index:
            if self.is_indexing:
                # Add all to queue
                for folder in folders_to_index:
                    self._add_to_index_queue(folder)
            else:
                # Start first, queue the rest
                first = folders_to_index[0]
                for folder in folders_to_index[1:]:
                    self._add_to_index_queue(folder)
                self.index_path = first
                self.index_label.setText(f"Index folder: {first}")
                self.index_button_action.setEnabled(True)
                self._start_indexing_path(first)
        
        # Index individual files
        if files_to_index and not folders_to_index:
            self._index_individual_files(files_to_index)
    
    def _index_individual_files(self, files: list):
        """Index individual dropped files."""
        from app.core.search import search_service
        
        total = len(files)
        self.drop_zone.setVisible(False)  # Hide drop zone during indexing
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, total)
        self.index_percent_label.setVisible(True)
        self.index_progress_label.setVisible(True)
        
        indexed = 0
        for file_path in files:
            try:
                result = search_service.index_single_file(file_path)
                if not result.get('error'):
                    indexed += 1
                percent = int((indexed / total) * 100)
                self.progress_bar.setValue(indexed)
                self.index_percent_label.setText(f"{percent}%")
                self.index_progress_label.setText(f"Indexing file {indexed} of {total}")
                QApplication.processEvents()  # Keep UI responsive
            except Exception as e:
                logger.error(f"Error indexing {file_path}: {e}")
        
        # Done
        self.progress_bar.setVisible(False)
        self.index_percent_label.setVisible(False)
        self.index_progress_label.setVisible(False)
        self.drop_zone.setVisible(True)  # Show drop zone again
        
        QMessageBox.information(
            self,
            "Indexing Complete",
            f"Successfully indexed {indexed} of {total} files."
        )
        
        # Refresh views
        self.refresh_debug_view()
        stats = search_service.get_index_statistics()
        self.update_search_statistics(stats)
        self.search_button.setEnabled(True)
    
    # ==================== END DRAG AND DROP ====================
    
    def update_search_statistics(self, stats: Dict[str, Any]):
        """Update search statistics display."""
        if not stats:
            self.search_stats_label.setText("No files indexed yet")
            return
        
        total_files = stats.get('total_files', 0)
        files_with_ocr = stats.get('files_with_ocr', 0)
        total_size_mb = stats.get('total_size_mb', 0)
        
        stats_text = f"Indexed: {total_files} files ({files_with_ocr} with OCR) - {total_size_mb} MB"
        self.search_stats_label.setText(stats_text)

    # Debug functionality methods
    def refresh_debug_view(self):
        """Refresh the debug view with current database contents."""
        # Skip if debug table doesn't exist (hidden in MVP mode)
        if not hasattr(self, 'debug_table'):
            return
            
        try:
            # Get all files from database using a direct query
            import sqlite3
            from app.core.database import file_index
            
            with sqlite3.connect(file_index.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM files ORDER BY file_name")
                rows = cursor.fetchall()
            
            # Store file data for Quick Actions
            self._last_debug_files = []
            
            # Update debug table
            self._populating_debug_table = True
            self.debug_table.blockSignals(True)
            self.debug_table.setRowCount(len(rows))
            
            for row_idx, row in enumerate(rows):
                # Build file dict for Quick Actions
                try:
                    meta = json.loads(row["metadata"]) if row["metadata"] else {}
                except Exception:
                    meta = {}
                try:
                    tags_list = json.loads(row["tags"]) if row["tags"] else []
                except Exception:
                    tags_list = []
                
                file_dict = {
                    'id': row["id"],
                    'file_path': row["file_path"],
                    'file_name': row["file_name"],
                    'category': row["category"],
                    'file_size': row["file_size"],
                    'label': row["label"] if "label" in row.keys() else None,
                    'tags': tags_list,
                    'caption': row["caption"] if "caption" in row.keys() else None,
                    'metadata': meta,
                }
                self._last_debug_files.append(file_dict)
                
                # Checkbox column (col 0)
                checkbox_item = QTableWidgetItem()
                checkbox_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                checkbox_item.setCheckState(Qt.Unchecked)
                self.debug_table.setItem(row_idx, 0, checkbox_item)
                
                # File name (col 1)
                name_item = QTableWidgetItem(row["file_name"])
                name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
                try:
                    name_item.setData(Qt.UserRole, row["id"])
                except Exception:
                    pass
                self.debug_table.setItem(row_idx, 1, name_item)
                
                # Category (col 2)
                category_item = QTableWidgetItem(row["category"])
                category_item.setFlags(category_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 2, category_item)
                
                # Size (col 3)
                size_bytes = row["file_size"]
                size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else 0
                size_item = QTableWidgetItem(f"{size_mb} MB")
                size_item.setFlags(size_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 3, size_item)
                
                # Has OCR (col 4)
                has_ocr = bool(row["has_ocr"])
                ocr_item = QTableWidgetItem("Yes" if has_ocr else "No")
                ocr_item.setFlags(ocr_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 4, ocr_item)

                # Label (col 5)
                label = row["label"] if "label" in row.keys() else None
                label_item = QTableWidgetItem(label or '')
                label_item.setFlags((label_item.flags() | Qt.ItemIsEditable))
                self.debug_table.setItem(row_idx, 5, label_item)

                # Tags (col 6)
                tags_raw = row["tags"] if "tags" in row.keys() else None
                try:
                    tags_list = json.loads(tags_raw) if tags_raw else []
                    tags_text = ", ".join(tags_list)
                except Exception:
                    tags_text = tags_raw or ''
                tags_item = QTableWidgetItem(tags_text)
                tags_item.setFlags((tags_item.flags() | Qt.ItemIsEditable))
                self.debug_table.setItem(row_idx, 6, tags_item)

                # Caption (col 7)
                caption = row["caption"] if "caption" in row.keys() else None
                caption_item = QTableWidgetItem(caption or '')
                caption_item.setFlags((caption_item.flags() | Qt.ItemIsEditable))
                self.debug_table.setItem(row_idx, 7, caption_item)
                
                # OCR text preview (col 8)
                ocr_text = row["ocr_text"] or ""
                if ocr_text:
                    preview = ocr_text[:100] + "..." if len(ocr_text) > 100 else ocr_text
                else:
                    preview = "No OCR text"
                ocr_preview_item = QTableWidgetItem(preview)
                ocr_preview_item.setFlags(ocr_preview_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 8, ocr_preview_item)

                # AI source (col 9)
                ai_source = row["ai_source"] if "ai_source" in row.keys() else None
                ai_source_item = QTableWidgetItem(ai_source or '')
                ai_source_item.setFlags(ai_source_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 9, ai_source_item)

                # Vision score (col 10)
                try:
                    vscore = float(row["vision_confidence"]) if row["vision_confidence"] is not None else None
                except Exception:
                    vscore = None
                vscore_item = QTableWidgetItem(f"{vscore:.2f}" if vscore is not None else '')
                vscore_item.setFlags(vscore_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 10, vscore_item)

                # Purpose (col 11)
                purpose_item = QTableWidgetItem((meta.get('purpose') or ''))
                purpose_item.setFlags((purpose_item.flags() | Qt.ItemIsEditable))
                self.debug_table.setItem(row_idx, 11, purpose_item)

                # Suggested filename (col 12)
                sfile_item = QTableWidgetItem((meta.get('suggested_filename') or ''))
                sfile_item.setFlags((sfile_item.flags() | Qt.ItemIsEditable))
                self.debug_table.setItem(row_idx, 12, sfile_item)

                # Detected text (col 13)
                dtxt = meta.get('detected_text') or ''
                if dtxt:
                    dtxt_preview = dtxt[:100] + "..." if len(dtxt) > 100 else dtxt
                else:
                    dtxt_preview = ''
                dtxt_item = QTableWidgetItem(dtxt_preview)
                dtxt_item.setFlags(dtxt_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 13, dtxt_item)
                
                # File path (col 14)
                file_path_val = row["file_path"] or ""
                path_item = QTableWidgetItem(file_path_val)
                path_item.setFlags(path_item.flags() & ~Qt.ItemIsEditable)
                self.debug_table.setItem(row_idx, 14, path_item)
                
                # Actions (col 15)
                actions_widget = QWidget()
                actions_layout = QHBoxLayout(actions_widget)
                actions_layout.setContentsMargins(2, 0, 2, 0)  # Minimal margins
                actions_layout.setSpacing(6)
                actions_layout.setAlignment(Qt.AlignVCenter)
                
                btn_style = """
                    QPushButton {
                        background-color: #00B8D4;
                        color: white;
                        font-size: 11px;
                        font-weight: bold;
                        border: none;
                        border-radius: 4px;
                        padding: 2px 6px;
                        min-height: 24px;
                        max-height: 24px;
                    }
                    QPushButton:hover {
                        background-color: #00ACC1;
                    }
                """
                
                btn_copy = QPushButton("Copy")
                btn_open = QPushButton("Open")
                btn_copy.setFixedHeight(24)
                btn_open.setFixedHeight(24)
                btn_copy.setMinimumWidth(48)
                btn_open.setMinimumWidth(48)
                btn_copy.setStyleSheet(btn_style)
                btn_open.setStyleSheet(btn_style)
                btn_copy.setToolTip("Copy file path to clipboard")
                btn_open.setToolTip("Open file with default app")
                actions_layout.addWidget(btn_copy)
                actions_layout.addWidget(btn_open)
                self.debug_table.setCellWidget(row_idx, 15, actions_widget)
                
                # Set row height to fit buttons properly
                self.debug_table.setRowHeight(row_idx, 48)
                
                # Connect actions
                btn_copy.clicked.connect(lambda _, p=file_path_val: self.copy_path_to_clipboard(p))
                btn_open.clicked.connect(lambda _, p=file_path_val: self.open_file_in_os(p))
            
            self.debug_table.blockSignals(False)
            self._populating_debug_table = False
            self.debug_info_label.setText(f"Showing {len(rows)} indexed files")
            self.status_bar.showMessage(f"Debug view refreshed - {len(rows)} files shown")
            
        except Exception as e:
            QMessageBox.critical(self, "Debug Error", f"Error refreshing debug view:\n{e}")
            self.debug_info_label.setText("Error loading debug data")
    
    def clear_index(self):
        """Clear the search index."""
        reply = QMessageBox.question(
            self, "Clear Index",
            "Are you sure you want to clear the entire search index?\n\n"
            "This will remove all indexed files and OCR data.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            try:
                search_service.index.clear_index()
                self.refresh_debug_view()
                self.update_search_statistics({})
                self.search_button.setEnabled(False)
                self.status_bar.showMessage("Search index cleared")
            except Exception as e:
                QMessageBox.critical(self, "Clear Error", f"Error clearing index:\n{e}")

    def dump_active_dialog_tree(self) -> None:
        """Debug helper: dump the active window's controls (UIA and win32) to logs."""
        try:
            from pywinauto import Desktop
            logger.info("[QS] --- Dumping active dialog tree (UIA) ---")
            try:
                win = Desktop(backend='uia').get_active()
                if win:
                    logger.info("[QS] UIA Active: '%s' class='%s'", win.window_text(), getattr(win.element_info, 'class_name', '?'))
                    # Dump buttons and edits
                    for btn in win.descendants(control_type='Button')[:50]:
                        try:
                            r = btn.rectangle();
                            logger.info("[QS] UIA Button name='%s' id='%s' rect=%s", btn.window_text(), getattr(btn.element_info, 'automation_id', ''), (r.left, r.top, r.right, r.bottom))
                        except Exception:
                            pass
                    for ed in win.descendants(control_type='Edit')[:50]:
                        try:
                            r = ed.rectangle();
                            logger.info("[QS] UIA Edit name='%s' id='%s' rect=%s", ed.window_text(), getattr(ed.element_info, 'automation_id', ''), (r.left, r.top, r.right, r.bottom))
                        except Exception:
                            pass
                else:
                    logger.info("[QS] UIA: no active window")
            except Exception:
                logger.info("[QS] UIA dump failed", exc_info=True)
            logger.info("[QS] --- Dumping active dialog tree (win32) ---")
            try:
                winw = Desktop(backend='win32').get_active()
                if winw:
                    logger.info("[QS] win32 Active: '%s' class='%s'", winw.window_text(), getattr(winw.element_info, 'class_name', '?'))
                    for btn in winw.descendants(class_name='Button')[:50]:
                        try:
                            r = btn.rectangle(); logger.info("[QS] win32 Button name='%s' rect=%s", btn.window_text(), (r.left, r.top, r.right, r.bottom))
                        except Exception:
                            pass
                    for ed in winw.descendants(class_name='Edit')[:50]:
                        try:
                            r = ed.rectangle(); logger.info("[QS] win32 Edit name='%s' rect=%s", ed.window_text(), (r.left, r.top, r.right, r.bottom))
                        except Exception:
                            pass
                else:
                    logger.info("[QS] win32: no active window")
            except Exception:
                logger.info("[QS] win32 dump failed", exc_info=True)
        except Exception:
            logger.info("[QS] dump_active_dialog_tree outer failed", exc_info=True)
    
    def debug_comprehensive_state(self) -> None:
        """Phase 4: Debug helper for comprehensive system state logging."""
        try:
            from app.ui.win_hotkey import log_system_state
            
            logger.info("[QS] === MANUAL DEBUG TRIGGER (Ctrl+Alt+S) ===")
            
            # Log comprehensive system state
            log_system_state(logger, "[QS]")
            
            # If quick search overlay has saved state, log that too
            overlay = getattr(self, 'quick_overlay', None)
            if overlay and overlay.has_valid_saved_state():
                logger.info("[QS] Quick Search Overlay has saved state:")
                overlay.log_debug_target_window()
            else:
                logger.info("[QS] No saved state in Quick Search Overlay")
            
            # Log current autofill settings
            from app.core.settings import settings
            logger.info(f"[QS] Auto-paste: {settings.quick_search_autopaste}")
            logger.info(f"[QS] Auto-confirm: {settings.quick_search_auto_confirm}")
            logger.info(f"[QS] Shortcut: {settings.quick_search_shortcut}")
            
            logger.info("[QS] === END MANUAL DEBUG ===")
            self.status_bar.showMessage("Debug state logged - check console/logs")
            
        except Exception as e:
            logger.error(f"[QS] Error in debug_comprehensive_state: {e}")
            self.status_bar.showMessage("Debug logging failed")

