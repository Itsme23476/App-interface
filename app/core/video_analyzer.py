"""
Video and audio analysis using OpenAI.
- Videos: Extract key frames + first 30s audio → analyze with GPT-4o-mini vision + Whisper
- Audio: Transcribe with Whisper → analyze text with GPT

Uses keyframe extraction for efficient video analysis without uploading entire files.
"""

import logging
import os
import base64
import json
import tempfile
from pathlib import Path
from typing import Dict, Any, Optional, List
from io import BytesIO

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    cv2 = None

from PIL import Image

# moviepy import is done lazily to avoid import-time failures
MOVIEPY_AVAILABLE = None  # Will be set on first use

logger = logging.getLogger(__name__)

# Video extensions supported
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.wmv', '.flv', '.3gp'}

# Audio extensions supported
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a'}

# Frame analysis prompt - emphasizes specificity
VIDEO_FRAME_PROMPT = """Analyze this video frame carefully. Be SPECIFIC - identify exact names, not generic categories.

Return a JSON object with:
{
  "scene": "<what's happening - be specific about activities>",
  "software_apps": ["<EXACT names of visible software/apps: 'Cursor IDE', 'VS Code', 'OBS Studio', 'Chrome', 'Discord', 'Figma', etc.>"],
  "websites_brands": ["<EXACT names of websites/brands visible: 'YouTube', 'GitHub', 'Supabase', 'Vercel', 'Twitter', etc.>"],
  "text_visible": "<read and include any visible text, titles, error messages, menu items>",
  "objects_people": ["<other visible objects or people>"],
  "tags": ["<10-12 SPECIFIC lowercase tags - use exact names like 'cursor ide' not 'code editor', 'obs studio' not 'recording software'>"],
  "mood": "<overall mood/tone>"
}

IMPORTANT: 
- Read any visible text/logos to identify exact software and website names
- Be specific: 'react typescript' not just 'coding', 'youtube tutorial' not just 'video'
- Include visible brand names, product names, technology names

Return ONLY valid JSON, no markdown."""

# Combined video analysis prompt - AUDIO IS PRIMARY, frames are supporting context
VIDEO_COMBINE_PROMPT = """Analyze this video based on the audio transcript (PRIMARY) and frame analyses (supporting context).

## PRIORITY ORDER FOR GENERATING TAGS:
1. **AUDIO TRANSCRIPT (MOST IMPORTANT)** - What the speaker says tells you WHAT the video is about:
   - Software names mentioned: "I'm using Cursor", "let's open Supabase"
   - Technologies discussed: "we'll use React", "this is TypeScript"
   - Concepts explained: "authentication", "API integration", "database"
   - Project names, people, websites mentioned

2. **VISIBLE TEXT IN FRAMES** - Software names, website URLs, error messages, menu items

3. **VISUAL CONTEXT** - What's on screen (screen recording, webcam, etc.)

## CRITICAL RULES:
- Extract EVERY specific name from the audio transcript
- The transcript tells you what the video is ABOUT, frames show HOW it looks
- Be SPECIFIC: "Cursor IDE" not "code editor", "Supabase" not "database"

Return a JSON object with:
{
  "type": "<tutorial, vlog, gameplay, music video, interview, presentation, screen recording, home video, other>",
  "caption": "<2-3 sentences describing what the speaker is TALKING about in the video>",
  "tags": ["<25-35 SPECIFIC lowercase tags - prioritize terms SPOKEN in audio over visual observations>"],
  "confidence": <float 0-1>
}

AVOID generic tags: coding, software, computer, technology, interface, website, programming
PREFER specific tags from audio: cursor ide, supabase, vercel, react, typescript, openai api, authentication

"""


def is_video_file(file_path: Path) -> bool:
    """Check if a file is a video file based on extension."""
    return file_path.suffix.lower() in VIDEO_EXTENSIONS


def is_audio_file(file_path: Path) -> bool:
    """Check if a file is an audio file based on extension."""
    return file_path.suffix.lower() in AUDIO_EXTENSIONS


