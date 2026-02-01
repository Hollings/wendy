/**
 * ik.js - Two-bone analytical IK solver
 *
 * Uses Law of Cosines for a closed-form solution.
 * No iteration needed - exact solution for 2-bone chains.
 *
 * References:
 * - https://theorangeduck.com/page/simple-two-joint
 * - https://blog.littlepolygon.com/posts/twobone/
 */

import * as THREE from 'three';

// Reusable vectors to avoid allocations
const _shoulderWorld = new THREE.Vector3();
const _toTarget = new THREE.Vector3();
const _targetDir = new THREE.Vector3();
const _elbowPos = new THREE.Vector3();
const _toElbow = new THREE.Vector3();
const _elbowToWrist = new THREE.Vector3();
const _rotAxis = new THREE.Vector3();
const _tempVec = new THREE.Vector3();
const _tempQuat = new THREE.Quaternion();

// Additional preallocated objects for solveTwoBoneIK
const _parentQuatInverse = new THREE.Quaternion();
const _elbowWorldQuat = new THREE.Quaternion();
const _elbowWorldQuatInverse = new THREE.Quaternion();
const _toElbowLocal = new THREE.Vector3();
const _toWristLocal = new THREE.Vector3();
const _defaultDir = new THREE.Vector3(0, -1, 0);
const _tempVec2 = new THREE.Vector3();

/**
 * Solve two-bone IK and apply rotations to shoulder and elbow joints.
 *
 * This uses a geometric approach:
 * 1. Calculate where the elbow should be in world space
 * 2. Derive shoulder rotation to point at elbow
 * 3. Derive elbow rotation to point at target
 *
 * @param {Object} arm - Arm object with { shoulder, elbow, upperLength, forearmLength }
 * @param {THREE.Vector3} targetWorld - Target position in world space
 * @param {THREE.Vector3} poleWorld - Pole vector in world space (controls elbow direction)
 */
export function solveTwoBoneIK(arm, targetWorld, poleWorld) {
    const { shoulder, elbow, upperLength, forearmLength } = arm;

    // Get shoulder world position
    shoulder.getWorldPosition(_shoulderWorld);

    // Vector from shoulder to target
    _toTarget.subVectors(targetWorld, _shoulderWorld);
    let targetDist = _toTarget.length();

    // Clamp to reachable range
    const minReach = Math.abs(upperLength - forearmLength) + 0.001;
    const maxReach = upperLength + forearmLength - 0.001;
    targetDist = THREE.MathUtils.clamp(targetDist, minReach, maxReach);

    // Normalized direction to target
    _targetDir.copy(_toTarget).normalize();

    // === Law of Cosines to find elbow angle ===
    // Triangle: shoulder -> elbow -> wrist (target)
    // Sides: upperLength, forearmLength, targetDist
    //
    // Angle at shoulder (between upper arm and line to target):
    // cos(A) = (upper² + dist² - forearm²) / (2 * upper * dist)

    const upperSq = upperLength * upperLength;
    const forearmSq = forearmLength * forearmLength;
    const distSq = targetDist * targetDist;

    const cosShoulderAngle = (upperSq + distSq - forearmSq) / (2 * upperLength * targetDist);
    const shoulderAngle = Math.acos(THREE.MathUtils.clamp(cosShoulderAngle, -1, 1));

    // === Calculate elbow position in world space ===
    // The elbow lies on a circle around the shoulder-to-target axis.
    // The pole vector determines where on this circle.

    // Create a coordinate frame for the bend plane:
    // - X axis: perpendicular to target direction, toward pole
    // - Y axis: target direction
    // - Z axis: perpendicular to both (the other bend direction)

    // Project pole onto plane perpendicular to target direction
    _tempVec.subVectors(poleWorld, _shoulderWorld);
    const poleDotTarget = _tempVec.dot(_targetDir);
    _tempVec2.copy(_targetDir).multiplyScalar(poleDotTarget);
    _tempVec.sub(_tempVec2);

    // If pole is parallel to target, pick an arbitrary perpendicular
    if (_tempVec.lengthSq() < 0.0001) {
        // Use world up, or world right if target is vertical
        if (Math.abs(_targetDir.y) > 0.99) {
            _tempVec.set(1, 0, 0);
        } else {
            _tempVec.set(0, 1, 0);
        }
        _tempVec2.copy(_targetDir).multiplyScalar(_tempVec.dot(_targetDir));
        _tempVec.sub(_tempVec2);
    }
    _tempVec.normalize();

    // Elbow position = shoulder + rotate(targetDir * upperLength) by shoulderAngle around perpendicular axis
    // The perpendicular axis for this rotation is: cross(targetDir, poleDir)
    _rotAxis.crossVectors(_targetDir, _tempVec).normalize();

    // Start with elbow pointing toward target at upperLength distance
    _elbowPos.copy(_targetDir).multiplyScalar(upperLength);

    // Rotate by shoulder angle around the perpendicular axis (toward pole)
    _tempQuat.setFromAxisAngle(_rotAxis, shoulderAngle);
    _elbowPos.applyQuaternion(_tempQuat);

    // Translate to world space
    _elbowPos.add(_shoulderWorld);

    // === Now compute rotations ===

    // Vector from shoulder to elbow (in world space)
    _toElbow.subVectors(_elbowPos, _shoulderWorld).normalize();

    // Vector from elbow to target (in world space)
    _elbowToWrist.subVectors(targetWorld, _elbowPos).normalize();

    // Get the parent's world rotation to convert to local space
    _parentQuatInverse.identity();
    if (shoulder.parent) {
        shoulder.parent.getWorldQuaternion(_parentQuatInverse);
        _parentQuatInverse.invert();
    }

    // Convert toElbow to shoulder's local space
    _toElbowLocal.copy(_toElbow).applyQuaternion(_parentQuatInverse);

    // Shoulder rotation: rotate from default (-Y) to point at elbow
    shoulder.quaternion.setFromUnitVectors(_defaultDir, _toElbowLocal);

    // For elbow: we need to find the rotation in the elbow's local space
    // The elbow's rest pose also points down (-Y in elbow local space)
    // We need to rotate to point toward the wrist

    // Transform wrist direction into elbow's local space
    // First get elbow's world quaternion
    shoulder.getWorldQuaternion(_elbowWorldQuat);

    // Invert to get world-to-elbow-local transform
    _elbowWorldQuatInverse.copy(_elbowWorldQuat).invert();

    // Convert elbow-to-wrist direction to elbow local space
    _toWristLocal.copy(_elbowToWrist).applyQuaternion(_elbowWorldQuatInverse);

    // Elbow rotation: from default (-Y) to point at wrist
    elbow.quaternion.setFromUnitVectors(_defaultDir, _toWristLocal);
}

