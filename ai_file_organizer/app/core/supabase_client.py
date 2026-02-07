"""
Supabase client for authentication and subscription management.
Uses individual packages (supabase-auth, postgrest) instead of full supabase package.
"""

import logging
import webbrowser
from datetime import datetime
from typing import Optional, Dict, Any

# Try to import the individual packages
try:
    from gotrue import SyncGoTrueClient
    from postgrest import SyncPostgrestClient
    SUPABASE_AVAILABLE = True
except ImportError:
    try:
        # Alternative import paths
        from supabase_auth import SyncGoTrueClient
        from postgrest import SyncPostgrestClient
        SUPABASE_AVAILABLE = True
    except ImportError:
        SUPABASE_AVAILABLE = False
        SyncGoTrueClient = None
        SyncPostgrestClient = None

logger = logging.getLogger(__name__)

# Supabase configuration
SUPABASE_URL = "https://gsvccxhdgcshiwgjvgfi.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdzdmNjeGhkZ2NzaGl3Z2p2Z2ZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjczOTY2NTIsImV4cCI6MjA4Mjk3MjY1Mn0.Sbb6YJjlQ_ig2LCcs9zz_Be1kU-iIHBx4Vu4nzCPyTM"

# Stripe configuration
STRIPE_PRICE_ID = "price_1SlJ7TBATYQXewwiW3WdOIRN"


