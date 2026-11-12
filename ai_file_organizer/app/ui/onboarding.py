"""
Interactive Onboarding Overlay for Filect
A floating panel that guides users through the app's features: the first-run guide (nine screens in three sections:
Agents, Your voice, Your files) and, in page-tour mode, every page's "See what it can do" (page_tours.py).

Ported from the Mac (onboarding.py at 92e895f) for Windows:
  * Windows keys and words: Ctrl + Win to dictate (+ Shift to search, + Alt to organize), "the card at the top of
    your screen" (the Mac's notch);
  * Windows page numbers (page_stack: Voice 3, Settings 4); the sidebar button is the nav_button_group's for that page;
  * the panel is a Tool window OWNED by the main window, not "always on top": on Windows that flag floats it over
    every other app too; owned, it stays above Filect only;
  * key caps use Segoe UI (the Mac's SF Pro Display isn't on Windows); the logo comes from Filect's own icon;
  * the voice drawing is Windows' own (a mic with Ctrl + Win key chips).
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QFrame, QWidget, QGraphicsDropShadowEffect, QProgressBar, QPlainTextEdit
)
from PySide6.QtCore import Qt, Signal, QPoint, QTimer, QEvent, QPropertyAnimation, QRect, QRectF, QEasingCurve, Property, QPointF
from PySide6.QtGui import QColor, QFont, QKeyEvent, QPainter, QPen, QBrush, QPainterPath, QRegion, QLinearGradient, QPixmap
from datetime import datetime, timedelta
import random
import math


class OnboardingAnimation(QWidget):
    """Animated illustration widget for onboarding steps"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(440, 130)
        self._art = "welcome"
        self._frame = 0
        self._max_frames = 300  # 5 seconds at 60fps then loops
        
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(16)  # ~60fps
        
        # Accent colors (same in both themes)
        self._purple = QColor("#7C4DFF")
        self._purple_light = QColor("#B39DDB")
        self._gold = QColor("#FFD700")
        self._green = QColor("#4CAF50")
        self._blue = QColor("#42A5F5")
        self._pink = QColor("#FF69B4")
        self._cyan = QColor("#00CED1")
        # Theme-dependent colors
        self._update_theme_colors()
        # Real app logo (purple sparkle icon) for the Welcome step.
        self._logo = self._load_logo()

    def _load_logo(self):
        """Load the bundled Filect app logo (resources/logo.png), dev + frozen."""
        try:
            import sys
            from pathlib import Path
            base = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") \
                else Path(__file__).resolve().parents[2]   # → ai_file_organizer/
            for name in ("logo.png", "icon.png", "iconnn.ico"):
                p = base / "resources" / name
                if p.exists():
                    pm = QPixmap(str(p))
                    if not pm.isNull():
                        # Pre-scale once (source is 1024px) so paintEvent doesn't resample.
                        return pm.scaled(160, 160, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            from PySide6.QtWidgets import QApplication
            ic = QApplication.windowIcon()
            if not ic.isNull():
                return ic.pixmap(160, 160)
        except Exception:
            pass
        return None

    def _update_theme_colors(self):
        """Set theme-dependent colors for animations."""
        from app.ui.theme_manager import get_theme_colors
        c = get_theme_colors()
        self._purple_bg = QColor(c['surface'])
        self._white = QColor(c['text'])
        self._dark = QColor(c['text'])
        self._gray = QColor(c['text_disabled'])
        self._card_bg = QColor(c['border_strong'])
        self._surface = QColor(c['card'])

    def set_art(self, name):
        """Switch to a different animation: one of the _draw_* methods, or ("keys", [...]) / ("icon", name) / ("logos",)."""
        if isinstance(name, (tuple, list)):
            name, self._art_arg = name[0], (name[1] if len(name) > 1 else None)
        self._art = name
        self._frame = 0
        if not self._timer.isActive():
            self._timer.start()
        self.update()
    
    def stop(self):
        self._timer.stop()
    
    def _tick(self):
        self._frame += 1
        if self._frame >= self._max_frames:
            self._frame = 0  # Loop
        self.update()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Draw rounded background
        bg = QPainterPath()
        bg.addRoundedRect(QRectF(0, 0, self.width(), self.height()), 12, 12)
        painter.fillPath(bg, QColor("#16161F"))
        
        # Border
        painter.setPen(QPen(QColor("#252535"), 1.5))
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width()-1, self.height()-1), 12, 12)
        
        getattr(self, f"_draw_{self._art}")(painter)

        painter.end()

    # ── Step 0: Welcome ──────────────────────────────
    def _draw_welcome(self, p: QPainter):
        cx, cy = self.width() / 2, self.height() / 2
        t = self._frame / 60.0  # time in seconds
        
        # Pulsing app icon silhouette
        pulse = 1.0 + 0.08 * math.sin(t * 3)
        size = 40 * pulse
        
        # Real app logo (the purple sparkle icon), gently pulsing.
        p.save()
        p.translate(cx, cy - 8)
        p.scale(pulse, pulse)
        if self._logo is not None and not self._logo.isNull():
            lp = 78  # drawn size (logo is pre-scaled to 160px)
            p.drawPixmap(QRectF(-lp / 2, -lp / 2, lp, lp), self._logo,
                         QRectF(self._logo.rect()))
        else:
            # Fallback (logo missing): the previous hand-drawn folder + sparkle.
            p.setPen(Qt.NoPen)
            p.setBrush(self._purple)
            p.drawRoundedRect(QRectF(-20, -18, 16, 8), 3, 3)
            p.drawRoundedRect(QRectF(-22, -12, 44, 30), 4, 4)
            sparkle_alpha = int(180 + 75 * math.sin(t * 5))
            p.setBrush(QColor(255, 255, 255, sparkle_alpha))
            self._draw_star(p, 0, 3, 8 + 2 * math.sin(t * 4))
        p.restore()
        
        # Floating sparkles around
        for i in range(5):
            angle = t * 1.2 + i * (2 * math.pi / 5)
            radius = 50 + 8 * math.sin(t * 2 + i)
            sx = cx + radius * math.cos(angle)
            sy = cy + radius * math.sin(angle) - 5
            alpha = int(100 + 100 * math.sin(t * 3 + i * 1.5))
            color = QColor(self._purple)
            color.setAlpha(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            s = 3 + 2 * math.sin(t * 4 + i)
            p.drawEllipse(QPointF(sx, sy), s, s)
        
        # "AI File Organizer" text below
        p.setPen(self._purple)
        font = QFont("Segoe UI", 11, QFont.Bold)
        p.setFont(font)
        p.drawText(QRectF(0, cy + 30, self.width(), 25), Qt.AlignCenter, "Filect")
    
    def _draw_star(self, p: QPainter, cx, cy, size):
        """Draw a 4-point sparkle star"""
        path = QPainterPath()
        path.moveTo(cx, cy - size)
        path.lineTo(cx + size * 0.25, cy - size * 0.25)
        path.lineTo(cx + size, cy)
        path.lineTo(cx + size * 0.25, cy + size * 0.25)
        path.lineTo(cx, cy + size)
        path.lineTo(cx - size * 0.25, cy + size * 0.25)
        path.lineTo(cx - size, cy)
        path.lineTo(cx - size * 0.25, cy - size * 0.25)
        path.closeSubpath()
        p.drawPath(path)

    # ── Step 1: Smart Search ─────────────────────────
    def _draw_search(self, p: QPainter):
        t = self._frame / 60.0
        w, h = self.width(), self.height()
        
        # Draw file cards on the left
        file_colors = [self._blue, self._green, self._pink, self._purple_light, self._cyan]
        file_labels = ["📄", "🖼️", "📊", "📝", "🎵"]
        
        for i in range(5):
            fx = 30 + i * 56
            fy = 25
            
            # File card
            p.setPen(Qt.NoPen)
            card_color = QColor("#252535")
            p.setBrush(card_color)
            p.drawRoundedRect(QRectF(fx, fy, 46, 56), 6, 6)
            
            # File color bar on top
            p.setBrush(file_colors[i])
            p.drawRoundedRect(QRectF(fx, fy, 46, 10), 6, 6)
            p.drawRect(QRectF(fx, fy + 5, 46, 5))
            
            # File content lines
            p.setBrush(QColor("#3A3A4A"))
            p.drawRoundedRect(QRectF(fx + 6, fy + 16, 34, 3), 1, 1)
            p.drawRoundedRect(QRectF(fx + 6, fy + 23, 26, 3), 1, 1)
            p.drawRoundedRect(QRectF(fx + 6, fy + 30, 30, 3), 1, 1)
            
            # Icon
            font = QFont("Segoe UI Emoji", 10)
            p.setFont(font)
            p.setPen(self._dark)
            p.drawText(QRectF(fx, fy + 36, 46, 18), Qt.AlignCenter, file_labels[i])
        
        # Magnifying glass sweeping across
        scan_x = 30 + (t * 40) % (5 * 56 + 20)
        
        # Scan beam (vertical line)
        beam_alpha = int(120 + 60 * math.sin(t * 8))
        beam_color = QColor(self._purple)
        beam_color.setAlpha(beam_alpha)
        p.setPen(QPen(beam_color, 2))
        p.drawLine(QPointF(scan_x, 20), QPointF(scan_x, 85))
        
        # Glow on scanned area
        grad = QLinearGradient(scan_x - 30, 0, scan_x, 0)
        grad.setColorAt(0, QColor(124, 77, 255, 0))
        grad.setColorAt(1, QColor(124, 77, 255, 40))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRect(QRectF(scan_x - 30, 20, 30, 65))
        
        # Magnifying glass icon
        mg_x = scan_x + 5
        mg_y = 40
        p.setPen(QPen(self._purple, 3))
        p.setBrush(QColor(124, 77, 255, 30))
        p.drawEllipse(QPointF(mg_x, mg_y), 14, 14)
        # Handle
        p.setPen(QPen(self._purple, 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(mg_x + 10, mg_y + 10), QPointF(mg_x + 18, mg_y + 18))
        
        # "Found!" label appears when glass is over a file
        file_index = int((scan_x - 30) / 56)
        if 0 <= file_index < 5:
            # Highlight the found file
            hx = 30 + file_index * 56
            highlight_alpha = int(80 + 40 * math.sin(t * 6))
            p.setPen(QPen(self._purple, 2))
            p.setBrush(QColor(124, 77, 255, highlight_alpha))
            p.drawRoundedRect(QRectF(hx - 2, 23, 50, 60), 6, 6)
        
        # Search bar at bottom
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#252535"))
        bar_rect = QRectF(60, 95, w - 120, 26)
        p.drawRoundedRect(bar_rect, 13, 13)
        p.setPen(QPen(self._purple, 1.5))
        p.drawRoundedRect(bar_rect, 13, 13)
        
        # Typing animation in search bar
        query = "vacation photos"
        chars_shown = min(len(query), int(t * 3) % (len(query) + 8))
        if chars_shown > len(query):
            chars_shown = len(query)
        display_text = query[:chars_shown]
        
        # Blinking cursor
        cursor = "│" if int(t * 3) % 2 == 0 else ""
        
        p.setPen(self._dark)
        font = QFont("Segoe UI", 9)
        p.setFont(font)
        p.drawText(bar_rect.adjusted(12, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, f"🔍 {display_text}{cursor}")

    # ── Step 4: Index Files ──────────────────────────
    def _draw_index(self, p: QPainter):
        t = self._frame / 60.0
        w, h = self.width(), self.height()
        
        # Grid of files
        cols, rows = 8, 3
        cell_w, cell_h = 40, 30
        grid_x = (w - cols * cell_w) / 2
        grid_y = 15
        
        # Scanning progress
        scan_progress = (t * 0.4) % 1.2  # 0 to 1.2 (extra for completion)
        scanned_cells = int(scan_progress * cols * rows)
        
        for row in range(rows):
            for col in range(cols):
                idx = row * cols + col
                x = grid_x + col * cell_w + 4
                y = grid_y + row * cell_h + 2
                
                is_scanned = idx < scanned_cells
                
                # File card
                p.setPen(Qt.NoPen)
                if is_scanned:
                    card_bg = QColor(124, 77, 255, 40)
                    border_color = self._purple
                else:
                    card_bg = QColor("#252535")
                    border_color = QColor("#3A3A4A")
                
                p.setBrush(card_bg)
                p.drawRoundedRect(QRectF(x, y, cell_w - 8, cell_h - 4), 4, 4)
                p.setPen(QPen(border_color, 1))
                p.drawRoundedRect(QRectF(x, y, cell_w - 8, cell_h - 4), 4, 4)
                
                # Checkmark for scanned
                if is_scanned:
                    p.setPen(QPen(self._green, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                    cx_c = x + (cell_w - 8) / 2
                    cy_c = y + (cell_h - 4) / 2
                    p.drawLine(QPointF(cx_c - 5, cy_c), QPointF(cx_c - 1, cy_c + 4))
                    p.drawLine(QPointF(cx_c - 1, cy_c + 4), QPointF(cx_c + 5, cy_c - 3))
                else:
                    # File icon placeholder
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor("#3A3A4A"))
                    p.drawRoundedRect(QRectF(x + 8, y + 6, 16, 3), 1, 1)
                    p.drawRoundedRect(QRectF(x + 8, y + 12, 12, 3), 1, 1)
        
        # Scanning bar
        scan_y = grid_y + (scan_progress / 1.0) * (rows * cell_h)
        if scan_progress < 1.0:
            scan_bar_color = QColor(124, 77, 255, 150)
            p.setPen(QPen(scan_bar_color, 2))
            p.drawLine(QPointF(grid_x, scan_y), QPointF(grid_x + cols * cell_w, scan_y))
            # Glow
            grad = QLinearGradient(0, scan_y - 8, 0, scan_y)
            grad.setColorAt(0, QColor(124, 77, 255, 0))
            grad.setColorAt(1, QColor(124, 77, 255, 60))
            p.setPen(Qt.NoPen)
            p.setBrush(grad)
            p.drawRect(QRectF(grid_x, scan_y - 8, cols * cell_w, 8))
        
        # Progress text at bottom
        pct = min(100, int(scan_progress / 1.0 * 100))
        p.setPen(self._purple)
        font = QFont("Segoe UI", 9, QFont.Bold)
        p.setFont(font)
        p.drawText(QRectF(0, h - 28, w, 20), Qt.AlignCenter, f"Indexing... {pct}%")

    def _draw_organize(self, p: QPainter):
        t = self._frame / 60.0
        w, h = self.width(), self.height()
        cx = w / 2
        
        # Phase: 0-2s scattered, 2-4s sliding into folders
        phase = (t % 5)
        
        # Target folders on the right
        folders = [
            {"label": "Documents", "color": self._blue, "y": 20},
            {"label": "Photos", "color": self._green, "y": 55},
            {"label": "Music", "color": self._pink, "y": 90},
        ]
        
        folder_x = w - 120
        for f in folders:
            # Folder shape
            p.setPen(Qt.NoPen)
            p.setBrush(f["color"])
            p.drawRoundedRect(QRectF(folder_x, f["y"], 10, 5), 2, 2)
            p.drawRoundedRect(QRectF(folder_x - 2, f["y"] + 4, 100, 28), 4, 4)
            # Label
            p.setPen(self._white)
            font = QFont("Segoe UI", 8, QFont.Bold)
            p.setFont(font)
            p.drawText(QRectF(folder_x - 2, f["y"] + 4, 100, 28), Qt.AlignCenter, f["label"])
        
        # Scattered files on the left
        files = [
            {"icon": "📄", "target_folder": 0, "start_x": 30, "start_y": 15},
            {"icon": "🖼️", "target_folder": 1, "start_x": 80, "start_y": 50},
            {"icon": "📄", "target_folder": 0, "start_x": 50, "start_y": 80},
            {"icon": "🎵", "target_folder": 2, "start_x": 120, "start_y": 30},
            {"icon": "🖼️", "target_folder": 1, "start_x": 140, "start_y": 75},
            {"icon": "📄", "target_folder": 0, "start_x": 100, "start_y": 95},
        ]
        
        for i, f in enumerate(files):
            target_f = folders[f["target_folder"]]
            target_x = folder_x + 40
            target_y = target_f["y"] + 10
            
            # Animation progress for this file (staggered)
            file_phase = max(0, min(1, (phase - 1.5 - i * 0.3) / 0.8))
            # Ease out
            file_phase = 1 - (1 - file_phase) ** 3
            
            curr_x = f["start_x"] + (target_x - f["start_x"]) * file_phase
            curr_y = f["start_y"] + (target_y - f["start_y"]) * file_phase
            
            # File card
            p.setPen(Qt.NoPen)
            card_alpha = 255 if file_phase < 0.95 else int(255 * (1 - (file_phase - 0.95) / 0.05) * 0.5 + 128)
            card_color = QColor("#252535")
            card_color.setAlpha(card_alpha)
            p.setBrush(card_color)
            size = 28 - 8 * file_phase  # Shrink as it enters folder
            p.drawRoundedRect(QRectF(curr_x - size/2, curr_y - size/2, size, size), 4, 4)
            
            # Icon
            if file_phase < 0.9:
                font = QFont("Segoe UI Emoji", max(6, int(10 - 4 * file_phase)))
                p.setFont(font)
                p.setPen(self._dark)
                p.drawText(QRectF(curr_x - size/2, curr_y - size/2, size, size), Qt.AlignCenter, f["icon"])
        
        # Arrow in the middle
        arrow_alpha = int(150 + 80 * math.sin(t * 3))
        arrow_color = QColor(self._purple)
        arrow_color.setAlpha(arrow_alpha)
        p.setPen(QPen(arrow_color, 3, Qt.SolidLine, Qt.RoundCap))
        mid_x = w / 2 + 10
        mid_y = h / 2
        p.drawLine(QPointF(mid_x - 20, mid_y), QPointF(mid_x + 20, mid_y))
        # Arrow head
        p.drawLine(QPointF(mid_x + 20, mid_y), QPointF(mid_x + 12, mid_y - 8))
        p.drawLine(QPointF(mid_x + 20, mid_y), QPointF(mid_x + 12, mid_y + 8))

    def _draw_assistant(self, p: QPainter):
        t = self._frame / 60.0
        cx, cy = self.width() / 2, self.height() / 2 - 12
        for k in range(3):
            phase = (t * 0.7 + k / 3.0) % 1.0
            c = QColor(self._purple)
            c.setAlpha(int(120 * (1 - phase)))
            p.setPen(QPen(c, 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), 30 + phase * 24, 30 + phase * 24)
        p.setPen(Qt.NoPen)
        p.setBrush(self._purple)
        p.drawEllipse(QPointF(cx, cy), 30, 30)
        from app.ui.icons import line_pixmap
        p.drawPixmap(QPointF(cx - 13, cy - 13), line_pixmap("mic", 26, "#FFFFFF"))
        p.setPen(self._purple_light)
        p.setFont(QFont("Segoe UI", 10, QFont.Bold))
        p.drawText(QRectF(0, cy + 42, self.width(), 22), Qt.AlignCenter, "Say what you want")

    def _draw_agents(self, p: QPainter):
        from app.ui.agents_view import agent_logo as logo
        t = self._frame / 60.0
        w, row_h = 330, 42
        x = (self.width() - w) / 2
        for i, (agent, text) in enumerate((("claude", "Building your landing page"), ("codex", "Summing up this week's invoices"))):
            y = 18 + i * (row_h + 10)
            p.setPen(QPen(QColor("#2E2E40"), 1))
            p.setBrush(QColor("#252535"))
            p.drawRoundedRect(QRectF(x, y, w, row_h), 10, 10)
            pm = logo(agent, 20)
            if pm is not None:
                p.drawPixmap(QPointF(x + 12, y + 11), pm)
            p.setPen(QColor("#E8E8F0"))
            p.setFont(QFont("Segoe UI", 10))
            p.drawText(QRectF(x + 42, y, w - 80, row_h), Qt.AlignVCenter | Qt.AlignLeft, text)
            c = QColor(self._purple_light)
            c.setAlpha(int(140 + 115 * (0.5 + 0.5 * math.sin(t * 4 + i * 2))))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPointF(x + w - 20, y + row_h / 2), 4, 4)

    def _draw_voice(self, p: QPainter):
        t = self._frame / 60.0
        w, h = self.width(), self.height()
        mic_cx, cy = w * 0.33, h / 2 - 8

        # Pulsing sound-wave rings emanating from the mic (both sides).
        for i in range(3):
            phase = (t * 0.85 + i * 0.4) % 1.2
            r = 24 + phase * 40
            alpha = int(max(0, 150 * (1 - phase / 1.2)))
            col = QColor(self._purple); col.setAlpha(alpha)
            p.setPen(QPen(col, 2.5)); p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(mic_cx - r, cy - r, 2 * r, 2 * r), int(-50 * 16), int(100 * 16))
            p.drawArc(QRectF(mic_cx - r, cy - r, 2 * r, 2 * r), int(130 * 16), int(100 * 16))

        # Mic body (gradient capsule) + cradle + stand.
        grad = QLinearGradient(mic_cx, cy - 20, mic_cx, cy + 10)
        grad.setColorAt(0, QColor("#B28BFF")); grad.setColorAt(1, QColor("#6D28D9"))
        p.setPen(Qt.NoPen); p.setBrush(grad)
        p.drawRoundedRect(QRectF(mic_cx - 10, cy - 22, 20, 32), 10, 10)
        p.setPen(QPen(self._purple, 2.5)); p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(mic_cx - 16, cy - 14, 32, 32), int(200 * 16), int(140 * 16))
        p.setPen(QPen(self._purple, 2.5, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(mic_cx, cy + 18), QPointF(mic_cx, cy + 26))
        p.drawLine(QPointF(mic_cx - 7, cy + 26), QPointF(mic_cx + 7, cy + 26))

        # Reactive waveform bars to the right (the 'listening' motif).
        bars, bx0, gap = 9, w * 0.56, 11
        for i in range(bars):
            amp = 0.5 + 0.5 * math.sin(t * 5 + i * 0.7)
            bh = 8 + amp * 34
            x = bx0 + i * gap
            bg = QLinearGradient(x, cy - bh / 2, x, cy + bh / 2)
            bg.setColorAt(0, QColor("#B39DFF")); bg.setColorAt(1, QColor("#7C4DFF"))
            p.setPen(QPen(bg, 4, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(x, cy - bh / 2), QPointF(x, cy + bh / 2))

        # Caption: [Ctrl] + [Win] key chips, centered.
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        fm = p.fontMetrics()
        keys = ["Ctrl", "Win"]
        chip_ws = [fm.horizontalAdvance(k) + 16 for k in keys]
        plus_w = 14
        tail = "  to dictate"
        total = sum(chip_ws) + plus_w + fm.horizontalAdvance(tail) + 6
        x = (w - total) / 2
        cap_y = h - 26
        for i, k in enumerate(keys):
            cw = chip_ws[i]
            p.setPen(Qt.NoPen); p.setBrush(QColor(124, 77, 255, 36))
            p.drawRoundedRect(QRectF(x, cap_y, cw, 18), 5, 5)
            p.setPen(QPen(QColor(124, 77, 255, 120), 1)); p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(x + 0.5, cap_y + 0.5, cw - 1, 17), 5, 5)
            p.setPen(self._purple); p.drawText(QRectF(x, cap_y, cw, 18), Qt.AlignCenter, k)
            x += cw
            if i < len(keys) - 1:
                p.setPen(self._purple); p.drawText(QRectF(x, cap_y, plus_w, 18), Qt.AlignCenter, "+")
                x += plus_w
        p.setPen(self._gray)
        p.drawText(QRectF(x + 4, cap_y, fm.horizontalAdvance(tail) + 10, 18), Qt.AlignVCenter | Qt.AlignLeft, tail.strip())

    # ── Page tours: ("keys", [...]) · ("icon", name) · ("logos",) ──
    def _draw_keys(self, p: QPainter):
        """Key caps, lit one after another in the order you press them. "+" and "→" are plain text."""
        toks = self._art_arg
        font = QFont("Segoe UI", 15, QFont.DemiBold)      # (Windows: no SF Pro Display)
        p.setFont(font)
        fm = p.fontMetrics()
        widths = [fm.horizontalAdvance(t) + 14 if t in ("+", "→") else max(52, fm.horizontalAdvance(t) + 30) for t in toks]
        gap = 8
        x = (self.width() - sum(widths) - gap * (len(toks) - 1)) / 2
        caps = [i for i, t in enumerate(toks) if t not in ("+", "→")]
        lit = caps[int(self._frame / 40) % (len(caps) + 1)] if caps and int(self._frame / 40) % (len(caps) + 1) < len(caps) else None
        cy, h = self.height() / 2, 52
        for i, (t, w) in enumerate(zip(toks, widths)):
            if t in ("+", "→"):
                p.setPen(self._gray)
                p.drawText(QRectF(x, cy - h / 2, w, h), Qt.AlignCenter, t)
            else:
                down = 3 if i == lit else 0
                r = QRectF(x, cy - h / 2 + down, w, h - 3)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#0E0E16"))
                p.drawRoundedRect(r.adjusted(0, 3, 0, 3 - down), 10, 10)            # (the cap's side)
                p.setBrush(QColor("#2A2140") if i == lit else QColor("#252535"))
                p.drawRoundedRect(r, 10, 10)
                p.setPen(QPen(self._purple if i == lit else QColor("#3A3A4E"), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(r, 10, 10)
                p.setPen(QColor("#FFFFFF") if i == lit else QColor("#D8D8E6"))
                p.drawText(r, Qt.AlignCenter, t)
            x += w + gap

    def _draw_icon(self, p: QPainter):
        """One big line icon in a softly pulsing ring."""
        from app.ui.icons import line_pixmap
        t = self._frame / 60.0
        cx, cy = self.width() / 2, self.height() / 2
        for k in range(2):
            phase = (t * 0.6 + k / 2.0) % 1.0
            c = QColor(self._purple)
            c.setAlpha(int(90 * (1 - phase)))
            p.setPen(QPen(c, 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QPointF(cx, cy), 34 + phase * 22, 34 + phase * 22)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#252535"))
        p.drawEllipse(QPointF(cx, cy), 34, 34)
        pm = line_pixmap(self._art_arg, 34, "#B39DDB")
        p.drawPixmap(QPointF(cx - 17, cy - 17), pm)

    def _draw_logos(self, p: QPainter):
        """Claude Code and Codex, side by side."""
        from app.ui.agents_view import agent_logo as logo
        cx, cy = self.width() / 2, self.height() / 2
        for dx, agent in ((-50, "claude"), (50, "codex")):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#252535"))
            p.drawRoundedRect(QRectF(cx + dx - 36, cy - 36, 72, 72), 18, 18)
            pm = logo(agent, 40)
            if pm is not None:
                p.drawPixmap(QPointF(cx + dx - 20, cy - 20), pm)


class OnboardingOverlay(QDialog):
    """
    Interactive onboarding panel that floats over the app
    and guides users through key features
    """
    
    finished_onboarding = Signal()
    remind_later = Signal()  # Signal for "remind me later"
    
    def __init__(self, main_window, steps=None, tour=None):
        """tour: a page's own tour (page_tours.py): the same panel with `steps`, no sections, nothing saved."""
        super().__init__(main_window)
        self.main_window = main_window
        self._tour = tour
        self.current_step = 0
        self.drag_position = None
        self.is_minimized = False  # For "Try It" mode
        self._dragged = False

        lp = getattr(main_window, "live_page", None)
        self._was_listening = bool(getattr(getattr(lp, "listener", None), "on", False))

        # section: the chip it belongs to. nav_index: the page behind it (page numbers, not sidebar order).
        # highlight: attribute path on main_window to spotlight. not_ready: what changes if no agent is set up.
        self.steps = [
            {
                "art": "welcome",
                "title": "Control your entire computer by voice",
                "description": "• Find your files, run AI agents, and work across your apps\n• Just by speaking\n• Takes about a minute",
                "nav_index": None,
                "button_text": "Let's Go!",
                "highlight": None,
            },
            {
                "section": "agents",
                "art": "assistant",
                "title": "Talk, and it gets done",
                "description": "• Click Start listening and say what you want\n• Claude Code or Codex does the work\n• The card at the top of your screen shows what's happening",
                "nav_index": 6,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": "live_page.start_btn",
                "interactive": "assistant",
                "not_ready": {
                    "title": "Set up your agent",
                    "description": "• The agents run on your own Claude Code or Codex\n• Neither is set up on this computer yet\n• One click opens the setup, or do it later",
                    "button_text": "Later",
                    "show_try_it": False,
                    "highlight": None,
                },
            },
            {
                "section": "agents",
                "art": "agents",
                "title": "Every job gets its own chat",
                "description": "• See what each agent is doing\n• Run several at once\n• Follow up on any of them, by typing or by voice",
                "nav_index": 5,
                "button_text": "Next",
                "highlight": None,
            },
            {
                "section": "voice",
                "art": "voice",
                "title": "Hold Ctrl + Win and talk",
                "description": "Hold Ctrl + Win, say something, and let go. Your words appear wherever you're typing, in any app. Try it here:",
                "nav_index": 3,
                "button_text": "Next",
                "highlight": None,
                "interactive": "dictation",
            },
            {
                "section": "voice",
                "art": "voice",
                "title": "Teach Filect your words (optional)",
                "description": "Tell us your field and we'll suggest words to spell right.",
                "nav_index": None,
                "button_text": "Next",
                "interactive": "vocab",
                "highlight": None,
            },
            {
                "section": "files",
                "art": "index",
                "title": "Add a folder",
                "description": "• Filect reads what's inside your files\n• Add a folder to get started\n• Search needs this first",
                "nav_index": 2,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": "drop_zone",
            },
            {
                "section": "files",
                "art": "search",
                "title": "Find any file",
                "description": "• Describe it in your own words\n• Filect finds it, even by what's inside\n• By voice: hold Ctrl + Win + Shift and say it",
                "nav_index": 0,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": "search_input",
            },
            {
                "section": "files",
                "art": "organize",
                "title": "Sort a whole folder",
                "description": "• Pick a folder and Filect sorts it into subfolders\n• You see the plan before anything moves, and can undo it\n• By voice: hold Ctrl + Win + Alt and name the folder",
                "nav_index": 1,
                "sub_tab": 0,
                "button_text": "Next",
                "show_try_it": True,
                "highlight": "organize_page.dest_card",
            },
            {
                "art": "welcome",
                "title": "You're ready",
                "description": "• Hold Ctrl + Win to talk in any app\n• More shortcuts: \"See what it can do\" on the Assistant page\n• This guide is in Settings any time",
                "nav_index": None,
                "button_text": "Start",
                "highlight": None,
            },
        ]

        if tour:
            self.steps = steps

        # Spotlight overlay for Phase 3
        self.spotlight = None
        
        self._setup_ui()
        self._apply_styling()
        self._update_step()
    
    def _setup_ui(self):
        """Set up the UI components"""
        # Window settings
        # (Windows: a Tool window owned by the main window stays above it; "always on top" would float the guide
        # over every other app too, e.g. while you try dictating somewhere else.)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        w, h = (480, 480) if self._tour else (520, 620)    # (a page tour has no sections or try-it box: smaller)
        self.setFixedSize(w, h)
        
        # Main container with shadow
        self.container = QFrame(self)
        self.container.setObjectName("onboardingContainer")
        self.container.setGeometry(10, 10, w - 20, h - 20)
        
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
        
        self.step_label = QLabel(f"Step 1 of {len(self.steps)}")
        self.step_label.setObjectName("stepLabel")
        header.addWidget(self.step_label)
        
        header.addStretch()

        self.skip_btn = QPushButton("Skip this section")
        self.skip_btn.setObjectName("remindButton")
        self.skip_btn.setCursor(Qt.PointingHandCursor)
        self.skip_btn.clicked.connect(self._skip_section)
        header.addWidget(self.skip_btn)

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

        # The three sections: where you are, and a way to jump to any of them.
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self._chips = {}
        for key, label in (("agents", "Agents"), ("voice", "Your voice"), ("files", "Your files")):
            b = QPushButton(label)
            b.setObjectName("sectionChip")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._go_section(k))
            chips.addWidget(b, 1)
            self._chips[key] = b
        layout.addLayout(chips)
        if self._tour:
            for b in self._chips.values():
                b.hide()
            self.remind_btn.setText("✕ Close")

        # Center the title + animation + description as one balanced block (a matching
        # stretch sits after the description) so short steps aren't top-heavy.
        layout.addStretch(1)

        # Title
        self.title_label = QLabel("Welcome!")
        self.title_label.setObjectName("titleLabel")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        
        # Animation widget
        self.anim_widget = OnboardingAnimation(self.container)
        self.anim_widget.setFixedSize(w - 80, 130)
        layout.addWidget(self.anim_widget, 0, Qt.AlignCenter)
        
        # Description
        self.desc_label = QLabel("Description goes here")
        self.desc_label.setObjectName("descLabel")
        self.desc_label.setWordWrap(True)
        self.desc_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(self.desc_label)

        # Interactive vocabulary panel — shown ONLY on the custom-words step so the user
        # types their field and picks suggested terms right here, instead of a spotlight
        # behind the dialog that covers the card. Hidden on every other step.
        from app.ui.voice_cards.vocab_suggest import VocabSuggestPanel
        self.vocab_panel = VocabSuggestPanel(self.container)
        self.vocab_panel.setVisible(False)
        layout.addWidget(self.vocab_panel)

        # Dictation step: a real box, so holding Ctrl + Win types into it like any other app.
        self.try_box = QPlainTextEdit(self.container)
        self.try_box.setObjectName("tryBox")
        self.try_box.setPlaceholderText("Click here if you need to, then hold Ctrl + Win and talk")
        self.try_box.setFixedHeight(92)
        self.try_box.setVisible(False)
        layout.addWidget(self.try_box)
        self.try_ok = QLabel("✓ That's it. It works the same in any app.")
        self.try_ok.setObjectName("tryOk")
        self.try_ok.setVisible(False)
        layout.addWidget(self.try_ok)
        self.try_box.textChanged.connect(lambda: self.try_ok.setVisible(bool(self.try_box.toPlainText().strip())))

        # Assistant step when no agent is set up: the same one-click setup as the Agents page.
        self.setup_row = QWidget(self.container)
        row = QHBoxLayout(self.setup_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for agent, label in (("claude", "Set up Claude Code"), ("codex", "Set up Codex")):
            b = QPushButton(label)
            b.setObjectName("tryButton")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, a=agent: self.main_window.assistant_page.setup_agent(a))
            row.addWidget(b, 1)
        self.setup_row.setVisible(False)
        layout.addWidget(self.setup_row)
        ap = getattr(self.main_window, "assistant_page", None)
        if hasattr(ap, "agents_setup_changed"):
            ap.agents_setup_changed.connect(self._on_agents_changed)

        layout.addStretch(1)

        # Keyboard hint
        self.keyboard_hint = QLabel("💡 Use ← → arrow keys  •  Esc to " + ("close" if self._tour else "skip"))
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
        # Windows: a clicked button takes the keyboard focus, and a focused button spends ← → on moving the focus to
        # the next button - after clicking Next or Continue Tour the arrows did nothing (seen live). The panel's own
        # buttons never take the focus, so ← → Return Esc always reach the panel. (The text boxes keep theirs.)
        for b in [self.skip_btn, self.remind_btn, self.back_btn, self.try_btn, self.next_btn] + \
                self.setup_row.findChildren(QPushButton):
            b.setFocusPolicy(Qt.NoFocus)
        
        layout.addLayout(nav_layout)
        
        # Shown while you Try It. It lives on the main window, so the guide's stylesheet doesn't reach it: it has its own.
        self.continue_btn = QPushButton("▶ Continue Tour", self.main_window)
        self.continue_btn.setCursor(Qt.PointingHandCursor)
        self.continue_btn.clicked.connect(self._restore_from_try)
        self.continue_btn.setStyleSheet(
            "QPushButton { background-color: #7C4DFF; border: none; border-radius: 20px; color: white;"
            " font-size: 13px; font-weight: 600; padding: 0 22px; }"
            " QPushButton:hover { background-color: #9575FF; }")
        self.continue_btn.ensurePolished()
        self.continue_btn.setFixedSize(self.continue_btn.sizeHint().width(), 40)
        self.continue_btn.hide()
        # The window going full screen or changing size: the dim, the cutout and this button follow it.
        self._highlight = None
        self._follow_timer = QTimer(self, singleShot=True, interval=80, timeout=self._follow_window)
        self.main_window.installEventFilter(self)
        # However it closes (a button, Esc, or reject() from code: a second tour opening), the dim and the Continue
        # pill go with it - they live on the main window, so they'd stay and leave the app dark. (A Mac open item.)
        self.finished.connect(self._closed)

    def _closed(self, *_):
        self.is_minimized = False
        self.continue_btn.hide()
        if self.spotlight:
            self.spotlight.hide()
        self.anim_widget.stop()
    
    def _apply_styling(self):
        """Apply theme-aware styling"""
        from app.ui.theme_manager import get_theme_colors
        c = get_theme_colors()
        self.setStyleSheet(f"""
            QFrame#onboardingContainer {{
                background-color: {c['surface']};
                border-radius: 16px;
                border: 2px solid rgba(124, 77, 255, 0.3);
            }}
            
            QLabel#stepLabel {{
                color: #7C4DFF;
                font-size: 12px;
                font-weight: 600;
            }}
            
            QLabel#titleLabel {{
                color: {c['text']};
                font-size: 22px;
                font-weight: 700;
            }}
            
            QLabel#descLabel {{
                color: {c['text_secondary']};
                font-size: 15px;
                line-height: 1.6;
            }}
            
            QPushButton#sectionChip {{
                background-color: transparent;
                border: 1px solid {c['border_strong']};
                border-radius: 9px;
                color: {c['text_muted']};
                font-size: 12px;
                font-weight: 600;
                padding: 5px 8px;
            }}
            QPushButton#sectionChip:hover {{
                border-color: #7C4DFF;
                color: #7C4DFF;
            }}
            QPushButton#sectionChip:checked {{
                background-color: rgba(124, 77, 255, 0.14);
                border-color: #7C4DFF;
                color: #7C4DFF;
            }}

            QPlainTextEdit#tryBox {{
                background-color: {c['input_bg']};
                border: 1px solid {c['border_strong']};
                border-radius: 10px;
                color: {c['text']};
                font-size: 14px;
                padding: 8px;
            }}
            QPlainTextEdit#tryBox:focus {{
                border-color: #7C4DFF;
            }}

            QLabel#tryOk {{
                color: #4CAF50;
                font-size: 13px;
                font-weight: 600;
            }}

            QLabel#keyboardHint {{
                color: {c['text_muted']};
                font-size: 11px;
                padding: 4px;
            }}
            
            QProgressBar#progressBar {{
                background-color: {c['border']};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar#progressBar::chunk {{
                background-color: #7C4DFF;
                border-radius: 3px;
            }}
            
            QPushButton#remindButton {{
                background-color: transparent;
                border: none;
                color: {c['text_muted']};
                font-size: 12px;
                padding: 4px 8px;
            }}
            QPushButton#remindButton:hover {{
                color: #7C4DFF;
            }}
            
            QPushButton#backButton {{
                background-color: transparent;
                border: 2px solid {c['border_strong']};
                border-radius: 10px;
                color: {c['text_muted']};
                font-size: 14px;
                font-weight: 600;
                padding: 10px 20px;
                min-width: 90px;
            }}
            QPushButton#backButton:hover {{
                border-color: #7C4DFF;
                color: #7C4DFF;
            }}
            QPushButton#backButton:disabled {{
                border-color: {c['border']};
                color: {c['text_disabled']};
            }}
            
            QPushButton#tryButton {{
                background-color: transparent;
                border: 2px solid #7C4DFF;
                border-radius: 10px;
                color: #7C4DFF;
                font-size: 14px;
                font-weight: 600;
                padding: 10px 16px;
                min-width: 90px;
            }}
            QPushButton#tryButton:hover {{
                background-color: rgba(124, 77, 255, 0.1);
            }}
            
            QPushButton#nextButton {{
                background-color: #7C4DFF;
                border: none;
                border-radius: 10px;
                color: white;
                font-size: 14px;
                font-weight: 600;
                padding: 10px 24px;
                min-width: 120px;
            }}
            QPushButton#nextButton:hover {{
                background-color: #9575FF;
            }}
        """)
    
    # Stable slugs so analytics aren't broken by future title rewordings.
    _STEP_SLUGS = [
        "welcome", "assistant", "agents", "dictation", "custom_words", "add_folder", "search", "organize", "done",
    ]

    def _update_step(self):
        """Update the UI for the current step"""
        step = self.steps[self.current_step]
        kind = step.get("interactive")
        needs_agent = "not_ready" in step and not self._agent_ready()
        if needs_agent:
            step = {**step, **step["not_ready"]}
        if kind != "assistant" and not self._tour:    # (a page tour never turns off what you started)
            self._stop_listening_if_tour_started_it()

        # Funnel signal — fires every time a step is actually rendered (including
        # re-renders from going back). max(step_index) per user = where they got
        # to. Total step events show navigation patterns.
        try:                                       # (page tours aren't part of the first-run funnel)
            from app.core.supabase_client import track
            slug = self._STEP_SLUGS[self.current_step] if self.current_step < len(self._STEP_SLUGS) else "unknown"
            if not self._tour:
                track("onboarding_step_viewed", step=slug, step_index=self.current_step,
                      total_steps=len(self.steps))
        except Exception:
            pass

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

        section = step.get("section")
        self.skip_btn.setVisible(section is not None)
        for key, chip in self._chips.items():
            chip.setChecked(key == section)
        
        # Update Try It button visibility
        self.try_btn.setVisible(step.get("show_try_it", False))
        
        # Update animation
        self.anim_widget.set_art(step["art"])

        # Interactive vocab step: swap the animation + description for the live suggest panel.
        is_vocab = kind == "vocab"
        is_box = kind == "dictation"
        self.vocab_panel.setVisible(is_vocab)
        self.anim_widget.setVisible(not is_vocab and not is_box)
        self.desc_label.setVisible(not is_vocab)
        self.keyboard_hint.setVisible(not is_vocab and not is_box)
        self.try_box.setVisible(is_box)
        self.try_ok.setVisible(is_box and bool(self.try_box.toPlainText().strip()))
        self.setup_row.setVisible(needs_agent)
        if is_box:                                    # (it has to be the focused box for Ctrl + Win to type into it)
            self.activateWindow()
            self.try_box.setFocus()
        elif not is_vocab:
            # Off the typing screens the panel has the keyboard again: a hidden box keeps the focus otherwise and
            # eats ← → Return (seen live on Windows: after the custom-words screen, Return did nothing).
            self.setFocus(Qt.OtherFocusReason)

        # Navigate to the appropriate page in the app
        nav_index = step["nav_index"]
        if nav_index is not None:
            if hasattr(self.main_window, 'page_stack'):
                self.main_window.page_stack.setCurrentIndex(nav_index)
            # Check the sidebar button OF that page (the sidebar order isn't the page order: the button group
            # has each button under its page number).
            group = getattr(self.main_window, 'nav_button_group', None)
            b = group.button(nav_index) if group is not None else None
            if b is not None:
                b.setChecked(True)
        
        # Handle sub-tab switching for Organize page
        sub_tab = step.get("sub_tab")
        if sub_tab is not None and hasattr(self.main_window, 'organize_page'):
            self.main_window.organize_page._switch_tab(sub_tab)
        
        # The app is always dimmed behind the guide; a screen that points at something gets a bright cutout.
        self._highlight = step.get("highlight")
        self._update_spotlight(self._highlight)

    def _show_full_dim(self):
        """Dim the entire app behind the dialog (no bright cutout) so the interactive
        onboarding step gets the user's full attention."""
        if not self.spotlight:
            self.spotlight = SpotlightOverlay(self.main_window)
        self.spotlight.setGeometry(self.main_window.rect())
        self.spotlight.set_spotlight(None)   # None -> fills the whole window, no cutout
        self.spotlight.show()
        self.spotlight.raise_()
        self.raise_()

    def _resolve(self, path):
        """"organize_page.dest_card" → that widget; a number picks from a list ("live_page.tab_btns.1")."""
        obj = self.main_window
        for part in path.split("."):
            try:
                obj = obj[int(part)] if part.isdigit() else getattr(obj, part)
            except (AttributeError, IndexError, TypeError):
                return None
        return obj

    def _update_spotlight(self, highlight):
        """Dim the app; a bright cutout around what this screen points at: one path, or several (their union;
        whichever are showing). Nothing showing (e.g. Mute while the Assistant is off) = the whole app dimmed."""
        from PySide6.QtWidgets import QScrollArea, QApplication, QWidget
        paths = list(highlight) if isinstance(highlight, (list, tuple)) else [highlight] if highlight else []
        targets = [w for w in map(self._resolve, paths) if isinstance(w, QWidget) and w.isVisible() and w.width() > 0]
        if not targets:
            self._show_full_dim()
            self._place(None)
            return
        if not self.spotlight:
            self.spotlight = SpotlightOverlay(self.main_window)

        # Inside a scroll area (a card below the fold): scroll it into view first.
        anc = targets[0].parentWidget()
        while anc is not None:
            if isinstance(anc, QScrollArea):
                bar = anc.verticalScrollBar()            # (near the top: room under it for the panel)
                bar.setValue(targets[0].mapTo(anc.widget(), QPoint(0, 0)).y() - 24)
                anc.ensureWidgetVisible(targets[0], 40, 40)
                QApplication.processEvents()
                break
            anc = anc.parentWidget()

        self.spotlight.setGeometry(self.main_window.rect())
        local, screen = QRect(), QRect()
        for w in targets:
            local = local.united(QRect(w.mapTo(self.main_window, QPoint(0, 0)), w.size()))
            screen = screen.united(QRect(w.mapToGlobal(QPoint(0, 0)), w.size()))
        self.spotlight.set_spotlight(local)
        self.spotlight.show()
        self.spotlight.raise_()
        self._place(screen)
        self.raise_()                              # (the panel above the dim)

    def _place(self, target):
        """The corner of the window that hides least of `target` (bottom-right when it's a tie or there's none).
        Left alone once you've dragged it."""
        self._target = target                      # (showing the panel places it for this, not for "nothing")
        if self._dragged or not self.main_window:
            return
        geo = self.main_window.geometry()
        xs = (geo.right() - self.width() - 30, geo.left() + 20)
        ys = (geo.bottom() - self.height() - 30, geo.top() + 20)
        corners = [QRect(x, y, self.width(), self.height()) for y in ys for x in xs]

        def hidden(r):
            i = r.intersected(target) if target is not None else QRect()
            return i.width() * i.height()
        self.move(min(corners, key=hidden).topLeft())    # (min keeps the first of equals: bottom-right)

    def _agent_ready(self) -> bool:
        """Claude Code or Codex is set up. Before the first check comes back, what the last one found."""
        from app.core.settings import settings
        from app.ui.agents_view import needs_setup
        state = getattr(getattr(self.main_window, "assistant_page", None), "_agents_state", None)
        return not needs_setup(state) if state else settings.agent_was_ready

    def _on_agents_changed(self, _state):
        if self.isVisible() and "not_ready" in self.steps[self.current_step]:
            self._update_step()

    def _stop_listening_if_tour_started_it(self):
        """Tried the Assistant and moved on: it mustn't act on whatever's said during the rest of the tour."""
        lp = getattr(self.main_window, "live_page", None)
        listener = getattr(lp, "listener", None)
        if listener is not None and listener.on and not self._was_listening:
            lp.stop()

    def _go_section(self, key):
        self.current_step = next(i for i, s in enumerate(self.steps) if s.get("section") == key)
        self._update_step()

    def _skip_section(self):
        section = self.steps[self.current_step].get("section")
        while self.steps[self.current_step].get("section") == section:
            self.current_step += 1
        self._update_step()

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
        
        self._place_continue()
        self.continue_btn.show()

    def _place_continue(self):
        """Bottom-right corner of the window, on top of everything in it."""
        r, b = self.main_window.rect(), self.continue_btn
        b.move(r.width() - b.width() - 20, r.height() - b.height() - 20)
        b.raise_()

    def eventFilter(self, obj, event):
        if obj is self.main_window and event.type() == QEvent.Resize and (self.isVisible() or self.is_minimized):
            self._follow_timer.start()            # (once the window's layout has settled)
        return super().eventFilter(obj, event)

    def _follow_window(self):
        if self.is_minimized:
            self._place_continue()
        elif self.isVisible():
            self._update_spotlight(self._highlight)

    def _restore_from_try(self):
        """Restore the onboarding panel after trying"""
        self.is_minimized = False
        self.continue_btn.hide()
        self.show()
        self.raise_()
        self.activateWindow()                         # (Windows: so ← → and Esc reach it again, as when it opened)
        self._update_spotlight(self._highlight)
    
    def _remind_later(self):
        """Remind the user later instead of skipping entirely. On a page tour: Close."""
        if self._tour:
            self._end_page_tour("page_tour_closed", step=self.current_step + 1)
            return
        try:
            from app.core.supabase_client import track
            slug = self._STEP_SLUGS[self.current_step] if self.current_step < len(self._STEP_SLUGS) else "unknown"
            track("onboarding_dismissed", step=slug, step_index=self.current_step,
                  total_steps=len(self.steps))
        except Exception:
            pass
        if self.spotlight:
            self.spotlight.hide()
        self.remind_later.emit()
        self.reject()

    def _end_page_tour(self, event, **props):
        try:
            from app.core.supabase_client import track
            track(event, page=self._tour, **props)
        except Exception:
            pass
        self.continue_btn.hide()
        if self.spotlight:
            self.spotlight.hide()
        self.accept()

    def _finish_tour(self):
        """Complete the onboarding with celebration"""
        if self._tour:
            self._end_page_tour("page_tour_completed")
            return
        try:
            from app.core.supabase_client import track
            track("onboarding_completed", total_steps=len(self.steps))
        except Exception:
            pass
        self.continue_btn.hide()
        if self.spotlight:
            self.spotlight.hide()
        self._show_confetti()  # Phase 4: Confetti celebration!
        opening = self.main_window.nav_button_group.button(6 if self._agent_ready() else 0)
        if opening is not None:                       # (where Filect itself would open)
            opening.click()
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
            self._dragged = True
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """Reset drag position"""
        self.drag_position = None
    
    def showEvent(self, event):
        """Position the panel when shown - for what the step points at. (It was "nothing": every tour's first step
        jumped to the bottom-right corner, over its own target - 54% of the search box hidden, seen live.)"""
        super().showEvent(event)
        self._place(getattr(self, "_target", None))
        QTimer.singleShot(0, self._take_keyboard)

    def _take_keyboard(self):
        """Windows shows a Tool window WITHOUT activating it (Qt: SW_SHOWNOACTIVATE), so a tour opened from its
        button left the keyboard in the main window: "Esc to close" and the arrows did nothing (seen live)."""
        if self.isVisible():
            self.raise_()
            self.activateWindow()
    
    def closeEvent(self, event):
        """Clean up when closing"""
        self.continue_btn.hide()
        if hasattr(self, 'anim_widget'):
            self.anim_widget.stop()
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
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        
        self.spotlight_rect = None
        self.opacity = 0.65
        
        if parent:
            self.setGeometry(parent.rect())
            self.raise_()
    
    def set_spotlight(self, rect):
        """Set the spotlight area (QRect in parent coordinates)"""
        if rect:
            self.spotlight_rect = QRect(
                rect.x() - 10, rect.y() - 10,
                rect.width() + 20, rect.height() + 20
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
