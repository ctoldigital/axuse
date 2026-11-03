# axdroid — manual APK build (no Gradle, no downloads beyond the SDK you have).
#
# Uses aapt2 + javac + d8 + apksigner straight from the Android SDK.
# Deterministic, offline, a few seconds.
#
#   powershell -File tools\build-apk.ps1 [-Sdk <path>] [-Jdk <path>] [-Api 36]
#
# Output: phone-use\android\axdroid.apk (debug-signed; adb install -r)

param(
    [string]$Sdk = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$Jdk = "",                 # defaults: JAVA_HOME, then a JDK under ~\.axuse-tools
    [int]$Api = 36
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $PSScriptRoot
$src  = Join-Path $here "app\src"
$res  = Join-Path $here "app\res"
$manifest = Join-Path $here "app\AndroidManifest.xml"
$out  = Join-Path $here "app\build"

# ── locate a JDK (17+ needed by d8/apksigner; javac targets release 8) ──────
if (-not $Jdk) {
    $cand = Get-ChildItem "$env:USERPROFILE\.axuse-tools" -Directory -Filter "jdk*" -ErrorAction SilentlyContinue |
            Where-Object { Test-Path "$($_.FullName)\bin\javac.exe" } | Select-Object -First 1
    if ($cand) { $Jdk = $cand.FullName }
    if (-not $Jdk -and $env:JAVA_HOME -and (Test-Path "$env:JAVA_HOME\bin\javac.exe")) { $Jdk = $env:JAVA_HOME }
}
if (-not $Jdk -or -not (Test-Path "$Jdk\bin\javac.exe")) {
    $zip = "$env:USERPROFILE\.axuse-tools\jdk17.zip"
    if (Test-Path $zip) {
        Write-Host "extracting $zip …"
        Expand-Archive -Path $zip -DestinationPath "$env:USERPROFILE\.axuse-tools" -Force
        $cand = Get-ChildItem "$env:USERPROFILE\.axuse-tools" -Directory -Filter "jdk*" |
                Where-Object { Test-Path "$($_.FullName)\bin\javac.exe" } | Select-Object -First 1
        if ($cand) { $Jdk = $cand.FullName }
    }
}
if (-not $Jdk -or -not (Test-Path "$Jdk\bin\javac.exe")) { throw "no JDK found (need 17+ for d8; set -Jdk)" }
Write-Host "JDK: $Jdk"

# ── locate SDK pieces ────────────────────────────────────────────────────────
$platformJar = "$Sdk\platforms\android-$Api\android.jar"
if (-not (Test-Path $platformJar)) { throw "missing $platformJar (install platform $Api via sdkmanager)" }
$bt = Get-ChildItem "$Sdk\build-tools" -Directory | Sort-Object Name -Descending | Select-Object -First 1
if (-not $bt) { throw "no build-tools in $Sdk" }
$aapt2 = "$($bt.FullName)\aapt2.exe"
$d8bat = "$($bt.FullName)\d8.bat"
$apksigner = "$($bt.FullName)\apksigner.bat"
$zipalign = "$($bt.FullName)\zipalign.exe"
foreach ($t in @($aapt2, $d8bat, $apksigner, $zipalign)) { if (-not (Test-Path $t)) { throw "missing $t" } }
Write-Host "SDK: build-tools $($bt.Name), android-$Api"

New-Item -ItemType Directory -Force $out | Out-Null
Remove-Item -Recurse -Force "$out\gen", "$out\obj", "$out\apk" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$out\gen", "$out\obj", "$out\apk" | Out-Null

# ── 1. compile resources → link with manifest → resources.ap_ + R.java ──────
& $aapt2 compile --dir $res -o "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }
& $aapt2 link -I $platformJar --manifest $manifest --java "$out\gen" -o "$out\resources.ap_" "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

# ── 2. compile java ──────────────────────────────────────────────────────────
$sources = Get-ChildItem $src -Recurse -Filter *.java | ForEach-Object { $_.FullName }
$rjava = Get-ChildItem "$out\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName }
& "$Jdk\bin\javac.exe" --release 8 -encoding UTF-8 -cp $platformJar -d "$out\obj" @($sources + $rjava)
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

# ── 3. dex ───────────────────────────────────────────────────────────────────
$classes = Get-ChildItem "$out\obj" -Recurse -Filter *.class | ForEach-Object { $_.FullName }
$env:JAVA_HOME = $Jdk
& cmd /c "set JAVA_HOME=$Jdk&& `"$d8bat`" --release --min-api 26 --lib `"$platformJar`" --output `"$out\apk`" $($classes -join ' ')"
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

# ── 4. package: resources + classes.dex ──────────────────────────────────────
$apkUnaligned = "$out\axdroid-unsigned.apk"
Copy-Item "$out\resources.ap_" $apkUnaligned -Force
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($apkUnaligned, 'Update')
$entry = $zip.CreateEntry("classes.dex")
$stream = $entry.Open()
$bytes = [System.IO.File]::ReadAllBytes("$out\apk\classes.dex")
$stream.Write($bytes, 0, $bytes.Length)
$stream.Close()
$zip.Dispose()

# ── 5. align + sign (debug key) ──────────────────────────────────────────────
$apkAligned = "$out\axdroid-aligned.apk"
& $zipalign -f 4 $apkUnaligned $apkAligned
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }
$keystore = "$out\debug.keystore"
if (-not (Test-Path $keystore)) {
    & "$Jdk\bin\keytool.exe" -genkeypair -keystore $keystore -storepass android -keypass android `
        -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 `
        -dname "CN=Android Debug,O=Android,C=US" | Out-Null
}
& cmd /c "set JAVA_HOME=$Jdk&& `"$apksigner`" sign --ks `"$keystore`" --ks-pass pass:android --key-pass pass:android `"$apkAligned`""
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }
$apk = Join-Path $here "axdroid.apk"
Copy-Item $apkAligned $apk -Force
Write-Host "OK → $apk"
Write-Host "install:  adb install -r `"$apk`""
