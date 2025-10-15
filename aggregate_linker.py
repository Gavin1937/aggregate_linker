import json
import os
import sys
import signal
import time
from pathlib import PurePath, Path
import platform

# We use the watchdog library, which internally utilizes inotify on Linux, 
# for efficient file system monitoring (using ReadDirectoryChangesW on Windows).
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Error: The 'watchdog' library is not installed.")
    print("Please install it using: pip install -r requirements.txt")
    sys.exit(1)

# --- CONFIGURATION & UTILITIES ---

CONFIG_FILE = "config.json"
GLOBAL_CONFIG = None
ROOT_PATH = None

# Determine default paths based on OS for auto-creation
if platform.system() == "Windows":
    TEMP_DIR = Path(os.environ.get('TEMP', 'C:\\Temp'))
    DEFAULT_ROOT_FOLDER = TEMP_DIR / "SymlinkUnifiedRoot"
    DEFAULT_SOURCE_CONFIGS = [
        {
            "PATH": str(TEMP_DIR / "SymlinkSource_A" / "*.txt"),
            "FINAL_EXCLUDE": "*temp*.txt"
        },
        {
            "PATH": str(TEMP_DIR / "SymlinkSource_B" / "*"),
            "FINAL_EXCLUDE": "*backup*"
        }
    ]
else:
    # Unix-like (Linux, macOS)
    DEFAULT_ROOT_FOLDER = Path("/tmp/SymlinkUnifiedRoot")
    DEFAULT_SOURCE_CONFIGS = [
        {
            "PATH": "/tmp/SymlinkSource_A/*.txt",
            "FINAL_EXCLUDE": "*temp*.txt"
        },
        {
            "PATH": "/tmp/SymlinkSource_B/*",
            "FINAL_EXCLUDE": "*backup*"
        }
    ]


def load_config():
    """
    Loads the configuration file. If not found, creates a default file and exits, 
    requiring the user to configure the paths.
    """
    global GLOBAL_CONFIG, ROOT_PATH
    
    # --- AUTO-CREATE CONFIG LOGIC ---
    if not os.path.exists(CONFIG_FILE):
        print(f"Configuration file '{CONFIG_FILE}' not found. Creating default configuration...")
        
        # Use simple strings for default config data
        default_config_data = {
            "ROOT_FOLDER": str(DEFAULT_ROOT_FOLDER),
            # SOURCE_FOLDERS is now a list of objects
            "SOURCE_FOLDERS": DEFAULT_SOURCE_CONFIGS,
            # Global exclusion remains at the top level
            "GLOBAL_EXCLUDE_PATTERNS": ["*Bank1*", ".*"] 
        }
        
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(default_config_data, f, indent=4)
            
            print("\n--- ACTION REQUIRED ---")
            print(f"Default configuration written to '{CONFIG_FILE}'.")
            print("Please edit the 'ROOT_FOLDER' and the 'SOURCE_FOLDERS' list with your actual paths.")
            print("The script will now exit. Rerun the script once the configuration is ready.")
            print("-----------------------")
            sys.exit(0) 
            
        except IOError as e:
            print(f"Error writing default configuration file: {e}")
            sys.exit(1)
    # --- END AUTO-CREATE CONFIG LOGIC ---

    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            
        root_path = Path(config.get("ROOT_FOLDER")).resolve()
        
        # Extract new structured source configuration
        source_configs = config.get("SOURCE_FOLDERS", [])
        global_exclude_patterns = config.get("GLOBAL_EXCLUDE_PATTERNS", [])

        if not root_path or not source_configs:
            raise ValueError("ROOT_FOLDER or SOURCE_FOLDERS cannot be empty in config.json.")

        # Ensure source folders exist (create the base directories for monitoring/linking)
        for src_config in source_configs:
            pattern = src_config.get("PATH", "")
            if not pattern:
                continue

            # Find the deepest non-wildcard directory to ensure it exists
            base_dir = Path(pattern)
            while '*' in base_dir.name or '?' in base_dir.name:
                base_dir = base_dir.parent
                if base_dir == base_dir.parent:
                    break
            
            if not base_dir.exists():
                base_dir.mkdir(parents=True, exist_ok=True)
                print(f"Created base directory for monitoring: {base_dir.resolve()}")
            
            if not base_dir.is_dir():
                print(f"Warning: Base directory for pattern '{pattern}' is not a valid directory: {base_dir}")
                
        ROOT_PATH = root_path
        GLOBAL_CONFIG = {
            "ROOT_FOLDER": root_path, 
            "SOURCE_CONFIGS": source_configs,
            "GLOBAL_EXCLUDE_PATTERNS": global_exclude_patterns
        }
        return GLOBAL_CONFIG

    except json.JSONDecodeError:
        print(f"Error: Invalid JSON format in '{CONFIG_FILE}'. Please correct or delete the file and re-run.")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading configuration: {e}")
        sys.exit(1)


