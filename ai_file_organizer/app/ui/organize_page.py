"""
AI-powered file organization page.

Flow:
1. User enters natural language instruction
2. App sends instruction + file metadata to LLM
3. LLM returns organization plan (folders + file assignments)
4. App validates the plan
5. User previews and approves
6. App executes moves deterministically
"""

import os
import sqlite3
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QProgressBar, QMessageBox, QFileDialog, QGroupBox,
    QSplitter, QFrame, QSizePolicy, QScrollArea,
    QDialog, QListWidget, QListWidgetItem, QCheckBox,
    QSpacerItem
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer

from app.core.settings import settings

from app.core.database import file_index
from app.core.ai_organizer import (
    request_organization_plan, validate_plan, plan_to_moves, get_plan_summary,
    deduplicate_plan, ensure_all_files_included
)
from app.core.apply import apply_moves

logger = logging.getLogger(__name__)


class PlanWorker(QThread):
    """Background worker for LLM planning - keeps UI responsive."""
    finished = Signal(object)  # plan dict or None
    error = Signal(str)
    
    def __init__(self, instruction: str, files: list):
        super().__init__()
        self.instruction = instruction
        self.files = files
    
    def run(self):
        try:
            plan = request_organization_plan(self.instruction, self.files)
            self.finished.emit(plan)
        except Exception as e:
            logger.error(f"Plan worker error: {e}")
            self.error.emit(str(e))


class RefineWorker(QThread):
    """Background worker for plan refinement."""
    finished = Signal(object)
    error = Signal(str)
    
    def __init__(self, original_instruction: str, current_plan: dict, feedback: str, files: list):
        super().__init__()
        self.original_instruction = original_instruction
        self.current_plan = current_plan
        self.feedback = feedback
        self.files = files
    
    def run(self):
        try:
            from app.core.ai_organizer import request_plan_refinement
            plan = request_plan_refinement(
                self.original_instruction,
                self.current_plan,
                self.feedback,
                self.files
            )
            self.finished.emit(plan)
        except Exception as e:
            logger.error(f"Refine worker error: {e}")
            self.error.emit(str(e))


class VoiceRecordWorker(QThread):
    """Background worker for voice recording and transcription."""
    finished = Signal(str)  # transcribed text
    error = Signal(str)
    recording_stopped = Signal()  # emitted when recording stops
    
    def __init__(self, duration: int = 30, sample_rate: int = 16000):
        super().__init__()
        self.duration = duration
        self.sample_rate = sample_rate
        self.is_recording = False
        self.audio_data = []
    
    def run(self):
        try:
            import sounddevice as sd
            import numpy as np
            from scipy.io import wavfile
            import tempfile
            import os
            from openai import OpenAI
            from app.core.settings import settings
            
            self.is_recording = True
            self.audio_data = []
            
            def audio_callback(indata, frames, time, status):
                if self.is_recording:
                    self.audio_data.append(indata.copy())
            
            # Start recording
            with sd.InputStream(samplerate=self.sample_rate, channels=1, 
                              dtype='int16', callback=audio_callback):
                while self.is_recording:
                    sd.sleep(100)  # Check every 100ms
            
            self.recording_stopped.emit()
            
            if not self.audio_data:
                self.error.emit("No audio recorded")
                return
            
            # Combine audio chunks
            audio = np.concatenate(self.audio_data, axis=0)
            
            # Save to temporary WAV file
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
                temp_path = f.name
                wavfile.write(temp_path, self.sample_rate, audio)
            
            try:
                # Transcribe with OpenAI Whisper
                client = OpenAI(api_key=settings.openai_api_key)
                
                with open(temp_path, 'rb') as audio_file:
                    transcription = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=audio_file,
                        language="en"
                    )
                
                self.finished.emit(transcription.text)
            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except:
                    pass
                    
        except ImportError as e:
            self.error.emit(f"Missing audio library: {e}\nRun: pip install sounddevice scipy")
        except Exception as e:
            logger.error(f"Voice recording error: {e}")
            self.error.emit(str(e))
    
    def stop_recording(self):
        """Stop the recording."""
        self.is_recording = False


class IndexBeforeOrganizeWorker(QThread):
    """Background worker for indexing files before organizing."""
    progress = Signal(int, int, str)  # current, total, message
    finished = Signal(dict)  # stats dict
    error = Signal(str)
    
    def __init__(self, folder_path: Path):
        super().__init__()
        self.folder_path = folder_path
    
    def run(self):
        try:
            from app.core.search import SearchService
            
            search_service = SearchService()
            
            def progress_callback(current, total, message):
                self.progress.emit(current, total, message)
            
            stats = search_service.index_directory(
                self.folder_path,
                recursive=False,  # Only top-level files
                progress_cb=progress_callback
            )
            
            self.finished.emit(stats)
            
        except Exception as e:
            logger.error(f"Index before organize error: {e}")
            self.error.emit(str(e))


class EmptyFolderDialog(QDialog):
    """Dialog to let user choose which empty folders to delete."""
    
    def __init__(self, empty_folders: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Empty Folders Found")
        self.setMinimumWidth(500)
        self.setMinimumHeight(350)
        
        self.empty_folders = empty_folders
        self.folders_to_delete = []
        
        layout = QVBoxLayout(self)
        
        # Header
        header = QLabel(
            "The following folders are now empty after organization.\n"
            "Select which ones you want to delete:"
        )
        header.setWordWrap(True)
        layout.addWidget(header)
        
        # Folder list with checkboxes
        self.folder_list = QListWidget()
        self.folder_list.setAlternatingRowColors(True)
        
        for folder_path in empty_folders:
            item = QListWidgetItem()
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)  # Default to checked
            item.setText(folder_path)
            item.setData(Qt.UserRole, folder_path)
            self.folder_list.addItem(item)
        
        layout.addWidget(self.folder_list)
        
        # Selection buttons
        selection_layout = QHBoxLayout()
        
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self._select_all)
        selection_layout.addWidget(select_all_btn)
        
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.clicked.connect(self._deselect_all)
        selection_layout.addWidget(deselect_all_btn)
        
        selection_layout.addStretch()
        layout.addLayout(selection_layout)
        
        # Action buttons
        button_layout = QHBoxLayout()
        
        delete_selected_btn = QPushButton("Delete Selected")
        delete_selected_btn.setStyleSheet("background-color: #d9534f; color: white;")
        delete_selected_btn.clicked.connect(self._delete_selected)
        button_layout.addWidget(delete_selected_btn)
        
        delete_all_btn = QPushButton("Delete All")
        delete_all_btn.setStyleSheet("background-color: #c9302c; color: white;")
        delete_all_btn.clicked.connect(self._delete_all)
        button_layout.addWidget(delete_all_btn)
        
        keep_all_btn = QPushButton("Keep All")
        keep_all_btn.clicked.connect(self.reject)
        button_layout.addWidget(keep_all_btn)
        
        layout.addLayout(button_layout)
    
    def _select_all(self):
        for i in range(self.folder_list.count()):
            self.folder_list.item(i).setCheckState(Qt.Checked)
    
    def _deselect_all(self):
        for i in range(self.folder_list.count()):
            self.folder_list.item(i).setCheckState(Qt.Unchecked)
    
    def _delete_selected(self):
        self.folders_to_delete = []
        for i in range(self.folder_list.count()):
            item = self.folder_list.item(i)
            if item.checkState() == Qt.Checked:
                self.folders_to_delete.append(item.data(Qt.UserRole))
        self.accept()
    
    def _delete_all(self):
        self.folders_to_delete = [item.data(Qt.UserRole) for i in range(self.folder_list.count())
                                   for item in [self.folder_list.item(i)]]
        self.accept()
    
    def get_folders_to_delete(self) -> list:
        return self.folders_to_delete


