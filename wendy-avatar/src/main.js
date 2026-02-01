/**
 * main.js - Application entry point
 *
 * Minimal scene with event feed connection.
 */

import * as THREE from 'three';
import { createScene } from './scene.js';
import { BrainStream, classifyEvent } from './stream.js';
import { StateMachine, STATES } from './states.js';
import { Wendy } from './wendy.js';
import { solveTwoBoneIK, createIKDebugHelpers } from './ik.js';
import { Keyboard } from './keyboard.js';
import { Mouse } from './mouse.js';
import { Monitor } from './monitor.js';
import { ChatManager } from './chat-manager.js';
import { TypingController } from './typing-controller.js';
import { EffectsManager, STYLES } from './effects.js';

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
let mouse = null;
let monitor = null;
let monitorMesh = null;
let monitorLight = null;  // Glow light from monitor
let screenMaterial = null;  // Reference for glow slider
let desk = null;  // Tabletop/desk surface
let chatManager = null;

// Track all positionable scene objects for debug menu
const sceneObjects = {};
let typingController = null;
let effectsManager = null;

// IK state (for manual IK debug panel)
let ikDebugHelpers = null;
let ikTarget = new THREE.Vector3(0.3, 0.2, 0.4);
let ikPole = new THREE.Vector3(0.5, 0.3, -0.5);
let ikEnabled = true;
let ikArm = 'right';

// Typing IK enabled state (controller handles the actual typing)
let typingEnabled = true;  // Enabled by default

// Typing mode management - prevents overlapping typing intervals
const TypingMode = {
    NONE: 'none',
    RANDOM: 'random',      // Continuous random typing (editing state)
    BURST: 'burst',        // Quick burst of random keys
    MESSAGE: 'message',    // Typing a real message via ChatManager
};
let currentTypingMode = TypingMode.NONE;
let typingModeInterval = null;  // Single interval tracker

// Mouse tracking state - when true, left arm follows mouse position
let isTrackingMouse = false;

let currentState = null;
let eventCount = 0;
const pageLoadTime = Date.now();
const INITIAL_LOAD_GRACE_PERIOD = 5000;  // Don't animate for first 5 seconds after page load

// Thought bubble state
let thoughtBubble = null;
let thoughtText = null;
let isThinking = false;

// State transition queue (for handling transitions during typing)
let pendingTransitions = [];
let isRushingTyping = false;

/**
 * Check if we're still in the initial load period (historical events replaying)
 * @returns {boolean}
 */
function isInInitialLoadPeriod() {
    return (Date.now() - pageLoadTime) < INITIAL_LOAD_GRACE_PERIOD;
}

/**
 * Process pending state transitions after typing completes
 * Applies each state in order with a minimum 1 second between each
 */
function processPendingTransitions() {
    if (pendingTransitions.length === 0) return;

    console.log(`[Queue] Processing ${pendingTransitions.length} pending transitions`);

    // Copy and clear the queue
    const transitions = [...pendingTransitions];
    pendingTransitions = [];
    isRushingTyping = false;

    // Apply each transition in order with delay
    let delay = 0;

    transitions.forEach((transition, index) => {
        setTimeout(() => {
            const fakeEvent = {
                detail: {
                    from: currentState,
                    to: transition.to,
                    data: transition.data
                }
            };
            console.log(`[Queue] Applying state ${index + 1}/${transitions.length}: ${transition.to}`);
            handleStateTransition(fakeEvent);
        }, delay);

        // Thinking gets 3s, other states get 1s
        const stateDelay = transition.to === 'thinking' ? 3000 : 1000;
        delay += stateDelay;
    });
}

// =============================================================================
// Thought Bubble
// =============================================================================

/**
 * Show thought bubble with text above Wendy's head
 * @param {string} text - Thinking text to display
 */
function showThoughtBubble(text) {
    if (!thoughtBubble || !thoughtText) {
        thoughtBubble = document.getElementById('thought-bubble');
        thoughtText = document.getElementById('thought-text');
    }
    if (!thoughtBubble || !thoughtText) return;

    // Truncate long thinking text
    const maxLength = 200;
    const displayText = text.length > maxLength
        ? text.slice(0, maxLength) + '...'
        : text;

    thoughtText.textContent = displayText;
    thoughtBubble.classList.remove('hidden');
    isThinking = true;

    // Position will be updated in animate loop
    updateThoughtBubblePosition();
}

/**
 * Hide thought bubble
 */
function hideThoughtBubble() {
    if (!thoughtBubble) {
        thoughtBubble = document.getElementById('thought-bubble');
    }
    if (thoughtBubble) {
        thoughtBubble.classList.add('hidden');
    }
    isThinking = false;
}

/**
 * Update thought bubble position to float above Wendy's head
 * Clamps to viewport bounds if target is off-screen
 */
