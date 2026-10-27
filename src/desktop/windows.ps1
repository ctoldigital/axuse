# axuse Windows desktop driver.
# Lineage: ZCode's Computer Use Helper — a separate native-privileged process
# that owns input/screen grants while the agent-side stays credential-free.
# Here: a resident PowerShell process with a line-delimited JSON protocol.
# Input uses SendInput (trusted OS input events); screenshots are physical-pixel
# JPEG via GDI+ (DPI-aware). 64-bit PowerShell required (INPUT union layout).
#
# One op per line in, one JSON object per line out. Ops:
#   screen_info | screenshot | move | click | down | up | wheel |
#   key_down | key_up | key_tap | key_text | windows | activate

$ErrorActionPreference = 'Stop'
try {
  [Console]::InputEncoding = [System.Text.Encoding]::UTF8
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

if (-not [Environment]::Is64BitProcess) {
  [Console]::Out.WriteLine(('{"ok":false,"error":"64-bit PowerShell required"}' | ConvertTo-Json -Compress))
  exit 1
}

Add-Type -AssemblyName System.Drawing

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public class AxWin {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int nIndex);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder sb, int max);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextLength(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);

  public const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
  public const uint MOUSEEVENTF_LEFTDOWN = 0x0002, MOUSEEVENTF_LEFTUP = 0x0004;
  public const uint MOUSEEVENTF_RIGHTDOWN = 0x0008, MOUSEEVENTF_RIGHTUP = 0x0010;
  public const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020, MOUSEEVENTF_MIDDLEUP = 0x0040;
  public const uint MOUSEEVENTF_WHEEL = 0x0800, MOUSEEVENTF_HWHEEL = 0x1000;
  public const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004;

  [StructLayout(LayoutKind.Sequential)]
  public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)]
  public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Explicit)]
  public struct INPUT { [FieldOffset(0)] public uint type; [FieldOffset(8)] public MOUSEINPUT mi; [FieldOffset(8)] public KEYBDINPUT ki; }

  public static void Send(int type, MOUSEINPUT mi) {
    INPUT[] inp = new INPUT[1];
    inp[0].type = (uint)type; inp[0].mi = mi;
    SendInput(1, inp, Marshal.SizeOf(typeof(INPUT)));
  }
  public static void SendKey(KEYBDINPUT ki) {
    INPUT[] inp = new INPUT[1];
    inp[0].type = INPUT_KEYBOARD; inp[0].ki = ki;
    SendInput(1, inp, Marshal.SizeOf(typeof(INPUT)));
  }
  public static void Button(uint down, uint up) {
    MOUSEINPUT m = new MOUSEINPUT();
    m.dwFlags = down; Send((int)INPUT_MOUSE, m);
    m.dwFlags = up;   Send((int)INPUT_MOUSE, m);
  }
  public static void PressPair(uint down, uint up, int intervalMs) {
    MOUSEINPUT m = new MOUSEINPUT();
    m.dwFlags = down; Send((int)INPUT_MOUSE, m);
    if (intervalMs > 0) System.Threading.Thread.Sleep(intervalMs);
    m.dwFlags = up;   Send((int)INPUT_MOUSE, m);
  }
  public static void ClickButton(string button, bool dbl, int intervalMs) {
    uint down, up;
    switch (button) {
      case "right":  down = MOUSEEVENTF_RIGHTDOWN;  up = MOUSEEVENTF_RIGHTUP;  break;
      case "middle": down = MOUSEEVENTF_MIDDLEDOWN; up = MOUSEEVENTF_MIDDLEUP; break;
      default:       down = MOUSEEVENTF_LEFTDOWN;   up = MOUSEEVENTF_LEFTUP;   break;
    }
    PressPair(down, up, intervalMs);
    if (dbl) { System.Threading.Thread.Sleep(Math.Max(30, intervalMs)); PressPair(down, up, intervalMs); }
  }
  public static void Wheel(int dx, int dy) {
    if (dy != 0) {
      MOUSEINPUT m = new MOUSEINPUT();
      m.dwFlags = MOUSEEVENTF_WHEEL;
      unchecked { m.mouseData = (uint)dy; }
      Send((int)INPUT_MOUSE, m);
    }
    if (dx != 0) {
      MOUSEINPUT m = new MOUSEINPUT();
      m.dwFlags = MOUSEEVENTF_HWHEEL;
      unchecked { m.mouseData = (uint)dx; }
      Send((int)INPUT_MOUSE, m);
    }
  }
  public static void KeyTap(ushort vk) {
    KEYBDINPUT k = new KEYBDINPUT();
    k.wVk = vk; SendKey(k);
    System.Threading.Thread.Sleep(10);
    k.dwFlags = KEYEVENTF_KEYUP; SendKey(k);
  }
  public static void KeyUpDown(ushort vk, bool up) {
    KEYBDINPUT k = new KEYBDINPUT();
    k.wVk = vk; if (up) k.dwFlags = KEYEVENTF_KEYUP; SendKey(k);
  }
  public static void TypeText(string text) {
    foreach (char c in text) {
      KEYBDINPUT down = new KEYBDINPUT();
      down.wScan = c; down.dwFlags = KEYEVENTF_UNICODE; SendKey(down);
      KEYBDINPUT up = new KEYBDINPUT();
      up.wScan = c; up.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP; SendKey(up);
      System.Threading.Thread.Sleep(5);
    }
  }
  public static string[] ListWindows() {
    var list = new List<string>();
    EnumWindows((h, l) => {
      if (IsWindowVisible(h)) {
        int len = GetWindowTextLength(h);
        if (len > 0) {
          var sb = new StringBuilder(len + 2);
          GetWindowText(h, sb, sb.Capacity);
          uint pid; GetWindowThreadProcessId(h, out pid);
          string t = (sb.ToString() ?? "").Replace((char)9, ' ').Trim();
          if (t.Length > 0) list.Add(h.ToInt64() + ((char)9).ToString() + pid + ((char)9).ToString() + t);
        }
      }
      return true;
    }, IntPtr.Zero);
    return list.ToArray();
  }
}
"@