def get_directories_to_monitor(config):
    """
    Extracts the unique base directories from the source patterns that Watchdog 
    needs to monitor.
    """
    monitored_dirs = set()
    
    for src_config in config["SOURCE_CONFIGS"]:
        source_string = src_config.get("PATH", "")
        if not source_string: continue
        
        path_obj = Path(source_string)
        
        # Find the deepest directory path that does NOT contain a wildcard
        base_dir = path_obj
        while True:
            # Check if the current component contains a wildcard
            if '*' in base_dir.name or '?' in base_dir.name:
                # If so, the directory to monitor is the parent.
                base_dir = base_dir.parent
            else:
                # If the component has no wildcard, this is the deepest directory.
                break

            # Stop if we hit the root of the filesystem
            if base_dir == base_dir.parent:
                break
        
        # Ensure the directory exists before adding it to the monitor list
        monitor_path = base_dir.resolve()
        
        if monitor_path.is_dir() and monitor_path.exists():
            monitored_dirs.add(monitor_path)
        else:
            print(f"Warning: Cannot monitor non-existent or invalid directory '{monitor_path}' derived from pattern '{source_string}'.")
            
    return list(monitored_dirs)


def should_link(item_path):
    """
    Determines if an item should be linked based on global exclusion,
    inclusion by source path, and local exclusion (FINAL_EXCLUDE).
    Returns True if the item should be linked, False otherwise.
    """
    global GLOBAL_CONFIG
    if not GLOBAL_CONFIG:
        return False

    # 1. Global Exclusion Check
    for pattern in GLOBAL_CONFIG["GLOBAL_EXCLUDE_PATTERNS"]:
        # Check against full path or just the filename
        if item_path.match(pattern) or Path(item_path.name).match(pattern):
            return False # Item is globally excluded

    # 2. Inclusion and Local Exclusion Check
    is_included = False
    
    for src_config in GLOBAL_CONFIG["SOURCE_CONFIGS"]:
        pattern = src_config.get("PATH", "")
        if not pattern: continue
        
        # Check if the item matches the source path pattern
        # Path.match() works well for glob patterns
        if item_path.match(pattern):
            is_included = True
        
        # Edge case: If the source path is a literal directory, check if the item is inside it
        pattern_path = Path(pattern).resolve()
        if not ('*' in pattern or '?' in pattern) and item_path.parent.resolve() == pattern_path:
            is_included = True
        
        # If included by this source config, apply its local exclusion
        if is_included:
            final_exclude_pattern = src_config.get("FINAL_EXCLUDE")
            if final_exclude_pattern:
                # Check against full path or just the filename
                if item_path.match(final_exclude_pattern) or Path(item_path.name).match(final_exclude_pattern):
                    # Item is included by the source path but excluded locally
                    return False 

            # Passed all checks for this specific source configuration
            return True
            
    return False # No match found in any source path


def create_link(source_path):
    """
    Creates a symbolic link for the given source path in the root folder.
    Note: On Windows, creating symbolic links typically requires running 
    Python as Administrator or enabling Developer Mode.
    """
    if not source_path.is_dir() and not source_path.is_file():
        return

    link_name = ROOT_PATH / source_path.name
    source_target = source_path.resolve()
    
    # 1. Check for existence to respect the unique name constraint
    if link_name.exists():
        if link_name.is_symlink():
            return
        else:
            print(f"Warning: Item '{link_name.name}' already exists in root and is not a link. Skipping.")
            return

    # 2. Create the symbolic link
    try:
        os.symlink(source_target, link_name, target_is_directory=source_path.is_dir())
        print(f"[LINK CREATED] {link_name.name} -> {source_target}")
    except OSError as e:
        if platform.system() == "Windows" and "operation not permitted" in str(e).lower():
            print("---------------------------------------------------------------------------------")
            print(" Windows Link Creation Error: Please run the script as Administrator or enable")
            print(" Developer Mode in Windows Settings to allow unprivileged symlink creation.")
            print("---------------------------------------------------------------------------------")
        print(f"Error creating symlink for {source_path}: {e}")

def delete_link(item_name):
    """Deletes the corresponding symbolic link from the root folder."""
    link_path = ROOT_PATH / item_name
    
    if link_path.is_symlink():
        try:
            link_path.unlink()
            print(f"[LINK DELETED] {link_path.name}")
        except OSError as e:
            print(f"Error deleting symlink {link_path}: {e}")
    else:
        pass