function updateThoughtBubblePosition() {
    if (!isThinking || !thoughtBubble || !wendy || !sceneComponents) return;

    // Get position above Wendy's head in world space
    const headPos = new THREE.Vector3();
    wendy.head.getWorldPosition(headPos);
    headPos.y += 0.25;  // Offset above head

    // Project to screen coordinates
    const screenPos = headPos.clone();
    screenPos.project(sceneComponents.camera);

    // Convert to CSS coordinates
    const canvas = sceneComponents.renderer.domElement;
    const rect = canvas.getBoundingClientRect();

    let x = (screenPos.x * 0.5 + 0.5) * rect.width + rect.left;
    let y = (-screenPos.y * 0.5 + 0.5) * rect.height + rect.top;

    // Get bubble dimensions for clamping
    const bubbleRect = thoughtBubble.getBoundingClientRect();
    const bubbleWidth = bubbleRect.width || 200;  // Fallback if not yet rendered
    const bubbleHeight = bubbleRect.height || 50;
    const padding = 50;  // Padding from canvas edge

    // Clamp X to keep bubble within canvas bounds
    // Bubble is centered horizontally, so account for half width
    const minX = rect.left + padding + bubbleWidth / 2;
    const maxX = rect.right - padding - bubbleWidth / 2;
    x = Math.max(minX, Math.min(maxX, x));

    // Clamp Y to keep bubble within canvas bounds
    // Bubble is positioned above the point (transform: translate(-50%, -100%))
    // So the bubble extends upward from y
    const minY = rect.top + padding + bubbleHeight;  // Need room for full bubble height above
    const maxY = rect.bottom - padding;
    y = Math.max(minY, Math.min(maxY, y));

    // Position bubble centered above the point
    thoughtBubble.style.left = `${x}px`;
    thoughtBubble.style.top = `${y}px`;
    thoughtBubble.style.transform = 'translate(-50%, -100%)';
}

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
    wendy.setPosition(0, 0.51, -0.04);

    // Create desk/tabletop
    desk = createDesk(sceneComponents.scene);

    // Create keyboard and add to scene
    keyboard = new Keyboard();
    keyboard.addToScene(sceneComponents.scene);
    keyboard.setPosition(0, 0.45, 0.34);
    keyboard.group.rotation.y = Math.PI;

    // Create mouse and add to scene (left side of keyboard)
    mouse = new Mouse();
    mouse.addToScene(sceneComponents.scene);
    mouse.setPosition(-0.43, 0.45, 0.30);
    mouse.group.rotation.y = Math.PI;  // Face same direction as keyboard

    // Create 3D monitor with canvas texture
    createMonitor(sceneComponents.scene);

    // Register objects for debug menu
    sceneObjects.wendy = { obj: wendy.group, name: 'Wendy' };
    sceneObjects.desk = { obj: desk, name: 'Desk' };
    sceneObjects.keyboard = { obj: keyboard.group, name: 'Keyboard' };
    sceneObjects.mouse = { obj: mouse.group, name: 'Mouse' };
    sceneObjects.monitor = { obj: monitorMesh, name: 'Monitor' };
    sceneObjects.monitorLight = { obj: monitorLight, name: 'Monitor Light' };

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
            // Set typing mode to prevent other typing from interfering
            if (currentTypingMode !== TypingMode.NONE) {
                stopCurrentTypingMode();
            }
            currentTypingMode = TypingMode.MESSAGE;
        },
        onTypingEnd: () => {
            console.log('ChatManager: Typing ended');
            // Clear typing mode
            currentTypingMode = TypingMode.NONE;
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

    // Create state machine
    stateMachine = new StateMachine();

    // Wire up state transitions
    stateMachine.addEventListener('transition', handleStateTransition);

    // Wire up state results (contains parsed data like messages)
    stateMachine.addEventListener('result', handleStateResult);

    // Create effects manager for visual styles
    effectsManager = new EffectsManager(
        sceneComponents.renderer,
        sceneComponents.scene,
        sceneComponents.camera
    );


    // Handle window resize for effects
    window.addEventListener('resize', () => {
        if (effectsManager) {
            const width = window.innerWidth;
            const height = window.innerHeight;
            effectsManager.resize(width, height);
        }
    });

    // Start logic loop (runs even when tab is hidden)
    lastLogicTime = performance.now();
    setInterval(updateLogic, 16);  // ~60fps logic updates

    // Start render loop
    animate();
}

// =============================================================================
// State Handling
// =============================================================================

