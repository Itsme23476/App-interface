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
# Standard plan - $15/month with 10-day free trial, 1000 analyses
STRIPE_PRICE_ID = "price_1StmTIBATYQXewwisuaa8ms1"
STRIPE_PRICE_ID_STANDARD = "price_1StmTIBATYQXewwisuaa8ms1"

# Ultra plan - $49/month, 5000 analyses
STRIPE_PRICE_ID_ULTRA = "price_1SuJOxBATYQXewwiuqsqAcMJ"  # TODO: Replace with actual Stripe price ID

# Plan limits
PLAN_LIMITS = {
    'standard': {'image': 750, 'video': 250, 'total': 1000},
    'ultra': {'image': 3750, 'video': 1250, 'total': 5000},
}


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
            return {'has_subscription': False, 'status': None, 'error': 'Not authenticated'}
        
        try:
            user_id = self._user.get('id')
            if not user_id:
                return {'has_subscription': False, 'status': None, 'error': 'No user ID'}
            
            # Get DB client with auth token
            db_client = self._get_db_client()
            if not db_client:
                return {'has_subscription': False, 'status': None, 'error': 'Database not available'}
            
            # Query subscriptions table - get most recent with valid status
            response = db_client.from_('subscriptions') \
                .select('*') \
                .eq('user_id', user_id) \
                .in_('status', ['active', 'trialing', 'past_due']) \
                .order('created_at', desc=True) \
                .limit(1) \
                .execute()
            
            logger.info(f"Subscription query for user {user_id}: {len(response.data) if response.data else 0} results")
            
            if response.data and len(response.data) > 0:
                sub = response.data[0]
                logger.info(f"Found subscription with status: {sub.get('status')}")
                self._subscription = sub
                
                status = sub.get('status')
                is_active = status in ('active', 'trialing')
                
                # Check if subscription has expired
                period_end = sub.get('current_period_end')
                if period_end and is_active:
                    try:
                        end_date = datetime.fromisoformat(period_end.replace('Z', '+00:00'))
                        if end_date < datetime.now(end_date.tzinfo):
                            is_active = False
                    except Exception:
                        pass
                
                return {
                    'has_subscription': is_active,
                    'status': status,
                    'expires_at': period_end
                }
            else:
                return {'has_subscription': False, 'status': None}
                
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Subscription check error: {error_msg}")
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
    
    def open_customer_portal(self) -> tuple[bool, str]:
        """
        Open Stripe Customer Portal in browser for subscription management.
        
        Returns:
            Tuple of (success: bool, message: str)
        """
        if not self._user:
            logger.warning("Cannot open portal: user not authenticated")
            return False, "Please sign in first"
        
        email = self._user.get('email', '')
        user_id = self._user.get('id', '')
        
        if not email or not user_id:
            logger.warning("Cannot open portal: missing user info")
            return False, "Please sign in again"
        
        # Open portal URL directly in browser (same pattern as checkout)
        # Edge function will redirect to Stripe Customer Portal
        portal_url = f"{SUPABASE_URL}/functions/v1/create-portal-session?user_id={user_id}&email={email}"
        
        try:
            webbrowser.open(portal_url)
            logger.info(f"Opened customer portal for user: {email}")
            return True, "Opening subscription management..."
        except Exception as e:
            logger.error(f"Failed to open customer portal: {e}")
            return False, f"Error: {str(e)}"
    
    def get_current_plan(self) -> str:
        """
        Get the user's current subscription plan type.
        
        Returns:
            'ultra' if on Ultra plan, 'standard' otherwise
        """
        if not self._subscription:
            # Try to get subscription
            self.check_subscription()
        
        if self._subscription:
            price_id = self._subscription.get('price_id', '')
            if price_id == STRIPE_PRICE_ID_ULTRA:
                return 'ultra'
        
        return 'standard'
    
    def get_plan_limits(self) -> Dict[str, int]:
        """Get the limits for the current plan."""
        plan = self.get_current_plan()
        return PLAN_LIMITS.get(plan, PLAN_LIMITS['standard'])
    
    def get_monthly_usage(self) -> Dict[str, Any]:
        """
        Get the user's AI usage for the current month.
        
        Returns:
            Dict with 'image_count', 'video_count', 'total_count', 'remaining', 'plan', 'total_limit'
        """
        # Get limits based on current plan
        limits = self.get_plan_limits()
        IMAGE_LIMIT = limits['image']
        VIDEO_LIMIT = limits['video']
        TOTAL_DISPLAY_LIMIT = limits['total']
        current_plan = self.get_current_plan()
        
        default = {
            'image_count': 0,
            'video_count': 0, 
            'total_count': 0,
            'remaining': TOTAL_DISPLAY_LIMIT,
            'total_limit': TOTAL_DISPLAY_LIMIT,
            'plan': current_plan,
            'at_limit': False
        }
        
        if not self._user:
            logger.warning(f"[USAGE] Cannot get usage: no user logged in")
            return default
        
        db_client = self._get_db_client()
        if not db_client:
            logger.warning(f"[USAGE] Cannot get usage: db_client not available")
            return default
        
        try:
            user_id = self._user.get('id')
            if not user_id:
                logger.warning("[USAGE] No user_id found")
                return default
            
            logger.info(f"[USAGE] Fetching usage for user {user_id}")
            
            # Get current month's start (use UTC to match database)
            from datetime import timezone
            now = datetime.now(timezone.utc)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            
            # Query api_usage for this month
            response = db_client.from_('api_usage') \
                .select('endpoint') \
                .eq('user_id', user_id) \
                .gte('created_at', month_start.isoformat()) \
                .execute()
            
            logger.info(f"[USAGE] Query returned {len(response.data) if response.data else 0} records")
            
            if not response.data:
                logger.info("[USAGE] No usage records found, returning default")
                return default
            
            # Count by type
            image_count = 0
            video_count = 0
            
            for record in response.data:
                endpoint = record.get('endpoint', '')
                if endpoint in ('vision', 'chat'):
                    image_count += 1
                elif endpoint == 'whisper':
                    video_count += 1
            
            # Check limits
            image_at_limit = image_count >= IMAGE_LIMIT
            video_at_limit = video_count >= VIDEO_LIMIT
            
            # Calculate remaining (simplified for user display)
            total_used = image_count + video_count
            remaining = max(0, TOTAL_DISPLAY_LIMIT - total_used)
            
            result = {
                'image_count': image_count,
                'video_count': video_count,
                'total_count': total_used,
                'remaining': remaining,
                'total_limit': TOTAL_DISPLAY_LIMIT,
                'plan': current_plan,
                'at_limit': image_at_limit and video_at_limit,
                'image_at_limit': image_at_limit,
                'video_at_limit': video_at_limit,
            }
            logger.info(f"[USAGE] Returning: {result}")
            return result
            
        except Exception as e:
            logger.error(f"[USAGE] Error getting usage: {e}")
            return default
    
    def can_use_ai(self, analysis_type: str = 'image') -> tuple[bool, str]:
        """
        Check if user can perform AI analysis (under limit).
        
        Args:
            analysis_type: 'image' or 'video'
            
        Returns:
            (can_use, message)
        """
        usage = self.get_monthly_usage()
        limits = self.get_plan_limits()
        plan = usage.get('plan', 'standard')
        total_limit = usage.get('total_limit', 1000)
        
        # For Ultra plan, only show upgrade message if they're not already on it
        upgrade_msg = " Upgrade to Ultra for 5,000 analyses!" if plan == 'standard' else ""
        
        if analysis_type == 'video' and usage.get('video_at_limit'):
            self._limit_reached = True
            self._limit_message = f"Monthly video analysis limit reached ({usage['video_count']}/{limits['video']}).{upgrade_msg}"
            return False, self._limit_message
        
        if analysis_type == 'image' and usage.get('image_at_limit'):
            self._limit_reached = True
            self._limit_message = f"Monthly image analysis limit reached ({usage['image_count']}/{limits['image']}).{upgrade_msg}"
            return False, self._limit_message
        
        # Check total limit
        if usage.get('remaining', total_limit) <= 0:
            self._limit_reached = True
            self._limit_message = f"Monthly AI analysis limit reached ({total_limit}/{total_limit}).{upgrade_msg}"
            return False, self._limit_message
        
        return True, f"{usage['remaining']} AI analyses remaining"
    
    def check_and_clear_limit_flag(self) -> tuple[bool, str]:
        """Check if limit was reached and clear the flag. Returns (was_reached, message)."""
        if hasattr(self, '_limit_reached') and self._limit_reached:
            msg = getattr(self, '_limit_message', 'AI analysis limit reached')
            self._limit_reached = False
            self._limit_message = ''
            return True, msg
        return False, ''


# Global instance
supabase_auth = SupabaseAuth()
