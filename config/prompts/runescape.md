# OSRS - Grand Exchange Trading & Tools

Everything related to delta's Old School RuneScape GE trading, portfolio management, and associated services.

## GE Advisor - THIS IS IMPORTANT
- Script: `/data/wendy/channels/coding/ge-bond-tracker/ge_advisor.py`
- Portfolio data: `/data/wendy/channels/coding/ge-bond-tracker/portfolios.json`
- Function: Unified generic portfolio tracker for delta's OSRS GE trading. Tracks ANY item (not hardcoded to specific items). Manages GE orders, analyzes price history across multiple timeframes (30d/90d/365d), generates reports.
- **"Lunch Break" Workflow**: When delta asks "what should I buy/sell" or "it's runescape time" or anything similar:
  **CRITICAL: Follow the step-by-step guide below (Lunch Break Workflow section)**
  Do NOT oneshot the analysis. Work through it in discrete steps, writing findings to files as you go.
  1. Create a timestamped session folder: `mkdir -p ge-bond-tracker/sessions/$(date +%Y%m%d_%H%M)`
  2. Run the report, save to session folder
  3. Generate tiled chart, copy to session folder
  4. Work through steps 1-6 in the Lunch Break Workflow, writing notes at each step
  5. Read your accumulated notes, then compose the final Discord message
  6. Attach the tiled chart image. Do NOT spam individual charts.
- CLI commands: `report` (full analysis), `summary` (portfolio overview), `analyze <id> [days]`, `price <id>`
- Delta's strategy: flip consumables (sara brews, super restores, karambwan, etc.) in short cycles, hold bonds long-term, keep >33% GP liquid. Strategy evolves - check current portfolio for what she's actually trading.
- Delta's membership: started 2026-02-12, may extend with untradeable bonds
- Legacy scripts (bond_monitor, whip_advisor, lcd_ticker, etc.) moved to `deprecated/` subfolder

## IMPORTANT Trading Context (READ THIS)
- **NEVER RECOMMEND SELLING AT A LOSS** unless the situation is genuinely dire (structural collapse, no recovery catalyst visible). Always check avg_buy and break_even_sell from portfolios.json BEFORE suggesting any sell price. If net_after_tax (sell * 0.98) < avg_buy, that's a loss. Delta would rather hold for a month than sell 4 days after buying at a loss. Only recommend cutting losses when it's clearly hopeless, and explicitly flag it as such with the exact GP lost.
- **Bonds** = stable long-term investment. Delta trades bonds even when NOT actively playing, on a F2P account. Bonds are always tradeable regardless of membership status.
- **Everything else** (brews, super restores, karambwan, etc.) = flipping that requires active membership. Delta may extend membership using untradeable bonds - do NOT assume she's selling everything before it lapses. Ask her or check her latest message for intent.
- **GE orders persist after membership lapses.** Sell orders placed while a member stay active on F2P. They will still fill at the listed price. You just can't create NEW member-item sell orders on F2P. This means there is NO deadline pressure to lower prices before membership expires - orders can sit indefinitely. Only rush sells if the item's profit outlook is genuinely hopeless.
- **Delta's GP may change independently** - she plays the game normally and buys stuff outside of GE trading. She'll tell you her current GP when she asks for advice. Don't assume the portfolio file is perfectly up to date without confirming.
- **The automated report is a SUGGESTION** - the scores and recommendations in `ge_advisor.py report` are starting points. YOU must consider the data yourself, with your LLM brain, before making actionable suggestions. Look at the actual numbers, trends, and context. Don't just parrot the script's output.
- Portfolio tracking and recommendations happen in direct conversation initiated by delta.
- **ALWAYS check 30d, 90d, AND 365d charts** before making any recommendations. Short-term data alone can be misleading (e.g. a "floor" on 30d might be a crash on 90d).
- **GE TAX: 2% on all sells.** Always factor this into margin calculations. Minimum profitable margin is ~2.04% above buy price. Big-ticket items with thin margins (e.g. whips) can lose money after tax even if sell price > buy price. High-volume consumable flips handle tax better.
- **BOND CONVERSION FEE: 10% of GE value** to convert untradeable bonds to tradeable. This is a HUGE hidden cost. When calculating bond ROI, the true cost basis per tradeable bond = buy_price + (current_GE_value * 0.10). Then selling also incurs the 2% GE tax. Break-even sell = (buy_price + conversion_fee) / 0.98. At current prices (~14M), conversion costs ~1.4M per bond, making bond flipping much harder than it looks.

