"""
Move planning and dry-run logic.
"""

import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple
from .categorize import categorize_file


logger = logging.getLogger(__name__)


def create_move_plan(files: List[Dict[str, Any]], 
                    source_root: Path, 
                    destination_root: Path) -> List[Dict[str, Any]]:
    """
    Create a move plan for files.
    
    Args:
        files: List of file metadata dictionaries
        source_root: Source directory root
        destination_root: Destination directory root
        
    Returns:
        List of move plan dictionaries
    """
    move_plan = []
    
    try:
        for file_metadata in files:
            source_path = Path(file_metadata.get('source_path', ''))
            if not source_path.exists():
                logger.warning(f"Source file no longer exists: {source_path}")
                continue
            
            # Determine destination path
            dest_path = _calculate_destination_path(
                source_path, source_root, destination_root, file_metadata
            )
            
            # Handle filename collisions
            dest_path = _resolve_collision(dest_path)
            
            move_plan.append({
                "source_path": str(source_path.absolute()),
                "destination_path": str(dest_path.absolute()),
                "relative_destination": str(dest_path.relative_to(destination_root)),
                "file_name": source_path.name,
                "category": file_metadata.get('category', 'Misc'),
                "size": file_metadata.get('size', 0),
                "status": "planned"
            })
        
        logger.info(f"Created move plan with {len(move_plan)} files")
        return move_plan
        
    except Exception as e:
        logger.error(f"Error creating move plan: {e}")
        return []


def _calculate_destination_path(source_path: Path, 
                              source_root: Path, 
                              destination_root: Path,
                              file_metadata: Dict[str, Any]) -> Path:
    """
    Calculate the destination path for a file.
    
    Args:
        source_path: Source file path
        source_root: Source directory root
        destination_root: Destination directory root
        file_metadata: File metadata
        
    Returns:
        Calculated destination path
    """
    # Get category from metadata
    category = file_metadata.get('category', 'Misc')
    
    # Create category subdirectory
    category_dir = destination_root / category
    category_dir.mkdir(parents=True, exist_ok=True)
    
    # Destination is category directory + filename
    return category_dir / source_path.name


def _resolve_collision(dest_path: Path) -> Path:
    """
    Resolve filename collisions by adding numeric suffixes.
    
    Args:
        dest_path: Original destination path
        
    Returns:
        Collision-free destination path
    """
    if not dest_path.exists():
        return dest_path
    
    # Split path into directory, name, and extension
    directory = dest_path.parent
    name = dest_path.stem
    extension = dest_path.suffix
    
    counter = 1
    while True:
        new_name = f"{name} ({counter}){extension}"
        new_path = directory / new_name
        
        if not new_path.exists():
            return new_path
        
        counter += 1


def validate_move_plan(move_plan: List[Dict[str, Any]], 
                      source_root: Path, 
                      destination_root: Path) -> Tuple[bool, List[str]]:
    """
    Validate a move plan for safety.
    
    Args:
        move_plan: List of move plan dictionaries
        source_root: Source directory root
        destination_root: Destination directory root
        
    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []
    
    # Check that source and destination are different
    if source_root.resolve() == destination_root.resolve():
        errors.append("Source and destination directories cannot be the same")
    
    # Check that destination is not inside source
    try:
        if destination_root.resolve().is_relative_to(source_root.resolve()):
            errors.append("Destination directory cannot be inside source directory")
    except ValueError:
        pass  # Not inside source, which is good
    
    # Check that all source files still exist
    for move in move_plan:
        source_path = Path(move['source_path'])
        if not source_path.exists():
            errors.append(f"Source file no longer exists: {source_path}")
        elif not source_path.is_file():
            errors.append(f"Source path is not a file: {source_path}")
    
    # Check for potential permission issues
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
        test_file = destination_root / ".test_write_permission"
        test_file.touch()
        test_file.unlink()
    except Exception as e:
        errors.append(f"Cannot write to destination directory: {e}")
    
    return len(errors) == 0, errors


def get_plan_summary(move_plan: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Get a summary of the move plan.
    
    Args:
        move_plan: List of move plan dictionaries
        
    Returns:
        Summary dictionary
    """
    if not move_plan:
        return {"total_files": 0, "categories": {}, "total_size": 0}
    
    categories = {}
    total_size = 0
    
    for move in move_plan:
        category = move.get('category', 'Misc')
        size = move.get('size', 0)
        
        if category not in categories:
            categories[category] = {"count": 0, "size": 0}
        
        categories[category]["count"] += 1
        categories[category]["size"] += size
        total_size += size
    
    return {
        "total_files": len(move_plan),
        "categories": categories,
        "total_size": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2)
    }