[void][AxWin]::SetProcessDPIAware()

function Get-VirtualScreen {
  $x = [AxWin]::GetSystemMetrics(76); $y = [AxWin]::GetSystemMetrics(77)
  $w = [AxWin]::GetSystemMetrics(78); $h = [AxWin]::GetSystemMetrics(79)
  @{ x = $x; y = $y; w = $w; h = $h }
}

function Invoke-Screenshot($req) {
  $vs = Get-VirtualScreen
  $x = if ($null -ne $req.x) { [int]$req.x } else { [int]$vs.x }
  $y = if ($null -ne $req.y) { [int]$req.y } else { [int]$vs.y }
  $w = if ($null -ne $req.w) { [int]$req.w } else { [int]$vs.w }
  $h = if ($null -ne $req.h) { [int]$req.h } else { [int]$vs.h }
  $quality = if ($null -ne $req.quality) { [int]$req.quality } else { 80 }
  if ($w -lt 1 -or $h -lt 1) { return @{ ok = $false; error = "bad capture rect" } }
  try {
    $bmp = New-Object System.Drawing.Bitmap($w, $h)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($x, $y, 0, 0, (New-Object System.Drawing.Size($w, $h)))
    $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    $codec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' }
    $ep = New-Object System.Drawing.Imaging.EncoderParameters(1)
    $ep.Param[0] = New-Object System.Drawing.Imaging.EncoderParameter([System.Drawing.Imaging.Encoder]::Quality, [long]$quality)
    $bmp.Save($ms, $codec, $ep)
    $bmp.Dispose()
    $ep.Dispose()
    @{ ok = $true; base64 = [Convert]::ToBase64String($ms.ToArray()); mime = 'image/jpeg' }
  } catch {
    @{ ok = $false; error = "capture failed: $($_.Exception.Message)" }
  }
}

