# KB-EMAIL-001: Email / Exchange Issues

**Category:** Email  
**Tags:** Outlook, Exchange, email sync, mobile, mailbox  
**Last Updated:** 2024-01-10  

## Symptoms
- Outlook mobile app not syncing emails
- Emails stuck in Outbox
- Cannot connect to Exchange server
- Mobile device prompting for password repeatedly

## Root Causes
1. Exchange ActiveSync policy conflict on mobile device
2. Mailbox quota exceeded (emails bounce or fail to sync)
3. OAuth token expired — requires re-authentication
4. Exchange server maintenance window active

## Resolution Steps

### Step 1 — Check Exchange server status (1 min)
Check https://exchange-status.zensar.internal. If maintenance is active, inform user and give ETA.

### Step 2 — Remove and re-add account on mobile (5 min)
iOS: Settings → Mail → Accounts → [Corporate Account] → Delete Account → Add Account → Exchange → enter credentials.  
Android: Settings → Accounts → [Corporate Account] → Remove → Add Account → Exchange.

### Step 3 — Check mailbox quota (2 min)
In Exchange Admin Center: find user → mailbox properties → check storage. If > 90% full, increase quota by 500MB or archive old emails.

### Step 4 — Force sync (1 min)
In Outlook mobile: pull down on inbox to force sync. Check Settings → Mail → Fetch New Data → set to Push.

## Auto-Resolve Eligibility
Email sync issues (Steps 2-4) are **L1 auto-resolvable** if Exchange server is healthy.

## SLA
- P3: Resolve within 8 hours
- Assignment Group: Email-Support
