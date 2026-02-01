/**
 * typing-controller.js - Manages keyboard typing IK animation
 *
 * Encapsulates the keypress queue, arm target positions, and IK updates
 * for animating Wendy's hands typing on the keyboard.
 */

import * as THREE from 'three';
import { solveTwoBoneIK } from './ik.js';

// Keypress phases: 'hover' -> 'press' -> 'lift'
const HOVER_HEIGHT = 0.06;  // Height above key for hover
const PRESS_HEIGHT = 0.01;  // Height above key when pressed
const ARM_SPEED = 4;        // Units per second for arm movement
const KEYPRESS_THRESHOLD = 0.015;  // Distance to trigger phase change

/**
 * TypingController manages the IK animation for typing on a keyboard.
 *
 * Usage:
 *   const typingController = new TypingController(wendy, keyboard);
 *   typingController.typeCharacter('a');  // Queue a character to type
 *   typingController.update(delta);        // Call each frame
 */
export class TypingController {
    /**
     * @param {Wendy} wendy - The Wendy avatar instance
     * @param {Keyboard} keyboard - The keyboard instance
     */
    constructor(wendy, keyboard) {
        this.wendy = wendy;
        this.keyboard = keyboard;

        // Arm target vectors (current interpolated positions)
        this.leftArmTarget = new THREE.Vector3();
        this.rightArmTarget = new THREE.Vector3();

        // Arm target goals (where arms are moving toward)
        this.leftArmTargetGoal = new THREE.Vector3();
        this.rightArmTargetGoal = new THREE.Vector3();

        // Pole vectors (elbow positions - pointing outward and back)
        this.leftArmPole = new THREE.Vector3(-1.0, 0.5, 0.25);
        this.rightArmPole = new THREE.Vector3(1.0, 0.5, 0.25);

        // Lock flags - when true, that arm is controlled externally (e.g., mouse)
        this.leftArmLocked = false;
        this.rightArmLocked = false;

        // Keypress queue (characters waiting to be typed)
        this.keypressQueue = [];
        this.currentKeypress = null;  // The keypress currently being animated

        // Callback for when a key is visually pressed
        this.onKeyPressed = null;

        // Initialize rest positions
        this._initRestPositions();
    }

    /**
     * Initialize arm rest positions over home row keys (F and J)
     * @private
     */
    _initRestPositions() {
        if (!this.keyboard || !this.wendy) return;

        const fKey = this.keyboard.getKeyPosition('f');
        const jKey = this.keyboard.getKeyPosition('j');

        if (fKey && jKey) {
            // Assign based on actual world X position
            const leftKey = fKey.position.x < jKey.position.x ? fKey : jKey;
            const rightKey = fKey.position.x < jKey.position.x ? jKey : fKey;

            // Left arm rests over left key, raised slightly
            this.leftArmTarget.copy(leftKey.position);
            this.leftArmTarget.y += 0.06;
            this.leftArmTargetGoal.copy(this.leftArmTarget);

            // Right arm rests over right key, raised slightly
            this.rightArmTarget.copy(rightKey.position);
            this.rightArmTarget.y += 0.06;
            this.rightArmTargetGoal.copy(this.rightArmTarget);
        }
    }

    /**
     * Queue a character to be typed with IK animation
     * @param {string} char - The character to type
     */
    typeCharacter(char) {
        if (!this.keyboard) return;

        const keyInfo = this.keyboard.getKeyPosition(char);
        if (!keyInfo) return;

        // Make Wendy look at the key
        if (this.wendy) {
            this.wendy.lookAt(keyInfo.position);
        }

        // Add to keypress queue
        this.keypressQueue.push({
            char,
            isLeftHand: keyInfo.isLeftHand,
            phase: 'hover',  // 'hover' -> 'press' -> 'lift'
            keyY: keyInfo.position.y,  // Store key Y for phase transitions
        });
    }

    /**
     * Update the typing IK animation (call each frame)
     * @param {number} delta - Time since last frame in seconds
     */
    update(delta) {
        if (!this.wendy || !this.keyboard) return;

        // Smoothly move arms toward their target goals
        const maxMove = ARM_SPEED * delta;

        // Update left arm
        this._updateArmPosition(this.leftArmTarget, this.leftArmTargetGoal, maxMove);

        // Update right arm
        this._updateArmPosition(this.rightArmTarget, this.rightArmTargetGoal, maxMove);

        // Process keypress queue
        this._processKeypressQueue();

        // Check if current keypress should advance phase
        this._updateCurrentKeypress();

        // Apply IK to both arms
        solveTwoBoneIK(this.wendy.leftArm, this.leftArmTarget, this.leftArmPole);
        solveTwoBoneIK(this.wendy.rightArm, this.rightArmTarget, this.rightArmPole);
    }

