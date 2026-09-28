$ErrorActionPreference = 'Stop'
$target = 'C:\CourseAI\Bridge'
if (-not (Test-Path "$target\.venv\Scripts\python.exe")) {
    throw 'Existing bridge Python environment was not found at C:\CourseAI\Bridge.'
}
Write-Host 'Stop the running bridge watcher with Ctrl+C before continuing.'
Read-Host 'Press Enter when the watcher is stopped'
$backup = Join-Path $target ('backup-before-groq-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup | Out-Null
foreach ($name in @('src', 'pyproject.toml', '.env.example')) {
    if (Test-Path (Join-Path $target $name)) {
        Copy-Item (Join-Path $target $name) $backup -Recurse
    }
}
Copy-Item "$PSScriptRoot\src" $target -Recurse -Force
Copy-Item "$PSScriptRoot\pyproject.toml" $target -Force
Copy-Item "$PSScriptRoot\.env.example" $target -Force
& "$target\.venv\Scripts\python.exe" -m pip install -e $target
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Existing files backed up in ' + $backup }
$envPath = Join-Path $target '.env'
if (-not (Test-Path $envPath)) {
    Copy-Item "$PSScriptRoot\.env.example" $envPath
} else {
    $existing = Get-Content $envPath -Raw
    $settings = [ordered]@{
        GROQ_AUDIO_ENABLED = 'true'
        GROQ_AUDIO_MODEL = 'whisper-large-v3-turbo'
        AUDIO_CACHE_DIR = 'audio-cache'
        ACTIVE_COURSE = 'KIN120'
        LECTURE_DATE = ''
        GROQ_ENABLED = 'true'
        GROQ_API_KEY = ''
        GROQ_MODEL = 'openai/gpt-oss-120b'
        REVIEW_DIR = 'reviews'
        REVIEW_CONTEXT_DIR = 'review-context'
    }
    foreach ($name in $settings.Keys) {
        if ($existing -notmatch ('(?m)^\s*' + [regex]::Escape($name) + '\s*=')) {
            Add-Content -Path $envPath -Value ("`n" + $name + '=' + $settings[$name]) -Encoding UTF8
        }
    }
}
New-Item -ItemType Directory -Force 'C:\CourseAI\Lectures\Audio Inbox' | Out-Null
New-Item -ItemType Directory -Force 'C:\CourseAI\Lectures\Recording Staging' | Out-Null
Copy-Item "$PSScriptRoot\Start-Listener.cmd" $target -Force
Write-Host 'Existing Notion credentials and courses.yaml retained. Fill GROQ_API_KEY and ensure GROQ_ENABLED=true.'
Write-Host 'Then run from C:\CourseAI\Bridge: .\.venv\Scripts\courseai-listener.exe'
Start-Process notepad.exe -ArgumentList $envPath
