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
  'Ini files ^',
]

MARKER_CODE = app_proc(PROC_GET_UNIQUE_TAG, '') # Generate a unique integer tag for this plugin's markers to avoid conflicts with other plugins
DECOR_TAG = app_proc(PROC_GET_UNIQUE_TAG, '')  # Unique tag for gutter decorations


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

    def show_gutter_icon(self, ed_self, line_index, active=False):
        """Shows the gutter icon at the specified line."""
        # Remove any existing gutter icon first
        self.hide_gutter_icon(ed_self)

        # Choose icon based on active state
        icon_index = self.icon_active if active else self.icon_inactive

        ed_self.decor(DECOR_SET, line=line_index, tag=DECOR_TAG, text='', image=icon_index, auto_del=False)

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

        # Check if we have a selection
        x0, y0, x1, y1 = ed_self.get_carets()[0]
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
        session = self.get_session(ed_self)
        # now that we created a session we should always call update_gutter_icon_on_selection before start_sync_edit to set gutter_icon_line (set by show_gutter_icon) which will be used in start_sync_edit to set the active gutter icon
        # Update gutter icon before starting to ensure session.gutter_icon_line is set. This allows us to flip it to "Active" mode shortly after.
        self.update_gutter_icon_on_selection(ed_self)

        carets = ed_self.get_carets()
        if len(carets) != 1:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Need single caret'))
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
            return

        self.set_progress(3)
        session.dictionary = {}

        # Save coordinates and "Lock" the selection
        session.start_l, session.end_l = ed_self.get_sel_lines()
        session.selected = True

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

        # Force naive way if lexer is none or lexer is one of the text file types
        cur_lexer = ed_self.get_prop(PROP_LEXER_FILE)
        session.use_simple_naive_mode = ini_config.get_lexer_bool(cur_lexer, 'use_simple_naive_mode', USE_SIMPLE_NAIVE_MODE_DEFAULT)
        if cur_lexer == '':
            # If lexer is none, use simple naive mode
            session.use_simple_naive_mode = True
        if cur_lexer in NAIVE_LEXERS or session.use_simple_naive_mode:
            session.use_simple_naive_mode = True

        # Determine if we use specific lexer rules
        if cur_lexer in NON_STANDARD_LEXERS:
            # If it is non-standard lexer, change its behavior
            local_styles_default = NON_STANDARD_LEXERS[cur_lexer]
        else:
            local_styles_default = IDENTIFIER_STYLE_INCLUDE_DEFAULT
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
        ed_self.action(EDACTION_LEXER_SCAN, session.start_l) #API 1.0.289
        self.set_progress(40)

        # Find all occurences of regex, get all tokens in the selected range
        tokenlist = ed_self.get_token(TOKEN_LIST_SUB, session.start_l, session.end_l)
        # print("tokenlist",tokenlist)

        self.set_progress(45)

        # --- 3. Token Processing ---
        if not tokenlist and not session.use_simple_naive_mode:
            self.reset(ed_self)
            msg_status(_('Sync Editing: No syntax tokens found in selection'))
            self.set_progress(-1)
            restore_caret()
            return

        elif session.use_simple_naive_mode:
            # Naive Mode: Scan text purely by Regex, ignoring syntax context
            for y in range(session.start_l, session.end_l+1):
                cur_line = ed_self.get_text_line(y)
                for match in session.regex_identifier.finditer(cur_line):
                    # Create pseudo-token structure: ((x1, y1), (x2, y2), string, style)
                    token = ((match.start(), y), (match.end(), y), match.group(), 'id')
                    if match.group() in session.dictionary:
                        session.dictionary[match.group()].append(token)
                    else:
                        session.dictionary[match.group()] = [(token)]
        else:
            # Standard Mode: Filter tokens by Style (Variable, Function, etc.)
            for token in tokenlist:
                if not self.token_style_ok(ed_self, token['style']):
                    continue
                idd = token['str'].strip()
                if not session.case_sensitive:
                    idd = idd.lower()

                # Structure: ((x1, y1), (x2, y2), string, style)
                old_style_token = ((token['x1'], token['y1']), (token['x2'], token['y2']), token['str'], token['style'])

                if idd in session.dictionary:
                    if old_style_token not in session.dictionary[idd]:
                        session.dictionary[idd].append(old_style_token)
                else:
                    session.dictionary[idd] = [(old_style_token)]

        self.set_progress(60)
        self.fix_tokens(ed_self) # Clean up whitespace issues and remove singletons

        # Validation: Ensure we actually found words to edit. Exit if no id's (eg: comments and etc)
        if len(session.dictionary) == 0:
            self.reset(ed_self)
            msg_status(_('Sync Editing: No editable identifiers found in selection'))
            self.set_progress(-1)
            restore_caret()
            return

        self.set_progress(90)

        # --- 4. Apply Visual Markers ---
        # Visualize all editable identifiers in the selection. Mark all words that we can modify with pretty light color
        self.mark_all_words(ed_self)
        self.set_progress(-1)

        msg_status(_('Sync Editing: Click an ID to edit, click gutter icon or press Esc to exit.'))
        # restore caret but w/o selection
        restore_caret()

    # Fix tokens with spaces at the start of the line (eg: ((0, 50), (16, 50), '        original', 'Id')) and remove if it has 1 occurence (issue #44 and #45)
    def fix_tokens(self, ed_self):
        """
        Post-processing of tokens found by Lexer/Regex.
        1. Trims whitespace from the start of tokens (Lexers sometimes include leading space in ranges).
        2. Removes any identifiers that have fewer than 2 occurrences (cannot sync-edit a single word).
        """
        session = self.get_session(ed_self)
        new_replace = []

        # Pass 1: Trim Whitespace
        for key in session.dictionary:
            for key_tuple in session.dictionary[key]:
                token = key_tuple[2]
                # If token starts with space, calculate offset
                if token and token[0] != ' ':
                    continue  # Skip if no leading space (optimization)
                offset = 0
                for i in range(len(token)):
                    if token[i] != ' ':
                        offset = i
                        break
                # Create new trimmed token tuple
                new_token = token[offset:]
                new_start = key_tuple[0][0] + offset
                new_tuple = ((new_start, key_tuple[0][1]), key_tuple[1], new_token, key_tuple[3])
                new_replace.append([new_tuple, key_tuple])

        # Update the dictionary with corrected tokens (replacements)
        for neww in new_replace:
            for key in list(session.dictionary.keys()):  # Use list() to avoid runtime errors during iteration
                for i in range(len(session.dictionary[key])):
                    if session.dictionary[key][i] == neww[1]:
                        session.dictionary[key][i] = neww[0]

        # Pass 2: Remove Singletons
        # We delete keys that don't have duplicates because sync editing requires at least 2 instances.
        todelete = []
        for key in list(session.dictionary.keys()):
            if len(session.dictionary[key]) < 2:
                todelete.append(key)

        for dell in todelete:
            session.dictionary.pop(dell, None)

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
        for key in session.dictionary:
            # Generate unique color for every unique word
            color  = html_color_to_int(rand_color.generate(luminosity='light')[0])
            for key_tuple in session.dictionary[key]:
                ed_self.attr(MARKERS_ADD,
                    tag = MARKER_CODE,
                    x = key_tuple[0][0],
                    y = key_tuple[0][1],
                    len = key_tuple[1][0] - key_tuple[0][0],
                    color_font = 0xb000000, # this color is better than marker_fg_color especially with black themes because we use colored background
                    color_bg = color,
                    color_border = 0xb000000,
                    border_down = 1
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
            if session.regex_identifier:
                match = session.regex_identifier.match(current_line[start_x:]) if start_x <= len(current_line) else None
                if match:
                    dynamic_end = start_x + len(match.group(0))
                    if x0 <= dynamic_end:
                        return True

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

        # Add active carets and borders to ALL instances of the clicked word
        for key_tuple in session.dictionary[session.our_key]:
            ed_self.attr(MARKERS_ADD, tag = MARKER_CODE, \
            x = key_tuple[0][0], y = key_tuple[0][1], \
            len = key_tuple[1][0] - key_tuple[0][0], \
            color_font=session.marker_fg_color, \
            color_bg=session.marker_bg_color, \
            color_border=session.marker_border_color, \
            border_left=1, \
            border_right=1, \
            border_down=1, \
            border_up=1 \
            )
            # Add secondary caret at the corresponding offset
            ed_self.set_caret(key_tuple[0][0] + offset, key_tuple[0][1], id=CARET_ADD)

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

        if decorations:
            for decor in decorations:
                if decor.get('tag') == DECOR_TAG:
                    # User clicked on our sync edit icon
                    session = self.get_session(ed_self)

                    if session.selected or session.editing:
                        # If already in sync edit mode, exit
                        self.reset(ed_self)
                    else:
                        # Otherwise, start sync editing
                        self.start_sync_edit(ed_self)
                    return False  # Prevent default processing

        # Not our decoration, allow default processing
        return None

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
                return

            # NOTE: self.redraw(ed_self) is called here to update word markers live during typing.
            # This recalculates borders and shifts other tokens on the line as the word grows/shrinks. This is a performance hit on simple caret moves (arrow keys) but necessary for live updates.
            self.redraw(ed_self)

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

    def on_start2(self, ed_self):
        pass

    def redraw(self, ed_self):
        """
        Dynamically updates markers and dictionary positions during typing.
        Because editing changes the length of the word, we must:
        1. Find the new word string at the caret position.
        2. Update the dictionary entry for the currently edited word (start/end positions).
        3. Shift positions of ALL other words that exist on the same line after the caret.
        4. Re-draw all the borders.
        """
        session = self.get_session(ed_self)
        if not session.our_key:
            return

        # 1. Capture State. Find out what changed on the first caret (on others changes will be the same)
        old_key = session.our_key
        session.our_key = None # Temporarily unset to allow clean lookup

        # Get current state at the first caret
        first_y = ed_self.get_carets()[0][1]
        first_x = ed_self.get_carets()[0][0]
        first_y_line = ed_self.get_text_line(first_y)
        start_pos = first_x

        # Backtrack from caret to find start of the new word
        # Workaround for end of id case: If caret is at the very end, move back 1 to capture the match
        if not session.regex_identifier.match(first_y_line[start_pos:]):
            start_pos -= 1

        # Move start_pos back until we find the beginning of the identifier
        while session.regex_identifier.match(first_y_line[start_pos:]):
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
            return

        new_key = match.group(0)
        if not session.case_sensitive:
            new_key = new_key.lower()

        # 2. Calculate Length Delta change
        old_length = len(old_key)
        new_length = len(new_key)
        length_delta = new_length - old_length

        # Identify lines affected by this edit (where this word appears)
        affected_lines = set()
        old_key_dictionary = session.dictionary[old_key]
        for entry in old_key_dictionary:
            affected_lines.add(entry[0][1])  # y coordinate

        # 3. Rebuild Dictionary positions for the modified Active Word with new values
        existing_entries = session.dictionary.get(new_key, [])
        pointers = []
        for i in old_key_dictionary:
            pointers.append(i[0])

        # Recalculate start/end positions for all instances of the edited word
        for pointer in pointers:
            x = pointer[0]
            y = pointer[1]
            y_line = ed_self.get_text_line(y)

            # Scan backwards to find start of the new word instance. Find the new start X for this instance
            while session.regex_identifier.match(y_line[x:]):
                x -= 1
            x += 1
            # Workaround for EOL #65
            if x < 0:
                x = 0

            # Remove old position from target list if it exists (collision handling)
            existing_entries = [item for item in existing_entries if item[0] != (x, y)]
            # Add new position
            existing_entries.append(((x, y), (x+len(new_key), y), new_key, 'Id'))

        # Update dictionary keys for the edited word. Clean up old key if it changed completely
        if old_key != new_key:
            session.dictionary.pop(old_key, None)
        session.dictionary[new_key] = existing_entries

        # 4. Shift Other Words on the Same Line. Update positions of ALL other words on affected lines
        if length_delta != 0:
            for line_num in affected_lines:
                # For each edited position on this line, shift words that come after it
                # Find all X positions where the *edited* word sits on this line
                edited_positions = [pos[0][0] for pos in existing_entries if pos[0][1] == line_num]

                # Iterate over ALL other words in the dictionary
                for other_key in list(session.dictionary.keys()):
                    if other_key == new_key:
                        continue  # Skip the word we just edited

                    updated_entries = []
                    for entry in session.dictionary[other_key]:
                        if entry[0][1] == line_num:  # If this word is on the affected line. Same line
                            word_start_x = entry[0][0]
                            word_end_x = entry[1][0]

                            # Calculate total shift: Check if this word comes after any of the edited positions
                            # If this word is to the right of 3 edited instances, it moves 3 * delta.
                            shift_amount = 0
                            for edit_x in sorted(edited_positions):
                                if word_start_x > edit_x:
                                    shift_amount += length_delta

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

        session.our_key = new_key

        # 5. Repaint borders for ALL words
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)

        # Draw active borders for the currently edited word
        for key_tuple in session.dictionary[session.our_key]:
                ed_self.attr(MARKERS_ADD, tag = MARKER_CODE, \
                x = key_tuple[0][0], y = key_tuple[0][1], \
                len = key_tuple[1][0] - key_tuple[0][0], \
                color_font=session.marker_fg_color, \
                color_bg=session.marker_bg_color, \
                color_border=session.marker_border_color, \
                border_left=1, \
                border_right=1, \
                border_down=1, \
                border_up=1 \
                )

    def config(self):
        """Opens the plugin configuration INI file."""
        try:
            ini_config = PluginConfig()
            file_open(ini_config.file_path)
        except Exception as ex:
            msg_status(_('Cannot open config: ') + str(ex))
