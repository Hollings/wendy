/**
 * main.js - Application entry point
 *
 * Minimal scene with event feed connection.
 */

import * as THREE from 'three';
import { createScene } from './scene.js';
import { BrainStream, classifyEvent } from './stream.js';
import { StateMachine, STATES } from './states.js';
import { MOCK_SESSION, MOCK_QUICK_SESSION, MOCK_EDIT_SESSION, MOCK_READ_SESSION } from './mock-data.js';
import { Wendy } from './wendy.js';
import { solveTwoBoneIK, createIKDebugHelpers } from './ik.js';
import { Keyboard } from './keyboard.js';
import { Monitor } from './monitor.js';
import { ChatManager } from './chat-manager.js';
import { TypingController } from './typing-controller.js';

// =============================================================================
// Configuration
// =============================================================================

const CONFIG = {
    get BRAIN_HOST() {
        const params = new URLSearchParams(location.search);
        if (params.has('live') || location.hostname !== 'localhost') {
            return 'wendy.monster';
        }
        return 'localhost:8910';
    },

    get BRAIN_WS_URL() {
        const isProduction = this.BRAIN_HOST === 'wendy.monster';
        return `${isProduction ? 'wss' : 'ws'}://${this.BRAIN_HOST}/ws/brain`;
    },

    get BRAIN_AUTH_URL() {
        const isProduction = this.BRAIN_HOST === 'wendy.monster';
        return `${isProduction ? 'https' : 'http'}://${this.BRAIN_HOST}/api/brain/auth`;
    },

    DEBUG: true,
};

// =============================================================================
// Application State
// =============================================================================

let sceneComponents = null;
let stream = null;
let stateMachine = null;
let wendy = null;
let keyboard = null;
let monitor = null;
let monitorMesh = null;
let chatManager = null;
let typingController = null;

// IK state (for manual IK debug panel)
let ikDebugHelpers = null;
let ikTarget = new THREE.Vector3(0.3, 0.2, 0.4);
let ikPole = new THREE.Vector3(0.5, 0.3, -0.5);
let ikEnabled = true;
let ikArm = 'right';

// Typing IK enabled state (controller handles the actual typing)
let typingEnabled = true;  // Enabled by default

let currentState = null;
let eventCount = 0;
const pageLoadTime = Date.now();
const INITIAL_LOAD_GRACE_PERIOD = 10000;  // FIX: Don't animate for first 10 seconds after page load

// =============================================================================
// Initialization
// =============================================================================

async function init() {
    document.getElementById('auth-form').addEventListener('submit', handleAuth);

    const params = new URLSearchParams(location.search);
    const mockMode = params.has('mock') || params.has('demo');
    const debugMode = params.has('debug') || params.get('debug') === 'true';

    // Show debug panels if debug mode enabled
    if (debugMode) {
        document.getElementById('debug-panel')?.classList.remove('hidden');
        document.getElementById('keyboard-panel')?.classList.remove('hidden');
        document.getElementById('scene-tuner-toggle')?.classList.remove('hidden');
    }

    if (mockMode) {
        console.log('Mock mode - use debug buttons to replay sessions');
        showScene();
        initApp();
        updateDebug('connected', 'mock');
        return;
    }

    console.log(`Connecting to brain feed at ${CONFIG.BRAIN_HOST}`);

    stream = new BrainStream(CONFIG.BRAIN_WS_URL, CONFIG.BRAIN_AUTH_URL);

    if (stream.loadStoredToken()) {
        showScene();
        initApp();
        connectStream();
    } else {
        // No token - try stored passphrase
        stream.tryReauthenticate().then(reauthed => {
            if (reauthed) {
                showScene();
                initApp();
                connectStream();
            } else {
                showAuth();
            }
        });
    }

    stream.addEventListener('auth_required', () => {
        // Reset chat state on auth failure
        if (chatManager) {
            chatManager.reset();
        }
        showAuth();
    });
}

