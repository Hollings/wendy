# Wendy Avatar - Phase 1 Tasks

## Goal
3D visualization of Wendy at her desk, typing on a keyboard, with a monitor displaying real session data from the brain feed.

---

## Current Status

**Working Infrastructure (keep):**
- `scene.js` - Floor, lights, camera, controls
- `monitor.js` - Canvas renderer for all display modes
- `states.js` - Complete state machine
- `stream.js` - WebSocket connection to brain feed
- `mock-data.js` - Test session data for replay
- `main.js` - App entry point with auth and stream connection

**Removed (rebuild from scratch):**
- wendy.js - Character model and IK
- keyboard.js - 3D keyboard
- typing.js - Typing animation controller

---

## Task List

### 1. Scene Objects
- [x] Floor/ground plane
- [x] Basic lighting
- [x] Camera with OrbitControls
- [ ] Desk/tabletop
- [ ] Monitor (frame + screen with canvas texture)

### 2. Keyboard
- [x] Keyboard base
- [x] Individual key meshes (full QWERTY layout)
- [x] Key position mapping (char → world position)
- [x] Key press animation
- [x] Debug panel with live typing + demo button

### 3. Wendy Body
- [x] Torso
- [x] Head
- [x] Eyes
- [x] Upper arms (cylinders)
- [x] Forearms (tapered cones)
- [x] Wired into main.js

### 4. IK Arm System
- [x] 2-bone IK solver (Law of Cosines)
- [x] Debug spheres (toggleable)
- [x] Debug panel with X/Y/Z sliders
- [x] Pole vector for elbow direction
- [ ] Elbow constraints (above keyboard plane)
- [ ] Target key positions

### 5. Typing Animation
- [x] Character → arm targeting
- [x] Left/right hand coordination
- [x] Key press sync (arm reaches key, then keypress triggers)
- [x] Smooth arm movement (lerp to target)

### 6. Integration
- [ ] Wire state machine to visuals
- [ ] Wire stream events to monitor
- [ ] Test with mock replay

---

## Notes

- Build and test each component before moving on
- Debug spheres are essential for IK development
- Elbow constraints: arms must stay above keyboard plane
- Use `?mock` URL param for testing