def _get_moviepy():
    """Lazy import of moviepy to avoid import-time failures.
    Supports both moviepy 1.x (moviepy.editor) and moviepy 2.x (moviepy direct).
    """
    global MOVIEPY_AVAILABLE
    
    if MOVIEPY_AVAILABLE is None:
        VideoFileClip = None
        concatenate_audioclips = None
        
        # Try moviepy 2.x imports first (direct imports)
        try:
            from moviepy import VideoFileClip as VFC
            VideoFileClip = VFC
            try:
                from moviepy import concatenate_audioclips as CAC
                concatenate_audioclips = CAC
            except ImportError:
                # Try alternative location in moviepy 2.x
                try:
                    from moviepy.audio.AudioClip import concatenate_audioclips as CAC
                    concatenate_audioclips = CAC
                except ImportError:
                    concatenate_audioclips = None
            MOVIEPY_AVAILABLE = True
            logger.info("moviepy 2.x loaded successfully")
            return VideoFileClip, concatenate_audioclips
        except ImportError:
            pass
        
        # Try moviepy 1.x imports (editor submodule)
        try:
            from moviepy.editor import VideoFileClip as VFC
            VideoFileClip = VFC
            try:
                from moviepy.editor import concatenate_audioclips as CAC
                concatenate_audioclips = CAC
            except ImportError:
                concatenate_audioclips = None
            MOVIEPY_AVAILABLE = True
            logger.info("moviepy 1.x loaded successfully")
            return VideoFileClip, concatenate_audioclips
        except ImportError as e:
            logger.warning(f"moviepy not available: {e}")
            MOVIEPY_AVAILABLE = False
            return None, None
    elif MOVIEPY_AVAILABLE:
        # Return cached imports - try 2.x first, then 1.x
        try:
            from moviepy import VideoFileClip
            try:
                from moviepy import concatenate_audioclips
            except ImportError:
                try:
                    from moviepy.audio.AudioClip import concatenate_audioclips
                except ImportError:
                    concatenate_audioclips = None
            return VideoFileClip, concatenate_audioclips
        except ImportError:
            from moviepy.editor import VideoFileClip
            try:
                from moviepy.editor import concatenate_audioclips
            except ImportError:
                concatenate_audioclips = None
            return VideoFileClip, concatenate_audioclips
    else:
        return None, None