function initApp() {
    const container = document.getElementById('scene-container');

    // Create 3D scene (floor and lights)
    sceneComponents = createScene(container);

    // Create Wendy and add to scene
    wendy = new Wendy();
    wendy.addToScene(sceneComponents.scene);
    wendy.setPosition(0, 0.25, 0);  // Raise her so torso center is above floor

    // Create keyboard and add to scene
    keyboard = new Keyboard();
    keyboard.addToScene(sceneComponents.scene);
    keyboard.setPosition(0, 0.18, 0.34);  // In front of Wendy
    keyboard.group.rotation.y = Math.PI;  // Rotate to face Wendy

    // Create 3D monitor with canvas texture
    createMonitor(sceneComponents.scene);

    // Create typing controller for IK animation
    typingController = new TypingController(wendy, keyboard);

    // Wire up key press callback to update monitor
    typingController.onKeyPressed = (char) => {
        if (monitor && monitor.isTyping()) {
            monitor.typeChar(char);
        }
    };

    // Create chat manager with keyboard sync callbacks
    chatManager = new ChatManager({
        monitor,
        onTypeChar: (char) => {
            // Queue character for keyboard/IK typing via controller
            if (typingController && typingEnabled) {
                typingController.typeCharacter(char);
            }
        },
        onTypingStart: (text) => {
            console.log('ChatManager: Typing started', text.slice(0, 30) + '...');
        },
        onTypingEnd: () => {
            console.log('ChatManager: Typing ended');
            // Return arms to rest position
            if (typingController) {
                typingController.returnToRest();
            }
            if (wendy) {
                wendy.clearLookTarget();
            }
        },
    });

    // Create IK debug helpers
    ikDebugHelpers = createIKDebugHelpers(sceneComponents.scene);
    setupIKPanel();
    setupKeyboardPanel();

    // Create state machine
    stateMachine = new StateMachine();

    // Wire up state transitions
    stateMachine.addEventListener('transition', handleStateTransition);

    // Wire up state results (contains parsed data like messages)
    stateMachine.addEventListener('result', handleStateResult);

    // Start render loop
    animate();
}

// =============================================================================
// State Handling
// =============================================================================

