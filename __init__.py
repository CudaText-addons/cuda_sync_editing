# Sync Editing plugin for CudaText
# by Vladislav Utkin <vlad@teamfnd.ru>
# MIT License
# 2018

import re
import os
import time
import random
from cudatext import *
from cudatext_keys import *
from cudax_lib import get_translation
from collections import defaultdict

_ = get_translation(__file__)  # I18N

# --- Plugin Description & Logic ---
# This plugin enables Synchronous Editing (multi-cursor editing) within a specific selection.
#
# WORKFLOW (Continuous Mode):
# 1. Activation: User selects text and a gutter icon appears on the last line of selection.
# 2. Start: User clicks the gutter icon or triggers the command to start sync editing.
# 3. Analysis: The plugin scans for identifiers (variables, etc.) and highlights them with background colors.
# 4. Interaction Loop:
#    - [Selection State]: User sees highlighted words. Clicking a word triggers [Edit State].
#    - [Edit State]: Multi-carets are placed on all instances of the clicked word. User types.
#      -> If user clicks another highlighted word: Previous edit commits, new edit starts immediately on the new word.
#      -> If user moves caret off-word: Edit commits, returns to [Selection State] (markers remain visible).
# 5. Exit: Clicking the gutter icon again or pressing 'ESC' fully terminates the session.


# Set to True to enable code profiling (outputs to CudaText console).
ENABLE_PROFILING_inside_start_sync_edit = False
ENABLE_PROFILING_inside_on_caret = False
ENABLE_PROFILING_inside_redraw = False
ENABLE_PROFILING_inside_on_click = False
ENABLE_BENCH_TIMER = False # print real time spent, usefull when profiling is disabled because profiling adds more overhead

# --- Default Configuration ---
USE_COLORS_DEFAULT = True
USE_SIMPLE_NAIVE_MODE_DEFAULT = False
CASE_SENSITIVE_DEFAULT = True
IDENTIFIER_REGEX_DEFAULT = r'\w+'
# IDENTIFIER_REGEX_DEFAULT = r'\b\w+\b'

# Regex to identify valid tokens (identifiers) vs invalid ones
IDENTIFIER_STYLE_INCLUDE_DEFAULT = r'(?i)id[\w\s]*'    # Styles that are considered "Identifiers"
IDENTIFIER_STYLE_EXCLUDE_DEFAULT = '(?i).*keyword.*'   # Styles that are strictly keywords (should not be edited)

CONFIG_FILENAME = 'cuda_sync_editing.ini'
ICON_INACTIVE_PATH = os.path.join(os.path.dirname(__file__), 'sync_off.png')
ICON_ACTIVE_PATH = os.path.join(os.path.dirname(__file__), 'sync_on.png')

# Overrides for specific lexers that have unique naming conventions
NON_STANDARD_LEXERS = {
  'HTML': 'Text|Tag id correct|Tag prop',
  'PHP': 'Var',
}

# Lexers where we skip syntax parsing and just use Regex (Naive mode)
# This is useful for plain text formats or where CudaText lexers don't output specific 'Id' styles.
NAIVE_LEXERS = [
  'Markdown', # it has 'Text' rule for many chars, including punctuation+spaces
  'reStructuredText',
  'Textile',
  'ToDo',
  'Todo.txt',
  'JSON',
  'JSON ^',
  'Ini files',
  'Ini files ^',
]

MARKER_CODE = app_proc(PROC_GET_UNIQUE_TAG, '') # Generate a unique integer tag for this plugin's markers to avoid conflicts with other plugins
DECOR_TAG = app_proc(PROC_GET_UNIQUE_TAG, '')  # Unique tag for gutter decorations
TOOLTIP_TEXT = _('Sync Editing: click to toggle')

SCROLL_DEBOUNCE_DELAY = 150  # milliseconds to wait after scroll stops

SHOW_PROGRESS=True


# Check API version
# this API introduced improved events managments: PROC_EVENTS_SUB/UNSUB, and now keys=sel,selreset for on_caret_slow works as expected too
API_NEW = app_api_version() >= '1.0.471'

# --- 1. OPTIMIZATION FOR NEW API ---
# If newer API, we "upgrade" on_caret_slow to use filters (sel,selreset).
# This optimizes performance by triggering only on selection changes.
# We do this at runtime so install.inf remains compatible with old versions. because older cudatext versions do not subscribe to on_caret_slow if we set keys=sel,selreset in install.inf see https://github.com/Alexey-T/CudaText/issues/6146
# so to keep backward compatibility, we set only events=on_caret_slow without keys=sel,selreset in install.inf, then inside the plugin if api is greater than 1.0.471 we subscribe dynamically again to on_caret_slow but now with sel,selreset filter, this will unsub from on_caret_slow so plugin will consume no resources in new cudatext versions until the user select text. and in old versions it will use the old on_caret_slow which will run with every slow caret (not bad anyway)
# there is only one problem, now newer cudatext versions with the new api will load the plugin always when cudatext starts, because we do not set keys=sel,selreset in install.inf. but the plugin consumes no resources anyway.
if API_NEW:
    # This overrides the broad on_caret_slow from install.inf
    # Note: We must subscribe to this SEPARATELY from other events because it uses a filter list.
    app_proc(PROC_EVENTS_SUB, 'cuda_sync_editing;on_caret_slow;;sel,selreset')

# --- 2. LEGACY SUPPORT SETUP ---
# For older API, we must parse install.inf to preserve events when using PROC_SET_EVENTS
if not API_NEW:
    filename_install_inf = os.path.join(os.path.dirname(__file__), 'install.inf')
    
    def parse_install_inf_events():
        """
        Parse all event sections from install.inf.
        Returns a dict with event names as keys and their filter strings as values.
        Example: {'on_caret_slow': 'sel', 'on_click~': '', 'on_key~': ''}
        """
        events_dict = {}
        
        # Find all sections that start with 'item'
        item_num = 1
        while True:
            section = f'item{item_num}'
            section_type = ini_read(filename_install_inf, section, 'section', '')
            
            if not section_type:
                break  # No more sections
            
            if section_type == 'events':
                events_str = ini_read(filename_install_inf, section, 'events', '')
                keys_str = ini_read(filename_install_inf, section, 'keys', '')
                
                # Split events by comma and add to dict
                for event in events_str.split(','):
                    event = event.strip()
                    if event:
                        events_dict[event] = keys_str
            
            item_num += 1
        
        return events_dict

    install_inf_events = parse_install_inf_events()
else:
    install_inf_events = {}


def set_events_safely(events_to_add, lexer_list=''):
    """
    Set events dynamically based on API version.
    
    API >= 1.0.471:
      - Uses PROC_EVENTS_SUB/UNSUB.
      - Manages only dynamic events (on_lexer_parsed, on_scroll, on_caret, on_click, on_key, on_open_reopen).
      - Static events in install.inf are preserved automatically by CudaText.
      
    API < 1.0.471:
      - Uses PROC_SET_EVENTS.
      - Merges install.inf events but FORCEFULLY STRIPS keys/filters 
        (ignoring 'sel,selreset') to avoid old API bugs.
        
    Args:
        events_to_add: Set or list of event names to add (without filter strings)
        lexer_list: Comma-separated lexer names (optional)
    """
    
    if API_NEW:
        # --- NEW API LOGIC ---
        
        # 1. Define dynamic events managed by this plugin
        DYNAMIC_EVENTS = {'on_lexer_parsed', 'on_scroll', 'on_caret', 'on_click', 'on_key', 'on_open_reopen'}
        
        # 2. Unsubscribe from dynamic events that are NOT in the needed list
        # We must explicitly unsub to turn them off, otherwise they stick around.
        current_needed = set(events_to_add)
        to_unsub = [ev for ev in DYNAMIC_EVENTS if ev not in current_needed]
        
        if to_unsub:
            app_proc(PROC_EVENTS_UNSUB, f'cuda_sync_editing;{",".join(to_unsub)}')
            
        # 3. Subscribe to the needed dynamic events
        # Note: These dynamic events do not use filters in this context, so we pass empty filter args.
        if events_to_add:
            # Format: "module;event_list;lexer_list;filter_list"
            events_str = ','.join(events_to_add)
            app_proc(PROC_EVENTS_SUB, f'cuda_sync_editing;{events_str};{lexer_list};;')
            
    else:
        # --- LEGACY API LOGIC (Old CudaText) ---
        
        # in the old API we have to set events while preserving those from install.inf. because PROC_SET_EVENTS resets all the events including those from install.inf (only events in plugins.ini are preserved).
        # and also we should not use on_caret_slow keys filters like sel and selreset, the old API was buged, so when we parse install.inf we must remove them when we subscribe dynamically to on_caret_slow. this will not break this plugin because on_caret_slow events are suficient to detect text selections without using sel/selreset.
        
        # Combine install.inf events with new events
        all_events = {}
        
        # Add install.inf events (on_caret_slow, on_close, on_click_gutter) but FORCE empty filters (ignore keys like 'sel,selreset')
        for ev in install_inf_events:
            all_events[ev] = '' 
        
        # Add new dynamic events (on_key, on_click, etc.), without filters
        for event in events_to_add:
            if event not in all_events:
                all_events[event] = ''
        
        # Build event string
        event_names = ','.join(all_events.keys())
        
        # Filter string is explicitly empty because we stripped keys from install.inf events
        filter_strings = ''
        
        app_proc(PROC_SET_EVENTS, f"cuda_sync_editing;{event_names};{lexer_list};{filter_strings}")

def bool_to_ini(value):
    return 'true' if value else 'false'

