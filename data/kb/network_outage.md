# KB-NET-002: Network Outage — Switch / Infrastructure Failure

**Category:** Network  
**Tags:** network, switch, outage, building, connectivity  
**Last Updated:** 2024-01-10  

## Symptoms
- Multiple users in same physical area lose network connectivity
- Switch shows as unresponsive in monitoring dashboard
- No internet or internal network access for affected floor/building
- Wi-Fi drops intermittently in specific rooms

## Root Causes
1. Network switch hardware failure
2. Switch firmware crash (requires reboot)
3. Uplink cable disconnected or damaged
4. Power failure to network closet
5. Rogue DHCP server causing IP conflicts

## Resolution Steps

### Step 1 — Identify scope (2 min)
Determine: which floor/building/rooms affected? How many users? Check NMS at https://nms.zensar.internal for offline devices.

### Step 2 — Check physical layer (5 min)
Dispatch L1 tech or facilities to check:
- Network closet power status (UPS indicator lights)
- Switch panel indicator lights (look for amber/red port LEDs)
- Check uplink cable is seated

### Step 3 — Remote reboot attempt (3 min)
If switch has out-of-band management:  
`ssh admin@<switch-ip>` → `reload` → confirm → wait 3 minutes for boot.  
Note: this will cause a brief outage for all users on that switch.

### Step 4 — Hardware failure escalation
If switch does not come back online after reboot:
1. Escalate to Network-Ops senior engineer
2. Locate spare switch in Rack B of server room
3. ETA for replacement: 30-60 minutes

### Step 5 — P1 Declaration
If > 20 users affected: declare P1, follow Major Incident process.

## Auto-Resolve Eligibility
**Not L1 auto-resolvable.** Requires physical access or senior Network-Ops engineer.  
L1 role: document, triage, escalate, communicate to users.

## SLA
- P1 (> 20 users): Resolve within 1 hour
- P2 (5–20 users): Resolve within 4 hours
- Assignment Group: Network-Ops