function handleStateTransition(e) {
    const { from, to, data } = e.detail;
    currentState = to;

    // Track session lifecycle
    if (to === 'waking' && chatManager) {
        chatManager.onSessionStart();
    }

    // -------------------------------------------------------------------------
    // Handle LEAVING states first (before entering new states)
    // -------------------------------------------------------------------------

    // Clear overlay when leaving editing state (unless going to another overlay state)
    if (from === 'editing') {
        stopRandomTyping();
        // Only clear if NOT going to another overlay state
        if (to !== 'read_file' && to !== 'terminal' && monitor) {
            monitor.clearOverlay();
        }
    }

    // Clear overlay when leaving read_file state (unless going to another overlay state)
    if (from === 'read_file') {
        // Stop reading animation
        if (wendy) {
            wendy.stopReading();
        }
        // Only clear if NOT going to another overlay state
        if (to !== 'editing' && to !== 'terminal' && monitor) {
            monitor.clearOverlay();
        }
    }

    // Clear overlay when leaving terminal state (unless going to another overlay state)
    if (from === 'terminal') {
        if (to !== 'editing' && to !== 'read_file' && monitor) {
            monitor.clearOverlay();
        }
    }

    // -------------------------------------------------------------------------
    // Handle ENTERING states
    // -------------------------------------------------------------------------

    // Handle send_message state - trigger Wendy typing
    if (to === 'send_message') {
        console.log('[DEBUG] send_message state transition:', {
            hasStartTyping: data.startTyping,
            hasMessageContent: !!data.messageContent,
            messageContent: data.messageContent?.slice(0, 50),
            hasChatManager: !!chatManager,
        });

        if (data.startTyping && data.messageContent && chatManager) {
            const isInitial = chatManager.isInitialLoad();
            const inGracePeriod = (Date.now() - pageLoadTime) < INITIAL_LOAD_GRACE_PERIOD;
            console.log('[DEBUG] About to type, isInitialLoad:', isInitial, 'inGracePeriod:', inGracePeriod);
            if (isInitial || inGracePeriod) {
                // Just add to chat without animation (during initial load or grace period)
                console.log('[DEBUG] Initial/grace period - adding message without animation');
                chatManager.addWendyMessage(data.messageContent);
            } else {
                // Type with animation
                console.log('[DEBUG] Live mode - calling typeMessage()');
                chatManager.typeMessage(data.messageContent);
            }
        } else {
            console.log('[DEBUG] Skipping typing - missing required data');
        }
    }

    // Handle editing state - show diff and random typing
    if (to === 'editing' && monitor) {
        console.log('[DEBUG] Editing state:', data);
        // Show diff on monitor
        monitor.showDiff(data.filePath || 'unknown', data.oldString || '', data.newString || '');
        // Start random typing animation
        startRandomTyping();
    }

    // Handle read_file state - show file viewer
    if (to === 'read_file' && monitor) {
        console.log('[DEBUG] Read file state:', data);
        // Show file overlay with loading state (content comes in tool_result)
        monitor.showFile(data.filePath || 'unknown', null);
        // Quick typing burst to "open" the file
        doTypingBurst();
        // Start reading animation
        if (wendy) {
            wendy.startReading();
        }
    }

    // Handle terminal state - show command
    if (to === 'terminal' && monitor) {
        console.log('[DEBUG] Terminal state:', data);
        monitor.showTerminal(data.command || '');
        // Quick typing burst to "run" the command
        doTypingBurst();
    }

    // Clear initial load flag when session completes
    if (to === 'done' && chatManager) {
        chatManager.onSessionEnd();
    }

    if (CONFIG.DEBUG) {
        console.log(`State: ${from} -> ${to}`, data);
        updateDebug('state', to);
    }
}

function handleStateResult(e) {
    const { state, data } = e.detail;

    // Handle check_messages result - update chat with messages
    if (state === 'check_messages' && data.messages && data.messages.length > 0 && chatManager) {
        // Extract channel ID from the command if available
        const channelId = extractChannelId(data.command) || 'default';
        chatManager.setCurrentChannel(channelId);
        chatManager.receiveMessages(channelId, data.messages, chatManager.isInitialLoad());
    }

    // Handle read_file result - update file content on monitor
    if (state === 'read_file' && data.result && monitor) {
        monitor.updateFileContent(data.result);
    }

    // Handle terminal result - update output on monitor
    if (state === 'terminal' && data.result && monitor) {
        monitor.updateTerminalOutput(data.result);
    }
}

/**
 * Extract channel ID from check_messages command
 */
function extractChannelId(command) {
    if (!command) return null;
    const match = command.match(/check_messages\/(\d+)/);
    return match ? match[1] : null;
}

// =============================================================================
// Animation Loop
// =============================================================================

function animate() {
    requestAnimationFrame(animate);

    const delta = sceneComponents.clock.getDelta();

    // Update Wendy
    if (wendy) {
        wendy.update(delta);

        // Run typing IK if enabled (via controller)
        if (typingEnabled && typingController) {
            typingController.update(delta);

            // Check if typing is complete (queue empty, no current keypress, all chars displayed)
            if (!typingController.isTyping() && monitor && monitor.isTypingComplete()) {
                if (chatManager) {
                    chatManager.finishTyping();
                } else {
                    monitor.finishTyping();
                }
            }
        }
        // Otherwise run manual IK debug if enabled
        else if (ikEnabled && ikDebugHelpers) {
            const arm = ikArm === 'left' ? wendy.leftArm : wendy.rightArm;
            solveTwoBoneIK(arm, ikTarget, ikPole);

            // Update debug helper positions
            ikDebugHelpers.target.position.copy(ikTarget);
            ikDebugHelpers.pole.position.copy(ikPole);
            ikDebugHelpers.updateFromArm(arm);
        }
    }

    // Update keyboard animations
    if (keyboard) {
        keyboard.update(delta);
    }

    // Update monitor
    if (monitor) {
        monitor.update(delta);
    }

    // Update controls
    sceneComponents.controls.update();

    // Update camera debug display
    updateCameraDebug();

    // Render
    sceneComponents.renderer.render(sceneComponents.scene, sceneComponents.camera);
}

