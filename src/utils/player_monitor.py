"""
Media Player Monitor
Monitors media players (especially PotPlayer) for opened anime episodes
"""

import psutil
import time
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass

@dataclass
class PlayerInfo:
    """Information about a running media player"""
    pid: int
    name: str
    window_title: str
    file_path: Optional[str]
    start_time: float

@dataclass
class EpisodeInfo:
    """Parsed episode information"""
    anime_name: str
    episode_number: int
    season_number: Optional[int] = None
    original_filename: str = ""

class PlayerMonitor:
    """Monitor media players for anime episodes"""
    
    def __init__(self, config):
        self.config = config
        self.supported_players = config.get('monitoring.supported_players', [])
        self.check_interval = config.get('monitoring.check_interval', 5)
        self.min_watch_time = config.get('monitoring.min_watch_time', 60)
        
        self.running = False
        self.monitor_thread = None
        self.active_players: Dict[int, PlayerInfo] = {}
        self.watched_episodes: Dict[str, float] = {}  # filename -> start_time
        self.updated_episodes: set = set()  # Track which episodes have been updated
        
        # Callbacks
        self.on_episode_detected: Optional[Callable[[EpisodeInfo], None]] = None
        self.on_episode_watched: Optional[Callable[[EpisodeInfo, float], None]] = None
        self.on_player_closed: Optional[Callable[[], None]] = None
    
    def start_monitoring(self):
        """Start monitoring media players"""
        if not self.running:
            self.running = True
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
    
    def stop_monitoring(self):
        """Stop monitoring media players"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1)
    
    def _monitor_loop(self):
        """Main monitoring loop"""
        while self.running:
            try:
                self._check_players()
                time.sleep(self.check_interval)
            except Exception as e:
                print(f"Error in monitor loop: {e}")
                time.sleep(self.check_interval)
    
    def _check_players(self):
        """Check for running media players"""
        current_players = {}
        
        # Find running media players
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] in self.supported_players:
                    pid = proc.info['pid']
                    
                    # Get window title first (this contains the current file)
                    window_title = self._get_window_title(pid)
                    
                    # Try to extract current file from window title
                    current_file = self._extract_file_from_title(window_title)
                    
                    # Fallback to command line if window title doesn't work
                    if not current_file:
                        cmdline = proc.info['cmdline']
                        current_file = self._extract_file_path(cmdline)
                    
                    if current_file and self._is_video_file(current_file):
                        player_info = PlayerInfo(
                            pid=pid,
                            name=proc.info['name'],
                            window_title=window_title or Path(current_file).name,
                            file_path=current_file,
                            start_time=time.time()
                        )
                        
                        current_players[pid] = player_info
                        
                        # Check if this is a new player
                        if pid not in self.active_players:
                            self._handle_new_player(player_info)
            
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        
        # Check for file changes in existing players
        for pid, current_player in current_players.items():
            if pid in self.active_players:
                old_player = self.active_players[pid]
                # Check if the file path has changed
                if (current_player.file_path != old_player.file_path and 
                    current_player.file_path is not None and 
                    old_player.file_path is not None):
                    # Add debug logging
                    print(f"File change detected for PID {pid}:")
                    print(f"  Old: {old_player.file_path}")
                    print(f"  New: {current_player.file_path}")
                    # File changed - handle old file as closed and new file as opened
                    self._handle_file_change(old_player, current_player)
        
        # Check for closed players
        closed_pids = set(self.active_players.keys()) - set(current_players.keys())
        for pid in closed_pids:
            self._handle_closed_player(self.active_players[pid])
        
        # If all players closed, trigger callback
        if self.active_players and not current_players:
            if hasattr(self, 'on_player_closed') and self.on_player_closed:
                self.on_player_closed()
        
        # Check for episodes that have been watched for 1 minute (even if still playing)
        self._check_watch_time_updates()
        
        self.active_players = current_players
    
    def _extract_file_path(self, cmdline: List[str]) -> Optional[str]:
        """Extract file path from command line arguments"""
        if not cmdline or len(cmdline) < 2:
            return None
        
        # Look for file paths in command line
        for arg in cmdline[1:]:  # Skip executable name
            if self._is_video_file(arg) and Path(arg).exists():
                return arg
        
        return None
    
    def _is_video_file(self, file_path: str) -> bool:
        """Check if file is a video file"""
        video_extensions = {
            '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', 
            '.m4v', '.3gp', '.ogv', '.ts', '.m2ts', '.vob'
        }
        
        try:
            return Path(file_path).suffix.lower() in video_extensions
        except:
            return False
    
    def _get_window_title(self, pid: int) -> Optional[str]:
        """Get window title for process (Windows-specific implementation)"""
        try:
            import win32gui
            import win32process
            
            def enum_windows_callback(hwnd, windows):
                if win32gui.IsWindowVisible(hwnd):
                    _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
                    if window_pid == pid:
                        title = win32gui.GetWindowText(hwnd)
                        if title:
                            windows.append(title)
                return True
            
            windows = []
            win32gui.EnumWindows(enum_windows_callback, windows)
            return windows[0] if windows else None
            
        except ImportError:
            # Fallback if win32gui is not available
            return None
        except:
            return None
    
    def _extract_file_from_title(self, window_title: str) -> Optional[str]:
        """Extract file path from window title"""
        if not window_title:
            return None
        
        # Support multiple players:
        # PotPlayer: "filename.ext - PotPlayer", "[position/duration] filename.ext - PotPlayer"
        # VLC: "filename.ext - VLC media player"
        # MPC-HC: "filename.ext - Media Player Classic"
        
        # Remove player suffix (including variants)
        title = window_title
        player_suffixes = [
            " - PotPlayer", " - PotPlayer Rus", " - VLC media player", 
            " - Media Player Classic", " - MPC-HC", " - mpv"
        ]
        
        for suffix in player_suffixes:
            if suffix in title:
                title = title.split(suffix)[0]
                break
        
        # Remove time indicators like [00:00/00:00] or (00:00/00:00)
        title = re.sub(r'^[\[\(]\d+:\d+[:/]\d+:\d+[\]\)]\s*', '', title)
        
        # Remove progress indicators like "50% - "
        title = re.sub(r'^\d+%\s*-\s*', '', title)
        
        # Check if it's a full path (Windows path with drive letter or UNC path)
        if title and (title.startswith('\\\\') or (len(title) > 2 and title[1] == ':')):
            # It's a full path
            if Path(title).exists() and self._is_video_file(title):
                return title
        
        # If not a full path, use the cleaned title as an identifier
        # This allows us to detect file changes even without full paths
        if title and title.strip():
            cleaned_title = title.strip()
            # Remove any remaining time stamps or episode indicators that might change
            cleaned_title = re.sub(r'\s*-\s*\d+:\d+$', '', cleaned_title)
            return cleaned_title
        
        return None
    
    def _handle_new_player(self, player_info: PlayerInfo):
        """Handle newly detected player"""
        episode_info = self._parse_episode_info(player_info)
        if episode_info and self.on_episode_detected:
            self.on_episode_detected(episode_info)
        
        # Start tracking watch time
        if player_info.file_path:
            self.watched_episodes[player_info.file_path] = time.time()
    
    def _handle_closed_player(self, player_info: PlayerInfo):
        """Handle closed player"""
        if not player_info.file_path:
            return
        
        # Just remove from tracking without triggering scrobbling
        # Scrobbling should only happen on timer, not when player closes
        start_time = self.watched_episodes.get(player_info.file_path)
        if start_time:
            # Remove from tracking
            del self.watched_episodes[player_info.file_path]
            # Remove from updated episodes set
            self.updated_episodes.discard(player_info.file_path)
    
    def _handle_file_change(self, old_player: PlayerInfo, new_player: PlayerInfo):
        """Handle when a player switches to a different file"""
        # Handle the old file as if the player closed
        self._handle_closed_player(old_player)
        
        # Handle the new file as if a new player opened
        self._handle_new_player(new_player)
    
    def _check_watch_time_updates(self):
        """Check if any currently playing episodes have reached 1 minute watch time"""
        current_time = time.time()
        
        for file_path, start_time in self.watched_episodes.items():
            watch_time = current_time - start_time
            
            # If watched for 1 minute and not yet updated
            if watch_time >= self.min_watch_time and file_path not in self.updated_episodes:
                # Find the player info for this file
                player_info = None
                for pid, info in self.active_players.items():
                    if info.file_path == file_path:
                        player_info = info
                        break
                
                if player_info:
                    episode_info = self._parse_episode_info(player_info)
                    if episode_info and self.on_episode_watched:
                        self.on_episode_watched(episode_info, watch_time)
                        # Mark as updated so we don't update again
                        self.updated_episodes.add(file_path)
    
    def _parse_episode_info(self, player_info: PlayerInfo) -> Optional[EpisodeInfo]:
        """Parse anime name and episode number from file/title"""
        if not player_info.file_path:
            return None
        
        filename = Path(player_info.file_path).stem
        
        # Common anime filename patterns
        patterns = [
            # [Group] Anime Name - S01E01 pattern
            r'^\[.*?\]\s*(.+?)\s*-\s*S\d+E(\d+)',
            # [Group] Anime Name - Episode [Quality]
            r'^\[.*?\]\s*(.+?)\s*-\s*(\d+)(?:\s*\[.*?\])?$',
            # Anime Name - Episode
            r'^(.+?)\s*-\s*(\d+)(?:\s*\[.*?\])?$',
            # Anime Name Episode Number
            r'^(.+?)\s+(\d+)(?:\s*\[.*?\])?$',
            # Anime Name SxxExx or Season x Episode x
            r'^(.+?)\s*[Ss](\d+)[Ee](\d+)',
            # [Group] Anime Name Episode Number
            r'^\[.*?\]\s*(.+?)\s*(\d+)$',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, filename, re.IGNORECASE)
            if match:
                groups = match.groups()
                anime_name = groups[0].strip()
                
                # Clean up anime name - remove brackets at the beginning and extra text
                # Remove group tags like [DKB] at the beginning
                anime_name = re.sub(r'^\[.*?\]\s*', '', anime_name)
                
                # Clean up formatting
                anime_name = re.sub(r'\s+', ' ', anime_name)  # Multiple spaces to single
                anime_name = re.sub(r'[_\.]', ' ', anime_name)  # Underscores/dots to spaces
                
                # Remove quality indicators and extra info
                anime_name = re.sub(r'\s*\[.*?\]\s*', ' ', anime_name)  # Remove any [quality] tags
                anime_name = re.sub(r'\s*\(.*?\)\s*', ' ', anime_name)  # Remove any (info) tags
                anime_name = re.sub(r'\s*-\s*S\d+E\d+.*$', '', anime_name)  # Remove season/episode at end
                
                # For example '[DKB] Lazarus - S01E01 [...]' -> 'Lazarus'
                # Remove extra dashes and clean up spaces
                anime_name = re.sub(r'\s*--\s*', ' ', anime_name)  # Remove double dashes
                anime_name = re.sub(r'\s*-\s*$', '', anime_name)  # Remove trailing dash
                anime_name = re.sub(r'^\s*-\s*', '', anime_name)  # Remove leading dash
                anime_name = re.sub(r'\s+', ' ', anime_name).strip()  # Clean up spaces
                
                if len(groups) == 2:
                    # Simple episode pattern
                    try:
                        episode_num = int(groups[1])
                    except ValueError:
                        continue  # Skip if episode number is not valid
                    
                    return EpisodeInfo(
                        anime_name=anime_name,
                        episode_number=episode_num,
                        original_filename=filename
                    )
                elif len(groups) == 3:
                    # Season/episode pattern
                    season_num = int(groups[1])
                    episode_num = int(groups[2])
                    return EpisodeInfo(
                        anime_name=anime_name,
                        episode_number=episode_num,
                        season_number=season_num,
                        original_filename=filename
                    )
        
        return None
