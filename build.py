"""
Build script for creating the standalone Windows executable.

Usage:
    python build.py

This will:
1. Run PyInstaller to bundle the app
2. Create output in dist/AI File Organizer/
3. You can then run Inno Setup on installer.iss to create the installer

Requirements:
    pip install pyinstaller
"""

import subprocess
import sys
import shutil
import time
from pathlib import Path

# Configuration
APP_NAME = "AI File Organizer"
MAIN_SCRIPT = "main.py"
ICON_FILE = "resources/icon.ico"  # Optional: add your icon here

def build():
    print("=" * 60)
    print(f"Building {APP_NAME}")
    print("=" * 60)
    
    # Clean previous build
    dist_path = Path("dist")
    build_path = Path("build")
    
    if dist_path.exists():
        print("Cleaning previous dist folder...")
        try:
            shutil.rmtree(dist_path)
        except PermissionError:
            # The app might still be running; try to close it and retry
            print("Previous build is still in use. Closing running app and retrying...")
            try:
                subprocess.run(
                    ["taskkill", "/IM", f"{APP_NAME}.exe", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                pass
            time.sleep(1)
            try:
                shutil.rmtree(dist_path)
            except PermissionError as e:
                print("\nERROR: Could not remove previous build.")
                print("Please close any running 'AI File Organizer' windows and try again.")
                raise e
    
    if build_path.exists():
        print("Cleaning previous build folder...")
        shutil.rmtree(build_path)
    
    # PyInstaller command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--onedir",           # Create a folder (not single file - faster startup)
        "--windowed",         # No console window
        "--noconfirm",        # Overwrite without asking
        
        # Include data files
        "--add-data", "resources;resources",
        "--add-data", "app/ui/styles.qss;app/ui",
        "--add-data", "app/ui/styles_light.qss;app/ui",
        
        # Hidden imports (packages PyInstaller might miss)
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "PIL",
        "--hidden-import", "cv2",
        "--hidden-import", "moviepy",
        "--hidden-import", "mutagen",
        "--hidden-import", "openai",
        "--hidden-import", "requests",
        "--hidden-import", "sqlite3",
        "--hidden-import", "json5",
        "--hidden-import", "pdf2image",
        "--hidden-import", "pytesseract",
        
        # Exclude unnecessary packages to reduce size
        "--exclude-module", "matplotlib",
        "--exclude-module", "notebook",
        "--exclude-module", "jupyter",
        "--exclude-module", "IPython",
        "--exclude-module", "tkinter",
    ]
    
    # Add icon if exists
    icon_path = Path(ICON_FILE)
    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])
        print(f"Using icon: {icon_path}")
    else:
        print(f"No icon found at {icon_path} - using default")
    
    # Add main script
    cmd.append(MAIN_SCRIPT)
    
    print("\nRunning PyInstaller...")
    print(f"Command: {' '.join(cmd)}\n")
    
    result = subprocess.run(cmd)
    
    if result.returncode == 0:
        print("\n" + "=" * 60)
        print("BUILD SUCCESSFUL!")
        print("=" * 60)
        print(f"\nOutput: dist/{APP_NAME}/")
        print(f"Test by running: dist/{APP_NAME}/{APP_NAME}.exe")
        print("\nNext steps:")
        print("1. Test the .exe to make sure it works")
        print("2. Install Inno Setup from https://jrsoftware.org/isinfo.php")
        print("3. Open installer.iss in Inno Setup and click 'Compile'")
        print("4. This creates the final installer: Output/AI_File_Organizer_Setup.exe")
    else:
        print("\n" + "=" * 60)
        print("BUILD FAILED!")
        print("=" * 60)
        print("Check the error messages above.")
        sys.exit(1)

if __name__ == "__main__":
    build()
