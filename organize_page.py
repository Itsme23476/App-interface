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
    QProgressBar, QMessageBox, QFileDialog, QGroupBox, QApplication,
    QSplitter, QFrame, QSizePolicy, QScrollArea,
    QDialog, QListWidget, QListWidgetItem, QCheckBox
)
from PySide6.QtCore import Qt, QThread, Signal

from app.core.database import file_index
from app.core.ai_organizer import (
    request_organization_plan, validate_plan, plan_to_moves, get_plan_summary,
    deduplicate_plan
)
from app.core.apply import apply_moves
from app.core.settings import settings

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
    """Dialog for configuring Watch & Auto-Organize folders with per-folder instructions."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Watch & Auto-Organize Settings")
        self.setMinimumWidth(600)
        self.setMinimumHeight(450)
        self.setModal(True)
        
        # Store folder data: {path: instruction}
        self.folder_data = {}
        self.folder_widgets = {}  # {path: {frame, instruction_input}}
        
        self._setup_ui()
        self._load_from_settings()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)
        
        # Header
        header = QLabel("Configure folders to watch for new files")
        header.setStyleSheet("font-size: 14px; color: #666;")
        layout.addWidget(header)
        
        # Folders container with scroll
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(250)
        scroll.setStyleSheet("""
            QScrollArea {
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                background-color: #fafafa;
            }
        """)
        
        self.folders_container = QWidget()
        self.folders_layout = QVBoxLayout(self.folders_container)
        self.folders_layout.setSpacing(12)
        self.folders_layout.setContentsMargins(12, 12, 12, 12)
        self.folders_layout.addStretch()
        
        scroll.setWidget(self.folders_container)
        layout.addWidget(scroll)
        
        # Placeholder for empty state
        self.empty_label = QLabel("No folders added yet.\nClick '+ Add Folder' to start monitoring a folder.")
        self.empty_label.setStyleSheet("color: #999; font-size: 13px;")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.folders_layout.insertWidget(0, self.empty_label)
        
        # Add folder button
        add_btn = QPushButton("+ Add Folder")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setStyleSheet("""
            QPushButton {
                background-color: white;
                border: 2px dashed #7c3aed;
                border-radius: 8px;
                padding: 12px 24px;
                color: #7c3aed;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #f5f0ff;
                border-style: solid;
            }
        """)
        add_btn.clicked.connect(self._add_folder)
        layout.addWidget(add_btn)
        
        # Auto-start checkbox
        self.auto_start_checkbox = QCheckBox("Auto-start when app opens")
        self.auto_start_checkbox.setChecked(settings.auto_organize_auto_start)
        self.auto_start_checkbox.setStyleSheet("font-size: 13px; color: #333;")
        layout.addWidget(self.auto_start_checkbox)
        
        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f5f5f5;
                border: 1px solid #ddd;
                border-radius: 6px;
                padding: 10px 24px;
                color: #666;
                font-weight: 500;
            }
            QPushButton:hover { background-color: #eee; }
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("Save & Close")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #7c3aed;
                border: none;
                border-radius: 6px;
                padding: 10px 24px;
                color: white;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #6d28d9; }
        """)
        save_btn.clicked.connect(self._save_and_close)
        btn_layout.addWidget(save_btn)
        
        layout.addLayout(btn_layout)
    
    def _load_from_settings(self):
        """Load folders from settings."""
        for folder_info in settings.auto_organize_folders:
            path = folder_info.get('path', '')
            instruction = folder_info.get('instruction', '')
            if path and os.path.isdir(path):
                self._create_folder_widget(path, instruction)
        self._update_empty_state()
    
    def _add_folder(self):
        """Add a new folder to watch."""
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Folder to Watch", str(Path.home())
        )
        if folder and folder not in self.folder_data:
            self._create_folder_widget(folder, "")
            self._update_empty_state()
    
    def _create_folder_widget(self, folder_path: str, instruction: str):
        """Create a folder configuration card."""
        self.folder_data[folder_path] = instruction
        
        frame = QFrame()
        frame.setStyleSheet("""
            QFrame {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 10px;
            }
        """)
        
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(16, 12, 16, 12)
        frame_layout.setSpacing(8)
        
        # Top row: folder path + remove button
        top_row = QHBoxLayout()
        
        folder_icon = QLabel("📁")
        folder_icon.setStyleSheet("font-size: 16px; border: none;")
        top_row.addWidget(folder_icon)
        
        path_label = QLabel(folder_path)
        path_label.setStyleSheet("font-weight: bold; color: #333; font-size: 12px; border: none;")
        path_label.setWordWrap(True)
        top_row.addWidget(path_label, 1)
        
        remove_btn = QPushButton("🗑️")
        remove_btn.setFixedSize(32, 32)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #fee2e2; border-radius: 6px; }
        """)
        remove_btn.clicked.connect(lambda: self._remove_folder(folder_path))
        top_row.addWidget(remove_btn)
        
        frame_layout.addLayout(top_row)
        
        # Instruction input
        instruction_label = QLabel("AI Instruction:")
        instruction_label.setStyleSheet("color: #666; font-size: 11px; border: none;")
        frame_layout.addWidget(instruction_label)
        
        instruction_input = QLineEdit()
        instruction_input.setPlaceholderText("e.g., Organize by file type, put screenshots in Screenshots folder...")
        instruction_input.setText(instruction)
        instruction_input.setStyleSheet("""
            QLineEdit {
                background-color: #f9f9f9;
                border: 1px solid #e0e0e0;
                border-radius: 6px;
                padding: 10px 12px;
                font-size: 12px;
                color: #333;
            }
            QLineEdit:focus {
                border-color: #7c3aed;
                background-color: white;
            }
        """)
        instruction_input.textChanged.connect(
            lambda text, fp=folder_path: self._on_instruction_changed(fp, text)
        )
        frame_layout.addWidget(instruction_input)
        
        # Store widget reference
        self.folder_widgets[folder_path] = {
            'frame': frame,
            'instruction_input': instruction_input
        }
        
        # Insert before the stretch
        self.folders_layout.insertWidget(self.folders_layout.count() - 1, frame)
    
    def _remove_folder(self, folder_path: str):
        """Remove a folder from the list."""
        if folder_path in self.folder_widgets:
            widget_data = self.folder_widgets.pop(folder_path)
            widget_data['frame'].deleteLater()
        if folder_path in self.folder_data:
            del self.folder_data[folder_path]
        self._update_empty_state()
    
    def _on_instruction_changed(self, folder_path: str, text: str):
        """Update instruction when text changes."""
        self.folder_data[folder_path] = text
    
    def _update_empty_state(self):
        """Show/hide empty state message."""
        self.empty_label.setVisible(len(self.folder_widgets) == 0)
    
    def _save_and_close(self):
        """Save settings and close dialog."""
        # Clear existing and save new
        settings.auto_organize_folders = []
        for path, instruction in self.folder_data.items():
            settings.add_auto_organize_folder(path, instruction)
        
        # Save auto-start preference
        settings.set_auto_organize_auto_start(self.auto_start_checkbox.isChecked())
        
        self.accept()
    
    def get_folder_data(self) -> dict:
        """Return the folder configuration."""
        return self.folder_data.copy()


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
        self.setup_ui()
    
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
        header.setObjectName("pageHeader")
        header.setStyleSheet("font-size: 28px; font-weight: bold;")
        layout.addWidget(header)
        
        subtitle = QLabel(
            "Describe how you want your files organized in plain English. "
            "AI will analyze your indexed files and propose an organization plan."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setStyleSheet("color: #888; font-size: 14px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        
        # Instruction Input
        instruction_group = QGroupBox("Your Instruction")
        instruction_layout = QVBoxLayout(instruction_group)
        
        # Input row with text field and mic button
        input_row = QHBoxLayout()
        
        self.instruction_input = QLineEdit()
        self.instruction_input.setPlaceholderText(
            "e.g., Organize thumbnails by client name or Sort invoices by year"
        )
        self.instruction_input.setMinimumHeight(44)
        self.instruction_input.setStyleSheet("font-size: 14px; padding: 8px 12px;")
        self.instruction_input.textChanged.connect(self._update_generate_button)
        self.instruction_input.returnPressed.connect(self.generate_plan)
        input_row.addWidget(self.instruction_input)
        
        # Microphone button for voice input
        self.mic_button = QPushButton("🎤")
        self.mic_button.setMinimumHeight(44)
        self.mic_button.setMinimumWidth(50)
        self.mic_button.setMaximumWidth(50)
        self.mic_button.setToolTip("Click to speak your instruction (click again to stop)")
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
        self.mic_button.clicked.connect(self._toggle_voice_recording)
        input_row.addWidget(self.mic_button)
        
        instruction_layout.addLayout(input_row)
        
        # Voice recording state
        self.voice_worker = None
        self.is_recording_voice = False
        
        examples_label = QLabel(
            "Examples: Organize by file type, Group photos by date, "
            "Only files from projects folder ignore temp, Sort by topic"
        )
        examples_label.setStyleSheet("color: #666; font-size: 11px; font-style: italic;")
        examples_label.setWordWrap(True)
        instruction_layout.addWidget(examples_label)
        
        layout.addWidget(instruction_group)
        
        # Destination Folder
        dest_group = QGroupBox("Destination Folder")
        dest_layout = QHBoxLayout(dest_group)
        
        self.dest_label = QLabel("Select where organized files will be moved...")
        self.dest_label.setStyleSheet("color: #888;")
        self.dest_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        dest_layout.addWidget(self.dest_label)
        
        self.dest_button = QPushButton("Choose Folder")
        self.dest_button.setMinimumHeight(36)
        self.dest_button.clicked.connect(self.select_destination)
        dest_layout.addWidget(self.dest_button)
        
        layout.addWidget(dest_group)

        # Action Buttons
        action_layout = QHBoxLayout()
        action_layout.setSpacing(12)
        
        self.generate_button = QPushButton("Generate Plan")
        self.generate_button.setMinimumHeight(44)
        self.generate_button.setMinimumWidth(160)
        self.generate_button.setEnabled(False)
        self.generate_button.setStyleSheet("""
            QPushButton {
                background-color: #4a9eff;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #3a8eef; }
            QPushButton:disabled { background-color: #555; color: #888; }
        """)
        self.generate_button.clicked.connect(self.generate_plan)
        action_layout.addWidget(self.generate_button)
        
        self.apply_button = QPushButton("Apply Organization")
        self.apply_button.setMinimumHeight(44)
        self.apply_button.setMinimumWidth(180)
        self.apply_button.setEnabled(False)
        self.apply_button.setStyleSheet("""
            QPushButton {
                background-color: #2ecc71;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #27ae60; }
            QPushButton:disabled { background-color: #555; color: #888; }
        """)
        self.apply_button.clicked.connect(self.apply_organization)
        action_layout.addWidget(self.apply_button)
        
        self.clear_button = QPushButton("Clear")
        self.clear_button.setMinimumHeight(44)
        self.clear_button.clicked.connect(self.clear_plan)
        action_layout.addWidget(self.clear_button)
        
        self.undo_button = QPushButton("Undo Last")
        self.undo_button.setMinimumHeight(44)
        self.undo_button.setMinimumWidth(120)
        self.undo_button.setEnabled(False)
        self.undo_button.setToolTip("Undo the last organization (move files back)")
        self.undo_button.setStyleSheet("""
            QPushButton {
                background-color: #e67e22;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #d35400; }
            QPushButton:disabled { background-color: #555; color: #888; }
        """)
        self.undo_button.clicked.connect(self.undo_last_organization)
        action_layout.addWidget(self.undo_button)
        
        action_layout.addStretch()
        layout.addLayout(action_layout)
        
        # Progress and Status
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumHeight(8)
        layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888; font-style: italic; font-size: 13px;")
        layout.addWidget(self.status_label)

        # Results Area (Splitter: Tree + Details)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        
        # Left: Plan Tree
        plan_group = QGroupBox("Proposed Organization")
        plan_layout = QVBoxLayout(plan_group)
        plan_layout.setContentsMargins(8, 12, 8, 8)
        
        self.plan_tree = QTreeWidget()
        self.plan_tree.setHeaderLabels(["Folder / File", "Files"])
        self.plan_tree.setColumnWidth(0, 350)
        self.plan_tree.setColumnWidth(1, 60)
        self.plan_tree.setAlternatingRowColors(True)
        self.plan_tree.setStyleSheet("QTreeWidget { font-size: 13px; }")
        self.plan_tree.itemClicked.connect(self._on_tree_item_clicked)
        plan_layout.addWidget(self.plan_tree)
        
        splitter.addWidget(plan_group)
        
        # Right: Details Panel
        details_group = QGroupBox("Details")
        details_layout = QVBoxLayout(details_group)
        details_layout.setContentsMargins(8, 12, 8, 8)
        
        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setStyleSheet("font-size: 12px; font-family: Consolas, Monaco, monospace;")
        details_layout.addWidget(self.details_text)
        
        splitter.addWidget(details_group)
        splitter.setSizes([500, 300])
        
        layout.addWidget(splitter, 1)
        
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
        
        self.refine_button = QPushButton("Refine")
        self.refine_button.setMinimumHeight(36)
        self.refine_button.setMinimumWidth(100)
        self.refine_button.setStyleSheet("""
            QPushButton {
                background-color: #9b59b6;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #8e44ad; }
        """)
        self.refine_button.clicked.connect(self.refine_plan)
        feedback_layout.addWidget(self.refine_button)
        
        layout.addWidget(self.feedback_group)
        
        # ============================================================
        # ADVANCED: Auto-Organize Section (Collapsible)
        # ============================================================
        self._create_auto_organize_section(layout)
        
        # Finalize scroll area
        scroll.setWidget(container)
        main_layout.addWidget(scroll)
        
        # Load initial state
        self._update_file_count()
    
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
    
    def _create_auto_organize_section(self, parent_layout):
        """Create the collapsible Auto-Organize advanced section with AI instructions."""
        from app.core.auto_watcher import auto_watcher
        
        # Store references
        self.auto_watcher = auto_watcher
        self.auto_organize_worker = None
        self.watch_folders = []
        self.is_watching = False
        
        # Collapsible header (clean style matching app theme)
        self.auto_header = QPushButton("▶ More Options")
        self.auto_header.setCheckable(True)
        self.auto_header.setChecked(False)
        self.auto_header.setStyleSheet("""
            QPushButton {
                text-align: center;
                padding: 12px;
                font-size: 14px;
                font-weight: normal;
                background-color: transparent;
                border: none;
                color: #666;
            }
            QPushButton:hover { color: #7c3aed; }
        """)
        self.auto_header.clicked.connect(self._toggle_auto_section)
        parent_layout.addWidget(self.auto_header)
        
        # Collapsible content (clean light theme)
        self.auto_content = QFrame()
        self.auto_content.setStyleSheet("""
            QFrame {
                background-color: transparent;
                border: none;
            }
        """)
        self.auto_content.setVisible(False)
        
        auto_layout = QVBoxLayout(self.auto_content)
        auto_layout.setSpacing(20)
        auto_layout.setContentsMargins(0, 10, 0, 10)
        
        # ================================================================
        # Section 1: Watch & Auto-Organize (Simplified - uses dialog for config)
        # ================================================================
        watch_container = QFrame()
        watch_container.setStyleSheet("""
            QFrame {
                background-color: #f5f0ff;
                border: 2px dashed #c4b5fd;
                border-radius: 16px;
                padding: 20px;
            }
        """)
        watch_layout = QVBoxLayout(watch_container)
        watch_layout.setSpacing(12)
        
        # Title with icon
        watch_title = QLabel("👁 Watch & Auto-Organize")
        watch_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #7c3aed; border: none;")
        watch_layout.addWidget(watch_title, alignment=Qt.AlignCenter)
        
        watch_desc = QLabel("Monitor folders for new files • Auto-index with AI • Auto-organize")
        watch_desc.setWordWrap(True)
        watch_desc.setStyleSheet("color: #888; font-size: 12px; border: none;")
        watch_desc.setAlignment(Qt.AlignCenter)
        watch_layout.addWidget(watch_desc)
        
        # Status summary label
        self.watch_summary_label = QLabel("No folders configured")
        self.watch_summary_label.setStyleSheet("""
            color: #666; 
            font-size: 13px; 
            border: none; 
            background-color: white; 
            padding: 12px 16px; 
            border-radius: 8px;
        """)
        self.watch_summary_label.setAlignment(Qt.AlignCenter)
        watch_layout.addWidget(self.watch_summary_label)
        
        # Button row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        
        # Configure button
        self.watch_configure_btn = QPushButton("⚙️ Configure")
        self.watch_configure_btn.setCursor(Qt.PointingHandCursor)
        self.watch_configure_btn.setStyleSheet("""
            QPushButton {
                background-color: white;
                border: 1px solid #7c3aed;
                border-radius: 8px;
                padding: 10px 20px;
                color: #7c3aed;
                font-weight: 500;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #f5f0ff; }
        """)
        self.watch_configure_btn.clicked.connect(self._open_watch_config)
        btn_layout.addWidget(self.watch_configure_btn)
        
        # Start/Stop button
        self.watch_toggle_btn = QPushButton("▶ Start Watching")
        self.watch_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.watch_toggle_btn.setMinimumWidth(140)
        self.watch_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #6d28d9; }
        """)
        self.watch_toggle_btn.clicked.connect(self._toggle_watch_mode)
        btn_layout.addWidget(self.watch_toggle_btn)
        
        btn_layout.addStretch()
        watch_layout.addLayout(btn_layout)
        
        # Status
        self.watch_status_label = QLabel("Not watching")
        self.watch_status_label.setStyleSheet("color: #888; font-size: 12px; border: none;")
        self.watch_status_label.setAlignment(Qt.AlignCenter)
        watch_layout.addWidget(self.watch_status_label)
        
        # Activity log
        self.watch_activity_log = QListWidget()
        self.watch_activity_log.setMinimumHeight(40)
        self.watch_activity_log.setMaximumHeight(80)
        self.watch_activity_log.setStyleSheet("""
            QListWidget {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                font-size: 11px;
                color: #666;
            }
        """)
        watch_layout.addWidget(self.watch_activity_log)
        
        auto_layout.addWidget(watch_container)
        
        # Initialize folder widgets dict (for compatibility)
        self.watch_folder_widgets = {}
        
        # ================================================================
        # Section 2: Organize Entire PC
        # ================================================================
        pc_container = QFrame()
        pc_container.setStyleSheet("""
            QFrame {
                background-color: #f5f0ff;
                border: 2px dashed #c4b5fd;
                border-radius: 16px;
                padding: 20px;
            }
        """)
        pc_layout = QVBoxLayout(pc_container)
        pc_layout.setSpacing(12)
        
        # Title
        pc_title = QLabel("⚡ Organize Entire PC")
        pc_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #7c3aed; border: none;")
        pc_layout.addWidget(pc_title, alignment=Qt.AlignCenter)
        
        pc_desc = QLabel("Scan Downloads, Desktop, Documents • Index with AI • Organize")
        pc_desc.setWordWrap(True)
        pc_desc.setStyleSheet("color: #888; font-size: 12px; border: none;")
        pc_desc.setAlignment(Qt.AlignCenter)
        pc_layout.addWidget(pc_desc)
        
        # Info label about AI indexing  
        pc_info = QLabel("✓ Files are analyzed by AI before organizing for smarter categorization")
        pc_info.setWordWrap(True)
        pc_info.setStyleSheet("color: #7c3aed; font-size: 11px; border: none; background-color: #f0ebff; padding: 6px; border-radius: 6px;")
        pc_info.setAlignment(Qt.AlignCenter)
        pc_layout.addWidget(pc_info)
        
        # Instructions input
        self.pc_instruction_input = QLineEdit()
        self.pc_instruction_input.setPlaceholderText(
            "Instructions: e.g., Sort by file type and year..."
        )
        self.pc_instruction_input.setStyleSheet("""
            QLineEdit {
                background-color: white;
                border: 1px solid #e0e0e0;
                border-radius: 8px;
                padding: 10px 14px;
                font-size: 13px;
                color: #333;
            }
            QLineEdit:focus { border-color: #7c3aed; }
        """)
        pc_layout.addWidget(self.pc_instruction_input)
        
        # Organize PC button
        self.organize_pc_btn = QPushButton("⚡ Organize Entire PC Now")
        self.organize_pc_btn.setMinimumHeight(44)
        self.organize_pc_btn.setCursor(Qt.PointingHandCursor)
        self.organize_pc_btn.setStyleSheet("""
            QPushButton {
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 22px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #6d28d9; }
            QPushButton:disabled { background-color: #ccc; }
        """)
        self.organize_pc_btn.clicked.connect(self._organize_entire_pc)
        pc_layout.addWidget(self.organize_pc_btn)
        
        # Status
        self.pc_status_label = QLabel("Ready")
        self.pc_status_label.setStyleSheet("color: #888; font-size: 12px; border: none;")
        self.pc_status_label.setAlignment(Qt.AlignCenter)
        pc_layout.addWidget(self.pc_status_label)
        
        # Progress bar
        self.pc_progress = QProgressBar()
        self.pc_progress.setVisible(False)
        self.pc_progress.setStyleSheet("""
            QProgressBar {
                border: none;
                border-radius: 4px;
                background-color: #e0e0e0;
                height: 8px;
            }
            QProgressBar::chunk {
                background-color: #7c3aed;
                border-radius: 4px;
            }
        """)
        pc_layout.addWidget(self.pc_progress)
        
        auto_layout.addWidget(pc_container)
        
        parent_layout.addWidget(self.auto_content)
        
        # Connect watcher signals
        self.auto_watcher.file_organized.connect(self._on_watch_file_organized)
        self.auto_watcher.status_changed.connect(self._on_watch_status)
        self.auto_watcher.error_occurred.connect(self._on_watch_error)
        
        # Load saved folders from settings
        self._load_saved_folders()
        
        # Schedule auto-start check (delay to let UI finish loading)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1000, self._check_auto_start)
    
    def _toggle_auto_section(self, checked: bool):
        """Toggle visibility of auto-organize section."""
        self.auto_content.setVisible(checked)
        self.auto_header.setText("▼ More Options" if checked else "▶ More Options")
    
    def _open_watch_config(self):
        """Open the watch configuration dialog."""
        dialog = WatchConfigDialog(self)
        if dialog.exec() == QDialog.Accepted:
            # Reload folder data from settings after dialog closes
            self._reload_watch_folders()
            self._update_watch_summary()
    
    def _reload_watch_folders(self):
        """Reload watch folders from settings."""
        self.watch_folders = []
        for folder_data in settings.auto_organize_folders:
            folder_path = folder_data.get('path', '')
            if folder_path and os.path.isdir(folder_path):
                self.watch_folders.append(folder_path)
    
    def _update_watch_summary(self):
        """Update the summary label showing configured folders."""
        folder_count = len(settings.auto_organize_folders)
        auto_start = settings.auto_organize_auto_start
        
        if folder_count == 0:
            self.watch_summary_label.setText("No folders configured")
            self.watch_summary_label.setStyleSheet("""
                color: #999; 
                font-size: 13px; 
                border: none; 
                background-color: white; 
                padding: 12px 16px; 
                border-radius: 8px;
            """)
        else:
            auto_text = "• Auto-start enabled" if auto_start else ""
            self.watch_summary_label.setText(f"📁 {folder_count} folder(s) configured {auto_text}")
            self.watch_summary_label.setStyleSheet("""
                color: #7c3aed; 
                font-size: 13px; 
                font-weight: 500;
                border: none; 
                background-color: #f0ebff; 
                padding: 12px 16px; 
                border-radius: 8px;
            """)
    
    def _load_saved_folders(self):
        """Load saved folders from settings."""
        self._reload_watch_folders()
        self._update_watch_summary()
    
    def _check_auto_start(self):
        """Check if we should auto-start the watcher (called after UI loads)."""
        if not settings.auto_organize_auto_start:
            return
        
        if not self.watch_folders:
            return
        
        # Check for catch-up (files added while app was closed)
        last_active = settings.get_auto_organize_last_active_time()
        
        if last_active:
            # Count files modified since last active
            from datetime import datetime
            new_files_count = 0
            for folder in self.watch_folders:
                if not os.path.isdir(folder):
                    continue
                for file_path in Path(folder).rglob('*'):
                    if file_path.is_file():
                        try:
                            mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                            if mtime > last_active:
                                new_files_count += 1
                        except Exception:
                            pass
            
            if new_files_count > 0:
                # Ask user about catch-up
                reply = QMessageBox.question(
                    self, "Organize New Files?",
                    f"Welcome back! 👋\n\n"
                    f"Found {new_files_count} file(s) added since you last used the app.\n\n"
                    f"Do you want to organize them now with your saved instructions?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes
                )
                if reply == QMessageBox.Yes:
                    # Expand the More Options section to show status
                    self.auto_header.setChecked(True)
                    self._toggle_auto_section(True)
                    # Start with catch-up
                    self._start_watch_mode(is_catch_up=True, catch_up_since=last_active)
                    return
        
        # No catch-up needed, just auto-start
        # Expand the More Options section to show status
        self.auto_header.setChecked(True)
        self._toggle_auto_section(True)
        self._start_watch_mode()
    
    def _toggle_watch_mode(self):
        """Toggle watching on/off."""
        if self.is_watching:
            self._stop_watch_mode()
        else:
            self._start_watch_mode()
    
    def _start_watch_mode(self, is_catch_up: bool = False, catch_up_since=None):
        """Start watching folders with AI organization."""
        # Reload folders from settings
        self._reload_watch_folders()
        
        if not self.watch_folders:
            if not is_catch_up:
                QMessageBox.warning(
                    self, "No Folders", 
                    "Please configure folders to watch first.\n\nClick 'Configure' to add folders."
                )
            return
        
        # Build per-folder instructions dict from settings
        folder_instructions = {}
        has_any_instruction = False
        for folder_data in settings.auto_organize_folders:
            folder_path = folder_data.get('path', '')
            instruction = folder_data.get('instruction', '')
            if folder_path:
                # Normalize the path to match how watcher stores folders (backslashes on Windows)
                normalized_path = os.path.normpath(folder_path)
                folder_instructions[normalized_path] = instruction
                if instruction:
                    has_any_instruction = True
        
        if not has_any_instruction and not is_catch_up:
            reply = QMessageBox.question(
                self, "No Instructions",
                "You haven't provided organization instructions for any folder.\n\n"
                "AI will use default categorization (by file type).\n\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply != QMessageBox.Yes:
                return
        
        # Store per-folder instructions for the watcher
        self.auto_watcher.folder_instructions = folder_instructions
        
        # Add folders to watcher FIRST (this populates _known_files)
        for folder in self.watch_folders:
            self.auto_watcher.add_folder(folder)
        
        # Count existing files
        existing_count = 0
        for folder in self.watch_folders:
            existing_count += len(self.auto_watcher._scan_folder_recursive(folder))
        
        # Determine if we should organize existing files
        organize_existing = False
        
        if is_catch_up and catch_up_since:
            # Catch-up mode: organize files modified since last active
            organize_existing = True
            self.auto_watcher.catch_up_since = catch_up_since
        elif existing_count > 0 and has_any_instruction and not is_catch_up:
            # Create custom dialog with three options
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Organize Existing Files?")
            dialog.setText(f"Found {existing_count} file(s) in the folder(s).")
            dialog.setInformativeText(
                "How would you like to handle existing files?\n\n"
                "• Re-organize All: Flatten folders first, then organize fresh\n"
                "  (Best when changing to new instructions)\n\n"
                "• Organize As-Is: Organize files in their current locations\n"
                "  (Keeps existing folder structure)\n\n"
                "• Watch Only: Skip existing, only organize new files"
            )
            
            reorganize_btn = dialog.addButton("Re-organize All", QMessageBox.AcceptRole)
            organize_btn = dialog.addButton("Organize As-Is", QMessageBox.AcceptRole)
            watch_btn = dialog.addButton("Watch Only", QMessageBox.RejectRole)
            dialog.setDefaultButton(reorganize_btn)
            
            dialog.exec()
            clicked = dialog.clickedButton()
            
            if clicked == reorganize_btn:
                # Flatten folders first, then organize
                self.watch_status_label.setText("Flattening folder structure...")
                self.watch_status_label.setStyleSheet("color: #f59e0b;")
                QApplication.processEvents()
                
                total_flattened = 0
                for folder in self.watch_folders:
                    flattened = self.auto_watcher.flatten_folder(folder)
                    total_flattened += flattened
                
                if total_flattened > 0:
                    self.watch_status_label.setText(f"Flattened {total_flattened} files, now organizing...")
                    QApplication.processEvents()
                
                organize_existing = True
            elif clicked == organize_btn:
                organize_existing = True
            else:
                organize_existing = False
        
        self.auto_watcher.start(organize_existing=organize_existing)
        self.is_watching = True
        
        # Update last active timestamp
        settings.update_auto_organize_last_active()
        
        self.watch_toggle_btn.setText("⏹ Stop")
        self.watch_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #ef4444;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #dc2626; }
        """)
        self.watch_status_label.setText(f"✓ Watching {len(self.watch_folders)} folder(s)...")
        self.watch_status_label.setStyleSheet("color: #27ae60;")
    
    def _stop_watch_mode(self):
        """Stop watching folders."""
        self.auto_watcher.stop()
        self.is_watching = False
        
        # Save last active timestamp for catch-up feature
        settings.update_auto_organize_last_active()
        
        self.watch_toggle_btn.setText("▶ Start Watching")
        self.watch_toggle_btn.setStyleSheet("""
            QPushButton {
                background-color: #7c3aed;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #6d28d9; }
        """)
        self.watch_status_label.setText("Not watching")
        self.watch_status_label.setStyleSheet("color: #888;")
    
    def _on_watch_file_organized(self, source: str, dest: str, category: str):
        """Handle file organized by watcher."""
        filename = os.path.basename(source)
        self.watch_activity_log.insertItem(0, f"• {filename} → {category}/")
        while self.watch_activity_log.count() > 10:
            self.watch_activity_log.takeItem(self.watch_activity_log.count() - 1)
    
    def _on_watch_status(self, status: str):
        """Update watch status."""
        self.watch_status_label.setText(status)
    
    def _on_watch_error(self, path: str, error: str):
        """Handle watch error."""
        filename = os.path.basename(path)
        self.watch_activity_log.insertItem(0, f"✗ {filename}: {error}")
    
    def _organize_entire_pc(self):
        """Organize common PC folders using AI."""
        instruction = self.pc_instruction_input.text().strip()
        
        if not instruction:
            reply = QMessageBox.question(
                self, "No Instructions",
                "You haven't provided organization instructions.\n\n"
                "AI will organize files by type (Images, Documents, etc.).\n\n"
                "Continue anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            if reply != QMessageBox.Yes:
                return
        
        # Get common folders
        home = Path.home()
        common_folders = [
            home / "Downloads",
            home / "Desktop",
            home / "Documents",
        ]
        
        existing_folders = [str(f) for f in common_folders if f.exists()]
        
        if not existing_folders:
            QMessageBox.warning(self, "No Folders", "Could not find common folders to organize.")
            return
        
        reply = QMessageBox.question(
            self, "Confirm PC Organization",
            f"This will organize files in:\n\n"
            + "\n".join(f"  • {f}" for f in existing_folders) +
            f"\n\nInstruction: {instruction or '(Default categorization)'}\n\n"
            "Files will be moved into organized subfolders.\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Run organization
        self.organize_pc_btn.setEnabled(False)
        self.pc_progress.setVisible(True)
        self.pc_progress.setRange(0, 0)
        self.pc_status_label.setText("Scanning files...")
        
        # Use a worker thread for this
        self._run_pc_organize(existing_folders, instruction)
    
    def _run_pc_organize(self, folders: list, instruction: str):
        """Run PC organization in background with auto-indexing."""
        from app.core.ai_organizer import request_organization_plan, validate_plan, plan_to_moves, deduplicate_plan
        from app.core.apply import apply_moves
        from app.core.search import search_service
        
        # Collect all files from the folders
        file_paths = []
        for folder in folders:
            folder_path = Path(folder)
            for f in folder_path.iterdir():
                if f.is_file():
                    file_paths.append(str(f))
        
        if not file_paths:
            self.organize_pc_btn.setEnabled(True)
            self.pc_progress.setVisible(False)
            self.pc_status_label.setText("No files found to organize")
            return
        
        # STEP 1: Auto-index all files with AI first
        self.pc_status_label.setText(f"Indexing {len(file_paths)} files with AI...")
        self.pc_progress.setRange(0, len(file_paths))
        
        all_files = []
        for i, file_path in enumerate(file_paths):
            self.pc_progress.setValue(i + 1)
            self.pc_status_label.setText(f"Indexing: {Path(file_path).name}")
            QApplication.processEvents()  # Keep UI responsive
            
            # Index the file with AI
            result = search_service.index_single_file(Path(file_path), force_ai=False)
            
            if result.get('error'):
                logger.warning(f"Failed to index {file_path}: {result['error']}")
                # Still include with basic info
                all_files.append({
                    "id": hash(file_path),
                    "file_path": file_path,
                    "file_name": Path(file_path).name,
                    "file_size": Path(file_path).stat().st_size if Path(file_path).exists() else 0,
                    "tags": [],
                    "caption": "",
                    "label": "",
                    "category": "",
                })
            else:
                # Get indexed record with rich AI metadata
                record = file_index.get_file_by_path(file_path)
                if record:
                    all_files.append({
                        "id": record.get('id', hash(file_path)),
                        "file_path": file_path,
                        "file_name": record.get('file_name', Path(file_path).name),
                        "file_size": record.get('file_size', 0),
                        "extension": record.get('file_extension', Path(file_path).suffix),
                        # Rich AI-generated metadata!
                        "tags": record.get('tags', []),
                        "caption": record.get('caption', ''),
                        "label": record.get('label', ''),
                        "category": record.get('category', ''),
                        "ocr_text": (record.get('ocr_text', '') or '')[:200],
                    })
                else:
                    all_files.append({
                        "id": hash(file_path),
                        "file_path": file_path,
                        "file_name": Path(file_path).name,
                        "file_size": Path(file_path).stat().st_size if Path(file_path).exists() else 0,
                        "tags": [],
                        "caption": "",
                        "label": "",
                        "category": "",
                    })
        
        self.pc_progress.setRange(0, 0)  # Indeterminate mode
        self.pc_status_label.setText(f"Indexed {len(all_files)} files. Asking AI to organize...")
        QApplication.processEvents()
        
        # Use the existing organization flow
        if not instruction:
            instruction = "Organize files by type into folders: Images, Documents, Videos, Audio, Archives, Other"
        
        try:
            plan = request_organization_plan(instruction, all_files)
            
            if not plan:
                raise Exception("AI returned no plan")
            
            # Deduplicate and validate
            plan = deduplicate_plan(plan)
            
            # Build files_by_id
            files_by_id = {f["id"]: f for f in all_files}
            
            # Validate and convert to moves
            is_valid, errors = validate_plan(plan, set(files_by_id.keys()))
            
            if not is_valid:
                raise Exception(f"Invalid plan: {errors[0]}")
            
            # Use first folder as destination
            dest_folder = folders[0]
            moves = plan_to_moves(plan, files_by_id, dest_folder)
            
            if not moves:
                self.pc_status_label.setText("✓ Files are already organized!")
                self.organize_pc_btn.setEnabled(True)
                self.pc_progress.setVisible(False)
                return
            
            self.pc_status_label.setText(f"Moving {len(moves)} files...")
            
            # Execute moves
            move_plan = [{"source_path": m["source_path"], 
                         "destination_path": m["destination_path"],
                         "file_name": m["file_name"]} for m in moves]
            
            success, errors, log_file = apply_moves(move_plan)
            
            # Update database paths for moved files
            for m in moves:
                try:
                    file_index.update_file_path(m["file_id"], m["destination_path"])
                except Exception as e:
                    logger.warning(f"Failed to update DB path: {e}")
            
            self.organize_pc_btn.setEnabled(True)
            self.pc_progress.setVisible(False)
            
            if success:
                self.pc_status_label.setText(f"✓ Organized {len(moves)} files!")
                QMessageBox.information(
                    self, "Organization Complete",
                    f"Successfully organized {len(moves)} files!\n\n"
                    f"Log saved to: {log_file}"
                )
            else:
                self.pc_status_label.setText(f"Completed with errors")
                
        except Exception as e:
            self.organize_pc_btn.setEnabled(True)
            self.pc_progress.setVisible(False)
            self.pc_status_label.setText(f"Error: {str(e)[:50]}")
            logger.error(f"PC organize error: {e}")
            QMessageBox.warning(self, "Organization Error", str(e))

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
        
        # Show refinement section if we have a valid plan
        if folder_count > 0 or files_in_plan > 0:
            self.feedback_group.setVisible(True)
            self.feedback_input.clear()
    
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