def ini_to_bool(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('1', 'true', 'yes', 'on'):
            return True
        if normalized in ('0', 'false', 'no', 'off'):
            return False
    return default

GLOBAL_DEFAULTS = {
    'use_colors': bool_to_ini(USE_COLORS_DEFAULT),
    'use_simple_naive_mode': bool_to_ini(USE_SIMPLE_NAIVE_MODE_DEFAULT),
    'case_sensitive': bool_to_ini(CASE_SENSITIVE_DEFAULT),
    'identifier_regex': IDENTIFIER_REGEX_DEFAULT,
    'identifier_style_include': IDENTIFIER_STYLE_INCLUDE_DEFAULT,
    'identifier_style_exclude': IDENTIFIER_STYLE_EXCLUDE_DEFAULT,
}

class PluginConfig:
    """Handles reading and ensuring defaults for plugin configuration stored in an INI file."""

    _SENTINEL = '__cuda_sync_editing_missing__'

    def __init__(self):
        settings_dir = app_path(APP_DIR_SETTINGS)
        self.file_path = os.path.join(settings_dir, CONFIG_FILENAME)
        self.ensure_file()

    def ensure_file(self):
        """Creates the config file if missing and populates default keys."""
        # Add missing keys in [global] (do not overwrite existing values)
        for key, value in GLOBAL_DEFAULTS.items():
            if self._read_raw('global', key) is None:
                ini_write(self.file_path, 'global', key, value)

    def get_lexer_bool(self, lexer, key, default):
        raw = self._get_lexer_value(lexer, key)
        return ini_to_bool(raw, default)

    def get_lexer_str(self, lexer, key, default):
        raw = self._get_lexer_value(lexer, key)
        return default if raw is None else raw

    def _get_lexer_value(self, lexer, key):
        """
        Return the raw string value:
          1) If the value exists in the per-lexer section [lexer_Name], return it.
          2) Else return value from [global] if present.
          3) Else return None.
        """
        raw = None
        if lexer:
            # Try to read [lexer_Name]
            section = f'lexer_{lexer}'
            raw = self._read_raw(section, key)
        if raw is None:
            # Fallback to [global]
            raw = self._read_raw('global', key)
        return raw

    def _read_raw(self, section, key):
        result = ini_read(self.file_path, section, key, self._SENTINEL)
        return None if result == self._SENTINEL else result

def theme_color(name, is_font):
    """Retrieves color from the current CudaText theme by fetching the dictionary fresh."""
    # Load current IDE theme colors
    theme = app_proc(PROC_THEME_SYNTAX_DICT_GET, '')
    if name in theme:
        return theme[name]['color_font' if is_font else 'color_back']
    return 0x808080

def generate_color(key):
    # get random color for a given key
    hash_val = hash(key) + random.randint(0, 0xFFFFFF)
    r = ((hash_val & 0xFF0000) >> 16) % 127 + 128
    g = ((hash_val & 0x00FF00) >> 8) % 127 + 128
    b = (hash_val & 0x0000FF) % 127 + 128
    return r | (g << 8) | (b << 16)
    
class TokenRef:
    """
    Mutable token reference for efficient in-place updates. this is better than immutable tuples because it avoids recreation overhead during edits.
    """
    __slots__ = ['start_x', 'start_y', 'end_x', 'end_y', 'text', 'style']
    
    def __init__(self, start_x, start_y, end_x, end_y, text, style):
        self.start_x = start_x
        self.start_y = start_y
        self.end_x = end_x
        self.end_y = end_y
        self.text = text
        self.style = style
    
    def shift(self, delta):
        """Shift position by delta characters (in-place update)."""
        self.start_x += delta
        self.end_x += delta


class SyncEditSession:
    """
    Represents a single sync edit session for one file (tab).
    Each editor handle has its own instance of this class to maintain state isolation.
    OPTIMIZED with spatial indexing for large files.
    """
    def __init__(self):
        self.selected = False
        self.editing = False
        
        # OPTIMIZATION: Line-based spatial index for O(1) line lookups
        # Structure: { line_num: [(TokenRef, word_key)] }
        # Allows fast queries like "what tokens are on line 42?"
        self.line_index = defaultdict(list)
        
        # Dictionary stores TokenRef objects
        # Structure: { word_key: [TokenRef objects] }
        self.dictionary = defaultdict(list)
        
        self.our_key = None  # The specific word currently being edited
        self.original = None # Original caret position before editing
        self.start_l = None  # Start line of selection
        self.end_l = None    # End line of selection
        self.gutter_icon_line = None     # Line where gutter icon is displayed
        self.gutter_icon_active = False  # Whether gutter icon is currently shown as active

        # Config ini
        self.use_colors = USE_COLORS_DEFAULT
        self.use_simple_naive_mode = USE_SIMPLE_NAIVE_MODE_DEFAULT
        self.case_sensitive = CASE_SENSITIVE_DEFAULT
        self.identifier_regex = IDENTIFIER_REGEX_DEFAULT
        self.identifier_style_include = IDENTIFIER_STYLE_INCLUDE_DEFAULT
        self.identifier_style_exclude = IDENTIFIER_STYLE_EXCLUDE_DEFAULT

        # Compiled regex objects
        self.regex_identifier = None

        # Marker colors
        self.marker_fg_color = None
        self.marker_bg_color = None
        self.marker_border_color = None
        self.word_colors = {}  # Cache of {word: color} to maintain consistent colors and reduce overhead

        self.original_occurrence_index = None  # Tracks which occurrence (0, 1, 2...) the user originally clicked

        # Cache carets state for detecting problematic movements, for fast caret integrity validation
        # Built once from dictionary, reused for all subsequent checks during editing
        # More efficient than re-accessing dictionary on every keystroke
        self.cached_carets_count = None   # Number of carets we expect
        self.cached_carets_lines = None   # List of y positions (line numbers (ordered)) where carets should be

class Command:
    """
    Main Logic for Sync Editing.
    Manages the Circular State Machine: Selection <-> Editing.
    Can be toggled via gutter icon or command.
    Supports Multiple Files - one session per file.
    OPTIMIZED: plugin does minimal checks possible when not in use to save resources.
    """

    def __init__(self):
        """Initializes plugin state."""
        # Dictionary to store sessions: {editor_handle: SyncEditSession}
        self.sessions = {}
        self.inited_icon_eds = set()
        self.icon_inactive = -1
        self.icon_active = -1
        self.pending_delay_handles = set() # Track editors waiting for the 600ms lexer delay timer
        self.pending_lexer_parse_handles = set()  # Track editors waiting for lexer parsing notification
        self.active_scroll_handles = set() # Track editors with active sync sessions (for on_scroll event)
        self.active_caret_handles = set() # Track editors with active sync sessions (for on_caret event)
        self.lexer_parsed_message_shown = set() # Track editors that already showed the message box
        self.selection_scroll_handles = set() # Track editors with selections but NO active session (for on_scroll event)

    def _update_event_subscriptions(self):
        """
        Central event subscription management method
        Central method to manage all dynamic event subscriptions.
        Coordinates on_lexer_parsed, on_scroll, on_caret, on_click, on_key, and on_open_reopen based on current plugin state.
        
        This ensures:
        - on_lexer_parsed is active when editors are waiting for lexer parsing
        - on_scroll is active when editors have active sync sessions OR have selections
        - on_caret is active when editors have active sync sessions
        - on_click is active when editors have active sync sessions
        - on_key is active when editors have active sync sessions
        - on_open_reopen is active when editors have active sync sessions
        - Events are properly combined and don't conflict
        
        this necesary for now, once PROC_GET_EVENTS https://github.com/Alexey-T/CudaText/issues/6138 is implemented this can be simplified 
        """
        events_needed = []
        
        # 1. Parsing Events
        # Add on_lexer_parsed if any editor is waiting for lexer parsing
        if self.pending_lexer_parse_handles:
            events_needed.append('on_lexer_parsed')
        
        # 2. Scroll Events (Active Session OR Selection with Gutter Icon)
        # Add on_scroll if any editor has an active sync session OR has a selection
        if self.active_scroll_handles or self.selection_scroll_handles:
            events_needed.append('on_scroll')
        
        # 3. Active Session Events (Editing/Interaction)
        # Add on_caret, on_click, on_key, on_open_reopen if any editor has an active sync session
        if self.active_caret_handles:
            events_needed.append('on_caret')        # For caret tracking
            events_needed.append('on_click')        # For clicking words
            events_needed.append('on_key')          # For Esc/Arrow keys
            events_needed.append('on_open_reopen')  # For file reload safety
            
        # Update subscriptions
        set_events_safely(events_needed)

    def get_editor_handle(self, ed_self):
        """Returns a unique identifier for the editor."""
        return ed_self.get_prop(PROP_HANDLE_SELF)

    def get_session(self, ed_self):
        """Gets or creates a session for the current editor."""
        handle = self.get_editor_handle(ed_self)
        if handle not in self.sessions:
            # print("============creates a session:",handle) # DEBUG
            self.sessions[handle] = SyncEditSession()
        return self.sessions[handle]

    def has_session(self, ed_self):
        """
        Checks if editor has an active session.
        Allows checking for session existence without instantiating one.
        """
        handle = self.get_editor_handle(ed_self)
        return handle in self.sessions

    def remove_session(self, ed_self):
        """Removes the session for the current editor (cleanup)."""
        handle = self.get_editor_handle(ed_self)
        if handle in self.sessions:
            del self.sessions[handle]

    def load_gutter_icons(self, ed_self):
        """Load the gutter icon images into CudaText's imagelist."""
        _h_ed = self.get_editor_handle(ed_self)
        if _h_ed not in self.inited_icon_eds:
            # print('Sync Editing: Loading icons:', ed_self.get_filename())
            self.inited_icon_eds.add(_h_ed)
            _h_im = ed_self.decor(DECOR_GET_IMAGELIST)
            self.icon_inactive = imagelist_proc(_h_im, IMAGELIST_ADD, value=ICON_INACTIVE_PATH)
            self.icon_active = imagelist_proc(_h_im, IMAGELIST_ADD, value=ICON_ACTIVE_PATH)

    def on_close(self, ed_self):
        """
        Called when editor is closed.
        Delegates to reset() for proper cleanup of all state.
        """
        handle = self.get_editor_handle(ed_self)
        # Clean up icon tracking
        if handle in self.inited_icon_eds:
            # print('Sync Editing: Forget handle')
            self.inited_icon_eds.remove(handle)
        
        self.reset(ed_self, cleanup_lexer_events=True)

    def on_open_reopen(self, ed_self):
        """
        Called when the file is reloaded/reopened from disk (File => Reload).
        The entire document content is replaced, so all marker positions become invalid.
        We must fully exit sync editing to avoid crashes or visual glitches.
        """
        if self.has_session(ed_self):
            handle = self.get_editor_handle(ed_self)
            self.lexer_parsed_message_shown.discard(handle)
            self.reset(ed_self)
            
    def show_gutter_icon(self, ed_self, line_index, active=False):
        """Shows the gutter icon at the specified line."""
        # Remove any existing gutter icon first
        self.hide_gutter_icon(ed_self)

        # Choose icon based on active state
        icon_index = self.icon_active if active else self.icon_inactive

        ed_self.decor(DECOR_SET, line=line_index, tag=DECOR_TAG, text=''+chr(1)+TOOLTIP_TEXT, image=icon_index, auto_del=False)

        if self.has_session(ed_self):
            session = self.get_session(ed_self)
            session.gutter_icon_line = line_index
            session.gutter_icon_active = True

    def hide_gutter_icon(self, ed_self):
        """Removes the gutter icon."""
        ed_self.decor(DECOR_DELETE_BY_TAG, -1, DECOR_TAG)
        if self.has_session(ed_self):
            session = self.get_session(ed_self)
            session.gutter_icon_line = None
            session.gutter_icon_active = False

    def update_gutter_icon_on_selection(self, ed_self):
        """
        Called when selection changes (via on_caret).
        Shows gutter icon at the best visible line if there's a valid selection.
        Icon follows the viewport when scrolling through selection.
        Manages on_scroll subscription for selection tracking (separate from sync edit sessions).
        """
        self.load_gutter_icons(ed_self)
        
        handle = self.get_editor_handle(ed_self)
        
        # Get the best line to show icon (viewport-aware)
        icon_line = self.get_visible_selection_line(ed_self)
        
        if icon_line is not None:
            # Show icon at the calculated line
            self.show_gutter_icon(ed_self, icon_line)
            
            # Subscribe to on_scroll for this editor if NOT already in a sync edit session
            # This allows icon to follow viewport during scrolling
            if not self.has_session(ed_self):
                if handle not in self.selection_scroll_handles:
                    self.selection_scroll_handles.add(handle)
                    self._update_event_subscriptions()
        else:
            # No selection, hide icon if not in active sync edit mode
            if self.has_session(ed_self):
                session = self.get_session(ed_self)
                if not session.selected and not session.editing:
                    self.hide_gutter_icon(ed_self)
                    # Unsubscribe from selection scroll tracking
                    if handle in self.selection_scroll_handles:
                        self.selection_scroll_handles.remove(handle)
                        self._update_event_subscriptions()
            else:
                self.hide_gutter_icon(ed_self)
                # Unsubscribe from selection scroll tracking
                if handle in self.selection_scroll_handles:
                    self.selection_scroll_handles.remove(handle)
                    self._update_event_subscriptions()

    def toggle(self, ed_self=None):
        """
        Main Entry Point - can be called from command or gutter click.
        If already active, exits. Otherwise starts sync editing.
        """
        if ed_self is None:
            ed_self = ed
        session = self.get_session(ed_self)
        # If already in sync edit mode, exit
        if session.selected or session.editing:
            self.reset(ed_self)
            return

        # Otherwise, start sync editing
        self.start_sync_edit(ed_self, allow_timer=True)

    def get_visible_line_range(self, ed_self):
        """
        Calculate the range of visible lines in the editor viewport.
        Returns (first_visible_line, last_visible_line) tuple.
        """
        line_top = ed_self.get_prop(PROP_LINE_TOP)
        line_bottom = ed_self.get_prop(PROP_LINE_BOTTOM)
        return (line_top, line_bottom)

    def get_visible_selection_line(self, ed_self):
        """
        Returns the middle line of the visible viewport if there's a valid selection.
        The icon always appears in the center of the screen as long as there's a selection,
        even if the selection itself is not visible in the viewport.
        Returns None if no valid selection exists.
        """
        carets = ed_self.get_carets()
        if len(carets) != 1:
            return None
        
        x0, y0, x1, y1 = carets[0]
        # Check if we have a selection
        if y1 < 0 or (y0 == y1 and x0 == x1):
            return None
        
        # Get visible viewport range
        line_top = ed_self.get_prop(PROP_LINE_TOP)
        line_bottom = ed_self.get_prop(PROP_LINE_BOTTOM)
        
        # Always return middle line of viewport when there's a selection
        middle_line = (line_top + line_bottom) // 2
        
        return middle_line

    def on_lexer_parsed(self, ed_self):
        """
        Event handler for when lexer finishes parsing (>600ms case).
        
        IMPORTANT: This event fires ONCE per editor when that specific editor's lexer completes.
        The ed_self parameter tells us WHICH editor triggered the event.
        So if Editor A and Editor B are both waiting:
          - When Editor A finishes => on_lexer_parsed(ed_self=Editor_A) fires
          - When Editor B finishes => on_lexer_parsed(ed_self=Editor_B) fires
        Each call processes only the editor that triggered it (via ed_self).
        """
        handle = self.get_editor_handle(ed_self)
        
        # Only process if THIS SPECIFIC editor is waiting for lexer parsing notification
        # This check ensures we only handle events for editors we're tracking
        if handle not in self.pending_lexer_parse_handles:
            return
        
        # Remove THIS editor from pending set (cleanup for this specific editor only)
        self.pending_lexer_parse_handles.remove(handle)
        
        # Unsubscribe from on_lexer_parsed ONLY if no more editors are waiting. This keeps the event active for other editors still parsing
        # Use central event management instead of direct call to set_events_safely([]) to preserve other event from other editors like on_scroll/on_lexer_parsed
        self._update_event_subscriptions()
    
        # prevent the message box from showing multiple times for the same editor. The issue is that on_lexer_parsed can be called multiple times for the same editor if lexer parsing happens repeatedly.
        # dirty solution for late lexer parsing: to understand better why we need this check read the comment "dirty solution for late lexer parsing" in start_sync_edit. when a file is big and cudatext take time to finish parsing and user start sync edit the sync edit will exist because it did not find any tokens, when cudatext finishes parsing it will fire on_lexer_parsed then this function will show the message box alert below to the user telling him to restart the sync edit session. the problem is that when the user start the sync edit again, the plugin will subscribe again to on_lexer_parsed and cudatext have a strange behavior: even when the token parsing have already finished cudatext will still send on_lexer_parsed event when you edit the text of the file, this happens with big files, this means that the user will receive again the message box a second time even when in fact he does not really need to restart the sync edit session, this is why we prevent here to show the message more than one time. this is dirty but working with on_lexer_parsed and this strange behavior of cudatext with on_lexer_parsed does not allow a cleaner solution. but it works fine at least...
        if handle in self.lexer_parsed_message_shown:
            return
        # Mark this editor as having shown the message
        self.lexer_parsed_message_shown.add(handle)
        
        # i prefer to show a message alert than running the sync edit, because if the user is already editing inside sync edit and we refresh he will not understand what happened and we may break what he is writing, so a message alert is more safe and inform the user of the problem
        # msg_status(_("Sync Editing: Lexer parsed. Starting..."))
        # self.start_sync_edit(ed_self, allow_timer=False)
        
        # Show message to user (lexer took >600ms, so timer already fired with incomplete data)
        tab_title = ed_self.get_prop(PROP_TAB_TITLE)
        msg_box("Sync Editing:\n\n"
            f"CudaText lexer parsing has just completed for the tab titled: '{tab_title}'.\n"
            "This often occurs with large files, the initial analysis ran before all tokens were ready.\n\n"
            "Please restart the Sync Edit session for that tab to capture all duplicated identifiers correctly.\n\n"
            "To restart: click the gutter icon (or press Esc), then click the gutter icon again.",MB_OK + MB_ICONINFO)

    def lexer_timer(self, tag='', info=''):
        """600ms timer callback"""
        if not tag:
            return
        try:
            h_ed = int(tag)
        except ValueError:
            return

        if h_ed not in self.pending_delay_handles:
            return

        self.pending_delay_handles.remove(h_ed)

        ed = Editor(h_ed)
        if ed.get_prop(PROP_HANDLE_SELF): # Editor may have been closed in the meantime
            self.start_sync_edit(ed, allow_timer=False)

    def start_sync_edit(self, ed_self, allow_timer=False):
        """
        Starts sync editing session.
        1. Validates selection.
        2. Scans text (via Lexer or Regex).
        3. Groups identical words into dictionary.
        4. Filters singletons (words with < 2 occurrences).
        5. Builds spatial index for fast lookups. Builds spatial index (line_index) ONLY for valid words.
        6. Applies visual markers (colors) - ONLY FOR VISIBLE VIEWPORT PORTION.

        All configuration is read fresh from file/theme on every start so the user does not need to restart CudaText.
        """
        session = self.get_session(ed_self)
        
        # Clean up selection scroll tracking since we're starting a sync session
        handle = self.get_editor_handle(ed_self)
        if handle in self.selection_scroll_handles:
            self.selection_scroll_handles.remove(handle)
            self._update_event_subscriptions()
            
            
        # now that we created a session we should always call update_gutter_icon_on_selection before start_sync_edit to set gutter_icon_line (set by show_gutter_icon) which will be used in start_sync_edit to set the active gutter icon
        # Update gutter icon before starting to ensure session.gutter_icon_line is set. This allows us to flip it to "Active" mode shortly after.
        self.update_gutter_icon_on_selection(ed_self)
        
        # --- 1. Initial basic checks ---

        carets = ed_self.get_carets()
        if len(carets) != 1:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Need single caret'))
            return
        caret = carets[0]

        def restore_caret(caret, keep_selection=False):
            if keep_selection:
                # FAILURE CASE: Restore exact previous state (caret + selection)
                ed_self.set_caret(caret[0], caret[1], 
                    caret[2], caret[3], id=CARET_SET_ONE)
            else:
                # SUCCESS CASE: Keep caret position, but clear selection range (-1)
                ed_self.set_caret(caret[0], caret[1], -1, -1, id=CARET_SET_ONE)
                
        original = ed_self.get_text_sel()

        # Check if we have selection of text
        if not original:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Make selection first'))
            return

        # --- 2. Lexer vs Naive mode decision ---
        # Instantiate config to get fresh values from disk on every session
        ini_config = PluginConfig()
        
        cur_lexer = ed_self.get_prop(PROP_LEXER_FILE)
        
        # Force naive way if lexer is none or lexer is one of the text file types
        is_naive_lexer = not cur_lexer or cur_lexer in NAIVE_LEXERS
        session.use_simple_naive_mode = is_naive_lexer or ini_config.get_lexer_bool(cur_lexer, 'use_simple_naive_mode', USE_SIMPLE_NAIVE_MODE_DEFAULT)

        if not session.use_simple_naive_mode and allow_timer:
            # when we call start_sync_edit() on a big file that uses a lexer we may get wrong results if the user just opened it recently, because Cudatext takes some time to parse and set tokens (Id, comments, strings..etc), this means that start_sync_edit will get wrong results because it will have few token or none, because cudatext did not return all tokens with get_token(TOKEN_LIST_SUB ...), so to fix this problem we need to subscribe to on_lexer_parsed event, but this event become enabled only if the token parsing takes more than 600ms, so we need to also use a timer that stops after 600ms and starts start_sync_edit after 600ms. this  make the script more robust and safe.
            # so the plugin work like this: when user start a sync edit session, it cancel calling start_sync_edit here and start a timer of 600ms life and listen to on_lexer_parsed event and delete the created sync edit session, then when 600ms fires it calls start_sync_edit. this means also that if on_lexer_parsed fires later after 2s for example then it will call again start_sync_edit, so this means that the file may be checked twice, one because we clicked the gutter icon, and one if on_lexer_parsed fires later, we cannot prevent this double run unless the API removes the 600ms limit
            
            handle = ed_self.get_prop(PROP_HANDLE_SELF)

            # Cancel any previous delay timer for this exact editor (important if user clicks twice quickly)
            timer_proc(TIMER_STOP, self.lexer_timer, interval=0, tag=str(handle))
            self.pending_delay_handles.discard(handle) # Remove old entry if existed

            self.pending_delay_handles.add(handle)
            timer_proc(TIMER_START_ONE, self.lexer_timer, interval=600, tag=str(handle))
            
            # Track this editor for lexer parsing notification (fires only if parsing >600ms)
            # if we already showed the message box then we should not show it again so we do not need to subscribe to on_lexer_parsed in that case
            if handle not in self.lexer_parsed_message_shown:
                self.pending_lexer_parse_handles.add(handle)
            
            # Subscribe to on_lexer_parsed event for files that take longer than 600ms to parse
            # Use central event management to preserve on_scroll from other editors
            self._update_event_subscriptions()

            # Clean up visual elements but DON'T unsubscribe from the lexer events we just subscribed above
            self.reset(ed_self, cleanup_lexer_events=False)
            
            msg_status(_('Sync Editing: delay start for 600ms for safety'))
            return

        # --- 3. Start ---
        # now we are sure that CudaText lexer parsing finished so we can start the work safely
        
        # === PROFILING START: START_SYNC_EDIT ===
        if ENABLE_PROFILING_inside_start_sync_edit:
            pr_start, s_start = start_profiling()
        # ========================================

        msg_status(_('Sync Editing: Analyzing...'))
        t0 = time.perf_counter()
        t_prev = t0

        # --- 3.1. Selection Handling ---
        
        # Save coordinates and "Lock" the selection
        session.start_l, session.end_l = ed_self.get_sel_lines()
        session.selected = True
        start_l = session.start_l
        end_l = session.end_l
        
        # Break text selection and clear visual selection to show markers instead
        ed_self.set_sel_rect(0,0,0,0)

        # Update gutter icon to show active state
        if session.gutter_icon_line is not None:
            self.show_gutter_icon(ed_self, session.gutter_icon_line, active=True)

        # Mark the range properties for CudaText
        ed_self.set_prop(PROP_MARKED_RANGE, (start_l, end_l))
        
        # --- 3.2. Load Configuration ---

        # Determine if we use specific lexer rules
        # If it is non-standard lexer, change its behavior otherwise use the default
        local_styles_default = NON_STANDARD_LEXERS.get(cur_lexer, IDENTIFIER_STYLE_INCLUDE_DEFAULT)
        session.identifier_style_include = ini_config.get_lexer_str(cur_lexer, 'identifier_style_include', local_styles_default)

        # Load Lexer/Global Configs into session
        session.use_colors = ini_config.get_lexer_bool(cur_lexer, 'use_colors', USE_COLORS_DEFAULT)
        session.case_sensitive = ini_config.get_lexer_bool(cur_lexer, 'case_sensitive', CASE_SENSITIVE_DEFAULT)
        session.identifier_regex = ini_config.get_lexer_str(cur_lexer, 'identifier_regex', IDENTIFIER_REGEX_DEFAULT)
        session.identifier_style_exclude = ini_config.get_lexer_str(cur_lexer, 'identifier_style_exclude', IDENTIFIER_STYLE_EXCLUDE_DEFAULT)

        # Set colors based on theme 'Id' and 'SectionBG4' styles
        session.marker_fg_color = theme_color('Id', True)
        session.marker_bg_color = theme_color('SectionBG4', False)
        session.marker_border_color = session.marker_fg_color

        # Compile regex patterns with fallbacks
        try:
            session.regex_identifier = re.compile(session.identifier_regex)
        except Exception:
            msg_status(_('Sync Editing: Invalid identifier_regex config - using fallback'))
            print(_('ERROR: Sync Editing: Invalid identifier_regex config - using fallback'))
            session.regex_identifier = re.compile(IDENTIFIER_REGEX_DEFAULT)

        try:
            include_re = re.compile(session.identifier_style_include)
        except Exception:
            msg_status(_('Sync Editing: Invalid identifier_style_include config - using fallback'))
            print(_('ERROR: Sync Editing: Invalid identifier_style_include config - using fallback'))
            include_re = re.compile(local_styles_default)

        try:
            exclude_re = re.compile(session.identifier_style_exclude)
        except Exception:
            msg_status(_('Sync Editing: Invalid identifier_style_exclude config - using fallback'))
            print(_('ERROR: Sync Editing: Invalid identifier_style_exclude config - using fallback'))
            exclude_re = re.compile(IDENTIFIER_STYLE_EXCLUDE_DEFAULT)

        # --- 4. Build Dictionary ---

        # NOTE: Do not use app_idle (set_progress) before EDACTION_LEXER_SCAN.
        # App_idle runs message processing which can conflict with parsing actions.
        # so do not use set_progress here before ed.action(EDACTION_LEXER_SCAN. see bug: https://github.com/Alexey-T/CudaText/issues/6120 the bug happen only with this line. Alexey said: app_idle is the main reason, it is bad to insert it before some parsing action. Usually app_idle is needed after some action, to run the app message processing. Not before. Dont use it if not nessesary...
        # if SHOW_PROGRESS: self.set_progress(10)

        # Run lexer scan from start. Force a Lexer scan to ensure tokens are up to date
        # EDACTION_LEXER_SCAN seems not needed anymore see:https://github.com/Alexey-T/CudaText/issues/6124
        # ed_self.action(EDACTION_LEXER_SCAN, session.start_l) #API 1.0.289
        
        if ENABLE_BENCH_TIMER: 
            t_now = time.perf_counter()
            print(f"START_SYNC_EDIT 5% config: {t_now - t0:.4f}s ({t_now - t_prev:.4f}s)")
            t_prev = t_now
        if SHOW_PROGRESS: self.set_progress(30)

        # Coordinate Correction
        x1, y1, x2, y2 = caret
        if (y1, x1) > (y2, x2):
            x1, y1, x2, y2 = x2, y2, x1, y1 # Sort coords of caret
            
        # FIX #15 regression: when selection ends at the start of a new empty line, and we remove token outside of the selection (done bellow in "Drop tokens outside of selection") we lose the last line. the problem come from get_sel_lines() it does not return the last empty line, but get_carets() include the last empty line.
        # here we handle selection ending at the start of a new line.
        # If x2 is 0 and we have multiple lines, it means the selection visually ends at the previous line's end. We adjust x2, y2 to point to the "end" of the previous line so our filter later don't drop tokens on that line.
        if x2 == 0 and y2 > y1:
            y2 -= 1
            x2 = ed_self.get_line_len(y2)

        # Pre-compute case sensitivity handler
        key_normalizer = (lambda s: s) if session.case_sensitive else (lambda s: s.lower())

        # --- 4. Step A: Build Dictionary AND Line Index ---
        
        # Use defaultdict (fastest for list appending workload)
        session.dictionary = defaultdict(list)
        session.line_index = defaultdict(list)
        if session.use_simple_naive_mode:
            # === NAIVE MODE (Regex Only) ===
            # Naive Mode: Scan text purely by Regex, ignoring syntax context. This is generally faster as it bypasses ed.get_token
            
            for y in range(start_l, end_l+1):
                cur_line = ed_self.get_text_line(y)
                for match in session.regex_identifier.finditer(cur_line):
                    mstart, mend = match.span()
                    matchg = match.group()
                    # 1. Drop tokens outside of selection
                    if y == start_l and mstart < x1: continue
                    if y == end_l and mend > x2: continue
                    
                    key = key_normalizer(matchg)
                    token_ref = TokenRef(mstart, y, mend, y, matchg, 'id')
                    
                    # 2. Build dict and line_index
                    session.dictionary[key].append(token_ref)
                    session.line_index[y].append((token_ref, key))
        else:
            # === LEXER MODE (Syntax Aware) ===
            # Standard Lexer Mode: Filter tokens by Style (Variable, Function, etc.)
            
            # 1. Get Tokens in the selected range
            tokenlist = ed_self.get_token(TOKEN_LIST_SUB, start_l, end_l) or []
            
            if not tokenlist:
                # dirty solution for late lexer parsing: here we cannot simply call self.reset(ed_self) because it will remove on_lexer_parsed event, and this is not always wanted, for example if the user clicked the gutter icon to activate sync edit mode on a big file, cudatext will take some time to finish lexer token parsing so tokenlist will be empty because get_token(TOKEN_LIST_SUB returns nothing, in this case if we call self.reset(ed_self) we will reset on_lexer_parsed and when cudatext finishes its tokens parsing the user will not receive the alert to restart the sync edit session and he will think that the plugin is bugged or that his document have no duplicates. so here we must reset but use cleanup_lexer_events=False arg to prevent removing on_lexer_parsed. but this means also that on_lexer_parsed will keep active if the file is small because cudatext send on_lexer_parsed event only if parsing take more than 600ms, so in this case on_lexer_parsed will not be reset so when the user open another big file he will receive on_lexer_parsed, but this is not bad because in on_lexer_parsed() function we check if the editor is registred to receive on_lexer_parsed event, so it is safe to leave on_lexer_parsed active
                self.reset(ed_self, cleanup_lexer_events=False)
                # self.reset(ed_self)
                msg_status(_('Sync Editing: No syntax tokens found, or Lexer Parsing not finished yet...'))
                if SHOW_PROGRESS: self.set_progress(-1)
                # keep_selection=True because we are aborting
                restore_caret(caret, keep_selection=True)
                
                # === PROFILING STOP: START_SYNC_EDIT (Exit Early) ===
                if ENABLE_PROFILING_inside_start_sync_edit:
                    stop_profiling(pr_start, s_start, title='PROFILE: start_sync_edit (Entry Mode - Early Exit)')
                # ====================================================
                return

            if ENABLE_BENCH_TIMER: 
                t_now = time.perf_counter()
                print(f"START_SYNC_EDIT 30% get_token: {t_now - t0:.4f}s ({t_now - t_prev:.4f}s)")
                t_prev = t_now
            if SHOW_PROGRESS: self.set_progress(60)

            # Pre-build style checks once for all unique styles
            # Build a set of all unique style strings first, then batch-check them
            unique_styles = {t['style'] for t in tokenlist}
            # Batch validate all unique styles at once (much faster than per-token)
            style_valid = {
                style: bool(include_re.match(style) and not exclude_re.match(style))
                for style in unique_styles
            }
            
            # Process tokens with immediate TokenRef creation
            for token in tokenlist:
                # A. Drop tokens outside of selection
                if token['y1'] == start_l and token['x1'] < x1: continue
                if token['y2'] == end_l and token['x2'] > x2: continue
                
                # B. Check if a token's style matches the allowed patterns (IDs) and rejects Keywords (O(1) dict lookup)
                if not style_valid.get(token['style'], False):
                    continue
                
                # C. Add to dictionary AND line index in one pass
                key = key_normalizer(token['str'])
                token_ref = TokenRef(token['x1'], token['y1'], token['x2'], token['y2'], token['str'], token['style'])
                
                # Build dict and line_index.
                session.dictionary[key].append(token_ref)
                session.line_index[token['y1']].append((token_ref, key))
        
        if ENABLE_BENCH_TIMER: 
            t_now = time.perf_counter()
            print(f"START_SYNC_EDIT 60% Build dict+line: {t_now - t0:.4f}s ({t_now - t_prev:.4f}s)")
            t_prev = t_now
        if SHOW_PROGRESS: self.set_progress(70)
        
        # --- 4 Step B: Remove Singletons (Clean garbage) ---
        
        # Filter dictionary and line_index simultaneously to avoid rebuilding line_index
        keys_to_remove = [k for k, v in session.dictionary.items() if len(v) < 2]
        for key in keys_to_remove:
            # Get tokens before deleting
            singleton_tokens = session.dictionary[key]
            del session.dictionary[key]
            
            # Remove from line_index (mark for removal)
            for token_ref in singleton_tokens:
                line_num = token_ref.start_y
                if line_num in session.line_index:
                    # Filter out this specific token_ref
                    session.line_index[line_num] = [
                        (ref, k) for ref, k in session.line_index[line_num] 
                        if ref is not token_ref
                    ]
                    # Clean up empty line entries
                    if not session.line_index[line_num]:
                        del session.line_index[line_num]
        
        if ENABLE_BENCH_TIMER: 
            t_now = time.perf_counter()
            print(f"START_SYNC_EDIT 70% remove dup: {t_now - t0:.4f}s ({t_now - t_prev:.4f}s)")
            t_prev = t_now
        if SHOW_PROGRESS: self.set_progress(85)
        
        # Validation: Ensure we actually found words to edit. Exit if no id's (eg: comments and etc)
        if not session.dictionary:
            self.reset(ed_self)
            msg_status(_('Sync Editing: No editable identifiers found in selection'))
            if SHOW_PROGRESS: self.set_progress(-1)
            # keep_selection=True because we are aborting
            restore_caret(caret, keep_selection=True)
            
            # === PROFILING STOP: START_SYNC_EDIT (Exit Early) ===
            if ENABLE_PROFILING_inside_start_sync_edit:
                stop_profiling(pr_start, s_start, title='PROFILE: start_sync_edit (Entry Mode - Early Exit)')
            # ====================================================
            return

        # --- 5. Generate Color Map (once for entire session) ---
        
        # Pre-generate all colors to maintain consistency of colors when switching between View and Edit mode, so words will have the same color always inside the same session, and this reduce overhead also
        if session.use_colors:
            rdm = random.randint(0, 0xFFFFFF)
            session.word_colors = {
                key: ((((hash(key) + rdm) & 0xFF0000) >> 16) % 127 + 128) |
                     (((((hash(key) + rdm) & 0x00FF00) >> 8) % 127 + 128) << 8) |
                     ((((hash(key) + rdm) & 0x0000FF) % 127 + 128) << 16)
                for key in session.dictionary
            }
        else:
            session.word_colors = {}

        if ENABLE_BENCH_TIMER: 
            t_now = time.perf_counter()
            print(f"START_SYNC_EDIT 85% gen colors: {t_now - t0:.4f}s ({t_now - t_prev:.4f}s)")
            t_prev = t_now
        if SHOW_PROGRESS: self.set_progress(95)
        
        # --- 6. Apply Visual Markers (ONLY FOR VISIBLE VIEWPORT PORTION) ---
        
        # Visualize all editable identifiers in the selection. Mark all words that we can modify with pretty light color
        self.mark_all_words(ed_self)
        
        if ENABLE_BENCH_TIMER: 
            t_now = time.perf_counter()
            print(f"START_SYNC_EDIT 95% mark_all_words: {t_now - t0:.4f}s ({t_now - t_prev:.4f}s)")
            t_prev = t_now 
        if SHOW_PROGRESS: self.set_progress(-1)

        # Calculate summary statistics for the status bar message
        unique_duplicates_count = len(session.dictionary) 
        total_duplicates_count = sum(len(v) for v in session.dictionary.values())
        total_elapsed_time = time.perf_counter() - t0
        msg_summary = _(f'Sync Editing: Click ID to edit or Icon/Esc to exit. IDs={unique_duplicates_count}, Dups={total_duplicates_count}  ({total_elapsed_time:.3f}s)')
        msg_status(msg_summary)
        
        # Subscribe to on_scroll, on_caret, on_click, on_key, on_open_reopen events for this editor
        handle = self.get_editor_handle(ed_self)
        self.active_scroll_handles.add(handle)
        self.active_caret_handles.add(handle)
        self._update_event_subscriptions()
        
        # restore caret but w/o selection
        # keep_selection=False (default) because markers are now active
        restore_caret(caret, keep_selection=False)
        
        # === PROFILING STOP: START_SYNC_EDIT ===
        if ENABLE_PROFILING_inside_start_sync_edit:
            stop_profiling(pr_start, s_start, sort_key='cumulative', max_lines=200, title='PROFILE: start_sync_edit (Entry Mode)')
        # see wall-clock time (Python + native marker add + repaint)
        if ENABLE_BENCH_TIMER:
            print(f"START_SYNC_EDIT: {time.perf_counter() - t0:.4f}s")
            print(f"Sync Editing: IDs={unique_duplicates_count}, Total Dups={total_duplicates_count}")
        # =======================================

    def set_progress(self, prg):
        """Updates the CudaText status bar progress (fixes issue #46)."""
        app_proc(PROC_PROGRESSBAR, prg)
        app_idle()

    def mark_all_words(self, ed_self):
        """
        Visualizes all editable identifiers in the selection. ONLY IN THE VISIBLE VIEWPORT PORTION.
        Used during initialization and when returning to selection mode after an edit.
        Uses batch marker operations for better performance.
        """
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        session = self.get_session(ed_self)
        if not session.use_colors:
            return
        
        # Get visible line range
        line_top, line_bottom = self.get_visible_line_range(ed_self)
        
        # Collect all markers to add, sorted by (y, x)
        # Collect markers only for visible VIEWPORT lines
        markers_to_add = []
        
        for key in session.dictionary:
            # Get pre-generated color for this word or generate a new color for new words (edited words become new words after edits)
            color = session.word_colors.get(key)
            if color is None:
                color = generate_color(key)
                session.word_colors[key] = color
                            
            for token_ref in session.dictionary[key]:
                # OPTIMIZATION: Only add markers for the visible lines of the VIEWPORT
                if line_top <= token_ref.start_y <= line_bottom:
                    markers_to_add.append((
                        token_ref.start_y,  # y
                        token_ref.start_x,  # x
                        token_ref.end_x - token_ref.start_x,  # len
                        color
                    ))
        
        # Sort markers by (y, x) because this is what attr(MARKERS_ADD does internally, so we help it here to speed up things
        # this is very important for big files, a 9mb javascript file with 400k duplicates takes 14min, with this it takes only 22s see: https://github.com/CudaText-addons/cuda_sync_editing/issues/23
        markers_to_add.sort(key=lambda m: (m[0], m[1]))
        
        # Add all markers in sorted order
        for y, x, length, color in markers_to_add:
            ed_self.attr(MARKERS_ADD,
                tag=MARKER_CODE,
                x=x,
                y=y,
                len=length,
                color_font=0xb000000, # this color is better than marker_fg_color especially with black themes because we use colored background
                color_bg=color,
                color_border=0xb000000,
                border_down=1
            )

    def _cleanup_empty_word(self, ed_self, session, word_key):
        """
        Helper: Removes a word from dictionary and line_index if it was completely deleted (zero-length).
        Returns True if the word was removed, False otherwise.
        """
        if not word_key:
            return False
            
        tokens_list = session.dictionary.get(word_key)
        if not tokens_list:
            return False
        
        # Check if all tokens are zero-length (word was deleted)
        all_empty = all(token_ref.text == '' or token_ref.start_x == token_ref.end_x 
                        for token_ref in tokens_list)
        
        if all_empty:
            # Word was completely deleted - remove from dictionary
            affected_lines = set(token_ref.start_y for token_ref in tokens_list)
            del session.dictionary[word_key]
            
            # Remove from line_index
            for line_num in affected_lines:
                if line_num in session.line_index:
                    session.line_index[line_num] = [
                        (ref, key) for ref, key in session.line_index[line_num]
                        if key != word_key
                    ]
                    # Clean up empty line entries
                    if not session.line_index[line_num]:
                        del session.line_index[line_num]
            
            return True
        
        return False

    def finish_editing(self, ed_self, colorize=True):
        """
        Transitions the state from 'Editing' back to 'Selection/Viewing'.
        Crucial for Continuous Editing: It saves the current state and re-enables
        highlighting for other words without exiting the plugin.
        """
        session = self.get_session(ed_self)
        if not session.editing:
            return

        # Ensure the final edit is captured in dictionary
        if self.caret_in_current_token(ed_self):
            self.redraw(ed_self)

        # Remove the "Active Editing" markers (borders)
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)

        # Check if the word was deleted (empty/zero-length)
        # If so, remove it from dictionary and line_index
        self._cleanup_empty_word(ed_self, session, session.our_key)

        """
        SOLVE THE CARET POSITIONING PROBLEM:
        ================================================
        when the user switch from Edit Mode to Selection/View mode we need to reset multicarets to one caret, the caret must stay where the user expect it to be, stay where he moved it. whats seems to be simple becomes complex!
        
        Problem: When editing a word that appears N times, we have N carets. When user moves with arrow keys (up/down/left/right), ALL carets move together, and the carets list gets re-sorted by CudaText based on (y, x) position. When we reset multicaret to one caret we need to know which caret corresponds to the word the user originally clicked.
        
        Failed solutions (do not always work):
        ======================================
        
        method1:
        ========
        
        # Reset carets to the first caret (keep first caret position)
        carets = ed_self.get_carets()
        ed_self.set_caret(carets[0][0], carets[0][1], id=CARET_SET_ONE)
        
        Problem: caret jump to the top first ID which is not nice, when file is big the document scroll to the top

        method2:
        ========
        
        # use the position of the original word where the user clicked the first time
        ed_self.set_caret(session.original[0], session.original[1], id=CARET_SET_ONE)
        
        Problem: when user moves caret with arrow keys (up/down), the caret stay on the edited word not the upper/bellow line, the user will think cudatext is buged because his caret did not move
            
        method3:
        ========
        
        # Reset carets to single caret at the ORIGINAL position (where user first clicked)
        # Find the caret that corresponds to the line where the user started editing,
        # otherwise it defaults to carets[0] and jumps to the top of the file.
        carets = ed_self.get_carets()
        if carets:
            final_x, final_y = carets[0][0], carets[0][1] # Default to first caret            
            if session.original:
                orig_y = session.original[1]
                # Find the caret that is on the same line as the original click
                # if user used up/down keyboard keys to move the caret we have to check also one line above (orig_y+1) and one line bellow (orig_y-1) the current line to get the wanted caret
                for (cx, cy, _, _) in carets:
                    if cy == orig_y or cy == orig_y+1 or cy == orig_y-1:
                        final_x, final_y = cx, cy
                        break
            ed_self.set_caret(final_x, final_y, id=CARET_SET_ONE)
            
        Problem: this was working in all the cases until i found this bug:
        here we are inside Edit mode and caret '|' is at the end of the second ccc
          aaa  ccc
          ccc|  aaa
          aaa  ccc
        
        when we move the caret to the right, we exit Edit mode, so the above code reset the carets, but we get wrong caret positioning, the caret jumps to the start of the second line!
          aaa  ccc
        |  ccc  aaa
          aaa  ccc
        
        this is just one example, but in this text example i found a lot of bugs with all positioning up/down/left/right, specially when one of the carets find the end of line. so we need another solution!

        
        method4: Best
        =============
        
        Solution: Track the OCCURRENCE INDEX (which duplicate was clicked: 1st, 2nd, 3rd, etc).
        
        Example scenario:
        -----------------
          aaa  ccc    <- occurrence index 0 (first "ccc")
          ccc  aaa    <- occurrence index 1 (second "ccc") <- USER CLICKS HERE
          aaa  ccc    <- occurrence index 2 (third "ccc")
        
        When user clicks the middle "ccc":
        1. We save original_occurrence_index = 1
        2. We create 3 carets (one per "ccc"), sorted by position
        3. Caret at index 1 in the sorted list corresponds to the middle "ccc"
        
        When user presses UP arrow:
        - All 3 carets move up one line
        - The carets list is re-sorted by CudaText: [line0_ccc, line1_ccc, line2_ccc]
        - BUT: The caret that was at index 1 is STILL at index 1 in the sorted list
        - Why? Because sorting by (y,x) preserves the relative order of word occurrences
        
        When user presses RIGHT arrow (moving into the space after "ccc"):
        - All 3 carets move right (now in spaces, not in words)
        - Carets are still sorted by (y,x)
        - The caret at index 1 still corresponds to the middle occurrence
        - That caret now has the updated x position (in the space)
        
        Result: By using carets[original_occurrence_index], we get the caret that moved
        with the word the user originally clicked, with its current position after movements.
        
        How it is implemented:
        ----------------------
        In on_click, we find which occurrence index this clicked word is (e.g., 0 for first, 1 for second, 2 for third occurrence) and save it to original_occurrence_index. Then the rest of the code auto-refreshes this word's positioning while it is edited (via redraw()) and saves it to session.dictionary.
        Both session.dictionary[our_key] (list of TokenRef objects) and carets (list of caret tuples) are sorted by (y, x) position. This creates a stable 1-to-1 mapping: the token at index N in the dictionary always corresponds to the caret at index N in the carets list, even when the user moves with arrow keys.
        Therefore, in finish_editing, we use carets[original_occurrence_index] to get the caret that tracked the originally clicked word occurrence. This caret has moved with the user's arrow key presses and contains the correct final position where the single caret should land.
        """
        # Reset to single caret at the position corresponding to the originally clicked occurrence
        carets = ed_self.get_carets()
        if carets and session.original_occurrence_index is not None:
            # Carets are sorted by (y, x), same order as our dictionary tokens
            # The Nth occurrence corresponds to the Nth caret
            idx = session.original_occurrence_index
            
            # Safety check: Ensure the index is valid
            # Why might idx >= len(carets)?
            # ---------------------------------------------------------------
            # Normally, this should NEVER happen because:
            # - We create N carets for N occurrences when starting edit mode
            # - Arrow key movements preserve all carets
            # - on_caret is called BEFORE finish_editing, so carets still exist
            # 
            # However, this can happen in edge cases:
            # 1. User manually deleted some carets
            # 2. Some other plugin interfered with carets
            # 3. Unexpected CudaText behavior or bug
            # 4. The word was edited to become invalid (e.g., deleted completely) and some carets disappeared
            # 5. Carets were removed by CudaText when we use up/down key, but this should never happen now because we block this keys in on_key
            # 
            # In such cases, we fall back to carets[0] as a safe default rather than crashing.
            # This ensures the plugin remains stable even in unexpected scenarios.
            if idx < len(carets):
                # Normal case: Use the caret at the occurrence index
                # This caret has moved with the user's arrow key presses and is at the correct position
                final_x, final_y = carets[idx][0], carets[idx][1]
            else:
                # Fallback case: Use first caret (should rarely/never happen)
                # Better to land somewhere reasonable than to crash with IndexError
                final_x, final_y = carets[0][0], carets[0][1]

            
            # Set single caret at the determined position
            # This removes all other carets and places one caret at (final_x, final_y)
            # CARET_OPTION_NO_SCROLL gave bad result, when carets are removed it seems that cudatext remembers the first top caret and when i set the caret here cudatext scroll to the first caret even when the caret is on my wanted position which i set here, but because of CARET_OPTION_NO_SCROLL cudatext will not scroll to my wanted position, what a strange bahvior
            # ed_self.set_caret(final_x, final_y, id=CARET_SET_ONE, options=CARET_OPTION_NO_EVENT+CARET_OPTION_NO_SCROLL)
            ed_self.set_caret(final_x, final_y, id=CARET_SET_ONE, options=CARET_OPTION_NO_EVENT)

        # Reset flags to 'View/Selection' mode
        session.selected = True
        session.editing = False
        session.our_key = None
        session.original = None
        session.original_occurrence_index = None
        
        # Clear caret cache (will be rebuilt on next edit session)
        session.cached_carets_count = None
        session.cached_carets_lines = None

        # Re-paint markers so user can see what else to edit (ONLY VISIBLE VIEWPORT PORTION)
        if colorize:
            self.mark_all_words(ed_self)

    def caret_in_current_token(self, ed_self):
        """
        Helper: Checks if the primary caret is inside the word being edited.
        Handles 'Drift': Compensates for token positions shifting on the line 
        as previous instances grow/shrink during Sync Editing.
        """
        session = self.get_session(ed_self)
        if not session.our_key:
            return False

        carets = ed_self.get_carets()
        if not carets:
            return False
            
        idx = session.original_occurrence_index
        tokens_list = session.dictionary.get(session.our_key)
        
        # 1. Validation
        if idx is None or not tokens_list or idx >= len(carets) or idx >= len(tokens_list):
            return False

        # Get the specific Caret and TokenRef
        x0, y0 = carets[idx][0], carets[idx][1]
        token_ref = tokens_list[idx]
        
        # 2. Strict Line Check 
        # (If caret wrapped to next line, y0 changed, but token_ref is old -> False)
        # this should never happen now thanks to _validate_carets_integrity
        if y0 != token_ref.start_y:
            return False
        
        # 3. Special handling for empty/deleted words - PRECISE BOUNDARY CHECK
        if token_ref.text == '' or token_ref.start_x == token_ref.end_x:
            # Word is empty (deleted)
            # Allow caret at exact position or immediately after (start_x + 1)
            # Why +1? When deleting last char, caret can land at start OR start+1
            if token_ref.start_x <= x0 <= token_ref.start_x + 1:
                return True
            return False
            
        # 4. Find the ACTUAL start of the word under the caret
        # We cannot rely on token_ref.start_x because it is stale.
        # We must scan backwards from the caret to find where this word currently begins.
        line_text = ed_self.get_text_line(y0)
        
        # Helper logic similar to redraw(): Backtrack to find start of ID
        # Start scanning from caret position
        actual_start_x = x0
        
        # Safety: Don't go below 0. If we are at the end of the line or word, step back one char to catch the word
        if actual_start_x > 0 and (actual_start_x >= len(line_text) or not session.regex_identifier.match(line_text[actual_start_x:])):
            actual_start_x -= 1

        # Move back, but never below 0. as long as the regex matches the string starting at that position
        # This aligns with how standard regex identifiers work (greedy match from left)
        # Move back, but never below 0
        while actual_start_x > 0:
            # Check if a word starts here that extends to/past our caret roughly?
            # Simpler approach: verify char by char if it's part of an ID.
            # But since we use a regex config, we stick to the user's regex logic:
            if not session.regex_identifier.match(line_text[actual_start_x:]):
                break
            actual_start_x -= 1
        
        # If we're at position 0, check if there's a match there
        if actual_start_x == 0:
            if not session.regex_identifier.match(line_text[0:]):
                # No match at position 0 - PRECISE BOUNDARY CHECK for empty word case
                token_length = len(token_ref.text)
                if token_length == 0:
                    # Empty word - allow caret at start or start+1
                    if token_ref.start_x <= x0 <= token_ref.start_x + 1:
                        return True
                return False
        else:
            actual_start_x += 1
        
        if actual_start_x < 0:
            actual_start_x = 0

        # 5. Check if this is a valid word match
        match = session.regex_identifier.match(line_text[actual_start_x:])
        
        # SPECIAL CASE: If no match but caret is near the original token position
        if not match:
            token_length = len(token_ref.text)
            
            # Case A: Word is empty (deleted)
            if token_length == 0:
                if token_ref.start_x <= x0 <= token_ref.start_x + 1:
                    return True
                return False
            
            # Case B: Word exists but caret moved outside
            # Be strict - if regex didn't match, we're not inside
            return False
            
        current_word = match.group(0)
        current_word_len = len(current_word)
        
        # Verify caret is actually within this word bounds (or at immediate end)
        if not (actual_start_x <= x0 <= actual_start_x + current_word_len):
            return False

        # 6. DRIFT CORRECTION (The fix for "cccd cccd cccddd")
        # We need to ensure the word we found is actually the one we are tracking.
        # But its position has shifted. 
        # Drift = (Index of this token on this line) * (Change in length)
        
        # Calculate Delta (How much did the word grow/shrink?)
        # We compare current word on screen vs the original key we started editing
        # Use token.text (the TRUE current state) for drift calculation
        delta = current_word_len - len(token_ref.text)
        
        # Count how many instances of 'our_key' are strictly BEFORE this one on the same line.
        # This tells us how many times 'delta' was applied before reaching us.
        tokens_on_line_before = 0
        for t in tokens_list:
            if t.start_y == y0 and t.start_x < token_ref.start_x:
                tokens_on_line_before += 1
                
        calculated_drift = tokens_on_line_before * delta
        expected_start_x = token_ref.start_x + calculated_drift
        
        # 7. Final Identity Verification
        # Does the word we found match the expected position of our tracked token?
        if actual_start_x == expected_start_x:
            return True
            
        return False

    def reset(self, ed_self=None, cleanup_lexer_events=True):
        """
        FULLY Exits the plugin.
        Clears markers, releases selection lock, and resets all state variables.
        Triggered via 'Toggle' command, gutter icon click, or 'ESC' key.
        
        Args:
            - ed_self: Editor instance
            - cleanup_lexer_events: If True, clean up lexer event subscriptions. If False, keep lexer events active (used during lexer parsing delay where we need events to persist).
            NOTE: on_scroll, on_caret, on_click, on_key and on_open_reopen events are ALWAYS cleaned up regardless.
        """
        if ed_self is None:
            ed_self = ed
        session = self.get_session(ed_self)
        handle = self.get_editor_handle(ed_self)
        
        # ALWAYS clean up on_scroll, on_caret, on_click, on_key and on_open_reopen tracking (session is ending)
        self.active_scroll_handles.discard(handle)
        self.active_caret_handles.discard(handle)
        
        # Also clean up selection scroll tracking
        self.selection_scroll_handles.discard(handle)
        
        # Clean up lexer parsing tracking only if requested
        if cleanup_lexer_events:
            # Clean up any pending lexer timers
            timer_proc(TIMER_STOP, self.lexer_timer, interval=0, tag=str(handle))
            self.pending_delay_handles.discard(handle)
            
            self.pending_lexer_parse_handles.discard(handle)

            # Clear message tracking when fully resetting
            # after a lot of thinking and attempts to solve the problem of cudatext sending on_lexer_parsed events even when the token parsing already finished, i decided to never delete the handles of the editors that already showed the message box here, the set is small anyway so it does not matter if we keep it, we discard the handle from lexer_parsed_message_shown only in on_open_reopen so the message box will appear again only if the user reloads the file; so never reset this here
            # self.lexer_parsed_message_shown.discard(handle)

        # Update event subscriptions
        # here we unsubscribe from on_lexer_parsed if no more editors are waiting, on_scroll if no other editor need it, on_caret if no other editor needs it, and on_click/on_key/on_open_reopen if no active sessions
        self._update_event_subscriptions()
    
        # Restore original position if needed
        if session.original:
            ed_self.set_caret(session.original[0], session.original[1], id=CARET_SET_ONE)

        # Clear all markers
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        ed_self.set_prop(PROP_MARKED_RANGE, (-1, -1))
        if SHOW_PROGRESS: self.set_progress(-1)

        # Hide gutter icon
        self.hide_gutter_icon(ed_self)

        # Remove the session
        self.remove_session(ed_self)

        msg_status(_('Sync Editing: Deactivated'))

    def doclick(self, ed_self=None):
        """command 'Emulate mouse click' for people who don't like mouse."""
        if ed_self is None:
            ed_self = ed
        # state = app_proc(PROC_GET_KEYSTATE, '')
        state = ''
        return self.on_click(ed_self, state)

    def on_click(self, ed_self, _state):
        """
        Handles mouse clicks to toggle between 'Viewing/Selection' and 'Editing' Modes.
        Logic:
        1. If Editing -> Check if click is on valid ID.
           - Yes: Do nothing (Do not exit). Switch from ID to ID
           - No: Finish current edit (Loop back to Viewing).
        2. If Viewing -> Check if click is on valid ID.
           - Yes: Start Editing (Add carets to ALL duplicates, and add color and borders to VISIBLE VIEWPORT PORTION only).
           - No: Do nothing (Do not exit).
        Uses spatial index for faster word lookups.
        """
        # exit early if sync edit mode is not active
        if not self.has_session(ed_self):
            return

        session = self.get_session(ed_self)
        if not session.selected and not session.editing:
            return

        carets = ed_self.get_carets()
        if not carets:
            return

        caret = carets[0]
        clicked_x, clicked_y = caret[0], caret[1]

        # Find which word was clicked (fast O(1) lookup via line_index)
        clicked_key = None
        offset = 0
        if clicked_y in session.line_index:
            for token_ref, key in session.line_index[clicked_y]:
                if clicked_x >= token_ref.start_x and clicked_x <= token_ref.end_x:
                    clicked_key = key
                    offset = clicked_x - token_ref.start_x
                    break

        # === PROFILING START: BENCHMARKING ID-to-ID SWITCH ===
        is_switch = session.editing and clicked_key is not None
        if is_switch:
            # print(">>> ID-to-ID SWITCH START <<<")
            if ENABLE_PROFILING_inside_on_click:
                pr_switch, s_switch = start_profiling()
            if ENABLE_BENCH_TIMER:
                switch_start = time.perf_counter()
        # =====================================================

        # Did we click on a valid ID?
        
        if not clicked_key:
            # click was NOT on a valid ID
            
            if session.editing:
                # User clicked outside while he was in editing mode => finish editing mode and show colors (return to selection/view mode)
                self.finish_editing(ed_self)
            else: 
                # User clicked outside while he was in View/Selection mode
                msg_status(_("Sync Editing: Not an ID! Click on ID to edit it. Writing outside ID's is not supported"))
            return

        # At this point we know that we have clicked a valid ID (clicked_key) => we are either:
        #   1. Starting editing from selection mode, or
        #   2. Switching directly from one ID to another ID

        # ID to ID switch preparation: clean up previous word if it was deleted, then clear markers + reset carets, NO colorization with mark_all_words()
        if session.editing:
            # we clicked a valid ID, it may be a new one or the same edited one
            
            # Clean up the previous word if it was completely deleted
            old_key = session.our_key
            self._cleanup_empty_word(ed_self, session, old_key)
            
            # Clear markers and reset carets
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
            # Reset to single caret at the clicked position (keeps caret where user clicked)
            ed_self.set_caret(clicked_x, clicked_y, id=CARET_SET_ONE)
            
            # === PROFILING ===
            if is_switch:
                if ENABLE_BENCH_TIMER:
                    print(f">>> Switch phase 1 (finish old): {time.perf_counter() - switch_start:.4f}s")
            # =================
        else:
            # we cliked a valid ID and we were in View/Selection mode, so First time entering Editing mode => clear colored backgrounds
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)

        # --- Start Editing Mode ---

        # Get visible line range
        line_top, line_bottom = self.get_visible_line_range(ed_self)

        # Collect all markers to add, sorted by (y, x)
        # Collect markers only for visible lines
        # we collect ALL instances for caret placement, but we colorize only the visible lines
        all_carets = []
        markers_to_add = []
        
        for token_ref in session.dictionary[clicked_key]:
            # Add caret to ALL instances (editing must work on all words)
            all_carets.append((
                token_ref.start_y,  # y
                token_ref.start_x,  # x
                offset  # store offset for caret placement
            ))
            
            # But only add markers for VISIBLE instances (rendering optimization)
            if line_top <= token_ref.start_y <= line_bottom:
                markers_to_add.append((
                    token_ref.start_y,  # y
                    token_ref.start_x,  # x
                    token_ref.end_x - token_ref.start_x,  # len
                ))
        
        # Sort both lists by (y, x), sorting is very important, read in redraw() why
        all_carets.sort(key=lambda c: (c[0], c[1]))
        markers_to_add.sort(key=lambda m: (m[0], m[1]))
            
        # Add active borders ONLY to visible VIEWPORT instances of the clicked word
        for y, x, length in markers_to_add:
            ed_self.attr(MARKERS_ADD, tag=MARKER_CODE,
                x=x, y=y,
                len=length,
                color_font=session.marker_fg_color,
                color_bg=session.marker_bg_color,
                color_border=session.marker_border_color,
                border_left=1,
                border_right=1,
                border_down=1,
                border_up=1
            )
        
        # Add secondary caret at the corresponding offset
        # Add carets to ALL instances (not just visible VIEWPORT ones)
        for y, x, off in all_carets:
            ed_self.set_caret(x + off, y, id=CARET_ADD, options=CARET_OPTION_NO_EVENT)

        # === PROFILING STOP: BENCHMARKING ID-to-ID SWITCH ===
        if is_switch:
            if ENABLE_PROFILING_inside_on_click:
                stop_profiling(pr_switch, s_switch, sort_key='cumulative', max_lines=200, title='PROFILE: ID-to-ID switch (on_click)')
            if ENABLE_BENCH_TIMER:
                phase2_time = time.perf_counter() - switch_start
                total_switch_time = time.perf_counter() - switch_start
                print(f">>> Switch phase 2 (setup new word): {phase2_time:.4f}s")
                print(f">>> ID-to-ID SWITCH TOTAL TIME: {total_switch_time:.4f}s")
        # ====================================================

        # Reset flags to 'Editing' mode, and Update state
        session.selected = False
        session.editing = True
        session.our_key = clicked_key
        session.original = (clicked_x, clicked_y)

        # Find which occurrence index this clicked word is (0-based).
        # Example: if "ccc" appears 3 times and user clicked the 2nd one, this will be index 1.
        # This index is CRITICAL because it maps directly to the caret position in the sorted carets list.
        # 
        # Why this works:
        # - session.dictionary[clicked_key] contains TokenRef objects sorted by (y, x) position
        # - When we create multiple carets (in on_click), we sort them by (y, x) 
        # - Therefore: dictionary index N corresponds to caret index N
        # - If user clicks occurrence #1 (middle "ccc" in example), we save index=1
        # - Later when user moves with arrows, ALL carets move together but maintain their relative order
        # - Caret at index 1 always corresponds to the word occurrence at index 1, even after movements
        # find more info about this in 'SOLVE THE CARET POSITIONING PROBLEM' in finish_editing()
        for i, token_ref in enumerate(session.dictionary[clicked_key]):
            if token_ref.start_y == clicked_y and token_ref.start_x <= clicked_x <= token_ref.end_x:
                session.original_occurrence_index = i
                break

        # Reset caret cache so integrity checks rebuild it for the new word
        session.cached_carets_count = None
        session.cached_carets_lines = None

    def on_click_gutter(self, ed_self, _state, nline, _nband):
        """
        Handles clicks on the gutter area.
        If user clicks on the sync edit icon, toggle the sync editing mode.
        """
        # Check if there's a decoration on this line with our tag
        decorations = ed_self.decor(DECOR_GET_ALL, nline)

        for decor in decorations:
            if decor['tag'] == DECOR_TAG:
                # User clicked on our sync edit icon
                session = self.get_session(ed_self)

                if session.selected or session.editing:
                    # If already in sync edit mode, exit
                    self.reset(ed_self)
                else:
                    # Otherwise, start sync editing
                    self.start_sync_edit(ed_self, allow_timer=True)
                return False  # Prevent default processing

    def on_caret_slow(self, ed_self):
        """
        Handles gutter icon visibility based on selection state.
        Only active when NOT in sync Edit Mode (lightweight).
        
        - Called when user make a selection or cancel the selection (when using keys=sel,selreset)
        - Called when caret stops moving (when not using keys=sel,selreset).
        
        - before api 1.0.470 this event is called for every slow caret
        - after api 1.0.470 and using keys=sel,selreset, this event is called only when the user makes a selection or cancel the selection, so the plugin now practically consumes no resources while it is not used
        """
        # Only handle gutter icon when NOT in active sync edit mode
        if not self.has_session(ed_self):
            self.update_gutter_icon_on_selection(ed_self)
            return

    def on_caret(self, ed_self):
    # on_caret_slow is better because it will consume less resources but it breaks the colors recalculations when user edit an ID, so stick with on_caret
        """
        Hooks into caret movement during active sync editing session.
        
        Continuous Edit Logic:
        If the user moves the caret OUTSIDE the active word, we 'finish' the edit, return to View/Selection mode, and show colors.
        
        Includes INTEGRITY CHECK to detect and exit if caret integrity is compromised (carets removed or moved to different lines).
        
        NOTE: This event is ONLY subscribed during active sync sessions (dynamically).
        
        Here we handle only carets events from keyboard movements left/right. up/down are blocked in on_key. carets events made by mouse clicks are handeled in on_click.
        Here we handle only Sync Edit Mode, Sync View/Selection Mode is handeled in on_click
        
        NOTE: on_caret events come always before on_click events, if Cudatext change this in the future the code should continue working fine
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        # This should never happen since we only subscribe to this event when the sync mode is active, but when sync edit is active in one tab then we must prevent this to run on other tabs if the user switch to another tab, otherwise we will create a session for every tab the user switch to it and of course will add overhead also, so we must keep this check
        handle = self.get_editor_handle(ed_self)
        if handle not in self.sessions: # inline has_session
            return

        # Now we know that this document have a session active, get the session and exit early if we are not in editing mode, view/selection mode should not be processed here (by on_caret) we do it in on_click
        session = self.sessions[handle]
        if not session.editing:
            return
       
        # Check if this is a Mouse Action (Left or Right Button is pressed), mouse clicks must be handeleted in view/selection mode, we check it here because on_caret events come always before on_click events, so we have to discard those false events here, we are interested about keyboard carets movements only
        pressed_keys = app_proc(PROC_GET_KEYSTATE, '')
        is_mouse_click = ('L' in pressed_keys) or ('R' in pressed_keys)                    
        if is_mouse_click:
            return

        # Check caret integrity FIRST: Detect if carets were lost or jumped to another line
        if not self._validate_carets_integrity(ed_self):
            msg_status(_("Carets removed or moved to different lines - exiting Sync Edit Mode"))
            # exit Edit mode
            self.finish_editing(ed_self, colorize=True)
            return
        
        # === PROFILING START: ON_CARET ===
        if ENABLE_PROFILING_inside_on_caret:
            pr_on_caret, s_on_caret = start_profiling()
        # =================================
        
        # Now we are in Editing mode, and caret moved with keyboard and carets are in a good state, lets check if caret is still inside the edited word
        if not self.caret_in_current_token(ed_self):
            # Caret left current token
            self.finish_editing(ed_self)            
        else:
            # caret moved, and it is still inside the word currently being edited
            # NOTE: self.redraw(ed_self) is called here to update word markers live during typing. This recalculates borders and shifts other tokens on the line as the word grows/shrinks. This is a performance hit on simple caret moves (arrow keys) but necessary for live updates.
            self.redraw(ed_self)
            
        # === PROFILING STOP: ON_CARET (Exit Editing) ===
        if ENABLE_PROFILING_inside_on_caret:
            stop_profiling(pr_on_caret, s_on_caret, sort_key='cumulative', title='PROFILE: on_caret (Exit Editing)')
        # ===============================================

    def _validate_carets_integrity(self, ed_self):
        """
        Validates that carets are still in a valid state for sync editing.
        Returns True if carets are valid, False if they've been corrupted.
        
        Uses the Dictionary as the Source of Truth.

        INTEGRITY CHECK: Detects:
        1. Number of carets changed (some were removed by CudaText)
        2. Carets moved to different lines (vertical movement or line wrap)
        
        Detect if carets were lost (hit EOF/Start, this can never happen now because we block up/down key) or carets jumped to another line (Left/Right keys at EOL)
        We validate that the physical carets match our internal token records.
        
        Why? when the user press left or right keyboard keys, if the carets are at the end of line and the user press left/right keys the carets at the end of line will jump to the next line while carets in the middle of line will continue inside the edited words, so this breaks editing words, so when this happens we have to exit sync edit mode and return to view/selection mode, so we have to make a cache of carets, and every time there is a carets movements we check the y position of all the current carets to the cached one and if one of them changed or the total of carets is diferent then we stop sync edit mode
        """
        
        session = self.get_session(ed_self)
        if not session.editing or not session.our_key:
            return True
        
        # Build cache on first call only
        # After this, we never touch the dictionary again during this edit session
        if session.cached_carets_lines is None:
            # Source of Truth: The tokens we are currently editing
            tokens = session.dictionary.get(session.our_key, [])
            if not tokens:
                return False
            
            # Cache both count and line positions
            session.cached_carets_count = len(tokens)
            session.cached_carets_lines = [token.start_y for token in tokens]
        
        current_carets = ed_self.get_carets()
        if not current_carets:
            return False
        
        # Check 1: Count Check. If number of carets changed (e.g. hit EOF), we lost sync. Catches cases where CudaText removed carets at file boundaries
        if len(current_carets) != session.cached_carets_count:
            return False
        
        # Check 2: Y-Position Check: Carets must stay on the same line as their token.
        # If a caret moves to a different line (Left/Right at EOL), cy will change, ensuring this check fails so we exit edit mode.
        # - Up/Down arrow or Enter keys that moved carets to different lines (must never happen now because we block those keys in on_key)
        # - Left/Right at EOL that wrapped carets to next line
        # - Any vertical movement that breaks sync
        for i, (cx, cy, _, _) in enumerate(current_carets):
            # We assume tokens are sorted (y,x) and get_carets returns sorted (y,x).
            if cy != session.cached_carets_lines[i]:
                return False
                
        return True

    def on_scroll(self, ed_self):
        """
        Called when user scrolls the editor viewport.
        
        Handles two separate cases:
        1. Selection mode (NOT in sync edit): Updates gutter icon position to middle of viewport
        2. Sync edit mode: Updates markers to show only visible portions (debounced)
          Updates markers to show only visible portions. This enables smooth scrolling with large files.
          Uses a timer to debounce scroll events - only updates markers when scrolling stops (reduces CPU usage and makes scroll smooth).
        """
        handle = self.get_editor_handle(ed_self)
        
        # Case 1: Editor has selection but NO active session - update icon position immediately
        if handle in self.selection_scroll_handles:
            self.update_gutter_icon_on_selection(ed_self)
            return
        
        # Case 2: Editor has active sync edit session - handle marker updates
        if not self.has_session(ed_self):
            return
        
        session = self.get_session(ed_self)
        # Only update if we're in active mode
        if not (session.selected or session.editing):
            return
      
        # Stop any existing scroll timer for this editor
        timer_proc(TIMER_STOP, self._on_scroll_timer_finished, interval=0, tag=str(handle))
        
        # Start a new timer that will fire when scrolling stops (150ms delay)
        timer_proc(TIMER_START_ONE, self._on_scroll_timer_finished, interval=SCROLL_DEBOUNCE_DELAY, tag=str(handle))

    def _on_scroll_timer_finished(self, tag='', info=''):
        """
        Called when the scroll timer finishes (scrolling has stopped (debounced)).
        Updates markers for the new visible VIEWPORT portion.
        Also updates gutter icon position to keep it in the middle of viewport.
        """
        if not tag:
            return
        
        # Get editor from handle
        editor_handle = int(tag)
        ed_self = Editor(editor_handle)
        
        # Check if this editor still has an active session
        if not self.has_session(ed_self):
            return
        
        session = self.get_session(ed_self)
        
        # Update gutter icon position to middle of viewport (keep it always visible)
        if session.gutter_icon_active:
            line_top = ed_self.get_prop(PROP_LINE_TOP)
            line_bottom = ed_self.get_prop(PROP_LINE_BOTTOM)
            middle_line = (line_top + line_bottom) // 2
            self.show_gutter_icon(ed_self, middle_line, active=True)
        
        if session.editing:
            # In editing mode: redraw with borders markers for current active word
            self._update_edit_markers(ed_self)
            # self.redraw(ed_self)
        elif session.selected:
            # In selection/view mode: update colored background markers
            self.mark_all_words(ed_self)

    def _update_edit_markers(self, ed_self):
        """
        Updates border markers for the currently edited word
        ONLY IN THE VISIBLE VIEWPORT PORTION.
        
        This function is a pure "Painter". It assumes the internal dictionary positions are already correct (because the user hasn't typed, only scrolled).
        It simply looks at the existing data and draws the borders for the lines that are now visible.
        
        If we use redraw() inside on_scroll, we would be forcing the plugin to re-verify the word under the caret and attempt to update data structures every time the scroll timer fires. (High overhead)
        The redraw function is designed to handle text changes (typing). so we should not use it for on_scroll event because there is absolutely no need to run Regex, calculate deltas, or modify the internal dictionary coordinates.
        """
        session = self.get_session(ed_self)
        if not session.our_key:
            return
        
        # Clear existing markers
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        
        # Get visible line range
        line_top, line_bottom = self.get_visible_line_range(ed_self)
        
        # Collect all markers to add, sorted by (y, x)
        # Collect markers only for visible lines
        markers_to_add = []
        for token_ref in session.dictionary[session.our_key]:
            # OPTIMIZATION: Only add markers for the visible lines of the VIEWPORT
            if line_top <= token_ref.start_y <= line_bottom:
                markers_to_add.append((
                    token_ref.start_y,  # y
                    token_ref.start_x,  # x
                    token_ref.end_x - token_ref.start_x  # len
                ))
        
        # Sort markers by (y, x)
        markers_to_add.sort(key=lambda m: (m[0], m[1]))
        
        # Draw active borders for the currently edited word
        for y, x, length in markers_to_add:
            ed_self.attr(MARKERS_ADD, tag=MARKER_CODE,
                x=x, y=y,
                len=length,
                color_font=session.marker_fg_color,
                color_bg=session.marker_bg_color,
                color_border=session.marker_border_color,
                border_left=1,
                border_right=1,
                border_down=1,
                border_up=1
            )

    def on_key(self, ed_self, key, _state):
        """
        Handles Esc Keyboard input to cancel sync editing, and blocks problematic keys.
        1. VK_ESCAPE: Cancel sync editing completely.
        2. VK_UP/DOWN/ENTER: BLock them to avoid caret desync / line breaking.
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        if not self.has_session(ed_self):
            return

        if key == VK_ESCAPE:
            self.reset(ed_self)
            return False
            
        session = self.get_session(ed_self)
        # Only check problematic keys during editing mode
        if not session.editing:
            return

        # sometimes when i move the carets with the keyboard up and down key some carets are removed when they found no place where to land (start of file or end of file or an empty line with no place where to put multiple carets ), this problem breaks a lot of things in the plugin, for example original_occurrence_index will point to the wrong index because the total number of carets changed so for example if there was 10 carets, after the caret movement they become 5 carets, so ed_self.get_carets() will return 5 carets, but original_occurrence_index is still pointing to the one of the old 10 carets. to fix this we have to disable up and down keys
        # enter key is also problematic and would create multi-line editing chaos, so we have to disable it too
        
        # Keys that can break caret integrity
        problematic_keys = {
            VK_UP,      # may remove carets at top of file
            VK_DOWN,    # may remove carets at bottom of file  
            VK_ENTER,   # Would create multi-line editing chaos because it breaks the line (changing Y), which invalidates our dictionary Y-coords.
        }
        
        if key in problematic_keys:
            # met1: Exit editing mode and return to selection/view mode
            # self.finish_editing(ed_self)
            # return True
            
            # met2: Prevent the key from being processed
            # Allow the user to continue editing and print a status bar message 
            msg_status(_("Up/Down/Enter keys are not allowed inside Edit Mode"))
            return False

    def redraw(self, ed_self):
        """
        Dynamically updates markers and dictionary positions during typing.
        Because editing changes the length of the word, we must:
        1. Find the new word string at the caret position.
        2. Update the dictionary entry for the currently edited word (start/end positions).
        3. Shift positions of ALL other words that exist on the same line after the caret.
        4. Re-draw borders ONLY FOR VISIBLE VIEWPORT PORTION.
        
        HEAVILY OPTIMIZED
        - Delta-based position updates (only shift what changed)
        - Line-based spatial index for O(1) lookups
        - In-place TokenRef updates (no tuple recreation)
        - Early exit when nothing changed
        - O(k) complexity where k = tokens per line
        - Only renders VIEWPORT visible markers
        """
        # === PROFILING START: REDRAW ===
        # Measures the time taken for recalculating positions on a keypress.
        if ENABLE_PROFILING_inside_redraw:
            pr_redraw, s_redraw = start_profiling()
        if ENABLE_BENCH_TIMER:
            t0 = time.perf_counter()
        # ===============================

        session = self.get_session(ed_self)
        if not session.our_key:
            # === PROFILING STOP: REDRAW (Exit Early 1) ===
            if ENABLE_PROFILING_inside_redraw:
                stop_profiling(pr_redraw, s_redraw, sort_key='cumulative', title='PROFILE: redraw (Live Typing - Exit Early 1)')
            # ===========================================
            return

        # 1. Capture State. Find out what changed on the first caret (on others changes will be the same)
        old_key = session.our_key
        session.our_key = None # Temporarily unset to allow clean lookup

        # Get current state at the first caret
        carets = ed_self.get_carets()
        if not carets: return
        first_y = carets[0][1]
        first_x = carets[0][0]
        first_y_line = ed_self.get_text_line(first_y)
        
        # Find start of the word under caret
        # Clamp caret position first
        if first_x < 0:
            first_x = 0
        if first_x > len(first_y_line):
            first_x = len(first_y_line)
        
        start_pos = first_x

        # Backtrack from caret to find start of the new word
        # Workaround for end of id case: If caret is at the very end, move back 1 to capture the match
        # Safety check: only move back if we're not at position 0
        if start_pos > 0:
            if start_pos >= len(first_y_line) or not session.regex_identifier.match(first_y_line[start_pos:]):
                start_pos -= 1

        # Move start_pos back until we find the beginning of the identifier
        # Move back as long as the regex matches, but never below 0
        while start_pos > 0 and session.regex_identifier.match(first_y_line[start_pos:]):
            start_pos -= 1
        
        # Only increment if we actually moved back AND we're not at a match position
        # Check if we're at position 0 and there's a match
        if start_pos == 0:
            if not session.regex_identifier.match(first_y_line[0:]):
                # No match at 0, word might be deleted
                pass
            # If there IS a match at 0, start_pos stays at 0
        else:
            # We moved back, so increment to get to start of word
            start_pos += 1
        
        # Final safety clamp
        # Workaround for EOL #65. Safety for EOL/BOL cases
        if start_pos < 0:
            start_pos = 0

        # Check if word became empty (deleted) or invalid. Workaround for empty id (eg. when it was deleted) #62
        match = session.regex_identifier.match(first_y_line[start_pos:])
        
        if not match:
            # Word was deleted completely
            new_key = ''
            new_length = 0
        else:
            new_key = match.group(0)
            if not session.case_sensitive:
                new_key = new_key.lower()
            new_length = len(new_key)

        # 2. Get tokens first (we need them to calculate old_length correctly!)
        edited_tokens = session.dictionary.get(old_key, [])
        if not edited_tokens:
            return

        # CRITICAL FIX: Get old_length from the actual token, not from the key!
        # The token.text reflects the TRUE current state (can be empty "")
        # This fixes the delta calculation when transitioning from empty to letter (when user delete all the word and write again)
        old_length = len(edited_tokens[0].text) if edited_tokens else 0
        
        # Calculate Length Delta change
        delta = new_length - old_length

        # Early exit if nothing changed
        if delta == 0 and old_key == new_key:
            session.our_key = old_key
            # === PROFILING STOP: REDRAW (No Change) ===
            if ENABLE_PROFILING_inside_redraw:
                stop_profiling(pr_redraw, s_redraw, title='PROFILE: redraw (No Change)')
            # if ENABLE_BENCH_TIMER:
                # print(f"REDRAW (NO CHANGE): {time.perf_counter() - t0:.4f}s")
            # ==========================================
            return

        # Identify lines affected by this edit (where this word appears)
        affected_lines = set()
        for token_ref in edited_tokens:
            affected_lines.add(token_ref.start_y)  # y coordinate

        # 3. Rebuild Dictionary positions for the modified Active Word with new values (Delta shifting)
        # Delta-based updates: For each edited token instance, apply delta and shift other tokens on same line
        for token_ref in edited_tokens:
            line_num = token_ref.start_y
            old_token_x = token_ref.start_x
            
            if new_length == 0:
                # Word deleted - keep position but zero length
                token_ref.end_x = token_ref.start_x
                token_ref.text = ''
            else:
                # Find new word position (may have shifted due to earlier edits on same line)
                y_line = ed_self.get_text_line(line_num)
                
                # Scan backwards to find start of the new word instance from the adjusted position
                # here we search for the token starting from its old position
                search_x = old_token_x
                
                # Only backtrack if we're not at position 0. Search backward from old position, but never below 0
                while search_x > 0 and session.regex_identifier.match(y_line[search_x:]):
                    search_x -= 1
                
                # Increment only if we moved back
                # Check if we're at position 0 with a match
                if search_x == 0:
                    if not session.regex_identifier.match(y_line[0:]):
                        # No match at 0, keep old position (shouldn't happen)
                        search_x = old_token_x
                    # Else: match at 0, search_x stays 0
                else:
                    # We moved back, increment to start of word
                    search_x += 1
                
                # Safety clamp
                # Workaround for EOL #65
                if search_x < 0:
                    search_x = 0
                
                # Update this token's position in-place
                token_ref.start_x = search_x
                token_ref.end_x = search_x + new_length
                token_ref.text = new_key
            
            # Shift other tokens on this line that come AFTER this token
            # Only process tokens on the same line (using spatial index)
            # CRITICAL: Use >= old_token_x + old_length to handle x=0 case (user delete a word at position x=0)
            if delta != 0 and line_num in session.line_index:
                for other_ref, other_key in session.line_index[line_num]:
                    # Skip the token we just updated
                    if other_ref is token_ref:
                        continue
                    # Only shift tokens that come AFTER the end of the old word
                    # If other token comes after this one, shift it
                    # Use >= instead of > to handle x=0 case correctly
                    if other_ref.start_x >= old_token_x + old_length:
                        other_ref.shift(delta)

        '''
        # 4. met1: Update dictionary keys if word changed, and also handle collisions (when we edit a word and create a new word that already existed before, we merge both and consider them as one token so it become colorized with the same color), this seems the best thing but after more thinking i found it a bad idea, so i will use met2, see bellow. i keep code here to know/remember about this collision problem and why i took this decision because it is not obvious
        if old_key != new_key:
            # Handle collision if new_key already exists
            if new_key in session.dictionary:
                # Merge current tokens into existing list
                session.dictionary[new_key].extend(edited_tokens)
            else:
                # Create new entry
                session.dictionary[new_key] = edited_tokens

            del session.dictionary[old_key]
            
            # Update line index references (key string update)
            for line_num in affected_lines:
                if line_num in session.line_index:
                    new_line_list = []
                    for ref, k in session.line_index[line_num]:
                         # Check identity to update only the modified tokens
                         if ref in edited_tokens:
                             new_line_list.append((ref, new_key))
                         else:
                             new_line_list.append((ref, k))
                    session.line_index[line_num] = new_line_list
            
            session.our_key = new_key
        else:
            session.our_key = old_key
        '''
        
        # 4. met2: Update dictionary keys if word changed, and do not handle collisions (when we edit a word and create a new word that already existed before we simply disable the old one, then user will have to restart sync edit session to include the diabled word), i found this safer because we should not fix user bugs, if user do not pay attention that he created a new word that already exist then why should we fix it for him? this is not a bug fixer plugin! so here we will simply consider the old word (which is similar to the new word) as a dead word so we do not colorize/edit it, this is safer.
        # Update dictionary keys if word changed (and is not empty)
        if new_key != '' and old_key != new_key:
            # Word changed to a different non-empty word
            session.dictionary[new_key] = edited_tokens
            del session.dictionary[old_key]
            
            # Update line index references
            for line_num in affected_lines:
                if line_num in session.line_index:
                    session.line_index[line_num] = [
                        (ref, new_key if key == old_key else key) 
                        for ref, key in session.line_index[line_num]
                    ]
            
            session.our_key = new_key
        else:
            # Word is empty or unchanged - keep using old_key
            # Even if text is empty, we keep the dictionary entry under old_key
            session.our_key = old_key
            
            
        # 5. Repaint borders ONLY FOR VISIBLE VIEWPORT PORTION
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)

        if new_length == 0:
            # === PROFILING STOP: REDRAW ===
            if ENABLE_PROFILING_inside_redraw:
                stop_profiling(pr_redraw, s_redraw, sort_key='time', title='PROFILE: redraw (Empty)')
            if ENABLE_BENCH_TIMER:
                print(f"REDRAW (EMPTY): {time.perf_counter() - t0:.4f}s")
            # ==============================
            return

        # Get visible line range
        line_top, line_bottom = self.get_visible_line_range(ed_self)

        # Collect all markers to add, sorted by (y, x)
        # Collect markers only for visible lines
        markers_to_add = []
        for token_ref in edited_tokens:
            # OPTIMIZATION: Only add markers for the visible lines of the VIEWPORT
            if line_top <= token_ref.start_y <= line_bottom:
                markers_to_add.append((
                    token_ref.start_y,  # y
                    token_ref.start_x,  # x
                    token_ref.end_x - token_ref.start_x  # len
                ))
        
        # Sort markers by (y, x)
        markers_to_add.sort(key=lambda m: (m[0], m[1]))
        
        # Draw active borders for the currently edited word (visible VIEWPORT portion only)
        for y, x, length in markers_to_add:
            ed_self.attr(MARKERS_ADD, tag=MARKER_CODE,
                x=x, y=y,
                len=length,
                color_font=session.marker_fg_color,
                color_bg=session.marker_bg_color,
                color_border=session.marker_border_color,
                border_left=1,
                border_right=1,
                border_down=1,
                border_up=1
            )

        # === PROFILING STOP: REDRAW ===
        if ENABLE_PROFILING_inside_redraw:
            stop_profiling(pr_redraw, s_redraw, sort_key='time', max_lines=200, title='PROFILE: redraw (Live Typing)')
        if ENABLE_BENCH_TIMER:
            # see wall-clock time (Python + native marker add + repaint)
            print(f"REDRAW: {time.perf_counter() - t0:.4f}s  edited_tokens={len(edited_tokens)}")
        # ==============================

    def config(self):
        """Opens the plugin configuration INI file."""
        session = self.get_session(ed)
        print("self.word_colors",session.word_colors)
        print("self.sessions",self.sessions)
        print("self.pending_lexer_parse_handles",self.pending_lexer_parse_handles)
        try:
            ini_config = PluginConfig()
            file_open(ini_config.file_path)
        except Exception as ex:
            msg_status(_('Cannot open config: ') + str(ex))