function handleStateTransition(e) {
    const { from, to, data } = e.detail;

    // If a new session starts (waking), abort everything and start fresh
    if (to === 'waking') {
        console.log('[Queue] New session starting - clearing queue and aborting typing');
        pendingTransitions = [];
        isRushingTyping = false;
        if (chatManager && chatManager.isTyping()) {
            chatManager.abortTyping();
        }
        if (typingController) {
            typingController.clearQueue();
        }
        currentTypingMode = TypingMode.NONE;
        // Don't return - let this transition apply immediately
    }
    // If we're currently typing a message and a new transition comes in,
    // queue it and speed up typing instead of interrupting
    else if (chatManager && chatManager.isTyping() && currentState === 'send_message') {
        console.log(`[Queue] Transition queued while typing: ${to}`, data);
        pendingTransitions.push({ from, to, data });

        // Speed up typing to finish faster
        if (!isRushingTyping) {
            isRushingTyping = true;
            chatManager.rushTyping();
            console.log('[Queue] Rushing typing to finish');
        }
        return;  // Don't apply this transition yet
    }

    currentState = to;

    // Smoothly move camera to state's preset position
    setCameraForState(to);

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

    // Hide thought bubble and stop thinking animation when leaving thinking state
    if (from === 'thinking') {
        hideThoughtBubble();
        if (wendy) {
            wendy.stopThinking();
        }
    }

    // Cancel typing when leaving send_message state
    if (from === 'send_message') {
        if (chatManager) {
            chatManager.abortTyping();
        }
        if (typingController) {
            typingController.clearQueue();
        }
        currentTypingMode = TypingMode.NONE;
        // Clear any pending transitions since we're handling the state change now
        pendingTransitions = [];
        isRushingTyping = false;
    }

    // -------------------------------------------------------------------------
    // Handle ENTERING states
    // -------------------------------------------------------------------------

    // Handle thinking state - show thought bubble and thinking animation
    if (to === 'thinking') {
        // Only show thought bubble if not in initial load period
        if (!isInInitialLoadPeriod() && data.text) {
            showThoughtBubble(data.text);
            // Start thinking animation
            if (wendy) {
                wendy.startThinking();
            }
        }
    }

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
            const inGracePeriod = isInInitialLoadPeriod();
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

    // Handle editing state - show diff and mouse + typing interaction
    if (to === 'editing' && monitor) {
        console.log('[DEBUG] Editing state:', data);
        // Show diff on monitor
        monitor.showDiff(data.filePath || 'unknown', data.oldString || '', data.newString || '');
        // Only do mouse + left hand + typing if not in initial load period
        if (!isInInitialLoadPeriod()) {
            doMouseAndLeftHandKeysThenType();
        }
    }

    // Handle read_file state - show file viewer
    if (to === 'read_file' && monitor) {
        console.log('[DEBUG] Read file state:', data);
        // Show file overlay with loading state (content comes in tool_result)
        monitor.showFile(data.filePath || 'unknown', null);
        // Only do mouse + left hand interaction if not in initial load period
        if (!isInInitialLoadPeriod()) {
            doMouseAndLeftHandKeys();
        }
        // Start reading animation
        if (wendy) {
            wendy.startReading();
        }
    }

    // Handle terminal state - show command
    if (to === 'terminal' && monitor) {
        console.log('[DEBUG] Terminal state:', data);
        monitor.showTerminal(data.command || '');
        // Only do typing burst if not in initial load period
        if (!isInInitialLoadPeriod()) {
            doTypingBurst();
        }
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

// Logic clock for background updates (separate from render clock)
let logicClock = null;
let lastLogicTime = 0;

/**
 * Logic update loop - runs via setInterval even when tab is hidden
 * Handles typing, state transitions, and other non-visual updates
 */
function updateLogic() {
    if (!sceneComponents) return;

    const now = performance.now();
    const delta = (now - lastLogicTime) / 1000;
    lastLogicTime = now;

    // Cap delta to prevent huge jumps after tab regains focus
    const cappedDelta = Math.min(delta, 0.1);

    // Update typing controller (IK logic)
    if (typingEnabled && typingController) {
        typingController.update(cappedDelta);

        // Check if typing is complete
        if (!typingController.isTyping()) {
            if (chatManager && chatManager.isTypingComplete()) {
                chatManager.finishTyping();
                if (currentTypingMode === TypingMode.MESSAGE) {
                    currentTypingMode = TypingMode.NONE;
                }
                processPendingTransitions();
            }
        }
    }

    // Update mouse
    if (mouse) {
        mouse.update(cappedDelta);
        if (isTrackingMouse && typingController) {
            const mousePos = mouse.getGripPosition();
            typingController.moveLeftArmTo(mousePos);
        }
    }

    // Update keyboard
    if (keyboard) {
        keyboard.update(cappedDelta);
    }

    // Update monitor
    if (monitor) {
        monitor.update(cappedDelta);
    }
}

function animate() {
    requestAnimationFrame(animate);

    const delta = sceneComponents.clock.getDelta();

    // Update Wendy (visual animations like breathing, blinking)
    // Note: IK for typing is applied in updateLogic() via typingController
    if (wendy) {
        wendy.update(delta);

        // Manual IK debug (only if typing is disabled)
        if (!typingEnabled && ikEnabled && ikDebugHelpers) {
            const arm = ikArm === 'left' ? wendy.leftArm : wendy.rightArm;
            solveTwoBoneIK(arm, ikTarget, ikPole);

            ikDebugHelpers.target.position.copy(ikTarget);
            ikDebugHelpers.pole.position.copy(ikPole);
            ikDebugHelpers.updateFromArm(arm);
        }
    }

    // Update thought bubble position (tracks Wendy's head)
    if (isThinking) {
        updateThoughtBubblePosition();
    }

    // Subtle monitor light flicker (CRT effect)
    if (monitorLight) {
        const time = sceneComponents.clock.getElapsedTime();
        // Base intensity with subtle random flicker
        const flicker = 1.0 + Math.sin(time * 30) * 0.02 + Math.sin(time * 60) * 0.01;
        monitorLight.intensity = 2.5 * flicker;
    }

    // Update camera animation (smooth transitions between presets)
    updateCameraAnimation(delta);

    // Update controls (skip if camera animation is handling it)
    if (!cameraAnimation.active) {
        sceneComponents.controls.update();
    }

    // Update camera debug display
    updateCameraDebug();

    // Update effects and check if we should render this frame (frame rate limiting)
    const shouldRender = effectsManager ? effectsManager.update(delta) : true;

    // Render (through effects manager if available, otherwise direct)
    if (shouldRender) {
        if (effectsManager) {
            effectsManager.render();
        } else {
            sceneComponents.renderer.render(sceneComponents.scene, sceneComponents.camera);
        }
    }
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

    // Note: We no longer abort typing when tab is hidden.
    // The logic loop runs via setInterval even when tab is hidden.

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
// State Trigger (for debug panel)
// =============================================================================

// Sample data for each state
const STATE_SAMPLES = {
    idle: {},
    waking: {},
    check_messages: {
        command: 'curl -s http://localhost:8945/api/check_messages/123456'
    },
    thinking: {
        text: 'Let me analyze this code and figure out the best approach to fix the collision detection...'
    },
    terminal: {
        command: 'cd /data/wendy/coding && ./deploy.sh platformer3d'
    },
    editing: {
        filePath: '/data/wendy/coding/game/physics.js',
        oldString: `function checkCollision(pos) {
    if (pos.y <= 0) {
        return true;
    }
    return false;
}`,
        newString: `function checkCollision(pos, bounds) {
    // Only collide within bounds
    if (pos.y <= 0 &&
        pos.x >= bounds.min.x &&
        pos.x <= bounds.max.x) {
        return true;
    }
    return false;
}`
    },
    read_file: {
        filePath: '/data/wendy/coding/game/player.js'
    },
    send_message: {
        startTyping: true,
        messageContent: 'Done! I fixed the collision detection so you can fall off the edge now.'
    },
    done: {
        success: true
    }
};

let activeStateButton = null;

/**
 * Directly trigger a state with sample data (stays until another is clicked)
 */
function triggerState(stateName) {
    const data = STATE_SAMPLES[stateName] || {};

    // Update active button styling
    if (activeStateButton) {
        activeStateButton.classList.remove('active');
    }
    const buttons = document.querySelectorAll('.state-buttons button');
    buttons.forEach(btn => {
        if (btn.textContent === stateName) {
            btn.classList.add('active');
            activeStateButton = btn;
        }
    });

    // Reset chat manager for clean state
    if (chatManager && stateName === 'send_message') {
        chatManager.reset();
        chatManager.onSessionStart();
        chatManager.onSessionEnd();  // Mark first session done so animations play
    }

    // For read_file, show sample content after a moment
    if (stateName === 'read_file' && monitor) {
        setTimeout(() => {
            monitor.updateFileContent(`   1→import * as THREE from 'three';
   2→
   3→export class Player {
   4→    constructor(scene) {
   5→        this.scene = scene;
   6→        this.position = new THREE.Vector3(0, 1, 0);
   7→        this.velocity = new THREE.Vector3();
   8→        this.grounded = false;
   9→    }
  10→
  11→    update(delta) {
  12→        if (!this.grounded) {
  13→            this.velocity.y -= 9.8 * delta;
  14→        }
  15→        this.position.add(this.velocity.clone().multiplyScalar(delta));
  16→    }
  17→}`);
        }, 500);
    }

    // For terminal, show sample output after a moment
    if (stateName === 'terminal' && monitor) {
        setTimeout(() => {
            monitor.updateTerminalOutput(`Deploying platformer3d...
Creating tarball...
Tarball size: 3083 bytes
Deployment successful!
URL: https://wendy.monster/platformer3d/`);
        }, 800);
    }

    // Transition to the state
    stateMachine.transition(stateName, data);

    console.log(`Triggered state: ${stateName}`, data);
}

/**
 * Process a classified event for chat management (used by live stream)
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

// Expose for HTML onclick handlers and console debugging
window.triggerState = triggerState;
window.getWendy = () => wendy;
window.getKeyboard = () => keyboard;
window.getMouse = () => mouse;
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
        position: { x: -0.59, y: 1.15, z: -0.21 },
        target: { x: -0.10, y: 0.69, z: 0.26 },
    },
    {
        name: "Wendy's Face",
        position: { x: -0.46, y: 1.00, z: 0.69 },
        target: { x: -0.06, y: 0.80, z: 0.24 },
    },
    {
        name: 'Keyboard',
        position: { x: 0.66, y: 1.10, z: 0.15 },
        target: { x: 0.33, y: 0.73, z: 0.21 },
    },
    {
        name: 'Computer Only',
        position: { x: 0.03, y: 0.85, z: 0.01 },
        target: { x: 0.01, y: 0.77, z: 0.52 },
    },
];

let currentCameraPreset = 0;

// State to camera preset mapping (index into CAMERA_PRESETS)
// 0 = Over Shoulder, 1 = Face, 2 = Keyboard, 3 = Computer
const STATE_CAMERA_MAP = {
    idle: 0,
    waking: 0,
    check_messages: 0,
    thinking: 1,      // Face cam for thinking
    terminal: 0,
    editing: 0,
    read_file: 0,
    send_message: 0,
    done: 0,
};

// Camera animation state
let cameraAnimation = {
    active: false,
    startPos: null,
    startTarget: null,
    endPos: null,
    endTarget: null,
    progress: 0,
    duration: 0.8,  // seconds
};

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

    updateCameraUI(index);
}

/**
 * Smoothly animate camera to a preset
 */
function animateCameraToPreset(index) {
    const preset = CAMERA_PRESETS[index];
    if (!preset || !sceneComponents) return;

    const { camera, controls } = sceneComponents;

    // Start animation
    cameraAnimation.active = true;
    cameraAnimation.progress = 0;
    cameraAnimation.startPos = camera.position.clone();
    cameraAnimation.startTarget = controls.target.clone();
    cameraAnimation.endPos = new THREE.Vector3(preset.position.x, preset.position.y, preset.position.z);
    cameraAnimation.endTarget = new THREE.Vector3(preset.target.x, preset.target.y, preset.target.z);

    currentCameraPreset = index;
    updateCameraUI(index);
}

/**
 * Update camera animation (call from animate loop)
 */
function updateCameraAnimation(delta) {
    if (!cameraAnimation.active || !sceneComponents) return;

    const { camera, controls } = sceneComponents;

    cameraAnimation.progress += delta / cameraAnimation.duration;

    if (cameraAnimation.progress >= 1) {
        // Animation complete
        cameraAnimation.progress = 1;
        cameraAnimation.active = false;
    }

    // Smooth easing (ease-in-out)
    const t = cameraAnimation.progress;
    const ease = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;

    // Lerp position and target
    camera.position.lerpVectors(cameraAnimation.startPos, cameraAnimation.endPos, ease);
    controls.target.lerpVectors(cameraAnimation.startTarget, cameraAnimation.endTarget, ease);
    controls.update();
}

/**
 * Update camera UI (buttons, labels)
 */
function updateCameraUI(index) {
    const preset = CAMERA_PRESETS[index];

    const presetLabel = document.getElementById('debug-camera-preset');
    if (presetLabel) {
        presetLabel.textContent = preset?.name || '';
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

/**
 * Trigger camera move for a state (smooth animation)
 */
function setCameraForState(stateName) {
    const presetIndex = STATE_CAMERA_MAP[stateName];
    if (presetIndex !== undefined && presetIndex !== currentCameraPreset) {
        animateCameraToPreset(presetIndex);
    }
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
// Random Typing (for editing state) - with mode management
// =============================================================================

const RANDOM_TYPING_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789 ';
const RANDOM_TYPING_DELAY = 120;  // ms between keypresses

/**
 * Stop any current typing mode and clean up
 */
function stopCurrentTypingMode() {
    if (typingModeInterval) {
        clearInterval(typingModeInterval);
        typingModeInterval = null;
    }

    if (currentTypingMode !== TypingMode.NONE) {
        console.log('[DEBUG] Stopping typing mode:', currentTypingMode);
        currentTypingMode = TypingMode.NONE;
        returnArmsToRest();
        if (wendy) {
            wendy.clearLookTarget();
        }
    }
}

/**
 * Start random typing animation (used during editing)
 */
function startRandomTyping() {
    // Don't start during initial load period
    if (isInInitialLoadPeriod()) {
        console.log('[DEBUG] Cannot start random typing - in initial load period');
        return;
    }

    // Don't start if another mode is active (except NONE)
    if (currentTypingMode !== TypingMode.NONE) {
        console.log('[DEBUG] Cannot start random typing - mode active:', currentTypingMode);
        return;
    }

    console.log('[DEBUG] Starting random typing');
    currentTypingMode = TypingMode.RANDOM;

    typingModeInterval = setInterval(() => {
        // Safety check - stop if mode changed
        if (currentTypingMode !== TypingMode.RANDOM) {
            clearInterval(typingModeInterval);
            typingModeInterval = null;
            return;
        }
        const char = RANDOM_TYPING_CHARS[Math.floor(Math.random() * RANDOM_TYPING_CHARS.length)];
        typeCharacter(char);
    }, RANDOM_TYPING_DELAY);
}

/**
 * Stop random typing animation
 */
function stopRandomTyping() {
    if (currentTypingMode === TypingMode.RANDOM) {
        stopCurrentTypingMode();
    }
}

/**
 * Do a quick burst of random keypresses (for transitions like opening a file)
 * @param {number} count - Number of keypresses (default 2-5 random)
 */
function doTypingBurst(count = null) {
    // Don't start during initial load period
    if (isInInitialLoadPeriod()) {
        console.log('[DEBUG] Cannot start typing burst - in initial load period');
        return;
    }

    // Don't start if another mode is active
    if (currentTypingMode !== TypingMode.NONE) {
        console.log('[DEBUG] Cannot start typing burst - mode active:', currentTypingMode);
        return;
    }

    const numKeys = count || Math.floor(Math.random() * 4) + 2;  // 2-5 keys
    let keysTyped = 0;
    const burstDelay = 80;  // Faster than normal typing

    console.log('[DEBUG] Starting typing burst:', numKeys, 'keys');
    currentTypingMode = TypingMode.BURST;

    typingModeInterval = setInterval(() => {
        // Safety check - stop if mode changed
        if (currentTypingMode !== TypingMode.BURST) {
            clearInterval(typingModeInterval);
            typingModeInterval = null;
            return;
        }

        if (keysTyped >= numKeys) {
            clearInterval(typingModeInterval);
            typingModeInterval = null;
            // Return to rest after a short delay
            setTimeout(() => {
                if (currentTypingMode === TypingMode.BURST) {
                    currentTypingMode = TypingMode.NONE;
                    returnArmsToRest();
                    if (wendy) {
                        wendy.clearLookTarget();
                    }
                }
            }, 200);
            return;
        }

        const char = RANDOM_TYPING_CHARS[Math.floor(Math.random() * RANDOM_TYPING_CHARS.length)];
        typeCharacter(char);
        keysTyped++;
    }, burstDelay);
}

// =============================================================================
// Mouse + Left Hand Interaction (for read_file and editing states)
// =============================================================================

// Right-hand keys (right side of QWERTY keyboard)
const RIGHT_HAND_KEYS = ['y', 'u', 'i', 'o', 'p', 'h', 'j', 'k', 'l', 'n', 'm'];

/**
 * Type a random right-hand key
 */
function typeRightHandKey() {
    const char = RIGHT_HAND_KEYS[Math.floor(Math.random() * RIGHT_HAND_KEYS.length)];
    typeCharacter(char);
}

/**
 * Reading interaction: left hand on mouse, right hand hits 2 keys
 * Used for read_file state
 *
 * Sequence:
 * 1. Arm moves to mouse (at current position)
 * 2. Mouse moves to new random position (arm follows)
 * 3. Arm leaves, mouse stays at new position
 */
function doMouseAndLeftHandKeys() {
    if (isInInitialLoadPeriod() || currentTypingMode !== TypingMode.NONE) return;
    if (!mouse || !typingController) return;

    currentTypingMode = TypingMode.BURST;

    // Step 1: Move arm to mouse's current position
    const initialMousePos = mouse.getGripPosition();
    typingController.moveLeftArmTo(initialMousePos);

    // Step 2: After arm arrives, start tracking and move mouse
    setTimeout(() => {
        isTrackingMouse = true;
        typingController.leftArmLocked = true;  // Lock left arm for mouse

        // Move mouse to random offset from base (stays there)
        const offsetX = (Math.random() - 0.5) * 0.15;
        const offsetZ = (Math.random() - 0.5) * 0.10;
        mouse.moveTo(offsetX, offsetZ);

        // Queue 2 keypresses while moving (only unlocked arm will type)
        setTimeout(() => typeRightHandKey(), 300);
        setTimeout(() => typeRightHandKey(), 600);
    }, 500);

    // Step 3: Arm leaves, mouse stays
    setTimeout(() => {
        isTrackingMouse = false;
        typingController.leftArmLocked = false;  // Unlock left arm
        currentTypingMode = TypingMode.NONE;
        returnArmsToRest();
        if (wendy) {
            wendy.clearLookTarget();
        }
    }, 2500);
}

/**
 * Editing interaction: mouse + keys, then random typing
 * Used for editing state
 *
 * Sequence:
 * 1. Arm moves to mouse (at current position)
 * 2. Mouse moves to new random position (arm follows)
 * 3. Arm leaves, mouse stays, then random typing starts
 */
function doMouseAndLeftHandKeysThenType() {
    if (isInInitialLoadPeriod() || currentTypingMode !== TypingMode.NONE) return;
    if (!mouse || !typingController) return;

    currentTypingMode = TypingMode.BURST;

    // Step 1: Move arm to mouse's current position
    const initialMousePos = mouse.getGripPosition();
    typingController.moveLeftArmTo(initialMousePos);

    // Step 2: After arm arrives, start tracking and move mouse
    setTimeout(() => {
        isTrackingMouse = true;
        typingController.leftArmLocked = true;  // Lock left arm for mouse

        // Move mouse to random offset from base (stays there)
        const offsetX = (Math.random() - 0.5) * 0.15;
        const offsetZ = (Math.random() - 0.5) * 0.10;
        mouse.moveTo(offsetX, offsetZ);

        // Queue 2 keypresses while moving (only unlocked arm will type)
        setTimeout(() => typeRightHandKey(), 300);
        setTimeout(() => typeRightHandKey(), 600);
    }, 500);

    // Step 3: Arm leaves, mouse stays, then typing starts
    setTimeout(() => {
        isTrackingMouse = false;
        typingController.leftArmLocked = false;  // Unlock left arm
        currentTypingMode = TypingMode.NONE;
        startRandomTyping();
    }, 2500);
}

/**
 * Type a string with IK animation (used by debug panel only)
 * Note: For stream events, ChatManager handles typing
 */
async function typeStringWithIK(text, delay = 300) {
    // Don't start if another mode is active
    if (currentTypingMode !== TypingMode.NONE) {
        console.warn('typeStringWithIK: typing mode active, ignoring');
        return;
    }

    // Don't start if ChatManager is typing
    if (chatManager && chatManager.isTyping()) {
        console.warn('typeStringWithIK: ChatManager is typing, ignoring');
        return;
    }

    console.log('[DEBUG] Starting string typing:', text.slice(0, 20));
    currentTypingMode = TypingMode.MESSAGE;

    // Start typing on monitor (for debug panel use)
    if (monitor) {
        monitor.startTyping(text);
    }

    for (const char of text) {
        // Stop if mode was interrupted
        if (currentTypingMode !== TypingMode.MESSAGE) break;
        typeCharacter(char);
        await new Promise(r => setTimeout(r, delay));
    }

    // Finish and return to rest
    setTimeout(() => {
        if (currentTypingMode === TypingMode.MESSAGE) {
            currentTypingMode = TypingMode.NONE;
            if (monitor) {
                monitor.finishTyping();
            }
            returnArmsToRest();
            if (wendy) {
                wendy.clearLookTarget();
            }
        }
    }, 500);
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
// Desk/Tabletop Setup
// =============================================================================

function createDesk(scene) {
    // Desk dimensions
    const width = 1.2;
    const depth = 0.6;
    const thickness = 0.04;
    const legHeight = 0.15;
    const legThickness = 0.04;

    // Desk group
    const deskGroup = new THREE.Group();

    // Tabletop material - dark wood look
    const topMaterial = new THREE.MeshStandardMaterial({
        color: 0x2a1a0a,
        roughness: 0.7,
        metalness: 0.1,
    });

    // Leg material - darker
    const legMaterial = new THREE.MeshStandardMaterial({
        color: 0x1a0f05,
        roughness: 0.8,
    });

    // Tabletop surface
    const topGeometry = new THREE.BoxGeometry(width, thickness, depth);
    const top = new THREE.Mesh(topGeometry, topMaterial);
    top.position.y = legHeight + thickness / 2;
    top.castShadow = true;
    top.receiveShadow = true;
    deskGroup.add(top);

    // Four legs
    const legGeometry = new THREE.BoxGeometry(legThickness, legHeight, legThickness);
    const legPositions = [
        { x: -width / 2 + legThickness, z: -depth / 2 + legThickness },
        { x: width / 2 - legThickness, z: -depth / 2 + legThickness },
        { x: -width / 2 + legThickness, z: depth / 2 - legThickness },
        { x: width / 2 - legThickness, z: depth / 2 - legThickness },
    ];

    for (const pos of legPositions) {
        const leg = new THREE.Mesh(legGeometry, legMaterial);
        leg.position.set(pos.x, legHeight / 2, pos.z);
        leg.castShadow = true;
        deskGroup.add(leg);
    }

    // Position desk
    deskGroup.position.set(0, 0.26, 0.4);

    scene.add(deskGroup);
    return deskGroup;
}

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

    // Screen material - BasicMaterial ignores scene lighting, shows texture as-is
    // We'll blend the texture with a glow color controlled by the slider
    screenMaterial = new THREE.ShaderMaterial({
        uniforms: {
            map: { value: texture },
            glowIntensity: { value: 0.5 },
            glowColor: { value: new THREE.Color(0x4488cc) },
        },
        vertexShader: `
            varying vec2 vUv;
            void main() {
                vUv = uv;
                gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
            }
        `,
        fragmentShader: `
            uniform sampler2D map;
            uniform float glowIntensity;
            uniform vec3 glowColor;
            varying vec2 vUv;
            void main() {
                vec4 texColor = texture2D(map, vUv);
                vec3 glow = glowColor * glowIntensity;
                gl_FragColor = vec4(texColor.rgb + glow, 1.0);
            }
        `,
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

    // Position monitor behind keyboard, facing Wendy
    monitorMesh.position.set(0, 0.71, 0.55);
    monitorMesh.rotation.x = 0.02;
    monitorMesh.rotation.y = Math.PI;

    scene.add(monitorMesh);

    // Add monitor glow light - illuminates Wendy from the screen
    // Main light - bright center glow
    monitorLight = new THREE.PointLight(0x6699cc, 3, 2.5);  // Blue-white, bright
    monitorLight.position.set(-0.06, 0.55, 0.4);  // In front of monitor
    monitorLight.castShadow = true;
    monitorLight.shadow.mapSize.width = 512;
    monitorLight.shadow.mapSize.height = 512;
    monitorLight.shadow.bias = -0.001;
    scene.add(monitorLight);

    // Secondary fill light - softer spread
    const monitorFill = new THREE.PointLight(0x4477aa, 1.5, 2);
    monitorFill.position.set(0, 0.81, 0.35);
    scene.add(monitorFill);

    // Spot light for dramatic forward projection
    const monitorSpot = new THREE.SpotLight(0x6699cc, 2, 3, Math.PI / 4, 0.5);
    monitorSpot.position.set(0, 0.71, 0.50);
    monitorSpot.target.position.set(0, 0.55, 0);  // Aim at Wendy's chest/hands
    scene.add(monitorSpot);
    scene.add(monitorSpot.target);

    // Initialize the Monitor renderer with the canvas and texture
    monitor = new Monitor(canvas, texture);

    // Expose for debugging
    window.getMonitor = () => monitor;
    window.getMonitorLight = () => monitorLight;
}

// =============================================================================
// Object Debug Menu
// =============================================================================

let objectDebugVisible = false;

window.toggleObjectDebug = () => {
    objectDebugVisible = !objectDebugVisible;
    const panel = document.getElementById('object-debug-panel');
    panel.classList.toggle('hidden', !objectDebugVisible);

    if (objectDebugVisible) {
        buildObjectDebugUI();
    }
};

function buildObjectDebugUI() {
    const container = document.getElementById('object-list');
    container.innerHTML = '';

    for (const [key, data] of Object.entries(sceneObjects)) {
        const obj = data.obj;
        if (!obj) continue;

        const item = document.createElement('div');
        item.className = 'object-item';
        item.innerHTML = `
            <div class="object-item-header">${data.name}</div>
            <div class="object-controls">
                <label>Pos X</label>
                <input type="range" data-key="${key}" data-prop="px" min="-2" max="2" step="0.01" value="${obj.position.x}">
                <span class="value" id="${key}-px-val">${obj.position.x.toFixed(2)}</span>

                <label>Pos Y</label>
                <input type="range" data-key="${key}" data-prop="py" min="-1" max="2" step="0.01" value="${obj.position.y}">
                <span class="value" id="${key}-py-val">${obj.position.y.toFixed(2)}</span>

                <label>Pos Z</label>
                <input type="range" data-key="${key}" data-prop="pz" min="-2" max="2" step="0.01" value="${obj.position.z}">
                <span class="value" id="${key}-pz-val">${obj.position.z.toFixed(2)}</span>

                <label>Rot X</label>
                <input type="range" data-key="${key}" data-prop="rx" min="-3.14" max="3.14" step="0.01" value="${obj.rotation.x}">
                <span class="value" id="${key}-rx-val">${obj.rotation.x.toFixed(2)}</span>

                <label>Rot Y</label>
                <input type="range" data-key="${key}" data-prop="ry" min="-3.14" max="3.14" step="0.01" value="${obj.rotation.y}">
                <span class="value" id="${key}-ry-val">${obj.rotation.y.toFixed(2)}</span>

                <label>Rot Z</label>
                <input type="range" data-key="${key}" data-prop="rz" min="-3.14" max="3.14" step="0.01" value="${obj.rotation.z}">
                <span class="value" id="${key}-rz-val">${obj.rotation.z.toFixed(2)}</span>
            </div>
        `;
        container.appendChild(item);
    }

    // Add event listeners to all sliders
    container.querySelectorAll('input[type="range"]').forEach(slider => {
        slider.addEventListener('input', (e) => {
            const key = e.target.dataset.key;
            const prop = e.target.dataset.prop;
            const value = parseFloat(e.target.value);
            const obj = sceneObjects[key]?.obj;

            if (!obj) return;

            switch (prop) {
                case 'px': obj.position.x = value; break;
                case 'py': obj.position.y = value; break;
                case 'pz': obj.position.z = value; break;
                case 'rx': obj.rotation.x = value; break;
                case 'ry': obj.rotation.y = value; break;
                case 'rz': obj.rotation.z = value; break;
            }

            document.getElementById(`${key}-${prop}-val`).textContent = value.toFixed(2);
            updateExportOutput();
        });
    });

    updateExportOutput();
}

function updateExportOutput() {
    const output = {};
    for (const [key, data] of Object.entries(sceneObjects)) {
        const obj = data.obj;
        if (!obj) continue;
        output[key] = {
            position: {
                x: parseFloat(obj.position.x.toFixed(3)),
                y: parseFloat(obj.position.y.toFixed(3)),
                z: parseFloat(obj.position.z.toFixed(3)),
            },
            rotation: {
                x: parseFloat(obj.rotation.x.toFixed(3)),
                y: parseFloat(obj.rotation.y.toFixed(3)),
                z: parseFloat(obj.rotation.z.toFixed(3)),
            },
        };
    }
    document.getElementById('object-debug-output').textContent = JSON.stringify(output, null, 2);
}

window.exportScenePositions = () => {
    updateExportOutput();
    const output = document.getElementById('object-debug-output').textContent;
    console.log('Scene Object Positions:\n' + output);
    alert('Scene positions logged to console');
};

window.copyScenePositions = () => {
    const output = document.getElementById('object-debug-output').textContent;
    navigator.clipboard.writeText(output);
};

// Expose for console debugging
window.getSceneObjects = () => sceneObjects;

// =============================================================================
// Start
// =============================================================================

init();
