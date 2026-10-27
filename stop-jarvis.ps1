# Stops the JARVIS Zero backend (the AI model is unloaded from RAM as it exits).
Get-NetTCPConnection -LocalPort 8600 -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue }
Write-Host "Spidey stopped."
