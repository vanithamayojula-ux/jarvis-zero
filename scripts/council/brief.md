# JARVIS Zero: brief for the upgrade council

(Written for the council. Contains no personal data. The council only ever sees this file.)

## What JARVIS Zero is
A voice assistant for one person on a Windows 11 laptop (i5-1340P, 16 GB RAM, no graphics card, Iris Xe).
Budget: exactly zero rupees. Free tiers only: no card, no trials, no paid APIs.
The person cares most about: SECURITY FIRST, honesty (never claim an action that didn't happen), and that it only acts
when they ask.

## How it works today
- Hearing: push-to-talk (Ctrl+Alt+J). Optional hands-free mode with an offline "hey jarvis" wake word (openWakeWord).
  Speech-to-text uses Groq Whisper large-v3-turbo (free cloud) with local faster-whisper base.en as backup.
- Turn-taking: Silero voice detection plus the Pipecat Smart Turn v3 model, which only ever waits longer, never cuts in sooner.
- Brain: fixed command rules first (instant and reliable). Then a free Groq cloud model (Qwen3.8-27B) restates messy
  commands as plain ones, which go back through the same rules. Chat uses Groq too, with local Llama 3.2 3B as backup.
- Voice: Groq Orpheus for chat (about 100 a day free), local Piper for short confirmations.
- Memory: facts about the person, stored on the laptop and learned from chat. Never given to websites.
- Actions: open approved apps/sites/files, YouTube/Spotify search and play, reminders and timers, volume and brightness,
  world time, maths, a morning briefing. Clicks and typing go into apps through Windows UI Automation; never into
  terminals, Run, regedit, Task Manager or password boxes. Buttons like Send/Buy/Delete always ask first.
- Its own Microsoft Edge: a clean profile with sign-in blocked. Camera, mic, location, clipboard, notifications,
  devices, pop-ups and downloads are all denied or asked. Blocklists cover 302,000 malware/phishing/scam/gambling/adult
  domains plus piracy sites; blocked sites are refused before loading, and a watchdog replaces pages reached by
  clicking within about a second. A form agent fills forms by element number and always asks before Submit/Pay/Sign up.
  It never types passwords, OTPs, card numbers, Aadhaar, PAN or bank details.
- Local API on 127.0.0.1 only, with a secret token and origin checks. The token is masked in all logs.
- Edge's control port is random on every start, bound to 127.0.0.1 and closed to web pages (done 22 Oct, council idea 1).
- The project is under git with a strict .gitignore and a pre-commit secret scan that blocks the real .env values,
  key patterns and personal-data paths (done 22 Oct, council idea 2). Every change can now be undone.
- A floating "spidey" desktop bot with animations, a chat bar (Ctrl+Alt+0), reminders and quick-action buttons. Since 23 Oct: a typing, scrolling speech bubble with links, Copy and Yes/No buttons; a status pill (listening/thinking, cloud or local, timer countdown, Spotify/Edge song with media keys); drag and drop of links, files and text (a dropped text file is summed up via /api/summarize, as data, never routed as commands); costumes; web-shooting and hanging tricks; calmer at night.
- Since 25 Oct: a Chat mode and a Voice mode (setup.bat asks; also spidey's right-click Mode menu, or "chat mode" /
  "voice mode"). Chat mode opens no mic, no speaker stream and loads no voice models (measured ~114 MB vs ~211 MB),
  and the talk shortcut opens spidey's typing bar instead. "type …" now types the rest exactly as written
  (capitals and punctuation kept; "in the search bar" picks the page's search box).
- Since 25 Oct: optional web lookups with a free search key in .env (Tavily, Brave or Serper). Only the query is sent;
  JARVIS's own model answers from the results, which are treated as data, not instructions. Used for "look up …",
  weather/news/price questions, and questions about recent or current things. Without a key nothing changes.
- Since 25 Oct: tapping Right Ctrl alone opens spidey's chat bar (a low-level keyboard hook that only compares key
  codes, ignores injected keys, stores nothing; can be switched off), and a small native JARVIS panel beside spidey
  replaces the browser dashboard on start (conversation, message box, Chat/Voice switch, status, next reminder).
- Since 25 Oct (SafetyGate): every click, key press and typed character in any app, and in JARVIS's Edge, passes one
  gate (backend/jarvis/gate.py). It refuses terminals, password managers, remote-control tools, sign-in/banking pages,
  the Explorer address bar, the editor terminal and card/OTP/password boxes; asks first for Send/Buy/Delete/Allow/Install
  buttons and Enter outside search boxes; and pauses itself if the form agent acts too fast. Kill switch: Ctrl+Alt+Backspace
  (a Win32 hot key, no keyboard hook) freezes everything until you say "resume". Every decision is one secret-free line in
  data/actions.log (hash-chained; typed text is never stored, only its length); say "what did you do". Risky yes/no
  questions need a whole-sentence "yes", expire after 30 s and re-check the window and button before acting. Settings that
  widen JARVIS's powers (allowed_commands, site lists, desktop control on) wait for a "yes" in JARVIS, not the API.
- Since 25 Oct (local seeing): "what's on my screen", "read this error", "what does this window say", "summarize this window",
  "ocr this" read ONE window, only when asked, entirely on the laptop: UI Automation text first (error and dialog text first,
  password/PIN/OTP/card boxes skipped), then Windows' built-in offline OCR (a one-off in-memory picture of that window, never
  saved, refused for capture-protected or black windows, read by a short-lived fixed PowerShell script with a size cap and a
  10 s kill). Password managers, Windows Security/UAC, banking/sign-in/checkout pages (Indian banks included), JARVIS's own
  windows and browsers on finance/sign-in addresses are refused outright, and there is no "read it anyway". Terminals can be
  read (never clicked or typed in). Everything read is redacted (cards, Aadhaar, PAN, UPI ids, codes, keys, tokens), fenced as
  untrusted data and checked for text that gives orders to the assistant (flagged out loud, never obeyed, never routed as a
  command). Page text, search snippets and the form agent's page view get the same fence; the agent may only open the page it
  is on or a site you named, only types what is in your request, and asks before any click inside a password form. A "looking
  at Notepad" pill shows for 3 s on every read; "stop looking at the screen" / "you can look at the screen again" (also in
  spidey's menu and the panel) switch it. Summaries use the local model; the cloud sees screen text only if screen_to_cloud is
  turned on (off by default, needs a yes in JARVIS). The log keeps names and lengths only.
