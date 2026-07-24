# Microsoft Store / MSIX Migration

This app is now wired for Microsoft Store entitlement (no VPS login required).

## Runtime license model

- Trial: 3 days (`AUTOCUTTER_STORE_TRIAL_DAYS`, default `3`)
- Subscription: Monthly (`AUTOCUTTER_STORE_MONTHLY_STORE_ID`)
- One-time: Lifetime (`AUTOCUTTER_STORE_LIFETIME_STORE_ID`)
- License checks:
  - every 24h normally
  - every 1h in last 24h before trial/subscription expiry

If no Store add-on entitlement is found, app uses local trial state.

## Required Partner Center data

Before final Store release, fill:

- `AUTOCUTTER_STORE_MONTHLY_STORE_ID`
- `AUTOCUTTER_STORE_LIFETIME_STORE_ID`
- MSIX `IdentityName` (Package/Identity from Partner Center)
- MSIX `Publisher` (must match certificate publisher exactly)

## Build MSIX

From project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build-msix.ps1 `
  -Version 1.0.0.0 `
  -IdentityName "<PartnerCenterIdentityName>" `
  -Publisher "<PublisherFromPartnerCenter>" `
  -DisplayName "Auto-Cutter" `
  -PublisherDisplayName "Auto-Cutter"
```

Optional signing:

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build-msix.ps1 `
  -Version 1.0.0.0 `
  -IdentityName "<PartnerCenterIdentityName>" `
  -Publisher "<PublisherFromPartnerCenter>" `
  -PfxPath "C:\path\codesign.pfx" `
  -PfxPassword "<password>"
```

If you already have `dist\AutoCutter` and want only repack:

```powershell
powershell -ExecutionPolicy Bypass -File .\build\build-msix.ps1 `
  -SkipBuild `
  -Version 1.0.0.0 `
  -IdentityName "<PartnerCenterIdentityName>" `
  -Publisher "<PublisherFromPartnerCenter>"
```

## Notes

- In unpackaged local runs, service falls back to simulator mode automatically.
- For strict local Store API testing, set:
  - `AUTOCUTTER_LICENSE_MODE=store`
  - `AUTOCUTTER_STORE_ALLOW_UNPACKAGED=1`
