# Code Signing (Installer)

This project can sign `AutoCutterSetup.exe` during installer build.

## Recommended (Production)

Use a real code-signing certificate (`.pfx`) from a trusted CA.

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build-installer.ps1 `
  -BuildAppFirst `
  -PfxPath "C:\cert\your-cert.pfx" `
  -PfxPassword "YOUR_PASSWORD"
```

Optional:

- `-SignToolPath "C:\Program Files (x86)\Windows Kits\10\bin\10.0.xxxxx.0\x64\signtool.exe"`
- `-TimestampUrl "http://timestamp.digicert.com"`

If `signtool.exe` is not installed, the script automatically falls back to PowerShell Authenticode signing.

## Test Only (Self-signed)

Generate a local test certificate:

```powershell
powershell -ExecutionPolicy Bypass -File .\build\create-test-signing-cert.ps1 `
  -PfxPassword "ChangeMe-StrongPassword-123!"
```

Then sign with that PFX.  
Note: self-signed certs do **not** remove SmartScreen/unknown publisher warnings on end-user machines.
