/**
 * wendy.js - Wendy 3D character model
 *
 * Simple articulated character with:
 * - Rectangular torso
 * - Spherical head with eyes
 * - Two-bone arms (upper arm + forearm) ready for IK
 */

import * as THREE from 'three';

// =============================================================================
// Configuration
// =============================================================================

const CONFIG = {
    // Torso
    torso: {
        width: 0.4,
        height: 0.5,
        depth: 0.25,
        color: 0x4a4a4a,
    },

    // Head
    head: {
        radius: 0.15,
        color: 0xffdbac,
    },

    // Eyes
    eye: {
        radius: 0.03,
        color: 0x222222,
        offsetX: 0.06,      // Distance from center
        offsetY: 0.02,      // Height on head
        offsetZ: 0.12,      // Forward from head center
    },

    // Arms
    arm: {
        upperLength: 0.25,
        upperRadius: 0.04,
        forearmLength: 0.28,
        forearmRadius: 0.035,
        color: 0xffdbac,
        shoulderOffsetX: 0.22,  // Distance from torso center
        shoulderOffsetY: 0.2,   // Height on torso (from torso center)
    },
};

// =============================================================================
// Wendy Class
// =============================================================================

export class Wendy {
    constructor() {
        // Root group - position this to place Wendy in the scene
        this.group = new THREE.Group();

        // Body parts (set by create methods)
        this.torso = null;
        this.head = null;
        this.leftEye = null;
        this.rightEye = null;

        // Arm hierarchy for IK
        // Each arm: { shoulder, upperArm, elbow, forearm }
        this.leftArm = null;
        this.rightArm = null;

        // Head look target (smooth interpolation)
        this._lookTarget = null;
        this._lookSpeed = 5;  // Radians per second for head rotation

        // Reading animation state
        this._isReading = false;
        this._readingTime = 0;

        this._createBody();
        this._createHead();
        this._createArms();
    }

    // =========================================================================
    // Creation Methods
    // =========================================================================

    _createBody() {
        const { width, height, depth, color } = CONFIG.torso;

        const geometry = new THREE.BoxGeometry(width, height, depth);
        const material = new THREE.MeshStandardMaterial({
            color,
            roughness: 0.8,
        });

        this.torso = new THREE.Mesh(geometry, material);
        this.torso.castShadow = true;
        this.group.add(this.torso);
    }

    _createHead() {
        const { radius, color } = CONFIG.head;
        const torsoHeight = CONFIG.torso.height;

        const geometry = new THREE.SphereGeometry(radius, 16, 16);
        const material = new THREE.MeshStandardMaterial({
            color,
            roughness: 0.7,
        });

        this.head = new THREE.Mesh(geometry, material);
        this.head.position.y = torsoHeight / 2 + radius;
        this.head.castShadow = true;
        this.group.add(this.head);

        this._createEyes();
    }

    _createEyes() {
        const { radius, color, offsetX, offsetY, offsetZ } = CONFIG.eye;

        const geometry = new THREE.SphereGeometry(radius, 8, 8);
        const material = new THREE.MeshStandardMaterial({ color });

        // Left eye (Wendy's left = negative X)
        this.leftEye = new THREE.Mesh(geometry, material);
        this.leftEye.position.set(-offsetX, offsetY, offsetZ);
        this.head.add(this.leftEye);

        // Right eye (Wendy's right = positive X)
        this.rightEye = new THREE.Mesh(geometry, material);
        this.rightEye.position.set(offsetX, offsetY, offsetZ);
        this.head.add(this.rightEye);
    }

    _createArms() {
        this.leftArm = this._createArm(-1);  // Left side (negative X)
        this.rightArm = this._createArm(1);  // Right side (positive X)
    }

    /**
     * Create a single arm with shoulder pivot, upper arm, elbow pivot, and forearm.
     * @param {number} side - 1 for right (positive X), -1 for left (negative X)
     * @returns {Object} Arm structure with shoulder, upperArm, elbow, forearm
     */
    _createArm(side) {
        const {
            upperLength,
            upperRadius,
            forearmLength,
            forearmRadius,
            color,
            shoulderOffsetX,
            shoulderOffsetY,
        } = CONFIG.arm;

        const material = new THREE.MeshStandardMaterial({
            color,
            roughness: 0.7,
        });

        // Shoulder pivot - attached to torso
        // This is the rotation point for the whole arm
        const shoulder = new THREE.Group();
        shoulder.position.set(
            side * shoulderOffsetX,
            shoulderOffsetY,
            0
        );
        this.torso.add(shoulder);

        // Upper arm - cylinder hanging down from shoulder
        // Geometry is created along Y axis, so it naturally points down
        const upperGeometry = new THREE.CylinderGeometry(
            upperRadius,
            upperRadius,
            upperLength,
            8
        );
        const upperArm = new THREE.Mesh(upperGeometry, material);
        upperArm.position.y = -upperLength / 2;  // Center below shoulder pivot
        upperArm.castShadow = true;
        shoulder.add(upperArm);

        // Elbow pivot - at the end of upper arm
        const elbow = new THREE.Group();
        elbow.position.y = -upperLength / 2;  // Bottom of upper arm
        upperArm.add(elbow);

        // Forearm - cylinder hanging down from elbow
        const forearmGeometry = new THREE.CylinderGeometry(
            forearmRadius,
            forearmRadius * 0.7,  // Slight taper toward wrist
            forearmLength,
            8
        );
        const forearm = new THREE.Mesh(forearmGeometry, material);
        forearm.position.y = -forearmLength / 2;  // Center below elbow pivot
        forearm.castShadow = true;
        elbow.add(forearm);

        return {
            shoulder,
            upperArm,
            elbow,
            forearm,
            // Store lengths for IK calculations
            upperLength,
            forearmLength,
        };
    }