class SupabaseAuth:
    """Handles Supabase authentication and subscription management."""
    
    def __init__(self):
        self._auth_client = None
        self._db_client = None
        self._user: Optional[Dict[str, Any]] = None
        self._session: Optional[Dict[str, Any]] = None
        self._subscription: Optional[Dict[str, Any]] = None
        self._access_token: Optional[str] = None
        
        if SUPABASE_AVAILABLE:
            try:
                # Initialize GoTrue client for authentication
                self._auth_client = SyncGoTrueClient(
                    url=f"{SUPABASE_URL}/auth/v1",
                    headers={"apikey": SUPABASE_ANON_KEY}
                )
                logger.info("Supabase auth client initialized")
            except Exception as e:
                logger.error(f"Failed to initialize Supabase auth client: {e}")
        else:
            logger.warning("Supabase packages not installed. Run: pip install postgrest supabase-auth httpx")
    
    def _get_db_client(self) -> Optional[SyncPostgrestClient]:
        """Get a PostgREST client with current auth token."""
        if not SUPABASE_AVAILABLE:
            return None
        
        headers = {"apikey": SUPABASE_ANON_KEY}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        
        return SyncPostgrestClient(
            base_url=f"{SUPABASE_URL}/rest/v1",
            headers=headers
        )
    
    @property
    def is_available(self) -> bool:
        """Check if Supabase client is available."""
        return self._auth_client is not None
    
    @property
    def is_authenticated(self) -> bool:
        """Check if user is currently authenticated."""
        return self._user is not None and self._session is not None
    
    @property
    def current_user(self) -> Optional[Dict[str, Any]]:
        """Get current authenticated user."""
        return self._user
    
    @property
    def user_email(self) -> Optional[str]:
        """Get current user's email."""
        if self._user:
            return self._user.get('email')
        return None
    
    def _extract_user_dict(self, user_obj) -> Dict[str, Any]:
        """Extract user data from response object."""
        if hasattr(user_obj, 'model_dump'):
            return user_obj.model_dump()
        elif hasattr(user_obj, '__dict__'):
            return user_obj.__dict__
        elif isinstance(user_obj, dict):
            return user_obj
        else:
            return {'id': str(user_obj)}
    
    def _extract_session_dict(self, session_obj) -> Dict[str, Any]:
        """Extract session data from response object."""
        if session_obj is None:
            return {}
        if hasattr(session_obj, 'model_dump'):
            return session_obj.model_dump()
        elif hasattr(session_obj, '__dict__'):
            return session_obj.__dict__
        elif isinstance(session_obj, dict):
            return session_obj
        else:
            return {}
    
    def sign_up(self, email: str, password: str) -> Dict[str, Any]:
        """
        Sign up a new user.
        
        Returns:
            dict with 'success' bool and 'error' or 'user' keys
        """
        if not self._auth_client:
            return {'success': False, 'error': 'Supabase not available'}
        
        try:
            response = self._auth_client.sign_up({
                'email': email,
                'password': password
            })
            
            if response.user:
                self._user = self._extract_user_dict(response.user)
                if response.session:
                    self._session = self._extract_session_dict(response.session)
                    self._access_token = self._session.get('access_token')
                else:
                    self._session = None
                logger.info(f"User signed up: {email}")
                return {'success': True, 'user': self._user, 'needs_confirmation': response.session is None}
            else:
                return {'success': False, 'error': 'Sign up failed'}
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Sign up error: {error_msg}")
            return {'success': False, 'error': error_msg}
    
    def sign_in(self, email: str, password: str) -> Dict[str, Any]:
        """
        Sign in an existing user.
        
        Returns:
            dict with 'success' bool and 'error' or 'user' keys
        """
        if not self._auth_client:
            return {'success': False, 'error': 'Supabase not available'}
        
        try:
            response = self._auth_client.sign_in_with_password({
                'email': email,
                'password': password
            })
            
            if response.user and response.session:
                self._user = self._extract_user_dict(response.user)
                self._session = self._extract_session_dict(response.session)
                self._access_token = self._session.get('access_token')
                logger.info(f"User signed in: {email}")
                return {'success': True, 'user': self._user}
            else:
                return {'success': False, 'error': 'Sign in failed'}
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Sign in error: {error_msg}")
            return {'success': False, 'error': error_msg}
    
    def sign_out(self) -> Dict[str, Any]:
        """Sign out current user."""
        if not self._auth_client:
            return {'success': False, 'error': 'Supabase not available'}
        
        try:
            self._auth_client.sign_out()
            self._user = None
            self._session = None
            self._subscription = None
            self._access_token = None
            logger.info("User signed out")
            return {'success': True}
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Sign out error: {error_msg}")
            return {'success': False, 'error': error_msg}
    
    def restore_session(self, access_token: str, refresh_token: str) -> Dict[str, Any]:
        """
        Restore a session from stored tokens.
        
        Returns:
            dict with 'success' bool
        """
        if not self._auth_client:
            return {'success': False, 'error': 'Supabase not available'}
        
        try:
            response = self._auth_client.set_session(access_token, refresh_token)
            
            if response.user and response.session:
                self._user = self._extract_user_dict(response.user)
                self._session = self._extract_session_dict(response.session)
                self._access_token = self._session.get('access_token')
                logger.info("Session restored")
                return {'success': True}
            else:
                return {'success': False, 'error': 'Session restoration failed'}
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Session restore error: {error_msg}")
            return {'success': False, 'error': error_msg}
    
    def get_session_tokens(self) -> Optional[Dict[str, str]]:
        """Get current session tokens for storage."""
        if self._session:
            return {
                'access_token': self._session.get('access_token', ''),
                'refresh_token': self._session.get('refresh_token', '')
            }
        return None
    
    def check_subscription(self) -> Dict[str, Any]:
        """
        Check if current user has an active subscription.
        
        Returns:
            dict with 'has_subscription' bool, 'status', and 'expires_at'
        """
        if not self._user:
            logger.warning("[SUB CHECK] Not authenticated - no user")
            return {'has_subscription': False, 'status': None, 'error': 'Not authenticated'}
        
        try:
            user_id = self._user.get('id')
            logger.info(f"[SUB CHECK] Checking subscription for user_id: {user_id}")
            
            if not user_id:
                logger.warning("[SUB CHECK] No user ID found")
                return {'has_subscription': False, 'status': None, 'error': 'No user ID'}
            
            # Get DB client with auth token
            db_client = self._get_db_client()
            if not db_client:
                logger.warning("[SUB CHECK] Database client not available")
                return {'has_subscription': False, 'status': None, 'error': 'Database not available'}
            
            # Query subscriptions table
            logger.info(f"[SUB CHECK] Querying subscriptions table for user_id: {user_id}")
            response = db_client.from_('subscriptions').select('*').eq('user_id', user_id).execute()
            
            logger.info(f"[SUB CHECK] Query response: {response.data}")
            
            if response.data and len(response.data) > 0:
                sub = response.data[0]
                self._subscription = sub
                logger.info(f"[SUB CHECK] Found subscription: {sub}")
                
                status = sub.get('status')
                is_active = status in ('active', 'trialing')
                logger.info(f"[SUB CHECK] Status: {status}, is_active (before date check): {is_active}")
                
                # Check if subscription has expired
                period_end = sub.get('current_period_end')
                logger.info(f"[SUB CHECK] current_period_end: {period_end}")
                
                if period_end and is_active:
                    try:
                        end_date = datetime.fromisoformat(period_end.replace('Z', '+00:00'))
                        now = datetime.now(end_date.tzinfo)
                        logger.info(f"[SUB CHECK] end_date: {end_date}, now: {now}")
                        if end_date < now:
                            logger.info("[SUB CHECK] Subscription has EXPIRED")
                            is_active = False
                        else:
                            logger.info("[SUB CHECK] Subscription is VALID")
                    except Exception as e:
                        logger.warning(f"[SUB CHECK] Date parsing error: {e}")
                
                logger.info(f"[SUB CHECK] Final result: has_subscription={is_active}")
                return {
                    'has_subscription': is_active,
                    'status': status,
                    'expires_at': period_end
                }
            else:
                logger.warning(f"[SUB CHECK] No subscription found for user_id: {user_id}")
                return {'has_subscription': False, 'status': None}
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[SUB CHECK] Exception: {error_msg}")
            return {'has_subscription': False, 'status': None, 'error': error_msg}
    
    def open_checkout(self) -> bool:
        """
        Open Stripe checkout in browser for subscription.
        
        Returns:
            True if browser was opened successfully
        """
        if not self._user:
            logger.warning("Cannot open checkout: user not authenticated")
            return False
        
        email = self._user.get('email', '')
        user_id = self._user.get('id', '')
        
        # Create checkout URL with user info
        # This will redirect to our Supabase Edge Function that creates a Stripe Checkout Session
        checkout_url = f"{SUPABASE_URL}/functions/v1/create-checkout?user_id={user_id}&email={email}&price_id={STRIPE_PRICE_ID}"
        
        try:
            webbrowser.open(checkout_url)
            logger.info(f"Opened checkout for user: {email}")
            return True
        except Exception as e:
            logger.error(f"Failed to open checkout: {e}")
            return False


# Global instance
supabase_auth = SupabaseAuth()
