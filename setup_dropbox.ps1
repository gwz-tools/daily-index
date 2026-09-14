# Optional one-time Windows setup. No secrets are written to disk or printed.
# Paste the contents into PowerShell if local script execution is disabled.
$ErrorActionPreference = 'Stop'
function Read-PrivateValue([string]$Prompt) {
    $secureValue = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureValue)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
Write-Host 'Open https://www.dropbox.com/developers/apps and select your existing app.'
Write-Host 'It must have Full Dropbox access and files.metadata.read + files.content.write permissions.'
$dropboxAppKey = Read-Host 'Dropbox App key'
$dropboxAppSecret = Read-PrivateValue 'Dropbox App secret (hidden)'
$authorizationUrl = 'https://www.dropbox.com/oauth2/authorize?client_id=' + [Uri]::EscapeDataString($dropboxAppKey) + '&response_type=code&token_access_type=offline&scope=files.metadata.read%20files.content.write'
Start-Process $authorizationUrl
$dropboxCode = Read-PrivateValue 'Authorize in Dropbox, then paste the one-time code (hidden)'
try {
    $tokenResponse = Invoke-RestMethod -Method Post -Uri 'https://api.dropboxapi.com/oauth2/token' -ContentType 'application/x-www-form-urlencoded' -Body @{
        grant_type = 'authorization_code'
        code = $dropboxCode
        client_id = $dropboxAppKey
        client_secret = $dropboxAppSecret
    }
} catch {
    Write-Host 'Dropbox authorization failed. Check the app permissions and request a fresh code.'
    return
}
if (-not $tokenResponse.refresh_token) { throw 'Dropbox did not return a refresh token.' }
$secretValues = @{
    DROPBOX_APP_KEY = $dropboxAppKey
    DROPBOX_APP_SECRET = $dropboxAppSecret
    DROPBOX_REFRESH_TOKEN = $tokenResponse.refresh_token
}
foreach ($secretName in @('DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN')) {
    Set-Clipboard -Value $secretValues[$secretName]
    Write-Host "Create GitHub Actions repository secret: $secretName"
    Write-Host 'Its value is now on your clipboard. Paste into the Secret field, then save.'
    Read-Host 'Press Enter after saving this secret' | Out-Null
}
Set-Clipboard -Value ''
$secretValues.Clear()
$dropboxAppSecret = $null
$dropboxCode = $null
$tokenResponse = $null
Write-Host 'Done. Run the Daily index workflow manually once.'
