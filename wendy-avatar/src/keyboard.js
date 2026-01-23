/**
 * keyboard.js - 3D Keyboard with individual key meshes
 *
 * Features:
 * - Realistic QWERTY layout
 * - Individual key meshes for targeting
 * - Keypress animations
 * - Character to key position mapping
 */

import * as THREE from 'three';

// =============================================================================
// Configuration
// =============================================================================

const CONFIG = {
    // Key dimensions
    keySize: 0.04,          // Standard key width/depth
    keyHeight: 0.015,       // Key height
    keyGap: 0.006,          // Gap between keys
    keyRadius: 0.003,       // Corner radius (visual only, using box)

    // Key press animation
    pressDepth: 0.008,      // How far key travels down
    pressDuration: 80,      // Ms for key to go down
    releaseDuration: 100,   // Ms for key to come back up

    // Colors
    baseColor: 0x1a1a1a,
    keyColor: 0x2d2d2d,
    keyColorAlt: 0x252525,  // For alternating look
    keyColorAccent: 0x3d3d3d, // For special keys

    // Base plate
    baseWidth: 0.58,
    baseDepth: 0.22,
    baseHeight: 0.012,
    basePadding: 0.015,
};

// QWERTY Layout - each row with key definitions
// width: multiplier of standard key size (1 = normal key)
const LAYOUT = [
    // Row 0: Number row
    [
        { char: '`', width: 1 }, { char: '1', width: 1 }, { char: '2', width: 1 },
        { char: '3', width: 1 }, { char: '4', width: 1 }, { char: '5', width: 1 },
        { char: '6', width: 1 }, { char: '7', width: 1 }, { char: '8', width: 1 },
        { char: '9', width: 1 }, { char: '0', width: 1 }, { char: '-', width: 1 },
        { char: '=', width: 1 }, { char: 'Backspace', width: 1.8, special: true },
    ],
    // Row 1: QWERTY
    [
        { char: 'Tab', width: 1.4, special: true },
        { char: 'Q', width: 1 }, { char: 'W', width: 1 }, { char: 'E', width: 1 },
        { char: 'R', width: 1 }, { char: 'T', width: 1 }, { char: 'Y', width: 1 },
        { char: 'U', width: 1 }, { char: 'I', width: 1 }, { char: 'O', width: 1 },
        { char: 'P', width: 1 }, { char: '[', width: 1 }, { char: ']', width: 1 },
        { char: '\\', width: 1.4 },
    ],
    // Row 2: ASDF
    [
        { char: 'Caps', width: 1.6, special: true },
        { char: 'A', width: 1 }, { char: 'S', width: 1 }, { char: 'D', width: 1 },
        { char: 'F', width: 1 }, { char: 'G', width: 1 }, { char: 'H', width: 1 },
        { char: 'J', width: 1 }, { char: 'K', width: 1 }, { char: 'L', width: 1 },
        { char: ';', width: 1 }, { char: "'", width: 1 },
        { char: 'Enter', width: 2.0, special: true },
    ],
    // Row 3: ZXCV
    [
        { char: 'LShift', width: 2.1, special: true },
        { char: 'Z', width: 1 }, { char: 'X', width: 1 }, { char: 'C', width: 1 },
        { char: 'V', width: 1 }, { char: 'B', width: 1 }, { char: 'N', width: 1 },
        { char: 'M', width: 1 }, { char: ',', width: 1 }, { char: '.', width: 1 },
        { char: '/', width: 1 },
        { char: 'RShift', width: 2.5, special: true },
    ],
    // Row 4: Space bar row
    [
        { char: 'LCtrl', width: 1.3, special: true },
        { char: 'Win', width: 1.1, special: true },
        { char: 'LAlt', width: 1.2, special: true },
        { char: ' ', width: 6.0 }, // Space bar
        { char: 'RAlt', width: 1.2, special: true },
        { char: 'Win2', width: 1.1, special: true },
        { char: 'Menu', width: 1.1, special: true },
        { char: 'RCtrl', width: 1.3, special: true },
    ],
];