def extract_audio_snippets(video_path: Path, snippet_duration: int = 40) -> Optional[str]:
    """
    Extract 3 audio snippets from video: beginning, middle, end.
    This gives better coverage of video content than just the first 30 seconds.
    
    Args:
        video_path: Path to video file
        snippet_duration: Duration of each snippet in seconds (default 40s, total ~2 min to stay under Whisper 25MB limit)
        
    Returns:
        Path to temporary WAV file with combined audio, or None on failure
    """
    VideoFileClip, concatenate_audioclips = _get_moviepy()
    
    if VideoFileClip is None:
        logger.warning("moviepy not available - cannot extract audio from video")
        return None
    
    video = None
    try:
        # Load video
        video = VideoFileClip(str(video_path))
        
        if video.audio is None:
            logger.info(f"No audio track in video: {video_path.name}")
            video.close()
            return None
        
        video_duration = video.duration
        
        # Log audio track info for debugging
        try:
            audio_duration = video.audio.duration if hasattr(video.audio, 'duration') else 'unknown'
            logger.debug(f"Video {video_path.name}: duration={video_duration:.1f}s, audio_duration={audio_duration}")
        except Exception:
            pass  # Ignore logging errors
        
        if video_duration < 5:
            logger.info(f"Video too short for audio extraction: {video_path.name}")
            video.close()
            return None
        
        # Calculate sample positions: 5%, 50%, 90% of video
        # For a 40-min video, this gives us audio from ~2min, ~20min, ~36min
        sample_positions = [
            video_duration * 0.05,   # Near start (skip first few seconds of intro)
            video_duration * 0.50,   # Middle
            video_duration * 0.90,   # Near end (skip outro)
        ]
        
        # Adjust snippet duration if video is short
        actual_snippet_duration = min(snippet_duration, video_duration / 4)
        
        audio_clips = []
        for pos in sample_positions:
            # Calculate start/end, ensuring we don't go past video bounds
            start = max(0, pos - actual_snippet_duration / 2)
            end = min(video_duration, start + actual_snippet_duration)
            
            # Ensure we have at least 5 seconds
            if end - start >= 5:
                try:
                    # Try moviepy 2.x API first (subclipped), then 1.x (subclip)
                    if hasattr(video.audio, 'subclipped'):
                        clip = video.audio.subclipped(start, end)
                    else:
                        clip = video.audio.subclip(start, end)
                    audio_clips.append(clip)
                    logger.debug(f"Extracted audio clip {start:.1f}s-{end:.1f}s")
                except Exception as e:
                    logger.warning(f"Could not extract audio at {start:.1f}s-{end:.1f}s: {type(e).__name__}: {e}")
        
        if not audio_clips:
            # Fallback: try extracting full audio (limited to first 3 minutes)
            logger.info(f"Subclipping failed, trying full audio extraction for {video_path.name}")
            try:
                max_audio_duration = min(video_duration, 180)  # Max 3 minutes
                # Try to get the full audio
                full_audio = video.audio
                if full_audio.duration and full_audio.duration > max_audio_duration:
                    # Trim to max duration
                    if hasattr(full_audio, 'subclipped'):
                        full_audio = full_audio.subclipped(0, max_audio_duration)
                    else:
                        full_audio = full_audio.subclip(0, max_audio_duration)
                
                # Save directly to temp file
                temp_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
                temp_path = temp_file.name
                temp_file.close()
                
                full_audio.write_audiofile(temp_path, logger=None, bitrate="64k")
                video.close()
                
                logger.info(f"Extracted full audio ({max_audio_duration:.1f}s) from {video_path.name}")
                return temp_path
            except Exception as e:
                logger.warning(f"Full audio extraction also failed for {video_path.name}: {type(e).__name__}: {e}")
                video.close()
                return None
        
        # Concatenate audio clips (or use first one if concatenation not available)
        if len(audio_clips) == 1:
            combined_audio = audio_clips[0]
            clips_used = 1
        elif concatenate_audioclips is not None:
            combined_audio = concatenate_audioclips(audio_clips)
            clips_used = len(audio_clips)
        else:
            # Fallback: just use the first clip if we can't concatenate
            combined_audio = audio_clips[0]
            clips_used = 1
            logger.debug("concatenate_audioclips not available, using single clip")
        
        # Save to temporary WAV file
        temp_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
        temp_path = temp_file.name
        temp_file.close()
        
        # Write audio to temp file (suppress moviepy's verbose output)
        combined_audio.write_audiofile(temp_path, logger=None, bitrate="64k")
        
        # Calculate total duration
        if clips_used > 1:
            total_extracted = sum(clip.duration for clip in audio_clips)
        else:
            total_extracted = combined_audio.duration
        
        # Clean up
        for clip in audio_clips:
            try:
                clip.close()
            except:
                pass
        if clips_used > 1 and combined_audio is not audio_clips[0]:
            try:
                combined_audio.close()
            except:
                pass
        video.close()
        
        logger.info(f"Extracted {total_extracted:.1f}s audio ({clips_used} clips) from {video_path.name}")
        return temp_path
        
    except Exception as e:
        logger.warning(f"Could not extract audio from {video_path.name}: {e}")
        if video:
            try:
                video.close()
            except:
                pass
        return None


def extract_audio_snippet(video_path: Path, max_duration_seconds: int = 30) -> Optional[str]:
    """
    Extract the first N seconds of audio from a video file.
    DEPRECATED: Use extract_audio_snippets() for better coverage.
    
    Args:
        video_path: Path to video file
        max_duration_seconds: Maximum duration to extract (default 30 seconds)
        
    Returns:
        Path to temporary WAV file, or None on failure
    """
    VideoFileClip, _ = _get_moviepy()
    
    if VideoFileClip is None:
        logger.warning("moviepy not available - cannot extract audio from video")
        return None
    
    video = None
    try:
        # Load video and extract audio
        video = VideoFileClip(str(video_path))
        
        if video.audio is None:
            logger.info(f"No audio track in video: {video_path.name}")
            video.close()
            return None
        
        # Get duration - extract first N seconds or full duration if shorter
        duration = min(video.duration, max_duration_seconds)
        
        if duration < 1:
            logger.info(f"Video too short for audio extraction: {video_path.name}")
            video.close()
            return None
        
        # Extract audio subclip (moviepy 2.x uses subclipped, 1.x uses subclip)
        if hasattr(video.audio, 'subclipped'):
            audio_clip = video.audio.subclipped(0, duration)
        else:
            audio_clip = video.audio.subclip(0, duration)
        
        # Save to temporary WAV file
        temp_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
        temp_path = temp_file.name
        temp_file.close()
        
        # Write audio to temp file (suppress moviepy's verbose output)
        audio_clip.write_audiofile(temp_path, logger=None, bitrate="64k")
        
        # Clean up
        audio_clip.close()
        video.close()
        
        logger.info(f"Extracted {duration:.1f}s audio from {video_path.name}")
        return temp_path
        
    except Exception as e:
        logger.warning(f"Could not extract audio from {video_path.name}: {e}")
        if video:
            try:
                video.close()
            except:
                pass
        return None


