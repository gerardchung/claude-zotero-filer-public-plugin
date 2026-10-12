---
name: zotero-filer
description: Add papers to the user's Zotero library and file them into the right Zotero folders (collections), and manage their "To read" folder. Use when the user gives a paper (PDF file, DOI, link, or title) and wants it saved, added, imported or filed in Zotero; when they want to save something "to read later"; when they ask what is on their to-read / reading list; or when they say they have finished reading a paper and want it sorted into folders.
---

# Zotero filer

You help the user put papers into Zotero and file them in the right folders.
You do the thinking (which folder fits); a helper script does the Zotero work.

## The helper

Run it with Python 3. The script is in this skill's folder:

```
python3 "${CLAUDE_SKILL_DIR}/scripts/zotero_tool.py" <command> [options]
```

If `${CLAUDE_SKILL_DIR}` was not filled in, use the "Base directory for this skill"
path instead.

On Windows, use `python` (or `py`) if `python3` is not found. Every command prints JSON. If the JSON has an `"error"` key, explain the error to the
user in plain words. If the error says `ZOTERO_API_KEY` or `ZOTERO_USER_ID` is not set,
point them to "Setup" in the README at
https://github.com/gerardchung/claude-zotero-filer-public-plugin. Never ask the user to paste
their API key into the chat.

| Command | What it does |
|---|---|
| `check` | Checks the key works, can write, and finds the "To read" folder |
| `lookup "<pdf path / DOI / link / title>"` | Finds paper details. Returns `candidates` |
| `find-dup --doi D --title T` | Is it already in Zotero? Returns `duplicates` |
| `collections` | All folders: `key`, `path`, `num_items`, `sample_titles`; plus `to_read` (cached 1 hour; `--refresh` after the user changes folders) |
| `fetch-pdf --doi D` or `--url U` | Tries to download a free, legal PDF. Returns `pdf_path` or null |
| `add --meta FILE --collections K1,K2 [--to-read] [--pdf FILE] [--tags a,b]` | Creates the item in those folders and attaches the PDF |
| `new-collection --name N [--parent K]` | Makes a folder (reuses one with the same name and parent) |
| `to-read [--limit N]` | Papers in "To read", newest first, numbered, with `has_pdf` |
| `refile --item KEY [--add K1,K2] [--remove K] [--add-to-read] [--remove-to-read] [--tag read]` | Changes the folders and tags of a paper already in Zotero |

Folders are always passed by **key**, never by name. Get keys from `collections`.
Use the user's words for folders in chat ("PhD > Ch2 > Measurement"), never keys.

## Job 1: add a new paper

1. **Find the details.** Run `lookup` on what the user gave.
   - `input_type: "title"` gives up to 3 search results with a `match` score. If the
     top one is not close to 1.0, or several look alike, ask the user which one.
   - `input_type: "pdf"`: check `title_found_in_pdf`. If it is false, or there are no
     candidates, read `pdf_text_start`, find the real title, and run `lookup` again with
     that title. Keep the PDF path for step 5.
   - `input_type: "url"`: if `candidates` is empty, the site blocked the script. Ask for
     the DOI or title. If `pdf_url` is present, keep it for step 5.
   - If nothing works, ask the user for the DOI or the title.
   - Save the chosen candidate (one object) to a temp JSON file for `add --meta`.

2. **Check for duplicates.** Run `find-dup` with the DOI and title. If a duplicate
   exists, tell the user where it already is. Offer to add that existing item to new
   folders with `refile` instead of making a copy.

3. **"Read later" shortcut.** If the user said "to read", "read later", "save for later",
   or similar, skip step 4. File it in "To read" only (`add --to-read`).

4. **Suggest folders.** Run `collections`. Compare the paper's title, abstract, journal
   and keywords with each folder's `path` and `sample_titles`.
   - Pick the 1–3 best folders. Prefer the most specific (deepest) folder that fits.
     Suggest more than one only when the paper really belongs in several.
   - Give each suggestion one short reason ("matches your other loneliness scales papers").
   - Ask with **AskUserQuestion**, `multiSelect: true`, max 4 options:
     your suggestions (label = folder path, description = reason) plus **"To read"**.
     The user can type something else in "Other", such as another folder or a new folder name.
   - If the user wants a new folder, run `new-collection`. Pick the parent by asking, or
     use the obvious parent from what they said.
   - Never file without the user's choice.

5. **Get the PDF.** If the user gave a PDF, use it. Otherwise, if you have `pdf_url`, run
   `fetch-pdf --url`. Then try `fetch-pdf --doi`. If no PDF is found, add the paper
   without one and say so. Do not try to get around paywalls.

6. **Add it.** Run `add --meta FILE --collections KEYS [--to-read] [--pdf PATH]`.
   If `pdf_error` is present, say that the paper was saved but the PDF was not.

7. **Report back** in one or two lines: the title, the folders, whether the PDF is attached.

## Job 2: show the "To read" list

When the user says "what's on my to-read list", "show my reading list", or similar:
- Run `to-read`. Show a short numbered list using `n`:
  `n. Title — Authors (year), Journal · added date · PDF ✓/✗`.
- If `total` is more than `shown`, say how many more there are.
- If the user asks to filter ("only the ones about measurement"), use the titles,
  abstracts, folders and tags in the output. Do not invent anything.

## Job 3: after reading, file the paper and take it off the list

When the user says "I finished #3", "I read the Smith 2024 paper", or similar:
1. Run `to-read` and find the paper by its number or name. Confirm which one if unclear.
2. Run `collections` and suggest folders as in Job 1, step 4. Mention any folders
   it is already in (`folders`). Do not offer "To read" as a choice here.
3. Ask with AskUserQuestion. In the same call, add a second question:
   "Remove it from To read?" with the options "Yes (Recommended)" and "No, keep it there".
4. Run `refile --item KEY --add KEYS --tag read`, adding `--remove-to-read` if the user
   said yes. Removing only takes the paper out of the folder; it stays in the library.
5. Report the folders before and after.

**Several at once** ("sort the ones I've read"): show the list, ask which ones they
have read (multi-select, or let them type numbers), then do steps 2–4 for each paper,
one at a time.

## Rules

- Only change what the user agreed to. Never delete items or folders.
- Never show or ask for the API key.
- If the helper prints an error about network access in a cloud session, tell the user
  which site was blocked (for example `api.zotero.org`) and point to the README's cloud setup.