// =============================================================================
// Auth Flow
// =============================================================================

async function handleAuth(e) {
    e.preventDefault();

    const input = document.getElementById('code-input');
    const error = document.getElementById('auth-error');
    const code = input.value.trim();

    if (!code) return;

    error.textContent = '';
    input.disabled = true;

    try {
        await stream.authenticate(code);
        showScene();
        initApp();
        connectStream();
    } catch (err) {
        error.textContent = err.message || 'Authentication failed';
        input.disabled = false;
        input.focus();
    }
}

function showAuth() {
    document.getElementById('auth-screen').classList.remove('hidden');
    document.getElementById('scene-wrapper').classList.add('hidden');
}

function showScene() {
    document.getElementById('auth-screen').classList.add('hidden');
    document.getElementById('scene-wrapper').classList.remove('hidden');
    // Apply first camera preset on load
    setCameraPreset(0);
}

// =============================================================================
// Stream Connection
// =============================================================================

function connectStream() {
    stream.addEventListener('connected', () => {
        document.getElementById('debug-panel').classList.add('connected');
        updateDebug('connected', 'yes');
    });

    stream.addEventListener('disconnected', () => {
        document.getElementById('debug-panel').classList.remove('connected');
        updateDebug('connected', 'no');

        // Abort any in-progress typing on disconnect
        if (chatManager) {
            chatManager.abortTyping();
        }
    });

    // Handle tab visibility changes
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            // Tab hidden - abort typing to prevent desync
            if (chatManager && chatManager.isTyping()) {
                console.log('Tab hidden, aborting typing');
                chatManager.abortTyping();
            }
        }
    });

    stream.addEventListener('event', (e) => {
        eventCount++;
        updateDebug('events', eventCount);

        const classified = classifyEvent(e.detail);

        // Process for chat management
        processEventForChat(classified);

        // Process through state machine
        stateMachine.processEvent(classified);
    });

    stream.connect();
}

// =============================================================================
// Debug Helpers
// =============================================================================

function updateDebug(field, value) {
    if (!CONFIG.DEBUG) return;
    const el = document.getElementById(`debug-${field}`);
    if (el) el.textContent = value;
}

// =============================================================================
// Mock/Replay (exposed globally for debug buttons)
// =============================================================================

let replayRunning = false;

async function replaySession(sessionData) {
    if (replayRunning) {
        console.log('Replay already running');
        return;
    }

    replayRunning = true;
    console.log(`Starting replay with ${sessionData.length} events`);

    // Reset chat manager for replay (forces animation)
    if (chatManager) {
        chatManager.reset();
        chatManager.onSessionStart();  // Mark as new session
        chatManager.onSessionEnd();    // Mark first session done so animations play
    }

    // Clear monitor to start fresh
    if (monitor) {
        monitor.setMessages([]);
    }

    for (const item of sessionData) {
        if (!replayRunning) break;
        await sleep(item.delay || 500);

        const classified = classifyEvent({
            ts: Date.now(),
            channel_id: 'test',
            event: item.event
        });

        // Process event for chat manager
        processEventForChat(classified);

        // Also send to state machine
        stateMachine.processEvent(classified);
    }

    replayRunning = false;
    console.log('Replay complete');
}

/**
 * Process a classified event for chat management
 */