def _transcribe_audio_snippet(audio_path: str) -> Optional[str]:
    """
    Transcribe an audio file using OpenAI Whisper.
    
    Args:
        audio_path: Path to audio file (WAV format)
        
    Returns:
        Transcript text, or None on failure
    """
    try:
        from openai import OpenAI
    except ImportError:
        return None
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    
    try:
        client = OpenAI(api_key=api_key)
        
        with open(audio_path, 'rb') as audio_file:
            transcript_response = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                response_format="text"
            )
        
        transcript = transcript_response if isinstance(transcript_response, str) else str(transcript_response)
        
        # Clean up temp file
        try:
            os.unlink(audio_path)
        except:
            pass
        
        if transcript and len(transcript.strip()) > 10:
            logger.info(f"Transcribed audio: {len(transcript)} chars")
            return transcript.strip()
        
        return None
        
    except Exception as e:
        logger.warning(f"Could not transcribe audio: {e}")
        # Clean up temp file
        try:
            os.unlink(audio_path)
        except:
            pass
        return None


def _pil_to_base64(img: Image.Image, max_size: int = 512) -> str:
    """Convert PIL Image to base64 string, resizing if needed."""
    # Resize to reduce API costs
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=85)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def extract_key_frames(video_path: Path, num_frames: int = 5) -> List[Image.Image]:
    """
    Extract key frames from a video at evenly spaced intervals.
    
    Args:
        video_path: Path to video file
        num_frames: Number of frames to extract (default 5: 0%, 25%, 50%, 75%, 100%)
        
    Returns:
        List of PIL Images
    """
    if not CV2_AVAILABLE:
        logger.error("OpenCV not available - cannot extract video frames")
        return []
    
    frames = []
    cap = None
    
    try:
        cap = cv2.VideoCapture(str(video_path))
        
        if not cap.isOpened():
            logger.error(f"Could not open video: {video_path}")
            return []
        
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        if total_frames <= 0:
            logger.error(f"Could not get frame count for: {video_path}")
            return []
        
        # Calculate frame positions (evenly spaced)
        if num_frames == 1:
            positions = [total_frames // 2]  # Middle frame
        else:
            positions = [int(total_frames * i / (num_frames - 1)) for i in range(num_frames)]
            # Ensure last position doesn't exceed total
            positions[-1] = min(positions[-1], total_frames - 1)
        
        for pos in positions:
            cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
            ret, frame = cap.read()
            
            if ret and frame is not None:
                # Convert BGR (OpenCV) to RGB (PIL)
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_image = Image.fromarray(rgb_frame)
                frames.append(pil_image)
            else:
                logger.warning(f"Could not read frame at position {pos}")
        
        logger.info(f"Extracted {len(frames)} frames from {video_path.name}")
        return frames
        
    except Exception as e:
        logger.error(f"Error extracting frames from {video_path}: {e}")
        return []
    finally:
        if cap is not None:
            cap.release()


def _analyze_single_frame(client, frame: Image.Image, frame_idx: int) -> Optional[Dict[str, Any]]:
    """Analyze a single frame with GPT-4o-mini vision."""
    try:
        from .settings import settings
        
        frame_b64 = _pil_to_base64(frame)
        data_url = f"data:image/jpeg;base64,{frame_b64}"
        
        model = settings.openai_vision_model or "gpt-4o-mini"
        
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a video frame analyzer. Be concise and accurate."},
                {"role": "user", "content": [
                    {"type": "text", "text": VIDEO_FRAME_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]}
            ],
            temperature=0.2,
            max_tokens=300,
        )
        
        content = resp.choices[0].message.content or ""
        
        # Parse JSON
        try:
            # Find JSON in response
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1:
                data = json.loads(content[start:end+1])
                return data
        except json.JSONDecodeError:
            logger.warning(f"Could not parse frame {frame_idx} response as JSON")
        
        return None
        
    except Exception as e:
        logger.error(f"Error analyzing frame {frame_idx}: {e}")
        return None


