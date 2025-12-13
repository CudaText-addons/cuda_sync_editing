# Sync Editing plugin for CudaText
# by Vladislav Utkin <vlad@teamfnd.ru>
# MIT License
# 2018

import re
import os
from . import randomcolor
from cudatext import *
from cudatext_keys import *
from cudax_lib import html_color_to_int
from collections import defaultdict

from cudax_lib import get_translation
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
ENABLE_PROFILING = False
ENABLE_PROFILING_inside_on_caret = False
ENABLE_PROFILING_inside_redraw = False
ENABLE_BENCH_TIMER = False # print real time spent, usefull when profiling is disabled because profiling adds more overhead
if ENABLE_BENCH_TIMER:
    import time

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

class SyncEditSession:
    """
    Represents a single sync edit session for one file (tab).
    Each editor handle has its own instance of this class to maintain state isolation.
    """
    def __init__(self):
        self.selected = False
        self.editing = False
        self.dictionary = {} # Stores mapping of { "word_string": [list_of_token_tuples_positions] }
                             # Token tuple format: ((x1, y1), (x2, y2), string, style)
        self.our_key = None  # The specific word currently being edited
        self.original = None # Original caret position before editing
        self.start_l = None  # Start line of selection
        self.end_l = None    # End line of selection
        self.gutter_icon_line = None     # Line where gutter icon is displayed
        self.gutter_icon_active = False  # Whether gutter icon is currently shown as active
        self.line_lengths = {} # Track line lengths to detect edit deltas

        # Config ini
        self.use_colors = USE_COLORS_DEFAULT
        self.use_simple_naive_mode = USE_SIMPLE_NAIVE_MODE_DEFAULT
        self.case_sensitive = CASE_SENSITIVE_DEFAULT
        self.identifier_regex = IDENTIFIER_REGEX_DEFAULT
        self.identifier_style_include = IDENTIFIER_STYLE_INCLUDE_DEFAULT
        self.identifier_style_exclude = IDENTIFIER_STYLE_EXCLUDE_DEFAULT

        # Compiled regex objects
        self.regex_identifier = None
        self.regex_style_include = None
        self.regex_style_exclude = None

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
        handle = self.get_editor_handle(ed_self)
        if handle in self.inited_icon_eds:
            # print('Sync Editing: Forget handle')
            self.inited_icon_eds.remove(handle)
        self.remove_session(ed_self)

    def on_open_reopen(self, ed_self):
        """
        Called when the file is reloaded/reopened from disk (File → Reload).
        The entire document content is replaced, so all marker positions become invalid.
        We must fully exit sync editing to avoid crashes or visual glitches.
        """
        if self.has_session(ed_self):
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
        Shows gutter icon if there's a valid selection, hides it otherwise.
        """
        self.load_gutter_icons(ed_self)

        carets = ed_self.get_carets()
        if len(carets) != 1: # Don't support multi-carets
            self.hide_gutter_icon(ed_self)
            return

        # Check if we have a selection
        x0, y0, x1, y1 = carets[0]
        if y1 >= 0 and (y0 != y1 or x0 != x1):  # Has selection
            # Show icon at the last line of selection
            last_line = max(y0, y1)
            self.show_gutter_icon(ed_self, last_line)
        else:
            # No selection, hide icon if not in active sync edit mode
            # (If we are editing, we keep the icon logic managed by start_sync_edit)
            if self.has_session(ed_self):
                session = self.get_session(ed_self)
                if not session.selected and not session.editing:
                    self.hide_gutter_icon(ed_self)
            else:
                self.hide_gutter_icon(ed_self)

    def token_style_ok(self, ed_self, s):
        """Checks if a token's style matches the allowed patterns (IDs) and rejects Keywords."""
        session = self.get_session(ed_self)
        good = session.regex_style_include.fullmatch(s)
        bad = session.regex_style_exclude.fullmatch(s)
        return good and not bad

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
        self.start_sync_edit(ed_self)

    def start_sync_edit(self, ed_self):
        """
        Starts sync editing session.
        1. Validates selection.
        2. Scans text (via Lexer or Regex).
        3. Groups identical words.
        4. Applies visual markers (colors).

        All configuration is read fresh from file/theme on every start so the user does not need to restart CudaText.
        """
        # === PROFILING START: START_SYNC_EDIT ===
        if ENABLE_PROFILING:
            pr_start, s_start = start_profiling()
        if ENABLE_BENCH_TIMER:
            t0 = time.perf_counter()
        # ========================================
        
        session = self.get_session(ed_self)
        # now that we created a session we should always call update_gutter_icon_on_selection before start_sync_edit to set gutter_icon_line (set by show_gutter_icon) which will be used in start_sync_edit to set the active gutter icon
        # Update gutter icon before starting to ensure session.gutter_icon_line is set. This allows us to flip it to "Active" mode shortly after.
        self.update_gutter_icon_on_selection(ed_self)

        carets = ed_self.get_carets()
        if len(carets) != 1:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Need single caret'))
            
            # === PROFILING STOP: START_SYNC_EDIT (Exit Early) ===
            if ENABLE_PROFILING:
                stop_profiling(pr_start, s_start, title='PROFILE: start_sync_edit (Entry Mode - Early Exit)')
            # ====================================================
            return
        caret = carets[0]

        def restore_caret():
            ed_self.set_caret(caret[0], caret[1])

        original = ed_self.get_text_sel()

        # --- 1. Selection Handling ---
        # Check if we have selection of text
        if not original:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Make selection first'))
            
            # === PROFILING STOP: START_SYNC_EDIT (Exit Early) ===
            if ENABLE_PROFILING:
                stop_profiling(pr_start, s_start, title='PROFILE: start_sync_edit (Entry Mode - Early Exit)')
            # ====================================================
            return

        self.set_progress(3)
        session.dictionary = defaultdict(list)

        # Save coordinates and "Lock" the selection
        session.start_l, session.end_l = ed_self.get_sel_lines()
        session.selected = True

        # Init line lengths
        session.line_lengths = {}
        for y in range(session.start_l, session.end_l + 1):
            session.line_lengths[y] = len(ed_self.get_text_line(y))

        # Break text selection and clear visual selection to show markers instead
        ed_self.set_sel_rect(0,0,0,0)

        self.set_progress(5)

        # Update gutter icon to show active state
        if session.gutter_icon_line is not None:
            self.show_gutter_icon(ed_self, session.gutter_icon_line, active=True)

        # Mark the range properties for CudaText
        ed_self.set_prop(PROP_MARKED_RANGE, (session.start_l, session.end_l))


        # --- 2. Lexer / Parser Configuration ---
        # Instantiate config to get fresh values from disk on every session
        ini_config = PluginConfig()
        
        cur_lexer = ed_self.get_prop(PROP_LEXER_FILE)
        session.use_simple_naive_mode = ini_config.get_lexer_bool(cur_lexer, 'use_simple_naive_mode', USE_SIMPLE_NAIVE_MODE_DEFAULT)
        
        # Force naive way if lexer is none or lexer is one of the text file types
        if not cur_lexer or cur_lexer in NAIVE_LEXERS:
            session.use_simple_naive_mode = True
        
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
            session.regex_style_include = re.compile(session.identifier_style_include)
        except Exception:
            msg_status(_('Sync Editing: Invalid identifier_style_include config - using fallback'))
            print(_('ERROR: Sync Editing: Invalid identifier_style_include config - using fallback'))
            session.regex_style_include = re.compile(local_styles_default)

        try:
            session.regex_style_exclude = re.compile(session.identifier_style_exclude)
        except Exception:
            msg_status(_('Sync Editing: Invalid identifier_style_exclude config - using fallback'))
            print(_('ERROR: Sync Editing: Invalid identifier_style_exclude config - using fallback'))
            session.regex_style_exclude = re.compile(IDENTIFIER_STYLE_EXCLUDE_DEFAULT)

        # NOTE: Do not use app_idle (set_progress) before EDACTION_LEXER_SCAN.
        # App_idle runs message processing which can conflict with parsing actions.
        # self.set_progress(10) # do not use this here before ed.action(EDACTION_LEXER_SCAN. see bug: https://github.com/Alexey-T/CudaText/issues/6120 the bug happen only with this line. Alexey said: app_idle is the main reason, it is bad to insert it before some parsing action. Usually app_idle is needed after some action, to run the app message processing. Not before. Dont use it if not nessesary...

        # Run lexer scan from start. Force a Lexer scan to ensure tokens are up to date
        # EDACTION_LEXER_SCAN seems not needed anymore see:https://github.com/Alexey-T/CudaText/issues/6124
        # ed_self.action(EDACTION_LEXER_SCAN, session.start_l) #API 1.0.289
        self.set_progress(40)

        # Find all occurences of regex, get all tokens in the selected range
        tokenlist = ed_self.get_token(TOKEN_LIST_SUB, session.start_l, session.end_l) or []
        # print("tokenlist",tokenlist)

        x1, y1, x2, y2 = caret
        # Sort coords of caret
        if (y1, x1) > (y2, x2):
            x1, y1, x2, y2 = x2, y2, x1, y1
        # FIX #15 regression: the problem come from get_sel_lines() it does not return the last empty line, but get_carets() include the last empty line. here we handle selection ending at the start of a new line.
        # If x2 is 0 and we have multiple lines, it means the selection visually ends at the previous line's end. We adjust x2, y2 to point to the "end" of the previous line so filters don't drop tokens on that line.
        if x2 == 0 and y2 > y1:
            y2 -= 1
            x2 = ed_self.get_line_len(y2)
        # Drop tokens outside of selection
        tokenlist = [t for t in tokenlist if \
            not (t['y1'] == session.start_l and t['x1'] < x1) and \
            not (t['y2'] == session.end_l and t['x2'] > x2) \
            ]


        self.set_progress(45)

        # --- 3. Token Processing ---
        if not tokenlist and not session.use_simple_naive_mode:
            self.reset(ed_self)
            msg_status(_('Sync Editing: No syntax tokens found in selection'))
            self.set_progress(-1)
            restore_caret()
            
            # === PROFILING STOP: START_SYNC_EDIT (Exit Early) ===
            if ENABLE_PROFILING:
                stop_profiling(pr_start, s_start, title='PROFILE: start_sync_edit (Entry Mode - Early Exit)')
            # ====================================================
            return
        
        # Pre-compute case sensitivity handler
        key_normalizer = (lambda s: s.lower()) if not session.case_sensitive else (lambda s: s)
        
        if session.use_simple_naive_mode:
            # Naive Mode: Scan text purely by Regex, ignoring syntax context
            for y in range(session.start_l, session.end_l+1):
                cur_line = ed_self.get_text_line(y)
                for match in session.regex_identifier.finditer(cur_line):
                    # Drop tokens out of selection
                    if y == session.start_l and match.start() < x1:
                        continue
                    if y == session.end_l and match.end() > x2:
                        continue
                    key = key_normalizer(match.group())
                    # Create pseudo-token structure: ((x1, y1), (x2, y2), string, style)
                    token = ((match.start(), y), (match.end(), y), match.group(), 'id')
                    session.dictionary[key].append(token)
        else:
            # Standard Lexer Mode: Filter tokens by Style (Variable, Function, etc.)
            for token in tokenlist:
                if not self.token_style_ok(ed_self, token['style']):
                    continue
                
                idd = key_normalizer(token['str'].strip())
                # Structure: ((x1, y1), (x2, y2), string, style)
                old_style_token = ((token['x1'], token['y1']), (token['x2'], token['y2']), token['str'], token['style'])
                session.dictionary[idd].append(old_style_token)
        
        self.set_progress(60)
        
        # Remove Singletons: We delete keys that don't have duplicates because sync editing requires at least 2 occurrences. (issue #44 and #45)
        session.dictionary = {k: v for k, v in session.dictionary.items() if len(v) >= 2}

        # Validation: Ensure we actually found words to edit. Exit if no id's (eg: comments and etc)
        if not session.dictionary:
            self.reset(ed_self)
            msg_status(_('Sync Editing: No editable identifiers found in selection'))
            self.set_progress(-1)
            restore_caret()
            
            # === PROFILING STOP: START_SYNC_EDIT (Exit Early) ===
            if ENABLE_PROFILING:
                stop_profiling(pr_start, s_start, title='PROFILE: start_sync_edit (Entry Mode - Early Exit)')
            # ====================================================
            return

        self.set_progress(90)

        # --- 4. Generate Color Map (once for entire session) ---
        # Pre-generate all colors to maintain consistency of colors when switching between View and Edit mode, so words will have the same color always inside the same session, and this reduce overhead also
        if session.use_colors:
            session.word_colors = {}
            rand_color = randomcolor.RandomColor()
            for key in session.dictionary:
                session.word_colors[key] = html_color_to_int(rand_color.generate(luminosity='light')[0])

        self.set_progress(95)
        
        # --- 5. Apply Visual Markers ---
        # Visualize all editable identifiers in the selection. Mark all words that we can modify with pretty light color
        self.mark_all_words(ed_self)
        self.set_progress(-1)

        msg_status(_('Sync Editing: Click an ID to edit, click gutter icon or press Esc to exit.'))
        # restore caret but w/o selection
        restore_caret()
        
        # === PROFILING STOP: START_SYNC_EDIT ===
        if ENABLE_PROFILING:
            stop_profiling(pr_start, s_start, sort_key='cumulative', max_lines=200, title='PROFILE: start_sync_edit (Entry Mode)')
        # see wall-clock time (Python + native marker add + repaint)
        if ENABLE_BENCH_TIMER:
            print(f"START_SYNC_EDIT: {time.perf_counter() - t0:.4f}s")
        # =======================================

    def set_progress(self, prg):
        """Updates the CudaText status bar progress (fixes issue #46)."""
        app_proc(PROC_PROGRESSBAR, prg)
        app_idle()

    def mark_all_words(self, ed_self):
        """
        Visualizes all editable identifiers in the selection.
        Used during initialization and when returning to selection mode after an edit.
        """
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        session = self.get_session(ed_self)
        if not session.use_colors:
            return
        
        rand_color = randomcolor.RandomColor()
        
        # Collect all markers to add, sorted by (y, x)
        markers_to_add = []
        
        for key in session.dictionary:
            # Get pre-generated color for this word
            color = session.word_colors.get(key, 0xFFFFFF)
            
            for key_tuple in session.dictionary[key]:
                markers_to_add.append((
                    key_tuple[0][1],  # y
                    key_tuple[0][0],  # x
                    key_tuple[1][0] - key_tuple[0][0],  # len
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

        # Re-paint markers so user can see what else to edit
        self.mark_all_words(ed_self)

    def caret_in_current_token(self, ed_self):
        """
        Helper: Checks if the primary caret is strictly inside
        the boundaries of the word currently being edited.
        
        FIX: During Edit Mode, we ignore the regex check and rely purely on the geometric
        bounds, which are dynamically updated in `redraw`. This allows non-identifier
        characters (like dots) to be included in the word without breaking the session.
        """
        session = self.get_session(ed_self)
        if not session.our_key:
            return False
        carets = ed_self.get_carets()
        if not carets:
            return False
        x0, y0, x1, y1 = carets[0]
        current_line = ed_self.get_text_line(y0)
        for key_tuple in session.dictionary.get(session.our_key, []):
            start_pos, end_pos = key_tuple[0], key_tuple[1]
            if y0 != start_pos[1]:
                continue
            start_x = start_pos[0]
            if x0 < start_x:
                continue

            # Special Check: Allow caret to be at the immediate end of the word being typed.
            # If the regex matches a string starting at start_x, and the caret is at the end of that match,
            # we consider it "inside" so the user can continue typing. so this allow caret to stay considered "inside" while the token is being grown
            # if session.regex_identifier:
            #     match = session.regex_identifier.match(current_line[start_x:]) if start_x <= len(current_line) else None
            #     if match:
            #         dynamic_end = start_x + len(match.group(0))
            #         if x0 <= dynamic_end:
            #             return True

            if x0 <= end_pos[0]:
                return True
        return False

    def reset(self, ed_self=None):
        """
        FULLY Exits the plugin.
        Clears markers, releases selection lock, and resets all state variables.
        Triggered via 'Toggle' command, gutter icon click, or 'ESC' key.
        """
        if ed_self is None:
            ed_self = ed
        session = self.get_session(ed_self)

        # Restore original position if needed
        if session.original:
            ed_self.set_caret(session.original[0], session.original[1], id=CARET_SET_ONE)

        # Clear all markers
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        ed_self.set_prop(PROP_MARKED_RANGE, (-1, -1))
        self.set_progress(-1)

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
           - Yes: Start Editing (Add carets, borders).
           - No: Do nothing (Do not exit).
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        if not self.has_session(ed_self):
            return

        session = self.get_session(ed_self)
        if not session.selected and not session.editing:
            return

        # This finish_editing() is necessary for edge cases:
        # - When the event sequence is unpredictable, currently cudatext in my tests always send on_caret event before on_click event so finish_editing runs in on_caret so we do not need it here, but if cudatext change the events orders we will need finish_editing here
        # - When clicking on empty space while editing if on_click event come before on_caret (should not happen)
        # on_caret handles ID-to-ID transitions smoothly, but this ensures we always reach a clean state before starting new editing
        if session.editing:
            self.finish_editing(ed_self)

        carets = ed_self.get_carets()
        if not carets:
            return

        clicked_key = None
        caret = carets[0]
        offset = 0

        # Find which word was clicked
        for key in session.dictionary:
            for key_tuple in session.dictionary[key]:
                if  caret[1] >= key_tuple[0][1] \
                and caret[1] <= key_tuple[1][1] \
                and caret[0] <= key_tuple[1][0] \
                and caret[0] >= key_tuple[0][0]:
                    clicked_key = key
                    offset = caret[0] - key_tuple[0][0]
                    break
            if clicked_key:
                break

        # If click was NOT on a valid word
        # Not editing - in selection mode
        if not clicked_key:
            msg_status(_('Sync Editing: Not a word! Click on ID to edit it.'))
            return

        # --- Start Editing Sequence ---
        # Clear passive markers (background colors)
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        session.our_key = clicked_key
        session.original = (caret[0], caret[1])

        # Collect all markers to add, sorted by (y, x)
        markers_to_add = []
        for key_tuple in session.dictionary[session.our_key]:
            markers_to_add.append((
                key_tuple[0][1],  # y
                key_tuple[0][0],  # x
                key_tuple[1][0] - key_tuple[0][0],  # len
                offset  # store offset for caret placement
            ))
        
        # Sort markers by (y, x)
        markers_to_add.sort(key=lambda m: (m[0], m[1]))
        
        # Add active carets and borders to ALL instances of the clicked word
        for y, x, length, off in markers_to_add:
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
            ed_self.set_caret(x + off, y, id=CARET_ADD)

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
                    self.start_sync_edit(ed_self)
                return False  # Prevent default processing

    def on_caret(self, ed_self):
    # on_caret_slow is better because it will consume less resources but it breaks the colors recalculations when user edit an ID, so stick with on_caret
        """
        Hooks into caret movement.
        Continuous Edit Logic:
        If the user moves the caret OUTSIDE the active word, we do NOT exit immediately.
        We check if the landing spot is another valid ID.
        - If landing on valid ID: Do nothing (let on_click handle the switch seamlessy).
        - If landing elsewhere: We 'finish' the edit, return to Selection mode, and show colors.

        Also handles showing/hiding gutter icon based on selection state.
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        if not self.has_session(ed_self):
            # Only show/hide gutter icon when NOT in sync edit mode
            self.update_gutter_icon_on_selection(ed_self)
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
                    for key in session.dictionary:
                        for key_tuple in session.dictionary[key]:
                            if  caret[1] >= key_tuple[0][1] \
                            and caret[1] <= key_tuple[1][1] \
                            and caret[0] <= key_tuple[1][0] \
                            and caret[0] >= key_tuple[0][0]:
                                clicked_key = key
                                break
                        if clicked_key:
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
        if ENABLE_PROFILING_inside_on_caret:
            stop_profiling(pr_on_caret, s_on_caret, sort_key='cumulative', title='PROFILE: on_caret (End)')
        # =================================================

    def redraw(self, ed_self):
        """
        Dynamically updates markers and dictionary positions during typing.
        Because editing changes the length of the word, we must:
        1. Find the new word string at the caret position.
        2. Update the dictionary entry for the currently edited word (start/end positions).
        3. Shift positions of ALL other words that exist on the same line after the caret.
        4. Re-draw all the borders.
        
        FIX: Geometry-Based Redraw.
        We track the change in line length (delta) to calculate the new word string
        and subsequent shifts, ignoring IDENTIFIER_REGEX_DEFAULT for the active word.
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

        old_key = session.our_key
        
        carets = ed_self.get_carets()
        if not carets: 
            # === PROFILING STOP: REDRAW (Exit Early 2) ===
            if ENABLE_PROFILING_inside_redraw:
                stop_profiling(pr_redraw, s_redraw, sort_key='cumulative', title='PROFILE: redraw (Live Typing - Exit Early 2)')
            # ===========================================
            return

        cx, cy = carets[0][:2]
        
        # 1. Identify the 'Anchor' instance we are editing
        # We look in the dictionary for the token on this line that is closest to the caret
        # Note: dictionary has OLD coordinates
        candidates = [t for t in session.dictionary.get(old_key, []) if t[0][1] == cy]
        if not candidates: 
            # Fallback (should not happen in valid state)
            # === PROFILING STOP: REDRAW (Exit Early 3) ===
            if ENABLE_PROFILING_inside_redraw:
                stop_profiling(pr_redraw, s_redraw, sort_key='cumulative', title='PROFILE: redraw (Live Typing - Exit Early 3)')
            # ===========================================
            return
            
        # Find closest token to caret
        active_entry = min(candidates, key=lambda t: abs(t[0][0] - cx))
        start_x = active_entry[0][0] # The starting X of the token before this edit

        # 2. Calculate Delta
        current_line_text = ed_self.get_text_line(cy)
        old_line_len = session.line_lengths.get(cy, len(current_line_text))
        delta = len(current_line_text) - old_line_len
        
        # 3. Determine New Key
        # New length = Old Key Length + Delta (This is the key to accepting non-identifier chars)
        new_len = len(old_key) + delta
        
        if new_len <= 0:
            # Handle deletion of word
            session.our_key = None
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
            session.dictionary.pop(old_key, None)
            
            # === PROFILING STOP: REDRAW (Exit Early 4) ===
            if ENABLE_PROFILING_inside_redraw:
                stop_profiling(pr_redraw, s_redraw, sort_key='cumulative', title='PROFILE: redraw (Live Typing - Exit Early 4)')
            # ===========================================
            return
            
        # Extract new key string directly from text using geometry
        new_key_display = current_line_text[start_x : start_x + new_len]
        if not session.case_sensitive:
            new_key = new_key_display.lower() # Logic case
        else:
            new_key = new_key_display

        session.our_key = new_key # Now we update it

        # 4. Update Dictionary & Calculate Shifts
        
        # Pre-calculate shift for lines
        affected_lines = set()
        old_key_dictionary = session.dictionary.get(old_key, [])
        for entry in old_key_dictionary:
            affected_lines.add(entry[0][1])

        # Prepare existing_entries list for the new key (which may be old_key if only length changed)
        # We start with an empty list for the new key, and populate it with shifted old_key tokens.
        existing_entries = []
        
        # Pointers are the starting coordinates of all instances of the OLD_KEY
        pointers = [i[0] for i in old_key_dictionary]
        pointers.sort(key=lambda p: (p[1], p[0])) # Sort top-down, left-right

        # This map tracks the *accumulated* shift on a line due to multiple instances of the same word being edited.
        line_shifts = defaultdict(int) 
        
        for p in pointers:
            ox, oy = p
            
            # The new X position for this instance is its original X plus the total shift caused by previous instances on the same line that were also edited.
            nx = ox + line_shifts[oy]
            
            # Update list
            existing_entries.append(((nx, oy), (nx+new_len, oy), new_key_display, 'Id'))
            
            # Increment the shift for the NEXT word on this line (if there is one)
            line_shifts[oy] += delta
            
        # Update stored line lengths for the *next* redraw
        for y in affected_lines:
            session.line_lengths[y] = len(ed_self.get_text_line(y))

        # Update dictionary keys for the edited word. Clean up old key if it changed completely
        if old_key != new_key:
            session.dictionary.pop(old_key, None)
        session.dictionary[new_key] = existing_entries

        # 5. Shift Other Words on the Same Line. Update positions of ALL other words on affected lines
        if delta != 0:
            for line_num in affected_lines:
                # Iterate over ALL other words in the dictionary
                for other_key in list(session.dictionary.keys()):
                    if other_key == new_key:
                        continue  # Skip the word we just edited

                    updated_entries = []
                    for entry in session.dictionary[other_key]:
                        if entry[0][1] == line_num:  # If this word is on the affected line. Same line
                            word_start_x = entry[0][0]
                            word_end_x = entry[1][0]

                            # Calculate total shift: Check how many edited instances are to the left of this word.
                            # We use the sorted original tokens of the OLD_KEY on this line.
                            shift_amount = 0
                            for p in pointers:
                                if p[1] == line_num and p[0] < word_start_x:
                                    shift_amount += delta

                            if shift_amount != 0:
                                # Create new entry with shifted position
                                new_entry = (
                                    (word_start_x + shift_amount, entry[0][1]),
                                    (word_end_x + shift_amount, entry[1][1]),
                                    entry[2],
                                    entry[3]
                                )
                                updated_entries.append(new_entry)
                            else:
                                updated_entries.append(entry)
                        else:
                            # Different line, keep as-is
                            updated_entries.append(entry)

                    session.dictionary[other_key] = updated_entries

        # 6. Repaint borders for ALL words
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)

        # Collect all markers to add, sorted by (y, x)
        markers_to_add = []
        for key_tuple in session.dictionary[session.our_key]:
            markers_to_add.append((
                key_tuple[0][1],  # y
                key_tuple[0][0],  # x
                key_tuple[1][0] - key_tuple[0][0]  # len
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

        # === PROFILING STOP: REDRAW ===
        if ENABLE_PROFILING_inside_redraw:
            stop_profiling(pr_redraw, s_redraw, sort_key='time', max_lines=200, title='PROFILE: redraw (Live Typing)')
        if ENABLE_BENCH_TIMER:
            # see wall-clock time (Python + native marker add + repaint)
            print(f"REDRAW: {time.perf_counter() - t0:.4f}s  instances={len(existing_entries)}")
        # ==============================

    def config(self):
        """Opens the plugin configuration INI file."""
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
