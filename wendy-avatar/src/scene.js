/**
 * scene.js - Three.js scene setup
 *
 * Creates a minimal 3D environment: lights and floor.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// Scene configuration
const CONFIG = {
    // Viewport - responsive, will be set by container size
    width: 1024,

    // Camera
    fov: 50,
    near: 0.1,
    far: 100,
    // Over Shoulder preset as default
    initialPosition: { x: -0.59, y: 1.15, z: -0.21 },
    lookAt: { x: -0.10, y: 0.69, z: 0.26 },

    // Colors - dark for monitor glow emphasis
    backgroundColor: 0x080810,
    floorColor: 0x0a0a12,
};

/**
 * Create the complete 3D scene
 * @param {HTMLElement} container - DOM element to render into
 * @returns {Object} Scene components
 */
export function createScene(container) {
    // Scene
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(CONFIG.backgroundColor);

    // Get container dimensions
    const getSize = () => {
        const rect = container.getBoundingClientRect();
        const size = Math.min(rect.width, rect.height, CONFIG.width);
        return size || CONFIG.width;
    };

    // Camera (1:1 aspect ratio for square viewport)
    const camera = new THREE.PerspectiveCamera(
        CONFIG.fov,
        1, // Always square
        CONFIG.near,
        CONFIG.far
    );
    camera.position.set(
        CONFIG.initialPosition.x,
        CONFIG.initialPosition.y,
        CONFIG.initialPosition.z
    );

    // Renderer (responsive to container)
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    const initialSize = getSize();
    renderer.setSize(initialSize, initialSize);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    container.insertBefore(renderer.domElement, container.firstChild);

    // Handle resize
    const onResize = () => {
        const size = getSize();
        renderer.setSize(size, size);
    };
    window.addEventListener('resize', onResize);

    // Orbit controls
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(CONFIG.lookAt.x, CONFIG.lookAt.y, CONFIG.lookAt.z);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;
    controls.minDistance = 0.5;   // Allow close zoom
    controls.maxDistance = 20;    // Allow far zoom
    controls.maxPolarAngle = Math.PI / 2;

    // Disable built-in zoom and use custom handler for finer control
    controls.enableZoom = false;

    // Custom wheel handler with very fine zoom
    renderer.domElement.addEventListener('wheel', (e) => {
        e.preventDefault();
        const zoomFactor = 0.03;  // Very small steps
        const delta = e.deltaY > 0 ? 1 : -1;

        // Move camera along the direction it's looking
        const direction = new THREE.Vector3();
        camera.getWorldDirection(direction);

        const distance = camera.position.distanceTo(controls.target);
        const moveAmount = distance * zoomFactor * delta;

        camera.position.addScaledVector(direction, moveAmount);

        // Clamp distance
        const newDist = camera.position.distanceTo(controls.target);
        if (newDist < controls.minDistance) {
            camera.position.sub(direction.multiplyScalar(controls.minDistance - newDist));
        } else if (newDist > controls.maxDistance) {
            camera.position.add(direction.multiplyScalar(newDist - controls.maxDistance));
        }
    }, { passive: false });

    controls.update();

    // Add scene objects
    createLights(scene);
    createFloor(scene);

    // Clock for animation
    const clock = new THREE.Clock();

    // No resize handler - fixed 500x500 viewport

    return {
        scene,
        camera,
        renderer,
        controls,
        clock,
    };
}

/**
 * Create scene lighting
 * Kept dim to let monitor glow be the primary light source
 */
function createLights(scene) {
    // Ambient - very dim for moody atmosphere
    const ambientLight = new THREE.AmbientLight(0x202030, 0.4);
    scene.add(ambientLight);

    // Key light - dimmed, warm backlight
    const keyLight = new THREE.DirectionalLight(0xffeecc, 0.3);
    keyLight.position.set(2, 3, -2);  // Behind and to the side
    keyLight.castShadow = true;
    keyLight.shadow.mapSize.width = 1024;
    keyLight.shadow.mapSize.height = 1024;
    scene.add(keyLight);

    // Subtle rim light from behind
    const rimLight = new THREE.DirectionalLight(0x4444aa, 0.2);
    rimLight.position.set(-1, 2, -1);
    scene.add(rimLight);

    // Very dim overhead - just enough to prevent pure black shadows
    const overheadLight = new THREE.PointLight(0x333344, 0.3, 5);
    overheadLight.position.set(0, 2.5, 0.3);
    scene.add(overheadLight);
}

/**
 * Create floor plane
 */
function createFloor(scene) {
    const geometry = new THREE.PlaneGeometry(10, 10);
    const material = new THREE.MeshStandardMaterial({
        color: CONFIG.floorColor,
        roughness: 0.9,
    });
    const floor = new THREE.Mesh(geometry, material);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);
}
