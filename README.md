# aggregate_linker

**aggregate_linker** is a cross-platform Python tool that dynamically aggregates files from multiple source folders into a single **unified “root” folder** using **symbolic links**.  
It automatically monitors all configured folders and keeps the unified folder synchronized in real time.  

## ✨ Features

- 🔁 **Real-time updates** — Automatically creates or removes symlinks as files appear or disappear in source folders.  
- ⚙️ **Config-driven** — All behavior is defined via a simple `config.json` file.  
- 🧹 **Flexible filtering** — Supports both global and per-source exclusion patterns.  
- 🪟 **Cross-platform** — Works on **Windows**, **Linux**, and **macOS**.  
- 🧱 **Automatic setup** — Creates default config and folder structure if none exists.  

## 📦 Requirements

- **Python 3.8+**
- **Dependencies:**
```bash
  pip install watchdog
````

Or install from `requirements.txt`:

```bash
pip install -r requirements.txt
```

## 🚀 Getting Started

### 1️⃣ Clone or Copy the Project

```bash
git clone https://github.com/Gavin1937/aggregate_linker.git
cd aggregate_linker
```

### 2️⃣ Run Once to Generate Default Config

```bash
python aggregate_linker.py
```

If `config.json` is missing, the tool will automatically create it and exit with instructions:

```
Configuration file 'config.json' not found. Creating default configuration...

--- ACTION REQUIRED ---
Default configuration written to 'config.json'.
Please edit the 'ROOT_FOLDER' and 'SOURCE_FOLDERS' list with your actual paths.
-----------------------
```

## ⚙️ Configuration

Edit the generated `config.json` file to fit your setup.

### Example:

```json
{
    "ROOT_FOLDER": "/tmp/SymlinkUnifiedRoot",
    "SOURCE_FOLDERS": [
        {
            "PATH": "/tmp/SymlinkSource_A/*.txt",
            "FINAL_EXCLUDE": "*temp*.txt"
        },
        {
            "PATH": "/tmp/SymlinkSource_B/*",
            "FINAL_EXCLUDE": "*backup*"
        }
    ],
    "GLOBAL_EXCLUDE_PATTERNS": ["*Bank1*", ".*"]
}
```

| Key                         | Description                                                                      |
|-----------------------------|----------------------------------------------------------------------------------|
| **ROOT_FOLDER**             | Target folder where all symlinks will appear.                                    |
| **SOURCE_FOLDERS**          | List of source folder configurations. Each `PATH` supports wildcards (`*`, `?`). |
| **FINAL_EXCLUDE**           | Optional per-source exclusion pattern.                                           |
| **GLOBAL_EXCLUDE_PATTERNS** | Patterns applied to all files globally.                                          |

## ▶️ Run the Aggregator

After editing the configuration, start the watcher:

```bash
python aggregate_linker.py
```

Or use a custom config file path

```bash
python aggregate_linker.py -c /path/to/my/config.json
```

Sample output:

```
Detected OS: Linux
--- Initializing Symlink Structure ---
Root Folder created/verified: /tmp/SymlinkUnifiedRoot
Globbing files in: /tmp/SymlinkSource_A
Found 12 potential items, linking 10 after filtering.
[LINK CREATED] file1.txt -> /tmp/SymlinkSource_A/file1.txt
Monitoring Directory: /tmp/SymlinkSource_A
Monitoring Directory: /tmp/SymlinkSource_B
Symlink Manager is running. Press CTRL+C to stop.
```

## 🧼 Cleanup

When you stop the program (`CTRL+C`), it automatically removes all symlinks it created but keeps the root folder intact.

```
--- Starting Cleanup Process (Removing Links Only) ---
Removed 10 symbolic links from the root.
Root folder /tmp/SymlinkUnifiedRoot remains in place.
--- Cleanup Complete ---
```

## ⚠️ Notes

### 🪟 Windows

* Run the script as **Administrator**

### 🐧 Linux / macOS

* No special privileges required.
* Uses inotify (Linux) or FSEvents (macOS) via the `watchdog` library.

### 🧩 Glob Pattern Examples

* `/data/images/*.jpg`
* `C:\\Logs\\2025-*\\*.log`

## 📁 Example Directory Structure

```
config.json
aggregate_linker.py
/tmp/
 ├── SymlinkSource_A/
 │   ├── a1.txt
 │   ├── a2_temp.txt
 ├── SymlinkSource_B/
 │   ├── b1.txt
 │   ├── b1_backup.txt
 └── SymlinkUnifiedRoot/
     ├── a1.txt -> /tmp/SymlinkSource_A/a1.txt
     ├── a2_temp.txt -> /tmp/SymlinkSource_A/a2_temp.txt
     ├── b1.txt -> /tmp/SymlinkSource_B/b1.txt
     ├── b1_backup.txt -> /tmp/SymlinkSource_B/b1_backup.txt
```

## ⚔️ Handling Duplicate Filenames

When two or more source folders contain files or directories **with the same name**,
**aggregate_linker** ensures that no file is overwritten or replaced inside the unified root folder.

### Behavior Details:

1. When creating a new symlink, the script first checks whether a file or link with the same name already exists in the root folder.
2. If such an item exists:

   * If it’s an **existing symlink**, it is left unchanged.
   * If it’s a **regular file or directory** (not a symlink), the script **skips linking** that item and logs a warning:

     ```
     Warning: Item 'example.txt' already exists in root and is not a link. Skipping.
     ```
3. As a result, **the first matching filename encountered wins**, and all subsequent duplicates are **ignored**.

This conservative approach prevents accidental overwrites or link loops and guarantees that your existing root folder content remains safe.

### Example:

If both `/SourceA/report.txt` and `/SourceB/report.txt` exist:

```
[LINK CREATED] report.txt -> /SourceA/report.txt
Warning: Item 'report.txt' already exists in root and is not a link. Skipping.
```

Result:

```
/Root/
 └── report.txt -> /SourceA/report.txt
```

## Handling source folder deletion

aggregate_linker will monitor the parent folder of all the source folder for auto healing. After a source folder get deleted and re-created, aggregate_linker will wait until no more modification happens to the newly created source folder for `HEAL_IDLE_TIMEOUT` seconds, then relink files inside. You can change `HEAL_IDLE_TIMEOUT` variable, default to 5 seconds.

## 🧩 Cross-Platform Behavior

| Platform    | Symlink Support | Notes                        |
|-------------|-----------------|------------------------------|
| **Windows** | ✅ (Admin)       | Uses `ReadDirectoryChangesW` |
| **Linux**   | ✅               | Uses inotify                 |
| **macOS**   | ✅               | Uses FSEvents                |

## 💡 Tip

To run in the background:

```bash
nohup python aggregate_linker.py &
```

On Windows PowerShell:

```powershell
Start-Job -ScriptBlock { python aggregate_linker.py }
```
