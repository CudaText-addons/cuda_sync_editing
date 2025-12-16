Plugin for CudaText.

This plugin allows you to easly edit multiple occurrences of the same word simultaneously within a selected block of text, inspired by SynWrite editor.


Basic usage
-----------

1. Select a block of text (one or multiple lines) containing the identifiers you want to edit.

2. Activation:
   - An icon will appear in the Gutter (left margin) within your selection. If you scroll, it will move to stay visible in the viewport as long as the selection exists.
   - Click this Gutter Icon to enter en Editing mode. Or use the menu: Plugins / Sync Editing / Activate.

3. Editing:
   - The selection highlights are replaced by colored markers.
   - Click on any colored word.
   - Multi-carets will appear on all identical words in that block.
   - Type to rename them all at once.
     Restriction: The UP, DOWN, and ENTER keys are disabled during editing to ensure all multiple cursors stay perfectly synchronized. Use LEFT/RIGHT to navigate within the word.

4. Switch Words (Continuous Editing):
   - To edit a different word in the same block, simply click on it.
   - The previous edit is saved, and the new word becomes active immediately.
   - If you move the caret away from an active word, the plugin returns to "Selection State" (markers remain visible).

5. Finish:
   - Click the Gutter Icon again or press Esc-key to exit the sync-editing completely.


Configuration file
------------------

The plugin creates a configuration file at `settings/cuda_sync_editing.ini`. It will be created automatically upon the first run.

Global settings must be placed in the [global] section and will apply to all files. You can override settings for specific lexers using [lexer_LexerName] sections.

Boolean values can be written not only like 0/1, but also like false/true, off/on, no/yes; value can be in any casing.

Supported options:

  - use_colors (boolean)
    - Default: true
    - Visualizes identifiers with background colors. If true, assigns a unique background color to every distinct word found in the selection.

  - case_sensitive (boolean)
    - Default: true
    - If true, "Var" and "var" are treated as different words.

  - use_simple_naive_mode (boolean)
    - Default: false
    - If true, ignores syntax highlighting roles and detects words purely by RegEx.
    - Useful for plain text files or lexers where styles are not standard.

  - identifier_regex
    - Default: \w+
    - The Regular Expression used to identify word boundaries.

  - identifier_style_include
    - Default: (?i)id[\w\s]*
    - Regex to match Lexer Styles that are considered "Identifiers" (e.g., "Id", "Var", "Tag id").
    - See "How to find Style Names" below.

  - identifier_style_exclude
    - Default: (?i).*keyword.*
    - Regex to match Lexer Styles that should be IGNORED (e.g., "Id keyword").


Configuration, advanced
-----------------------

You can customize the plugin for specific programming languages by creating sections named after the lexer, such as [lexer_Bash script], [lexer_PHP] or [lexer_HTML].

**Priority Rule:**
Any setting defined in a specific lexer section (e.g., [lexer_Python]) will override the same setting in the [global] section.

--- Mode 1: Standard Lexer Mode (Default) ---

This mode is enabled by default and can be disabled by setting "use_simple_naive_mode" to true.

In Standard Mode, By default, the plugin asks CudaText "Is this a Variable?" or "Is this a Comment?". It ignores strings and comments to prevent accidental edits. So in this mode the plugin relies on the CudaText lexer (syntax highlighter) to find identifiers and ignores tokens classified as comments, strings, or keywords. This can be disabled by setting "use_simple_naive_mode" to true which will allow editing any duplicates even those inside comments and strings.

Relevant Settings for Standard Lexer Mode:

* identifier_style_include
  - A Regex pattern matching CudaText style names to ALLOW (e.g., "Id", "Var").
  - Example: `(?i)id[\w\s]*` (Matches styles like "Id", "Id keyword", "Id function" ...etc).

* identifier_style_exclude
  - A Regex pattern matching CudaText style names to IGNORE (e.g., "Id keyword").
  - Example: `(?i).*keyword.*` (Prevents editing keywords like "function", "if", "class" ...etc).

