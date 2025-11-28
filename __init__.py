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
# 1. Activation: User selects text and a gutter icon appears on the last line of selection
# 2. User clicks the gutter icon to start sync editing
# 3. Analysis: The plugin scans for identifiers (variables, etc.) and highlights them with background colors.
# 4. Interaction Loop (Circular State Machine):
#    - [Selection State]: User sees highlighted words. Clicking a word triggers [Edit State].
#    - [Edit State]: Multi-carets are placed. User types. 
#      -> If user clicks another word: Previous edit commits, new edit starts immediately.
#      -> If user moves caret off-word: Edit commits, returns to [Selection State].
# 5. Exit: Clicking the gutter icon again or pressing 'ESC' fully terminates the session.


# --- Default Configuration ---
USE_COLORS_DEFAULT = True
USE_SIMPLE_NAIVE_MODE_DEFAULT = False
CASE_SENSITIVE_DEFAULT = True
FIND_REGEX_DEFAULT = r'\w+'
# FIND_REGEX_DEFAULT = r'\b\w+\b'
# Regex to identify valid tokens (identifiers) vs invalid ones
STYLES_DEFAULT = r'(?i)id[\w\s]*'       # Styles that are considered "Identifiers"
STYLES_NO_DEFAULT = '(?i).*keyword.*'   # Styles that are strictly keywords (should not be edited)

USE_COLORS = USE_COLORS_DEFAULT
USE_SIMPLE_NAIVE_MODE = USE_SIMPLE_NAIVE_MODE_DEFAULT
CASE_SENSITIVE = CASE_SENSITIVE_DEFAULT
FIND_REGEX = FIND_REGEX_DEFAULT
STYLES = STYLES_DEFAULT
STYLES_NO = STYLES_NO_DEFAULT

# Visual settings for the markers
MARKER_BG_COLOR = 0xFFAAAA
MARKER_F_COLOR  = 0x005555
MARKER_BORDER_COLOR = 0xFF0000

CONFIG_FILENAME = 'cuda_sync_editing.ini'

# Overrides for specific lexers that have unique naming conventions
NON_STANDART_LEXERS = {
  'HTML': 'Text|Tag id correct|Tag prop',
  'PHP': 'Var',
}
  
