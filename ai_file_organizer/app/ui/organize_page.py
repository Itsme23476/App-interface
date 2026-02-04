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
    QSpacerItem, QStackedWidget, QButtonGroup
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
    """Modern dialog to let user choose which empty folders to delete."""
    
    def __init__(self, empty_folders: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Empty Folders")
        self.setMinimumSize(520, 400)
        self.setModal(True)
        self._drag_pos = None
        
        self.empty_folders = empty_folders
        self.folders_to_delete = []
        
        # Remove default window frame for custom styling
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Main container
        self.container = QFrame(self)
        self.container.setObjectName("emptyFolderContainer")
        self.container.setStyleSheet("""
            QFrame#emptyFolderContainer {
                background-color: #FFFFFF;
                border-radius: 24px;
                border: 1px solid #E0E0E0;
            }
        """)
        
        # Add drop shadow
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setXOffset(0)
        shadow.setYOffset(5)
        shadow.setColor(QColor(0, 0, 0, 50))
        self.container.setGraphicsEffect(shadow)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.addWidget(self.container)
        
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        
        # Header
        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        
        icon_label = QLabel("🗑️")
        icon_label.setStyleSheet("""
            font-size: 24px;
            background-color: #FFF3E0;
            border-radius: 20px;
            border: 2px solid #FFE0B2;
        """)
        icon_label.setFixedSize(48, 48)
        icon_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(icon_label)
        
        title_label = QLabel("Empty Folders Found")
        title_label.setStyleSheet("""
            font-family: "Segoe UI", "SF Pro Display", sans-serif;
            font-size: 20px;
            font-weight: 700;
            color: #1A1A2E;
        """)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        close_btn = QPushButton("X")
        close_btn.setFixedSize(36, 36)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #E53935;
                color: white;
                border: none;
                border-radius: 18px;
                font-size: 20px;
                font-weight: bold;
                font-family: Arial, Helvetica, sans-serif;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #C62828;
            }
        """)
        close_btn.clicked.connect(self.reject)
        header_layout.addWidget(close_btn)
        
        layout.addLayout(header_layout)
        
        # Description
        desc = QLabel("These folders are now empty. Select which ones to delete:")
        desc.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 13px;
            color: #666666;
        """)
        layout.addWidget(desc)
        
        # Divider
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #EEEEEE;")
        layout.addWidget(divider)
        
        # Folder list with checkboxes
        self.folder_list = QListWidget()
        self.folder_list.setStyleSheet("""
            QListWidget {
                border: 1px solid #E8DFFF;
                border-radius: 12px;
                background-color: #FAFAFA;
                padding: 8px;
            }
            QListWidget::item {
                padding: 10px 12px;
                border-radius: 8px;
                font-family: "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QListWidget::item:hover {
                background-color: #F5F0FF;
            }
            QListWidget::item:selected {
                background-color: #E8DFFF;
                color: #7C4DFF;
            }
        """)
        
        for folder_path in empty_folders:
            # Show just the folder name with path hint
            from pathlib import Path
            folder = Path(folder_path)
            display_text = f"📁 {folder.name}"
            
            item = QListWidgetItem()
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)  # Default to checked
            item.setText(display_text)
            item.setToolTip(folder_path)  # Full path on hover
            item.setData(Qt.UserRole, folder_path)
            self.folder_list.addItem(item)
        
        layout.addWidget(self.folder_list, 1)
        
        # Selection buttons row
        selection_layout = QHBoxLayout()
        selection_layout.setSpacing(10)
        
        select_all_btn = QPushButton("Select All")
        select_all_btn.setMinimumHeight(36)
        select_all_btn.setCursor(Qt.PointingHandCursor)
        select_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #F5F5F5;
                color: #666666;
                border: 1px solid #E0E0E0;
                border-radius: 8px;
                font-size: 12px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background-color: #E8DFFF;
                border-color: #7C4DFF;
                color: #7C4DFF;
            }
        """)
        select_all_btn.clicked.connect(self._select_all)
        selection_layout.addWidget(select_all_btn)
        
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.setMinimumHeight(36)
        deselect_all_btn.setCursor(Qt.PointingHandCursor)
        deselect_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #F5F5F5;
                color: #666666;
                border: 1px solid #E0E0E0;
                border-radius: 8px;
                font-size: 12px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background-color: #FFEBEE;
                border-color: #EF9A9A;
                color: #D32F2F;
            }
        """)
        deselect_all_btn.clicked.connect(self._deselect_all)
        selection_layout.addWidget(deselect_all_btn)
        
        selection_layout.addStretch()
        layout.addLayout(selection_layout)
        
        # Action buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        
        keep_all_btn = QPushButton("Keep All")
        keep_all_btn.setMinimumHeight(44)
        keep_all_btn.setCursor(Qt.PointingHandCursor)
        keep_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #666666;
                border: 2px solid #E0E0E0;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
                padding: 8px 24px;
            }
            QPushButton:hover {
                background-color: #F5F5F5;
                border-color: #7C4DFF;
                color: #7C4DFF;
            }
        """)
        keep_all_btn.clicked.connect(self.reject)
        button_layout.addWidget(keep_all_btn)
        
        button_layout.addStretch()
        
        delete_selected_btn = QPushButton("🗑️ Delete Selected")
        delete_selected_btn.setMinimumHeight(44)
        delete_selected_btn.setCursor(Qt.PointingHandCursor)
        delete_selected_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
                padding: 8px 24px;
            }
            QPushButton:hover {
                background-color: #9575FF;
            }
        """)
        delete_selected_btn.clicked.connect(self._delete_selected)
        button_layout.addWidget(delete_selected_btn)
        
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
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()