def start_profiling():
    """Initializes and enables the profiler, and creates an IO stream."""
    import cProfile
    import io
    pr = cProfile.Profile()
    pr.enable()
    s = io.StringIO()
    return pr, s

def stop_profiling(pr, s, sort_key='cumulative', max_lines=20, title='Profile Results'):
    """
    Disables the profiler, processes the stats, and prints them.
    Accepts pr (cProfile.Profile) and s (io.StringIO) objects.
    """
    import pstats
    
    # pr and s are guaranteed to be non-None if stop_profiling is called when ENABLE_PROFILING is True.
    
    try:
        pr.disable()
    except ValueError:
        # This can happen if an exception occurred in the profiled code before pr.enable() finished,
        # or if the profiler was stopped manually beforehand (which is now avoided).
        print(f"ERROR: Profiler for {title} was not properly enabled/disabled.")
        return

    # Get the stats object
    try:
        # Map human-readable sort_key to pstats.SortKey
        # default is sort by cumulative time (time spent in function + all sub-functions)
        sort_map = {
            'cumulative': pstats.SortKey.CUMULATIVE,
            'time': pstats.SortKey.TIME,
        }
        sortby = sort_map.get(sort_key.lower(), pstats.SortKey.CUMULATIVE)
        
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        
        # Print the stats to the in-memory stream 's'
        ps.print_stats(max_lines) 
        
        # Print the captured output to the console/log
        print(f"\n--- {title} ---")
        print(s.getvalue())
    except Exception as e:
        print(f"ERROR: Error processing profiling results for {title}: {e}")