function Invoke-Op($req) {
  switch ($req.op) {
    'screen_info' {
      $vs = Get-VirtualScreen
      @{ ok = $true; x = [int]$vs.x; y = [int]$vs.y; w = [int]$vs.w; h = [int]$vs.h }
    }
    'screenshot'  { Invoke-Screenshot $req }
    'move'   { [void][AxWin]::SetCursorPos([int]$req.x, [int]$req.y); @{ ok = $true } }
    'click'  {
      [void][AxWin]::SetCursorPos([int]$req.x, [int]$req.y)
      $interval = if ($null -ne $req.interval_ms) { [int]$req.interval_ms } else { 0 }
      [AxWin]::ClickButton([string]$req.button, [bool]$req.double, $interval)
      @{ ok = $true }
    }
    'down'   {
      if ($req.button -eq 'right') { [AxWin]::Button([AxWin]::MOUSEEVENTF_RIGHTDOWN, [AxWin]::MOUSEEVENTF_RIGHTDOWN) }
      elseif ($req.button -eq 'middle') { [AxWin]::Button([AxWin]::MOUSEEVENTF_MIDDLEDOWN, [AxWin]::MOUSEEVENTF_MIDDLEDOWN) }
      else { [AxWin]::Button([AxWin]::MOUSEEVENTF_LEFTDOWN, [AxWin]::MOUSEEVENTF_LEFTDOWN) }
      @{ ok = $true }
    }
    'up'     {
      if ($req.button -eq 'right') { [AxWin]::Button([AxWin]::MOUSEEVENTF_RIGHTUP, [AxWin]::MOUSEEVENTF_RIGHTUP) }
      elseif ($req.button -eq 'middle') { [AxWin]::Button([AxWin]::MOUSEEVENTF_MIDDLEUP, [AxWin]::MOUSEEVENTF_MIDDLEUP) }
      else { [AxWin]::Button([AxWin]::MOUSEEVENTF_LEFTUP, [AxWin]::MOUSEEVENTF_LEFTUP) }
      @{ ok = $true }
    }
    'wheel'  { [AxWin]::Wheel([int]$req.dx, [int]$req.dy); @{ ok = $true } }
    'key_down' { [AxWin]::KeyUpDown([int]$req.vk, $false); @{ ok = $true } }
    'key_up'   { [AxWin]::KeyUpDown([int]$req.vk, $true);  @{ ok = $true } }
    'key_tap'  { [AxWin]::KeyTap([int]$req.vk); @{ ok = $true } }
    'key_text' { [AxWin]::TypeText([string]$req.text); @{ ok = $true } }
    'windows'  {
      $rows = [AxWin]::ListWindows() | ForEach-Object {
        $p = $_ -split "`t", 3
        [ordered]@{ hwnd = [int64]$p[0]; pid = [int]$p[1]; title = [string]$p[2] }
      }
      @{ ok = $true; windows = @($rows) }
    }
    'activate' {
      $ok = $false
      if ($null -ne $req.hwnd) { $ok = [AxWin]::SetForegroundWindow([IntPtr][int64]$req.hwnd) }
      elseif ($null -ne $req.title) {
        $match = [AxWin]::ListWindows() | ForEach-Object {
          $p = $_ -split "`t", 3
          [pscustomobject]@{ hwnd = [int64]$p[0]; title = [string]$p[2] }
        } | Where-Object { $_.title -like "*$($req.title)*" } | Select-Object -First 1
        if ($match) { $ok = [AxWin]::SetForegroundWindow([IntPtr][int64]$match.hwnd) }
      }
      if ($ok) { @{ ok = $true } } else { @{ ok = $false; error = 'activate failed (window not found or foreground lock)' } }
    }
    default { @{ ok = $false; error = "unknown op $($req.op)" } }
  }
}

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  if ([string]::IsNullOrWhiteSpace($line)) { continue }
  $res = $null
  try { $res = Invoke-Op ($line | ConvertFrom-Json) }
  catch { $res = @{ ok = $false; error = "$_" } }
  try {
    [Console]::Out.WriteLine(($res | ConvertTo-Json -Compress -Depth 6))
    [Console]::Out.Flush()
  } catch { break }
}
