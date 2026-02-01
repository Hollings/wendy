/**
 * mouse.js - 3D Mouse model for desk
 *
 * Simple computer mouse that Wendy can interact with.
 * Supports smooth movement animation for "using" the mouse.
 */

import * as THREE from 'three';

// =============================================================================
// Configuration
// =============================================================================

const CONFIG = {
    // Mouse body dimensions
    bodyWidth: 0.04,
    bodyLength: 0.06,
    bodyHeight: 0.02,

    // Scroll wheel
    wheelRadius: 0.005,
    wheelWidth: 0.008,

    // Colors
    bodyColor: 0x2a2a2a,
    wheelColor: 0x1a1a1a,

    // Animation
    moveSpeed: 2,  // Units per second for smooth movement (slower)
    maxOffset: 0.12,  // Maximum movement range from base position (larger)
};

// =============================================================================
// Mouse Class
// =============================================================================

export class Mouse {
    constructor() {
        this.group = new THREE.Group();

        // Position tracking
        this.basePosition = new THREE.Vector3();
        this.targetPosition = new THREE.Vector3();

        // Animation state
        this.isMoving = false;

        this._createMouse();
    }

    // =========================================================================
    // Creation
    // =========================================================================

    _createMouse() {
        const material = new THREE.MeshStandardMaterial({
            color: CONFIG.bodyColor,
            roughness: 0.7,
        });

        // Mouse body - slightly rounded box shape
        // Using a box with beveled appearance via scale
        const bodyGeometry = new THREE.BoxGeometry(
            CONFIG.bodyWidth,
            CONFIG.bodyHeight,
            CONFIG.bodyLength
        );

        // Taper the front of the mouse
        const positions = bodyGeometry.attributes.position;
        for (let i = 0; i < positions.count; i++) {
            const z = positions.getZ(i);
            const y = positions.getY(i);

            // Taper front (negative Z in local space)
            if (z < 0) {
                const taperFactor = 0.7;
                positions.setX(i, positions.getX(i) * taperFactor);
            }

            // Round the top
            if (y > 0) {
                const roundFactor = 0.9;
                positions.setX(i, positions.getX(i) * roundFactor);
            }
        }
        bodyGeometry.computeVertexNormals();

        this.body = new THREE.Mesh(bodyGeometry, material);
        this.body.position.y = CONFIG.bodyHeight / 2;
        this.body.castShadow = true;
        this.group.add(this.body);

        // Scroll wheel
        const wheelGeometry = new THREE.CylinderGeometry(
            CONFIG.wheelRadius,
            CONFIG.wheelRadius,
            CONFIG.wheelWidth,
            8
        );
        const wheelMaterial = new THREE.MeshStandardMaterial({
            color: CONFIG.wheelColor,
            roughness: 0.5,
        });

        this.wheel = new THREE.Mesh(wheelGeometry, wheelMaterial);
        this.wheel.rotation.z = Math.PI / 2;  // Rotate to horizontal
        this.wheel.position.set(0, CONFIG.bodyHeight + 0.002, -0.01);
        this.group.add(this.wheel);

        // Left/right button divider line (subtle groove)
        const grooveGeometry = new THREE.BoxGeometry(0.001, 0.001, CONFIG.bodyLength * 0.4);
        const grooveMaterial = new THREE.MeshStandardMaterial({
            color: 0x1a1a1a,
            roughness: 0.9,
        });
        const groove = new THREE.Mesh(grooveGeometry, grooveMaterial);
        groove.position.set(0, CONFIG.bodyHeight + 0.001, -0.005);
        this.group.add(groove);
    }

    // =========================================================================
    // Public Methods
    // =========================================================================

    /**
     * Add mouse to scene
     * @param {THREE.Scene} scene
     */
    addToScene(scene) {
        scene.add(this.group);
    }

    /**
     * Set base position (rest position)
     * @param {number} x
     * @param {number} y
     * @param {number} z
     */
    setPosition(x, y, z) {
        this.basePosition.set(x, y, z);
        this.targetPosition.copy(this.basePosition);
        this.group.position.copy(this.basePosition);
    }

    /**
     * Get the grip position for IK targeting (top center of mouse)
     * @returns {THREE.Vector3}
     */
    getGripPosition() {
        const pos = this.group.position.clone();
        pos.y += CONFIG.bodyHeight + 0.01;  // Slightly above mouse surface
        return pos;
    }

    /**
     * Move mouse to offset from base position (animated)
     * @param {number} offsetX - X offset from base
     * @param {number} offsetZ - Z offset from base
     */
    moveTo(offsetX, offsetZ) {
        // Clamp offsets to max range
        const clampedX = Math.max(-CONFIG.maxOffset, Math.min(CONFIG.maxOffset, offsetX));
        const clampedZ = Math.max(-CONFIG.maxOffset, Math.min(CONFIG.maxOffset, offsetZ));

        this.targetPosition.set(
            this.basePosition.x + clampedX,
            this.basePosition.y,
            this.basePosition.z + clampedZ
        );
        this.isMoving = true;
    }

    /**
     * Return mouse to base position (animated)
     */
    returnToBase() {
        this.targetPosition.copy(this.basePosition);
        this.isMoving = true;
    }

    /**
     * Update animation
     * @param {number} delta - Time since last frame in seconds
     */
    update(delta) {
        if (!this.isMoving) return;

        const currentPos = this.group.position;
        const distance = currentPos.distanceTo(this.targetPosition);

        if (distance < 0.001) {
            // Reached target
            currentPos.copy(this.targetPosition);
            this.isMoving = false;
        } else {
            // Move toward target
            const moveAmount = CONFIG.moveSpeed * delta;
            if (moveAmount >= distance) {
                currentPos.copy(this.targetPosition);
                this.isMoving = false;
            } else {
                currentPos.lerp(this.targetPosition, moveAmount / distance);
            }
        }
    }
}