## Chart Generator
- Script: `/data/wendy/channels/coding/ge-bond-tracker/chart.py`
- Supports: bond, whip, ranging, eternal, sotd, zenyte, primordial, suffering, ancestral_hat, dclaws, super_restore, armadyl_godsword, berserker_ring, magic_logs, shark, cannonball, dragon_bones, black_chinchompa, saradomin_brew, ghrazi_rapier, nature_rune, death_rune, blood_rune (extensible via ITEMS dict)
- **Single chart**: `python3 chart.py <item> <days>` (e.g., `chart.py whip 30`)
- **Tiled chart (PREFERRED)**: `python3 chart.py <item1,item2,...> tiled` - generates NxM grid (rows=items, cols=30d/90d/365d)
- Uses Weird Gloop API: `last90d` endpoint for <=90d, `all` endpoint for >90d (data back to 2015)
- Tiled outputs go to `ge-bond-tracker/<items>_tiled.png`

## Hiscores API
Pull delta's live stats:
```bash
curl -s -A "Mozilla/5.0" "https://secure.runescape.com/m=hiscore_oldschool/index_lite.ws?player=Santa%20Pawbs"
```
Returns rank,level,xp per line. Skill order: Overall, Attack, Defence, Strength, Hitpoints, Ranged, Prayer, Magic, Cooking, Woodcutting, Fletching, Fishing, Firemaking, Crafting, Smithing, Mining, Herblore, Agility, Thieving, Slayer, Farming, Runecrafting, Hunter, Construction. Then boss KCs and activities.

## Portfolio Management
- Edit `portfolios.json` directly when delta reports trades. Track GP, inventory, GE orders, and completed trades.
- **ALWAYS push to Pi after editing**: `python3 /data/wendy/channels/coding/ge-bond-tracker/push_portfolio.py` (ge_advisor.py does this automatically via save_portfolio() and report command).
- Portfolio data: `/data/wendy/channels/coding/ge-bond-tracker/portfolios.json`

## LCD Ticker (runs on testpi)
- Runs on Pi (`lcd-server/pi-lcd-ticker.js`), NOT on Wendy's machine
- Pi reads `portfolio.json` locally, polls Wiki API, updates display every 5 min
- Wendy's only job is pushing updated portfolio data to the Pi when it changes
- The old `lcd_ticker.py` daemon is deprecated - do NOT restart it
- **IMPORTANT: After uploading updated ticker code to the Pi, ALWAYS restart the service:**
  ```bash
  python3 /data/wendy/channels/coding/pi-ssh.py exec "sudo systemctl restart wendy-lcd-ticker"
  ```
  Verify it's running: `python3 /data/wendy/channels/coding/pi-ssh.py exec "systemctl is-active wendy-lcd-ticker"`
- Ticker source on Pi: `/workbench/receive/lcd-server/pi-lcd-ticker.js`
- Upload command: `python3 pi-ssh.py upload /data/wendy/channels/coding/ge-bond-tracker/lcd-server/pi-lcd-ticker.js /workbench/receive/lcd-server/pi-lcd-ticker.js`

## Timestamps
All timestamps are stored internally in UTC (ISO format). When displaying times TO DELTA, convert to Pacific (Seattle) time: `YYYY-MM-DD HH:MM AM/PM (PST/PDT)`. The report script handles this automatically via `utc_to_pacific()`. When writing timestamps in Discord messages, also use Pacific time. DST is handled by `zoneinfo.ZoneInfo("America/Los_Angeles")`.

---

# Lunch Break Workflow - Step-by-Step Guide

When delta says "do the thing" or asks for trading advice, follow these steps
IN ORDER. Each step writes findings to files in a timestamped session folder.
Read your previous step files before starting the next step.

## Setup

```bash
# Create session folder
SESSION=$(date +%Y%m%d_%H%M)
mkdir -p /data/wendy/channels/coding/ge-bond-tracker/sessions/$SESSION
cd /data/wendy/channels/coding/ge-bond-tracker
```

Send a quick "on it" message to Discord so delta knows you're working.

## Step 1: Raw Data Collection

Run the report and save it:
```bash
python3 ge_advisor.py report > sessions/$SESSION/01_report.txt 2>&1
```

Generate tiled chart (adjust items to match current portfolio):
```bash
python3 chart.py <items> tiled
cp *_tiled.png sessions/$SESSION/
```

Also generate INDIVIDUAL item charts for Gemini analysis:
```bash
for item in bond saradomin_brew super_restore karambwan; do
  python3 chart.py $item tiled
  cp ${item}_tiled.png sessions/$SESSION/
done
```

Send each individual item chart to analyze_file SEPARATELY (not the composite).
This gives Gemini focused context per item instead of analyzing 16 graphs at once.
The full composite tiled chart is still what gets attached to the final Discord message.

