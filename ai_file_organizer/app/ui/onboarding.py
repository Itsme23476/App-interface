"""
Interactive Onboarding Overlay for AI File Organizer
A floating panel that guides users through the app's features
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QFrame, QWidget, QGraphicsDropShadowEffect, QProgressBar
)
from PySide6.QtCore import Qt, Signal, QPoint, QTimer, QPropertyAnimation, QRect, QEasingCurve, QPropertyAnimation, QRect, QEasingCurve, Property
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter, QPen, QBrush, QPainter, QPen, QBrush, QPainterPath, QRegion
from datetime import datetime, timedelta
import random


class OnboardingOverlay(QDialog):
    """
    Interactive onboarding panel that floats over the app
    and guides users through key features
    """
    
    finished_onboarding = Signal()
    remind_later = Signal()  # Signal for "remind me later"
    
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main_window = main_window
        self.current_step = 0
        self.drag_position = None
        self.is_minimized = False  # For "Try It" mode
        
        # Steps definition with shorter, bullet-point text
        # nav_index: 0=Search, 1=Organize, 2=Index Files, 3=Settings
        # highlight: attribute name on main_window to spotlight
        self.steps = [
            {
                "title": "Welcome to AI File Organizer! 🎉",
                "description": "• Quick tour of key features\n• Takes about 30 seconds\n• Use ← → keys to navigate",
                "nav_index": None,
                "button_text": "Let's Go!",
                "show_try_it": False,
                "highlight": None
            },
            {
                "title": "🔍 Smart Search",
                "description": "• Find files by content, not just names\n• Try: \"vacation photo\" or \"tax document\"\n• AI understands what's inside files",
                "nav_index": 0,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": "search_input"
            },
            {
                "title": "🗂️ Organize Files",
                "description": "• Select a destination folder\n• Click \"Generate Plan\"\n• Review suggestions & apply",
                "nav_index": 1,
                "sub_tab": 0,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": "organize_page"
            },
            {
                "title": "⚡ Auto-Organize",
                "description": "• Set it and forget it\n• Files sorted automatically on arrival\n• Configure watched folders here",
                "nav_index": 1,
                "sub_tab": 1,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": "organize_page"
            },
            {
                "title": "📁 Index Files",
                "description": "• AI learns about your files first\n• Click \"Add Folder\" to start\n• Required before search/organize",
                "nav_index": 2,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": None
            },
            {
                "title": "⚙️ Settings",
                "description": "• Protect files from being moved\n• Add exclusion patterns (.json, .py)\n• Configure app behavior",
                "nav_index": 3,
                "button_text": "Next",
                "show_try_it": False,
                "highlight": None
            },
            {
                "title": "✅ You're Ready!",
                "description": "• Press Ctrl+Alt+H for quick search\n• Check History for past actions\n• Pin files to lock them in place",
                "nav_index": 1,
                "sub_tab": 0,
                "button_text": "Start Using the App",
                "show_try_it": False,
                "highlight": None
            }
        ]
        
        # Spotlight overlay for Phase 3
        self.spotlight = None
        
        self._setup_ui()
        self._apply_styling()
        self._update_step()
    
    def _setup_ui(self):
        """Set up the UI components"""
        # Window settings
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(500, 480)
        
        # Main container with shadow
        self.container = QFrame(self)
        self.container.setObjectName("onboardingContainer")
        self.container.setGeometry(10, 10, 480, 460)
        
        # Add shadow effect
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 60))
        self.container.setGraphicsEffect(shadow)
        
        # Layout
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        
        # Header with step indicator
        header = QHBoxLayout()
        header.setSpacing(8)
        
        self.step_label = QLabel("Step 1 of 7")
        self.step_label.setObjectName("stepLabel")
        header.addWidget(self.step_label)
        
        header.addStretch()
        
        # Remind Me Later button (replaces Skip)
        self.remind_btn = QPushButton("⏰ Remind Me Later")
        self.remind_btn.setObjectName("remindButton")
        self.remind_btn.setCursor(Qt.PointingHandCursor)
        self.remind_btn.clicked.connect(self._remind_later)
        header.addWidget(self.remind_btn)
        
        layout.addLayout(header)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progressBar")
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        layout.addWidget(self.progress_bar)
        
        # Title
        self.title_label = QLabel("Welcome!")
        self.title_label.setObjectName("titleLabel")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        
        # Description
        self.desc_label = QLabel("Description goes here")
        self.desc_label.setObjectName("descLabel")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self.desc_label, 1)
        
        # Keyboard hint
        self.keyboard_hint = QLabel("💡 Use ← → arrow keys  •  Esc to skip")
        self.keyboard_hint.setObjectName("keyboardHint")
        self.keyboard_hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.keyboard_hint)
        
        # Navigation buttons
        nav_layout = QHBoxLayout()
        nav_layout.setSpacing(12)
        
        self.back_btn = QPushButton("← Back")
        self.back_btn.setObjectName("backButton")
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.clicked.connect(self._go_back)
        nav_layout.addWidget(self.back_btn)
        
        nav_layout.addStretch()
        
        # Try It button
        self.try_btn = QPushButton("🎯 Try It")
        self.try_btn.setObjectName("tryButton")
        self.try_btn.setCursor(Qt.PointingHandCursor)
        self.try_btn.clicked.connect(self._try_it)
        self.try_btn.setVisible(False)
        nav_layout.addWidget(self.try_btn)
        
        self.next_btn = QPushButton("Next →")
        self.next_btn.setObjectName("nextButton")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.clicked.connect(self._go_next)
        nav_layout.addWidget(self.next_btn)
        
        layout.addLayout(nav_layout)
        
        # Minimized "Continue" button (shown when trying features)
        self.continue_btn = QPushButton("▶ Continue Tour")
        self.continue_btn.setObjectName("continueButton")
        self.continue_btn.setCursor(Qt.PointingHandCursor)
        self.continue_btn.clicked.connect(self._restore_from_try)
        self.continue_btn.setFixedSize(140, 40)
        self.continue_btn.setParent(self.main_window)
        self.continue_btn.hide()
    
    def _apply_styling(self):
        """Apply the purple theme styling"""
        self.setStyleSheet("""
            QFrame#onboardingContainer {
                background-color: white;
                border-radius: 16px;
                border: 2px solid rgba(124, 77, 255, 0.3);
            }
            
            QLabel#stepLabel {
                color: #7C4DFF;
                font-size: 12px;
                font-weight: 600;
            }
            
            QLabel#titleLabel {
                color: #1a1a2e;
                font-size: 22px;
                font-weight: 700;
            }
            
            QLabel#descLabel {
                color: #444444;
                font-size: 15px;
                line-height: 1.6;
            }
            
            QLabel#keyboardHint {
                color: #999999;
                font-size: 11px;
                padding: 4px;
            }
            
            QProgressBar#progressBar {
                background-color: #E8E0FF;
                border: none;
                border-radius: 3px;
            }
            QProgressBar#progressBar::chunk {
                background-color: #7C4DFF;
                border-radius: 3px;
            }
            
            QPushButton#remindButton {
                background-color: transparent;
                border: none;
                color: #999999;
                font-size: 12px;
                padding: 4px 8px;
            }
            QPushButton#remindButton:hover {
                color: #7C4DFF;
            }
            
            QPushButton#backButton {
                background-color: transparent;
                border: 2px solid #E0E0E0;
                border-radius: 10px;
                color: #666666;
                font-size: 14px;
                font-weight: 600;
                padding: 10px 20px;
                min-width: 90px;
            }
            QPushButton#backButton:hover {
                border-color: #7C4DFF;
                color: #7C4DFF;
            }
            QPushButton#backButton:disabled {
                border-color: #F0F0F0;
                color: #CCCCCC;
            }
            
            QPushButton#tryButton {
                background-color: transparent;
                border: 2px solid #7C4DFF;
                border-radius: 10px;
                color: #7C4DFF;
                font-size: 14px;
                font-weight: 600;
                padding: 10px 16px;
                min-width: 90px;
            }
            QPushButton#tryButton:hover {
                background-color: rgba(124, 77, 255, 0.1);
            }
            
            QPushButton#nextButton {
                background-color: #7C4DFF;
                border: none;
                border-radius: 10px;
                color: white;
                font-size: 14px;
                font-weight: 600;
                padding: 10px 24px;
                min-width: 120px;
            }
            QPushButton#nextButton:hover {
                background-color: #9575FF;
            }
            
            QPushButton#continueButton {
                background-color: #7C4DFF;
                border: none;
                border-radius: 20px;
                color: white;
                font-size: 13px;
                font-weight: 600;
            }
            QPushButton#continueButton:hover {
                background-color: #9575FF;
            }
        """)
    
    def _update_step(self):
        """Update the UI for the current step"""
        step = self.steps[self.current_step]
        
        # Update labels
        self.step_label.setText(f"Step {self.current_step + 1} of {len(self.steps)}")
        self.title_label.setText(step["title"])
        self.desc_label.setText(step["description"])
        self.next_btn.setText(step["button_text"])
        
        # Update progress bar
        progress = int((self.current_step + 1) / len(self.steps) * 100)
        self.progress_bar.setValue(progress)
        
        # Update back button visibility
        self.back_btn.setVisible(self.current_step > 0)
        
        # Update remind button visibility (hide on last step)
        self.remind_btn.setVisible(self.current_step < len(self.steps) - 1)
        
        # Update Try It button visibility
        self.try_btn.setVisible(step.get("show_try_it", False))
        
        # Navigate to the appropriate page in the app
        nav_index = step["nav_index"]
        if nav_index is not None:
            if hasattr(self.main_window, 'page_stack'):
                self.main_window.page_stack.setCurrentIndex(nav_index)
            if hasattr(self.main_window, 'nav_buttons') and nav_index < len(self.main_window.nav_buttons):
                self.main_window.nav_buttons[nav_index].setChecked(True)
        
        # Handle sub-tab switching for Organize page
        sub_tab = step.get("sub_tab")
        if sub_tab is not None and hasattr(self.main_window, 'organize_page'):
            self.main_window.organize_page._switch_tab(sub_tab)
        
        # Phase 3: Update spotlight overlay
        self._update_spotlight(step.get("highlight"))
    
    def _update_spotlight(self, highlight_attr):
        """Update the spotlight overlay to highlight a widget"""
        # Hide existing spotlight when not needed or on certain steps
        if not highlight_attr:
            if self.spotlight:
                self.spotlight.hide()
            return
        
        # Get the widget to highlight
        target_widget = None
        if hasattr(self.main_window, highlight_attr):
            target_widget = getattr(self.main_window, highlight_attr)
        
        if not target_widget:
            if self.spotlight:
                self.spotlight.hide()
            return
        
        # Create spotlight if needed
        if not self.spotlight:
            self.spotlight = SpotlightOverlay(self.main_window)
        
        # Position and show spotlight
        self.spotlight.setGeometry(self.main_window.rect())
        
        # Get widget rect relative to main window
        widget_pos = target_widget.mapTo(self.main_window, QPoint(0, 0))
        widget_rect = QRect(widget_pos.x(), widget_pos.y(), target_widget.width(), target_widget.height())
        
        self.spotlight.set_spotlight(widget_rect)
        self.spotlight.show()
        self.spotlight.raise_()
        
        # Make sure our panel is above the spotlight
        self.raise_()
    
    def _go_next(self):
        """Go to the next step or finish"""
        if self.current_step < len(self.steps) - 1:
            self.current_step += 1
            self._update_step()
        else:
            self._finish_tour()
    
    def _go_back(self):
        """Go to the previous step"""
        if self.current_step > 0:
            self.current_step -= 1
            self._update_step()
    
    def _try_it(self):
        """Minimize panel so user can try the feature"""
        self.is_minimized = True
        self.hide()
        
        # Hide spotlight while trying
        if self.spotlight:
            self.spotlight.hide()
        
        # Show the "Continue Tour" button at bottom-right
        if self.main_window:
            main_geo = self.main_window.geometry()
            btn_x = main_geo.width() - self.continue_btn.width() - 20
            btn_y = main_geo.height() - self.continue_btn.height() - 20
            self.continue_btn.move(btn_x, btn_y)
            self.continue_btn.show()
            self.continue_btn.raise_()
    
    def _restore_from_try(self):
        """Restore the onboarding panel after trying"""
        self.is_minimized = False
        self.continue_btn.hide()
        self.show()
        self.raise_()
        # Re-show spotlight
        step = self.steps[self.current_step]
        self._update_spotlight(step.get("highlight"))
    
    def _remind_later(self):
        """Remind the user later instead of skipping entirely"""
        if self.spotlight:
            self.spotlight.hide()
        self.remind_later.emit()
        self.reject()
    
    def _finish_tour(self):
        """Complete the onboarding with celebration"""
        self.continue_btn.hide()
        if self.spotlight:
            self.spotlight.hide()
        self._show_confetti()  # Phase 4: Confetti celebration!
        self.finished_onboarding.emit()
        self.accept()
    
    def keyPressEvent(self, event: QKeyEvent):
        """Handle keyboard navigation"""
        if event.key() == Qt.Key_Right or event.key() == Qt.Key_Return:
            self._go_next()
        elif event.key() == Qt.Key_Left:
            if self.current_step > 0:
                self._go_back()
        elif event.key() == Qt.Key_Escape:
            self._remind_later()
        else:
            super().keyPressEvent(event)
    
    def mousePressEvent(self, event):
        """Enable dragging the panel"""
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        """Handle dragging"""
        if event.buttons() == Qt.LeftButton and self.drag_position:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """Reset drag position"""
        self.drag_position = None
    
    def showEvent(self, event):
        """Position the panel when shown"""
        super().showEvent(event)
        # Position at bottom-right of main window
        if self.main_window:
            main_geo = self.main_window.geometry()
            x = main_geo.right() - self.width() - 30
            y = main_geo.bottom() - self.height() - 30
            self.move(x, y)
    
    def closeEvent(self, event):
        """Clean up when closing"""
        self.continue_btn.hide()
        if hasattr(self, 'spotlight') and self.spotlight:
            self.spotlight.hide()
        super().closeEvent(event)
    
    def _show_confetti(self):
        """Show confetti celebration animation"""
        if not self.main_window:
            return
        self.confetti = ConfettiWidget(self.main_window)
        self.confetti.show()
        self.confetti.start_animation()


class ConfettiWidget(QWidget):
    """Confetti celebration animation widget"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        
        # Cover the parent window
        if parent:
            self.setGeometry(parent.rect())
        
        # Confetti particles: (x, y, size, color, speed, angle)
        self.particles = []
        self.colors = [
            QColor("#7C4DFF"),  # Purple
            QColor("#B39DDB"),  # Light purple
            QColor("#E8E0FF"),  # Very light purple
            QColor("#FFD700"),  # Gold
            QColor("#FF69B4"),  # Pink
            QColor("#00CED1"),  # Cyan
        ]
        
        # Create particles
        import random
        for _ in range(60):
            self.particles.append({
                'x': random.randint(0, self.width() if self.width() > 0 else 800),
                'y': random.randint(-100, -10),
                'size': random.randint(6, 12),
                'color': random.choice(self.colors),
                'speed': random.uniform(3, 8),
                'wobble': random.uniform(-2, 2),
                'rotation': random.randint(0, 360),
            })
        
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_particles)
        self.frame_count = 0
        self.max_frames = 120  # 2 seconds at 60fps
    
    def start_animation(self):
        """Start the confetti animation"""
        # Reinitialize particles when starting
        import random
        parent_width = self.parent().width() if self.parent() else 800
        self.particles = []
        for _ in range(60):
            self.particles.append({
                'x': random.randint(0, parent_width),
                'y': random.randint(-100, -10),
                'size': random.randint(6, 12),
                'color': random.choice(self.colors),
                'speed': random.uniform(3, 8),
                'wobble': random.uniform(-2, 2),
                'rotation': random.randint(0, 360),
            })
        self.frame_count = 0
        self.timer.start(16)  # ~60fps
    
    def _update_particles(self):
        """Update particle positions"""
        import random
        self.frame_count += 1
        
        for p in self.particles:
            p['y'] += p['speed']
            p['x'] += p['wobble']
            p['rotation'] += 5
        
        self.update()
        
        # Stop after max frames
        if self.frame_count >= self.max_frames:
            self.timer.stop()
            self.hide()
            self.deleteLater()
    
    def paintEvent(self, event):
        """Draw confetti particles"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Fade out in last 30 frames
        opacity = 1.0
        if self.frame_count > self.max_frames - 30:
            opacity = (self.max_frames - self.frame_count) / 30.0
        
        for p in self.particles:
            color = QColor(p['color'])
            color.setAlphaF(opacity)
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.NoPen)
            
            painter.save()
            painter.translate(p['x'], p['y'])
            painter.rotate(p['rotation'])
            
            # Draw rectangle confetti
            painter.drawRect(-p['size']//2, -p['size']//4, p['size'], p['size']//2)
            
            painter.restore()


class SpotlightOverlay(QWidget):
    """Semi-transparent overlay with a spotlight hole"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        
        self.spotlight_rect = None
        self.opacity = 0.6
        
        if parent:
            self.setGeometry(parent.rect())
    
    def set_spotlight(self, widget):
        """Set the widget to spotlight"""
        if widget:
            # Get widget position relative to parent
            pos = widget.mapTo(self.parent(), QPoint(0, 0))
            self.spotlight_rect = QRect(
                pos.x() - 10, pos.y() - 10,
                widget.width() + 20, widget.height() + 20
            )
        else:
            self.spotlight_rect = None
        self.update()
    
    def paintEvent(self, event):
        """Draw the overlay with spotlight hole"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw semi-transparent overlay
        overlay_color = QColor(0, 0, 0, int(255 * self.opacity))
        
        if self.spotlight_rect:
            # Create a path for the entire widget minus the spotlight
            path = QPainterPath()
            path.addRect(0, 0, self.width(), self.height())
            
            # Cut out the spotlight area (rounded rect)
            spotlight_path = QPainterPath()
            spotlight_path.addRoundedRect(
                self.spotlight_rect.x(), self.spotlight_rect.y(),
                self.spotlight_rect.width(), self.spotlight_rect.height(),
                12, 12
            )
            path = path.subtracted(spotlight_path)
            
            painter.fillPath(path, overlay_color)
            
            # Draw glowing border around spotlight
            glow_pen = QPen(QColor("#7C4DFF"))
            glow_pen.setWidth(3)
            painter.setPen(glow_pen)
            painter.drawRoundedRect(self.spotlight_rect, 12, 12)
        else:
            painter.fillRect(self.rect(), overlay_color)
    def _show_confetti(self):
        """Show confetti celebration animation"""
        if not self.main_window:
            return
        self.confetti = ConfettiWidget(self.main_window)
        self.confetti.show()
        self.confetti.start_animation()


class ConfettiWidget(QWidget):
    """Confetti celebration animation widget"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        if parent:
            self.setGeometry(parent.rect())
        self.particles = []
        self.colors = [QColor("#7C4DFF"), QColor("#B39DDB"), QColor("#E8E0FF"), QColor("#FFD700"), QColor("#FF69B4"), QColor("#00CED1")]
        import random
        for _ in range(60):
            self.particles.append({'x': random.randint(0, self.width() if self.width() > 0 else 800), 'y': random.randint(-100, -10), 'size': random.randint(6, 12), 'color': random.choice(self.colors), 'speed': random.uniform(3, 8), 'wobble': random.uniform(-2, 2), 'rotation': random.randint(0, 360)})
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_particles)
        self.frame_count = 0
        self.max_frames = 120
    
    def start_animation(self):
        import random
        parent_width = self.parent().width() if self.parent() else 800
        self.particles = []
        for _ in range(60):
            self.particles.append({'x': random.randint(0, parent_width), 'y': random.randint(-100, -10), 'size': random.randint(6, 12), 'color': random.choice(self.colors), 'speed': random.uniform(3, 8), 'wobble': random.uniform(-2, 2), 'rotation': random.randint(0, 360)})
        self.frame_count = 0
        self.timer.start(16)
    
    def _update_particles(self):
        self.frame_count += 1
        for p in self.particles:
            p['y'] += p['speed']
            p['x'] += p['wobble']
            p['rotation'] += 5
        self.update()
        if self.frame_count >= self.max_frames:
            self.timer.stop()
            self.hide()
            self.deleteLater()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        opacity = 1.0
        if self.frame_count > self.max_frames - 30:
            opacity = (self.max_frames - self.frame_count) / 30.0
        for p in self.particles:
            color = QColor(p['color'])
            color.setAlphaF(opacity)
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.NoPen)
            painter.save()
            painter.translate(p['x'], p['y'])
            painter.rotate(p['rotation'])
            painter.drawRect(-p['size']//2, -p['size']//4, p['size'], p['size']//2)
            painter.restore()


class SpotlightOverlay(QWidget):
    """Semi-transparent overlay with a spotlight hole for Phase 3"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.spotlight_rect = None
        self.opacity = 0.5
        if parent:
            self.setGeometry(parent.rect())
            self.raise_()
    
    def set_spotlight(self, rect):
        if rect:
            self.spotlight_rect = QRect(rect.x() - 8, rect.y() - 8, rect.width() + 16, rect.height() + 16)
        else:
            self.spotlight_rect = None
        self.update()
    
    def paintEvent(self, event):
        from PySide6.QtGui import QPainterPath
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        overlay_color = QColor(0, 0, 0, int(255 * self.opacity))
        if self.spotlight_rect:
            path = QPainterPath()
            path.addRect(0, 0, self.width(), self.height())
            spotlight_path = QPainterPath()
            spotlight_path.addRoundedRect(float(self.spotlight_rect.x()), float(self.spotlight_rect.y()), float(self.spotlight_rect.width()), float(self.spotlight_rect.height()), 12.0, 12.0)
            path = path.subtracted(spotlight_path)
            painter.fillPath(path, overlay_color)
            glow_pen = QPen(QColor("#7C4DFF"))
            glow_pen.setWidth(3)
            painter.setPen(glow_pen)
            painter.drawRoundedRect(self.spotlight_rect, 12, 12)
        else:
            painter.fillRect(self.rect(), overlay_color)
