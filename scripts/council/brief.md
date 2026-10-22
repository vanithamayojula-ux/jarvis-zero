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
- Local API on 127.0.0.1 only, with a secret token and origin checks.
- A floating "spidey" desktop bot with animations, a chat bar (Ctrl+Alt+0), reminders and quick-action buttons.
- 533 automated tests pass.

## Known gaps and pain points (from real use)
1. Indian names are misheard (Hanumankind, Arijit Singh). Partly fixed with name hints and a fuzzy name fixer.
2. The free Groq limits: about 8,000 tokens a minute for chat and about 100 voice replies a day. Long form tasks hit
   the per-minute limit.
3. In hands-free mode, background speech used to become commands (now mitigated by the wake word, untested with the
   person's real voice).
4. It can't click inside games (no accessibility labels) or change Wi-Fi or Bluetooth.
5. The local Edge control port (127.0.0.1:9333) can be used by any program already running on the laptop.
6. The first chat reply after a restart once came back empty (not reproduced).
7. There is no version control on the project folder, so every change is hard to undo.

## Hard rules for any proposal
- Security and privacy come first. No personal data leaves the laptop beyond what is needed to answer a command.
- No paid services. No new always-on network connections. No telemetry.
- JARVIS acts only on the person's request. No autonomous clicking, submitting, purchasing or messaging.
- Every change must be testable on this laptop and must keep the existing tests passing.
