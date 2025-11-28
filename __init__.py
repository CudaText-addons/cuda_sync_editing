# Sync Editing plugin for CudaText
# by Vladislav Utkin <vlad@teamfnd.ru>
# MIT License
# 2018

import re
import os
from . import randomcolor
from cudatext import *
from cudatext_keys import *
from cudax_lib import html_color_to_int, get_opt, set_opt, CONFIG_LEV_USER, CONFIG_LEV_LEX

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

# Generate a unique integer tag for this plugin's markers to avoid conflicts with other plugins
# Uniq value for all marker plugins
MARKER_CODE = app_proc(PROC_GET_UNIQUE_TAG, '') 
DECOR_TAG = app_proc(PROC_GET_UNIQUE_TAG, '')  # Unique tag for gutter decorations

# --- Default Configuration ---
CASE_SENSITIVE = True
FIND_REGEX_DEFAULT = r'\w+'
FIND_REGEX = FIND_REGEX_DEFAULT

# Regex to identify valid tokens (identifiers) vs invalid ones
STYLES_DEFAULT = r'(?i)id[\w\s]*'       # Styles that are considered "Identifiers"
STYLES_NO_DEFAULT = '(?i).*keyword.*'   # Styles that are strictly keywords (should not be edited)
STYLES = STYLES_DEFAULT
STYLES_NO = STYLES_NO_DEFAULT

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

# Visual settings for the markers
MARKER_BG_COLOR = 0xFFAAAA
MARKER_F_COLOR  = 0x005555
MARKER_BORDER_COLOR = 0xFF0000
MARK_COLORS = True
ASK_TO_EXIT = True

# Load current IDE theme colors
theme = app_proc(PROC_THEME_SYNTAX_DICT_GET, '')

def theme_color(name, is_font):
    """Retrieves color from the current CudaText theme."""
    if name in theme:
        return theme[name]['color_font' if is_font else 'color_back']
    return 0x808080