- Since 25 Oct (acting on request, all through the SafetyGate): "find the Save button", "what buttons are on this window",
  "list the text boxes" list up to six matches (best first: exact name, then starts-with, then contains), say where each is
  ("top left", "bottom right") and put a thin red outline on the best one; "click number 2" / "no, the second one" press
  the one you pick, only within 30 s and only if the window and the control are still just as they were (and a Delete or
  Send still asks first). "What would you click for save" only points. Password, PIN, OTP and card boxes are never listed.
  The outline is spidey's one reusable click-through, no-focus, outline-only window (setting highlight_actions, on by
  default); the rectangle is checked on both ends. Shortcuts come from one fixed list ("press ctrl+s", "save this", "copy
  that", "paste", "undo", "select all", "print"): ctrl+w, alt+f4, print, cut, clear history and private window ask first;
  nothing with the Windows key, no ctrl+alt+delete, no Run. Window verbs: "minimise this", "maximise Notepad", "restore it",
  "snap this left", "switch to Notepad", "show the desktop" (the Windows key only for snap left/right and show desktop);
  "close this window" asks first and just sends the window its own close request. In JARVIS's Edge: "list my tabs",
  "switch to the second tab", "switch to the YouTube tab", "close the second tab", "close other tabs" (asks, keeps the one
  in front, never closes the last), and "find pricing on this page" / "how many times does X appear" / "next match" (page
  text only, never what is typed into a box; highlighted for 20 s; the words around a match are read out as quoted data);
  "keep reading" carries on through a page. "Open display / Wi-Fi / Bluetooth / sound / update … settings" opens that
  Windows page from a fixed table; "turn on wifi" says honestly it can't flip it and opens the page; "open the Recycle Bin /
  This PC". Links that would go through cmd.exe are refused if they hold & | ^ < > " whitespace or a bare %NAME%.
- Since 27 Oct (spidey's picture board, display only): "show me a picture of a red panda" / "what does a narwhal look
  like" shows the lead picture of the best-matching Wikipedia article (no key; only the words are sent; downloaded only from
  Wikimedia's picture servers, re-saved as a size-capped PNG, kept in memory, never on disk). "Show my photo called goa"
  finds a picture file in the approved folders. "Draw a cat" / "draw the water cycle": the cloud model writes SVG (max 1,800
  tokens, no local fallback when the cloud is rate-limited), which is turned into at most 220 checked plain shapes (checked
  again in spidey); spidey draws them stroke by stroke. The board is a no-focus window beside spidey with flip, open source
  page (through JARVIS's site checks), save to Downloads and close ("close the picture"). Setting picture_board.
- Since 27 Oct (spidey's mini TV, display only): "play lofi on the TV" (the top YouTube result: the results page is
  fetched with only the words and read as data, taking an 11-character id and a cleaned title), "show a map of Goa / pictures
  of X / google X / youtube.com/… / put this on the TV", "pause / mute / turn up the TV", "make the TV bigger", "what's on
  the TV", "turn off the TV". It is a separate Edge app window with its own clean profile (data/edge-tv: same account
  lock-out and permission lockdown, same blocklists and watchdog), always on top beside spidey, marked never-activate; if Edge
  grabs the keyboard while starting, it is handed straight back (AttachThreadInput + SetForegroundWindow; nothing typed).
  While on, one control connection registers a single page script (a stylesheet that makes a YouTube player fill the
  window). Off = that Edge process is closed (it is also closed when JARVIS stops). Desktop control treats it as JARVIS's
  own window. Settings mini_tv, tv_size. Costs ~300 MB RAM only while on.
- Since 27 Oct the assistant is called Spidey (persona, replies, panel, dashboard, README; wake word "spidey", matched
  in the transcript with mishearing aliases and a not-the-wake-word list such as "spider" and "spicy"; code, folders and
  the `jarvis` package keep the old name). More natural talk: everyday reactions (haha, oof, no way…) in the
  rules plus more made-up example exchanges in the system prompt. This is prompting, not training.
- Since 27 Oct, meme reactions after every reply (not in typing mode, not on phone calls): spidey acts out a mood guessed
  from the words at once (nothing sent), then a second Groq model (openai/gpt-oss-20b, separate free limits; its failures
  never pause the chat model) gets the two lines, redacted, and never anything read from a page or window. It picks one of 35
  Imgflip templates and writes captions, which are checked (known template, length cap, no links or hashtags) and drawn
  locally with Pillow. Templates come from Imgflip's public list, are downloaded only from i.imgflip.com, verified as
  pictures and cached in data/memes. Without the cloud, a plain reaction picture for the mood is shown. The meme panel is a
  no-focus window beside spidey that hides after 14 s. Settings memes, meme_model; "stop the memes" / "memes on".
- Since 27 Oct, round 2: the cloud voice performs [laugh]/[chuckle]/[sigh]/[cheerful] for "haha/hehe/ugh/no way"
  (the text shown is unchanged). Memes come now and then (meme_rate). Study mode (pomodoro) with nudges, plus
  spidey's break / late-night / morning nudges through POST /api/react (fixed moments only). Spidey sometimes reacts
  to the mini TV's title. CI runs the tests on GitHub (windows-latest) on every push.
- Since 27 Oct, the floating island guide ("guide me to …"): it reads the window's interactive control labels (names,
  kinds, positions; each cut to 40 characters after redaction; never values, password/PIN/card boxes or private
  windows), sends them fenced as data to the cloud model, and points at the next control (island step, spidey's web,
  a ring refreshed every 1.5 s under its 2 s cap). It waits for the click by polling the mouse button and cursor
  position only, then looks again. It never clicks or types and keeps no look past its step. Settings guide and
  guide_cloud_labels (protected: turning it on via the API waits for a yes).
- 1483 automated tests pass (3 skipped, including the real-OCR test with JARVIS_OCR_LIVE=1 and the real-Edge page-script
  test with JARVIS_JS_LIVE=1).

## Known gaps and pain points (from real use)
1. Indian names are misheard (Hanumankind, Arijit Singh). Partly fixed with name hints and a fuzzy name fixer.
2. The free Groq limits: about 8,000 tokens a minute for chat and about 100 voice replies a day. Long form tasks hit
   the per-minute limit.
3. In hands-free mode, background speech used to become commands (now mitigated by the wake word, untested with the
   person's real voice).
4. It can't click inside games (no accessibility labels) or flip Wi-Fi or Bluetooth (it opens the right Windows page).
   In its own Edge it can't press browser-level keys from outside (print, save, full screen, reopen tab, the find bar);
   it says so and offers its own find-on-page instead. Tabs are numbered in the order they were opened.
5. Any program already running as the person could still find Edge's random control port (Chrome-style control
   ports have no password). The Edge profile holds no accounts or saved data, which limits the damage.
6. The first chat reply after a restart once came back empty, and a first Edge start once failed (neither reproduced;
   unexpected tool errors are now logged).
7. Settings and memory live in plain JSON files on the laptop (not encrypted).

## Hard rules for any proposal
- Security and privacy come first. No personal data leaves the laptop beyond what is needed to answer a command.
- No paid services. No new always-on network connections. No telemetry.
- JARVIS acts only on the person's request. No autonomous clicking, submitting, purchasing or messaging.
- Every change must be testable on this laptop and must keep the existing tests passing.