class ModernConfirmDialog(QDialog):
    """
    Modern styled confirmation dialog matching the app's purple theme.
    Futuristic look with drop shadow, draggable window, and smooth styling.
    """
    
    def __init__(self, parent=None, title: str = "Confirm", message: str = "", 
                 details: list = None, highlight_text: str = "", info_text: str = "",
                 yes_text: str = "Yes", no_text: str = "No"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(500)
        self.setModal(True)
        self.result_accepted = False
        self._drag_pos = None
        
        # Remove default window frame for custom styling
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Main container with rounded corners and shadow
        self.container = QFrame(self)
        self.container.setObjectName("modernDialogContainer")
        self.container.setStyleSheet("""
            QFrame#modernDialogContainer {
                background-color: #FFFFFF;
                border-radius: 24px;
                border: 1px solid #E0E0E0;
            }
        """)
        
        # Add subtle drop shadow effect
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 40))
        self.container.setGraphicsEffect(shadow)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)  # Space for shadow
        main_layout.addWidget(self.container)
        
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(20)
        
        # Header with gradient icon and title
        header_layout = QHBoxLayout()
        header_layout.setSpacing(16)
        
        # Clean icon background
        icon_label = QLabel("✨")
        icon_label.setStyleSheet("""
            font-size: 26px;
            background-color: #F3EEFF;
            border-radius: 22px;
            border: 2px solid #E8DFFF;
        """)
        icon_label.setFixedSize(52, 52)
        icon_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(icon_label)
        
        title_label = QLabel(title)
        title_label.setStyleSheet("""
            font-family: "Segoe UI", "SF Pro Display", sans-serif;
            font-size: 20px;
            font-weight: 700;
            color: #1A1A2E;
            letter-spacing: -0.3px;
        """)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        # Close button - solid purple with white X (ALWAYS visible)
        close_btn = QPushButton("X")
        close_btn.setFixedSize(36, 36)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 18px;
                font-size: 20px;
                font-weight: bold;
                font-family: Arial, Helvetica, sans-serif;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #5E35B1;
            }
        """)
        close_btn.clicked.connect(self.reject)
        header_layout.addWidget(close_btn)
        
        layout.addLayout(header_layout)
        
        # Subtle divider line
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #EEEEEE;")
        layout.addWidget(divider)
        
        # Main message
        if message:
            msg_label = QLabel(message)
            msg_label.setWordWrap(True)
            msg_label.setStyleSheet("""
                font-family: "Segoe UI", sans-serif;
                font-size: 15px;
                color: #444444;
                line-height: 1.6;
            """)
            layout.addWidget(msg_label)
        
        # Details list (bullet points) - clean solid style
        if details:
            details_frame = QFrame()
            details_frame.setStyleSheet("""
                QFrame {
                    background-color: #F8F8FA;
                    border-radius: 16px;
                    border: 1px solid #EEEEEE;
                }
            """)
            details_layout = QVBoxLayout(details_frame)
            details_layout.setContentsMargins(20, 16, 20, 16)
            details_layout.setSpacing(12)
            
            for detail in details:
                detail_row = QHBoxLayout()
                detail_row.setSpacing(12)
                
                # Purple dot indicator
                dot = QLabel("•")
                dot.setStyleSheet("font-size: 18px; color: #7C4DFF;")
                dot.setFixedWidth(16)
                detail_row.addWidget(dot)
                
                detail_label = QLabel(detail)
                detail_label.setStyleSheet("""
                    font-family: "Segoe UI", sans-serif;
                    font-size: 14px;
                    color: #333333;
                    font-weight: 500;
                """)
                detail_row.addWidget(detail_label)
                detail_row.addStretch()
                
                details_layout.addLayout(detail_row)
            
            layout.addWidget(details_frame)
        
        # Highlighted text (purple with glow effect)
        if highlight_text:
            highlight_label = QLabel(highlight_text)
            highlight_label.setWordWrap(True)
            highlight_label.setStyleSheet("""
                font-family: "Segoe UI", sans-serif;
                font-size: 14px;
                font-weight: 600;
                color: #7C4DFF;
                padding: 8px 0px;
            """)
            layout.addWidget(highlight_label)
        
        # Info text
        if info_text:
            info_label = QLabel(info_text)
            info_label.setWordWrap(True)
            info_label.setStyleSheet("""
                font-family: "Segoe UI", sans-serif;
                font-size: 13px;
                color: #888888;
                font-style: italic;
            """)
            layout.addWidget(info_label)
        
        layout.addSpacing(12)
        
        # Buttons with modern styling
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(14)
        btn_layout.addStretch()
        
        no_btn = QPushButton(no_text)
        no_btn.setMinimumHeight(46)
        no_btn.setMinimumWidth(120)
        no_btn.setCursor(Qt.PointingHandCursor)
        no_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #666666;
                border: 2px solid #E8E8E8;
                border-radius: 12px;
                font-family: "Segoe UI", sans-serif;
                font-weight: 600;
                font-size: 14px;
                padding: 10px 28px;
            }
            QPushButton:hover {
                background-color: #FAFAFA;
                border-color: #CCCCCC;
                color: #444444;
            }
        """)
        no_btn.clicked.connect(self.reject)
        btn_layout.addWidget(no_btn)
        
        yes_btn = QPushButton(yes_text)
        yes_btn.setMinimumHeight(46)
        yes_btn.setMinimumWidth(120)
        yes_btn.setCursor(Qt.PointingHandCursor)
        yes_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 12px;
                font-family: "Segoe UI", sans-serif;
                font-weight: 600;
                font-size: 14px;
                padding: 10px 28px;
            }
            QPushButton:hover {
                background-color: #9575FF;
            }
        """)
        yes_btn.clicked.connect(self.accept)
        btn_layout.addWidget(yes_btn)
        
        layout.addLayout(btn_layout)
    
    def mousePressEvent(self, event):
        """Enable dragging the dialog."""
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        """Handle drag movement."""
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """End dragging."""
        self._drag_pos = None
        event.accept()
    
    def accept(self):
        self.result_accepted = True
        super().accept()
    
    @staticmethod
    def ask(parent, title: str, message: str, details: list = None, 
            highlight_text: str = "", info_text: str = "",
            yes_text: str = "Yes", no_text: str = "No") -> bool:
        """Show dialog and return True if user clicked Yes."""
        dialog = ModernConfirmDialog(
            parent, title, message, details, highlight_text, info_text, yes_text, no_text
        )
        dialog.exec()
        return dialog.result_accepted


class ModernInfoDialog(QDialog):
    """
    Modern styled info/warning dialog with single OK button.
    Matches the app's purple theme with clean, minimal design.
    """
    
    def __init__(self, parent=None, title: str = "Information", message: str = "",
                 details: list = None, info_text: str = "", icon: str = "ℹ️",
                 ok_text: str = "OK", is_warning: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self.setModal(True)
        self._drag_pos = None
        
        # Remove default window frame for custom styling
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Main container with rounded corners and shadow
        self.container = QFrame(self)
        self.container.setObjectName("modernInfoContainer")
        self.container.setStyleSheet("""
            QFrame#modernInfoContainer {
                background-color: #FFFFFF;
                border-radius: 24px;
                border: 1px solid #E0E0E0;
            }
        """)
        
        # Add subtle drop shadow effect
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 40))
        self.container.setGraphicsEffect(shadow)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.addWidget(self.container)
        
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(18)
        
        # Header with icon and title
        header_layout = QHBoxLayout()
        header_layout.setSpacing(16)
        
        # Icon with appropriate color
        icon_bg = "#FFF3E0" if is_warning else "#F3EEFF"
        icon_border = "#FFE0B2" if is_warning else "#E8DFFF"
        icon_label = QLabel(icon)
        icon_label.setStyleSheet(f"""
            font-size: 24px;
            background-color: {icon_bg};
            border-radius: 22px;
            border: 2px solid {icon_border};
        """)
        icon_label.setFixedSize(52, 52)
        icon_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(icon_label)
        
        title_label = QLabel(title)
        title_label.setStyleSheet("""
            font-family: "Segoe UI", "SF Pro Display", sans-serif;
            font-size: 20px;
            font-weight: 700;
            color: #1A1A2E;
            letter-spacing: -0.3px;
        """)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        # Close button - solid purple with white X (ALWAYS visible)
        close_btn = QPushButton("X")
        close_btn.setFixedSize(36, 36)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 18px;
                font-size: 20px;
                font-weight: bold;
                font-family: Arial, Helvetica, sans-serif;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #5E35B1;
            }
        """)
        close_btn.clicked.connect(self.accept)
        header_layout.addWidget(close_btn)
        
        layout.addLayout(header_layout)
        
        # Subtle divider
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #EEEEEE;")
        layout.addWidget(divider)
        
        # Main message
        if message:
            msg_label = QLabel(message)
            msg_label.setWordWrap(True)
            msg_label.setStyleSheet("""
                font-family: "Segoe UI", sans-serif;
                font-size: 15px;
                color: #444444;
                line-height: 1.6;
            """)
            layout.addWidget(msg_label)
        
        # Details list (bullet points)
        if details:
            details_frame = QFrame()
            details_frame.setStyleSheet("""
                QFrame {
                    background-color: #F8F8FA;
                    border-radius: 14px;
                    border: 1px solid #EEEEEE;
                }
            """)
            details_layout = QVBoxLayout(details_frame)
            details_layout.setContentsMargins(18, 14, 18, 14)
            details_layout.setSpacing(10)
            
            for detail in details:
                detail_row = QHBoxLayout()
                detail_row.setSpacing(10)
                
                dot = QLabel("•")
                dot.setStyleSheet("font-size: 16px; color: #7C4DFF;")
                dot.setFixedWidth(14)
                detail_row.addWidget(dot)
                
                detail_label = QLabel(detail)
                detail_label.setWordWrap(True)
                detail_label.setStyleSheet("""
                    font-family: "Segoe UI", sans-serif;
                    font-size: 14px;
                    color: #555555;
                """)
                detail_row.addWidget(detail_label)
                detail_row.addStretch()
                
                details_layout.addLayout(detail_row)
            
            layout.addWidget(details_frame)
        
        # Info text
        if info_text:
            info_label = QLabel(info_text)
            info_label.setWordWrap(True)
            info_label.setStyleSheet("""
                font-family: "Segoe UI", sans-serif;
                font-size: 13px;
                color: #888888;
                font-style: italic;
            """)
            layout.addWidget(info_label)
        
        layout.addSpacing(8)
        
        # Single OK button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        ok_btn = QPushButton(ok_text)
        ok_btn.setMinimumHeight(46)
        ok_btn.setMinimumWidth(140)
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 12px;
                font-family: "Segoe UI", sans-serif;
                font-weight: 600;
                font-size: 14px;
                padding: 10px 32px;
            }
            QPushButton:hover {
                background-color: #9575FF;
            }
        """)
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)
        
        layout.addLayout(btn_layout)
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()
    
    @staticmethod
    def show_warning(parent, title: str, message: str, details: list = None, 
                     info_text: str = "", ok_text: str = "OK"):
        """Show a warning dialog."""
        dialog = ModernInfoDialog(
            parent, title, message, details, info_text, 
            icon="⚠️", ok_text=ok_text, is_warning=True
        )
        dialog.exec()
    
    @staticmethod
    def show_info(parent, title: str, message: str, details: list = None,
                  info_text: str = "", ok_text: str = "OK"):
        """Show an info dialog."""
        dialog = ModernInfoDialog(
            parent, title, message, details, info_text,
            icon="ℹ️", ok_text=ok_text, is_warning=False
        )
        dialog.exec()


class HistoryDialog(QDialog):
    """
    Modern dialog showing organization history with undo capability.
    Displays recent file organization operations in a clean, scrollable list.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_page = parent
        self.setWindowTitle("Organization History")
        self.setMinimumSize(600, 500)
        self.setModal(True)
        self._drag_pos = None
        
        # Remove default window frame for custom styling
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Main container
        self.container = QFrame(self)
        self.container.setObjectName("historyDialogContainer")
        self.container.setStyleSheet("""
            QFrame#historyDialogContainer {
                background-color: #FFFFFF;
                border-radius: 24px;
                border: 1px solid #E0E0E0;
            }
        """)
        
        # Add drop shadow
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setXOffset(0)
        shadow.setYOffset(5)
        shadow.setColor(QColor(0, 0, 0, 50))
        self.container.setGraphicsEffect(shadow)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.addWidget(self.container)
        
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        
        # Header
        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        
        icon_label = QLabel("📋")
        icon_label.setStyleSheet("""
            font-size: 24px;
            background-color: #F3EEFF;
            border-radius: 20px;
            border: 2px solid #E8DFFF;
        """)
        icon_label.setFixedSize(48, 48)
        icon_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(icon_label)
        
        title_label = QLabel("Organization History")
        title_label.setStyleSheet("""
            font-family: "Segoe UI", "SF Pro Display", sans-serif;
            font-size: 20px;
            font-weight: 700;
            color: #1A1A2E;
        """)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        close_btn = QPushButton("X")
        close_btn.setFixedSize(36, 36)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 18px;
                font-size: 20px;
                font-weight: bold;
                font-family: Arial, Helvetica, sans-serif;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #5E35B1;
            }
        """)
        close_btn.clicked.connect(self.accept)
        header_layout.addWidget(close_btn)
        
        layout.addLayout(header_layout)
        
        # Divider
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #EEEEEE;")
        layout.addWidget(divider)
        
        # History list container
        self.history_list = QWidget()
        self.history_layout = QVBoxLayout(self.history_list)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(10)
        
        # Scroll area for history items
        scroll = QScrollArea()
        scroll.setWidget(self.history_list)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background: #F5F5F5;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #CCCCCC;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #AAAAAA;
            }
        """)
        layout.addWidget(scroll, 1)
        
        # Load history
        self._load_history()
        
        # Footer with clear button
        footer_layout = QHBoxLayout()
        footer_layout.addStretch()
        
        clear_btn = QPushButton("Clear History")
        clear_btn.setMinimumHeight(40)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #999999;
                border: 1px solid #E0E0E0;
                border-radius: 10px;
                font-size: 13px;
                padding: 8px 20px;
            }
            QPushButton:hover {
                background-color: #FFF0F0;
                border-color: #FFCCCC;
                color: #CC6666;
            }
        """)
        clear_btn.clicked.connect(self._clear_history)
        footer_layout.addWidget(clear_btn)
        
        close_dialog_btn = QPushButton("Close")
        close_dialog_btn.setMinimumHeight(40)
        close_dialog_btn.setCursor(Qt.PointingHandCursor)
        close_dialog_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
                padding: 8px 28px;
            }
            QPushButton:hover {
                background-color: #9575FF;
            }
        """)
        close_dialog_btn.clicked.connect(self.accept)
        footer_layout.addWidget(close_dialog_btn)
        
        layout.addLayout(footer_layout)
    
    def _load_history(self):
        """Load move history from log files."""
        from app.core.apply import get_move_history
        
        history = get_move_history()
        
        if not history:
            # Show empty state
            empty_label = QLabel("No organization history yet")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("""
                font-family: "Segoe UI", sans-serif;
                font-size: 15px;
                color: #AAAAAA;
                padding: 40px;
            """)
            self.history_layout.addWidget(empty_label)
            self.history_layout.addStretch()
            return
        
        # Add history items (most recent first, limit to 20)
        for item in history[:20]:
            self._add_history_item(item)
        
        self.history_layout.addStretch()
    
    def _add_history_item(self, item: dict):
        """Add a single history item to the list."""
        from datetime import datetime
        
        item_frame = QFrame()
        item_frame.setStyleSheet("""
            QFrame {
                background-color: #FAFAFA;
                border-radius: 12px;
                border: 1px solid #EEEEEE;
            }
            QFrame:hover {
                background-color: #F5F0FF;
                border-color: #E8DFFF;
            }
        """)
        
        item_layout = QHBoxLayout(item_frame)
        item_layout.setContentsMargins(16, 14, 16, 14)
        item_layout.setSpacing(14)
        
        # Icon
        icon = QLabel("📁")
        icon.setStyleSheet("font-size: 20px; background: transparent; border: none;")
        icon.setFixedWidth(28)
        item_layout.addWidget(icon)
        
        # Info section
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        
        # Parse timestamp
        timestamp_str = item.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(timestamp_str)
            formatted_date = dt.strftime("%b %d, %Y at %I:%M %p")
        except:
            formatted_date = timestamp_str[:19] if timestamp_str else "Unknown date"
        
        date_label = QLabel(formatted_date)
        date_label.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 14px;
            font-weight: 600;
            color: #333333;
            background: transparent;
            border: none;
        """)
        info_layout.addWidget(date_label)
        
        files_count = item.get("successful_moves", item.get("total_files", 0))
        details_label = QLabel(f"{files_count} file(s) organized")
        details_label.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 12px;
            color: #888888;
            background: transparent;
            border: none;
        """)
        info_layout.addWidget(details_label)
        
        item_layout.addLayout(info_layout, 1)
        
        # View button
        view_btn = QPushButton("View")
        view_btn.setFixedSize(70, 32)
        view_btn.setCursor(Qt.PointingHandCursor)
        view_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #7C4DFF;
                border: 1px solid #7C4DFF;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #7C4DFF;
                color: white;
            }
        """)
        log_file = item.get("log_file", "")
        view_btn.clicked.connect(lambda checked, lf=log_file: self._view_details(lf))
        item_layout.addWidget(view_btn)
        
        self.history_layout.addWidget(item_frame)
    
    def _view_details(self, log_file: str):
        """Show details of a specific organization operation in a scrollable dialog."""
        import json
        from pathlib import Path
        
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_data = json.load(f)
            
            moves = log_data.get("moves", [])
            renamed = log_data.get("renamed_files", [])
            
            # Create a custom scrollable details dialog
            dialog = QDialog(self)
            dialog.setWindowTitle("Operation Details")
            dialog.setMinimumSize(550, 450)
            dialog.setModal(True)
            dialog._drag_pos = None
            
            # Remove frame for custom styling
            dialog.setWindowFlags(dialog.windowFlags() | Qt.FramelessWindowHint)
            dialog.setAttribute(Qt.WA_TranslucentBackground)
            
            # Container
            container = QFrame(dialog)
            container.setObjectName("detailsContainer")
            container.setStyleSheet("""
                QFrame#detailsContainer {
                    background-color: #FFFFFF;
                    border-radius: 20px;
                    border: 1px solid #E0E0E0;
                }
            """)
            
            # Shadow
            from PySide6.QtWidgets import QGraphicsDropShadowEffect
            from PySide6.QtGui import QColor
            shadow = QGraphicsDropShadowEffect(dialog)
            shadow.setBlurRadius(25)
            shadow.setXOffset(0)
            shadow.setYOffset(4)
            shadow.setColor(QColor(0, 0, 0, 40))
            container.setGraphicsEffect(shadow)
            
            main_layout = QVBoxLayout(dialog)
            main_layout.setContentsMargins(15, 15, 15, 15)
            main_layout.addWidget(container)
            
            layout = QVBoxLayout(container)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(14)
            
            # Header
            header = QHBoxLayout()
            header.setSpacing(12)
            
            icon = QLabel("📋")
            icon.setStyleSheet("font-size: 22px; background: #F3EEFF; border-radius: 18px; border: 2px solid #E8DFFF;")
            icon.setFixedSize(44, 44)
            icon.setAlignment(Qt.AlignCenter)
            header.addWidget(icon)
            
            title = QLabel(f"Organized {len(moves)} file(s)")
            title.setStyleSheet("font-family: 'Segoe UI'; font-size: 18px; font-weight: 700; color: #1A1A2E;")
            header.addWidget(title)
            header.addStretch()
            
            close_btn = QPushButton("X")
            close_btn.setFixedSize(32, 32)
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.setStyleSheet("""
                QPushButton { background: #7C4DFF; border: none; color: white; font-size: 18px; font-weight: bold; font-family: Arial, Helvetica, sans-serif; border-radius: 16px; padding: 0px; margin: 0px; }
                QPushButton:hover { background: #5E35B1; }
            """)
            close_btn.clicked.connect(dialog.accept)
            header.addWidget(close_btn)
            
            layout.addLayout(header)
            
            # Divider
            divider = QFrame()
            divider.setFixedHeight(1)
            divider.setStyleSheet("background: #EEEEEE;")
            layout.addWidget(divider)
            
            # Scrollable file list
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("""
                QScrollArea { border: none; background: transparent; }
                QScrollBar:vertical { border: none; background: #F5F5F5; width: 6px; border-radius: 3px; }
                QScrollBar::handle:vertical { background: #CCC; border-radius: 3px; min-height: 20px; }
                QScrollBar::handle:vertical:hover { background: #AAA; }
            """)
            
            list_widget = QWidget()
            list_layout = QVBoxLayout(list_widget)
            list_layout.setContentsMargins(0, 8, 0, 8)
            list_layout.setSpacing(6)
            
            # Add ALL moves to the list
            for move in moves:
                from_name = Path(move.get("from", "")).name
                to_folder = Path(move.get("to", "")).parent.name
                
                item = QLabel(f"📄 {from_name}  →  {to_folder}/")
                item.setStyleSheet("""
                    font-family: 'Segoe UI'; font-size: 13px; color: #444;
                    padding: 8px 12px; background: #FAFAFA; border-radius: 8px;
                """)
                item.setWordWrap(True)
                list_layout.addWidget(item)
            
            # Show renamed files if any
            if renamed:
                spacer = QLabel("")
                spacer.setFixedHeight(10)
                list_layout.addWidget(spacer)
                
                renamed_header = QLabel(f"📝 {len(renamed)} file(s) renamed to avoid duplicates:")
                renamed_header.setStyleSheet("font-family: 'Segoe UI'; font-size: 13px; font-weight: 600; color: #7C4DFF; padding: 4px 0;")
                list_layout.addWidget(renamed_header)
                
                for r in renamed:
                    orig = r.get("original_name", "?")
                    new = r.get("new_name", "?")
                    rename_item = QLabel(f"  {orig}  →  {new}")
                    rename_item.setStyleSheet("font-family: 'Segoe UI'; font-size: 12px; color: #666; padding: 4px 12px;")
                    list_layout.addWidget(rename_item)
            
            list_layout.addStretch()
            scroll.setWidget(list_widget)
            layout.addWidget(scroll, 1)
            
            # Footer
            footer = QHBoxLayout()
            footer.addStretch()
            
            ok_btn = QPushButton("Close")
            ok_btn.setMinimumHeight(40)
            ok_btn.setCursor(Qt.PointingHandCursor)
            ok_btn.setStyleSheet("""
                QPushButton { background: #7C4DFF; color: white; border: none; border-radius: 10px;
                              font-weight: 600; font-size: 14px; padding: 8px 28px; }
                QPushButton:hover { background: #9575FF; }
            """)
            ok_btn.clicked.connect(dialog.accept)
            footer.addWidget(ok_btn)
            
            layout.addLayout(footer)
            
            # Make dialog draggable
            def mousePressEvent(event):
                if event.button() == Qt.LeftButton:
                    dialog._drag_pos = event.globalPosition().toPoint() - dialog.frameGeometry().topLeft()
                    event.accept()
            def mouseMoveEvent(event):
                if dialog._drag_pos and event.buttons() == Qt.LeftButton:
                    dialog.move(event.globalPosition().toPoint() - dialog._drag_pos)
                    event.accept()
            def mouseReleaseEvent(event):
                dialog._drag_pos = None
                event.accept()
            
            dialog.mousePressEvent = mousePressEvent
            dialog.mouseMoveEvent = mouseMoveEvent
            dialog.mouseReleaseEvent = mouseReleaseEvent
            
            dialog.exec()
            
        except Exception as e:
            ModernInfoDialog.show_warning(
                self,
                title="Error",
                message="Could not load operation details.",
                details=[str(e)]
            )
    
    def _clear_history(self):
        """Clear all history log files."""
        from app.core.settings import settings
        import shutil
        
        confirmed = ModernConfirmDialog.ask(
            self,
            title="Clear History",
            message="Delete all organization history?",
            details=["This cannot be undone", "Undo operations will no longer be possible for past moves"],
            yes_text="Clear All",
            no_text="Cancel"
        )
        
        if confirmed:
            try:
                moves_dir = settings.get_moves_dir()
                for log_file in moves_dir.glob("moves-*.json"):
                    log_file.unlink()
                
                # Refresh the list
                # Clear existing items
                while self.history_layout.count() > 0:
                    item = self.history_layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
                
                # Show empty state
                empty_label = QLabel("No organization history yet")
                empty_label.setAlignment(Qt.AlignCenter)
                empty_label.setStyleSheet("""
                    font-family: "Segoe UI", sans-serif;
                    font-size: 15px;
                    color: #AAAAAA;
                    padding: 40px;
                """)
                self.history_layout.addWidget(empty_label)
                self.history_layout.addStretch()
                
            except Exception as e:
                ModernInfoDialog.show_warning(
                    self,
                    title="Error",
                    message="Could not clear history.",
                    details=[str(e)]
                )
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()


class PinnedDialog(QDialog):
    """
    Modern dialog for managing pinned files/folders that won't be organized.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_page = parent
        self.setWindowTitle("Pinned Items")
        self.setMinimumSize(550, 450)
        self.setModal(True)
        self._drag_pos = None
        
        # Remove default window frame for custom styling
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Main container
        self.container = QFrame(self)
        self.container.setObjectName("pinnedDialogContainer")
        self.container.setStyleSheet("""
            QFrame#pinnedDialogContainer {
                background-color: #FFFFFF;
                border-radius: 24px;
                border: 1px solid #E0E0E0;
            }
        """)
        
        # Add drop shadow
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setXOffset(0)
        shadow.setYOffset(5)
        shadow.setColor(QColor(0, 0, 0, 50))
        self.container.setGraphicsEffect(shadow)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.addWidget(self.container)
        
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        
        # Header
        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        
        icon_label = QLabel("📌")
        icon_label.setStyleSheet("""
            font-size: 24px;
            background-color: #F3EEFF;
            border-radius: 20px;
            border: 2px solid #E8DFFF;
        """)
        icon_label.setFixedSize(48, 48)
        icon_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(icon_label)
        
        title_label = QLabel("Pinned Items")
        title_label.setStyleSheet("""
            font-family: "Segoe UI", "SF Pro Display", sans-serif;
            font-size: 20px;
            font-weight: 700;
            color: #1A1A2E;
        """)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        close_btn = QPushButton("X")
        close_btn.setFixedSize(36, 36)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 18px;
                font-size: 20px;
                font-weight: bold;
                font-family: Arial, Helvetica, sans-serif;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #5E35B1;
            }
        """)
        close_btn.clicked.connect(self.accept)
        header_layout.addWidget(close_btn)
        
        layout.addLayout(header_layout)
        
        # Description
        desc = QLabel("Pinned files and folders will never be organized.")
        desc.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 13px;
            color: #888888;
        """)
        layout.addWidget(desc)
        
        # Divider
        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background-color: #EEEEEE;")
        layout.addWidget(divider)
        
        # Pinned items list container
        self.pinned_list = QWidget()
        self.pinned_layout = QVBoxLayout(self.pinned_list)
        self.pinned_layout.setContentsMargins(0, 0, 0, 0)
        self.pinned_layout.setSpacing(8)
        
        # Scroll area for pinned items
        scroll = QScrollArea()
        scroll.setWidget(self.pinned_list)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background: #F5F5F5;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #CCCCCC;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #AAAAAA;
            }
        """)
        layout.addWidget(scroll, 1)
        
        # Load pinned items
        self._load_pinned_items()
        
        # Add new item section
        add_layout = QHBoxLayout()
        add_layout.setSpacing(10)
        
        add_file_btn = QPushButton("📄 Pin File")
        add_file_btn.setMinimumHeight(40)
        add_file_btn.setCursor(Qt.PointingHandCursor)
        add_file_btn.setStyleSheet("""
            QPushButton {
                background-color: #F5F0FF;
                color: #7C4DFF;
                border: 1px solid #E8DFFF;
                border-radius: 10px;
                font-size: 13px;
                font-weight: 600;
                padding: 8px 16px;
            }
            QPushButton:hover {
                background-color: #E8DFFF;
                border-color: #7C4DFF;
            }
        """)
        add_file_btn.clicked.connect(self._add_pinned_file)
        add_layout.addWidget(add_file_btn)
        
        add_folder_btn = QPushButton("📁 Pin Folder")
        add_folder_btn.setMinimumHeight(40)
        add_folder_btn.setCursor(Qt.PointingHandCursor)
        add_folder_btn.setStyleSheet("""
            QPushButton {
                background-color: #F5F0FF;
                color: #7C4DFF;
                border: 1px solid #E8DFFF;
                border-radius: 10px;
                font-size: 13px;
                font-weight: 600;
                padding: 8px 16px;
            }
            QPushButton:hover {
                background-color: #E8DFFF;
                border-color: #7C4DFF;
            }
        """)
        add_folder_btn.clicked.connect(self._add_pinned_folder)
        add_layout.addWidget(add_folder_btn)
        
        add_layout.addStretch()
        layout.addLayout(add_layout)
        
        # Footer
        footer_layout = QHBoxLayout()
        footer_layout.addStretch()
        
        clear_btn = QPushButton("Unpin All")
        clear_btn.setMinimumHeight(40)
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #999999;
                border: 1px solid #E0E0E0;
                border-radius: 10px;
                font-size: 13px;
                padding: 8px 20px;
            }
            QPushButton:hover {
                background-color: #FFF0F0;
                border-color: #FFCCCC;
                color: #CC6666;
            }
        """)
        clear_btn.clicked.connect(self._clear_all_pinned)
        footer_layout.addWidget(clear_btn)
        
        close_dialog_btn = QPushButton("Done")
        close_dialog_btn.setMinimumHeight(40)
        close_dialog_btn.setCursor(Qt.PointingHandCursor)
        close_dialog_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
                padding: 8px 28px;
            }
            QPushButton:hover {
                background-color: #9575FF;
            }
        """)
        close_dialog_btn.clicked.connect(self.accept)
        footer_layout.addWidget(close_dialog_btn)
        
        layout.addLayout(footer_layout)
    
    def _load_pinned_items(self):
        """Load pinned items from settings."""
        pinned_paths = settings.get_pinned_paths()
        
        if not pinned_paths:
            # Show empty state
            empty_label = QLabel("No pinned items yet.\nPin files or folders to protect them from organization.")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("""
                font-family: "Segoe UI", sans-serif;
                font-size: 14px;
                color: #AAAAAA;
                padding: 30px;
            """)
            self.pinned_layout.addWidget(empty_label)
            self.pinned_layout.addStretch()
            return
        
        # Add pinned items
        for path in pinned_paths:
            self._add_pinned_item_row(path)
        
        self.pinned_layout.addStretch()
    
    def _add_pinned_item_row(self, path: str):
        """Add a single pinned item row."""
        from pathlib import Path
        
        item_frame = QFrame()
        item_frame.setStyleSheet("""
            QFrame {
                background-color: #FAFAFA;
                border-radius: 12px;
                border: 1px solid #EEEEEE;
            }
            QFrame:hover {
                background-color: #F5F0FF;
                border-color: #E8DFFF;
            }
        """)
        
        item_layout = QHBoxLayout(item_frame)
        item_layout.setContentsMargins(14, 12, 14, 12)
        item_layout.setSpacing(12)
        
        # Icon - folder or file
        p = Path(path)
        is_folder = p.is_dir() if p.exists() else ('.' not in p.name)
        icon = QLabel("📁" if is_folder else "📄")
        icon.setStyleSheet("font-size: 18px; background: transparent; border: none;")
        icon.setFixedWidth(26)
        item_layout.addWidget(icon)
        
        # Path info
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        
        name_label = QLabel(p.name)
        name_label.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 14px;
            font-weight: 600;
            color: #333333;
            background: transparent;
            border: none;
        """)
        info_layout.addWidget(name_label)
        
        # Show parent folder
        parent_str = str(p.parent)
        if len(parent_str) > 45:
            parent_str = "..." + parent_str[-42:]
        parent_label = QLabel(parent_str)
        parent_label.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 11px;
            color: #999999;
            background: transparent;
            border: none;
        """)
        info_layout.addWidget(parent_label)
        
        item_layout.addLayout(info_layout, 1)
        
        # Status indicator - exists or not
        if not p.exists():
            status = QLabel("⚠️")
            status.setToolTip("File/folder no longer exists")
            status.setStyleSheet("font-size: 14px; background: transparent; border: none;")
            item_layout.addWidget(status)
        
        # Unpin button - solid red with white X (ALWAYS visible)
        unpin_btn = QPushButton("X")
        unpin_btn.setFixedSize(28, 28)
        unpin_btn.setCursor(Qt.PointingHandCursor)
        unpin_btn.setToolTip("Unpin this item")
        unpin_btn.setStyleSheet("""
            QPushButton {
                background-color: #E53935;
                color: white;
                border: none;
                border-radius: 14px;
                font-size: 16px;
                font-weight: bold;
                font-family: Arial, Helvetica, sans-serif;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #C62828;
            }
        """)
        unpin_btn.clicked.connect(lambda checked, p=path: self._unpin_item(p))
        item_layout.addWidget(unpin_btn)
        
        self.pinned_layout.addWidget(item_frame)
    
    def _add_pinned_file(self):
        """Add a file to pinned list via file dialog."""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select File to Pin",
            "",
            "All Files (*)"
        )
        
        if file_path:
            if settings.add_pinned_path(file_path):
                self._refresh_list()
    
    def _add_pinned_folder(self):
        """Add a folder to pinned list via folder dialog."""
        folder_path = QFileDialog.getExistingDirectory(
            self,
            "Select Folder to Pin",
            ""
        )
        
        if folder_path:
            if settings.add_pinned_path(folder_path):
                self._refresh_list()
    
    def _unpin_item(self, path: str):
        """Remove a path from pinned list."""
        settings.remove_pinned_path(path)
        self._refresh_list()
    
    def _clear_all_pinned(self):
        """Clear all pinned items."""
        if not settings.get_pinned_paths():
            return
        
        confirmed = ModernConfirmDialog.ask(
            self,
            title="Unpin All Items",
            message="Remove all pinned items?",
            details=["These files/folders will be subject to organization again"],
            yes_text="Unpin All",
            no_text="Cancel"
        )
        
        if confirmed:
            settings.clear_all_pinned()
            self._refresh_list()
    
    def _refresh_list(self):
        """Refresh the pinned items list."""
        # Clear existing items
        while self.pinned_layout.count() > 0:
            item = self.pinned_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        # Reload
        self._load_pinned_items()
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() == Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()


class ApplyInstructionsDialog(QDialog):
    """
    Modern dialog for choosing how to apply new instructions to existing files.
    Clean, minimal design with three options.
    """
    
    # Result constants
    REORGANIZE_ALL = 1
    ORGANIZE_AS_IS = 2
    CONTINUE_WATCHING = 3
    
    def __init__(self, parent=None, file_count: int = 0, subfolder_count: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Apply Instructions")
        self.setMinimumWidth(420)
        self.setModal(True)
        self.result_choice = self.CONTINUE_WATCHING
        self._drag_pos = None
        
        # Remove default window frame for custom styling
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        # Main container
        self.container = QFrame(self)
        self.container.setObjectName("applyDialogContainer")
        self.container.setStyleSheet("""
            QFrame#applyDialogContainer {
                background-color: #FFFFFF;
                border-radius: 20px;
                border: 1px solid #E0E0E0;
            }
        """)
        
        # Add drop shadow
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 40))
        self.container.setGraphicsEffect(shadow)
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.addWidget(self.container)
        
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        
        # Header
        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)
        
        icon_label = QLabel("🔄")
        icon_label.setStyleSheet("""
            font-size: 24px;
            background-color: #F3EEFF;
            border-radius: 20px;
            border: 2px solid #E8DFFF;
        """)
        icon_label.setFixedSize(44, 44)
        icon_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(icon_label)
        
        title_label = QLabel("Apply New Instructions")
        title_label.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 18px;
            font-weight: 700;
            color: #1A1A2E;
        """)
        header_layout.addWidget(title_label)
        header_layout.addStretch()
        
        close_btn = QPushButton("X")
        close_btn.setFixedSize(32, 32)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 16px;
                font-size: 18px;
                font-weight: bold;
                font-family: Arial, Helvetica, sans-serif;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #5E35B1;
            }
        """)
        close_btn.clicked.connect(self._on_continue)
        header_layout.addWidget(close_btn)
        
        layout.addLayout(header_layout)
        
        # Subtitle with file count
        if subfolder_count > 0:
            subtitle = f"Found {file_count} files in {subfolder_count} subfolders"
        else:
            subtitle = f"Found {file_count} existing files"
        
        subtitle_label = QLabel(subtitle)
        subtitle_label.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 13px;
            color: #666666;
        """)
        layout.addWidget(subtitle_label)
        
        # Options as styled buttons
        options_layout = QVBoxLayout()
        options_layout.setSpacing(10)
        
        # Option 1: Re-organize All
        reorganize_btn = self._create_option_button(
            "🔄 Re-organize All",
            "Flatten folders, then organize fresh",
            primary=True
        )
        reorganize_btn.clicked.connect(self._on_reorganize)
        options_layout.addWidget(reorganize_btn)
        
        # Option 2: Organize As-Is
        organize_btn = self._create_option_button(
            "📂 Organize As-Is",
            "Apply new instructions to current files"
        )
        organize_btn.clicked.connect(self._on_organize)
        options_layout.addWidget(organize_btn)
        
        # Option 3: Continue Watching
        continue_btn = self._create_option_button(
            "⏭️ Skip",
            "Only apply to new files going forward"
        )
        continue_btn.clicked.connect(self._on_continue)
        options_layout.addWidget(continue_btn)
        
        layout.addLayout(options_layout)
    
    def _create_option_button(self, title: str, subtitle: str, primary: bool = False) -> QPushButton:
        """Create a styled option button with title and subtitle."""
        btn = QPushButton()
        btn.setCursor(Qt.PointingHandCursor)
        btn.setMinimumHeight(56)
        
        # Create layout for button content
        btn_layout = QVBoxLayout(btn)
        btn_layout.setContentsMargins(16, 10, 16, 10)
        btn_layout.setSpacing(2)
        
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"""
            font-family: "Segoe UI", sans-serif;
            font-size: 14px;
            font-weight: 600;
            color: {"#FFFFFF" if primary else "#333333"};
            background: transparent;
        """)
        title_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        btn_layout.addWidget(title_lbl)
        
        subtitle_lbl = QLabel(subtitle)
        subtitle_lbl.setStyleSheet(f"""
            font-family: "Segoe UI", sans-serif;
            font-size: 11px;
            color: {"rgba(255,255,255,0.8)" if primary else "#888888"};
            background: transparent;
        """)
        subtitle_lbl.setAttribute(Qt.WA_TransparentForMouseEvents)
        btn_layout.addWidget(subtitle_lbl)
        
        if primary:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #7C4DFF;
                    border: none;
                    border-radius: 12px;
                    text-align: left;
                }
                QPushButton:hover {
                    background-color: #9575FF;
                }
                QPushButton:pressed {
                    background-color: #6A3DE8;
                }
            """)
        else:
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #F8F8FA;
                    border: 1px solid #E8E8E8;
                    border-radius: 12px;
                    text-align: left;
                }
                QPushButton:hover {
                    background-color: #F0EEFF;
                    border-color: #7C4DFF;
                }
                QPushButton:pressed {
                    background-color: #E8E0FF;
                }
            """)
        
        return btn
    
    def _on_reorganize(self):
        self.result_choice = self.REORGANIZE_ALL
        self.accept()
    
    def _on_organize(self):
        self.result_choice = self.ORGANIZE_AS_IS
        self.accept()
    
    def _on_continue(self):
        self.result_choice = self.CONTINUE_WATCHING
        self.accept()
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()


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
        
        # Voice recording state
        self.voice_worker = None
        self.is_recording_voice = False
        self.current_recording_folder = None  # Track which folder's mic is recording
        
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
        
        self.save_btn = QPushButton("Save Changes")
        self.save_btn.setMinimumHeight(44)
        self.save_btn.setMinimumWidth(140)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: none;
                border-radius: 10px;
                font-weight: 600;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #9575FF;
            }
            QPushButton:pressed {
                background-color: #6A3DE8;
            }
        """)
        self.save_btn.clicked.connect(self._save_and_close)
        button_layout.addWidget(self.save_btn)
        
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
        
        remove_btn = QPushButton("Remove")
        remove_btn.setMinimumHeight(28)
        remove_btn.setMinimumWidth(70)
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setToolTip("Remove this folder from auto-organize")
        remove_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #999999;
                border: 1px solid #DDDDDD;
                border-radius: 6px;
                font-weight: 500;
                font-size: 12px;
                padding: 4px 10px;
            }
            QPushButton:hover {
                background-color: #FFEBEE;
                color: #D32F2F;
                border-color: #D32F2F;
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
        
        # Input row with text field and mic button
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        
        instruction_input = QLineEdit()
        instruction_input.setPlaceholderText("e.g. Move screenshots to Images/Screenshots, organize others by type...")
        instruction_input.setText(instruction)
        instruction_input.setMinimumHeight(38)
        instruction_input.textChanged.connect(
            lambda text, fp=folder_path: self._on_instruction_changed(fp, text)
        )
        input_row.addWidget(instruction_input, 1)
        
        # Microphone button
        mic_button = QPushButton("Voice")
        mic_button.setMinimumSize(55, 38)
        mic_button.setMaximumSize(55, 38)
        mic_button.setCursor(Qt.PointingHandCursor)
        mic_button.setToolTip("Click to speak your instruction")
        mic_button.setStyleSheet("""
            QPushButton {
                font-size: 11px;
                font-weight: bold;
                background-color: #F5F0FF;
                border: 1px solid #7C4DFF;
                border-radius: 6px;
                color: #7C4DFF;
                padding: 0px;
            }
            QPushButton:hover {
                background-color: #EDE7FF;
            }
        """)
        mic_button.clicked.connect(lambda checked, fp=folder_path: self._toggle_folder_voice(fp))
        input_row.addWidget(mic_button)
        
        instruction_layout.addLayout(input_row)
        
        frame_layout.addLayout(instruction_layout)
        
        # Store widgets for later reference
        self.folder_widgets[folder_path] = {
            'widget': frame,
            'input': instruction_input,
            'mic_button': mic_button
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
        try:
            logger.info(f"WatchConfigDialog._save_and_close called with {len(self.folder_data)} folders")
            
            # Update settings
            new_folders = []
            for path, instruction in self.folder_data.items():
                logger.info(f"  Saving folder: {path}, instruction: {instruction[:50] if instruction else '(empty)'}...")
                new_folders.append({
                    'path': path,
                    'instruction': instruction
                })
            
            settings.auto_organize_folders = new_folders
            settings._save_config()
            logger.info("Settings saved, accepting dialog")
            
            self.accept()
        except Exception as e:
            logger.error(f"Error in _save_and_close: {e}")
            import traceback
            traceback.print_exc()
    
    def _toggle_folder_voice(self, folder_path: str):
        """Toggle voice recording for a specific folder's instruction."""
        if self.is_recording_voice:
            # Stop current recording
            self._stop_folder_voice()
        else:
            # Start recording for this folder
            self._start_folder_voice(folder_path)
    
    def _start_folder_voice(self, folder_path: str):
        """Start recording voice for a folder's instruction."""
        self.is_recording_voice = True
        self.current_recording_folder = folder_path
        
        # Update the mic button to show recording state
        if folder_path in self.folder_widgets:
            mic_btn = self.folder_widgets[folder_path].get('mic_button')
            if mic_btn:
                mic_btn.setText("Stop")
                mic_btn.setStyleSheet("""
                    QPushButton {
                        font-size: 11px;
                        font-weight: bold;
                        background-color: #EF5350;
                        border: 1px solid #D32F2F;
                        border-radius: 6px;
                        color: white;
                        padding: 0px;
                    }
                    QPushButton:hover {
                        background-color: #E53935;
                    }
                """)
                mic_btn.setToolTip("Recording... Click to stop")
        
        # Start voice worker
        self.voice_worker = VoiceRecordWorker()
        self.voice_worker.finished.connect(self._on_folder_voice_transcribed)
        self.voice_worker.error.connect(self._on_folder_voice_error)
        self.voice_worker.recording_stopped.connect(self._on_folder_recording_stopped)
        self.voice_worker.start()
    
    def _stop_folder_voice(self):
        """Stop recording voice input."""
        if self.voice_worker:
            self.voice_worker.stop_recording()
    
    def _on_folder_recording_stopped(self):
        """Called when recording has stopped, before transcription."""
        self.is_recording_voice = False
        self._reset_folder_mic_button()
    
    def _on_folder_voice_transcribed(self, text: str):
        """Handle transcribed text for a folder's instruction."""
        self.is_recording_voice = False
        self._reset_folder_mic_button()
        
        if text.strip() and self.current_recording_folder:
            folder_path = self.current_recording_folder
            if folder_path in self.folder_widgets:
                instruction_input = self.folder_widgets[folder_path].get('input')
                if instruction_input:
                    # Append to existing or replace
                    current = instruction_input.text().strip()
                    if current:
                        instruction_input.setText(f"{current}. {text}")
                    else:
                        instruction_input.setText(text)
        
        self.current_recording_folder = None
    
    def _on_folder_voice_error(self, error: str):
        """Handle voice recording errors."""
        self.is_recording_voice = False
        self._reset_folder_mic_button()
        self.current_recording_folder = None
        
        # Show error in a simple message
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "Voice Error", f"Could not transcribe: {error}")
    
    def _reset_folder_mic_button(self):
        """Reset mic button to default state for the current recording folder."""
        if self.current_recording_folder and self.current_recording_folder in self.folder_widgets:
            mic_btn = self.folder_widgets[self.current_recording_folder].get('mic_button')
            if mic_btn:
                mic_btn.setText("Voice")
                mic_btn.setStyleSheet("""
                    QPushButton {
                        font-size: 11px;
                        font-weight: bold;
                        background-color: #F5F0FF;
                        border: 1px solid #7C4DFF;
                        border-radius: 6px;
                        color: #7C4DFF;
                        padding: 0px;
                    }
                    QPushButton:hover {
                        background-color: #EDE7FF;
                    }
                """)
                mic_btn.setToolTip("Click to speak your instruction")
    
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
        
        self.subtitle = QLabel(
            "Describe how you want your files organized in plain English. "
            "AI will analyze your indexed files and propose an organization plan."
        )
        self.subtitle.setObjectName("heroSubtitle")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.subtitle)
        
        # ========== SEGMENTED CONTROL (TAB SWITCHER) ==========
        tab_container = QHBoxLayout()
        tab_container.setSpacing(0)
        tab_container.addStretch()
        
        self.tab_organize_now = QPushButton("✨ Organize Now")
        self.tab_organize_now.setMinimumHeight(44)
        self.tab_organize_now.setMinimumWidth(160)
        self.tab_organize_now.setCursor(Qt.PointingHandCursor)
        self.tab_organize_now.setCheckable(True)
        self.tab_organize_now.setChecked(True)
        self.tab_organize_now.setStyleSheet("""
            QPushButton {
                background-color: #7C4DFF;
                color: white;
                border: 2px solid #7C4DFF;
                border-top-left-radius: 12px;
                border-bottom-left-radius: 12px;
                border-top-right-radius: 0px;
                border-bottom-right-radius: 0px;
                font-weight: 600;
                font-size: 14px;
                padding: 10px 20px;
            }
            QPushButton:checked {
                background-color: #7C4DFF;
                color: white;
            }
            QPushButton:!checked {
                background-color: #FFFFFF;
                color: #7C4DFF;
                border: 2px solid #E0E0E0;
            }
            QPushButton:!checked:hover {
                background-color: rgba(124, 77, 255, 0.05);
                border-color: #7C4DFF;
            }
        """)
        tab_container.addWidget(self.tab_organize_now)
        
        self.tab_auto_organize = QPushButton("👁️ Auto-Organize")
        self.tab_auto_organize.setMinimumHeight(44)
        self.tab_auto_organize.setMinimumWidth(160)
        self.tab_auto_organize.setCursor(Qt.PointingHandCursor)
        self.tab_auto_organize.setCheckable(True)
        self.tab_auto_organize.setChecked(False)
        self.tab_auto_organize.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #7C4DFF;
                border: 2px solid #E0E0E0;
                border-top-right-radius: 12px;
                border-bottom-right-radius: 12px;
                border-top-left-radius: 0px;
                border-bottom-left-radius: 0px;
                font-weight: 600;
                font-size: 14px;
                padding: 10px 20px;
            }
            QPushButton:checked {
                background-color: #7C4DFF;
                color: white;
                border-color: #7C4DFF;
            }
            QPushButton:!checked {
                background-color: #FFFFFF;
                color: #7C4DFF;
            }
            QPushButton:!checked:hover {
                background-color: rgba(124, 77, 255, 0.05);
                border-color: #7C4DFF;
            }
        """)
        tab_container.addWidget(self.tab_auto_organize)
        
        tab_container.addStretch()
        layout.addLayout(tab_container)
        
        layout.addSpacing(10)
        
        # ========== STACKED WIDGET FOR TAB CONTENT ==========
        self.content_stack = QStackedWidget()
        
        # ----- PAGE 0: Organize Now -----
        organize_now_page = QWidget()
        organize_now_layout = QVBoxLayout(organize_now_page)
        organize_now_layout.setContentsMargins(0, 0, 0, 0)
        organize_now_layout.setSpacing(20)
        
        # Instruction Input Card
        self.instruction_card = QFrame()
        self.instruction_card.setObjectName("organizeCard")
        self.instruction_card.setStyleSheet("""
            QFrame#organizeCard {
                background-color: rgba(124, 77, 255, 0.06);
                border: 2px dashed rgba(124, 77, 255, 0.5);
                border-radius: 20px;
                padding: 24px;
            }
        """)
        instruction_layout = QVBoxLayout(self.instruction_card)
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
        
        organize_now_layout.addWidget(self.instruction_card)
        
        # Destination Folder Card
        self.dest_card = QFrame()
        self.dest_card.setObjectName("organizeCard")
        self.dest_card.setStyleSheet("""
            QFrame#organizeCard {
                background-color: rgba(124, 77, 255, 0.06);
                border: 2px dashed rgba(124, 77, 255, 0.5);
                border-radius: 20px;
            }
        """)
        dest_layout = QHBoxLayout(self.dest_card)
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
        
        organize_now_layout.addWidget(self.dest_card)

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
                background-color: rgba(124, 77, 255, 0.1);
                border: 2px solid #7C4DFF;
                color: #7C4DFF;
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
                color: #7C4DFF;
                border: 2px solid #7C4DFF;
                border-radius: 12px;
                font-weight: 600;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: #F3EEFF;
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
                color: #9575FF;
                border: 2px solid #9575FF;
                border-radius: 12px;
                font-weight: 600;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: #F3EEFF;
                color: #7C4DFF;
                border-color: #7C4DFF;
            }
            QPushButton:disabled {
                background-color: #FAFAFA;
                border-color: #E8E8E8;
                color: #CCCCCC;
            }
        """)
        self.undo_button.clicked.connect(self.undo_last_organization)
        action_layout.addWidget(self.undo_button)
        
        # History button - shows past organization operations
        self.history_button = QPushButton("📋 History")
        self.history_button.setMinimumHeight(48)
        self.history_button.setMinimumWidth(130)
        self.history_button.setCursor(Qt.PointingHandCursor)
        self.history_button.setToolTip("View past organization operations")
        self.history_button.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #666666;
                border: 2px solid #E0E0E0;
                border-radius: 12px;
                font-weight: 600;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: #F5F5F5;
                border-color: #7C4DFF;
                color: #7C4DFF;
            }
        """)
        self.history_button.clicked.connect(self._show_history_dialog)
        action_layout.addWidget(self.history_button)
        
        # Pinned button - manage pinned files/folders
        self.pinned_button = QPushButton("📌 Pinned")
        self.pinned_button.setMinimumHeight(48)
        self.pinned_button.setMinimumWidth(130)
        self.pinned_button.setCursor(Qt.PointingHandCursor)
        self.pinned_button.setToolTip("View and manage pinned files/folders that won't be organized")
        self.pinned_button.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #666666;
                border: 2px solid #E0E0E0;
                border-radius: 12px;
                font-weight: 600;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: #F5F5F5;
                border-color: #7C4DFF;
                color: #7C4DFF;
            }
        """)
        self.pinned_button.clicked.connect(self._show_pinned_dialog)
        action_layout.addWidget(self.pinned_button)
        
        # Edit button (to show inputs again after plan generation)
        self.edit_inputs_button = QPushButton("✏️ Edit")
        self.edit_inputs_button.setMinimumHeight(48)
        self.edit_inputs_button.setCursor(Qt.PointingHandCursor)
        self.edit_inputs_button.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #666666;
                border: 2px solid #E0E0E0;
                border-radius: 12px;
                font-weight: 600;
                font-size: 15px;
                padding: 0px 20px;
            }
            QPushButton:hover {
                background-color: #F5F5F5;
                border-color: #7C4DFF;
                color: #7C4DFF;
            }
        """)
        self.edit_inputs_button.clicked.connect(self._show_input_cards)
        self.edit_inputs_button.setVisible(False)
        action_layout.addWidget(self.edit_inputs_button)
        
        # No stretch - let buttons stay left-aligned
        
        # Hide these buttons initially - shown after plan is generated
        self.apply_button.setVisible(False)
        self.clear_button.setVisible(False)
        self.undo_button.setVisible(False)
        
        # Wrap action buttons in a scroll area to prevent cutoff on small windows
        action_widget = QWidget()
        action_widget.setLayout(action_layout)
        action_widget.setStyleSheet("background: transparent;")
        
        action_scroll = QScrollArea()
        action_scroll.setWidget(action_widget)
        action_scroll.setWidgetResizable(True)
        action_scroll.setFixedHeight(70)  # Fixed height for the button row + scrollbar space
        action_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        action_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        action_scroll.setStyleSheet("""
            QScrollArea {
                border: none;
                background: transparent;
            }
            QScrollBar:horizontal {
                border: none;
                background: #F0F0F0;
                height: 8px;
                border-radius: 4px;
                margin-top: 4px;
            }
            QScrollBar::handle:horizontal {
                background: #C0C0C0;
                border-radius: 4px;
                min-width: 40px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #7C4DFF;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
        """)
        
        organize_now_layout.addWidget(action_scroll)
        
        # Progress and Status
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMinimumHeight(8)
        organize_now_layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #888; font-style: italic; font-size: 13px;")
        organize_now_layout.addWidget(self.status_label)

        # Results Area (Splitter: Tree + Details) - Hidden until plan is generated
        self.results_splitter = QSplitter(Qt.Horizontal)
        self.results_splitter.setChildrenCollapsible(False)
        
        # Plan Tree Card - Matching the clean input card style
        plan_card = QFrame()
        plan_card.setObjectName("planCard")
        plan_card.setStyleSheet("""
            QFrame#planCard {
                background-color: rgba(124, 77, 255, 0.06);
                border: 2px dashed rgba(124, 77, 255, 0.5);
                border-radius: 20px;
            }
        """)
        
        plan_layout = QVBoxLayout(plan_card)
        plan_layout.setContentsMargins(20, 20, 20, 20)
        plan_layout.setSpacing(12)
        
        # Simple title matching input card style
        plan_title = QLabel("📁 Proposed Organization")
        plan_title.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-weight: 600;
            font-size: 16px;
            color: #7C4DFF;
            background: transparent;
        """)
        plan_layout.addWidget(plan_title)
        
        self.plan_tree = QTreeWidget()
        self.plan_tree.setHeaderHidden(True)
        self.plan_tree.setIndentation(20)
        self.plan_tree.setAlternatingRowColors(False)
        self.plan_tree.setStyleSheet("""
            QTreeWidget {
                background-color: transparent;
                border: none;
                font-family: "Segoe UI", sans-serif;
                font-size: 14px;
                padding: 4px;
                outline: none;
            }
            QTreeWidget::item {
                height: 38px;
                color: #333333;
                border-radius: 10px;
                padding-left: 8px;
                margin: 2px 0px;
            }
            QTreeWidget::item:hover {
                background-color: rgba(255, 255, 255, 0.6);
            }
            QTreeWidget::item:selected {
                background-color: rgba(255, 255, 255, 0.8);
                color: #7C4DFF;
                font-weight: 600;
            }
            /* Hide native branch indicators completely */
            QTreeView::branch {
                background: transparent;
                width: 0px;
                border: none;
                image: none;
            }
        """)
        self.plan_tree.setRootIsDecorated(False)  # Remove native expand buttons
        self.plan_tree.itemClicked.connect(self._on_tree_item_clicked)
        self.plan_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.plan_tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        plan_layout.addWidget(self.plan_tree)
        
        self.results_splitter.addWidget(plan_card)
        
        self.results_splitter.setVisible(False)  # Hidden until plan is generated
        
        # Summary line (shown after plan generation) - subtle and clean
        self.plan_summary_label = QLabel("")
        self.plan_summary_label.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 13px;
            font-weight: 500;
            color: #888888;
            padding: 4px 0px;
        """)
        self.plan_summary_label.setVisible(False)
        organize_now_layout.addWidget(self.plan_summary_label)
        
        # Info note for existing folders (subtle, not alarming)
        self.existing_folders_note = QLabel("")
        self.existing_folders_note.setWordWrap(True)
        self.existing_folders_note.setStyleSheet("""
            font-family: "Segoe UI", sans-serif;
            font-size: 12px;
            color: #999999;
            font-style: italic;
            padding: 2px 0px;
        """)
        self.existing_folders_note.setVisible(False)
        organize_now_layout.addWidget(self.existing_folders_note)
        
        organize_now_layout.addWidget(self.results_splitter, 1)
        
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
        
        organize_now_layout.addWidget(self.feedback_group)
        organize_now_layout.addStretch()
        
        self.content_stack.addWidget(organize_now_page)
        
        # ----- PAGE 1: Auto-Organize -----
        auto_organize_page = QWidget()
        auto_organize_layout = QVBoxLayout(auto_organize_page)
        auto_organize_layout.setContentsMargins(0, 0, 0, 0)
        auto_organize_layout.setSpacing(20)
        
        # ========== WATCH & AUTO-ORGANIZE SECTION ==========
        self._create_auto_organize_section(auto_organize_layout)
        
        auto_organize_layout.addStretch()
        self.content_stack.addWidget(auto_organize_page)
        
        layout.addWidget(self.content_stack, 1)
        
        # Connect tab buttons
        self.tab_organize_now.clicked.connect(lambda: self._switch_tab(0))
        self.tab_auto_organize.clicked.connect(lambda: self._switch_tab(1))
        
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
        watch_layout.setSpacing(12)
        watch_layout.setContentsMargins(24, 24, 24, 24)
        
        # Header with icon and title
        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        
        watch_icon = QLabel("🔄")
        watch_icon.setStyleSheet("font-size: 28px; background: transparent;")
        header_row.addWidget(watch_icon)
        
        header_info = QVBoxLayout()
        header_info.setSpacing(2)
        
        watch_title = QLabel("Auto-Organize")
        watch_title.setStyleSheet("font-size: 18px; font-weight: 600; color: #7C4DFF; background: transparent;")
        header_info.addWidget(watch_title)
        
        watch_desc = QLabel("Monitor folders and organize new files automatically")
        watch_desc.setStyleSheet("color: #808080; font-size: 13px; background: transparent;")
        header_info.addWidget(watch_desc)
        
        header_row.addLayout(header_info, 1)
        watch_layout.addLayout(header_row)
        
        # Separator line
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setStyleSheet("background-color: rgba(124, 77, 255, 0.2); border: none; max-height: 1px;")
        watch_layout.addWidget(separator)
        
        # Combined folder count + status on one line
        self.watch_folder_label = QLabel("📁 No folders configured")
        self.watch_folder_label.setStyleSheet("""
            font-size: 13px; 
            color: #555555; 
            background: transparent;
            padding: 4px 0;
        """)
        watch_layout.addWidget(self.watch_folder_label)
        
        # Buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        
        # Edit button (previously Configure)
        self.watch_config_btn = QPushButton("✏️ Edit")
        self.watch_config_btn.setMinimumHeight(42)
        self.watch_config_btn.setMinimumWidth(100)
        self.watch_config_btn.setCursor(Qt.PointingHandCursor)
        self.watch_config_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #7C4DFF;
                border: 2px solid #7C4DFF;
                border-radius: 12px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 16px;
            }
            QPushButton:hover {
                background-color: rgba(124, 77, 255, 0.1);
            }
        """)
        self.watch_config_btn.clicked.connect(self._open_watch_config)
        btn_row.addWidget(self.watch_config_btn)
        
        # Start/Stop button (purple theme)
        self.watch_toggle_btn = QPushButton("▶ Start")
        self.watch_toggle_btn.setMinimumHeight(42)
        self.watch_toggle_btn.setMinimumWidth(120)
        self.watch_toggle_btn.setCursor(Qt.PointingHandCursor)
        self.watch_toggle_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C4DFF, stop:1 #9575FF);
                color: white;
                border: none;
                border-radius: 12px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 20px;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9575FF, stop:1 #B39DFF);
            }
            QPushButton:disabled {
                background: rgba(124, 77, 255, 0.3);
                color: rgba(255, 255, 255, 0.5);
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
            dialog = ApplyInstructionsDialog(self, existing_count, subfolder_count)
            dialog.exec()
            
            if dialog.result_choice == ApplyInstructionsDialog.REORGANIZE_ALL:
                # Flatten and reorganize
                self.auto_watcher._organize_existing_files_with_options(flatten_first=True)
            elif dialog.result_choice == ApplyInstructionsDialog.ORGANIZE_AS_IS:
                # Organize as-is with new instructions
                self.auto_watcher._organize_existing_files_with_options(flatten_first=False)
            # else: CONTINUE_WATCHING - just keep watching with new instructions
        
        # Instructions updated - just update the summary
        self._update_watch_summary()
        logger.info("Applied configuration changes while watching")
    
    def _update_watch_summary(self):
        """Update the watch status display."""
        folder_count = len(settings.auto_organize_folders)
        is_watching = self.auto_watcher and self.auto_watcher.is_running
        
        if folder_count == 0:
            # No folders configured
            self.watch_folder_label.setText("📁 No folders configured")
            self.watch_folder_label.setStyleSheet("""
                font-size: 13px; 
                color: #888888; 
                background: transparent;
                padding: 4px 0;
            """)
            self.watch_toggle_btn.setEnabled(False)
        else:
            # Show folder count + status on one line
            if is_watching:
                status_text = f"📁 {folder_count} folder{'s' if folder_count > 1 else ''} • ✅ Active"
                color = "#7C4DFF"
            else:
                status_text = f"📁 {folder_count} folder{'s' if folder_count > 1 else ''} configured"
                color = "#555555"
            
            self.watch_folder_label.setText(status_text)
            self.watch_folder_label.setStyleSheet(f"""
                font-size: 13px; 
                color: {color}; 
                background: transparent;
                padding: 4px 0;
                font-weight: {'500' if is_watching else '400'};
            """)
            
            self.watch_toggle_btn.setEnabled(True)
        
        # Update button state (purple theme for both states)
        if is_watching:
            self.watch_toggle_btn.setText("⏹ Stop")
            self.watch_toggle_btn.setStyleSheet("""
                QPushButton {
                    background: transparent;
                    color: #7C4DFF;
                    border: 2px solid #7C4DFF;
                    border-radius: 12px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 0 20px;
                }
                QPushButton:hover {
                    background: rgba(124, 77, 255, 0.1);
                }
            """)
        else:
            self.watch_toggle_btn.setText("▶ Start")
            self.watch_toggle_btn.setStyleSheet("""
                QPushButton {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7C4DFF, stop:1 #9575FF);
                    color: white;
                    border: none;
                    border-radius: 12px;
                    font-size: 14px;
                    font-weight: 600;
                    padding: 0 20px;
                }
                QPushButton:hover {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9575FF, stop:1 #B39DFF);
                }
                QPushButton:disabled {
                    background: rgba(124, 77, 255, 0.3);
                    color: rgba(255, 255, 255, 0.5);
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
        # Update folder label to show active status
        folder_count = len(self.watch_folders) if self.watch_folders else len(settings.auto_organize_folders)
        self.watch_folder_label.setText(f"📁 {folder_count} folder{'s' if folder_count > 1 else ''} • ✅ Active")
        self.watch_folder_label.setStyleSheet("""
            font-size: 13px; 
            color: #7C4DFF; 
            background: transparent;
            padding: 4px 0;
            font-weight: 500;
        """)
        
        # Update toggle button to Stop state (purple outline)
        self.watch_toggle_btn.setText("⏹ Stop")
        self.watch_toggle_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: #7C4DFF;
                border: 2px solid #7C4DFF;
                border-radius: 12px;
                font-size: 14px;
                font-weight: 600;
                padding: 0 20px;
            }
            QPushButton:hover {
                background: rgba(124, 77, 255, 0.1);
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
            dialog = ApplyInstructionsDialog(self, existing_count, subfolder_count)
            dialog.exec()
            
            if dialog.result_choice == ApplyInstructionsDialog.REORGANIZE_ALL:
                organize_existing = True
                flatten_first = True
            elif dialog.result_choice == ApplyInstructionsDialog.ORGANIZE_AS_IS:
                organize_existing = True
            # else: CONTINUE_WATCHING - just watch, don't organize existing
        elif is_catch_up:
            organize_existing = True
        
        # Start the watcher
        self.auto_watcher.start(organize_existing=organize_existing, flatten_first=flatten_first)
        
    def _stop_watch_mode(self):
        """Stop watching folders."""
        if self.auto_watcher:
            self.auto_watcher.stop()
        
        # Save last active timestamp for catch-up feature
        settings.update_auto_organize_last_active()
        
        # Update UI using centralized method
        self._update_watch_summary()
    
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
        # Just log it, don't show in UI per user request
        pass
        logger.info(f"Watch organized: {source} -> {dest}")
    
    def _on_watch_file_indexed(self, file_path: str):
        """Handle file indexed signal from watcher."""
        logger.info(f"Watch auto-indexed: {file_path}")
    
    def _on_watch_status(self, status: str):
        """Handle status updates from watcher."""
        logger.info(f"Watch status: {status}")
    
    def _on_watch_error(self, path: str, error: str):
        """Handle errors from watcher."""
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
    
    def _switch_tab(self, index: int):
        """Switch between Organize Now (0) and Auto-Organize (1) tabs."""
        self.content_stack.setCurrentIndex(index)
        
        # Update button checked states
        self.tab_organize_now.setChecked(index == 0)
        self.tab_auto_organize.setChecked(index == 1)
    def _hide_input_cards(self):
        """Hide instruction and destination cards after plan is generated."""
        self.instruction_card.setVisible(False)
        self.dest_card.setVisible(False)
        self.subtitle.setVisible(False)  # Hide subtitle to save space
        self.edit_inputs_button.setVisible(True)
    
    def _show_input_cards(self):
        """Show instruction and destination cards, hide plan (return to input view)."""
        self.instruction_card.setVisible(True)
        self.dest_card.setVisible(True)
        self.subtitle.setVisible(True)  # Show subtitle again
        self.edit_inputs_button.setVisible(False)
        
        # Hide the plan completely - return to clean input view
        self._hide_plan_ui()
        self.apply_button.setVisible(False)
        self.clear_button.setVisible(False)
        self.undo_button.setVisible(False)
        self.feedback_group.setVisible(False)
    
    def _show_plan_summary(self, folder_count: int, file_count: int, total_size_mb: float):
        """Show the plan summary line."""
        self.plan_summary_label.setText(f"📊 {folder_count} folders  •  {file_count} files  •  {total_size_mb:.2f} MB")
        self.plan_summary_label.setVisible(True)
    
    def _show_existing_folders_warning(self, folders: list):
        """Show subtle note for existing folders."""
        if folders:
            if len(folders) <= 3:
                folder_names = ", ".join(folders)
            else:
                folder_names = ", ".join(folders[:3]) + f" +{len(folders) - 3} more"
            self.existing_folders_note.setText(f"Note: {folder_names} already exist — files will be added to them.")
            self.existing_folders_note.setVisible(True)
        else:
            self.existing_folders_note.setVisible(False)
    
    def _hide_plan_ui(self):
        """Hide plan-related UI elements."""
        self.plan_summary_label.setVisible(False)
        self.existing_folders_note.setVisible(False)
        self.results_splitter.setVisible(False)
        self.edit_inputs_button.setVisible(False)

    
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
        """Load indexed files from the database, filtering to destination folder only.
        
        CRITICAL: Only loads files that are WITHIN the destination folder.
        This prevents accidentally moving files from other locations.
        """
        files = []
        self.files_by_id = {}
        excluded_count = 0
        outside_folder_count = 0
        
        # Get destination path for filtering (normalized, case-insensitive on Windows)
        dest_path_str = None
        if self.destination_path:
            dest_path_str = os.path.normpath(str(self.destination_path)).lower()
        
        try:
            with sqlite3.connect(file_index.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM files")
                rows = cursor.fetchall()
            
            for row in rows:
                file_path = row["file_path"]
                
                # CRITICAL: Only include files within the destination folder
                # This prevents files from other indexed locations being moved
                if dest_path_str:
                    normalized_file_path = os.path.normpath(file_path).lower()
                    if not normalized_file_path.startswith(dest_path_str + os.sep) and normalized_file_path != dest_path_str:
                        outside_folder_count += 1
                        continue  # Skip files outside destination folder
                
                # Skip files matching exclusion patterns
                if settings.should_exclude(file_path):
                    excluded_count += 1
                    continue
                
                f = {
                    "id": row["id"],
                    "file_path": file_path,
                    "file_name": row["file_name"],
                    "file_size": row["file_size"] or 0,
                    "label": row["label"] if "label" in row.keys() else None,
                    "caption": row["caption"] if "caption" in row.keys() else None,
                    "tags": self._parse_tags(row["tags"] if "tags" in row.keys() else None),
                    "category": row["category"] if "category" in row.keys() else None,
                }
                files.append(f)
                self.files_by_id[row["id"]] = f
            
            if outside_folder_count > 0:
                logger.info(f"Filtered out {outside_folder_count} files outside destination folder")
            if excluded_count > 0:
                logger.info(f"Excluded {excluded_count} files based on exclusion patterns")
                
        except Exception as e:
            logger.error(f"Error loading files: {e}")
        
        return files
    
    def _check_instruction_for_exclusions(self, instruction: str) -> List[str]:
        """Check if the instruction mentions file types that are in the exclusion list.
        
        Returns a list of matched exclusion patterns, or empty list if none.
        """
        instruction_lower = instruction.lower()
        matched_exclusions = []
        
        # Common file type keywords to check
        file_type_keywords = {
            '.json': ['json', '.json'],
            '.py': ['python', '.py', 'py files'],
            '.js': ['javascript', '.js', 'js files'],
            '.ts': ['typescript', '.ts', 'ts files'],
            '.csv': ['csv', '.csv'],
            '.xml': ['xml', '.xml'],
            '.yaml': ['yaml', '.yaml', '.yml'],
            '.md': ['markdown', '.md', 'md files'],
            '.txt': ['text', '.txt', 'txt files'],
            '.log': ['log', '.log', 'log files'],
            '.env': ['env', '.env', 'environment'],
            '.git': ['git', '.git'],
            '.pyc': ['pyc', '.pyc', 'compiled python'],
        }
        
        for exclusion_pattern in settings.exclusion_patterns:
            pattern_lower = exclusion_pattern.lower()
            
            # Check if pattern is mentioned directly in instruction
            # Remove * from pattern for matching (*.json -> .json or json)
            clean_pattern = pattern_lower.replace('*', '').strip('.')
            
            if clean_pattern and len(clean_pattern) >= 2:
                # Check direct mention
                if clean_pattern in instruction_lower:
                    matched_exclusions.append(exclusion_pattern)
                    continue
                
                # Check if it's a known file type with alternative keywords
                for ext, keywords in file_type_keywords.items():
                    if clean_pattern in ext or ext.strip('.') == clean_pattern:
                        for keyword in keywords:
                            if keyword in instruction_lower:
                                matched_exclusions.append(exclusion_pattern)
                                break
                        break
        
        return list(set(matched_exclusions))  # Remove duplicates
    
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
    
    def _check_for_unindexed_files(self) -> List[str]:
        """
        Scan destination folder for files not in the database.
        
        Returns list of unindexed file paths.
        """
        if not self.destination_path or not self.destination_path.exists():
            return []
        
        unindexed_files = []
        
        try:
            # Get all indexed file paths (normalized for comparison)
            indexed_paths = set()
            with sqlite3.connect(file_index.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT file_path FROM files")
                for row in cursor.fetchall():
                    indexed_paths.add(os.path.normpath(row[0]).lower())
            
            # Scan destination folder for files
            for root, dirs, filenames in os.walk(str(self.destination_path)):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    normalized_path = os.path.normpath(file_path).lower()
                    
                    # Skip if already indexed or matches exclusion
                    if normalized_path in indexed_paths:
                        continue
                    if settings.should_exclude(file_path):
                        continue
                    
                    unindexed_files.append(file_path)
            
        except Exception as e:
            logger.error(f"Error checking for unindexed files: {e}")
        
        return unindexed_files
    
    def generate_plan(self):
        """Request organization plan from LLM."""
        instruction = self.instruction_input.text().strip()
        
        if not self.destination_path:
            return
        
        # Check for unindexed files and offer to index them with full AI analysis
        unindexed_files = self._check_for_unindexed_files()
        if unindexed_files:
            # Ask user if they want to index new files first
            confirmed = ModernConfirmDialog.ask(
                self,
                title="New Files Detected",
                message=f"Found {len(unindexed_files)} new file(s) that haven't been analyzed yet.",
                details=[
                    "AI analysis provides better organization",
                    "Files will be tagged and categorized",
                    "This only takes a moment"
                ],
                info_text="Indexing ensures the AI can make smart organization decisions.",
                yes_text="Index Now",
                no_text="Skip"
            )
            
            if confirmed:
                # Trigger full AI indexing, then continue with plan
                self._index_folder_before_organize(self.destination_path)
                return  # Will call generate_plan again after indexing
        
        # Check if instruction mentions excluded file types
        if instruction:
            excluded_types_mentioned = self._check_instruction_for_exclusions(instruction)
            if excluded_types_mentioned:
                # Show warning and don't proceed
                excluded_list = ", ".join(excluded_types_mentioned)
                QMessageBox.warning(
                    self,
                    "Excluded File Types",
                    f"Your instruction mentions file types that are in your exclusion list:\n\n"
                    f"• {excluded_list}\n\n"
                    f"These files are protected from organization.\n\n"
                    f"To organize them, go to Settings → Exclusions and remove the pattern."
                )
                return
        
        # Auto-organize mode: no instruction provided
        if not instruction:
            confirmed = ModernConfirmDialog.ask(
                self,
                title="Auto-Organize Mode",
                message="AI will analyze your files and propose an organization structure based on:",
                details=[
                    "File types and categories",
                    "AI-generated tags and labels", 
                    "Content analysis"
                ],
                highlight_text="The structure will be kept simple with minimal nesting.",
                info_text="You will preview the plan before anything is moved.",
                yes_text="Continue",
                no_text="Cancel"
            )
            if not confirmed:
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
        
        # Re-filter exclusions after path verification (paths may have changed)
        excluded_after_verify = 0
        filtered_files = []
        for f in files:
            if settings.should_exclude(f["file_path"]):
                excluded_after_verify += 1
                logger.info(f"Excluding file after path verify: {f['file_name']} (matches exclusion pattern)")
            else:
                filtered_files.append(f)
        files = filtered_files
        if excluded_after_verify > 0:
            logger.info(f"Excluded {excluded_after_verify} files based on exclusion patterns (post-verification)")
        
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
        # Details panel removed
        
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
            # Details panel removed - summary shown in plan_summary_label
            
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
        
        # Calculate plan statistics
        folder_count = len(plan.get("folders", {}))
        file_count = sum(len(f_ids) for f_ids in plan.get("folders", {}).values())
        total_size = sum(
            self.files_by_id.get(fid, {}).get('file_size', 0) 
            for folder_files in plan.get("folders", {}).values() 
            for fid in folder_files
        )
        total_size_mb = total_size / (1024 * 1024)
        
        # Show plan summary
        self._show_plan_summary(folder_count, file_count, total_size_mb)
        
        # Check for existing folders and show warning
        existing_folders = []
        if self.destination_path and self.destination_path.exists():
            for folder_name in plan.get("folders", {}).keys():
                proposed_path = self.destination_path / folder_name
                if proposed_path.exists():
                    existing_folders.append(folder_name)
        self._show_existing_folders_warning(existing_folders)
        
        # Hide input cards to focus on the plan
        self._hide_input_cards()
        
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
            # self.details_text.append(f"\n" + "="*50 + f"\nNote: {len(existing_folders)} folder(s) already exist: {folder_list}\nFiles will be added to existing folders.")
        
        folder_count = len(plan.get("folders", {}))
        files_in_plan = sum(len(fids) for fids in plan.get("folders", {}).values())
        valid_moves = len(self.current_moves)
        
        logger.info(f"Plan has {files_in_plan} files, {valid_moves} valid moves possible")
        
        if valid_moves == 0 and files_in_plan > 0:
            self.status_label.setText(f"Plan has {files_in_plan} files but none can be moved")
            self.apply_button.setEnabled(False)
            ModernInfoDialog.show_warning(
                self,
                title="No Files to Move",
                message=f"The AI proposed organizing {files_in_plan} file(s), but none need to be moved.",
                details=[
                    "Files are already in the destination folder",
                    "Files were already moved or deleted",
                    "Files no longer exist at their indexed paths"
                ],
                info_text="If files have been moved, please re-index to update the database."
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
        
        # Connect expand/collapse signals to update arrows
        try:
            self.plan_tree.itemExpanded.disconnect()
            self.plan_tree.itemCollapsed.disconnect()
        except:
            pass
        self.plan_tree.itemExpanded.connect(self._on_folder_expanded)
        self.plan_tree.itemCollapsed.connect(self._on_folder_collapsed)
        
        folders = plan.get("folders", {})
        
        for folder_name, file_ids in sorted(folders.items()):
            # Add expand arrow prefix - starts collapsed
            folder_item = QTreeWidgetItem([f"▶  📁 {folder_name}  ({len(file_ids)} files)"])
            folder_item.setExpanded(False)  # Start collapsed
            folder_item.setData(0, Qt.UserRole, {"type": "folder", "name": folder_name})
            
            display_limit = 25
            for i, fid in enumerate(file_ids[:display_limit]):
                try:
                    fid_int = int(fid)
                    file_info = self.files_by_id.get(fid_int, {})
                    fname = file_info.get("file_name", f"id:{fid}")
                    file_item = QTreeWidgetItem([fname])  # No icon, just filename
                    file_item.setData(0, Qt.UserRole, {"type": "file", "id": fid_int})
                    folder_item.addChild(file_item)
                except:
                    pass
            
            if len(file_ids) > display_limit:
                more_item = QTreeWidgetItem([f"+ {len(file_ids) - display_limit} more files..."])
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
        
        # Details panel removed - summary shown in plan_summary_label
    
    def _get_file_icon(self, filename: str) -> str:
        """Get appropriate emoji icon based on file type."""
        ext = filename.lower().split('.')[-1] if '.' in filename else ''
        
        # Images
        if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'tiff']:
            return '🖼️'
        # Videos
        elif ext in ['mp4', 'mov', 'avi', 'mkv', 'webm', 'flv', 'wmv', 'm4v']:
            return '🎬'
        # Audio
        elif ext in ['mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a', 'wma']:
            return '🎵'
        # Documents
        elif ext in ['pdf']:
            return '📕'
        elif ext in ['doc', 'docx']:
            return '📘'
        elif ext in ['txt', 'md', 'rtf']:
            return '📝'
        # Spreadsheets
        elif ext in ['xlsx', 'xls', 'csv']:
            return '📊'
        # Presentations
        elif ext in ['ppt', 'pptx']:
            return '📙'
        # Code
        elif ext in ['py', 'js', 'ts', 'html', 'css', 'java', 'cpp', 'c', 'h', 'rb', 'go', 'rs']:
            return '💻'
        # Data
        elif ext in ['json', 'xml', 'yaml', 'yml']:
            return '📋'
        # Archives
        elif ext in ['zip', 'rar', '7z', 'tar', 'gz']:
            return '📦'
        # Executables
        elif ext in ['exe', 'msi', 'app', 'dmg']:
            return '⚙️'
        # Default
        else:
            return '📄'
    
    def _on_folder_expanded(self, item: QTreeWidgetItem):
        """Update folder arrow when expanded."""
        data = item.data(0, Qt.UserRole)
        if data and data.get("type") == "folder":
            text = item.text(0)
            if text.startswith("▶"):
                item.setText(0, "▼" + text[1:])
    
    def _on_folder_collapsed(self, item: QTreeWidgetItem):
        """Update folder arrow when collapsed."""
        data = item.data(0, Qt.UserRole)
        if data and data.get("type") == "folder":
            text = item.text(0)
            if text.startswith("▼"):
                item.setText(0, "▶" + text[1:])
    
    def _on_tree_item_clicked(self, item: QTreeWidgetItem, column: int):
        """Handle tree item click - toggle expand/collapse for folders."""
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        
        # Toggle expand/collapse for folders
        if data.get("type") == "folder":
            item.setExpanded(not item.isExpanded())
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
            # Details panel removed - summary shown in plan_summary_label
    
    def _show_tree_context_menu(self, position):
        """Show right-click context menu for tree items with pin option."""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction
        
        item = self.plan_tree.itemAt(position)
        if not item:
            return
        
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #FFFFFF;
                border: 1px solid #E0E0E0;
                border-radius: 8px;
                padding: 6px 4px;
            }
            QMenu::item {
                padding: 8px 20px;
                border-radius: 6px;
                font-family: "Segoe UI", sans-serif;
                font-size: 13px;
            }
            QMenu::item:selected {
                background-color: #F5F0FF;
                color: #7C4DFF;
            }
        """)
        
        if data.get("type") == "file":
            fid = data.get("id")
            file_info = self.files_by_id.get(fid, {})
            file_path = file_info.get("file_path", "")
            file_name = file_info.get("file_name", "Unknown")
            
            if file_path:
                is_pinned = settings.is_pinned(file_path)
                
                if is_pinned:
                    unpin_action = QAction(f"📌 Unpin '{file_name}'", self)
                    unpin_action.triggered.connect(lambda: self._unpin_from_tree(file_path, fid))
                    menu.addAction(unpin_action)
                else:
                    pin_action = QAction(f"📌 Pin '{file_name}' (never organize)", self)
                    pin_action.triggered.connect(lambda: self._pin_from_tree(file_path, fid))
                    menu.addAction(pin_action)
        
        elif data.get("type") == "folder":
            folder_name = data.get("name", "Unknown")
            if self.destination_path:
                folder_path = str(self.destination_path / folder_name)
                is_pinned = settings.is_pinned(folder_path)
                
                if is_pinned:
                    unpin_action = QAction(f"📌 Unpin folder '{folder_name}'", self)
                    unpin_action.triggered.connect(lambda: self._unpin_folder_from_tree(folder_path, item))
                    menu.addAction(unpin_action)
                else:
                    pin_action = QAction(f"📌 Pin folder '{folder_name}' (never organize contents)", self)
                    pin_action.triggered.connect(lambda: self._pin_folder_from_tree(folder_path, item))
                    menu.addAction(pin_action)
        
        if menu.actions():
            menu.exec(self.plan_tree.viewport().mapToGlobal(position))
    
    def _pin_from_tree(self, file_path: str, file_id: int):
        """Pin a file from the tree view and remove it from current plan."""
        if settings.add_pinned_path(file_path):
            # Remove from current moves
            self.current_moves = [m for m in self.current_moves if m.get("file_id") != file_id]
            
            # Regenerate tree display
            if self.current_plan:
                # Remove the file from the plan
                for folder_name, file_ids in self.current_plan.get("folders", {}).items():
                    if file_id in file_ids:
                        file_ids.remove(file_id)
                    if str(file_id) in file_ids:
                        file_ids.remove(str(file_id))
                
                # Redisplay the plan
                self._display_plan(self.current_plan)
            
            logger.info(f"Pinned file: {file_path}")
            self.status_label.setText(f"📌 Pinned '{Path(file_path).name}' - removed from plan")
    
    def _unpin_from_tree(self, file_path: str, file_id: int):
        """Unpin a file from the tree view."""
        settings.remove_pinned_path(file_path)
        logger.info(f"Unpinned file: {file_path}")
        self.status_label.setText(f"Unpinned '{Path(file_path).name}'")
    
    def _pin_folder_from_tree(self, folder_path: str, item: QTreeWidgetItem):
        """Pin a folder from the tree view."""
        if settings.add_pinned_path(folder_path):
            # Remove all files in this folder from current moves
            folder_name = Path(folder_path).name
            if folder_name in self.current_plan.get("folders", {}):
                file_ids = self.current_plan["folders"][folder_name]
                self.current_moves = [m for m in self.current_moves 
                                      if m.get("file_id") not in file_ids 
                                      and str(m.get("file_id")) not in file_ids]
                del self.current_plan["folders"][folder_name]
            
            # Redisplay the plan
            self._display_plan(self.current_plan)
            
            logger.info(f"Pinned folder: {folder_path}")
            self.status_label.setText(f"📌 Pinned folder '{folder_name}' - removed from plan")
    
    def _unpin_folder_from_tree(self, folder_path: str, item: QTreeWidgetItem):
        """Unpin a folder from the tree view."""
        settings.remove_pinned_path(folder_path)
        folder_name = Path(folder_path).name
        logger.info(f"Unpinned folder: {folder_path}")
        self.status_label.setText(f"Unpinned folder '{folder_name}'")

    def apply_organization(self):
        """Execute the organization plan after user confirmation."""
        logger.info(f"apply_organization called. current_moves count: {len(self.current_moves)}")
        
        if not self.current_moves:
            logger.warning("Apply clicked but current_moves is empty")
            ModernInfoDialog.show_warning(
                self,
                title="No Files to Move",
                message="No files can be moved.",
                details=[
                    "Files have already been moved or deleted",
                    "Files no longer exist at their indexed paths"
                ],
                info_text="Try re-indexing your files in Index Files first."
            )
            return
        
        folder_count = len(self.current_plan.get("folders", {}))
        file_count = len(self.current_moves)
        
        # Use modern styled dialog matching app theme
        dialog = ModernConfirmDialog(
            parent=self,
            title="Confirm Organization",
            message=f"Move {file_count} files into {folder_count} folders?",
            highlight_text=str(self.destination_path),
            info_text="This will physically move the files.\nA log will be saved for reference.",
            yes_text="Move Files",
            no_text="Cancel"
        )
        
        if not dialog.exec():
            return
        
        # Final safety check: filter out any excluded files before moving
        filtered_moves = []
        excluded_count = 0
        for m in self.current_moves:
            if settings.should_exclude(m["source_path"]):
                excluded_count += 1
                logger.info(f"Skipping excluded file in apply: {m['file_name']}")
            else:
                filtered_moves.append(m)
        
        if excluded_count > 0:
            logger.info(f"Excluded {excluded_count} files from final move (matched exclusion patterns)")
        
        if not filtered_moves:
            ModernInfoDialog.show_info(
                self,
                title="No Files to Move",
                message=f"All {excluded_count} file(s) matched exclusion patterns and were skipped.",
                info_text="You can modify exclusion patterns in Settings."
            )
            return
        
        move_plan = []
        for m in filtered_moves:
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
        
        success, errors, log_file, renamed_count = apply_moves(move_plan)
        
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
            
            # Build details list for success dialog
            details = [
                f"Organized {len(move_plan)} file(s)",
                "File paths updated in database"
            ]
            if renamed_count > 0:
                details.append(f"{renamed_count} file(s) renamed to avoid duplicates")
            if cleanup_msg:
                details.append(cleanup_msg.strip())
            
            ModernInfoDialog.show_info(
                self,
                title="Organization Complete",
                message="Your files have been organized successfully!",
                details=details,
                info_text=f"You can use Undo to reverse this. Log saved to:\n{log_file}",
                ok_text="Done"
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
            
            # Build error details (first 5 errors)
            error_details = errors[:5]
            if len(errors) > 5:
                error_details.append(f"... and {len(errors) - 5} more errors")
            
            ModernInfoDialog.show_warning(
                self,
                title="Partial Failure",
                message=f"Some files could not be moved. {paths_updated} file(s) were successfully organized.",
                details=error_details,
                info_text="Check the log file for more details."
            )
            self.status_label.setText(f"Completed with {len(errors)} errors")
            self.apply_button.setEnabled(True)

    def clear_plan(self):
        """Clear the current plan and reset UI."""
        self.current_plan = None
        self.current_moves = []
        self.original_instruction = None
        self.plan_tree.clear()
        self.apply_button.setEnabled(False)
        self.feedback_group.setVisible(False)
        self.feedback_input.clear()
        
        # Hide plan UI elements
        self.apply_button.setVisible(False)
        self.clear_button.setVisible(False)
        self.undo_button.setVisible(False)
        self._hide_plan_ui()
        
        # Show input cards again
        self._show_input_cards()
        
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
    
    def _show_history_dialog(self):
        """Show the organization history dialog."""
        dialog = HistoryDialog(self)
        dialog.exec()
    
    def _show_pinned_dialog(self):
        """Show the pinned items management dialog."""
        dialog = PinnedDialog(self)
        dialog.exec()
    
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