// Shift character mappings (for display purposes)
const SHIFT_MAP = {
    '1': '!', '2': '@', '3': '#', '4': '$', '5': '%',
    '6': '^', '7': '&', '8': '*', '9': '(', '0': ')',
    '-': '_', '=': '+', '[': '{', ']': '}', '\\': '|',
    ';': ':', "'": '"', ',': '<', '.': '>', '/': '?', '`': '~',
};

// Left hand keys (standard touch typing)
const LEFT_HAND_KEYS = new Set([
    '`', '1', '2', '3', '4', '5', '6',
    'Tab', 'Q', 'W', 'E', 'R', 'T',
    'Caps', 'A', 'S', 'D', 'F', 'G',
    'LShift', 'Z', 'X', 'C', 'V', 'B',
    'LCtrl', 'Win', 'LAlt',
    // Shifted versions
    '~', '!', '@', '#', '$', '%', '^',
]);

// =============================================================================
// Keyboard Class
// =============================================================================

export class Keyboard {
    constructor() {
        this.group = new THREE.Group();

        // Map: character -> key data { mesh, baseY, isPressed }
        this.keys = new Map();

        // All key meshes for iteration
        this.keyMeshes = [];

        // Animation queue
        this.animations = [];

        this._createBase();
        this._createKeys();
    }

    // =========================================================================
    // Creation
    // =========================================================================

    _createBase() {
        const { baseWidth, baseDepth, baseHeight, baseColor } = CONFIG;

        const geometry = new THREE.BoxGeometry(baseWidth, baseHeight, baseDepth);
        const material = new THREE.MeshStandardMaterial({
            color: baseColor,
            roughness: 0.9,
        });

        const base = new THREE.Mesh(geometry, material);
        base.position.y = baseHeight / 2;
        base.receiveShadow = true;
        this.group.add(base);
    }

    _createKeys() {
        const { keySize, keyGap, baseWidth, baseDepth, basePadding, baseHeight } = CONFIG;

        const totalRows = LAYOUT.length;
        const startY = baseHeight + CONFIG.keyHeight / 2;

        // Calculate total keyboard height for centering
        const totalKeyboardDepth = totalRows * (keySize + keyGap) - keyGap;
        let currentZ = -baseDepth / 2 + basePadding + keySize / 2;

        // Create rows from top (number row) to bottom (space bar)
        for (let rowIdx = 0; rowIdx < LAYOUT.length; rowIdx++) {
            const row = LAYOUT[rowIdx];

            // Calculate row width for centering
            let rowWidth = 0;
            for (const keyDef of row) {
                rowWidth += keyDef.width * keySize + keyGap;
            }
            rowWidth -= keyGap;

            // Start X position (left side)
            let currentX = -rowWidth / 2 + keySize / 2;

            for (const keyDef of row) {
                const keyWidth = keyDef.width * keySize + (keyDef.width - 1) * keyGap;

                // Adjust X for wide keys
                const keyX = currentX + (keyWidth - keySize) / 2;

                this._createKey(keyDef, keyX, startY, currentZ, keyWidth);

                currentX += keyWidth + keyGap;
            }

            currentZ += keySize + keyGap;
        }
    }

    _createKey(keyDef, x, y, z, width) {
        const { keyHeight, keySize, keyColor, keyColorAlt, keyColorAccent } = CONFIG;
        const depth = keySize;

        // Choose color based on key type
        let color = keyColor;
        if (keyDef.special) {
            color = keyColorAccent;
        } else if (Math.random() > 0.5) {
            color = keyColorAlt; // Slight variation
        }

        const geometry = new THREE.BoxGeometry(width, keyHeight, depth);
        const material = new THREE.MeshStandardMaterial({
            color,
            roughness: 0.7,
        });

        const mesh = new THREE.Mesh(geometry, material);
        mesh.position.set(x, y, z);
        mesh.castShadow = true;
        mesh.receiveShadow = true;

        this.group.add(mesh);
        this.keyMeshes.push(mesh);

        // Determine which hand types this key
        const isLeftHand = LEFT_HAND_KEYS.has(keyDef.char) || LEFT_HAND_KEYS.has(keyDef.char.toUpperCase());

        // Store key data
        const keyData = {
            mesh,
            char: keyDef.char,
            baseY: y,
            isPressed: false,
            width,
            isLeftHand,
        };

        // Map by character (lowercase for letters)
        const char = keyDef.char;
        this.keys.set(char, keyData);

        // Also map lowercase version for letters
        if (char.length === 1 && char >= 'A' && char <= 'Z') {
            this.keys.set(char.toLowerCase(), keyData);
        }

        // Map shifted characters
        if (SHIFT_MAP[char]) {
            this.keys.set(SHIFT_MAP[char], keyData);
        }

        return keyData;
    }

