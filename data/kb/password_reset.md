# KB-ACC-001: Active Directory Password Reset & Account Unlock

**Category:** Access  
**Tags:** password, AD, account lockout, MFA  
**Last Updated:** 2024-01-10  

## Symptoms
- User cannot login — "Your account has been locked"
- Password expired notification
- MFA app not generating codes
- "The username or password is incorrect" after password change

## Root Causes
1. Account locked after 5 failed login attempts (AD policy)
2. Password expired (90-day policy)
3. MFA authenticator app not synced (time drift > 30 seconds)
4. Cached credentials on device not updated after password change

## Resolution Steps

### Step 1 — Unlock AD account (L1 — 2 min)
Access tool: Active Directory Users and Computers → find user → Properties → Account tab → check "Unlock account".  
Or via PowerShell: `Unlock-ADAccount -Identity <username>`

### Step 2 — Reset password (L1 — 2 min)
Right-click user in ADUC → Reset Password → set temporary password → tick "User must change password at next logon".  
Communicate temporary password via phone call only — never email.

### Step 3 — Resync MFA app (L1 — 5 min)
If MFA codes not working:  
1. Check device time is set to automatic/sync with internet
2. In Microsoft Authenticator → tap account → Refresh codes
3. If still failing: in Azure AD portal, remove and re-register MFA for user
4. User re-scans QR code from https://aka.ms/mfasetup

### Step 4 — Clear cached credentials (2 min)
Windows: Control Panel → Credential Manager → Windows Credentials → remove entries for corporate domain.  
Then login fresh with new password.

## Auto-Resolve Eligibility
Password reset and account unlock are **fully L1 auto-resolvable**. Resolution Agent should:
1. Confirm identity via employee ID (not via email)
2. Perform reset via AD tool
3. Communicate temp password via phone
4. Log action in ServiceNow

## SLA
- P2: Resolve within 4 hours  
- P1 (if affecting critical service account): Resolve within 1 hour
- Assignment Group: Service-Desk
