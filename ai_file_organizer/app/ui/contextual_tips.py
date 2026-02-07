"""
Contextual Tips System for AI File Organizer
Provides pulsing dot indicators and spotlight popups for feature discovery
"""

from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFrame, QGraphicsDropShadowEffect, QApplication
)
from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QRect, QPoint, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QPainterPath


# ============================================================================
# TIP DEFINITIONS
# ============================================================================

CONTEXTUAL_TIPS = {
    # Organize Page - High Priority
    "history_button": {
        "title": "📋 Organization History",
        "message": "• View your past organization actions\n• See what files were moved\n• Undo any previous operation",
    },
    "pinned_button": {
        "title": "📌 Pinned Files & Folders",
        "message": "• Lock files to prevent organization\n• Pin entire folders to protect them\n• Right-click items in the plan to pin",
    },
    "undo_button": {
        "title": "↩️ Undo Last Action",
        "message": "• Instantly reverse your last organization\n• Files return to original locations\n• Works even after closing the app",
    },
    "voice_button": {
        "title": "🎤 Voice Instructions",
        "message": "• Speak instead of typing\n• Say: 'Organize by date'\n• Click and hold to record",
    },
    "edit_button": {
        "title": "✏️ Edit Plan",
        "message": "• Go back to modify your instruction\n• Change the destination folder\n• Generate a new organization plan",
    },
    "apply_button": {
        "title": "✓ Apply Organization",
        "message": "• Execute the plan and move files\n• Creates folders automatically\n• Can be undone if needed",
    },
    
    # Settings Page - Medium Priority
    "exclusions_section": {
        "title": "🚫 Exclusion Patterns",
        "message": "• Add patterns like *.json or *.py\n• Protected files won't be organized\n• Wildcards supported (*.log, temp*)",
    },
    "welcome_guide_button": {
        "title": "📖 Welcome Guide",
        "message": "• Re-run the onboarding tour anytime\n• Great refresher for all features\n• Shows interactive walkthrough",
    },
    
    # Search Page - Low Priority
    "search_input": {
        "title": "🔍 AI-Powered Search",
        "message": "• Search by content, not just names\n• Try: 'vacation photos' or 'tax 2024'\n• AI understands what's inside files",
    },
}


# ============================================================================
# TIP INDICATOR (Pulsing Dot)
# ============================================================================

class TipIndicator(QLabel):
    """Arrow badge indicator showing 'TIP' for contextual tips"""
    
    clicked = Signal()
    
    def __init__(self, parent=None, position="right"):
        super().__init__(parent)
        self.position = position  # "right", "left", "above", "below"
        self._update_text()
        self.setFixedHeight(24)
        self.setMinimumWidth(50)
        self.setCursor(Qt.PointingHandCursor)
        self.setAlignment(Qt.AlignCenter)
        
        self._opacity = 1.0
        self._setup_style()
        self._start_pulse()
    
    def _update_text(self):
        """Update text based on position"""
        if self.position == "right":
            self.setText("◀ TIP")
        elif self.position == "left":
            self.setText("TIP ▶")
        elif self.position == "above":
            self.setText("TIP ▼")
        elif self.position == "below":
            self.setText("▲ TIP")
        else:
            self.setText("◀ TIP")
    
    def set_position(self, position):
        """Set the position and update arrow direction"""
        self.position = position
        self._update_text()
    
    def _setup_style(self):
        self.setStyleSheet("""
            QLabel {
                color: white;
                font-size: 11px;
                font-weight: bold;
                background-color: #7C4DFF;
                border-radius: 12px;
                padding: 4px 10px;
            }
        """)
    
    def _start_pulse(self):
        """Start a subtle pulsing animation"""
        self.pulse_timer = QTimer(self)
        self.pulse_timer.timeout.connect(self._pulse_step)
        self.pulse_direction = -1
        self.pulse_timer.start(80)
    
    def _pulse_step(self):
        """Animate the pulse - subtle opacity change"""
        self._opacity += self.pulse_direction * 0.03
        if self._opacity <= 0.7:
            self.pulse_direction = 1
        elif self._opacity >= 1.0:
            self.pulse_direction = -1
        
        # Update style with opacity for background
        self.setStyleSheet(f"""
            QLabel {{
                color: white;
                font-size: 11px;
                font-weight: bold;
                background-color: rgba(124, 77, 255, {self._opacity});
                border-radius: 12px;
                padding: 4px 10px;
            }}
        """)
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
    
    def stop_pulse(self):
        """Stop the pulsing and hide"""
        if hasattr(self, 'pulse_timer'):
            self.pulse_timer.stop()
        self.hide()