def _combine_frame_analyses(client, frame_analyses: List[Dict[str, Any]], filename: str, transcript: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Combine multiple frame analyses into unified video metadata."""
    try:
        from .settings import settings
        
        # Format frame analyses for the prompt
        analyses_text = "Frame analyses:"
        for i, analysis in enumerate(frame_analyses):
            analyses_text += f"\nFrame {i+1}: {json.dumps(analysis)}"
        
        # Add audio transcript if available
        if transcript:
            # Truncate transcript if too long
            truncated_transcript = transcript[:2000] if len(transcript) > 2000 else transcript
            analyses_text += f"\n\nAudio transcript (first 30 seconds):\n{truncated_transcript}"
        else:
            analyses_text += "\n\nAudio transcript: (no audio available)"
        
        analyses_text += f"\n\nFilename: {filename}"
        
        model = settings.openai_vision_model or "gpt-4o-mini"
        
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a video content summarizer. Be SPECIFIC with names of software, websites, and technologies."},
                {"role": "user", "content": VIDEO_COMBINE_PROMPT + analyses_text}
            ],
            temperature=0.2,
            max_tokens=600,
        )
        
        content = resp.choices[0].message.content or ""
        
        # Parse JSON
        try:
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1:
                data = json.loads(content[start:end+1])
                return data
        except json.JSONDecodeError:
            logger.warning("Could not parse combined analysis as JSON")
        
        return None
        
    except Exception as e:
        logger.error(f"Error combining frame analyses: {e}")
        return None


def analyze_video(video_path: Path) -> Optional[Dict[str, Any]]:
    """
    Analyze a video by extracting key frames and using GPT-4o-mini vision.
    
    Args:
        video_path: Path to video file
        
    Returns:
        Dictionary with label, tags, caption, etc. or None on failure
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("OpenAI library not available")
        return None
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("No OpenAI API key - skipping video analysis")
        return None
    
    try:
        client = OpenAI(api_key=api_key)
        
        # Extract key frames
        frames = extract_key_frames(video_path, num_frames=5)
        
        if not frames:
            logger.warning(f"Could not extract frames from {video_path.name}")
            return None
        
        # Extract and transcribe audio (3 clips: beginning, middle, end - ~3 min total)
        # Audio is PRIMARY for understanding video content
        transcript = None
        audio_path = extract_audio_snippets(video_path)
        if audio_path:
            transcript = _transcribe_audio_snippet(audio_path)
            if transcript:
                logger.info(f"Transcribed {len(transcript)} chars of audio from {video_path.name}")
        
        # Analyze each frame
        frame_analyses = []
        for i, frame in enumerate(frames):
            analysis = _analyze_single_frame(client, frame, i)
            if analysis:
                frame_analyses.append(analysis)
        
        if not frame_analyses:
            logger.warning(f"Could not analyze any frames from {video_path.name}")
            return None
        
        # Combine frame analyses with audio transcript
        combined = _combine_frame_analyses(client, frame_analyses, video_path.name, transcript=transcript)
        
        if not combined:
            # Fallback: merge tags from individual frames
            all_tags = set()
            for analysis in frame_analyses:
                tags = analysis.get('tags', [])
                if isinstance(tags, list):
                    all_tags.update(t.lower() for t in tags if isinstance(t, str))
            
            combined = {
                "type": "video",
                "caption": f"Video file: {video_path.name}",
                "tags": list(all_tags)[:25],
                "confidence": 0.5
            }
        
        # Normalize the result
        label = str(combined.get("type", "video")).strip().lower()
        tags = combined.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        # Allow up to 35 tags since we're now getting more from audio
        tags = [str(t).lower()[:64] for t in tags if isinstance(t, str)][:35]
        caption = str(combined.get("caption", "")).strip()[:1200]
        
        try:
            confidence = float(combined.get("confidence", 0.8))
        except (ValueError, TypeError):
            confidence = 0.8
        
        # Determine AI source based on whether we used audio
        ai_source = "openai:gpt-4o-mini:frames+audio" if transcript else "openai:gpt-4o-mini:frames"
        
        result = {
            "label": label,
            "tags": tags,
            "caption": caption,
            "vision_confidence": confidence,
            "ai_source": ai_source,
        }
        
        audio_info = " (with audio)" if transcript else " (no audio)"
        logger.info(f"Video analysis successful for {video_path.name}: {label}, {len(tags)} tags{audio_info}")
        return result
        
    except Exception as e:
        logger.error(f"Error analyzing video {video_path}: {e}")
        return None


def _get_audio_metadata(audio_path: Path) -> Dict[str, str]:
    """
    Extract metadata (artist, album, title, genre) from audio file using mutagen.
    
    Returns:
        Dictionary with metadata fields that exist
    """
    try:
        from mutagen import File as MutagenFile
        from mutagen.easyid3 import EasyID3
        from mutagen.mp3 import MP3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4
    except ImportError:
        logger.debug("mutagen not available - skipping metadata extraction")
        return {}
    
    metadata = {}
    
    try:
        audio = MutagenFile(str(audio_path), easy=True)
        
        if audio is None:
            return {}
        
        # Common metadata fields
        field_mappings = {
            'title': 'title',
            'artist': 'artist',
            'album': 'album',
            'genre': 'genre',
            'albumartist': 'album_artist',
        }
        
        for mutagen_key, our_key in field_mappings.items():
            if mutagen_key in audio:
                value = audio[mutagen_key]
                if isinstance(value, list) and value:
                    metadata[our_key] = str(value[0])
                elif value:
                    metadata[our_key] = str(value)
        
        # Get duration if available
        if hasattr(audio, 'info') and hasattr(audio.info, 'length'):
            metadata['duration'] = audio.info.length
        
        if metadata:
            logger.info(f"Extracted metadata from {audio_path.name}: {list(metadata.keys())}")
        
        return metadata
        
    except Exception as e:
        logger.debug(f"Could not extract metadata from {audio_path.name}: {e}")
        return {}


def _get_audio_duration(audio_path: Path) -> Optional[float]:
    """Get audio file duration in seconds using mutagen or moviepy."""
    # Try mutagen first (faster)
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(audio_path))
        if audio and hasattr(audio, 'info') and hasattr(audio.info, 'length'):
            return audio.info.length
    except Exception:
        pass
    
    # Fallback to moviepy
    VideoFileClip, _ = _get_moviepy()
    if VideoFileClip:
        try:
            # moviepy can also open audio files
            from moviepy import AudioFileClip
            clip = AudioFileClip(str(audio_path))
            duration = clip.duration
            clip.close()
            return duration
        except Exception:
            pass
    
    return None


