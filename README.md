# File Search Assistant v1.0

**Instantly search and autofill file paths in any application.** A privacy-first desktop assistant that brings intelligent file search to your entire workflow with a global hotkey.

## ⚡ Quick Start

Press **Ctrl+Alt+H** from anywhere in Windows to instantly search your indexed files and autofill paths into any file dialog!

## 🎯 What It Does

File Search Assistant is a powerful desktop tool that:

- **Indexes your files** with AI-powered metadata extraction (captions, OCR, tags)
- **Searches instantly** using natural language queries
- **Autofills paths** directly into file dialogs with a global hotkey
- **Works everywhere** - Photoshop, Excel, Chrome, any app with file dialogs

### Why Use This?

Stop navigating through endless folders. Just press the hotkey, type what you're looking for, and the file path is instantly filled in.

**Example workflow:**
```
1. You're in Photoshop → File → Open
2. Press Ctrl+Alt+H
3. Search "vacation photo beach sunset"
4. Press Enter
5. The path is auto-filled in the Open dialog!
```

## 🚀 Installation

### Prerequisites

- **Windows 10/11** (required for global hotkey and autofill features)
- **Python 3.11 or higher**
- **Ollama** (optional, for local AI models) - [Download here](https://ollama.ai)

### Setup

1. **Clone or download this project**
   ```bash
   cd ai_file_organizer
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **(Optional) Install Ollama models for AI features**
   ```bash
   # For image analysis (vision)
   ollama pull llama3.2-vision
   ollama pull moondream
   
   # For semantic search (embeddings)
   ollama pull nomic-embed-text
   ```

4. **Run the application**
   ```bash
   python main.py
   ```

## 📖 How to Use

### Step 1: Index Your Directories

1. Launch the app
2. Go to the **Search Files** tab
3. Click **"Select Folder to Index"**
4. Choose a folder (Documents, Pictures, Downloads, etc.)
5. Click **"Index Directory"**
6. Wait for indexing to complete (AI analysis may take time)

**What gets indexed:**
- File names and paths
- AI-generated captions for images
- OCR text from images and PDFs
- File categories and tags
- Semantic embeddings for intelligent search

### Step 2: Search in the App

Test your search in the app first:
- Type in the search box
- Results appear instantly
- Try operators: `type:image`, `tag:vacation`, `has:ocr`

### Step 3: Use Global Hotkey

Now use it anywhere in Windows:

1. Open any app (Photoshop, Word, browser, etc.)
2. Go to File → Open/Save
3. **Press Ctrl+Alt+H**
4. Search overlay appears
5. Type your query
6. Select result and press Enter
7. Path is auto-filled!

## 🔍 Search Features

### Natural Language Search

Just describe what you're looking for:
- `photo of my cat`
- `invoice from january`
- `presentation about marketing`
- `screenshot with error message`

### Search Operators

Power users can use operators:
- `type:pdf` or `label:pdf` - Find PDFs
- `tag:receipt` - Files tagged as receipts
- `has:ocr` - Files with extracted text
- `has:vision` - Files with AI captions

### AI-Powered Metadata

The indexer uses AI to understand your files:
- **Vision models** describe images ("beach sunset with palm trees")
- **OCR** extracts text from images and PDFs
- **Auto-tagging** categorizes files intelligently
- **Semantic search** finds relevant files even without exact matches

## ⚙️ Settings

### AI Providers

**Local (Default):**
- Uses Ollama with llama3.2-vision and moondream
- 100% private, no data leaves your computer
- Requires Ollama installation

**OpenAI (Optional):**
- Toggle "Use ChatGPT (OpenAI) Fallback"
- Add your OpenAI API key
- Faster but sends data to OpenAI
- Select model: gpt-4o, gpt-4o-mini, etc.

### Quick Search Settings

- **Auto-Paste**: Automatically paste path into dialogs (default: ON)
- **Auto-Confirm**: Automatically press Enter (default: OFF)
- **Shortcut**: Customize the global hotkey (default: Ctrl+Alt+H)

## 🔐 Privacy First

- **All processing happens locally** (when using Ollama)
- **No telemetry** or analytics
- **Your files stay on your computer**
- **Optional cloud AI** (OpenAI) only when you enable it

## 📁 Data Storage

The app stores its index and settings in:
- **Windows**: `%APPDATA%\ai-file-organizer\`

Files stored:
- `file_index.db` - SQLite database with file metadata
- `config.json` - Your settings
- `logs/ai_file_organizer.log` - Application logs

## 🛠️ Troubleshooting

### Global Hotkey Not Working

1. Check if another app is using Ctrl+Alt+H
2. Try changing the shortcut in Settings
3. Run the app as Administrator (some apps require elevated permissions)

### Autofill Not Working

1. Ensure "Auto-Paste" is enabled in Settings
2. The app works best with standard Windows file dialogs
3. Some apps use custom file pickers that may not be compatible
4. Check the log file for detailed error messages

### OCR/Vision Not Working

1. Install Ollama: `https://ollama.ai`
2. Pull required models:
   ```bash
   ollama pull llama3.2-vision
   ollama pull moondream
   ```
3. Make sure Ollama is running

### Indexing Is Slow

- AI vision analysis takes time (especially for images)
- Consider enabling OpenAI fallback for faster indexing
- Or disable AI features and use basic file metadata only

## 🔥 Pro Tips

1. **Index multiple folders** - Index Documents, Pictures, Downloads separately
2. **Re-index periodically** - Run indexing again to pick up new files
3. **Use descriptive filenames** - Better filenames = better search results
4. **Try semantic search** - "red car" will find images of red cars even if not in filename
5. **Combine operators** - `type:pdf has:ocr invoice` for scanned PDF invoices

## 🚀 Distribution (Coming Soon)

To package as a standalone .exe:

```bash
pip install pyinstaller
pyinstaller --name="FileSearchAssistant" --windowed --onefile main.py
```

Note: You may need a `.spec` file to include all dependencies and resources.

## 🐛 Known Limitations

- **Windows only** (macOS/Linux support planned)
- **Some apps use custom file pickers** that don't support autofill
- **Large image collections** take time to index (vision analysis is slow)
- **Semantic search requires embeddings** (needs nomic-embed-text model)

## 📝 Logs and Debugging

Check the log file for troubleshooting:
- **Location**: `%APPDATA%\ai-file-organizer\logs\ai_file_organizer.log`
- **Debug shortcuts** (while app is focused):
  - `Ctrl+Alt+D` - Dump active dialog tree
  - `Ctrl+Alt+S` - Log system state

## 🤝 Support

Having issues? Check the logs first:
1. Find log file: `%APPDATA%\ai-file-organizer\logs\`
2. Look for errors or warnings
3. Try the troubleshooting steps above

## 📜 License

This project is provided as-is for educational and personal use.

---

**Built for productivity. Designed for privacy.** 🔍✨

Press **Ctrl+Alt+H** and never navigate folders again!
