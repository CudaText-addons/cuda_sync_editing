# Sync Editing plugin for CudaText
# by Vladislav Utkin <vlad@teamfnd.ru>
# MIT License
# 2018

import re
import os
import time
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
ENABLE_PROFILING_inside_on_click = True
ENABLE_BENCH_TIMER = True # print real time spent, usefull when profiling is disabled because profiling adds more overhead

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

_mydir = os.path.dirname(__file__)
filename_install_inf = os.path.join(_mydir, 'install.inf')

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

# Parse install.inf events and keys
install_inf_events = parse_install_inf_events()

def set_events_safely(events_to_add, lexer_list=''):
    """
    Set events while preserving those from install.inf. because PROC_SET_EVENTS resets all the events including those from install.inf (only events in plugins.ini are preserved).
    
    Args:
        events_to_add: Set or list of event names to add (without filter strings)
        lexer_list: Comma-separated lexer names (optional)
    """
    # Combine install.inf events with new events
    all_events = {}
    
    # Add install.inf events with their filters
    all_events.update(install_inf_events)
    
    # Add new events (without filters, will use empty string)
    for event in events_to_add:
        if event not in all_events:
            all_events[event] = ''
    
    # Build event string with filters
    # Format: "plugin_name;event1,event2;lexer_list;filter1,filter2"
    # Only include filter strings for events that have non-empty filters
    event_names = ','.join(all_events.keys())
    
    # Build filter string - only include non-empty filters
    filter_list = [f for f in all_events.values() if f]
    filter_strings = ','.join(filter_list) if filter_list else ''
    
    # print('PROC_SET_EVENTS', f"cuda_sync_editing;{event_names};{lexer_list};{filter_strings}")
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
        central event subscription management method
        Central method to manage all dynamic event subscriptions.
        Coordinates on_lexer_parsed, on_scroll, and on_caret based on current plugin state.
        
        This ensures:
        - on_lexer_parsed is active when editors are waiting for lexer parsing
        - on_scroll is active when editors have active sync sessions OR have selections
        - on_caret is active when editors have active sync sessions
        - Events are properly combined and don't conflict
        
        this necesary for now, once PROC_GET_EVENTS https://github.com/Alexey-T/CudaText/issues/6138 is implemented this can be simplified 
        """
        events_needed = []
        
        # Add on_lexer_parsed if any editor is waiting for lexer parsing
        if self.pending_lexer_parse_handles:
            events_needed.append('on_lexer_parsed')
        
        # Add on_scroll if any editor has an active sync session OR has a selection
        if self.active_scroll_handles or self.selection_scroll_handles:
            events_needed.append('on_scroll')
        
        # Add on_caret if any editor has an active sync session
        if self.active_caret_handles:
            events_needed.append('on_caret')
            
        # Update subscriptions (preserves install.inf events)
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
        
        # Clean up selection scroll tracking before reset
        self.selection_scroll_handles.discard(handle)
        
        self.reset(ed_self, cleanup_lexer_events=True)

    def on_open_reopen(self, ed_self):
        """
        Called when the file is reloaded/reopened from disk (File → Reload).
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
          - When Editor A finishes → on_lexer_parsed(ed_self=Editor_A) fires
          - When Editor B finishes → on_lexer_parsed(ed_self=Editor_B) fires
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

        def restore_caret():
            ed_self.set_caret(caret[0], caret[1])

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
                restore_caret()
                
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
            restore_caret()
            
            # === PROFILING STOP: START_SYNC_EDIT (Exit Early) ===
            if ENABLE_PROFILING_inside_start_sync_edit:
                stop_profiling(pr_start, s_start, title='PROFILE: start_sync_edit (Entry Mode - Early Exit)')
            # ====================================================
            return

        # --- 5. Generate Color Map (once for entire session) ---
        
        # Pre-generate all colors to maintain consistency of colors when switching between View and Edit mode, so words will have the same color always inside the same session, and this reduce overhead also
        if session.use_colors:
            session.word_colors = {
                key: (((hash(key) & 0xFF0000) >> 16) % 127 + 128) |
                     ((((hash(key) & 0x00FF00) >> 8) % 127 + 128) << 8) |
                     (((hash(key) & 0x0000FF) % 127 + 128) << 16)
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
        
        # Subscribe to on_scroll event for this editor
        handle = self.get_editor_handle(ed_self)
        self.active_scroll_handles.add(handle)
        # Subscribe to on_caret event for live editing updates
        self.active_caret_handles.add(handle)
        self._update_event_subscriptions()
        
        # restore caret but w/o selection
        restore_caret()
        
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
            # Get pre-generated color for this word
            color = session.word_colors.get(key, 0x00FFFF) # when user edit a word it becomes a new word so it have no cached color in word_colors, so it will use 0x00FFFF:yellow, i found this better than generating a new color because it allows to easly identify which words were changed
            
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

    def finish_editing(self, ed_self):
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

        # Reset carets to single caret (keep first caret position)
        carets = ed_self.get_carets()
        if carets:
            first_caret = carets[0]
            ed_self.set_caret(first_caret[0], first_caret[1], id=CARET_SET_ONE)

        # Reset flags to 'Selection' mode
        session.original = None
        session.editing = False
        session.selected = True
        session.our_key = None

        # Re-paint markers so user can see what else to edit (ONLY VISIBLE VIEWPORT PORTION)
        self.mark_all_words(ed_self)

    def caret_in_current_token(self, ed_self):
        """
        Helper: Checks if the primary caret is strictly inside
        the boundaries of the word currently being edited.
        Uses line index for faster lookup.
        """
        session = self.get_session(ed_self)
        if not session.our_key:
            return False
        carets = ed_self.get_carets()
        if not carets:
            return False
        x0, y0, x1, y1 = carets[0]
        
        # Check line index first (O(1) lookup)
        if y0 not in session.line_index:
            return False
        
        current_line = ed_self.get_text_line(y0)
        
        # Only check tokens on the current line
        for token_ref, key in session.line_index[y0]:
            if key != session.our_key:
                continue
            
            start_x = token_ref.start_x
            if x0 < start_x:
                continue

            # Special Check: Allow caret to be at the immediate end of the word being typed.
            # If the regex matches a string starting at start_x, and the caret is at the end of that match,
            # we consider it "inside" so the user can continue typing. so this allow caret to stay considered "inside" while the token is being grown
            if session.regex_identifier:
                match = session.regex_identifier.match(current_line[start_x:]) if start_x <= len(current_line) else None
                if match:
                    dynamic_end = start_x + len(match.group(0))
                    if x0 <= dynamic_end:
                        return True

            if x0 <= token_ref.end_x:
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
            NOTE: Scroll and caret events are ALWAYS cleaned up regardless.
        """
        if ed_self is None:
            ed_self = ed
        session = self.get_session(ed_self)
        handle = self.get_editor_handle(ed_self)
        
        # ALWAYS clean up scroll and caret tracking (session is ending)
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
        # here we unsubscribe from on_lexer_parsed if no more editors are waiting, on_scroll if no other editor need it, and on_caret if no other editor needs it
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
        Handles mouse clicks to toggle between 'Viewing' and 'Editing'.
        Logic:
        1. If Editing -> Finish current edit (Loop back to Selection).
        2. If Selection -> Check if click is on valid ID.
           - Yes: Start Editing (Add carets to ALL duplicates, and add color and borders to VISIBLE VIEWPORT PORTION only).
           - No: Do nothing (Do not exit).
        Uses spatial index for faster word lookups.
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
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
        # ===================================================================

        # If click was NOT on a valid word
        # Not editing - in selection mode
        if not clicked_key:
            if session.editing:
                # User clicked outside while editing → finish editing normally (show colors again)
                # this will never happen because we already called finish_editing inside on_caret which sets session.editing = False, because on_caret event always come before on_click events in cudatext, but if cudatext change this in the future then we are safe
                self.finish_editing(ed_self)
            # else:
                # msg_status(_('Sync Editing: Not a word! Click on ID to edit it.'))
            return

        # At this point we have a valid clicked_key → we are either:
        #   1. Starting editing from selection mode, or
        #   2. Switching directly from one ID to another ID (seamless, no colors flash)

        # Seamless switch preparation: only clear markers + reset carets, NO mark_all_words()
        if session.editing:
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
            # Reset to single caret at the clicked position (keeps caret where user clicked)
            ed_self.set_caret(clicked_x, clicked_y, id=CARET_SET_ONE)
            if is_switch:
                if ENABLE_BENCH_TIMER:
                    print(f">>> Switch phase 1 (finish old): {time.perf_counter() - switch_start:.4f}s")
        else:
            # First time entering editing mode → clear colored backgrounds
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)

        # --- Start Editing Sequence (new word) ---
        session.our_key = clicked_key
        session.original = (clicked_x, clicked_y)

        # Get visible line range
        line_top, line_bottom = self.get_visible_line_range(ed_self)

        # Collect all markers to add, sorted by (y, x)
        # Collect markers only for visible lines
        # we collect ALL instances for caret placement, but markers only for visible lines
        all_carets = []
        markers_to_add = []
        
        for token_ref in session.dictionary[session.our_key]:
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
        
        # Sort both lists by (y, x)
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
        # ===================================================================

        # Update state
        session.selected = False
        session.editing = True

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
        Called when caret stops moving (debounced caret event).
        Handles gutter icon visibility based on selection state.
        Only active when NOT in sync edit mode (lightweight).
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
        If the user moves the caret OUTSIDE the active word, we do NOT exit immediately.
        We check if the landing spot is another valid ID.
        - If landing on valid ID: Do nothing (let on_click handle the switch seamlessy).
        - If landing elsewhere: We 'finish' the edit, return to Selection mode, and show colors.
        
        NOTE: This event is ONLY subscribed during active sync sessions (dynamically).
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        # This should never happen since we only subscribe when active, but when sync edit is active in one tab then we must prevent this to run on other tabs if the user switch to another tab, otherwise we will create a session for every tab the user switch to it and of course will add overhead also, so we must keep this check
        if not self.has_session(ed_self):
            return
        
        # === PROFILING START: ON_CARET ===
        if ENABLE_PROFILING_inside_on_caret:
            pr_on_caret, s_on_caret = start_profiling()
        # =================================
        
        # Now we know sync edit is active, get session
        session = self.get_session(ed_self)

        if session.editing:
            if not self.caret_in_current_token(ed_self):
                # Caret left current token - check if it's on another valid ID
                carets = ed_self.get_carets()
                if carets:
                    caret = carets[0]
                    clicked_key = None

                    # Check if caret is on a valid ID
                    # Use line index for fast lookup
                    clicked_y = caret[1]
                    clicked_x = caret[0]
                    
                    if clicked_y in session.line_index:
                        for token_ref, key in session.line_index[clicked_y]:
                            if clicked_x >= token_ref.start_x and clicked_x <= token_ref.end_x:
                                clicked_key = key
                                break

                    # If NOT on a valid ID, finish editing and show colors
                    if not clicked_key:
                        self.finish_editing(ed_self)
                    # If on a valid ID, do nothing - let on_click handle the transition
                    # This prevents flashing colors when switching directly between IDs
                else:
                    self.finish_editing(ed_self)

                # === PROFILING STOP: ON_CARET (Exit Editing) ===
                if ENABLE_PROFILING_inside_on_caret:
                    stop_profiling(pr_on_caret, s_on_caret, sort_key='cumulative', title='PROFILE: on_caret (Exit Editing)')
                # ===============================================
                return

            # NOTE: self.redraw(ed_self) is called here to update word markers live during typing.
            # This recalculates borders and shifts other tokens on the line as the word grows/shrinks. This is a performance hit on simple caret moves (arrow keys) but necessary for live updates.
            self.redraw(ed_self)
        
        # === PROFILING STOP: ON_CARET (End of function) ===
        # if ENABLE_PROFILING_inside_on_caret:
            # stop_profiling(pr_on_caret, s_on_caret, sort_key='cumulative', title='PROFILE: on_caret (End)')
        # =================================================

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
        Handles Esc Keyboard input to cancel sync editing.
        Strict Exit Logic: Only VK_ESCAPE triggers the full 'reset' (Exit).
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        if not self.has_session(ed_self):
            return

        if key == VK_ESCAPE:
            self.reset(ed_self)
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
        start_pos = first_x

        # Backtrack from caret to find start of the new word
        # Workaround for end of id case: If caret is at the very end, move back 1 to capture the match
        if not session.regex_identifier.match(first_y_line[start_pos:]):
            start_pos -= 1

        # Move start_pos back until we find the beginning of the identifier
        while start_pos >= 0 and session.regex_identifier.match(first_y_line[start_pos:]):
            start_pos -= 1
        start_pos += 1
        # Workaround for EOL #65. Safety for EOL/BOL cases
        if start_pos < 0:
            start_pos = 0

        # Check if word became empty (deleted). Workaround for empty id (eg. when it was deleted) #62
        match = session.regex_identifier.match(first_y_line[start_pos:])
        if not match:
            # Word was deleted completely
            session.our_key = old_key
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
            
            # === PROFILING STOP: REDRAW (Exit Early 2) ===
            if ENABLE_PROFILING_inside_redraw:
                stop_profiling(pr_redraw, s_redraw, sort_key='cumulative', title='PROFILE: redraw (Live Typing - Exit Early 2)')
            # ===========================================
            return

        new_key = match.group(0)
        if not session.case_sensitive:
            new_key = new_key.lower()

        # 2. Calculate Length Delta change
        old_length = len(old_key)
        new_length = len(new_key)
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

        # Get all instances of edited word
        edited_tokens = session.dictionary.get(old_key, [])
        if not edited_tokens:
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
            
            # Find new position (may have shifted due to earlier edits on same line)
            y_line = ed_self.get_text_line(line_num)
            
            # Scan backwards to find start of the new word instance from the adjusted position
            # here we search for the token starting from its old position
            search_x = old_token_x
            while search_x >= 0 and session.regex_identifier.match(y_line[search_x:]):
                search_x -= 1
            search_x += 1
            # Workaround for EOL #65
            if search_x < 0:
                search_x = 0
            
            # Update this token's position in-place
            token_ref.start_x = search_x
            token_ref.end_x = search_x + new_length
            token_ref.text = new_key
            
            # Shift other tokens on this line that come AFTER this token
            # Only process tokens on the same line (using spatial index)
            if delta != 0 and line_num in session.line_index:
                for other_ref, other_key in session.line_index[line_num]:
                    # Skip the token we just updated
                    if other_ref is token_ref:
                        continue
                    
                    # If other token comes after this one, shift it
                    if other_ref.start_x > old_token_x:
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
        if old_key != new_key:
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
            session.our_key = old_key
            
            
        # 5. Repaint borders ONLY FOR VISIBLE VIEWPORT PORTION
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
