"""Redesign WatchConfigDialog to match the modern purple-bluish brand theme."""
import os

file_path = os.path.join(os.path.dirname(__file__), '..', 'ui', 'organize_page.py')

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Replace the entire WatchConfigDialog class with a modern version
old_class_def = '''class WatchConfigDialog(QDialog):
    """
    Dialog for configuring Watch & Auto-Organize folders with per-folder instructions.
    
    Features:
    - Add/remove folders to watch
    - Set per-folder organization instructions
    - Toggle auto-start on app open
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Watch & Auto-Organize")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        
        # Track folder data: {path: instruction}
        self.folder_data: Dict[str, str] = {}
        # Track folder widgets for updates
        self.folder_widgets: Dict[str, Dict] = {}
        
        self._setup_ui()
        self._load_from_settings()
    
    def _setup_ui(self):
        """Setup the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        
        # Header
        header = QLabel("Watch & Auto-Organize Configuration")
        header.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(header)
        
        subtitle = QLabel(
            "Add folders to watch for new files. Each folder can have its own organization instructions."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #888;")
        layout.addWidget(subtitle)
        
        # Add folder button
        add_row = QHBoxLayout()
        self.add_folder_btn = QPushButton("+ Add Folder")
        self.add_folder_btn.setMinimumHeight(36)
        self.add_folder_btn.setStyleSheet("""
            QPushButton {
                background-color: #4a9eff;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: bold;
                padding: 0 20px;
            }
            QPushButton:hover { background-color: #3a8eef; }
        """)
        self.add_folder_btn.clicked.connect(self._add_folder)
        add_row.addWidget(self.add_folder_btn)
        add_row.addStretch()
        layout.addLayout(add_row)
        
        # Scroll area for folder list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumHeight(250)
        
        self.folders_container = QWidget()
        self.folders_layout = QVBoxLayout(self.folders_container)
        self.folders_layout.setContentsMargins(0, 0, 0, 0)
        self.folders_layout.setSpacing(12)
        
        # Placeholder for when no folders
        self.no_folders_label = QLabel("No folders configured. Click '+ Add Folder' to get started.")
        self.no_folders_label.setStyleSheet("color: #888; font-style: italic; padding: 20px;")
        self.no_folders_label.setAlignment(Qt.AlignCenter)
        self.folders_layout.addWidget(self.no_folders_label)
        
        self.folders_layout.addStretch()
        
        scroll.setWidget(self.folders_container)
        layout.addWidget(scroll, 1)
        
        # Action buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumHeight(40)
        cancel_btn.setMinimumWidth(100)
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        save_btn = QPushButton("Save")
        save_btn.setMinimumHeight(40)
        save_btn.setMinimumWidth(100)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #2ecc71;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #27ae60; }
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
        
        # Create frame
        frame = QFrame()
        frame.setStyleSheet("""
            QFrame {
                background-color: rgba(100, 100, 100, 0.1);
                border-radius: 8px;
                padding: 8px;
            }
        """)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setSpacing(8)
        
        # Header row with path and remove button
        header_row = QHBoxLayout()
        
        path_label = QLabel(folder_path)
        path_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        path_label.setWordWrap(True)
        header_row.addWidget(path_label, 1)
        
        remove_btn = QPushButton("✕")
        remove_btn.setFixedSize(28, 28)
        remove_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c;
                color: white;
                border: none;
                border-radius: 14px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #c0392b; }
        """)
        remove_btn.clicked.connect(lambda: self._remove_folder(folder_path))
        header_row.addWidget(remove_btn)
        
        frame_layout.addLayout(header_row)
        
        # Instruction input
        instruction_label = QLabel("Organization instruction (optional):")
        instruction_label.setStyleSheet("color: #888; font-size: 11px;")
        frame_layout.addWidget(instruction_label)
        
        instruction_input = QLineEdit()
        instruction_input.setPlaceholderText("e.g., Organize by file type, Group by date, Sort by project...")
        instruction_input.setText(instruction)
        instruction_input.setMinimumHeight(32)
        instruction_input.textChanged.connect(
            lambda text, fp=folder_path: self._on_instruction_changed(fp, text)
        )
        frame_layout.addWidget(instruction_input)
        
        # Store widgets for later reference
        self.folder_widgets[folder_path] = {
            'widget': frame,
            'input': instruction_input
        }
        
        # Add to layout
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
        
        self.accept()'''

new_class_def = '''class WatchConfigDialog(QDialog):
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
        
        self.accept()'''

if old_class_def in content:
    content = content.replace(old_class_def, new_class_def)
    print("Replaced WatchConfigDialog with modern light-theme redesign")
else:
    print("Could not find WatchConfigDialog class definition to replace")
    # Debug: Print first 200 chars of old def to see if formatting matches
    print("Searching for:")
    print(old_class_def[:200])

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Done!")
