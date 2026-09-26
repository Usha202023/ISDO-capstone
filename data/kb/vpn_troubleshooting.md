# KB-NET-001: VPN Connectivity Troubleshooting

**Category:** Network  
**Tags:** VPN, Cisco AnyConnect, remote access, authentication  
**Last Updated:** 2024-01-10  

## Symptoms
- VPN client fails to connect after password change
- Error message: "Authentication failed" or "Connection timed out"
- VPN works on some networks but not others

## Root Causes
1. AD password not synced to VPN gateway (most common after password reset)
2. Cisco AnyConnect client out of date
3. Split-tunnel policy blocking corporate DNS
4. Firewall rule blocking UDP 443

## Resolution Steps

### Step 1 — Verify password sync (2 min)
Ask user to try logging into the web VPN portal at https://vpn.zensar.internal using their new credentials. If portal login works but AnyConnect fails, proceed to Step 2.

### Step 2 — Restart AnyConnect service (1 min)
On Windows: Open Services → find "Cisco AnyConnect VPN Agent" → Restart.  
On Mac: Run `sudo launchctl stop com.cisco.anyconnect.vpnagentd` then start.

### Step 3 — Clear cached credentials (2 min)
Open AnyConnect → Preferences → clear saved passwords → reconnect.

### Step 4 — Update AnyConnect client (5 min)
Download latest client from https://software.zensar.internal/vpn → install → restart → retry.

### Step 5 — Escalate to Network-Ops
If none of the above resolves the issue, raise to Network-Ops team with:
- User's AD username
- VPN gateway name from error message
- Screenshot of error

## Auto-Resolve Eligibility
This issue is **L1 auto-resolvable** if root cause is password sync (Step 1 confirms portal works). Resolution Agent can send the Step 2–3 fix directly to user.

## SLA
- P2: Resolve within 4 hours
- Assignment Group: Network-Ops