function processEventForChat(classified) {
    const { type, subtype, content, messageContent } = classified;

    if (!chatManager) return;

    // Handle send_message tool use - queue the message
    if (type === 'tool_use' && (subtype === 'send_message' || subtype === 'Bash')) {
        const cmd = content?.command || '';
        if (cmd.includes('send_message')) {
            const extractedMessage = messageContent || extractMessageFromCommand(cmd);
            const channelId = extractChannelIdFromSend(cmd) || chatManager.currentChannelId || 'default';
            if (extractedMessage) {
                chatManager.queueWendyMessage(channelId, extractedMessage);
            }
        }
    }
}

/**
 * Extract message content from a curl command
 */
function extractMessageFromCommand(cmd) {
    if (!cmd) return null;

    // Match "content": "..." pattern
    const match = cmd.match(/"content"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (match) {
        return match[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\');
    }
    return null;
}

/**
 * Extract channel ID from send_message command
 */
function extractChannelIdFromSend(cmd) {
    if (!cmd) return null;
    const match = cmd.match(/"channel_id"\s*:\s*"(\d+)"/);
    return match ? match[1] : null;
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Expose for HTML onclick handlers and console debugging
window.mockSession = () => replaySession(MOCK_SESSION);
window.mockQuickSession = () => replaySession(MOCK_QUICK_SESSION);
window.mockEditSession = () => replaySession(MOCK_EDIT_SESSION);
window.mockReadSession = () => replaySession(MOCK_READ_SESSION);
window.stopReplay = () => { replayRunning = false; };
window.getWendy = () => wendy;
window.getKeyboard = () => keyboard;
window.getIKTarget = () => ikTarget;
window.getIKPole = () => ikPole;
window.getIKHelpers = () => ikDebugHelpers;
window.getChatManager = () => chatManager;
window.getTypingController = () => typingController;

// =============================================================================
// Camera Presets
// =============================================================================

const CAMERA_PRESETS = [
    {
        name: 'Over Shoulder',
        position: { x: 0.49, y: 0.77, z: -0.24 },
        target: { x: 0.18, y: 0.60, z: 0.12 },
    },
    {
        name: "Wendy's Face",
        position: { x: 0.53, y: 0.79, z: 0.71 },
        target: { x: 0.11, y: 0.56, z: 0.29 },
    },
    {
        name: 'Keyboard',
        position: { x: 0.66, y: 0.84, z: 0.15 },
        target: { x: 0.33, y: 0.47, z: 0.21 },
    },
    {
        name: 'Computer Only',
        position: { x: 0.03, y: 0.59, z: 0.01 },
        target: { x: 0.01, y: 0.51, z: 0.52 },
    },
];

let currentCameraPreset = 0;

function cycleCamera() {
    currentCameraPreset = (currentCameraPreset + 1) % CAMERA_PRESETS.length;
    applyCameraPreset(currentCameraPreset);
}

function applyCameraPreset(index) {
    const preset = CAMERA_PRESETS[index];
    if (!preset || !sceneComponents) return;

    const { camera, controls } = sceneComponents;

    camera.position.set(preset.position.x, preset.position.y, preset.position.z);
    controls.target.set(preset.target.x, preset.target.y, preset.target.z);
    controls.update();

    const presetLabel = document.getElementById('debug-camera-preset');
    if (presetLabel) {
        presetLabel.textContent = preset.name;
    }

    // Update active button state
    const buttons = document.querySelectorAll('#camera-buttons button');
    buttons.forEach((btn, i) => {
        btn.classList.toggle('active', i === index);
    });
}

function setCameraPreset(index) {
    currentCameraPreset = index;
    applyCameraPreset(index);
}

window.setCameraPreset = setCameraPreset;

function updateCameraDebug() {
    if (!sceneComponents) return;

    const { camera, controls } = sceneComponents;
    const pos = camera.position;
    const tgt = controls.target;

    const debugEl = document.getElementById('debug-camera');
    if (debugEl) {
        debugEl.textContent = `pos(${pos.x.toFixed(2)}, ${pos.y.toFixed(2)}, ${pos.z.toFixed(2)}) tgt(${tgt.x.toFixed(2)}, ${tgt.y.toFixed(2)}, ${tgt.z.toFixed(2)})`;
    }
}

window.cycleCamera = cycleCamera;
window.getCameraPresets = () => CAMERA_PRESETS;

// =============================================================================
// Typing IK System (delegates to TypingController)
// =============================================================================

/**
 * Queue a character for typing via the TypingController
 * @param {string} char - The character to type
 */
function typeCharacter(char) {
    if (!typingController || !typingEnabled) return;
    typingController.typeCharacter(char);
}

/**
 * Return arms to rest position over home row
 */
function returnArmsToRest() {
    if (!typingController) return;
    typingController.returnToRest();
}

// =============================================================================
// Random Typing (for editing state)
// =============================================================================

let randomTypingInterval = null;
const RANDOM_TYPING_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789 ';
const RANDOM_TYPING_DELAY = 120;  // ms between keypresses

/**
 * Start random typing animation (used during editing)
 */
function startRandomTyping() {
    if (randomTypingInterval) return;  // Already typing

    console.log('[DEBUG] Starting random typing');

    randomTypingInterval = setInterval(() => {
        // Pick a random character
        const char = RANDOM_TYPING_CHARS[Math.floor(Math.random() * RANDOM_TYPING_CHARS.length)];
        typeCharacter(char);
    }, RANDOM_TYPING_DELAY);
}

/**
 * Stop random typing animation
 */
function stopRandomTyping() {
    if (!randomTypingInterval) return;

    console.log('[DEBUG] Stopping random typing');

    clearInterval(randomTypingInterval);
    randomTypingInterval = null;
    returnArmsToRest();

    if (wendy) {
        wendy.clearLookTarget();
    }
}

/**
 * Do a quick burst of random keypresses (for transitions like opening a file)
 * @param {number} count - Number of keypresses (default 2-5 random)
 */
function doTypingBurst(count = null) {
    const numKeys = count || Math.floor(Math.random() * 4) + 2;  // 2-5 keys
    let keysTyped = 0;
    const burstDelay = 80;  // Faster than normal typing

    const burstInterval = setInterval(() => {
        if (keysTyped >= numKeys) {
            clearInterval(burstInterval);
            // Return to rest after a short delay
            setTimeout(() => {
                returnArmsToRest();
                if (wendy) {
                    wendy.clearLookTarget();
                }
            }, 200);
            return;
        }

        const char = RANDOM_TYPING_CHARS[Math.floor(Math.random() * RANDOM_TYPING_CHARS.length)];
        typeCharacter(char);
        keysTyped++;
    }, burstDelay);
}

/**
 * Type a string with IK animation (used by debug panel only)
 * Note: For stream events, ChatManager handles typing
 */
async function typeStringWithIK(text, delay = 300) {
    // Don't start if ChatManager is typing
    if (chatManager && chatManager.isTyping()) {
        console.warn('typeStringWithIK: ChatManager is typing, ignoring');
        return;
    }

    // Start typing on monitor (for debug panel use)
    if (monitor) {
        monitor.startTyping(text);
    }

    for (const char of text) {
        typeCharacter(char);
        await new Promise(r => setTimeout(r, delay));
    }

    // Finish and return to rest
    setTimeout(() => {
        if (monitor) {
            monitor.finishTyping();
        }
        returnArmsToRest();
        if (wendy) {
            wendy.clearLookTarget();
        }
    }, 500);
}

// =============================================================================
// Keyboard Debug Panel
// =============================================================================

function setupKeyboardPanel() {
    const input = document.getElementById('keyboard-input');
    const typeBtn = document.getElementById('keyboard-type-btn');
    const demoBtn = document.getElementById('keyboard-demo-btn');
    const ikCheckbox = document.getElementById('typing-ik-enabled');

    if (!input || !typeBtn) return;

    // Typing IK toggle
    if (ikCheckbox) {
        ikCheckbox.checked = typingEnabled;  // Sync with default
        ikCheckbox.addEventListener('change', (e) => {
            typingEnabled = e.target.checked;
            // TypingController handles its own initialization
        });
    }

    // Live typing as user types
    input.addEventListener('input', (e) => {
        const char = e.data;
        if (char && keyboard) {
            if (typingEnabled) {
                typeCharacter(char);
            } else {
                keyboard.pressKey(char);
            }
        }
    });

    // Type button - type the full input
    typeBtn.addEventListener('click', () => {
        if (keyboard && input.value) {
            if (typingEnabled) {
                typeStringWithIK(input.value, 300);
            } else {
                keyboard.typeString(input.value, 80);
            }
        }
    });

    // Demo button - type a sample sentence
    demoBtn.addEventListener('click', () => {
        const demoText = "Hello, I'm Wendy!";
        input.value = demoText;
        if (keyboard) {
            if (typingEnabled) {
                typeStringWithIK(demoText, 300);
            } else {
                keyboard.typeString(demoText, 100);
            }
        }
    });
}

// =============================================================================
// IK Debug Panel
// =============================================================================

function setupIKPanel() {
    // Target position sliders
    setupSlider('ik-x', (v) => { ikTarget.x = v; });
    setupSlider('ik-y', (v) => { ikTarget.y = v; });
    setupSlider('ik-z', (v) => { ikTarget.z = v; });

    // Pole position sliders
    setupSlider('pole-x', (v) => { ikPole.x = v; });
    setupSlider('pole-y', (v) => { ikPole.y = v; });
    setupSlider('pole-z', (v) => { ikPole.z = v; });

    // Enable checkbox
    const enableCheckbox = document.getElementById('ik-enabled');
    enableCheckbox.addEventListener('change', (e) => {
        ikEnabled = e.target.checked;
        ikDebugHelpers.setVisible(ikEnabled);
    });

    // Arm selector
    const armSelect = document.getElementById('ik-arm');
    armSelect.addEventListener('change', (e) => {
        ikArm = e.target.value;
    });
}

function setupSlider(id, setter) {
    const slider = document.getElementById(id);
    const valSpan = document.getElementById(id + '-val');

    if (!slider || !valSpan) return;

    // Set initial value
    setter(parseFloat(slider.value));

    slider.addEventListener('input', () => {
        const v = parseFloat(slider.value);
        valSpan.textContent = v.toFixed(2);
        setter(v);
    });
}

// =============================================================================
// Scene Tuner (camera target only)
// =============================================================================

let sceneTunerEnabled = false;

window.toggleSceneTuner = () => {
    const panel = document.getElementById('scene-tuner');
    sceneTunerEnabled = !sceneTunerEnabled;
    panel.classList.toggle('hidden', !sceneTunerEnabled);
    if (sceneTunerEnabled) {
        setupSceneTunerSliders();
    }
};

function setupSceneTunerSliders() {
    if (!sceneComponents || !sceneComponents.controls) return;

    const controls = sceneComponents.controls;

    const sliders = {
        'cam-target-x': (v) => { controls.target.x = v; },
        'cam-target-y': (v) => { controls.target.y = v; },
        'cam-target-z': (v) => { controls.target.z = v; },
    };

    // Set initial slider values from current camera target
    document.getElementById('cam-target-x').value = controls.target.x;
    document.getElementById('cam-target-y').value = controls.target.y;
    document.getElementById('cam-target-z').value = controls.target.z;

    // Update display values
    for (const id of Object.keys(sliders)) {
        const el = document.getElementById(id);
        const valEl = document.getElementById(id + '-val');
        if (el && valEl) {
            valEl.textContent = parseFloat(el.value).toFixed(2);
        }
    }

    // Attach listeners
    for (const [id, setter] of Object.entries(sliders)) {
        const el = document.getElementById(id);
        if (el) {
            el.oninput = () => {
                const v = parseFloat(el.value);
                document.getElementById(id + '-val').textContent = v.toFixed(2);
                setter(v);
                updateSceneTunerOutput();
            };
        }
    }

    updateSceneTunerOutput();
}

function updateSceneTunerOutput() {
    if (!sceneComponents || !sceneComponents.controls) return;
    const controls = sceneComponents.controls;
    const output = {
        cameraTarget: {
            x: controls.target.x.toFixed(2),
            y: controls.target.y.toFixed(2),
            z: controls.target.z.toFixed(2),
        },
    };
    document.getElementById('scene-tuner-output').textContent = JSON.stringify(output, null, 2);
}

window.copySceneValues = () => {
    const text = document.getElementById('scene-tuner-output').textContent;
    navigator.clipboard.writeText(text);
};

// =============================================================================
// Monitor Setup
// =============================================================================

function createMonitor(scene) {
    // Monitor dimensions
    const screenWidth = 0.48;
    const screenHeight = 0.30;
    const frameDepth = 0.03;
    const frameThickness = 0.015;
    const standHeight = 0.08;
    const standWidth = 0.12;
    const standDepth = 0.08;

    // Create offscreen canvas for the screen content
    const canvas = document.createElement('canvas');
    canvas.width = 640;
    canvas.height = 400;

    // Create texture from canvas
    const texture = new THREE.CanvasTexture(canvas);
    texture.minFilter = THREE.LinearFilter;
    texture.magFilter = THREE.LinearFilter;

    // Screen material with emissive for glow effect
    const screenMaterial = new THREE.MeshStandardMaterial({
        map: texture,
        emissive: 0xffffff,
        emissiveMap: texture,
        emissiveIntensity: 0.3,
    });

    // Frame material
    const frameMaterial = new THREE.MeshStandardMaterial({
        color: 0x1a1a1a,
        roughness: 0.8,
    });

    // Create monitor group
    monitorMesh = new THREE.Group();

    // Screen plane
    const screenGeometry = new THREE.PlaneGeometry(screenWidth, screenHeight);
    const screen = new THREE.Mesh(screenGeometry, screenMaterial);
    screen.position.z = frameDepth / 2 + 0.001;  // Slightly in front of frame
    monitorMesh.add(screen);

    // Frame (box behind the screen)
    const frameGeometry = new THREE.BoxGeometry(
        screenWidth + frameThickness * 2,
        screenHeight + frameThickness * 2,
        frameDepth
    );
    const frame = new THREE.Mesh(frameGeometry, frameMaterial);
    frame.castShadow = true;
    frame.receiveShadow = true;
    monitorMesh.add(frame);

    // Stand neck (vertical piece)
    const neckGeometry = new THREE.BoxGeometry(0.03, standHeight, 0.03);
    const neck = new THREE.Mesh(neckGeometry, frameMaterial);
    neck.position.y = -(screenHeight / 2 + frameThickness + standHeight / 2);
    neck.castShadow = true;
    monitorMesh.add(neck);

    // Stand base
    const baseGeometry = new THREE.BoxGeometry(standWidth, 0.01, standDepth);
    const base = new THREE.Mesh(baseGeometry, frameMaterial);
    base.position.y = -(screenHeight / 2 + frameThickness + standHeight);
    base.castShadow = true;
    base.receiveShadow = true;
    monitorMesh.add(base);

    // Position monitor behind keyboard, facing Wendy, tilted slightly toward her
    monitorMesh.position.set(0, 0.55, 0.55);
    monitorMesh.rotation.y = Math.PI;  // Face Wendy
    monitorMesh.rotation.x = 0.15;  // Tilt toward her

    scene.add(monitorMesh);

    // Initialize the Monitor renderer with the canvas and texture
    monitor = new Monitor(canvas, texture);

    // Expose for debugging
    window.getMonitor = () => monitor;
}

// =============================================================================
// Start
// =============================================================================

init();
