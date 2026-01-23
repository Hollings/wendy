/**
 * scene.js - Three.js scene setup
 *
 * Creates a minimal 3D environment: lights and floor.
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// Scene configuration
const CONFIG = {
    // Camera
    fov: 50,
    near: 0.1,
    far: 100,
    initialPosition: { x: 0, y: 1.5, z: 3 },
    lookAt: { x: 0, y: 1.0, z: 0 },

    // Colors
    backgroundColor: 0x111111,
    floorColor: 0x1a1a1a,
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

    // Camera
    const camera = new THREE.PerspectiveCamera(
        CONFIG.fov,
        window.innerWidth / window.innerHeight,
        CONFIG.near,
        CONFIG.far
    );
    camera.position.set(
        CONFIG.initialPosition.x,
        CONFIG.initialPosition.y,
        CONFIG.initialPosition.z
    );

    // Renderer
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    container.insertBefore(renderer.domElement, container.firstChild);

    // Orbit controls
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.set(CONFIG.lookAt.x, CONFIG.lookAt.y, CONFIG.lookAt.z);
    controls.enableDamping = true;
    controls.dampingFactor = 0.05;
    controls.minDistance = 0.5;   // Allow close zoom
    controls.maxDistance = 20;    // Allow far zoom
    controls.zoomSpeed = 0.5;     // Finer control (default is 1.0)
    controls.maxPolarAngle = Math.PI / 2;
    controls.update();

    // Add scene objects
    createLights(scene);
    createFloor(scene);

    // Clock for animation
    const clock = new THREE.Clock();

    // Handle resize
    const onResize = () => {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
    };
    window.addEventListener('resize', onResize);

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
 */
function createLights(scene) {
    // Ambient
    const ambientLight = new THREE.AmbientLight(0x404040, 0.5);
    scene.add(ambientLight);

    // Key light
    const keyLight = new THREE.DirectionalLight(0xffffff, 1);
    keyLight.position.set(2, 3, 2);
    keyLight.castShadow = true;
    keyLight.shadow.mapSize.width = 1024;
    keyLight.shadow.mapSize.height = 1024;
    scene.add(keyLight);

    // Fill light
    const fillLight = new THREE.DirectionalLight(0x8888ff, 0.3);
    fillLight.position.set(-2, 1, 2);
    scene.add(fillLight);
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