    // =========================================================================
    // Public Methods
    // =========================================================================

    /**
     * Add keyboard to scene
     */
    addToScene(scene) {
        scene.add(this.group);
    }

    /**
     * Set keyboard position
     */
    setPosition(x, y, z) {
        this.group.position.set(x, y, z);
    }

    /**
     * Get world position of a key (for IK targeting)
     * @param {string} char - Character to look up
     * @returns {{ position: THREE.Vector3, width: number, isLeftHand: boolean } | null}
     */
    getKeyPosition(char) {
        const keyData = this.keys.get(char) || this.keys.get(char.toUpperCase());

        if (!keyData) {
            // Unknown character - return space bar position
            return this.getKeyPosition(' ');
        }

        const worldPos = new THREE.Vector3();
        keyData.mesh.getWorldPosition(worldPos);

        // Return position at top of key
        worldPos.y += CONFIG.keyHeight / 2;

        // Determine left/right hand based on world X position
        // Negative X = Wendy's left side, Positive X = Wendy's right side
        const isLeftHand = worldPos.x < 0;

        return {
            position: worldPos,
            width: keyData.width,
            isLeftHand,
        };
    }

    /**
     * Press a key with animation
     * @param {string} char - Character to press
     */
    pressKey(char) {
        const keyData = this.keys.get(char) || this.keys.get(char.toUpperCase());

        if (!keyData || keyData.isPressed) return;

        keyData.isPressed = true;

        // Animate down
        this.animations.push({
            mesh: keyData.mesh,
            startY: keyData.baseY,
            targetY: keyData.baseY - CONFIG.pressDepth,
            startTime: performance.now(),
            duration: CONFIG.pressDuration,
            onComplete: () => {
                // Animate back up
                this.animations.push({
                    mesh: keyData.mesh,
                    startY: keyData.baseY - CONFIG.pressDepth,
                    targetY: keyData.baseY,
                    startTime: performance.now(),
                    duration: CONFIG.releaseDuration,
                    onComplete: () => {
                        keyData.isPressed = false;
                    },
                });
            },
        });
    }

    /**
     * Type a string with delays between keypresses
     * @param {string} text - Text to type
     * @param {number} delay - Ms between keypresses (default 100)
     * @returns {Promise} Resolves when done typing
     */
    async typeString(text, delay = 100) {
        for (const char of text) {
            this.pressKey(char);
            await new Promise(r => setTimeout(r, delay));
        }
    }

    /**
     * Update animations (call from render loop)
     * @param {number} deltaTime - Time since last frame in seconds
     */
    update(deltaTime) {
        const now = performance.now();
        const completed = [];

        for (let i = 0; i < this.animations.length; i++) {
            const anim = this.animations[i];
            const elapsed = now - anim.startTime;
            const progress = Math.min(elapsed / anim.duration, 1);

            // Ease out cubic
            const eased = 1 - Math.pow(1 - progress, 3);

            // Interpolate Y position
            anim.mesh.position.y = anim.startY + (anim.targetY - anim.startY) * eased;

            if (progress >= 1) {
                completed.push(i);
                if (anim.onComplete) anim.onComplete();
            }
        }

        // Remove completed animations (in reverse order)
        for (let i = completed.length - 1; i >= 0; i--) {
            this.animations.splice(completed[i], 1);
        }
    }

    /**
     * Get all key characters
     * @returns {string[]}
     */
    getAllKeys() {
        return Array.from(this.keys.keys());
    }
}