class Command:
    """
    Main Logic for Sync Editing.
    Manages the Circular State Machine: Selection <-> Editing.
    Can be toggled via gutter icon or command.
    """
    start = None
    end = None
    selected = False 
    editing = False
    dictionary = {} # Stores mapping of { "word_string": [list_of_token_positions] }
    our_key = None  # The specific word currently being edited
    original = None # Original caret position before editing
    start_l = None  # Start line of selection
    end_l = None    # End line of selection
    saved_sel = None
    pattern = None
    pattern_styles = None
    pattern_styles_no = None
    naive_mode = False
    gutter_icon_line = None  # Line where gutter icon is displayed
    gutter_icon_active = False  # Whether gutter icon is currently shown
    
    
    def __init__(self):
        """Initializes plugin, loads theme colors and user options."""
        global MARKER_F_COLOR
        global MARKER_BG_COLOR
        global MARKER_BORDER_COLOR
        global MARK_COLORS
        global ASK_TO_EXIT
        
        # Set colors based on theme 'Id' and 'SectionBG4' styles
        MARKER_F_COLOR = theme_color('Id', True)
        MARKER_BG_COLOR = theme_color('SectionBG4', False)
        MARKER_BORDER_COLOR = MARKER_F_COLOR
        
        # Load user preferences from user.json
        ASK_TO_EXIT = get_opt('syncedit_ask_to_exit', True, lev=CONFIG_LEV_USER)
        MARK_COLORS = get_opt('syncedit_mark_words', True, lev=CONFIG_LEV_USER)


    def show_gutter_icon(self, line_index, active=False):
        """Shows the gutter icon at the specified line."""
        # Remove any existing gutter icon
        self.hide_gutter_icon()
        
        # Choose color based on active state
        color = 0x0000AA if active else 0x00AA00  # Red when active, green when inactive
        
        ed.decor(DECOR_SET, line=line_index, tag=DECOR_TAG, text="≡", color=color, bold=True, italic=False, image=-1, auto_del=False)
        
        self.gutter_icon_line = line_index
        self.gutter_icon_active = True
    
    
    def hide_gutter_icon(self):
        """Removes the gutter icon."""
        if self.gutter_icon_active:
            ed.decor(DECOR_DELETE_BY_TAG, -1, DECOR_TAG)
            self.gutter_icon_line = None
            self.gutter_icon_active = False
    
    
    def update_gutter_icon_on_selection(self):
        """
        Called when selection changes. Shows gutter icon if there's a valid selection.
        """
        # Check if we have a selection
        x0, y0, x1, y1 = ed.get_carets()[0]
        if y1 >= 0 and (y0 != y1 or x0 != x1):  # Has selection
            # Show icon at the last line of selection
            last_line = max(y0, y1)
            self.show_gutter_icon(last_line)
        else:
            # No selection, hide icon if not in active sync edit mode
            if not self.selected and not self.editing:
                self.hide_gutter_icon()
    
    
    def token_style_ok(self, s):
        """Checks if a token's style matches the allowed patterns (IDs) and rejects Keywords."""
        good = self.pattern_styles.fullmatch(s)
        bad = self.pattern_styles_no.fullmatch(s)
        return good and not bad
         

    def toggle(self):
        """
        Main Entry Point - can be called from command or gutter click.
        If already active, exits. Otherwise starts sync editing.
        """
        # If already in sync edit mode, exit
        if self.selected or self.editing:
            self.reset()
            return
        
        # Otherwise, start sync editing
        self.start_sync_edit()
    
    
    def start_sync_edit(self):
        """
        Starts sync editing session.
        1. Validates selection.
        2. Scans text (via Lexer or Regex).
        3. Groups identical words.
        4. Applies visual markers (colors).
        """
        global FIND_REGEX
        global CASE_SENSITIVE
        global STYLES_DEFAULT
        global STYLES_NO_DEFAULT
        global STYLES
        global STYLES_NO
        
        carets = ed.get_carets()
        if len(carets)!=1:
            msg_status(_('Sync Editing: Need single caret'))
            return
        caret = carets[0]

        def restore_caret():
            ed.set_caret(caret[0], caret[1])

        original = ed.get_text_sel()
        
        # --- 1. Selection Handling ---
        # Check if we have selection of text
        if not original and self.saved_sel is None:
            msg_status(_('Sync Editing: Make selection first'))
            return
        
        self.set_progress(3)
        self.dictionary = {}
        
        # If we are resuming a session or starting new
        if self.saved_sel is not None:
            self.start_l, self.end_l = self.saved_sel
            self.selected = True
        else:
            # Save coordinates and "Lock" the selection
            self.start_l, self.end_l = ed.get_sel_lines()
            self.selected = True
            # Save text selection
            self.saved_sel = ed.get_sel_lines()
            # Break text selection
            ed.set_sel_rect(0,0,0,0) # Clear visual selection to show markers instead
        # Mark text that was selected
        self.set_progress(5)
        
        # Update gutter icon to show active state (change color to red)
        if self.gutter_icon_line is not None:
            self.show_gutter_icon(self.gutter_icon_line, active=True)
        
        # Mark the range properties for CudaText
        ed.set_prop(PROP_MARKED_RANGE, (self.start_l, self.end_l))
        ed.set_prop(PROP_TAG, 'sync_edit:1') # Tag editor state as 'sync active'

        # --- 2. Lexer / Parser Configuration ---
        # Go naive way if lexer id none or other text file
        cur_lexer = ed.get_prop(PROP_LEXER_FILE)
        
        # Determine if we use specific lexer rules or "Naive" mode
        if cur_lexer in NON_STANDART_LEXERS:
            # If it if non-standart lexer, change it's behaviour
            STYLES_DEFAULT = NON_STANDART_LEXERS[cur_lexer]
        elif cur_lexer == '':
            # If lexer is none, go very naive way
            self.naive_mode = True
        
        if cur_lexer in NAIVE_LEXERS or get_opt('syncedit_naive_mode', False, lev=CONFIG_LEV_LEX):
            self.naive_mode = True
        # Load lexer config
        CASE_SENSITIVE = get_opt('case_sens', True, lev=CONFIG_LEV_LEX)
        FIND_REGEX = get_opt('id_regex', FIND_REGEX_DEFAULT, lev=CONFIG_LEV_LEX)
        STYLES = get_opt('id_styles', STYLES_DEFAULT, lev=CONFIG_LEV_LEX)
        STYLES_NO = get_opt('id_styles_no', STYLES_NO_DEFAULT, lev=CONFIG_LEV_LEX)
        # Compile regex
        self.pattern = re.compile(FIND_REGEX)
        self.pattern_styles = re.compile(STYLES)
        self.pattern_styles_no = re.compile(STYLES_NO)
        # Run lexer scan form start
        #self.set_progress(10)
        
        # Force a Lexer scan to ensure tokens are up to date
        ed.action(EDACTION_LEXER_SCAN, self.start_l) #API 1.0.289
        self.set_progress(40)
        
        # Find all occurences of regex
        # Get all tokens in the selected range
        tokenlist = ed.get_token(TOKEN_LIST_SUB, self.start_l, self.end_l)
        #print(tokenlist)
        
        # --- 3. Token Processing ---
        if not tokenlist and not self.naive_mode:
            self.reset()
            self.saved_sel = None
            msg_status(_('Sync Editing: Cannot find IDs in selection'))
            self.set_progress(-1)
            restore_caret()
            return
            
        elif self.naive_mode:
            # Naive filling
            # Naive Mode: Scan text purely by Regex, ignoring syntax context
            for y in range(self.start_l, self.end_l+1):
                cur_line = ed.get_text_line(y)
                for match in self.pattern.finditer(cur_line):
                    # Create pseudo-token structure
                    token = ((match.start(), y), (match.end(), y), match.group(), 'id')
                    if match.group() in self.dictionary:
                        self.dictionary[match.group()].append(token)
                    else:
                        self.dictionary[match.group()] = [(token)]
        else:
            # Standard Mode: Filter tokens by Style (Variable, Function, etc.)
            for token in tokenlist:
                if not self.token_style_ok(token['style']):
                    continue
                idd = token['str'].strip()
                if not CASE_SENSITIVE:
                    idd = idd.lower()
                
                # Structure: ((x1, y1), (x2, y2), string, style)
                old_style_token = ((token['x1'], token['y1']), (token['x2'], token['y2']), token['str'], token['style'])
                
                if idd in self.dictionary:
                    if old_style_token not in self.dictionary[idd]:
                        self.dictionary[idd].append(old_style_token)
                else:
                    self.dictionary[idd] = [(old_style_token)]
        # Fix tokens
        self.set_progress(60)
        self.fix_tokens() # Clean up whitespace issues
        # Exit if no id's (eg: comments and etc)
        
        # Validation: Ensure we actually found words to edit
        if len(self.dictionary) == 0:
            self.reset()
            self.saved_sel = None
            msg_status(_('Sync Editing: Cannot find IDs in selection'))
            self.set_progress(-1)
            restore_caret()
            return
        # Issue #44: If only 1 instance of a word exists, there is nothing to sync-edit so we exit
        elif len(self.dictionary) == 1 and len(self.dictionary[list(self.dictionary.keys())[0]]) == 1:
            self.reset()
            self.saved_sel = None
            msg_status(_('Sync Editing: Need several IDs in selection'))
            self.set_progress(-1)
            restore_caret()
            return
            
        self.set_progress(90)
        
        # --- 4. Apply Visual Markers ---
        # Mark all words that we can modify with pretty light color
        self.mark_all_words(ed)
        self.set_progress(-1)
        
        msg_status(_('Sync Editing: Click an ID to edit, click gutter icon or press Esc to exit.'))
        # restore caret but w/o selection
        restore_caret()
        
        
    # Fix tokens with spaces at the start of the line (eg: ((0, 50), (16, 50), '        original', 'Id')) and remove if it has 1 occurence (issue #44 and #45)
    def fix_tokens(self):
        """
        Trims whitespace from the start of tokens. 
        Corrects issues where the lexer includes leading spaces in the token range.
        """
        new_replace = []
        for key in self.dictionary:
            for key_tuple in self.dictionary[key]:
                token = key_tuple[2]
                # If token starts with space, calculate offset
                if token[0] != ' ':
                    continue
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
        
        # Update the dictionary with corrected tokens
        todelete = []
        for neww in new_replace:
            for key in self.dictionary:
                for i in range(len(self.dictionary[key])):
                    if self.dictionary[key][i] == neww[1]:
                        self.dictionary[key][i] = neww[0]
                # If dictionary entry has < 2 items after fix, mark for deletion
                if len(self.dictionary[key]) < 2:
                    todelete.append(key)
        
        # Remove entries that don't have duplicates
        for dell in todelete:
            self.dictionary.pop(dell, None)
    
    
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
        if not MARK_COLORS:
            return
        rand_color = randomcolor.RandomColor()
        for key in self.dictionary:
            # Generate unique color for every unique word
            color  = html_color_to_int(rand_color.generate(luminosity='light')[0])
            for key_tuple in self.dictionary[key]:
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
        if not self.editing:
            return
        
        # Ensure the final edit is captured in dictionary
        if self.caret_in_current_token(ed_self):
            self.redraw(ed_self)
            
        # Remove the "Active Editing" markers (borders)
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        
        # Reset flags to 'Selection' mode
        self.original = None
        self.editing = False
        self.selected = True
        self.our_key = None
        
        # Re-paint markers so user can see what else to edit
        self.mark_all_words(ed_self)


    def caret_in_current_token(self, ed_self):
        """
        Helper: Checks if the primary caret is strictly inside 
        the boundaries of the word currently being edited.
        """
        if not self.our_key:
            return False
        carets = ed_self.get_carets()
        if not carets:
            return False
        x0, y0, x1, y1 = carets[0]
        for key_tuple in self.dictionary.get(self.our_key, []):
            if y0 == key_tuple[0][1] and key_tuple[0][0] <= x0 <= key_tuple[1][0]:
                return True
        return False


    def reset(self):
        """
        FULLY Exits the plugin.
        Clears markers, releases selection lock, and resets all state variables.
        Triggered via 'Toggle' command, gutter icon click, or 'ESC' key.
        """
        self.start = None
        self.end = None
        self.selected = False
        self.editing = False
        self.dictionary = {}
        self.our_key = None
        self.offset = None
        self.start_l = None
        self.end_l = None
        self.pattern = None
        self.pattern_styles = None
        self.pattern_styles_no = None
        self.naive_mode = False
        self.saved_sel = None
        
        # Restore original position if needed
        if self.original:
            ed.set_caret(self.original[0], self.original[1], id=CARET_SET_ONE)
            self.original = None
            
        # Clear all markers
        ed.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        ed.set_prop(PROP_MARKED_RANGE, (-1, -1))
        self.set_progress(-1)
        # Clear the active tag
        ed.set_prop(PROP_TAG, 'sync_edit:0')
        
        # Hide gutter icon
        self.hide_gutter_icon()
        
        msg_status(_('Sync Editing: Cancelled'))

    def doclick(self):
        """API Hook for Mouse Click events."""
        # state = app_proc(PROC_GET_KEYSTATE, '')
        state = ''
        return self.on_click(ed, state)


    def on_click(self, ed_self, state):
        """
        Handles mouse clicks to toggle between 'Viewing' and 'Editing'.
        Logic:
        1. If Editing -> Finish current edit (Loop back to Selection).
        2. If Selection -> Check if click is on valid ID.
           - Yes: Start Editing (Add carets, borders).
           - No: Do nothing (Do not exit).
        """
        # Check if plugin is active
        if ed_self.get_prop(PROP_TAG, 'sync_edit:0') != '1':
            return
            
        if not self.selected and not self.editing:
            return
        
        # If we were editing, finish that session first
        # TODO: do i really need to finish editing here? on_caret already handle the case of switching from a valid ID to another valid ID so this seems not necesary anymore but maybe there is a case where it is necesary , so i will keep it for now to make more tests later
        # but it make clicking on torrent word in the c++ file by alexey faster!!
        if self.editing:
            self.finish_editing(ed_self)
            
        carets = ed_self.get_carets()
        if not carets:
            return
            
        self.our_key = None
        caret = carets[0]
        
        # Find which word was clicked
        for key in self.dictionary:
            for key_tuple in self.dictionary[key]:
                if  caret[1] >= key_tuple[0][1] \
                and caret[1] <= key_tuple[1][1] \
                and caret[0] <= key_tuple[1][0] \
                and caret[0] >= key_tuple[0][0]:
                    self.our_key = key
                    self.offset = caret[0] - key_tuple[0][0]
                    
        # If click was NOT on a valid word
        if not self.our_key:
            msg_status(_('Sync Editing: Not a word! Click on ID to edit it.'))
            return
            
        # --- Start Editing Sequence ---
        # Clear passive markers
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        self.original = (caret[0], caret[1])
        
        # Add active carets and borders
        for key_tuple in self.dictionary[self.our_key]:
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
            ed_self.set_caret(key_tuple[0][0] + self.offset, key_tuple[0][1], id=CARET_ADD)
        
        # Update state
        self.selected = False
        self.editing = True
        
        # Track bounds
        first_caret = ed_self.get_carets()[0]
        self.start = first_caret[1]
        self.end = first_caret[3]
        if self.start > self.end and not self.end == -1:
            self.start, self.end = self.end, self.start


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
                    self.toggle()
                    return False  # Prevent default processing
        
        # Not our decoration, allow default processing
        return None

    
    def on_caret(self, ed_self):
        """
        Hooks into caret movement.
        Continuous Edit Logic:
        If the user moves the caret OUTSIDE the active word, we do NOT exit, we check if landing on another valid ID.
        - If landing on valid ID: Do nothing (let on_click handle the switch)
        - If landing elsewhere: We simply 'finish' the edit and return to Selection mode and show colors
        
        Also handles showing/hiding gutter icon based on selection.
        """
        # Update gutter icon based on selection (only if not in active sync edit mode)
        if not self.selected and not self.editing:
            self.update_gutter_icon_on_selection()
        
        if ed_self.get_prop(PROP_TAG, 'sync_edit:0') != '1':
            return
            
        if self.editing:
            if not self.caret_in_current_token(ed_self):
                # Caret left current token - check if it's on another valid ID
                carets = ed_self.get_carets()
                if carets:
                    caret = carets[0]
                    clicked_key = None
                    
                    # Check if caret is on a valid ID
                    for key in self.dictionary:
                        for key_tuple in self.dictionary[key]:
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
        if ed_self.get_prop(PROP_TAG, 'sync_edit:0') != '1':
            return
        if key == VK_ESCAPE:
            self.reset()
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
        3. Re-draw the borders.
        """
        if not self.our_key:
            return
        # Find out what changed on the first caret (on others changes will be the same)
        old_key = self.our_key
        self.our_key = None
        
        # Get current state at the first caret
        first_y = ed_self.get_carets()[0][1]
        first_x = ed_self.get_carets()[0][0]
        first_y_line = ed_self.get_text_line(first_y)
        start_pos = first_x
        
        # Backtrack to find start of the word
        # Workaround for end of id case
        if not self.pattern.match(first_y_line[start_pos:]):
            start_pos -= 1
        while self.pattern.match(first_y_line[start_pos:]):
            start_pos -= 1
        start_pos += 1
        # Workaround for EOL #65
        if start_pos < 0:
            start_pos = 0
        
        # Check if word became empty (deleted)
        # Workaround for empty id (eg. when it was deleted) #62
        if not self.pattern.match(first_y_line[start_pos:]):
            self.our_key = old_key
            ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
            return
            
        new_key = self.pattern.match(first_y_line[start_pos:]).group(0)
        if not CASE_SENSITIVE:
            new_key = new_key.lower()
            
        # Rebuild dictionary positions for the modified word with new values
        old_key_dictionary = self.dictionary[old_key]
        existing_entries = self.dictionary.get(new_key, [])
        pointers = []
        for i in old_key_dictionary:
            pointers.append(i[0])
            
        # Recalculate positions for all instances
        for pointer in pointers:
            x = pointer[0]
            y = pointer[1]
            y_line = ed_self.get_text_line(y)
            # Scan backwards to find start of the new word instance
            while self.pattern.match(y_line[x:]):
                x -= 1
            x += 1
            # Workaround for EOL #65
            if x < 0:
                x = 0
            existing_entries = [item for item in existing_entries if item[0] != (x, y)]
            existing_entries.append(((x, y), (x+len(new_key), y), new_key, 'Id'))
        
        # Update dictionary keys
        if old_key != new_key:
            self.dictionary.pop(old_key, None)
        self.dictionary[new_key] = existing_entries
        # End rebuilding dictionary
        self.our_key = new_key
        
        # Repaint borders for the new word length
        ed_self.attr(MARKERS_DELETE_BY_TAG, tag=MARKER_CODE)
        for key_tuple in self.dictionary[self.our_key]:
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
        """Opens the configuration/readme file."""
        if msg_box(_('Open plugin\'s readme.txt to read about configuring?'), 
                MB_OKCANCEL+MB_ICONQUESTION) == ID_OK:
            fn = os.path.join(os.path.dirname(__file__), 'readme', 'readme.txt')
            if os.path.isfile(fn):
                file_open(fn)
            else:
                msg_status(_('Cannot find file: ')+fn)
