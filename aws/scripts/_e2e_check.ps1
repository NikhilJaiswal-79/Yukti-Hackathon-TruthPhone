param([string]$Mission = 'Verify the Samsung Galaxy M14 5G listing')

$ErrorActionPreference = 'Stop'
$base = 'http://localhost:8000'

function Post($url, $body) {
  Invoke-RestMethod -Method Post -Uri $url -ContentType 'application/json' -Body ($body | ConvertTo-Json -Depth 5)
}

function Get-State($id) {
  for ($i = 0; $i -lt 5; $i++) {
    try { return Invoke-RestMethod "$base/sessions/$id" } catch { Start-Sleep -Seconds 2 }
  }
  throw "could not read session $id"
}

function Wait-For($id, [string[]]$wanted, [int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  $seen = 0
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 4
    $state = Get-State $id
    if ($state.messages.Count -gt $seen) {
      $state.messages[$seen..($state.messages.Count - 1)] | ForEach-Object {
        if ($_.role -eq 'assistant') { "  > $($_.content)" }
      }
      $seen = $state.messages.Count
    }
    if (($state.phase -in $wanted -and $state.jobStatus -ne 'running') -or $state.phase -eq 'error') { return $state }
  }
  return (Get-State $id)
}

$session = Post "$base/sessions" @{}
$id = $session.sessionId
"Sessions: $id"

Post "$base/sessions/$id/messages" @{ message = $Mission; forceSearch = $true } | Out-Null

$state = Wait-For $id @('selection', 'done') 180
if ($state.phase -eq 'clarification') {
  "clarification received; answering"
  Post "$base/sessions/$id/messages" @{ message = "No budget limit. Please verify this exact listing: $Mission"; forceSearch = $true } | Out-Null
  $state = Wait-For $id @('selection', 'done') 240
}

"phase: $($state.phase)"
if ($state.error) { "ERROR: $($state.error)"; exit 1 }
"results: $($state.searchResults.Count)"
$state.searchResults | ForEach-Object { " - $($_.name) | $($_.price)" }

if ($state.phase -eq 'selection' -and $state.searchResults.Count -gt 0) {
  Post "$base/sessions/$id/select" @{ index = 0 } | Out-Null
  "selected: $($state.searchResults[0].name)"
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $state = Wait-For $id @('done') 540
  "verify seconds: $([int]$sw.Elapsed.TotalSeconds)"
  $state | ConvertTo-Json -Depth 8 | Set-Content ".\scripts\_last_state.json" -Encoding utf8
}

"phase: $($state.phase)"
if ($state.error) { "ERROR: $($state.error)" }
""
"=== CLAIMS ($($state.verificationResults.Count)) ==="
$state.verificationResults | ForEach-Object {
  "[$($_.verdict)/$($_.severity)] $($_.claim) :: $($_.reality)"
  if ($_.hidden_catch) { "    catch: $($_.hidden_catch)" }
  if ($_.evidence) { "    src: $(($_.evidence | ForEach-Object { $_.source }) -join ', ')" }
}
""
"=== REPORT ==="
$state.finalReport
