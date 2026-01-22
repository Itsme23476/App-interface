"""
API Client for OpenAI calls via Supabase Edge Function.

This module routes all AI API calls through Supabase, which:
1. Verifies user authentication
2. Checks subscription status
3. Tracks usage per user
4. Makes the actual OpenAI API call

For local development or if user provides their own API key,
it can fall back to direct OpenAI calls.
"""

import os
import json
import base64
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
import requests

logger = logging.getLogger(__name__)

# Supabase configuration
SUPABASE_URL = "https://gsvccxhdgcshiwgjvgfi.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdzdmNjeGhkZ2NzaGl3Z2p2Z2ZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjczOTY2NTIsImV4cCI6MjA4Mjk3MjY1Mn0.Sbb6YJjlQ_ig2LCcs9zz_Be1kU-iIHBx4Vu4nzCPyTM"
EDGE_FUNCTION_URL = f"{SUPABASE_URL}/functions/v1/openai-proxy"


class APIClient:
    """
    Client for making AI API calls.
    
    Supports two modes:
    1. Supabase Proxy (for subscribed users) - routes through Edge Function
    2. Direct OpenAI (for users with own API key) - direct API calls
    """
    
    def __init__(self):
        self._access_token: Optional[str] = None
        self._use_proxy = True  # Default to proxy mode
        self._direct_api_key: Optional[str] = None
    
    def set_access_token(self, token: str):
        """Set the Supabase access token for authenticated requests."""
        self._access_token = token
        self._use_proxy = True
        logger.info("API client configured for Supabase proxy mode")
    
    def set_direct_api_key(self, api_key: str):
        """Set a direct OpenAI API key (bypasses Supabase)."""
        self._direct_api_key = api_key
        self._use_proxy = False
        logger.info("API client configured for direct OpenAI mode")
    
    def clear_auth(self):
        """Clear all authentication."""
        self._access_token = None
        self._direct_api_key = None
        self._use_proxy = True
    
    @property
    def is_authenticated(self) -> bool:
        """Check if client has valid authentication."""
        return bool(self._access_token or self._direct_api_key)
    
    def _make_proxy_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Make request through Supabase Edge Function."""
        if not self._access_token:
            raise ValueError("No access token set. User must be logged in.")
        
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "apikey": SUPABASE_ANON_KEY,
        }
        
        response = requests.post(
            EDGE_FUNCTION_URL,
            headers=headers,
            json=payload,
            timeout=120
        )
        
        if response.status_code == 401:
            raise PermissionError("Authentication failed. Please log in again.")
        elif response.status_code == 403:
            raise PermissionError("No active subscription. Please subscribe to use this feature.")
        elif not response.ok:
            error_data = response.json() if response.text else {"error": "Unknown error"}
            raise RuntimeError(f"API error: {error_data.get('error', response.text)}")
        
        return response.json()
    
    def _make_direct_request(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Make direct request to OpenAI API."""
        from openai import OpenAI
        
        if not self._direct_api_key:
            raise ValueError("No API key set.")
        
        client = OpenAI(api_key=self._direct_api_key)
        
        if endpoint in ("chat", "vision"):
            response = client.chat.completions.create(
                model=payload.get("model", "gpt-4o-mini"),
                messages=payload.get("messages", []),
                max_tokens=payload.get("max_tokens", 500),
                temperature=payload.get("temperature", 0.2),
            )
            return {
                "choices": [{"message": {"content": response.choices[0].message.content}}],
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                }
            }
        elif endpoint == "whisper":
            # For whisper, payload should have audio_path
            audio_path = payload.get("audio_path")
            if not audio_path:
                raise ValueError("audio_path required for whisper")
            with open(audio_path, "rb") as f:
                response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text"
                )
            return {"text": response}
        elif endpoint == "embeddings":
            response = client.embeddings.create(
                model=payload.get("model", "text-embedding-3-small"),
                input=payload.get("input", ""),
            )
            return {
                "data": [{"embedding": e.embedding} for e in response.data],
                "usage": {"total_tokens": response.usage.total_tokens}
            }
        else:
            raise ValueError(f"Unknown endpoint: {endpoint}")
    
    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str = "gpt-4o-mini",
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        """
        Make a chat completion request.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Model to use
            max_tokens: Maximum tokens in response
            temperature: Sampling temperature
            
        Returns:
            Response dict with 'choices' containing the completion
        """
        payload = {
            "endpoint": "chat",
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        
        if self._use_proxy:
            return self._make_proxy_request(payload)
        else:
            return self._make_direct_request("chat", payload)
    
    def vision_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str = "gpt-4o-mini",
        max_tokens: int = 500,
    ) -> Dict[str, Any]:
        """
        Make a vision (image analysis) request.
        
        Args:
            messages: List of message dicts, can include image_url content
            model: Model to use (must support vision)
            max_tokens: Maximum tokens in response
            
        Returns:
            Response dict with 'choices' containing the analysis
        """
        payload = {
            "endpoint": "vision",
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        
        if self._use_proxy:
            return self._make_proxy_request(payload)
        else:
            return self._make_direct_request("vision", payload)
    
    def transcribe_audio(
        self,
        audio_path: Optional[str] = None,
        audio_bytes: Optional[bytes] = None,
        filename: str = "audio.mp3",
    ) -> str:
        """
        Transcribe audio using Whisper.
        
        Args:
            audio_path: Path to audio file (for direct mode)
            audio_bytes: Raw audio bytes (for proxy mode)
            filename: Filename hint for the audio
            
        Returns:
            Transcribed text
        """
        if self._use_proxy:
            if audio_bytes is None and audio_path:
                with open(audio_path, "rb") as f:
                    audio_bytes = f.read()
            
            if audio_bytes is None:
                raise ValueError("audio_bytes or audio_path required")
            
            payload = {
                "endpoint": "whisper",
                "audio_base64": base64.b64encode(audio_bytes).decode("utf-8"),
                "audio_filename": filename,
            }
            result = self._make_proxy_request(payload)
            return result.get("text", "")
        else:
            if audio_path is None:
                raise ValueError("audio_path required for direct mode")
            result = self._make_direct_request("whisper", {"audio_path": audio_path})
            return result.get("text", "")
    
    def create_embedding(
        self,
        text: str,
        model: str = "text-embedding-3-small",
    ) -> List[float]:
        """
        Create text embedding.
        
        Args:
            text: Text to embed
            model: Embedding model to use
            
        Returns:
            List of floats (embedding vector)
        """
        payload = {
            "endpoint": "embeddings",
            "model": model,
            "input": text,
        }
        
        if self._use_proxy:
            result = self._make_proxy_request(payload)
        else:
            result = self._make_direct_request("embeddings", payload)
        
        if result.get("data") and len(result["data"]) > 0:
            return result["data"][0].get("embedding", [])
        return []


# Global instance
api_client = APIClient()


def get_api_client() -> APIClient:
    """Get the global API client instance."""
    return api_client