def _sample_audio_file(audio_path: Path, snippet_duration: int = 40) -> Optional[str]:
    """
    Sample 3 clips from a long audio file (beginning, middle, end).
    Similar to video audio extraction.
    
    Args:
        audio_path: Path to audio file
        snippet_duration: Duration of each snippet in seconds
        
    Returns:
        Path to temporary MP3 file with sampled audio, or None on failure
    """
    try:
        from moviepy import AudioFileClip
    except ImportError:
        try:
            from moviepy.editor import AudioFileClip
        except ImportError:
            logger.warning("moviepy not available - cannot sample audio file")
            return None
    
    # Get concatenate function
    _, concatenate_audioclips = _get_moviepy()
    
    audio = None
    try:
        audio = AudioFileClip(str(audio_path))
        audio_duration = audio.duration
        
        if audio_duration < 5:
            logger.info(f"Audio too short for sampling: {audio_path.name}")
            audio.close()
            return None
        
        # Calculate sample positions: 5%, 50%, 90%
        sample_positions = [
            audio_duration * 0.05,
            audio_duration * 0.50,
            audio_duration * 0.90,
        ]
        
        actual_snippet_duration = min(snippet_duration, audio_duration / 4)
        
        audio_clips = []
        for pos in sample_positions:
            start = max(0, pos - actual_snippet_duration / 2)
            end = min(audio_duration, start + actual_snippet_duration)
            
            if end - start >= 5:
                try:
                    if hasattr(audio, 'subclipped'):
                        clip = audio.subclipped(start, end)
                    else:
                        clip = audio.subclip(start, end)
                    audio_clips.append(clip)
                except Exception as e:
                    logger.debug(f"Could not sample audio at {start:.1f}s: {e}")
        
        if not audio_clips:
            logger.warning(f"Could not sample any clips from {audio_path.name}")
            audio.close()
            return None
        
        # Concatenate clips
        if len(audio_clips) == 1:
            combined_audio = audio_clips[0]
        elif concatenate_audioclips:
            combined_audio = concatenate_audioclips(audio_clips)
        else:
            combined_audio = audio_clips[0]
        
        # Save to MP3
        temp_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
        temp_path = temp_file.name
        temp_file.close()
        
        combined_audio.write_audiofile(temp_path, logger=None, bitrate="64k")
        
        total_duration = sum(c.duration for c in audio_clips)
        
        # Cleanup
        for clip in audio_clips:
            try:
                clip.close()
            except:
                pass
        audio.close()
        
        logger.info(f"Sampled {total_duration:.1f}s audio from {audio_path.name}")
        return temp_path
        
    except Exception as e:
        logger.warning(f"Could not sample audio from {audio_path.name}: {e}")
        if audio:
            try:
                audio.close()
            except:
                pass
        return None