    /**
     * Smoothly interpolate arm position toward goal
     * @private
     */
    _updateArmPosition(target, goal, maxMove) {
        const dist = target.distanceTo(goal);
        if (dist > 0.001) {
            if (dist <= maxMove) {
                target.copy(goal);
            } else {
                target.lerp(goal, maxMove / dist);
            }
        }
    }

    /**
     * Start next keypress from queue if none active
     * @private
     */
    _processKeypressQueue() {
        while (!this.currentKeypress && this.keypressQueue.length > 0) {
            const nextKeypress = this.keypressQueue.shift();
            // Validate that the key exists before assigning
            const keyInfo = this.keyboard.getKeyPosition(nextKeypress.char);
            if (keyInfo) {
                // Skip if the required arm is locked (e.g., on mouse)
                if (nextKeypress.isLeftHand && this.leftArmLocked) continue;
                if (!nextKeypress.isLeftHand && this.rightArmLocked) continue;

                this.currentKeypress = nextKeypress;
                // Set initial target goal for hover
                const targetGoal = this.currentKeypress.isLeftHand
                    ? this.leftArmTargetGoal
                    : this.rightArmTargetGoal;
                targetGoal.copy(keyInfo.position);
                targetGoal.y = keyInfo.position.y + HOVER_HEIGHT;
            }
            // If key doesn't exist or arm locked, skip and try next
        }
    }

    /**
     * Update current keypress phase transitions
     * @private
     */
    _updateCurrentKeypress() {
        if (!this.currentKeypress) return;

        const armTarget = this.currentKeypress.isLeftHand
            ? this.leftArmTarget
            : this.rightArmTarget;
        const armGoal = this.currentKeypress.isLeftHand
            ? this.leftArmTargetGoal
            : this.rightArmTargetGoal;
        const dist = armTarget.distanceTo(armGoal);

        if (dist < KEYPRESS_THRESHOLD) {
            const phase = this.currentKeypress.phase;

            if (phase === 'hover') {
                // Reached hover position - now press down
                this.currentKeypress.phase = 'press';
                armGoal.y = this.currentKeypress.keyY + PRESS_HEIGHT;
            } else if (phase === 'press') {
                // Reached press position - trigger keypress and lift
                this.keyboard.pressKey(this.currentKeypress.char);

                // Notify callback if set
                if (this.onKeyPressed) {
                    this.onKeyPressed(this.currentKeypress.char);
                }

                this.currentKeypress.phase = 'lift';
                armGoal.y = this.currentKeypress.keyY + HOVER_HEIGHT;
            } else if (phase === 'lift') {
                // Done with this keypress
                this.currentKeypress = null;
            }
        }
    }

    /**
     * Return arms to rest position over home row
     */
    returnToRest() {
        if (!this.keyboard) return;

        const fKey = this.keyboard.getKeyPosition('f');
        const jKey = this.keyboard.getKeyPosition('j');
        if (fKey && jKey) {
            const leftKey = fKey.position.x < jKey.position.x ? fKey : jKey;
            const rightKey = fKey.position.x < jKey.position.x ? jKey : fKey;

            this.leftArmTargetGoal.copy(leftKey.position);
            this.leftArmTargetGoal.y += 0.05;

            this.rightArmTargetGoal.copy(rightKey.position);
            this.rightArmTargetGoal.y += 0.05;
        }
    }

    /**
     * Check if currently typing (has queued or active keypresses)
     * @returns {boolean}
     */
    isTyping() {
        return this.currentKeypress !== null || this.keypressQueue.length > 0;
    }

    /**
     * Clear all pending keypresses
     */
    clearQueue() {
        this.keypressQueue = [];
        this.currentKeypress = null;
    }

    /**
     * Get the number of pending keypresses
     * @returns {number}
     */
    getQueueLength() {
        return this.keypressQueue.length + (this.currentKeypress ? 1 : 0);
    }

    /**
     * Move right arm to a specific world position (e.g., mouse)
     * @param {THREE.Vector3} position - Target world position
     */
    moveRightArmTo(position) {
        this.rightArmTargetGoal.copy(position);
        this.rightArmTargetGoal.y += HOVER_HEIGHT;  // Slightly above
    }

    /**
     * Move left arm to a specific world position (e.g., mouse)
     * @param {THREE.Vector3} position - Target world position
     */
    moveLeftArmTo(position) {
        this.leftArmTargetGoal.copy(position);
        this.leftArmTargetGoal.y += HOVER_HEIGHT;  // Slightly above
    }

    /**
     * Check if right arm has reached its goal (for sequencing)
     * @returns {boolean}
     */
    isRightArmAtGoal() {
        return this.rightArmTarget.distanceTo(this.rightArmTargetGoal) < KEYPRESS_THRESHOLD;
    }

    /**
     * Check if left arm has reached its goal (for sequencing)
     * @returns {boolean}
     */
    isLeftArmAtGoal() {
        return this.leftArmTarget.distanceTo(this.leftArmTargetGoal) < KEYPRESS_THRESHOLD;
    }
}