# ============================================================================
# SPOTLIGHT OVERLAY (Reused from onboarding)
# ============================================================================

class TipSpotlightOverlay(QWidget):
    """Semi-transparent overlay with a spotlight hole for tips"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.spotlight_rect = None
        self.opacity = 0.5
        
        if parent:
            self.setGeometry(parent.rect())
            self.raise_()
    
    def set_spotlight(self, rect):
        """Set the area to spotlight"""
        if rect:
            # Add padding around the rect
            self.spotlight_rect = QRect(
                rect.x() - 10, rect.y() - 10,
                rect.width() + 20, rect.height() + 20
            )
        else:
            self.spotlight_rect = None
        self.update()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        overlay_color = QColor(0, 0, 0, int(255 * self.opacity))
        
        if self.spotlight_rect:
            # Create path for entire widget minus spotlight
            path = QPainterPath()
            path.addRect(0, 0, self.width(), self.height())
            
            spotlight_path = QPainterPath()
            spotlight_path.addRoundedRect(
                float(self.spotlight_rect.x()),
                float(self.spotlight_rect.y()),
                float(self.spotlight_rect.width()),
                float(self.spotlight_rect.height()),
                12.0, 12.0
            )
            path = path.subtracted(spotlight_path)
            
            painter.fillPath(path, overlay_color)
            
            # Draw glowing border
            glow_pen = QPen(QColor("#7C4DFF"))
            glow_pen.setWidth(3)
            painter.setPen(glow_pen)
            painter.drawRoundedRect(self.spotlight_rect, 12, 12)
        else:
            painter.fillRect(self.rect(), overlay_color)
    
    def mousePressEvent(self, event):
        # Allow clicks to pass through to dismiss
        event.accept()


# ============================================================================
# TIP POPUP
# ============================================================================

class TipPopup(QFrame):
    """Tooltip popup with tip content and Got it! button"""
    
    dismissed = Signal(str)  # Emits tip_id when dismissed
    
    def __init__(self, tip_id, title, message, parent=None):
        super().__init__(parent)
        self.tip_id = tip_id
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        
        self._setup_ui(title, message)
        self._apply_style()
    
    def _setup_ui(self, title, message):
        self.setFixedWidth(280)
        
        # Container
        container = QFrame(self)
        container.setObjectName("tipContainer")
        
        layout = QVBoxLayout(container)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        
        # Title
        title_label = QLabel(title)
        title_label.setObjectName("tipTitle")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)
        
        # Message
        msg_label = QLabel(message)
        msg_label.setObjectName("tipMessage")
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)
        
        # Got it button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.got_it_btn = QPushButton("Got it!")
        self.got_it_btn.setObjectName("gotItButton")
        self.got_it_btn.setCursor(Qt.PointingHandCursor)
        self.got_it_btn.clicked.connect(self._on_got_it)
        btn_layout.addWidget(self.got_it_btn)
        
        layout.addLayout(btn_layout)
        
        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.addWidget(container)
        
        # Shadow
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 50))
        container.setGraphicsEffect(shadow)
    
    def _apply_style(self):
        self.setStyleSheet("""
            QFrame#tipContainer {
                background-color: white;
                border-radius: 12px;
                border: 2px solid rgba(124, 77, 255, 0.3);
            }
            
            QLabel#tipTitle {
                color: #1a1a2e;
                font-size: 15px;
                font-weight: 700;
            }
            
            QLabel#tipMessage {
                color: #555555;
                font-size: 13px;
                line-height: 1.4;
            }
            
            QPushButton#gotItButton {
                background-color: #7C4DFF;
                border: none;
                border-radius: 8px;
                color: white;
                font-size: 13px;
                font-weight: 600;
                padding: 8px 20px;
                min-width: 80px;
            }
            QPushButton#gotItButton:hover {
                background-color: #9575FF;
            }
        """)
    
    def _on_got_it(self):
        self.dismissed.emit(self.tip_id)
        self.close()


# ============================================================================
# CONTEXTUAL TIPS MANAGER
# ============================================================================

class ContextualTipsManager:
    """Manages contextual tips display and tracking"""
    
    def __init__(self, main_window, settings):
        self.main_window = main_window
        self.settings = settings
        self.active_indicators = {}  # tip_id -> TipIndicator
        self.spotlight = None
        self.current_popup = None
    
    def add_tip(self, tip_id, target_widget, force_position=None):
        """Add a tip indicator next to a widget (but don't show yet)"""
        # Skip if already seen
        if tip_id in self.settings.seen_tips:
            return
        
        # Skip if already added
        if tip_id in self.active_indicators:
            return
        
        # Skip if tip doesn't exist
        if tip_id not in CONTEXTUAL_TIPS:
            return
        
        # Create indicator but don't show yet
        indicator = TipIndicator(self.main_window)
        indicator.clicked.connect(lambda tid=tip_id: self._show_tip(tid, target_widget))
        indicator.hide()  # Hide initially
        
        self.active_indicators[tip_id] = {
            'indicator': indicator,
            'target': target_widget,
            'force_position': force_position  # "below", "above", "left", "right" or None
        }
    
    def show_tips_for_visible_widgets(self):
        """Show tips only for widgets that are currently visible AND on the active page"""
        for tip_id, data in list(self.active_indicators.items()):
            # Double-check it hasn't been seen
            if tip_id in self.settings.seen_tips:
                data['indicator'].hide()
                continue
            
            indicator = data['indicator']
            target = data['target']
            force_pos = data.get('force_position')
            
            # Check if target widget AND all its parents are truly visible
            if self._is_widget_truly_visible(target):
                try:
                    # Also check if it's in the visible viewport
                    pos = target.mapTo(self.main_window, QPoint(0, 0))
                    if pos.x() >= 0 and pos.y() >= 0:
                        self._position_indicator(indicator, target, force_pos)
                        indicator.show()
                        indicator.raise_()
                    else:
                        indicator.hide()
                except:
                    indicator.hide()
            else:
                indicator.hide()
    
    def _is_widget_truly_visible(self, widget):
        """Check if a widget is truly visible by checking all parent widgets"""
        if not widget or not widget.isVisible() or not widget.isEnabled():
            return False
        
        # Walk up the parent chain to check all parents are visible
        parent = widget.parent()
        while parent:
            # If parent is not visible, widget isn't truly visible
            if not parent.isVisible():
                return False
            
            # Check if parent is a stacked widget and this isn't the current widget
            if hasattr(parent, 'currentWidget') and hasattr(parent, 'indexOf'):
                # This is a QStackedWidget
                current_index = parent.currentIndex()
                # Find which index our widget is in
                child = widget
                while child.parent() != parent:
                    child = child.parent()
                    if child is None:
                        break
                
                if child:
                    widget_index = parent.indexOf(child)
                    if widget_index != -1 and widget_index != current_index:
                        return False
            
            parent = parent.parent()
        
        return True
    
    def hide_all_tips(self):
        """Hide all tip indicators"""
        for data in self.active_indicators.values():
            data['indicator'].hide()
    
    def _position_indicator(self, indicator, target_widget, force_position=None):
        """Position the indicator next to the target widget with correct arrow"""
        try:
            pos = target_widget.mapTo(self.main_window, QPoint(0, 0))
            main_width = self.main_window.width()
            main_height = self.main_window.height()
            
            # Calculate potential positions
            indicator_w = indicator.minimumWidth() + 10
            indicator_h = indicator.height()
            
            # If force_position is set, use that
            if force_position == "below":
                below_x = pos.x() + (target_widget.width() - indicator_w) // 2
                below_y = pos.y() + target_widget.height() + 6
                indicator.set_position("below")
                indicator.move(below_x, below_y)
                return
            elif force_position == "above":
                above_x = pos.x() + (target_widget.width() - indicator_w) // 2
                above_y = pos.y() - indicator_h - 6
                indicator.set_position("above")
                indicator.move(above_x, above_y)
                return
            elif force_position == "left":
                left_x = pos.x() - indicator_w - 6
                left_y = pos.y() + (target_widget.height() - indicator_h) // 2
                indicator.set_position("left")
                indicator.move(left_x, left_y)
                return
            elif force_position == "right":
                right_x = pos.x() + target_widget.width() + 6
                right_y = pos.y() + (target_widget.height() - indicator_h) // 2
                indicator.set_position("right")
                indicator.move(right_x, right_y)
                return
            
            # Auto-position: Try right side first
            right_x = pos.x() + target_widget.width() + 6
            right_y = pos.y() + (target_widget.height() - indicator_h) // 2
            
            if right_x + indicator_w < main_width - 20:
                indicator.set_position("right")
                indicator.move(right_x, right_y)
                return
            
            # Try below
            below_x = pos.x() + (target_widget.width() - indicator_w) // 2
            below_y = pos.y() + target_widget.height() + 6
            
            if below_y + indicator_h < main_height - 20:
                indicator.set_position("below")
                indicator.move(below_x, below_y)
                return
            
            # Try left side
            left_x = pos.x() - indicator_w - 6
            left_y = pos.y() + (target_widget.height() - indicator_h) // 2
            
            if left_x > 20:
                indicator.set_position("left")
                indicator.move(left_x, left_y)
                return
            
            # Try above
            above_x = pos.x() + (target_widget.width() - indicator_w) // 2
            above_y = pos.y() - indicator_h - 6
            
            indicator.set_position("above")
            indicator.move(above_x, above_y)
            
        except Exception as e:
            pass
    
    def _show_tip(self, tip_id, target_widget):
        """Show the tip popup with spotlight"""
        tip_data = CONTEXTUAL_TIPS.get(tip_id)
        if not tip_data:
            return
        
        # Hide indicator
        if tip_id in self.active_indicators:
            self.active_indicators[tip_id]['indicator'].hide()
        
        # Create spotlight
        self.spotlight = TipSpotlightOverlay(self.main_window)
        self.spotlight.setGeometry(self.main_window.rect())
        
        # Get target rect
        try:
            widget_pos = target_widget.mapTo(self.main_window, QPoint(0, 0))
            widget_rect = QRect(widget_pos.x(), widget_pos.y(), 
                              target_widget.width(), target_widget.height())
            self.spotlight.set_spotlight(widget_rect)
        except:
            self.spotlight.set_spotlight(None)
        
        self.spotlight.show()
        self.spotlight.raise_()
        
        # Create popup
        self.current_popup = TipPopup(
            tip_id,
            tip_data["title"],
            tip_data["message"],
            self.main_window
        )
        self.current_popup.dismissed.connect(self._on_tip_dismissed)
        
        # Position popup near the target (below or above)
        self._position_popup(target_widget)
        
        self.current_popup.show()
        self.current_popup.raise_()
    
    def _position_popup(self, target_widget):
        """Position the popup near the target widget"""
        if not self.current_popup:
            return
        
        try:
            widget_pos = target_widget.mapTo(self.main_window, QPoint(0, 0))
            popup_width = self.current_popup.width()
            popup_height = self.current_popup.sizeHint().height()
            
            # Try to position below the widget
            x = widget_pos.x() + (target_widget.width() - popup_width) // 2
            y = widget_pos.y() + target_widget.height() + 15
            
            # Ensure popup stays within window bounds
            main_rect = self.main_window.rect()
            
            # Adjust horizontal position
            if x < 10:
                x = 10
            elif x + popup_width > main_rect.width() - 10:
                x = main_rect.width() - popup_width - 10
            
            # If below doesn't fit, try above
            if y + popup_height > main_rect.height() - 10:
                y = widget_pos.y() - popup_height - 15
            
            self.current_popup.move(x, y)
        except:
            # Fallback to center
            self.current_popup.move(
                (self.main_window.width() - self.current_popup.width()) // 2,
                (self.main_window.height() - self.current_popup.sizeHint().height()) // 2
            )
    
    def _on_tip_dismissed(self, tip_id):
        """Handle tip dismissal - remove permanently"""
        # Mark as seen in settings FIRST
        self.settings.mark_tip_seen(tip_id)
        
        # Hide spotlight
        if self.spotlight:
            self.spotlight.hide()
            self.spotlight.deleteLater()
            self.spotlight = None
        
        # Remove and delete indicator completely
        if tip_id in self.active_indicators:
            indicator = self.active_indicators[tip_id]['indicator']
            indicator.stop_pulse()
            indicator.hide()
            indicator.deleteLater()
            del self.active_indicators[tip_id]
        
        self.current_popup = None
        
        self.current_popup = None
    
    def update_positions(self):
        """Update indicator positions (call on resize)"""
        for tip_id, data in self.active_indicators.items():
            self._position_indicator(data['indicator'], data['target'])
    
    def cleanup(self):
        """Clean up all indicators and overlays"""
        for data in self.active_indicators.values():
            data['indicator'].deleteLater()
        self.active_indicators.clear()
        
        if self.spotlight:
            self.spotlight.deleteLater()
            self.spotlight = None
        
        if self.current_popup:
            self.current_popup.close()
            self.current_popup = None
