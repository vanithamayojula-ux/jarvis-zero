# Holds today's JARVIS upgrade council in the background (no windows). Five free Groq agents debate
# scripts\council\brief.md; the decision goes to data\council\<date>.md and spidey says when it's ready.
# Safe by design: the agents only see the brief and can't run anything; nothing changes until you approve.
$Root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
New-Item -ItemType Directory -Force (Join-Path $Root "data\council") | Out-Null
Start-Process -FilePath (Join-Path $Root ".venv\Scripts\pythonw.exe") `
  -ArgumentList "`"$(Join-Path $Root 'scripts\council\council.py')`"" -WorkingDirectory $Root -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Root "data\council\last-run.log") -RedirectStandardError (Join-Path $Root "data\council\last-run.err.log")