class WatchConfigDialog(QDialog):
    """
    Dialog for configuring Watch & Auto-Organize folders with per-folder instructions.
    Modern redesign to match the app's purple-bluish brand theme.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Watch & Auto-Organize")
        self.setMinimumWidth(650)
        self.setMinimumHeight(550)
        
        # Set light theme for this dialog specifically as requested
        self.setStyleSheet("""
            QDialog {
                background-color: #FFFFFF;
                color: #1A1A1A;
            }
            QLabel {
                color: #1A1A1A;
            }
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollBar:vertical {
                background: #F0F0F0;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #CCCCCC;
                border-radius: 4px;
            }
            QLineEdit {
                background-color: #FAFAFA;
                border: 1px solid #E0E0E0;
                border-radius: 8px;
                padding: 8px 12px;
                color: #1A1A1A;
            }
            QLineEdit:focus {
                border: 1px solid #7C4DFF;
                background-color: #FFFFFF;
            }
        """)
        
        # Track folder data: {path: instruction}
        self.folder_data: Dict[str, str] = {}
        # Track folder widgets for updates
        self.folder_widgets: Dict[str, Dict] = {}
        
        self._setup_ui()
        self._load_from_settings()
    
    def _setup_ui(self):
        """Setup the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)
        
        # Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(6)
        
        header = QLabel("Watch & Auto-Organize Configuration")
        header.setStyleSheet("font-size: 20px; font-weight: 700; color: #1A1A1A;")
        header_layout.addWidget(header)
        
        subtitle = QLabel(
            "Add folders to watch for new files. Each folder can have its own organization instructions."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #666666; font-size: 14px;")
        header_layout.addWidget(subtitle)
        
        layout.addLayout(header_layout)
        
        # Action Bar (Add Folder)
        action_row = QHBoxLayout()
        
        self.add_folder_btn = QPushButton("+ Add Folder")
        self.add_folder_btn.setMinimumHeight(40)
        self.add_folder_btn.setCursor(Qt.PointingHandCursor)
        self.add_folder_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #7C4DFF;
                border: 2px solid #7C4DFF;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
                padding: 0 20px;
            }
            QPushButton:hover {
                background-color: rgba(124, 77, 255, 0.05);
            }
            QPushButton:pressed {
                background-color: rgba(124, 77, 255, 0.1);
            }
        """)
        self.add_folder_btn.clicked.connect(self._add_folder)
        action_row.addWidget(self.add_folder_btn)
        action_row.addStretch()
        
        layout.addLayout(action_row)
        
        # Scroll area for folder list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        
        self.folders_container = QWidget()
        self.folders_container.setStyleSheet("background-color: transparent;")
        self.folders_layout = QVBoxLayout(self.folders_container)
        self.folders_layout.setContentsMargins(0, 0, 5, 0)
        self.folders_layout.setSpacing(12)
        
        # Placeholder for when no folders
        self.no_folders_label = QLabel("No folders configured.\nClick '+ Add Folder' to start watching.")
        self.no_folders_label.setStyleSheet("""
            color: #999999;
            font-size: 14px;
            padding: 40px;
            background: #F8F9FA;
            border-radius: 12px;
            border: 2px dashed #E0E0E0;
        """)
        self.no_folders_label.setAlignment(Qt.AlignCenter)
        self.folders_layout.addWidget(self.no_folders_label)
        
        self.folders_layout.addStretch()
        
        scroll.setWidget(self.folders_container)
        layout.addWidget(scroll, 1)
        
        # Separator line
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setStyleSheet("background-color: #EEEEEE; border: none; max-height: 1px;")
        layout.addWidget(line)
        
        # Bottom Action buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        button_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(44)
        cancel_btn.setMinimumWidth(100)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #666666;
                border: none;
                font-weight: 500;
                font-size: 14px;
            }
            QPushButton:hover {
                color: #1A1A1A;
                background-color: #F5F5F5;
                border-radius: 8px;
            }
        """)
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("Save Changes")
        save_btn.setMinimumHeight(44)
        save_btn.setMinimumWidth(140)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C4DFF, stop:1 #9575FF);
                color: white;
                border: none;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9575FF, stop:1 #B39DFF);
            }
            QPushButton:pressed {
                background: #6A3DE8;
            }
        """)
        save_btn.clicked.connect(self._save_and_close)
        button_layout.addWidget(save_btn)
        
        layout.addLayout(button_layout)
    
    def _load_from_settings(self):
        """Load saved folders from settings."""
        for folder_info in settings.auto_organize_folders:
            path = folder_info.get('path', '')
            instruction = folder_info.get('instruction', '')
            if path and os.path.isdir(path):
                self._create_folder_widget(path, instruction)
        
        self._update_no_folders_visibility()
    
    def _add_folder(self):
        """Add a new folder via file dialog."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select Folder to Watch", str(Path.home())
        )
        if folder:
            # Normalize path
            folder = os.path.normpath(folder)
            
            if folder in self.folder_data:
                QMessageBox.information(
                    self, "Already Added",
                    "This folder is already in the watch list."
                )
                return
            
            self._create_folder_widget(folder, '')
            self._update_no_folders_visibility()
    
    def _create_folder_widget(self, folder_path: str, instruction: str):
        """Create a widget card for a folder."""
        folder_path = os.path.normpath(folder_path)
        
        # Store in data
        self.folder_data[folder_path] = instruction
        
        # Create card frame
        frame = QFrame()
        frame.setStyleSheet("""
            QFrame {
                background-color: #F8F9FA;
                border: 1px solid #E0E0E0;
                border-radius: 12px;
            }
        """)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setSpacing(12)
        frame_layout.setContentsMargins(16, 16, 16, 16)
        
        # Header row with path and remove button
        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        
        folder_icon = QLabel("📂")
        folder_icon.setStyleSheet("font-size: 18px; border: none; background: transparent;")
        header_row.addWidget(folder_icon)
        
        path_label = QLabel(folder_path)
        path_label.setStyleSheet("font-weight: 600; font-size: 13px; color: #333333; border: none; background: transparent;")
        path_label.setWordWrap(True)
        header_row.addWidget(path_label, 1)
        
        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(28, 28)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setToolTip("Remove folder")
        remove_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #999999;
                border: none;
                border-radius: 14px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #FFEBEE;
                color: #D32F2F;
            }
        """)
        remove_btn.clicked.connect(lambda: self._remove_folder(folder_path))
        header_row.addWidget(remove_btn)
        
        frame_layout.addLayout(header_row)
        
        # Instruction input
        instruction_layout = QVBoxLayout()
        instruction_layout.setSpacing(6)
        
        instruction_label = QLabel("Organization Instruction (Optional)")
        instruction_label.setStyleSheet("color: #666666; font-size: 11px; font-weight: 600; text-transform: uppercase; border: none; background: transparent;")
        instruction_layout.addWidget(instruction_label)
        
        instruction_input = QLineEdit()
        instruction_input.setPlaceholderText("e.g. Move screenshots to Images/Screenshots, organize others by type...")
        instruction_input.setText(instruction)
        instruction_input.setMinimumHeight(38)
        instruction_input.textChanged.connect(
            lambda text, fp=folder_path: self._on_instruction_changed(fp, text)
        )
        instruction_layout.addWidget(instruction_input)
        
        frame_layout.addLayout(instruction_layout)
        
        # Store widgets for later reference
        self.folder_widgets[folder_path] = {
            'widget': frame,
            'input': instruction_input
        }
        
        # Add to layout (before spacer)
        self.folders_layout.insertWidget(self.folders_layout.count() - 2, frame)
    
    def _remove_folder(self, folder_path: str):
        """Remove a folder from the list."""
        if folder_path in self.folder_widgets:
            # Remove widget
            widget = self.folder_widgets[folder_path]['widget']
            widget.deleteLater()
            del self.folder_widgets[folder_path]
            
            # Remove data
            if folder_path in self.folder_data:
                del self.folder_data[folder_path]
            
            self._update_no_folders_visibility()
    
    def _on_instruction_changed(self, folder_path: str, text: str):
        """Handle instruction text change."""
        if folder_path in self.folder_data:
            self.folder_data[folder_path] = text
    
    def _update_no_folders_visibility(self):
        """Show/hide placeholder based on folder count."""
        has_folders = len(self.folder_data) > 0
        self.no_folders_label.setVisible(not has_folders)
    
    def _save_and_close(self):
        """Save settings and close dialog."""
        # Update settings
        new_folders = []
        for path, instruction in self.folder_data.items():
            new_folders.append({
                'path': path,
                'instruction': instruction
            })
        
        settings.auto_organize_folders = new_folders
        settings.save()
        
        self.accept()
    
    def get_folder_count(self) -> int:
        """Get the number of configured folders."""
        return len(self.folder_data)



class OrganizePage(QWidget):
    """
    AI Organization page widget.
    
    Implements the safe organization flow:
    - AI decides what should happen (proposes plan)
    - App decides what actually happens (validates + executes)
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_plan = None
        self.current_moves = []
        self.files_by_id = {}
        self.destination_path = None
        self.plan_worker = None
        # Undo tracking - stores the last completed organization
        self.last_organization = None  # List of {source, destination, file_id}
        # Refinement tracking
        self.original_instruction = None
        
        # Watch & Auto-Organize
        self.auto_watcher = None
        self.watch_folders: List[str] = []
        self._init_auto_watcher()
        
        self.setup_ui()
        
        # Check auto-start after UI is ready
        QTimer.singleShot(500, self._check_auto_start)
    
    def _init_auto_watcher(self):
        """Initialize the auto-organize watcher."""
        from app.core.auto_watcher import AutoOrganizeWatcher
        
        self.auto_watcher = AutoOrganizeWatcher(self)
        self.auto_watcher.file_organized.connect(self._on_watch_file_organized)
        self.auto_watcher.file_indexed.connect(self._on_watch_file_indexed)
        self.auto_watcher.status_changed.connect(self._on_watch_status)
        self.auto_watcher.error_occurred.connect(self._on_watch_error)
    
    def setup_ui(self):
        """Setup the organization page UI."""
        # Main layout for this widget
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Scroll area to handle overflow
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        # Container widget inside scroll area
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(20)
        
        # Header
        header = QLabel("AI File Organizer")
        header.setObjectName("heroHeading")
        layout.addWidget(header)
        
        subtitle = QLabel(
            "Describe how you want your files organized in plain English. "
            "AI will analyze your indexed files and propose an organization plan."
        )
        subtitle.setObjectName("heroSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        
        # Instruction Input Card
        instruction_card = QFrame()
        instruction_card.setObjectName("organizeCard")
        instruction_card.setStyleSheet("""
            QFrame#organizeCard {
                background-color: rgba(124, 77, 255, 0.06);
                border: 2px dashed rgba(124, 77, 255, 0.5);
                border-radius: 20px;
                padding: 24px;
            }
        """)
        instruction_layout = QVBoxLayout(instruction_card)
        instruction_layout.setContentsMargins(20, 20, 20, 20)
        instruction_layout.setSpacing(12)
        
        # Section title
        inst_title = QLabel("✨ Your Instruction")
        inst_title.setStyleSheet("font-size: 16px; font-weight: 600; color: #7C4DFF; background: transparent;")
        instruction_layout.addWidget(inst_title)
        
        # Input row with text field and mic button
        input_row = QHBoxLayout()
        input_row.setSpacing(12)
        
        self.instruction_input = QLineEdit()
        self.instruction_input.setPlaceholderText(
            "e.g., Organize thumbnails by client name or Sort invoices by year"
        )
        self.instruction_input.setMinimumHeight(50)
        self.instruction_input.setStyleSheet("""
            QLineEdit {
                font-size: 15px;
                padding: 12px 16px;
                background-color: #FFFFFF;
                border: 2px solid #E0E0E0;
                border-radius: 12px;
                color: #1A1A1A;
            }
            QLineEdit:focus {
                border: 2px solid #7C4DFF;
                background-color: #FFFFFF;
            }
            QLineEdit::placeholder {
                color: #999999;
            }
        """)
        self.instruction_input.textChanged.connect(self._update_generate_button)
        self.instruction_input.returnPressed.connect(self.generate_plan)
        input_row.addWidget(self.instruction_input)
        
        # Microphone button for voice input
        self.mic_button = QPushButton("🎤")
        self.mic_button.setMinimumHeight(50)
        self.mic_button.setMinimumWidth(60)
        self.mic_button.setMaximumWidth(60)
        self.mic_button.setToolTip("Click to speak your instruction (click again to stop)")
        self.mic_button.setStyleSheet("""
            QPushButton {
                font-size: 18px;
                background-color: #FFFFFF;
                border: 2px solid #E0E0E0;
                border-radius: 12px;
                color: #1A1A1A;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: rgba(124, 77, 255, 0.05);
                border-color: #7C4DFF;
            }
            QPushButton:pressed {
                background-color: rgba(124, 77, 255, 0.1);
            }
        """)
        self.mic_button.clicked.connect(self._toggle_voice_recording)
        input_row.addWidget(self.mic_button)
        
        instruction_layout.addLayout(input_row)
        
        # Voice recording state
        self.voice_worker = None
        self.is_recording_voice = False
        
        examples_label = QLabel(
            "💡 Examples: Organize by file type, Group photos by date, Sort by topic"
        )
        examples_label.setStyleSheet("color: #808080; font-size: 12px; background: transparent;")
        examples_label.setWordWrap(True)
        instruction_layout.addWidget(examples_label)
        
        layout.addWidget(instruction_card)
        
        # Destination Folder Card
        dest_card = QFrame()
        dest_card.setObjectName("organizeCard")
        dest_card.setStyleSheet("""
            QFrame#organizeCard {
                background-color: rgba(124, 77, 255, 0.06);
                border: 2px dashed rgba(124, 77, 255, 0.5);
                border-radius: 20px;
            }
        """)
        dest_layout = QHBoxLayout(dest_card)
        dest_layout.setContentsMargins(20, 16, 20, 16)
        dest_layout.setSpacing(16)
        
        dest_icon = QLabel("📂")
        dest_icon.setStyleSheet("font-size: 24px; background: transparent;")
        dest_layout.addWidget(dest_icon)
        
        dest_info = QVBoxLayout()
        dest_info.setSpacing(4)
        
        dest_title = QLabel("Destination Folder")
        dest_title.setStyleSheet("font-size: 16px; font-weight: 600; color: #7C4DFF; background: transparent;")
        dest_info.addWidget(dest_title)
        
        self.dest_label = QLabel("Select where organized files will be moved...")
        self.dest_label.setStyleSheet("color: #808080; font-size: 13px; background: transparent;")
        self.dest_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        dest_info.addWidget(self.dest_label)
        
        dest_layout.addLayout(dest_info, 1)
        
        self.dest_button = QPushButton("Choose Folder")
        self.dest_button.setMinimumHeight(40)
        self.dest_button.setMinimumWidth(140)
        self.dest_button.setCursor(Qt.PointingHandCursor)
        self.dest_button.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #7C4DFF;
                border: 2px solid #7C4DFF;
                border-radius: 10px;
                font-size: 14px;
                font-weight: 600;
                padding: 8px 16px;
            }
            QPushButton:hover {
                background-color: rgba(124, 77, 255, 0.1);
            }
        """)
        self.dest_button.clicked.connect(self.select_destination)
        dest_layout.addWidget(self.dest_button)
        
        layout.addWidget(dest_card)

        # Action Buttons
        action_layout = QHBoxLayout()
        action_layout.setSpacing(12)
        
        self.generate_button = QPushButton("✨ Generate Plan")
        self.generate_button.setMinimumHeight(48)
        self.generate_button.setMinimumWidth(180)
        self.generate_button.setEnabled(False)
        self.generate_button.setCursor(Qt.PointingHandCursor)
        self.generate_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(124, 77, 255, 0.1);
                color: #7C4DFF;
                border: 2px solid #7C4DFF;
                border-radius: 12px;
                font-weight: 700;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: #7C4DFF;
                color: white;
            }
            QPushButton:disabled {
                background-color: transparent;
                border-color: #3A3A3A;
                color: #606060;
            }
        """)
        self.generate_button.clicked.connect(self.generate_plan)
        action_layout.addWidget(self.generate_button)
        
        self.apply_button = QPushButton("✓ Apply Organization")
        self.apply_button.setMinimumHeight(48)
        self.apply_button.setMinimumWidth(200)
        self.apply_button.setEnabled(False)
        self.apply_button.setCursor(Qt.PointingHandCursor)
        self.apply_button.setStyleSheet("""
            QPushButton {
                background-color: rgba(76, 175, 80, 0.1);
                color: #4CAF50;
                border: 2px solid #4CAF50;
                border-radius: 12px;
                font-weight: 700;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: #4CAF50;
                color: white;
            }
            QPushButton:disabled {
                background-color: transparent;
                border-color: #3A3A3A;
                color: #606060;
            }
        """)
        self.apply_button.clicked.connect(self.apply_organization)
        action_layout.addWidget(self.apply_button)
        
        self.clear_button = QPushButton("Clear")
        self.clear_button.setMinimumHeight(48)
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #666666;
                border: 2px solid #999999;
                border-radius: 12px;
                font-weight: 600;
                font-size: 15px;
            }
            QPushButton:hover {
                border-color: #333333;
                color: #333333;
                background-color: #F5F5F5;
            }
        """)
        self.clear_button.clicked.connect(self.clear_plan)
        action_layout.addWidget(self.clear_button)
        
        self.undo_button = QPushButton("↩ Undo Last")
        self.undo_button.setMinimumHeight(48)
        self.undo_button.setMinimumWidth(130)
        self.undo_button.setEnabled(False)
        self.undo_button.setCursor(Qt.PointingHandCursor)
        self.undo_button.setToolTip("Undo the last organization (move files back)")
        self.undo_button.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #FFA726;
                border: 2px solid #FFA726;
                border-radius: 12px;
                font-weight: 600;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: #FFF3E0;
                color: #F57C00;
                border-color: #F57C00;
            }
            QPushButton:disabled {
                background-color: transparent;
                border-color: #E0E0E0;
                color: #CCCCCC;
            }
        """)
        self.undo_button.clicked.connect(self.undo_last_organization)
        action_layout.addWidget(self.undo_button)
        
        action_layout.addStretch()
        
        # Hide these buttons initially - shown after plan is generated
        self.apply_button.setVisible(False)
        self.clear_button.setVisible(False)
        self.undo_button.setVisible(False)
        
        layout.addLayout(action_layout)
        
        # Progress and Status
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumHeight(8)
        layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888; font-style: italic; font-size: 13px;")
        layout.addWidget(self.status_label)

        # Results Area (Splitter: Tree + Details) - Hidden until plan is generated
        self.results_splitter = QSplitter(Qt.Horizontal)
        self.results_splitter.setChildrenCollapsible(False)
        
        # Left: Plan Tree Card
        plan_card = QFrame()
        plan_card.setObjectName("resultsCard")
        plan_card.setStyleSheet("""
            QFrame#resultsCard {
                background-color: #FFFFFF;
                border: 1px solid #E0E0E0;
                border-radius: 16px;
            }
        """)
        plan_layout = QVBoxLayout(plan_card)
        plan_layout.setContentsMargins(0, 0, 0, 0)
        plan_layout.setSpacing(0)
        
        # Modern Header for Tree
        plan_header = QLabel("  Proposed Organization")
        plan_header.setStyleSheet("""
            background-color: #FAFAFA;
            color: #666666;
            font-family: "Segoe UI", sans-serif;
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            padding: 12px 16px;
            border-top-left-radius: 16px;
            border-top-right-radius: 16px;
            border-bottom: 1px solid #EEEEEE;
        """)
        plan_layout.addWidget(plan_header)
        
        self.plan_tree = QTreeWidget()
        self.plan_tree.setHeaderHidden(True)  # Hide default header
        self.plan_tree.setIndentation(20)
        self.plan_tree.setAlternatingRowColors(False)
        self.plan_tree.setStyleSheet("""
            QTreeWidget {
                border: none;
                background-color: transparent;
                font-family: "Segoe UI", "Helvetica Neue", sans-serif;
                font-size: 14px;
                padding: 10px;
                outline: none;
            }
            QTreeWidget::item {
                height: 32px;
                color: #2D2D2D;
                border-radius: 6px;
                padding-left: 4px;
            }
            QTreeWidget::item:hover {
                background-color: #F5F5F5;
            }
            QTreeWidget::item:selected {
                background-color: rgba(124, 77, 255, 0.08);
                color: #7C4DFF;
                font-weight: 600;
            }
            /* Explicitly set Black Arrows */
            QTreeView::branch:has-children:!has-siblings:closed,
            QTreeView::branch:closed:has-children:has-siblings {
                image: url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDE2IDE2Ij48cGF0aCBmaWxsPSIjMzMzMzMzIiBkPSZNIDYgNCBMIDYgMTIgTCAxMiA4IFogIi8+PC9zdmc+);
            }
            QTreeView::branch:open:has-children:!has-siblings,
            QTreeView::branch:open:has-children:has-siblings {
                image: url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxNiIgaGVpZ2h0PSIxNiIgdmlld0JveD0iMCAwIDE2IDE2Ij48cGF0aCBmaWxsPSIjMzMzMzMzIiBkPSZNIDQgNiBMIDEyIDYgTCA4IDEyIFogIi8+PC9zdmc+);
            }
        """)
        self.plan_tree.itemClicked.connect(self._on_tree_item_clicked)
        plan_layout.addWidget(self.plan_tree)
        
        self.results_splitter.addWidget(plan_card)
        
        # Right: Details Panel Card
        details_card = QFrame()
        details_card.setObjectName("resultsCard")
        details_card.setStyleSheet("""
            QFrame#resultsCard {
                background-color: #FFFFFF;
                border: 1px solid #E0E0E0;
                border-radius: 16px;
            }
        """)
        details_layout = QVBoxLayout(details_card)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(0)
        
        # Modern Header for Details
        details_header = QLabel("  Plan Details")
        details_header.setStyleSheet("""
            background-color: rgba(124, 77, 255, 0.05);
            color: #7C4DFF;
            font-weight: 600;
            font-size: 13px;
            padding: 12px;
            border-top-left-radius: 16px;
            border-top-right-radius: 16px;
            border-bottom: 1px solid #E0E0E0;
        """)
        details_layout.addWidget(details_header)
        
        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setFrameShape(QFrame.NoFrame)
        self.details_text.setStyleSheet("""
            QTextEdit {
                background-color: transparent;
                color: #333333;
                font-family: "Segoe UI", "Helvetica Neue", sans-serif;
                font-size: 14px;
                line-height: 1.5;
                padding: 16px;
                border: none;
            }
        """)
        details_layout.addWidget(self.details_text)
        
        self.results_splitter.addWidget(details_card)
        self.results_splitter.setSizes([500, 300])
        
        self.results_splitter.setVisible(False)  # Hidden until plan is generated
        layout.addWidget(self.results_splitter, 1)
        
        # Feedback/Refinement Section (hidden until plan is generated)
        self.feedback_group = QGroupBox("Refine Plan")
        self.feedback_group.setVisible(False)
        feedback_layout = QHBoxLayout(self.feedback_group)
        
        self.feedback_input = QLineEdit()
        self.feedback_input.setPlaceholderText(
            "e.g., 'Move the JSON files to a separate folder' or 'Don't include the screenshots'"
        )
        self.feedback_input.setMinimumHeight(36)
        self.feedback_input.returnPressed.connect(self.refine_plan)
        feedback_layout.addWidget(self.feedback_input, 1)
        
        self.refine_button = QPushButton("🔄 Refine")
        self.refine_button.setMinimumHeight(42)
        self.refine_button.setMinimumWidth(110)
        self.refine_button.setCursor(Qt.PointingHandCursor)
        self.refine_button.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C4DFF, stop:1 #9575FF);
                color: white;
                border: none;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9575FF, stop:1 #B39DFF);
            }
        """)
        self.refine_button.clicked.connect(self.refine_plan)
        feedback_layout.addWidget(self.refine_button)
        
        layout.addWidget(self.feedback_group)
        
        # ========== WATCH & AUTO-ORGANIZE SECTION ==========
        self._create_auto_organize_section(layout)
        
        # Finalize scroll area
        scroll.setWidget(container)
        main_layout.addWidget(scroll)
        
        # Load initial state
        self._update_file_count()
    
    def _create_auto_organize_section(self, parent_layout):
        """Create the Watch & Auto-Organize section matching app theme."""
        # Main card container
        watch_card = QFrame()
        watch_card.setObjectName("watchAutoCard")
        watch_card.setStyleSheet("""
            QFrame#watchAutoCard {
                background-color: rgba(124, 77, 255, 0.06);
                border: 2px dashed rgba(124, 77, 255, 0.5);
                border-radius: 20px;
            }
        """)
        watch_layout = QVBoxLayout(watch_card)
        watch_layout.setSpacing(16)
        watch_layout.setContentsMargins(24, 24, 24, 24)
        
        # Header with icon and title
        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        
        watch_icon = QLabel("👁️")
        watch_icon.setStyleSheet("font-size: 28px; background: transparent;")
        header_row.addWidget(watch_icon)
        
        header_info = QVBoxLayout()
        header_info.setSpacing(4)
        
        watch_title = QLabel("Watch & Auto-Organize")
        watch_title.setStyleSheet("font-size: 18px; font-weight: 600; color: #7C4DFF; background: transparent;")
        header_info.addWidget(watch_title)
        
        watch_desc = QLabel("Monitor folders for new files and organize them automatically")
        watch_desc.setStyleSheet("color: #808080; font-size: 13px; background: transparent;")
        header_info.addWidget(watch_desc)
        
        header_row.addLayout(header_info, 1)
        watch_layout.addLayout(header_row)
        
        # Separator line
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet("background-color: #2A2A2A; border: none; max-height: 1px;")
        watch_layout.addWidget(separator)
        
        # Status section
        status_container = QHBoxLayout()
        status_container.setSpacing(16)
        
        # Status indicator
        self.watch_status_label = QLabel("📁 No folders configured")
        self.watch_status_label.setStyleSheet("font-size: 14px; color: #A0A0A0; background: transparent;")
        status_container.addWidget(self.watch_status_label, 1)
        
        watch_layout.addLayout(status_container)
        
        # Activity line (shows latest organized file when watching)
        self.watch_activity_label = QLabel("")
        self.watch_activity_label.setStyleSheet("""
            font-size: 13px; 
            color: #7C4DFF; 
            background: rgba(124, 77, 255, 0.1); 
            padding: 8px 12px; 
            border-radius: 8px;
            border: 1px solid rgba(124, 77, 255, 0.2);
        """)
        self.watch_activity_label.setVisible(False)
        watch_layout.addWidget(self.watch_activity_label)
        
        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(16)
        
        # Configure button
        self.watch_config_btn = QPushButton("⚙️ Configure Folders")
        self.watch_config_btn.setMinimumHeight(44)
        self.watch_config_btn.setMinimumWidth(160)
        self.watch_config_btn.setCursor(Qt.PointingHandCursor)
        self.watch_config_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #7C4DFF;
                border: 2px solid #7C4DFF;
                border-radius: 12px;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: rgba(124, 77, 255, 0.1);
            }
        """)
        self.watch_config_btn.clicked.connect(self._open_watch_config)
        btn_row.addWidget(self.watch_config_btn)
        
        # Start/Stop button
        self.watch_toggle_btn = QPushButton("▶ Start Watching")
        self.watch_toggle_btn.setMinimumHeight(44)
        self.watch_toggle_btn.setMinimumWidth(160)
        self.watch_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.watch_toggle_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4CAF50, stop:1 #66BB6A);
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #66BB6A, stop:1 #81C784);
            }
            QPushButton:disabled {
                background: #2A2A2A;
                color: #606060;
            }
        """)
        self.watch_toggle_btn.clicked.connect(self._toggle_watch_mode)
        btn_row.addWidget(self.watch_toggle_btn)
        
        btn_row.addStretch()
        watch_layout.addLayout(btn_row)
        
        # Hidden summary label for compatibility
        self.watch_summary_label = QLabel("")
        self.watch_summary_label.setVisible(False)
        
        parent_layout.addWidget(watch_card)
        
        # Initial UI update
        self._update_watch_summary()
    
    def _open_watch_config(self):
        """Open the watch configuration dialog."""
        dialog = WatchConfigDialog(self)
        result = dialog.exec()
        
        if result == QDialog.Accepted:
            self._update_watch_summary()
            
            # If watcher is running, apply new settings without stopping
            if self.auto_watcher and self.auto_watcher.is_running:
                self._apply_config_changes()
    
    def _apply_config_changes(self):
        """Apply configuration changes while watcher is running."""
        # Update folder instructions from settings
        folder_instructions = {}
        for folder_data in settings.auto_organize_folders:
            folder_path = folder_data.get('path', '')
            instruction = folder_data.get('instruction', '')
            if folder_path:
                normalized_path = os.path.normpath(folder_path)
                folder_instructions[normalized_path] = instruction
        
        # Update watcher's instructions
        self.auto_watcher.folder_instructions = folder_instructions
        
        # Count existing files
        existing_count = 0
        subfolder_count = 0
        for folder in self.watch_folders:
            try:
                for item in os.listdir(folder):
                    item_path = os.path.join(folder, item)
                    if os.path.isfile(item_path):
                        existing_count += 1
                    elif os.path.isdir(item_path) and not item.startswith('.'):
                        subfolder_count += 1
                        for sub_item in os.listdir(item_path):
                            if os.path.isfile(os.path.join(item_path, sub_item)):
                                existing_count += 1
            except Exception:
                pass
        
        total_items = existing_count + subfolder_count
        
        if total_items > 0:
            # Ask user what to do with existing files with new instructions
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Apply New Instructions?")
            
            if subfolder_count > 0:
                dialog.setText(f"Instructions changed. Found {existing_count} file(s) and {subfolder_count} subfolder(s).")
            else:
                dialog.setText(f"Instructions changed. Found {existing_count} file(s) in watched folders.")
            dialog.setInformativeText(
                "How would you like to apply the new instructions?\n\n"
                "• Re-organize All: Flatten folders first, then organize fresh\n"
                "• Organize As-Is: Organize files with new instructions\n"
                "• Continue Watching: Keep watching, only apply to new files"
            )
            
            reorganize_btn = dialog.addButton("Re-organize All", QMessageBox.AcceptRole)
            organize_btn = dialog.addButton("Organize As-Is", QMessageBox.AcceptRole)
            continue_btn = dialog.addButton("Continue Watching", QMessageBox.RejectRole)
            
            dialog.exec()
            clicked = dialog.clickedButton()
            
            if clicked == reorganize_btn:
                # Flatten and reorganize
                self.auto_watcher._organize_existing_files_with_options(flatten_first=True)
            elif clicked == organize_btn:
                # Organize as-is with new instructions
                self.auto_watcher._organize_existing_files_with_options(flatten_first=False)
            # else: continue_btn - just keep watching with new instructions
        
        self.watch_activity_label.setText("Instructions updated, watching...")
        logger.info("Applied configuration changes while watching")
    
    def _update_watch_summary(self):
        """Update the watch status display."""
        folder_count = len(settings.auto_organize_folders)
        is_watching = self.auto_watcher and self.auto_watcher.is_running
        auto_start = settings.auto_organize_auto_start
        
        if folder_count == 0:
            self.watch_status_label.setText("📁 No folders configured")
            self.watch_status_label.setStyleSheet("font-size: 13px; color: #888;")
            self.watch_toggle_btn.setEnabled(False)
        else:
            # Build status text
            auto_start_text = " • Auto-start enabled" if auto_start else ""
            
            if is_watching:
                status_text = f"✅ Watching {folder_count} folder{'s' if folder_count > 1 else ''}{auto_start_text}"
                self.watch_status_label.setStyleSheet("font-size: 13px; color: #2ecc71; font-weight: 500;")
            else:
                status_text = f"📁 {folder_count} folder(s) configured{auto_start_text}"
                self.watch_status_label.setStyleSheet("font-size: 13px; color: #aaa;")
            
            self.watch_status_label.setText(status_text)
            self.watch_toggle_btn.setEnabled(True)
        
        # Update button state
        if is_watching:
            self.watch_toggle_btn.setText("⏹ Stop")
            self.watch_toggle_btn.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #EF5350, stop:1 #E57373);
                    color: white;
                    border: none;
                    border-radius: 12px;
                    font-size: 14px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #E57373, stop:1 #EF9A9A);
                }
            """)
        else:
            self.watch_toggle_btn.setText("▶ Start Watching")
            self.watch_toggle_btn.setStyleSheet("""
                QPushButton {
                    background-color: #2ecc71;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    font-size: 13px;
                    font-weight: 600;
                }
                QPushButton:hover {
                    background-color: #27ae60;
                }
                QPushButton:disabled {
                    background-color: #3a3a3a;
                    color: #666;
                }
            """)
    
    def _update_watch_summary_as_watching(self):
        """Immediately update UI to show watching state (before watcher actually starts)."""
        folder_count = len(self.watch_folders)
        auto_start = settings.auto_organize_auto_start
        auto_start_text = " • Auto-start enabled" if auto_start else ""
        
        status_text = f"✅ Watching {folder_count} folder{'s' if folder_count > 1 else ''}{auto_start_text}"
        self.watch_status_label.setText(status_text)
        self.watch_status_label.setStyleSheet("font-size: 13px; color: #2ecc71; font-weight: 500;")
        
        self.watch_toggle_btn.setText("⏹ Stop")
        self.watch_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #c0392b;
            }
        """)
    
    def _toggle_watch_mode(self):
        """Toggle the watch mode on/off."""
        if self.auto_watcher and self.auto_watcher.is_running:
            self._stop_watch_mode()
        else:
            self._start_watch_mode()
    
    def _start_watch_mode(self, is_catch_up: bool = False, catch_up_since=None, skip_existing_popup: bool = False):
        """Start watching folders for new files.
        
        Args:
            is_catch_up: If True, organize files modified since catch_up_since
            catch_up_since: Datetime to filter files for catch-up mode
            skip_existing_popup: If True, skip the "Organize Existing Files?" popup (for auto-start)
        """
        if not settings.auto_organize_folders:
            QMessageBox.warning(
                self, "No Folders",
                "Please configure folders to watch first."
            )
            return
        
        # Clear and setup watcher
        self.auto_watcher.clear_folders()
        self.watch_folders.clear()
        
        # Build per-folder instructions dict from settings
        # CRITICAL: Use os.path.normpath to match watcher's path format
        folder_instructions = {}
        has_any_instruction = False
        
        for folder_data in settings.auto_organize_folders:
            folder_path = folder_data.get('path', '')
            instruction = folder_data.get('instruction', '')
            
            if folder_path:
                # Normalize the path to match how watcher stores folders
                normalized_path = os.path.normpath(folder_path)
                
                if os.path.isdir(normalized_path):
                    self.auto_watcher.add_folder(normalized_path)
                    self.watch_folders.append(normalized_path)
                    folder_instructions[normalized_path] = instruction
                    
                    if instruction:
                        has_any_instruction = True
                    
                    logger.info(f"Added watch folder: {normalized_path} with instruction: {instruction[:30] if instruction else '(none)'}...")
        
        if not self.watch_folders:
            QMessageBox.warning(
                self, "No Valid Folders",
                "None of the configured folders exist. Please reconfigure."
            )
            return
        
        # Set folder instructions
        self.auto_watcher.folder_instructions = folder_instructions
        
        # Set catch-up filter if provided
        if catch_up_since:
            self.auto_watcher.catch_up_since = catch_up_since
        
        # UPDATE UI IMMEDIATELY - show "watching" state right away
        self._update_watch_summary_as_watching()
        self.watch_activity_label.setVisible(True)
        self.watch_activity_label.setText("Preparing...")
        
        # Process events to update UI before dialog
        from PySide6.QtWidgets import QApplication
        QApplication.processEvents()
        
        # Count existing files (including files in subfolders)
        existing_count = 0
        subfolder_count = 0
        for folder in self.watch_folders:
            try:
                for item in os.listdir(folder):
                    item_path = os.path.join(folder, item)
                    if os.path.isfile(item_path):
                        existing_count += 1
                    elif os.path.isdir(item_path) and not item.startswith('.'):
                        # Count files in subfolders too
                        subfolder_count += 1
                        for sub_item in os.listdir(item_path):
                            if os.path.isfile(os.path.join(item_path, sub_item)):
                                existing_count += 1
            except Exception:
                pass
        
        # Ask user what to do with existing files
        organize_existing = False
        flatten_first = False
        
        total_items = existing_count + subfolder_count
        if total_items > 0 and not is_catch_up and not skip_existing_popup:
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Organize Existing Files?")
            
            if subfolder_count > 0:
                dialog.setText(f"Found {existing_count} file(s) and {subfolder_count} subfolder(s) in the watched folder(s).")
            else:
                dialog.setText(f"Found {existing_count} file(s) in the watched folder(s).")
            dialog.setInformativeText(
                "Choose how to handle existing files:\n\n"
                "• Re-organize All: Flatten folders first, then organize fresh\n"
                "• Organize As-Is: Organize files in current locations\n"
                "• Watch Only: Skip existing, only organize new files"
            )
            
            reorganize_btn = dialog.addButton("Re-organize All", QMessageBox.AcceptRole)
            organize_btn = dialog.addButton("Organize As-Is", QMessageBox.AcceptRole)
            watch_btn = dialog.addButton("Watch Only", QMessageBox.RejectRole)
            
            dialog.exec()
            clicked = dialog.clickedButton()
            
            if clicked == reorganize_btn:
                organize_existing = True
                flatten_first = True
            elif clicked == organize_btn:
                organize_existing = True
            # else: watch_btn - just watch, don't organize existing
        elif is_catch_up:
            organize_existing = True
        
        # Start the watcher
        self.auto_watcher.start(organize_existing=organize_existing, flatten_first=flatten_first)
        
        # Update activity label
        self.watch_activity_label.setText("Waiting for new files...")
    
    def _stop_watch_mode(self):
        """Stop watching folders."""
        if self.auto_watcher:
            self.auto_watcher.stop()
        
        # Save last active timestamp for catch-up feature
        settings.update_auto_organize_last_active()
        
        # Update UI using centralized method
        self._update_watch_summary()
        self.watch_activity_label.setVisible(False)
        self.watch_activity_label.setText("")
    
    def _check_auto_start(self):
        """Check if we should auto-start the watcher on app open."""
        # Auto-start if there are configured folders (no toggle needed)
        if not settings.auto_organize_folders:
            return
        
        # Check for catch-up (files added while app was closed)
        last_active = settings.get_auto_organize_last_active_time()
        
        if last_active:
            # Calculate time difference
            from datetime import datetime
            now = datetime.now()
            diff = now - last_active
            hours = diff.total_seconds() / 3600
            
            if hours > 0.5:  # More than 30 min since last active - do catch-up silently
                logger.info(f"Auto-start catch-up: watcher was inactive for {hours:.1f} hours")
                self._start_watch_mode(is_catch_up=True, catch_up_since=last_active, skip_existing_popup=True)
                return
        
        # Normal auto-start - skip existing files popup, just watch for new files
        self._start_watch_mode(skip_existing_popup=True)
    
    def _on_watch_file_organized(self, source: str, dest: str, category: str):
        """Handle file organized signal from watcher."""
        file_name = os.path.basename(source)
        self.watch_activity_label.setVisible(True)
        self.watch_activity_label.setText(f"Latest: {file_name} → {category}/")
        logger.info(f"Watch organized: {source} -> {dest}")
    
    def _on_watch_file_indexed(self, file_path: str):
        """Handle file indexed signal from watcher."""
        file_name = os.path.basename(file_path)
        self.watch_activity_label.setVisible(True)
        self.watch_activity_label.setText(f"Indexing: {file_name}")
        logger.info(f"Watch auto-indexed: {file_path}")
    
    def _on_watch_status(self, status: str):
        """Handle status updates from watcher."""
        # Show status in activity label, not status label (which now shows folder count)
        self.watch_activity_label.setVisible(True)
        self.watch_activity_label.setText(status)
    
    def _on_watch_error(self, path: str, error: str):
        """Handle errors from watcher."""
        file_name = os.path.basename(path) if path else "Unknown"
        self.watch_activity_label.setVisible(True)
        self.watch_activity_label.setText(f"⚠️ Error: {file_name}")
        logger.error(f"Watch error for {path}: {error}")
    
    def showEvent(self, event):
        """Refresh file count when page becomes visible."""
        super().showEvent(event)
        if not self.current_plan:
            self._update_file_count()
    
    def _update_file_count(self):
        """Show how many indexed files are available."""
        try:
            count = file_index.get_file_count()
            if count > 0:
                self.status_label.setText(f"{count} indexed files available for organization")
            else:
                self.status_label.setText("No files indexed yet. Go to Index Files to add files first.")
        except Exception as e:
            logger.error(f"Error getting file count: {e}")
            self.status_label.setText("Could not load file count")
    
    def select_destination(self):
        """Open folder picker for destination."""
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Destination Folder", str(Path.home())
        )
        if folder:
            self.destination_path = Path(folder)
            self.dest_label.setText(str(self.destination_path))
            self.dest_label.setStyleSheet("color: inherit; font-weight: bold;")
            self._update_generate_button()
    
    def _update_generate_button(self):
        """Enable generate button when destination is set (instruction optional for auto-organize)."""
        has_destination = self.destination_path is not None
        self.generate_button.setEnabled(has_destination)
    
    def _toggle_voice_recording(self):
        """Toggle voice recording on/off."""
        if self.is_recording_voice:
            self._stop_voice_recording()
        else:
            self._start_voice_recording()
    
    def _start_voice_recording(self):
        """Start recording voice input."""
        self.is_recording_voice = True
        self.mic_button.setText("⏹️")
        self.mic_button.setStyleSheet("""
            QPushButton {
                font-size: 18px;
                background-color: #ff4444;
                border: 1px solid #cc0000;
                border-radius: 6px;
                color: white;
            }
            QPushButton:hover {
                background-color: #ff6666;
            }
        """)
        self.mic_button.setToolTip("Recording... Click to stop")
        self.status_label.setText("🎤 Recording... Speak your instruction, then click to stop.")
        
        # Start voice worker
        self.voice_worker = VoiceRecordWorker()
        self.voice_worker.finished.connect(self._on_voice_transcribed)
        self.voice_worker.error.connect(self._on_voice_error)
        self.voice_worker.recording_stopped.connect(self._on_recording_stopped)
        self.voice_worker.start()
    
    def _stop_voice_recording(self):
        """Stop recording voice input."""
        if self.voice_worker:
            self.voice_worker.stop_recording()
        self.status_label.setText("⏳ Transcribing...")
    
    def _on_recording_stopped(self):
        """Called when recording has stopped, before transcription."""
        self.is_recording_voice = False
        self._reset_mic_button()
    
    def _on_voice_transcribed(self, text: str):
        """Handle transcribed text from voice input."""
        self.is_recording_voice = False
        self._reset_mic_button()
        
        if text.strip():
            # Append to existing text or replace
            current = self.instruction_input.text().strip()
            if current:
                self.instruction_input.setText(f"{current} {text}")
            else:
                self.instruction_input.setText(text)
            self.status_label.setText(f"✓ Voice transcribed: \"{text[:50]}{'...' if len(text) > 50 else ''}\"")
            logger.info(f"Voice transcribed: {text}")
        else:
            self.status_label.setText("No speech detected. Try again.")
    
    def _on_voice_error(self, error: str):
        """Handle voice recording errors."""
        self.is_recording_voice = False
        self._reset_mic_button()
        self.status_label.setText(f"Voice error: {error}")
        logger.error(f"Voice recording error: {error}")
        
        # Show error if it's about missing libraries
        if "Missing audio library" in error:
            QMessageBox.warning(
                self, "Audio Library Missing",
                f"{error}\n\nThe voice input feature requires additional libraries.\n"
                "Please install them and restart the app."
            )
    
    def _reset_mic_button(self):
        """Reset mic button to default state."""
        self.mic_button.setText("🎤")
        self.mic_button.setStyleSheet("""
            QPushButton {
                font-size: 18px;
                background-color: #f0f0f0;
                border: 1px solid #ccc;
                border-radius: 6px;
            }
            QPushButton:hover {
                background-color: #e0e0e0;
            }
            QPushButton:pressed {
                background-color: #d0d0d0;
            }
        """)
        self.mic_button.setToolTip("Click to speak your instruction (click again to stop)")

    def _load_files_from_db(self) -> List[Dict[str, Any]]:
        """Load all indexed files from the database."""
        files = []
        self.files_by_id = {}
        
        try:
            with sqlite3.connect(file_index.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM files")
                rows = cursor.fetchall()
            
            for row in rows:
                f = {
                    "id": row["id"],
                    "file_path": row["file_path"],
                    "file_name": row["file_name"],
                    "file_size": row["file_size"] or 0,
                    "label": row["label"] if "label" in row.keys() else None,
                    "caption": row["caption"] if "caption" in row.keys() else None,
                    "tags": self._parse_tags(row["tags"] if "tags" in row.keys() else None),
                    "category": row["category"] if "category" in row.keys() else None,
                }
                files.append(f)
                self.files_by_id[row["id"]] = f
        except Exception as e:
            logger.error(f"Error loading files: {e}")
        
        return files
    
    def _parse_tags(self, raw) -> List[str]:
        """Parse tags from DB storage format."""
        if not raw:
            return []
        if isinstance(raw, list):
            return raw
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return [str(t) for t in v]
        except:
            pass
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
        return []
    
    def _verify_and_fix_paths(self, files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Verify file paths and fix them if files have been moved.
        
        For each file:
        1. Check if it exists at the recorded path
        2. If not, search for it by name in the destination folder
        3. Try partial matching for renamed files (Windows adds (1), (2) etc)
        4. If found elsewhere, update the database path
        5. If not found anywhere, remove from the list
        
        Returns the list of files with verified/updated paths.
        """
        if not files or not self.destination_path:
            return files
        
        verified_files = []
        updated_count = 0
        updated_names = []
        removed_count = 0
        removed_names = []
        
        # Build a map of all files in the destination folder for quick lookup
        existing_files = {}  # exact filename -> [paths]
        all_files_list = []  # [(filename_lower, full_path), ...] for partial matching
        try:
            for root, dirs, filenames in os.walk(str(self.destination_path)):
                for filename in filenames:
                    full_path = os.path.join(root, filename)
                    # Store by filename (lowercase for case-insensitive matching)
                    key = filename.lower()
                    if key not in existing_files:
                        existing_files[key] = []
                    existing_files[key].append(full_path)
                    all_files_list.append((key, full_path))
        except Exception as e:
            logger.warning(f"Error scanning destination folder: {e}")
        
        logger.info(f"Scanned {len(all_files_list)} files in destination folder")
        
        for f in files:
            file_path = f.get("file_path", "")
            file_name = f.get("file_name", "")
            file_id = f.get("id")
            
            # Check if file exists at recorded path
            if os.path.exists(file_path):
                verified_files.append(f)
                continue
            
            # File not at recorded path - try to find it
            logger.info(f"File not found at recorded path: {file_path}")
            
            # Search by exact filename in destination folder
            key = file_name.lower()
            candidates = existing_files.get(key, [])
            
            new_path = None
            
            if candidates:
                # Found file(s) with exact same name
                new_path = candidates[0]
                candidates.pop(0)
            else:
                # Try partial matching - look for files that start with same base name
                # This handles Windows renaming like "file.png" -> "file (1).png"
                base_name = os.path.splitext(file_name)[0].lower()
                extension = os.path.splitext(file_name)[1].lower()
                
                for existing_name, existing_path in all_files_list:
                    # Check if existing file starts with our base name and has same extension
                    if existing_name.startswith(base_name) and existing_name.endswith(extension):
                        # Make sure it's not already matched to another file
                        if existing_path not in [vf.get("file_path") for vf in verified_files]:
                            new_path = existing_path
                            logger.info(f"Partial match: {file_name} -> {os.path.basename(existing_path)}")
                            break
            
            if new_path:
                logger.info(f"Found moved file: {file_name} -> {new_path}")
                
                # Update database
                if file_index.update_file_path(file_id, new_path):
                    f["file_path"] = new_path
                    verified_files.append(f)
                    updated_count += 1
                    updated_names.append(f"{file_name} → {os.path.basename(new_path)}")
                else:
                    logger.warning(f"Failed to update path in database for {file_name}")
                    removed_count += 1
                    removed_names.append(file_name)
            else:
                # File not found anywhere - skip it
                logger.info(f"File no longer exists, skipping: {file_name}")
                removed_count += 1
                removed_names.append(file_name)
        
        # Show summary dialog if changes were made
        if updated_count > 0 or removed_count > 0:
            logger.info(f"Path verification: {updated_count} updated, {removed_count} removed, {len(verified_files)} verified")
            
            msg_parts = []
            if updated_count > 0:
                msg_parts.append(f"✓ Updated {updated_count} file path(s):")
                for name in updated_names[:5]:
                    msg_parts.append(f"   • {name}")
                if len(updated_names) > 5:
                    msg_parts.append(f"   ... and {len(updated_names) - 5} more")
            
            # Silent path verification - just update status bar, no popup
            status_msg = f"Path check: {updated_count} fixed"
            if removed_count > 0:
                status_msg += f", {removed_count} missing"
            status_msg += f". Sending {len(verified_files)} files to AI..."
            self.status_label.setText(status_msg)
        
        return verified_files
    
    def generate_plan(self):
        """Request organization plan from LLM."""
        instruction = self.instruction_input.text().strip()
        
        if not self.destination_path:
            return
        
        # Auto-organize mode: no instruction provided
        if not instruction:
            reply = QMessageBox.question(
                self,
                "Auto-Organize Mode",
                "AI will analyze your files and propose an organization structure based on:\n\n"
                "  - File types and categories\n"
                "  - AI-generated tags and labels\n"
                "  - Content analysis\n\n"
                "The structure will be kept simple with minimal nesting.\n\n"
                "You will preview the plan before anything is moved.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply != QMessageBox.Yes:
                return
            
            # Use auto-organize instruction - MUST organize ALL files
            instruction = (
                "[AUTO-ORGANIZE] Organize ALL of the provided files into a logical folder structure. "
                "CRITICAL: EVERY single file must be placed in a folder - do NOT leave any file out. "
                "Keep it simple - use only a few broad, clear folder names (e.g., screenshots, documents, images). "
                "Avoid deep nesting (no subfolders inside subfolders). "
                "Group similar files together based on their type, tags, and content. "
                "If some files don't fit any clear category, put them in a 'misc' or 'other' folder. "
                "EVERY file_id provided MUST appear in exactly one folder."
            )
        
        # Save the instruction for potential refinement
        self.original_instruction = instruction
        
        files = self._load_files_from_db()
        
        if not files:
            # No indexed files - check if destination folder has files to index
            if self.destination_path and self.destination_path.exists():
                # Count files in destination folder
                folder_files = []
                try:
                    for item in os.listdir(str(self.destination_path)):
                        item_path = os.path.join(str(self.destination_path), item)
                        if os.path.isfile(item_path):
                            folder_files.append(item_path)
                except Exception as e:
                    logger.error(f"Error scanning destination folder: {e}")
                
                if folder_files:
                    # Ask user if they want to index the folder first
                    reply = QMessageBox.question(
                        self,
                        "Index Files First?",
                        f"Found {len(folder_files)} file(s) in the destination folder that haven't been indexed.\n\n"
                        "Would you like to index them now? This is required before organizing.",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes
                    )
                    
                    if reply == QMessageBox.Yes:
                        # Index the folder
                        self._index_folder_before_organize(self.destination_path)
                        return  # The indexing will call generate_plan again when done
                    else:
                        return
            
            QMessageBox.warning(
                self, "No Files",
                "No indexed files found. Please go to Index Files and index some files first."
            )
            return
        
        # Verify file paths and fix any that have been moved
        self.status_label.setText("Verifying file paths...")
        original_count = len(files)
        files = self._verify_and_fix_paths(files)
        
        # Update files_by_id with verified files only
        self.files_by_id = {f["id"]: f for f in files}
        
        if not files:
            QMessageBox.warning(
                self, "No Valid Files",
                f"All {original_count} indexed files have been moved or deleted.\n\n"
                "Please re-index the folder to update the file list."
            )
            return
        
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.generate_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status_label.setText(f"Asking AI to organize {len(files)} files...")
        self.plan_tree.clear()
        self.details_text.clear()
        
        self.plan_worker = PlanWorker(instruction, files)
        self.plan_worker.finished.connect(self._on_plan_received)
        self.plan_worker.error.connect(self._on_plan_error)
        self.plan_worker.start()
    
    def _index_folder_before_organize(self, folder_path: Path):
        """Index a folder before organizing, then continue with organization."""
        from app.core.search import SearchService
        
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.generate_button.setEnabled(False)
        self.status_label.setText(f"Indexing files in {folder_path.name}...")
        
        # Create a worker thread for indexing
        self._index_worker = IndexBeforeOrganizeWorker(folder_path)
        self._index_worker.progress.connect(self._on_index_progress)
        self._index_worker.finished.connect(self._on_index_before_organize_finished)
        self._index_worker.error.connect(self._on_index_error)
        self._index_worker.start()
    
    def _on_index_progress(self, current: int, total: int, message: str):
        """Handle indexing progress updates."""
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        self.status_label.setText(message)
    
    def _on_index_before_organize_finished(self, stats: dict):
        """Handle indexing completion, then continue with organization."""
        indexed_count = stats.get('indexed_files', 0)
        self.progress_bar.setVisible(False)
        self.generate_button.setEnabled(True)
        
        if indexed_count > 0:
            self.status_label.setText(f"Indexed {indexed_count} files. Generating organization plan...")
            # Now call generate_plan again - files should be available
            QTimer.singleShot(100, self.generate_plan)
        else:
            self.status_label.setText("No files were indexed.")
            QMessageBox.warning(
                self, "Indexing Failed",
                "Could not index any files from the folder.\n"
                "Please check that the folder contains supported file types."
            )
    
    def _on_index_error(self, error: str):
        """Handle indexing errors."""
        self.progress_bar.setVisible(False)
        self.generate_button.setEnabled(True)
        self.status_label.setText(f"Indexing error: {error}")
        logger.error(f"Index before organize error: {error}")

    def _on_plan_received(self, plan: Optional[Dict[str, Any]]):
        """Handle LLM plan response."""
        self.progress_bar.setVisible(False)
        self.generate_button.setEnabled(True)
        
        if not plan:
            self.status_label.setText("Failed to generate plan. Check AI settings.")
            QMessageBox.warning(
                self, "Plan Failed",
                "Could not generate organization plan. Make sure your AI provider is configured in Settings."
            )
            return
        
        # Deduplicate file IDs (AI sometimes puts same file in multiple folders)
        plan = deduplicate_plan(plan)
        
        valid_ids = set(self.files_by_id.keys())
        
        # GRACEFUL RECOVERY: Filter out invalid file IDs from the plan
        # This prevents "Unknown file_id" errors if AI hallucinates IDs
        if "folders" in plan:
            cleaned_folders = {}
            for folder_name, file_ids in plan["folders"].items():
                valid_file_ids = []
                for fid in file_ids:
                    # Handle both int and string IDs
                    try:
                        fid_int = int(fid)
                        if fid_int in valid_ids:
                            valid_file_ids.append(fid_int)
                    except (ValueError, TypeError):
                        continue
                
                if valid_file_ids:
                    cleaned_folders[folder_name] = valid_file_ids
            
            plan["folders"] = cleaned_folders

        # Only ensure ALL files are included in AUTO-ORGANIZE mode
        # For specific instructions (e.g., "move screenshots to X"), we want to leave other files untouched
        is_auto_organize = self.original_instruction and self.original_instruction.startswith("[AUTO-ORGANIZE]")
        if is_auto_organize:
            files_list = list(self.files_by_id.values())
            plan = ensure_all_files_included(plan, valid_ids, files_list)
        
        is_valid, errors = validate_plan(plan, valid_ids)
        
        if not is_valid:
            error_text = "\n".join(errors[:10])
            if len(errors) > 10:
                error_text += f"\n... and {len(errors) - 10} more errors"
            
            # Log what the AI actually returned for debugging
            logger.warning(f"Invalid plan from AI: {plan}")
            
            self.status_label.setText("Plan validation failed")
            
            # Build detailed error display
            details = f"Validation Errors:\n{'='*40}\n\n{error_text}\n\n"
            details += f"{'='*40}\nAI Response (for debugging):\n"
            details += json.dumps(plan, indent=2, default=str)[:1000]  # Limit length
            self.details_text.setPlainText(details)
            
            # More helpful error message
            first_error = errors[0] if errors else "Unknown error"
            if "folders" in first_error.lower():
                msg = (
                    f"The AI returned an invalid response format.\n\n"
                    f"Error: {first_error}\n\n"
                    "Try rephrasing your instruction to be more specific about what you want to organize."
                )
            else:
                msg = (
                    f"The AI plan failed validation:\n\n{first_error}\n\n"
                    "This can happen if the AI invented file IDs or proposed invalid folders."
                )
            
            QMessageBox.warning(self, "Invalid Plan", msg)
            return
        
        self.current_plan = plan
        self.current_moves = plan_to_moves(plan, self.files_by_id, self.destination_path)
        
        self._display_plan(plan)
        
        # Check for folders that already exist in destination
        existing_folders = []
        if self.destination_path and self.destination_path.exists():
            for folder_name in plan.get("folders", {}).keys():
                proposed_path = self.destination_path / folder_name
                if proposed_path.exists():
                    existing_folders.append(folder_name)
        
        if existing_folders:
            folder_list = ", ".join(existing_folders[:3])
            if len(existing_folders) > 3:
                folder_list += f" and {len(existing_folders) - 3} more"
            self.details_text.append(f"\n" + "="*50 + f"\nNote: {len(existing_folders)} folder(s) already exist: {folder_list}\nFiles will be added to existing folders.")
        
        folder_count = len(plan.get("folders", {}))
        files_in_plan = sum(len(fids) for fids in plan.get("folders", {}).values())
        valid_moves = len(self.current_moves)
        
        logger.info(f"Plan has {files_in_plan} files, {valid_moves} valid moves possible")
        
        if valid_moves == 0 and files_in_plan > 0:
            self.status_label.setText(f"Plan has {files_in_plan} files but none can be moved")
            self.apply_button.setEnabled(False)
            QMessageBox.warning(
                self, "No Files to Move",
                f"The AI proposed organizing {files_in_plan} files, but none need to be moved.\n\n"
                "Possible reasons:\n"
                "• Files are already in the destination folder\n"
                "• Files were already moved or deleted\n"
                "• Files no longer exist at their indexed paths\n\n"
                "If files have been moved, please re-index to update the database."
            )
        elif valid_moves < files_in_plan:
            self.status_label.setText(f"Plan ready: {valid_moves}/{files_in_plan} files can be moved to {folder_count} folders")
            self.apply_button.setEnabled(valid_moves > 0)
        else:
            self.status_label.setText(f"Plan ready: {valid_moves} files to {folder_count} folders")
            self.apply_button.setEnabled(valid_moves > 0)
        
        # Show refinement section and other elements if we have a valid plan
        if folder_count > 0 or files_in_plan > 0:
            self.feedback_group.setVisible(True)
            self.feedback_input.clear()
            # Show the results section and action buttons
            self.results_splitter.setVisible(True)
            self.apply_button.setVisible(True)
            self.clear_button.setVisible(True)
            self.undo_button.setVisible(True)
    
    def _on_plan_error(self, error: str):
        """Handle planning error."""
        self.progress_bar.setVisible(False)
        self.generate_button.setEnabled(True)
        self.status_label.setText(f"Error: {error}")
        logger.error(f"Plan generation error: {error}")

    def _display_plan(self, plan: Dict[str, Any]):
        """Show the organization plan in the tree widget."""
        self.plan_tree.clear()
        
        folders = plan.get("folders", {})
        
        for folder_name, file_ids in sorted(folders.items()):
            folder_item = QTreeWidgetItem([f"{folder_name}", str(len(file_ids))])
            folder_item.setExpanded(True)
            folder_item.setData(0, Qt.UserRole, {"type": "folder", "name": folder_name})
            
            display_limit = 25
            for i, fid in enumerate(file_ids[:display_limit]):
                try:
                    fid_int = int(fid)
                    file_info = self.files_by_id.get(fid_int, {})
                    fname = file_info.get("file_name", f"id:{fid}")
                    file_item = QTreeWidgetItem([f"    {fname}", ""])
                    file_item.setData(0, Qt.UserRole, {"type": "file", "id": fid_int})
                    folder_item.addChild(file_item)
                except:
                    pass
            
            if len(file_ids) > display_limit:
                more_item = QTreeWidgetItem([f"    ... and {len(file_ids) - display_limit} more files", ""])
                more_item.setDisabled(True)
                folder_item.addChild(more_item)
            
            self.plan_tree.addTopLevelItem(folder_item)
        
        summary = get_plan_summary(plan, self.files_by_id)
        
        details = f"""Organization Plan Summary
{'='*50}

Destination: {self.destination_path}
Total folders: {summary["total_folders"]}
Total files to move: {summary["total_files"]}
Total size: {summary["total_size_mb"]} MB

Folders:
{'-'*50}
"""
        for folder in summary["folders"]:
            details += f"{folder['name']}: {folder['file_count']} files ({folder['size_mb']} MB)\n"
        
        self.details_text.setPlainText(details)
    
    def _on_tree_item_clicked(self, item: QTreeWidgetItem, column: int):
        """Handle tree item click to show details."""
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        
        if data.get("type") == "file":
            fid = data.get("id")
            file_info = self.files_by_id.get(fid, {})
            
            details = f"""File Details
{'='*50}

ID: {fid}
Name: {file_info.get('file_name', 'unknown')}
Path: {file_info.get('file_path', 'unknown')}
Size: {round(file_info.get('file_size', 0) / 1024, 2)} KB
Label: {file_info.get('label', 'none')}
Tags: {', '.join(file_info.get('tags', [])) or 'none'}
Caption: {file_info.get('caption', 'none')}
"""
            self.details_text.setPlainText(details)

    def apply_organization(self):
        """Execute the organization plan after user confirmation."""
        logger.info(f"apply_organization called. current_moves count: {len(self.current_moves)}")
        
        if not self.current_moves:
            logger.warning("Apply clicked but current_moves is empty")
            QMessageBox.warning(
                self, "No Files to Move",
                "No files can be moved.\n\n"
                "This usually happens when:\n"
                "Files have already been moved/deleted\n"
                "Files no longer exist at their indexed paths\n\n"
                "Try re-indexing your files in Index Files first."
            )
            return
        
        folder_count = len(self.current_plan.get("folders", {}))
        file_count = len(self.current_moves)
        
        reply = QMessageBox.question(
            self,
            "Confirm Organization",
            f"Move {file_count} files into {folder_count} folders?\n\n"
            f"Destination: {self.destination_path}\n\n"
            "This will physically move the files.\n"
            "A log will be saved for reference.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        move_plan = []
        for m in self.current_moves:
            move_plan.append({
                "source_path": m["source_path"],
                "destination_path": m["destination_path"],
                "file_name": m["file_name"],
                "size": m["size"],
                "category": m["destination_folder"],
            })
        
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(move_plan))
        self.apply_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.status_label.setText("Moving files...")
        
        success, errors, log_file = apply_moves(move_plan)
        
        self.progress_bar.setVisible(False)
        self.generate_button.setEnabled(True)
        
        if success:
            # Save undo information BEFORE updating database paths
            self.last_organization = []
            for m in self.current_moves:
                self.last_organization.append({
                    "source": m["source_path"],
                    "destination": m["destination_path"],
                    "file_id": m["file_id"],
                })
            self.undo_button.setEnabled(True)
            logger.info(f"Saved {len(self.last_organization)} moves for potential undo")
            
            paths_updated = 0
            for m in self.current_moves:
                if file_index.update_file_path(m["file_id"], m["destination_path"]):
                    paths_updated += 1
            
            logger.info(f"Updated {paths_updated}/{len(self.current_moves)} file paths in database")
            
            # Scan entire destination folder for empty folders (not just source folders)
            empty_folders = self._scan_all_empty_folders()
            
            cleanup_msg = ""
            if empty_folders:
                # Show dialog for user to choose which folders to delete
                removed_count = self._show_empty_folder_dialog(empty_folders)
                if removed_count > 0:
                    cleanup_msg = f"\n\nDeleted {removed_count} empty folder(s)."
            
            QMessageBox.information(
                self, "Success",
                f"Successfully organized {len(move_plan)} files!\n\n"
                f"File paths updated in database (no re-indexing needed).\n\n"
                f"You can use Undo Last to reverse this if needed.{cleanup_msg}\n\n"
                f"Log saved to:\n{log_file}"
            )
            self.status_label.setText("Organization complete! (Undo available)")
            self.clear_plan()
            self._update_file_count()
        else:
            paths_updated = 0
            for m in self.current_moves:
                dest_path = Path(m["destination_path"])
                if dest_path.exists():
                    if file_index.update_file_path(m["file_id"], m["destination_path"]):
                        paths_updated += 1
            
            logger.info(f"Partial success: Updated {paths_updated} file paths in database")
            
            error_text = "\n".join(errors[:5])
            if len(errors) > 5:
                error_text += f"\n... and {len(errors) - 5} more errors"
            
            QMessageBox.warning(
                self, "Partial Failure",
                f"Some files could not be moved:\n\n{error_text}\n\n"
                f"({paths_updated} files were moved and their paths updated)"
            )
            self.status_label.setText(f"Completed with {len(errors)} errors")
            self.apply_button.setEnabled(True)

    def clear_plan(self):
        """Clear the current plan and reset UI."""
        self.current_plan = None
        self.current_moves = []
        self.original_instruction = None
        self.plan_tree.clear()
        self.details_text.clear()
        self.apply_button.setEnabled(False)
        self.feedback_group.setVisible(False)
        self.feedback_input.clear()
        
        # Hide the results section and action buttons (progressive disclosure)
        self.results_splitter.setVisible(False)
        self.apply_button.setVisible(False)
        self.clear_button.setVisible(False)
        self.undo_button.setVisible(False)
        
        self._update_file_count()
    
    def refine_plan(self):
        """Refine the current plan based on user feedback."""
        feedback = self.feedback_input.text().strip()
        if not feedback:
            return
        
        if not self.current_plan or not self.original_instruction:
            QMessageBox.warning(
                self, "No Plan to Refine",
                "Generate a plan first before refining."
            )
            return
        
        # Build refinement prompt
        from app.core.ai_organizer import request_plan_refinement
        
        files = self._load_files_from_db()
        if not files:
            return
        
        # Show progress
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.generate_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.refine_button.setEnabled(False)
        self.status_label.setText(f"Refining plan based on feedback...")
        
        # Run refinement in background
        self.plan_worker = RefineWorker(
            self.original_instruction,
            self.current_plan,
            feedback,
            files
        )
        self.plan_worker.finished.connect(self._on_plan_received)
        self.plan_worker.error.connect(self._on_plan_error)
        self.plan_worker.finished.connect(lambda _: self.refine_button.setEnabled(True))
        self.plan_worker.error.connect(lambda _: self.refine_button.setEnabled(True))
        self.plan_worker.start()
    
    def undo_last_organization(self):
        """Undo the last organization by moving files back to their original locations."""
        if not self.last_organization:
            QMessageBox.information(
                self, "Nothing to Undo",
                "There is no previous organization to undo."
            )
            return
        
        can_undo = []
        cannot_undo = []
        
        for move in self.last_organization:
            dest_path = Path(move["destination"])
            source_path = Path(move["source"])
            
            if not dest_path.exists():
                cannot_undo.append(f"File not found: {dest_path.name}")
            elif source_path.exists():
                cannot_undo.append(f"Original location occupied: {source_path.name}")
            else:
                can_undo.append(move)
        
        if not can_undo:
            QMessageBox.warning(
                self, "Cannot Undo",
                f"Cannot undo the last organization:\n\n" +
                "\n".join(cannot_undo[:5]) +
                (f"\n... and {len(cannot_undo) - 5} more issues" if len(cannot_undo) > 5 else "")
            )
            return
        
        warning_text = ""
        if cannot_undo:
            warning_text = f"\n\n{len(cannot_undo)} files cannot be undone (modified or moved)."
        
        reply = QMessageBox.question(
            self,
            "Confirm Undo",
            f"Move {len(can_undo)} files back to their original locations?{warning_text}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        import shutil
        success_count = 0
        errors = []
        
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(can_undo))
        self.status_label.setText("Undoing organization...")
        
        for i, move in enumerate(can_undo):
            self.progress_bar.setValue(i + 1)
            try:
                dest_path = Path(move["destination"])
                source_path = Path(move["source"])
                
                source_path.parent.mkdir(parents=True, exist_ok=True)
                
                shutil.move(str(dest_path), str(source_path))
                
                file_index.update_file_path(move["file_id"], str(source_path))
                
                success_count += 1
                logger.info(f"Undo: {dest_path} -> {source_path}")
            except Exception as e:
                errors.append(f"{dest_path.name}: {e}")
                logger.error(f"Undo failed for {move['destination']}: {e}")
        
        self.progress_bar.setVisible(False)
        
        self._cleanup_empty_folders()
        
        self.last_organization = None
        self.undo_button.setEnabled(False)
        
        if errors:
            QMessageBox.warning(
                self, "Partial Undo",
                f"Restored {success_count} files.\n\n"
                f"{len(errors)} files could not be restored:\n" +
                "\n".join(errors[:3])
            )
            self.status_label.setText(f"Undo partial: {success_count} restored, {len(errors)} failed")
        else:
            QMessageBox.information(
                self, "Undo Complete",
                f"Successfully restored {success_count} files to their original locations!"
            )
            self.status_label.setText(f"Undo complete: {success_count} files restored")
        
        self._update_file_count()
    
    def _collect_empty_folders(self, source_folders: set) -> list:
        """
        Collect empty source folders after moving files out (does NOT delete).
        
        Safety rules:
        - Only checks folders from the provided set (where files came from)
        - Only includes if completely empty (no files, no subfolders)
        - Walks bottom-up (deepest folders first)
        - Never includes the destination path itself
        - Recursively checks parent folders up the tree (with depth limit)
        - Returns list of empty folder paths for user review
        """
        empty_folders = []
        already_checked = set()
        
        if not source_folders:
            logger.debug("No source folders to check for emptiness")
            return empty_folders
        
        logger.info(f"Checking {len(source_folders)} source folders for emptiness")
        
        # Sort by depth (deepest first) to handle nested empty folders
        sorted_folders = sorted(source_folders, key=lambda p: len(p.parts), reverse=True)
        
        # Track the minimum depth we should check (don't go too far up)
        min_depths = {}
        for folder in sorted_folders:
            # Allow checking up to 3 levels above the source folder
            min_depths[folder] = max(1, len(folder.parts) - 3)
        
        def check_folder_and_parents(folder: Path, min_depth: int):
            """Recursively check folder and its parents if empty."""
            if folder in already_checked:
                return
            already_checked.add(folder)
            
            # Safety: never include destination path
            if self.destination_path and folder.resolve() == self.destination_path.resolve():
                logger.debug(f"Skipping destination folder: {folder}")
                return
            
            # Safety: don't go above min depth (prevents deleting too far up the tree)
            if len(folder.parts) < min_depth:
                logger.debug(f"Reached min depth, stopping at: {folder}")
                return
            
            # Safety: don't delete drive roots or very short paths
            if len(folder.parts) <= 2:
                logger.debug(f"Too close to root, skipping: {folder}")
                return
            
            # Safety: must exist and be a directory
            if not folder.exists() or not folder.is_dir():
                logger.debug(f"Folder doesn't exist or not a dir: {folder}")
                return
            
            try:
                # Check if completely empty (no files, no subdirs)
                contents = list(folder.iterdir())
                if not contents:
                    empty_folders.append(str(folder))
                    logger.info(f"Found empty source folder: {folder}")
                    
                    # Recursively check parent
                    check_folder_and_parents(folder.parent, min_depth)
                else:
                    logger.debug(f"Folder not empty ({len(contents)} items): {folder}")
            except OSError as e:
                logger.debug(f"Could not check folder {folder}: {e}")
            except Exception as e:
                logger.warning(f"Error checking folder {folder}: {e}")
        
        for folder in sorted_folders:
            min_depth = min_depths.get(folder, 1)
            logger.debug(f"Checking folder: {folder} (min_depth={min_depth})")
            check_folder_and_parents(folder, min_depth)
        
        logger.info(f"Found {len(empty_folders)} empty folders total")
        return empty_folders
    
    def _scan_all_empty_folders(self) -> list:
        """
        Scan the entire destination folder for empty folders.
        
        This finds ALL empty folders, not just source folders of moved files.
        Returns a list of empty folder paths sorted by depth (deepest first).
        """
        if not self.destination_path or not self.destination_path.exists():
            logger.debug("No destination path set for empty folder scan")
            return []
        
        empty_folders = []
        
        logger.info(f"Scanning entire destination for empty folders: {self.destination_path}")
        
        try:
            # Walk bottom-up (topdown=False) to find empty folders
            for dirpath, dirnames, filenames in os.walk(str(self.destination_path), topdown=False):
                folder = Path(dirpath)
                
                # Skip the destination path itself
                if folder.resolve() == self.destination_path.resolve():
                    continue
                
                # Safety: don't check paths too close to root
                if len(folder.parts) <= 2:
                    continue
                
                try:
                    # Check if folder is completely empty
                    contents = list(folder.iterdir())
                    if not contents:
                        empty_folders.append(str(folder))
                        logger.info(f"Found empty folder: {folder}")
                except OSError as e:
                    logger.debug(f"Could not check folder {folder}: {e}")
                except Exception as e:
                    logger.warning(f"Error checking folder {folder}: {e}")
        
        except Exception as e:
            logger.error(f"Error scanning destination folder: {e}")
        
        # Sort by depth (deepest first) for proper deletion order
        empty_folders.sort(key=lambda p: len(Path(p).parts), reverse=True)
        
        logger.info(f"Scan complete: found {len(empty_folders)} empty folders")
        return empty_folders
    
    def _show_empty_folder_dialog(self, empty_folders: list) -> int:
        """
        Show dialog for user to choose which empty folders to delete.
        Returns the number of folders actually deleted.
        """
        if not empty_folders:
            return 0
        
        dialog = EmptyFolderDialog(empty_folders, self)
        result = dialog.exec()
        
        if result == QDialog.Accepted:
            folders_to_delete = dialog.get_folders_to_delete()
            if folders_to_delete:
                return self._delete_folders(folders_to_delete)
        
        return 0
    
    def _delete_folders(self, folder_paths: list) -> int:
        """
        Delete the specified folders. Returns count of successfully deleted folders.
        Deletes in order (deepest first to handle nested folders).
        """
        deleted_count = 0
        
        # Sort by depth (deepest first)
        sorted_paths = sorted(folder_paths, key=lambda p: len(Path(p).parts), reverse=True)
        
        for folder_path in sorted_paths:
            try:
                folder = Path(folder_path)
                if folder.exists() and folder.is_dir():
                    # Double-check it's still empty
                    contents = list(folder.iterdir())
                    if not contents:
                        folder.rmdir()
                        deleted_count += 1
                        logger.info(f"Deleted empty folder: {folder}")
                    else:
                        logger.warning(f"Folder no longer empty, skipping: {folder}")
            except OSError as e:
                logger.warning(f"Could not delete folder {folder_path}: {e}")
            except Exception as e:
                logger.error(f"Error deleting folder {folder_path}: {e}")
        
        return deleted_count
    
    def _cleanup_empty_folders(self):
        """Remove empty folders left after undo (only in destination path)."""
        if not self.destination_path or not self.destination_path.exists():
            return
        
        try:
            for dirpath, dirnames, filenames in os.walk(str(self.destination_path), topdown=False):
                if not filenames and not dirnames:
                    try:
                        Path(dirpath).rmdir()
                        logger.info(f"Removed empty folder: {dirpath}")
                    except OSError:
                        pass
        except Exception as e:
            logger.warning(f"Error cleaning up folders: {e}")