# Lexers where we skip syntax parsing and just use Regex (Naive mode)
NAIVE_LEXERS = [
  'Markdown', # it has 'Text' rule for many chars, including punctuation+spaces
  'reStructuredText',
  'Textile',
  'ToDo',
  'Todo.txt',
  'JSON',
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
    'id_regex': FIND_REGEX_DEFAULT,
    'id_styles': STYLES_DEFAULT,
    'id_styles_no': STYLES_NO_DEFAULT,
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
          1) If the value exists in the per-lexer section, return it
          2) Else return value from [global] if present
          3) Else return None
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
    Represents a single sync edit session for one file.
    Each file can have only one active session.
    """
    def __init__(self):
        self.start = None
        self.end = None
        self.selected = False 
        self.editing = False
        self.dictionary = {} # Stores mapping of { "word_string": [list_of_token_positions] }
        self.our_key = None  # The specific word currently being edited
        self.original = None # Original caret position before editing
        self.start_l = None  # Start line of selection
        self.end_l = None    # End line of selection
        self.saved_sel = None
        self.pattern = None
        self.pattern_styles = None
        self.pattern_styles_no = None
        self.naive_mode = False
        self.gutter_icon_line = None  # Line where gutter icon is displayed
        self.gutter_icon_active = False  # Whether gutter icon is currently shown
        self.offset = None


class Command:
    """
    Main Logic for Sync Editing.
    Manages the Circular State Machine: Selection <-> Editing.
    Can be toggled via gutter icon or command.
    NOW SUPPORTS MULTIPLE FILES - one session per file.
    OPTIMIZED: Does nothing when not in use to save resources.
    """

    def __init__(self):
        """Initializes plugin state."""
        # Dictionary to store sessions: {editor_handle: SyncEditSession}
        self.sessions = {}

    def get_editor_handle(self, ed_self):
        """Returns a unique identifier for the editor."""
        return ed_self.get_prop(PROP_HANDLE_SELF)

    def get_session(self, ed_self):
        """Gets or creates a session for the current editor."""
        handle = self.get_editor_handle(ed_self)
        if handle not in self.sessions:
            print("============creates a session:",handle)
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
        """Removes the session for the current editor."""
        handle = self.get_editor_handle(ed_self)
        if handle in self.sessions:
            del self.sessions[handle]

    def is_sync_active(self, ed_self):
        """Check if sync edit is active using PROP_TAG (lightweight check)."""
        return ed_self.get_prop(PROP_TAG, 'cuda_sync_editing:undefined') == 'active'

    def show_gutter_icon(self, ed_self, line_index, active=False):
        """Shows the gutter icon at the specified line."""
        # Remove any existing gutter icon
        self.hide_gutter_icon(ed_self)
        
        # Choose color based on active state
        color = 0x0000AA if active else 0x00AA00  # Red when active, green when inactive
        
        ed_self.decor(DECOR_SET, line=line_index, tag=DECOR_TAG, text="≡", color=color, bold=True, italic=False, image=-1, auto_del=False)
        
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

    def token_style_ok(self, ed_self, s):
        """Checks if a token's style matches the allowed patterns (IDs) and rejects Keywords."""
        session = self.get_session(ed_self)
        good = session.pattern_styles.fullmatch(s)
        bad = session.pattern_styles_no.fullmatch(s)
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
        
        All configuration is read fresh from file/theme on every start so do not need to restart.
        """
        session = self.get_session(ed_self)
        # now that we created a session we should always call update_gutter_icon_on_selection before start_sync_edit to set gutter_icon_line (set by show_gutter_icon) which will be used in start_sync_edit to set the red/active gutter icon
        self.update_gutter_icon_on_selection(ed_self)
        
        # --- Declare all globals that need fresh values ---
        global USE_COLORS
        global CASE_SENSITIVE
        global FIND_REGEX
        global STYLES
        global STYLES_DEFAULT
        global STYLES_NO
        global STYLES_NO_DEFAULT
        global MARKER_F_COLOR
        global MARKER_BG_COLOR
        global MARKER_BORDER_COLOR
        
        carets = ed_self.get_carets()
        if len(carets)!=1:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Need single caret'))
            return
        caret = carets[0]

        def restore_caret():
            ed_self.set_caret(caret[0], caret[1])

        original = ed_self.get_text_sel()
        
        # --- 1. Selection Handling ---
        # Check if we have selection of text
        if not original and session.saved_sel is None:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Make selection first'))
            return
        
        self.set_progress(3)
        session.dictionary = {}
        
        # If we are resuming a session or starting new
        if session.saved_sel is not None:
            session.start_l, session.end_l = session.saved_sel
            session.selected = True
        else:
            # Save coordinates and "Lock" the selection
            session.start_l, session.end_l = ed_self.get_sel_lines()
            session.selected = True
            # Save text selection
            session.saved_sel = ed_self.get_sel_lines()
            # Break text selection
            ed_self.set_sel_rect(0,0,0,0) # Clear visual selection to show markers instead
        # Mark text that was selected
        self.set_progress(5)
        
        # Update gutter icon to show active state (change color to red)
        if session.gutter_icon_line is not None:
            self.show_gutter_icon(ed_self, session.gutter_icon_line, active=True)
        
        # Mark the range properties for CudaText
        ed_self.set_prop(PROP_MARKED_RANGE, (session.start_l, session.end_l))
        ed_self.set_prop(PROP_TAG, 'cuda_sync_editing:active') # Tag editor state as 'sync active'


        # --- 2. Lexer / Parser Configuration ---
        # Go naive way if lexer id none or other text file
        cur_lexer = ed_self.get_prop(PROP_LEXER_FILE)
        
        # Determine if we use specific lexer rules or "Naive" mode
        if cur_lexer in NON_STANDART_LEXERS:
            # If it if non-standart lexer, change it's behaviour
            STYLES_DEFAULT = NON_STANDART_LEXERS[cur_lexer]
        elif cur_lexer == '':
            # If lexer is none, go very naive way
            session.naive_mode = True
        
        # Instantiate config to get fresh values from disk on every session
        ini_config = PluginConfig()
        USE_SIMPLE_NAIVE_MODE = ini_config.get_lexer_bool(cur_lexer, 'use_simple_naive_mode', USE_SIMPLE_NAIVE_MODE_DEFAULT)

        if cur_lexer in NAIVE_LEXERS or USE_SIMPLE_NAIVE_MODE:
            session.naive_mode = True
        
        # Load Lexer/Global Configs
        USE_COLORS = ini_config.get_lexer_bool(cur_lexer, 'use_colors', USE_COLORS_DEFAULT)
        CASE_SENSITIVE = ini_config.get_lexer_bool(cur_lexer, 'case_sensitive', CASE_SENSITIVE_DEFAULT)
        FIND_REGEX = ini_config.get_lexer_str(cur_lexer, 'id_regex', FIND_REGEX_DEFAULT)
        STYLES = ini_config.get_lexer_str(cur_lexer, 'id_styles', STYLES_DEFAULT)
        STYLES_NO = ini_config.get_lexer_str(cur_lexer, 'id_styles_no', STYLES_NO_DEFAULT)

        # Set colors based on theme 'Id' and 'SectionBG4' styles
        MARKER_F_COLOR = theme_color('Id', True)
        MARKER_BG_COLOR = theme_color('SectionBG4', False)
        MARKER_BORDER_COLOR = MARKER_F_COLOR
        
        # Compile regex
        session.pattern = re.compile(FIND_REGEX)
        session.pattern_styles = re.compile(STYLES)
        session.pattern_styles_no = re.compile(STYLES_NO)
        # Run lexer scan form start
        
        # self.set_progress(10) # do not use this here before ed.action(EDACTION_LEXER_SCAN. see bug: https://github.com/Alexey-T/CudaText/issues/6120 the bug happen only with this line. Alexey said: app_idle is the main reason, it is bad to insert it before some parsing action. Usually app_idle is needed after some action, to run the app message processing. Not before. Dont use it if not nessesary...
        
        # Force a Lexer scan to ensure tokens are up to date
        ed_self.action(EDACTION_LEXER_SCAN, session.start_l) #API 1.0.289
        self.set_progress(40)
        
        # Find all occurences of regex
        # Get all tokens in the selected range
        tokenlist = ed_self.get_token(TOKEN_LIST_SUB, session.start_l, session.end_l)
        # print("tokenlist",tokenlist)
        
        self.set_progress(45)
        
        # --- 3. Token Processing ---
        if not tokenlist and not session.naive_mode:
            self.reset(ed_self)
            msg_status(_('Sync Editing: No syntax tokens found in selection'))
            self.set_progress(-1)
            restore_caret()
            return
            
        elif session.naive_mode:
            # Naive filling
            # Naive Mode: Scan text purely by Regex, ignoring syntax context
            for y in range(session.start_l, session.end_l+1):
                cur_line = ed_self.get_text_line(y)
                for match in session.pattern.finditer(cur_line):
                    # Create pseudo-token structure
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
                if not CASE_SENSITIVE:
                    idd = idd.lower()
                
                # Structure: ((x1, y1), (x2, y2), string, style)
                old_style_token = ((token['x1'], token['y1']), (token['x2'], token['y2']), token['str'], token['style'])
                
                if idd in session.dictionary:
                    if old_style_token not in session.dictionary[idd]:
                        session.dictionary[idd].append(old_style_token)
                else:
                    session.dictionary[idd] = [(old_style_token)]
        # Fix tokens
        self.set_progress(60)
        self.fix_tokens(ed_self) # Clean up whitespace issues
        # Exit if no id's (eg: comments and etc)
        
        # Validation: Ensure we actually found words to edit
        if len(session.dictionary) == 0:
            self.reset(ed_self)
            msg_status(_('Sync Editing: No editable identifiers found in selection'))
            self.set_progress(-1)
            restore_caret()
            return
                    
        # TODO: this is a dead code:
        # this condition can never evaluate to True in practice. The fix_tokens method (called just before this check) explicitly removes any dictionary entries where a word has fewer than 2 occurrences. this means that after fix_tokens runs:
        # - If the dictionary ends up empty (e.g., because the only word had just 1 occurrence and got removed), the preceding if len(sess['dictionary']) == 0 block handles it.
        # - If the dictionary has entries, each must have at least 2 occurrences (otherwise they'd be removed).
        # Therefore it is impossible to reach a state where there's exactly 1 unique word and it has exactly 1 occurrence—the removal logic prevents this. The elif is effectively dead code and could be removed without changing behavior.
        # why the previos author add it? anyway it does not heart so i will leave it as is for now

        # Issue #44: If only 1 instance of a word exists, there is nothing to sync-edit so we exit
        elif len(session.dictionary) == 1 and len(session.dictionary[list(session.dictionary.keys())[0]]) == 1:
            self.reset(ed_self)
            msg_status(_('Sync Editing: Aborted. The found identifier appears only once in the selection.'))
            self.set_progress(-1)
            restore_caret()
            return
            
        self.set_progress(90)
        
        # --- 4. Apply Visual Markers ---
        # Mark all words that we can modify with pretty light color
        self.mark_all_words(ed_self)
        self.set_progress(-1)
        
        msg_status(_('Sync Editing: Click an ID to edit, click gutter icon or press Esc to exit.'))
        # restore caret but w/o selection
        restore_caret()

    # Fix tokens with spaces at the start of the line (eg: ((0, 50), (16, 50), '        original', 'Id')) and remove if it has 1 occurence (issue #44 and #45)
    def fix_tokens(self, ed_self):
        """
        Trims whitespace from the start of tokens. 
        Corrects issues where the lexer includes leading spaces in the token range.
        Then removes any groups with fewer than 2 occurrences.
        """
        session = self.get_session(ed_self)
        new_replace = []
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
            for key in list(session.dictionary.keys()):  # Use list() to avoid runtime errors during iteration if dict changes size
                for i in range(len(session.dictionary[key])):
                    if session.dictionary[key][i] == neww[1]:
                        session.dictionary[key][i] = neww[0]
        
        # Now, separately remove entries that don't have duplicates (always run this, even if no replacements)
        todelete = []
        for key in list(session.dictionary.keys()):
            if len(session.dictionary[key]) < 2:
                todelete.append(key)
        
        # Remove entries that don't have duplicates
        for dell in todelete:
            session.dictionary.pop(dell, None)

    # Set progress (issue #46)
    def set_progress(self, prg):
        """Updates the CudaText status bar progress."""
        app_proc(PROC_PROGRESSBAR, prg)
        app_idle()

    def mark_all_words(self, ed_self):
        """
        Visualizes all editable identifiers in the selection.
        Used during initialization and when returning to selection mode after an edit.
        """
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        if not USE_COLORS:
            return
        rand_color = randomcolor.RandomColor()
        session = self.get_session(ed_self)
        for key in session.dictionary:
            # Generate unique color for every unique word
            color  = html_color_to_int(rand_color.generate(luminosity='light')[0])
            for key_tuple in session.dictionary[key]:
                ed_self.attr(MARKERS_ADD,
                    tag = MARKER_CODE,
                    x = key_tuple[0][0],
                    y = key_tuple[0][1],
                    len = key_tuple[1][0] - key_tuple[0][0],
                    color_font = 0xb000000,
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

            # Allow caret to stay considered "inside" while the token is being grown
            if session.pattern:
                match = session.pattern.match(current_line[start_x:]) if start_x <= len(current_line) else None
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
        ed_self.set_prop(PROP_TAG, 'cuda_sync_editing:inactive') # Tag editor state as 'sync inactive'
        
        # Hide gutter icon
        self.hide_gutter_icon(ed_self)
        
        # Remove the session
        self.remove_session(ed_self)
        
        msg_status(_('Sync Editing: Cancelled'))

    def doclick(self, ed_self=None):
        """API Hook for Mouse Click events."""
        if ed_self is None:
            ed_self = ed
        # state = app_proc(PROC_GET_KEYSTATE, '')
        state = ''
        return self.on_click(ed_self, state)

    def on_click(self, ed_self, state):
        """
        Handles mouse clicks to toggle between 'Viewing' and 'Editing'.
        Logic:
        1. If Editing -> Finish current edit (Loop back to Selection).
        2. If Selection -> Check if click is on valid ID.
           - Yes: Start Editing (Add carets, borders).
           - No: Do nothing (Do not exit).
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        # if not ed_self.get_prop(PROP_TAG, 'cuda_sync_editing:undefined') == 'active':
        if not self.is_sync_active(ed_self):
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
                    
        # If click was NOT on a valid word
        # Not editing - in selection mode
        if not clicked_key:
            msg_status(_('Sync Editing: Not a word! Click on ID to edit it.'))
            return
            
        # --- Start Editing Sequence ---
        # Clear passive markers
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        session.our_key = clicked_key
        session.offset = offset
        session.original = (caret[0], caret[1])
        
        # Add active carets and borders
        for key_tuple in session.dictionary[session.our_key]:
            ed_self.attr(MARKERS_ADD, tag = MARKER_CODE, \
            x = key_tuple[0][0], y = key_tuple[0][1], \
            len = key_tuple[1][0] - key_tuple[0][0], \
            color_font=MARKER_F_COLOR, \
            color_bg=MARKER_BG_COLOR, \
            color_border=MARKER_BORDER_COLOR, \
            border_left=1, \
            border_right=1, \
            border_down=1, \
            border_up=1 \
            )
            ed_self.set_caret(key_tuple[0][0] + session.offset, key_tuple[0][1], id=CARET_ADD)
        
        # Update state
        session.selected = False
        session.editing = True
        
        # Track bounds
        first_caret = ed_self.get_carets()[0]
        session.start = first_caret[1]
        session.end = first_caret[3]
        if session.start > session.end and not session.end == -1:
            session.start, session.end = session.end, session.start

    def on_click_gutter(self, ed_self, state, nline, nband):
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

    def update_gutter_icon_on_selection(self, ed_self):
        """
        Called when selection changes. Shows gutter icon if there's a valid selection.
        """
        # Check if we have a selection
        x0, y0, x1, y1 = ed_self.get_carets()[0]
        if y1 >= 0 and (y0 != y1 or x0 != x1):  # Has selection
            # Show icon at the last line of selection
            last_line = max(y0, y1)
            self.show_gutter_icon(ed_self, last_line)
        else:
            # No selection, hide icon if not in active sync edit mode
            if self.has_session(ed_self):
                session = self.get_session(ed_self)
                if not session.selected and not session.editing:
                    self.hide_gutter_icon(ed_self)
            else:
                self.hide_gutter_icon(ed_self)

    def on_caret(self, ed_self):
    # on_caret_slow is better because it will consume less resources but it breaks the colors recalculations when user edit an ID, so stick with on_caret
        """
        Hooks into caret movement.
        Continuous Edit Logic:
        If the user moves the caret OUTSIDE the active word, we do NOT exit, we check if landing on another valid ID.
        - If landing on valid ID: Do nothing (let on_click handle the switch)
        - If landing elsewhere: We simply 'finish' the edit and return to Selection mode and show colors
        
        Also handles showing/hiding gutter icon based on selection.
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        # TODO: which one is faster
        if not self.has_session(ed_self):
        # if not self.is_sync_active(ed_self):
        # if not ed_self.get_prop(PROP_TAG, 'cuda_sync_editing:undefined') == 'active':
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
                    # Clicked on a valid ID - switch directly without showing colors, this prevent showing colors for an instant when i switch from an ID to another ID
                else:
                    self.finish_editing(ed_self)
                return
            self.redraw(ed_self)

    def on_key(self, ed_self, key, state):
        """
        Handles Esc Keyboard input to cancel sync editing.
        Strict Exit Logic:
        Only VK_ESCAPE triggers the full 'reset' (Exit).
        """
        # OPTIMIZATION: exit early if sync edit mode is not active
        # if not ed_self.get_prop(PROP_TAG, 'cuda_sync_editing:undefined') == 'active':
        if not self.is_sync_active(ed_self):
            return
            
        if key == VK_ESCAPE:
            self.reset(ed_self)
            return False

    def on_start2(self, ed_self):
        pass

    # Redraws Id's borders
    def redraw(self, ed_self):
        # Simple workaround to prevent redraw while redraw
        """
        Dynamically updates markers during typing.
        Because editing changes the length of the word, we must:
        1. Find the new word at the caret.
        2. Re-scan the document (dictionary) for that specific word.
        3. Update positions of ALL words that come after the edit on the same line.
        4. Re-draw all the borders.
        """
        session = self.get_session(ed_self)
        if not session.our_key:
            return
        
        # Find out what changed on the first caret (on others changes will be the same)
        old_key = session.our_key
        session.our_key = None
        
        # Get current state at the first caret
        first_y = ed_self.get_carets()[0][1]
        first_x = ed_self.get_carets()[0][0]
        first_y_line = ed_self.get_text_line(first_y)
        start_pos = first_x
        
        # Backtrack to find start of the word
        # Workaround for end of id case
        if not session.pattern.match(first_y_line[start_pos:]):
            start_pos -= 1
        while session.pattern.match(first_y_line[start_pos:]):
            start_pos -= 1
        start_pos += 1
        # Workaround for EOL #65
        if start_pos < 0:
            start_pos = 0
        
        # Check if word became empty (deleted)
        # Workaround for empty id (eg. when it was deleted) #62
        if not session.pattern.match(first_y_line[start_pos:]):
            session.our_key = old_key
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
            return
            
        new_key = session.pattern.match(first_y_line[start_pos:]).group(0)
        if not CASE_SENSITIVE:
            new_key = new_key.lower()
        
        # Calculate the length change
        old_length = len(old_key)
        new_length = len(new_key)
        length_delta = new_length - old_length
        
        # Get the list of affected lines (lines where we edited)
        affected_lines = set()
        old_key_dictionary = session.dictionary[old_key]
        for entry in old_key_dictionary:
            affected_lines.add(entry[0][1])  # y coordinate
        
        # Rebuild dictionary positions for the modified word with new values
        existing_entries = session.dictionary.get(new_key, [])
        pointers = []
        for i in old_key_dictionary:
            pointers.append(i[0])
            
        # Recalculate positions for all instances of the edited word
        for pointer in pointers:
            x = pointer[0]
            y = pointer[1]
            y_line = ed_self.get_text_line(y)
            # Scan backwards to find start of the new word instance
            while session.pattern.match(y_line[x:]):
                x -= 1
            x += 1
            # Workaround for EOL #65
            if x < 0:
                x = 0
            existing_entries = [item for item in existing_entries if item[0] != (x, y)]
            existing_entries.append(((x, y), (x+len(new_key), y), new_key, 'Id'))
        
        # Update dictionary keys for the edited word
        if old_key != new_key:
            session.dictionary.pop(old_key, None)
        session.dictionary[new_key] = existing_entries
        
        # Update positions of ALL other words on affected lines
        if length_delta != 0:
            for line_num in affected_lines:
                # For each edited position on this line, shift words that come after it
                edited_positions = [pos[0][0] for pos in existing_entries if pos[0][1] == line_num]
                
                for other_key in list(session.dictionary.keys()):
                    if other_key == new_key:
                        continue  # Skip the word we just edited
                    
                    updated_entries = []
                    for entry in session.dictionary[other_key]:
                        if entry[0][1] == line_num:  # Same line
                            word_start_x = entry[0][0]
                            word_end_x = entry[1][0]
                            
                            # Check if this word comes after any of the edited positions
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
        
        # Repaint borders for ALL words
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        
        # Draw borders for the currently edited word
        for key_tuple in session.dictionary[session.our_key]:
                ed_self.attr(MARKERS_ADD, tag = MARKER_CODE, \
                x = key_tuple[0][0], y = key_tuple[0][1], \
                len = key_tuple[1][0] - key_tuple[0][0], \
                color_font=MARKER_F_COLOR, \
                color_bg=MARKER_BG_COLOR, \
                color_border=MARKER_BORDER_COLOR, \
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