**Write** `sessions/$SESSION/01_notes.md`:
- List all current GE orders with ages
- Note any orders flagged STALE
- Record current prices vs sell prices (how far off are they?)
- Record reserve ratio
- Note anything surprising in the raw data

## Step 2: Market Microstructure

For each item in the portfolio, look at the NEW data points:
- Spread (tight/moderate/wide)
- 5m volume split (buy pressure vs sell pressure)
- 6h intraday direction

**Write** `sessions/$SESSION/02_microstructure.md`:
- Per-item: is the market actively moving toward or away from your sell price?
- Flag any items with wide spreads (>3%) - these are illiquid
- Flag any items with strong sell pressure - prices may drop further
- Flag any items with buy pressure + upward intraday - fills may be coming

## Step 3: Cross-Item Comparison

Look at the MARKET SCANNER section. Compare held items to similar items:
- Are all potions moving the same way? (brews vs restores vs ranging pots)
- Are all foods moving together? (karambwan vs sharks vs anglerfish)
- Is this a broad market downturn or item-specific?

**Write** `sessions/$SESSION/03_cross_item.md`:
- Category trends observed
- Any outliers (item moving opposite to its category)
- What this means for timing sells

## Step 4: News & Events Research

Search for recent OSRS news. Check:
- Game updates this week
- Upcoming content (next 2 weeks)
- Active events (DMM, Leagues, etc.)
- Any relevant Reddit/community buzz

**Write** `sessions/$SESSION/04_news.md`:
- Relevant news items
- Expected price impact per item
- Timeline of upcoming events

## Step 5: Historical Event Verification

If there's a relevant event (e.g. DMM ending), look up what happened last time:
```bash
python3 chart.py <item> 365  # Check around previous event dates
```

**Write** `sessions/$SESSION/05_historical.md`:
- What happened to relevant item prices during previous similar events
- Dates and price movements
- Whether the current situation matches or differs

## Step 6: Synthesis & Recommendation

Read ALL previous step files (01 through 05). Then write your final analysis.

**CRITICAL: BREAK-EVEN CHECK**
Before recommending ANY sell price change, calculate the ACTUAL P&L:
- Look up avg_buy and break_even_sell from portfolios.json for each item
- Net after tax = recommended_sell * 0.98
- If net < avg_buy, you are recommending a LOSS. Do NOT recommend this unless:
  1. The item's prospects are genuinely dire (structural decline, no recovery catalyst)
  2. AND you explicitly flag it as "cutting losses" with the exact GP amount lost
- Default to PATIENCE over locking in losses. Delta would rather hold for a month
  than sell 4 days after buying at a loss.
- GE sell orders persist after membership lapses. There is NO deadline to rush
  sells before membership expires. Orders sit indefinitely on F2P.
- The break-even check is YOUR INTERNAL safeguard. Do NOT waste report characters
  constantly reminding delta that orders are "above break-even" - she knows.
  Only mention break-even if you are recommending a LOSS (cutting losses).

**Write** `sessions/$SESSION/06_recommendation.md`:
- Per-item recommendation with reasoning
- For each sell recommendation: show buy price, break-even, recommended sell, net after tax, P&L
- Overall portfolio action plan
- TL;DR action items
- Confidence level for each recommendation

## Step 7: Send to Discord

Compose your Discord message in the SAME FORMAT as before:
- Portfolio summary with current positions and GP
- Per-item price analysis and recommendation
- Overall strategy notes
- TL;DR action items at the bottom
- Attach the tiled chart

The internal steps are for YOUR analysis process. The final output to delta
should look the same as it always has - one cohesive report, not a dump of
your working notes.

**TWO MESSAGES, ALWAYS.**

**Message 1 (self-contained report):** The main report. Stands on its own -
shareable, pinnable, complete. Portfolio summary, per-item analysis, TL;DR
action items, tiled chart attached. ALWAYS include a confidence ranking for
each item (e.g. Super Restore HIGH > Sara Brew MODERATE > Karambwan LOW).
Keep under 2000 chars.

**Message 2 (elaboration):** Deeper reasoning and nuance. WHY you're making
each recommendation, what the charts show, event impacts, risk factors,
confidence levels, anything that didn't fit in the clean summary. This is
where you spell things out. No char limit pressure - be thorough.

Both messages are sent every time. Message 1 is the quick-reference report.
Message 2 is the "here's my thinking" companion.

---

## Why This Workflow Exists

The old workflow tried to oneshot everything: run report, search news, analyze
charts, and write recommendations all in one pass. As the portfolio and data
grew, this became too much to reliably process at once. Context compaction could
wipe out mid-analysis work. This step-by-step approach:

- Makes each step small enough to complete reliably
- Persists findings to disk so compaction can't erase them
- Creates an audit trail of reasoning
- Lets each step build on previous findings instead of juggling everything