NOTE: These two settings are IGNORED if `use_simple_naive_mode` is set to true.

--- Mode 2: Simple Naive Mode (Regex Only) ---

This mode can be enabled by setting "use_simple_naive_mode" to true.

Naive Mode disables the syntax checks and finds words purely by matching a Regex pattern. This is useful for plain text files, configuration files, or lexers where styling is not specific enough. When using "Naive Mode," this plugin ignores CudaText syntax highlighting and simply grabs every word that matches a pattern, this will allow editing any duplicates even those inside comments and strings.

Relevant Settings for Naive Mode:

* identifier_regex
  - The Regex pattern used to define and find words.
  - Default: \w+ (Matches letters, numbers, and underscores).
  NOTE: This setting is primarily used when use_simple_naive_mode is true, though the regex still defines valid word characters during editing in all modes, so in Standard mode, this setting is still used to define word boundaries while typing.


Configuration examples
----------------------

Example cuda_sync_editing.ini content:

[global]
use_colors=true
use_simple_naive_mode=false
case_sensitive=true
identifier_regex=\w+
identifier_style_include=(?i)id[\w\s]*
identifier_style_exclude=(?i).*keyword.*

; FORCE NAIVE MODE for Markdown
; Markdown lexer often treats text as one big block, so we skip syntax checks
[lexer_Markdown]
use_simple_naive_mode=true

; HTML specific settings to allow editing tags
; Allow editing standard Tags, but also specific properties
[lexer_HTML]
identifier_style_include=Text|Tag id correct|Tag prop

; PHP specific settings
; Only allow editing things explicitly styled as variables
[lexer_PHP]
identifier_style_include=Var


How to find Style Names
-----------------------

To configure "identifier_style_include" correctly, you need to know what CudaText calls the specific parts of your code.
1. Open a file with the desired syntax.
2. Go to "Options / Lexers / Lexer properties / Styles tab".
3. Look at the list of styles (e.g., "Id", "Id keyword", "Comment").
4. Use these names (separated by |) in your config.


Notes
-----
- The plugin automatically uses "Simple Naive Mode" (Regex only) for: Markdown, reStructuredText, Textile, ToDo, Todo.txt, JSON and Ini files.
- Identifiers inside 'Comments' or 'Strings' are usually ignored unless `use_simple_naive_mode` is set to true.
- The plugin is optimized for large files and therefore only tracks changes made while in Edit Mode (when the multi-carets are active).
    - Limitation on Writing: Writing or pasting new text outside of the currently colored (selected) identifiers while in Selection/View Mode is not supported.
    - Why? To maintain performance on massive files (e.g., 9MB with 400k duplicates), the file analysis is only run once at the start of the session. Re-running the analysis to detect new identifiers after every single change outside of an active edit would cause a noticeable 6+ second delay on big files.
    - Workaround: If you write new code outside of the currently colored identifiers, simply Exit the Sync Edit session (Esc or Gutter Icon) and Reactivate it. The plugin will immediately scan the modified file content.


Troubleshooting
---------------

- "No editable identifiers found":
  If you see this message, then the plugin likely thinks your selection contains only Keywords or Comments. Try setting `use_simple_naive_mode=true` for that lexer to bypass this check, or configure that specific lexer as explained above.

- "CudaText is still parsing the file..." Message:
  This message appears when opening very large files. It means CudaText hadn't finished analyzing the syntax. Just wait a few seconds and try again.


About
-----

Authors:
  - Vladislav Utkin (https://github.com/viad00) - Original author.
  - Alexey Torgashin (CudaText) - Made some bug fixes and other improvements.
  - Badr Elmers (https://github.com/badrelmers) - Major refactoring: Added Continuous Edit mode, Multisession files edit, gutter activation and optimized the code for speed.

License: MIT
