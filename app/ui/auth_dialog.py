"""
Authentication dialog for login, signup, and subscription management.
Modern, clean design that adapts to dark/light theme.
"""

import logging
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QStackedWidget, QWidget, QMessageBox, QFrame,
    QGraphicsDropShadowEffect, QSpacerItem, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QFont, QColor

from app.core.supabase_client import supabase_auth
from app.core.settings import settings

logger = logging.getLogger(__name__)


class AuthDialog(QDialog):
    """Authentication dialog for user login/signup and subscription."""
    
    # Signals
    auth_successful = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File Search Assistant")
        self.setFixedSize(520, 820)  # Taller to fit trial info and features
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setModal(True)
        self.setObjectName("authDialog")
        
        self._setup_ui()
        self._setup_connections()
        
        # Subscription polling timer
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_subscription)
        self._poll_count = 0
        
        # Try to restore session on init
        QTimer.singleShot(100, self._try_restore_session)
    
    def _setup_ui(self):
        """Set up the dialog UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Main container with card styling
        self.container = QFrame()
        self.container.setObjectName("authContainer")
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(40, 36, 40, 36)  # Slightly smaller margins
        container_layout.setSpacing(0)
        
        # Header with logo
        header_layout = QVBoxLayout()
        header_layout.setSpacing(12)
        
        # Logo icon
        logo_label = QLabel("🔍")
        logo_label.setObjectName("authLogo")
        logo_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(logo_label)
        
        # Title
        self.title_label = QLabel("File Search Assistant")
        self.title_label.setObjectName("authTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self.title_label)
        
        # Subtitle
        self.subtitle_label = QLabel("Sign in to your account")
        self.subtitle_label.setObjectName("authSubtitle")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self.subtitle_label)
        
        container_layout.addLayout(header_layout)
        container_layout.addSpacing(32)
        
        # Stacked widget for different views
        self.stack = QStackedWidget()
        self.stack.setObjectName("authStack")
        container_layout.addWidget(self.stack)
        
        # Create pages
        self._create_login_page()
        self._create_signup_page()
        self._create_subscribe_page()
        
        layout.addWidget(self.container)
        
        # Start with login page
        self.stack.setCurrentIndex(0)
    
    def _create_input_field(self, label_text, placeholder, is_password=False):
        """Create a styled input field with label."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        
        label = QLabel(label_text)
        label.setObjectName("authLabel")
        layout.addWidget(label)
        
        input_field = QLineEdit()
        input_field.setPlaceholderText(placeholder)
        input_field.setObjectName("authInput")
        input_field.setMinimumHeight(52)
        if is_password:
            input_field.setEchoMode(QLineEdit.Password)
        
        layout.addWidget(input_field)
        
        return container, input_field
    
    def _create_login_page(self):
        """Create the login page."""
        page = QWidget()
        page.setObjectName("authPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        
        # Email field
        email_container, self.login_email = self._create_input_field(
            "Email", "Enter your email address"
        )
        layout.addWidget(email_container)
        
        # Password field
        password_container, self.login_password = self._create_input_field(
            "Password", "Enter your password", is_password=True
        )
        layout.addWidget(password_container)
        
        layout.addSpacing(8)
        
        # Login button
        self.login_button = QPushButton("Sign In")
        self.login_button.setObjectName("primaryButton")
        self.login_button.setMinimumHeight(52)
        self.login_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.login_button)
        
        # Error label
        self.login_error = QLabel("")
        self.login_error.setObjectName("errorLabel")
        self.login_error.setAlignment(Qt.AlignCenter)
        self.login_error.setWordWrap(True)
        self.login_error.setMinimumHeight(24)
        layout.addWidget(self.login_error)
        
        layout.addStretch()
        
        # Divider
        divider = QFrame()
        divider.setObjectName("authDivider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        
        layout.addSpacing(16)
        
        # Switch to signup
        switch_layout = QHBoxLayout()
        switch_layout.setSpacing(4)
        switch_label = QLabel("Don't have an account?")
        switch_label.setObjectName("authSwitchLabel")
        self.to_signup_button = QPushButton("Create account")
        self.to_signup_button.setObjectName("linkButton")
        self.to_signup_button.setCursor(Qt.PointingHandCursor)
        switch_layout.addStretch()
        switch_layout.addWidget(switch_label)
        switch_layout.addWidget(self.to_signup_button)
        switch_layout.addStretch()
        layout.addLayout(switch_layout)
        
        self.stack.addWidget(page)
    
    def _create_signup_page(self):
        """Create the signup page."""
        page = QWidget()
        page.setObjectName("authPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        
        # Email field
        email_container, self.signup_email = self._create_input_field(
            "Email", "Enter your email address"
        )
        layout.addWidget(email_container)
        
        # Password field
        password_container, self.signup_password = self._create_input_field(
            "Password", "Create a password (min 6 chars)", is_password=True
        )
        layout.addWidget(password_container)
        
        # Confirm Password field
        confirm_container, self.signup_confirm = self._create_input_field(
            "Confirm Password", "Confirm your password", is_password=True
        )
        layout.addWidget(confirm_container)
        
        layout.addSpacing(4)
        
        # Signup button
        self.signup_button = QPushButton("Create Account")
        self.signup_button.setObjectName("primaryButton")
        self.signup_button.setMinimumHeight(52)
        self.signup_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.signup_button)
        
        # Error label
        self.signup_error = QLabel("")
        self.signup_error.setObjectName("errorLabel")
        self.signup_error.setAlignment(Qt.AlignCenter)
        self.signup_error.setWordWrap(True)
        self.signup_error.setMinimumHeight(24)
        layout.addWidget(self.signup_error)
        
        layout.addStretch()
        
        # Divider
        divider = QFrame()
        divider.setObjectName("authDivider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        
        layout.addSpacing(16)
        
        # Switch to login
        switch_layout = QHBoxLayout()
        switch_layout.setSpacing(4)
        switch_label = QLabel("Already have an account?")
        switch_label.setObjectName("authSwitchLabel")
        self.to_login_button = QPushButton("Sign in")
        self.to_login_button.setObjectName("linkButton")
        self.to_login_button.setCursor(Qt.PointingHandCursor)
        switch_layout.addStretch()
        switch_layout.addWidget(switch_label)
        switch_layout.addWidget(self.to_login_button)
        switch_layout.addStretch()
        layout.addLayout(switch_layout)
        
        self.stack.addWidget(page)
    
    def _create_subscribe_page(self):
        """Create the subscription page."""
        page = QWidget()
        page.setObjectName("authPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        
        # Welcome message
        self.welcome_label = QLabel("Welcome!")
        self.welcome_label.setObjectName("authWelcome")
        self.welcome_label.setAlignment(Qt.AlignCenter)
        self.welcome_label.setWordWrap(True)  # Allow wrap for long names
        self.welcome_label.setMinimumWidth(300)  # Ensure readable width
        layout.addWidget(self.welcome_label)
        
        layout.addSpacing(8)
        
        # Subscription card
        sub_card = QFrame()
        sub_card.setObjectName("subscriptionCard")
        sub_card.setMinimumHeight(340)  # Ensure card is tall enough for all content
        card_layout = QVBoxLayout(sub_card)
        card_layout.setContentsMargins(20, 16, 20, 16)
        card_layout.setSpacing(8)  # Tighter spacing
        
        # Free trial badge
        trial_badge = QLabel("✨ 10-DAY FREE TRIAL ✨")
        trial_badge.setObjectName("trialBadge")
        trial_badge.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(trial_badge)
        
        # Plan badge
        plan_badge = QLabel("PRO PLAN")
        plan_badge.setObjectName("planBadge")
        plan_badge.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(plan_badge)
        
        # Price
        price_layout = QHBoxLayout()
        price_layout.setAlignment(Qt.AlignCenter)
        price_layout.setSpacing(4)
        
        price_amount = QLabel("$15")
        price_amount.setObjectName("priceAmount")
        price_period = QLabel("/ month")
        price_period.setObjectName("pricePeriod")
        
        price_layout.addWidget(price_amount)
        price_layout.addWidget(price_period)
        card_layout.addLayout(price_layout)
        
        # Trial info
        trial_info = QLabel("Start free, cancel anytime")
        trial_info.setObjectName("trialInfo")
        trial_info.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(trial_info)
        
        # Features
        features = [
            "🔍  AI-powered semantic search",
            "👁️  Vision AI for image analysis",
            "📝  OCR text extraction",
            "⚡  Auto-indexing of new files",
        ]
        
        features_container = QWidget()
        features_container.setObjectName("featuresContainer")
        features_layout = QVBoxLayout(features_container)
        features_layout.setContentsMargins(0, 4, 0, 0)
        features_layout.setSpacing(6)  # Tighter feature spacing
        
        for feature in features:
            feat_label = QLabel(feature)
            feat_label.setObjectName("featureLabel")
            features_layout.addWidget(feat_label)
        
        card_layout.addWidget(features_container)
        layout.addWidget(sub_card)
        
        layout.addSpacing(16)
        
        # Subscribe button
        self.subscribe_button = QPushButton("Start Free Trial")
        self.subscribe_button.setObjectName("primaryButton")
        self.subscribe_button.setMinimumHeight(52)
        self.subscribe_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.subscribe_button)
        
        # Cancel waiting button (hidden by default)
        self.cancel_wait_button = QPushButton("Cancel / Try Again")
        self.cancel_wait_button.setObjectName("linkButton")
        self.cancel_wait_button.setCursor(Qt.PointingHandCursor)
        self.cancel_wait_button.hide()  # Hidden until waiting starts
        layout.addWidget(self.cancel_wait_button, alignment=Qt.AlignCenter)
        
        # Status label
        self.sub_status = QLabel("")
        self.sub_status.setObjectName("statusLabel")
        self.sub_status.setAlignment(Qt.AlignCenter)
        self.sub_status.setWordWrap(True)
        self.sub_status.setMinimumHeight(24)
        layout.addWidget(self.sub_status)
        
        layout.addStretch()
        
        # Logout
        self.logout_button = QPushButton("Sign Out")
        self.logout_button.setObjectName("linkButton")
        self.logout_button.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.logout_button, alignment=Qt.AlignCenter)
        
        self.stack.addWidget(page)
    
    def _setup_connections(self):
        """Set up signal connections."""
        # Login page
        self.login_button.clicked.connect(self._do_login)
        self.login_password.returnPressed.connect(self._do_login)
        self.to_signup_button.clicked.connect(self._go_to_signup)
        
        # Signup page
        self.signup_button.clicked.connect(self._do_signup)
        self.signup_confirm.returnPressed.connect(self._do_signup)
        self.to_login_button.clicked.connect(self._go_to_login)
        
        # Subscribe page
        self.subscribe_button.clicked.connect(self._open_checkout)
        self.cancel_wait_button.clicked.connect(self._cancel_waiting)
        self.logout_button.clicked.connect(self._do_logout)
    
    def _go_to_signup(self):
        """Navigate to signup page."""
        self.title_label.setText("Create Account")
        self.subtitle_label.setText("Sign up to get started")
        self.stack.setCurrentIndex(1)
    
    def _go_to_login(self):
        """Navigate to login page."""
        self.title_label.setText("File Search Assistant")
        self.subtitle_label.setText("Sign in to your account")
        self.stack.setCurrentIndex(0)
    
    def _try_restore_session(self):
        """Try to restore a previous session."""
        if settings.has_stored_session():
            result = supabase_auth.restore_session(
                settings.auth_access_token,
                settings.auth_refresh_token
            )
            
            if result.get('success'):
                logger.info("Session restored successfully")
                # Update settings with fresh tokens from restored session
                tokens = supabase_auth.get_session_tokens()
                if tokens:
                    settings.set_auth_tokens(
                        tokens['access_token'],
                        tokens['refresh_token'],
                        settings.auth_user_email
                    )
                    logger.info("Tokens refreshed after session restore")
                self._check_subscription_silent()
            else:
                settings.clear_auth_tokens()
    
    def _do_login(self):
        """Handle login button click."""
        email = self.login_email.text().strip()
        password = self.login_password.text()
        
        if not email or not password:
            self.login_error.setText("Please enter email and password")
            return
        
        self.login_button.setEnabled(False)
        self.login_button.setText("Signing in...")
        
        result = supabase_auth.sign_in(email, password)
        
        self.login_button.setEnabled(True)
        self.login_button.setText("Sign In")
        
        if result.get('success'):
            self.login_error.setText("")
            tokens = supabase_auth.get_session_tokens()
            if tokens:
                settings.set_auth_tokens(
                    tokens['access_token'],
                    tokens['refresh_token'],
                    email
                )
            self._check_subscription_silent()
        else:
            error = result.get('error', 'Login failed')
            self.login_error.setText(error)
    
    def _do_signup(self):
        """Handle signup button click."""
        email = self.signup_email.text().strip()
        password = self.signup_password.text()
        confirm = self.signup_confirm.text()
        
        if not email or not password:
            self.signup_error.setText("Please fill all fields")
            return
        
        if password != confirm:
            self.signup_error.setText("Passwords don't match")
            return
        
        if len(password) < 6:
            self.signup_error.setText("Password must be at least 6 characters")
            return
        
        self.signup_button.setEnabled(False)
        self.signup_button.setText("Creating account...")
        
        result = supabase_auth.sign_up(email, password)
        
        self.signup_button.setEnabled(True)
        self.signup_button.setText("Create Account")
        
        if result.get('success'):
            self.signup_error.setText("")
            
            if result.get('needs_confirmation'):
                QMessageBox.information(
                    self,
                    "Check Your Email",
                    "We've sent a confirmation email. Please check your inbox and click the link to verify your account, then sign in."
                )
                self._go_to_login()
                self.login_email.setText(email)
            else:
                tokens = supabase_auth.get_session_tokens()
                if tokens:
                    settings.set_auth_tokens(
                        tokens['access_token'],
                        tokens['refresh_token'],
                        email
                    )
                self._show_subscribe_page()
        else:
            error = result.get('error', 'Signup failed')
            self.signup_error.setText(error)
    
    def _show_subscribe_page(self):
        """Show the subscription page."""
        email = supabase_auth.user_email or settings.auth_user_email
        short_email = email.split('@')[0] if email else "there"
        self.welcome_label.setText(f"Hey {short_email}! 👋")
        self.title_label.setText("Unlock Pro Features")
        self.subtitle_label.setText("Subscribe to access all features")
        self.stack.setCurrentIndex(2)
    
    def _open_checkout(self):
        """Open Stripe checkout in browser and start polling."""
        self.sub_status.setText("Opening checkout...")
        self.sub_status.setObjectName("statusLabel")
        self.sub_status.setStyleSheet("")  # Reset any error styling
        
        success = supabase_auth.open_checkout()
        
        if success:
            self.sub_status.setText("Complete checkout in your browser...")
            self.subscribe_button.setEnabled(False)
            self.subscribe_button.setText("Waiting...")
            self.cancel_wait_button.show()  # Show cancel option
            # Start polling
            self._poll_count = 0
            self._poll_timer.start(3000)
        else:
            self.sub_status.setText("Failed to open checkout. Try again.")
            self.sub_status.setObjectName("errorLabel")
    
    def _cancel_waiting(self):
        """Cancel the payment waiting and reset UI."""
        self._poll_timer.stop()
        self.subscribe_button.setEnabled(True)
        self.subscribe_button.setText("Start Free Trial")
        self.cancel_wait_button.hide()
        self.sub_status.setText("Checkout cancelled. Click to try again.")
        self.sub_status.setObjectName("statusLabel")
    
    def _poll_subscription(self):
        """Poll for subscription status after checkout."""
        self._poll_count += 1
        
        # Stop after 100 attempts (5 minutes)
        if self._poll_count > 100:
            self._poll_timer.stop()
            self.subscribe_button.setEnabled(True)
            self.subscribe_button.setText("Start Free Trial")
            self.cancel_wait_button.hide()
            self.sub_status.setText("Timed out. Click to try again.")
            return
        
        # Update status
        remaining = (100 - self._poll_count) * 3
        minutes = remaining // 60
        seconds = remaining % 60
        self.sub_status.setText(f"Checking payment status... ({minutes}:{seconds:02d})")
        
        result = supabase_auth.check_subscription()
        
        if result.get('has_subscription'):
            self._poll_timer.stop()
            self.cancel_wait_button.hide()
            self.sub_status.setText("Payment confirmed! 🎉")
            logger.info("Subscription verified!")
            QTimer.singleShot(500, lambda: (self.auth_successful.emit(), self.accept()))
    
    def _check_subscription_silent(self):
        """Check subscription without UI updates."""
        result = supabase_auth.check_subscription()
        
        if result.get('has_subscription'):
            logger.info("Active subscription found")
            self.auth_successful.emit()
            self.accept()
        else:
            self._show_subscribe_page()
    
    def _do_logout(self):
        """Handle logout."""
        supabase_auth.sign_out()
        settings.clear_auth_tokens()
        
        self.login_email.clear()
        self.login_password.clear()
        self.signup_email.clear()
        self.signup_password.clear()
        self.signup_confirm.clear()
        
        self._go_to_login()
    
    def closeEvent(self, event):
        """Handle dialog close."""
        if self._poll_timer.isActive():
            self._poll_timer.stop()
        event.accept()
