#  CudaText Sync Editing plugin
Sync Editing feature to edit identical identifiers (inspired by [SynWrite](http://uvviewsoft.com/synwrite/))

### Showcase
![A plugin showcase gif](readme/using.gif)

### Usage
1. Select a block of text (one or multiple lines) containing the identifiers you want to edit.

2. Activation:
   - A "hamburger" icon (≡) will appear in the Gutter (left margin) on the last line of your selection.
   - Click this Gutter Icon to enter en Editing mode. Or use the menu: Plugins / Sync Editing / Activate.

3. Editing:
   - The selection highlights are replaced by colored markers.
   - Click on any colored word. Multi-carets will appear on all identical words in that block.
   - Type to rename them all at once.

4. Switch Words (Continuous Editing):
   - To edit a different word in the same block, simply click on it.
   - The previous edit is saved, and the new word becomes active immediately.
   - If you move the caret away from an active word, the plugin returns to "Selection State" (markers remain visible).

5. Finish:
   - Click the Gutter Icon again or press `Esc` key to exit the plugin completely.