def create_initial_links(config):
    """Creates the root folder and generates initial symlinks from all resolved sources."""
    print("--- Initializing Symlink Structure ---")
    
    ROOT_PATH.mkdir(parents=True, exist_ok=True)
    print(f"Root Folder created/verified: {ROOT_PATH}")

    items_to_link = set()
    
    # Iterate over the new structured source configuration
    for src_config in config["SOURCE_CONFIGS"]:
        pattern = src_config.get("PATH", "")
        if not pattern: continue
        
        pattern_path = Path(pattern)
        
        # Check if the pattern contains a glob part; if not, treat it as a direct path to its contents
        if '*' not in pattern and '?' not in pattern and pattern_path.is_dir():
            # If it's a non-globbed directory, we link its contents
            print(f"Globbing files in: {pattern_path}")
            for item in pattern_path.iterdir():
                items_to_link.add(item.resolve()) 
        else:
            # Use glob to resolve the pattern (KNOWN BUG mentioned in conversation history)
            try:
                pattern_path = PurePath(pattern)
                pattern_path_idx = 0
                pattern_path_parts = pattern_path.parts
                for p in pattern_path_parts:
                    if '*' in p or '?' in p:
                        break
                    pattern_path_idx += 1
                prefix_path = Path(*pattern_path_parts[:pattern_path_idx]).resolve()
                suffix_path = os.sep.join(pattern_path_parts[pattern_path_idx:])
                print(f"Globbing files in: {prefix_path}")
                for item in prefix_path.glob(suffix_path):
                    if item.exists():
                        items_to_link.add(item.resolve())
            except Exception as e:
                print(f"Error globbing pattern {pattern}: {e}. Skipping initial links for this pattern.")


    # Filter and link
    final_items_to_link = []
    for item in items_to_link:
        if should_link(item):
            final_items_to_link.append(item)

    print(f"Found {len(items_to_link)} potential items, linking {len(final_items_to_link)} after filtering.")
    
    for item in final_items_to_link:
        create_link(item)
    
    print("--- Initial Symlink Creation Complete ---")

def cleanup(config):
    """Removes all created symlinks but keeps the root folder intact."""
    print("\n--- Starting Cleanup Process (Removing Links Only) ---")
    
    links_removed_count = 0
    
    if ROOT_PATH.exists():
        for link in ROOT_PATH.iterdir():
            if link.is_symlink():
                try:
                    link.unlink()
                    links_removed_count += 1
                except OSError as e:
                    print(f"Error during cleanup (removing link {link.name}): {e}")

        print(f"Removed {links_removed_count} symbolic links from the root.")
        print(f"Root folder {ROOT_PATH} remains in place.")

    print("--- Cleanup Complete ---")


# --- INOTIFY/WATCHDOG EVENT HANDLER ---

class SymlinkManagerHandler(FileSystemEventHandler):
    """Handles file system events (created/deleted) in source folders."""

    def on_created(self, event):
        """Called when a file or directory is created."""
        source_path = Path(event.src_path)
        
        # 1. Filter out temp files and ensure the path matches all rules
        if source_path.name.startswith(".") or not should_link(source_path):
            return
            
        # 2. Give the system a moment to finish writing the file contents.
        time.sleep(0.1) 
        
        # 3. Create the link
        create_link(source_path)

    def on_deleted(self, event):
        """Called when a file or directory is deleted."""
        deleted_name = Path(event.src_path).name
        delete_link(deleted_name)

    # Ignore modified and moved events, as the link remains valid
    def on_moved(self, event):
        pass
    
    def on_modified(self, event):
        pass


# --- MAIN EXECUTION ---

def main():
    """Main function to setup and run the symlink manager."""
    global GLOBAL_CONFIG
    
    os_name = platform.system()
    print(f"Detected OS: {os_name}")
    if os_name == "Windows":
        print("Note: On Windows, ensure you run this script as Administrator for symlink creation to succeed.")

    # 1. Load config (Exits if config.json is not found)
    config = load_config()
    create_initial_links(config)

    # 2. Setup Watchdog Observer
    event_handler = SymlinkManagerHandler()
    observer = Observer()
    
    # Get the list of unique directories to monitor
    monitored_dirs = get_directories_to_monitor(config)
    
    # Schedule an observer for each base directory
    for monitor_path in monitored_dirs:
        print(f"Monitoring Directory: {monitor_path}")
        observer.schedule(event_handler, str(monitor_path), recursive=False)
        
    observer.start()
    print("Symlink Manager is running. Press CTRL+C to stop.")

    # 3. Handle graceful shutdown (cleanup)
    def signal_handler(sig, frame):
        """Handles graceful shutdown on CTRL+C."""
        print('\nShutdown signal received. Stopping observer...')
        observer.stop()
        observer.join()
        cleanup(config)
        sys.exit(0)

    # Register the signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    # Keep the main thread alive for the observer
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        observer.stop()
        observer.join()
        cleanup(config)
        sys.exit(1)


if __name__ == "__main__":
    main()
