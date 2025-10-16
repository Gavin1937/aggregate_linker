import json
import os
import sys
import signal
import time
from pathlib import PurePath, Path
import platform
import threading # Added for the healing monitor thread

# We use the watchdog library, which internally utilizes inotify on Linux, 
# for efficient file system monitoring (using ReadDirectoryChangesW on Windows).
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Error: The 'watchdog' library is not installed.")
    print("Please install it using: pip install -r requirements.txt")
    sys.exit(1)

# --- GLOBAL HEALING STATE ---
HEAL_IDLE_TIMEOUT = 5.0  # Time in seconds the directory must be idle before healing
HEALING_PATHS = {}       # {Path(target_dir): last_activity_time}
HEALING_LOCK = threading.Lock()
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
    needs to monitor, plus their unique parent directories for self-healing.
    """
    target_monitored_dirs = set()
    parent_monitored_dirs = set()
    
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
        
        monitor_path = base_dir.resolve()
        
        if monitor_path.is_dir() and monitor_path.exists():
            target_monitored_dirs.add(monitor_path)

            # Add parent for self-healing, provided it's not the root itself
            if monitor_path.parent != monitor_path:
                parent_monitored_dirs.add(monitor_path.parent.resolve())
        else:
            print(f"Warning: Cannot monitor non-existent or invalid directory '{monitor_path}' derived from pattern '{source_string}'.")
            
    return list(target_monitored_dirs), list(parent_monitored_dirs)


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


# --- HEALING MONITOR THREAD ---

class HealingMonitor(threading.Thread):
    def __init__(self, observer_instance, target_paths):
        super().__init__()
        self.observer = observer_instance
        self.target_paths = target_paths # Set of Path objects we expect to be monitored
        self._stop_event = threading.Event()
        self.daemon = True # Allows thread to exit when main thread exits

    def stop(self):
        self._stop_event.set()

    def _perform_heal_action(self, path_to_heal):
        """Attempts to re-schedule watcher and re-link contents for an idle path."""
        if path_to_heal in self.target_paths:

            if path_to_heal.is_dir() and path_to_heal.exists():

                print(f"[HEALING ACTION] Re-scheduling watcher for idle source folder: {path_to_heal.name}")
                # Note: The handler instance (event_handler) is implicitly scheduled via the observer instance.
                # We need to ensure we schedule with the correct event handler instance.
                # Since the handler is coupled with the observer and path, we just re-schedule it.
                # The observer will handle internal checks if a watcher exists.
                self.observer.schedule(self.observer.event_handler, str(path_to_heal), recursive=False)

                # Re-run initial link creation for any content that might have been added quickly
                for item in path_to_heal.iterdir():
                    if should_link(item):
                        create_link(item)
                return True
        return False

    def run(self):
        while not self._stop_event.is_set():
            time.sleep(1) # Check every second

            paths_to_remove = []

            with HEALING_LOCK:
                current_time = time.time()

                # Use list() to iterate over a copy of keys, allowing modification to HEALING_PATHS
                for path_to_heal, last_activity in list(HEALING_PATHS.items()):

                    if current_time - last_activity >= HEAL_IDLE_TIMEOUT:
                        # Path is idle, attempt to heal
                        if self._perform_heal_action(path_to_heal):
                            paths_to_remove.append(path_to_heal)
                        else:
                            # If healing failed (e.g., dir still deleted), assume it needs future healing
                            # if it reappears, but stop monitoring its idle state for now.
                            # We keep it in HEALING_PATHS until healed, but the check handles non-existence.
                            pass

            # Clean up healed paths outside the lock
            with HEALING_LOCK:
                for path in paths_to_remove:
                    HEALING_PATHS.pop(path, None)


# --- INOTIFY/WATCHDOG EVENT HANDLER ---

class SymlinkManagerHandler(FileSystemEventHandler):
    """Handles file system events (created/deleted) in source folders."""

    # Store the monitored directories set for quick lookup
    def __init__(self, observer_instance, target_paths, parent_paths):
        super().__init__()
        self.observer = observer_instance
        # Paths where files are expected to be created/deleted (Source_A, Source_B)
        self.target_paths = target_paths # Set of Path objects we expect to be monitored
        # Paths that are parents of the target paths (for directory re-creation healing)
        self.parent_paths = parent_paths # Set of Path objects being monitored for directory creation events

    def _mark_for_healing(self, path):
        """Marks a target path as needing healing due to recent activity."""
        # Only mark if the path is a known target directory
        if path in self.target_paths:
            with HEALING_LOCK:
                # Update timestamp to the current time, restarting the idle timer
                HEALING_PATHS[path] = time.time()
                print(f"[HEALING PENDING] Marked {path.name} for delayed healing.")


    def on_created(self, event):
        """Called when a file or directory is created."""
        source_path = Path(event.src_path).resolve()

        # --- Self-Healing Check (Directory Re-creation) ---
        # 1. Check if the event occurred in one of the parent directories we're watching.
        if event.is_directory and source_path.parent.resolve() in self.parent_paths:
            # 2. Check if the created directory is one of our target paths (i.e., a deleted directory was recreated)
            if source_path in self.target_paths:
                self._mark_for_healing(source_path) # Mark for delayed healing (do NOT heal immediately)
                return

        # --- Regular File/Folder Creation Logic ---
        # Only process file/folder creation if it happened inside a target monitored path
        if source_path.parent.resolve() in self.target_paths:

            # Regular file/folder creation logic
            if source_path.name.startswith(".") or not should_link(source_path):
                return

            # Give the system a moment to finish writing the file contents.
            time.sleep(0.1)

            # 3. Create the link
            create_link(source_path)

    def on_modified(self, event):
        """Called when a file or directory is modified."""
        source_path = Path(event.src_path).resolve()

        # --- Self-Healing Check (Directory Modification in Parent) ---
        # If DirModifiedEvent occurred on a Parent Path, it indicates activity (delete/create cycle).
        if event.is_directory and source_path in self.parent_paths:

            # Check all known target directories to see if any of them now exist under this modified parent.
            for target_path in self.target_paths:
                # If the target path is a child of the modified parent
                if target_path.parent.resolve() == source_path:
                    # Mark potential target for healing
                    self._mark_for_healing(target_path)
            return

        # Regular file modification (ignored since links remain valid)
        pass

    def on_deleted(self, event):
        """Called when a file or directory is deleted."""
        source_path = Path(event.src_path).resolve()

        # If a monitored target directory is deleted, ensure it's removed from healing paths
        # as it can't be healed until recreated. When recreated, the on_created logic will handle it.
        if event.is_directory and source_path in self.target_paths:
            print(f"[UN-MONITOR] Source folder deleted: {source_path.name}")
            with HEALING_LOCK:
                HEALING_PATHS.pop(source_path, None) # Stop checking for idleness immediately

        # Check if the deleted item was inside a target monitored path
        if source_path.parent.resolve() in self.target_paths or source_path in self.target_paths:
            # Delete the corresponding link from the root
            deleted_name = source_path.name
            delete_link(deleted_name)

    def on_moved(self, event):
        # Handle cases where a monitored directory is moved/renamed, which can also break the watch
        if event.is_directory and Path(event.src_path).resolve() in self.target_paths:
            # Unschedule the old path (watch)
            self.observer.unschedule(event.watch)
            print(f"[UN-MONITOR] Directory moved/renamed: {event.src_path}")
            # Reschedule the new path if it's still a directory
            new_path = Path(event.dest_path)
            if new_path.is_dir():
                self.observer.schedule(self, str(new_path), recursive=False)
                print(f"[RE-MONITOR] Re-scheduled watcher for moved directory: {new_path}")

        # If a file is moved within a monitored directory, we need to delete the old link and create the new one
        if not event.is_directory:
            delete_link(Path(event.src_path).name) # Delete old link name
            new_path = Path(event.dest_path)
            if should_link(new_path):
                create_link(new_path) # Create new link name


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
    observer = Observer()
    
    # Get the list of unique directories to monitor
    target_paths, parent_paths = get_directories_to_monitor(config)
    
    # 3. Pass the observer and the list of monitored paths to the handler
    event_handler = SymlinkManagerHandler(observer, set(target_paths), set(parent_paths))

    # Attach handler to observer for use by HealingMonitor
    observer.event_handler = event_handler

    # 4. Schedule observers
    for monitor_path in target_paths:
        print(f"Monitoring Target Directory: {monitor_path}")
        observer.schedule(event_handler, str(monitor_path), recursive=False)

    for monitor_path in parent_paths:
        print(f"Monitoring Parent Directory for healing: {monitor_path}")
        observer.schedule(event_handler, str(monitor_path), recursive=False)
        
    observer.start()

    # 5. Start Healing Monitor Thread
    healing_monitor = HealingMonitor(observer, set(target_paths))
    healing_monitor.start()

    print("Symlink Manager is running. Press CTRL+C to stop.")

    # 6. Handle graceful shutdown (cleanup)
    def signal_handler(sig, frame):
        """Handles graceful shutdown on CTRL+C."""
        print('\nShutdown signal received. Stopping observer...')
        healing_monitor.stop() # Stop the healing thread first
        observer.stop()
        healing_monitor.join() # Wait for healing thread to finish
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
        healing_monitor.stop()
        observer.stop()
        healing_monitor.join()
        observer.join()
        cleanup(config)
        sys.exit(1)


if __name__ == "__main__":
    main()