def analyze_audio(audio_path: Path) -> Optional[Dict[str, Any]]:
    """
    Analyze an audio file using OpenAI Whisper for transcription + GPT for analysis.
    
    Strategy:
    - Short files (< 3 min): Transcribe full file
    - Long files (≥ 3 min): Sample 3 clips of 40s each (beginning, middle, end)
    - Extract metadata (artist, album, title, genre) and include in tags
    
    Args:
        audio_path: Path to audio file
        
    Returns:
        Dictionary with label, tags, caption, etc. or None on failure
    """
    try:
        from openai import OpenAI
    except ImportError:
        logger.error("OpenAI library not available")
        return None
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.warning("No OpenAI API key - skipping audio analysis")
        return None
    
    try:
        client = OpenAI(api_key=api_key)
        
        # Extract metadata first
        metadata = _get_audio_metadata(audio_path)
        metadata_tags = []
        
        # Add metadata as tags
        if metadata.get('artist'):
            metadata_tags.append(metadata['artist'].lower())
        if metadata.get('album'):
            metadata_tags.append(metadata['album'].lower())
        if metadata.get('title'):
            metadata_tags.append(metadata['title'].lower())
        if metadata.get('genre'):
            metadata_tags.append(metadata['genre'].lower())
        
        # Get duration and decide strategy
        duration = metadata.get('duration') or _get_audio_duration(audio_path)
        
        # Threshold: 3 minutes = 180 seconds
        SHORT_AUDIO_THRESHOLD = 180
        
        temp_file_to_delete = None
        file_to_transcribe = audio_path
        
        if duration and duration >= SHORT_AUDIO_THRESHOLD:
            # Long file - sample 3 clips
            logger.info(f"Audio file {audio_path.name} is {duration:.0f}s - sampling 3 clips")
            sampled_path = _sample_audio_file(audio_path, snippet_duration=40)
            if sampled_path:
                file_to_transcribe = Path(sampled_path)
                temp_file_to_delete = sampled_path
            else:
                logger.warning(f"Sampling failed for {audio_path.name}, will try full file")
        else:
            logger.info(f"Audio file {audio_path.name} is short ({duration:.0f}s if known) - transcribing full file")
        
        # Transcribe with Whisper
        transcript = None
        try:
            with open(file_to_transcribe, 'rb') as audio_file:
                transcript_response = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="text"
                )
            transcript = transcript_response if isinstance(transcript_response, str) else str(transcript_response)
            
            if transcript:
                logger.info(f"Transcribed {len(transcript)} chars from {audio_path.name}")
        except Exception as e:
            logger.warning(f"Could not transcribe {audio_path.name}: {e}")
        finally:
            # Clean up temp file
            if temp_file_to_delete:
                try:
                    os.unlink(temp_file_to_delete)
                except:
                    pass
        
        # Build context for GPT
        context_parts = []
        
        if metadata:
            meta_str = ", ".join(f"{k}: {v}" for k, v in metadata.items() if k != 'duration')
            if meta_str:
                context_parts.append(f"Metadata: {meta_str}")
        
        if transcript:
            # Truncate if too long
            truncated = transcript[:3000] if len(transcript) > 3000 else transcript
            context_parts.append(f"Transcript:\n{truncated}")
        
        context_parts.append(f"Filename: {audio_path.name}")
        
        context = "\n\n".join(context_parts)
        
        # Analyze with GPT
        from .settings import settings
        model = settings.openai_vision_model or "gpt-4o-mini"
        
        analysis_prompt = f"""Analyze this audio file and return JSON:
{{
  "type": "<audio type: song, podcast, audiobook, voice memo, interview, lecture, tutorial, other>",
  "caption": "<2-3 sentence description of the audio content>",
  "tags": ["<20-30 SPECIFIC lowercase tags - include artist names, song titles, topics discussed, people mentioned>"],
  "language": "<detected language or 'instrumental' for music without vocals>",
  "confidence": <float 0-1>
}}

{context}

IMPORTANT:
- Be SPECIFIC with names: artist names, song titles, people mentioned, topics discussed
- Avoid generic tags like 'music', 'audio', 'sound' unless truly applicable
- Include any proper nouns from metadata or transcript

Return ONLY valid JSON."""

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are an audio content analyzer. Be specific with names and topics."},
                {"role": "user", "content": analysis_prompt}
            ],
            temperature=0.2,
            max_tokens=600,
        )
        
        content = resp.choices[0].message.content or ""
        
        # Parse JSON
        try:
            start = content.find('{')
            end = content.rfind('}')
            if start != -1 and end != -1:
                data = json.loads(content[start:end+1])
            else:
                data = {}
        except json.JSONDecodeError:
            data = {}
        
        # Normalize result
        label = str(data.get("type", "audio")).strip().lower()
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        
        # Combine metadata tags with GPT-generated tags (metadata first for priority)
        all_tags = metadata_tags + [str(t).lower()[:64] for t in tags if isinstance(t, str)]
        # Deduplicate while preserving order
        seen = set()
        unique_tags = []
        for tag in all_tags:
            if tag not in seen:
                seen.add(tag)
                unique_tags.append(tag)
        tags = unique_tags[:35]
        
        caption = str(data.get("caption", "")).strip()[:1200]
        
        try:
            confidence = float(data.get("confidence", 0.8))
        except (ValueError, TypeError):
            confidence = 0.8
        
        # Determine sampling info for logging
        sampled = temp_file_to_delete is not None
        
        result = {
            "label": label,
            "tags": tags,
            "caption": caption,
            "vision_confidence": confidence,
            "transcript_summary": transcript[:500] if transcript else "",
            "ai_source": "openai:whisper+gpt-4o-mini",
        }
        
        sampling_info = " (sampled)" if sampled else " (full)"
        metadata_info = f", {len(metadata_tags)} from metadata" if metadata_tags else ""
        logger.info(f"Audio analysis successful for {audio_path.name}: {label}, {len(tags)} tags{metadata_info}{sampling_info}")
        return result
        
    except Exception as e:
        logger.error(f"Error analyzing audio {audio_path}: {e}")
        return None


def test_video_extraction(video_path: str) -> Dict[str, Any]:
    """
    Test video frame extraction and analysis.
    
    Args:
        video_path: Path to a video file
        
    Returns:
        Dictionary with test results
    """
    path = Path(video_path)
    
    if not path.exists():
        return {"success": False, "error": "File not found"}
    
    if not is_video_file(path):
        return {"success": False, "error": "Not a video file"}
    
    if not CV2_AVAILABLE:
        return {"success": False, "error": "OpenCV not installed"}
    
    frames = extract_key_frames(path, num_frames=3)
    
    if frames:
        return {
            "success": True, 
            "frames_extracted": len(frames),
            "frame_sizes": [(f.width, f.height) for f in frames]
        }
    else:
        return {"success": False, "error": "Could not extract frames"}