    // =========================================================================
    // Public Methods
    // =========================================================================

    /**
     * Add Wendy to a scene
     * @param {THREE.Scene} scene
     */
    addToScene(scene) {
        scene.add(this.group);
    }

    /**
     * Set Wendy's position in the scene
     * @param {number} x
     * @param {number} y
     * @param {number} z
     */
    setPosition(x, y, z) {
        this.group.position.set(x, y, z);
    }

    /**
     * Set Wendy's rotation (Y axis)
     * @param {number} radians
     */
    setRotation(radians) {
        this.group.rotation.y = radians;
    }

    /**
     * Get the world position of a shoulder
     * @param {'left'|'right'} side
     * @returns {THREE.Vector3}
     */
    getShoulderPosition(side) {
        const arm = side === 'left' ? this.leftArm : this.rightArm;
        const pos = new THREE.Vector3();
        arm.shoulder.getWorldPosition(pos);
        return pos;
    }

    /**
     * Get the world position of the wrist (end of forearm)
     * @param {'left'|'right'} side
     * @returns {THREE.Vector3}
     */
    getWristPosition(side) {
        const arm = side === 'left' ? this.leftArm : this.rightArm;
        const pos = new THREE.Vector3();
        // Get position at bottom of forearm
        arm.forearm.localToWorld(pos.set(0, -arm.forearmLength / 2, 0));
        return pos;
    }

    /**
     * Make the head look at a world position (smoothly interpolated)
     * @param {THREE.Vector3} target - World position to look at
     */
    lookAt(target) {
        this._lookTarget = target.clone();
    }

    /**
     * Clear the look target (head returns to neutral)
     */
    clearLookTarget() {
        this._lookTarget = null;
    }

    /**
     * Start reading animation (gentle head movement scanning code)
     */
    startReading() {
        this._isReading = true;
        this._readingTime = 0;
        this._lookTarget = null;  // Clear any look target
    }

    /**
     * Stop reading animation
     */
    stopReading() {
        this._isReading = false;
    }

    /**
     * Update method for animation loop
     * @param {number} deltaTime - Time since last frame in seconds
     */
    update(deltaTime) {
        // Smooth head rotation toward look target
        if (this._lookTarget && this.head) {
            // Get head world position
            const headWorld = new THREE.Vector3();
            this.head.getWorldPosition(headWorld);

            // Direction from head to target
            const toTarget = new THREE.Vector3().subVectors(this._lookTarget, headWorld);

            // Calculate target rotation (pitch and yaw only, no roll)
            const targetYaw = Math.atan2(toTarget.x, toTarget.z);
            const horizontalDist = Math.sqrt(toTarget.x * toTarget.x + toTarget.z * toTarget.z);
            const targetPitch = Math.atan2(-toTarget.y, horizontalDist);

            // Clamp pitch to reasonable range (don't look too far up/down)
            const clampedPitch = THREE.MathUtils.clamp(targetPitch, -0.5, 0.8);

            // Clamp yaw to reasonable range (don't turn head too far)
            const clampedYaw = THREE.MathUtils.clamp(targetYaw, -0.6, 0.6);

            // Smoothly interpolate current rotation toward target
            const lerpFactor = 1 - Math.exp(-this._lookSpeed * deltaTime);
            this.head.rotation.x = THREE.MathUtils.lerp(this.head.rotation.x, clampedPitch, lerpFactor);
            this.head.rotation.y = THREE.MathUtils.lerp(this.head.rotation.y, clampedYaw, lerpFactor);
        } else if (this._isReading && this.head) {
            // Reading animation - gentle scanning motion
            this._readingTime += deltaTime;

            // Horizontal scan: slow sweep left-to-right like reading lines
            // Use a sawtooth-ish pattern: quick return, slow read
            const lineTime = 3.0;  // Seconds per "line"
            const lineCycle = (this._readingTime % lineTime) / lineTime;
            // Ease in-out for smooth reading motion
            // Scan phase (0-0.9): smoothly goes from 0 to 0.5
            // Return phase (0.9-1.0): smoothly goes from 0.5 back to 0
            const eased = lineCycle < 0.9
                ? lineCycle / 0.9 * (2 - lineCycle / 0.9) * 0.5  // Smooth scan right (0 to 0.5)
                : 0.5 * (1 - ((lineCycle - 0.9) / 0.1));  // Smooth return left (0.5 to 0)
            const targetYaw = (eased - 0.5) * 0.3;  // -0.15 to 0.15 radians

            // Vertical drift: subtle downward drift as reading progresses, then jump up
            const pageTime = 12.0;  // Seconds per "page"
            const pageCycle = (this._readingTime % pageTime) / pageTime;
            const verticalDrift = pageCycle < 0.95
                ? pageCycle / 0.95 * 0.15  // Gradual look down
                : 0;  // Jump back to top
            const targetPitch = 0.1 + verticalDrift;  // Slightly looking down at screen

            // Smoothly interpolate
            const lerpFactor = 1 - Math.exp(-3 * deltaTime);
            this.head.rotation.x = THREE.MathUtils.lerp(this.head.rotation.x, targetPitch, lerpFactor);
            this.head.rotation.y = THREE.MathUtils.lerp(this.head.rotation.y, targetYaw, lerpFactor);
        } else if (this.head) {
            // Return to neutral when no target
            const lerpFactor = 1 - Math.exp(-this._lookSpeed * deltaTime);
            this.head.rotation.x = THREE.MathUtils.lerp(this.head.rotation.x, 0, lerpFactor);
            this.head.rotation.y = THREE.MathUtils.lerp(this.head.rotation.y, 0, lerpFactor);
        }
    }
}
