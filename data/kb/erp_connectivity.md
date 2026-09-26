# KB-APP-001: ERP / SAP Connectivity Issues

**Category:** Application  
**Tags:** SAP, ERP, database, DBCON_FAIL, login error  
**Last Updated:** 2024-01-10  

## Symptoms
- SAP login fails with error DBCON_FAIL
- "Cannot connect to application server" message
- Multiple users affected simultaneously
- Slow response times in SAP transactions

## Root Causes
1. SAP application server down or restarting
2. Database connection pool exhausted (high concurrent users)
3. Network route to SAP server unavailable
4. SAP licence server unreachable

## Resolution Steps

### Step 1 — Check SAP system status (2 min)
Access SAP System Status dashboard at https://sap-monitor.zensar.internal. If status shows red, escalate immediately to App-Support.

### Step 2 — Verify it affects multiple users (1 min)
Ask user to confirm with 2-3 colleagues. If single user only, likely a local client issue — go to Step 4. If multiple users, this is a P1 — escalate immediately.

### Step 3 — P1 Escalation (immediate)
Multiple users affected by SAP failure is always P1:
1. Create P1 incident in ServiceNow
2. Page on-call SAP Basis team: escalation@zensar.internal
3. Notify affected department heads
4. Post status update to IT Status Portal every 15 minutes

### Step 4 — Single-user SAP client fix (5 min)
1. Clear SAP logon cache: SAP Logon → Options → SNC → Clear Cache
2. Delete %AppData%\SAP\Common folder
3. Reinstall SAP GUI if above fails (package: sap-gui-7.70.msi on software server)

## Auto-Resolve Eligibility
Single-user SAP issues (Step 4) are **L1 auto-resolvable**.  
Multi-user SAP outages are **always P1 — escalate, never auto-resolve**.

## SLA
- P1 (multi-user): Resolve within 1 hour
- P2/P3 (single user): Resolve within 4 hours
- Assignment Group: App-Support
