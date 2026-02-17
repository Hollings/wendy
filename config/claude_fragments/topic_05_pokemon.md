---
type: topic
order: 5
keywords: [pokemon, gameboy, pyboy, emulator, tileset, wram, "play.py"]
---
# Pokemon Red Emulator Project

## PyBoy Emulator Setup
- Emulator: PyBoy (Python GameBoy emulator)
- ROM: Pokemon Red
- Script: `play.py` in the coding channel workspace
- Uses PyBoy's memory access for reading game state (WRAM addresses)

## Key WRAM Addresses (Pokemon Red)
- Party Pokemon count: 0xD163
- Party species list: 0xD164-0xD169
- Current map: 0xD35E
- Player X/Y: 0xD362/0xD361
- Badge flags: 0xD356

## Tips
- PyBoy can run headless for automated play
- Screenshots via `pyboy.screen.ndarray`
- Save states with `pyboy.save_state()`
- Tile data accessible via `pyboy.tilemap_window()`