/**
 * Create a debug visualization for IK
 * @param {THREE.Scene} scene
 * @returns {Object} Debug helpers { target, pole, shoulder, elbow, wrist, setVisible, updateFromArm }
 */
export function createIKDebugHelpers(scene) {
    const sphereGeo = new THREE.SphereGeometry(0.03, 8, 8);

    // Target - red
    const targetMat = new THREE.MeshBasicMaterial({ color: 0xff0000 });
    const target = new THREE.Mesh(sphereGeo, targetMat);
    target.name = 'ik-target';
    scene.add(target);

    // Pole - green
    const poleMat = new THREE.MeshBasicMaterial({ color: 0x00ff00 });
    const pole = new THREE.Mesh(sphereGeo, poleMat);
    pole.name = 'ik-pole';
    scene.add(pole);

    // Shoulder - blue
    const shoulderMat = new THREE.MeshBasicMaterial({ color: 0x0088ff });
    const shoulderHelper = new THREE.Mesh(sphereGeo, shoulderMat);
    shoulderHelper.name = 'ik-shoulder';
    scene.add(shoulderHelper);

    // Elbow - yellow
    const elbowMat = new THREE.MeshBasicMaterial({ color: 0xffff00 });
    const elbowHelper = new THREE.Mesh(sphereGeo, elbowMat);
    elbowHelper.name = 'ik-elbow';
    scene.add(elbowHelper);

    // Wrist - magenta
    const wristMat = new THREE.MeshBasicMaterial({ color: 0xff00ff });
    const wrist = new THREE.Mesh(sphereGeo, wristMat);
    wrist.name = 'ik-wrist';
    scene.add(wrist);

    const helpers = { target, pole, shoulder: shoulderHelper, elbow: elbowHelper, wrist };

    return {
        ...helpers,

        setVisible(visible) {
            Object.values(helpers).forEach(h => h.visible = visible);
        },

        updateFromArm(arm) {
            // Update shoulder position
            arm.shoulder.getWorldPosition(shoulderHelper.position);

            // Update elbow position (at the elbow joint)
            arm.elbow.getWorldPosition(elbowHelper.position);

            // Update wrist position (end of forearm)
            const wristPos = new THREE.Vector3(0, -arm.forearmLength / 2, 0);
            arm.forearm.localToWorld(wristPos);
            wrist.position.copy(wristPos);
        }
    };
}
