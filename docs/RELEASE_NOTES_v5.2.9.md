# PGClockBot v5.2.9 — Release Notes

**Tag:** (pending)  
**App version:** `5.2.9`  
**Restore point:** branch `cursor/restore-before-home-pulse-5b2d` @ `v5.2.8` / `72a2570`

---

## Dashboard Pulse

Home is no longer a warehouse of stat cards. One composition:

1. **Pulse strip** — CPU/RAM + Bot / PasarGuard / Nodes (admin); Bot / PG + optional PAYG + quota gauges (reseller).
2. **Three vitals** — pending receipts, open tickets, revenue (click-through).
3. **Light foot** — link to user-behavior details + a few shortcuts.

Full bot/PG grids and the funnel panel are removed from `/home` (still available on overview / finance).

## Performance

- `build_home_overview(..., lite=True)` skips PasarGuard admin/group/host list fetches (nodes only).
- No `funnel_summary` query on home load.

## Security / tenancy

- Platform KPIs stay platform-scoped (`bot_panel_summary`).
- Reseller stats stay `shop_owner_id`-scoped; tenant bot token only (never main `BOT_TOKEN`).
- Reseller PG block only when that staff has PG credentials / limits.

## Deploy

After merge: deploy `5.2.9`, hard-refresh `/home` in light/dark and mobile. To roll back UI: checkout `cursor/restore-before-home-pulse-5b2d`.
